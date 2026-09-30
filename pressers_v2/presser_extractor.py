"""
NBA pressers v2 — press-conference quotes + clip manifest.

Polls the 30 NBA team YouTube channels via the YouTube Data API v3, keeps
only press conference / postgame / pregame / media availability videos
published in the last 48h, sends each one to Gemini and saves:

  pressers_v2/output/<publish-date>/<video_id>.md / .json   per-video quotes
  pressers_v2/output/<publish-date>/digest.md               aggregate digest
  pressers_v2/output/<publish-date>/digest-<slot>.md        this run's digest
  pressers_v2/output/latest_clips.json                      rolling clip manifest
  pressers_v2/output/clips/<run-date>.json                  dated manifest copy

Approach mirrors jsierrahoopshype/hoopshype-yt-quotes (quote_extractor.py):
same Gemini model, same 5-step retry, 503/500 deferral, spending-cap abort,
long-video chunking, hallucination title guard, filesystem-as-database
"already processed" check, and Slack payload posted after git push.

Usage (from repo root):
    python pressers_v2/presser_extractor.py
"""

import html
import json
import math
import os
import re
import signal
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types


load_dotenv()

MODEL = "gemini-3.1-flash-lite"
ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
CONFIG_PATH = ROOT / "config.json"
LATEST_CLIPS_PATH = OUTPUT_DIR / "latest_clips.json"
CLIPS_ARCHIVE_DIR = OUTPUT_DIR / "clips"
SLACK_PAYLOAD_PATH = ROOT / ".slack_payload.json"
WATCH_URL_TEMPLATE = "https://www.youtube.com/watch?v={video_id}"
YT_API_BASE = "https://www.googleapis.com/youtube/v3"

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
DAY_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

PROMPT = """You are watching a video from an NBA team's official YouTube channel: a press conference, postgame or pregame media availability, practice/shootaround availability, or similar media session. Extract the quotes a news desk would clip into short vertical social videos and write up as news.

WHAT TO PICK
- Prefer quotes with news: injury updates and timelines, lineup or rotation changes, minutes restrictions, trade or free-agency comments, contract talk, criticism of teammates, officials or opponents, admissions of mistakes, bold predictions, strong opinions, emotional moments, memorable one-liners, specific praise that says something new about a player.
- Skip: generic cliches ("we just have to execute", "credit to them", "one game at a time"), play-by-play recaps with no insight, reporters' questions, PR staff intros, dead air.
- How many: 2-6 quotes for a video under 10 minutes, up to 10 for 10-30 minutes, up to 15 for longer videos. Hard cap: 15 per request. Do not pad with weak quotes. Return an empty array only when nothing is newsworthy.

CLIP RULES (each quote becomes a standalone vertical video clip)
- Each quote is ONE contiguous span of a single speaker answering. Never stitch separate passages together.
- "start_seconds": the second the speaker starts the first word of the quote (not the reporter's question).
- "end_seconds": the second the speaker finishes the last word of the quote.
- end_seconds minus start_seconds MUST be between 15 and 60. If a great line is shorter than 15 seconds, extend the span to include the neighbouring sentences of the same answer so the clip makes sense on its own. If the answer runs longer than 60 seconds, pick the strongest self-contained 15-60 second stretch.
- Self-contained: a viewer with zero context must understand the clip. Start at the beginning of a sentence and end at the end of a sentence.
- Timestamps are integers in seconds from the start of the video (or of the slice, see CHUNK CONTEXT if present). Be precise; the clip is cut from these numbers.

TEXT
- "text": the VERBATIM words the speaker says between start_seconds and end_seconds, in order. Remove only "uh", "um" and stuttered repeats ("the the"). Do not paraphrase, summarize, reorder or tidy grammar. This text is burned into the clip as captions, so it must match the audio.
- PLAYER NAMES: use standard NBA reporting spellings (Mikal Bridges not Michael, Karl-Anthony Towns, Scottie Barnes, Jrue Holiday, Donovan Mitchell, Tyrese Maxey, Cade Cunningham, Jalen Brunson, Jaylen Brown, Jayson Tatum, Shai Gilgeous-Alexander, Victor Wembanyama). Apply this to all names across the league.
- "speaker": SPEAKER NAMING RULE. Only name a speaker if THIS VIDEO identifies them by name: an on-screen name graphic or chyron, the speaker being introduced by name, someone addressing them by name, or the video title naming the single person at the podium. Otherwise write exactly "Unidentified speaker". Do NOT guess from voice, appearance, job title, team, or who "usually" does these pressers. A coach being discussed or quoted by podcast hosts is not the speaker; the host is. Never write "Unknown".
- "speaker_confidence": "named" when the speaker's name comes from one of the in-video sources listed above; "inferred" in every other case (including "Unidentified speaker"). When in doubt, use "inferred".
- If speaker_confidence is "inferred", do not name or guess the speaker anywhere else either: "news_angle" and "social_post" must not attribute the quote to a named person.
- "team": the NBA team the speaker belongs to, full name (e.g. "Boston Celtics"). The channel's team is given above; use it unless the speaker is clearly from another team (e.g. an opposing coach). Use "" if unsure.
- MULTI-SPEAKER EXCHANGES: when two or more people each contribute more than about 5 words to the same answer or exchange inside the clip (a podcast back-and-forth, a reporter follow-up that the player answers), also fill "text_blocks" with one entry per contribution in order: {"speaker": ..., "speaker_confidence": ..., "text": ...}, applying the same SPEAKER NAMING RULE to each block. "text" must still hold the full verbatim transcript of the whole clip, and "speaker" is the person with the longest contribution. Omit "text_blocks" or leave it empty for single-speaker quotes.
- "pull_quote": the punchiest standalone line from the quote, at most 15 words, copied WORD FOR WORD from "text" (no paraphrasing, no added words or punctuation). Use "" if nothing works on its own.
- "names_mentioned": every player, coach or executive named INSIDE "text", spelled exactly as it appears in "text". Exclude team names and the speaker's own name.

EDITORIAL FIELDS
- "news_angle": one line, at most 15 words, stating the news in the quote as a specific headline-style fact that names the player, team or issue. No em-dashes. Example: "Jalen Brunson says his sprained ankle is fine and he will play Friday".
- "social_post": a draft post for X / Bluesky, at most 240 characters. ONE idea only. Lead with the news, punchy and conversational; it may include a short verbatim phrase from the quote in quotation marks. Rules: NO hashtags, NO emojis, NO em-dashes or en-dashes (use commas or periods), no "BREAKING", no vague clickbait, do not state anything the speaker did not say.

LANGUAGE: if a quote is not spoken in English, translate "text", "news_angle" and "social_post" to English.

HALLUCINATION GUARD: begin your JSON with a "video_title" field that ECHOES BACK EXACTLY the YouTube title supplied above (the line starting with "YouTube title:"). If your analysis does not match that title, the response is rejected.

Return ONLY valid JSON, no surrounding text or markdown fences:

{
  "video_title": "exact echo of the YouTube title supplied above",
  "speakers_seen": ["names you saw or heard, in order of appearance"],
  "quotes": [
    {
      "rank": 1,
      "speaker": "full name identified in the video, or \\"Unidentified speaker\\"",
      "speaker_confidence": "named or inferred",
      "team": "full NBA team name, or empty string",
      "start_seconds": 83,
      "end_seconds": 121,
      "text": "verbatim words spoken between start_seconds and end_seconds",
      "pull_quote": "up to 15 words copied verbatim from text, or empty string",
      "names_mentioned": ["Evan Mobley"],
      "text_blocks": [
        {"speaker": "X", "speaker_confidence": "named", "text": "X's verbatim contribution"},
        {"speaker": "Unidentified speaker", "speaker_confidence": "inferred", "text": "the other person's verbatim contribution"}
      ],
      "news_angle": "one-line specific news angle",
      "social_post": "draft social post, one idea, no hashtags, no emojis, no em-dashes"
    }
  ]
}"""

