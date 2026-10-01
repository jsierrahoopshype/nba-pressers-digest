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

**Content types.** Every video and clip has a `content_type`: `presser`
(press conferences, availabilities, media day, pre/postgame, shootaround),
`podcast` (podcasts, shows, livestreams, reaction shows) or `oneoff` (anything
passed in `extra_videos`). Title keywords in `config.json`
(`content_type_keywords`, podcast checked first) decide; when no keyword
matches, Gemini's own `content_type` is used. The digest and Slack post are
split into Press conferences, Podcasts & shows and One-offs.

Each quote has: speaker, speaker_confidence, team, verbatim text, a pull
quote, names mentioned, start/end seconds, a news_score (1-10), a one-line
news angle and a draft social post. Multi-speaker exchanges also carry
text_blocks.

**Timestamps.** Gemini's times are approximate. After extraction the pipeline
tries to fetch the video's YouTube captions and fuzzy-match each quote to
them (`timestamp_source: "captions"`). YouTube blocks GitHub's servers
(`RequestBlocked`, logged per video as `[captions] <id>: ...`), so in practice
the next step does the work: a **refinement pass** sends Gemini only a
2-minute window of the video around each first-pass time (then a 6-minute
window if the quote isn't there) and asks where the quote's first words are
spoken. An answer inside the window becomes `"gemini-refined"`; otherwise the
first-pass time stays (`"gemini-approx"`) and the digest shows `(approx.)`.
Refinement runs after all extraction and stops when the run's 35-minute
budget is nearly used, so it never costs a video its quotes. The Windows clipper always re-finds the
quote itself before cutting (see below), so clips are cut on the real words
either way.

Speakers are only named when the video itself identifies them (name graphic,
introduction, addressed by name, or the title naming the podium speaker,
e.g. "James Harden Media Availability"). Anything else is "Unidentified speaker" with
speaker_confidence "inferred", and those quotes are left out of
latest_clips.json so a guessed name never ends up in a clip's lower third.

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

YouTube blocks GitHub's servers from reading captions, but not your PC. The PC
job runs on its own 30 minutes after each cloud run (06:45, 14:45, 20:45 UTC,
only while the PC is on and you're logged in, no window), and:

1. fetches captions with yt-dlp for every quote that isn't caption-aligned and
   finds its exact start/end (`timestamp_source: "captions-pc"`);
2. commits the corrected times to this repo through the GitHub API (the
   video's .json/.md, the day digests, `latest_clips.json`, and the `docs/`
   mirror). A file that changed on GitHub meanwhile is re-read and retried
   once, then skipped and logged. The cloud never overwrites a `captions-pc`
   time;
3. writes `output/pc_alignment_report.json`: Gemini time vs real time per
   quote, plus median / p90 difference per cloud run;
4. renders the top 10 clips by news score into
   `Documents\presser-clips\<date>\pressers|podcasts|oneoffs\`;
5. logs to `Documents\presser-clips\pc-job-log.txt`.

Install: download `install-presser-pc-job.bat` from this folder, double-click
it, paste a fine-grained GitHub token when asked (repository: only
nba-pressers-digest; permissions: Contents read & write, Actions read). The
token is stored in `%USERPROFILE%\.nba-pressers\token`, never in the repo.
Each run downloads the latest job/clipper files from `main` first. To remove
it: `schtasks /delete /tn "NBA Pressers PC Job" /f`.

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
