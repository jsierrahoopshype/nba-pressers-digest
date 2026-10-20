"""
Structured presser quotes: one record per quote, an append-only history per
season, and the public JSON of the season's best quotes.

Reads the per-video JSON the extractor already writes
(pressers_v2/output/<date>/<video_id>.json). The record fields (role,
subjects, teams, topics, context, best) come from the extractor's third
Gemini call (the "structured" block in each quote, "fields_source":
"gemini"). With --gemini, a video that has no such block (processed before
the call existed, or the call failed) gets the same call here, before its
records are written ("gemini-backfill"; answers kept in
data/structured-fields.json, by video and quote rank). Only when that call
fails too are the fields derived by rules from what is stored
("rules-fallback").

Only press conferences and media availabilities (content_type "presser"),
and only quotes whose speaker is named in the video.

Outputs (repo root):
  data/history/<season>.jsonl          append-only, one record per line. A
                                       reprocessed video appends a new
                                       snapshot of all its records (newer
                                       extracted_at); readers keep the
                                       latest snapshot per video.
  data/history/processed-videos.json   video_id -> processed_at already read
  data/structured-fields.json          --gemini answers for stored videos
  docs/pressers/api/best-quotes.json   the current season's best quotes

Transcript check: for new videos, when YouTube captions can be fetched
(youtube-transcript-api, optional), each quote is aligned against them
(caption_align.py) and dropped when it doesn't match. No captions:
"transcript_check": "unavailable" and the quote is kept.

Usage (from repo root):
    python pressers_v2/structured_quotes.py [--headshots <nba-headshots checkout>] [--gemini]
"""

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import caption_align  # noqa: E402
import presser_extractor as pe  # noqa: E402
import ytq_vendor as ytq  # noqa: E402

REPO = ROOT.parent
HISTORY_DIR = REPO / "data" / "history"
PROCESSED_PATH = HISTORY_DIR / "processed-videos.json"
FIELDS_CACHE = REPO / "data" / "structured-fields.json"
GEMINI_WORKERS = 4
PUBLIC_JSON = REPO / "docs" / "pressers" / "api" / "best-quotes.json"
PUBLIC_URL = "https://jsierrahoopshype.github.io/nba-pressers-digest/pressers/api/best-quotes.json"

TOPICS = pe.STRUCTURED_TOPICS
ROLES = pe.STRUCTURED_ROLES
SEASON_START_MONTH = 7          # July 1 opens the next season (offseason news belongs to it)
BEST_MIN_WORDS = 15
BACKFILL_BEST_SCORE = 7         # rules fallback: best = news_score 7+ (plus the filler checks)
TRANSCRIPT_TIMEOUT_SECS = 20
TRANSCRIPT_MAX_FETCHES = 40     # per run
TRANSCRIPT_GIVE_UP_AFTER = 3    # consecutive failed fetches (blocked runner) -> stop trying

