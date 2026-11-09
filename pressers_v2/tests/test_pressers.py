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


def strip_clip_links(test, md: str) -> str:
    """Remove the "Clip it" line (and the blank line before it) that follows
    each quote block's timestamped URL; it must sit exactly there."""
    lines, out = md.split("\n"), []
    for line in lines:
        if pe.CLIP_LINK_LINE_RE.match(line):
            test.assertEqual(out[-1], "")
            test.assertRegex(out[-2], r"^\[https://www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{11}&t=\d+s\]")
            out.pop()
            continue
        out.append(line)
    return "\n".join(out)


def assert_only_link_wrappers_differ(test, ours: str, theirs: str) -> None:
    """Line by line: every line that differs is theirs with its bare URL
    wrapped as [url](url), plus the "Clip it" line after each quote block;
    nothing else may change."""
    ours = strip_clip_links(test, ours)
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


class ClipLinkUpgradeTests(unittest.TestCase):
    def test_older_clip_it_only_line_gets_send_to_slack(self):
        url = "https://www.youtube.com/watch?v=3EYEd9Lgvgc&t=52s"
        old = (f"**1. Kenny Atkinson — \"x\"** [00:52]({url})\n\nKenny Atkinson: \"x\"\n\n[{url}]({url})\n\n"
               "<small>[Clip it](presserclips://clip?v=3EYEd9Lgvgc&t=52&q=1)</small>\n\n---\n")
        new = pe.add_clip_links(old)
        self.assertIn("<small>[Clip it](presserclips://clip?v=3EYEd9Lgvgc&t=52&q=1) · "
                      "[Send to Slack](presserclips://clip?v=3EYEd9Lgvgc&t=52&q=1&slack=1)</small>\n", new)
        self.assertEqual(new.count("[Clip it]"), 1)
        self.assertEqual(pe.add_clip_links(new), new)                        # idempotent
        self.assertEqual(new.replace(" · [Send to Slack](presserclips://clip?v=3EYEd9Lgvgc&t=52&q=1&slack=1)", ""), old)


