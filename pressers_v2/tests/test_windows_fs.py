"""
Real-filesystem checks for the clip tool. Written for Windows (CI runs them
on windows-latest) but valid anywhere:
  * the installer's folder check, on a fresh folder like C:\\presser-clips
  * deleting a file another handle still holds open (WinError 32 on Windows)
  * a real ffmpeg render of all three formats, moved from the app folder into
    the clips folder (on the Windows runner: from D: to C:, across drives)
  * the age-based cleanup, with real file times

    python -m unittest discover -s pressers_v2/tests -p test_windows_fs.py -v

REQUIRE_FFMPEG=1 turns "ffmpeg missing" into a failure instead of a skip.
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

import make_presser_clips as mc  # noqa: E402
import presser_clips_setup as setup_mod  # noqa: E402

REQUIRE_FFMPEG = os.environ.get("REQUIRE_FFMPEG") == "1"
ON_WINDOWS_CI = os.name == "nt" and bool(os.environ.get("CI"))
CLIP = {"video_id": "J5unk3vEQmk", "start_seconds": 118, "end_seconds": 121, "speaker": "James Harden",
        "team": "Cleveland Cavaliers", "publish_date": "2026-09-30", "content_type": "presser",
        "text": "We are in the right direction.", "social_post": "Harden on the Cavs",
        "clip_url": "https://www.youtube.com/watch?v=J5unk3vEQmk&t=118s"}


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

    def test_real_render_and_move_into_the_clips_folder(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg or not setup_mod.ffmpeg_has_subtitles(ffmpeg):
            if REQUIRE_FFMPEG:
                self.fail(f"ffmpeg with subtitle support is required here (found: {ffmpeg})")
            self.skipTest("ffmpeg with libass not installed")
        src = self.base / "source.mp4"
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=3",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(src)],
                       check=True, timeout=120)

        def fake_download(clip, a, b, work, ff):
            out = work / "src.mp4"
            shutil.copyfile(src, out)
            return out
        with mock.patch.multiple(mc, download_section=fake_download,
                                 locate_quote=lambda clip, ff: (0.5, 2.5, "captions", 0.95)):
            made, existing = mc.make_clip(dict(CLIP), self.clips, list(mc.FORMAT_ORDER), ffmpeg, pad=0.25)
            again, existing2 = mc.make_clip(dict(CLIP), self.clips, list(mc.FORMAT_ORDER), ffmpeg, pad=0.25)
        self.assertEqual(len(made), 3)
        self.assertEqual((again, existing2), ([], ["vertical", "youtube", "square"]))
        in_clips = sorted(p.name for p in self.clips.rglob("*") if p.is_file())
        self.assertEqual([n.rsplit("_", 1)[1] for n in in_clips], ["square.mp4", "vertical.mp4", "youtube.mp4"])
        for p in made:
            self.assertGreater(p.stat().st_size, 10_000)
        sizes = {}
        ffprobe = shutil.which("ffprobe")
        if ffprobe:
            for p in made:
                out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v", "-show_entries",
                                      "stream=width,height", "-of", "csv=p=0", str(p)],
                                     capture_output=True, text=True, timeout=60).stdout.strip()
                sizes[p.stem.rsplit("_", 1)[1]] = out
            self.assertEqual(sizes, {"vertical": "1080,1920", "youtube": "1920,1080", "square": "1080,1080"})
        self.assertTrue(any((self.app / "notes" / "2026-09-30").glob("*.txt")))
        self.assertEqual(list((self.app / "tmp").iterdir()), [])          # temp renders removed

    def test_cleanup_with_real_file_times(self):
        old = self.clips / "2026-09-01" / "pressers" / "old_vertical.mp4"
        new = self.clips / "2026-09-30" / "pressers" / "new_vertical.mp4"
        for p, age in ((old, 10), (new, 1)):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * 2048)
            t = time.time() - age * 86400
            os.utime(p, (t, t))
        n, freed = mc.run_cleanup(self.clips, 7)
        self.assertEqual((n, freed), (1, 2048))
        self.assertFalse(old.exists() or (self.clips / "2026-09-01").exists())
        self.assertTrue(new.exists())


if __name__ == "__main__":
    unittest.main()
