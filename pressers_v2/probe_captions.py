"""
Diagnose the cloud caption fetch: try youtube-transcript-api for a few videos
from wherever this runs (e.g. a GitHub Actions runner) and print one line per
video with the exact outcome. No API keys, no Gemini, nothing written.

    python pressers_v2/probe_captions.py [VIDEO_ID ...]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import presser_extractor as pe  # noqa: E402

# Recent processed pressers (Harden, Maxey, Caleb Wilson, Cavs podcast)
DEFAULT_IDS = ["J5unk3vEQmk", "_eFAZb37nTI", "auzyc8y-uNI", "p7o-S3hmoD4"]


def main() -> int:
    ids = [a for a in sys.argv[1:] if pe.VIDEO_ID_RE.match(a)] or DEFAULT_IDS
    for vid in ids:
        pe._captions_blocked.clear()   # probe every video, don't short-circuit
        words, status = pe.fetch_caption_words(vid)
        print(f"{vid}: {status}" + (f" ({len(words)} words)" if words else ""), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
