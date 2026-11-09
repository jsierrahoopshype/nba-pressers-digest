"""
Offline tests for the Nov 9 2026 changes: three pages per slot, podcasts
from the yt-quotes rotation, team LIVE streams as pressers, reporter
questions, Best-of-the-best rules, "From:" pipes, one clip pair per entry,
full-session duplicates. No network, no API keys.

    python -m unittest discover -s pressers_v2/tests -v
"""

import json
import re
import sys
import types as pytypes
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import presser_extractor as pe  # noqa: E402
import slack_notify  # noqa: E402
from test_pressers import TempOutput  # noqa: E402

# Brayden Burries, Nov 8 2026 (digest-overnight 2026-11-09, quotes 1-3)
BURRIES_Q1 = ("You get nine and a half minutes of really good basketball on both ends and didn't quite sustain. "
              "I know that's kind of been the theme unfortunately, but I guess tonight, what did you see maybe flip? "
              "I know Jayson Tatum and George combined for 18 straight points, but obviously that means you guys "
              "aren't getting anything on the other end. What did you see change there in the first and into the "
              "second quarter?")
BURRIES_Q3 = ("When things are frustrating, not going your way, how do you prevent that from shaking your confidence "
              "or your decision-making ability? That's one thing you can't do. You can't really lose confidence. So "
              "as you lose confidence, it's over with pretty much, so you got to be out there with confidence.")
# answers that only sound like questions (must be left alone)
DAIGNEAULT = ("It's the same thing I've said with him. I think a lot of it is like what's your identity without the "
              "shooting off the table, what are you left with as a player? And I think we've got a lot of guys on "
              "the team that model that well.")
ADELMAN = ("I thought he made a couple plays where he really drew their defense to him and found Nikola Jokic that "
           "led to secondary action. His efficiency, it always will make the other team have to make a decision. "
           "Are you going to let him be crazy efficient from two, or are you going to bring a secondary defender?")
HYLAND = ("Coming into the season a lot of people thought we were probably going to be small, which I think pretty, "
          "we may be on paper, but I don't think we are when we check into the game. We like to muck up the game, "
          "but I think we just pass, you know what I'm saying?")


def q(rank, speaker, text, news=5, social=None, role=None, **extra):
    d = {"rank": rank, "speaker": speaker, "timestamp": f"0{rank}:00", "quote": text, "summary_phrase": "",
         "excerpt": "", "names_mentioned": [], "news_score": news, "speaker_confidence": "named",
         "start_seconds": rank * 60, "end_seconds": rank * 60 + 10}
    if social is not None:
        d["social_score"] = social
    if role:
        d["structured"] = {"speaker_role": role}
    d.update(extra)
    return d


def store(day: Path, vid: str, title: str, quotes: list, team="Milwaukee Bucks", ctype="presser",
          run_id="2026-11-09-overnight", **extra):
    data = {"video_id": vid, "video_title": title, "channel_team": team, "content_type": ctype,
            "run_id": run_id, "quotes": quotes, "speakers_seen": sorted({x["speaker"] for x in quotes}),
            "published": "2026-11-09T04:00:00Z", "processed_at": "2026-11-09T08:04:00Z", **extra}
    day.mkdir(parents=True, exist_ok=True)
    (day / f"{vid}.json").write_text(json.dumps(data), encoding="utf-8")
    (day / f"{vid}.md").write_text(pe.render_markdown(vid, team, data), encoding="utf-8")
    return data


