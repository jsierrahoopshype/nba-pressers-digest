"""
Unattended PC job for NBA pressers v2 (Windows Task Scheduler runs it via
run-presser-pc-job.bat, 30 minutes after each pressers-v2 cron slot).

Each run:
  a) for every quote in the recent outputs that isn't caption-aligned, fetch
     the video's captions locally with yt-dlp and align the quote
     (caption_align.py) to get its exact start/end;
  b) commit the corrected times back to GitHub through the REST contents
     API: the video's .json (timestamp_source "captions-pc"), its re-rendered
     .md, the affected day digests, latest_clips.json, and the docs/ mirror;
  c) write pressers_v2/output/pc_alignment_report.json (Gemini time vs
     aligned time, delta, score; median/p90 delta per cloud run);
  d) render the top 10 clips by news_score with make_presser_clips.py;
  e) log everything to Documents\\presser-clips\\pc-job-log.txt.

The GitHub token is read from %USERPROFILE%\\.nba-pressers\\token (set up by
install-presser-pc-job.bat via --setup-token) and is never logged.
A file that changed on GitHub between read and write is re-fetched and
retried once, then skipped and logged.

    python presser_pc_job.py                 normal run
    python presser_pc_job.py --setup-token   ask for the token and store it
    python presser_pc_job.py --write-task-xml FILE
"""

import argparse
import base64
import getpass
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

REPO = "jsierrahoopshype/nba-pressers-digest"
BRANCH = "main"
API = os.environ.get("NBA_PC_API", "https://api.github.com")
# The token may only ever go to GitHub (or a local test server).
if not re.match(r"^(https://api\.github\.com|http://(127\.0\.0\.1|localhost)(:\d+)?)$", API):
    API = "https://api.github.com"
WORKFLOW_FILE = "pressers-v2.yml"
FALLBACK_CRON_UTC = ["06:15", "14:15", "20:15"]   # used only if the workflow can't be read
DELAY_AFTER_CRON_MIN = 30

HOME = Path(os.environ.get("NBA_PC_HOME") or os.environ.get("USERPROFILE") or Path.home())
WORK = HOME / "Documents" / "nba-pressers-digest-pc"
CLIPS_ROOT = HOME / "Documents" / "presser-clips"
TOKEN_PATH = HOME / ".nba-pressers" / "token"
LOG_PATH = CLIPS_ROOT / "pc-job-log.txt"
MIRROR = WORK / "mirror"                      # local copy of the files this run reads/writes
OUT_PREFIX = "pressers_v2/output"
DOCS_PREFIX = "docs/pressers_v2"
REPORT_NAME = "pc_alignment_report.json"
PC_SOURCE = "captions-pc"
WINDOW_HOURS = 48
RECENT_DAYS = 3
TOP_CLIPS = 10
CLOUD_WAIT_MAX_SECS = 25 * 60
LOG_MAX_BYTES = 2_000_000
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

sys.path.insert(0, str(Path(__file__).resolve().parent))


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except (OSError, ValueError):
        pass   # no console when run hidden
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.is_file() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
            LOG_PATH.replace(LOG_PATH.with_suffix(".old.txt"))
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Token
# --------------------------------------------------------------------------- #

def read_token() -> str:
    try:
        return TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def setup_token() -> int:
    print("Paste your GitHub fine-grained token for nba-pressers-digest and press Enter.")
    print("(It won't show while you paste. Needs: Contents = Read and write, Actions = Read.)")
    token = getpass.getpass("Token: ").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{20,255}", token):
        print("[X] That doesn't look like a GitHub token. Nothing was saved.")
        return 1
    gh = GitHub(token)
    try:
        gh.get_file(f"{OUT_PREFIX}/latest_clips.json")
    except GitHubError as e:
        print(f"[X] GitHub rejected the token ({e}). Nothing was saved.")
        return 1
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token, encoding="utf-8")
    if os.name == "nt":
        # Only the current Windows user may read the token file.
        user = os.environ.get("USERNAME", "")
        if user:
            subprocess.run(["icacls", str(TOKEN_PATH), "/inheritance:r", "/grant:r", f"{user}:F"],
                           capture_output=True)
    print(f"Token saved to {TOKEN_PATH} (outside the repo folder).")
    return 0