MIN_CLIP_SECS = 15
MAX_CLIP_SECS = 60
MAX_QUOTES_PER_VIDEO = 15
# Rough speaking rate used only when Gemini omits/garbles end_seconds.
WORDS_PER_SECOND = 2.6

DIGEST_CLOSING_LINE = (
    '<a href="https://www.youtube.com/feed/subscriptions" target="_blank" '
    'rel="noopener">CHECK OTHER YOUTUBE VIDEOS HERE</a>'
)
VIDEO_WORKERS = 3                     # concurrent process_video calls across the queue
PER_VIDEO_TIMEOUT_SECS = 12 * 60      # hard upper bound per video
SCRIPT_TIMEOUT_SECS = 35 * 60         # hard wall-clock budget for the whole run

# Long-video chunking (same scheme as yt-quotes): videos longer than
# CHUNK_DURATION_SECS are sent as 60-min slices via VideoMetadata offsets.
CHUNK_DURATION_SECS = 60 * 60
MAX_CHUNKS = 4

_TRANSIENT_MESSAGE_PATTERNS = (
    "UNAVAILABLE",
    "RESOURCE_EXHAUSTED",
    "INTERNAL",
    "SERVER DISCONNECTED",
    "CONNECTION RESET",
    "CONNECTION ABORTED",
    "READ TIMED OUT",
    "REMOTE END CLOSED CONNECTION",
    "READERROR",
    "REMOTEPROTOCOLERROR",
    "CONNECTERROR",
    "READTIMEOUT",
    "CONNECTTIMEOUT",
)

_SPENDING_CAP_PATTERNS = (
    "SPENDING CAP",
    "EXCEEDED YOUR CURRENT QUOTA",
    "QUOTA EXCEEDED",
)

HALLUCINATION_OVERLAP_THRESHOLD = 0.70  # strictly greater; 0.70 itself is rejected

RETRY_CAP = 2  # after this many cap-eligible failures, write FAILED.txt
_CAP_ELIGIBLE_FAILURES = frozenset({"failed-timeout", "failed-hallucination", "failed-other"})

