"""
Verify every channel in pressers_v2/config.json resolves to an uploads
playlist, and suggest the right ID for any that don't.

    YOUTUBE_API_KEY=... python pressers_v2/check_channels.py

Cost: 1 unit for all channel IDs (one batched channels.list), plus 1 unit per
handle tried and 100 units per search, only for channels that fail. Exits 1
if any active channel is broken, so the PR check goes red.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from presser_extractor import (CONFIG_PATH, QuotaExceeded, list_recent_uploads,  # noqa: E402
                               parse_iso, title_filter, youtube_api_get)
from datetime import datetime, timedelta, timezone  # noqa: E402


def channel_rows(ids: list, key: str) -> dict:
    out = {}
    for i in range(0, len(ids), 50):
        data = youtube_api_get("channels", {"part": "snippet,contentDetails,statistics",
                                            "id": ",".join(ids[i:i + 50])}, key)
        for item in data.get("items", []):
            out[item["id"]] = item
    return out


def team_nickname(team: str) -> str:
    """"Portland Trail Blazers" -> "Blazers", "Philadelphia 76ers" -> "76ers"."""
    return team.split()[-1]


def describe(item: dict) -> str:
    uploads = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
    snip = item.get("snippet", {})
    subs = item.get("statistics", {}).get("subscriberCount", "?")
    return f"{item['id']}  title={snip.get('title')!r} handle={snip.get('customUrl')!r} subs={subs} uploads={uploads}"


def main() -> int:
    key = os.getenv("YOUTUBE_API_KEY")
    if not key:
        print("YOUTUBE_API_KEY not set")
        return 2
    config = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
    channels = [c for c in config.get("channels", []) if c.get("active", True)]
    try:
        # Channels listed by @handle only (no channel_id yet): resolve and print
        # the ID so it can be pasted into config.json.
        for c in channels:
            if not c.get("channel_id") and c.get("handle"):
                data = youtube_api_get("channels", {"part": "snippet,contentDetails,statistics",
                                                    "forHandle": c["handle"]}, key)
                items = data.get("items", [])
                if items:
                    c["channel_id"] = items[0]["id"]
                    print(f"[HANDLE] {c['team']:<24} {c['handle']} -> {describe(items[0])}")
                else:
                    print(f"[HANDLE] {c['team']:<24} {c['handle']} -> not found")
        channels = [c for c in channels if c.get("channel_id")]
        rows = channel_rows([c["channel_id"] for c in channels], key)
        broken = []
        seen_ids = {}
        for c in channels:
            item = rows.get(c["channel_id"])
            has_uploads = bool(item and item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads"))
            # An ID can exist and still be the wrong channel (e.g. another
            # team's), so the channel title must contain the team nickname.
            title = (item or {}).get("snippet", {}).get("title", "").lower()
            nickname = team_nickname(c["team"]).lower()
            status = "OK "
            if not has_uploads:
                status = "BAD"
            elif nickname not in title:
                status = "WRONG CHANNEL"
            elif c["channel_id"] in seen_ids:
                status = f"DUPLICATE of {seen_ids[c['channel_id']]}"
            seen_ids.setdefault(c["channel_id"], c["team"])
            print(f"[{status}] {c['team']:<24} " + (describe(item) if item else c["channel_id"] + "  (not found)"))
            if status != "OK ":
                broken.append(c)
        for c in broken:
            print(f"\n--- candidates for {c['team']} ---")
            seen = set()
            for handle in c.get("handle_candidates", []):
                data = youtube_api_get("channels", {"part": "snippet,contentDetails,statistics",
                                                    "forHandle": handle}, key)
                for item in data.get("items", []):
                    if item["id"] not in seen:
                        seen.add(item["id"])
                        print(f"  handle {handle}: {describe(item)}")
            data = youtube_api_get("search", {"part": "snippet", "type": "channel", "maxResults": 5,
                                              "q": f"{c['team']} NBA official"}, key)
            ids = [it["id"]["channelId"] for it in data.get("items", []) if it.get("id", {}).get("channelId")]
            for cid, item in channel_rows([i for i in ids if i not in seen], key).items():
                print(f"  search: {describe(item)}")
    except QuotaExceeded as e:
        print(f"YouTube quota exceeded: {e}")
        return 2
    print(f"\n{len(channels) - len(broken)}/{len(channels)} channels resolve to an uploads playlist.")
    try:
        title_audit(channels, config, key, rows)
        find_presser_sources(key)
    except QuotaExceeded as e:
        print(f"YouTube quota exceeded during title audit: {e}")
    return 1 if broken else 0


def title_audit(channels: list, config: dict, key: str, rows: dict) -> None:
    """Print each team channel's last 7 days of uploads and whether the
    presser title filter keeps them (+ kept, - dropped), so a team that
    never shows up in the digest can be diagnosed. About 1 unit per channel.
    Informational only: never fails the check."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    for c in channels:
        if c.get("all_uploads"):
            continue
        item = rows.get(c["channel_id"]) or {}
        uploads_id = item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
        if not uploads_id:
            continue
        ups = [u for u in list_recent_uploads(uploads_id, key, 50)
               if (parse_iso(u["published"]) or cutoff) >= cutoff]
        kept = sum(1 for u in ups if not title_filter(u["title"], config))
        print(f"\n=== {c['team']} ({c['channel_id']}): {kept}/{len(ups)} uploads in the last 7 days pass the title filter ===")
        for u in ups:
            reason = title_filter(u["title"], config)
            print(f"  {'+' if not reason else '-'} {u['published'][:10]} {u['video_id']} {u['title']}"
                  + (f"   [{reason}]" if reason else ""))


def find_presser_sources(key: str) -> None:
    """TEMPORARY (Nov 16 2026): where do these teams post postgame pressers?"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for team in ["Boston Celtics", "Brooklyn Nets", "Chicago Bulls", "Dallas Mavericks", "Golden State Warriors",
                 "Houston Rockets", "Los Angeles Lakers", "Miami Heat", "New York Knicks", "Sacramento Kings"]:
        data = youtube_api_get("search", {"part": "snippet", "type": "video", "maxResults": 15,
                                          "order": "date", "publishedAfter": cutoff,
                                          "q": f"{team} postgame press conference"}, key)
        print(f"\n### search: {team}")
        for it in data.get("items", []):
            sn = it.get("snippet", {})
            print(f"  {sn.get('publishedAt','')[:10]} [{sn.get('channelTitle')} {sn.get('channelId')}] {sn.get('title')}")


if __name__ == "__main__":
    sys.exit(main())
