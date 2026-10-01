"""
NBA pressers v2: press-conference quotes + clip manifest.

Polls the 30 NBA team YouTube channels via the YouTube Data API, keeps
press conference / postgame / pregame / media availability videos from the
last 48h, and extracts quotes with hoopshype-yt-quotes' own code (vendored
byte for byte in ytq_vendor.py: same prompt, config, MM:SS timestamps,
chunking, splitter and markdown renderer). The only prompt change is the
appended speaker-naming rule.

On top of that, one text-only Gemini call per video (no video input)
derives the clip fields: speaker_confidence, news_score, social_post and
the video's content_type. End time = start + estimated speech duration.

Outputs (pressers_v2/output/):
  <publish-date>/<video_id>.md / .json   yt-quotes format (+ clip fields in the JSON)
  <publish-date>/digest.md, digest-<slot>.md
                                         sections: Press conferences /
                                         Podcasts & shows / One-offs
  latest_clips.json, clips/<date>.json   clip manifest (named speakers only)

Usage (from repo root):
    python pressers_v2/presser_extractor.py
"""

import json
import os
import re
import signal
import sys
import threading
import time
import traceback
import unicodedata
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types

import ytq_vendor as ytq

load_dotenv()

MODEL = ytq.MODEL
ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ytq.OUTPUT_DIR                       # pressers_v2/output (same object yt-quotes code writes to)
CONFIG_PATH = ROOT / "config.json"
LATEST_CLIPS_PATH = OUTPUT_DIR / "latest_clips.json"
CLIPS_ARCHIVE_DIR = OUTPUT_DIR / "clips"
SLACK_PAYLOAD_PATH = ROOT / ".slack_payload.json"
WATCH_URL_TEMPLATE = "https://www.youtube.com/watch?v={video_id}"
YT_API_BASE = "https://www.googleapis.com/youtube/v3"
FORMAT_VERSION = "ytq-1"

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
DAY_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

VIDEO_WORKERS = 3                     # concurrent videos
PER_VIDEO_TIMEOUT_SECS = 12 * 60      # hard upper bound per video
SCRIPT_TIMEOUT_SECS = 35 * 60         # hard wall-clock budget for the whole run
WORDS_PER_SECOND = 2.6                # clip end = start + words / this
MIN_CLIP_SECS = 5

RETRY_CAP = 2  # after this many cap-eligible failures, write FAILED.txt
_CAP_ELIGIBLE_FAILURES = frozenset({"failed-timeout", "failed-hallucination", "failed-other"})

# Process-wide abort flags (same semantics as yt-quotes' main loop).
_spending_cap_hit = threading.Event()
_transient_overload_hit = threading.Event()
_deferred_items: list = []

RUN_ID = ""
# Set once the digest, manifest and Slack payload are on disk. If the
# watchdog fires after that, the run's work is done and it exits 0.
_outputs_written = threading.Event()
_stats = {"extract_calls": 0, "video_seconds": 0, "text_calls": 0}
_stats_lock = threading.Lock()

UNIDENTIFIED_SPEAKER = "Unidentified speaker"

CONTENT_TYPES = ("presser", "podcast", "oneoff")
CONTENT_TYPE_LABELS = {"presser": "Press conferences", "podcast": "Podcasts & shows", "oneoff": "One-offs"}
DEFAULT_CONTENT_TYPE_KEYWORDS = {
    "podcast": ["podcast", "show", "livestream", "live stream", "broadcast", "reaction", "reactions",
                "roundup", "live"],
    "presser": ["press conference", "presser", "media availability", "availability", "media day",
                "postgame", "post-game", "post game", "pregame", "pre-game", "pre game", "shootaround",
                "practice", "interview", "speaks", "talks", "previews", "introductory"],
}
_CONFIG: dict = {}   # set in main(); classification reads its keyword lists


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #

log = ytq.log


def set_output_dir(path: Path) -> None:
    """Point both this module and the vendored yt-quotes code at one folder."""
    global OUTPUT_DIR, LATEST_CLIPS_PATH, CLIPS_ARCHIVE_DIR
    OUTPUT_DIR = ytq.OUTPUT_DIR = Path(path)
    LATEST_CLIPS_PATH = OUTPUT_DIR / "latest_clips.json"
    CLIPS_ARCHIVE_DIR = OUTPUT_DIR / "clips"


def video_id_from_url(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else ""


def extract_one_off_video_id(token: str) -> str:
    token = (token or "").strip()
    if not token:
        return ""
    if VIDEO_ID_RE.fullmatch(token):
        return token
    return video_id_from_url(token)


def parse_extra_videos_env(raw: str) -> list:
    if not raw:
        return []
    return [t.strip() for t in re.split(r"[,\s]+", raw) if t.strip()]


def env_flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def title_keyword_hit(title: str, keywords: list) -> str:
    low = (title or "").lower()
    for kw in keywords:
        if kw.lower() in low:
            return kw
    return ""


def parse_iso(iso: str) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def publish_date(video: dict) -> str:
    return ytq._publish_date(video)


def video_day_dir(video: dict) -> Path:
    return OUTPUT_DIR / publish_date(video)


def determine_run_slot() -> str:
    """digest-06utc.md for scheduled runs, digest-manual-HHMM.md otherwise."""
    event_name = (os.getenv("GITHUB_EVENT_NAME") or "").strip()
    schedule = (os.getenv("GITHUB_EVENT_SCHEDULE") or "").strip()
    if event_name == "schedule" and schedule:
        m = re.match(r"^\s*\S+\s+(\d{1,2})\s+", schedule)
        if m:
            return f"{int(m.group(1)) % 24:02d}utc"
    return f"manual-{datetime.now(timezone.utc).strftime('%H%M')}"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


# --------------------------------------------------------------------------- #
# Speakers, content types, social posts
# --------------------------------------------------------------------------- #

def is_unidentified(speaker: str) -> bool:
    """No usable name: yt-quotes' own unknown check, plus our
    "Unidentified speaker" label from the appended prompt rule."""
    s = (speaker or "").strip()
    return ytq._is_unknown_speaker(s) or s.lower().startswith("unidentified")


def _name_tokens(text: str) -> list:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    return re.findall(r"[a-z0-9]+", t)


def title_names_speaker(title: str, name: str) -> bool:
    """True when the video title itself names this person: the full name
    ("James Harden Media Availability") or, for a 2+ word name, a surname
    of 4+ letters ("Coach Nurse" -> Nick Nurse). Accents and case ignored."""
    name_toks = _name_tokens(name)
    title_toks = _name_tokens(title)
    if not name_toks or not title_toks or is_unidentified(name):
        return False
    n = len(name_toks)
    if any(title_toks[i:i + n] == name_toks for i in range(len(title_toks) - n + 1)):
        return True
    return n >= 2 and len(name_toks[-1]) >= 4 and name_toks[-1] in title_toks


def classify_content_type(title: str, is_one_off: bool = False, gemini_value=None,
                          config: dict | None = None) -> tuple[str, str]:
    """-> (content_type, source). extra_videos are always "oneoff". Otherwise
    the config's content_type_keywords decide, checked in order (podcast
    first, so "Chase Down Podcast Live: Media Day Reactions" is a podcast);
    whole-word, case-insensitive. No keyword -> Gemini's content_type ->
    "presser"."""
    if is_one_off:
        return "oneoff", "extra_videos"
    cfg = config if config is not None else _CONFIG
    keywords = cfg.get("content_type_keywords") or DEFAULT_CONTENT_TYPE_KEYWORDS
    low = (title or "").lower()
    for ctype, words in keywords.items():
        if ctype not in CONTENT_TYPES:
            continue
        for kw in words:
            if re.search(rf"(?<![a-z0-9]){re.escape(kw.lower())}(?![a-z0-9])", low):
                return ctype, f"keyword: {kw}"
    g = str(gemini_value or "").strip().lower()
    if g in ("presser", "podcast"):
        return g, "gemini"
    return "presser", "default"


def video_content_type(data: dict) -> str:
    ctype = data.get("content_type")
    if ctype in CONTENT_TYPES:
        return ctype
    return classify_content_type(data.get("video_title") or "", bool(data.get("is_one_off")))[0]


_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\uFE0F\u200D]+")


