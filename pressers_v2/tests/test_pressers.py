"""
Offline tests for pressers_v2 (no network, no API keys, Gemini mocked).

    python -m unittest discover -s pressers_v2/tests -v
"""

import json
import sys
import tempfile
import threading
import time
import types as pytypes
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import presser_extractor as pe  # noqa: E402
import make_presser_clips as mc  # noqa: E402

CONFIG = json.loads((HERE.parent / "config.json").read_text(encoding="utf-8"))


class FakeModels:
    """Returns queued answers and records the window of every call."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.windows = []

    def generate_content(self, model, contents, config):
        part = contents[0]
        vm = part.video_metadata
        self.windows.append((int(vm.start_offset.rstrip("s")), int(vm.end_offset.rstrip("s"))))
        ans = self.answers.pop(0)
        return pytypes.SimpleNamespace(text=json.dumps({"timestamp": ans}))


def fake_client(answers):
    return pytypes.SimpleNamespace(models=FakeModels(answers))


def quote(start, end):
    return {"text": "We have to be better on the defensive end and that is on me.",
            "start_seconds": start, "end_seconds": end,
            "gemini_start_seconds": start, "gemini_end_seconds": end,
            "timestamp_source": "gemini-approx"}


class RefinementTests(unittest.TestCase):
    def setUp(self):
        pe._spending_cap_hit.clear()
        pe._transient_overload_hit.clear()
        self.lock = threading.Lock()
        self.deadline = time.monotonic() + 60

    def refine(self, client, q, duration=1000):
        stats = {}
        pe.refine_quote(client, "https://www.youtube.com/watch?v=AAAAAAAAAA1", duration, q,
                        self.deadline, stats, self.lock)
        return stats

    def test_in_window_hit(self):
        client, q = fake_client(["01:05"]), quote(300, 330)
        stats = self.refine(client, q)
        self.assertEqual(client.models.windows, [(240, 360)])          # +-60s
        self.assertEqual(q["start_seconds"], 305)                      # 240 + 65
        self.assertEqual(q["end_seconds"], 335)                        # same length, shifted
        self.assertEqual(q["gemini_start_seconds"], 300)               # first pass kept
        self.assertEqual(q["timestamp_source"], "gemini-refined")
        self.assertEqual(stats["calls"], 1)
        self.assertEqual(stats["video_seconds"], 120)

    def test_not_found_then_wider_retry_hits(self):
        client, q = fake_client(["NOT_FOUND", "02:30"]), quote(300, 330)
        stats = self.refine(client, q)
        self.assertEqual(client.models.windows, [(240, 360), (120, 480)])   # +-60 then +-180
        self.assertEqual(q["start_seconds"], 270)                            # 120 + 150
        self.assertEqual(q["timestamp_source"], "gemini-refined")
        self.assertEqual(stats["not_found_60"], 1)
        self.assertEqual(stats["calls"], 2)
        self.assertEqual(stats["video_seconds"], 480)

    def test_out_of_window_rejected(self):
        client, q = fake_client(["09:00"]), quote(300, 330)     # 540s: outside 240-360 either way
        stats = self.refine(client, q)
        self.assertEqual(q["start_seconds"], 300)
        self.assertEqual(q["timestamp_source"], "gemini-approx")
        self.assertEqual(stats["out_of_window"], 1)
        self.assertEqual(len(client.models.windows), 1)       # no wider retry after a rejection

    def test_not_found_twice_keeps_first_pass(self):
        client, q = fake_client(["NOT_FOUND", "NOT_FOUND"]), quote(300, 330)
        stats = self.refine(client, q)
        self.assertEqual(q["start_seconds"], 300)
        self.assertEqual(q["timestamp_source"], "gemini-approx")
        self.assertEqual(stats["not_found_180"], 1)

    def test_window_clamped_to_video(self):
        client, q = fake_client(["00:10"]), quote(20, 40)
        self.refine(client, q, duration=50)
        self.assertEqual(client.models.windows, [(0, 50)])
        self.assertEqual(q["start_seconds"], 10)
        self.assertLessEqual(q["end_seconds"], 50)

    def test_garbage_answer_is_not_zero(self):
        client, q = fake_client(["around the middle"]), quote(300, 330)
        stats = self.refine(client, q)
        self.assertEqual(q["start_seconds"], 300)
        self.assertEqual(stats["unusable"], 1)

    def test_no_refining_after_deadline(self):
        client, q = fake_client(["01:05"]), quote(300, 330)
        self.deadline = time.monotonic() - 1
        stats = self.refine(client, q)
        self.assertEqual(client.models.windows, [])
        self.assertEqual(stats["skipped_time"], 1)

    def test_run_refinement_only_touches_approx_quotes(self):
        approx, aligned = quote(300, 330), quote(100, 120)
        aligned["timestamp_source"] = "captions"
        data = {"quotes": [approx, aligned]}
        video = {"video_id": "AAAAAAAAAA1", "url": "https://www.youtube.com/watch?v=AAAAAAAAAA1",
                 "duration": 1000, "title": "t", "published": "2026-09-30T10:00:00Z"}
        client = fake_client(["01:05"])
        with tempfile.TemporaryDirectory() as tmp:
            old = pe.OUTPUT_DIR
            pe.OUTPUT_DIR = Path(tmp)
            try:
                with ThreadPoolExecutor(max_workers=3) as ex:
                    stats = pe.run_refinement(ex, client, [{"video": video, "team": "Boston Celtics",
                                                            "data": data}], time.monotonic() + 30)
                self.assertTrue((Path(tmp) / "2026-09-30" / "AAAAAAAAAA1.json").is_file())
            finally:
                pe.OUTPUT_DIR = old
        self.assertEqual(stats["refined"], 1)
        self.assertEqual(aligned["start_seconds"], 100)
        self.assertEqual(len(client.models.windows), 1)

    def test_digest_marks_only_first_pass_times(self):
        q1, q2, q3 = quote(10, 30), quote(40, 60), quote(70, 90)
        q1["timestamp_source"], q2["timestamp_source"] = "captions", "gemini-refined"
        for q in (q1, q2, q3):
            q.update(rank=1, speaker="Joe Mazzulla", speaker_confidence="named", team="Boston Celtics")
        md = pe.to_markdown({"video_id": "AAAAAAAAAA1"}, "Boston Celtics", {"quotes": [q1, q2, q3]})
        self.assertEqual(md.count("(approx.)"), 1)
        self.assertIn("&t=70s) (approx.)", md)


class ContentTypeTests(unittest.TestCase):
    def test_cavs_podcast_title(self):
        t = "Chase Down Podcast Live, presented by fubo: Media Day Reactions!"
        self.assertEqual(pe.classify_content_type(t, config=CONFIG)[0], "podcast")

    def test_availability_title(self):
        self.assertEqual(pe.classify_content_type("James Harden Media Availability", config=CONFIG)[0], "presser")
        self.assertEqual(pe.classify_content_type(
            "Cavs Training Camp | James Harden Media Availability | 09.30.2026", config=CONFIG)[0], "presser")

    def test_extra_videos_always_oneoff(self):
        self.assertEqual(pe.classify_content_type("James Harden Media Availability", is_one_off=True,
                                                  config=CONFIG), ("oneoff", "extra_videos"))

    def test_gemini_fallback_and_default(self):
        t = "Luka, Austin and the Squad Discuss the Season"
        self.assertEqual(pe.classify_content_type(t, gemini_value="podcast", config=CONFIG), ("podcast", "gemini"))
        self.assertEqual(pe.classify_content_type(t, config=CONFIG), ("presser", "default"))

    def test_whole_words_only(self):
        # "show" must not fire inside "Showtime"
        self.assertEqual(pe.classify_content_type("Showtime Highlights", gemini_value="presser",
                                                  config=CONFIG)[1], "gemini")

    def test_extra_videos_url_becomes_oneoff_end_to_end(self):
        """An extra_videos URL goes through process_video and comes out as a
        one-off, even though its title reads like a presser."""
        title = "James Harden Media Availability"

        class Models:
            def generate_content(self, model, contents, config):
                return pytypes.SimpleNamespace(usage_metadata=None, text=json.dumps({
                    "video_title": title, "speakers_seen": ["James Harden"], "content_type": "presser",
                    "quotes": [{"rank": 1, "speaker": "James Harden", "speaker_confidence": "named",
                                "timestamp": "01:00", "end_timestamp": "01:20",
                                "text": "We are in the right direction and they will definitely help us."}]}))

        vid = pe.extract_one_off_video_id("https://youtu.be/HARDEN00001")
        video = {"video_id": vid, "url": pe.WATCH_URL_TEMPLATE.format(video_id=vid), "title": title,
                 "duration": 300, "published": "2026-09-30T10:00:00Z", "is_one_off": True}
        old_fetch, old_dir = pe.fetch_caption_words, pe.OUTPUT_DIR
        pe.fetch_caption_words = lambda v: ([], "blocked: test")
        with tempfile.TemporaryDirectory() as tmp:
            pe.OUTPUT_DIR = Path(tmp)
            try:
                status, data = pe.process_video(pytypes.SimpleNamespace(models=Models()), video, "Cleveland Cavaliers")
            finally:
                pe.fetch_caption_words, pe.OUTPUT_DIR = old_fetch, old_dir
        self.assertEqual(status, "ok")
        self.assertEqual(data["content_type"], "oneoff")
        self.assertEqual(data["content_type_source"], "extra_videos")

    def test_digest_sections_in_order_and_empty_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = pe.OUTPUT_DIR
            pe.OUTPUT_DIR = Path(tmp)
            try:
                day = Path(tmp) / "2026-09-30"
                day.mkdir()
                for vid, ctype in (("PODCAST0001", "podcast"), ("PRESSER0001", "presser")):
                    (day / f"{vid}.json").write_text(json.dumps({"content_type": ctype, "quotes": []}))
                    (day / f"{vid}.md").write_text(f"# {ctype} video\n\n**1. X** [00:10](https://y)\n")
                digest = pe.write_digest_file("2026-09-30", "digest.md").read_text()
            finally:
                pe.OUTPUT_DIR = old
        self.assertLess(digest.index("## Press conferences"), digest.index("## Podcasts & shows"))
        self.assertNotIn("## One-offs", digest)
        self.assertIn("### presser video", digest)


class ClipperPickTests(unittest.TestCase):
    CLIPS = [
        {"clip_id": "a", "content_type": "presser", "news_score": 5, "run_id": "R2"},
        {"clip_id": "b", "content_type": "podcast", "news_score": 9, "run_id": "R2"},
        {"clip_id": "c", "content_type": "presser", "news_score": 8, "run_id": "R2"},
        {"clip_id": "d", "content_type": "oneoff", "news_score": 7, "run_id": "R2"},
        {"clip_id": "e", "news_score": 10, "run_id": "R1"},          # older run, no type -> presser
    ]

    def test_grouping_and_type_filter(self):
        ordered, latest, _ = mc.order_clips({"latest_run_id": "R2"}, list(self.CLIPS))
        self.assertEqual([c["clip_id"] for c in ordered], ["c", "a", "e", "b", "d"])
        top2 = [ordered[i - 1]["clip_id"] for i in mc.default_selection(ordered, latest, 2)]
        self.assertEqual(sorted(top2), ["b", "c"])                     # overall, latest run only
        pressers = [ordered[i - 1]["clip_id"] for i in mc.default_selection(ordered, latest, 10, "presser")]
        self.assertEqual(sorted(pressers), ["a", "c"])
        self.assertEqual(mc.TYPE_FOLDERS[mc.clip_type(self.CLIPS[4])], "pressers")


if __name__ == "__main__":
    unittest.main()
