"""
Slack alert state for the pressers_v2 run (Jorge, Nov 12 2026: stop repetitive alerts).

One committed JSON file, pressers_v2/output/slack_alerted.json:

    {"alerted": {video_id: {"at", "slot_date", "run_id", "quote", ["dup_of"]}},
     "pending": {video_id: {item fields + "run_id", "run_slot", "slot_date", "queued_at"}}}

- Every run adds its new (non-podcast) videos to "pending".
- Overnight items wait until the night is over: a run that starts at or after
  10:00 UTC on the same day, or any run on a later day (the catch-up when the
  10 UTC run fails or never fires). Every other item is due right away.
- A video id in "alerted" is never posted again. slack_notify.py moves ids
  from "pending" to "alerted" only after a successful post.
- A due item whose top quote is essentially the same text as one already
  shown (this message or posted for the same day) is dropped; between a full
  media session and a single-speaker video, the single-speaker one is kept.
- Entries older than 14 days are pruned.

Pure functions, no network: the extractor and slack_notify.py both use them.
"""

import difflib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_PATH = Path(__file__).resolve().parent / "output" / "slack_alerted.json"
OVERNIGHT_SLOT = "overnight"
OVERNIGHT_LAST_HOUR = 10       # the last overnight trigger is 10:23 UTC
KEEP_DAYS = 14
QUOTE_PREFIX_CHARS = 200
QUOTE_MIN_SIMILARITY = 0.85
QUOTE_MIN_CHARS = 40           # shorter quotes ("Yeah, for sure.") never count as duplicates
SLOT_ORDER = {"overnight": 0, "14utc": 1, "20utc": 2}

# Fields of an extractor item kept in "pending" (enough to rebuild the Slack line).
ITEM_FIELDS = ("video_id", "title", "channel", "top_quote", "speaker", "clip_count", "date",
               "is_one_off", "content_type", "single_speaker")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def empty_state() -> dict:
    return {"alerted": {}, "pending": {}}


def load(path: Path = STATE_PATH) -> dict | None:
    """The stored state, or None when the file does not exist yet (the
    caller seeds it). A corrupt file reads as empty rather than crashing."""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_state()
    if not isinstance(data, dict):
        return empty_state()
    return {"alerted": dict(data.get("alerted") or {}), "pending": dict(data.get("pending") or {})}


def save(state: dict, path: Path = STATE_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"alerted": state.get("alerted") or {}, "pending": state.get("pending") or {}},
                              ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def prune(state: dict, now: datetime, days: int = KEEP_DAYS) -> None:
    cutoff = now - timedelta(days=days)
    for key, stamp in (("alerted", "at"), ("pending", "queued_at")):
        bucket = state.setdefault(key, {})
        for vid in [v for v, rec in bucket.items() if (_parse((rec or {}).get(stamp)) or now) < cutoff]:
            del bucket[vid]


def normalize_quote(text: str) -> str:
    """Lowercase, punctuation to spaces, whitespace collapsed, first 200 chars."""
    text = re.sub(r"[^\w\s]", " ", str(text or "").lower())
    return re.sub(r"\s+", " ", text).strip()[:QUOTE_PREFIX_CHARS]


def same_quote(a: str, b: str) -> bool:
    """a, b already normalized. ~85%+ similar (difflib ratio)."""
    if len(a) < QUOTE_MIN_CHARS or len(b) < QUOTE_MIN_CHARS:
        return False
    if a == b:
        return True
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return (sm.real_quick_ratio() >= QUOTE_MIN_SIMILARITY and sm.quick_ratio() >= QUOTE_MIN_SIMILARITY
            and sm.ratio() >= QUOTE_MIN_SIMILARITY)


def overnight_run_id(day: str) -> str:
    return f"{day}-{OVERNIGHT_SLOT}"


def is_due(item: dict, now: datetime) -> bool:
    """Overnight items wait for the end of their night: due from 10:00 UTC
    on their own day, or on any later day (catch-up). The rest: right away."""
    if item.get("run_slot") != OVERNIGHT_SLOT:
        return True
    if item.get("run_id") != overnight_run_id(now.strftime("%Y-%m-%d")):
        return True
    return now.hour >= OVERNIGHT_LAST_HOUR


def add_pending(state: dict, items: list, run_id: str, run_slot: str, slot_date: str, now: datetime) -> list:
    """Queue this run's new items. Podcasts never go to Slack (the
    hoopshype-yt-quotes bot alerts them) and alerted ids are skipped.
    Returns the ids queued."""
    queued = []
    alerted, pending = state.setdefault("alerted", {}), state.setdefault("pending", {})
    for it in items:
        vid = it.get("video_id")
        if not vid or vid in alerted or it.get("content_type") == "podcast":
            continue
        rec = {k: it.get(k) for k in ITEM_FIELDS if k in it}
        rec.update({"run_id": run_id, "run_slot": run_slot, "slot_date": slot_date,
                    "queued_at": (pending.get(vid) or {}).get("queued_at") or _iso(now)})
        pending[vid] = rec
        queued.append(vid)
    return queued