class ReporterQuestionTests(unittest.TestCase):
    def test_rules_find_reporter_questions(self):
        self.assertTrue(pe.is_reporter_question(BURRIES_Q1))
        head, answer = pe.split_leading_question(BURRIES_Q3)
        self.assertTrue(head.endswith("decision-making ability?"))
        self.assertTrue(answer.startswith("That's one thing you can't do."))

    def test_rules_leave_rhetorical_answers_alone(self):
        for text in (DAIGNEAULT, ADELMAN, HYLAND, "What can I say? We played hard and we got the win tonight."):
            self.assertFalse(pe.is_reporter_question(text), text[:40])
            self.assertIsNone(pe.split_leading_question(text), text[:40])

    def test_marking_hides_bare_question_and_splits_glued_one(self):
        data = {"quotes": [q(1, "Brayden Burries", BURRIES_Q1), q(2, "Brayden Burries", BURRIES_Q3),
                           q(3, "Mark Daigneault", DAIGNEAULT)]}
        self.assertTrue(pe.mark_reporter_questions(data))
        q1, q2, q3 = data["quotes"]
        self.assertTrue(q1["reporter_question"])
        self.assertEqual(q1["speaker"], pe.REPORTER)
        self.assertFalse(pe.quote_visible(q1))
        self.assertEqual([b["speaker"] for b in q2["text_blocks"]], [pe.REPORTER, "Brayden Burries"])
        self.assertTrue(pe.speaker_own_text(q2).startswith("That's one thing"))
        self.assertNotIn("text_blocks", q3)
        self.assertFalse(pe.mark_reporter_questions(data))          # idempotent
        md = pe.render_markdown("1tN8GdDvezs", "Milwaukee Bucks", dict(data, video_title="Brayden Burries Postgame"))
        self.assertNotIn("what did you see maybe flip", md)          # the bare question is left off the page
        self.assertIn("**Unidentified reporter:** \"When things are frustrating", md)
        self.assertIn("**2. Brayden Burries", md)                    # numbers kept for the clip links

    def test_gemini_answer_decides_when_given(self):
        data = {"quotes": [q(1, "Mark Daigneault", DAIGNEAULT), q(2, "Brayden Burries", BURRIES_Q3),
                           q(3, "Brayden Burries", BURRIES_Q1)]}
        n3 = len(BURRIES_Q1.split())
        pe.mark_reporter_questions(data, {1: 0, 2: 18, 3: n3})
        q1, q2, q3 = data["quotes"]
        self.assertNotIn("text_blocks", q1)
        self.assertTrue(q2["text_blocks"][0]["text"].endswith("ability?"))
        self.assertTrue(q3["reporter_question"])
        self.assertEqual(data["reporter_check_source"], "gemini")

    def test_question_check_call(self):
        class Models:
            def generate_content(self, model, contents, config):
                assert contents[0] == pe.QUESTION_CHECK_PROMPT
                return pytypes.SimpleNamespace(text=json.dumps({"quotes": [{"rank": 1, "question_words": 12}]}),
                                               usage_metadata=None)
        client = pytypes.SimpleNamespace(models=Models())
        self.assertEqual(pe.derive_question_words(client, {"quotes": [q(1, "A", BURRIES_Q3)]}), {1: 12})
        self.assertIsNone(pe.derive_question_words(None, {"quotes": [q(1, "A", BURRIES_Q3)]}))


