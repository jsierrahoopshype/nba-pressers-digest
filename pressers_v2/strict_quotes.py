"""
Strict press conference quotes: zero guessing. DRY RUN ONLY for now: nothing
here writes the history, the pages or the public JSON.

A quote survives only when every rule holds:
  1. Speaker: a full name (2+ words, no title such as "Coach") that the
     video's title or YouTube description contains word for word. No channel
     to coach mapping, no roster inference.
  2. Subjects: full names that are spoken word for word inside the quote,
     taken from the names the extraction listed, and that are exactly a known
     person (nba-headshots' player list, quotes-lab's people). No first names,
     nicknames, titles, corrected spellings, or names that aren't in the quote.
     A quote with no such subject is dropped.
  3. Context line: kept only when every name in it (any capitalized word that
     doesn't start the sentence) also appears in the quote; otherwise no line.
  4. Exact words: a second, independent Gemini call watches only the quote's
     segment of the video and transcribes it word for word. The quote must
     appear in that transcription exactly (case and punctuation ignored), as
     one unbroken run of words. Any difference: dropped.
  5. No team labels at all.
Single-speaker quotes only (a block with a reporter's question in it can't
match a transcription of the speaker word for word).

Candidates are the quotes the structured-fields call marked "best" (the ones
the Quotes section would take); the others are counted, not transcribed.

Usage (from repo root, GEMINI_API_KEY and YOUTUBE_API_KEY set):
    python pressers_v2/strict_quotes.py --dry-run [--limit N]
"""

import argparse
import difflib
import json
import os
import re
import sys
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import presser_extractor as pe  # noqa: E402
import structured_quotes as sq  # noqa: E402

TITLE_WORDS = {"coach", "head", "assistant", "gm", "president", "mr", "mrs", "ms", "dr", "general", "manager",
               "owner", "governor", "commissioner", "sir"}
SEGMENT_BEFORE = 15        # seconds of video before the extraction's timestamp
SEGMENT_AFTER = 30         # seconds after the estimated end of the quote
WORKERS = 4
TRANSCRIBE_PROMPT = """Transcribe, word for word, everything that is said in this clip of a press conference.
Write exactly the words spoken, in order: do not summarize, correct grammar, drop filler words ("um", "you know") or add anything.
No speaker labels, no timestamps, no notes. Return only the words."""


def tokens(text: str) -> list:
    """Lowercase words without accents or punctuation ("Dončić's" -> "doncics")."""
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    t = re.sub(r"['’`]", "", t)
    return re.findall(r"[a-z0-9]+", t)


def contains(hay: list, needle: list) -> bool:
    n = len(needle)
    return n > 0 and any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))


SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def says_name(quote_tokens: list, name: str) -> bool:
    """The quote says this name word for word, not as the start of a longer one
    ("Gary Payton" is not said by "Gary Payton II": different people)."""
    n = tokens(name)
    k = len(n)
    return any(quote_tokens[i:i + k] == n and (i + k == len(quote_tokens) or quote_tokens[i + k] not in SUFFIXES)
               for i in range(len(quote_tokens) - k + 1)) if k else False


def full_name(name: str) -> bool:
    """2+ words, none a title, the last one a real word (not initials: "J.A." is not a name)."""
    words = (name or "").split()
    toks = tokens(name)
    last = tokens(words[-1]) if words else []
    return (len(words) >= 2 and not (set(toks) & TITLE_WORDS) and all(t.isalpha() for t in toks)
            and len(last) == 1 and len(last[0]) >= 2)


def speaker_named(speaker: str, title: str, description: str) -> bool:
    return full_name(speaker) and says_name(tokens(f"{title}\n{description}"), speaker)


def subjects_in_quote(candidates: list, quote: str, speaker: str, known: set | None = None) -> list:
    """Full names spoken word for word in the quote that are also, exactly, a known
    person (known: set of " ".join(tokens(name))). A misspelled name in the quote
    ("Drew Holiday") matches nobody and is not a subject."""
    q = tokens(quote)
    out = []
    for n in candidates:
        n = " ".join(str(n or "").split())
        k = " ".join(tokens(n))
        if full_name(n) and says_name(q, n) and tokens(n) != tokens(speaker) \
                and (known is None or k in known) and all(tokens(n) != tokens(o) for o in out):
            out.append(n)
    return out


