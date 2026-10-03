"""
Setup and self-update for NBA Presser Clips (Windows, macOS).

The installers (install-presser-clips.bat, install-presser-clips-mac.command)
put Python, ffmpeg and a private Python environment in place, download the
app files into the app folder, then call this script:

    python presser_clips_setup.py --setup         the three questions, settings.json,
                                                  "Clip it" links, desktop shortcut
    python presser_clips_setup.py --update        refresh the app files from GitHub (and add
                                                  any Python package a new version needs)
    python presser_clips_setup.py --auto          what the retired automatic mode's scheduled
                                                  task still runs: switches it off for good
    python presser_clips_setup.py --find-ffmpeg   exit 0 = found with subtitles support,
                                                  1 = not found, 3 = found without subtitles
    python presser_clips_setup.py --find-deno     exit 0 = found
    python presser_clips_setup.py --pins          the pinned pip packages (faster-whisper, PyAV)

"Clip it" links (presserclips://clip?...) are registered for the current
user only: on Windows under HKEY_CURRENT_USER (no administrator rights), on
macOS as a small app in ~/Applications that declares the link type
(CFBundleURLTypes) and opens Terminal to show progress.

App folder: %LOCALAPPDATA%\\NBA Presser Clips (Windows) or
~/Library/Application Support/NBA Presser Clips (macOS). Nothing here needs
a GitHub account or token: every download is a public file.
"""

import argparse
import hashlib
import http.client
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import make_presser_clips as mc  # noqa: E402

APP_NAME = "NBA Presser Clips"
MODEL_FILE = "models/face_detection_yunet_2023mar.onnx"
MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
# dependencies first, so a new make_presser_clips.py never lands without them
UPDATE_FILES = ["caption_align.py", "reframe.py", MODEL_FILE, "make_presser_clips.py", "presser_clips_setup.py"]
PIP_PACKAGES = {"cv2": "opencv-python-headless"}       # import name -> pip name (face tracking)
# Exact versions for the Whisper fallback. PyAV 19 removed the
# metadata_errors argument that faster-whisper 1.2.1 (the latest) still
# passes to av.open(), so every transcription crashed with
# "open() got an unexpected keyword argument 'metadata_errors'".
PINNED = {"faster-whisper": "1.2.1", "av": "18.1.0"}
PIN_RETRY_SECS = 24 * 3600      # after a failed pip attempt, wait a day before trying again
TASK_NAME = "NBA Presser Clips (auto)"                  # the retired automatic mode
OLD_TASK_NAME = "NBA Pressers PC Job"                   # the earlier Windows-only PC job
LAUNCHD_LABEL = "com.hoopshype.nba-presser-clips"
MAC_APP_ID = "com.hoopshype.nba-presser-clips.link"
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework"
              "/Support/lsregister")
IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def _run_quiet(cmd: list, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)


# --------------------------------------------------------------------------- #
# Self-update
# --------------------------------------------------------------------------- #

def _valid_download(name: str, body: bytes) -> bool:
    if name.endswith(".py"):
        compile(body.decode("utf-8"), name, "exec")
        return True
    if name == MODEL_FILE:
        return hashlib.sha256(body).hexdigest() == MODEL_SHA256
    return bool(body)


CORE_FILES = ("make_presser_clips.py", "caption_align.py", "reframe.py", "presser_clips_setup.py")
UPDATE_TIMEOUTS = (60, 120, 180)        # seconds per attempt: each retry waits longer for GitHub
UPDATE_RETRY_WAITS = (2, 5)             # pause before the 2nd and 3rd attempt
UPDATE_WARNING = "Couldn't update, running the previous version; check your internet or rerun the installer."


