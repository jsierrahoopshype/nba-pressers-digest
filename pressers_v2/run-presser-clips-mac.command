#!/bin/bash
# NBA Presser Clips launcher (macOS). The installer copies it to your Desktop
# as "NBA Presser Clips.command": double-click it to make clips. Refreshes the
# app from GitHub, then opens the pick-list.
APP="$HOME/Library/Application Support/NBA Presser Clips"
VPY="$APP/venv/bin/python"
if [ ! -x "$VPY" ]; then
  echo "NBA Presser Clips is not installed correctly. Run install-presser-clips-mac.command again."
  read -r -p "Press Return to close this window." _
  exit 1
fi
"$VPY" "$APP/presser_clips_setup.py" --update
"$VPY" "$APP/make_presser_clips.py" "$@"
echo
read -r -p "Press Return to close this window." _