def known_people(headshots_dir: str | None, extra_files: list) -> set:
    """Exact full names: every player in nba-headshots players_all.json, plus the
    names in any extra JSON list (quotes-lab duo.json: {sp: [[name, ...]], su: [...]})."""
    names = set()
    p = Path(headshots_dir or "") / "players" / "metadata" / "players_all.json"
    if headshots_dir and p.is_file():
        for x in json.loads(p.read_text(encoding="utf-8")).get("players", []):
            names.add(" ".join(tokens(x.get("full_name") or "")))
    for f in extra_files:
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for key in ("sp", "su"):
            for row in d.get(key) or []:
                names.add(" ".join(tokens(row[0])))
    names.discard("")
    return names


def context_ok(context: str, quote: str) -> str:
    """The context line, or "" when it names anything the quote doesn't."""
    context = " ".join((context or "").split())
    if not context:
        return ""
    q = set(tokens(quote))
    start = True
    for raw in context.split():
        word = raw.strip("\"'“”‘’()[],;:!?")
        if word and word[0].isupper() and not start:
            if any(t not in q for t in tokens(word)):
                return ""
        start = raw.endswith((".", "!", "?"))
    return context


def segment(q: dict, duration: int) -> tuple[int, int]:
    start = int(q.get("start_seconds") or 0)
    words = len((q.get("quote") or "").split())
    end = start + int(words / pe.WORDS_PER_SECOND) + SEGMENT_AFTER
    lo = max(0, start - SEGMENT_BEFORE)
    return lo, (min(end, duration) if duration else end)


def transcribe(client, video_id: str, lo: int, hi: int) -> str:
    """The second, independent call: only this slice of the video, word for word."""
    from google.genai import types
    part = types.Part.from_uri(file_uri=f"https://www.youtube.com/watch?v={video_id}", mime_type="video/mp4")
    part.video_metadata = types.VideoMetadata(start_offset=f"{lo}s", end_offset=f"{hi}s")
    resp = pe.with_retry(lambda: client.models.generate_content(
        model=pe.MODEL, contents=[part, TRANSCRIBE_PROMPT],
        config=types.GenerateContentConfig(temperature=0.0),
    ), "verbatim")
    return resp.text or ""


def difference(quote: str, heard: str) -> str:
    """Where the quote and the transcription part ways, for the dry-run report (public video words
    only): the longest run of the quote found word for word, then the first words that differ."""
    q, h = tokens(quote), tokens(heard)
    if not h:
        return "transcription empty"
    sm = difflib.SequenceMatcher(None, q, h, autojunk=False)
    run = sm.find_longest_match(0, len(q), 0, len(h)).size
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "equal":
            return (f"{run}/{len(q)} quote words in one run; quote has '{' '.join(q[max(0, i1 - 3):i2 + 2])}' "
                    f"where the clip has '{' '.join(h[max(0, j1 - 3):j2 + 2])}'")
    return f"{run}/{len(q)}"


def descriptions(video_ids: list, api_key: str) -> dict:
    out = {}
    ids = list(dict.fromkeys(video_ids))
    for i in range(0, len(ids), 50):
        data = pe.youtube_api_get("videos", {"part": "snippet", "id": ",".join(ids[i:i + 50])}, api_key)
        for item in data.get("items", []):
            sn = item.get("snippet") or {}
            out[item.get("id")] = {"title": sn.get("title") or "", "description": sn.get("description") or ""}
    return out


def candidates(videos: list, fields: "sq.GeminiFields", counts: dict) -> list:
    """(data, quote, structured fields) for every single-speaker, named, best
    quote of a stored press conference; everything else is counted."""
    out = []
    for data in videos:
        if pe.video_content_type(data) != "presser":
            continue
        by_rank = fields.by_rank(data) or {}
        for q in data.get("quotes") or []:
            counts["quotes"] = counts.get("quotes", 0) + 1
            s = sq.current_block(q) or by_rank.get(q.get("rank"))
            if q.get("speaker_confidence") != "named" or pe.is_unidentified(q.get("speaker") or ""):
                counts["unnamed"] = counts.get("unnamed", 0) + 1
                continue
            if q.get("text_blocks"):
                counts["multi_speaker"] = counts.get("multi_speaker", 0) + 1
                continue
            if not isinstance(s, dict) or not s.get("best"):
                counts["not_best"] = counts.get("not_best", 0) + 1
                continue
            out.append((data, q, s))
    return out