# --------------------------------------------------------------------------- #
# GitHub REST
# --------------------------------------------------------------------------- #

class GitHubError(Exception):
    pass


class Conflict(GitHubError):
    """The file changed on GitHub since we read it (409/422 sha mismatch)."""


class GitHub:
    def __init__(self, token: str):
        self.token = token

    def _req(self, method: str, path: str, body: dict | None = None, raw: bool = False):
        url = f"{API}/repos/{REPO}/{path.lstrip('/')}"
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "nba-pressers-pc-job",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode("utf-8")).get("message", "")
            except Exception:
                pass
            if e.code == 404:
                return None
            if e.code == 409 or (e.code == 422 and "sha" in detail.lower()):
                raise Conflict(f"HTTP {e.code} {detail}") from None
            raise GitHubError(f"HTTP {e.code} {detail}".strip()) from None
        except urllib.error.URLError as e:
            raise GitHubError(f"network error: {e.reason}") from None
        return payload if raw else json.loads(payload.decode("utf-8") or "null")

    def get_file(self, repo_path: str) -> tuple:
        """(text, sha) or (None, None) when the file doesn't exist."""
        data = self._req("GET", f"contents/{repo_path}?ref={BRANCH}")
        if not data or not isinstance(data, dict) or data.get("type") != "file":
            return None, None
        content = data.get("content")
        if content is None or data.get("encoding") != "base64":
            # >1 MB files come back without content; fetch the blob instead
            blob = self._req("GET", f"git/blobs/{data['sha']}")
            content = blob.get("content", "")
        return base64.b64decode(content).decode("utf-8"), data["sha"]

    def list_dir(self, repo_path: str) -> list:
        data = self._req("GET", f"contents/{repo_path}?ref={BRANCH}")
        return data if isinstance(data, list) else []

    def put_file(self, repo_path: str, text: str, sha: str | None, message: str) -> str:
        body = {"message": message, "branch": BRANCH,
                "content": base64.b64encode(text.encode("utf-8")).decode("ascii")}
        if sha:
            body["sha"] = sha
        data = self._req("PUT", f"contents/{repo_path}", body)
        return data["content"]["sha"]

    def cloud_run_active(self) -> bool:
        for status in ("in_progress", "queued"):
            data = self._req("GET", f"actions/workflows/{WORKFLOW_FILE}/runs?status={status}&per_page=1")
            if data and data.get("total_count"):
                return True
        return False


def commit_file(gh: GitHub, repo_path: str, text: str, sha: str | None, rebuild=None) -> bool:
    """PUT one file. On a conflict, re-fetch it and retry once (re-applying
    our change via rebuild(fresh_text) when given); otherwise skip + log."""
    msg = f"PC caption alignment: {repo_path}"
    try:
        gh.put_file(repo_path, text, sha, msg)
        return True
    except Conflict:
        log(f"  [commit] {repo_path} changed on GitHub; re-fetching and retrying once")
    fresh, fresh_sha = gh.get_file(repo_path)
    new_text = rebuild(fresh) if (rebuild and fresh is not None) else text
    if new_text is None or new_text == fresh:
        log(f"  [commit] {repo_path}: nothing left to change after re-fetch; skipped")
        return False
    try:
        gh.put_file(repo_path, new_text, fresh_sha, msg)
        return True
    except Conflict:
        log(f"  [commit] {repo_path} changed again; SKIPPED (will retry next run)")
        return False


# --------------------------------------------------------------------------- #
# Alignment
# --------------------------------------------------------------------------- #

def needs_alignment(q: dict) -> bool:
    return q.get("timestamp_source") not in ("captions", PC_SOURCE) and bool(q.get("text"))