# Process-wide abort flags, same semantics as yt-quotes.
_spending_cap_hit = threading.Event()
_transient_overload_hit = threading.Event()
_deferred_items: list = []


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def timestamp_to_seconds(ts) -> int:
    """Accepts 83, 83.4, "83", "1:23" or "0:01:23". Returns 0 on garbage."""
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return max(0, int(round(ts)))
    parts = str(ts or "").strip().split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 3:
        total = nums[0] * 3600 + nums[1] * 60 + nums[2]
    elif len(nums) == 2:
        total = nums[0] * 60 + nums[1]
    else:
        total = nums[0] if nums else 0
    return max(0, int(round(total)))


def seconds_to_timestamp(secs: int) -> str:
    secs = max(0, int(secs or 0))
    hours, rem = divmod(secs, 3600)
    mins, s = divmod(rem, 60)
    if hours:
        return f"{hours}:{mins:02d}:{s:02d}"
    return f"{mins:02d}:{s:02d}"


def compute_chunks(duration_secs: int) -> list | None:
    if duration_secs is None or duration_secs <= 0:
        return [(None, None)]
    if duration_secs <= CHUNK_DURATION_SECS:
        return [(None, None)]
    if duration_secs > CHUNK_DURATION_SECS * MAX_CHUNKS:
        return None
    n_chunks = math.ceil(duration_secs / CHUNK_DURATION_SECS)
    return [
        (i * CHUNK_DURATION_SECS, min((i + 1) * CHUNK_DURATION_SECS, duration_secs))
        for i in range(n_chunks)
    ]


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


def _normalize_title(title: str) -> str:
    if not title:
        return ""
    stripped = re.sub(r"[^a-z0-9\s]+", " ", title.lower())
    return re.sub(r"\s+", " ", stripped).strip()


def title_word_overlap(expected: str, got: str) -> float:
    exp_words = set(_normalize_title(expected).split())
    if not exp_words:
        return 1.0
    got_words = set(_normalize_title(got).split())
    return len(exp_words & got_words) / len(exp_words)


def parse_iso(iso: str) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def publish_date(video: dict) -> str:
    dt = parse_iso(video.get("published") or "")
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


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


UNIDENTIFIED_SPEAKER = "Unidentified speaker"


def is_unknown_speaker(speaker: str) -> bool:
    """Ported from yt-quotes' _is_unknown_speaker, plus 'Unidentified ...'."""
    if not speaker:
        return True
    lower = speaker.strip().lower()
    if lower.startswith("unknown") or lower.startswith("unidentified"):
        return True
    return lower in ("speaker", "n/a", "none")


# --------------------------------------------------------------------------- #
# Text safety. Everything that came from YouTube or Gemini is untrusted:
# it is rendered by Jekyll/kramdown on GitHub Pages (raw HTML passes through,
# Liquid tags execute) and by Slack (<!channel>, <url|label> are live syntax).
# --------------------------------------------------------------------------- #

_MD_ENTITY_MAP = {
    "{": "&#123;",   # Liquid {{ }} / {% %} would execute or break the Pages build
    "}": "&#125;",
    "[": "&#91;",    # no smuggled [label](javascript:...) links
    "]": "&#93;",
    "*": "&#42;",    # keep our own **bold** markup intact
    "`": "&#96;",
    "|": "&#124;",
}


def md_escape(text: str) -> str:
    """Escape untrusted text for markdown that GitHub Pages renders to HTML."""
    out = html.escape(str(text or ""), quote=False)  # & < > (no attributes, so quotes stay readable)
    for ch, ent in _MD_ENTITY_MAP.items():
        out = out.replace(ch, ent)
    # Collapse newlines so fetched text can't start a new markdown block.
    return re.sub(r"\s*[\r\n]+\s*", " ", out).strip()


_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F900-\U0001F9FF"
    "\U00002B00-\U00002BFF"
    "️‍"
    "]+"
)


def strip_dashes(text: str) -> str:
    """Replace em/en dashes (and spaced hyphens used as dashes) with commas."""
    out = re.sub(r"\s*[—–‒―]\s*", ", ", text or "")
    out = re.sub(r"\s+-{1,2}\s+", ", ", out)
    out = re.sub(r",\s*([,.!?])", r"\1", out)
    return re.sub(r"\s{2,}", " ", out).strip(" ,")


def clean_social_post(text: str) -> str:
    """Enforce the house rules even if Gemini ignores them."""
    out = _EMOJI_RE.sub("", text or "")
    out = re.sub(r"#(\w)", r"\1", out)   # drop hashtag markers, keep the word
    out = strip_dashes(out)
    return re.sub(r"\s{2,}", " ", out).strip()


# --------------------------------------------------------------------------- #
# State: filesystem-as-database (same rules as yt-quotes)
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
# Gemini (same call shape + retry policy as yt-quotes)
# --------------------------------------------------------------------------- #

