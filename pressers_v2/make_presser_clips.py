"""
NBA presser clip maker (Windows and macOS; anything with Python + ffmpeg).

Reads the clip list (pressers_v2/output/latest_clips.json on GitHub, public,
no account needed), shows a numbered pick-list for the latest cloud run and,
for each chosen quote:
  0. finds the REAL start/end of the quote: yt-dlp fetches the video's
     captions and the quote's first/last words are fuzzy-matched to them;
     without captions, faster-whisper transcribes +-90s around the given time.
     A low-confidence match skips the clip (listed at the end) rather than
     cutting the wrong segment.
  1. yt-dlp --download-sections grabs only that slice (+0.5s padding), ONCE
  2. ffmpeg renders each chosen format from that one download:
       vertical  1080x1920  (Reels, TikTok, Shorts)
       youtube   1920x1080  (X, YouTube, Facebook)
       square    1080x1080  (Instagram/Facebook feed)
     Each has its own layout: the original frame fitted with a blurred fill,
     a speaker/team lower third and burned-in captions placed for that shape
     (outside the picture for vertical and square, in the top-left corner and
     along the bottom edge for YouTube).
  3. Saves <clips folder>/<video date>/<pressers|podcasts|oneoffs>/
     <date>_<team>_<speaker>_<videoid>-<start>s_<format>.mp4. The draft
     social post + source link go to <app folder>/notes/<date>/ with the
     same base name, so the clips folder holds nothing but finished clips.

Names depend only on the quote, so a clip that already exists in the folder
(also a shared Google Drive / OneDrive / Dropbox folder somebody else fills)
is never rendered again, per format. Clips are rendered into
<app folder>/tmp and moved into the clips folder when complete (one rename
on the same drive; otherwise copied under a .partial name and renamed), so
nobody sees half-written clips.

Every run deletes files older than keep_days (settings, default 7, 0 = never)
from the clips folder and from <app folder>/notes and /tmp, then removes
empty subfolders. The clips folder must be used for clips only.

App folder: %LOCALAPPDATA%\\NBA Presser Clips (Windows),
~/Library/Application Support/NBA Presser Clips (macOS).

Settings (clips folder, keep_days, default formats) come from settings.json
next to this file, written by the installer. Without it:
<home>/Documents/presser-clips, all three formats and no cleanup.

    (no options)       interactive pick-list
    --url URL          clip the one quote at this timestamped YouTube link
    --formats VYS      formats: any of V (vertical) Y (youtube) S (square)
    --yes              don't ask; top --top clips by news score, default formats
    --pick 1,3,5-7     these numbers from the printed list
    --all              every clip from the last 48 hours
    --type TYPE        top clips of one type: presser / podcast / oneoff
                       (or bare P / D / O)
    --top N            default selection size (10)
    --team TEXT        only clips whose team contains TEXT
    --limit N          only the first N clips
    --force            render again even when the file exists
    --out DIR          save folder (overrides settings.json)
    --manifest X       a local file or another URL instead of GitHub
    --settings FILE    another settings.json
    --no-cleanup       skip the old-file cleanup (the automatic job does it itself)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import caption_align
except ImportError:
    print("[X] caption_align.py is missing. Run the installer again (it downloads it next to "
          "make_presser_clips.py).")
    sys.exit(2)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_RAW = "https://raw.githubusercontent.com/jsierrahoopshype/nba-pressers-digest/main/pressers_v2"
MANIFEST_URL = f"{REPO_RAW}/output/latest_clips.json"
SETTINGS_PATH = HERE / "settings.json"

PAD_SECS = 0.5
DEFAULT_TOP = 10
WHISPER_WINDOW_SECS = 90
WHISPER_MODEL = "base.en"   # ~140 MB, downloaded once on first use
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
URL_LOOKBACK_DAYS = 10      # how far back --url looks for a quote no longer in the 48h list
STALE_PARTIAL_SECS = 6 * 3600
DEFAULT_KEEP_DAYS = 7
APP_NAME = "NBA Presser Clips"

# --------------------------------------------------------------------------- #
# Formats and their layouts
# --------------------------------------------------------------------------- #
# box = where the original 16:9 frame sits (x, y, w, h); the rest is blurred
# fill. Lower third ("lt") and captions ("cap") are positioned per shape:
#   vertical: frame in the middle, lower third just above it, captions just
#             below it (clear of the bottom ~370px that Reels/TikTok cover).
#   square:   frame slightly above centre, lower third in the top band,
#             captions in the bottom band.
#   youtube:  the frame fills the picture; compact lower third in the top-left
#             corner, captions along the bottom edge (where podium and
#             shoulders are, not faces).
FORMATS = {
    "vertical": {"letter": "v", "size": (1080, 1920), "box": (0, 656, 1080, 608),
                 "label": "Vertical 9:16 (Instagram Reels, TikTok, YouTube Shorts)",
                 "lt_pos": (60, 612), "lt_align": 1, "lt_size": 48, "lt_team_size": 32,
                 "cap_align": 8, "cap_margin_v": 1300, "cap_margin_lr": 70, "cap_size": 64,
                 "cap_outline": 5, "cap_shadow": 0, "cap_chars": 30, "cap_words": 6},
    "youtube": {"letter": "y", "size": (1920, 1080), "box": (0, 0, 1920, 1080),
                "label": "YouTube 16:9 (X, YouTube, Facebook)",
                "lt_pos": (56, 52), "lt_align": 7, "lt_size": 40, "lt_team_size": 28,
                "cap_align": 2, "cap_margin_v": 56, "cap_margin_lr": 240, "cap_size": 56,
                "cap_outline": 4, "cap_shadow": 1, "cap_chars": 42, "cap_words": 8},
    "square": {"letter": "s", "size": (1080, 1080), "box": (0, 196, 1080, 608),
               "label": "Square 1:1 (Instagram and Facebook feed)",
               "lt_pos": (50, 160), "lt_align": 1, "lt_size": 40, "lt_team_size": 28,
               "cap_align": 8, "cap_margin_v": 832, "cap_margin_lr": 60, "cap_size": 54,
               "cap_outline": 4, "cap_shadow": 0, "cap_chars": 32, "cap_words": 6},
}
FORMAT_ORDER = ("vertical", "youtube", "square")
LT_BOX_PAD = 14     # BorderStyle 3 box padding around the lower third


def say(msg: str = "") -> None:
    print(msg, flush=True)


def parse_formats(text: str, default: list) -> list:
    """'' -> default; letters V/Y/S in any order or combination ("VS",
    "v, y"), full names, or ALL. Returns names in FORMAT_ORDER."""
    t = (text or "").strip().lower()
    if not t:
        return list(default)
    if t in ("all", "a"):
        return list(FORMAT_ORDER)
    chosen = set()
    for token in re.split(r"[\s,+/]+", t):
        if not token:
            continue
        if token in FORMATS:
            chosen.add(token)
            continue
        for ch in token:
            match = [f for f in FORMATS if FORMATS[f]["letter"] == ch]
            if not match:
                raise ValueError(f"'{ch.upper()}' isn't a format; use V, Y and/or S")
            chosen.add(match[0])
    if not chosen:
        raise ValueError("no format given")
    return [f for f in FORMAT_ORDER if f in chosen]


def format_letters(formats: list) -> str:
    return "".join(FORMATS[f]["letter"].upper() for f in formats)


# --------------------------------------------------------------------------- #
# Settings + tool discovery
# --------------------------------------------------------------------------- #

def app_dir() -> Path:
    """Per-user local folder for notes, logs and temp renders (never the
    clips folder). NBA_PRESSER_APP_DIR overrides it (tests)."""
    env = os.environ.get("NBA_PRESSER_APP_DIR")
    if env:
        return Path(env)
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_NAME


def app_subdir(name: str) -> Path:
    d = app_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- #
# File operations that survive Windows: another process (antivirus, the
# Google Drive / OneDrive client, Explorer's preview) often holds a new file
# for a moment, and Windows then refuses to move or delete it (WinError 32,
# sometimes 5). Every move/delete goes through these helpers, which retry a
# few times with short waits. Files are always closed before they're moved.
# --------------------------------------------------------------------------- #

RETRY_WAITS = (0.1, 0.25, 0.5, 1.0, 2.0)


def _is_busy(e: OSError) -> bool:
    return getattr(e, "winerror", None) in (5, 32, 33) or isinstance(e, PermissionError)


def with_retry(fn, *args):
    for wait in RETRY_WAITS:
        try:
            return fn(*args)
        except FileNotFoundError:
            raise
        except OSError as e:
            if not _is_busy(e):
                raise
            time.sleep(wait)
    return fn(*args)


def remove_file(path: Path) -> bool:
    """Delete a file; True when it's gone (or was never there)."""
    try:
        with_retry(os.remove, str(path))
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