def _download(name: str) -> bytes:
    """One app file from GitHub, tried up to 3 times with a longer timeout
    each time when the connection fails or times out. A plain "not found"
    or other HTTP answer isn't retried."""
    url = f"{mc.REPO_RAW}/{name}"
    for attempt, timeout in enumerate(UPDATE_TIMEOUTS):
        if attempt:
            time.sleep(UPDATE_RETRY_WAITS[min(attempt - 1, len(UPDATE_RETRY_WAITS) - 1)])
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nba-presser-clips/3.1"})
            with urllib.request.urlopen(req, timeout=timeout, context=mc._ssl_context()) as resp:
                return resp.read()
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead):
            if attempt == len(UPDATE_TIMEOUTS) - 1:
                raise
    raise RuntimeError("unreachable")


def update(quiet: bool = True) -> int:
    """Download the latest app files; replace each one only if it downloaded
    completely and is valid (Python that compiles; the face model with its
    known checksum). Offline or GitHub down: keep the current ones, and say
    so clearly when a core file couldn't be refreshed. Then, whatever
    happened above, check the pinned packages and make sure the retired
    automatic mode is off."""
    changed, failed = 0, []
    try:
        for name in UPDATE_FILES:
            target = APP_DIR / name
            tmp = target.with_name(target.name + ".new")
            try:
                body = _download(name)
                if not _valid_download(name, body):
                    raise ValueError("failed the integrity check")
                if target.is_file() and target.read_bytes() == body:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(body)                       # closed before the move
                mc.with_retry(os.replace, str(tmp), str(target))
                changed += 1
            except Exception as e:
                failed.append((name, f"{type(e).__name__}: {e}"[:200]))
                if not quiet:
                    say(f"  (could not update {name}: {type(e).__name__}; keeping the current copy)")
            finally:
                mc.remove_file(tmp)
        if changed and not quiet:
            say(f"  updated {changed} file(s)")
        core = [(n, why) for n, why in failed if n in CORE_FILES]
        if core:
            say(f"[!] {UPDATE_WARNING}")
            mc.log_line("setup-log.txt", f"update failed for {', '.join(n for n, _ in core)}: "
                                         + "; ".join(f"{n}: {why}" for n, why in core))
        elif failed:
            mc.log_line("setup-log.txt", "update failed for " + "; ".join(f"{n}: {why}" for n, why in failed))
    finally:
        # the pins and the automatic-mode switch-off don't depend on the download
        try:
            ensure_packages()
        except Exception as e:
            mc.log_line("setup-log.txt", f"package check failed: {type(e).__name__}: {e}")
        if mc.load_settings(APP_DIR / "settings.json").get("auto_run"):
            disable_auto()
    return changed


def ensure_packages() -> None:
    """pip-install what a newer version needs (e.g. OpenCV for face tracking)
    into this app's own Python environment, once."""
    for module, package in PIP_PACKAGES.items():
        if _run_quiet([sys.executable, "-c", f"import {module}"], timeout=120).returncode == 0:
            continue
        say(f"Installing {package} for face tracking (one time, about a minute)...")
        res = _run_quiet([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--quiet",
                          package], timeout=900)
        if res.returncode != 0:
            say("  could not install it now; clips use a centred crop until it works")
    ensure_pins()


def pip_specs() -> list:
    return [f"{name}=={version}" for name, version in PINNED.items()]


