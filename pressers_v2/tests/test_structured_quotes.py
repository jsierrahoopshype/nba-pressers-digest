"""
Offline tests for the structured presser quotes and the Pressers section pages
(no network, no API keys, Gemini mocked, made-up records).

    python -m unittest discover -s pressers_v2/tests -p test_structured_quotes.py -v
"""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import presser_extractor as pe  # noqa: E402
import pressers_site as ps  # noqa: E402
import structured_quotes as sq  # noqa: E402
from test_pressers import HARDEN, TempOutput, client_for, video  # noqa: E402

STRUCTURED = {"content_type": "presser", "quotes": [
    {"rank": 1, "speaker_confidence": "named", "news_score": 6, "social_post": "James Harden likes the camp.",
     "speaker_role": "Player", "subjects": ["Mario Hezonja", "James Harden", "Mario Hezonja"], "teams": ["cle", "NYK", "toolong"],
     "topics": ["teammates", "weather", "injuries", "trades"], "context": "James Harden — asked about camp.", "best": "true"},
    {"rank": 2, "speaker_confidence": "inferred", "news_score": 2, "social_post": "x"}]}


def stored_video(vid="AAAAAAAAAAA", title="Cavs Practice | Jane Doe Media Availability", quotes=None,
                 processed="2026-10-19T19:53:42Z", published="2026-10-19T18:59:17Z", ctype="presser"):
    return {"video_title": title, "video_id": vid, "content_type": ctype, "channel_team": "Cleveland Cavaliers",
            "published": published, "processed_at": processed, "format": "ytq-1",
            "quotes": quotes if quotes is not None else [
                {"rank": 1, "speaker": "Jane Doe", "timestamp": "00:13", "start_seconds": 13, "speaker_confidence": "named",
                 "news_score": 8, "summary_phrase": "jane Doe on the new contract talks",
                 "names_mentioned": ["John Roe"], "quote": "I think the contract extension talks are going well and I want to stay here for a long time, honestly."},
                {"rank": 2, "speaker": "Unidentified speaker", "timestamp": "00:40", "start_seconds": 40,
                 "speaker_confidence": "inferred", "quote": "Can you talk about the bench this year and what you expect?"},
                {"rank": 3, "speaker": "Jane Doe", "timestamp": "01:10", "start_seconds": 70, "speaker_confidence": "named",
                 "news_score": 4, "quote": "",
                 "text_blocks": [{"speaker": "Reporter", "text": "What about the bench?"},
                                 {"speaker": "Jane Doe", "text": "We just have to keep working, one game at a time."}],
                 "structured": {"speaker_role": "player", "subjects": [], "teams": ["CLE"], "topics": [],
                                "context": "Jane Doe after practice.", "best": True}},
            ]}


def write_video(base: Path, data: dict, day="2026-10-19"):
    d = base / day
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{data['video_id']}.json").write_text(json.dumps(data), encoding="utf-8")


class ExtractionTests(unittest.TestCase):
    def test_same_call_extended_prompt_keeps_original_text(self):
        with TempOutput():
            client = client_for(HARDEN, STRUCTURED)
            status, data = pe.process_video(client, video("aaaaaaaaaaa", "James Harden Media Availability"), "Cleveland Cavaliers")
        self.assertEqual(status, "ok")
        self.assertEqual(client.models.calls, ["video", "text"])   # no extra Gemini call
        q1, q2 = data["quotes"]
        self.assertEqual(q1["structured"], {"speaker_role": "player", "subjects": ["Mario Hezonja"], "teams": ["CLE", "NYK"],
                                            "topics": ["teammates", "injuries"], "context": "James Harden, asked about camp.",
                                            "best": True})
        self.assertNotIn("structured", q2)       # an answer without the new fields adds nothing
        self.assertEqual(q1["news_score"], 6)    # the existing clip fields are unchanged
        self.assertTrue(pe.STRUCTURED_FIELDS_PROMPT.startswith("\n\nALSO"))

    def test_failed_text_call_adds_no_structured_block(self):
        with TempOutput():
            _, data = pe.process_video(client_for(HARDEN, None), video("bbbbbbbbbbb", "James Harden Media Availability"), "Cleveland Cavaliers")
        self.assertTrue(all("structured" not in q for q in data["quotes"]))


