"""
Offline tests for the PC job (GitHub API and YouTube captions faked).

    python -m unittest discover -s pressers_v2/tests -v
"""

import base64
import json
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
_TMP_HOME = tempfile.mkdtemp(prefix="pcjob_home_")
os.environ["NBA_PC_HOME"] = _TMP_HOME          # must be set before importing the job

import caption_align  # noqa: E402
import make_presser_clips as mc  # noqa: E402
import presser_extractor as pe  # noqa: E402
import presser_pc_job as job  # noqa: E402

QUOTE = ("We have to be better on the defensive end. Our effort in the third quarter "
         "was not good enough, and that is on me.")
VID = "AAAAAAAAAA1"
DAY = "2026-09-30"


class FakeGitHub(job.GitHub):
    """In-memory repo: path -> (text, sha). `conflicts` makes the next N PUTs
    to a path fail as if someone committed first (and applies `mutate`)."""

    def __init__(self, files):
        super().__init__("test-token")
        self.files = {p: (t, f"sha{i}") for i, (p, t) in enumerate(files.items())}
        self.counter = 1000
        self.conflicts = {}
        self.puts = []

    def _sha(self):
        self.counter += 1
        return f"sha{self.counter}"

    def get_file(self, path):
        return self.files.get(path, (None, None))

    def list_dir(self, path):
        names = set()
        for p in self.files:
            if p.startswith(path + "/"):
                rest = p[len(path) + 1:]
                names.add((rest.split("/")[0], "dir" if "/" in rest else "file"))
        return [{"name": n, "type": t} for n, t in sorted(names)]

    def put_file(self, path, text, sha, message):
        if self.conflicts.get(path):
            self.conflicts[path] -= 1
            cur_text, _ = self.files[path]
            self.files[path] = (cur_text, self._sha())   # someone else committed
            raise job.Conflict("HTTP 409")
        if path in self.files and self.files[path][1] != sha:
            raise job.Conflict("HTTP 409 stale sha")
        new = self._sha()
        self.files[path] = (text, new)
        self.puts.append(path)
        return new

    def cloud_run_active(self):
        return False


def video_json(source="gemini-approx", start=20):
    return {
        "video_id": VID, "video_title": "Joe Mazzulla Postgame Press Conference",
        "channel_team": "Boston Celtics", "published": "2026-09-30T23:00:00Z",
        "processed_at": "2099-01-01T00:00:00Z", "run_id": "2026-09-30T2015Z-20utc",
        "content_type": "presser",
        "quotes": [{"rank": 1, "speaker": "Joe Mazzulla", "speaker_confidence": "named",
                    "team": "Boston Celtics", "start_seconds": start, "end_seconds": start + 25,
                    "gemini_start_seconds": start, "gemini_end_seconds": start + 25,
                    "timestamp_source": source, "text": QUOTE, "news_score": 8,
                    "news_angle": "Mazzulla takes the blame", "social_post": "x"}],
    }


def caption_words():
    segs = [(0, 30, "opening remarks about the season and the schedule ahead " * 3),
            (31.2, 9.0, QUOTE)]
    return caption_align.words_from_segments(segs)


