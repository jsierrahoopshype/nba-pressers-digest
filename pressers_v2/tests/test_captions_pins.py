"""
Caption fetching (track choice, one track per request, retries on 429, the
reason logged when captions aren't used) and the faster-whisper / PyAV pins.

    python -m unittest discover -s pressers_v2/tests -v
"""

import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
PV2 = HERE.parent
sys.path.insert(0, str(PV2))
os.environ.setdefault("NBA_PRESSER_APP_DIR", tempfile.mkdtemp(prefix="npc_app_"))

import make_presser_clips as mc  # noqa: E402
import presser_clips_setup as setup_mod  # noqa: E402

J3 = {"events": [{"tStartMs": 158000, "dDurationMs": 4000, "segs": [
    {"utf8": "we"}, {"utf8": " are", "tOffsetMs": 300}, {"utf8": " in", "tOffsetMs": 600},
    {"utf8": " the", "tOffsetMs": 800}, {"utf8": " right", "tOffsetMs": 1000},
    {"utf8": " direction", "tOffsetMs": 1400}]}]}


def info(auto=None, manual=None) -> dict:
    return {"id": "J5unk3vEQmk", "title": "t", "extractor": "generic", "extractor_key": "Generic",
            "webpage_url": "http://127.0.0.1/none", "ext": "mp4",
            "formats": [{"format_id": "18", "url": "http://127.0.0.1:9/v.mp4", "ext": "mp4",
                         "vcodec": "h264", "acodec": "aac"}],
            "automatic_captions": auto or {}, "subtitles": manual or {}}


class FakeYtdlp:
    """Plays yt-dlp: -J returns the info; a caption download writes a file or
    fails with the scripted error (per track)."""

    def __init__(self, info_dict, script):
        self.info, self.script, self.calls = info_dict, script, []

    def __call__(self, args, timeout=180):
        self.calls.append(args)
        if "-J" in args:
            if self.info is None:
                return subprocess.CompletedProcess(args, 1, "", "ERROR: [youtube] x: Sign in to confirm you're not a bot")
            return subprocess.CompletedProcess(args, 0, json.dumps(self.info), "")
        lang = args[args.index("--sub-langs") + 1].replace("\\", "")
        outcome = self.script[lang].pop(0)
        if outcome == "ok":
            out = Path(args[args.index("-o") + 1].replace("%(ext)s", f"{lang}.json3"))
            out.write_text(json.dumps(J3), encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", outcome)


THROTTLED = ("WARNING: The info failed to download: Unable to download video subtitles for 'en-orig': "
             "HTTP Error 429: Too Many Requests; trying with URL https://www.youtube.com/watch?v=x\n"
             "[youtube] x: Downloading webpage\n[youtube] x: Downloading player")


class CaptionFetchTests(unittest.TestCase):
    def setUp(self):
        mc._caption_cache.clear()
        self.waits = mock.patch.object(mc, "CAPTION_RETRY_WAITS", (0, 0))
        self.waits.start()

    def tearDown(self):
        self.waits.stop()
        mc._caption_cache.clear()

    def fetch(self, fake):
        with mock.patch.object(mc, "_run_ytdlp", fake):
            return mc.fetch_caption_words("J5unk3vEQmk", "ffmpeg")

    def test_track_order_prefers_speech_recognition_then_manual_english(self):
        i = info(auto={"en-orig": [{}], "en": [{}], "es": [{}]},
                 manual={"en-GB": [{}], "en": [{}], "live_chat": [{}], "fr": [{}]})
        self.assertEqual(mc.caption_tracks(i), [("auto", "en-orig"), ("auto", "en"),
                                                ("manual", "en"), ("manual", "en-GB")])

    def test_one_track_per_request_and_a_429_is_retried(self):
        fake = FakeYtdlp(info(auto={"en-orig": [{}], "en": [{}]}), {"en-orig": [THROTTLED, "ok"]})
        words, word_level, reason = self.fetch(fake)
        self.assertEqual((len(words), word_level, reason), (6, True, ""))
        langs = [c[c.index("--sub-langs") + 1] for c in fake.calls if "--sub-langs" in c]
        self.assertEqual(langs, ["en\\-orig", "en\\-orig"])                 # one track each time
        self.assertIn("--load-info-json", fake.calls[1])                   # the page isn't fetched again

    def test_a_failing_track_falls_through_to_the_next(self):
        fake = FakeYtdlp(info(auto={"en-orig": [{}]}, manual={"en": [{}]}),
                         {"en-orig": ["ERROR: Unable to download video subtitles: HTTP Error 403: Forbidden"],
                          "en": ["ok"]})
        words, _, reason = self.fetch(fake)
        self.assertEqual((len(words), reason), (6, ""))

    def test_reasons_are_exact_and_failures_are_not_cached(self):
        fake = FakeYtdlp(info(auto={"en-orig": [{}]}), {"en-orig": [THROTTLED, THROTTLED, THROTTLED, "ok"]})
        words, _, reason = self.fetch(fake)
        self.assertEqual(words, [])
        self.assertIn("auto en-orig: WARNING: The info failed to download", reason)
        self.assertIn("HTTP Error 429: Too Many Requests", reason)
        words, _, reason = self.fetch(fake)                                 # tried again, now it works
        self.assertEqual((len(words), reason), (6, ""))

    def test_no_english_and_unreadable_page_say_so(self):
        _, _, reason = self.fetch(FakeYtdlp(info(auto={"es": [{}], "pt": [{}]}), {}))
        self.assertEqual(reason, "the video has no English captions (tracks: es, pt)")
        _, _, reason = self.fetch(FakeYtdlp(None, {}))
        self.assertIn("yt-dlp couldn't read the video", reason)
        self.assertIn("Sign in to confirm", reason)

    def test_unused_captions_are_logged_with_the_reason(self):
        with tempfile.TemporaryDirectory() as app, \
                mock.patch.dict(os.environ, {"NBA_PRESSER_APP_DIR": app}), \
                mock.patch.object(mc, "fetch_caption_words", lambda v, f: ([], False, "auto en-orig: HTTP Error 429")), \
                mock.patch.object(mc, "whisper_words", side_effect=mc.LowConfidence("no audio")):
            with self.assertRaises(mc.LowConfidence):
                mc.locate_quote({"video_id": "J5unk3vEQmk", "start_seconds": 159, "end_seconds": 170,
                                 "text": "we are in the right direction"}, "ffmpeg")
            log = (Path(app) / "logs" / "clips-log.txt").read_text(encoding="utf-8")
        self.assertIn("J5unk3vEQmk @159s: captions not used: auto en-orig: HTTP Error 429", log)

    def test_real_ytdlp_downloads_just_the_chosen_track(self):
        import importlib.util
        if importlib.util.find_spec("yt_dlp") is None:
            self.skipTest("yt-dlp not installed")
        served = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                served.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(J3).encode())

            def log_message(self, *a):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        the_info = info(auto={"en-orig": [{"ext": "json3", "url": f"{base}/en-orig.json3"}],
                              "en": [{"ext": "json3", "url": f"{base}/en.json3"}],
                              "es": [{"ext": "json3", "url": f"{base}/es.json3"}]})
        real = mc._run_ytdlp

        def run(args, timeout=180):
            if "-J" in args:
                return subprocess.CompletedProcess(args, 0, json.dumps(the_info), "")
            return real(args, timeout)
        try:
            with mock.patch.dict(os.environ, {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}), \
                    mock.patch.object(mc, "_run_ytdlp", run):
                words, word_level, reason = mc.fetch_caption_words("J5unk3vEQmk", shutil.which("ffmpeg") or "ffmpeg")
        finally:
            server.shutdown()
        self.assertEqual((len(words), word_level, reason), (6, True, ""), reason)
        self.assertEqual(served, ["/en-orig.json3"])                       # one request, the right track


