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

## Making clips (Windows and Mac)

Plain-English guide for anyone making clips: [CLIPS-GUIDE.md](CLIPS-GUIDE.md).

**Install** (no GitHub account or token): Windows
[install-presser-clips.bat](https://jsierrahoopshype.github.io/nba-pressers-digest/presser-clips/install-presser-clips.bat),
Mac [install-presser-clips-mac.zip](https://jsierrahoopshype.github.io/nba-pressers-digest/presser-clips/install-presser-clips-mac.zip)
(the .command inside). The installer sets up Python (winget / Homebrew),
ffmpeg, a private venv with yt-dlp, deno and faster-whisper, downloads the
app into `%LOCALAPPDATA%\NBA Presser Clips` or
`~/Library/Application Support/NBA Presser Clips`, and asks: save folder
(default `<home>/Documents/presser-clips`; shared Drive/OneDrive/Dropbox
folders work, as long as the folder is used only for clips), days to keep
files (Enter = 7, 0 = never), default formats (Enter = all three) and
automatic mode (Y/N, default N). Answers go to `settings.json` in the app folder. It also puts an
**NBA Presser Clips** shortcut on the desktop. Re-running it updates and asks
again. The Pages copies in `docs/presser-clips/` are rebuilt with
`python pressers_v2/tools/build_installers.py` (a test checks they match).

**On demand.** The shortcut refreshes the app files from `main`, then shows
the latest run's quotes grouped Press conferences / Podcasts & shows /
One-offs (number, score, formats already made, speaker, team, angle).
Enter = top 10, numbers like `1,3,5-7`, P / D / O, MORE (whole 48 hours), or
paste a timestamped YouTube link from the digest to clip that one quote.
Then it asks for formats: Enter = the defaults, or any of V / Y / S.

**Formats.** `vertical` 1080x1920, `youtube` 1920x1080, `square` 1080x1080,
each rendered from one download of the segment. The frame is never cropped
(fitted, blurred fill). Vertical and square put the lower third and the
captions in the bands outside the picture; YouTube puts a compact lower third
top-left and captions along the bottom edge.

**Cuts.** For each quote the clipper finds where it's really spoken (YouTube
captions via yt-dlp, else Whisper on the 90 seconds around it) and cuts with
0.5s padding. A low-confidence match is skipped, not cut.

**Files.** The clips folder holds finished clips only:
`<folder>/<video date>/pressers|podcasts|oneoffs/<date>_<team>_<speaker>_<videoid>-<start>s_<format>.mp4`.
Everything else lives in the per-user app folder (`%LOCALAPPDATA%\NBA Presser Clips`
or `~/Library/Application Support/NBA Presser Clips`): the post text in
`notes/<date>/<same base name>.txt`, logs in `logs/`, renders in `tmp/`.
Names depend only on the quote, so an existing file is never rendered again
(per format), also when several people share the folder. A finished render
moves into the clips folder in one rename (same drive) or via a `.partial`
copy that's renamed (another drive, e.g. Google Drive G:). Every move and
delete retries when Windows reports the file as busy (WinError 32/5). Notes
and logs left in the clips folder by earlier versions are moved to the app
folder on the next run.

**Cleanup.** Every run (shortcut or automatic) deletes files older than
`keep_days` (install question, default 7, 0 = never) from the clips folder,
`notes/` and `tmp/`, removes empty subfolders, and prints/logs what it freed
(`logs/cleanup-log.txt`). The installer refuses folders that obviously hold
other files (a drive root, home, Documents, Desktop, Downloads, a Drive /
OneDrive / Dropbox root). Without an installer `settings.json` nothing is
deleted.

**Automatic mode.** Task Scheduler (Windows) or launchd (Mac) starts
`presser_pc_job.py --only-new` every 30 minutes while you're logged in; it
cleans up, then renders the top 10 in the default formats only when the clip
list shows a new cloud run. Log: `logs/pc-job-log.txt` in the app folder.
Nothing is written to GitHub.

**Windows CI.** `pressers-v2-tests.yml` also runs on `windows-latest`: the
installer's folder check, a real ffmpeg render moved into `C:\` from the
app folder on `D:`, the cleanup, the setup questions with typed answers, and
the whole test suite.

**Older Windows tools.** `make-presser-clips.bat` and the earlier
`install-presser-pc-job.bat` job still work (the job keeps rendering vertical
clips into `Documents\presser-clips`); the new installer offers to remove
that older scheduled task because it replaces it.

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
