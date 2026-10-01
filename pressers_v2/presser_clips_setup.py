"""
Setup, self-update and automatic mode for NBA Presser Clips (Windows, macOS).

The installers (install-presser-clips.bat, install-presser-clips-mac.command)
put Python, ffmpeg and a private Python environment in place, download the
app files into the app folder, then call this script:

    python presser_clips_setup.py --setup         the four questions, settings.json,
                                                  desktop shortcut, automatic mode on/off
    python presser_clips_setup.py --update        refresh the app's .py files from GitHub
    python presser_clips_setup.py --auto          update, then make clips if a new cloud run
                                                  is out (what automatic mode runs)
    python presser_clips_setup.py --find-ffmpeg   exit 0 = found with subtitles support,
                                                  1 = not found, 3 = found without subtitles
    python presser_clips_setup.py --find-deno     exit 0 = found

App folder: %LOCALAPPDATA%\\NBA Presser Clips (Windows) or
~/Library/Application Support/NBA Presser Clips (macOS). Nothing here needs
a GitHub account or token: every download is a public file.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import make_presser_clips as mc  # noqa: E402

APP_NAME = "NBA Presser Clips"
UPDATE_FILES = ["make_presser_clips.py", "caption_align.py", "presser_pc_job.py", "presser_clips_setup.py"]
TASK_NAME = "NBA Presser Clips (auto)"
OLD_TASK_NAME = "NBA Pressers PC Job"          # the earlier Windows-only PC job
LAUNCHD_LABEL = "com.hoopshype.nba-presser-clips"
AUTO_INTERVAL_MIN = 30                          # checks for a new cloud run this often
IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


# --------------------------------------------------------------------------- #
# Self-update
# --------------------------------------------------------------------------- #

def update(quiet: bool = True) -> int:
    """Download the latest app files; replace each one only if it downloaded
    completely and compiles. Offline or GitHub down: keep the current ones."""
    changed = 0
    for name in UPDATE_FILES:
        target = APP_DIR / name
        tmp = APP_DIR / (name + ".new")
        try:
            req = urllib.request.Request(f"{mc.REPO_RAW}/{name}", headers={"User-Agent": "nba-presser-clips/2.0"})
            with urllib.request.urlopen(req, timeout=30, context=mc._ssl_context()) as resp:
                body = resp.read()
            compile(body.decode("utf-8"), name, "exec")
            if target.is_file() and target.read_bytes() == body:
                continue
            tmp.write_bytes(body)                       # closed before the move
            mc.with_retry(os.replace, str(tmp), str(target))
            changed += 1
        except Exception as e:
            if not quiet:
                say(f"  (could not update {name}: {type(e).__name__}; keeping the current copy)")
        finally:
            mc.remove_file(tmp)
    if changed and not quiet:
        say(f"  updated {changed} file(s)")
    return changed


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

def ffmpeg_has_subtitles(ffmpeg: str) -> bool:
    try:
        res = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return bool(re.search(r"(?m)^\s*\S*\s+ass\s+V->V", res.stdout or ""))


def find_ffmpeg_cmd() -> int:
    ffmpeg = mc.find_ffmpeg(mc.load_settings())
    if not ffmpeg:
        say("ffmpeg: not found")
        return 1
    if not ffmpeg_has_subtitles(ffmpeg):
        say(f"ffmpeg: {ffmpeg} (WITHOUT subtitle support)")
        return 3
    say(f"ffmpeg: {ffmpeg}")
    return 0


def find_deno_cmd() -> int:
    mc.prepare_path(mc.load_settings())
    deno = shutil.which("deno")
    say(f"deno: {deno or 'not found'}")
    return 0 if deno else 1


# --------------------------------------------------------------------------- #
# The four questions
# --------------------------------------------------------------------------- #

def clean_folder_answer(answer: str) -> str:
    """Accept a pasted path with quotes (Windows "Copy as path") or a folder
    dragged into the Mac Terminal (spaces escaped with backslashes)."""
    a = (answer or "").strip().strip('"').strip("'").strip()
    if not IS_WINDOWS:
        a = re.sub(r"\\(.)", r"\1", a)
    return os.path.expandvars(os.path.expanduser(a))


def check_folder(path: Path) -> str:
    """'' if clips can be written there, otherwise why not. Writes a test
    file with a unique name, CLOSES it, then deletes it (retrying: on
    Windows an antivirus or sync client can hold a brand-new file for a
    moment). If the write worked but the delete keeps failing, the folder is
    accepted with a warning."""
    if not path.is_absolute():
        return "please give the full folder path"
    unsafe = mc.unsafe_clips_folder(path)
    if unsafe:
        return (f"{unsafe}, and files older than the limit get deleted there. "
                "Pick or create a folder just for clips, e.g. ...\\presser-clips")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return f"can't create it ({e.strerror or type(e).__name__})"
    probe = path / f".nba-presser-clips-write-test-{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(probe, "wb") as f:
            f.write(b"ok")
    except OSError as e:
        return f"can't write there ({e.strerror or type(e).__name__})"
    if not mc.remove_file(probe):
        say(f"   Note: the folder works, but the test file {probe.name} couldn't be deleted yet")
        say("   (another program is holding it). Delete it by hand if it's still there later.")
    return ""


def ask_folder(current: str | None) -> Path:
    default = Path(current) if current else mc.default_out_dir()
    say("1) Where should the clips be saved?")
    say("   IMPORTANT: use a folder ONLY for these clips. Anything in it older than the")
    say("   limit you choose next is deleted automatically, whoever put it there.")
    say(f"   Press Enter for: {default}")
    say("   Or paste a folder (a shared Google Drive, OneDrive or Dropbox folder works too,")
    say("   as long as it's a folder just for these clips).")
    while True:
        answer = clean_folder_answer(ask("   > "))
        path = Path(answer) if answer else default
        problem = check_folder(path)
        if not problem:
            return path
        say(f"   {problem}. Try again.")


def ask_keep_days(current) -> int:
    default = mc.keep_days({"out_dir": "x", "keep_days": current} if current is not None
                           else {"out_dir": "x"})
    say()
    say("2) Delete files in the clips folder older than how many days?")
    say(f"   Press Enter for {default}, or type a number (0 = never delete).")
    say("   This also clears the saved post text and temporary files on this computer.")
    while True:
        a = ask("   > ").strip()
        if not a:
            return default
        if a.isdigit() and int(a) <= 3650:
            return int(a)
        say("   Please type a whole number of days, like 7 (or 0 for never).")


def ask_formats(current: list | None) -> list:
    default = current or list(mc.FORMAT_ORDER)
    say()
    say("3) Which formats should be made by default? (you can still choose each time)")
    for f in mc.FORMAT_ORDER:
        say(f"   {mc.FORMATS[f]['letter'].upper()} = {mc.FORMATS[f]['label']}")
    say(f"   Press Enter for {'all three' if len(default) == 3 else mc.format_letters(default)}, "
        "or type letters (e.g. VS)")
    while True:
        try:
            return mc.parse_formats(ask("   > "), default)
        except ValueError as e:
            say(f"   {e}. Try again.")


def ask_yes_no(question: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        a = ask(f"{question} [{hint}] > ").strip().lower()
        if not a:
            return default
        if a[0] in "yn":
            return a[0] == "y"
        say("   Please answer Y or N.")


# --------------------------------------------------------------------------- #
# Desktop shortcut
# --------------------------------------------------------------------------- #

def make_desktop_shortcut() -> str:
    if IS_WINDOWS:
        ps = ("$d=[Environment]::GetFolderPath('Desktop');"
              "$p=Join-Path $d 'NBA Presser Clips.lnk';"
              "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($p);"
              "$s.TargetPath=$env:NPC_TARGET;$s.WorkingDirectory=$env:NPC_DIR;"
              "$s.Description='Make NBA presser clips';$s.Save();Write-Output $p")
        env = dict(os.environ, NPC_TARGET=str(APP_DIR / "run-presser-clips.bat"), NPC_DIR=str(APP_DIR))
        res = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                             capture_output=True, text=True, env=env, timeout=120)
        if res.returncode != 0:
            raise RuntimeError((res.stderr or res.stdout or "PowerShell failed").strip()[-300:])
        return (res.stdout or "").strip().splitlines()[-1]
    desktop = Path.home() / "Desktop"
    desktop.mkdir(exist_ok=True)
    target = desktop / f"{APP_NAME}.command"
    shutil.copyfile(APP_DIR / "run-presser-clips-mac.command", target)
    target.chmod(0o755)
    return str(target)


# --------------------------------------------------------------------------- #
# Automatic mode
# --------------------------------------------------------------------------- #

def write_windows_task(xml_path: Path, vbs_path: Path, bat_path: Path) -> None:
    """Every 30 minutes while you're logged in: a hidden check for a new cloud
    run (the job exits at once when there's nothing new)."""
    start = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>NBA Presser Clips: makes the top clips after each new cloud run (checks every {AUTO_INTERVAL_MIN} minutes).</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT{AUTO_INTERVAL_MIN}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT3H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>wscript.exe</Command>
      <Arguments>//B //Nologo "{xml_escape(str(vbs_path))}"</Arguments>
    </Exec>
  </Actions>
</Task>
"""
    xml_path.write_text(xml, encoding="utf-16")
    vbs_path.write_text('Set sh = CreateObject("WScript.Shell")\r\n'
                        f'sh.Run """{bat_path}"" --auto", 0, True\r\n', encoding="utf-8")


def launchd_plist(python: str) -> str:
    log = APP_DIR / "auto-launchd.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{xml_escape(python)}</string>
    <string>{xml_escape(str(APP_DIR / 'presser_clips_setup.py'))}</string>
    <string>--auto</string>
  </array>
  <key>StartInterval</key><integer>{AUTO_INTERVAL_MIN * 60}</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>{xml_escape(str(log))}</string>
  <key>StandardErrorPath</key><string>{xml_escape(str(log))}</string>
</dict>
</plist>
"""


def _run_quiet(cmd: list) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=120)


def set_auto(enabled: bool) -> str:
    if IS_WINDOWS:
        if not enabled:
            _run_quiet(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
            return "off"
        xml_path, vbs = APP_DIR / "auto-task.xml", APP_DIR / "run-hidden.vbs"
        write_windows_task(xml_path, vbs, APP_DIR / "run-presser-clips.bat")
        res = _run_quiet(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"])
        if res.returncode != 0:
            raise RuntimeError((res.stderr or res.stdout or "schtasks failed").strip()[-300:])
        return f"on (Task Scheduler: \"{TASK_NAME}\", checks every {AUTO_INTERVAL_MIN} minutes)"
    if IS_MAC:
        agents = Path.home() / "Library" / "LaunchAgents"
        plist = agents / f"{LAUNCHD_LABEL}.plist"
        if plist.exists():
            _run_quiet(["launchctl", "unload", "-w", str(plist)])
        if not enabled:
            mc.remove_file(plist)
            return "off"
        agents.mkdir(parents=True, exist_ok=True)
        plist.write_text(launchd_plist(sys.executable), encoding="utf-8")
        res = _run_quiet(["launchctl", "load", "-w", str(plist)])
        if res.returncode != 0:
            raise RuntimeError((res.stderr or res.stdout or "launchctl failed").strip()[-300:])
        return f"on (checks every {AUTO_INTERVAL_MIN} minutes while you're logged in)"
    return "not available on this system"


def old_windows_task_exists() -> bool:
    return IS_WINDOWS and _run_quiet(["schtasks", "/Query", "/TN", OLD_TASK_NAME]).returncode == 0


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #

def setup() -> int:
    settings_path = APP_DIR / "settings.json"
    current = mc.load_settings(settings_path)
    say()
    say("=" * 60)
    say(f"  {APP_NAME}: four quick questions")
    say("=" * 60)
    say()
    out_dir = ask_folder(current.get("out_dir"))
    days = ask_keep_days(current.get("keep_days"))
    formats = ask_formats(mc.settings_formats(current) if current else None)
    say()
    say("4) Make clips automatically after each cloud run?")
    say("   (the top 10 quotes by news score, in your default formats, while this computer is on)")
    auto = ask_yes_no("  ", bool(current.get("auto_run", False)))

    settings = {
        "out_dir": str(out_dir),
        "keep_days": days,
        "formats": formats,
        "auto_run": auto,
        "ffmpeg": mc.find_ffmpeg(current) or "",
        "installed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    tmp = settings_path.with_name("settings.json.tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    mc.with_retry(os.replace, str(tmp), str(settings_path))

    say()
    try:
        shortcut = make_desktop_shortcut()
    except Exception as e:
        shortcut = f"could not be created ({e})"
    try:
        auto_state = set_auto(auto)
    except Exception as e:
        auto_state = f"could not be set up ({e})"
    if old_windows_task_exists():
        say(f"Found the older automatic job \"{OLD_TASK_NAME}\". This app replaces it.")
        if ask_yes_no("   Remove the old one?", True):
            _run_quiet(["schtasks", "/Delete", "/TN", OLD_TASK_NAME, "/F"])
            say("   removed.")

    say()
    say("=" * 60)
    say("  All set.")
    say(f"  Clips are saved in: {out_dir}")
    say(f"  Cleanup:            {'never' if not days else f'files older than {days} days are deleted'}")
    say(f"  Post text and logs: {mc.app_dir()}")
    say(f"  Default formats:    {', '.join(formats)}")
    say(f"  Automatic mode:     {auto_state}")
    say(f"  Desktop shortcut:   {shortcut}")
    say("=" * 60)
    say("To make clips, double-click \"NBA Presser Clips\" on your desktop.")
    say("To change these answers later, run the installer again.")
    return 0


def auto() -> int:
    """Automatic mode: refresh the code, then hand over to the job (a fresh
    process, so it runs the updated files)."""
    update(quiet=True)
    return subprocess.run([sys.executable, str(APP_DIR / "presser_pc_job.py"), "--scheduled", "--only-new"]).returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=f"{APP_NAME} setup")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--setup", action="store_true")
    g.add_argument("--update", action="store_true")
    g.add_argument("--auto", action="store_true")
    g.add_argument("--find-ffmpeg", action="store_true")
    g.add_argument("--find-deno", action="store_true")
    args = ap.parse_args()
    if args.setup:
        return setup()
    if args.update:
        update(quiet=False)
        return 0
    if args.auto:
        return auto()
    if args.find_ffmpeg:
        return find_ffmpeg_cmd()
    return find_deno_cmd()


if __name__ == "__main__":
    sys.exit(main())