class PinTests(unittest.TestCase):
    def test_pins(self):
        self.assertEqual(setup_mod.PINNED, {"faster-whisper": "1.2.1", "av": "18.1.0"})
        self.assertEqual(setup_mod.pip_specs(), ["faster-whisper==1.2.1", "av==18.1.0"])

    def test_installers_use_the_same_pins(self):
        win = (PV2 / "install-presser-clips.bat").read_text(encoding="utf-8")
        mac = (PV2 / "install-presser-clips-mac.command").read_text(encoding="utf-8")
        for spec in setup_mod.pip_specs():
            self.assertIn(f'"{spec}"', win)
            self.assertIn(f'"{spec}"', mac)

    def test_existing_installs_get_the_pins_on_their_next_run(self):
        calls = []

        def fake_run(cmd, timeout=120):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with tempfile.TemporaryDirectory() as app, mock.patch.dict(os.environ, {"NBA_PRESSER_APP_DIR": app}), \
                mock.patch.object(setup_mod, "_run_quiet", fake_run), \
                mock.patch.object(setup_mod, "installed_versions",
                                  lambda: {"faster-whisper": "1.2.1", "av": "19.0.0"}):
            self.assertTrue(setup_mod.ensure_pins())
        self.assertEqual(calls[-1][-2:], ["faster-whisper==1.2.1", "av==18.1.0"])
        self.assertIn("pip", calls[-1])

    def test_matching_pins_do_nothing_and_a_failure_waits_a_day(self):
        with tempfile.TemporaryDirectory() as app, mock.patch.dict(os.environ, {"NBA_PRESSER_APP_DIR": app}):
            with mock.patch.object(setup_mod, "installed_versions", lambda: dict(setup_mod.PINNED)), \
                    mock.patch.object(setup_mod, "_run_quiet", side_effect=AssertionError("no pip")):
                self.assertTrue(setup_mod.ensure_pins())
            calls = []

            def failing(cmd, timeout=120):
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 1, "", "network down")
            with mock.patch.object(setup_mod, "installed_versions", lambda: {"faster-whisper": None, "av": None}), \
                    mock.patch.object(setup_mod, "_run_quiet", failing):
                self.assertFalse(setup_mod.ensure_pins())
                self.assertFalse(setup_mod.ensure_pins())                  # within a day: not retried
            self.assertEqual(len(calls), 1)
            marker = Path(app) / "logs" / "pip-pins-failed.txt"
            old = time.time() - setup_mod.PIN_RETRY_SECS - 10
            os.utime(marker, (old, old))
            with mock.patch.object(setup_mod, "installed_versions", lambda: {"faster-whisper": None, "av": None}), \
                    mock.patch.object(setup_mod, "_run_quiet", failing):
                setup_mod.ensure_pins()
            self.assertEqual(len(calls), 2)

    def test_installed_versions_reads_real_metadata(self):
        have = setup_mod.installed_versions()
        self.assertEqual(set(have), set(setup_mod.PINNED))


