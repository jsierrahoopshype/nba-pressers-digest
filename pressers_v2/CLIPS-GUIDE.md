# NBA Presser Clips: quick guide

This tool turns quotes from the NBA pressers digest into ready-to-post video clips. Click **Clip it** under a quote in the digest and the clip is made on your computer: vertical, YouTube and square versions, cropped to follow the speaker, with subtitles that appear as the words are spoken. It works on Windows and Mac. You don't need a GitHub account.

## 1. Install (once)

**Windows**
1. Download [install-presser-clips.bat](https://jsierrahoopshype.github.io/nba-pressers-digest/presser-clips/install-presser-clips.bat). If the browser warns about the file, choose **Keep**.
2. Double-click it. If you see "Windows protected your PC", click **More info**, then **Run anyway**.
3. Wait while it installs what it needs (5 to 10 minutes the first time), then answer three questions (below).

**Mac**
1. Download [install-presser-clips-mac.zip](https://jsierrahoopshype.github.io/nba-pressers-digest/presser-clips/install-presser-clips-mac.zip) and double-click it to unzip.
2. Double-click **install-presser-clips-mac.command**. If the Mac says it can't verify the file, open **System Settings > Privacy & Security**, scroll down and click **Open Anyway**.
   (Or open **Terminal** and paste this line, then press Return:
   `bash -c "$(curl -fsSL https://raw.githubusercontent.com/jsierrahoopshype/nba-pressers-digest/main/pressers_v2/install-presser-clips-mac.command)"`)
3. It may ask for your Mac password (nothing shows while you type). The first install can take 10 to 20 minutes.

**The three questions**
- **Where to save clips.** Press Enter for `Documents/presser-clips` in your user folder, or paste another folder. A shared Google Drive, OneDrive or Dropbox folder works, so the whole team sees the same clips. **Use a folder only for these clips**: the tool deletes anything in it older than the limit below, no matter who put it there.
- **Delete clips older than how many days?** Press Enter for 7, or type a number (`0` = never delete).
- **Default formats.** Press Enter for all three, or type letters such as `VS`. Clip it links always make your default formats.

To change any answer later, run the installer again. If you had automatic mode from an earlier version, it is switched off now: clips are only made when you ask.

## 2. Make a clip: click "Clip it"

Open the digest. Under every quote, after the timestamped YouTube link, there is a small **Clip it** link. Click it.

- The first time, the browser asks whether to open **NBA Presser Clips**. Tick "Always allow" (if offered) and click **Open**. On a Mac, the first click may also ask to let NBA Presser Clips control Terminal: click **OK**.
- A window opens and shows the progress: finding the quote in the video, downloading that part, making each format. Nothing to answer. It closes by itself when it's done (after a minute when something went wrong, so you can read why).
- Each clip takes a minute or two. If the clip already exists in the folder (also when a teammate made it), nothing is made again.

**From a list instead:** double-click **NBA Presser Clips** on your desktop. It lists the latest digest's quotes with a number, news score, speaker, team and angle. Press Enter for the top 10, type numbers like `1,3,5-7`, P / D / O for the top press conferences, podcasts or one-offs, MORE for the last 48 hours, or paste a timestamped YouTube link from the digest. Then choose formats.

## 3. Which format for which platform

| Letter | Format | Size | Use it for |
|---|---|---|---|
| V | Vertical 9:16 | 1080x1920 | Instagram Reels, TikTok, YouTube Shorts |
| Y | YouTube 16:9 | 1920x1080 | X, YouTube, Facebook |
| S | Square 1:1 | 1080x1080 | Instagram and Facebook feed posts |

Vertical and square are cropped from the video and follow the speaker's face; YouTube shows the full picture. There is no name banner on screen: add the speaker's name in your post.

## 4. Where clips land

Each quote gets its own folder in your clips folder, named with the date, the speaker and a short angle, for example `2026-09-30 James Harden - on Mario and Peyton's camp`. Inside:
- `vertical.mp4`, `youtube.mp4`, `square.mp4` (the formats you chose)
- `quote.txt`: the speaker, team, the exact quote, the YouTube link at the right second, and a draft social post

Clips appear only once they're complete. Nothing else goes in the clips folder.

## 5. Automatic cleanup

Every time the tool runs, it deletes quote folders older than your limit (7 days unless you chose otherwise), each folder as a whole. It shows what it deleted and how much space it freed. Save any clip you want to keep somewhere else before it ages out.

On Google Drive, deleted clips go to the Drive trash, which keeps them for up to 30 days and they still count against your storage until then. To free the space sooner, empty it at https://drive.google.com/drive/trash.

## 6. Check before you post

- **Speaker.** Make sure the person on screen is the one in `quote.txt` and the name in your post is spelled correctly. Names come from the video and the AI can get them wrong.
- **Quote text.** Watch with sound on and compare the subtitles with what is said. They use the digest's wording, timed to the speech, which can differ slightly from the audio. Some clips have no subtitles: that happens when the video has no word-by-word timing.
- **Framing.** In vertical and square, check that the speaker stays in the picture, especially in videos with several people.
- **Start and end.** Watch the whole clip. It should start at the beginning of the thought and not cut off the last words.
- **Draft post.** The post in `quote.txt` is a starting point. Edit it to your voice and check every fact.

## If something goes wrong

- **Clip it does nothing** or the browser can't open it: run the installer again (it registers the link), then reload the digest.
- **Skipped (low confidence)**: the quote couldn't be found precisely in the video, so it wasn't cut. Pick a different quote.
- **Failed** or "YouTube refused the download": run the installer again (it repairs the setup), then try again.
- On a Mac, if it asks to access Documents, Desktop or a cloud folder, click **Allow**.