def strip_dashes(text: str) -> str:
    out = re.sub(r"\s*[\u2014\u2013\u2012\u2015]\s*", ", ", text or "")
    out = re.sub(r"\s+-{1,2}\s+", ", ", out)
    out = re.sub(r",\s*([,.!?])", r"\1", out)
    return re.sub(r"\s{2,}", " ", out).strip(" ,")


def clean_social_post(text: str) -> str:
    """House rules even if Gemini ignores them: no emojis, hashtags, dashes."""
    out = _EMOJI_RE.sub("", text or "")
    out = re.sub(r"#(\w)", r"\1", out)
    out = strip_dashes(out)
    return re.sub(r"\s{2,}", " ", out).strip()


# --------------------------------------------------------------------------- #
# Pages safety. yt-quotes renders fetched text as-is; GitHub Pages runs it
# through Liquid and kramdown (raw HTML passes). Only the sequences that can
# execute are neutralised, so ordinary quotes render byte-identical to
# yt-quotes.
# --------------------------------------------------------------------------- #

_PAGES_UNSAFE = [
    (re.compile(r"<"), "&lt;"),
    (re.compile(r">"), "&gt;"),
    (re.compile(r"\{\{"), "&#123;&#123;"),
    (re.compile(r"\{%"), "&#123;%"),
    (re.compile(r"\}\}"), "&#125;&#125;"),
    (re.compile(r"%\}"), "%&#125;"),
    (re.compile(r"(?i)javascript:"), "javascript&#58;"),
]


def pages_safe(value):
    if isinstance(value, str):
        for rx, rep in _PAGES_UNSAFE:
            value = rx.sub(rep, value)
        return value
    if isinstance(value, list):
        return [pages_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: pages_safe(v) for k, v in value.items()}
    return value


# kramdown (GitHub Pages) doesn't autolink bare URLs the way GitHub's file
# view does, so the "Source:" line and the timestamped URL that closes each
# quote block are wrapped as [url](url): same visible text, now clickable.
# Only whole lines holding exactly a YouTube watch URL match, so running it
# twice changes nothing.
_BARE_SOURCE_RE = re.compile(r"(?m)^Source: (https://www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{11})$")
_BARE_TS_URL_RE = re.compile(r"(?m)^(https://www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{11}(?:&t=\d+s)?)$")


def link_bare_urls(md: str) -> str:
    md = _BARE_SOURCE_RE.sub(r"Source: [\1](\1)", md)
    return _BARE_TS_URL_RE.sub(r"[\1](\1)", md)


# "Clip it": after the timestamped URL that closes each quote block, a small
# link in the presserclips: scheme the clip tool's installer registers. A
# click renders that quote (video id, start second, quote number) on the
# reader's computer. Built from the block's own header number and URL, so
# it also works on stored files; a block that already has one is skipped.
_QUOTE_HEADER_RE = re.compile(r"^\*\*(\d+)\.")
_TS_LINK_LINE_RE = re.compile(r"^\[https://www\.youtube\.com/watch\?v=([A-Za-z0-9_-]{11})&t=(\d+)s\]"
                              r"\(https://www\.youtube\.com/watch\?v=\1&t=\2s\)$")
CLIP_LINK_LINE_RE = re.compile(r"^<small>\[Clip it\]\(presserclips://clip\?v=[A-Za-z0-9_-]{11}&t=\d+&q=\d+\)</small>$")


def clip_link_line(video_id: str, secs: int, rank: int) -> str:
    return f"<small>[Clip it](presserclips://clip?v={video_id}&t={int(secs)}&q={int(rank)})</small>"


def add_clip_links(md: str) -> str:
    lines = md.split("\n")
    out, rank = [], None
    for i, line in enumerate(lines):
        out.append(line)
        m = _QUOTE_HEADER_RE.match(line)
        if m:
            rank = int(m.group(1))
            continue
        t = _TS_LINK_LINE_RE.match(line)
        if t and rank is not None:
            has_one = i + 2 < len(lines) and lines[i + 1] == "" and CLIP_LINK_LINE_RE.match(lines[i + 2])
            if not has_one:
                out.extend(["", clip_link_line(t.group(1), int(t.group(2)), rank)])
            rank = None
    return "\n".join(out)


def render_markdown(video_id: str, channel_name: str, data: dict) -> str:
    """yt-quotes' to_markdown, unchanged, on a Pages-safe copy of the data,
    with its bare YouTube URLs turned into links (see link_bare_urls) and a
    "Clip it" link after each quote block (see add_clip_links)."""
    return add_clip_links(link_bare_urls(
        ytq.to_markdown(WATCH_URL_TEMPLATE.format(video_id=video_id), channel_name, pages_safe(data))))


# --------------------------------------------------------------------------- #
# State: filesystem-as-database
# --------------------------------------------------------------------------- #

def iter_day_dirs() -> list:
    if not OUTPUT_DIR.exists():
        return []
    return sorted(
        (p for p in OUTPUT_DIR.iterdir() if p.is_dir() and DAY_DIR_RE.match(p.name)),
        reverse=True,
    )


