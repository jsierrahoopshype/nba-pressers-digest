#!/bin/bash
# NBA Presser Clips installer for macOS. Double-click it, or run it in
# Terminal. Installs Homebrew (if missing), ffmpeg, Python, the app, the
# "Clip it" link handler and a desktop launcher, then asks three setup
# questions. Safe to run again: it updates everything and asks again. No
# GitHub account or token needed.
set -u
APP="$HOME/Library/Application Support/NBA Presser Clips"
RAW="https://raw.githubusercontent.com/jsierrahoopshype/nba-pressers-digest/main/pressers_v2"
FILES="caption_align.py reframe.py make_presser_clips.py presser_clips_setup.py run-presser-clips-mac.command"
MODEL="models/face_detection_yunet_2023mar.onnx"   # face detection (YuNet, MIT licence)

fail() {
  echo
  echo "[X] $1"
  echo
  read -r -p "Press Return to close this window." _
  exit 1
}

find_brew() {
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -x "$b" ]; then eval "$("$b" shellenv)"; return 0; fi
  done
  command -v brew >/dev/null 2>&1
}

echo "============================================"
echo "  NBA Presser Clips - installer (macOS)"
echo "============================================"
echo

# 1. Homebrew
if ! find_brew; then
  echo "Installing Homebrew. It asks for your Mac password (nothing shows while you type)"
  echo "and may install Apple's command line tools first; that can take 10+ minutes."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
    || fail "Homebrew could not be installed."
  find_brew || fail "Homebrew was installed but can't be found. Close this window and run the installer again."
fi

# 2. ffmpeg and Python
for pkg in ffmpeg python@3.12; do
  if ! brew list --versions "$pkg" >/dev/null 2>&1; then
    echo "Installing $pkg (a few minutes the first time)..."
    brew install "$pkg" || fail "brew install $pkg failed. Run the installer again."
  fi
done
PY="$(brew --prefix python@3.12)/bin/python3.12"
[ -x "$PY" ] || fail "Python 3.12 was not found at $PY"

# 3. The app files
mkdir -p "$APP" || fail "Could not create $APP"
echo "Downloading the app into $APP ..."
for f in $FILES; do
  curl -fsSL --max-time 120 -o "$APP/$f.new" "$RAW/$f" \
    || fail "Download of $f failed. Check your internet connection and run this again."
  mv -f "$APP/$f.new" "$APP/$f"
done
mkdir -p "$APP/models"
curl -fsSL --max-time 120 -o "$APP/$MODEL.new" "$RAW/$MODEL" \
  || fail "Download of the face model failed. Check your internet connection and run this again."
mv -f "$APP/$MODEL.new" "$APP/$MODEL"
chmod 755 "$APP/run-presser-clips-mac.command"

# 4. A private Python environment: yt-dlp, deno, OpenCV, faster-whisper
VPY="$APP/venv/bin/python"
if [ ! -x "$VPY" ]; then
  "$PY" -m venv "$APP/venv" || fail "Could not create the Python environment."
fi
echo "Installing yt-dlp, deno, OpenCV and faster-whisper (the first time takes a few minutes)..."
"$VPY" -m pip install --upgrade --disable-pip-version-check --quiet pip
"$VPY" -m pip install --upgrade --disable-pip-version-check --quiet "yt-dlp[default]" certifi deno opencv-python-headless \
  || fail "pip could not install yt-dlp. Check your internet connection and run this again."
"$VPY" -m pip install --upgrade --disable-pip-version-check --quiet faster-whisper \
  || echo "NOTE: faster-whisper could not be installed. Videos without YouTube captions will be skipped."

# 5. Checks
"$VPY" "$APP/presser_clips_setup.py" --find-ffmpeg \
  || fail "ffmpeg is missing or has no subtitle support. Fix: open Terminal and run: brew reinstall ffmpeg"
if ! "$VPY" "$APP/presser_clips_setup.py" --find-deno >/dev/null; then
  brew install deno || echo "NOTE: deno could not be installed; some YouTube downloads may fail."
fi
echo

# 6. The three questions, Clip it links, desktop launcher
"$VPY" "$APP/presser_clips_setup.py" --setup || fail "Setup did not finish. Run the installer again."
echo
read -r -p "Press Return to close this window." _
