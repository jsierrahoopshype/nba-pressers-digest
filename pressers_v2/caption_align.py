"""
Fuzzy-align a quote's text to timed words (YouTube captions or Whisper).

Used by presser_extractor.py (cloud, youtube-transcript-api captions) and by
make_presser_clips.py (local, yt-dlp captions or faster-whisper words).
Pure standard library so it runs anywhere.

    words = words_from_segments([(start, duration, text), ...])
    hit = align_quote(quote_text, words, hint_start=83)
    if hit and hit["score"] >= MIN_ALIGN_SCORE:
        start, end = hit["start"], hit["end"]
"""

import json
import re
import unicodedata
from difflib import SequenceMatcher

MIN_ALIGN_SCORE = 0.6       # below this the match is treated as unreliable
HEAD_TAIL_WORDS = 8          # words compared at each end of the quote
SECS_PER_MISSING_WORD = 0.3  # time allowance for quote words absent from captions
WORDS_PER_SECOND = 2.6


def norm_words(text: str) -> list:
    """Lowercase ASCII tokens without punctuation ("Don't" -> "dont")."""
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    t = t.replace("'", "").replace("’", "")
    return re.findall(r"[a-z0-9]+", t)


# --------------------------------------------------------------------------- #
# Timed-word builders: each returns [(start, end, token), ...] sorted by start
# --------------------------------------------------------------------------- #

def words_from_segments(segments) -> list:
    """Segments are (start, duration, text); times inside a segment are
    interpolated evenly by token. Works for youtube-transcript-api snippets."""
    out = []
    for start, dur, text in segments:
        toks = norm_words(text)
        if not toks:
            continue
        dur = max(float(dur or 0), 0.2 * len(toks))
        step = dur / len(toks)
        for i, tok in enumerate(toks):
            s = float(start) + i * step
            out.append((s, s + step, tok))
    out.sort(key=lambda w: w[0])
    return out


def words_from_json3(data) -> list:
    """YouTube json3 captions. Auto-captions carry per-word offsets
    (tOffsetMs); manual ones don't, so those get interpolated."""
    if isinstance(data, (str, bytes)):
        data = json.loads(data)
    out = []
    for ev in data.get("events") or []:
        segs = ev.get("segs") or []
        if not segs or "tStartMs" not in ev:
            continue
        ev_start = ev["tStartMs"] / 1000.0
        ev_end = ev_start + (ev.get("dDurationMs") or 0) / 1000.0
        has_offsets = any("tOffsetMs" in s for s in segs[1:])
        if has_offsets:
            timed = []
            for s in segs:
                toks = norm_words(s.get("utf8", ""))
                t0 = ev_start + (s.get("tOffsetMs") or 0) / 1000.0
                for tok in toks:
                    timed.append([t0, None, tok])
            for i, w in enumerate(timed):
                nxt = timed[i + 1][0] if i + 1 < len(timed) else max(ev_end, w[0] + 0.3)
                w[1] = max(nxt, w[0] + 0.05)
            out.extend(tuple(w) for w in timed)
        else:
            text = "".join(s.get("utf8", "") for s in segs)
            out.extend(words_from_segments([(ev_start, ev_end - ev_start, text)]))
    out.sort(key=lambda w: w[0])
    return out


_VTT_TIME = r"(\d+):(\d{2}):(\d{2})\.(\d{3})"


def _vtt_secs(m) -> float:
    h, mi, s, ms = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000.0