class RecordTests(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(sq.season_of("2026-10-19"), "2026-27")
        self.assertEqual(sq.season_of("2027-06-10"), "2026-27")
        self.assertEqual(sq.season_of("2027-07-01"), "2027-28")
        self.assertEqual(sq.ny_date("2026-10-20T02:30:00Z"), "2026-10-19")   # EDT
        self.assertEqual(sq.ny_date("2026-12-20T04:30:00Z"), "2026-12-19")   # EST
        self.assertEqual(sq.ny_date("2026-12-20T05:30:00Z"), "2026-12-20")
        a = sq.quote_id("AAAAAAAAAAA", "It's GREAT, really.")
        self.assertEqual(a, sq.quote_id("AAAAAAAAAAA", "its great really"))
        self.assertNotEqual(a, sq.quote_id("BBBBBBBBBBB", "its great really"))
        self.assertEqual(sq.team_abbr("LA Clippers"), "LAC")
        self.assertEqual(sq.team_abbr("gs"), "GSW")
        self.assertEqual(sq.name_key("Jaren Jackson Jr."), "jaren jackson")

    def test_records_for_video(self):
        counts = {}
        recs = sq.records_for_video(stored_video(), sq.Players(), None, counts)
        self.assertEqual(len(recs), 2)
        self.assertEqual(counts["unnamed"], 1)
        r1, r3 = recs
        self.assertEqual(r1["fields_source"], "backfill")
        self.assertEqual(r1["speaker"], {"name": "Jane Doe", "role": "player"})
        self.assertEqual(r1["subjects"], ["John Roe"])
        self.assertEqual(r1["teams"], ["CLE"])
        self.assertEqual(r1["topics"], ["contracts"])
        self.assertEqual(r1["context"], "Jane Doe on the new contract talks.")
        self.assertTrue(r1["best"])                       # news score 8, not filler
        self.assertEqual((r1["date"], r1["season"], r1["start_seconds"]), ("2026-10-19", "2026-27", 13))
        self.assertEqual(r1["transcript_check"], "unavailable")
        # Multi-speaker block: only the speaker's own words; the stock phrase can't be "best".
        self.assertEqual(r3["quote"], "We just have to keep working, one game at a time.")
        self.assertEqual(r3["fields_source"], "gemini")
        self.assertFalse(r3["best"])
        for key in ("id", "video_id", "video_title", "published_at", "start_seconds", "speaker", "subjects",
                    "teams", "topics", "quote", "context", "best"):
            self.assertIn(key, r1)

    def test_podcasts_are_left_out(self):
        counts = {}
        self.assertEqual(sq.records_for_video(stored_video(ctype="podcast"), sq.Players(), None, counts), [])
        self.assertEqual(counts["not_presser_videos"], 1)

    def test_transcript_mismatch_is_dropped(self):
        said = "I think the contract extension talks are going well and I want to stay here for a long time, honestly."
        t = sq.Transcripts(enabled=True, fetcher=lambda vid: [(10.0, 8.0, said)])
        counts = {}
        recs = sq.records_for_video(stored_video(), sq.Players(), t, counts)
        self.assertEqual([r["transcript_check"] for r in recs], ["matched"])
        self.assertEqual(counts["transcript_mismatch"], 1)

    def test_transcripts_give_up_when_blocked(self):
        def boom(vid):
            raise RuntimeError("blocked")
        t = sq.Transcripts(enabled=True, fetcher=boom)
        for _ in range(5):
            self.assertIsNone(t.words("AAAAAAAAAAA"))
        self.assertEqual(t.fetches, sq.TRANSCRIPT_GIVE_UP_AFTER)


class HistoryTests(unittest.TestCase):
    def test_append_only_and_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, hist = Path(tmp) / "out", Path(tmp) / "hist"
            write_video(out, stored_video())
            c1 = sq.update_history(out, hist)
            self.assertEqual(c1["records_appended"], 2)
            path = sq.history_path("2026-27", hist)
            first = path.read_text(encoding="utf-8")
            c2 = sq.update_history(out, hist)
            self.assertEqual(c2["records_appended"], 0)                 # nothing new, nothing written
            self.assertEqual(path.read_text(encoding="utf-8"), first)
            # Reprocessed: a new snapshot is appended; old lines stay; ids of unchanged quotes stay.
            data = stored_video(processed="2026-10-20T08:00:00Z")
            data["quotes"] = data["quotes"][:1]
            write_video(out, data)
            sq.update_history(out, hist)
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith(first))
            self.assertEqual(len(text.splitlines()), 3)
            view = sq.current_view(sq.read_history("2026-27", hist))
            self.assertEqual(len(view), 1)
            self.assertEqual(view[0]["id"], json.loads(first.splitlines()[0])["id"])

    def test_public_payload_has_best_only(self):
        recs = sq.records_for_video(stored_video(), sq.Players())
        p = sq.public_payload(recs, "2026-27")
        self.assertEqual(p["count"], 1)
        self.assertEqual(p["quotes"][0]["url"], "https://www.youtube.com/watch?v=AAAAAAAAAAA&t=13s")
        self.assertEqual(p["updated"], "2026-10-19T18:59:17Z")


