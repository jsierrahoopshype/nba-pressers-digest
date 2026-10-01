"""
Offline tests for the PC job (local clip rendering only; no GitHub writes).

    python -m unittest discover -s pressers_v2/tests -v
"""

import os
import sys
import tempfile
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ["NBA_PC_HOME"] = tempfile.mkdtemp(prefix="pcjob_home_")   # before importing the job
os.environ.setdefault("NBA_PRESSER_APP_DIR", tempfile.mkdtemp(prefix="npc_app_"))

import presser_pc_job as job  # noqa: E402


class PcJobTests(unittest.TestCase):
    def test_task_xml_and_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            xml_path, vbs = Path(tmp) / "task.xml", Path(tmp) / "run-hidden.vbs"
            old = job.cron_slots_utc
            job.cron_slots_utc = lambda: ["06:15", "14:15", "20:15"]
            try:
                times = job.write_task_xml(xml_path, vbs)
            finally:
                job.cron_slots_utc = old
            root = ET.fromstring(xml_path.read_text(encoding="utf-16"))
            ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
            starts = [e.text[-9:] for e in root.findall(".//t:StartBoundary", ns)]
            self.assertEqual(starts, ["06:45:00Z", "14:45:00Z", "20:45:00Z"])
            self.assertEqual(root.find(".//t:StartWhenAvailable", ns).text, "false")
            self.assertEqual(root.find(".//t:Command", ns).text, "wscript.exe")
            self.assertIn(", 0, True", vbs.read_text(encoding="utf-8"))
            self.assertEqual(times, ["06:45 UTC", "14:45 UTC", "20:45 UTC"])

    def test_run_renders_top_clips_from_public_manifest_without_token(self):
        import make_presser_clips as mc
        calls = []
        old_run, old_find = job.subprocess.run, mc.find_ffmpeg
        job.subprocess.run = lambda cmd, **kw: (calls.append(cmd),
                                                types.SimpleNamespace(returncode=0, stdout="Done. 2 clip(s) made",
                                                                      stderr=""))[1]
        mc.find_ffmpeg = lambda settings=None: "/usr/bin/ffmpeg"
        try:
            self.assertEqual(job.run_job(), 0)
        finally:
            job.subprocess.run, mc.find_ffmpeg = old_run, old_find
        cmd = calls[0]
        self.assertIn("--yes", cmd)
        self.assertEqual(cmd[cmd.index("--top") + 1], "10")
        # no settings.json next to the job = the older Windows install: vertical only, as before
        self.assertEqual(cmd[cmd.index("--formats") + 1], "vertical")
        self.assertEqual(cmd[cmd.index("--out") + 1], str(Path(os.environ["NBA_PC_HOME"]) / "Documents" / "presser-clips"))
        self.assertTrue(cmd[cmd.index("--manifest") + 1].startswith("https://raw.githubusercontent.com/"))
        self.assertIn("Done. 2 clip(s) made", job.LOG_PATH.read_text(encoding="utf-8"))

    def test_only_new_skips_a_run_it_already_rendered(self):
        import make_presser_clips as mc
        calls = []
        old_run, old_find, old_load = job.subprocess.run, mc.find_ffmpeg, mc.load_manifest
        job.subprocess.run = lambda cmd, **kw: (calls.append(cmd),
                                                types.SimpleNamespace(returncode=0, stdout="", stderr=""))[1]
        mc.find_ffmpeg = lambda settings=None: "/usr/bin/ffmpeg"
        mc.load_manifest = lambda src: {"latest_run_id": "2026-10-01T1418Z-1415"}
        try:
            job.STATE_PATH.unlink(missing_ok=True)
            job.run_job(only_new=True)
            job.run_job(only_new=True)            # same cloud run: nothing to do
            mc.load_manifest = lambda src: {"latest_run_id": "2026-10-01T2018Z-2015"}
            job.run_job(only_new=True)
        finally:
            job.subprocess.run, mc.find_ffmpeg, mc.load_manifest = old_run, old_find, old_load
        self.assertEqual(len(calls), 2)
        self.assertEqual(job.STATE_PATH.read_text(encoding="utf-8"), "2026-10-01T2018Z-2015")

    def test_log_lives_in_the_app_folder_and_old_installs_never_delete(self):
        self.assertEqual(job.LOG_PATH, Path(os.environ["NBA_PRESSER_APP_DIR"]) / "logs" / "pc-job-log.txt")
        self.assertEqual(job.KEEP_DAYS, 0)          # no installer settings.json: cleanup stays off

    def test_job_has_no_github_write_path(self):
        src = (HERE.parent / "presser_pc_job.py").read_text(encoding="utf-8")
        for word in ("Authorization", "put_file", "api.github.com", "token"):
            self.assertNotIn(word, src.replace("no token is needed", ""))


if __name__ == "__main__":
    unittest.main()