def words_from_vtt(text: str) -> list:
    """WebVTT captions. Rolling auto-captions repeat the previous line, so
    only tokens beyond what the previous cue already showed are kept."""
    segments, prev_tokens = [], []
    for block in re.split(r"\n\s*\n", text or ""):
        m = re.search(_VTT_TIME + r"\s*-->\s*" + _VTT_TIME, block)
        if not m:
            continue
        start = _vtt_secs(re.match(_VTT_TIME, m.group(0)))
        end_m = re.search(r"-->\s*" + _VTT_TIME, m.group(0))
        end = _vtt_secs(re.match(_VTT_TIME, end_m.group(0)[3:].strip())) if end_m else start + 2
        body = block[m.end():]
        body = re.sub(r"<[^>]+>", "", body)
        toks = norm_words(body)
        # drop the prefix repeated from the previous cue
        k = 0
        for n in range(min(len(prev_tokens), len(toks)), 0, -1):
            if prev_tokens[-n:] == toks[:n]:
                k = n
                break
        new = toks[k:]
        if new:
            segments.append((start, max(end - start, 0.2), " ".join(new)))
        prev_tokens = toks or prev_tokens
    return words_from_segments(segments)


# --------------------------------------------------------------------------- #
# Alignment
# --------------------------------------------------------------------------- #

def _match_in_window(needle: list, hay: list) -> tuple:
    """(fraction of needle matched in order, first hay idx, last hay idx,
    first needle idx, last needle idx) or (0, ...) when nothing matches."""
    sm = SequenceMatcher(None, needle, hay, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size]
    if not blocks:
        return 0.0, -1, -1, -1, -1
    matched = sum(b.size for b in blocks)
    first, last = blocks[0], blocks[-1]
    return (matched / len(needle), first.b, last.b + last.size - 1,
            first.a, last.a + last.size - 1)


def align_quote(text: str, words: list, hint_start: float | None = None,
                window: tuple | None = None) -> dict | None:
    """Find where `text` is spoken inside `words` ([(start, end, token)]).

    Returns {"start", "end", "score", "head", "tail", "coverage"} for the
    best candidate, or None when nothing plausible is found. `score` is
    0..1; callers should treat < MIN_ALIGN_SCORE as unreliable.
    `window` = (t0, t1) restricts the search; `hint_start` breaks ties
    between repeated phrases in favour of the Gemini estimate."""
    q = norm_words(text)
    if len(q) < 4 or not words:
        return None
    if window:
        words = [w for w in words if window[0] <= w[0] <= window[1]]
        if not words:
            return None
    toks = [w[2] for w in words]
    k = max(3, min(HEAD_TAIL_WORDS, len(q) // 2))
    head, tail = q[:k], q[-k:]
    head_set, tail_set = set(head[:3]), set(tail[-3:])
    slack = 3

    heads = []
    for i, tok in enumerate(toks):
        if tok not in head_set:
            continue
        frac, b0, _, a0, _ = _match_in_window(head, toks[i:i + k + slack])
        if frac >= 0.5:
            heads.append((frac, i + b0, a0))
    tails = []
    for j, tok in enumerate(toks):
        if tok not in tail_set:
            continue
        lo = max(0, j - k - slack + 1)
        frac, _, b1, _, a1 = _match_in_window(tail, toks[lo:j + 1])
        if frac >= 0.5:
            tails.append((frac, lo + b1, len(tail) - 1 - a1))
    if not heads or not tails:
        return None

    expected = len(q) / WORDS_PER_SECOND
    best = None
    heads.sort(reverse=True)
    tails.sort(reverse=True)
    for hf, hi, missing_head in heads[:25]:
        for tf, ti, missing_tail in tails[:25]:
            if ti < hi:
                continue
            start = max(0.0, words[hi][0] - missing_head * SECS_PER_MISSING_WORD)
            end = words[ti][1] + missing_tail * SECS_PER_MISSING_WORD
            span = end - start
            if not (0.4 * expected <= span <= 2.5 * expected + 3):
                continue
            cov, *_ = _match_in_window(q, toks[hi:ti + 1])
            score = 0.25 * hf + 0.25 * tf + 0.5 * cov
            dist = abs(start - hint_start) if hint_start is not None else 0.0
            key = (round(score, 3), -dist)
            if best is None or key > best[0]:
                best = (key, {"start": round(start, 2), "end": round(end, 2),
                              "score": round(score, 3), "head": round(hf, 3),
                              "tail": round(tf, 3), "coverage": round(cov, 3)})
    return best[1] if best else None
