"""
Local presser clipper (Windows-first, runs anywhere with Python + ffmpeg).

Downloads pressers_v2/output/latest_clips.json from GitHub, prints a numbered
pick-list (news score, speaker, team, angle) and, for each chosen clip:
  0. finds the REAL start/end of the quote: yt-dlp fetches the video's
     captions and the quote's first/last words are fuzzy-matched to them;
     without captions, faster-whisper transcribes +-90s around the given time.
     A low-confidence match skips the clip (listed at the end) rather than
     cutting the wrong segment.
  1. yt-dlp --download-sections grabs only that slice (+0.5s padding each side)
  2. ffmpeg renders a 1080x1920 vertical MP4: blurred-background fill, the
     original frame centred, a speaker/team lower third and burned-in
     captions built from the quote text in short chunks.
  3. Saves <out>\\<YYYY-MM-DD>\\<pressers|podcasts|oneoffs>\\
     <date>_<team>_<speaker>_<n>.mp4 plus a matching .txt with the social post
     and source URL.

Clips already made (tracked in <out>\\_made.json) are skipped, so running it
several times a day only renders the new ones. Failed clips are skipped and
listed at the end.

Normally started by double-clicking make-presser-clips.bat, which asks which
clips to render (Enter = top 10 by news score from the latest run). Options:
    P / D / O         (bare argument) top 10 pressers / podcasts / one-offs
    --type TYPE       same, as presser / podcast / oneoff
    --all             render every clip in the list
    --pick 1,3,5-7    render these numbers from the printed list
    --top N           default selection size (10)
    --yes             don't ask; use the default selection
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import caption_align
except ImportError:
    print("[X] caption_align.py is missing. Download it from the same GitHub folder as this script "
          "(pressers_v2/caption_align.py) and put it next to make_presser_clips.py.")
    sys.exit(2)

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

PAD_SECS = 0.5
DEFAULT_TOP = 10
WHISPER_WINDOW_SECS = 90
WHISPER_MODEL = "base.en"   # ~140 MB, downloaded once on first use
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
    with tempfile.TemporaryDirectory(prefix="presser_caps_") as tmp:
        work = Path(tmp)
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
                            "(run make-presser-clips.bat to install it)")
    w0 = max(0.0, start - WHISPER_WINDOW_SECS)
    w1 = end + WHISPER_WINDOW_SECS
    with tempfile.TemporaryDirectory(prefix="presser_audio_") as tmp:
        work = Path(tmp)
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


def print_pick_list(ordered: list, latest: str, done_ids: set) -> None:
    current = None
    for i, c in enumerate(ordered, start=1):
        if clip_type(c) != current:
            current = clip_type(c)
            say(f"\n  {TYPE_LABELS[current]}")
            say(f"{'#':>3}  {'score':>5}  {'':4}  {'speaker':<24} {'team':<22} angle")
        sc = c.get("news_score")
        tag = "NEW " if latest and c.get("run_id") == latest else "    "
        if c.get("clip_id") in done_ids:
            tag = "done"
        say(f"{i:>3}  {sc if sc is not None else '-':>5}  {tag}  {(c.get('speaker') or '?')[:24]:<24} "
            f"{(c.get('team') or '?')[:22]:<22} {(c.get('news_angle') or '')[:70]}")


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
        f"Aligned: {clip.get('_aligned', '')}",
        "",
        f"Quote: \"{(clip.get('text') or '').strip()}\"",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def make_clip(clip: dict, day_dir: Path, today: str, ffmpeg: str, pad: float) -> Path:
    start, end, method, score = locate_quote(clip, ffmpeg)
    drift = start - float(clip["start_seconds"])
    say(f"    speech found at {start:.1f}-{end:.1f}s via {method} (score {score:.2f}, "
        f"{drift:+.1f}s vs manifest)")
    clip["_aligned"] = f"{start:.1f}s to {end:.1f}s via {method}, score {score:.2f}"
    dl_start = max(0.0, start - pad)
    dl_end = end + pad
    speech_start = start - dl_start
    speech_end = speech_start + (end - start)
    clip_len = dl_end - dl_start

    base = f"{today}_{slug(clip.get('team'), 'team')}_{slug(clip.get('speaker'), 'speaker')}"
    day_dir = day_dir / TYPE_FOLDERS[clip_type(clip)]
    day_dir.mkdir(parents=True, exist_ok=True)
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
    ap.add_argument("--pad", type=float, default=PAD_SECS)
    ap.add_argument("--all", action="store_true", help="render every clip in the list")
    ap.add_argument("--pick", default="", help="numbers from the list, e.g. 1,3,5-7")
    ap.add_argument("--top", type=int, default=DEFAULT_TOP, help="default selection size")
    ap.add_argument("--yes", action="store_true", help="don't ask; use the default selection")
    ap.add_argument("--type", choices=CONTENT_TYPES, help="default selection from one type only")
    ap.add_argument("type_letter", nargs="?", default="",
                    help="P = pressers, D = podcasts, O = one-offs (same as --type)")
    args = ap.parse_args()
    if args.type_letter:
        letter = args.type_letter.strip().lower()[:1]
        if letter not in TYPE_LETTERS:
            ap.error("type must be P (pressers), D (podcasts) or O (one-offs)")
        args.type = TYPE_LETTERS[letter]

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
    for c in clips:
        c.setdefault("clip_id", f"{c.get('video_id')}_{c.get('start_seconds')}_{c.get('end_seconds')}")
    done_ids = {cid for cid, rel in registry.items() if (out_root / rel).is_file()}

    ordered, latest, n_latest = order_clips(manifest, clips)
    if not ordered:
        say("No clips in the list.")
        return 0
    say()
    print_pick_list(ordered, latest, done_ids)
    say()
    if args.all:
        chosen = list(range(1, len(ordered) + 1))
    elif args.pick:
        chosen = parse_pick(args.pick, len(ordered))
    elif args.type or args.yes or not sys.stdin.isatty():
        chosen = default_selection(ordered, latest, args.top, args.type)
    else:
        say(f"Enter = top {args.top} overall by news score from the latest run"
            + (f" ({latest})" if n_latest else ""))
        say(f"P = top {args.top} pressers, D = top {args.top} podcasts, O = top {args.top} one-offs")
        say("ALL = every clip, or type numbers like 1,3,5-7")
        while True:
            answer = input("> ").strip()
            if not answer:
                chosen = default_selection(ordered, latest, args.top)
            elif answer.lower() == "all":
                chosen = list(range(1, len(ordered) + 1))
            elif answer.lower() in TYPE_LETTERS:
                chosen = default_selection(ordered, latest, args.top, TYPE_LETTERS[answer.lower()])
                if not chosen:
                    say("  No clips of that type. Try again.")
                    continue
            else:
                try:
                    chosen = parse_pick(answer, len(ordered))
                except ValueError as e:
                    say(f"  {e}. Try again.")
                    continue
            break

    todo = []
    skipped_done = 0
    for i in chosen:
        c = ordered[i - 1]
        if not args.force and c["clip_id"] in done_ids:
            skipped_done += 1
            continue
        todo.append((c["clip_id"], c))
    if args.limit > 0:
        todo = todo[:args.limit]
    say(f"{skipped_done} already made, {len(todo)} to render into {day_dir}")
    say()

    made, failures, low_conf = [], [], []
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
        except LowConfidence as e:
            say(f"    SKIPPED (low confidence): {e}")
            low_conf.append((label, str(e)))
            continue
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
    say(f"Done. {len(made)} clip(s) made, {len(low_conf)} skipped (low confidence), "
        f"{len(failures)} failed, {skipped_done} already made earlier.")
    say(f"Folder: {day_dir}")
    if low_conf:
        say()
        say("Skipped because the quote couldn't be located reliably (not cut, to avoid a wrong clip):")
        for label, reason in low_conf:
            say(f"  - {label}: {reason}")
    if failures:
        say()
        say("Failed clips:")
        for label, reason in failures:
            say(f"  - {label}: {reason}")
        if any("Sign in" in r or "JavaScript" in r or "js runtime" in r.lower() for _, r in failures):
            say()
            say("Tip: YouTube blocks some downloads unless yt-dlp has a JavaScript runtime.")
            say("     Install Deno once in PowerShell:  winget install --id DenoLand.Deno -e")
    return 0 if made or not todo or low_conf else 1


if __name__ == "__main__":
    sys.exit(main())