def call_gemini(client, url: str, video_title: str, team: str, duration_secs: int,
                start_offset_secs: int | None = None,
                end_offset_secs: int | None = None) -> tuple[str, object]:
    parts = [f"YouTube title: {video_title}", f"Channel team: {team}"]
    if duration_secs:
        parts.append(f"Video length: {duration_secs} seconds.")
    if start_offset_secs is not None and end_offset_secs is not None:
        parts.append(
            f"CHUNK CONTEXT: this is one slice of a longer video, covering seconds "
            f"{start_offset_secs} through {end_offset_secs} (relative to the original). "
            f"Return start_seconds / end_seconds relative to the slice (0 = the slice's "
            f"first second). The pipeline will translate them to absolute times."
        )
    prefixed_prompt = "\n\n".join(parts) + "\n\n" + PROMPT
    video_part = types.Part.from_uri(file_uri=url, mime_type="video/mp4")
    if start_offset_secs is not None or end_offset_secs is not None:
        start_str = f"{start_offset_secs or 0}s"
        end_str = f"{end_offset_secs}s" if end_offset_secs is not None else None
        try:
            video_part.video_metadata = types.VideoMetadata(start_offset=start_str, end_offset=end_str)
        except (AttributeError, TypeError):
            log(f"  [chunk] WARN: types.VideoMetadata unavailable in this SDK; "
                f"falling back to whole-video for offsets ({start_str}, {end_str})")
    response = client.models.generate_content(
        model=MODEL,
        contents=[video_part, prefixed_prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.3,
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
        ),
    )
    return (response.text or ""), response.usage_metadata


def gemini_error_status(exc: Exception) -> int | None:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    m = re.search(r"\b(4\d\d|5\d\d)\b", str(exc))
    return int(m.group(1)) if m else None


def is_token_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "token" in msg and ("limit" in msg or "exceed" in msg or "too" in msg)


class SpendingCapExhausted(Exception):
    """429 caused by a project spending cap / daily quota; abort the queue."""


class TransientServerOverload(Exception):
    """503/500 that survived all retries; defer the video to the next run."""


def is_spending_cap_error(exc: Exception) -> bool:
    msg = str(exc).upper()
    return any(p in msg for p in _SPENDING_CAP_PATTERNS)


def is_deferred_transient_error(exc: Exception) -> bool:
    if gemini_error_status(exc) in (500, 503):
        return True
    msg = str(exc).upper()
    return "UNAVAILABLE" in msg or "500 INTERNAL" in msg


def is_transient_gemini_error(exc: Exception) -> bool:
    if gemini_error_status(exc) in (429, 500, 503):
        return True
    try:
        import httpx
        if isinstance(exc, (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError,
                            httpx.ReadTimeout, httpx.ConnectTimeout)):
            return True
    except ImportError:
        pass
    msg = str(exc).upper()
    return any(p in msg for p in _TRANSIENT_MESSAGE_PATTERNS)


def call_gemini_with_retry(client, url, video_title, team, duration_secs,
                           start_offset_secs=None, end_offset_secs=None) -> tuple[str, object]:
    """5s/15s/45s/120s/300s backoff. Spending cap -> SpendingCapExhausted
    immediately. Terminal 503/500 -> TransientServerOverload (deferred)."""
    backoffs = [5, 15, 45, 120, 300]
    for attempt, sleep_s in enumerate(backoffs, start=1):
        try:
            return call_gemini(client, url, video_title, team, duration_secs,
                               start_offset_secs=start_offset_secs,
                               end_offset_secs=end_offset_secs)
        except Exception as e:
            if is_spending_cap_error(e):
                log(f"  [main] [SPENDING CAP] {e}")
                raise SpendingCapExhausted(str(e)) from e
            if not is_transient_gemini_error(e):
                raise
            if _spending_cap_hit.is_set():
                raise SpendingCapExhausted("spending cap hit by a sibling worker") from e
            if attempt < len(backoffs):
                log(f"  [main] transient error on attempt {attempt}/5, sleeping {sleep_s}s: {e}")
                time.sleep(sleep_s)
            else:
                log(f"  [main] giving up after 5 attempts: {e}")
                if is_deferred_transient_error(e):
                    log("  [DEFERRED] Gemini server overload after retries; will retry next run")
                    raise TransientServerOverload(str(e)) from e
                raise


# --------------------------------------------------------------------------- #
# Quote normalisation
# --------------------------------------------------------------------------- #

