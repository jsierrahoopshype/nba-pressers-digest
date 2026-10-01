"""
Automatic clip job for NBA Presser Clips: renders the top clips locally.

Two ways it gets installed:
  * install-presser-clips.bat / install-presser-clips-mac.command (any
    computer): settings.json sits next to this file. Clips go to the folder
    and formats chosen at install; the scheduler (Task Scheduler on Windows,
    launchd on macOS) starts it every 30 minutes with --only-new, so it only
    works when a new cloud run is out.
  * the older install-presser-pc-job.bat (Windows): no settings.json; Task
    Scheduler runs it 30 minutes after each cloud run and it renders vertical
    clips into <home>\\Documents\\presser-clips, as before.

Every run first tidies the clips folder (notes and logs move to the app
folder) and deletes files older than keep_days (default 7).

Each run renders the top 10 quotes by news_score from the latest clip list
(clips already in the folder are skipped). Before each cut the clipper aligns
the quote on the video's captions locally (or Whisper), so cuts are exact.
Nothing is written to GitHub and no token is needed.

    python presser_pc_job.py                         normal run
    python presser_pc_job.py --only-new              skip unless a new cloud run is out
    python presser_pc_job.py --write-task-xml FILE   scheduler definition (older installer)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

WORKFLOW_RAW_URL = ("https://raw.githubusercontent.com/jsierrahoopshype/nba-pressers-digest/"
                    "main/.github/workflows/pressers-v2.yml")
FALLBACK_CRON_UTC = ["06:15", "14:15", "20:15"]   # used only if the workflow can't be read
DELAY_AFTER_CRON_MIN = 30

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))
import make_presser_clips as mc  # noqa: E402
SETTINGS_PATH = APP_DIR / "settings.json"
TOP_CLIPS = 10
LOG_MAX_BYTES = 2_000_000


def _load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


SETTINGS = _load_settings()
HOME = Path(os.environ.get("NBA_PC_HOME") or os.environ.get("USERPROFILE") or Path.home())
if SETTINGS.get("out_dir"):
    # installed by install-presser-clips (any computer)
    WORK = APP_DIR
    CLIPS_ROOT = Path(SETTINGS["out_dir"])
    FORMATS = ",".join(f for f in SETTINGS.get("formats") or [] if f in ("vertical", "youtube", "square")) \
        or "vertical,youtube,square"
else:
    # the older Windows install-presser-pc-job.bat layout: vertical only, as before
    WORK = HOME / "Documents" / "nba-pressers-digest-pc"
    CLIPS_ROOT = HOME / "Documents" / "presser-clips"
    FORMATS = "vertical"
STATE_PATH = WORK / "last-auto-run.txt"
# Logs live in the per-user app folder (never in the clips folder, which may
# be shared): %LOCALAPPDATA%\\NBA Presser Clips\\logs or
# ~/Library/Application Support/NBA Presser Clips/logs.
LOG_PATH = mc.app_dir() / "logs" / "pc-job-log.txt"
KEEP_DAYS = mc.keep_days(SETTINGS)



def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except (OSError, ValueError):
        pass   # no console when run hidden
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.is_file() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
            mc.with_retry(os.replace, str(LOG_PATH), str(LOG_PATH.with_suffix(".old.txt")))
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Scheduler XML
# --------------------------------------------------------------------------- #

def cron_slots_utc() -> list:
    """HH:MM UTC of each pressers-v2 cron, read from the public workflow file."""
    try:
        req = urllib.request.Request(WORKFLOW_RAW_URL, headers={"User-Agent": "nba-pressers-pc-job"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8")
    except Exception:
        text = ""
    slots = [f"{int(m.group(2)):02d}:{int(m.group(1)):02d}"
             for m in re.finditer(r"cron:\s*'(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*'", text)]
    return slots or list(FALLBACK_CRON_UTC)


def write_task_xml(path: Path, vbs_path: Path) -> list:
    """Task Scheduler XML: one daily trigger per cron slot + 30 min, anchored
    in UTC (daylight saving can't shift it). Runs only while the PC is on and
    you're logged in; missed runs are not caught up; started through a .vbs
    launcher so no window appears. Also writes that launcher."""
    today = datetime.now(timezone.utc).date()
    triggers, times = [], []
    for hhmm in cron_slots_utc():
        h, m = (int(x) for x in hhmm.split(":"))
        t = (datetime(today.year, today.month, today.day, h, m, tzinfo=timezone.utc)
             + timedelta(minutes=DELAY_AFTER_CRON_MIN))
        times.append(t.strftime("%H:%M UTC"))
        triggers.append(
            "    <CalendarTrigger>\n"
            f"      <StartBoundary>{t.strftime('%Y-%m-%dT%H:%M:%SZ')}</StartBoundary>\n"
            "      <Enabled>true</Enabled>\n"
            "      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>\n"
            "    </CalendarTrigger>")
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>NBA pressers: render the top clips locally, 30 minutes after each pressers-v2 cloud run.</Description>
  </RegistrationInfo>
  <Triggers>
{chr(10).join(triggers)}
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
    <StartWhenAvailable>false</StartWhenAvailable>
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
    path.write_text(xml, encoding="utf-16")
    bat = vbs_path.parent / "run-presser-pc-job.bat"
    vbs_path.write_text('Set sh = CreateObject("WScript.Shell")\r\n'
                        f'sh.Run """{bat}"" --scheduled", 0, True\r\n', encoding="utf-8")
    return times


# --------------------------------------------------------------------------- #
# The job
# --------------------------------------------------------------------------- #

class Lock:
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and time.time() - self.path.stat().st_mtime < 3 * 3600:
            raise RuntimeError("another clip job run is still going (lock file is fresh)")
        self.path.write_text(str(os.getpid()), encoding="utf-8")
        return self

    def __exit__(self, *exc):
        mc.remove_file(self.path)


def latest_run_id(mc) -> str:
    """The clip list's latest run, or '' if it can't be read right now."""
    try:
        return str(mc.load_manifest(mc.MANIFEST_URL).get("latest_run_id") or "")
    except Exception:
        return ""


def housekeeping() -> None:
    """Every run, new cloud run or not: notes/logs out of the clips folder,
    then delete files older than KEEP_DAYS (clips folder, notes, tmp)."""
    try:
        moved = mc.tidy_clips_folder(CLIPS_ROOT)
        if moved:
            log(f"Moved {moved} note/log file(s) out of {CLIPS_ROOT} into {mc.app_dir()}")
        n, freed = mc.run_cleanup(CLIPS_ROOT, KEEP_DAYS)
        if n:
            log(f"Cleanup: deleted {n} file(s) older than {KEEP_DAYS} days, freed {mc.human_size(freed)}")
    except Exception as e:
        log(f"[!] cleanup skipped: {type(e).__name__}: {e}")


def run_job(only_new: bool = False) -> int:
    housekeeping()
    run_id = ""
    if only_new:
        run_id = latest_run_id(mc)
        try:
            last = STATE_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            last = ""
        if not run_id or run_id == last:
            return 0          # nothing new (or offline): stay quiet, no log line every 30 minutes
    log("=" * 60)
    log("Clip job starting" + (f" for cloud run {run_id}" if run_id else ""))
    if not mc.find_ffmpeg(SETTINGS):
        log("[X] ffmpeg not found. Fix: run the installer again (it repairs the setup).")
        return 2
    cmd = [sys.executable, str(Path(mc.__file__).resolve()), "--yes", "--top", str(TOP_CLIPS),
           "--out", str(CLIPS_ROOT), "--formats", FORMATS, "--manifest", mc.MANIFEST_URL, "--no-cleanup"]
    log(f"Rendering the top {TOP_CLIPS} clips by news score ({FORMATS}) into {CLIPS_ROOT}...")
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                         stdin=subprocess.DEVNULL, timeout=3 * 3600)
    for line in (res.stdout or "").splitlines():
        if line.strip():
            log(f"  clipper | {line.rstrip()}")
    if res.returncode not in (0, None):
        log(f"[!] clipper exited with {res.returncode}: {(res.stderr or '').strip()[-300:]}")
    elif run_id:
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(run_id, encoding="utf-8")
        except OSError:
            pass
    log("Clip job finished")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="NBA pressers PC job")
    ap.add_argument("--write-task-xml", metavar="FILE")
    ap.add_argument("--vbs", metavar="FILE", help="launcher .vbs the task should run")
    ap.add_argument("--scheduled", action="store_true", help="started by the scheduler")
    ap.add_argument("--only-new", action="store_true", help="skip unless a new cloud run is out")
    args = ap.parse_args()
    if args.write_task_xml:
        times = write_task_xml(Path(args.write_task_xml), Path(args.vbs or WORK / "run-hidden.vbs"))
        print("Scheduled daily at " + ", ".join(times) + " (30 min after each cloud run).")
        return 0
    try:
        with Lock(WORK / ".pc-job.lock"):
            return run_job(only_new=args.only_new)
    except RuntimeError as e:
        log(f"[!] {e}; exiting")
        return 0
    except Exception as e:
        log(f"[X] Clip job crashed: {type(e).__name__}: {e}")
        for line in traceback.format_exc().splitlines():
            log(f"    {line}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
