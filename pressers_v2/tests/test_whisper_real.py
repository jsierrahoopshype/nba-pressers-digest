"""
The Whisper fallback for real: faster-whisper with the base.en model (the
one the clip tool uses) transcribes a short spoken sentence, through the
clip tool's own transcribe_words(), with the pinned PyAV.

Needs network on first run (the model, ~140 MB, comes from Hugging Face) and
a way to make speech: Windows' built-in voice (System.Speech) or macOS `say`.
CI runs it on windows-latest with REQUIRE_WHISPER=1, which turns a skip
into a failure.

    python -m unittest discover -s pressers_v2/tests -p test_whisper_real.py -v
"""

import importlib.metadata
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault("NBA_PRESSER_APP_DIR", tempfile.mkdtemp(prefix="npc_app_"))

import caption_align as ca  # noqa: E402
import make_presser_clips as mc  # noqa: E402
import presser_clips_setup as setup_mod  # noqa: E402

REQUIRE = os.environ.get("REQUIRE_WHISPER") == "1"
SENTENCE = "We are in the right direction, and it is going to take some time."


def speak(text: str, out: Path) -> bool:
    """Write the sentence as speech to out (wav). False if this OS can't."""
    if os.name == "nt":
        ps = ("Add-Type -AssemblyName System.Speech;"
              "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
              "$s.Rate = -1; $s.SetOutputToWaveFile($env:NPC_WAV); $s.Speak($env:NPC_TEXT); $s.Dispose()")
        res = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             env=dict(os.environ, NPC_WAV=str(out), NPC_TEXT=text), timeout=120)
        return res.returncode == 0 and out.is_file() and out.stat().st_size > 10_000
    if sys.platform == "darwin" and shutil.which("say"):
        res = subprocess.run(["say", "-o", str(out), "--data-format=LEI16@16000", text], timeout=120)
        return res.returncode == 0 and out.is_file()
    return False


class RealWhisperTests(unittest.TestCase):
    def test_pinned_faster_whisper_transcribes_real_speech(self):
        if importlib.util.find_spec("faster_whisper") is None:
            if REQUIRE:
                self.fail("faster-whisper is required here")
            self.skipTest("faster-whisper not installed")
        versions = {d: importlib.metadata.version(d) for d in setup_mod.PINNED}
        if REQUIRE:
            self.assertEqual(versions, setup_mod.PINNED)                    # the pinned pair is what ran
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "speech.wav"
            if not speak(SENTENCE, wav):
                if REQUIRE:
                    self.fail("could not generate speech on this machine")
                self.skipTest("no speech synthesizer here")
            words = mc.transcribe_words(wav, offset=100.0)
        tokens = [w[2] for w in words]
        print(f"faster-whisper {versions['faster-whisper']} + PyAV {versions['av']}: {' '.join(tokens)}")
        for expected in ("right", "direction", "time"):
            self.assertIn(expected, tokens)
        starts = [w[0] for w in words]
        self.assertEqual(starts, sorted(starts))
        self.assertGreaterEqual(starts[0], 100.0)                             # offset applied
        # and the subtitles path works on these real timings
        timed = ca.quote_word_times(SENTENCE, words, starts[0], words[-1][1])
        self.assertTrue(timed, "the quote didn't match Whisper's words")
        chunks = ca.subtitle_chunks(timed)
        self.assertTrue(all(2 <= len(c[2].split()) <= 4 for c in chunks), chunks)


if __name__ == "__main__":
    unittest.main()