# The 30 teams: abbreviation, full name, nickname, other spellings Gemini or titles use.
TEAMS = [
    ("ATL", "Atlanta Hawks", "Hawks", []), ("BOS", "Boston Celtics", "Celtics", []),
    ("BKN", "Brooklyn Nets", "Nets", ["BRK", "BKLYN"]), ("CHA", "Charlotte Hornets", "Hornets", ["CHO"]),
    ("CHI", "Chicago Bulls", "Bulls", []), ("CLE", "Cleveland Cavaliers", "Cavaliers", ["Cavs"]),
    ("DAL", "Dallas Mavericks", "Mavericks", ["Mavs"]), ("DEN", "Denver Nuggets", "Nuggets", []),
    ("DET", "Detroit Pistons", "Pistons", []), ("GSW", "Golden State Warriors", "Warriors", ["GS"]),
    ("HOU", "Houston Rockets", "Rockets", []), ("IND", "Indiana Pacers", "Pacers", []),
    ("LAC", "LA Clippers", "Clippers", ["Los Angeles Clippers"]), ("LAL", "Los Angeles Lakers", "Lakers", []),
    ("MEM", "Memphis Grizzlies", "Grizzlies", []), ("MIA", "Miami Heat", "Heat", []),
    ("MIL", "Milwaukee Bucks", "Bucks", []), ("MIN", "Minnesota Timberwolves", "Timberwolves", ["Wolves"]),
    ("NOP", "New Orleans Pelicans", "Pelicans", ["NO", "NOLA"]), ("NYK", "New York Knicks", "Knicks", ["NY"]),
    ("OKC", "Oklahoma City Thunder", "Thunder", []), ("ORL", "Orlando Magic", "Magic", []),
    ("PHI", "Philadelphia 76ers", "76ers", ["Sixers"]), ("PHX", "Phoenix Suns", "Suns", ["PHO"]),
    ("POR", "Portland Trail Blazers", "Trail Blazers", ["Blazers"]), ("SAC", "Sacramento Kings", "Kings", []),
    ("SAS", "San Antonio Spurs", "Spurs", ["SA"]), ("TOR", "Toronto Raptors", "Raptors", []),
    ("UTA", "Utah Jazz", "Jazz", ["UTAH"]), ("WAS", "Washington Wizards", "Wizards", ["WSH"]),
]
TEAM_BY_ABBR = {a: {"abbr": a, "full": f, "nickname": n} for a, f, n, _ in TEAMS}
_ABBR_ALIASES = {a: a for a, *_ in TEAMS}
_ABBR_ALIASES.update({x.upper(): a for a, _, _, xs in TEAMS for x in xs if len(x) <= 5})
_TEAM_BY_FULL = {f.lower(): a for a, f, *_ in TEAMS}
_TEAM_BY_FULL.update({x.lower(): a for a, _, _, xs in TEAMS for x in xs if len(x) > 5})
# Nicknames in quote text ("the Lakers", "Cavs"); "Heat", "Magic", "Jazz", "Kings", "Thunder",
# "Suns", "Nets" are capitalized words in ordinary speech too, so only capitalized matches count.
_TEAM_TEXT_RE = [(a, re.compile(r"\b(?:%s)\b" % "|".join(re.escape(w) for w in [n] + [x for x in xs if len(x) > 3])))
                 for a, f, n, xs in TEAMS]

# Backfill topics: keyword patterns, strongest match first, at most 2.
TOPIC_PATTERNS = {
    "contracts": r"\b(contracts?|(?:contract|rookie|max) extensions?|max deal|supermax|free agen\w*|salary|cap space|payday|re-?sign\w*|player option|team option|new deal)\b",
    "injuries": r"\b(injur\w*|ankle|knee|hamstring|calf|achilles|groin|rehab\w*|surgery|sprain\w*|strain\w*|mri|concussion|sidelined|day-to-day|minutes restriction)\b",
    "trades": r"\b(trades?|traded|trade deadline|acquir\w*|rumou?rs?)\b",
    "teammates": r"\b(teammates?|chemistry|locker room|brotherhood|playing (?:alongside|with him|together))\b",
    "coaching decisions": r"\b(rotations?|starting lineup|lineups?|off the bench|starting five|playing time|(?:his|my|new|bigger|different) role|starters?|minutes restriction)\b",
}
TOPIC_RES = {k: re.compile(v, re.I) for k, v in TOPIC_PATTERNS.items()}
# Filler that never makes a quote "best", whatever the model said.
STOCK_PHRASES = [
    "one game at a time", "keep working", "keep getting better", "credit to them", "hats off",
    "it is what it is", "next man up", "take it day by day", "day by day", "we just have to",
    "we got to be better", "we have to be better", "control what we can control", "good win",
    "tough loss", "we'll watch the film", "watch the film", "play hard", "stay the course",
]
EXEC_TITLE_RE = re.compile(r"\b(general manager|gm|president|owner|governor|basketball operations|front office)\b", re.I)
COACH_TITLE_RE = re.compile(r"\b(coach|head coach|hc)\b", re.I)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def name_key(name: str) -> str:
    """"Luka Dončić" == "luka doncic"; "Jaren Jackson Jr." == "jaren jackson"."""
    t = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii").lower()
    toks = re.findall(r"[a-z0-9]+", t.replace("'", "").replace("’", ""))
    while len(toks) > 2 and toks[-1] in ("jr", "sr", "ii", "iii", "iv"):
        toks.pop()
    return " ".join(toks)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name_key(name)).strip("-") or "x"