class BestOfTheBestTests(unittest.TestCase):
    def test_one_quote_per_speaker_and_no_reporter_questions(self):
        with TempOutput() as out:
            day = out / "2026-11-09"
            store(day, "AAAAAAAAAA1", "Bones Hyland Postgame Sound | 11.8.26",
                  [q(r, "Bones Hyland", f"Bones answer number {r} about the game and the fans tonight", news=9 - r)
                   for r in range(1, 6)])
            store(day, "AAAAAAAAAA2", "Kyle Kuzma Postgame | 11.8.26",
                  [q(1, "Kyle Kuzma", "When things are frustrating, how do you prevent that from shaking your "
                                      "confidence? Yes.", news=10),
                   q(2, "Kyle Kuzma", "We just don't have a margin for error at this stage of the season", news=4)]
                  + [q(r, f"Player {r}", f"filler answer {r} with enough words to count", news=2) for r in range(3, 20)])
            data = json.loads((day / "AAAAAAAAAA2.json").read_text(encoding="utf-8"))
            data["quotes"][0].update(reporter_question=True, speaker=pe.REPORTER)
            (day / "AAAAAAAAAA2.json").write_text(json.dumps(data), encoding="utf-8")
            lines = pe.top_quotes_block([day / "AAAAAAAAAA1.md", day / "AAAAAAAAAA2.md"])
        block = "\n".join(lines)
        self.assertEqual(len(re.findall(r"(?m)^\*\*\d+\. Bones Hyland", block)), 1)
        self.assertNotIn("how do you prevent", block)
        self.assertIn("margin for error", block)

    def test_mostly_question_never_qualifies(self):
        quote = q(1, "Player A", "", text_blocks=[
            {"speaker": pe.REPORTER, "text": "Can you walk us through what you saw on that last possession and "
                                             "what the coaches told you in the timeout before it?"},
            {"speaker": "Player A", "text": "Yeah, we got it done."}])
        self.assertGreater(pe.reporter_share(quote), pe.TOP_MAX_REPORTER_SHARE)

    def test_from_line_escapes_pipes_and_has_one_clip_pair(self):
        with TempOutput() as out:
            day = out / "2026-11-07"
            store(day, "QSvQdMSqP5A", "Media Availability: Brian Keefe and Trae Young | 11.6.26",
                  [q(r, "Trae Young", f"Trae answer {r} about the reunion and the family in Atlanta", news=3)
                   for r in range(1, 5)] + [q(5, "Trae Young", "That family is meant a lot to me", news=9)],
                  team="Washington Wizards", run_id="2026-11-07T1844Z-14utc")
            page = pe.write_pages("2026-11-07", "14utc")["presser"].read_text(encoding="utf-8")
            relinked = pe.add_clip_links(pe.link_bare_urls(pe.drop_extra_best_clip_links(page)))
        best = page[page.index("# Best of the best"):page.index("\n---\n")]
        self.assertIn("From: [Media Availability: Brian Keefe and Trae Young \\| 11.6.26]", best)
        self.assertEqual(best.count("[Clip it]"), 1)
        self.assertIn("&q=5)", best)                                  # the quote's own number, not the block's 1
        self.assertNotIn("&q=1)", best)
        self.assertEqual(relinked, page)                              # a later relink adds no second pair

    def test_old_digest_loses_the_wrong_pair(self):
        url = "https://www.youtube.com/watch?v=QSvQdMSqP5A&t=565s"
        wrong = pe.clip_link_line("QSvQdMSqP5A", 565, 1)
        right = pe.clip_link_line("QSvQdMSqP5A", 565, 5)
        old = (f"# Best of the best\n\n**1. Trae Young — \"x\"** [09:25]({url})\n\nTrae Young: \"x\"\n\n[{url}]({url})\n\n"
               f"{wrong}\n\nFrom: [Media Availability](https://www.youtube.com/watch?v=QSvQdMSqP5A)\n\n{right}\n\n---\n")
        fixed = pe.drop_extra_best_clip_links(old)
        self.assertNotIn(wrong, fixed)
        self.assertEqual(fixed.count(right), 1)
        self.assertEqual(pe.add_clip_links(fixed), fixed)
        self.assertEqual(pe.drop_extra_best_clip_links(fixed), fixed)


class DuplicateSessionTests(unittest.TestCase):
    def test_live_stream_copy_dropped_single_cut_kept(self):
        answer = ("You saw a team that played desperate, like they lost that many in a row and that they wanted "
                  "to come out and take care of home, and that's exactly what they did")
        with TempOutput() as out:
            day = out / "2026-11-09"
            store(day, "AAAAAAAAAA1", "LIVE: Pelicans vs. Clippers Postgame Interviews 11/8/2026",
                  [q(1, "Jamahl Mosley", answer), q(2, "Trey Murphy III", "Something only the stream has, said "
                                                                           "by Trey after the game in the locker room")],
                  team="New Orleans Pelicans")
            store(day, "AAAAAAAAAA2", "Jamahl Mosley | Pelicans vs. Clippers Postgame Interview 11/8/26",
                  [q(1, "Jamahl Mosley", answer + ". Now we didn't help that cause.")], team="New Orleans Pelicans")
            self.assertEqual(pe.reconcile_split_speakers(["2026-11-09"]), ["2026-11-09"])
            stream = json.loads((day / "AAAAAAAAAA1.json").read_text(encoding="utf-8"))
            md = (day / "AAAAAAAAAA1.md").read_text(encoding="utf-8")
            single = json.loads((day / "AAAAAAAAAA2.json").read_text(encoding="utf-8"))
            manifest = pe.build_clip_manifest("t", 100000)
        self.assertEqual(stream["quotes"][0]["duplicate_of"], {"video_id": "AAAAAAAAAA2", "rank": 1})
        self.assertNotIn("duplicate_of", stream["quotes"][1])
        self.assertNotIn("played desperate", md)
        self.assertIn("Something only the stream has", md)
        self.assertNotIn("duplicate_of", single["quotes"][0])
        self.assertEqual(sorted((c["video_id"], c["rank"]) for c in manifest["clips"]),
                         [("AAAAAAAAAA1", 2), ("AAAAAAAAAA2", 1)])


