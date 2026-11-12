"""
Slack notifier for the pressers_v2 run.

Invoked by the workflow AFTER git push (same pattern as hoopshype-yt-quotes'
slack_notify_from_file.py) so the digest links are live when someone clicks:

    python pressers_v2/slack_notify.py pressers_v2/.slack_payload.json

Reads SLACK_WEBHOOK_URL from the environment; skips quietly when it's unset.
Links point at this repo's GitHub Pages site, derived from GITHUB_REPOSITORY,
so no extra URL secret is needed. Never fails the workflow.

After a successful digest post, the payload's alert_records move those
video ids from "pending" to "alerted" in output/slack_alerted.json (see
slack_state.py); the workflow commits that file in its next step.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import slack_state  # noqa: E402


def _log(msg: str) -> None:
    print(f"[slack] {msg}", flush=True)


def slack_escape(text: str) -> str:
    """Slack mrkdwn treats <...> as links/mentions (<!channel>) and & as an
    entity start. Escape all three for any text fetched from YouTube/Gemini."""
    return (str(text or "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _repo() -> tuple[str, str, str]:
    full = (os.getenv("GITHUB_REPOSITORY") or "jsierrahoopshype/nba-pressers-digest").strip()
    owner, _, name = full.partition("/")
    branch = (os.getenv("GITHUB_REF_NAME") or "main").strip()
    return owner, name, branch


def pages_base() -> str:
    owner, name, _ = _repo()
    return f"https://{owner.lower()}.github.io/{name}/pressers_v2"


def manifest_raw_url() -> str:
    owner, name, branch = _repo()
    return f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/pressers_v2/output/latest_clips.json"


def digest_link(date_str: str, run_slot: str | None) -> str:
    filename = f"digest-{run_slot}.html" if run_slot else "digest.html"
    return f"{pages_base()}/{date_str}/{filename}"


def _truncate(text: str, n: int = 200) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[:n].rstrip() + "..."


def _post(payload: dict) -> bool:
    url = (os.getenv("SLACK_WEBHOOK_URL") or "").strip()
    if not url:
        _log("SLACK_WEBHOOK_URL not set; skipping Slack post.")
        return False
    try:
        resp = requests.post(url, json=payload, timeout=15)
    except requests.RequestException as e:
        # str(e) can include the webhook URL; log only the exception type.
        _log(f"Slack POST failed: {type(e).__name__}")
        return False
    if resp.status_code != 200:
        _log(f"Slack returned {resp.status_code}: {resp.text[:200]}")
        return False
    return True


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _page_lines(pages: list) -> list:
    # One link per page: pressers, podcasts & shows, one-offs.
    return [f"{slack_escape(pg.get('label') or '')}: {pages_base()}/{pg['date']}/{pg['file']}"
            for pg in pages or [] if pg.get("date") and pg.get("file")]


def post_no_new_videos(date_str: str, run_slot: str = "", pages: list | None = None) -> bool:
    heading = slack_state.slot_heading(date_str, run_slot, _now()) if run_slot else date_str
    lines = [f"NBA Pressers v2 — {heading}: No new press conferences this run."]
    lines.extend(_page_lines(pages))
    return _post({"text": "\n".join(lines)})


def _groups(payload: dict) -> list:
    """The post's slots. Older payloads (one run, "items") read as one slot."""
    if payload.get("groups") is not None:
        return payload["groups"]
    items = [it for it in payload.get("items") or [] if it.get("content_type") != "podcast"]
    if not items:
        return []
    return [{"slot_date": payload.get("date_str") or "", "run_slot": payload.get("run_slot") or "",
             "items": items, "pages": payload.get("pages") or []}]