def word_count(text: str) -> int:
    return len((text or "").split())


def ny_date(iso: str) -> str:
    """UTC timestamp -> the date in New York (US daylight time rules), so a
    10pm ET postgame presser keeps its own date. No tz database needed."""
    dt = pe.parse_iso(iso or "")
    if dt is None:
        return (iso or "")[:10]
    y = dt.year
    # DST: second Sunday of March 07:00 UTC to first Sunday of November 06:00 UTC.
    mar = datetime(y, 3, 8, 7, tzinfo=timezone.utc)
    start = mar + timedelta(days=(6 - mar.weekday()) % 7)
    nov = datetime(y, 11, 1, 6, tzinfo=timezone.utc)
    end = nov + timedelta(days=(6 - nov.weekday()) % 7)
    offset = -4 if start <= dt < end else -5
    return (dt + timedelta(hours=offset)).strftime("%Y-%m-%d")


def season_of(date: str) -> str:
    y, m = int(date[:4]), int(date[5:7])
    first = y if m >= SEASON_START_MONTH else y - 1
    return f"{first}-{str(first + 1)[-2:]}"


def quote_id(video_id: str, text: str) -> str:
    """Stable id: the video plus the quote's own words (case, accents and
    punctuation ignored). The same quote always gets the same id."""
    words = " ".join(caption_align.norm_words(text))
    return "pq-" + hashlib.sha1(f"{video_id}|{words}".encode("utf-8")).hexdigest()[:12]


def watch_url(video_id: str, start: int) -> str:
    return f"https://www.youtube.com/watch?v={video_id}&t={int(start)}s"


def team_abbr(value: str) -> str:
    v = " ".join(str(value or "").split())
    return _ABBR_ALIASES.get(v.upper()) or _TEAM_BY_FULL.get(v.lower()) or ""


def is_stock(text: str) -> bool:
    low = (text or "").lower()
    return word_count(text) < 40 and any(p in low for p in STOCK_PHRASES)


def best_ok(text: str) -> bool:
    """Filler checks applied to every "best" flag (model or backfill)."""
    t = (text or "").strip()
    return word_count(t) >= BEST_MIN_WORDS and not t.endswith("?") and not is_stock(t)


# --------------------------------------------------------------------------- #
# Players (nba-headshots metadata): roles and full names for the backfill
# --------------------------------------------------------------------------- #

class Players:
    """nba-headshots players_all.json, by name key. Empty when not given."""

    def __init__(self, headshots_dir: str | Path | None = None):
        self.by_key, self.by_team = {}, {}
        path = Path(headshots_dir or "") / "players" / "metadata" / "players_all.json"
        if headshots_dir and path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            for p in data.get("players", data) if isinstance(data, dict) else data:
                k = name_key(p.get("full_name") or "")
                if not k:
                    continue
                self.by_key[k] = None if k in self.by_key else p   # None = two players share it
                if p.get("active") and p.get("team_abbrev"):
                    self.by_team.setdefault(p["team_abbrev"], []).append(p)

        self.by_last = {}
        for k, p in self.by_key.items():
            if p is not None:
                self.by_last.setdefault(k.split()[-1], []).append(p)

    def get(self, name: str) -> dict | None:
        """Exact name, else the same surname with a first name that is a
        short form of the other ("Cam"/"Cameron", "Ron Holland II"/"Ronald
        Holland II"), when exactly one player fits (active players first)."""
        k = name_key(name)
        if k in self.by_key:
            return self.by_key[k]
        toks = k.split()
        if len(toks) < 2:
            return None
        first = toks[0]
        hits = [p for p in self.by_last.get(toks[-1], [])
                if (lambda f: f.startswith(first) or first.startswith(f))(name_key(p["full_name"]).split()[0])
                and len(min(first, name_key(p["full_name"]).split()[0], key=len)) >= 2]
        active = [p for p in hits if p.get("active")]
        for group in (active, hits):
            if len(group) == 1:
                return group[0]
        return None

    def is_player(self, name: str) -> bool:
        return name_key(name) in self.by_key

    def resolve_short(self, token: str, teams) -> str:
        """A first or last name alone ("Evan", "Mobley") -> the one player on
        these teams' rosters it fits, else ""."""
        k = name_key(token)
        if not k or " " in k:
            return ""
        teams = [teams] if isinstance(teams, str) else list(teams or [])
        hits = {p["full_name"] for t in teams for p in self.by_team.get(t, []) if k in name_key(p["full_name"]).split()}
        return hits.pop() if len(hits) == 1 else ""


