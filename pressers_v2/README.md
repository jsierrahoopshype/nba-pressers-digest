# Pressers v2: press-conference quotes + clips

Additive pipeline. The original `scripts/` folder and `daily-digest.yml` are untouched.

Three times a day (06:15, 14:15, 20:15 UTC) GitHub Actions polls the 30 NBA team
YouTube channels, keeps press conferences / postgame / pregame / media
availability videos published in the last 48h, sends each one to Gemini
(`gemini-3.1-flash-lite`) and publishes:

| Output | Where |
|---|---|
| Per-run digest (markdown, yt-quotes format) | `pressers_v2/output/<date>/digest-<slot>.md` |
| Aggregate digest for the day | `pressers_v2/output/<date>/digest.md` |
| Rolling 48h clip manifest | `pressers_v2/output/latest_clips.json` |
| Dated copy of the manifest | `pressers_v2/output/clips/<date>.json` |
| GitHub Pages mirror | `docs/pressers_v2/` → https://jsierrahoopshype.github.io/nba-pressers-digest/pressers_v2/ |
| Slack post | after each run, via `SLACK_WEBHOOK_URL` |

Each quote has: speaker, team, verbatim text, start/end seconds (15-60s clip),
a one-line news angle and a draft social post.

## One-time setup

1. **Secrets** (Settings → Secrets and variables → Actions → New repository secret),
   same names and values as hoopshype-yt-quotes:
   `GEMINI_API_KEY`, `YOUTUBE_API_KEY`, `SLACK_WEBHOOK_URL`.
   `OUTPUT_BASE_URL` is **not** needed here (links are built from this repo's Pages URL).
2. **GitHub Pages**: Settings → Pages → Source: *Deploy from a branch* →
   Branch `main`, folder `/docs` → Save.
3. **First run**: Actions → *Pressers v2 (quotes + clips)* → *Run workflow*.
   The `extra_videos` box takes YouTube URLs to force-process (skips all filters).

## Making the vertical clips on Windows

1. Download `make_presser_clips.py` and `make-presser-clips.bat` from this folder
   into the same folder on your PC (e.g. `Documents\presser-clips-tool\`).
2. Double-click `make-presser-clips.bat`.
   - It installs/updates yt-dlp itself.
   - If ffmpeg is missing it stops and tells you the fix:
     `winget install --id Gyan.FFmpeg -e`, then open a new window and run again.
3. Clips land in `C:\Users\Jorge Sierra\Documents\presser-clips\<YYYY-MM-DD>\` as
   `<date>_<team>_<speaker>_<n>.mp4` + `.txt` (social post + source URL).
   Already-made clips are skipped on later runs; failures are listed at the end.

Optional flags (add after the .bat name in a terminal):
`--team celtics`, `--limit 5`, `--force`, `--out D:\clips`.

If YouTube downloads fail with "Sign in to confirm" or JavaScript errors,
install Deno once: `winget install --id DenoLand.Deno -e`.

## Tuning

Edit `pressers_v2/config.json`: title keywords (include/exclude), 48h window,
minimum duration, per-run caps, channel list. No code changes needed.

## How it decides what's new

Same as yt-quotes: the disk is the database. A video is done once
`output/<date>/<video_id>.md` and `.json` exist, or a `.SKIPPED-too-long` /
`.FAILED.txt` marker exists. Gemini 503/500 after 5 retries defers the video to
the next run (no marker); a spending-cap 429 aborts the rest of the queue.