def fake_view(n_quotes=6):
    recs = []
    for i in range(n_quotes):
        recs.append({
            "id": f"pq-{i:012d}", "video_id": "CCCCCCCCCCC" if i % 2 else "DDDDDDDDDDD",
            "video_title": "Cavs Practice | Media Availability", "published_at": f"2026-10-{10 + i:02d}T18:00:00Z",
            "date": f"2026-10-{10 + i:02d}", "season": "2026-27", "start_seconds": 60 + i,
            "speaker": {"name": "Jane Doe", "role": "player"} if i < 5 else {"name": "Coach Smith", "role": "coach"},
            "subjects": ["John Roe"], "teams": ["CLE"], "topics": ["teammates"] if i % 2 else [],
            "quote": f"I think John is playing great basketball and I love how he competes every night, number {i}.",
            "context": "After practice.", "best": i % 2 == 0, "news_score": 6, "fields_source": "gemini",
            "transcript_check": "unavailable", "extracted_at": "2026-10-19T00:00:00Z"})
    return sq.current_view(recs)


def fake_headshots(base: Path) -> Path:
    """A made-up nba-headshots checkout: two players, no images."""
    meta = base / "players" / "metadata"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "players_all.json").write_text(json.dumps({"players": [
        {"full_name": "Jane Doe", "slug": "jane-doe", "team_abbrev": "CLE", "active": True, "headshot": {"filename": "1-jane-doe.png"}},
        {"full_name": "John Roe", "slug": "john-roe", "team_abbrev": "CLE", "active": True, "headshot": {"filename": "2-john-roe.png"}},
    ]}), encoding="utf-8")
    return base