TITLE_WORDS = re.compile(r"^(?:head coach|coach|gm|president)\s+", re.I)


def known_speakers(videos: list) -> dict:
    """team -> {surname or first name: {full names}} for every named speaker
    with a 2+ word name in the stored videos, so "Coach Nurse" on a 76ers
    video becomes Nick Nurse."""
    known = {}
    for data in videos:
        team = team_abbr(data.get("channel_team") or "")
        for q in data.get("quotes") or []:
            sp = " ".join((q.get("speaker") or "").split())
            if q.get("speaker_confidence") != "named" or pe.is_unidentified(sp) or TITLE_WORDS.match(sp):
                continue
            toks = name_key(sp).split()
            if len(toks) >= 2:
                for t in (toks[0], toks[-1]):
                    known.setdefault(team, {}).setdefault(t, set()).add(sp)
    return known


def title_teams(title: str) -> list:
    return [a for a, rx in _TEAM_TEXT_RE if rx.search(title or "")]


def resolve_name(name: str, teams: list, players: "Players", known: dict | None = None) -> str:
    """Best full name for a speaker or subject: the player list (exact or
    short first name), a lone first/last name on these teams' rosters, or a
    "Coach X" / lone name matching one full-named speaker of these teams."""
    name = " ".join((name or "").split())
    p = players.get(name)
    if p:
        return p["full_name"]
    bare = TITLE_WORDS.sub("", name)
    if " " not in bare:
        full = players.resolve_short(bare, teams)
        if full:
            return full
        hits = set()
        for t in teams:
            hits |= (known or {}).get(t, {}).get(name_key(bare), set())
        if len(hits) == 1:
            return hits.pop()
    return name


# --------------------------------------------------------------------------- #
# Transcripts (optional): YouTube captions, for the verbatim check
# --------------------------------------------------------------------------- #

class Transcripts:
    """Fetches captions with youtube-transcript-api when it is installed and
    PRESSERS_TRANSCRIPTS isn't "0". Gives up for the run after a few
    failures in a row (GitHub runners are often blocked by YouTube)."""

    def __init__(self, enabled: bool | None = None, fetcher=None):
        if enabled is None:
            enabled = os.getenv("PRESSERS_TRANSCRIPTS", "1") != "0"
        self.fetcher = fetcher
        if enabled and fetcher is None:
            self.fetcher = _youtube_fetcher()
        self.fetches = self.failures_in_row = 0
        self.stats = {"fetched": 0, "unavailable": 0}

    def words(self, video_id: str):
        """[(start, end, token)] or None (no captions / not tried)."""
        if (self.fetcher is None or self.fetches >= TRANSCRIPT_MAX_FETCHES
                or self.failures_in_row >= TRANSCRIPT_GIVE_UP_AFTER):
            self.stats["unavailable"] += 1
            return None
        self.fetches += 1
        out = {}

        def run():
            try:
                out["segments"] = self.fetcher(video_id)
            except Exception as e:  # noqa: BLE001 (any failure = no transcript)
                out["error"] = type(e).__name__

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(TRANSCRIPT_TIMEOUT_SECS)
        segments = out.get("segments")
        if not segments:
            self.failures_in_row += 1
            self.stats["unavailable"] += 1
            return None
        self.failures_in_row = 0
        self.stats["fetched"] += 1
        return caption_align.words_from_segments(segments)


