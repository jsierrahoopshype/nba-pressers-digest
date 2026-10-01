"""
Retired: automatic clip making is switched off.

Earlier versions scheduled this job (Windows Task Scheduler "NBA Pressers PC
Job" / "NBA Presser Clips (auto)", or a macOS launchd agent) to render clips
after each cloud run. Those schedulers still download the latest copy of
this file and run it, so this version removes the scheduled task(s) it can
find and exits without rendering anything. Clips are now made on request:
the "Clip it" links in the digest, or the NBA Presser Clips desktop shortcut.

    python presser_pc_job.py       remove the old scheduled task(s), then exit
"""

import os
import subprocess
import sys
import time
from pathlib import Path

TASK_NAMES = ("NBA Pressers PC Job", "NBA Presser Clips (auto)")
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / "com.hoopshype.nba-presser-clips.plist"


def _quiet(cmd: list) -> int:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=60).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1


def retire() -> list:
    """Remove every scheduled task an earlier version set up. Returns the
    names it removed."""
    removed = []
    if os.name == "nt":
        for name in TASK_NAMES:
            if _quiet(["schtasks", "/Query", "/TN", name]) == 0 and \
                    _quiet(["schtasks", "/Delete", "/TN", name, "/F"]) == 0:
                removed.append(name)
    elif sys.platform == "darwin" and LAUNCHD_PLIST.exists():
        _quiet(["launchctl", "unload", "-w", str(LAUNCHD_PLIST)])
        try:
            LAUNCHD_PLIST.unlink()
            removed.append(LAUNCHD_PLIST.name)
        except OSError:
            pass
    return removed


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except (OSError, ValueError):
        pass                                   # no console when run hidden
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import make_presser_clips as mc
        mc.log_line("pc-job-log.txt", msg)
    except BaseException:
        pass


def main() -> int:
    removed = retire()
    log("Automatic clip making is retired; "
        + (f"removed the scheduled task(s): {', '.join(removed)}." if removed else "no scheduled task left."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