def installed_versions() -> dict:
    """{distribution: version or None} for the pinned packages, read in a
    fresh process so a just-finished upgrade is seen."""
    code = ("import importlib.metadata as m, json\n"
            "out = {}\n"
            f"for d in {list(PINNED)!r}:\n"
            "    try:\n        out[d] = m.version(d)\n"
            "    except m.PackageNotFoundError:\n        out[d] = None\n"
            "print(json.dumps(out))")
    res = _run_quiet([sys.executable, "-c", code], timeout=120)
    try:
        return json.loads(res.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {d: None for d in PINNED}


def ensure_pins() -> bool:
    """Bring faster-whisper / PyAV to the pinned versions (existing installs
    get the fix on their next run). A failed attempt is retried after a day,
    so a blocked pip doesn't slow every click. True when the pins hold."""
    have = installed_versions()
    if all(have.get(d) == v for d, v in PINNED.items()):
        return True
    marker = mc.app_dir() / "logs" / "pip-pins-failed.txt"
    try:
        if time.time() - marker.stat().st_mtime < PIN_RETRY_SECS:
            return False
    except OSError:
        pass
    wrong = ", ".join(f"{d} {have.get(d) or 'missing'} -> {v}" for d, v in PINNED.items() if have.get(d) != v)
    say(f"Updating the Whisper fallback ({wrong}); one time, about a minute...")
    res = _run_quiet([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--quiet",
                      *pip_specs()], timeout=1800)
    if res.returncode == 0:
        mc.remove_file(marker)
        mc.log_line("setup-log.txt", f"pinned {', '.join(pip_specs())} (was: {wrong})")
        return True
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text((res.stderr or res.stdout or "")[-2000:], encoding="utf-8")
    mc.log_line("setup-log.txt", f"pip could not install {', '.join(pip_specs())}: {(res.stderr or '')[-300:]}")
    say("  could not update it now; videos without usable captions may be skipped")
    return False


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
# The three questions
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
    say("2) Delete clips in the clips folder older than how many days?")
    say(f"   Press Enter for {default}, or type a number (0 = never delete).")
    say("   Each quote's folder goes as a whole; temporary files on this computer too.")
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
# "Clip it" links: presserclips://clip?v=..&t=..&q=..
# --------------------------------------------------------------------------- #

def link_command(python: Path, tool: Path) -> str:
    """What Windows runs for a presserclips: link. A console Python, so the
    progress shows in a window that closes by itself. The tool accepts
    nothing but "--link <one link>" in this mode."""
    return f'"{python}" "{tool}" --link "%1"'


def register_link_windows() -> str:
    import winreg
    base = rf"Software\Classes\{mc.LINK_SCHEME}"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:NBA Presser Clips link")
        winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ,
                          link_command(Path(sys.executable), APP_DIR / "make_presser_clips.py"))
    return "registered for this Windows user"


MAC_HANDLER = """on open location theURL
	set appDir to (POSIX path of (path to home folder)) & "Library/Application Support/NBA Presser Clips"
	set py to appDir & "/venv/bin/python"
	set tool to appDir & "/make_presser_clips.py"
	set cmd to "clear; " & quoted form of py & " " & quoted form of tool & " --link " & quoted form of theURL & "; exit"
	tell application "Terminal"
		activate
		do script cmd
	end tell
end open location

on run
	display dialog "NBA Presser Clips makes a clip when you click Clip it in the digest. To choose clips from a list, use NBA Presser Clips on your Desktop." buttons {"OK"} default button 1
end run
"""


def mac_bundle_path() -> Path:
    return Path.home() / "Applications" / f"{APP_NAME}.app"


def add_url_type(info: dict) -> dict:
    info["CFBundleIdentifier"] = MAC_APP_ID
    info["CFBundleName"] = APP_NAME
    info["CFBundleURLTypes"] = [{"CFBundleURLName": f"{APP_NAME} link",
                                 "CFBundleURLSchemes": [mc.LINK_SCHEME]}]
    return info


def register_link_mac() -> str:
    """A small AppleScript app in ~/Applications that declares the
    presserclips: link type; a click opens Terminal running the tool (the
    link is passed as one shell-quoted argument)."""
    bundle = mac_bundle_path()
    bundle.parent.mkdir(parents=True, exist_ok=True)
    mc.remove_tree(bundle)
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "handler.applescript"
        script.write_text(MAC_HANDLER, encoding="utf-8")
        res = _run_quiet(["osacompile", "-o", str(bundle), str(script)])
        if res.returncode != 0:
            raise RuntimeError((res.stderr or "osacompile failed").strip()[-300:])
    plist = bundle / "Contents" / "Info.plist"
    with open(plist, "rb") as f:
        info = plistlib.load(f)
    with open(plist, "wb") as f:
        plistlib.dump(add_url_type(info), f)
    _run_quiet(["codesign", "--force", "--deep", "--sign", "-", str(bundle)])   # re-sign after the edit
    if Path(LSREGISTER).exists():
        _run_quiet([LSREGISTER, "-f", str(bundle)])
    return f"registered ({bundle})"