class FormatParityTests(unittest.TestCase):
    def test_vendored_renderer_reproduces_ytquotes_output(self):
        data = json.loads((FIXTURES / "6sbyI-n2yh0.json").read_text(encoding="utf-8"))
        expected = (FIXTURES / "6sbyI-n2yh0.md").read_text(encoding="utf-8")
        ours = pe.render_markdown("6sbyI-n2yh0", "NBA on NBC", data)
        assert_only_link_wrappers_differ(self, ours, expected)
        # Source line + one timestamped URL per quote block are now links
        self.assertEqual(len(LINK_WRAP_RE.findall(ours)), 1 + len(data["quotes"]))
        # one "Clip it" link per quote block, with the block's video, second and number
        clip_lines = [l for l in ours.split("\n") if pe.CLIP_LINK_LINE_RE.match(l)]
        self.assertEqual(len(clip_lines), len(data["quotes"]))
        q1 = data["quotes"][0]
        self.assertEqual(clip_lines[0], pe.clip_link_line("6sbyI-n2yh0", ytq.timestamp_to_seconds(q1["timestamp"]),
                                                          q1["rank"]))
        self.assertEqual(pe.add_clip_links(ours), ours)                      # idempotent
        self.assertIn(" · [Send to Slack](presserclips://clip?v=6sbyI-n2yh0&t=", clip_lines[0])
        self.assertTrue(clip_lines[0].endswith(f"&q={q1['rank']}&slack=1)</small>"))
        self.assertIn("\nSource: [https://www.youtube.com/watch?v=6sbyI-n2yh0]"
                      "(https://www.youtube.com/watch?v=6sbyI-n2yh0)\n", ours)

    def test_pages_differ_from_ytquotes_only_in_title_links_and_best(self):
        """Each of our three pages vs yt-quotes' own write_digest_file over
        yt-quotes' own per-video .md of the same videos: only the page title,
        the links to the other pages, the Best of the best block and the
        link wrappers may differ (and only the podcasts page keeps the
        closing CHECK OTHER YOUTUBE PODCASTS HERE link)."""
        vids = (("J5unk3vEQmk", "James Harden Media Availability", False),
                ("p7o-S3hmoD4", "Chase Down Podcast Live: Media Day Reactions", False),
                ("auzyc8y-uNI", "Caleb Wilson: My First Media Day", True))
        with TempOutput() as out:
            for vid, title, one_off in vids:
                pe.process_video(client_for(HARDEN, HARDEN_FIELDS), video(vid, title, one_off),
                                 "Cleveland Cavaliers")
            pages = {c: p.read_text(encoding="utf-8") for c, p in pe.write_pages("2026-09-30", None).items()}
            ref_root = out / "ytq"
            ytq.OUTPUT_DIR = ref_root
            theirs = {}
            try:
                (ref_root / "2026-09-30").mkdir(parents=True)
                for (vid, _, _), ctype in zip(vids, pe.CONTENT_TYPES):
                    stored = json.loads((out / "2026-09-30" / f"{vid}.json").read_text(encoding="utf-8"))
                    (ref_root / "2026-09-30" / f"{vid}.md").write_text(
                        ytq.to_markdown(stored["url"], "Cleveland Cavaliers", stored), encoding="utf-8")
                    theirs[ctype] = ytq.write_digest_file("2026-09-30", "rotation", f"{ctype}.md",
                                                          [vid]).read_text(encoding="utf-8")
            finally:
                ytq.OUTPUT_DIR = out
        self.assertEqual(set(pages), set(pe.CONTENT_TYPES))
        for ctype, ours in pages.items():
            a = ours.split("\n")
            self.assertEqual(a[0], f"# {pe.PAGE_TITLES[ctype]} — 2026-09-30")
            others = [f"[{pe.CONTENT_TYPE_LABELS[o]}]({pe.page_filename(o, None)})" for o in pe.CONTENT_TYPES if o != ctype]
            self.assertEqual(a[2], " · ".join(others))          # plain links to the other two pages
            self.assertNotIn("# Press conferences", a)            # one type per page, no section headers
            a = ["# HoopsHype YT Quotes — 2026-09-30", ""] + a[4:]
            if pe.TOP_QUOTES_HEADER in a:
                top = a.index(pe.TOP_QUOTES_HEADER)
                end = a.index("---", top)
                del a[top:end + 2]
            if ctype == "podcast":
                self.assertTrue(ours.rstrip().endswith(ytq.DIGEST_CLOSING_LINE))
            else:
                self.assertNotIn("CHECK OTHER YOUTUBE PODCASTS HERE", ours)
                a = a + ["---", "", ytq.DIGEST_CLOSING_LINE, ""]
            assert_only_link_wrappers_differ(self, "\n".join(a), theirs[ctype])

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
        # extraction, reporter-question check, clip fields, structured fields
        self.assertEqual(client.models.calls, ["video", "text", "text", "text"])
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

    def test_three_pages_per_slot(self):
        with TempOutput() as out:
            for vid, title, one_off in (("J5unk3vEQmk", "James Harden Media Availability", False),
                                        ("p7o-S3hmoD4", "Chase Down Podcast Live: Media Day Reactions", False),
                                        ("auzyc8y-uNI", "Caleb Wilson: My First Media Day", True)):
                pe.process_video(client_for(HARDEN, HARDEN_FIELDS), video(vid, title, one_off), "Cleveland Cavaliers")
            written = pe.write_pages("2026-09-30", "overnight", ["J5unk3vEQmk", "p7o-S3hmoD4", "auzyc8y-uNI"])
            names = {c: p.name for c, p in written.items()}
            presser = written["presser"].read_text(encoding="utf-8")
            harden_md = (out / "2026-09-30" / "J5unk3vEQmk.md").read_text(encoding="utf-8").strip()
            # a slot with only press conferences: one page, no links, and the
            # podcasts / one-offs pages of that name are removed, not left empty
            only = pe.write_pages("2026-09-30", "overnight", ["J5unk3vEQmk"])
            left = sorted(p.name for p in (out / "2026-09-30").glob("*-overnight.md"))
            single = only["presser"].read_text(encoding="utf-8")
        self.assertEqual(names, {"presser": "digest-overnight.md", "podcast": "podcasts-overnight.md",
                                 "oneoff": "oneoffs-overnight.md"})
        self.assertTrue(presser.startswith("# NBA Pressers — 2026-09-30\n\n"
                                           "[Podcasts & shows](podcasts-overnight.md) · [One-offs](oneoffs-overnight.md)\n"))
        self.assertIn("#" + harden_md, presser)                 # video block = yt-quotes block, h1 -> h2
        self.assertNotIn("Chase Down", presser)                 # the podcast is on its own page now
        self.assertNotIn("Podcasts & shows\n", presser)
        self.assertEqual(left, ["digest-overnight.md"])
        self.assertTrue(single.startswith("# NBA Pressers — 2026-09-30\n\n# Best of the best\n"))

    def test_page_leads_with_best_of_the_best(self):
        with TempOutput():
            for vid, title in (("J5unk3vEQmk", "James Harden Media Availability"),
                               ("J5unk3vEQmA", "James Harden Postgame Media Availability")):
                pe.process_video(client_for(HARDEN, HARDEN_FIELDS), video(vid, title), "Cleveland Cavaliers")
            digest = pe.write_digest_file("2026-09-30", "digest.md").read_text(encoding="utf-8")
        top = digest.index(pe.TOP_QUOTES_HEADER)
        self.assertLess(top, digest.index("\n## "))
        block = digest[top:digest.index("\n---\n", top)]
        # 4 quotes -> 10% rounded up = 1; the unidentified reporter question never qualifies
        self.assertEqual(len(re.findall(r"(?m)^\*\*\d+\.", block)), 1)
        self.assertIn("**1. James Harden", block)
        self.assertNotIn("bench and how deep", block)
        src = re.search(r"From: \[[^\]]+\]\(https://www\.youtube\.com/watch\?v=([A-Za-z0-9_-]{11})\)", block)
        self.assertIsNotNone(src)
        # one clip pair per entry, with the quote's real number and second
        self.assertEqual(block.count("[Clip it]"), 1)
        self.assertIn(pe.clip_link_line(src.group(1), 118, 1), block)
        self.assertNotIn("CHECK OTHER YOUTUBE PODCASTS HERE", digest)

    def test_best_of_the_best_caps_per_video_and_skips_unscored(self):
        with TempOutput() as out:
            day = out / "2026-09-30"
            day.mkdir(parents=True)
            quotes = [{"rank": r, "speaker": f"Player {chr(64 + r)}", "timestamp": f"00:{r:02d}", "quote": f"q{r}",
                       "summary_phrase": "", "excerpt": "", "names_mentioned": [], "news_score": 9}
                      for r in range(1, 21)]
            quotes.append({"rank": 21, "speaker": "Coach A", "timestamp": "01:00", "quote": "x",
                           "news_score": None})
            for vid in ("AAAAAAAAAA1", "AAAAAAAAAA2"):
                (day / f"{vid}.json").write_text(json.dumps({"video_id": vid, "video_title": vid,
                                                             "channel_team": "T", "quotes": quotes}))
                (day / f"{vid}.md").write_text("x")
            lines = pe.top_quotes_block([day / "AAAAAAAAAA1.md", day / "AAAAAAAAAA2.md"])
        headers = [l for l in lines if re.match(r"^\*\*\d+\.", l)]
        self.assertEqual(len(headers), 2 * pe.TOP_QUOTES_PER_VIDEO)   # 10% of 42 = 5, capped at 2 per video
        self.assertEqual(pe.top_quotes_block([]), [])

    def test_best_of_the_best_puts_players_first(self):
        def q(rank, speaker, news, social=None, role=None):
            d = {"rank": rank, "speaker": speaker, "timestamp": f"00:{rank:02d}", "quote": f"q{rank}",
                 "summary_phrase": "", "excerpt": "", "names_mentioned": [], "news_score": news}
            if social is not None:
                d["social_score"] = social
            if role:
                d["structured"] = {"speaker_role": role}
            return d
        vids = {
            "AAAAAAAAAA1": [q(1, "Kenny Atkinson", 7), q(2, "Kenny Atkinson", 6)],          # coach tactics: out
            "AAAAAAAAAA2": [q(1, "Charles Lee", 9, role="coach")],                         # coach injury news: in
            "AAAAAAAAAA3": [q(1, "Alijah Martin", 3, social=9, role="player")],            # fun player line: in
            "AAAAAAAAAA4": [q(1, "Christian Braun", 2, social=8)],
            "AAAAAAAAAA5": [q(r, "Player B", 4) for r in range(1, 21)],
        }
        with TempOutput() as out:
            day = out / "2026-11-07"
            day.mkdir(parents=True)
            for vid, quotes in vids.items():
                (day / f"{vid}.json").write_text(json.dumps({"video_id": vid, "video_title": vid,
                                                             "channel_team": "T", "quotes": quotes}))
                (day / f"{vid}.md").write_text("x")
            block = "\n".join(pe.top_quotes_block([day / f"{v}.md" for v in vids]))
        self.assertNotIn("Kenny Atkinson", block)
        self.assertIn("Charles Lee", block)
        self.assertLess(block.index("Alijah Martin"), block.index("Charles Lee"))
        self.assertIn("Christian Braun", block)

    def test_each_page_has_its_own_best_of_the_best(self):
        def video(vid, ctype, speaker, title):
            quotes = [{"rank": r, "speaker": f"{speaker} {r}", "timestamp": f"00:{r:02d}", "quote": f"{vid} q{r}",
                       "summary_phrase": "", "excerpt": "", "names_mentioned": [], "news_score": 5}
                      for r in range(1, 6)]
            return {"video_id": vid, "video_title": title, "channel_team": "T", "content_type": ctype,
                    "quotes": quotes}
        vids = [video("AAAAAAAAAA1", "presser", "Player", "Player A Postgame"),
                video("AAAAAAAAAA2", "podcast", "Host", "Some Podcast"),
                video("AAAAAAAAAA3", "oneoff", "Guest", "Guest C on the trade")]
        with TempOutput() as out:
            day = out / "2026-11-07"
            day.mkdir(parents=True)
            for v in vids:
                (day / f"{v['video_id']}.json").write_text(json.dumps(v))
                (day / f"{v['video_id']}.md").write_text(pe.render_markdown(v["video_id"], "T", v), encoding="utf-8")
            pages = {c: p.read_text(encoding="utf-8") for c, p in pe.write_pages("2026-11-07", "14utc").items()}
        for ctype, page in pages.items():
            self.assertEqual(page.count("# Best of the best"), 1, ctype)
            block = page[page.index("# Best of the best"):page.index("\n---\n")]
            self.assertEqual(len(re.findall(r"(?m)^\*\*\d+\.", block)), pe.TOP_SECTION_MIN[ctype])
            self.assertNotIn("Best of the best: ", page)
        self.assertIn("Guest", pages["oneoff"])
        self.assertNotIn("Host", pages["presser"])

    def test_split_speakers_use_single_speaker_video(self):
        answer = "Honestly I did not know it but I knew I was going to have to play hard because my mom was coming"
        full = {"video_id": "AAAAAAAAAA1", "video_title": "Raptors Full Media Availability | Post Game",
                "channel_team": "Toronto Raptors", "speakers_seen": ["Jamal Shead", "Davion Mitchell"],
                "quotes": [{"rank": 1, "speaker": "Davion Mitchell", "timestamp": "07:38", "quote": answer,
                            "summary_phrase": "Davion Mitchell on his mom"},
                           {"rank": 2, "speaker": "Jamal Shead", "timestamp": "04:16",
                            "quote": "When guys are ready for their moment they pop and he popped tonight for us"}]}
        single = {"video_id": "AAAAAAAAAA2", "video_title": "Alijah Martin | Post Game",
                  "channel_team": "Toronto Raptors", "speakers_seen": ["Alijah Martin"],
                  "quotes": [{"rank": 1, "speaker": "Alijah Martin", "timestamp": "00:03",
                              "quote": "Honestly, I didn't know. It's being honest. " + answer[25:]}]}
        other_team = dict(single, video_id="AAAAAAAAAA3", channel_team="Milwaukee Bucks",
                          video_title="Bobby Smith | Post Game", speakers_seen=["Bobby Smith"],
                          quotes=[dict(single["quotes"][0], speaker="Bobby Smith")])
        with TempOutput() as out:
            day = out / "2026-11-07"
            day.mkdir(parents=True)
            for v in (full, single, other_team):
                (day / f"{v['video_id']}.json").write_text(json.dumps(v))
            self.assertEqual(pe.reconcile_split_speakers(["2026-11-07"]), ["2026-11-07"])
            fixed = json.loads((day / "AAAAAAAAAA1.json").read_text())
            self.assertTrue((day / "AAAAAAAAAA1.md").is_file())
        self.assertEqual(fixed["quotes"][0]["speaker"], "Alijah Martin")
        self.assertEqual(fixed["quotes"][0]["summary_phrase"], "Alijah Martin on his mom")
        self.assertEqual(fixed["quotes"][1]["speaker"], "Jamal Shead")      # no single video: untouched
        self.assertEqual(fixed["speakers_seen"], ["Jamal Shead", "Alijah Martin"])

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

    def test_team_channel_is_never_a_podcast_by_guess(self):
        # Nov 9 2026: Gemini's "podcast" no longer files a team-channel upload as a podcast
        t = "Luka, Austin and the Squad Discuss the Season"
        self.assertEqual(pe.classify_content_type(t, gemini_value="podcast", config=CONFIG), ("presser", "default"))
        self.assertEqual(pe.classify_content_type(t, gemini_value="presser", config=CONFIG), ("presser", "gemini"))
        self.assertEqual(pe.classify_content_type(t, config=CONFIG), ("presser", "default"))

    def test_team_live_streams_are_pressers(self):
        for t in ("LIVE: Pelicans vs. Clippers Postgame Interviews 11/8/2026",
                  "LIVE: Pelicans vs. Clippers Pregame w/ Jamahl Mosley 11/8/2026",
                  "POSTGAME SOUND: ORLANDO MAGIC VS. GOLDEN STATE WARRIORS | COACH SWEENEY",
                  "Media Availability: Brian Keefe and Trae Young | 11.6.26",
                  "Bucks Live Postgame Show"):
            self.assertEqual(pe.classify_content_type(t, config=CONFIG)[0], "presser", t)

    def test_channel_forced_presser_beats_podcast_keyword(self):
        # HoopsHype uploads are filed as pressers even when the title says "podcast"
        t = "HoopsHype Podcast: trade deadline reactions"
        self.assertEqual(pe.classify_content_type(t, config=CONFIG, forced="presser"), ("presser", "channel"))
        self.assertEqual(pe.classify_content_type(t, is_one_off=True, config=CONFIG, forced="presser")[0], "oneoff")


