"""
Offline tests for pressers_v2 (no network, no API keys, Gemini mocked).

    python -m unittest discover -s pressers_v2/tests -v
"""

import json
import re
import sys
import tempfile
import types as pytypes
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import presser_extractor as pe  # noqa: E402
import ytq_vendor as ytq  # noqa: E402
import make_presser_clips as mc  # noqa: E402

CONFIG = json.loads((HERE.parent / "config.json").read_text(encoding="utf-8"))
FIXTURES = HERE / "fixtures" / "ytq"

# The link wrappers render_markdown adds for GitHub Pages: [url](url).
LINK_WRAP_RE = re.compile(r"\[(https://www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{11}(?:&t=\d+s)?)\]\(\1\)")
SECTION_HEADERS = {"# Press conferences", "# Podcasts & shows", "# One-offs"}


def unwrap_links(md: str) -> str:
    return LINK_WRAP_RE.sub(r"\1", md)


def assert_only_link_wrappers_differ(test, ours: str, theirs: str) -> None:
    """Line by line: every line that differs is theirs with its bare URL
    wrapped as [url](url); nothing else may change."""
    a, b = ours.split("\n"), theirs.split("\n")
    test.assertEqual(len(a), len(b))
    for mine, ref in zip(a, b):
        if mine != ref:
            test.assertIn(ref.replace("Source: ", ""), mine)
            test.assertEqual(unwrap_links(mine), ref)


