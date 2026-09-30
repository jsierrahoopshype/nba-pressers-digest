"""
Local presser clipper (Windows-first, runs anywhere with Python + ffmpeg).

Downloads pressers_v2/output/latest_clips.json from GitHub and, for each clip:
  1. yt-dlp --download-sections grabs only that slice (+2s padding each side)
  2. ffmpeg renders a 1080x1920 vertical MP4: blurred-background fill, the
     original frame centred, a speaker/team lower third and burned-in
     captions built from the quote text in short chunks.
  3. Saves <out>\\<YYYY-MM-DD>\\<date>_<team>_<speaker>_<n>.mp4 plus a
     matching .txt with the social post and source URL.

Clips already made (tracked in <out>\\_made.json) are skipped, so running it
several times a day only renders the new ones. Failed clips are skipped and
listed at the end.

Normally started by double-clicking make-presser-clips.bat. Options:
    --limit N         only the first N clips
    --team TEXT       only clips whose team contains TEXT (e.g. --team celtics)
    --force           re-render clips that were already made
    --manifest X      use a local file or another URL instead of GitHub
    --out DIR         output root (default: Documents\\presser-clips)
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
import urllib.request
from datetime import date
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

MANIFEST_URL = (
    "https://raw.githubusercontent.com/jsierrahoopshype/nba-pressers-digest/"
    "main/pressers_v2/output/latest_clips.json"
)
if os.name == "nt":
    DEFAULT_OUT = Path(r"C:\Users\Jorge Sierra\Documents\presser-clips")
else:
    DEFAULT_OUT = Path.home() / "presser-clips"

PAD_SECS = 2
OUT_W, OUT_H = 1080, 1920
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Layout (16:9 source scaled to 1080x608 sits at y=656..1264).
LOWER_THIRD_X, LOWER_THIRD_Y = 60, 1235      # bottom-left anchor, inside the frame
CAPTION_TOP_Y = 1310                          # captions start just under the frame
CAPTION_MAX_WORDS = 6
CAPTION_MAX_CHARS = 32


def say(msg: str = "") -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# Tool checks
# --------------------------------------------------------------------------- #

def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def ytdlp_cmd() -> list:
    """Run yt-dlp as a module of this same Python, so PATH doesn't matter."""
    return [sys.executable, "-m", "yt_dlp"]


def check_tools() -> str | None:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        say("[X] ffmpeg is not installed (or not on PATH).")
        say("    Fix: open PowerShell and run:")
        say("        winget install --id Gyan.FFmpeg -e")
        say("    Then close this window, open a new one and run the clipper again.")
        return None
    try:
        subprocess.run(ytdlp_cmd() + ["--version"], check=True, capture_output=True, timeout=60)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        say("[X] yt-dlp is not installed for this Python.")
        say(f"    Fix: {Path(sys.executable).name} -m pip install --upgrade \"yt-dlp[default]\"")
        return None
    return ffmpeg


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #

def load_manifest(source: str) -> dict:
    if re.match(r"^https?://", source):
        url = source + ("&" if "?" in source else "?") + f"nocache={int(time.time())}"
        req = urllib.request.Request(url, headers={"User-Agent": "presser-clipper/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
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
# Naming + registry
# --------------------------------------------------------------------------- #

def slug(text: str, fallback: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return s[:40].strip("-") or fallback


def load_registry(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_registry(path: Path, registry: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    tmp.replace(path)


def next_free_stem(day_dir: Path, base: str) -> str:
    n = 1
    while (day_dir / f"{base}_{n}.mp4").exists() or (day_dir / f"{base}_{n}.txt").exists():
        n += 1
    return f"{base}_{n}"


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


def caption_chunks(text: str) -> list:
    """Split the quote into short on-screen chunks (<= ~6 words / 32 chars),
    breaking early at sentence ends so chunks read naturally."""
    words = (text or "").split()
    chunks, cur = [], []
    for w in words:
        candidate = " ".join(cur + [w])
        if cur and (len(cur) >= CAPTION_MAX_WORDS or len(candidate) > CAPTION_MAX_CHARS):
            chunks.append(" ".join(cur))
            cur = []
        cur.append(w)
        if len(cur) >= 3 and re.search(r"[.!?]$", w):
            chunks.append(" ".join(cur))
            cur = []
    if cur:
        chunks.append(" ".join(cur))
    return chunks


def build_ass(clip: dict, speech_start: float, speech_end: float, clip_len: float) -> str:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Arial,70,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,5,0,8,70,70,{CAPTION_TOP_Y},1
Style: Lower,Arial,44,&H00FFFFFF,&H00FFFFFF,&H40000000,&H40000000,-1,0,0,0,100,100,0,0,3,14,0,1,60,60,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    speaker = ass_text(clip.get("speaker") or "")
    team = ass_text(clip.get("team") or "")
    if speaker or team:
        if speaker and team:
            lt = f"{speaker.upper()}\\N{{\\fs32\\b0}}{team}"
        else:
            lt = (speaker or team).upper()
        events.append(
            f"Dialogue: 1,{ass_time(0)},{ass_time(clip_len)},Lower,,0,0,0,,"
            f"{{\\an1\\pos({LOWER_THIRD_X},{LOWER_THIRD_Y})}}{lt}"
        )
    chunks = caption_chunks(clip.get("text") or "")
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


def download_section(clip: dict, dl_start: int, dl_end: int, work: Path, ffmpeg: str) -> Path:
    url = f"https://www.youtube.com/watch?v={clip['video_id']}"
    cmd = ytdlp_cmd() + [
        "--no-playlist", "--no-progress", "--quiet", "--no-warnings",
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--download-sections", f"*{dl_start}-{dl_end}",
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


def render_vertical(src: Path, ass_name: str, out_tmp: Path, work: Path, ffmpeg: str) -> None:
    # Background: cheap blur (downscale, box blur, upscale) of the same frame,
    # darkened a touch. Foreground: the original frame fitted to 1080 wide.
    filt = (
        "[0:v]split=2[bg][fg];"
        f"[bg]scale={OUT_W // 4}:{OUT_H // 4}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W // 4}:{OUT_H // 4},boxblur=12:3,scale={OUT_W}:{OUT_H},"
        "eq=brightness=-0.10[bgb];"
        f"[fg]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease[fgs];"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2,"
        f"ass={ass_name},setsar=1,format=yuv420p[v]"
    )
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-filter_complex", filt,
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


def write_txt(path: Path, clip: dict) -> None:
    lines = [
        (clip.get("social_post") or "").strip(),
        "",
        f"Source: {clip.get('clip_url') or 'https://www.youtube.com/watch?v=' + clip['video_id']}",
        "",
        "---",
        f"Speaker: {clip.get('speaker') or ''} ({clip.get('team') or ''})",
        f"Angle: {clip.get('news_angle') or ''}",
        f"Clip: {clip['start_seconds']}s to {clip['end_seconds']}s of {clip.get('video_title') or clip['video_id']}",
        "",
        f"Quote: \"{(clip.get('text') or '').strip()}\"",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def make_clip(clip: dict, day_dir: Path, today: str, ffmpeg: str, pad: int) -> Path:
    start, end = int(clip["start_seconds"]), int(clip["end_seconds"])
    dl_start = max(0, start - pad)
    dl_end = end + pad
    speech_start = float(start - dl_start)
    speech_end = speech_start + (end - start)
    clip_len = float(dl_end - dl_start)

    base = f"{today}_{slug(clip.get('team'), 'team')}_{slug(clip.get('speaker'), 'speaker')}"
    stem = next_free_stem(day_dir, base)
    with tempfile.TemporaryDirectory(prefix="presser_") as tmp:
        work = Path(tmp)
        src = download_section(clip, dl_start, dl_end, work, ffmpeg)
        (work / "captions.ass").write_text(build_ass(clip, speech_start, speech_end, clip_len), encoding="utf-8")
        out_tmp = work / "out.mp4"
        render_vertical(src, "captions.ass", out_tmp, work, ffmpeg)
        final = day_dir / f"{stem}.mp4"
        shutil.move(str(out_tmp), str(final))
    write_txt(day_dir / f"{stem}.txt", clip)
    return final


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Make vertical presser clips from latest_clips.json")
    ap.add_argument("--manifest", default=MANIFEST_URL)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--team", default="")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--pad", type=int, default=PAD_SECS)
    args = ap.parse_args()

    ffmpeg = check_tools()
    if not ffmpeg:
        return 2

    say("Downloading clip list...")
    try:
        manifest = load_manifest(args.manifest)
    except Exception as e:
        say(f"[X] Could not load the clip list: {e}")
        return 2
    clips = manifest.get("clips") or []
    say(f"Clip list generated {manifest.get('generated_at', '?')}: {len(clips)} clip(s) from the last "
        f"{manifest.get('window_hours', 48)}h.")

    out_root = Path(args.out)
    today = date.today().isoformat()
    day_dir = out_root / today
    day_dir.mkdir(parents=True, exist_ok=True)
    registry_path = out_root / "_made.json"
    registry = load_registry(registry_path)

    if args.team:
        clips = [c for c in clips if args.team.lower() in str(c.get("team") or "").lower()]
    todo = []
    skipped_done = 0
    for c in clips:
        cid = c.get("clip_id") or f"{c.get('video_id')}_{c.get('start_seconds')}_{c.get('end_seconds')}"
        if not args.force and cid in registry and (out_root / registry[cid]).is_file():
            skipped_done += 1
            continue
        todo.append((cid, c))
    if args.limit > 0:
        todo = todo[:args.limit]
    say(f"{skipped_done} already made, {len(todo)} to render into {day_dir}")
    say()

    made, failures = [], []
    for i, (cid, clip) in enumerate(todo, start=1):
        label = f"{clip.get('speaker') or '?'} ({clip.get('team') or '?'}) {clip.get('video_id')} " \
                f"@{clip.get('start_seconds')}s"
        say(f"[{i}/{len(todo)}] {label}")
        problem = valid_clip(clip)
        if problem:
            say(f"    skipped: {problem}")
            failures.append((label, problem))
            continue
        try:
            final = make_clip(clip, day_dir, today, ffmpeg, args.pad)
        except Exception as e:
            say(f"    FAILED: {e}")
            failures.append((label, str(e)))
            continue
        registry[cid] = str(final.relative_to(out_root))
        save_registry(registry_path, registry)
        made.append(final)
        say(f"    saved {final.name}")

    say()
    say("=" * 60)
    say(f"Done. {len(made)} clip(s) made, {len(failures)} failed, {skipped_done} already made earlier.")
    say(f"Folder: {day_dir}")
    if failures:
        say()
        say("Failed clips:")
        for label, reason in failures:
            say(f"  - {label}: {reason}")
        if any("Sign in" in r or "JavaScript" in r or "js runtime" in r.lower() for _, r in failures):
            say()
            say("Tip: YouTube blocks some downloads unless yt-dlp has a JavaScript runtime.")
            say("     Install Deno once in PowerShell:  winget install --id DenoLand.Deno -e")
    return 0 if made or not todo else 1


if __name__ == "__main__":
    sys.exit(main())
