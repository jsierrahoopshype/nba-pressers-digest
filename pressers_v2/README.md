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

**Same extraction and format as hoopshype-yt-quotes.** Quotes are extracted
by yt-quotes' own code, vendored byte for byte in `ytq_vendor.py` (same
prompt, Gemini config, MM:SS timestamps, chunking, quote splitter and
markdown renderer). The only change is one appended prompt rule: a speaker
is named only if the video or its title identifies them; otherwise
"Unidentified speaker". To pick up a newer yt-quotes version, run
`python pressers_v2/tools/vendor_ytq.py <path to hoopshype-yt-quotes>`.

**Digest.** Titled "NBA Pressers — <date>", split into **Press conferences**,
**Podcasts & shows** and **One-offs** (empty sections skipped). Inside each
section every video and quote block is exactly yt-quotes' output.

**Content types.** `presser` (press conferences, availabilities, media day,
pre/postgame, shootaround), `podcast` (podcasts, shows, livestreams, reaction
shows) or `oneoff` (anything passed in `extra_videos`, whatever its content).
Title keywords in `config.json` (`content_type_keywords`, podcast checked
first) decide; when none matches, the clip-field call's `content_type` is used.
The same three groups drive the digest, the Slack post, the clip folders and
the clipper's pick-list.

**Clip fields.** One text-only Gemini call per video (no video input) reads
the extracted quotes and returns speaker_confidence, news_score (1-10), a
draft social post and the video's content_type. A clip's end time is its
start + the speech duration estimated from the word count (2.6 words/s).
`latest_clips.json` keeps its original fields (plus a few added ones) and
only lists quotes whose speaker is named.

## One-time setup

1. **Secrets** (Settings → Secrets and variables → Actions → New repository secret),
   same names and values as hoopshype-yt-quotes:
   `GEMINI_API_KEY`, `YOUTUBE_API_KEY`, `SLACK_WEBHOOK_URL`.
   `OUTPUT_BASE_URL` is **not** needed here (links are built from this repo's Pages URL).
2. **GitHub Pages**: Settings → Pages → Source: *Deploy from a branch* →
   Branch `main`, folder `/docs` → Save.
3. **First run**: Actions → *Pressers v2 (quotes + clips)* → *Run workflow*.
   The `extra_videos` box takes YouTube URLs to process regardless of title,
   age or length. Videos already processed are skipped unless you tick
   `force`, which reprocesses them and overwrites their output.

## Making the vertical clips on Windows

1. Download **three** files from this folder into the same folder on your PC
   (e.g. `Documents\presser-clips-tool\`): `make_presser_clips.py`,
   `caption_align.py` and `make-presser-clips.bat`.
2. Double-click `make-presser-clips.bat`.
   - It installs/updates yt-dlp and faster-whisper itself. The first time a
     video has no captions, Whisper downloads its model once (~140 MB).
   - If ffmpeg is missing it stops and tells you the fix:
     `winget install --id Gyan.FFmpeg -e`, then open a new window and run again.
3. It prints a numbered list grouped into PRESS CONFERENCES, PODCASTS & SHOWS
   and ONE-OFFS (number, news score, speaker, team, angle); the latest run's
   clips are marked NEW. Press **Enter** for the top 10 overall by news score
   from the latest run, **P** / **D** / **O** for the top 10 pressers /
   podcasts / one-offs, **ALL** for everything, or numbers like `1,3,5-7`.
   `make-presser-clips.bat P` skips the question and does pressers only.
4. For each clip it finds where the quote is really spoken: first from the
   video's YouTube captions, otherwise with Whisper on the 90 seconds around
   the given time. It cuts on those words with 0.5s padding. If it can't find
   the quote confidently it does **not** cut; the clip is listed at the end
   under "Skipped because the quote couldn't be located reliably".
5. Clips land in `C:\Users\Jorge Sierra\Documents\presser-clips\<YYYY-MM-DD>\pressers\`
   (or `\podcasts\`, `\oneoffs\`) as
   `<date>_<team>_<speaker>_<n>.mp4` + `.txt` (social post + source URL).
   Already-made clips are skipped on later runs; failures are listed at the end.

Optional flags (add after the .bat name in a terminal):
`--all`, `--pick 1,3,5-7`, `--top 15`, `--yes` (no question, default pick),
`--team celtics`, `--limit 5`, `--force`, `--out D:\clips`.

If YouTube downloads fail with "Sign in to confirm" or JavaScript errors,
install Deno once: `winget install --id DenoLand.Deno -e`.

## Unattended PC job (optional)

Renders clips on your PC by itself, 30 minutes after each cloud run (06:45,
14:45, 20:45 UTC), only while the PC is on and you're logged in, with no
window: the top 10 clips by news score go to
`Documents\presser-clips\<date>\pressers|podcasts|oneoffs\`, each cut
aligned on the video's captions locally (Whisper as fallback). It only reads
the public clip list; it writes nothing to GitHub and needs no token. Log:
`Documents\presser-clips\pc-job-log.txt`.

Install: download `install-presser-pc-job.bat` from this folder and
double-click it. Each run downloads the latest clipper files from `main`
first. To remove it: `schtasks /delete /tn "NBA Pressers PC Job" /f`.

## Tuning

Edit `pressers_v2/config.json`: title keywords (include/exclude), 48h window,
minimum duration, `max_duration_secs` (default 2700 = 45 min; longer videos
are skipped to save Gemini cost, except URLs passed in `extra_videos`),
per-run caps, channel list. No code changes needed. A PR that changes this
file runs `check_channels.py`, which fails if any channel ID doesn't resolve
to the right team's uploads.

## How it decides what's new

Same as yt-quotes: the disk is the database. A video is done once
`output/<date>/<video_id>.md` and `.json` exist, or a `.SKIPPED-too-long` /
`.FAILED.txt` marker exists. Gemini 503/500 after 5 retries defers the video to
the next run (no marker); a spending-cap 429 aborts the rest of the queue.
