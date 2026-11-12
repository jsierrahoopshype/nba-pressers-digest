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

A second text-only call per video (structured fields, its own prompt; the
clip-fields call is unchanged) adds each quote's speaker role, subjects,
teams, topics, context and best flag under "structured", read only by
structured_quotes.py. A failed call leaves the video without it.

Outputs (pressers_v2/output/):
  <publish-date>/<video_id>.md / .json   yt-quotes format (+ clip fields in the JSON)
  <publish-date>/digest-<slot>.md, podcasts-<slot>.md, oneoffs-<slot>.md
                                         one page per type and run slot (+ the
                                         day's digest.md / podcasts.md / oneoffs.md)
  latest_clips.json, clips/<date>.json   clip manifest (named speakers only)

Usage (from repo root):
    python pressers_v2/presser_extractor.py
"""

import json
import math
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

import slack_state
import ytq_vendor as ytq

load_dotenv()

MODEL = ytq.MODEL
ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ytq.OUTPUT_DIR                       # pressers_v2/output (same object yt-quotes code writes to)
CONFIG_PATH = ROOT / "config.json"
LATEST_CLIPS_PATH = OUTPUT_DIR / "latest_clips.json"
CLIPS_ARCHIVE_DIR = OUTPUT_DIR / "clips"
SLACK_PAYLOAD_PATH = ROOT / ".slack_payload.json"
SLACK_STATE_PATH = OUTPUT_DIR / "slack_alerted.json"   # committed: pending + alerted video ids
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
    "podcast": ["podcast"],
    "presser": ["press conference", "presser", "media availability", "availability", "media day",
                "postgame", "post-game", "post game", "pregame", "pre-game", "pre game", "shootaround",
                "practice", "interview", "speaks", "talks", "previews", "introductory"],
}
_CONFIG: dict = {}   # set in main(); classification reads its keyword lists

# Podcasts & shows come from hoopshype-yt-quotes' podcast rotation (its own
# channels.json): the workflow checks out its output for the last few days
# and imports the videos it already transcribed (no new video call here).
YTQ_SOURCE = "hoopshype-yt-quotes"
YTQ_IMPORT_MAX_PER_RUN = 25
REPORTER = "Unidentified reporter"


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #

log = ytq.log


def set_output_dir(path: Path) -> None:
    """Point both this module and the vendored yt-quotes code at one folder."""
    global OUTPUT_DIR, LATEST_CLIPS_PATH, CLIPS_ARCHIVE_DIR, SLACK_STATE_PATH
    OUTPUT_DIR = ytq.OUTPUT_DIR = Path(path)
    LATEST_CLIPS_PATH = OUTPUT_DIR / "latest_clips.json"
    CLIPS_ARCHIVE_DIR = OUTPUT_DIR / "clips"
    SLACK_STATE_PATH = OUTPUT_DIR / "slack_alerted.json"


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


# Jorge, Nov 10 2026: a daytime run (14utc / 20utc) belongs to the day it
# was scheduled for, however late GitHub starts it. The Nov 9 20:15 run
# started at 02:43 UTC on Nov 10, and its videos (published after midnight
# UTC) made Nov 10 "20utc" pages before that slot had run. SLOT_DATE is the
# scheduled day of this run; a video published after it is filed there.
SLOT_DATE = ""
_HOURLY_SLOT_RE = re.compile(r"^(\d{2})utc$")


def scheduled_date(run_id: str) -> str:
    """The day a run was scheduled for. '2026-11-10T0243Z-20utc' (the 20:15
    cron starting 02:43 the next day) -> '2026-11-09'; '2026-11-08T1849Z-14utc'
    -> '2026-11-08'. Overnight and manual runs: the day they started. ''
    when the run id can't be read."""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:T(\d{2})(\d{2})Z)?-(.+)$", run_id or "")
    if not m:
        return ""
    day, hh, slot = m.group(1), m.group(2), m.group(4)
    h = _HOURLY_SLOT_RE.match(slot)
    if h and hh is not None and int(hh) < int(h.group(1)):
        # started earlier in the day than its slot: the previous day's run, delayed
        return (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    return day


def filed_date(date_str: str, run_id: str = "") -> str:
    """The day folder a video goes in: its publish date, or the run's
    scheduled date when it was published after that day (a late daytime run)."""
    sd = scheduled_date(run_id) if run_id else SLOT_DATE
    if sd and _HOURLY_SLOT_RE.match(run_slot_of(run_id) if run_id else run_slot_of(RUN_ID)) and date_str > sd:
        return sd
    return date_str


def video_day_dir(video: dict) -> Path:
    return OUTPUT_DIR / filed_date(publish_date(video))


OVERNIGHT_SLOT = "overnight"   # the hourly overnight triggers all share this slot


def determine_run_slot() -> str:
    """digest-14utc.md for scheduled runs, digest-manual-HHMM.md otherwise.
    PRESSERS_RUN_SLOT (set by the workflow) wins when present: the overnight
    cron fires every hour 01-10 UTC and all those runs share one slot."""
    explicit = re.sub(r"[^A-Za-z0-9_-]", "", os.getenv("PRESSERS_RUN_SLOT") or "")
    if explicit:
        return explicit
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
                          config: dict | None = None, forced: str = "") -> tuple[str, str]:
    """-> (content_type, source). extra_videos are always "oneoff". Otherwise
    the config's content_type_keywords decide, checked in order (podcast
    first, so "Chase Down Podcast Live: Media Day Reactions" is a podcast);
    whole-word, case-insensitive. No keyword -> Gemini's content_type ->
    "presser"."""
    if is_one_off:
        return "oneoff", "extra_videos"
    if forced in CONTENT_TYPES:
        # Channel-level override (config.json "content_type" on the channel),
        # e.g. every HoopsHype upload is filed as a press conference.
        return forced, "channel"
    cfg = config if config is not None else _CONFIG
    keywords = cfg.get("content_type_keywords") or DEFAULT_CONTENT_TYPE_KEYWORDS
    low = (title or "").lower()
    for ctype, words in keywords.items():
        if ctype not in CONTENT_TYPES:
            continue
        for kw in words:
            if re.search(rf"(?<![a-z0-9]){re.escape(kw.lower())}(?![a-z0-9])", low):
                return ctype, f"keyword: {kw}"
    # Jorge, Nov 9 2026: a team-channel upload is a press conference unless
    # its title says "podcast". Gemini's guess no longer files team videos as
    # podcasts, and neither do "live" / "show" (the Pelicans' "LIVE: ...
    # Postgame Interviews" streams). Real podcasts come from the yt-quotes rotation.
    g = str(gemini_value or "").strip().lower()
    if g == "presser":
        return g, "gemini"
    return "presser", "default"


def reclassify_stored(day_dirs: list) -> int:
    """Re-run classify_content_type on stored team-channel videos (not
    one-offs, channel overrides or imported podcasts) so the old "live" /
    Gemini podcast guesses become press conferences. Returns how many
    changed; their .json is rewritten (the .md does not depend on it)."""
    changed = 0
    for day_dir in day_dirs:
        for js in sorted(Path(day_dir).glob("*.json")):
            if not VIDEO_ID_RE.match(js.stem):
                continue
            try:
                data = json.loads(js.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or data.get("is_one_off") or data.get("source") == YTQ_SOURCE:
                continue
            if data.get("content_type_source") in ("channel", "extra_videos"):
                continue
            new = classify_content_type(data.get("video_title") or "")
            if data.get("content_type") != new[0]:
                log(f"  [types] {js.stem}: {data.get('content_type')} -> {new[0]} ({new[1]})")
                data["content_type"], data["content_type_source"] = new
                atomic_write(js, json.dumps(data, ensure_ascii=False, indent=2))
                changed += 1
    return changed


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
# reader's computer. "Send to Slack" next to it is the same link plus
# slack=1: the tool makes the clip (or finds it made), then sends it to
# #socialmedia. Built from the block's own header number and URL, so it also
# works on stored files; a block that already has the line is skipped, and an
# older "Clip it"-only line is brought up to date.
_QUOTE_HEADER_RE = re.compile(r"^\*\*(\d+)\.")
_TS_LINK_LINE_RE = re.compile(r"^\[https://www\.youtube\.com/watch\?v=([A-Za-z0-9_-]{11})&t=(\d+)s\]"
                              r"\(https://www\.youtube\.com/watch\?v=\1&t=\2s\)$")
CLIP_LINK_LINE_RE = re.compile(r"^<small>\[Clip it\]\(presserclips://clip\?v=([A-Za-z0-9_-]{11})&t=(\d+)&q=(\d+)\)"
                               r" · \[Send to Slack\]\(presserclips://clip\?v=\1&t=\2&q=\3&slack=1\)</small>$")
OLD_CLIP_LINK_LINE_RE = re.compile(r"^<small>\[Clip it\]\(presserclips://clip\?v=([A-Za-z0-9_-]{11})&t=(\d+)&q=(\d+)\)</small>$")


def clip_link_line(video_id: str, secs: int, rank: int) -> str:
    link = f"presserclips://clip?v={video_id}&t={int(secs)}&q={int(rank)}"
    return f"<small>[Clip it]({link}) · [Send to Slack]({link}&slack=1)</small>"


def add_clip_links(md: str) -> str:
    lines = [OLD_CLIP_LINK_LINE_RE.sub(lambda m: clip_link_line(m[1], int(m[2]), int(m[3])), line)
             for line in md.split("\n")]
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
            # A "Best of the best" entry carries its own link (the quote's real
            # number) after its "From:" line; adding one here put a second
            # pair with the block's display number (Nov 7: q=1 next to q=5).
            has_one = has_one or (i + 4 < len(lines) and lines[i + 1] == "" and lines[i + 2].startswith("From: ")
                                  and lines[i + 3] == "" and CLIP_LINK_LINE_RE.match(lines[i + 4]))
            if not has_one:
                out.extend(["", clip_link_line(t.group(1), int(t.group(2)), rank)])
            rank = None
    return "\n".join(out)


def drop_extra_best_clip_links(md: str) -> str:
    """Older digests: remove the wrong clip pair that add_clip_links put
    between a Best-of-the-best entry's timestamp link and its "From:" line
    (that entry's own pair, after "From:", is the right one)."""
    lines = md.split("\n")
    out, i = [], 0
    while i < len(lines):
        if (CLIP_LINK_LINE_RE.match(lines[i]) and out and out[-1] == "" and len(out) >= 2
                and _TS_LINK_LINE_RE.match(out[-2]) and i + 4 < len(lines) and lines[i + 1] == ""
                and lines[i + 2].startswith("From: ") and lines[i + 3] == ""
                and CLIP_LINK_LINE_RE.match(lines[i + 4])):
            out.pop()          # the blank line before the wrong pair
            i += 1             # the wrong pair itself
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def from_line_title(title: str) -> str:
    """Video title for a "From:" line: a bare "|" made kramdown render the
    line as a table, so pipes are escaped (shown as plain "|")."""
    return (title or "").replace("|", "\\|")


# --------------------------------------------------------------------------- #
# Reporter questions (Jorge, Nov 9 2026): Gemini sometimes credits a
# reporter's question to the player ("Brayden Burries" quotes 1-2 on Nov 9).
# A quote that is only a question is relabeled "Unidentified reporter" and
# left off the pages; a question glued to the start of an answer becomes its
# own "Unidentified reporter" block, so the player is credited only with his
# answer. Applied to press conferences only (podcast hosts ask on purpose).
# A small text-only Gemini call (derive_question_words) decides; the rules
# below are the fallback when that call fails, and lean towards leaving a
# quote alone.
# --------------------------------------------------------------------------- #

REPORTER_CHECK_VERSION = 2   # 2: Gemini and the rules together (Gemini alone missed Burries 1-2)
_Q_WORDS = r"(?:what|how|why|when|where|which|who|did|do|does|can|could|would|will|is|are|was|were|have|has)"
_ASKS_YOU_RE = re.compile(rf"(?i)\b{_Q_WORDS}\b[^?]*\byou(?:r|rs|rself|'re|'ve|'ll)?\b")
_STARTS_WITH_Q_RE = re.compile(rf"(?i)^\W*(?:and |so |but |okay,? |ok,? |well,? )?{_Q_WORDS}\b")
_FIRST_PERSON_RE = re.compile(r"(?i)\b(?:i|i'm|i've|i'll|i'd|me|my|myself|we|we're|we've|us|our|ours)\b")
_FILLER_Q_RE = re.compile(r"(?i)(?:you know what i mean|you know|right|know what i'm saying|know what i'm talking about"
                          r"|you feel me|feel me|you see|isn't it|ain't it)\s*\?\s*$")
_ADDRESS_RE = re.compile(r"(?i)\b(?:you guys|y'all|you all|your team|for you)\b")
REPORTER_MIN_ANSWER_WORDS = 8
REPORTER_MAX_PLAIN_WORDS = 45


def is_reporter_question(seg: str) -> bool:
    """True for text that reads as a question put to the speaker: it ends
    with "?" and either asks "you" something or opens with a question word
    and never speaks in the first person ("What does do better mean?")."""
    s = " ".join((seg or "").split())
    if len(s.split()) < 4 or not s.endswith("?") or _FILLER_Q_RE.search(s):
        return False
    # Judge the closing sentence, the question itself: an answer that ends in
    # "..., you know what I'm saying?" or a rhetorical "why not us?" is not one.
    last = re.split(r"(?<=[.!?])\s+", s)[-1]
    if len(last.split()) < 4 or _FIRST_PERSON_RE.search(last):
        return False
    if len(s.split()) > REPORTER_MAX_PLAIN_WORDS and not _ADDRESS_RE.search(s):
        return False   # a long stretch with no "you guys" is more likely the speaker's own answer
    return bool(_ASKS_YOU_RE.search(last) or _STARTS_WITH_Q_RE.match(last))


def split_leading_question(text: str) -> tuple[str, str] | None:
    """(question, answer) when text opens with a reporter question followed
    by an answer of REPORTER_MIN_ANSWER_WORDS+ words; ("" , text) never."""
    s = (text or "").strip()
    found, prev = None, 0
    for m in re.finditer(r"\?", s):
        head, tail = s[:m.end()].strip(), s[m.end():].strip(" \"'")
        if len(tail.split()) < REPORTER_MIN_ANSWER_WORDS:
            break
        if found is None:
            if is_reporter_question(head):
                found = (head, tail)
        elif is_reporter_question(s[prev:m.end()]):
            found = (head, tail)    # a follow-up question right after the first one
        else:
            break
        prev = m.end()
    return found


def quote_visible(q: dict) -> bool:
    """Shown on the pages: not a bare reporter question, not an answer
    that a single-speaker video of the same session already carries, and
    (podcasts) not a quote about another sport."""
    return not q.get("reporter_question") and not q.get("duplicate_of") and not q.get("non_nba")


def speaker_own_text(q: dict) -> str:
    """The speaker's own words: the reporter block of a split quote left out."""
    blocks = q.get("text_blocks") or []
    if blocks and any((b.get("speaker") or "") == REPORTER for b in blocks if isinstance(b, dict)):
        return " ".join((b.get("text") or "").strip() for b in blocks
                        if isinstance(b, dict) and (b.get("speaker") or "") != REPORTER).strip()
    return ytq._canonical_quote_text(q)


def reporter_share(q: dict) -> float:
    """Share of the quote's words that belong to a reporter question."""
    if q.get("reporter_question"):
        return 1.0
    total = ytq._word_count(ytq._canonical_quote_text(q))
    return 1 - ytq._word_count(speaker_own_text(q)) / total if total else 0.0


QUESTION_CHECK_PROMPT = """You are given quotes already extracted from an NBA press conference or media availability video (text only). The extractor sometimes glued a reporter's question onto the start of the speaker's answer, or returned a reporter's question on its own as if the speaker said it.

For each quote, return "question_words": how many words at the START of its text are a question (with any lead-in) asked BY A REPORTER or interviewer TO the speaker. 0 when the text starts with the speaker's own words. The whole word count when the entire text is the reporter's question. The speaker's own rhetorical questions ("what are you left with as a player?", "are you going to let him score?") are part of the answer: count them as 0.

Return ONLY JSON: {"quotes": [{"rank": 1, "question_words": 0}]}"""


def derive_question_words(client, data: dict) -> dict | None:
    """One text-only call: {rank: words of a leading reporter question}.
    None when there is no client or the call fails (the rules then decide)."""
    quotes = data.get("quotes") or []
    if client is None or not quotes:
        return None
    payload = {"video_title": data.get("video_title") or "",
               "quotes": [{"rank": q.get("rank"), "speaker": q.get("speaker") or "",
                           "words": ytq._word_count(ytq._canonical_quote_text(q)),
                           "text": ytq._canonical_quote_text(q)} for q in quotes]}
    try:
        response = with_retry(lambda: client.models.generate_content(
            model=MODEL,
            contents=[QUESTION_CHECK_PROMPT, json.dumps(payload, ensure_ascii=False)],
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1),
        ), "question-check")
        parsed = json.loads(response.text or "")
    except ytq.SpendingCapExhausted as e:
        _spending_cap_hit.set()
        log(f"  [question-check] [SPENDING CAP] {e}; using the rules")
        return None
    except Exception as e:
        log(f"  [question-check] failed ({type(e).__name__}: {str(e)[:150]}); using the rules")
        return None
    out = {}
    for item in (parsed.get("quotes") if isinstance(parsed, dict) else None) or []:
        if isinstance(item, dict) and item.get("rank") is not None:
            try:
                out[item["rank"]] = max(0, int(item.get("question_words") or 0))
            except (TypeError, ValueError):
                continue
    return out


def _cut_after_words(text: str, n: int) -> tuple[str, str] | None:
    """Split text after its first n words, snapped to the nearest sentence
    end ("?" preferred) within a few words. None when no clean cut exists."""
    ends = [m.end() for m in re.finditer(r"[?.!](?=\s|$)", text)]
    best = None
    for pos in ends:
        k = len(text[:pos].split())
        score = abs(k - n) - (2 if text[pos - 1] == "?" else 0)
        if abs(k - n) <= 6 and (best is None or score < best[0]):
            best = (score, pos)
    if best is None:
        return None
    head, tail = text[:best[1]].strip(), text[best[1]:].strip(" \"'")
    return (head, tail) if head and tail else None


def _split_quote(q: dict, question: str, answer: str) -> None:
    speaker = q.get("speaker") or ""
    q["text_blocks"] = [{"speaker": REPORTER, "text": question}, {"speaker": speaker, "text": answer}]
    q["speakers"] = [REPORTER, speaker]
    q["quote"] = ""
    q["reporter_split"] = True
    if (q.get("excerpt") or "") and (q.get("excerpt") or "") not in answer:
        words = answer.split()
        first = re.split(r"(?<=[.!?])\s+", answer, maxsplit=1)[0].split()
        q["excerpt"] = " ".join((first if 6 <= len(first) <= 25 else words[:20]))


def mark_reporter_questions(data: dict, question_words: dict | None = None) -> bool:
    """Label reporter questions (see above): a quote is labeled when
    question_words (from derive_question_words) or the rules
    (is_reporter_question / split_leading_question) find one. Idempotent
    (data["reporter_check"]); returns True when anything changed."""
    if data.get("reporter_check") == REPORTER_CHECK_VERSION:
        return False
    for q in data.get("quotes") or []:
        blocks = q.get("text_blocks") or []
        text = ytq._canonical_quote_text(q)
        total = ytq._word_count(text)
        if question_words is not None and question_words.get(q.get("rank"), 0) > 0:
            n = question_words[q.get("rank")]
            if total - n < REPORTER_MIN_ANSWER_WORDS // 2 + 1:
                q.update(reporter_question=True, speaker=REPORTER, speaker_confidence="inferred")
                continue
            if not blocks and n >= 4:
                parts = _cut_after_words(text, n)
                if parts and ytq._word_count(parts[1]) >= REPORTER_MIN_ANSWER_WORDS // 2 + 1:
                    _split_quote(q, *parts)
                    continue
        # Gemini said no question (or wasn't asked): the conservative rules
        # still catch the clear cases (on Nov 9 it missed Burries 1 and 2).
        if blocks:
            for b in blocks:
                if isinstance(b, dict) and (b.get("speaker") or "") != REPORTER and is_reporter_question(b.get("text") or ""):
                    b["speaker"] = REPORTER
            if all(isinstance(b, dict) and b.get("speaker") == REPORTER for b in blocks):
                q.update(reporter_question=True, speaker=REPORTER, speaker_confidence="inferred")
            elif isinstance(q.get("speakers"), list):
                q["speakers"] = [b.get("speaker") or "" for b in blocks if isinstance(b, dict)]
            continue
        if is_reporter_question(text):
            q.update(reporter_question=True, speaker=REPORTER, speaker_confidence="inferred")
            continue
        parts = split_leading_question(text)
        if parts:
            _split_quote(q, *parts)
    data["reporter_check"] = REPORTER_CHECK_VERSION
    data["reporter_check_source"] = "gemini" if question_words is not None else "rules"
    return True


def render_markdown(video_id: str, channel_name: str, data: dict) -> str:
    """yt-quotes' to_markdown, unchanged, on a Pages-safe copy of the data,
    with its bare YouTube URLs turned into links (see link_bare_urls) and a
    "Clip it" link after each quote block (see add_clip_links). Bare
    reporter questions and answers duplicated from a single-speaker video
    are left out (quote_visible); the others keep their own numbers."""
    shown = dict(data, quotes=[q for q in data.get("quotes") or [] if quote_visible(q)])
    return add_clip_links(link_bare_urls(
        ytq.to_markdown(WATCH_URL_TEMPLATE.format(video_id=video_id), channel_name, pages_safe(shown))))


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
- "social_score": integer 1-10 for how much NBA fans would enjoy, share or react to the quote on social media, separate from news value. 9-10: funny, candid or personal moments (a player opening up about family, a self-deprecating joke, a surprising admission, a memorable one-liner, trash talk, an unusual story). 6-8: a vivid player opinion on a teammate or rival, a revealing behind-the-scenes detail. 3-5: ordinary but pleasant. 1-2: X's and O's, tactics, rotation talk, rebounding/turnover math, cliches ("one game at a time", "credit to them"). Be strict: most coach tactics answers are 1-3.
- "social_post": a draft post for X / Bluesky, at most 240 characters. ONE idea; lead with the news, punchy and conversational; may include a short verbatim phrase in quotation marks. NO hashtags, NO emojis, NO em-dashes or en-dashes, no "BREAKING", nothing the speaker did not say.

Also return the video's "content_type": "presser" for a press conference, media availability, media day session, pregame/postgame or shootaround interview; "podcast" for a podcast, talk show, reaction show or hosted livestream/broadcast.

Return ONLY JSON: {"content_type": "presser", "quotes": [{"rank": 1, "speaker_confidence": "named", "news_score": 6, "social_score": 4, "social_post": "..."}]}"""


# Podcast imports only (Jorge, Nov 10 2026): the same clip-fields call also
# says, per quote, whether it is about the NBA. The yt-quotes rotation has
# shows that cover other sports; their non-NBA quotes are kept in the .json
# (non_nba) but left off the podcasts pages and the clip list. A mixed show
# keeps its NBA quotes. Press conferences send CLIP_FIELDS_PROMPT unchanged.
PODCAST_NBA_RULE = """

This is a podcast or show that may cover other sports. For each quote ALSO return "nba": true when the quote is about NBA basketball (NBA players, coaches, teams, games, trades, the league, or an NBA player's life and career, past or present), false when it is about another sport or league (NFL, MLB, college football, golf, soccer, WNBA...) or about nothing basketball-related. A quote that mentions an NBA player only in passing while being about another sport is false.

Each quote object then looks like: {"rank": 1, "speaker_confidence": "named", "news_score": 6, "social_score": 4, "social_post": "...", "nba": true}"""


def _is_nba(value) -> bool:
    """Gemini's "nba" flag; anything but an explicit false keeps the quote."""
    if isinstance(value, str):
        return value.strip().lower() != "false"
    return value is not False


def apply_nba_flags(data: dict, derived: dict) -> int:
    """Mark the quotes the clip-fields answer called non-NBA. Returns how many."""
    by_rank = derived.get("by_rank") or {}
    n = 0
    for q in data.get("quotes") or []:
        d = by_rank.get(q.get("rank"))
        if d is not None and not _is_nba(d.get("nba", True)):
            q["non_nba"] = True
            n += 1
        else:
            q.pop("non_nba", None)
    return n


def derive_clip_fields(client, data: dict, team: str, extra_rules: str = "") -> dict:
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
            contents=[CLIP_FIELDS_PROMPT + extra_rules, json.dumps(payload, ensure_ascii=False)],
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


# Third Gemini call per video (text only, same model and key): the fields of
# the structured quote records (structured_quotes.py). Separate from the
# clip-fields call so news_score and social_post come out exactly as before.
STRUCTURED_FIELDS_PROMPT = """You are given quotes already extracted from an NBA press conference or media availability video (text only, no video). For each quote, return:
- "rank": the same rank you were given.
- "speaker_role": "player", "coach" or "exec". "exec" is anyone in the front office or ownership: general manager (GM), president of basketball operations, team president, owner, governor, any other executive. A GM or a president is ALWAYS "exec", never "coach". "coach" is the head coach or an assistant coach. Judge from the video title, speakers_seen and what the speaker says.
- "subjects": full names of the people the quote is ABOUT (a teammate, an opponent, a coach, an executive), never the speaker. Use the full name when the video title or the quotes make it clear. [] when the quote is about no person in particular.
- "teams": three-letter abbreviations (LAL, BOS, NYK...) of the NBA teams the quote is about, plus the speaker's own team (the channel).
- "topics": 0 to 2 values, ONLY from this list: "contracts", "injuries", "trades", "teammates", "coaching decisions". Only when the quote is clearly about it; [] otherwise.
- "context": ONE plain sentence, at most 30 words, that a reader needs to understand the quote (who is speaking, on what occasion, what they were asked or reacting to). No opinion, no quotation marks, nothing the quotes or title do not say.
- "best": true for AT MOST ONE quote in the whole video, and only when that quote is real news (an injury, a contract, a trade, a role or lineup change, a decision) or a strong opinion about a named person (that person goes in "subjects"). false for everything else: filler, cliches and stock phrases ("we just have to keep working", "one game at a time", "credit to them"), mild praise, game recaps and questions. Zero best quotes in a video is fine and common.

Return ONLY JSON: {"quotes": [{"rank": 1, "speaker_role": "player", "subjects": ["Evan Mobley"], "teams": ["CLE"], "topics": ["teammates"], "context": "...", "best": false}]}"""

# Short id of the prompt, stored with every answer: a changed prompt makes
# structured_quotes.py ask again for the stored videos (a new history snapshot).
STRUCTURED_PROMPT_ID = __import__("hashlib").sha1(STRUCTURED_FIELDS_PROMPT.encode("utf-8")).hexdigest()[:8]

STRUCTURED_ROLES = ("player", "coach", "exec")
STRUCTURED_TOPICS = ("contracts", "injuries", "trades", "teammates", "coaching decisions")
_EXEC_ROLE_WORDS = {"executive", "exec", "front office", "owner", "governor", "president", "general manager", "gm",
                    "president of basketball operations"}


def _str_list(value, limit: int = 12) -> list:
    out = []
    for v in value if isinstance(value, list) else []:
        s = " ".join(str(v or "").split())
        if s and s not in out:
            out.append(s)
    return out[:limit]


def structured_fields(d: dict, speaker: str) -> dict | None:
    """One quote of the structured-fields answer, cleaned up: role from a
    fixed list (GM / president / owner -> exec), subjects without the
    speaker, uppercase team abbreviations, 0-2 topics from STRUCTURED_TOPICS,
    one context sentence, a real boolean for best. None for no answer."""
    keys = ("speaker_role", "subjects", "teams", "topics", "context", "best")
    if not isinstance(d, dict) or not any(k in d for k in keys):
        return None
    role = " ".join(str(d.get("speaker_role") or "").strip().lower().split())
    role = "exec" if role in _EXEC_ROLE_WORDS else "coach" if "coach" in role else role
    sp = " ".join(_name_tokens(speaker))
    subjects = [s for s in _str_list(d.get("subjects")) if " ".join(_name_tokens(s)) != sp]
    teams = [t.upper() for t in _str_list(d.get("teams")) if re.fullmatch(r"[A-Za-z]{2,4}", t)]
    topics = [t for t in (s.lower() for s in _str_list(d.get("topics"))) if t in STRUCTURED_TOPICS][:2]
    best = d.get("best")
    if isinstance(best, str):
        best = best.strip().lower() == "true"
    return {
        "speaker_role": role if role in STRUCTURED_ROLES else "",
        "subjects": subjects,
        "teams": teams,
        "topics": topics,
        "context": strip_dashes(" ".join(str(d.get("context") or "").split()))[:300],
        "best": best is True,
    }


def derive_structured_fields(client, data: dict, team: str) -> dict | None:
    """The third call: one text-only call over the extracted quotes.
    Returns {rank: structured fields} ({} = no quotes), or None when the call
    fails (the video then has no "structured" block; structured_quotes.py
    falls back to its own rules and marks those records)."""
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
            contents=[STRUCTURED_FIELDS_PROMPT, json.dumps(payload, ensure_ascii=False)],
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.3),
        ), "structured-fields")
        parsed = json.loads(response.text or "")
    except ytq.SpendingCapExhausted as e:
        _spending_cap_hit.set()
        log(f"  [structured-fields] [SPENDING CAP] {e}; none stored")
        return None
    except Exception as e:
        log(f"  [structured-fields] failed ({type(e).__name__}: {str(e)[:150]}); none stored")
        return None
    if not isinstance(parsed, dict):
        return None
    speakers = {q.get("rank"): q.get("speaker") or "" for q in quotes}
    out = {}
    for item in parsed.get("quotes") or []:
        if isinstance(item, dict) and item.get("rank") in speakers:
            f = structured_fields(item, speakers[item["rank"]])
            if f is not None:
                f["prompt"] = STRUCTURED_PROMPT_ID
                out[item["rank"]] = f
    one_best(out, {q.get("rank"): q.get("news_score") for q in quotes})
    return out if out else None