class PagesAndIndexTests(unittest.TestCase):
    def test_reclassify_and_rebuild_day(self):
        with TempOutput() as out:
            day = out / "2026-11-09"
            store(day, "aJSJd1S8y_0", "LIVE: Pelicans vs. Clippers Postgame Interviews 11/8/2026",
                  [q(1, "Jamahl Mosley", "We gave up 139 points and that is unacceptable for this group")],
                  team="New Orleans Pelicans", ctype="podcast", content_type_source="keyword: live")
            store(day, "0TJRHXX9moo", "Steph Revives the Warriors", [q(1, "Host", "Steph is back")],
                  team="No Fouls Given", ctype="podcast", source=pe.YTQ_SOURCE)
            (day / "podcasts-overnight.md").write_text("stale", encoding="utf-8")
            pe.apply_page_rules([day])
            written = sorted(p.name for p in pe.rebuild_day("2026-11-09"))
            stream = json.loads((day / "aJSJd1S8y_0.json").read_text(encoding="utf-8"))
            pod = (day / "podcasts-overnight.md").read_text(encoding="utf-8")
            digest = (day / "digest-overnight.md").read_text(encoding="utf-8")
            pe.regenerate_index()
            index = (out / "index.md").read_text(encoding="utf-8")
        self.assertEqual(stream["content_type"], "presser")
        self.assertEqual(written, ["digest-overnight.md", "digest.md", "podcasts-overnight.md", "podcasts.md"])
        self.assertIn("Mosley", digest)
        self.assertIn("[Podcasts & shows](podcasts-overnight.md)", digest)
        self.assertIn("[Press conferences](digest-overnight.md)", pod)
        self.assertIn("CHECK OTHER YOUTUBE PODCASTS HERE", pod)
        self.assertNotIn("CHECK OTHER YOUTUBE PODCASTS HERE", digest)
        self.assertNotIn("One-offs", digest)                          # no link to a page that doesn't exist
        self.assertIn("- 2026-11-09: [Press conferences](2026-11-09/digest.md) · [Podcasts & shows](2026-11-09/podcasts.md)",
                      index)
        self.assertIn("  - overnight: [Press conferences](2026-11-09/digest-overnight.md) · "
                      "[Podcasts & shows](2026-11-09/podcasts-overnight.md)", index)


