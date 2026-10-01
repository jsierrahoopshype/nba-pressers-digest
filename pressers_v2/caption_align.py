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


# --------------------------------------------------------------------------- #
# Word-level timing (for subtitles that appear exactly when words are said)
# --------------------------------------------------------------------------- #

def json3_has_word_timing(data) -> bool:
    """True for YouTube's speech-recognition captions, whose json3 events
    carry a time offset for every word. Manual captions only time whole
    lines, which is not precise enough for word-timed subtitles."""
    if isinstance(data, (str, bytes)):
        data = json.loads(data)
    return any("tOffsetMs" in s for ev in data.get("events") or [] for s in (ev.get("segs") or [])[1:])


_VTT_WORD_TS = re.compile(r"<(\d+):(\d{2}):(\d{2})\.(\d{3})>")


def word_timings_from_vtt(text: str) -> list:
    """Word-level timings from auto-caption WebVTT, where each word carries
    an inline <00:00:01.234> timestamp. [] when the file has none."""
    if not _VTT_WORD_TS.search(text or ""):
        return []
    out, seen_until = [], -1.0
    for block in re.split(r"\n\s*\n", text):
        m = re.search(_VTT_TIME + r"\s*-->\s*" + _VTT_TIME, block)
        if not m:
            continue
        cue_start = _vtt_secs(re.match(_VTT_TIME, m.group(0)))
        body = block[m.end():]
        for line in body.splitlines():
            if not _VTT_WORD_TS.search(line):
                continue                      # the repeated previous line has no inline times
            pieces = re.split(r"(<\d+:\d{2}:\d{2}\.\d{3}>)", line)
            t = cue_start
            for piece in pieces:
                tm = _VTT_WORD_TS.fullmatch(piece)
                if tm:
                    t = _vtt_secs(tm)
                    continue
                for tok in norm_words(re.sub(r"<[^>]+>", "", piece)):
                    if t > seen_until:
                        out.append([t, None, tok])
            seen_until = max(seen_until, t)
    out.sort(key=lambda w: w[0])
    for i, w in enumerate(out):
        nxt = out[i + 1][0] if i + 1 < len(out) else w[0] + 0.4
        w[1] = max(w[0] + 0.05, min(nxt, w[0] + 1.2))
    return [tuple(w) for w in out]


def quote_word_times(text: str, words: list, t0: float, t1: float, min_match: float = 0.6) -> list:
    """Time every word of the quote text (with its punctuation and capitals)
    from word-level timings [(start, end, token)] around [t0, t1]. Words the
    captions missed get times interpolated between their neighbours.
    Returns [(start, end, display_word)], or [] when too few words match."""
    display = (text or "").split()
    if not display:
        return []
    needle, owner = [], []
    for i, w in enumerate(display):
        for tok in norm_words(w):
            needle.append(tok)
            owner.append(i)
    hay = [w for w in words if t0 - 1.0 <= w[0] <= t1 + 1.0]
    if not needle or not hay:
        return []
    sm = SequenceMatcher(None, needle, [w[2] for w in hay], autojunk=False)
    times = [None] * len(display)
    matched = 0
    for b in sm.get_matching_blocks():
        for k in range(b.size):
            i = owner[b.a + k]
            s, e = hay[b.b + k][0], hay[b.b + k][1]
            matched += 1
            times[i] = (s, e) if times[i] is None else (min(times[i][0], s), max(times[i][1], e))
    if matched / len(needle) < min_match:
        return []
    # interpolate the words the captions didn't have
    known = [i for i, t in enumerate(times) if t]
    for i in range(len(display)):
        if times[i]:
            continue
        before = max((k for k in known if k < i), default=None)
        after = min((k for k in known if k > i), default=None)
        if before is None:
            a, b = max(t0, times[after][0] - 0.3 * (after - i)), times[after][0]
            span_from, span_n = a, after
        elif after is None:
            a, b = times[before][1], times[before][1] + 0.3 * (i - before)
            span_from, span_n = a, i - before
        else:
            a, b = times[before][1], times[after][0]
            span_from, span_n = a, after - before
        pos = (i - (before if before is not None else 0)) or 1
        step = max(0.05, (b - span_from) / max(1, span_n))
        s = span_from + step * (pos - 1 if before is None else pos - 1)
        times[i] = (s, s + step)
    return [(round(times[i][0], 3), round(max(times[i][1], times[i][0] + 0.05), 3), display[i])
            for i in range(len(display))]


def subtitle_chunks(word_times: list, max_words: int = 4, min_words: int = 2, max_chars: int = 26) -> list:
    """Group timed words into 2-4 word captions, breaking after punctuation.
    Each caption shows from its first word's start until just after its last
    word (or until the next caption). Returns [(start, end, text)]."""
    groups, cur = [], []
    for w in word_times:
        text = " ".join(x[2] for x in cur + [w])
        if cur and (len(cur) >= max_words or len(text) > max_chars):
            groups.append(cur)
            cur = []
        cur.append(w)
        if len(cur) >= min_words and re.search(r"[.,!?;:]$", w[2]):
            groups.append(cur)
            cur = []
    if cur:
        if len(cur) < min_words and groups:
            if len(groups[-1]) < max_words:
                groups[-1].extend(cur)                    # "right direction" + "now"
                cur = []
            elif len(groups[-1]) > min_words:
                cur.insert(0, groups[-1].pop())           # 4 + 1 -> 3 + 2
        if cur:
            groups.append(cur)
    out = []
    for k, g in enumerate(groups):
        start, end = g[0][0], g[-1][1]
        nxt = groups[k + 1][0][0] if k + 1 < len(groups) else None
        end = min(end + 0.25, nxt) if nxt is not None else end + 0.4
        out.append((round(start, 3), round(max(end, start + 0.3), 3), " ".join(x[2] for x in g)))
    return out
