"""
The retired automatic job: any scheduled run removes the scheduled task(s)
and renders nothing.

    python -m unittest discover -s pressers_v2/tests -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault("NBA_PRESSER_APP_DIR", tempfile.mkdtemp(prefix="npc_app_"))

import presser_pc_job as job  # noqa: E402


class RetiredJobTests(unittest.TestCase):
    def test_windows_tasks_are_removed(self):
        calls = []

        def fake(cmd):
            calls.append(cmd)
            return 0
        with mock.patch.object(job, "_quiet", fake), mock.patch.object(job.os, "name", "nt"):
            removed = job.retire()
        self.assertEqual(removed, list(job.TASK_NAMES))
        self.assertIn(["schtasks", "/Delete", "/TN", "NBA Pressers PC Job", "/F"], calls)
        self.assertIn(["schtasks", "/Delete", "/TN", "NBA Presser Clips (auto)", "/F"], calls)

    def test_mac_agent_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp) / "com.hoopshype.nba-presser-clips.plist"
            plist.write_text("<plist/>")
            with mock.patch.object(job, "LAUNCHD_PLIST", plist), mock.patch.object(job.os, "name", "posix"), \
                    mock.patch.object(job.sys, "platform", "darwin"), mock.patch.object(job, "_quiet", lambda c: 0):
                self.assertEqual(job.retire(), [plist.name])
            self.assertFalse(plist.exists())

    def test_a_scheduled_run_renders_nothing(self):
        with mock.patch.object(job, "retire", lambda: []), \
                mock.patch.object(job.subprocess, "run", side_effect=AssertionError("nothing may run")):
            self.assertEqual(job.main(), 0)
        src = (HERE.parent / "presser_pc_job.py").read_text(encoding="utf-8")
        for word in ("yt_dlp", "make_clip", "Authorization", "api.github.com"):
            self.assertNotIn(word, src)


if __name__ == "__main__":
    unittest.main()
