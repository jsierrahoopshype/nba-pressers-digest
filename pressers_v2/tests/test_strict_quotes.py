"""
Offline tests for strict_quotes.py's rules (no network, no keys).

    python -m unittest discover -s pressers_v2/tests -p test_strict_quotes.py -v
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import strict_quotes as st  # noqa: E402

KNOWN = {"devin booker", "gary payton", "gary payton ii", "nikola jokic", "jrue holiday"}


class StrictRuleTests(unittest.TestCase):
    def test_full_names_only(self):
        for ok in ("Devin Booker", "J.B. Bickerstaff", "PJ Hall", "Gary Payton II", "Nikola Jokić"):
            self.assertTrue(st.full_name(ok), ok)
        for bad in ("Devin", "Book", "Coach Brown", "Head Coach Mike Brown", "J.A.", "Mr. Booker", ""):
            self.assertFalse(st.full_name(bad), bad)

    def test_speaker_named_in_title_or_description(self):
        self.assertTrue(st.speaker_named("Devin Booker", "Suns Practice | Devin Booker", ""))
        self.assertTrue(st.speaker_named("Devin Booker", "Suns Practice", "Devin Booker speaks to the media."))
        self.assertFalse(st.speaker_named("Devin Booker", "Suns Practice | Booker", "Book talks"))
        self.assertFalse(st.speaker_named("Coach Brown", "Coach Brown press conference", ""))

    def test_subjects_spoken_word_for_word_and_known(self):
        q = "I think Gary Payton II and Devin Booker are great. Book is special, and Drew Holiday too."
        got = st.subjects_in_quote(["Gary Payton II", "Gary Payton", "Book", "Devin", "Drew Holiday", "Jrue Holiday",
                                    "Devin Booker", "Coach Kerr"], q, "Steve Kerr", KNOWN)
        # "Gary Payton II" is said; "Gary Payton" (a different person) is not, though its words are there.
        self.assertEqual(got, ["Gary Payton II", "Devin Booker"])
        self.assertEqual(st.subjects_in_quote(["Gary Payton"], "Gary Payton was the best defender.", "X Y", KNOWN), ["Gary Payton"])
        self.assertFalse(st.speaker_named("Gary Payton", "Gary Payton II Media Availability", ""))
        self.assertEqual(st.subjects_in_quote(["Devin Booker"], q, "Devin Booker", KNOWN), [])   # never the speaker

    def test_context_only_names_in_the_quote(self):
        q = "Devin Booker was great tonight."
        self.assertEqual(st.context_ok("Asked about Devin Booker after the game.", q), "Asked about Devin Booker after the game.")
        self.assertEqual(st.context_ok("Mike Brown on Devin Booker.", q), "")
        self.assertEqual(st.context_ok("After the loss to the Lakers.", q), "")
        self.assertEqual(st.context_ok("", q), "")

    def test_verbatim_match(self):
        t = st.tokens("Um, so — I think, Devin Booker's playing great! He really is.")
        self.assertTrue(st.contains(t, st.tokens("I think Devin Booker's playing great.")))
        self.assertFalse(st.contains(t, st.tokens("I think Devin Booker is playing great.")))
        self.assertFalse(st.contains(t, []))

    def test_segment_window(self):
        self.assertEqual(st.segment({"start_seconds": 10, "quote": "a " * 26}, 0), (0, 10 + 10 + st.SEGMENT_AFTER))
        self.assertEqual(st.segment({"start_seconds": 100, "quote": "a b"}, 120), (85, 120))


if __name__ == "__main__":
    unittest.main()
