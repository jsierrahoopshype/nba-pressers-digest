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
ffmpeg, a private venv (yt-dlp, deno, OpenCV, faster-whisper), downloads the
app into `%LOCALAPPDATA%\NBA Presser Clips` or
`~/Library/Application Support/NBA Presser Clips`, and asks three questions:
clips folder (default `<home>/Documents/presser-clips`; a shared Drive /
OneDrive / Dropbox folder works if it's used only for clips), days to keep
clips (Enter = 7, 0 = never) and default formats (Enter = all three).
Answers go to `settings.json` in the app folder. It registers the "Clip it"
link type, puts an **NBA Presser Clips** shortcut on the desktop and switches
off the automatic mode of earlier versions. Re-running it updates and asks
again. The Pages copies in `docs/presser-clips/` are rebuilt with
`python pressers_v2/tools/build_installers.py` (a test checks they match).

**Clip it.** Every quote block in the digest (Pages) ends with a small
`Clip it` link: `presserclips://clip?v=<video id>&t=<start second>&q=<quote number>`.
The installer registers that link type for the current user only: Windows
under `HKCU\Software\Classes\presserclips` (no admin), running
`<venv>\python.exe make_presser_clips.py --link "%1"` in a console window;
macOS as `~/Applications/NBA Presser Clips.app` (an AppleScript app whose
`CFBundleURLTypes` declares the scheme) that opens Terminal with the link as
one shell-quoted argument. The tool accepts nothing but `--link <one link>`
in that mode and checks the link strictly: exactly `v`, `t`, `q`; an 11-char
`[A-Za-z0-9_-]` video id; whole seconds; a quote number that exists for that
video at that second (48-hour clip list, then the stored per-video data).
Anything else is refused and logged. A good link renders that quote in the
default formats, shows progress and closes the window.

**Desktop shortcut** (fallback): the pick-list of the latest run's quotes,
grouped Press conferences / Podcasts & shows / One-offs; Enter = top 10,
numbers, P / D / O, MORE (48 hours) or a pasted timestamped YouTube link; then
formats (Enter = defaults, or V / Y / S).

**Formats.** `vertical` 1080x1920 and `square` 1080x1080 are crops of the
source that follow the speaker (`reframe.py`): OpenCV's YuNet face detector
(`models/`, MIT) samples ~6 frames/s, the biggest face near the previous
position is the speaker, gaps hold the last position (centre if no face),
and a "lazy camera" pans only when the face leaves a dead zone, at a capped
speed with eased motion, cutting rather than panning when the source cuts
to another shot. The per-frame crop goes to ffmpeg through `sendcmd`.
`youtube` 1920x1080 is the full frame. Best source up to 1080p, one download
per quote for all formats. No bands, no lower third.

**Subtitles** show 2-4 words at a time exactly when they're spoken: the
quote's own words timed by YouTube's speech-recognition captions (json3 /
WebVTT word timings), or by faster-whisper when the captions only time whole
lines. No word-level timing: no subtitles.

**Cuts.** For each quote the clipper finds where it's really spoken (captions
via yt-dlp, else Whisper on the 90 seconds around it) and cuts with 0.5s
padding. A low-confidence match is skipped, not cut. Captions: `yt-dlp -J`
lists the tracks once, then ONE English track at a time is downloaded from
that saved info (speech-recognition `en-orig`, then `en`, then manual
English), retrying when YouTube answers 429; a failure isn't cached, and
whenever captions aren't used the exact reason is shown and logged
(`logs/clips-log.txt`). Whisper runs on pinned `faster-whisper==1.2.1` +
`av==18.1.0` (PyAV 19 removed an argument faster-whisper still passes);
the installer installs them and every update brings existing installs to
those versions.

**Files.** One folder per quote in the clips folder:
`<YYYY-MM-DD> <Speaker> - <short angle>/` holding `vertical.mp4`,
`youtube.mp4`, `square.mp4` (the chosen formats) and `quote.txt` (speaker,
team, the quote, source link with timestamp, draft post). Nothing else goes
in the clips folder: logs (`logs/`) and renders (`tmp/`) stay in the app
folder. A format that already exists in the quote's folder is never
rendered again, also when several people share the folder. A finished
render moves into place in one rename (same drive) or via a `.partial` copy
that's renamed (another drive, e.g. Google Drive G:); every move and delete
retries when Windows reports the file as busy (WinError 32/5).

**Cleanup.** Every run deletes quote folders whose newest file is older than
`keep_days` (whole folders), plus old leftovers of earlier layouts and old
files in the app's `tmp/`, and prints/logs what it freed
(`logs/cleanup-log.txt`). The installer refuses folders that obviously hold
other files (a drive root, home, Documents, Desktop, Downloads, a Drive /
OneDrive / Dropbox root). Without an installer `settings.json` nothing is
deleted.

**Automatic mode** is retired. Installs that had it get it removed: the
scheduled task's next run (`presser_clips_setup.py --auto`, or the older
`presser_pc_job.py` job) unregisters itself, and so do the next update and
the installer.

**CI.** `pressers-v2-tests.yml` runs the tests on Linux, on `windows-latest`
(folder check, a real ffmpeg render with the face crop moved into `C:\`
from the app folder on `D:`, the cleanup, the setup questions, the
`presserclips:` registration and the registered command refusing bad or
tampered links) and on `macos-latest` (the link app built, signed and
declaring the scheme).

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