def register_link() -> str:
    if IS_WINDOWS:
        return register_link_windows()
    if IS_MAC:
        return register_link_mac()
    return "not available on this system"


# --------------------------------------------------------------------------- #
# Automatic mode: retired. Switch it off wherever an earlier version set it up.
# --------------------------------------------------------------------------- #

def disable_auto() -> str:
    removed = []
    if IS_WINDOWS:
        for name in (TASK_NAME, OLD_TASK_NAME):
            if _run_quiet(["schtasks", "/Query", "/TN", name]).returncode == 0:
                _run_quiet(["schtasks", "/Delete", "/TN", name, "/F"])
                removed.append(name)
    elif IS_MAC:
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        if plist.exists():
            _run_quiet(["launchctl", "unload", "-w", str(plist)])
            mc.remove_file(plist)
            removed.append(LAUNCHD_LABEL)
    settings_path = APP_DIR / "settings.json"
    settings = mc.load_settings(settings_path)
    if settings.get("auto_run"):
        settings["auto_run"] = False
        save_settings(settings_path, settings)
    if removed:
        mc.log_line("setup-log.txt", f"automatic mode switched off: removed {', '.join(removed)}")
    return ", ".join(removed)


def save_settings(path: Path, settings: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    mc.with_retry(os.replace, str(tmp), str(path))


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #

def setup() -> int:
    settings_path = APP_DIR / "settings.json"
    current = mc.load_settings(settings_path)
    say()
    say("=" * 60)
    say(f"  {APP_NAME}: three quick questions")
    say("=" * 60)
    say()
    out_dir = ask_folder(current.get("out_dir"))
    days = ask_keep_days(current.get("keep_days"))
    formats = ask_formats(mc.settings_formats(current) if current else None)

    settings = {
        "out_dir": str(out_dir),
        "keep_days": days,
        "formats": formats,
        "auto_run": False,
        "ffmpeg": mc.find_ffmpeg(current) or "",
        "installed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    save_settings(settings_path, settings)

    say()
    try:
        links = register_link()
    except Exception as e:
        links = f"could not be registered ({e})"
    try:
        shortcut = make_desktop_shortcut()
    except Exception as e:
        shortcut = f"could not be created ({e})"
    retired = disable_auto()

    say()
    say("=" * 60)
    say("  All set.")
    say(f"  Clips are saved in: {out_dir}")
    say(f"  Cleanup:            {'never' if not days else f'quote folders older than {days} days are deleted'}")
    say(f"  Default formats:    {', '.join(formats)}")
    say(f"  Clip it links:      {links}")
    say(f"  Desktop shortcut:   {shortcut}")
    if retired:
        say(f"  Automatic mode:     switched off (removed {retired})")
    say(f"  Logs:               {mc.app_dir() / 'logs'}")
    say("=" * 60)
    say("Click \"Clip it\" under any quote in the digest to make that clip.")
    say("Or double-click \"NBA Presser Clips\" on your desktop to pick from a list.")
    say("To change these answers later, run the installer again.")
    return 0


def auto() -> int:
    """The retired automatic mode's scheduled task lands here: switch it off
    for good (and refresh the app files while at it)."""
    update(quiet=True)
    disable_auto()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=f"{APP_NAME} setup")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--setup", action="store_true")
    g.add_argument("--update", action="store_true")
    g.add_argument("--auto", action="store_true")
    g.add_argument("--find-ffmpeg", action="store_true")
    g.add_argument("--find-deno", action="store_true")
    g.add_argument("--pins", action="store_true", help="print the pinned pip packages (installers, CI)")
    args = ap.parse_args()
    if args.setup:
        return setup()
    if args.update:
        update(quiet=False)
        return 0
    if args.auto:
        return auto()
    if args.pins:
        print(" ".join(pip_specs()))
        return 0
    if args.find_ffmpeg:
        return find_ffmpeg_cmd()
    return find_deno_cmd()


if __name__ == "__main__":
    sys.exit(main())