def in_window(data: dict, cutoff: datetime) -> bool:
    for key in ("published", "processed_at"):
        try:
            t = datetime.fromisoformat(str(data.get(key) or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if t.tzinfo and t >= cutoff:
            return True
    return False


def apply_alignment(data: dict, aligned: dict) -> int:
    """aligned: {gemini_start_seconds: (start, end, score)} keyed by the
    first-pass time, which identifies the quote even if ranks moved.
    Only quotes still needing alignment are touched."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = 0
    for q in data.get("quotes") or []:
        key = q.get("gemini_start_seconds", q.get("start_seconds"))
        if key in aligned and needs_alignment(q):
            start, end, score = aligned[key]
            q["start_seconds"], q["end_seconds"] = start, end
            q["timestamp_source"] = PC_SOURCE
            q["align_score"] = score
            q["pc_aligned_at"] = stamp
            n += 1
    return n


def percentile(values: list, pct: float) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    k = (len(vals) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (k - lo), 1)


def summarize(entries: list) -> dict:
    by_run = {}
    for e in entries:
        if e.get("aligned"):
            by_run.setdefault(e.get("run_id") or "unknown", []).append(abs(e["delta_seconds"]))
    return {run: {"aligned": len(d), "median_abs_delta": round(statistics.median(d), 1),
                  "p90_abs_delta": percentile(d, 0.9)} for run, d in sorted(by_run.items())}


# --------------------------------------------------------------------------- #
# Scheduler XML
# --------------------------------------------------------------------------- #

def cron_slots_utc(gh: GitHub | None) -> list:
    """HH:MM UTC of each pressers-v2 cron, read from the workflow file."""
    try:
        text, _ = gh.get_file(f".github/workflows/{WORKFLOW_FILE}") if gh else (None, None)
    except GitHubError:
        text = None
    slots = []
    for m in re.finditer(r"cron:\s*'(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*'", text or ""):
        slots.append(f"{int(m.group(2)):02d}:{int(m.group(1)):02d}")
    return slots or list(FALLBACK_CRON_UTC)


def write_task_xml(path: Path, vbs_path: Path) -> list:
    """Task Scheduler XML: one daily trigger per cron slot + 30 min, anchored
    in UTC (so daylight-saving changes don't shift it). Runs only if the PC
    is on and you're logged in; missed runs are not caught up; hidden via
    wscript + a .vbs launcher, so no window appears."""
    slots = cron_slots_utc(GitHub(read_token()))
    today = datetime.now(timezone.utc).date()
    triggers, times = [], []
    for hhmm in slots:
        h, m = (int(x) for x in hhmm.split(":"))
        t = (datetime(today.year, today.month, today.day, h, m, tzinfo=timezone.utc)
             + timedelta(minutes=DELAY_AFTER_CRON_MIN))
        times.append(t.strftime("%H:%M UTC"))
        triggers.append(
            "    <CalendarTrigger>\n"
            f"      <StartBoundary>{t.strftime('%Y-%m-%dT%H:%M:%SZ')}</StartBoundary>\n"
            "      <Enabled>true</Enabled>\n"
            "      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>\n"
            "    </CalendarTrigger>")
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>NBA pressers: align quote times on captions, commit to GitHub, render top clips. Runs 30 minutes after each pressers-v2 cloud run.</Description>
  </RegistrationInfo>
  <Triggers>
{chr(10).join(triggers)}
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT3H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>wscript.exe</Command>
      <Arguments>//B //Nologo "{xml_escape(str(vbs_path))}"</Arguments>
    </Exec>
  </Actions>
</Task>
"""
    path.write_text(xml, encoding="utf-16")
    # Launcher: runs the .bat with window style 0 (hidden) and waits for it.
    bat = vbs_path.parent / "run-presser-pc-job.bat"
    vbs_path.write_text('Set sh = CreateObject("WScript.Shell")\r\n'
                        f'sh.Run """{bat}"" --scheduled", 0, True\r\n', encoding="utf-8")
    return times


# --------------------------------------------------------------------------- #
# The job
# --------------------------------------------------------------------------- #

class Lock:
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and time.time() - self.path.stat().st_mtime < 3 * 3600:
            raise RuntimeError("another PC job run is still going (lock file is fresh)")
        self.path.write_text(str(os.getpid()), encoding="utf-8")
        return self

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


def mirror_outputs(gh: GitHub, cutoff: datetime) -> dict:
    """Copy the recent day folders' .json/.md (+ digests) into MIRROR.
    Returns {repo_path: sha} for every mirrored file."""
    shas = {}
    if MIRROR.exists():
        shutil.rmtree(MIRROR)
    out_dir = MIRROR / "output"
    out_dir.mkdir(parents=True)
    manifest_text, manifest_sha = gh.get_file(f"{OUT_PREFIX}/latest_clips.json")
    days = set()
    if manifest_text:
        (out_dir / "latest_clips.json").write_text(manifest_text, encoding="utf-8")
        shas[f"{OUT_PREFIX}/latest_clips.json"] = manifest_sha
        try:
            days |= {c.get("publish_date") for c in json.loads(manifest_text).get("clips") or []}
        except ValueError:
            pass
    today = datetime.now(timezone.utc).date()
    days |= {(today - timedelta(days=i)).isoformat() for i in range(RECENT_DAYS)}
    listed = {e["name"] for e in gh.list_dir(OUT_PREFIX) if e.get("type") == "dir"}
    for day in sorted(d for d in days if d and DAY_RE.match(d) and d in listed):
        (out_dir / day).mkdir()
        for entry in gh.list_dir(f"{OUT_PREFIX}/{day}"):
            name = entry.get("name", "")
            if entry.get("type") != "file" or not name.endswith((".json", ".md")):
                continue
            text, sha = gh.get_file(f"{OUT_PREFIX}/{day}/{name}")
            if text is not None:
                (out_dir / day / name).write_text(text, encoding="utf-8")
                shas[f"{OUT_PREFIX}/{day}/{name}"] = sha
    return shas


def digest_video_ids(text: str) -> list:
    return re.findall(r"^Source: \[https://www\.youtube\.com/watch\?v=([A-Za-z0-9_-]{11})\]", text, flags=re.M)


def run_job(args) -> int:
    log("=" * 60)
    log("PC job starting")
    token = read_token()
    gh = GitHub(token)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        log("[X] ffmpeg not found. Fix: winget install --id Gyan.FFmpeg -e  (then reboot or log out/in)")
        return 2

    import caption_align
    import make_presser_clips as mc
    import presser_extractor as pe

    committed = 0
    local_manifest = None
    if not token:
        log("[!] No GitHub token (run install-presser-pc-job.bat). Skipping alignment commits; "
            "rendering clips from the public clip list only.")
    else:
        # Don't edit files a cloud run is about to rewrite.
        waited = 0
        try:
            while gh.cloud_run_active() and waited < CLOUD_WAIT_MAX_SECS:
                if waited == 0:
                    log("Cloud pressers-v2 run in progress; waiting for it to finish...")
                time.sleep(60)
                waited += 60
        except GitHubError as e:
            log(f"[!] Couldn't check cloud runs ({e}); continuing. Token needs Actions = Read.")
        if waited >= CLOUD_WAIT_MAX_SECS:
            log("[!] Cloud run still going after 25 min; skipping alignment this time.")
        else:
            try:
                committed, local_manifest = align_and_commit(gh, pe, caption_align, mc, ffmpeg)
            except Exception as e:
                # Never let a GitHub/network problem stop the clip rendering below.
                log(f"[X] alignment/commit step failed: {type(e).__name__}: {e}")
                for line in traceback.format_exc().splitlines()[-6:]:
                    log(f"    {line}")

    # (d) render the top clips (already-rendered ones are skipped by the clipper)
    manifest_arg = str(local_manifest) if local_manifest else mc.MANIFEST_URL
    cmd = [sys.executable, str(Path(mc.__file__).resolve()), "--yes", "--top", str(TOP_CLIPS),
           "--out", str(CLIPS_ROOT), "--manifest", manifest_arg]
    log(f"Rendering top {TOP_CLIPS} clips by news score...")
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                         stdin=subprocess.DEVNULL, timeout=3 * 3600)
    for line in (res.stdout or "").splitlines():
        if line.strip():
            log(f"  clipper | {line.rstrip()}")
    if res.returncode not in (0, None):
        log(f"[!] clipper exited with {res.returncode}: {(res.stderr or '').strip()[-300:]}")
    log(f"PC job finished ({committed} file(s) committed)")
    return 0


def align_and_commit(gh, pe, caption_align, mc, ffmpeg) -> tuple:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    shas = mirror_outputs(gh, cutoff)
    out_dir = MIRROR / "output"
    # Point the pipeline's own renderers at the mirror so .md/digests/manifest
    # come out exactly as the cloud would write them.
    pe.OUTPUT_DIR = out_dir
    pe.LATEST_CLIPS_PATH = out_dir / "latest_clips.json"
    cfg_text, _ = gh.get_file("pressers_v2/config.json")
    pe._CONFIG.clear()
    pe._CONFIG.update(json.loads(cfg_text) if cfg_text else {})

    entries, changed_json, touched_days = [], {}, set()
    for js in sorted(out_dir.glob("*/*.json")):
        if not VIDEO_ID_RE.match(js.stem):
            continue
        data = json.loads(js.read_text(encoding="utf-8"))
        todo = [q for q in data.get("quotes") or [] if needs_alignment(q)]
        if not todo or not in_window(data, cutoff):
            continue
        vid = js.stem
        log(f"{vid}: {len(todo)} quote(s) to align ({(data.get('video_title') or '')[:60]})")
        words = mc.fetch_caption_words(vid, ffmpeg)
        if not words:
            log(f"  {vid}: no captions available from YouTube; left as is")
        aligned = {}
        for q in todo:
            gem = q.get("gemini_start_seconds", q.get("start_seconds"))
            entry = {"video_id": vid, "run_id": data.get("run_id") or "", "rank": q.get("rank"),
                     "text_start": " ".join((q.get("text") or "").split()[:8]),
                     "source_before": q.get("timestamp_source") or "gemini-approx",
                     "gemini_start_seconds": gem, "start_before": q.get("start_seconds"),
                     "aligned": False}
            hit = caption_align.align_quote(q["text"], words, hint_start=gem) if words else None
            if hit:
                entry["score"] = hit["score"]
            if hit and hit["score"] >= caption_align.MIN_ALIGN_SCORE:
                start, end = int(hit["start"]), int(-(-hit["end"] // 1))
                aligned[gem] = (start, end, hit["score"])
                entry.update(aligned=True, aligned_start_seconds=start, aligned_end_seconds=end,
                             delta_seconds=start - int(gem or 0))
            else:
                entry["reason"] = "no captions" if not words else "low match score"
            entries.append(entry)
        if aligned and apply_alignment(data, aligned):
            js.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            video = {"video_id": vid, "title": data.get("video_title") or ""}
            (js.parent / f"{vid}.md").write_text(pe.to_markdown(video, data.get("channel_team") or "", data),
                                                 encoding="utf-8")
            changed_json[f"{OUT_PREFIX}/{js.parent.name}/{js.name}"] = aligned
            touched_days.add(js.parent.name)
            log(f"  {vid}: aligned {len(aligned)}/{len(todo)}")

    files = {}   # repo_path -> new text (only files whose content changed)

    def stage(local: Path):
        rel = local.relative_to(out_dir).as_posix()
        files[f"{OUT_PREFIX}/{rel}"] = local.read_text(encoding="utf-8")

    for repo_path in changed_json:
        day, name = repo_path.split("/")[-2:]
        stage(out_dir / day / name)
        stage(out_dir / day / name.replace(".json", ".md"))
    for day in sorted(touched_days):
        day_dir = out_dir / day
        pe.write_digest_file(day, "digest.md")
        stage(day_dir / "digest.md")
        changed_ids = {Path(p).stem for p in changed_json if f"/{day}/" in p}
        for digest in sorted(day_dir.glob("digest-*.md")):
            ids = digest_video_ids(digest.read_text(encoding="utf-8"))
            if changed_ids & set(ids):
                pe.write_digest_file(day, digest.name, video_ids=ids)
                stage(digest)
    if changed_json:
        old = json.loads((out_dir / "latest_clips.json").read_text(encoding="utf-8")) \
            if (out_dir / "latest_clips.json").is_file() else {}
        manifest = pe.build_clip_manifest(old.get("run_slot") or "pc-job", int(old.get("window_hours") or WINDOW_HOURS))
        (out_dir / "latest_clips.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                                   encoding="utf-8")
        stage(out_dir / "latest_clips.json")

    # (c) report: this run's entries + per-run summary + rolling history
    report_path = f"{OUT_PREFIX}/{REPORT_NAME}"
    old_text, report_sha = gh.get_file(report_path)
    try:
        history = (json.loads(old_text).get("history") or []) if old_text else []
    except ValueError:
        history = []
    deltas = [abs(e["delta_seconds"]) for e in entries if e.get("aligned")]
    run_summary = {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "quotes_checked": len(entries), "aligned": len(deltas),
                   "median_abs_delta": round(statistics.median(deltas), 1) if deltas else None,
                   "p90_abs_delta": percentile(deltas, 0.9) if deltas else None}
    report = {**run_summary, "by_cloud_run": summarize(entries), "entries": entries,
              "history": (history + [run_summary])[-200:]}
    if entries:
        (out_dir / REPORT_NAME).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        files[report_path] = (out_dir / REPORT_NAME).read_text(encoding="utf-8")
        shas[report_path] = report_sha
    if deltas:
        log(f"Alignment: {len(deltas)}/{len(entries)} quote(s) aligned; "
            f"median |delta| {run_summary['median_abs_delta']}s, p90 {run_summary['p90_abs_delta']}s")
    else:
        log(f"Alignment: 0/{len(entries)} quote(s) aligned")

    # (b) commit: JSON first (re-applied on conflict), then derived files,
    # each also mirrored under docs/.
    committed = 0
    order = sorted(files, key=lambda p: (not p.endswith(".json") or p.endswith("latest_clips.json")
                                         or p.endswith(REPORT_NAME), p))
    for repo_path in order:
        text = files[repo_path]
        rebuild = None
        if repo_path in changed_json:
            aligned = changed_json[repo_path]

            def rebuild(fresh, aligned=aligned):
                d = json.loads(fresh)
                return json.dumps(d, ensure_ascii=False, indent=2) if apply_alignment(d, aligned) else None
        if commit_file(gh, repo_path, text, shas.get(repo_path), rebuild):
            committed += 1
            docs_path = DOCS_PREFIX + repo_path[len(OUT_PREFIX):]
            try:
                _, docs_sha = gh.get_file(docs_path)
                if commit_file(gh, docs_path, text, docs_sha):
                    committed += 1
            except GitHubError as e:
                log(f"  [commit] docs mirror {docs_path} failed: {e}")
    return committed, (out_dir / "latest_clips.json") if (out_dir / "latest_clips.json").is_file() else None


def main() -> int:
    ap = argparse.ArgumentParser(description="NBA pressers PC job")
    ap.add_argument("--setup-token", action="store_true")
    ap.add_argument("--write-task-xml", metavar="FILE")
    ap.add_argument("--vbs", metavar="FILE", help="launcher .vbs the task should run")
    ap.add_argument("--scheduled", action="store_true", help="started by Task Scheduler")
    args = ap.parse_args()
    if args.setup_token:
        return setup_token()
    if args.write_task_xml:
        times = write_task_xml(Path(args.write_task_xml), Path(args.vbs or WORK / "run-hidden.vbs"))
        print("Scheduled daily at " + ", ".join(times) + " (30 min after each cloud run).")
        return 0
    try:
        with Lock(WORK / ".pc-job.lock"):
            return run_job(args)
    except RuntimeError as e:
        log(f"[!] {e}; exiting")
        return 0
    except Exception as e:
        log(f"[X] PC job crashed: {type(e).__name__}: {e}")
        for line in traceback.format_exc().splitlines():
            log(f"    {line}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