def find_existing_artifact(video_id: str) -> Path | None:
    """A video counts as processed when its .md AND .json exist non-empty,
    or a SKIPPED-too-long / FAILED.txt marker exists, in any date folder.
    """
    for day_dir in iter_day_dirs():
        md = day_dir / f"{video_id}.md"
        js = day_dir / f"{video_id}.json"
        try:
            if md.is_file() and js.is_file() and md.stat().st_size > 0 and js.stat().st_size > 0:
                return md
        except OSError:
            pass
        for marker_name in (f"{video_id}.SKIPPED-too-long", f"{video_id}.FAILED.txt"):
            marker = day_dir / marker_name
            if marker.is_file():
                return marker
    return None


def clear_video_markers(video_id: str) -> list:
    """Force-rerun helper: delete FAILED / ATTEMPTS / SKIPPED markers for one
    video so it gets a clean slate. The .md/.json are left in place and are
    overwritten when the rerun succeeds (kept if it fails)."""
    cleared = []
    for day_dir in iter_day_dirs():
        for name in (f"{video_id}.FAILED.txt", f"{video_id}.ATTEMPTS.txt", f"{video_id}.SKIPPED-too-long"):
            marker = day_dir / name
            try:
                if marker.is_file():
                    marker.unlink()
                    cleared.append(f"{day_dir.name}/{name}")
            except OSError:
                pass
    return cleared


def _attempts_path(day_dir: Path, video_id: str) -> Path:
    return day_dir / f"{video_id}.ATTEMPTS.txt"