def one_best(by_rank: dict, scores: dict) -> None:
    """At most one best quote per video, and never one about no named person:
    the model's picks without subjects are dropped, then the highest news
    score (earliest rank on a tie) keeps the flag."""
    picks = [r for r, f in by_rank.items() if f.get("best") and f.get("subjects")]
    keep = min(picks, key=lambda r: (-(scores.get(r) or 0), r)) if picks else None
    for r, f in by_rank.items():
        f["best"] = r == keep


def apply_structured_fields(data: dict, by_rank: dict | None) -> None:
    for q in data.get("quotes") or []:
        f = (by_rank or {}).get(q.get("rank"))
        if f is not None:
            q["structured"] = f


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
            "social_score": _news_score(d.get("social_score")),
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
    ytq_video = {k: v for k, v in video.items() if k not in ("is_one_off", "force_content_type")}
    if filed_date(publish_date(video)) != publish_date(video):
        # yt-quotes' own writer files by publish date; send it to the scheduled day
        ytq_video["published"] = f"{filed_date(publish_date(video))}T23:59:59Z"
    _tl.duration = int(video.get("duration") or 0)
    status, data = ytq.process_video(client, ytq_video, team, [], [])
    if status != "ok" or not data:
        return status, data

    if classify_content_type(data.get("video_title") or video.get("title") or "", is_one_off,
                             forced=video.get("force_content_type") or "")[0] == "presser":
        # before scoring, so a bare question is scored as the reporter's
        mark_reporter_questions(data, derive_question_words(client, data))
    derived = derive_clip_fields(client, data, team)
    apply_clip_fields(data, derived, team, int(video.get("duration") or 0))
    apply_structured_fields(data, derive_structured_fields(client, data, team))
    data["content_type"], data["content_type_source"] = classify_content_type(
        data.get("video_title") or video.get("title") or "", is_one_off, derived.get("content_type"),
        forced=video.get("force_content_type") or "")
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
                "date": video_day_dir(video).name,
                "is_one_off": bool(video.get("is_one_off")),
                "content_type": data.get("content_type") or "presser",
                # Slack quote dedupe keeps a single-speaker video over a full media session
                "single_speaker": bool(_single_speaker(data)),
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


