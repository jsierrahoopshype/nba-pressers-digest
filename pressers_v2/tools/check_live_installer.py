"""
Check that what people download is the version in this checkout:
  * the installer on GitHub Pages (presser-clips/install-presser-clips.bat
    and the Mac zip) is byte-identical to docs/presser-clips/
  * the app files the installer pulls from raw GitHub (main) are
    byte-identical to pressers_v2/

    python pressers_v2/tools/check_live_installer.py [--wait-secs N]

Pages and the raw CDN can lag a few minutes after a push; --wait-secs
re-checks until everything matches or time runs out.
"""

import argparse
import hashlib
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGES = "https://jsierrahoopshype.github.io/nba-pressers-digest/presser-clips"
RAW = "https://raw.githubusercontent.com/jsierrahoopshype/nba-pressers-digest/main/pressers_v2"
CHECKS = [(f"{PAGES}/install-presser-clips.bat", ROOT / "docs/presser-clips/install-presser-clips.bat"),
          (f"{PAGES}/install-presser-clips-mac.zip", ROOT / "docs/presser-clips/install-presser-clips-mac.zip")] + [
         (f"{RAW}/{name}", ROOT / "pressers_v2" / name)
         for name in ("presser_clips_setup.py", "make_presser_clips.py", "presser_pc_job.py",
                      "caption_align.py", "run-presser-clips.bat", "run-presser-clips-mac.command")]


def fetch(url: str) -> bytes:
    req = urllib.request.Request(f"{url}?nocache={int(time.time())}",
                                 headers={"User-Agent": "nba-presser-clips-check", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def check_once() -> bool:
    ok = True
    for url, local in CHECKS:
        want = local.read_bytes()
        try:
            got = fetch(url)
        except Exception as e:
            print(f"  FETCH FAILED {url}: {type(e).__name__}: {e}")
            ok = False
            continue
        same = got == want
        ok &= same
        print(f"  {'same' if same else 'DIFFERENT'}  live {sha(got)} / repo {sha(want)}  {url}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait-secs", type=int, default=0)
    args = ap.parse_args()
    deadline = time.time() + args.wait_secs
    while True:
        if check_once():
            print("PASS: the live installer and the files it downloads are this version")
            return 0
        if time.time() >= deadline:
            print("FAIL")
            return 1
        print("not yet; waiting 30s for Pages / raw GitHub to update...\n", flush=True)
        time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