def _clean_ws(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def resolve_speaker(name, confidence) -> tuple[str, str, str]:
    """Apply the naming rule. Returns (speaker, speaker_confidence, guess).

    Only an explicit "named" confidence keeps the name. Anything else
    (inferred, missing, legacy output without the field) becomes
    "Unidentified speaker"; Gemini's guess is kept separately in the JSON
    for the editor but never rendered or sent to the clipper."""
    name = _clean_ws(name)
    confidence = _clean_ws(confidence).lower()
    if confidence == "named" and not is_unknown_speaker(name):
        return name, "named", ""
    guess = "" if is_unknown_speaker(name) else name
    return UNIDENTIFIED_SPEAKER, "inferred", guess


def scrub_guess(text: str, guess: str) -> str:
    """Replace a guessed speaker name (full name, or a distinctive surname)
    with "Unidentified speaker" so it can't slip in via news_angle etc."""
    if not text or not guess or is_unknown_speaker(guess):
        return text or ""
    out = re.sub(re.escape(guess), UNIDENTIFIED_SPEAKER, text, flags=re.IGNORECASE)
    surname = guess.split()[-1]
    if len(surname) >= 4:
        out = re.sub(rf"\b{re.escape(surname)}\b", UNIDENTIFIED_SPEAKER, out, flags=re.IGNORECASE)
    return out


def _loose(text: str) -> str:
    """Lowercase, letters/digits only, single spaces: for verbatim checks."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (text or "").lower())).strip()


def normalize_quote(q: dict, offset: int, duration_secs: int, channel_team: str) -> dict | None:
    """Coerce one raw Gemini quote into the clip schema, enforcing 15-60s."""
    if not isinstance(q, dict):
        return None

    blocks = []
    for b in q.get("text_blocks") or []:
        if not isinstance(b, dict) or not _clean_ws(b.get("text")):
            continue
        b_speaker, b_conf, b_guess = resolve_speaker(b.get("speaker"), b.get("speaker_confidence"))
        block = {"speaker": b_speaker, "speaker_confidence": b_conf, "text": _clean_ws(b.get("text"))}
        if b_guess:
            block["speaker_guess"] = b_guess
        blocks.append(block)
    if len(blocks) < 2:
        blocks = []

    text = _clean_ws(q.get("text") or q.get("quote"))
    if not text and blocks:
        text = " ".join(b["text"] for b in blocks)
    if not text:
        return None

    start = timestamp_to_seconds(q.get("start_seconds", q.get("timestamp", 0))) + offset
    end_raw = q.get("end_seconds")
    end = timestamp_to_seconds(end_raw) + offset if end_raw not in (None, "") else 0
    if end <= start:
        est = int(round(len(text.split()) / WORDS_PER_SECOND))
        end = start + max(MIN_CLIP_SECS, min(MAX_CLIP_SECS, est))
    length = end - start
    if length < MIN_CLIP_SECS:
        end = start + MIN_CLIP_SECS
    elif length > MAX_CLIP_SECS:
        end = start + MAX_CLIP_SECS
    if duration_secs and end > duration_secs:
        # Keep the clip length by sliding it back from the end of the video.
        shift = end - duration_secs
        end = duration_secs
        start = max(0, start - shift)

    speaker, confidence, guess = resolve_speaker(q.get("speaker"), q.get("speaker_confidence"))
    team = _clean_ws(q.get("team")) or channel_team

    # pull_quote must be verbatim; drop it rather than put invented words in quotes.
    pull_quote = _clean_ws(q.get("pull_quote")).strip('"\u201c\u201d ')
    if pull_quote and (_loose(pull_quote) not in _loose(text) or len(pull_quote.split()) > 20):
        pull_quote = ""

    names = []
    for n in q.get("names_mentioned") or []:
        n = _clean_ws(n)
        if n and n in text and n != speaker and n not in names:
            names.append(n)

    out = {
        "rank": q.get("rank"),
        "speaker": speaker,
        "speaker_confidence": confidence,
        "team": team,
        "start_seconds": int(start),
        "end_seconds": int(end),
        "text": text,
        "pull_quote": pull_quote,
        "names_mentioned": names,
        "news_angle": scrub_guess(strip_dashes(_clean_ws(q.get("news_angle"))), guess),
        "social_post": scrub_guess(clean_social_post(_clean_ws(q.get("social_post"))), guess),
    }
    if guess:
        out["speaker_guess"] = guess
    if blocks:
        out["text_blocks"] = blocks
    return out


def finalize_quotes(quotes: list) -> list:
    """Dedupe (same text or same start), order by rank, renumber, cap."""
    def rank_key(q):
        try:
            return int(q.get("rank"))
        except (TypeError, ValueError):
            return 10_000
    ordered = sorted(quotes, key=rank_key)
    seen_text, seen_start, out = set(), set(), []
    for q in ordered:
        key = q["text"].lower()
        if key in seen_text or q["start_seconds"] in seen_start:
            continue
        seen_text.add(key)
        seen_start.add(q["start_seconds"])
        out.append(q)
    out = out[:MAX_QUOTES_PER_VIDEO]
    for i, q in enumerate(out, start=1):
        q["rank"] = i
    return out


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def md_link(url: str) -> str:
    """Explicit [url](url) link: kramdown on GitHub Pages doesn't auto-link
    bare URLs. Only used for URLs this script builds from a validated ID."""
    return f"[{url}]({url})"


def _bold_names(text: str, names: list) -> str:
    """Ported verbatim from yt-quotes. Wrap every occurrence of each name in
    **bold**, longest names first so "LeBron James" bolds as one unit.
    Callers pass already-escaped text and names so the match still lines up."""
    valid = [n for n in (names or []) if isinstance(n, str) and n.strip()]
    if not valid or not text:
        return text or ""
    sorted_names = sorted(set(valid), key=len, reverse=True)
    pattern = "|".join(re.escape(n) for n in sorted_names)
    return re.sub(pattern, lambda m: f"**{m.group(0)}**", text)


def _named_speakers(data: dict) -> list:
    """Speakers confirmed by name in the video, in order. Used for the
    "Speakers identified" line so a guessed name never reaches the digest."""
    out = []
    for q in data.get("quotes", []):
        entries = [q] + list(q.get("text_blocks") or [])
        for e in entries:
            name = e.get("speaker") or ""
            if e.get("speaker_confidence") == "named" and not is_unknown_speaker(name) and name not in out:
                out.append(name)
    return out


def to_markdown(video: dict, team: str, data: dict) -> str:
    """Port of yt-quotes' to_markdown. Same structure:

        # Title — *Team*
        Source: <link>
        _Speakers identified: ..._
        **N. Speaker (Team) — "pull quote" — summary** [MM:SS](link)
        Speaker: "quote with **names** bolded"   (or **Speaker:** "..." per block)
        <timestamped link as the last line>

    Differences from yt-quotes: team in the header, every URL is an explicit
    markdown link, and all fetched text goes through md_escape (HTML/Liquid)."""
    vid = video["video_id"]
    url = WATCH_URL_TEMPLATE.format(video_id=vid)
    title = data.get("video_title") or video.get("title") or "NBA press conference"
    lines = [
        f"# {md_escape(title)} — *{md_escape(team)}*",
        "",
        f"Source: {md_link(url)}",
        "",
    ]
    named = _named_speakers(data)
    if named:
        lines.append(f"_Speakers identified: {md_escape(', '.join(named))}_")
        lines.append("")
    for q in data.get("quotes", []):
        secs = int(q.get("start_seconds") or 0)
        ts_link = f"https://www.youtube.com/watch?v={vid}&t={secs}s"
        ts_label = seconds_to_timestamp(secs)
        rank = q.get("rank", "?")
        is_named = q.get("speaker_confidence") == "named" and not is_unknown_speaker(q.get("speaker") or "")
        speaker = md_escape(q.get("speaker")) if is_named else UNIDENTIFIED_SPEAKER
        q_team = md_escape(q.get("team") or "")
        guess = "" if is_named else (q.get("speaker_guess") or q.get("speaker") or "")
        summary = md_escape(scrub_guess(q.get("news_angle") or "", guess))
        excerpt = md_escape(q.get("pull_quote") or "")
        # Header: **N. Speaker (Team) — "pull quote" — summary** [MM:SS](url).
        # Team only for a named speaker: an unidentified voice on a team
        # channel may be a host or reporter, not a team member.
        fragments = [f"{speaker} ({q_team})" if is_named and q_team else speaker]
        if excerpt:
            fragments.append(f'"{excerpt}"')
        if summary:
            fragments.append(summary)
        inner = " — ".join(fragments)
        lines.append(f"**{rank}. {inner}** [{ts_label}]({ts_link})")
        lines.append("")
        names = [md_escape(n) for n in (q.get("names_mentioned") or [])]
        blocks = q.get("text_blocks") or []
        if blocks:
            for j, block in enumerate(blocks):
                block_named = block.get("speaker_confidence") == "named" and \
                    not is_unknown_speaker(block.get("speaker") or "")
                block_speaker = md_escape(block.get("speaker")) if block_named else UNIDENTIFIED_SPEAKER
                bolded = _bold_names(md_escape(block.get("text") or ""), names)
                lines.append(f"**{block_speaker}:** \"{bolded}\"")
                if j < len(blocks) - 1:
                    lines.append("")  # blank line -> markdown paragraph break
        else:
            bolded = _bold_names(md_escape(q.get("text") or ""), names)
            lines.append(f"{speaker}: \"{bolded}\"")
        # Timestamped URL as the LAST line of the quote block, as a link.
        lines.append("")
        lines.append(md_link(ts_link))
        lines.append("")
    return "\n".join(lines)


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


def write_outputs(video: dict, team: str, data: dict) -> Path:
    day_dir = video_day_dir(video)
    md_path = day_dir / f"{video['video_id']}.md"
    json_path = day_dir / f"{video['video_id']}.json"
    md_content = to_markdown(video, team, data)
    if not md_content:
        raise ValueError(f"to_markdown produced empty content for {video['video_id']}")
    atomic_write(json_path, json.dumps(data, ensure_ascii=False, indent=2))
    atomic_write(md_path, md_content)
    return md_path


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
    lines = ["# NBA pressers v2 — index", "",
             "Clip manifest: [latest_clips.json](latest_clips.json)", ""]
    if not rows:
        lines.append("_No videos processed yet._")
    else:
        lines.extend(f"- [{d}]({d}/digest.md)" for d in rows)
    (OUTPUT_DIR / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_digest_file(date_str: str, output_filename: str, video_ids: list | None = None) -> Path | None:
    """Concatenate per-video .md files into one digest (yt-quotes format)."""
    day_dir = OUTPUT_DIR / date_str
    if not day_dir.is_dir():
        return None
    if video_ids is None:
        md_paths = sorted(p for p in day_dir.glob("*.md") if p.is_file() and not p.name.startswith("digest"))
    else:
        md_paths = [day_dir / f"{v}.md" for v in video_ids if (day_dir / f"{v}.md").is_file()]
    parts = [f"# NBA Pressers — {date_str}", ""]
    first = True
    for md_path in md_paths:
        try:
            content = md_path.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if not content or not re.search(r"(?m)^\*\*\d+\.", content):
            continue
        if content.startswith("# "):
            content = "#" + content
        if not first:
            parts.extend(["---", ""])
        first = False
        parts.extend([content, ""])
    if first:
        return None
    parts.extend(["---", "", DIGEST_CLOSING_LINE, ""])
    digest_path = day_dir / output_filename
    atomic_write(digest_path, "\n".join(parts))
    return digest_path


def build_clip_manifest(run_slot: str, window_hours: int) -> dict:
    """Rolling manifest of every clip from videos published OR processed in
    the last window_hours, rebuilt from the per-video JSON on disk so a run
    that finds nothing new still leaves the recent clips available."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=window_hours)
    clips = []
    skipped_unnamed = 0
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
                # The clipper burns "speaker" into a lower third, so only
                # speakers named in the video itself make it into the
                # manifest. Legacy JSON without the field is excluded too.
                if q.get("speaker_confidence") != "named" or is_unknown_speaker(q.get("speaker") or ""):
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
                    "speaker": q.get("speaker") or "",
                    "team": q.get("team") or data.get("channel_team") or "",
                    "text": q.get("text") or "",
                    "news_angle": q.get("news_angle") or "",
                    "social_post": q.get("social_post") or "",
                    "rank": q.get("rank"),
                    "video_title": data.get("video_title") or "",
                    "channel_team": data.get("channel_team") or "",
                    "published": data.get("published") or "",
                    "publish_date": day_dir.name,
                    # Added fields (existing ones above are the clipper's contract).
                    "speaker_confidence": q.get("speaker_confidence") or "",
                    "pull_quote": q.get("pull_quote") or "",
                })
    clips.sort(key=lambda c: (c["published"], -(c["rank"] or 0)), reverse=True)
    if skipped_unnamed:
        log(f"[clips] left out {skipped_unnamed} quote(s) without a speaker named in the video")
    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_slot": run_slot,
        "window_hours": window_hours,
        "count": len(clips),
        "clips": clips,
    }