def remove_tree(path: Path) -> None:
    """Best-effort removal of a work folder (retries, never raises)."""
    for wait in RETRY_WAITS + (None,):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if wait is None:
                return
            time.sleep(wait)


def move_file(src: Path, final: Path) -> bool:
    """Move a finished (closed) file to its final name without anyone ever
    seeing it half-written: one rename when both are on the same drive;
    across drives (e.g. C: to a Google Drive G:), copy under a .partial name
    and rename that. False if the final file appeared in the meantime
    (someone else made it); the source is removed either way."""
    final.parent.mkdir(parents=True, exist_ok=True)
    try:
        if final.exists():
            return False
        try:
            with_retry(os.replace, str(src), str(final))
            return True
        except OSError as e:
            if getattr(e, "winerror", None) != 17 and getattr(e, "errno", None) != 18:
                raise                       # 17 / EXDEV: different drive
        part = final.with_name(final.name + ".partial")
        try:
            with_retry(shutil.copyfile, str(src), str(part))
            if final.exists():
                return False
            with_retry(os.replace, str(part), str(final))
            return True
        finally:
            remove_file(part)
    finally:
        remove_file(src)


class WorkDir:
    """A scratch folder under <app folder>/tmp, removed on exit (retrying on
    Windows locks, never failing the clip because cleanup didn't work)."""

    def __init__(self, prefix: str):
        self.prefix = prefix

    def __enter__(self) -> Path:
        self.path = Path(tempfile.mkdtemp(prefix=self.prefix, dir=str(app_subdir("tmp"))))
        return self.path

    def __exit__(self, *exc):
        remove_tree(self.path)


# --------------------------------------------------------------------------- #
# Housekeeping: the clips folder holds finished clips only
# --------------------------------------------------------------------------- #

def log_line(name: str, msg: str) -> None:
    try:
        with (app_subdir("logs") / name).open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def unsafe_clips_folder(path: Path) -> str:
    """Why a folder must not be the clips folder (cleanup deletes everything
    old in it), or ''. Catches the obvious mistakes: a drive root, the home
    folder, Documents, Desktop, Downloads."""
    try:
        p = path.expanduser().resolve()
    except OSError:
        p = path
    home = Path.home().resolve()
    if p == Path(p.anchor):
        return "that's a whole drive"
    risky = {home, home / "Documents", home / "Desktop", home / "Downloads",
             home / "OneDrive", home / "Dropbox", home / "Google Drive", home / "My Drive"}
    if p in risky or p.name.lower() in ("my drive", "shared drives", "onedrive", "dropbox"):
        return "that folder holds other files"
    return ""


def cleanup_old_files(root: Path, days: int, label: str) -> tuple:
    """Delete every file under root last modified more than `days` days ago,
    then the empty subfolders (never root itself). Returns (files, bytes)."""
    if days <= 0 or not root.is_dir() or unsafe_clips_folder(root):
        return 0, 0
    cutoff = time.time() - days * 86400
    count = freed = 0
    walked = []
    for dirpath, dirnames, filenames in os.walk(root):
        # never follow a symlinked folder or a Windows junction out of the clips folder
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))
                       and not getattr(os.path, "isjunction", lambda _: False)(os.path.join(dirpath, d))]
        walked.append(dirpath)
        for name in filenames:
            path = Path(dirpath) / name
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff and remove_file(path):
                count += 1
                freed += st.st_size
                log_line("cleanup-log.txt", f"deleted {path} ({human_size(st.st_size)})")
    for dirpath in reversed(walked):                    # deepest first
        if Path(dirpath) != root:
            try:
                os.rmdir(dirpath)                       # only succeeds when empty
            except OSError:
                pass
    if count:
        msg = f"Cleanup: deleted {count} file(s) older than {days} days from {label}, freed {human_size(freed)}."
        say(msg)
        log_line("cleanup-log.txt", msg)
    return count, freed