def check(data: dict, q: dict, s: dict, meta: dict, known: set | None = None) -> tuple[dict | None, str]:
    """Rules 1-3 (no Gemini). -> (pending record, "") or (None, reason)."""
    speaker = " ".join((q.get("speaker") or "").split())
    m = meta.get(data["video_id"]) or {"title": data.get("video_title") or "", "description": ""}
    if not speaker_named(speaker, m["title"], m["description"]):
        return None, "speaker not named by full name in title/description"
    quote = " ".join((q.get("quote") or "").split())
    subs = subjects_in_quote(list(s.get("subjects") or []) + list(q.get("names_mentioned") or []), quote, speaker, known)
    if not subs:
        return None, "no full-name subject spoken in the quote"
    start = int(q.get("start_seconds") or 0)
    return {"video_id": data["video_id"], "video_title": m["title"], "date": sq.ny_date(data.get("published") or ""),
            "start_seconds": start, "url": sq.watch_url(data["video_id"], start), "speaker": speaker,
            "subjects": subs, "quote": quote, "context": context_ok(s.get("context") or "", quote),
            "duration": int(data.get("duration_seconds") or 0), "_q": q}, ""


def dry_run(limit: int | None = None, known: set | None = None) -> dict:
    pe._CONFIG.update(pe.load_config())
    videos = list(sq.iter_stored_videos())
    counts = {}
    cands = candidates(videos, sq.GeminiFields(None), counts)
    yt_key = os.getenv("YOUTUBE_API_KEY")
    meta = descriptions([d["video_id"] for d, _, _ in cands], yt_key) if yt_key else {}
    if not yt_key:
        print("[strict] YOUTUBE_API_KEY not set: titles only, no descriptions")
    pending, dropped = [], []
    for data, q, s in cands:
        rec, why = check(data, q, s, meta, known)
        if rec:
            pending.append(rec)
        else:
            dropped.append((why, data, q))
    if limit:
        pending = pending[:limit]
    client = pe.genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    lock = threading.Lock()
    calls = {"n": 0, "failed": 0}

    def verify(rec):
        lo, hi = segment(rec["_q"], rec["duration"])
        try:
            text = transcribe(client, rec["video_id"], lo, hi)
        except Exception as e:  # noqa: BLE001 (any failure = not verified = dropped)
            with lock:
                calls["n"] += 1
                calls["failed"] += 1
            return rec, None, f"transcription call failed ({type(e).__name__})"
        with lock:
            calls["n"] += 1
        ok = contains(tokens(text), tokens(rec["quote"]))
        rec["_check"] = "match" if ok else difference(rec["quote"], text)
        rec["_heard_words"] = len(tokens(text))
        return rec, text, "" if ok else "words differ from the transcription"

    survivors = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for rec, text, why in pool.map(verify, pending):
            if why:
                dropped.append((why, None, rec))
            else:
                survivors.append(rec)
    reasons = {}
    for why, _, _ in dropped:
        reasons[why] = reasons.get(why, 0) + 1
    return {"counts": counts, "candidates": len(cands), "after_name_rules": len(pending), "gemini": calls,
            "dropped": reasons, "survivors": survivors, "checked": pending}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", required=True, help="report only; nothing is written")
    ap.add_argument("--limit", type=int, help="transcribe at most this many (testing)")
    ap.add_argument("--headshots", help="nba-headshots checkout (players_all.json: known players)")
    ap.add_argument("--people", action="append", default=[], help="extra known-people JSON (quotes-lab duo.json)")
    args = ap.parse_args(argv)
    known = known_people(args.headshots, args.people)
    print(f"[strict] known people (exact names): {len(known)}")
    r = dry_run(args.limit, known or None)
    c = r["counts"]
    print(f"[strict] stored presser quotes {c.get('quotes', 0)}: unnamed speaker {c.get('unnamed', 0)}, "
          f"multi-speaker {c.get('multi_speaker', 0)}, not best {c.get('not_best', 0)}; best candidates {r['candidates']}")
    print(f"[strict] after the speaker/subject rules: {r['after_name_rules']}; verbatim calls {r['gemini']['n']} "
          f"(failed {r['gemini']['failed']}); survivors {len(r['survivors'])}")
    for why, n in sorted(r["dropped"].items(), key=lambda x: -x[1]):
        print(f"[strict]   dropped: {why}: {n}")
    print("[strict] VERBATIM CHECK, every quote that passed the name rules (speaker | subjects | link | result)")
    for v in r["checked"]:
        print(f"[strict]   {v['speaker']} | {', '.join(v['subjects'])} | {v['url']} | clip {v.get('_heard_words', 0)} words | {v.get('_check', 'call failed')}")
        print(f"[strict]     quote: “{v['quote']}”")
    print("[strict] SURVIVORS (speaker | subjects | video link | quote | context)")
    for i, s in enumerate(sorted(r["survivors"], key=lambda s: (s["date"], s["video_id"], s["start_seconds"])), 1):
        print(f"[strict] {i}. {s['speaker']} | {', '.join(s['subjects'])} | {s['url']} | {s['date']} | "
              f"“{s['quote']}” | context: {s['context'] or '(none)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