def write_clip_manifest(manifest: dict) -> None:
    content = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    atomic_write(LATEST_CLIPS_PATH, content)
    atomic_write(CLIPS_ARCHIVE_DIR / f"{manifest['generated_at'][:10]}.json", content)
    log(f"[clips] wrote {manifest['count']} clip(s) to {LATEST_CLIPS_PATH.relative_to(ROOT.parent)}")


# --------------------------------------------------------------------------- #
# Per-video processing
# --------------------------------------------------------------------------- #

def process_video(client, video: dict, team: str) -> tuple[str, dict | None]:
    url = video["url"]
    video_id = video["video_id"]
    day_dir = video_day_dir(video)
    day_dir.mkdir(parents=True, exist_ok=True)
    expected_title = video.get("title") or ""
    duration_secs = int(video.get("duration") or 0)
    log(f"  [meta] {video_id} publish={publish_date(video)} title={expected_title!r} duration={duration_secs}s")

    chunks = compute_chunks(duration_secs)
    if chunks is None:
        log(f"  too-long on {video_id}: {duration_secs}s exceeds {MAX_CHUNKS}h chunking cap")
        (day_dir / f"{video_id}.SKIPPED-too-long").write_text("", encoding="utf-8")
        return "too-long", None

    n_chunks = len(chunks)
    chunk_results: list = []
    hallucinated = 0
    for i, (start_off, end_off) in enumerate(chunks):
        chunk_label = f"chunk {i + 1}/{n_chunks}"
        if start_off is not None:
            log(f"  [{chunk_label}] processing {video_id} from {start_off}s to {end_off}s")
        attempts_parse = 0
        while True:
            try:
                raw_text, _usage = call_gemini_with_retry(
                    client, url, expected_title, team, duration_secs,
                    start_offset_secs=start_off, end_offset_secs=end_off,
                )
            except (TransientServerOverload, SpendingCapExhausted):
                raise
            except Exception as e:
                if gemini_error_status(e) == 400 or is_token_limit_error(e):
                    log(f"  [{chunk_label}] token-limit / 400 on {video_id}: {e}; skipping chunk")
                else:
                    log(f"  [{chunk_label}] unexpected Gemini error on {video_id}: {e}; skipping chunk")
                break
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError as e:
                attempts_parse += 1
                log(f"  [{chunk_label}] malformed JSON (attempt {attempts_parse}): {e}")
                if attempts_parse >= 2:
                    break
                continue
            if not isinstance(data, dict):
                log(f"  [{chunk_label}] JSON was not an object; skipping chunk")
                break
            echoed = (data.get("video_title") or "").strip()
            overlap = title_word_overlap(expected_title, echoed)
            if overlap <= HALLUCINATION_OVERLAP_THRESHOLD:
                hallucinated += 1
                log(f"  [{chunk_label}] [hallucination] title mismatch (overlap {overlap:.0%}): "
                    f"expected {expected_title!r}, got {echoed!r}; skipping chunk")
                break
            offset = start_off or 0
            quotes = [normalize_quote(q, offset, duration_secs, team) for q in (data.get("quotes") or [])]
            data["quotes"] = [q for q in quotes if q]
            chunk_results.append(data)
            break

    if not chunk_results:
        log(f"  all {n_chunks} chunk(s) failed for {video_id}")
        return ("failed-hallucination" if hallucinated == n_chunks else "failed-other"), None

    merged = {
        "video_id": video_id,
        "url": url,
        "video_title": (chunk_results[0].get("video_title") or expected_title).strip(),
        "channel_team": team,
        "published": video.get("published") or "",
        "duration_seconds": duration_secs,
        "processed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": MODEL,
        "speakers_seen": [],
        "quotes": [],
    }
    seen = set()
    for d in chunk_results:
        for s in d.get("speakers_seen") or []:
            if isinstance(s, str) and s.strip() and s not in seen:
                seen.add(s)
                merged["speakers_seen"].append(s)
        merged["quotes"].extend(d["quotes"])
    merged["quotes"] = finalize_quotes(merged["quotes"])
    log(f"  {video_id}: {len(merged['quotes'])} quote(s) after normalisation")
    write_outputs(video, team, merged)
    return "ok", merged


