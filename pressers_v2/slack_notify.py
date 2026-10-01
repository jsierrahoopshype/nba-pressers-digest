"""
Slack notifier for the pressers_v2 run.

Invoked by the workflow AFTER git push (same pattern as hoopshype-yt-quotes'
slack_notify_from_file.py) so the digest links are live when someone clicks:

    python pressers_v2/slack_notify.py pressers_v2/.slack_payload.json

Reads SLACK_WEBHOOK_URL from the environment; skips quietly when it's unset.
Links point at this repo's GitHub Pages site, derived from GITHUB_REPOSITORY,
so no extra URL secret is needed. Never fails the workflow.
"""

import json
import os
import sys
from pathlib import Path

import requests


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


def post_no_new_videos(date_str: str) -> bool:
    return _post({"text": f"NBA Pressers v2 — {date_str}: No new press conferences this run."})


def post_digest(payload: dict) -> bool:
    items = payload.get("items") or []
    date_str = payload.get("date_str") or ""
    run_slot = payload.get("run_slot")
    aborted_count = int(payload.get("aborted_count") or 0)
    deferred_count = int(payload.get("deferred_count") or 0)
    one_off_count = int(payload.get("one_off_count") or 0)
    clip_count = int(payload.get("clip_count") or 0)
    if not items and not aborted_count and not deferred_count:
        return post_no_new_videos(date_str)

    lines = [f"*NBA Pressers v2 — {slack_escape(date_str)}*"]
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
    n = len(items)
    summary = f"Processed {n} video{'s' if n != 1 else ''}"
    if one_off_count:
        summary += f" ({n - one_off_count} from team channels, {one_off_count} one-off)"
    lines.append(summary + f". {clip_count} clip(s) in the 48h manifest.")
    for d in payload.get("digest_dates") or []:
        lines.append(digest_link(d, run_slot))
    lines.append(f"Clip manifest: {manifest_raw_url()}")

    # Same grouping and order as the digest; empty groups are skipped.
    groups = [("presser", "Press conferences"), ("podcast", "Podcasts & shows"), ("oneoff", "One-offs")]
    known = {g for g, _ in groups}
    ordered = []
    for ctype, label in groups:
        members = [it for it in items if (it.get("content_type") if it.get("content_type") in known
                                          else "presser") == ctype]
        if members:
            ordered.append((label, members))
    for label, members in ordered:
        lines.append("")
        lines.append(f"*{label}* ({len(members)})")
        for it in members:
            lines.extend(_item_lines(it))
    return _post({"text": "\n".join(lines)})


def _item_lines(it: dict) -> list:
    title = slack_escape(it.get("title") or it.get("video_id") or "")
    channel = slack_escape(it.get("channel") or "")
    speaker = slack_escape((it.get("speaker") or "").strip())
    quote = slack_escape(_truncate(it.get("top_quote") or ""))
    post = slack_escape(_truncate(it.get("social_post") or "", 280))
    lines = ["", f"🎙️ *{title}* ({channel}), {int(it.get('clip_count') or 0)} clip(s)"]
    if quote:
        lines.append(f'Top quote: "{quote}"' + (f" ({speaker})" if speaker else ""))
    if post:
        lines.append(f"Draft post: {post}")
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
        post_no_new_videos(payload.get("date_str") or "")
    elif kind == "digest":
        post_digest(payload)
    else:
        _log(f"unknown payload kind: {kind!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