def videos_with_run_id(day_dir: Path, run_id: str) -> list:
    """Video ids in day_dir whose stored JSON carries this run_id (the
    overnight slot shares one run_id across its hourly runs)."""
    out = []
    if not run_id or not day_dir.is_dir():
        return out
    for js in sorted(day_dir.glob("*.json")):
        if not VIDEO_ID_RE.match(js.stem):
            continue
        try:
            data = json.loads(js.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("run_id") == run_id:
            out.append(js.stem)
    return out


# "Best of the best": the top ~10% of the digest's quotes, repeated at the
# top so the HoopsHype-worthy ones are read first. Purely additive: built
# from the stored .json (news_score + the structured "best" flag), no new
# Gemini call, and every section below it is unchanged.
TOP_QUOTES_SHARE = 0.10
TOP_QUOTES_PER_VIDEO = 2          # one long presser can't fill the whole block
TOP_QUOTES_HEADER = "# Best of the best"
# Jorge, Nov 7 2026: players first. A coach or exec only makes the block with
# real news (injury, trade, contract: news_score >= 8), and never fills more
# than a quarter of it. Fun player moments rank by social_score.
TOP_COACH_MIN_NEWS = 8
TOP_COACH_MAX_SHARE = 0.25
_HEAD_COACHES = None


def _head_coaches() -> set:
    """Head coach names from teams_2026_27.json (fallback when a quote has
    no structured speaker_role)."""
    global _HEAD_COACHES
    if _HEAD_COACHES is None:
        try:
            teams = json.loads((Path(__file__).resolve().parent / "teams_2026_27.json").read_text(encoding="utf-8"))["teams"]
            _HEAD_COACHES = {" ".join(_name_tokens(t.get("head_coach") or "")) for t in teams.values()} - {""}
        except (OSError, ValueError, KeyError, AttributeError):
            _HEAD_COACHES = set()
    return _HEAD_COACHES


def _is_coach_or_exec(q: dict) -> bool:
    role = (q.get("structured") or {}).get("speaker_role") or ""
    if role:
        return role in ("coach", "exec")
    return " ".join(_name_tokens(q.get("speaker") or "")) in _head_coaches()


TOP_SECTION_HEADERS = {"presser": TOP_QUOTES_HEADER,
                       "podcast": TOP_QUOTES_HEADER,
                       "oneoff": TOP_QUOTES_HEADER}   # each type has its own page now (Nov 9 2026)
# Jorge, Nov 10 2026: every page with a quote leads with the block, at least
# 3 picks (or every quote that qualifies when there are fewer than 3); a page
# with fewer than 3 speakers repeats a speaker to get there.
TOP_MIN_PICKS = 3
TOP_SECTION_MIN = {"presser": TOP_MIN_PICKS, "podcast": TOP_MIN_PICKS, "oneoff": TOP_MIN_PICKS}
TOP_MAX_REPORTER_SHARE = 0.5      # a quote that is mostly a reporter question never qualifies


def top_quotes_blocks(md_paths: list) -> list:
    """One "Best of the best" block per content type, in type order (kept for
    callers that still build one block per type; the pages use one each)."""
    by_type = {ctype: [] for ctype in CONTENT_TYPES}
    for md_path in md_paths:
        by_type[_video_type_for_md(md_path)].append(md_path)
    lines = []
    for ctype in CONTENT_TYPES:
        if by_type[ctype]:
            lines.extend(top_quotes_block(by_type[ctype], header=TOP_SECTION_HEADERS[ctype],
                                          min_quotes=TOP_SECTION_MIN[ctype]))
    return lines


def top_quotes_block(md_paths: list, header: str = TOP_QUOTES_HEADER, coach_rule: bool = True,
                     min_quotes: int = 1, hidden: dict | None = None) -> list:
    """Markdown lines for the "Best of the best" block ([] when nothing is
    scored). Ranking: news_score / social_score, then the structured "best"
    flag, then the page's own order. Unidentified speakers, reporter
    questions (or quotes that are mostly one), hidden duplicates and
    unscored quotes never qualify; at most one quote per speaker
    (Jorge, Nov 9 2026). Each entry keeps the quote's own "Clip it" /
    "Send to Slack" link (original video, second and quote number), so the
    clip tool resolves it exactly like the copy below."""
    pool, total, order = [], 0, 0
    for md_path in md_paths:
        try:
            data = json.loads(md_path.with_suffix(".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        vid = data.get("video_id") or md_path.stem
        skip = (hidden or {}).get(vid) or set()
        for q in data.get("quotes") or []:
            if not quote_visible(q) or q.get("rank") in skip:
                continue
            total += 1
            order += 1
            if is_unidentified(q.get("speaker") or "") or reporter_share(q) > TOP_MAX_REPORTER_SHARE:
                continue
            news = _news_score(q.get("news_score"))
            social = _news_score(q.get("social_score")) or 0   # older videos have none
            coach = _is_coach_or_exec(q)
            # strict: scored, and a coach / exec only with real news; the
            # rest only fills the block up to its minimum (see below)
            strict = news is not None and not (coach_rule and coach and news < TOP_COACH_MIN_NEWS)
            score = max(news or 0, social)
            best = bool((q.get("structured") or {}).get("best"))
            pool.append((not strict, -score, coach, not best, order, vid, data, q))
    if not pool or not total:
        return []
    want = max(min_quotes, math.ceil(total * TOP_QUOTES_SHARE - 1e-9))  # 10% of all quotes, rounded up
    max_coach = max(1, math.floor(want * TOP_COACH_MAX_SHARE)) if coach_rule else want
    picked, per_video, coaches, speakers = [], {}, 0, set()
    ranked = sorted(pool, key=lambda t: t[:5])
    # Pass 1: the usual rules (scored, players first, at most 2 per video,
    # coaches capped). Pass 2, only to reach the minimum on a small page:
    # any remaining quote by a speaker not yet picked, best first. One quote
    # per speaker always; unidentified speakers and reporter questions never.
    # Pass 3 (Jorge, Nov 10 2026), only when the page has fewer than 3
    # speakers who qualify: repeat a speaker to reach 3 (or all its quotes).
    for relaxed, repeat in ((False, False), (True, False), (True, True)):
        if relaxed and len(picked) >= min(want, min_quotes):
            break
        target = want if not relaxed else min_quotes
        for item in ranked:
            if len(picked) >= target:
                break
            if any(item is p for p in picked):
                continue
            loose, vid, coach = item[0], item[5], item[2]
            who = " ".join(_name_tokens(item[7].get("speaker") or ""))
            if who in speakers and not repeat:
                continue
            if not relaxed and (loose or per_video.get(vid, 0) >= TOP_QUOTES_PER_VIDEO
                                or (coach and coaches >= max_coach)):
                continue
            per_video[vid] = per_video.get(vid, 0) + 1
            coaches += coach
            speakers.add(who)
            picked.append(item)
    lines = [header, ""]
    for n, (_, _, _, _, _, vid, data, q) in enumerate(picked, 1):
        one = dict(q, rank=n)  # shown as 1..N here; the clip link keeps the original number
        md = ytq.to_markdown(WATCH_URL_TEMPLATE.format(video_id=vid), "", pages_safe(
            {"video_title": data.get("video_title") or "", "quotes": [one]}))
        body = md.split("\n")
        start = next(i for i, l in enumerate(body) if l.startswith(f"**{n}."))
        lines.extend(link_bare_urls("\n".join(body[start:])).rstrip("\n").split("\n"))
        source = WATCH_URL_TEMPLATE.format(video_id=vid)
        title = from_line_title(pages_safe(data.get("video_title") or vid))
        team = from_line_title(pages_safe(data.get("channel_team") or ""))
        lines.extend(["", f"From: [{title}]({source})" + (f" · *{team}*" if team else "")])
        secs = ytq.timestamp_to_seconds(q.get("timestamp") or "0:00")
        lines.extend(["", clip_link_line(vid, secs, q.get("rank") or n), ""])
    lines.extend(["---", ""])
    return lines


# --------------------------------------------------------------------------- #
# Full sessions vs single-player videos (Jorge, Nov 7 2026)
# --------------------------------------------------------------------------- #
# Teams often post a "Full Media Availability" plus one video per speaker.
# The full session's title names nobody, so Gemini guesses names there and can
# get them wrong (Nov 6: Alijah Martin's answers labeled "Davion Mitchell").
# The single-player video's title names its speaker, so when the same answer
# shows up in both, the single-player video's name wins.
# Nov 9 2026: the answer is also kept only once, in the single-player video;
# the full session's copy is marked duplicate_of and left off the pages
# (the Pelicans' "LIVE: ... Postgame Interviews" stream next to its
# Mosley / Murphy cuts).
SPLIT_SPEAKER_MIN_WORDS = 8
SPLIT_SPEAKER_MIN_SIMILARITY = 0.6
SPLIT_SPEAKER_MIN_CONTAINED = 0.85       # share of the shorter copy found in the longer one
SPLIT_SPEAKER_MIN_CONTAINED_WORDS = 15


def _quote_words(q: dict) -> list:
    return _name_tokens(speaker_own_text(q))


def _single_speaker(data: dict) -> str:
    """The one speaker a video's title names ('' when it names none or
    several, or its quotes come from more than that one person)."""
    title = data.get("video_title") or ""
    names = {q.get("speaker") or "" for q in data.get("quotes") or []} | set(data.get("speakers_seen") or [])
    named = {n for n in names if n and not is_unidentified(n) and title_names_speaker(title, n)}
    if len(named) != 1:
        return ""
    name = next(iter(named))
    spoken = {q.get("speaker") for q in data.get("quotes") or [] if not is_unidentified(q.get("speaker") or "")}
    return name if spoken <= {name} else ""


def reconcile_split_speakers(dates: list) -> list:
    """Fix speakers in full-session videos from the same team's
    single-player videos (same day or the day either side), and mark the
    full session's copy of each matched answer as a duplicate. Rewrites the
    changed videos' .json/.md and returns the dates it touched."""
    import difflib
    days = set()
    for d in dates:
        try:
            base = datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            continue
        days.update((base + timedelta(days=k)).strftime("%Y-%m-%d") for k in (-1, 0, 1))
    videos = []   # (path, data)
    for d in sorted(days):
        day_dir = OUTPUT_DIR / d
        if not day_dir.is_dir():
            continue
        for js in sorted(day_dir.glob("*.json")):
            try:
                data = json.loads(js.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("quotes"):
                videos.append((js, data))
    singles = []  # (team, speaker, video_id, rank, quote words)
    single_ids = set()
    for js, data in videos:
        if data.get("source") == YTQ_SOURCE:
            continue
        who = _single_speaker(data)
        if who:
            single_ids.add(js.stem)
            for q in data["quotes"]:
                if not quote_visible(q):
                    continue
                words = _quote_words(q)
                if len(words) >= SPLIT_SPEAKER_MIN_WORDS:
                    singles.append((data.get("channel_team") or "", who, js.stem, q.get("rank"), words))
    touched = set()
    for js, data in videos:
        if js.stem in single_ids or data.get("source") == YTQ_SOURCE:
            continue
        team, title, changed = data.get("channel_team") or "", data.get("video_title") or "", False
        for q in data["quotes"]:
            if q.get("reporter_question"):
                continue
            old = q.get("speaker") or ""
            words = _quote_words(q)
            if len(words) < SPLIT_SPEAKER_MIN_WORDS:
                continue
            best, best_ratio = None, SPLIT_SPEAKER_MIN_SIMILARITY
            for s_team, who, vid, s_rank, s_words in singles:
                if s_team != team or vid == js.stem:
                    continue
                sm = difflib.SequenceMatcher(None, words, s_words, autojunk=False)
                ratio = sm.ratio()
                short = min(len(words), len(s_words))
                if short >= SPLIT_SPEAKER_MIN_CONTAINED_WORDS:
                    # one copy is a cut-down part of the other (the stream's
                    # quote is the tail of the single video's answer)
                    covered = sum(b.size for b in sm.get_matching_blocks()) / short
                    if covered >= SPLIT_SPEAKER_MIN_CONTAINED:
                        ratio = max(ratio, covered)
                if ratio >= best_ratio:
                    best, best_ratio = (who, vid, s_rank), ratio
            if not best:
                continue
            who, vid, s_rank = best
            dup = {"video_id": vid, "rank": s_rank}
            if q.get("duplicate_of") != dup:
                q["duplicate_of"] = dup
                changed = True
                log(f"  [dedupe] {js.stem} quote {q.get('rank')}: same answer as {vid} quote {s_rank}; kept there only")
            title_named = old and not is_unidentified(old) and title_names_speaker(title, old)
            if who == old or title_named:
                continue   # same name already, or this video's own title names the speaker: trust it
            q["speaker"] = who
            if old:
                q["summary_phrase"] = (q.get("summary_phrase") or "").replace(old, who)
                if isinstance(q.get("speakers"), list):
                    q["speakers"] = [who if x == old else x for x in q["speakers"]]
            q["speaker_confidence"] = "named"
            q["speaker_source"] = "single-speaker video"
            changed = True
            log(f"  [speakers] {js.stem} quote {q.get('rank')}: {old or '?'} -> {who} (matched single-speaker video)")
        if changed:
            used = {q.get("speaker") for q in data["quotes"]}
            seen = [n for n in (data.get("speakers_seen") or []) if n in used]
            data["speakers_seen"] = seen + sorted(n for n in used if n and n not in seen and not is_unidentified(n))
            atomic_write(js, json.dumps(data, ensure_ascii=False, indent=2))
            atomic_write(js.with_suffix(".md"), render_markdown(js.stem, team, data))
            touched.add(js.parent.name)
    return sorted(touched)


# --------------------------------------------------------------------------- #
# Pages: three separate digests per run slot (Jorge, Nov 9 2026)
#   digest-<slot>.md    press conferences (the URL people already use)
#   podcasts-<slot>.md  podcasts & shows (the yt-quotes rotation)
#   oneoffs-<slot>.md   ad hoc extra_videos
# plus the day's aggregates digest.md / podcasts.md / oneoffs.md. Each page
# leads with its own "Best of the best" and links to the other pages of the
# same slot that exist; a type with nothing to show gets no page.
# --------------------------------------------------------------------------- #

PAGE_BASENAMES = {"presser": "digest", "podcast": "podcasts", "oneoff": "oneoffs"}
PAGE_TITLES = {"presser": "NBA Pressers", "podcast": "NBA Podcasts & Shows", "oneoff": "NBA One-offs"}
_PAGE_NAME_RE = re.compile(r"^(digest|podcasts|oneoffs)(?:-([A-Za-z0-9_-]+))?\.md$")


def page_filename(ctype: str, slot: str | None) -> str:
    base = PAGE_BASENAMES[ctype]
    return f"{base}-{slot}.md" if slot else f"{base}.md"


def _is_page_name(name: str) -> bool:
    return bool(_PAGE_NAME_RE.match(name))


def dedupe_page_blocks(blocks: list) -> tuple[list, dict]:
    """Drop repeated quotes from one page's video blocks: a quote whose
    normalized text (first ~200 chars) is ~85%+ the same as one already on
    the page is left out of the later copy. Single-speaker videos claim their
    quotes first, so a full media session loses the copy, not the player's
    own video; a block with nothing left is dropped. Blocks with no repeat
    are kept byte for byte. Returns (blocks, {video_id: ranks hidden})."""
    loaded = []
    for i, (content, md_path) in enumerate(blocks):
        try:
            data = json.loads(md_path.with_suffix(".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        loaded.append((i, content, md_path, data if isinstance(data, dict) else None))
    order = sorted(loaded, key=lambda t: (not (t[3] and _single_speaker(t[3])), t[0]))
    seen, hide = [], {}   # seen: (normalized quote, block index); only other videos' copies count
    for i, _, md_path, data in order:
        if not data:
            continue
        mine = []
        for q in data.get("quotes") or []:
            if not quote_visible(q):
                continue
            norm = slack_state.normalize_quote(speaker_own_text(q))
            if any(j != i and slack_state.same_quote(norm, s) for s, j in seen):
                hide.setdefault(i, set()).add(q.get("rank"))
            elif norm:
                mine.append((norm, i))
        seen.extend(mine)
    out, hidden = [], {}
    for i, content, md_path, data in loaded:
        if i not in hide:
            out.append((content, md_path))
            continue
        vid = data.get("video_id") or md_path.stem
        hidden[vid] = hide[i]
        shown = dict(data, quotes=[q if q.get("rank") not in hide[i] else dict(q, duplicate_of={"page": True})
                                   for q in data.get("quotes") or []])
        if not any(quote_visible(q) for q in shown["quotes"]):
            log(f"  [pages] {vid}: every quote already on the page; block left out")
            continue
        md = render_markdown(vid, data.get("channel_team") or "", shown).strip()
        if md.startswith("# "):
            md = "#" + md
        log(f"  [pages] {vid}: {len(hide[i])} quote(s) already on the page left out")
        out.append((md, md_path))
    return out, hidden


def write_pages(date_str: str, slot: str | None, video_ids: list | None = None) -> dict:
    """Write the slot's (or, slot None, the day's) three pages from these
    videos (None = every video of the day). Returns {content_type: path} for
    the pages written; a type with no quotes to show gets no page, and an
    older page of that name is removed so no stale or empty page is left."""
    day_dir = OUTPUT_DIR / date_str
    if not day_dir.is_dir():
        return {}
    if video_ids is None:
        md_paths = sorted(p for p in day_dir.glob("*.md") if p.is_file() and not _is_page_name(p.name)
                          and VIDEO_ID_RE.match(p.stem))
    else:
        md_paths = [day_dir / f"{v}.md" for v in video_ids if (day_dir / f"{v}.md").is_file()]
    blocks = {ctype: [] for ctype in CONTENT_TYPES}   # ctype -> [(content, md_path)]
    for md_path in md_paths:
        try:
            content = md_path.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if not content or not re.search(r'(?m)^\*\*\d+\.', content):
            continue
        if content.startswith("# "):
            content = "#" + content  # demote h1 to h2 (as yt-quotes does)
        blocks[_video_type_for_md(md_path)].append((content, md_path))
    # The same answer never shows twice on a page (Jorge, Nov 12 2026). The
    # podcasts page is left exactly as it was.
    hidden = {}
    for ctype in ("presser", "oneoff"):
        if blocks[ctype]:
            blocks[ctype], h = dedupe_page_blocks(blocks[ctype])
            hidden.update(h)
    present = [c for c in CONTENT_TYPES if blocks[c]]
    written = {}
    for ctype in CONTENT_TYPES:
        path = day_dir / page_filename(ctype, slot)
        if ctype not in present:
            if path.is_file():
                path.unlink()
                log(f"  [pages] removed {date_str}/{path.name} (nothing to show)")
            continue
        parts = [f"# {PAGE_TITLES[ctype]} — {date_str}", ""]
        others = [f"[{CONTENT_TYPE_LABELS[o]}]({page_filename(o, slot)})" for o in present if o != ctype]
        if others:
            parts.extend([" · ".join(others), ""])
        parts.extend(top_quotes_block([p for _, p in blocks[ctype]], header=TOP_QUOTES_HEADER,
                                      min_quotes=TOP_SECTION_MIN[ctype], hidden=hidden))
        for i, (content, _) in enumerate(blocks[ctype]):
            if i:
                parts.extend(["---", ""])
            parts.extend([content, ""])
        if ctype == "podcast":
            parts.extend(["---", "", ytq.DIGEST_CLOSING_LINE, ""])
        atomic_write(path, "\n".join(parts))
        written[ctype] = path
    return written


def write_digest_file(date_str: str, output_filename: str, video_ids: list | None = None) -> Path | None:
    """Older entry point: "digest.md" -> the day's pages, "digest-<slot>.md"
    -> that slot's pages. Returns the press-conference page, else the first
    page written, else None."""
    m = _PAGE_NAME_RE.match(output_filename)
    slot = m.group(2) if m else None
    written = write_pages(date_str, slot, video_ids)
    return written.get("presser") or next(iter(written.values()), None)


def run_slot_of(run_id: str) -> str:
    """'2026-11-09-overnight' -> 'overnight', '2026-11-07T1844Z-14utc' -> '14utc'."""
    m = re.match(r"^\d{4}-\d{2}-\d{2}(?:T\d{4}Z)?-(.+)$", run_id or "")
    return m.group(1) if m else ""


def refile_late_runs(dates: list) -> list:
    """Move stored videos of late daytime runs to the day the run was
    scheduled for (filed_date). Returns the dates touched (both ends)."""
    touched = set()
    for d in dates:
        day_dir = OUTPUT_DIR / d
        if not day_dir.is_dir():
            continue
        for js in sorted(day_dir.glob("*.json")):
            if not VIDEO_ID_RE.match(js.stem):
                continue
            try:
                data = json.loads(js.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            target = filed_date(d, data.get("run_id") or "")
            if target == d:
                continue
            dest = OUTPUT_DIR / target
            dest.mkdir(parents=True, exist_ok=True)
            for f in sorted(day_dir.glob(f"{js.stem}.*")):
                f.replace(dest / f.name)
            log(f"  [late run] {js.stem}: {d} -> {target} (run {data.get('run_id')} was scheduled for {target})")
            touched.update((d, target))
    return sorted(touched)


def rebuild_day(date_str: str) -> list:
    """Every slot's pages and the day's pages for one day folder, from the
    stored videos (no Gemini call). Slot pages whose slot has no video left
    in the folder are removed. Returns the page paths written."""
    day_dir = OUTPUT_DIR / date_str
    if not day_dir.is_dir():
        return []
    by_slot = {}
    for js in sorted(day_dir.glob("*.json")):
        if not VIDEO_ID_RE.match(js.stem):
            continue
        try:
            data = json.loads(js.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        slot = run_slot_of(data.get("run_id") or "")
        if slot:
            by_slot.setdefault(slot, []).append(js.stem)
    for page in sorted(day_dir.glob("*.md")):
        m = _PAGE_NAME_RE.match(page.name)
        if m and m.group(2) and m.group(2) not in by_slot:
            page.unlink()
            log(f"  [pages] removed {date_str}/{page.name} (no video of that slot left here)")
    written = []
    for slot, ids in sorted(by_slot.items()):
        written.extend(write_pages(date_str, slot, ids).values())
    written.extend(write_pages(date_str, None, None).values())
    return written


def regenerate_index() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    slot_order = {"overnight": 0, "14utc": 1, "20utc": 2}
    lines = ["# NBA Pressers — index", "", "Clip manifest: [latest_clips.json](latest_clips.json)", ""]
    any_rows = False
    for day_dir in iter_day_dirs():
        pages = {}
        try:
            for p in day_dir.glob("*.md"):
                m = _PAGE_NAME_RE.match(p.name)
                if m and p.stat().st_size > 0:
                    ctype = next(c for c, b in PAGE_BASENAMES.items() if b == m.group(1))
                    pages.setdefault(m.group(2) or "", {})[ctype] = p.name
        except OSError:
            continue
        day = pages.pop("", {})
        if not day and not pages:
            continue
        any_rows = True
        d = day_dir.name

        def links(found: dict) -> str:
            return " · ".join(f"[{CONTENT_TYPE_LABELS[c]}]({d}/{found[c]})" for c in CONTENT_TYPES if c in found)
        lines.append(f"- {d}: {links(day)}" if day else f"- {d}")
        for slot in sorted(pages, key=lambda s: (slot_order.get(s, 9), s)):
            lines.append(f"  - {slot}: {links(pages[slot])}")
    if not any_rows:
        lines.append("_No videos processed yet._")
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
    for key in ("start_seconds", "end_seconds", "speaker_confidence", "news_score", "social_score", "social_post", "team"):
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
    and digest (written before those links existed), and drop the extra
    clip pair older Best-of-the-best entries got (drop_extra_best_clip_links).
    Text-only and idempotent; no Gemini calls."""
    changed = 0
    for day_dir in iter_day_dirs():
        for md in day_dir.glob("*.md"):
            try:
                text = md.read_text(encoding="utf-8")
            except OSError:
                continue
            linked = add_clip_links(link_bare_urls(drop_extra_best_clip_links(text)))
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
                if not quote_visible(q):
                    continue   # bare reporter question, or kept in the single-speaker video
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
                    "text": speaker_own_text(q),
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
# Podcasts & shows: hoopshype-yt-quotes' podcast rotation (Jorge, Nov 9 2026)
# --------------------------------------------------------------------------- #
# This pipeline only ever polled the 30 team channels (+ HoopsHype); the
# podcast channels live in hoopshype-yt-quotes' channels.json and are
# transcribed there. Rather than send those videos to Gemini a second time,
# the workflow checks out yt-quotes' output for the last few days
# (YTQ_PODCAST_DIR) and each new video is copied in as a podcast: same
# yt-quotes JSON, plus one text-only clip-fields call for the scores the
# Best of the best needs, and the publish time from the YouTube Data API.

_YTQ_MD_HEADER_RE = re.compile(r"^# .* — \*(.+)\*\s*$")


def _ytq_channel_name(md_path: Path) -> str:
    try:
        first = md_path.read_text(encoding="utf-8").split("\n", 1)[0]
    except OSError:
        return ""
    m = _YTQ_MD_HEADER_RE.match(first)
    return m.group(1).strip() if m else ""


def slot_for_time(published: str) -> tuple[str, str]:
    """(run_id, slot) of the scheduled run that would first see a video
    published at this time: before 10:23 UTC -> that day's overnight, before
    14:15 -> 14utc, before 20:15 -> 20utc, later -> next day's overnight."""
    t = parse_iso(published) or datetime.now(timezone.utc)
    hm = t.hour * 60 + t.minute
    if hm < 10 * 60 + 23:
        return f"{t:%Y-%m-%d}-overnight", "overnight"
    if hm < 14 * 60 + 15:
        return f"{t:%Y-%m-%d}T1415Z-14utc", "14utc"
    if hm < 20 * 60 + 15:
        return f"{t:%Y-%m-%d}T2015Z-20utc", "20utc"
    nxt = t + timedelta(days=1)
    return f"{nxt:%Y-%m-%d}-overnight", "overnight"


def stored_runs(days: int = 4) -> list:
    """[(processed_at, run_id)] of the real runs that stored team-channel
    videos in the last few day folders, oldest first (one entry per run_id
    and hour: the overnight run_id is shared by several runs)."""
    first = {}
    for day_dir in sorted(iter_day_dirs(), key=lambda p: p.name)[-days:]:
        for js in day_dir.glob("*.json"):
            if not VIDEO_ID_RE.match(js.stem):
                continue
            try:
                data = json.loads(js.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rid, at = data.get("run_id") or "", parse_iso(data.get("processed_at") or "")
            if not isinstance(data, dict) or data.get("source") == YTQ_SOURCE or not rid or not at:
                continue
            first.setdefault((at.strftime("%Y-%m-%dT%H"), rid), at)
            first[(at.strftime("%Y-%m-%dT%H"), rid)] = min(first[(at.strftime("%Y-%m-%dT%H"), rid)], at)
    return sorted((at, rid) for (_, rid), at in first.items())


def rebuild_run_id(published: str, runs: list) -> str:
    """The run that would have imported a video published then: the first
    stored run after it (GitHub fires the crons hours late, so the slot is
    the real run's, not the clock's); slot_for_time when none ran since."""
    pub = parse_iso(published)
    for at, rid in runs:
        if pub and at >= pub:
            return rid
    return slot_for_time(published)[0]


def _ytq_videos(root: Path) -> list:
    """[(json path, data)] of yt-quotes per-video files under root/<date>/."""
    out = []
    if not root.is_dir():
        return out
    for day in sorted((d for d in root.iterdir() if d.is_dir() and DAY_DIR_RE.match(d.name)), reverse=True):
        for js in sorted(day.glob("*.json")):
            if not VIDEO_ID_RE.match(js.stem):
                continue
            try:
                data = json.loads(js.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("quotes"):
                out.append((js, data))
    return out


def refile_ytq_oneoffs(oneoff_ids: set) -> list:
    """A video Jorge submitted ad hoc to yt-quotes that was also imported from
    its rotation folder as a podcast becomes a one-off here (no NBA filter:
    he picked it). Its stored scores are kept (no new call). Returns the
    processed_items-style entries of the videos changed."""
    changed = []
    for day_dir in sorted(iter_day_dirs(), key=lambda p: p.name)[-4:]:
        for js in sorted(day_dir.glob("*.json")):
            if js.stem not in oneoff_ids:
                continue
            try:
                data = json.loads(js.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("source") != YTQ_SOURCE or data.get("content_type") == "oneoff":
                continue
            for q in data.get("quotes") or []:
                q.pop("non_nba", None)
            data.update(content_type="oneoff", content_type_source="yt-quotes one-off", is_one_off=True)
            atomic_write(js, json.dumps(data, ensure_ascii=False, indent=2))
            atomic_write(js.with_suffix(".md"), render_markdown(js.stem, data.get("channel_team") or "", data))
            log(f"  [one-offs] {js.stem}: filed as a one-off (it was submitted ad hoc to yt-quotes)")
            changed.append({"video_id": js.stem, "date": day_dir.name, "content_type": "oneoff",
                            "run_id": data.get("run_id") or "", "refiled": True})
    return changed


def import_ytq_podcasts(client, yt_key: str, src_dir: Path, cutoff: datetime, run_id: str = "",
                        max_items: int = YTQ_IMPORT_MAX_PER_RUN) -> list:
    """Copy yt-quotes' videos that aren't stored here yet: its rotation
    (src_dir/<date>/) as podcasts, and the videos Jorge submits ad hoc to
    yt-quotes (src_dir/oneoffs/<date>/) as one-offs (Nov 10 2026). Each gets
    the same single clip-fields call (podcasts with the NBA rule). A video in
    both folders is a one-off. run_id "" = assign each video the run that
    would first have seen it (rebuilds, see rebuild_run_id); otherwise this
    run's id. Returns processed_items entries."""
    src_dir = Path(src_dir)
    if not src_dir.is_dir():
        log(f"[podcasts] yt-quotes output not found at {src_dir}; no podcasts imported")
        return []
    oneoffs = _ytq_videos(src_dir / "oneoffs")
    oneoff_ids = {js.stem for js, _ in oneoffs}
    items = refile_ytq_oneoffs(oneoff_ids)
    # one-offs first: they are ad hoc requests, and they bypass the 48h window
    found = [(js, data, "oneoff") for js, data in oneoffs if not find_existing_artifact(js.stem)]
    found += [(js, data, "podcast") for js, data in _ytq_videos(src_dir)
              if js.stem not in oneoff_ids and not find_existing_artifact(js.stem)]
    if not found:
        log("[podcasts] no new yt-quotes rotation videos or one-offs")
        return items
    meta = {}
    if yt_key:
        try:
            meta = hydrate_video_metadata([js.stem for js, _, _ in found], yt_key)
        except Exception as e:   # the import never blocks the run
            log(f"[podcasts] metadata fetch failed ({type(e).__name__}); using folder dates")
    runs = stored_runs() if not run_id else []
    imported = 0
    for js, data, kind in found:
        if imported >= max_items or _spending_cap_hit.is_set():
            break
        vid = js.stem
        m = meta.get(vid) or {}
        published = m.get("published_at") or f"{js.parent.name}T12:00:00Z"
        pub = parse_iso(published)
        if kind == "podcast" and pub and pub < cutoff:
            continue
        channel = _ytq_channel_name(js.with_suffix(".md")) or m.get("channel_title") or "Podcast"
        duration = int(m.get("duration") or 0)
        if client is not None:
            derived = derive_clip_fields(client, data, channel, PODCAST_NBA_RULE if kind == "podcast" else "")
        else:
            derived = {}
        apply_clip_fields(data, derived, channel, duration)
        if kind == "podcast":
            dropped = apply_nba_flags(data, derived)
            if dropped:
                log(f"  [podcasts] {vid}: {dropped} of {len(data.get('quotes') or [])} quote(s) not about the NBA; "
                    "left off the pages")
        rid = run_id or rebuild_run_id(published, runs)
        data.update({
            "content_type": kind,
            "content_type_source": "yt-quotes rotation" if kind == "podcast" else "yt-quotes one-off",
            "source": YTQ_SOURCE,
            "format": FORMAT_VERSION,
            "video_id": vid,
            "url": WATCH_URL_TEMPLATE.format(video_id=vid),
            "channel_team": channel,
            "published": published,
            "duration_seconds": duration,
            "processed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "run_id": rid,
            "is_one_off": kind == "oneoff",
        })
        if kind == "oneoff":
            # the folder yt-quotes filed it under (its publish date), whatever the API says
            video = {"video_id": vid, "published": f"{js.parent.name}T12:00:00Z"}
        else:
            video = {"video_id": vid, "published": published}
        day_dir = OUTPUT_DIR / filed_date(publish_date(video), rid)
        atomic_write(day_dir / f"{vid}.json", json.dumps(data, ensure_ascii=False, indent=2))
        atomic_write(day_dir / f"{vid}.md", render_markdown(vid, channel, data))
        imported += 1
        quotes = data.get("quotes") or []
        top = quotes[0] if quotes else {}
        log(f"  [{'one-offs' if kind == 'oneoff' else 'podcasts'}] {vid} ({channel}): {len(quotes)} quote(s) "
            f"from yt-quotes -> {day_dir.name}")
        if not any(quote_visible(x) for x in quotes):
            continue   # stored (so it isn't imported again) but nothing NBA to show or post
        items.append({
            "video_id": vid,
            "title": data.get("video_title") or vid,
            "channel": channel,
            "top_quote": ytq._canonical_quote_text(top) if top else "",
            "speaker": "" if is_unidentified(top.get("speaker") or "") else top.get("speaker", ""),
            "social_post": top.get("social_post", ""),
            "clip_count": len(quotes),
            "date": day_dir.name,
            "is_one_off": kind == "oneoff",
            "content_type": kind,
            "run_id": rid,
        })
    log(f"[podcasts] imported {imported} video(s) from yt-quotes (rotation + one-offs)")
    return items


def apply_page_rules(day_dirs: list, client=None) -> list:
    """Reclassify team-channel videos and apply the reporter-question rules
    to stored press conferences in these folders, re-rendering each changed
    video's .md. Returns the dates touched. No Gemini call."""
    touched = set()
    if reclassify_stored(day_dirs):
        touched.update(Path(d).name for d in day_dirs)
    for day_dir in day_dirs:
        for js in sorted(Path(day_dir).glob("*.json")):
            if not VIDEO_ID_RE.match(js.stem):
                continue
            try:
                data = json.loads(js.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or video_content_type(data) != "presser":
                continue
            if data.get("reporter_check") == REPORTER_CHECK_VERSION:
                continue
            if mark_reporter_questions(data, derive_question_words(client, data)):
                atomic_write(js, json.dumps(data, ensure_ascii=False, indent=2))
                atomic_write(js.with_suffix(".md"), render_markdown(js.stem, data.get("channel_team") or "", data))
                touched.add(Path(day_dir).name)
    return sorted(touched)


def rebuild_main(dates: list) -> int:
    """REBUILD_DATES mode (workflow_dispatch input): import the yt-quotes
    podcasts still inside the window, apply the classification, reporter
    and duplicate rules to the stored videos of these days, and rewrite
    every page of those days, the index and the clip manifest. No video is
    sent to Gemini; the import's clip-fields call runs only with a key."""
    dates = sorted({d for d in dates if DAY_DIR_RE.match(d)})
    log(f"Rebuild: {', '.join(dates) or '(no valid dates)'}")
    config = load_config()
    _CONFIG.clear()
    _CONFIG.update(config)
    migrate_legacy_outputs()
    relink_stored_markdown()
    gemini_key, yt_key = os.getenv("GEMINI_API_KEY"), os.getenv("YOUTUBE_API_KEY") or ""
    client = genai.Client(api_key=gemini_key) if gemini_key else None
    if client is not None:
        _count_gemini_calls(client)
    src = os.getenv("YTQ_PODCAST_DIR") or ""
    if src:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=int(config.get("window_hours", 48)))
        for it in import_ytq_podcasts(client, yt_key, Path(src), cutoff, run_id=""):
            if it["date"] not in dates:
                dates.append(it["date"])
    dates = sorted(set(dates) | set(refile_late_runs(sorted(set(dates)))))
    apply_page_rules([OUTPUT_DIR / d for d in dates if (OUTPUT_DIR / d).is_dir()], client)
    try:
        reconcile_split_speakers(dates)
    except Exception as e:
        log(f"  [speakers] reconcile failed ({type(e).__name__}: {e})")
    for d in dates:
        for p in rebuild_day(d):
            log(f"  [pages] wrote {d}/{p.name}")
    regenerate_index()
    write_clip_manifest(build_clip_manifest(determine_run_slot(), int(config.get("window_hours", 48))))
    _outputs_written.set()
    log("Rebuild done.")
    return 0


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


def load_slack_state(now: datetime) -> dict:
    """slack_alerted.json, pruned. Created on its first run from the videos
    already stored (the old per-run alerts posted all of them)."""
    state = slack_state.load(SLACK_STATE_PATH)
    if state is None:
        state = slack_state.seed_from_outputs(OUTPUT_DIR, now)
        log(f"[slack] {SLACK_STATE_PATH.name} created: {len(state['alerted'])} stored video(s) recorded as alerted")
    slack_state.prune(state, now)
    return state


def slot_pages(group: dict, run_slot: str, today: str, pages: list) -> list:
    """Page links for one slot of the Slack post: that slot's pages (press
    conferences, podcasts & shows, one-offs) on every day its videos are
    filed under, plus the pages this run wrote for its own slot."""
    dates = {it.get("date") for it in group["items"] if it.get("date")}
    if group["run_slot"] == run_slot and group["slot_date"] == today:
        dates |= {pg["date"] for pg in pages if pg.get("date")}
    out = []
    for d in sorted(dates):
        for ctype in CONTENT_TYPES:
            md = OUTPUT_DIR / d / page_filename(ctype, group["run_slot"])
            if md.is_file():
                out.append({"date": d, "file": md.with_suffix(".html").name, "label": CONTENT_TYPE_LABELS[ctype]})
    return out


def queue_slack(run_slot: str, today: str, now: datetime, processed_items: list, pages: list,
                clip_count: int, aborted_count: int = 0, deferred_count: int = 0,
                deferred_videos: list | None = None) -> dict | None:
    """Decide this run's Slack post (Jorge, Nov 12 2026) and queue it for the
    workflow's post-push step. Returns the payload queued (None: no post).

    - Podcasts & shows never go to Slack (hoopshype-yt-quotes alerts them);
      their page link stays in the post.
    - Overnight runs post nothing until the night is over (10:00 UTC); the
      first run from then on (any slot) posts every pending overnight video.
    - A video id already alerted is never posted again; a video whose top
      quote repeats one already shown is dropped (single-speaker wins).
    slack_alerted.json is saved here (pending) and by slack_notify.py after
    a successful post (alerted)."""
    try:
        state = load_slack_state(now)
        queued = slack_state.add_pending(state, processed_items, RUN_ID, run_slot, today, now)
        due = slack_state.due_items(state, now)
        kept, dropped = slack_state.dedupe(due, state, now)
        records = slack_state.alert_records(due, dropped, now)
        for vid, dup in dropped.items():
            log(f"[slack] {vid}: top quote repeats {dup}; left out of Slack")
        held = len(state["pending"]) - len(due)
        if queued or held:
            log(f"[slack] queued {len(queued)} new video(s); {len(due)} due now, {held} held for later")
        if not kept and dropped:
            # nothing left to post: the duplicates are settled now
            slack_state.mark_alerted(state, records)
        slack_state.save(state, SLACK_STATE_PATH)
    except Exception as e:   # never let the Slack bookkeeping block the run's outputs
        log(f"[slack] state failed ({type(e).__name__}: {e}); no Slack post this run")
        return None
    # An overnight run before 10:00 UTC is one of the hourly drip runs: it
    # only posts when an older slot's items are due (catch-up).
    final_run = run_slot != OVERNIGHT_SLOT or now.hour >= slack_state.OVERNIGHT_LAST_HOUR
    groups = slack_state.group_by_slot(kept)
    for g in groups:
        g["pages"] = slot_pages(g, run_slot, today, pages)
    if kept or (final_run and (aborted_count or deferred_count)):
        payload = {
            "kind": "digest",
            "date_str": today,
            "run_slot": run_slot,
            "groups": groups,
            "pages": pages,
            "clip_count": clip_count,
            "aborted_count": aborted_count,
            "deferred_count": deferred_count,
            "deferred_videos": deferred_videos or [],
            "alert_records": records if kept else {},
        }
    elif run_slot != OVERNIGHT_SLOT:
        # daytime / manual run with nothing new (the overnight slot never
        # posted these); a podcasts-only run still links its pages
        payload = {"kind": "no_new_videos", "date_str": today, "run_slot": run_slot, "pages": pages}
    else:
        log("[slack] overnight run: nothing to post")
        return None
    queue_slack_payload(payload)
    return payload


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
            # all_uploads channels (e.g. HoopsHype's own) take every upload;
            # the duration / livestream / window checks still apply later.
            if not channel.get("all_uploads"):
                reason = title_filter(up["title"], config)
                if reason:
                    continue
            forced = channel.get("content_type") or ""
            if forced:
                up = dict(up, force_content_type=forced)
            out.append((team, up))
            kept += 1
        log(f"  [{team}] {len(uploads)} recent upload(s), {kept} presser candidate(s) in window")
    return out


def main() -> int:
    install_script_timeout()
    rebuild_dates = [d for d in re.split(r"[\s,]+", os.getenv("REBUILD_DATES") or "") if d]
    if rebuild_dates:
        return rebuild_main(rebuild_dates)
    gemini_key = os.getenv("GEMINI_API_KEY")
    yt_key = os.getenv("YOUTUBE_API_KEY")
    missing = [n for n, v in (("GEMINI_API_KEY", gemini_key), ("YOUTUBE_API_KEY", yt_key)) if not v]
    if missing:
        log(f"ERROR: missing env vars: {', '.join(missing)}")
        return 1

    global RUN_ID, SLOT_DATE
    started = datetime.now(timezone.utc)
    today = started.strftime("%Y-%m-%d")
    run_slot = determine_run_slot()
    if run_slot == OVERNIGHT_SLOT:
        # One run_id per night, shared by every hourly overnight run, so the
        # clip tool's "latest run" view and digest-overnight.md cover the
        # whole night rather than the last hourly batch.
        RUN_ID = f"{started:%Y-%m-%d}-{run_slot}"
    else:
        RUN_ID = f"{started:%Y-%m-%dT%H%MZ}-{run_slot}"
    SLOT_DATE = scheduled_date(RUN_ID)
    if SLOT_DATE and SLOT_DATE != today:
        log(f"Late run: scheduled for {SLOT_DATE}, started {started:%Y-%m-%d %H:%M} UTC; "
            f"videos published after {SLOT_DATE} are filed under {SLOT_DATE}")
        today = SLOT_DATE   # the Slack post's date, too
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
        manifest = build_clip_manifest(run_slot, window_hours)
        write_clip_manifest(manifest)
        # still posts any pending items that are due (overnight catch-up)
        queue_slack(run_slot, today, datetime.now(timezone.utc), [], [], manifest["count"])
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
        if up.get("force_content_type"):
            video["force_content_type"] = up["force_content_type"]
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

    # Step 3b: podcasts & shows from the yt-quotes rotation (already
    # transcribed there; one text-only clip-fields call each here).
    client = genai.Client(api_key=gemini_key)
    _count_gemini_calls(client)
    imported: list = []
    ytq_dir = os.getenv("YTQ_PODCAST_DIR") or ""
    if ytq_dir:
        try:
            # re-filed videos (already posted earlier) are not news for this run's pages or Slack
            imported = [it for it in import_ytq_podcasts(client, yt_key, Path(ytq_dir), cutoff, run_id=RUN_ID)
                        if not it.get("refiled")]
        except Exception as e:   # never let the podcast import block the pressers
            log(f"[podcasts] import failed ({type(e).__name__}: {e})")
    if not queued and not imported:
        log("Nothing new to process.")
        return finish_without_videos()
    if len(queued) > max_total:
        log(f"Capping queue from {len(queued)} to max_videos_per_run={max_total}")
        queued = queued[:max_total]

    # Step 4: Gemini, VIDEO_WORKERS in parallel on one shared client.
    log(f"Processing {len(queued)} video(s) with {MODEL} (up to {VIDEO_WORKERS} in parallel)...")
    summary = {k: 0 for k in ("ok", "too-long", "failed-hallucination", "failed-timeout",
                              "failed-other", "aborted-spending-cap", "deferred-transient")}
    processed_items: list = list(imported)
    results: list = []
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
    run_dates = sorted({it["date"] for it in processed_items if it.get("date")})
    try:
        fixed_dates = reconcile_split_speakers(run_dates)
    except Exception as e:   # never let the speaker check block the digest
        log(f"  [speakers] reconcile failed ({type(e).__name__}: {e}); digests use the stored names")
        fixed_dates = []
    for d in fixed_dates:
        if d not in run_dates:
            write_pages(d, None, None)
    digest_dates, pages = [], []
    for pd in sorted({it["date"] for it in processed_items if it.get("date")}):
        run_ids = [it["video_id"] for it in processed_items if it.get("date") == pd]
        if run_slot == OVERNIGHT_SLOT:
            # digest-overnight.md accumulates: every video stored tonight
            # (same shared run_id), not just this hourly batch.
            run_ids = videos_with_run_id(OUTPUT_DIR / pd, RUN_ID) or run_ids
        written = write_pages(pd, run_slot, video_ids=run_ids)
        write_pages(pd, None, None)
        if written:
            digest_dates.append(pd)
        for ctype in CONTENT_TYPES:
            if ctype in written:
                pages.append({"date": pd, "file": written[ctype].with_suffix(".html").name,
                              "label": CONTENT_TYPE_LABELS[ctype]})
    regenerate_index()
    manifest = build_clip_manifest(run_slot, window_hours)
    write_clip_manifest(manifest)

    queue_slack(run_slot, today, datetime.now(timezone.utc), processed_items, pages, manifest["count"],
                aborted_count=summary.get("aborted-spending-cap", 0),
                deferred_count=summary.get("deferred-transient", 0),
                deferred_videos=list(_deferred_items))
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