class AllUploadsChannelTests(unittest.TestCase):
    def _run(self, channel):
        uploads = [{"video_id": "aaaaaaaaaaa", "title": "Why the Knicks should trade for Giannis",
                    "published": "2099-01-01T00:00:00Z"},
                   {"video_id": "bbbbbbbbbbb", "title": "Knicks postgame press conference",
                    "published": "2099-01-01T00:00:00Z"}]
        orig = (pe.get_uploads_playlist_id, pe.list_recent_uploads)
        pe.get_uploads_playlist_id = lambda cid, key: "UU" + cid[2:]
        pe.list_recent_uploads = lambda pid, key, n: [dict(u) for u in uploads]
        try:
            from datetime import datetime, timezone
            return pe.discover_candidates([channel], CONFIG, "k", datetime(2000, 1, 1, tzinfo=timezone.utc))
        finally:
            pe.get_uploads_playlist_id, pe.list_recent_uploads = orig

    def test_team_channel_keeps_title_filter(self):
        got = self._run({"team": "New York Knicks", "channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx"})
        self.assertEqual([u["video_id"] for _, u in got], ["bbbbbbbbbbb"])
        self.assertNotIn("force_content_type", got[0][1])

    def test_hoopshype_channel_takes_every_upload_as_presser(self):
        hh = next(c for c in CONFIG["channels"] if c["team"] == "HoopsHype")
        got = self._run(hh)
        self.assertEqual([u["video_id"] for _, u in got], ["aaaaaaaaaaa", "bbbbbbbbbbb"])
        self.assertTrue(all(u["force_content_type"] == "presser" for _, u in got))


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