def record_failed_attempt(day_dir: Path, video_id: str, status: str) -> int:
    if status not in _CAP_ELIGIBLE_FAILURES:
        return 0
    day_dir.mkdir(parents=True, exist_ok=True)
    try:
        attempts = int(_attempts_path(day_dir, video_id).read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        attempts = 0
    attempts += 1
    try:
        _attempts_path(day_dir, video_id).write_text(str(attempts), encoding="utf-8")
    except OSError:
        pass
    if attempts >= RETRY_CAP:
        failed_path = day_dir / f"{video_id}.FAILED.txt"
        if not failed_path.exists():
            try:
                failed_path.write_text(
                    f"persistent failure after {attempts} attempts; last status: {status}\n",
                    encoding="utf-8",
                )
            except OSError:
                pass
    return attempts


# --------------------------------------------------------------------------- #
# YouTube Data API v3
# --------------------------------------------------------------------------- #

class QuotaExceeded(Exception):
    """Raised when a YouTube Data API call returns 403 quotaExceeded."""


def youtube_api_get(endpoint: str, params: dict, api_key: str) -> dict:
    try:
        resp = requests.get(f"{YT_API_BASE}/{endpoint}", params={**params, "key": api_key}, timeout=20)
    except requests.RequestException as e:
        # requests' messages include the full URL, which carries the key.
        raise RuntimeError(f"YouTube API {endpoint} request failed: {type(e).__name__}") from None
    if resp.status_code == 403:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        errors = body.get("error", {}).get("errors", [])
        if any(e.get("reason") in ("quotaExceeded", "rateLimitExceeded") for e in errors):
            raise QuotaExceeded(body.get("error", {}).get("message", "quotaExceeded"))
    if not resp.ok:
        # Never let requests' default message leak the full URL (it holds the key).
        raise RuntimeError(f"YouTube API {endpoint} returned HTTP {resp.status_code}")
    return resp.json()


def get_uploads_playlist_id(channel_id: str, api_key: str) -> str:
    data = youtube_api_get("channels", {"part": "contentDetails", "id": channel_id}, api_key)
    items = data.get("items", [])
    if not items:
        return ""
    return items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")


def list_recent_uploads(playlist_id: str, api_key: str, max_results: int) -> list:
    """Returns [{video_id, title, published}] from the uploads playlist."""
    data = youtube_api_get(
        "playlistItems",
        {"part": "snippet,contentDetails", "playlistId": playlist_id,
         "maxResults": max(1, min(50, max_results))},
        api_key,
    )
    out = []
    for item in data.get("items", []):
        details = item.get("contentDetails", {}) or {}
        snippet = item.get("snippet", {}) or {}
        vid = details.get("videoId") or ""
        if VIDEO_ID_RE.match(vid):
            out.append({
                "video_id": vid,
                "title": snippet.get("title", ""),
                "published": details.get("videoPublishedAt") or snippet.get("publishedAt", ""),
            })
    return out


def parse_iso8601_duration(s: str) -> int:
    if not s:
        return 0
    m = re.match(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$", s)
    if not m:
        return 0
    days, hours, mins, secs = (int(x) if x else 0 for x in m.groups())
    return days * 86400 + hours * 3600 + mins * 60 + secs


def hydrate_video_metadata(video_ids: list, api_key: str) -> dict:
    """Batch videos.list (50 per call). Keyed by the response item id."""
    out = {}
    unique_ids = list(dict.fromkeys(video_ids))
    for i in range(0, len(unique_ids), 50):
        chunk = unique_ids[i:i + 50]
        data = youtube_api_get(
            "videos", {"part": "contentDetails,snippet", "id": ",".join(chunk)}, api_key,
        )
        for item in data.get("items", []):
            item_id = (item.get("id") or "").strip()
            if not VIDEO_ID_RE.match(item_id):
                continue
            snippet = item.get("snippet", {}) or {}
            details = item.get("contentDetails", {}) or {}
            out[item_id] = {
                "title": snippet.get("title", ""),
                "channel_title": snippet.get("channelTitle", ""),
                "channel_id": snippet.get("channelId", ""),
                "duration": parse_iso8601_duration(details.get("duration", "")),
                "published_at": snippet.get("publishedAt", ""),
                "live": snippet.get("liveBroadcastContent", "none"),
            }
    return out


# --------------------------------------------------------------------------- #
# Gemini: call counting, shared retry, text-only clip-field call
# --------------------------------------------------------------------------- #

def _count_gemini_calls(client) -> None:
    """Wrap client.models.generate_content to count calls and seconds of
    video sent (for the run summary line). Behaviour is unchanged."""
    models = client.models
    original = models.generate_content

    def counted(*args, **kwargs):
        contents = kwargs.get("contents") or (args[1] if len(args) > 1 else [])
        part = contents[0] if contents else None
        is_video = getattr(getattr(part, "file_data", None), "file_uri", None) is not None
        with _stats_lock:
            if is_video:
                _stats["extract_calls"] += 1
                vm = getattr(part, "video_metadata", None)
                secs = None
                if vm is not None and vm.start_offset is not None and vm.end_offset is not None:
                    secs = int(float(vm.end_offset.rstrip("s")) - float(vm.start_offset.rstrip("s")))
                _stats["video_seconds"] += secs if secs is not None else getattr(_tl, "duration", 0)
            else:
                _stats["text_calls"] += 1
        return original(*args, **kwargs)

    try:
        models.generate_content = counted
    except AttributeError:
        pass


_tl = threading.local()   # per-thread: duration of the video being extracted


def with_retry(fn, label: str):
    """Same backoff and error classes as yt-quotes' call_gemini_with_retry."""
    backoffs = [5, 15, 45, 120, 300]
    for attempt, sleep_s in enumerate(backoffs, start=1):
        try:
            return fn()
        except Exception as e:
            if ytq._is_spending_cap_error(e):
                raise ytq.SpendingCapExhausted(str(e)) from e
            if not ytq._is_transient_gemini_error(e):
                raise
            if attempt < len(backoffs):
                log(f"  [{label}] transient error on attempt {attempt}/5, sleeping {sleep_s}s: {e}")
                time.sleep(sleep_s)
            else:
                if ytq._is_deferred_transient_error(e):
                    raise ytq.TransientServerOverload(str(e)) from e
                raise


CLIP_FIELDS_PROMPT = """You are given quotes already extracted from an NBA video (text only, no video). For each quote, return:
- "rank": the same rank you were given.
- "speaker_confidence": "named" only if the speaker was identified by name in the video or in the video title (the extractor only names speakers it saw identified; "speakers_seen" lists them). "inferred" if the speaker is "Unidentified speaker", empty, or the name looks like a guess.
- "news_score": integer 1-10 for how newsworthy the quote is for an NBA news site. 9-10: injury news, trade or contract news, a public complaint or major admission. 6-8: specific role, rotation or strategy news, a strong opinion on a named player or team. 3-5: interesting but soft. 1-2: generic. Be strict; most quotes are 3-6.
- "social_post": a draft post for X / Bluesky, at most 240 characters. ONE idea; lead with the news, punchy and conversational; may include a short verbatim phrase in quotation marks. NO hashtags, NO emojis, NO em-dashes or en-dashes, no "BREAKING", nothing the speaker did not say.

Also return the video's "content_type": "presser" for a press conference, media availability, media day session, pregame/postgame or shootaround interview; "podcast" for a podcast, talk show, reaction show or hosted livestream/broadcast.

Return ONLY JSON: {"content_type": "presser", "quotes": [{"rank": 1, "speaker_confidence": "named", "news_score": 6, "social_post": "..."}]}"""


def derive_clip_fields(client, data: dict, team: str) -> dict:
    """One text-only call over the extracted quotes. Returns
    {"content_type": ..., "by_rank": {rank: {...}}}; {} on any failure
    (the video keeps its quotes; clip fields fall back to defaults)."""
    quotes = data.get("quotes") or []
    if not quotes:
        return {}
    payload = {
        "video_title": data.get("video_title") or "",
        "channel": team,
        "speakers_seen": data.get("speakers_seen") or [],
        "quotes": [{"rank": q.get("rank"), "speaker": q.get("speaker") or "",
                    "speakers": q.get("speakers") or [],
                    "summary_phrase": q.get("summary_phrase") or "",
                    "text": " ".join(ytq._canonical_quote_text(q).split()[:220])} for q in quotes],
    }
    try:
        response = with_retry(lambda: client.models.generate_content(
            model=MODEL,
            contents=[CLIP_FIELDS_PROMPT, json.dumps(payload, ensure_ascii=False)],
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.3),
        ), "clip-fields")
        parsed = json.loads(response.text or "")
    except ytq.SpendingCapExhausted as e:
        _spending_cap_hit.set()
        log(f"  [clip-fields] [SPENDING CAP] {e}; using defaults")
        return {}
    except Exception as e:
        log(f"  [clip-fields] failed ({type(e).__name__}: {str(e)[:150]}); using defaults")
        return {}
    if not isinstance(parsed, dict):
        return {}
    by_rank = {}
    for item in parsed.get("quotes") or []:
        if isinstance(item, dict) and item.get("rank") is not None:
            by_rank[item["rank"]] = item
    return {"content_type": parsed.get("content_type"), "by_rank": by_rank}


def _news_score(value) -> int | None:
    try:
        return max(1, min(10, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def apply_clip_fields(data: dict, derived: dict, team: str, duration_secs: int) -> None:
    """Add clip fields to each quote dict. The yt-quotes fields themselves
    (timestamp, speaker, quote, text_blocks, ...) are left untouched."""
    title = data.get("video_title") or ""
    seen = set(data.get("speakers_seen") or [])
    by_rank = derived.get("by_rank") or {}
    for q in data.get("quotes") or []:
        text = ytq._canonical_quote_text(q)
        start = ytq.timestamp_to_seconds(q.get("timestamp") or "0:00")
        end = start + max(MIN_CLIP_SECS, int(round(ytq._word_count(text) / WORDS_PER_SECOND)))
        if duration_secs:
            end = min(end, max(duration_secs, start + 1))
        d = by_rank.get(q.get("rank"), {})
        speaker = q.get("speaker") or ""
        if is_unidentified(speaker):
            conf = "inferred"
        elif str(d.get("speaker_confidence") or "").lower() == "named" or title_names_speaker(title, speaker):
            conf = "named"
        elif not d:
            # No clip-field answer: trust the extractor's own rule when the
            # name is among the speakers it saw identified.
            conf = "named" if speaker in seen else "inferred"
        else:
            conf = "inferred"
        q.update({
            "start_seconds": start,
            "end_seconds": end,
            "speaker_confidence": conf,
            "news_score": _news_score(d.get("news_score")),
            "social_post": clean_social_post(" ".join(str(d.get("social_post") or "").split())),
            "team": team,
        })


# --------------------------------------------------------------------------- #
# Per-video processing: yt-quotes' process_video, then the clip fields
# --------------------------------------------------------------------------- #

def process_video(client, video: dict, team: str) -> tuple[str, dict | None]:
    """Run the vendored yt-quotes process_video (it writes the .json/.md),
    then add clip fields + content type and rewrite the .json/.md."""
    is_one_off = bool(video.get("is_one_off"))
    # yt-quotes routes its one-offs to output/oneoffs/; pressers keeps every
    # video in output/<date>/ and marks one-offs with content_type instead.
    ytq_video = {k: v for k, v in video.items() if k != "is_one_off"}
    _tl.duration = int(video.get("duration") or 0)
    status, data = ytq.process_video(client, ytq_video, team, [], [])
    if status != "ok" or not data:
        return status, data

    derived = derive_clip_fields(client, data, team)
    apply_clip_fields(data, derived, team, int(video.get("duration") or 0))
    data["content_type"], data["content_type_source"] = classify_content_type(
        data.get("video_title") or video.get("title") or "", is_one_off, derived.get("content_type"))
    data.update({
        "format": FORMAT_VERSION,
        "video_id": video["video_id"],
        "url": WATCH_URL_TEMPLATE.format(video_id=video["video_id"]),
        "channel_team": team,
        "published": video.get("published") or "",
        "duration_seconds": int(video.get("duration") or 0),
        "processed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": RUN_ID,
        "is_one_off": is_one_off,
    })
    day_dir = video_day_dir(video)
    atomic_write(day_dir / f"{video['video_id']}.json", json.dumps(data, ensure_ascii=False, indent=2))
    atomic_write(day_dir / f"{video['video_id']}.md", render_markdown(video["video_id"], team, data))
    log(f"  {video['video_id']}: {len(data.get('quotes') or [])} quote(s), {data['content_type']} "
        f"({data['content_type_source']})")
    return "ok", data


def process_one_video(client, team: str, video: dict, lock, summary, processed_items,
                      results: list | None = None) -> None:
    video_id = video["video_id"]
    day_dir = video_day_dir(video)
    deferred_record = {"video_id": video_id, "title": video.get("title") or video_id, "channel": team}

    if _spending_cap_hit.is_set():
        log(f"  -> {video_id} [{team}]: skipping (spending cap already aborted this run)")
        with lock:
            summary["aborted-spending-cap"] = summary.get("aborted-spending-cap", 0) + 1
        return
    if _transient_overload_hit.is_set():
        log(f"  -> {video_id} [{team}]: skipping (Gemini overload already deferred this run)")
        with lock:
            summary["deferred-transient"] = summary.get("deferred-transient", 0) + 1
            _deferred_items.append(deferred_record)
        return

    log(f"  -> {video_id} [{team}{' / one-off' if video.get('is_one_off') else ''}]: {video['title'][:80]}")
    inner_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"vid{video_id}")
    future = inner_pool.submit(process_video, client, video, team)
    try:
        try:
            status, data = future.result(timeout=PER_VIDEO_TIMEOUT_SECS)
        except FuturesTimeoutError:
            log(f"  [timeout] video {video_id} exceeded {PER_VIDEO_TIMEOUT_SECS // 60}min, abandoning")
            status, data = "failed-timeout", None
        except ytq.SpendingCapExhausted as e:
            _spending_cap_hit.set()
            log(f"  [SPENDING CAP] {video_id}: {e} — aborting remaining queue this run")
            status, data = "aborted-spending-cap", None
        except ytq.TransientServerOverload as e:
            _transient_overload_hit.set()
            log(f"  [DEFERRED] {video_id}: Gemini overload after retries ({e}) — deferring rest of queue")
            status, data = "deferred-transient", None
        except Exception as e:
            log(f"  unexpected exception processing {video_id}: {e}")
            for line in traceback.format_exc().rstrip().splitlines():
                log(f"    {line}")
            status, data = "failed-other", None
    finally:
        inner_pool.shutdown(wait=False, cancel_futures=True)

    if status == "ok":
        md_path = day_dir / f"{video_id}.md"
        if not md_path.is_file() or md_path.stat().st_size <= 0:
            log(f"  [sanity] {video_id}.md missing/empty after ok; downgrading to failed-other")
            status, data = "failed-other", None

    if status in _CAP_ELIGIBLE_FAILURES:
        n = record_failed_attempt(day_dir, video_id, status)
        log(f"  [attempts] {video_id} now at {n}/{RETRY_CAP} failed attempts")

    log(f"     status [{video_id}]: {status}")
    with lock:
        summary[status] = summary.get(status, 0) + 1
        if status == "ok" and data:
            if results is not None:
                results.append({"video": video, "team": team, "data": data})
            quotes = data.get("quotes") or []
            top = quotes[0] if quotes else {}
            processed_items.append({
                "video_id": video_id,
                "title": data.get("video_title") or video.get("title") or video_id,
                "channel": team,
                "top_quote": ytq._canonical_quote_text(top) if top else "",
                "speaker": "" if is_unidentified(top.get("speaker") or "") else top.get("speaker", ""),
                "social_post": top.get("social_post", ""),
                "clip_count": len(quotes),
                "date": publish_date(video),
                "is_one_off": bool(video.get("is_one_off")),
                "content_type": data.get("content_type") or "presser",
            })
        elif status == "deferred-transient":
            _deferred_items.append(deferred_record)


# --------------------------------------------------------------------------- #
# Outputs: digests (three sections), index, clip manifest, legacy migration
# --------------------------------------------------------------------------- #

def _video_type_for_md(md_path: Path) -> str:
    try:
        return video_content_type(json.loads(md_path.with_suffix(".json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return "presser"


def write_digest_file(date_str: str, output_filename: str, video_ids: list | None = None) -> Path | None:
    """yt-quotes' digest, grouped into Press conferences / Podcasts & shows /
    One-offs (empty sections skipped). Each video block is the per-video .md
    exactly as yt-quotes' write_digest_file includes it (h1 demoted to h2,
    "---" between videos, closing link at the end)."""
    day_dir = OUTPUT_DIR / date_str
    if not day_dir.is_dir():
        return None
    if video_ids is None:
        md_paths = sorted(p for p in day_dir.glob("*.md") if p.is_file() and not p.name.startswith("digest"))
    else:
        md_paths = [day_dir / f"{v}.md" for v in video_ids if (day_dir / f"{v}.md").is_file()]
    sections = {ctype: [] for ctype in CONTENT_TYPES}
    for md_path in md_paths:
        try:
            content = md_path.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if not content or not re.search(r'(?m)^\*\*\d+\.', content):
            continue
        if content.startswith("# "):
            content = "#" + content  # demote h1 to h2 (as yt-quotes does)
        sections[_video_type_for_md(md_path)].append(content)
    if not any(sections.values()):
        return None
    parts = [f"# NBA Pressers — {date_str}", ""]
    first_section = True
    for ctype in CONTENT_TYPES:
        videos = sections[ctype]
        if not videos:
            continue
        if not first_section:
            parts.extend(["---", ""])
        first_section = False
        parts.extend([f"# {CONTENT_TYPE_LABELS[ctype]}", ""])
        for i, content in enumerate(videos):
            if i:
                parts.extend(["---", ""])
            parts.extend([content, ""])
    parts.extend(["---", "", ytq.DIGEST_CLOSING_LINE, ""])
    digest_path = day_dir / output_filename
    atomic_write(digest_path, "\n".join(parts))
    return digest_path


def regenerate_index() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for day_dir in iter_day_dirs():
        digest = day_dir / "digest.md"
        try:
            if digest.is_file() and digest.stat().st_size > 0:
                rows.append(day_dir.name)
        except OSError:
            pass
    lines = ["# NBA Pressers — index", "", "Clip manifest: [latest_clips.json](latest_clips.json)", ""]
    if not rows:
        lines.append("_No videos processed yet._")
    else:
        lines.extend(f"- [{d}]({d}/digest.md)" for d in rows)
    (OUTPUT_DIR / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _legacy_to_ytq(q: dict) -> dict:
    """Quote in the old pressers shape -> yt-quotes shape (+ clip fields)."""
    start = int(q.get("start_seconds") or 0)
    out = {
        "rank": q.get("rank"),
        "speaker": q.get("speaker") or "",
        "timestamp": ytq._seconds_to_timestamp(start),
        "summary_phrase": q.get("news_angle") or "",
        "names_mentioned": q.get("names_mentioned") or [],
        "excerpt": q.get("pull_quote") or "",
        "quote": q.get("text") or "",
    }
    blocks = [{"speaker": b.get("speaker") or "", "text": b.get("text") or ""}
              for b in (q.get("text_blocks") or []) if isinstance(b, dict)]
    if blocks:
        out["quote"] = ""
        out["text_blocks"] = blocks
        out["speakers"] = [b["speaker"] for b in blocks]
    for key in ("start_seconds", "end_seconds", "speaker_confidence", "news_score", "social_post", "team"):
        if key in q:
            out[key] = q[key]
    return out


def migrate_legacy_outputs() -> None:
    """Convert videos stored in the earlier pressers format to the yt-quotes
    format and re-render their .md, so every digest reads the same. No
    Gemini calls; idempotent (files already in the new format are skipped)."""
    touched = set()
    for day_dir in iter_day_dirs():
        for js in day_dir.glob("*.json"):
            if not VIDEO_ID_RE.match(js.stem):
                continue
            try:
                data = json.loads(js.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("format") == FORMAT_VERSION or not isinstance(data, dict):
                continue
            quotes = data.get("quotes") or []
            if quotes and "timestamp" not in quotes[0]:
                data["quotes"] = [_legacy_to_ytq(q) for q in quotes]
            team = data.get("channel_team") or ""
            for q in data["quotes"]:
                q.setdefault("team", team)
                if "speaker_confidence" not in q:
                    sp = q.get("speaker") or ""
                    q["speaker_confidence"] = ("named" if not is_unidentified(sp) and
                                               title_names_speaker(data.get("video_title") or "", sp)
                                               else "inferred")
                if "start_seconds" not in q:
                    q["start_seconds"] = ytq.timestamp_to_seconds(q.get("timestamp") or "0:00")
                if "end_seconds" not in q:
                    words = ytq._word_count(ytq._canonical_quote_text(q))
                    q["end_seconds"] = q["start_seconds"] + max(MIN_CLIP_SECS, int(round(words / WORDS_PER_SECOND)))
            data["format"] = FORMAT_VERSION
            data.setdefault("video_id", js.stem)
            atomic_write(js, json.dumps(data, ensure_ascii=False, indent=2))
            atomic_write(day_dir / f"{js.stem}.md", render_markdown(js.stem, team, data))
            touched.add(day_dir.name)
    for d in sorted(touched):
        write_digest_file(d, "digest.md", video_ids=None)
    if touched:
        log(f"[migrate] converted older pressers output to the yt-quotes format in {len(touched)} day folder(s)")


def relink_stored_markdown() -> None:
    """Apply link_bare_urls and add_clip_links to every stored per-video .md
    and digest (written before those links existed). Text-only and
    idempotent; no Gemini calls."""
    changed = 0
    for day_dir in iter_day_dirs():
        for md in day_dir.glob("*.md"):
            try:
                text = md.read_text(encoding="utf-8")
            except OSError:
                continue
            linked = add_clip_links(link_bare_urls(text))
            if linked != text:
                atomic_write(md, linked)
                changed += 1
    if changed:
        log(f"[relink] updated the links in {changed} stored markdown file(s)")


def build_clip_manifest(run_slot: str, window_hours: int) -> dict:
    """Rolling manifest of every clip from videos published OR processed in
    the last window_hours, rebuilt from the per-video JSON on disk. Only
    quotes whose speaker is named in the video make it in (the clipper burns
    the speaker into a lower third). Field set is a superset of the
    original manifest so older clippers keep working."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=window_hours)
    clips, skipped_unnamed = [], 0
    for day_dir in iter_day_dirs():
        for js in day_dir.glob("*.json"):
            if not VIDEO_ID_RE.match(js.stem):
                continue
            try:
                data = json.loads(js.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            pub = parse_iso(data.get("published") or "")
            processed = parse_iso(data.get("processed_at") or "")
            if not any(t and t >= cutoff for t in (pub, processed)):
                continue
            vid = js.stem
            for q in data.get("quotes") or []:
                speaker = q.get("speaker") or ""
                if q.get("speaker_confidence") != "named" or is_unidentified(speaker):
                    skipped_unnamed += 1
                    continue
                try:
                    start, end = int(q["start_seconds"]), int(q["end_seconds"])
                except (KeyError, TypeError, ValueError):
                    continue
                clips.append({
                    "clip_id": f"{vid}_{start}_{end}",
                    "video_id": vid,
                    "url": WATCH_URL_TEMPLATE.format(video_id=vid),
                    "clip_url": f"https://www.youtube.com/watch?v={vid}&t={start}s",
                    "start_seconds": start,
                    "end_seconds": end,
                    "duration_seconds": end - start,
                    "speaker": speaker,
                    "team": q.get("team") or data.get("channel_team") or "",
                    "text": ytq._canonical_quote_text(q),
                    "news_angle": q.get("summary_phrase") or "",
                    "social_post": q.get("social_post") or "",
                    "rank": q.get("rank"),
                    "video_title": data.get("video_title") or "",
                    "channel_team": data.get("channel_team") or "",
                    "published": data.get("published") or "",
                    "publish_date": day_dir.name,
                    "speaker_confidence": "named",
                    "pull_quote": q.get("excerpt") or "",
                    "news_score": q.get("news_score"),
                    "timestamp_source": "gemini",
                    "align_score": None,
                    "gemini_start_seconds": start,
                    "gemini_end_seconds": end,
                    "run_id": data.get("run_id") or "",
                    "processed_at": data.get("processed_at") or "",
                    "content_type": video_content_type(data),
                })
    clips.sort(key=lambda c: (c["published"], -(c["rank"] or 0)), reverse=True)
    if skipped_unnamed:
        log(f"[clips] left out {skipped_unnamed} quote(s) without a speaker named in the video")
    latest = max(clips, key=lambda c: c["processed_at"], default=None)
    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "latest_run_id": latest["run_id"] if latest else "",
        "run_slot": run_slot,
        "window_hours": window_hours,
        "count": len(clips),
        "clips": clips,
    }


def write_clip_manifest(manifest: dict) -> None:
    content = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    atomic_write(LATEST_CLIPS_PATH, content)
    atomic_write(CLIPS_ARCHIVE_DIR / f"{manifest['generated_at'][:10]}.json", content)
    log(f"[clips] wrote {manifest['count']} clip(s) to latest_clips.json")


# --------------------------------------------------------------------------- #
# Main pipeline
# --------------------------------------------------------------------------- #

def filter_video(video: dict, config: dict, cutoff: datetime) -> str:
    """'' if the video should be processed, otherwise a skip reason."""
    existing = find_existing_artifact(video["video_id"])
    if existing:
        return f"already processed ({existing.parent.name}/{existing.name})"
    pub = parse_iso(video.get("published") or "")
    if pub and pub < cutoff:
        return f"published {pub:%Y-%m-%d %H:%M}Z, outside window"
    if video.get("live") in ("live", "upcoming"):
        return f"livestream is {video['live']}"
    min_secs = int(config.get("min_duration_seconds", 60))
    duration = int(video.get("duration") or 0)
    if duration < min_secs:
        return f"duration {duration}s below {min_secs}s minimum"
    max_secs = int(config.get("max_duration_secs", 2700))
    if max_secs and duration > max_secs:
        return (f"duration {duration}s over max_duration_secs {max_secs}s (Gemini cost cap; "
                "pass the URL in extra_videos to process it anyway)")
    return ""


def title_filter(title: str, config: dict) -> str:
    """'' if the title looks like a presser, otherwise a skip reason."""
    kw = title_keyword_hit(title, config.get("exclude_title_keywords", []))
    if kw:
        return f"title contains excluded '{kw}'"
    if not title_keyword_hit(title, config.get("include_title_keywords", [])):
        return "title has no presser keyword"
    return ""


def _flush_std() -> None:
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass


def install_script_timeout() -> None:
    if not hasattr(signal, "SIGALRM"):
        log("signal.SIGALRM not available on this platform; script-level timeout disabled")
        return

    def _handler(signum, frame):
        if _outputs_written.is_set():
            log(f"[timeout] WARNING: {SCRIPT_TIMEOUT_SECS // 60}min watchdog fired after all outputs "
                "were written (a background thread was still alive); exiting 0")
            code = 0
        else:
            log(f"[timeout] script-level {SCRIPT_TIMEOUT_SECS // 60}min budget exceeded, exiting with partial output")
            code = 3
        _flush_std()
        os._exit(code)

    signal.signal(signal.SIGALRM, _handler)
    signal.alarm(SCRIPT_TIMEOUT_SECS)


def queue_slack_payload(payload: dict) -> None:
    try:
        SLACK_PAYLOAD_PATH.write_text(json.dumps(payload), encoding="utf-8")
        log(f"[slack] payload queued at {SLACK_PAYLOAD_PATH.name}; workflow posts after git push")
    except OSError as e:
        log(f"[slack] failed to write payload: {e}")


def discover_candidates(channels: list, config: dict, yt_key: str, cutoff: datetime) -> list:
    """Return [(team, {video_id, title, published})] whose title passes the
    presser filter and whose publish time is inside the window."""
    per_channel = int(config.get("recent_uploads_per_channel", 25))
    out = []
    for channel in channels:
        team = channel.get("team", channel.get("channel_id", "?"))
        cid = channel.get("channel_id", "")
        if not cid:
            continue
        try:
            uploads_id = get_uploads_playlist_id(cid, yt_key)
        except QuotaExceeded:
            raise
        except Exception as e:
            log(f"  [{team}] channels.list failed: {e}")
            continue
        if not uploads_id:
            log(f"  [{team}] no uploads playlist found (channel_id may be wrong)")
            continue
        try:
            uploads = list_recent_uploads(uploads_id, yt_key, per_channel)
        except QuotaExceeded:
            raise
        except Exception as e:
            log(f"  [{team}] playlistItems.list failed: {e}")
            continue
        kept = 0
        for up in uploads:
            pub = parse_iso(up["published"])
            if pub and pub < cutoff:
                continue
            reason = title_filter(up["title"], config)
            if reason:
                continue
            out.append((team, up))
            kept += 1
        log(f"  [{team}] {len(uploads)} recent upload(s), {kept} presser candidate(s) in window")
    return out


def main() -> int:
    install_script_timeout()
    gemini_key = os.getenv("GEMINI_API_KEY")
    yt_key = os.getenv("YOUTUBE_API_KEY")
    missing = [n for n, v in (("GEMINI_API_KEY", gemini_key), ("YOUTUBE_API_KEY", yt_key)) if not v]
    if missing:
        log(f"ERROR: missing env vars: {', '.join(missing)}")
        return 1

    global RUN_ID
    started = datetime.now(timezone.utc)
    today = started.strftime("%Y-%m-%d")
    run_slot = determine_run_slot()
    RUN_ID = f"{started:%Y-%m-%dT%H%MZ}-{run_slot}"
    log(f"Run slot: {run_slot} (run_id {RUN_ID})")

    config = load_config()
    _CONFIG.clear()
    _CONFIG.update(config)
    window_hours = int(config.get("window_hours", 48))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    channels = [c for c in config.get("channels", []) if c.get("active", True)]
    team_by_channel_id = {c.get("channel_id"): c.get("team") for c in config.get("channels", [])}
    max_total = int(config.get("max_videos_per_run", 30))
    max_per_channel = int(config.get("max_videos_per_channel_per_run", 6))

    migrate_legacy_outputs()
    relink_stored_markdown()

    def finish_without_videos() -> int:
        regenerate_index()
        write_clip_manifest(build_clip_manifest(run_slot, window_hours))
        queue_slack_payload({"kind": "no_new_videos", "date_str": today})
        _outputs_written.set()
        return 0

    # Step 1: discover presser candidates per channel.
    log(f"Polling {len(channels)} team channel(s) via YouTube Data API (window {window_hours}h)...")
    try:
        candidates = discover_candidates(channels, config, yt_key, cutoff)
        meta = hydrate_video_metadata([v["video_id"] for _, v in candidates], yt_key) if candidates else {}
    except QuotaExceeded as e:
        log(f"ERROR: YouTube Data API quota exceeded: {e}")
        return 2

    # Step 2: one-offs from EXTRA_VIDEOS (workflow_dispatch). They bypass the
    # title / age / duration filters because they were explicitly requested.
    # Already-processed videos are skipped unless FORCE_REPROCESS is true, in
    # which case their markers are cleared and their output is overwritten.
    extra_queue, extra_ids = [], set()
    extra_tokens = parse_extra_videos_env(os.getenv("EXTRA_VIDEOS", ""))
    force = env_flag("FORCE_REPROCESS")
    if extra_tokens:
        log(f"One-off input: {len(extra_tokens)} token(s) from EXTRA_VIDEOS (force={force})")
        ids = []
        for token in extra_tokens:
            vid = extract_one_off_video_id(token)
            if not vid:
                log(f"  [oneoff] skip {token[:80]!r}: could not parse video ID")
            elif vid not in ids:
                ids.append(vid)
        try:
            extra_meta = hydrate_video_metadata(ids, yt_key) if ids else {}
        except QuotaExceeded as e:
            log(f"ERROR: YouTube Data API quota exceeded: {e}")
            return 2
        except Exception as e:
            log(f"  [oneoff] metadata fetch failed: {e}")
            extra_meta = {}
        for vid in ids:
            m = extra_meta.get(vid)
            if not m:
                log(f"  [oneoff] skip {vid}: no metadata returned (private/deleted/invalid?)")
                continue
            existing = find_existing_artifact(vid)
            if existing and not force:
                log(f"  [oneoff] skip {vid}: already processed ({existing.parent.name}/{existing.name}); "
                    "run with force to reprocess")
                continue
            if existing:
                cleared = clear_video_markers(vid)
                log(f"  [oneoff] force: reprocessing {vid}, overwriting prior output"
                    + (f" (cleared {', '.join(cleared)})" if cleared else ""))
            team = team_by_channel_id.get(m.get("channel_id")) or m.get("channel_title") or "(one-off)"
            extra_queue.append((team, {
                "video_id": vid,
                "url": WATCH_URL_TEMPLATE.format(video_id=vid),
                "title": m.get("title", ""),
                "duration": m.get("duration", 0),
                "published": m.get("published_at", ""),
                "is_one_off": True,
            }))
            extra_ids.add(vid)
            log(f"  [oneoff] queued {vid} ({m.get('title', '')[:60]}) from {team}")

    # Step 3: filter the rotation queue.
    rotation_queue, per_channel_count = [], {}
    for team, up in candidates:
        vid = up["video_id"]
        if vid in extra_ids:
            continue
        m = meta.get(vid)
        if not m:
            log(f"  [{team}] skip {vid}: no metadata returned")
            continue
        video = {
            "video_id": vid,
            "url": WATCH_URL_TEMPLATE.format(video_id=vid),
            "title": m["title"],
            "duration": m["duration"],
            "published": m["published_at"] or up["published"],
            "live": m.get("live", "none"),
        }
        if per_channel_count.get(team, 0) >= max_per_channel:
            continue
        reason = filter_video(video, config, cutoff)
        if reason:
            log(f"  [{team}] skip {vid} ({video['title'][:60]}): {reason}")
            continue
        rotation_queue.append((team, video))
        per_channel_count[team] = per_channel_count.get(team, 0) + 1

    # Newest pressers first; anything past the cap is picked up next run
    # (it's still inside the 48h window and not yet on disk).
    rotation_queue.sort(key=lambda tv: tv[1].get("published") or "", reverse=True)
    queued = extra_queue + rotation_queue
    if not queued:
        log("Nothing new to process.")
        return finish_without_videos()
    if len(queued) > max_total:
        log(f"Capping queue from {len(queued)} to max_videos_per_run={max_total}")
        queued = queued[:max_total]

    # Step 4: Gemini, VIDEO_WORKERS in parallel on one shared client.
    client = genai.Client(api_key=gemini_key)
    log(f"Processing {len(queued)} video(s) with {MODEL} (up to {VIDEO_WORKERS} in parallel)...")
    summary = {k: 0 for k in ("ok", "too-long", "failed-hallucination", "failed-timeout",
                              "failed-other", "aborted-spending-cap", "deferred-transient")}
    processed_items: list = []
    results: list = []
    _count_gemini_calls(client)
    state_lock = threading.Lock()
    # Not a `with` block: its exit would wait for any hung call.
    ex = ThreadPoolExecutor(max_workers=VIDEO_WORKERS)
    try:
        futures = [ex.submit(process_one_video, client, team, video, state_lock, summary,
                             processed_items, results)
                   for team, video in queued]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                log(f"  unhandled worker exception: {e}")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    log(f"Gemini calls: {_stats['extract_calls']} extraction ({_stats['video_seconds']}s of video sent), "
        f"{_stats['text_calls']} text-only (quote splitter + clip fields)")

    # Step 5: digests, index, clip manifest.
    per_run_filename = f"digest-{run_slot}.md"
    digest_dates = []
    for pd in sorted({it["date"] for it in processed_items if it.get("date")}):
        run_ids = [it["video_id"] for it in processed_items if it.get("date") == pd]
        run_path = write_digest_file(pd, per_run_filename, video_ids=run_ids)
        write_digest_file(pd, "digest.md", video_ids=None)
        if run_path is not None:
            digest_dates.append(pd)
    regenerate_index()
    manifest = build_clip_manifest(run_slot, window_hours)
    write_clip_manifest(manifest)

    aborted_count = summary.get("aborted-spending-cap", 0)
    deferred_count = summary.get("deferred-transient", 0)
    if processed_items or aborted_count or deferred_count:
        queue_slack_payload({
            "kind": "digest",
            "date_str": today,
            "run_slot": run_slot,
            "items": processed_items,
            "digest_dates": digest_dates,
            "clip_count": manifest["count"],
            "one_off_count": sum(1 for it in processed_items if it.get("is_one_off")),
            "aborted_count": aborted_count,
            "deferred_count": deferred_count,
            "deferred_videos": list(_deferred_items),
        })
    else:
        queue_slack_payload({"kind": "no_new_videos", "date_str": today})
    _outputs_written.set()

    log(f"Done. {summary}")
    return 0


if __name__ == "__main__":
    rc = main()
    # Exit NOW. A Gemini call abandoned by the per-video timeout can leave a
    # worker thread blocked on a socket; a normal interpreter exit joins
    # executor threads and would hang until the watchdog killed the run
    # (the 2026-09-30 20:16 run: "Done." then exit 3 four minutes later).
    # Every output file is already written and closed at this point.
    _flush_std()
    os._exit(rc)
