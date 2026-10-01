"""
Real-filesystem checks for the clip tool. Written for Windows (CI runs them
on windows-latest) but valid anywhere:
  * the installer's folder check, on a fresh folder like C:\\presser-clips
  * deleting a file another handle still holds open (WinError 32 on Windows)
  * a real ffmpeg render of all three formats with the speaker-following
    crop and timed subtitles, moved from the app folder into the quote's
    folder (on the Windows runner: from D: to C:, across drives)
  * the quote-folder cleanup, with real file times
  * Windows only: the presserclips: link registration under HKCU, and that
    the registered command refuses a bad or tampered link

    python -m unittest discover -s pressers_v2/tests -p test_windows_fs.py -v

REQUIRE_FFMPEG=1 turns "ffmpeg / OpenCV missing" into a failure instead of a skip.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault("NBA_PRESSER_APP_DIR", tempfile.mkdtemp(prefix="npc_app_"))
os.environ.setdefault("NBA_PRESSER_LINK_WAIT", "0")

import caption_align as ca  # noqa: E402
import make_presser_clips as mc  # noqa: E402
import presser_clips_setup as setup_mod  # noqa: E402
import reframe  # noqa: E402

REQUIRE_FFMPEG = os.environ.get("REQUIRE_FFMPEG") == "1"
ON_WINDOWS_CI = os.name == "nt" and bool(os.environ.get("CI"))
TEXT = "We are in the right direction, and it is going to take some time."
CLIP = {"video_id": "J5unk3vEQmk", "start_seconds": 118, "end_seconds": 121, "speaker": "James Harden",
        "team": "Cleveland Cavaliers", "publish_date": "2026-09-30", "content_type": "presser", "rank": 2,
        "news_angle": "James Harden on the season", "text": TEXT, "social_post": "Harden on the Cavs"}
FOLDER = "2026-09-30 James Harden - on the season"


def need(ok: bool, what: str, test: unittest.TestCase) -> None:
    if not ok:
        if REQUIRE_FFMPEG:
            test.fail(f"{what} is required here")
        test.skipTest(f"{what} not installed")


class RealFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="npc_fs_"))
        self.app = self.base / "App Data ñ"
        # On the Windows runner the temp folder is on D:, so a clips folder on
        # C: makes every move a cross-drive one (like C: -> a Google Drive G:).
        if ON_WINDOWS_CI:
            self.clips = Path("C:/") / f"presser-clips-ci-{uuid.uuid4().hex[:8]}"
        else:
            self.clips = self.base / "Clips folder ñ"
        self.env = mock.patch.dict(os.environ, {"NBA_PRESSER_APP_DIR": str(self.app)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        mc.remove_tree(self.clips)
        mc.remove_tree(self.base)

    def test_installer_folder_check_accepts_a_fresh_folder_and_leaves_it_empty(self):
        for folder in (self.clips, self.clips / "nested new" / "presser-clips"):
            self.assertEqual(setup_mod.check_folder(folder), "", str(folder))
            self.assertTrue(folder.is_dir())
            self.assertEqual([p.name for p in folder.iterdir() if p.is_file()], [])

    def test_delete_waits_for_a_file_another_handle_holds(self):
        path = self.base / "held.mp4"
        f = open(path, "wb")
        f.write(b"x")
        if os.name == "nt":
            with self.assertRaises(PermissionError):      # the WinError 32 situation is real here
                os.remove(path)
        threading.Timer(0.4, f.close).start()
        self.assertTrue(mc.remove_file(path))
        self.assertFalse(path.exists())

    def _source_video(self, ffmpeg: str) -> Path:
        """6s 1280x720 'press conference': a face that walks right after 1s."""
        src = self.base / "source.mp4"
        face = HERE / "fixtures" / "face" / "mona-lisa.jpg"
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "color=c=0x264d73:s=1280x720:d=6:r=30",
                        "-loop", "1", "-i", str(face),
                        "-f", "lavfi", "-i", "sine=frequency=330:duration=6",
                        "-filter_complex",
                        "[0:v][1:v]overlay=x='if(lt(t,1),100,min(850,100+(t-1)*450))':y=150:shortest=1[v]",
                        "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "6", str(src)],
                       check=True, timeout=120)
        return src

    def test_real_render_into_the_quote_folder(self):
        ffmpeg = shutil.which("ffmpeg")
        need(bool(ffmpeg) and setup_mod.ffmpeg_has_subtitles(ffmpeg), "ffmpeg with libass", self)
        import importlib.util
        need(importlib.util.find_spec("cv2") is not None, "OpenCV (face tracking)", self)
        src = self._source_video(ffmpeg)
        words = [(0.4 + i * 0.35, 0.4 + i * 0.35 + 0.3, t) for i, t in enumerate(ca.norm_words(TEXT))]
        loc = mc.Located(0.4, words[-1][1], "captions", 0.95, words, True)

        def fake_download(clip, a, b, work, ff):
            out = work / "src.mp4"
            shutil.copyfile(src, out)
            return out
        with mock.patch.multiple(mc, download_section=fake_download, locate_quote=lambda clip, ff: loc):
            made, existing = mc.make_clip(dict(CLIP), self.clips, list(mc.FORMAT_ORDER), ffmpeg, pad=0.0)
            again, existing2 = mc.make_clip(dict(CLIP), self.clips, list(mc.FORMAT_ORDER), ffmpeg, pad=0.0)
        self.assertEqual(len(made), 3)
        self.assertEqual((again, existing2), ([], ["vertical", "youtube", "square"]))
        files = sorted(p.relative_to(self.clips).as_posix() for p in self.clips.rglob("*") if p.is_file())
        self.assertEqual(files, [f"{FOLDER}/quote.txt", f"{FOLDER}/square.mp4",
                                 f"{FOLDER}/vertical.mp4", f"{FOLDER}/youtube.mp4"])
        self.assertEqual(list((self.app / "tmp").iterdir()), [])          # temp renders removed
        ffprobe = shutil.which("ffprobe")
        if ffprobe:
            sizes = {}
            for p in made:
                sizes[p.stem] = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v", "-show_entries",
                                                "stream=width,height", "-of", "csv=p=0", str(p)],
                                               capture_output=True, text=True, timeout=60).stdout.strip()
            self.assertEqual(sizes, {"vertical": "1080,1920", "youtube": "1920,1080", "square": "1080,1080"})
        plan = reframe.plan_crop(src, 1080, 1920)
        reframe.forget(src)
        self.assertTrue(plan.tracked, plan.note)
        self.assertGreater(plan.xs[-1], plan.xs[0] + 300)                 # the crop followed the face right

    def test_old_quote_folders_go_as_a_whole(self):
        old = self.clips / "2026-09-01 Joe Mazzulla - on defense"
        new = self.clips / "2026-09-30 James Harden - on the season"
        for folder, age in ((old, 10), (new, 1)):
            for name in ("vertical.mp4", "quote.txt"):
                p = folder / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b"x" * 1024)
                t = time.time() - age * 86400
                os.utime(p, (t, t))
        n, freed = mc.run_cleanup(self.clips, 7)
        self.assertEqual((n, freed), (1, 2048))
        self.assertFalse(old.exists())
        self.assertTrue((new / "vertical.mp4").exists())


@unittest.skipUnless(ON_WINDOWS_CI, "registers a link type for the current Windows user (CI only)")
class WindowsLinkTests(unittest.TestCase):
    KEY = r"Software\Classes\presserclips"

    def tearDown(self):
        import winreg
        for sub in (r"\shell\open\command", r"\shell\open", r"\shell", ""):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, self.KEY + sub)
            except OSError:
                pass

    def _registered_command(self) -> str:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY) as k:
            self.assertEqual(winreg.QueryValueEx(k, "URL Protocol")[0], "")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY + r"\shell\open\command") as k:
            return winreg.QueryValueEx(k, "")[0]

    def _run_as_windows_would(self, cmd: str, url: str, app: Path) -> int:
        # what Windows does with a clicked link: substitute %1, start the command line as is
        env = dict(os.environ, NBA_PRESSER_APP_DIR=str(app), NBA_PRESSER_LINK_WAIT="0")
        return subprocess.run(cmd.replace("%1", url), env=env, timeout=120,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode

    def test_registration_and_the_registered_command_refuses_bad_links(self):
        setup_mod.register_link_windows()
        cmd = self._registered_command()
        self.assertTrue(cmd.endswith('--link "%1"'), cmd)
        self.assertIn("make_presser_clips.py", cmd)
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)
            self.assertEqual(self._run_as_windows_would(cmd, "presserclips://clip?v=bad&t=1&q=1", app), 1)
            # a link that tries to smuggle in extra arguments
            hostile = 'presserclips://clip?v=J5unk3vEQmk&t=118&q=2" --out "C:\\Windows\\Temp\\x'
            self.assertEqual(self._run_as_windows_would(cmd, hostile, app), 1)
            log = (app / "logs" / "link-log.txt").read_text(encoding="utf-8")
        self.assertIn("refused: bad v", log)
        self.assertIn("refused arguments", log)


if __name__ == "__main__":
    unittest.main()