class YtqImportTests(unittest.TestCase):
    def test_rotation_videos_become_podcasts(self):
        with TempOutput() as out:
            src = out / "ytq" / "2026-11-09"
            src.mkdir(parents=True)
            data = {"video_title": "Steph Revives the Warriors", "speakers_seen": ["Host A"],
                    "quotes": [{"rank": 1, "speaker": "Host A", "timestamp": "01:00", "summary_phrase": "s",
                                "names_mentioned": [], "excerpt": "e", "quote": "Steph is back and the Warriors "
                                                                               "are a real threat in the West"}]}
            (src / "0TJRHXX9moo.json").write_text(json.dumps(data), encoding="utf-8")
            (src / "0TJRHXX9moo.md").write_text("# Steph Revives the Warriors — *No Fouls Given*\n\nSource: x\n", encoding="utf-8")
            (src / "787oKBEJOg0.SKIPPED-reporter-gate").write_text("", encoding="utf-8")

            class Models:
                def generate_content(self, model, contents, config):
                    return pytypes.SimpleNamespace(text=json.dumps({"quotes": [
                        {"rank": 1, "speaker_confidence": "named", "news_score": 6, "social_score": 7,
                         "social_post": "Steph is back"}]}), usage_metadata=None)
            client = pytypes.SimpleNamespace(models=Models())
            cutoff = datetime(2026, 11, 7, tzinfo=timezone.utc)
            items = pe.import_ytq_podcasts(client, "", out / "ytq", cutoff, run_id="2026-11-09-overnight")
            again = pe.import_ytq_podcasts(client, "", out / "ytq", cutoff, run_id="2026-11-09-overnight")
            stored = json.loads((out / "2026-11-09" / "0TJRHXX9moo.json").read_text(encoding="utf-8"))
            page = pe.write_pages("2026-11-09", "overnight")["podcast"].read_text(encoding="utf-8")
        self.assertEqual([it["content_type"] for it in items], ["podcast"])
        self.assertEqual(again, [])                                   # already stored: not imported twice
        self.assertEqual((stored["content_type"], stored["channel_team"], stored["source"]),
                         ("podcast", "No Fouls Given", pe.YTQ_SOURCE))
        self.assertEqual(stored["quotes"][0]["news_score"], 6)
        self.assertTrue(page.startswith("# NBA Podcasts & Shows — 2026-11-09\n\n# Best of the best\n"))

    def test_rebuild_slot_follows_the_real_runs(self):
        runs = [(datetime(2026, 11, 9, 8, 4, tzinfo=timezone.utc), "2026-11-09-overnight"),
                (datetime(2026, 11, 9, 18, 53, tzinfo=timezone.utc), "2026-11-09-overnight")]
        self.assertEqual(pe.rebuild_run_id("2026-11-09T15:00:00Z", runs), "2026-11-09-overnight")
        self.assertEqual(pe.rebuild_run_id("2026-11-09T21:00:00Z", runs), "2026-11-10-overnight")
        self.assertEqual(pe.slot_for_time("2026-11-09T12:00:00Z"), ("2026-11-09T1415Z-14utc", "14utc"))
        self.assertEqual(pe.run_slot_of("2026-11-07T1844Z-14utc"), "14utc")
        self.assertEqual(pe.run_slot_of("2026-11-09-overnight"), "overnight")


class SlackPagesTests(unittest.TestCase):
    def test_one_link_per_page(self):
        sent = {}
        old = slack_notify._post
        slack_notify._post = lambda payload: sent.setdefault("p", payload) or True
        try:
            slack_notify.post_digest({"items": [{"video_id": "x", "title": "t", "channel": "c",
                                                 "content_type": "podcast"}],
                                      "date_str": "2026-11-09", "run_slot": "overnight",
                                      "digest_dates": ["2026-11-09"],
                                      "pages": [{"date": "2026-11-09", "file": "digest-overnight.html",
                                                 "label": "Press conferences"},
                                                {"date": "2026-11-09", "file": "podcasts-overnight.html",
                                                 "label": "Podcasts & shows"}]})
        finally:
            slack_notify._post = old
        text = sent["p"]["text"]
        self.assertIn("Press conferences: https://jsierrahoopshype.github.io/nba-pressers-digest/pressers_v2/"
                      "2026-11-09/digest-overnight.html", text)
        self.assertIn("/2026-11-09/podcasts-overnight.html", text)


class ConfigTests(unittest.TestCase):
    def test_missed_presser_titles_now_pass(self):
        config = json.loads((HERE.parent / "config.json").read_text(encoding="utf-8"))
        for t in ("Will hardy talks Darryn Peterson's continued growth, Kyle Filipowski's contributions",
                  "PRACTICE SOUND: COACH SWEENEY & DESMOND BANE BY SAS"):
            self.assertEqual(pe.title_filter(t, config), "", t)


if __name__ == "__main__":
    unittest.main()