class SiteTests(unittest.TestCase):
    def build(self, view, out, state=None, today="2026-10-20", preview=True):
        media = ps.Media(fake_headshots(out.parent / (out.name + "-hs")))
        return ps.build_site(view, "2026-27", media, out_dir=out, state=state, preview=preview,
                             today=today, share_images=False)

    def test_pages_seo_footer_and_watch(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "site"
            state, s = self.build(fake_view(), out)
            idx = (out / "index.html").read_text(encoding="utf-8")
            player = (out / "players" / "jane-doe" / "index.html").read_text(encoding="utf-8")
            team = (out / "teams" / "cle" / "index.html").read_text(encoding="utf-8")
            self.assertTrue((out / "players" / "john-roe" / "index.html").is_file())   # the subject's page
            for page, path in ((idx, ""), (player, "players/jane-doe/"), (team, "teams/cle/")):
                self.assertIn('<meta name="robots" content="noindex, follow">', page)
                self.assertIn(f'<link rel="canonical" href="https://hoopsmatic.com/pressers/{path}">', page)
                self.assertIn(f'<meta property="og:url" content="https://hoopsmatic.com/pressers/{path}">', page)
                self.assertIn("og:image", page)
                self.assertNotIn("github.io", page)
                # Nothing from YouTube loads with the page: only links to it and the click-to-load script.
                self.assertNotRegex(page, r"<(iframe|img|script)[^>]+(youtube|ytimg)")
                self.assertIn("Anything wrong? Email us at", page)
                foot = re.search(r"<footer[^>]*>(.*)</footer>", page, re.S).group(1)
                self.assertEqual(re.sub(r"<[^>]+>", "", foot),
                                 "Anything wrong? Email us at hoopshype@hoopshype.comFor business inquiries, contact us at ads@hoopshype.com")
            self.assertIn('data-v="CCCCCCCCCCC" data-t="61"', player)
            self.assertIn("Watch at 1:01", player)
            titles = {re.search(r"<title>(.*?)</title>", p).group(1) for p in (idx, player, team)}
            self.assertEqual(len(titles), 3)
            self.assertIn("Jane Doe Press Conference Quotes 2026-27 | HoopsMatic", titles)
            # Eligibility: Jane Doe has 5 quotes, Coach Smith has no player page, the team has 6.
            pages = state["pages"]
            self.assertTrue(pages["pressers/players/jane-doe/"]["eligible"])
            self.assertTrue(pages["pressers/players/john-roe/"]["eligible"])
            self.assertNotIn("pressers/players/coach-smith/", pages)
            sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
            self.assertIn("<loc>https://hoopsmatic.com/pressers/players/jane-doe/</loc><lastmod>2026-10-14</lastmod>", sitemap)
            self.assertTrue((out / "assets" / "watch.js").is_file())

    def test_player_pages_need_five_quotes_to_be_eligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "site"
            state, _ = self.build(fake_view(4), out, preview=False)
            self.assertFalse(state["pages"]["pressers/players/jane-doe/"]["eligible"])
            page = (out / "players" / "jane-doe" / "index.html").read_text(encoding="utf-8")
            self.assertIn('content="noindex, follow"', page)
            self.assertNotIn("players/jane-doe/", (out / "sitemap.xml").read_text(encoding="utf-8"))
            idx = (out / "index.html").read_text(encoding="utf-8")
            self.assertIn('content="index, follow"', idx)

    def test_lastmod_moves_only_when_content_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "site"
            view = fake_view()
            s1, _ = self.build(view, out, today="2026-10-20")
            s2, sm = self.build(view, out, state=s1, today="2026-11-30")
            self.assertEqual(s1["pages"], s2["pages"])
            self.assertEqual(sm["changed"], 0)
            # A new quote about John Roe only: his page (and the team's) move, Jane Doe's stays.
            extra = dict(view[0], id="pq-new000000000", quote="John Roe is the best defender I have played with, no question about it.",
                         speaker={"name": "Sam Poe", "role": "player"}, date="2026-10-18", published_at="2026-10-18T20:00:00Z",
                         video_id="EEEEEEEEEEE")
            s3, _ = self.build(sq.current_view(view + [extra]), out, state=s2, today="2026-12-01")
            self.assertEqual(s3["pages"]["pressers/players/jane-doe/"], s2["pages"]["pressers/players/jane-doe/"])
            self.assertEqual(s3["pages"]["pressers/players/john-roe/"]["lastmod"], "2026-10-18")  # the new quote's date
            self.assertNotEqual(s3["pages"]["pressers/teams/cle/"]["hash"], s2["pages"]["pressers/teams/cle/"]["hash"])
            # The new quote removed again: John Roe's page changes back, with no newer quote: today.
            s4, _ = self.build(view, out, state=s3, today="2026-12-02")
            self.assertEqual(s4["pages"]["pressers/players/john-roe/"]["lastmod"], "2026-12-02")

    def test_no_eyebrows_in_css(self):
        css = (ps.ASSETS / "pressers.css").read_text(encoding="utf-8")
        self.assertNotIn("text-transform", css)
        self.assertNotRegex(css, r"\.(eyebrow|kicker)")

    @unittest.skipUnless(__import__("importlib").util.find_spec("PIL"), "Pillow not installed")
    def test_share_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            state, s = ps.build_site(fake_view(), "2026-27", ps.Media(None), out_dir=out, today="2026-10-20")
            self.assertEqual(s["share_rendered"], s["pages"])
            _, s2 = ps.build_site(fake_view(), "2026-27", ps.Media(None), out_dir=out, state=state, today="2026-10-21")
            self.assertEqual((s2["share_rendered"], s2["share_kept"]), (0, s["pages"]))


if __name__ == "__main__":
    unittest.main()