class TempOutput:
    """Point the pipeline (and the vendored yt-quotes code) at a temp dir."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = pe.OUTPUT_DIR
        pe.set_output_dir(Path(self.tmp.name))
        return Path(self.tmp.name)

    def __exit__(self, *exc):
        pe.set_output_dir(self.old)
        self.tmp.cleanup()


class FakeModels:
    """Video call -> yt-quotes extraction JSON; text call -> clip fields."""

    def __init__(self, extraction: dict, clip_fields: dict | None):
        self.extraction, self.clip_fields = extraction, clip_fields
        self.calls = []

    def generate_content(self, model, contents, config):
        first = contents[0]
        if hasattr(first, "file_data"):
            self.calls.append("video")
            title = contents[1].split("\n", 1)[0].replace("YouTube title: ", "")
            body = dict(self.extraction, video_title=title)
            return pytypes.SimpleNamespace(text=json.dumps(body), usage_metadata=None)
        self.calls.append("text")
        if self.clip_fields is None:
            raise RuntimeError("400 bad request")
        return pytypes.SimpleNamespace(text=json.dumps(self.clip_fields), usage_metadata=None)


def client_for(extraction, clip_fields):
    return pytypes.SimpleNamespace(models=FakeModels(extraction, clip_fields))


HARDEN = {
    "speakers_seen": ["James Harden"],
    "quotes": [
        {"rank": 1, "speaker": "James Harden", "timestamp": "01:58",
         "summary_phrase": "James Harden on Mario and Peyton's camp",
         "names_mentioned": ["Mario", "Peyton"], "excerpt": "we are in the right direction",
         "quote": "Mario has been looking good for this past few weeks. Peyton has looked good so "
                  "putting everything together obviously it is going to take some time but we are "
                  "in the right direction."},
        {"rank": 2, "speaker": "Unidentified speaker", "timestamp": "03:10",
         "summary_phrase": "a reporter on the Cavs bench", "names_mentioned": [], "excerpt": "",
         "quote": "Can you talk about the bench and how deep this group is going to be this year?"},
    ],
}
HARDEN_FIELDS = {"content_type": "presser", "quotes": [
    {"rank": 1, "speaker_confidence": "named", "news_score": 6,
     "social_post": "James Harden says the Cavs are “in the right direction” — #Cavs \U0001F525"},
    {"rank": 2, "speaker_confidence": "inferred", "news_score": 2, "social_post": "x"}]}


def video(vid, title, one_off=False, duration=359):
    v = {"video_id": vid, "url": pe.WATCH_URL_TEMPLATE.format(video_id=vid), "title": title,
         "duration": duration, "published": "2026-09-30T18:00:00Z"}
    if one_off:
        v["is_one_off"] = True
    return v


class FormatParityTests(unittest.TestCase):
    def test_vendored_renderer_reproduces_ytquotes_output(self):
        data = json.loads((FIXTURES / "6sbyI-n2yh0.json").read_text(encoding="utf-8"))
        expected = (FIXTURES / "6sbyI-n2yh0.md").read_text(encoding="utf-8")
        ours = pe.render_markdown("6sbyI-n2yh0", "NBA on NBC", data)
        assert_only_link_wrappers_differ(self, ours, expected)
        # Source line + one timestamped URL per quote block are now links
        self.assertEqual(len(LINK_WRAP_RE.findall(ours)), 1 + len(data["quotes"]))
        self.assertIn("\nSource: [https://www.youtube.com/watch?v=6sbyI-n2yh0]"
                      "(https://www.youtube.com/watch?v=6sbyI-n2yh0)\n", ours)

    def test_digest_differs_from_ytquotes_only_in_title_sections_and_links(self):
        """Our digest vs yt-quotes' own write_digest_file over yt-quotes'
        own per-video .md: only the page title, the section headers and the
        link wrappers may differ."""
        vids = (("J5unk3vEQmk", "James Harden Media Availability", False),
                ("p7o-S3hmoD4", "Chase Down Podcast Live: Media Day Reactions", False),
                ("auzyc8y-uNI", "Caleb Wilson: My First Media Day", True))
        with TempOutput() as out:
            for vid, title, one_off in vids:
                pe.process_video(client_for(HARDEN, HARDEN_FIELDS), video(vid, title, one_off),
                                 "Cleveland Cavaliers")
            ours = pe.write_digest_file("2026-09-30", "digest.md").read_text(encoding="utf-8")
            # yt-quotes' side: its to_markdown per video, its write_digest_file
            ref_root = out / "ytq"
            ytq.OUTPUT_DIR = ref_root
            try:
                (ref_root / "2026-09-30").mkdir(parents=True)
                for vid, _, _ in vids:
                    stored = json.loads((out / "2026-09-30" / f"{vid}.json").read_text(encoding="utf-8"))
                    (ref_root / "2026-09-30" / f"{vid}.md").write_text(
                        ytq.to_markdown(stored["url"], "Cleveland Cavaliers", stored), encoding="utf-8")
                theirs = ytq.write_digest_file("2026-09-30", "rotation", "digest.md",
                                               [v for v, _, _ in vids]).read_text(encoding="utf-8")
            finally:
                ytq.OUTPUT_DIR = out
        a = ours.split("\n")
        self.assertEqual(a[0], "# NBA Pressers — 2026-09-30")
        a[0] = "# HoopsHype YT Quotes — 2026-09-30"
        kept = []
        for i, line in enumerate(a):
            if line in SECTION_HEADERS:
                self.assertEqual(a[i + 1], "")
                continue
            if i and a[i - 1] in SECTION_HEADERS:
                continue      # the blank line after a section header
            kept.append(line)
        self.assertEqual(sum(1 for line in a if line in SECTION_HEADERS), 3)
        assert_only_link_wrappers_differ(self, "\n".join(kept), theirs)

    def test_prompt_is_ytquotes_plus_speaker_rule(self):
        self.assertIn('"Unidentified speaker"', ytq.PROMPT[-400:])
        self.assertTrue(ytq.PROMPT.startswith("You are watching an NBA YouTube show."))

    def test_pages_safe_only_touches_executable_sequences(self):
        data = {"video_title": "Harden | Media Day: \"it's on me\" & more",
                "quotes": [{"quote": "<script>x</script> {{ site }} {% raw %} [a](javascript:alert(1))"}]}
        safe = pe.pages_safe(data)
        self.assertEqual(safe["video_title"], data["video_title"])          # ordinary text untouched
        q = safe["quotes"][0]["quote"]
        for bad in ("<script", "{{", "{%", "javascript:"):
            self.assertNotIn(bad, q)


class PipelineTests(unittest.TestCase):
    def test_presser_end_to_end(self):
        client = client_for(HARDEN, HARDEN_FIELDS)
        with TempOutput() as out:
            status, data = pe.process_video(client, video("J5unk3vEQmk", "James Harden Media Availability"),
                                            "Cleveland Cavaliers")
            self.assertEqual(status, "ok")
            md = (out / "2026-09-30" / "J5unk3vEQmk.md").read_text(encoding="utf-8")
            # the .md is exactly what yt-quotes' renderer makes from the same quotes
            stored = json.loads((out / "2026-09-30" / "J5unk3vEQmk.json").read_text(encoding="utf-8"))
            assert_only_link_wrappers_differ(self, md, ytq.to_markdown(stored["url"], "Cleveland Cavaliers", stored))
            manifest = pe.build_clip_manifest("t", 100000)
        self.assertEqual(client.models.calls, ["video", "text"])
        self.assertEqual(data["content_type"], "presser")
        q1, q2 = data["quotes"]
        self.assertEqual((q1["start_seconds"], q1["speaker_confidence"], q1["news_score"]), (118, "named", 6))
        words = len(q1["quote"].split())
        self.assertEqual(q1["end_seconds"], 118 + round(words / pe.WORDS_PER_SECOND))
        self.assertNotIn("#", q1["social_post"])
        self.assertNotIn("—", q1["social_post"])
        self.assertEqual(q2["speaker_confidence"], "inferred")
        # unidentified speakers stay out of the clip manifest
        self.assertEqual([c["speaker"] for c in manifest["clips"]], ["James Harden"])
        clip = manifest["clips"][0]
        for key in ("clip_id", "video_id", "url", "clip_url", "start_seconds", "end_seconds",
                    "duration_seconds", "speaker", "team", "text", "news_angle", "social_post", "rank",
                    "video_title", "channel_team", "published", "publish_date", "speaker_confidence",
                    "pull_quote", "news_score", "content_type", "run_id", "processed_at"):
            self.assertIn(key, clip)
        self.assertEqual(clip["news_angle"], "James Harden on Mario and Peyton's camp")

    def test_extra_video_is_oneoff_whatever_its_content(self):
        client = client_for(HARDEN, HARDEN_FIELDS)
        with TempOutput() as out:
            status, data = pe.process_video(client, video("J5unk3vEQmk", "James Harden Media Availability",
                                                          one_off=True), "Cleveland Cavaliers")
            self.assertTrue((out / "2026-09-30" / "J5unk3vEQmk.json").is_file())   # not in oneoffs/
        self.assertEqual((data["content_type"], data["content_type_source"]), ("oneoff", "extra_videos"))

    def test_clip_field_call_failure_keeps_quotes(self):
        client = client_for(HARDEN, None)
        with TempOutput():
            status, data = pe.process_video(client, video("J5unk3vEQmk", "James Harden Media Availability"),
                                            "Cleveland Cavaliers")
        self.assertEqual(status, "ok")
        self.assertEqual(len(data["quotes"]), 2)
        self.assertEqual(data["quotes"][0]["speaker_confidence"], "named")    # title names him
        self.assertIsNone(data["quotes"][0]["news_score"])

    def test_digest_sections(self):
        with TempOutput() as out:
            for vid, title, one_off in (("J5unk3vEQmk", "James Harden Media Availability", False),
                                        ("p7o-S3hmoD4", "Chase Down Podcast Live: Media Day Reactions", False),
                                        ("auzyc8y-uNI", "Caleb Wilson: My First Media Day", True)):
                pe.process_video(client_for(HARDEN, HARDEN_FIELDS), video(vid, title, one_off), "Cleveland Cavaliers")
            digest = pe.write_digest_file("2026-09-30", "digest.md").read_text(encoding="utf-8")
            harden_md = (out / "2026-09-30" / "J5unk3vEQmk.md").read_text(encoding="utf-8").strip()
        self.assertTrue(digest.startswith("# NBA Pressers — 2026-09-30\n"))
        order = [digest.index(h) for h in ("# Press conferences", "# Podcasts & shows", "# One-offs")]
        self.assertEqual(order, sorted(order))
        self.assertIn("#" + harden_md, digest)                 # video block = yt-quotes block, h1 -> h2
        self.assertNotIn("(approx.)", digest)
        self.assertTrue(digest.rstrip().endswith(ytq.DIGEST_CLOSING_LINE))

    def test_legacy_output_migrated_to_ytq_format(self):
        legacy = {"video_id": "AAAAAAAAAA1", "video_title": "Joe Mazzulla Postgame", "channel_team": "Boston Celtics",
                  "published": "2026-09-30T10:00:00Z", "speakers_seen": ["Joe Mazzulla"],
                  "quotes": [{"rank": 1, "speaker": "Joe Mazzulla", "speaker_confidence": "named",
                              "start_seconds": 65, "end_seconds": 80, "text": "We have to be better.",
                              "news_angle": "Mazzulla wants more", "pull_quote": "We have to be better",
                              "names_mentioned": [], "news_score": 7, "timestamp_source": "gemini-approx"}]}
        with TempOutput() as out:
            day = out / "2026-09-30"
            day.mkdir()
            (day / "AAAAAAAAAA1.json").write_text(json.dumps(legacy))
            pe.migrate_legacy_outputs()
            data = json.loads((day / "AAAAAAAAAA1.json").read_text(encoding="utf-8"))
            md = (day / "AAAAAAAAAA1.md").read_text(encoding="utf-8")
        q = data["quotes"][0]
        self.assertEqual((q["timestamp"], q["summary_phrase"], q["excerpt"], q["quote"]),
                         ("01:05", "Mazzulla wants more", "We have to be better", "We have to be better."))
        self.assertEqual(data["format"], pe.FORMAT_VERSION)
        assert_only_link_wrappers_differ(self, md, ytq.to_markdown(
            pe.WATCH_URL_TEMPLATE.format(video_id="AAAAAAAAAA1"), "Boston Celtics", data))

    def test_relink_makes_stored_bare_urls_clickable_once(self):
        bare = ytq.to_markdown(pe.WATCH_URL_TEMPLATE.format(video_id="J5unk3vEQmk"), "Cleveland Cavaliers",
                               dict(HARDEN, video_title="James Harden Media Availability"))
        with TempOutput() as out:
            day = out / "2026-09-30"
            day.mkdir()
            (day / "J5unk3vEQmk.md").write_text(bare, encoding="utf-8")
            (day / "digest-manual-1118.md").write_text("# NBA Pressers — 2026-09-30\n\n#" + bare, encoding="utf-8")
            pe.relink_stored_markdown()
            once = {p.name: p.read_text(encoding="utf-8") for p in day.glob("*.md")}
            pe.relink_stored_markdown()
            twice = {p.name: p.read_text(encoding="utf-8") for p in day.glob("*.md")}
        self.assertEqual(once, twice)
        assert_only_link_wrappers_differ(self, once["J5unk3vEQmk.md"], bare)
        self.assertIn("\n[https://www.youtube.com/watch?v=J5unk3vEQmk&t=118s]"
                      "(https://www.youtube.com/watch?v=J5unk3vEQmk&t=118s)\n", once["digest-manual-1118.md"])


class ContentTypeTests(unittest.TestCase):
    def test_cavs_podcast_title(self):
        t = "Chase Down Podcast Live, presented by fubo: Media Day Reactions!"
        self.assertEqual(pe.classify_content_type(t, config=CONFIG)[0], "podcast")

    def test_availability_title(self):
        self.assertEqual(pe.classify_content_type("James Harden Media Availability", config=CONFIG)[0], "presser")

    def test_extra_videos_always_oneoff(self):
        self.assertEqual(pe.classify_content_type("James Harden Media Availability", is_one_off=True,
                                                  config=CONFIG), ("oneoff", "extra_videos"))

    def test_gemini_fallback_and_default(self):
        t = "Luka, Austin and the Squad Discuss the Season"
        self.assertEqual(pe.classify_content_type(t, gemini_value="podcast", config=CONFIG), ("podcast", "gemini"))
        self.assertEqual(pe.classify_content_type(t, config=CONFIG), ("presser", "default"))


class ClipperPickTests(unittest.TestCase):
    CLIPS = [
        {"clip_id": "a", "content_type": "presser", "news_score": 5, "run_id": "R2"},
        {"clip_id": "b", "content_type": "podcast", "news_score": 9, "run_id": "R2"},
        {"clip_id": "c", "content_type": "presser", "news_score": 8, "run_id": "R2"},
        {"clip_id": "d", "content_type": "oneoff", "news_score": 7, "run_id": "R2"},
        {"clip_id": "e", "news_score": 10, "run_id": "R1"},
    ]

    def test_grouping_and_type_filter(self):
        ordered, latest, _ = mc.order_clips({"latest_run_id": "R2"}, list(self.CLIPS))
        self.assertEqual([c["clip_id"] for c in ordered], ["c", "a", "e", "b", "d"])
        top2 = [ordered[i - 1]["clip_id"] for i in mc.default_selection(ordered, latest, 2)]
        self.assertEqual(sorted(top2), ["b", "c"])
        pressers = [ordered[i - 1]["clip_id"] for i in mc.default_selection(ordered, latest, 10, "presser")]
        self.assertEqual(sorted(pressers), ["a", "c"])


if __name__ == "__main__":
    unittest.main()