def _youtube_fetcher():
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None

    def fetch(video_id):
        try:   # youtube-transcript-api 1.x
            got = YouTubeTranscriptApi().fetch(video_id, languages=["en"])
            return [(s.start, s.duration, s.text) for s in got.snippets]
        except AttributeError:   # 0.x
            got = YouTubeTranscriptApi.get_transcript(video_id, languages=["en"])
            return [(s["start"], s["duration"], s["text"]) for s in got]

    return fetch


def transcript_check(text: str, words, start: int) -> str:
    """"matched", "mismatch", or "unavailable" (no captions)."""
    if not words:
        return "unavailable"
    hit = caption_align.align_quote(text, words, hint_start=start)
    return "matched" if hit and hit["score"] >= caption_align.MIN_ALIGN_SCORE else "mismatch"


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #

def speaker_text(q: dict, speaker: str) -> str:
    """The speaker's own words: the quote, or for a multi-speaker block only
    the blocks that speaker says (questions from reporters left out)."""
    blocks = q.get("text_blocks") or []
    if blocks:
        mine = [(b.get("text") or "").strip() for b in blocks
                if isinstance(b, dict) and name_key(b.get("speaker") or "") == name_key(speaker)]
        return " ".join(t for t in mine if t).strip()
    return " ".join((q.get("quote") or "").split())


def exec_by_title(title: str, speaker: str) -> bool:
    """The title names this speaker and calls someone GM / president / owner
    ("Magic President Jeff Weltman Media Day"): an executive, whatever else
    said."""
    return bool(EXEC_TITLE_RE.search(title or "")) and pe.title_names_speaker(title or "", speaker)


def backfill_fields(q: dict, data: dict, speaker: str, text: str, team: str, players: Players) -> dict:
    """Rules fallback, only when the structured-fields call failed: role,
    subjects, teams, topics, context and best from the stored title, names,
    summary phrase and news score."""
    title = data.get("video_title") or ""
    if exec_by_title(title, speaker):
        role = "exec"
    elif players.get(speaker) or " " not in TITLE_WORDS.sub("", speaker) and not TITLE_WORDS.match(speaker):
        role = "player"   # in the player list, or a lone first name (staff are named by surname)
    elif EXEC_TITLE_RE.search(title) and pe.title_names_speaker(title, speaker):
        role = "exec"
    elif COACH_TITLE_RE.search(title) or players.by_key:
        # Not in the player list (or the title calls them coach): staff. Without
        # the list there is no telling, so "coach" only when the title says so.
        role = "exec" if EXEC_TITLE_RE.search(title) else "coach"
    else:
        role = "player"
    subjects = []
    ctx_teams = ([team] if team else []) + title_teams(title)
    for n in q.get("names_mentioned") or []:
        full = resolve_name(str(n or ""), ctx_teams, players)
        # A lone first name nobody could place is too vague to be a subject.
        if " " in full and name_key(full) != name_key(speaker) and full not in subjects:
            subjects.append(full)
    teams = [team] if team else []
    for abbr, rx in _TEAM_TEXT_RE:
        if abbr not in teams and rx.search(text):
            teams.append(abbr)
    scored = sorted(((len(rx.findall(text)), i, t) for i, (t, rx) in enumerate(TOPIC_RES.items())),
                    key=lambda x: (-x[0], x[1]))
    topics = [t for n, _, t in scored if n > 0][:2]
    phrase = pe.strip_dashes(" ".join(str(q.get("summary_phrase") or "").split())).rstrip(".")
    context = (phrase[0].upper() + phrase[1:] + ".") if phrase else ""
    best = (q.get("news_score") or 0) >= BACKFILL_BEST_SCORE
    return {"speaker_role": role, "subjects": subjects, "teams": teams, "topics": topics,
            "context": context, "best": best}


def needs_fields(data: dict) -> bool:
    """A press conference with named quotes but no "structured" block."""
    return pe.video_content_type(data) == "presser" and any(
        q.get("speaker_confidence") == "named" and not pe.is_unidentified(q.get("speaker") or "")
        and not isinstance(q.get("structured"), dict) for q in data.get("quotes") or [])