def process_one_video(client, team: str, video: dict, lock, summary, processed_items) -> None:
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
    inner_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"vid-{video_id}")
    future = inner_pool.submit(process_video, client, video, team)
    try:
        try:
            status, data = future.result(timeout=PER_VIDEO_TIMEOUT_SECS)
        except FuturesTimeoutError:
            log(f"  [timeout] video {video_id} exceeded {PER_VIDEO_TIMEOUT_SECS // 60}min, abandoning")
            status, data = "failed-timeout", None
        except SpendingCapExhausted as e:
            _spending_cap_hit.set()
            log(f"  [SPENDING CAP] {video_id}: {e} — aborting remaining queue this run")
            status, data = "aborted-spending-cap", None
        except TransientServerOverload as e:
            _transient_overload_hit.set()
            log(f"  [DEFERRED] {video_id}: Gemini overload after retries ({e}) — deferring rest of queue")
            status, data = "deferred-transient", None
        except Exception as e:
            log(f"  unexpected exception processing {video_id}: {e}")
            for line in traceback.format_exc().rstrip().splitlines():
                log(f"    {line}")
            status, data = "failed-other", None
    finally:
        inner_pool.shutdown(wait=False)

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
            quotes = data.get("quotes") or []
            top = quotes[0] if quotes else {}
            processed_items.append({
                "video_id": video_id,
                "title": data.get("video_title") or video.get("title") or video_id,
                "channel": team,
                "top_quote": top.get("text", ""),
                "speaker": top.get("speaker", ""),
                "social_post": top.get("social_post", ""),
                "clip_count": len(quotes),
                "date": publish_date(video),
                "is_one_off": bool(video.get("is_one_off")),
            })
        elif status == "deferred-transient":
            _deferred_items.append(deferred_record)


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
    if int(video.get("duration") or 0) < min_secs:
        return f"duration {video.get('duration')}s below {min_secs}s minimum"
    return ""