def build_digest_text(payload: dict, now: datetime | None = None) -> str:
    """The Slack text: header with the slot's date (and "posted late" after
    midnight UTC for a previous day's slot), warnings, then per slot its page
    links and its videos (title (team), clip count, top quote). Podcasts &
    shows are never listed (their page link stays)."""
    now = now or _now()
    groups = _groups(payload)
    date_str = payload.get("date_str") or ""
    run_slot = payload.get("run_slot") or ""
    aborted_count = int(payload.get("aborted_count") or 0)
    deferred_count = int(payload.get("deferred_count") or 0)
    clip_count = int(payload.get("clip_count") or 0)

    if len(groups) == 1:
        heading = slack_state.slot_heading(groups[0]["slot_date"], groups[0]["run_slot"], now)
    else:
        heading = slack_state.slot_heading(date_str, run_slot, now)
    lines = [f"*NBA Pressers v2 — {slack_escape(heading)}*"]
    if aborted_count:
        lines.append(
            f":warning: ABORTED: Gemini spending cap hit; {aborted_count} video(s) in queue "
            "were not processed. Re-run after the cap is raised."
        )
    if deferred_count:
        lines.append(
            f":hourglass_flowing_sand: {deferred_count} video(s) deferred due to Gemini "
            "high demand (503/500); will retry next run."
        )
        deferred = [d for d in (payload.get("deferred_videos") or []) if d.get("video_id")]
        if deferred:
            lines.append("Deferred videos:")
            for dv in deferred:
                url = f"https://www.youtube.com/watch?v={dv['video_id']}"
                lines.append(f"• {slack_escape(dv.get('title') or dv['video_id'])} "
                             f"({slack_escape(dv.get('channel') or '')}): {url}")
    n = sum(len(g["items"]) for g in groups)
    one_off = sum(1 for g in groups for it in g["items"] if it.get("is_one_off"))
    summary = f"{n} new video{'s' if n != 1 else ''}"
    if one_off:
        summary += f" ({n - one_off} from team channels, {one_off} one-off)"
    lines.append(summary + f". {clip_count} clip(s) in the 48h manifest.")
    if len(groups) <= 1:
        lines.extend(_page_lines(groups[0].get("pages") if groups else payload.get("pages")))
    lines.append(f"Clip manifest: {manifest_raw_url()}")

    for g in groups:
        if len(groups) > 1:
            # several slots (catch-up): each with its own heading and page links
            lines.extend(["", f"*{slack_escape(slack_state.slot_heading(g['slot_date'], g['run_slot'], now))}*"])
            lines.extend(_page_lines(g.get("pages")))
        # Same grouping and order as the digest; empty groups are skipped.
        for ctype, label in (("presser", "Press conferences"), ("oneoff", "One-offs")):
            members = [it for it in g["items"] if (it.get("content_type") if it.get("content_type") in
                                                   ("presser", "podcast", "oneoff") else "presser") == ctype]
            if members:
                lines.extend(["", f"*{label}* ({len(members)})"])
                for it in members:
                    lines.extend(_item_lines(it))
    return "\n".join(lines)


def post_digest(payload: dict) -> bool:
    groups = _groups(payload)
    if not groups and not int(payload.get("aborted_count") or 0) and not int(payload.get("deferred_count") or 0):
        return post_no_new_videos(payload.get("date_str") or "", payload.get("run_slot") or "",
                                  payload.get("pages"))
    return _post({"text": build_digest_text(payload)})


def record_alerted(records: dict, path: Path | None = None) -> None:
    """After a successful post: these ids move from pending to alerted in
    slack_alerted.json (the workflow commits it)."""
    if not records:
        return
    path = path or slack_state.STATE_PATH
    try:
        state = slack_state.load(path) or slack_state.empty_state()
        slack_state.mark_alerted(state, records)
        slack_state.save(state, path)
        _log(f"recorded {len(records)} video id(s) as alerted in {path.name}")
    except Exception as e:   # a failed write only means a possible repeat next run
        _log(f"could not record alerted ids ({type(e).__name__}: {e})")


def _item_lines(it: dict) -> list:
    """Title (team) with clip count, and the top quote. No draft post in
    Slack (the digest pages keep it)."""
    title = slack_escape(it.get("title") or it.get("video_id") or "")
    channel = slack_escape(it.get("channel") or "")
    speaker = slack_escape((it.get("speaker") or "").strip())
    quote = slack_escape(_truncate(it.get("top_quote") or ""))
    lines = ["", f"🎙️ *{title}* ({channel}), {int(it.get('clip_count') or 0)} clip(s)"]
    if quote:
        lines.append(f'Top quote: "{quote}"' + (f" ({speaker})" if speaker else ""))
    return lines


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python slack_notify.py <payload.json>", file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        _log(f"payload not found at {path}; nothing to post")
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _log(f"could not parse payload {path}: {e}")
        return 0
    kind = (payload.get("kind") or "").strip()
    if kind == "no_new_videos":
        post_no_new_videos(payload.get("date_str") or "", payload.get("run_slot") or "", payload.get("pages"))
    elif kind == "digest":
        if post_digest(payload):
            record_alerted(payload.get("alert_records") or {})
    else:
        _log(f"unknown payload kind: {kind!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
