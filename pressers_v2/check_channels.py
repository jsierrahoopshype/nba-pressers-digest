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
from presser_extractor import CONFIG_PATH, QuotaExceeded, youtube_api_get  # noqa: E402


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
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
