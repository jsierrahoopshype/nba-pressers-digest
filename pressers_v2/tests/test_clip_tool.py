"""
Offline tests for the clip tool: "Clip it" links, quote folders, the
speaker-following crop, speech-timed subtitles, cleanup, setup, installers.

    python -m unittest discover -s pressers_v2/tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
PV2 = HERE.parent
sys.path.insert(0, str(PV2))
# logs / tmp of the app go to a throwaway folder, never the real one
os.environ.setdefault("NBA_PRESSER_APP_DIR", tempfile.mkdtemp(prefix="npc_app_"))
os.environ.setdefault("NBA_PRESSER_LINK_WAIT", "0")

import caption_align as ca  # noqa: E402
import make_presser_clips as mc  # noqa: E402
import presser_clips_setup as setup_mod  # noqa: E402
import reframe  # noqa: E402

sys.path.insert(0, str(PV2 / "tools"))
import build_installers  # noqa: E402

try:
    import cv2
    import numpy as np
    HAVE_CV2 = True
except ImportError:
    HAVE_CV2 = False

TEXT = "Putting everything together, obviously it is going to take some time."
CLIP = {"video_id": "J5unk3vEQmk", "start_seconds": 118, "end_seconds": 126, "speaker": "James Harden",
        "team": "Cleveland Cavaliers", "publish_date": "2026-09-30", "content_type": "presser", "rank": 2,
        "news_angle": "James Harden on Mario and Peyton's camp", "text": TEXT,
        "social_post": "Harden on the Cavs", "clip_url": "https://www.youtube.com/watch?v=J5unk3vEQmk&t=118s"}
FOLDER = "2026-09-30 James Harden - on Mario and Peyton's camp"


def timed(text: str, start: float, step: float = 0.3) -> list:
    toks = ca.norm_words(text)
    return [(start + i * step, start + i * step + step * 0.9, t) for i, t in enumerate(toks)]


def located(word_level=True):
    return mc.Located(118.4, 121.9, "captions", 0.92, timed(TEXT, 118.4), word_level)


class FakeRender:
    """Stands in for yt-dlp + ffmpeg: counts downloads, records what each
    format got, writes small files."""

    def __init__(self, fail_formats=(), loc=None):
        self.downloads, self.renders, self.fail = 0, [], set(fail_formats)
        self.ass, self.crops = {}, {}
        self.loc = loc or located()

    def download(self, clip, a, b, work, ffmpeg):
        self.downloads += 1
        src = work / "src.mp4"
        src.write_bytes(b"source")
        return src

    def render(self, src, fmt, ass_name, out_tmp, work, ffmpeg, crop_chain=None):
        self.renders.append(fmt)
        self.ass[fmt] = (work / ass_name).read_text(encoding="utf-8") if ass_name else None
        self.crops[fmt] = crop_chain
        if fmt in self.fail:
            raise RuntimeError("ffmpeg failed: boom")
        out_tmp.write_bytes(f"video {fmt}".encode())

    def patch(self):
        plan = reframe.Plan(608, 1080, [100.0] * 90, [0.0] * 90, True, "face found in 18/18 samples")
        return mock.patch.multiple(mc, download_section=self.download, render_format=self.render,
                                   locate_quote=lambda clip, ffmpeg: self.loc,
                                   reframe=mock.Mock(plan_crop=lambda src, w, h: plan, forget=lambda s: None))


class TempDirs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "clips"
        self.app = Path(self.tmp.name) / "app"
        self.env = mock.patch.dict(os.environ, {"NBA_PRESSER_APP_DIR": str(self.app)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def files(self, root=None):
        root = root or self.out
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


# --------------------------------------------------------------------------- #
# "Clip it" links
# --------------------------------------------------------------------------- #

class LinkTests(unittest.TestCase):
    def test_valid_links(self):
        p = mc.parse_clip_link
        self.assertEqual(p("presserclips://clip?v=J5unk3vEQmk&t=118&q=2"), ("J5unk3vEQmk", 118, 2))
        self.assertEqual(p("presserclips://clip/?q=2&t=118&v=J5unk3vEQmk"), ("J5unk3vEQmk", 118, 2))
        self.assertEqual(p("PRESSERCLIPS://clip?v=-RVFB1Uu9QM&t=0&q=1"), ("-RVFB1Uu9QM", 0, 1))
        self.assertEqual(mc.clip_link("J5unk3vEQmk", 118, 2), "presserclips://clip?v=J5unk3vEQmk&t=118&q=2")

    def test_anything_else_is_refused(self):
        bad = [
            "", "x" * 300, "https://clip?v=J5unk3vEQmk&t=118&q=2", "presserclips://evil?v=J5unk3vEQmk&t=118&q=2",
            "presserclips://clip/x?v=J5unk3vEQmk&t=118&q=2", "presserclips://clip?v=J5unk3vEQmk&t=118",
            "presserclips://clip?v=J5unk3vEQmk&t=118&q=2&out=C:/x", "presserclips://clip?v=J5unk3vEQmk&v=x&t=1&q=2",
            "presserclips://clip?v=J5unk3vEQm&t=118&q=2", "presserclips://clip?v=J5unk3vEQmk1&t=118&q=2",
            "presserclips://clip?v=J5unk3vEQm!&t=118&q=2", "presserclips://clip?v=J5unk3vEQm%22&t=118&q=2",
            "presserclips://clip?v=J5unk3vEQmk&t=1.5&q=2", "presserclips://clip?v=J5unk3vEQmk&t=-1&q=2",
            "presserclips://clip?v=J5unk3vEQmk&t=1e3&q=2", "presserclips://clip?v=J5unk3vEQmk&t=1234567&q=2",
            "presserclips://clip?v=J5unk3vEQmk&t=118&q=0", "presserclips://clip?v=J5unk3vEQmk&t=118&q=1000",
            "presserclips://clip?v=J5unk3vEQmk&t=118&q=2#x", "presserclips://clip?v=J5unk3vEQmk&t=118&q= 2",
            'presserclips://clip?v=J5unk3vEQmk&t=118&q=2" --out "C:\\x', "presserclips://user@clip?v=J5unk3vEQmk&t=1&q=2",
            "presserclips://clip:99?v=J5unk3vEQmk&t=1&q=2", "presserclips://clip?v=J5unk3vEQmk&t=\uff11&q=2",
            "presserclips://clip?v=J5unk3vEQmk&t=118&q=2&", "presserclips://clip?v=J5unk3vEQmk;t=118;q=2", None,
        ]
        for url in bad:
            with self.assertRaises(mc.BadLink, msg=repr(url)):
                mc.parse_clip_link(url)

    def test_link_must_match_a_known_quote(self):
        clips = [dict(CLIP)]

        def offline(url):
            raise urllib.error.URLError("offline")
        self.assertEqual(mc.find_linked_quote("J5unk3vEQmk", 118, 2, clips, fetch=offline)["rank"], 2)
        self.assertIsNone(mc.find_linked_quote("J5unk3vEQmk", 118, 3, clips, fetch=offline))   # wrong number
        self.assertIsNone(mc.find_linked_quote("J5unk3vEQmk", 119, 2, clips, fetch=offline))   # wrong second

    def test_link_found_in_stored_video_data(self):
        day = mc.date.today().isoformat()
        data = {"video_title": "Media Day", "channel_team": "Chicago Bulls", "content_type": "oneoff",
                "quotes": [{"rank": 1, "speaker": "Caleb Wilson", "start_seconds": 14, "end_seconds": 20,
                            "summary_phrase": "rookie vibes", "quote": "Just with the flow."},
                           {"rank": 2, "speaker": "Unidentified speaker", "start_seconds": 34, "end_seconds": 40,
                            "summary_phrase": "x", "quote": "Something else."}]}

        def fetch(url):
            if url.endswith(f"/output/{day}/auzyc8y-uNI.json"):
                return data
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        clip = mc.find_linked_quote("auzyc8y-uNI", 34, 2, [], fetch=fetch)
        self.assertEqual((clip["rank"], clip["speaker"], clip["text"], clip["publish_date"]),
                         (2, "", "Something else.", day))
        self.assertIsNone(mc.find_linked_quote("auzyc8y-uNI", 34, 1, [], fetch=fetch))

    def test_link_mode_takes_nothing_else(self):
        with mock.patch.object(mc, "run_link", side_effect=AssertionError("must not run")):
            self.assertEqual(mc.main(["--link", "presserclips://clip?v=J5unk3vEQmk&t=118&q=2", "--out", "x"]), 1)
            self.assertEqual(mc.main(["--out", "x", "--link", "presserclips://clip?v=J5unk3vEQmk&t=118&q=2"]), 1)

    def test_bad_link_does_nothing(self):
        with mock.patch.object(mc.subprocess, "run", side_effect=AssertionError("no update on a bad link")), \
                mock.patch.object(mc, "render_all", side_effect=AssertionError("no render")):
            self.assertEqual(mc.run_link("presserclips://clip?v=J5unk3vEQmk&t=118&q=2&x=1"), 1)

    def test_good_link_renders_that_quote_in_default_formats_without_questions(self):
        seen = {}

        def fake_render_all(todo, out_root, formats, ffmpeg, pad, force):
            seen.update(todo=todo, out=out_root, formats=formats)
            return 0
        with tempfile.TemporaryDirectory() as tmp:
            settings = {"out_dir": str(Path(tmp) / "clips"), "formats": ["vertical", "square"], "keep_days": 7}
            with mock.patch.object(mc.subprocess, "run"), \
                    mock.patch("builtins.input", side_effect=AssertionError("no questions")), \
                    mock.patch.multiple(mc, load_settings=lambda path=None: settings,
                                        check_tools=lambda s=None: "ffmpeg", render_all=fake_render_all,
                                        load_manifest=lambda src: {"clips": [dict(CLIP)]},
                                        housekeeping=lambda out, days: None):
                rc = mc.main(["--link", "presserclips://clip?v=J5unk3vEQmk&t=118&q=2"])
        self.assertEqual(rc, 0)
        self.assertEqual((seen["formats"], [c["rank"] for c in seen["todo"]]), (["vertical", "square"], [2]))


# --------------------------------------------------------------------------- #
# Quote folders
# --------------------------------------------------------------------------- #

class NamingTests(TempDirs):
    def test_folder_name(self):
        self.assertEqual(mc.folder_name(CLIP), FOLDER)
        long = dict(CLIP, news_angle="James Harden explains why the Cavaliers bench depth will decide their whole "
                                     "season in the East")
        name = mc.folder_name(long)
        self.assertTrue(name.startswith("2026-09-30 James Harden - explains why"))
        self.assertLessEqual(len(name.split(" - ", 1)[1]), 48)
        self.assertFalse(name.endswith((" ", ".")))
        self.assertEqual(mc.folder_name(dict(CLIP, speaker="", news_angle="")),
                         "2026-09-30 Unnamed speaker - quote at 1m58s")

    def test_untrusted_text_stays_a_plain_folder_name(self):
        clip = dict(CLIP, publish_date="../../etc", speaker='..\\x/"y"', news_angle='a: b? *c* <d> |e|')
        folder = mc.quote_folder(self.out, clip)
        self.assertEqual(folder.parent, self.out)
        for ch in '<>:"/\\|?*':
            self.assertNotIn(ch, folder.name)
        self.assertRegex(folder.name, r"^\d{4}-\d{2}-\d{2} ")

    def test_same_name_other_quote_gets_its_own_folder(self):
        first = self.out / FOLDER
        first.mkdir(parents=True)
        (first / "quote.txt").write_text("Source: https://www.youtube.com/watch?v=OTHERvideo1&t=5s\n",
                                         encoding="utf-8")
        self.assertEqual(mc.quote_folder(self.out, CLIP).name, FOLDER + " (2)")
        (first / "quote.txt").write_text(mc.quote_txt(CLIP), encoding="utf-8")
        self.assertEqual(mc.quote_folder(self.out, CLIP), first)

    def test_quote_txt(self):
        txt = mc.quote_txt(CLIP)
        for part in ("Speaker: James Harden", "Team: Cleveland Cavaliers",
                     "Source: https://www.youtube.com/watch?v=J5unk3vEQmk&t=118s", f'"{TEXT}"',
                     "Draft social post:\nHarden on the Cavs"):
            self.assertIn(part, txt)


class RenderTests(TempDirs):
    def test_quote_folder_holds_only_the_clips_and_quote_txt(self):
        fake = FakeRender()
        with fake.patch():
            made, existing = mc.make_clip(dict(CLIP), self.out, list(mc.FORMAT_ORDER), "ffmpeg")
        self.assertEqual(fake.downloads, 1)
        self.assertEqual(self.files(), [f"{FOLDER}/quote.txt", f"{FOLDER}/square.mp4",
                                        f"{FOLDER}/vertical.mp4", f"{FOLDER}/youtube.mp4"])
        self.assertEqual([p.name for p in made], ["vertical.mp4", "youtube.mp4", "square.mp4"])
        self.assertEqual(list((self.app / "tmp").iterdir()), [])          # temp renders removed
        # crop for vertical/square, full frame for youtube
        self.assertIn("crop@rf=w=608:h=1080", fake.crops["vertical"])
        self.assertIn("crop@rf", fake.crops["square"])
        self.assertIsNone(fake.crops["youtube"])
        # second run: everything exists, nothing downloaded or rendered
        with fake.patch():
            made, existing = mc.make_clip(dict(CLIP), self.out, list(mc.FORMAT_ORDER), "ffmpeg")
        self.assertEqual((made, existing, fake.downloads), ([], ["vertical", "youtube", "square"], 1))
        # someone deleted the square one: only that is rendered again
        (self.out / FOLDER / "square.mp4").unlink()
        fake.renders.clear()
        with fake.patch():
            mc.make_clip(dict(CLIP), self.out, list(mc.FORMAT_ORDER), "ffmpeg")
        self.assertEqual((fake.renders, fake.downloads), (["square"], 2))

    def test_only_chosen_formats(self):
        with FakeRender().patch():
            mc.make_clip(dict(CLIP), self.out, ["square"], "ffmpeg")
        self.assertEqual(self.files(), [f"{FOLDER}/quote.txt", f"{FOLDER}/square.mp4"])

    def test_subtitles_are_timed_words_and_there_is_no_lower_third(self):
        fake = FakeRender()
        with fake.patch():
            mc.make_clip(dict(CLIP), self.out, ["vertical"], "ffmpeg", pad=0.5)
        ass = fake.ass["vertical"]
        self.assertNotIn("Style: Lower", ass)
        events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
        texts = [e.split(",,")[-1] for e in events]
        self.assertEqual(" ".join(texts), TEXT)                          # the quote, nothing else
        for t in texts:
            self.assertTrue(2 <= len(t.split()) <= 4, t)
        # the first caption starts when its first word is spoken: 118.4 - (118.4 - 0.5) = 0.5s in
        self.assertTrue(events[0].startswith("Dialogue: 0,0:00:00.50,"), events[0])

    def test_no_word_timing_means_no_subtitles(self):
        fake = FakeRender(loc=located(word_level=False))
        with fake.patch(), mock.patch.object(mc, "whisper_words",
                                             side_effect=mc.LowConfidence("faster-whisper isn't installed")):
            mc.make_clip(dict(CLIP), self.out, ["youtube"], "ffmpeg")
        self.assertIsNone(fake.ass["youtube"])
        self.assertTrue((self.out / FOLDER / "youtube.mp4").is_file())

    def test_whisper_word_times_when_captions_are_line_level(self):
        fake = FakeRender(loc=located(word_level=False))
        with fake.patch(), mock.patch.object(mc, "whisper_words", return_value=timed(TEXT, 118.6)):
            mc.make_clip(dict(CLIP), self.out, ["youtube"], "ffmpeg", pad=0.5)
        self.assertIn("Dialogue: 0,0:00:00.70,", fake.ass["youtube"])

    def test_failed_first_render_leaves_no_folder(self):
        fake = FakeRender(fail_formats={"vertical"})
        with fake.patch(), self.assertRaises(RuntimeError):
            mc.make_clip(dict(CLIP), self.out, ["vertical"], "ffmpeg")
        self.assertEqual(self.files(), [])
        self.assertFalse((self.out / FOLDER).exists())

    def test_failed_later_render_keeps_what_was_made(self):
        fake = FakeRender(fail_formats={"youtube"})
        with fake.patch(), self.assertRaises(RuntimeError):
            mc.make_clip(dict(CLIP), self.out, list(mc.FORMAT_ORDER), "ffmpeg")
        self.assertEqual(self.files(), [f"{FOLDER}/quote.txt", f"{FOLDER}/vertical.mp4"])

    def test_move_is_one_rename_on_the_same_drive(self):
        src = self.out.parent / "local.mp4"
        src.write_bytes(b"mine")
        final = self.out / "shared" / "vertical.mp4"
        seen = []
        real_replace = os.replace

        def spy(a, b):
            seen.append((Path(a).name, Path(b).name))
            real_replace(a, b)
        with mock.patch.object(mc.os, "replace", spy):
            self.assertTrue(mc.move_file(src, final))
        self.assertEqual(seen, [("local.mp4", "vertical.mp4")])
        self.assertEqual((final.read_bytes(), src.exists()), (b"mine", False))

    def test_move_across_drives_goes_through_a_partial_name(self):
        src = self.out.parent / "local.mp4"
        src.write_bytes(b"mine")
        final = self.out / "vertical.mp4"
        seen = []
        real_replace = os.replace

        def cross_device(a, b):
            if Path(a) == src:
                err = OSError(18, "Invalid cross-device link")
                err.winerror = 17
                raise err
            seen.append((Path(a).name, Path(b).name, Path(a).read_bytes()))
            real_replace(a, b)
        with mock.patch.object(mc.os, "replace", cross_device):
            self.assertTrue(mc.move_file(src, final))
        self.assertEqual(seen, [("vertical.mp4.partial", "vertical.mp4", b"mine")])
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["vertical.mp4"])

    def test_move_never_overwrites_someone_elses_clip(self):
        src = self.out.parent / "local.mp4"
        src.write_bytes(b"mine")
        final = self.out / "vertical.mp4"
        final.parent.mkdir(parents=True)
        final.write_bytes(b"theirs")
        self.assertFalse(mc.move_file(src, final))
        self.assertEqual((final.read_bytes(), src.exists()), (b"theirs", False))

    def test_busy_file_is_retried(self):
        calls = []

        def flaky(path):
            calls.append(path)
            if len(calls) < 3:
                err = PermissionError(13, "being used by another process")
                err.winerror = 32
                raise err
        with mock.patch.object(mc, "RETRY_WAITS", (0, 0, 0)), mock.patch.object(mc.os, "remove", flaky):
            self.assertTrue(mc.remove_file(self.out / "x"))
        self.assertEqual(len(calls), 3)


# --------------------------------------------------------------------------- #
# Formats, subtitles, reframing
# --------------------------------------------------------------------------- #

class FormatTests(unittest.TestCase):
    def test_parse_formats(self):
        allf = list(mc.FORMAT_ORDER)
        self.assertEqual(mc.parse_formats("", allf), ["vertical", "youtube", "square"])
        self.assertEqual(mc.parse_formats("", ["square"]), ["square"])
        self.assertEqual(mc.parse_formats("sv", allf), ["vertical", "square"])
        self.assertEqual(mc.parse_formats("V, Y", allf), ["vertical", "youtube"])
        self.assertEqual(mc.parse_formats("all", ["square"]), allf)
        with self.assertRaises(ValueError):
            mc.parse_formats("x", allf)

    def test_sizes_and_crop(self):
        self.assertEqual({f: (spec["size"], spec["crop"]) for f, spec in mc.FORMATS.items()},
                         {"vertical": ((1080, 1920), True), "youtube": ((1920, 1080), False),
                          "square": ((1080, 1080), True)})

    def test_no_bands_anywhere(self):
        for fmt in mc.FORMAT_ORDER:
            crop = ("sendcmd=f=c.cmd,crop@rf=w=608:h=1080:x=0:y=0,scale=1080:1920"
                    if mc.FORMATS[fmt]["crop"] else None)
            g = mc.filter_graph(fmt, "subs.ass", crop)
            for banned in ("boxblur", "overlay", "split"):
                self.assertNotIn(banned, g, fmt)
            self.assertTrue(g.endswith(",setsar=1,ass=subs.ass,format=yuv420p[v]"), g)
        self.assertIn("scale=1920:1080", mc.filter_graph("youtube", None))
        self.assertNotIn("ass=", mc.filter_graph("youtube", None))
        self.assertIn("crop=", mc.centred_crop_chain("vertical"))

    def test_ass_text_is_neutralised(self):
        ass = mc.build_ass([(10.0, 11.0, "hello {\\an8} world")], "square", 9.5, 5.0)
        self.assertIn("hello (/an8) world", ass)
        self.assertIn("Dialogue: 0,0:00:00.50,0:00:01.50,Caption", ass)


class SubtitleTests(unittest.TestCase):
    def test_quote_words_get_spoken_times_with_punctuation(self):
        words = timed("we are in the right direction mario has been great", 10.0)
        wt = ca.quote_word_times("We are in the right direction. Mario has really been great!", words, 10, 14)
        self.assertEqual([w[2] for w in wt][:6], ["We", "are", "in", "the", "right", "direction."])
        self.assertEqual(wt[0][0], 10.0)
        really = next(w for w in wt if w[2] == "really")       # missing from captions: interpolated
        has = next(w for w in wt if w[2] == "has")
        self.assertGreaterEqual(really[0], has[0])

    def test_too_few_matches_means_no_timing(self):
        self.assertEqual(ca.quote_word_times("Completely different words here",
                                             timed("nothing alike at all", 5), 5, 7), [])

    def test_chunks_are_two_to_four_words(self):
        words = timed("we are in the right direction mario has really been great", 10.0)
        wt = ca.quote_word_times("We are in the right direction. Mario has really been great!", words, 10, 14)
        chunks = ca.subtitle_chunks(wt)
        for start, end, text in chunks:
            self.assertTrue(2 <= len(text.split()) <= 4, text)
            self.assertLess(start, end)
        self.assertEqual([c[2] for c in chunks],
                         ["We are in the", "right direction.", "Mario has really", "been great!"])
        starts = [c[0] for c in chunks]
        self.assertEqual(starts, sorted(starts))

    def test_word_level_caption_detection(self):
        auto = {"events": [{"tStartMs": 0, "dDurationMs": 2000,
                            "segs": [{"utf8": "we"}, {"utf8": " are", "tOffsetMs": 400}]}]}
        manual = {"events": [{"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "We are here."}]}]}
        self.assertTrue(ca.json3_has_word_timing(json.dumps(auto)))
        self.assertFalse(ca.json3_has_word_timing(json.dumps(manual)))
        vtt = ("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nwe<00:00:01.400><c> are</c><00:00:01.700><c> here</c>\n\n"
               "00:00:03.000 --> 00:00:03.010\nwe are here\n")
        self.assertEqual([(round(a, 2), w) for a, _, w in ca.word_timings_from_vtt(vtt)],
                         [(1.0, "we"), (1.4, "are"), (1.7, "here")])
        self.assertEqual(ca.word_timings_from_vtt("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nno inline times\n"), [])


class ReframeTests(unittest.TestCase):
    def test_crop_sizes(self):
        self.assertEqual(reframe.crop_size(1920, 1080, 1080, 1920), (608, 1080))
        self.assertEqual(reframe.crop_size(1920, 1080, 1080, 1080), (1080, 1080))
        self.assertEqual(reframe.crop_size(1280, 720, 1080, 1920), (404, 720))
        self.assertEqual(reframe.crop_size(1440, 1080, 1080, 1920), (608, 1080))

    def _path(self, centers, crop=608, src=1920):
        times = [i / reframe.SAMPLE_FPS for i in range(len(centers))]
        return reframe.camera_path(times, centers, crop, src)

    def test_small_head_movements_do_not_move_the_camera(self):
        import random
        rnd = random.Random(1)
        cam = self._path([900 + rnd.uniform(-25, 25) for _ in range(60)])
        self.assertLess(max(cam) - min(cam), 3)                         # no jitter

    def test_walking_speaker_is_followed_smoothly(self):
        centers = [500] * 12 + [500 + 40 * i for i in range(20)] + [1300] * 30
        cam = self._path(centers)
        steps = [b - a for a, b in zip(cam, cam[1:])]
        self.assertTrue(all(s >= -1 for s in steps))                    # never swings back
        self.assertLessEqual(max(steps), reframe.MAX_PAN_PER_SEC * 608 / reframe.SAMPLE_FPS + 1)
        self.assertAlmostEqual(cam[-1] + 304, 1300, delta=0.12 * 608)    # ends with the face in frame

    def test_camera_cut_is_a_cut_not_a_pan(self):
        cam = self._path([400] * 20 + [1600] * 20)
        self.assertLess(cam[19] + 304, 700)
        self.assertGreater(cam[21] + 304, 1400)                         # there within a few frames

    def test_gaps_hold_the_last_position_and_no_face_means_centre(self):
        samples = [(0.0, (400, 500)), (0.2, None), (0.4, None), (0.6, (420, 500))]
        _, xs, _, found = reframe.fill_gaps(samples, 1920, 1080)
        self.assertEqual((xs, found), ([400, 400, 400, 420], True))
        _, xs, ys, found = reframe.fill_gaps([(0.0, None), (0.2, None)], 1920, 1080)
        self.assertEqual((xs, ys, found), ([960, 960], [540, 540], False))

    def test_missing_face_tracking_falls_back_to_a_centred_crop(self):
        with mock.patch.object(reframe, "detect_samples", side_effect=ImportError("no cv2")), \
                mock.patch.object(reframe, "probe_size", lambda v: (1920, 1080, 2.0)):
            plan = reframe.plan_crop(Path("nowhere.mp4"), 1080, 1920)
        reframe.forget(Path("nowhere.mp4"))
        self.assertFalse(plan.tracked)
        self.assertEqual((plan.cw, plan.ch, round(plan.xs[0])), (608, 1080, 656))

    def test_sendcmd_has_one_line_per_change(self):
        plan = reframe.Plan(608, 1080, [10.0, 10.0, 12.0], [0.0, 0.0, 0.0], True, "")
        self.assertEqual(plan.sendcmd(), "0.000 crop@rf x 10;\n0.067 crop@rf x 12;\n")
        self.assertEqual(plan.filter("c.cmd", 1080, 1920),
                         "sendcmd=f=c.cmd,crop@rf=w=608:h=1080:x=10:y=0,scale=1080:1920:flags=lanczos")

    @unittest.skipUnless(HAVE_CV2, "OpenCV not installed")
    def test_real_face_detection_follows_the_face(self):
        face = cv2.imread(str(HERE / "fixtures" / "face" / "mona-lisa.jpg"))
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "moving.avi"
            out = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"MJPG"), 30, (1280, 720))
            for i in range(150):                       # still 1s, walks 1.7s, still 3.3s
                x = 100 if i < 30 else min(850, 100 + (i - 30) * 15)
                frame = np.full((720, 1280, 3), (115, 77, 38), np.uint8)
                frame[150:150 + face.shape[0], x:x + face.shape[1]] = face
                out.write(frame)
            out.release()
            plan = reframe.plan_crop(video, 1080, 1920)
            reframe.forget(video)
        self.assertTrue(plan.tracked, plan.note)
        face_start, face_end = 100 + 150, 850 + 150                      # face centre, first / last frame
        self.assertLess(abs(plan.xs[5] + plan.cw / 2 - face_start), 0.15 * plan.cw)
        self.assertLess(abs(plan.xs[-1] + plan.cw / 2 - face_end), 0.15 * plan.cw)


# --------------------------------------------------------------------------- #
# Cleanup and housekeeping
# --------------------------------------------------------------------------- #

def make_file(path: Path, age_days: float, size: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    t = time.time() - age_days * 86400
    os.utime(path, (t, t))
    return path


class CleanupTests(TempDirs):
    def test_keep_days(self):
        self.assertEqual(mc.keep_days({}), 0)                          # no installer settings: never delete
        self.assertEqual(mc.keep_days({"out_dir": "x"}), 7)
        self.assertEqual(mc.keep_days({"out_dir": "x", "keep_days": 0}), 0)

    def test_whole_quote_folders_go_when_old(self):
        old = self.out / "2026-09-20 Joe Mazzulla - on defense"
        for name, size in (("vertical.mp4", 1000), ("square.mp4", 500), ("quote.txt", 24)):
            make_file(old / name, 9, size)
        young = self.out / "2026-09-30 James Harden - on Mario"
        make_file(young / "vertical.mp4", 9)                            # old file in a folder still in use
        make_file(young / "square.mp4", 1)
        legacy = make_file(self.out / "2026-09-20" / "pressers" / "x_vertical.mp4", 8, 7)
        n, freed = mc.run_cleanup(self.out, 7)
        self.assertFalse(old.exists())
        self.assertEqual(sorted(p.name for p in young.iterdir()), ["square.mp4", "vertical.mp4"])
        self.assertFalse(legacy.exists() or (self.out / "2026-09-20").exists())
        self.assertEqual((n, freed), (2, 1000 + 500 + 24 + 7))
        log = (self.app / "logs" / "cleanup-log.txt").read_text(encoding="utf-8")
        self.assertIn("deleted 1 quote folder(s) older than 7 days", log)

    def test_zero_days_and_unsafe_folders_delete_nothing(self):
        old = make_file(self.out / "2026-09-01 A - b" / "vertical.mp4", 100)
        self.assertEqual(mc.run_cleanup(self.out, 0), (0, 0))
        with mock.patch.object(mc, "unsafe_clips_folder", lambda p: "that folder holds other files"):
            self.assertEqual(mc.run_cleanup(self.out, 7)[0], 0)
        self.assertTrue(old.exists())

    @unittest.skipIf(os.name == "nt", "symlinks need extra rights on Windows")
    def test_cleanup_never_follows_a_link_out_of_the_folder(self):
        outside = make_file(Path(self.tmp.name) / "elsewhere" / "precious.mp4", 30)
        self.out.mkdir(parents=True)
        os.symlink(outside.parent, self.out / "2026-09-01 Link - x")
        os.symlink(outside.parent, self.out / "link")
        mc.run_cleanup(self.out, 7)
        self.assertTrue(outside.exists())

    def test_tidy_moves_old_notes_and_logs_but_not_quote_txt(self):
        make_file(self.out / "2026-09-30 James Harden - on Mario" / "quote.txt", 1)
        make_file(self.out / "2026-09-30" / "pressers" / "b.txt", 1)
        make_file(self.out / "pc-job-log.txt", 1)
        self.assertEqual(mc.tidy_clips_folder(self.out), 2)
        self.assertEqual(self.files(), ["2026-09-30 James Harden - on Mario/quote.txt"])
        self.assertTrue((self.app / "notes" / "2026-09-30" / "b.txt").is_file())
        self.assertTrue((self.app / "logs" / "pc-job-log.txt").is_file())


class MainTests(unittest.TestCase):
    def test_settings_choose_folder_and_formats_for_unattended_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "settings.json").write_text(json.dumps({"out_dir": str(tmp / "Shared clips"),
                                                           "formats": ["square", "vertical"]}))
            (tmp / "m.json").write_text(json.dumps({"latest_run_id": "R2", "clips": [
                dict(CLIP, run_id="R2", news_score=7), dict(CLIP, start_seconds=200, end_seconds=210,
                                                            run_id="R1", news_score=9)]}))
            seen = {}

            def fake_render_all(todo, out_root, formats, ffmpeg, pad, force):
                seen.update(todo=todo, out=out_root, formats=formats)
                return 0
            with mock.patch.multiple(mc, check_tools=lambda s=None: "ffmpeg", render_all=fake_render_all):
                rc = mc.main(["--yes", "--settings", str(tmp / "settings.json"), "--manifest", str(tmp / "m.json")])
        self.assertEqual(rc, 0)
        self.assertEqual(seen["out"], tmp / "Shared clips")
        self.assertEqual(seen["formats"], ["vertical", "square"])
        self.assertEqual([c["start_seconds"] for c in seen["todo"]], [118])      # latest run only

    def test_default_folder_is_the_users_home(self):
        self.assertEqual(mc.default_out_dir(), Path.home() / "Documents" / "presser-clips")


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #

class SetupTests(unittest.TestCase):
    def _setup(self, tmp: Path, answers: list):
        answers = iter(answers)
        calls = {}
        with mock.patch.object(setup_mod, "APP_DIR", tmp), \
                mock.patch.object(setup_mod, "ask", lambda prompt: next(answers)), \
                mock.patch.multiple(setup_mod, make_desktop_shortcut=lambda: "desktop",
                                    register_link=lambda: calls.setdefault("link", "registered"),
                                    disable_auto=lambda: calls.setdefault("auto_off", "")), \
                mock.patch.object(mc, "find_ffmpeg", lambda s=None: "/x/ffmpeg"):
            self.assertEqual(setup_mod.setup(), 0)
        return json.loads((tmp / "settings.json").read_text(encoding="utf-8")), calls

    def test_three_questions_and_no_automatic_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            target = tmp / "My Drive" / "Social & clips"
            settings, calls = self._setup(tmp, [f'"{target}"', "14", "vs"])
            self.assertEqual(list(target.iterdir()), [])          # the write test left nothing behind
        self.assertEqual((settings["out_dir"], settings["keep_days"], settings["formats"], settings["auto_run"]),
                         (str(target), 14, ["vertical", "square"], False))
        self.assertEqual(set(calls), {"link", "auto_off"})        # links registered, old task removed

    def test_enter_takes_the_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with mock.patch.object(mc, "default_out_dir", lambda: tmp / "Documents" / "presser-clips"):
                settings, _ = self._setup(tmp, ["", "", ""])
        self.assertEqual(settings["out_dir"], str(tmp / "Documents" / "presser-clips"))
        self.assertEqual((settings["keep_days"], settings["formats"]), (7, ["vertical", "youtube", "square"]))

    def test_folder_check_closes_its_test_file_and_accepts_an_undeletable_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "C presser-clips ñ"
            self.assertEqual(setup_mod.check_folder(folder), "")
            self.assertEqual(list(folder.iterdir()), [])
            with mock.patch.object(mc, "remove_file", lambda p: False):    # delete keeps failing
                self.assertEqual(setup_mod.check_folder(folder), "")
            self.assertNotEqual(setup_mod.check_folder(Path("relative/clips")), "")

    def test_folders_with_other_files_are_refused(self):
        home = Path.home()
        for bad in (home, home / "Documents", home / "Desktop", Path(home.anchor),
                    home / "Library" / "CloudStorage" / "GoogleDrive-x" / "My Drive"):
            self.assertIn("other files" if bad != Path(home.anchor) else "drive",
                          setup_mod.check_folder(bad), str(bad))

    def test_windows_link_command_passes_the_link_as_one_argument(self):
        py, tool = Path("App") / "venv" / "python.exe", Path("App") / "make_presser_clips.py"
        self.assertEqual(setup_mod.link_command(py, tool), f'"{py}" "{tool}" --link "%1"')

    def test_mac_app_declares_the_link_type_and_quotes_the_link(self):
        info = setup_mod.add_url_type({"CFBundleExecutable": "applet"})
        self.assertEqual(info["CFBundleURLTypes"][0]["CFBundleURLSchemes"], ["presserclips"])
        self.assertIn("quoted form of theURL", setup_mod.MAC_HANDLER)
        self.assertIn("--link", setup_mod.MAC_HANDLER)

    def test_disable_auto_turns_off_an_older_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "settings.json").write_text(json.dumps({"out_dir": "x", "auto_run": True}))
            with mock.patch.object(setup_mod, "APP_DIR", tmp), \
                    mock.patch.multiple(setup_mod, IS_WINDOWS=False, IS_MAC=False):
                setup_mod.disable_auto()
            self.assertFalse(json.loads((tmp / "settings.json").read_text())["auto_run"])

    def test_update_checks_the_model_and_python_files(self):
        class Resp:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self.body
        bodies = {name: (b"print('ok')\n" if name.endswith(".py") else b"tampered")
                  for name in setup_mod.UPDATE_FILES}
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with mock.patch.object(setup_mod, "APP_DIR", tmp), \
                    mock.patch.object(setup_mod.urllib.request, "urlopen",
                                      lambda req, **kw: Resp(bodies[req.full_url.split("/pressers_v2/", 1)[1]])), \
                    mock.patch.object(setup_mod, "ensure_packages", lambda: None):
                changed = setup_mod.update()
            self.assertEqual(changed, len(setup_mod.UPDATE_FILES) - 1)
            self.assertFalse((tmp / setup_mod.MODEL_FILE).exists())      # failed its checksum
            self.assertTrue((tmp / "reframe.py").is_file())


class InstallerTests(unittest.TestCase):
    SHIPPED = ["make_presser_clips.py", "caption_align.py", "reframe.py", "presser_pc_job.py",
               "presser_clips_setup.py", "install-presser-clips.bat", "run-presser-clips.bat",
               "install-presser-clips-mac.command", "run-presser-clips-mac.command", "CLIPS-GUIDE.md"]

    def test_no_hardcoded_user_paths_or_tokens(self):
        for name in self.SHIPPED:
            text = (PV2 / name).read_text(encoding="utf-8")
            for bad in ("Jorge", "C:\\Users", "/Users/", "/home/", "Authorization", "ghp_", "github_pat_"):
                self.assertNotIn(bad, text, f"{name} contains {bad!r}")

    def test_line_endings_and_exec_bits(self):
        for name in ("install-presser-clips.bat", "run-presser-clips.bat"):
            raw = (PV2 / name).read_bytes()
            self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"), name)
        for name in ("install-presser-clips-mac.command", "run-presser-clips-mac.command"):
            raw = (PV2 / name).read_bytes()
            self.assertNotIn(b"\r", raw, name)
            self.assertTrue(raw.startswith(b"#!/bin/bash\n"), name)
            if os.name != "nt":
                self.assertTrue(os.access(PV2 / name, os.X_OK), name)
                res = subprocess.run(["bash", "-n", str(PV2 / name)], capture_output=True, text=True)
                self.assertEqual(res.returncode, 0, res.stderr)

    def test_installers_fetch_every_file_the_app_updates(self):
        win = (PV2 / "install-presser-clips.bat").read_text(encoding="utf-8")
        mac = (PV2 / "install-presser-clips-mac.command").read_text(encoding="utf-8")
        for name in setup_mod.UPDATE_FILES:
            self.assertIn(name.replace("/", "\\") if name.startswith("models/") else name, win)
            self.assertIn(name, mac)
            self.assertTrue((PV2 / name).is_file())
        self.assertIn("opencv-python-headless", win)
        self.assertIn("opencv-python-headless", mac)
        self.assertNotIn("presser_pc_job", win + mac)                    # automatic mode is retired

    def test_shipped_model_matches_its_checksum(self):
        import hashlib
        self.assertEqual(hashlib.sha256((PV2 / setup_mod.MODEL_FILE).read_bytes()).hexdigest(),
                         setup_mod.MODEL_SHA256)

    def test_pages_downloads_match_the_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = build_installers.build(Path(tmp))
            for path in built:
                published = PV2.parent / "docs" / "presser-clips" / path.name
                self.assertEqual(path.read_bytes(), published.read_bytes(),
                                 f"{published.name} is stale: run python pressers_v2/tools/build_installers.py")


if __name__ == "__main__":
    unittest.main()