class GeminiFields:
    """The extractor's structured-fields call for stored videos, its answers
    cached in data/structured-fields.json ({video_id: {processed_at, by_rank}}).
    A failed call is not cached, so the next run tries again."""

    def __init__(self, client=None, cache_path: Path | None = None):
        self.client, self.path = client, Path(cache_path or FIELDS_CACHE)
        try:
            self.cache = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache = {}
        self.stats = {"calls": 0, "videos_ok": 0, "videos_failed": 0, "cached": 0}
        self._lock = threading.Lock()
        if client is not None:
            models, original = client.models, client.models.generate_content

            def counted(*a, **k):   # every attempt, retries included
                with self._lock:
                    self.stats["calls"] += 1
                return original(*a, **k)
            try:
                models.generate_content = counted
            except AttributeError:
                pass

    def by_rank(self, data: dict) -> dict | None:
        e = self.cache.get(data.get("video_id") or "")
        if e and e.get("processed_at") == (data.get("processed_at") or ""):
            return {int(k): v for k, v in (e.get("by_rank") or {}).items()}
        return None

    def fill(self, videos: list) -> None:
        todo = [d for d in videos if needs_fields(d) and self.by_rank(d) is None]
        self.stats["cached"] = sum(1 for d in videos if needs_fields(d)) - len(todo)
        if self.client is None or not todo:
            return

        def one(data):
            if pe._spending_cap_hit.is_set():
                return data, None
            return data, pe.derive_structured_fields(self.client, data, data.get("channel_team") or "")

        with ThreadPoolExecutor(max_workers=GEMINI_WORKERS) as pool:
            for data, got in pool.map(one, todo):
                if got:
                    self.cache[data["video_id"]] = {"processed_at": data.get("processed_at") or "",
                                                    "by_rank": {str(k): v for k, v in sorted(got.items())}}
                    self.stats["videos_ok"] += 1
                else:
                    self.stats["videos_failed"] += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pe.atomic_write(self.path, json.dumps(dict(sorted(self.cache.items())), ensure_ascii=False, indent=1) + "\n")


def records_for_video(data: dict, players: Players, transcripts: Transcripts | None = None,
                      counts: dict | None = None, known: dict | None = None, fields: dict | None = None) -> list:
    """Records for one stored video (press conferences only, named speakers
    only). fields: {rank: structured fields} from GeminiFields, for quotes
    without their own "structured" block."""
    counts = counts if counts is not None else {}
    vid = data.get("video_id") or ""
    if pe.video_content_type(data) != "presser" or not pe.VIDEO_ID_RE.match(vid):
        counts["not_presser_videos"] = counts.get("not_presser_videos", 0) + 1
        return []
    team = team_abbr(data.get("channel_team") or "")
    published = data.get("published") or ""
    date = ny_date(published) if published else ""
    if not date:
        return []
    ctx_teams = ([team] if team else []) + title_teams(data.get("video_title") or "")
    words = None
    out = []
    for q in data.get("quotes") or []:
        raw_speaker = " ".join((q.get("speaker") or "").split())
        if q.get("speaker_confidence") != "named" or pe.is_unidentified(raw_speaker):
            counts["unnamed"] = counts.get("unnamed", 0) + 1
            continue
        text = speaker_text(q, raw_speaker)
        speaker = resolve_name(raw_speaker, ctx_teams, players, known)
        if word_count(text) < 4:
            counts["empty"] = counts.get("empty", 0) + 1
            continue
        start = int(q.get("start_seconds") if q.get("start_seconds") is not None
                    else ytq.timestamp_to_seconds(q.get("timestamp") or "0:00"))
        if transcripts is not None and words is None:
            words = transcripts.words(vid) or []
        check = transcript_check(text, words, start)
        if check == "mismatch":
            counts["transcript_mismatch"] = counts.get("transcript_mismatch", 0) + 1
            continue
        s = q.get("structured")
        source = "gemini"
        if not isinstance(s, dict):
            s = (fields or {}).get(q.get("rank"))
            source = "gemini-backfill"
        if not isinstance(s, dict):
            s = backfill_fields(q, data, speaker, text, team, players)
            source = "rules-fallback"
        counts[source] = counts.get(source, 0) + 1
        p = players.get(speaker)
        role = s.get("speaker_role") or ("player" if p else "coach")
        if p and role != "player" and source == "rules-fallback" and not exec_by_title(data.get("video_title"), speaker):
            role = "player"
        if exec_by_title(data.get("video_title"), speaker):
            role = "exec"
        teams = []
        for t in ([team] if team else []) + list(s.get("teams") or []):
            a = team_abbr(t)
            if a and a not in teams:
                teams.append(a)
        subjects = []
        for n in s.get("subjects") or []:
            full = resolve_name(n, ctx_teams, players, known)
            if name_key(full) != name_key(speaker) and full not in subjects:
                subjects.append(full)
        out.append({
            "id": quote_id(vid, text),
            "video_id": vid,
            "video_title": data.get("video_title") or "",
            "published_at": published,
            "date": date,
            "season": season_of(date),
            "start_seconds": start,
            "speaker": {"name": speaker, "role": role if role in ROLES else "coach"},
            "subjects": subjects,
            "teams": teams,
            "topics": [t for t in s.get("topics") or [] if t in TOPICS][:2],
            "quote": text,
            "context": s.get("context") or "",
            "best": bool(s.get("best")) and best_ok(text),
            "news_score": q.get("news_score"),
            "fields_source": source,
            "transcript_check": check,
            "extracted_at": data.get("processed_at") or "",
        })
    return out


