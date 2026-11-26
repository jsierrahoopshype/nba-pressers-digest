import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import presser_extractor as p  # noqa: E402


class SameAnswerTests(unittest.TestCase):
    def test_same_answer_different_start_is_caught(self):
        a = ("I know that these fans love our team and our organization more than anything. "
             "Tough times don't last, tough people do. And this is a tough community and know "
             "that we are going to do everything we can to get this thing turned around.")
        b = ("Tough times don't last, tough people do. And this is a tough community and know "
             "that we are going to do everything we can to get this thing turned around for them.")
        self.assertTrue(p._same_answer(p._answer_words(a), p._answer_words(b)))

    def test_different_answers_are_kept(self):
        a = ("We got to be better at guarding the ball and keeping them out of transition. "
             "Second half we were a lot better defensively but offensively pretty stagnant.")
        b = ("My mom was coming to the game tonight so I had to play hard, she drove four hours "
             "from home and I wanted her to see a win in person for the first time this year.")
        self.assertFalse(p._same_answer(p._answer_words(a), p._answer_words(b)))

    def test_short_quotes_never_match(self):
        self.assertFalse(p._same_answer(p._answer_words("We played well tonight."),
                                        p._answer_words("We played well tonight.")))


if __name__ == "__main__":
    unittest.main()