if __name__ == "__main__":
    unittest.main()


class UpdateReliabilityTests(unittest.TestCase):
    """Self-update: 3 tries per file with longer timeouts, a clear warning
    when a core file can't be refreshed, pins checked no matter what."""

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    def run_update(self, urlopen):
        said, pins = [], []
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as app, \
                mock.patch.dict(os.environ, {"NBA_PRESSER_APP_DIR": app}), \
                mock.patch.object(setup_mod, "APP_DIR", Path(tmp)), \
                mock.patch.object(setup_mod, "UPDATE_RETRY_WAITS", (0, 0)), \
                mock.patch.object(setup_mod.urllib.request, "urlopen", urlopen), \
                mock.patch.object(setup_mod, "say", said.append), \
                mock.patch.object(setup_mod, "ensure_packages", lambda: pins.append(True)):
            changed = setup_mod.update(quiet=False)
            log_file = Path(app) / "logs" / "setup-log.txt"
            log = log_file.read_text(encoding="utf-8") if log_file.exists() else ""
            files = sorted(p.name for p in Path(tmp).rglob("*") if p.is_file())
        return changed, said, pins, log, files

    @staticmethod
    def body_for(url: str) -> bytes:
        name = url.split("/pressers_v2/", 1)[1]
        if name == setup_mod.MODEL_FILE:
            return (PV2 / name).read_bytes()
        return b"print('ok')\n"

    def test_a_flaky_connection_is_retried_with_longer_timeouts(self):
        calls = {}

        def flaky(req, timeout=None, **kw):
            n = calls.setdefault(req.full_url, [])
            n.append(timeout)
            if len(n) < 3:
                raise setup_mod.urllib.error.URLError(TimeoutError("timed out"))
            return self.Resp(self.body_for(req.full_url))
        changed, said, pins, log, _ = self.run_update(flaky)
        self.assertEqual(changed, len(setup_mod.UPDATE_FILES))
        self.assertTrue(all(t == list(setup_mod.UPDATE_TIMEOUTS) for t in calls.values()), calls)
        self.assertFalse(any(setup_mod.UPDATE_WARNING in s for s in said))
        self.assertEqual(pins, [True])

    def test_core_file_failure_warns_logs_and_still_checks_the_pins(self):
        def core_down(req, timeout=None, **kw):
            if req.full_url.endswith("/make_presser_clips.py"):
                raise TimeoutError("timed out")
            return self.Resp(self.body_for(req.full_url))
        changed, said, pins, log, files = self.run_update(core_down)
        self.assertIn(f"[!] {setup_mod.UPDATE_WARNING}", said)
        self.assertIn("update failed for make_presser_clips.py", log)
        self.assertIn("TimeoutError", log)
        self.assertNotIn("make_presser_clips.py", files)                  # the previous copy is left alone
        self.assertEqual(changed, len(setup_mod.UPDATE_FILES) - 1)
        self.assertEqual(pins, [True])

    def test_everything_offline_still_checks_the_pins(self):
        attempts = []

        def offline(req, timeout=None, **kw):
            attempts.append(req.full_url)
            raise setup_mod.urllib.error.URLError("no internet")
        changed, said, pins, log, _ = self.run_update(offline)
        self.assertEqual(changed, 0)
        self.assertEqual(len(attempts), 3 * len(setup_mod.UPDATE_FILES))
        self.assertIn(f"[!] {setup_mod.UPDATE_WARNING}", said)
        self.assertEqual(pins, [True])

    def test_a_missing_file_is_not_retried(self):
        attempts = []

        def not_found(req, timeout=None, **kw):
            attempts.append(req.full_url)
            raise setup_mod.urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        self.run_update(not_found)
        self.assertEqual(len(attempts), len(setup_mod.UPDATE_FILES))