def run_cleanup(out_root: Path, days: int) -> tuple:
    total = [0, 0]
    for root, label in ((out_root, f"the clips folder ({out_root})"),
                        (app_dir() / "notes", "the notes folder"), (app_dir() / "tmp", "the temp folder")):
        n, b = cleanup_old_files(root, days, label)
        total[0] += n
        total[1] += b
    return tuple(total)


def tidy_clips_folder(out_root: Path) -> int:
    """Move what isn't a clip out of the clips folder (from earlier versions):
    .txt notes -> <app>/notes/<date>/, logs and the old _made.json ->
    <app>/logs/. Returns how many files moved."""
    if not out_root.is_dir() or unsafe_clips_folder(out_root):
        return 0
    moved = 0
    for path in list(out_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(out_root)
        name = path.name.lower()
        if len(rel.parts) == 1 and (name.startswith("pc-job-log") or name == "_made.json"):
            dest_dir = app_subdir("logs")
        elif path.suffix.lower() == ".txt":
            day = rel.parts[0] if len(rel.parts) > 1 and DAY_RE.match(rel.parts[0]) else "older"
            dest_dir = app_subdir("notes") / day
        else:
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        try:
            if dest.exists():
                if dest.read_bytes() == path.read_bytes():
                    remove_file(path)
                    moved += 1
                    continue
                dest = dest_dir / f"{path.stem}-{uuid.uuid4().hex[:6]}{path.suffix}"
            with_retry(shutil.move, str(path), str(dest))
            moved += 1
        except OSError:
            continue
    if moved:
        say(f"Moved {moved} note/log file(s) out of the clips folder into {app_dir()}.")
        log_line("cleanup-log.txt", f"moved {moved} note/log file(s) from {out_root} to {app_dir()}")
    return moved


def keep_days(settings: dict) -> int:
    """Days to keep files; 0 = never delete. Only an install made by the
    installer (which warns that the folder is for clips only) cleans up:
    without settings.json nothing is ever deleted."""
    if not settings.get("out_dir"):
        return 0
    try:
        return max(0, int(settings.get("keep_days", DEFAULT_KEEP_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_KEEP_DAYS


def default_out_dir() -> Path:
    return Path.home() / "Documents" / "presser-clips"


def load_settings(path: Path = SETTINGS_PATH) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def settings_formats(settings: dict) -> list:
    raw = settings.get("formats")
    if isinstance(raw, list):
        fm = [f for f in FORMAT_ORDER if f in raw]
        if fm:
            return fm
    return list(FORMAT_ORDER)


def tool_dirs(settings: dict | None = None) -> list:
    """Folders that may hold ffmpeg / deno even when PATH hasn't caught up
    (winget and Homebrew installs, deno from pip inside the app's venv)."""
    dirs = []
    ff = (settings or {}).get("ffmpeg") or ""
    if ff and Path(ff).is_file():
        dirs.append(Path(ff).parent)
    dirs.append(Path(sys.executable).parent)      # venv Scripts/ or bin/: deno, yt-dlp
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "WinGet" / "Links")
    else:
        dirs += [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    dirs.append(Path.home() / ".deno" / "bin")
    return [d for d in dirs if d.is_dir()]


def prepare_path(settings: dict | None = None) -> None:
    """Put the tool folders first on PATH for this process and its children
    (yt-dlp looks up ffmpeg and deno on PATH)."""
    current = os.environ.get("PATH", "")
    extra = [str(d) for d in tool_dirs(settings) if str(d) not in current.split(os.pathsep)]
    if extra:
        os.environ["PATH"] = os.pathsep.join(extra + [current])


def find_ffmpeg(settings: dict | None = None) -> str | None:
    prepare_path(settings)
    found = shutil.which("ffmpeg")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if os.name == "nt" and local:
        hits = sorted((Path(local) / "Microsoft" / "WinGet" / "Packages").glob("Gyan.FFmpeg*/*/bin/ffmpeg.exe"))
        if hits:
            return str(hits[-1])
    return None


def ytdlp_cmd() -> list:
    """Run yt-dlp as a module of this same Python, so PATH doesn't matter."""
    return [sys.executable, "-m", "yt_dlp"]


def fix_hint() -> str:
    if os.name == "nt":
        return "Fix: run install-presser-clips.bat again (it repairs the setup)."
    if sys.platform == "darwin":
        return "Fix: run install-presser-clips-mac.command again (it repairs the setup)."
    return "Fix: install ffmpeg and yt-dlp."


def check_tools(settings: dict | None = None) -> str | None:
    ffmpeg = find_ffmpeg(settings)
    if not ffmpeg:
        say("[X] ffmpeg was not found.")
        say("    " + fix_hint())
        return None
    try:
        subprocess.run(ytdlp_cmd() + ["--version"], check=True, capture_output=True, timeout=60)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        say("[X] yt-dlp is not installed for this Python.")
        say("    " + fix_hint())
        return None
    return ffmpeg


# --------------------------------------------------------------------------- #
# Clip list
# --------------------------------------------------------------------------- #

def _ssl_context():
    try:
        import ssl
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def fetch_json(url: str, timeout: int = 30):
    url = url + ("&" if "?" in url else "?") + f"nocache={int(time.time())}"
    req = urllib.request.Request(url, headers={"User-Agent": "nba-presser-clips/2.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_manifest(source: str) -> dict:
    if re.match(r"^https?://", source):
        return fetch_json(source)
    return json.loads(Path(source).read_text(encoding="utf-8"))


def valid_clip(clip: dict) -> str:
    """'' if the manifest entry is usable, otherwise why not."""
    if not VIDEO_ID_RE.match(str(clip.get("video_id") or "")):
        return "bad video_id"
    try:
        start, end = int(clip["start_seconds"]), int(clip["end_seconds"])
    except (KeyError, TypeError, ValueError):
        return "missing start/end seconds"
    if start < 0 or end <= start or end - start > 180:
        return f"bad time range {start}-{end}"
    return ""


# --------------------------------------------------------------------------- #
# A pasted YouTube link -> the digest quote it points at
# --------------------------------------------------------------------------- #

YT_ID_RE = re.compile(r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|live/|embed/)|youtu\.be/)"
                      r"([A-Za-z0-9_-]{11})")
YT_T_RE = re.compile(r"[?&#](?:t|start)=((?:\d+h)?(?:\d+m)?\d+s?|(?:\d+h)?\d+m|\d+h)(?=$|[&#\s])")


def parse_youtube_url(text: str) -> tuple:
    """(video_id, seconds or None), or (None, None) if it isn't a YouTube link."""
    t = (text or "").strip().strip('"').strip("'")
    m = YT_ID_RE.search(t)
    if not m:
        return None, None
    secs = None
    tm = YT_T_RE.search(t)
    if tm:
        parts = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?", tm.group(1))
        if parts:
            h, mi, s = (int(x or 0) for x in parts.groups())
            secs = h * 3600 + mi * 60 + s
    return m.group(1), secs


def nearest_clip(clips: list, video_id: str, secs: int, tolerance: int = 5) -> dict | None:
    same = [c for c in clips if c.get("video_id") == video_id]
    try:
        best = min(same, key=lambda c: abs(int(c.get("start_seconds") or 0) - secs))
    except ValueError:
        return None
    return best if abs(int(best.get("start_seconds") or 0) - secs) <= tolerance else None


def _quote_text(q: dict) -> str:
    blocks = q.get("text_blocks") or []
    if blocks:
        return " ".join(str(b.get("text") or "").strip() for b in blocks if isinstance(b, dict)).strip()
    return str(q.get("quote") or "").strip()


def clip_from_video_json(data: dict, day: str, video_id: str, secs: int, tolerance: int = 5) -> dict | None:
    """Build a clip-list entry from a stored per-video JSON (for quotes no
    longer in the 48-hour list, or whose speaker isn't named)."""
    best, best_gap = None, None
    for q in data.get("quotes") or []:
        try:
            start = int(q.get("start_seconds"))
        except (TypeError, ValueError):
            continue
        gap = abs(start - secs)
        if gap <= tolerance and (best_gap is None or gap < best_gap):
            best, best_gap = q, gap
    if not best:
        return None
    start = int(best["start_seconds"])
    try:
        end = int(best.get("end_seconds"))
    except (TypeError, ValueError):
        end = start + 15
    speaker = str(best.get("speaker") or "").strip()
    if re.match(r"(?i)^(unidentified|unknown)\b", speaker) or speaker.lower() in ("", "speaker", "reporter"):
        speaker = ""
    return {
        "clip_id": f"{video_id}_{start}_{end}", "video_id": video_id,
        "clip_url": f"https://www.youtube.com/watch?v={video_id}&t={start}s",
        "start_seconds": start, "end_seconds": end, "speaker": speaker,
        "team": best.get("team") or data.get("channel_team") or "",
        "text": _quote_text(best), "news_angle": best.get("summary_phrase") or "",
        "social_post": best.get("social_post") or "", "news_score": best.get("news_score"),
        "video_title": data.get("video_title") or "", "publish_date": day,
        "content_type": data.get("content_type") or "presser",
    }


def find_quote_for_url(url: str, clips: list, fetch=fetch_json) -> tuple:
    """(clip, message). Looks in the clip list first, then in the stored
    per-video JSON of the last URL_LOOKBACK_DAYS days."""
    video_id, secs = parse_youtube_url(url)
    if not video_id:
        return None, "that isn't a YouTube link"
    if secs is None:
        return None, "the link has no timestamp; copy the link at the end of a quote in the digest (it ends in &t=...s)"
    hit = nearest_clip(clips, video_id, secs)
    if hit:
        return hit, ""
    today = date.today()
    for back in range(URL_LOOKBACK_DAYS + 1):
        day = (today - timedelta(days=back)).isoformat()
        try:
            data = fetch(f"{REPO_RAW}/output/{day}/{video_id}.json")
        except (urllib.error.URLError, OSError, ValueError):
            continue
        if isinstance(data, dict):
            clip = clip_from_video_json(data, day, video_id, secs)
            if clip:
                return clip, ""
            return None, f"no digest quote starts near {secs}s in that video"
    return None, "that video isn't in the digests of the last few days"


# --------------------------------------------------------------------------- #
# Finding the real start/end of the quote
# --------------------------------------------------------------------------- #

class LowConfidence(Exception):
    """The quote couldn't be located reliably; skip rather than mis-cut."""


_caption_cache: dict = {}
_whisper_model = None


def fetch_caption_words(video_id: str, ffmpeg: str) -> list:
    """English captions (manual or auto) as timed words, via yt-dlp. Cached
    per video. Returns [] when the video has none or the fetch fails."""
    if video_id in _caption_cache:
        return _caption_cache[video_id]
    words = []
    with WorkDir("caps_") as work:
        cmd = ytdlp_cmd() + [
            "--no-playlist", "--quiet", "--no-warnings", "--skip-download",
            "--write-subs", "--write-auto-subs",
            "--sub-langs", "en,en-US,en-GB,en-orig,en.*",
            "--sub-format", "json3/vtt/best",
            "--ffmpeg-location", ffmpeg,
            "-o", str(work / "cap.%(ext)s"),
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
        except subprocess.TimeoutExpired:
            pass
        # json3 first (auto-captions carry per-word timings), then vtt
        for path in sorted(work.glob("cap*.json3")) + sorted(work.glob("cap*.vtt")):
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
                parsed = (caption_align.words_from_json3(raw) if path.suffix == ".json3"
                          else caption_align.words_from_vtt(raw))
            except (ValueError, KeyError):
                continue
            if len(parsed) > len(words):
                words = parsed
    _caption_cache[video_id] = words
    return words


def whisper_words(video_id: str, start: float, end: float, ffmpeg: str) -> list:
    """faster-whisper word timings for [start-90s, end+90s] of the video."""
    global _whisper_model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise LowConfidence("no captions, and faster-whisper isn't installed "
                            "(run the installer again to add it)")
    w0 = max(0.0, start - WHISPER_WINDOW_SECS)
    w1 = end + WHISPER_WINDOW_SECS
    with WorkDir("audio_") as work:
        cmd = ytdlp_cmd() + [
            "--no-playlist", "--no-progress", "--quiet", "--no-warnings",
            "-f", "bestaudio/best",
            "--download-sections", f"*{w0:.2f}-{w1:.2f}",
            "-x", "--audio-format", "m4a",
            "--ffmpeg-location", ffmpeg,
            "-o", str(work / "aud.%(ext)s"),
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=900)
        audio = sorted(p for p in work.glob("aud.*") if p.suffix.lower() not in (".part", ".ytdl"))
        if res.returncode != 0 or not audio:
            raise LowConfidence(f"no captions, and the audio download for Whisper failed: "
                                f"{tail(res.stderr or res.stdout) or 'no file'}")
        if _whisper_model is None:
            say(f"    loading Whisper model {WHISPER_MODEL} (first time downloads ~140 MB)...")
            _whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        segments, _info = _whisper_model.transcribe(str(audio[0]), language="en",
                                                    word_timestamps=True, vad_filter=True)
        words = []
        for seg in segments:
            for w in seg.words or []:
                for tok in caption_align.norm_words(w.word):
                    words.append((w0 + w.start, w0 + w.end, tok))
    return words


def locate_quote(clip: dict, ffmpeg: str) -> tuple:
    """Return (start, end, method, score) for where the quote is actually
    spoken. Raises LowConfidence when neither captions nor Whisper give a
    reliable match."""
    text = clip.get("text") or ""
    hint = float(clip["start_seconds"])
    best_score = None
    words = fetch_caption_words(clip["video_id"], ffmpeg)
    if words:
        hit = caption_align.align_quote(text, words, hint_start=hint)
        if hit and hit["score"] >= caption_align.MIN_ALIGN_SCORE:
            return hit["start"], hit["end"], "captions", hit["score"]
        best_score = hit["score"] if hit else 0.0
        say(f"    captions match too weak ({best_score:.2f}); trying Whisper around {int(hint)}s")
    else:
        say("    no captions for this video; trying Whisper")
    words = whisper_words(clip["video_id"], hint, float(clip["end_seconds"]), ffmpeg)
    hit = caption_align.align_quote(text, words, hint_start=hint)
    if hit and hit["score"] >= caption_align.MIN_ALIGN_SCORE:
        return hit["start"], hit["end"], "whisper", hit["score"]
    scores = [x for x in (best_score, hit["score"] if hit else None) if x is not None]
    raise LowConfidence(f"quote not found reliably (best match {max(scores) if scores else 0:.2f}, "
                        f"need {caption_align.MIN_ALIGN_SCORE})")


# --------------------------------------------------------------------------- #
# Pick-list
# --------------------------------------------------------------------------- #

def parse_pick(spec: str, n: int) -> list:
    """"1,3,5-7" -> [1, 3, 5, 6, 7] (1-based, within 1..n)."""
    out = []
    for part in re.split(r"[,\s]+", spec.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise ValueError(f"not a number or range: {part!r}")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        for i in range(min(a, b), max(a, b) + 1):
            if 1 <= i <= n and i not in out:
                out.append(i)
    return out


CONTENT_TYPES = ("presser", "podcast", "oneoff")
TYPE_LABELS = {"presser": "PRESS CONFERENCES", "podcast": "PODCASTS & SHOWS", "oneoff": "ONE-OFFS"}
TYPE_FOLDERS = {"presser": "pressers", "podcast": "podcasts", "oneoff": "oneoffs"}
TYPE_LETTERS = {"p": "presser", "d": "podcast", "o": "oneoff"}


def clip_type(c: dict) -> str:
    t = c.get("content_type")
    return t if t in CONTENT_TYPES else "presser"   # clips from before content types


def _score(c: dict) -> int:
    try:
        return int(c.get("news_score"))
    except (TypeError, ValueError):
        return 0


def order_clips(manifest: dict, clips: list) -> tuple:
    """Grouped by type (pressers, podcasts, one-offs); inside each group the
    latest run first, then news_score (highest first). Returns
    (ordered clips, latest_run_id, count of clips from the latest run)."""
    latest = manifest.get("latest_run_id") or ""
    if not latest:
        dated = [c for c in clips if c.get("run_id")]
        if dated:
            latest = max(dated, key=lambda c: c.get("processed_at") or "")["run_id"]
    ordered = sorted(clips, key=lambda c: (CONTENT_TYPES.index(clip_type(c)),
                                           not (latest and c.get("run_id") == latest), -_score(c)))
    n_latest = sum(1 for c in clips if latest and c.get("run_id") == latest)
    return ordered, latest, n_latest


def default_selection(ordered: list, latest: str, top: int, ctype: str | None = None) -> list:
    """1-based list numbers of the top N by news_score from the latest run
    (overall, or only one content type). Falls back to all clips when the
    latest run has none of that type."""
    pool = [(i, c) for i, c in enumerate(ordered, start=1) if ctype is None or clip_type(c) == ctype]
    recent = [(i, c) for i, c in pool if latest and c.get("run_id") == latest]
    pool = recent or pool
    best = sorted(pool, key=lambda ic: -_score(ic[1]))[:top]
    return sorted(i for i, _ in best)


def print_pick_list(ordered: list, out_root: Path) -> None:
    current = None
    for i, c in enumerate(ordered, start=1):
        if clip_type(c) != current:
            current = clip_type(c)
            say(f"\n  {TYPE_LABELS[current]}")
            say(f"{'#':>3}  {'score':>5}  {'made':<4}  {'speaker':<24} {'team':<22} angle")
        sc = c.get("news_score")
        made = format_letters(made_formats(out_root, c)) if not valid_clip(c) else ""
        say(f"{i:>3}  {sc if sc is not None else '-':>5}  {made:<4}  {(c.get('speaker') or '?')[:24]:<24} "
            f"{(c.get('team') or '?')[:22]:<22} {(c.get('news_angle') or '')[:70]}")


# --------------------------------------------------------------------------- #
# Naming: one fixed name per quote and format, so anyone filling the same
# (shared) folder skips what already exists.
# --------------------------------------------------------------------------- #

def slug(text: str, fallback: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return s[:40].strip("-") or fallback


def clip_day(clip: dict) -> str:
    for value in (clip.get("publish_date"), str(clip.get("published") or "")[:10]):
        if value and DAY_RE.match(str(value)):
            return str(value)
    return date.today().isoformat()


def clip_dir(out_root: Path, clip: dict) -> Path:
    return out_root / clip_day(clip) / TYPE_FOLDERS[clip_type(clip)]


def clip_base(clip: dict) -> str:
    return (f"{clip_day(clip)}_{slug(clip.get('team'), 'team')}_{slug(clip.get('speaker'), 'speaker')}_"
            f"{clip['video_id']}-{int(clip['start_seconds'])}s")


def output_path(out_root: Path, clip: dict, fmt: str) -> Path:
    return clip_dir(out_root, clip) / f"{clip_base(clip)}_{fmt}.mp4"


def made_formats(out_root: Path, clip: dict) -> list:
    out = []
    for fmt in FORMAT_ORDER:
        try:
            if output_path(out_root, clip, fmt).stat().st_size > 0:
                out.append(fmt)
        except OSError:
            pass
    return out


def note_path(clip: dict) -> Path:
    """Draft post + source link for a quote: <app folder>/notes/<date>/<base>.txt"""
    return app_dir() / "notes" / clip_day(clip) / f"{clip_base(clip)}.txt"


def clean_stale_partials(folder: Path) -> None:
    """Leftovers of a copy that crashed hours ago (never one in progress)."""
    try:
        for p in folder.glob("*.partial"):
            if time.time() - p.stat().st_mtime > STALE_PARTIAL_SECS:
                remove_file(p)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Captions (ASS subtitles, rendered by ffmpeg's libass)
# --------------------------------------------------------------------------- #

def ass_text(text: str) -> str:
    """Neutralise ASS override syntax in untrusted text."""
    t = re.sub(r"\s+", " ", text or "").strip()
    return t.replace("\\", "/").replace("{", "(").replace("}", ")")


def ass_time(secs: float) -> str:
    secs = max(0.0, secs)
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = secs - h * 3600 - m * 60
    return f"{h}:{m:02d}:{s:05.2f}"


def caption_chunks(text: str, max_words: int = 6, max_chars: int = 32) -> list:
    """Split the quote into short on-screen chunks, breaking early at
    sentence ends so chunks read naturally."""
    words = (text or "").split()
    chunks, cur = [], []
    for w in words:
        candidate = " ".join(cur + [w])
        if cur and (len(cur) >= max_words or len(candidate) > max_chars):
            chunks.append(" ".join(cur))
            cur = []
        cur.append(w)
        if len(cur) >= 3 and re.search(r"[.!?]$", w):
            chunks.append(" ".join(cur))
            cur = []
    if cur:
        chunks.append(" ".join(cur))
    return chunks


def build_ass(clip: dict, speech_start: float, speech_end: float, clip_len: float,
              fmt: str = "vertical") -> str:
    L = FORMATS[fmt]
    w, h = L["size"]
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial,{L['cap_size']},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{L['cap_outline']},{L['cap_shadow']},{L['cap_align']},{L['cap_margin_lr']},{L['cap_margin_lr']},{L['cap_margin_v']},1
Style: Lower,Arial,{L['lt_size']},&H00FFFFFF,&H00FFFFFF,&H40000000,&H40000000,-1,0,0,0,100,100,0,0,3,{LT_BOX_PAD},0,{L['lt_align']},60,60,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    speaker = ass_text(clip.get("speaker") or "")
    team = ass_text(clip.get("team") or "")
    if speaker or team:
        if speaker and team:
            lt = f"{speaker.upper()}\\N{{\\fs{L['lt_team_size']}\\b0}}{team}"
        else:
            lt = (speaker or team).upper()
        x, y = L["lt_pos"]
        events.append(
            f"Dialogue: 1,{ass_time(0)},{ass_time(clip_len)},Lower,,0,0,0,,"
            f"{{\\an{L['lt_align']}\\pos({x},{y})}}{lt}"
        )
    chunks = caption_chunks(clip.get("text") or "", L["cap_words"], L["cap_chars"])
    total_chars = sum(len(c) for c in chunks) or 1
    span = max(1.0, speech_end - speech_start)
    t = speech_start
    for c in chunks:
        dur = max(0.6, span * len(c) / total_chars)
        events.append(
            f"Dialogue: 0,{ass_time(t)},{ass_time(min(t + dur, clip_len))},Caption,,0,0,0,,{ass_text(c)}"
        )
        t += dur
    return header + "\n".join(events) + "\n"


# --------------------------------------------------------------------------- #
# Download + render
# --------------------------------------------------------------------------- #

def tail(text: str, n: int = 3) -> str:
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return " | ".join(lines[-n:])[:400]


def download_section(clip: dict, dl_start: float, dl_end: float, work: Path, ffmpeg: str) -> Path:
    url = f"https://www.youtube.com/watch?v={clip['video_id']}"
    cmd = ytdlp_cmd() + [
        "--no-playlist", "--no-progress", "--quiet", "--no-warnings",
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--download-sections", f"*{dl_start:.2f}-{dl_end:.2f}",
        "--force-keyframes-at-cuts",
        "--merge-output-format", "mp4",
        "--ffmpeg-location", ffmpeg,
        "-o", str(work / "src.%(ext)s"),
        url,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    files = sorted(p for p in work.glob("src.*") if p.suffix.lower() not in (".part", ".ytdl"))
    if res.returncode != 0 or not files:
        raise RuntimeError(f"yt-dlp failed: {tail(res.stderr or res.stdout) or 'no file produced'}")
    return files[0]


def filter_graph(fmt: str, ass_name: str) -> str:
    # Background: cheap blur (downscale, box blur, upscale) of the same frame,
    # darkened a touch. Foreground: the original frame fitted inside the
    # format's box, never cropped.
    w, h = FORMATS[fmt]["size"]
    bx, by, bw, bh = FORMATS[fmt]["box"]
    return (
        "[0:v]split=2[bg][fg];"
        f"[bg]scale={w // 4}:{h // 4}:force_original_aspect_ratio=increase,"
        f"crop={w // 4}:{h // 4},boxblur=12:3,scale={w}:{h},"
        "eq=brightness=-0.10[bgb];"
        f"[fg]scale={bw}:{bh}:force_original_aspect_ratio=decrease[fgs];"
        f"[bgb][fgs]overlay={bx}+({bw}-w)/2:{by}+({bh}-h)/2,"
        f"ass={ass_name},setsar=1,format=yuv420p[v]"
    )


def render_format(src: Path, fmt: str, ass_name: str, out_tmp: Path, work: Path, ffmpeg: str) -> None:
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-filter_complex", filter_graph(fmt, ass_name),
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", "30",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_tmp),
    ]
    # cwd=work so the ass= filter gets a bare filename (no drive-letter colon
    # or backslashes to escape inside the filtergraph on Windows).
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", cwd=str(work), timeout=900)
    if res.returncode != 0 or not out_tmp.is_file() or out_tmp.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg failed: {tail(res.stderr) or 'no output'}")


def txt_content(clip: dict) -> str:
    lines = [
        (clip.get("social_post") or "").strip(),
        "",
        f"Source: {clip.get('clip_url') or 'https://www.youtube.com/watch?v=' + clip['video_id']}",
        "",
        "---",
        f"Speaker: {clip.get('speaker') or '(not named in the video)'} ({clip.get('team') or ''})",
        f"Angle: {clip.get('news_angle') or ''}",
        f"Clip: {clip['start_seconds']}s to {clip['end_seconds']}s of {clip.get('video_title') or clip['video_id']}",
        f"Aligned: {clip.get('_aligned', '')}",
        "",
        f"Quote: \"{(clip.get('text') or '').strip()}\"",
        "",
    ]
    return "\n".join(lines)


def make_clip(clip: dict, out_root: Path, formats: list, ffmpeg: str, pad: float = PAD_SECS,
              force: bool = False) -> tuple:
    """Render the chosen formats of one quote from a single download.
    Returns (paths made, formats that already existed)."""
    folder = clip_dir(out_root, clip)
    clean_stale_partials(folder)
    existing = [] if force else [f for f in formats if f in made_formats(out_root, clip)]
    todo = [f for f in formats if f not in existing]
    if not todo:
        return [], existing
    start, end, method, score = locate_quote(clip, ffmpeg)
    drift = start - float(clip["start_seconds"])
    say(f"    speech found at {start:.1f}-{end:.1f}s via {method} (score {score:.2f}, "
        f"{drift:+.1f}s vs the digest)")
    clip["_aligned"] = f"{start:.1f}s to {end:.1f}s via {method}, score {score:.2f}"
    dl_start = max(0.0, start - pad)
    dl_end = end + pad
    speech_start = start - dl_start
    speech_end = speech_start + (end - start)
    clip_len = dl_end - dl_start

    made = []
    with WorkDir("render_") as work:
        src = download_section(clip, dl_start, dl_end, work, ffmpeg)     # once for every format
        for fmt in todo:
            final = output_path(out_root, clip, fmt)
            if final.exists() and not force:
                existing.append(fmt)          # somebody else made it meanwhile
                continue
            ass_name = f"captions_{fmt}.ass"
            (work / ass_name).write_text(build_ass(clip, speech_start, speech_end, clip_len, fmt),
                                         encoding="utf-8")
            out_tmp = work / f"out_{fmt}.mp4"
            render_format(src, fmt, ass_name, out_tmp, work, ffmpeg)    # ffmpeg has exited: file closed
            if force and final.exists() and not remove_file(final):
                raise RuntimeError(f"can't replace {final.name}: it's open in another program")
            if move_file(out_tmp, final):
                made.append(final)
                say(f"    saved {final.name}")
            else:
                existing.append(fmt)
    note = note_path(clip)
    if made and not note.exists():
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(txt_content(clip), encoding="utf-8")
    return made, existing


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def housekeeping(out_root: Path, days: int) -> None:
    """Every run: move notes/logs out of the clips folder, then delete files
    older than `days` there and in the app's notes/tmp folders. Never fatal."""
    try:
        tidy_clips_folder(out_root)
        run_cleanup(out_root, days)
    except Exception as e:
        say(f"(cleanup skipped: {type(e).__name__}: {e})")


def ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def ask_formats(default: list) -> list:
    say()
    say("Which formats?")
    for f in FORMAT_ORDER:
        say(f"  {FORMATS[f]['letter'].upper()} = {FORMATS[f]['label']}")
    say(f"Press Enter for {format_letters(default)}, or type letters (e.g. VS):")
    while True:
        try:
            return parse_formats(ask("> "), default)
        except ValueError as e:
            say(f"  {e}. Try again.")


def render_all(todo: list, out_root: Path, formats: list, ffmpeg: str, pad: float, force: bool) -> int:
    made, failures, low_conf, already = [], [], [], 0
    for i, clip in enumerate(todo, start=1):
        label = f"{clip.get('speaker') or '?'} ({clip.get('team') or '?'}) {clip.get('video_id')} " \
                f"@{clip.get('start_seconds')}s"
        say(f"[{i}/{len(todo)}] {label}")
        problem = valid_clip(clip)
        if problem:
            say(f"    skipped: {problem}")
            failures.append((label, problem))
            continue
        try:
            new, existing = make_clip(clip, out_root, formats, ffmpeg, pad, force)
        except LowConfidence as e:
            say(f"    SKIPPED (low confidence): {e}")
            low_conf.append((label, str(e)))
            continue
        except Exception as e:
            say(f"    FAILED: {e}")
            failures.append((label, str(e)))
            continue
        if existing:
            already += len(existing)
            say(f"    already in the folder: {', '.join(existing)}")
        made += new

    say()
    say("=" * 60)
    say(f"Done. {len(made)} clip file(s) made, {already} already in the folder, "
        f"{len(low_conf)} quote(s) skipped (low confidence), {len(failures)} failed.")
    say(f"Clips folder: {out_root}")
    say(f"Post text (draft posts + source links): {app_dir() / 'notes'}")
    if low_conf:
        say()
        say("Skipped because the quote couldn't be located reliably (not cut, to avoid a wrong clip):")
        for label, reason in low_conf:
            say(f"  - {label}: {reason}")
    if failures:
        say()
        say("Failed:")
        for label, reason in failures:
            say(f"  - {label}: {reason}")
        if any("Sign in" in r or "JavaScript" in r or "js runtime" in r.lower() for _, r in failures):
            say()
            say("Tip: YouTube refused the download. " + fix_hint())
    return 0 if made or not todo or low_conf or not failures else 1


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="Make presser clips (vertical, YouTube, square) from the digest")
    ap.add_argument("--manifest", default=MANIFEST_URL)
    ap.add_argument("--settings", default=str(SETTINGS_PATH))
    ap.add_argument("--out", default="")
    ap.add_argument("--formats", default="", help="V, Y and/or S (default: from settings, else all three)")
    ap.add_argument("--url", default="", help="clip the quote at this timestamped YouTube link")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--team", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--pad", type=float, default=PAD_SECS)
    ap.add_argument("--all", action="store_true", help="every clip from the last 48 hours")
    ap.add_argument("--pick", default="", help="numbers from the list, e.g. 1,3,5-7")
    ap.add_argument("--top", type=int, default=DEFAULT_TOP, help="default selection size")
    ap.add_argument("--yes", action="store_true", help="don't ask; use the default selection")
    ap.add_argument("--type", choices=CONTENT_TYPES, help="default selection from one type only")
    ap.add_argument("--no-cleanup", action="store_true", help="skip the old-file cleanup")
    ap.add_argument("type_letter", nargs="?", default="",
                    help="P = pressers, D = podcasts, O = one-offs (same as --type)")
    args = ap.parse_args(argv)
    if args.type_letter:
        letter = args.type_letter.strip().lower()[:1]
        if letter not in TYPE_LETTERS:
            ap.error("type must be P (pressers), D (podcasts) or O (one-offs)")
        args.type = TYPE_LETTERS[letter]

    settings = load_settings(Path(args.settings))
    out_root = Path(os.path.expanduser(args.out or settings.get("out_dir") or str(default_out_dir())))
    default_formats = settings_formats(settings)
    try:
        fixed_formats = parse_formats(args.formats, default_formats) if args.formats else None
    except ValueError as e:
        ap.error(str(e))
    interactive = sys.stdin.isatty() and not (args.yes or args.all or args.pick or args.type or args.url)

    if not args.no_cleanup:
        housekeeping(out_root, keep_days(settings))

    ffmpeg = check_tools(settings)
    if not ffmpeg:
        return 2

    say("Downloading the clip list...")
    try:
        manifest = load_manifest(args.manifest)
    except Exception as e:
        say(f"[X] Could not load the clip list: {e}")
        return 2
    clips = manifest.get("clips") or []
    for c in clips:
        c.setdefault("clip_id", f"{c.get('video_id')}_{c.get('start_seconds')}_{c.get('end_seconds')}")
    say(f"Clip list updated {manifest.get('generated_at', '?')}: {len(clips)} quote(s) from the last "
        f"{manifest.get('window_hours', 48)} hours. Saving to: {out_root}")

    if args.url:
        clip, why = find_quote_for_url(args.url, clips)
        if not clip:
            say(f"[X] Can't clip that link: {why}.")
            return 1
        return render_all([clip], out_root, fixed_formats or default_formats, ffmpeg, args.pad, args.force)

    if args.team:
        clips = [c for c in clips if args.team.lower() in str(c.get("team") or "").lower()]
    ordered, latest, n_latest = order_clips(manifest, clips)

    if not interactive:
        if args.all:
            chosen = ordered
        elif args.pick:
            chosen = [ordered[i - 1] for i in parse_pick(args.pick, len(ordered))]
        else:
            chosen = [ordered[i - 1] for i in default_selection(ordered, latest, args.top, args.type)]
        if args.limit > 0:
            chosen = chosen[:args.limit]
        formats = fixed_formats or default_formats
        say(f"{len(chosen)} quote(s) chosen; formats: {', '.join(formats)}")
        return render_all(chosen, out_root, formats, ffmpeg, args.pad, args.force)

    # Interactive: the latest run's quotes first; MORE shows the whole 48 hours.
    view = [c for c in ordered if latest and c.get("run_id") == latest] or ordered
    showing_all = len(view) == len(ordered)
    while True:
        if not view:
            say("No clips in the list right now.")
        else:
            say()
            say("Quotes from the latest cloud run" if not showing_all else
                f"All quotes from the last {manifest.get('window_hours', 48)} hours")
            print_pick_list(view, out_root)
        say()
        say(f"Enter = top {args.top} by score   P / D / O = top {args.top} pressers / podcasts / one-offs")
        say("Numbers = those quotes (e.g. 1,3,5-7)   ALL = every quote above"
            + ("" if showing_all else "   MORE = show the last 48 hours"))
        say("Or paste a timestamped YouTube link from the digest to clip that one quote.   Q = quit")
        answer = ask("> ").strip()
        low = answer.lower()
        chosen = []
        if low in ("q", "quit", "exit"):
            return 0
        if low == "more" and not showing_all:
            view, showing_all = ordered, True
            continue
        if parse_youtube_url(answer)[0]:
            clip, why = find_quote_for_url(answer, clips)
            if not clip:
                say(f"  Can't clip that link: {why}.")
                continue
            say(f"  Found: {clip.get('speaker') or 'speaker not named'} ({clip.get('team') or '?'}): "
                f"{(clip.get('news_angle') or clip.get('text') or '')[:90]}")
            chosen = [clip]
        elif not view:
            continue
        elif not answer:
            chosen = [view[i - 1] for i in default_selection(view, latest, args.top)]
        elif low == "all":
            chosen = list(view)
        elif low in TYPE_LETTERS:
            nums = default_selection(view, latest, args.top, TYPE_LETTERS[low])
            chosen = [view[i - 1] for i in nums if clip_type(view[i - 1]) == TYPE_LETTERS[low]]
            if not chosen:
                say("  No quotes of that type in this list.")
                continue
        else:
            try:
                chosen = [view[i - 1] for i in parse_pick(answer, len(view))]
            except ValueError as e:
                say(f"  {e}. Try again.")
                continue
            if not chosen:
                say("  Those numbers aren't on the list. Try again.")
                continue
        break

    if args.limit > 0:
        chosen = chosen[:args.limit]
    formats = fixed_formats or ask_formats(default_formats)
    say(f"\n{len(chosen)} quote(s), formats: {', '.join(formats)}\n")
    return render_all(chosen, out_root, formats, ffmpeg, args.pad, args.force)


if __name__ == "__main__":
    sys.exit(main())
