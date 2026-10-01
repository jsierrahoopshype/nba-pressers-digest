"""
Offline tests for the clip tool: formats and layouts, one download per quote,
skip-if-exists, atomic writes, pasted links, settings, installers.

    python -m unittest discover -s pressers_v2/tests -v
"""

import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
PV2 = HERE.parent
sys.path.insert(0, str(PV2))
# notes / logs / tmp of the app go to a throwaway folder, never the real one
os.environ.setdefault("NBA_PRESSER_APP_DIR", tempfile.mkdtemp(prefix="npc_app_"))

import make_presser_clips as mc  # noqa: E402
import presser_clips_setup as setup_mod  # noqa: E402

sys.path.insert(0, str(PV2 / "tools"))
import build_installers  # noqa: E402

CLIP = {"video_id": "J5unk3vEQmk", "start_seconds": 118, "end_seconds": 126, "speaker": "James Harden",
        "team": "Cleveland Cavaliers", "publish_date": "2026-09-30", "content_type": "presser",
        "text": "Putting everything together obviously it is going to take some time.",
        "social_post": "Harden on the Cavs", "clip_url": "https://www.youtube.com/watch?v=J5unk3vEQmk&t=118s"}


class FakeRender:
    """Stands in for yt-dlp + ffmpeg: counts downloads, writes small files."""

    def __init__(self, fail_formats=()):
        self.downloads, self.renders, self.fail = 0, [], set(fail_formats)

    def download(self, clip, a, b, work, ffmpeg):
        self.downloads += 1
        src = work / "src.mp4"
        src.write_bytes(b"source")
        return src

    def render(self, src, fmt, ass_name, out_tmp, work, ffmpeg):
        self.renders.append(fmt)
        self.assertion_ass = (work / ass_name).read_text(encoding="utf-8")
        if fmt in self.fail:
            raise RuntimeError("ffmpeg failed: boom")
        out_tmp.write_bytes(f"video {fmt}".encode())

    def patch(self):
        return mock.patch.multiple(mc, download_section=self.download, render_format=self.render,
                                   locate_quote=lambda clip, ffmpeg: (118.4, 125.9, "captions", 0.92))