def title_filter(title: str, config: dict) -> str:
    """'' if the title looks like a presser, otherwise a skip reason."""
    kw = title_keyword_hit(title, config.get("exclude_title_keywords", []))
    if kw:
        return f"title contains excluded '{kw}'"
    if not title_keyword_hit(title, config.get("include_title_keywords", [])):
        return "title has no presser keyword"
    return ""


def install_script_timeout() -> None:
    if not hasattr(signal, "SIGALRM"):
        log("signal.SIGALRM not available on this platform; script-level timeout disabled")
        return

    def _handler(signum, frame):
        log(f"[timeout] script-level {SCRIPT_TIMEOUT_SECS // 60}min budget exceeded, exiting with partial output")
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(3)

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

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run_slot = determine_run_slot()
    log(f"Run slot: {run_slot}")

    config = load_config()
    window_hours = int(config.get("window_hours", 48))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    channels = [c for c in config.get("channels", []) if c.get("active", True)]
    team_by_channel_id = {c.get("channel_id"): c.get("team") for c in config.get("channels", [])}
    max_total = int(config.get("max_videos_per_run", 30))
    max_per_channel = int(config.get("max_videos_per_channel_per_run", 6))

    def finish_without_videos() -> int:
        regenerate_index()
        write_clip_manifest(build_clip_manifest(run_slot, window_hours))
        queue_slack_payload({"kind": "no_new_videos", "date_str": today})
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
    state_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=VIDEO_WORKERS) as ex:
        futures = [ex.submit(process_one_video, client, team, video, state_lock, summary, processed_items)
                   for team, video in queued]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                log(f"  unhandled worker exception: {e}")

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

    log(f"Done. {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