def due_items(state: dict, now: datetime) -> list:
    """Pending items to post now, oldest slot first, in the order queued."""
    alerted = state.get("alerted") or {}
    out = [dict(rec, video_id=vid) for vid, rec in (state.get("pending") or {}).items()
           if vid not in alerted and rec.get("content_type") != "podcast" and is_due(rec, now)]
    out.sort(key=lambda r: (r.get("slot_date") or "", SLOT_ORDER.get(r.get("run_slot") or "", 9),
                            r.get("run_id") or "", r.get("queued_at") or ""))
    return out


def dedupe(items: list, state: dict, now: datetime) -> tuple[list, dict]:
    """(items kept, {dropped video_id: video_id it duplicates}). Compared on
    the normalized top quote, against each other and against the videos
    alerted for the same slot day or posted today. Single-speaker videos are
    considered first, so they win over a full media session."""
    today = now.strftime("%Y-%m-%d")
    seen = []   # (normalized quote, video_id)
    for vid, rec in (state.get("alerted") or {}).items():
        if rec.get("quote") and ((rec.get("at") or "")[:10] == today
                                 or rec.get("slot_date") in {it.get("slot_date") for it in items}):
            seen.append((rec["quote"], vid, rec.get("slot_date"), (rec.get("at") or "")[:10]))
    order = sorted(range(len(items)), key=lambda i: (not items[i].get("single_speaker"), i))
    dropped, kept_idx = {}, set()
    for i in order:
        it = items[i]
        q = normalize_quote(it.get("top_quote") or "")
        match = next((v for sq, v, sd, at in seen
                      if (sd == it.get("slot_date") or at == today or sd is None) and same_quote(q, sq)), None)
        if match:
            dropped[it["video_id"]] = match
            continue
        kept_idx.add(i)
        if q:
            seen.append((q, it["video_id"], None, None))
    return [it for i, it in enumerate(items) if i in kept_idx], dropped


def alert_records(items: list, dropped: dict, now: datetime) -> dict:
    """{video_id: alerted record} for the kept items and the dropped duplicates."""
    out = {}
    for it in items:
        rec = {"at": _iso(now), "slot_date": it.get("slot_date") or "", "run_id": it.get("run_id") or "",
               "quote": normalize_quote(it.get("top_quote") or "")}
        if it["video_id"] in dropped:
            rec["dup_of"] = dropped[it["video_id"]]
        out[it["video_id"]] = rec
    return out


def mark_alerted(state: dict, records: dict) -> None:
    state.setdefault("alerted", {}).update(records)
    pending = state.setdefault("pending", {})
    for vid in records:
        pending.pop(vid, None)


def slot_label(run_slot: str) -> str:
    """'overnight' -> 'overnight', '20utc' -> '20 UTC run', 'manual-1405' -> 'manual 14:05 UTC run'."""
    m = re.match(r"^(\d{2})utc$", run_slot or "")
    if m:
        return f"{m.group(1)} UTC run"
    m = re.match(r"^manual-(\d{2})(\d{2})$", run_slot or "")
    if m:
        return f"manual {m.group(1)}:{m.group(2)} UTC run"
    return run_slot or ""


def slot_heading(slot_date: str, run_slot: str, now: datetime) -> str:
    """'2026-11-12 overnight', '2026-11-11 20 UTC run, posted late' (posted
    after midnight UTC for a previous day's slot)."""
    text = f"{slot_date} {slot_label(run_slot)}".strip()
    if slot_date and now.strftime("%Y-%m-%d") > slot_date:
        text += ", posted late"
    return text


def group_by_slot(items: list) -> list:
    """[{"slot_date", "run_slot", "items"}], oldest slot first."""
    groups = {}
    for it in items:
        key = (it.get("slot_date") or "", it.get("run_slot") or "")
        groups.setdefault(key, []).append(it)
    return [{"slot_date": k[0], "run_slot": k[1], "items": v}
            for k, v in sorted(groups.items(), key=lambda kv: (kv[0][0], SLOT_ORDER.get(kv[0][1], 9), kv[0][1]))]


def seed_from_outputs(output_dir: Path, now: datetime, days: int = KEEP_DAYS) -> dict:
    """First run with this file: every video already stored in the last 14
    days was posted by the old per-run alerts, so record them as alerted
    (their top quotes too, for the same-day quote check)."""
    state = empty_state()
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    for day_dir in sorted(Path(output_dir).glob("20??-??-??")):
        if not day_dir.is_dir() or day_dir.name < cutoff:
            continue
        for js in sorted(day_dir.glob("*.json")):
            try:
                data = json.loads(js.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or not data.get("quotes"):
                continue
            top = data["quotes"][0] if isinstance(data["quotes"][0], dict) else {}
            blocks = top.get("text_blocks") or []
            text = (" ".join((b.get("text") or "").strip() for b in blocks if isinstance(b, dict))
                    if blocks else (top.get("quote") or ""))
            at = data.get("processed_at") or _iso(now)
            if (_parse(at) or now) < now - timedelta(days=days):
                at = _iso(now)   # stored long ago but filed recently: keep it for the full 14 days
            state["alerted"][data.get("video_id") or js.stem] = {
                "at": at, "slot_date": day_dir.name, "run_id": data.get("run_id") or "",
                "quote": normalize_quote(text), "seeded": True}
    return state
