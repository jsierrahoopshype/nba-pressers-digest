# NBA Presser Clips: quick guide

This tool turns quotes from the NBA pressers digest into ready-to-post video clips with the speaker's name and captions burned in. It works on Windows and Mac. You don't need a GitHub account.

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
- **Where to save clips.** Press Enter for `Documents/presser-clips` in your user folder, or paste another folder. A shared Google Drive, OneDrive or Dropbox folder works, so the whole team sees the same clips.
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

Inside your clips folder: `<date of the video>/pressers` (or `podcasts`, `oneoffs`). File names show the date, team, speaker and format, for example `2026-09-30_cleveland-cavaliers_james-harden_J5unk3vEQmk-118s_vertical.mp4`. Next to each set of clips there is one `.txt` file with a draft social post, the source link and the full quote.

## 5. Check before you post

- **Speaker name.** Make sure the name on screen is the person talking and is spelled correctly. Names come from the video and the AI can get them wrong.
- **Quote text.** Watch with sound on and compare the captions with what is said. The captions use the digest's text, which can differ slightly from the audio.
- **Start and end.** Watch the whole clip. It should start at the beginning of the thought and not cut off the last words.
- **Draft post.** The post in the `.txt` file is a starting point. Edit it to your voice and check every fact.

## If something goes wrong

- A quote listed as **skipped (low confidence)** couldn't be found precisely in the video, so it wasn't cut. Pick a different quote or paste its link later.
- **Failed** clips or "YouTube refused the download": run the installer again (it repairs the setup), then try again.
- On a Mac, if it asks to access Documents, Desktop or a cloud folder, click **Allow**.