def iter_stored_videos(output_dir: Path | None = None):
    base = Path(output_dir or pe.OUTPUT_DIR)
    if not base.is_dir():
        return
    for day in sorted(p for p in base.iterdir() if p.is_dir() and pe.DAY_DIR_RE.match(p.name)):
        for js in sorted(day.glob("*.json")):
            if not pe.VIDEO_ID_RE.match(js.stem):
                continue
            try:
                data = json.loads(js.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and isinstance(data.get("quotes"), list):
                data.setdefault("video_id", js.stem)
                yield data


# --------------------------------------------------------------------------- #
# History (append-only) and views
# --------------------------------------------------------------------------- #

def history_path(season: str, history_dir: Path | None = None) -> Path:
    return Path(history_dir or HISTORY_DIR) / f"{season}.jsonl"


def read_history(season: str, history_dir: Path | None = None) -> list:
    path = history_path(season, history_dir)
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def current_view(records: list) -> list:
    """The latest snapshot of each video (highest extracted_at), one record
    per id, newest first."""
    latest = {}
    for r in records:
        v = r.get("video_id")
        latest[v] = max(latest.get(v, ""), r.get("extracted_at", ""))
    seen, out = set(), []
    for r in records:
        if r.get("extracted_at", "") != latest.get(r.get("video_id")) or r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append(r)
    out.sort(key=lambda r: (r.get("published_at", ""), r.get("video_id", ""), -int(r.get("start_seconds") or 0)),
             reverse=True)
    return out


def update_history(output_dir: Path | None = None, history_dir: Path | None = None,
                   players: Players | None = None, transcripts: Transcripts | None = None,
                   gemini: GeminiFields | None = None) -> dict:
    """Append the records of every stored video not read yet (or reprocessed
    since). Returns counts."""
    history_dir = Path(history_dir or HISTORY_DIR)
    history_dir.mkdir(parents=True, exist_ok=True)
    processed_path = history_dir / PROCESSED_PATH.name
    try:
        processed = json.loads(processed_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        processed = {}
    players = players or Players()
    counts = {"videos_read": 0, "records_appended": 0}
    new_lines = {}
    videos = list(iter_stored_videos(output_dir))
    known = known_speakers(videos)
    new = [d for d in videos if not (d["video_id"] in processed and processed[d["video_id"]] >= (d.get("processed_at") or ""))]
    if gemini is not None:
        gemini.fill(new)   # the structured-fields call for the videos about to be read
    for data in new:
        vid = data["video_id"]
        stamp = data.get("processed_at") or ""
        counts["videos_read"] += 1
        recs = records_for_video(data, players, transcripts, counts, known,
                                 gemini.by_rank(data) if gemini is not None else None)
        for r in recs:
            new_lines.setdefault(r["season"], []).append(r)
        processed[vid] = stamp
    for season, recs in sorted(new_lines.items()):
        recs.sort(key=lambda r: (r["published_at"], r["video_id"], r["start_seconds"]))
        path = history_path(season, history_dir)
        prefix = ""
        if path.is_file() and path.stat().st_size and not path.read_bytes().endswith(b"\n"):
            prefix = "\n"
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(prefix + "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in recs))
        counts["records_appended"] += len(recs)
    pe.atomic_write(processed_path, json.dumps(dict(sorted(processed.items())), indent=1) + "\n")
    return counts


def seasons_in(history_dir: Path | None = None) -> list:
    d = Path(history_dir or HISTORY_DIR)
    return sorted(p.stem for p in d.glob("*.jsonl") if re.fullmatch(r"\d{4}-\d{2}", p.stem)) if d.is_dir() else []


def current_season(history_dir: Path | None = None) -> str:
    seasons = seasons_in(history_dir)
    return seasons[-1] if seasons else season_of(ny_date(datetime.now(timezone.utc).isoformat()))


def public_payload(view: list, season: str) -> dict:
    best = [dict(r, url=watch_url(r["video_id"], r["start_seconds"])) for r in view if r.get("best")]
    return {
        "season": season,
        "source": "NBA team press conferences and media availabilities (nba-pressers-digest)",
        "url": PUBLIC_URL,
        "updated": max((r["published_at"] for r in best), default=""),
        "count": len(best),
        "quotes": best,
    }


def write_public_json(view: list, season: str, path: Path | None = None) -> Path:
    path = Path(path or PUBLIC_JSON)
    path.parent.mkdir(parents=True, exist_ok=True)
    pe.atomic_write(path, json.dumps(public_payload(view, season), ensure_ascii=False, indent=1) + "\n")
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--headshots", help="nba-headshots checkout (players_all.json) for roles and full names")
    ap.add_argument("--no-transcripts", action="store_true", help="skip the caption check")
    ap.add_argument("--gemini", action="store_true",
                    help="run the structured-fields call (GEMINI_API_KEY) for videos that have no answer yet")
    args = ap.parse_args(argv)
    pe._CONFIG.update(pe.load_config())   # title keywords for the content type of older videos
    players = Players(args.headshots)
    transcripts = Transcripts(enabled=not args.no_transcripts)
    client = None
    if args.gemini:
        key = os.getenv("GEMINI_API_KEY")
        if key:
            client = pe.genai.Client(api_key=key)
        else:
            print("[structured] GEMINI_API_KEY is not set: no structured-fields calls, rules fallback only")
    gemini = GeminiFields(client)
    counts = update_history(players=players, transcripts=transcripts, gemini=gemini)
    season = current_season()
    view = current_view(read_history(season))
    write_public_json(view, season)
    best = sum(1 for r in view if r.get("best"))
    print(f"[structured] players list: {len(players.by_key)} names; videos read: {counts['videos_read']}; "
          f"records appended: {counts['records_appended']}")
    print(f"[structured] skipped: unnamed speaker {counts.get('unnamed', 0)}, not a presser "
          f"{counts.get('not_presser_videos', 0)} video(s), transcript mismatch {counts.get('transcript_mismatch', 0)}; "
          f"captions fetched {transcripts.stats['fetched']}, unavailable {transcripts.stats['unavailable']}")
    g = gemini.stats
    print(f"[structured] structured-fields calls: {g['calls']} (videos answered {g['videos_ok']}, failed {g['videos_failed']}, "
          f"already cached {g['cached']}); records by source: gemini {counts.get('gemini', 0)}, "
          f"gemini-backfill {counts.get('gemini-backfill', 0)}, rules-fallback {counts.get('rules-fallback', 0)}")
    src = {}
    for r in view:
        src[r["fields_source"]] = src.get(r["fields_source"], 0) + 1
    print(f"[structured] {season}: {len(view)} quotes, {best} best -> {PUBLIC_JSON.relative_to(REPO)}; "
          f"by source {dict(sorted(src.items()))}; roles {dict(sorted(Counter(r['speaker']['role'] for r in view).items()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
