# NBA Presser Clips: quick guide

This tool turns quotes from the NBA pressers digest into ready-to-post video clips with the speaker's name and captions burned in. It works on Windows and Mac. You don't need a GitHub account.

## 1. Install (once)

**Windows**
1. Download [install-presser-clips.bat](https://jsierrahoopshype.github.io/nba-pressers-digest/presser-clips/install-presser-clips.bat). If the browser warns about the file, choose **Keep**.
2. Double-click it. If you see "Windows protected your PC", click **More info**, then **Run anyway**.
3. Wait while it installs what it needs (5 to 10 minutes the first time), then answer four questions (below).

**Mac**
1. Download [install-presser-clips-mac.zip](https://jsierrahoopshype.github.io/nba-pressers-digest/presser-clips/install-presser-clips-mac.zip) and double-click it to unzip.
2. Double-click **install-presser-clips-mac.command**. If the Mac says it can't verify the file, open **System Settings > Privacy & Security**, scroll down and click **Open Anyway**.
   (Or open **Terminal** and paste this line, then press Return:
   `bash -c "$(curl -fsSL https://raw.githubusercontent.com/jsierrahoopshype/nba-pressers-digest/main/pressers_v2/install-presser-clips-mac.command)"`)
3. It may ask for your Mac password (nothing shows while you type). The first install can take 10 to 20 minutes.

**The four questions**
- **Where to save clips.** Press Enter for `Documents/presser-clips` in your user folder, or paste another folder. A shared Google Drive, OneDrive or Dropbox folder works, so the whole team sees the same clips. **Use a folder only for these clips**: the tool deletes anything in it older than the limit below, no matter who put it there.
- **Delete files older than how many days?** Press Enter for 7, or type a number (`0` = never delete).
- **Default formats.** Press Enter for all three, or type letters such as `VS`.
- **Automatic mode.** `Y` makes the top 10 clips by itself after each new digest while your computer is on. `N` (the default) means you make clips only when you ask.

To change any answer later, run the installer again.

## 2. Make clips

Double-click **NBA Presser Clips** on your desktop. A window shows the quotes from the latest digest, grouped into Press conferences, Podcasts & shows and One-offs. Each line has a number, a news score (higher is bigger news), the speaker, the team and the angle. The "made" column shows which formats already exist.

Then type one of these and press Enter:
- **Enter** on its own: the top 10 by news score
- **Numbers**: just those quotes, for example `1,3,5-7`
- **P**, **D** or **O**: the top 10 press conferences, podcasts or one-offs
- **MORE**: show every quote from the last 48 hours
- **A link from the digest**: paste the timestamped YouTube link under any quote (it ends in `&t=...s`) to clip exactly that quote

Next it asks for formats. Press Enter for your defaults, or type any of `V`, `Y`, `S`. Each clip takes a minute or two. Clips that already exist are skipped, even if someone else made them in a shared folder.

## 3. Which format for which platform

| Letter | Format | Size | Use it for |
|---|---|---|---|
| V | Vertical 9:16 | 1080x1920 | Instagram Reels, TikTok, YouTube Shorts |
| Y | YouTube 16:9 | 1920x1080 | X, YouTube, Facebook |
| S | Square 1:1 | 1080x1080 | Instagram and Facebook feed posts |

## 4. Where clips land

The clips folder holds only finished clips: `<date of the video>/pressers` (or `podcasts`, `oneoffs`). File names show the date, team, speaker and format, for example `2026-09-30_cleveland-cavaliers_james-harden_J5unk3vEQmk-118s_vertical.mp4`. Clips appear only once they're complete.

The draft social post, source link and full quote for each clip are on your own computer, in a `.txt` file with the same name (without the format) in the `notes` folder:
- Windows: `%LOCALAPPDATA%\NBA Presser Clips\notes\<date>\` (paste that into the File Explorer address bar)
- Mac: `~/Library/Application Support/NBA Presser Clips/notes/<date>/` (in Finder: Go > Go to Folder)

## Automatic cleanup

Every time the tool runs (from the shortcut or in automatic mode), it deletes files older than your limit (7 days unless you chose otherwise) from the clips folder and its subfolders, then removes empty folders. Old notes and temporary files on your computer go too. It shows what it deleted and how much space it freed. Save any clip you want to keep somewhere else before it ages out.

On Google Drive, deleted clips go to the Drive trash, which keeps them for up to 30 days and they still count against your storage until then. To free the space sooner, empty it at https://drive.google.com/drive/trash.

## 5. Check before you post

- **Speaker name.** Make sure the name on screen is the person talking and is spelled correctly. Names come from the video and the AI can get them wrong.
- **Quote text.** Watch with sound on and compare the captions with what is said. The captions use the digest's text, which can differ slightly from the audio.
- **Start and end.** Watch the whole clip. It should start at the beginning of the thought and not cut off the last words.
- **Draft post.** The post in the `.txt` file is a starting point. Edit it to your voice and check every fact.

## If something goes wrong

- A quote listed as **skipped (low confidence)** couldn't be found precisely in the video, so it wasn't cut. Pick a different quote or paste its link later.
- **Failed** clips or "YouTube refused the download": run the installer again (it repairs the setup), then try again.
- On a Mac, if it asks to access Documents, Desktop or a cloud folder, click **Allow**.