class PcJobTests(unittest.TestCase):
    def setUp(self):
        data = video_json()
        md = pe.to_markdown({"video_id": VID}, "Boston Celtics", data)
        self.gh = FakeGitHub({
            f"pressers_v2/output/{DAY}/{VID}.json": json.dumps(data, indent=2),
            f"pressers_v2/output/{DAY}/{VID}.md": md,
            f"pressers_v2/output/{DAY}/digest.md": "# old digest\n",
            f"pressers_v2/output/{DAY}/digest-20utc.md":
                f"# NBA Pressers — {DAY}\n\nSource: [https://www.youtube.com/watch?v={VID}](x)\n",
            "pressers_v2/output/latest_clips.json": json.dumps(
                {"run_slot": "20utc", "window_hours": 48, "clips": [{"publish_date": DAY}]}),
            "pressers_v2/config.json": (HERE.parent / "config.json").read_text(encoding="utf-8"),
        })
        self._fetch = mc.fetch_caption_words
        mc.fetch_caption_words = lambda vid, ffmpeg: caption_words()
        self._out = pe.OUTPUT_DIR

    def tearDown(self):
        mc.fetch_caption_words = self._fetch
        pe.OUTPUT_DIR = self._out

    def run_align(self):
        return job.align_and_commit(self.gh, pe, caption_align, mc, "/usr/bin/ffmpeg")

    def test_aligns_commits_and_mirrors(self):
        committed, manifest_path = self.run_align()
        data = json.loads(self.gh.files[f"pressers_v2/output/{DAY}/{VID}.json"][0])
        q = data["quotes"][0]
        self.assertEqual(q["timestamp_source"], "captions-pc")
        self.assertEqual((q["start_seconds"], q["gemini_start_seconds"]), (31, 20))
        md = self.gh.files[f"pressers_v2/output/{DAY}/{VID}.md"][0]
        self.assertIn("&t=31s)", md)
        self.assertNotIn("(approx.)", md)
        self.assertIn("&t=31s", self.gh.files[f"pressers_v2/output/{DAY}/digest.md"][0])
        self.assertIn("&t=31s", self.gh.files[f"pressers_v2/output/{DAY}/digest-20utc.md"][0])
        clips = json.loads(self.gh.files["pressers_v2/output/latest_clips.json"][0])["clips"]
        self.assertEqual(clips[0]["start_seconds"], 31)
        report = json.loads(self.gh.files["pressers_v2/output/pc_alignment_report.json"][0])
        self.assertEqual(report["aligned"], 1)
        self.assertEqual(report["entries"][0]["delta_seconds"], 11)
        self.assertIn("2026-09-30T2015Z-20utc", report["by_cloud_run"])
        # every committed output file is mirrored under docs/
        for path in [p for p in self.gh.puts if p.startswith("pressers_v2/output/")]:
            self.assertIn("docs/pressers_v2/" + path[len("pressers_v2/output/"):], self.gh.files)
        # JSON is committed before the files derived from it
        self.assertEqual(self.gh.puts[0], f"pressers_v2/output/{DAY}/{VID}.json")
        self.assertTrue(manifest_path.is_file())

    def test_conflict_refetches_and_reapplies_once(self):
        self.gh.conflicts[f"pressers_v2/output/{DAY}/{VID}.json"] = 1
        self.run_align()
        q = json.loads(self.gh.files[f"pressers_v2/output/{DAY}/{VID}.json"][0])["quotes"][0]
        self.assertEqual(q["timestamp_source"], "captions-pc")

    def test_second_conflict_skips(self):
        path = f"pressers_v2/output/{DAY}/{VID}.json"
        self.gh.conflicts[path] = 2
        before = self.gh.files[path][0]
        self.run_align()
        self.assertEqual(self.gh.files[path][0], before)       # untouched, skipped + logged

    def test_already_aligned_quotes_left_alone(self):
        for source in ("captions", "captions-pc"):
            data = video_json(source=source)
            self.gh.files[f"pressers_v2/output/{DAY}/{VID}.json"] = (json.dumps(data), "shaX")
            self.gh.puts.clear()
            self.run_align()
            self.assertEqual(self.gh.puts, [], source)

    def test_task_xml_and_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            xml_path, vbs = Path(tmp) / "task.xml", Path(tmp) / "run-hidden.vbs"
            old = job.cron_slots_utc
            job.cron_slots_utc = lambda gh: ["06:15", "14:15", "20:15"]
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
            self.assertIn(", 0, True", vbs.read_text())
            self.assertEqual(times, ["06:45 UTC", "14:45 UTC", "20:45 UTC"])

    def test_cron_slots_read_from_workflow(self):
        wf = (HERE.parent.parent / ".github/workflows/pressers-v2.yml").read_text(encoding="utf-8")
        self.gh.files[".github/workflows/pressers-v2.yml"] = (wf, "w1")
        self.assertEqual(job.cron_slots_utc(self.gh), ["06:15", "14:15", "20:15"])


class CloudKeepsPcTimesTests(unittest.TestCase):
    def test_reprocess_keeps_pc_alignment(self):
        old = video_json(source="captions-pc", start=31)
        new = video_json(source="gemini-approx", start=20)
        new["quotes"][0]["text"] = QUOTE.replace("on me.", "on me")   # same quote, re-extracted
        self.assertEqual(pe.preserve_pc_alignments(old, new), 1)
        q = new["quotes"][0]
        self.assertEqual((q["start_seconds"], q["timestamp_source"]), (31, "captions-pc"))

    def test_unmatched_pc_quote_is_kept(self):
        old = video_json(source="captions-pc", start=31)
        new = video_json(start=200)
        new["quotes"][0]["text"] = "A completely different answer about the rotation and minutes tonight."
        pe.preserve_pc_alignments(old, new)
        self.assertEqual(len(new["quotes"]), 2)
        self.assertEqual(new["quotes"][1]["timestamp_source"], "captions-pc")

    def test_write_outputs_preserves_on_disk_pc_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            prev = pe.OUTPUT_DIR
            pe.OUTPUT_DIR = Path(tmp)
            try:
                video = {"video_id": VID, "title": "t", "published": "2026-09-30T23:00:00Z"}
                pe.write_outputs(video, "Boston Celtics", video_json(source="captions-pc", start=31))
                pe.write_outputs(video, "Boston Celtics", video_json(source="gemini-approx", start=20))
                saved = json.loads((Path(tmp) / DAY / f"{VID}.json").read_text())
            finally:
                pe.OUTPUT_DIR = prev
        self.assertEqual(saved["quotes"][0]["timestamp_source"], "captions-pc")
        self.assertEqual(saved["quotes"][0]["start_seconds"], 31)


if __name__ == "__main__":
    unittest.main()