class FormatTests(unittest.TestCase):
    def test_parse_formats(self):
        allf = list(mc.FORMAT_ORDER)
        self.assertEqual(mc.parse_formats("", allf), ["vertical", "youtube", "square"])
        self.assertEqual(mc.parse_formats("", ["square"]), ["square"])
        self.assertEqual(mc.parse_formats("sv", allf), ["vertical", "square"])
        self.assertEqual(mc.parse_formats("V, Y", allf), ["vertical", "youtube"])
        self.assertEqual(mc.parse_formats("all", ["square"]), allf)
        self.assertEqual(mc.parse_formats("youtube", allf), ["youtube"])
        with self.assertRaises(ValueError):
            mc.parse_formats("x", allf)

    def test_sizes(self):
        self.assertEqual(mc.FORMATS["vertical"]["size"], (1080, 1920))
        self.assertEqual(mc.FORMATS["youtube"]["size"], (1920, 1080))
        self.assertEqual(mc.FORMATS["square"]["size"], (1080, 1080))

    def test_overlays_stay_off_the_picture_and_inside_the_frame(self):
        for fmt in ("vertical", "square"):
            L = mc.FORMATS[fmt]
            w, h = L["size"]
            bx, by, bw, bh = L["box"]
            self.assertEqual(bh, round(bw * 9 / 16))                       # the 16:9 frame, uncropped
            lt_bottom = L["lt_pos"][1] + mc.LT_BOX_PAD
            lt_top = L["lt_pos"][1] - L["lt_size"] * 1.25 - L["lt_team_size"] * 1.25 - mc.LT_BOX_PAD
            self.assertLessEqual(lt_bottom, by, fmt)                       # lower third above the picture
            self.assertGreaterEqual(lt_top, 0, fmt)
            self.assertGreaterEqual(L["cap_margin_v"], by + bh, fmt)       # captions below it
            cap_bottom = L["cap_margin_v"] + 2 * L["cap_size"] * 1.25 + L["cap_outline"]
            self.assertLessEqual(cap_bottom, h, fmt)
        # vertical captions stay above the bottom area Reels/TikTok cover
        V = mc.FORMATS["vertical"]
        self.assertLessEqual(V["cap_margin_v"] + 2 * V["cap_size"] * 1.25, 1920 - 370)
        Y = mc.FORMATS["youtube"]
        self.assertEqual(Y["box"], (0, 0, 1920, 1080))
        self.assertEqual((Y["lt_align"], Y["cap_align"]), (7, 2))            # top-left, bottom-centre
        self.assertLess(Y["lt_pos"][1] + Y["lt_size"] * 2.5, 1080 * 0.2)

    def test_filter_graph_fits_the_frame_without_cropping_it(self):
        for fmt in mc.FORMAT_ORDER:
            g = mc.filter_graph(fmt, "captions.ass")
            fg = g.split("[fg]", 2)[2]
            self.assertIn("force_original_aspect_ratio=decrease", fg)
            self.assertNotIn("crop", fg)
            w, h = mc.FORMATS[fmt]["size"]
            self.assertIn(f"scale={w}:{h},", g)

    def test_ass_per_format_and_untrusted_text_neutralised(self):
        clip = dict(CLIP, speaker="Bad {\\pos(0,0)} Name", text="hello {\\an8} world " * 5)
        for fmt in mc.FORMAT_ORDER:
            ass = mc.build_ass(clip, 0.5, 8.0, 9.0, fmt)
            w, h = mc.FORMATS[fmt]["size"]
            self.assertIn(f"PlayResX: {w}\nPlayResY: {h}", ass)
            events = ass.split("[Events]")[1]
            self.assertNotIn("{\\pos(0,0)}", events)
            self.assertNotIn("{\\an8}", events)
            self.assertIn("BAD (/POS(0,0)) NAME", events)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "clips"
        self.app = Path(self.tmp.name) / "app"
        self.env = mock.patch.dict(os.environ, {"NBA_PRESSER_APP_DIR": str(self.app)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def files(self):
        return sorted(str(p.relative_to(self.out)).replace(os.sep, "/") for p in self.out.rglob("*") if p.is_file())

    def test_one_download_renders_every_format_then_skips_existing(self):
        fake = FakeRender()
        with fake.patch():
            made, existing = mc.make_clip(dict(CLIP), self.out, list(mc.FORMAT_ORDER), "ffmpeg")
        self.assertEqual(fake.downloads, 1)
        self.assertEqual(existing, [])
        base = "2026-09-30/pressers/2026-09-30_cleveland-cavaliers_james-harden_J5unk3vEQmk-118s"
        # the clips folder holds the finished clips and nothing else
        self.assertEqual(self.files(), [f"{base}_square.mp4", f"{base}_vertical.mp4", f"{base}_youtube.mp4"])
        # the post text sits in the app folder, same base name, by date
        note = self.app / "notes" / "2026-09-30" / (base.rsplit("/", 1)[1] + ".txt")
        self.assertIn("Harden on the Cavs", note.read_text(encoding="utf-8"))
        # and the temp renders are gone
        self.assertEqual(list((self.app / "tmp").iterdir()), [])
        self.assertEqual([p.name.rsplit("_", 1)[1] for p in made], ["vertical.mp4", "youtube.mp4", "square.mp4"])
        # second run: everything exists, nothing downloaded or rendered
        with fake.patch():
            made, existing = mc.make_clip(dict(CLIP), self.out, list(mc.FORMAT_ORDER), "ffmpeg")
        self.assertEqual((made, existing, fake.downloads), ([], ["vertical", "youtube", "square"], 1))
        # someone deleted the square one: only that is rendered again
        (self.out / f"{base}_square.mp4").unlink()
        fake.renders.clear()
        with fake.patch():
            made, existing = mc.make_clip(dict(CLIP), self.out, list(mc.FORMAT_ORDER), "ffmpeg")
        self.assertEqual((fake.renders, existing, fake.downloads), (["square"], ["vertical", "youtube"], 2))

    def test_failed_render_leaves_no_partial_file(self):
        fake = FakeRender(fail_formats={"youtube"})
        with fake.patch(), self.assertRaises(RuntimeError):
            mc.make_clip(dict(CLIP), self.out, list(mc.FORMAT_ORDER), "ffmpeg")
        names = self.files()
        self.assertTrue(any(n.endswith("_vertical.mp4") for n in names))
        self.assertFalse(any(n.endswith("_youtube.mp4") or n.endswith(".partial") for n in names))

    def test_move_is_one_rename_on_the_same_drive(self):
        src = self.out.parent / "local.mp4"
        src.write_bytes(b"mine")
        final = self.out / "shared" / "clip_vertical.mp4"
        seen = []
        real_replace = os.replace

        def spy(a, b):
            seen.append((Path(a).name, Path(b).name))
            real_replace(a, b)
        with mock.patch.object(mc.os, "replace", spy):
            self.assertTrue(mc.move_file(src, final))
        self.assertEqual(seen, [("local.mp4", "clip_vertical.mp4")])
        self.assertEqual((final.read_bytes(), src.exists()), (b"mine", False))

    def test_move_across_drives_goes_through_a_partial_name(self):
        src = self.out.parent / "local.mp4"
        src.write_bytes(b"mine")
        final = self.out / "clip_vertical.mp4"
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
        self.assertEqual(seen, [("clip_vertical.mp4.partial", "clip_vertical.mp4", b"mine")])
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["clip_vertical.mp4"])
        self.assertFalse(src.exists())

    def test_move_never_overwrites_someone_elses_clip(self):
        src = self.out.parent / "local.mp4"
        src.write_bytes(b"mine")
        final = self.out / "clip_vertical.mp4"
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

        def always_busy(path):
            err = PermissionError(13, "being used by another process")
            err.winerror = 32
            raise err
        with mock.patch.object(mc, "RETRY_WAITS", (0, 0)), mock.patch.object(mc.os, "remove", always_busy):
            self.assertFalse(mc.remove_file(self.out / "x"))

    def test_untrusted_dates_and_names_stay_inside_the_folder(self):
        clip = dict(CLIP, publish_date="../../etc", team="../..", speaker="..\\x")
        path = mc.output_path(self.out, clip, "vertical")
        self.assertTrue(str(path.resolve()).startswith(str(self.out.resolve())))
        self.assertNotIn("..", path.relative_to(self.out).as_posix())


def make_file(path: Path, age_days: float, size: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    t = time.time() - age_days * 86400
    os.utime(path, (t, t))
    return path


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "clips"
        self.app = Path(self.tmp.name) / "app"
        self.env = mock.patch.dict(os.environ, {"NBA_PRESSER_APP_DIR": str(self.app)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_keep_days(self):
        self.assertEqual(mc.keep_days({}), 0)                          # no installer settings: never delete
        self.assertEqual(mc.keep_days({"out_dir": "x"}), 7)
        self.assertEqual(mc.keep_days({"out_dir": "x", "keep_days": 0}), 0)
        self.assertEqual(mc.keep_days({"out_dir": "x", "keep_days": "30"}), 30)

    def test_old_files_and_empty_folders_go_new_ones_stay(self):
        old = make_file(self.out / "2026-09-20" / "pressers" / "a_vertical.mp4", 9, 1000)
        stray = make_file(self.out / "2026-09-20" / "random.docx", 8, 24)
        new = make_file(self.out / "2026-09-30" / "pressers" / "b_vertical.mp4", 1)
        old_note = make_file(self.app / "notes" / "2026-09-20" / "a.txt", 9)
        new_note = make_file(self.app / "notes" / "2026-09-30" / "b.txt", 1)
        old_tmp = make_file(self.app / "tmp" / "render_x" / "src.mp4", 8)
        n, freed = mc.run_cleanup(self.out, 7)
        self.assertEqual((n, freed), (4, 1000 + 24 + 10 + 10))
        for gone in (old, stray, old_note, old_tmp):
            self.assertFalse(gone.exists(), gone)
        self.assertTrue(new.exists() and new_note.exists())
        self.assertFalse((self.out / "2026-09-20").exists())            # empty subfolders removed
        self.assertTrue(self.out.is_dir())                              # never the clips folder itself
        log = (self.app / "logs" / "cleanup-log.txt").read_text(encoding="utf-8")
        self.assertIn("Cleanup: deleted 2 file(s) older than 7 days from the clips folder", log)
        self.assertIn("a_vertical.mp4", log)

    @unittest.skipIf(os.name == "nt", "symlinks need extra rights on Windows")
    def test_cleanup_never_follows_a_link_out_of_the_folder(self):
        outside = make_file(Path(self.tmp.name) / "elsewhere" / "precious.mp4", 30)
        self.out.mkdir(parents=True)
        os.symlink(outside.parent, self.out / "link")
        mc.run_cleanup(self.out, 7)
        self.assertTrue(outside.exists())

    def test_zero_days_and_unsafe_folders_delete_nothing(self):
        old = make_file(self.out / "a.mp4", 100)
        self.assertEqual(mc.run_cleanup(self.out, 0), (0, 0))
        self.assertTrue(old.exists())
        with mock.patch.object(mc, "unsafe_clips_folder", lambda p: "that folder holds other files"):
            self.assertEqual(mc.cleanup_old_files(self.out, 7, "x"), (0, 0))
        self.assertTrue(old.exists())

    def test_notes_and_logs_move_out_of_the_clips_folder(self):
        clip = make_file(self.out / "2026-09-30" / "pressers" / "b_vertical.mp4", 1)
        note = make_file(self.out / "2026-09-30" / "pressers" / "b.txt", 1)
        make_file(self.out / "pc-job-log.txt", 1)
        make_file(self.out / "_made.json", 1)
        self.assertEqual(mc.tidy_clips_folder(self.out), 3)
        files = sorted(p.relative_to(self.out).as_posix() for p in self.out.rglob("*") if p.is_file())
        self.assertEqual(files, ["2026-09-30/pressers/b_vertical.mp4"])
        self.assertTrue((self.app / "notes" / "2026-09-30" / "b.txt").is_file())
        self.assertTrue((self.app / "logs" / "pc-job-log.txt").is_file())
        self.assertTrue((self.app / "logs" / "_made.json").is_file())
        self.assertTrue(clip.exists() and not note.exists())

    def test_main_tidies_and_cleans_before_rendering(self):
        make_file(self.out / "2026-09-01" / "pressers" / "old_vertical.mp4", 30)
        make_file(self.out / "2026-09-30" / "pressers" / "b.txt", 1)
        settings = Path(self.tmp.name) / "settings.json"
        settings.write_text(json.dumps({"out_dir": str(self.out), "keep_days": 7}))
        manifest = Path(self.tmp.name) / "m.json"
        manifest.write_text(json.dumps({"clips": []}))
        with mock.patch.object(mc, "check_tools", lambda s=None: "ffmpeg"):
            mc.main(["--yes", "--settings", str(settings), "--manifest", str(manifest)])
        self.assertEqual([p for p in self.out.rglob("*") if p.is_file()], [])
        self.assertTrue((self.app / "notes" / "2026-09-30" / "b.txt").is_file())


class UrlTests(unittest.TestCase):
    def test_parse_youtube_url(self):
        p = mc.parse_youtube_url
        self.assertEqual(p("https://www.youtube.com/watch?v=J5unk3vEQmk&t=118s"), ("J5unk3vEQmk", 118))
        self.assertEqual(p('"https://youtu.be/J5unk3vEQmk?t=1m58s"'), ("J5unk3vEQmk", 118))
        self.assertEqual(p("https://www.youtube.com/watch?t=118&v=J5unk3vEQmk"), ("J5unk3vEQmk", 118))
        self.assertEqual(p("https://www.youtube.com/watch?v=J5unk3vEQmk"), ("J5unk3vEQmk", None))
        self.assertEqual(p("1,3,5-7"), (None, None))

    def test_link_in_clip_list(self):
        clips = [dict(CLIP), dict(CLIP, start_seconds=300, end_seconds=310)]
        clip, why = mc.find_quote_for_url("https://www.youtube.com/watch?v=J5unk3vEQmk&t=119s", clips,
                                          fetch=lambda url: self.fail("no fetch needed"))
        self.assertEqual((clip["start_seconds"], why), (118, ""))

    def test_link_found_in_stored_video_json(self):
        data = {"video_title": "Media Day", "channel_team": "Chicago Bulls", "content_type": "oneoff",
                "quotes": [{"speaker": "Unidentified speaker", "start_seconds": 14, "end_seconds": 20,
                            "summary_phrase": "rookie vibes", "quote": "Just with the flow."}]}
        day = mc.date.today().isoformat()

        def fetch(url):
            if url.endswith(f"/output/{day}/auzyc8y-uNI.json"):
                return data
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        clip, why = mc.find_quote_for_url("https://www.youtube.com/watch?v=auzyc8y-uNI&t=14s", [], fetch=fetch)
        self.assertEqual(why, "")
        self.assertEqual((clip["speaker"], clip["team"], clip["text"], clip["content_type"], clip["publish_date"]),
                         ("", "Chicago Bulls", "Just with the flow.", "oneoff", day))
        clip, why = mc.find_quote_for_url("https://www.youtube.com/watch?v=auzyc8y-uNI", [], fetch=fetch)
        self.assertIsNone(clip)
        self.assertIn("timestamp", why)


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
        self.assertEqual(mc.settings_formats({}), ["vertical", "youtube", "square"])


class SetupTests(unittest.TestCase):
    def test_four_questions_write_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            target = tmp / "My Drive" / "Social & clips"
            answers = iter([f'"{target}"', "14", "vs", "y"])
            with mock.patch.object(setup_mod, "APP_DIR", tmp), \
                    mock.patch.object(setup_mod, "ask", lambda prompt: next(answers)), \
                    mock.patch.multiple(setup_mod, make_desktop_shortcut=lambda: "desktop",
                                        set_auto=lambda on: f"auto={on}", old_windows_task_exists=lambda: False), \
                    mock.patch.object(mc, "find_ffmpeg", lambda s=None: "/x/ffmpeg"):
                self.assertEqual(setup_mod.setup(), 0)
            settings = json.loads((tmp / "settings.json").read_text())
            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])          # the write test left nothing behind
        self.assertEqual((settings["out_dir"], settings["keep_days"], settings["formats"], settings["auto_run"]),
                         (str(target), 14, ["vertical", "square"], True))

    def test_enter_takes_the_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            answers = iter(["", "", "", ""])
            with mock.patch.object(setup_mod, "APP_DIR", tmp), \
                    mock.patch.object(setup_mod, "ask", lambda prompt: next(answers)), \
                    mock.patch.object(mc, "default_out_dir", lambda: tmp / "Documents" / "presser-clips"), \
                    mock.patch.multiple(setup_mod, make_desktop_shortcut=lambda: "desktop",
                                        set_auto=lambda on: f"auto={on}", old_windows_task_exists=lambda: False):
                setup_mod.setup()
            settings = json.loads((tmp / "settings.json").read_text())
        self.assertEqual(settings["out_dir"], str(tmp / "Documents" / "presser-clips"))
        self.assertEqual((settings["keep_days"], settings["formats"], settings["auto_run"]),
                         (7, ["vertical", "youtube", "square"], False))

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
        self.assertEqual(mc.unsafe_clips_folder(home / "Documents" / "presser-clips"), "")

    def test_pasted_folder_forms(self):
        self.assertEqual(setup_mod.clean_folder_answer('  "D:\\Shared drives\\Clips"  ')[-5:], "Clips")
        if os.name != "nt":
            self.assertEqual(setup_mod.clean_folder_answer("/Users/x/Google\\ Drive/Clips\\ 2 "),
                             "/Users/x/Google Drive/Clips 2")

    def test_windows_task_runs_hidden_every_30_minutes(self):
        with tempfile.TemporaryDirectory() as tmp:
            xml, vbs, bat = Path(tmp) / "t.xml", Path(tmp) / "r.vbs", Path(tmp) / "run-presser-clips.bat"
            setup_mod.write_windows_task(xml, vbs, bat)
            root = ET.fromstring(xml.read_text(encoding="utf-16"))
            ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
            self.assertEqual(root.find(".//t:Interval", ns).text, "PT30M")
            self.assertIsNone(root.find(".//t:Repetition/t:Duration", ns))          # indefinitely
            self.assertEqual(root.find(".//t:Command", ns).text, "wscript.exe")
            self.assertIn('--auto", 0, True', vbs.read_text())

    def test_mac_agent_runs_auto_every_30_minutes(self):
        plist = plistlib.loads(setup_mod.launchd_plist("/x/venv/bin/python").encode())
        self.assertEqual(plist["ProgramArguments"][0], "/x/venv/bin/python")
        self.assertEqual(plist["ProgramArguments"][-1], "--auto")
        self.assertEqual(plist["StartInterval"], 1800)


class InstallerTests(unittest.TestCase):
    SHIPPED = ["make_presser_clips.py", "caption_align.py", "presser_pc_job.py", "presser_clips_setup.py",
               "install-presser-clips.bat", "run-presser-clips.bat", "install-presser-clips-mac.command",
               "run-presser-clips-mac.command", "CLIPS-GUIDE.md"]

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
            self.assertIn(name, win)
            self.assertIn(name, mac)
            self.assertTrue((PV2 / name).is_file())
        self.assertIn("run-presser-clips.bat", win)
        self.assertIn("run-presser-clips-mac.command", mac)

    def test_pages_downloads_match_the_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = build_installers.build(Path(tmp))
            for path in built:
                published = PV2.parent / "docs" / "presser-clips" / path.name
                self.assertEqual(path.read_bytes(), published.read_bytes(),
                                 f"{published.name} is stale: run python pressers_v2/tools/build_installers.py")


if __name__ == "__main__":
    unittest.main()
