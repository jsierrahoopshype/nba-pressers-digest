"""
Check that a published pressers digest page on GitHub Pages shows its
YouTube URLs as clickable links (<a> anchors), not plain text.

    python pressers_v2/tools/check_pages_links.py URL [--wait-secs N]

Passes when the page has a "Source:" anchor, at least one timestamped URL
anchor whose visible text is the URL itself, and no YouTube URL left as
plain text in a paragraph. With --wait-secs it re-fetches until the check
passes or time runs out (Pages takes a few minutes to deploy after a push).
"""

import argparse
import html
import re
import sys
import time
import urllib.request

ANCHOR_RE = re.compile(r'<a href="(https://www\.youtube\.com/watch\?v=[^"]+)"[^>]*>([^<]*)</a>')
BARE_RE = re.compile(r"(?m)(?:<p>|^)(?:Source: )?https://www\.youtube\.com/watch\?v=\S+?(?:</p>|$)")


def check(page: str) -> tuple[bool, list]:
    anchors = [(html.unescape(h), html.unescape(t)) for h, t in ANCHOR_RE.findall(page)]
    url_text = [h for h, t in anchors if h == t]
    source = re.search(r'Source: <a href="https://www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{11}"', page)
    timestamped = [h for h in url_text if "&t=" in h]
    bare = BARE_RE.findall(page)
    report = [f"anchors to YouTube: {len(anchors)}",
              f"  whose visible text is the URL itself: {len(url_text)}",
              f"  of those, timestamped (&t=): {len(timestamped)}",
              f"'Source:' line is a link: {bool(source)}",
              f"YouTube URLs left as plain text: {len(bare)}"]
    report += [f"  sample anchor: {h}" for h in url_text[:3]]
    report += [f"  plain text: {b[:120]}" for b in bare[:3]]
    return bool(source and timestamped and not bare), report


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "nba-pressers-pages-check",
                                               "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--wait-secs", type=int, default=0)
    args = ap.parse_args()
    deadline = time.time() + args.wait_secs
    while True:
        try:
            ok, report = check(fetch(f"{args.url}?nocache={int(time.time())}"))
        except Exception as e:
            ok, report = False, [f"fetch failed: {type(e).__name__}: {e}"]
        print("\n".join(report), flush=True)
        if ok:
            print("PASS: the YouTube URLs on the page are <a> anchors")
            return 0
        if time.time() >= deadline:
            print("FAIL")
            return 1
        print("not yet; waiting 30s for GitHub Pages to deploy...\n", flush=True)
        time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
