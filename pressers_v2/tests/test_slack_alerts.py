"""
Slack alert batching and dedupe (Nov 12 2026). Offline: the webhook is never called.

    python -m unittest discover -s pressers_v2/tests -p test_slack_alerts.py -v
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import presser_extractor as pe  # noqa: E402
import slack_notify  # noqa: E402
import slack_state  # noqa: E402

DAY = "2026-11-12"
OVERNIGHT_RUN = f"{DAY}-overnight"


def at(hhmm: str, day: str = DAY) -> datetime:
    return datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def item(vid: str, quote: str, ctype: str = "presser", single: bool = True, title: str = "", date: str = DAY):
    return {"video_id": vid, "title": title or f"Postgame {vid}", "channel": "Lakers", "top_quote": quote,
            "speaker": "Some Player", "social_post": f"DRAFT POST {vid}", "clip_count": 3, "date": date,
            "is_one_off": ctype == "oneoff", "content_type": ctype, "single_speaker": single}


QUOTES = [
    "We just kept attacking the paint and the shots started falling for us in the fourth quarter tonight.",
    "My ankle feels fine, I expect to play on Friday against Denver and the training staff agrees with me.",
    "Coach told me to be aggressive from the opening tip and I think we responded the right way all game.",
    "Honestly the crowd carried us tonight, that energy in the building is something I will never forget.",
]


class SlackAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_out = pe.OUTPUT_DIR
        self.old_payload = pe.SLACK_PAYLOAD_PATH
        self.old_run_id = pe.RUN_ID
        root = Path(self.tmp.name)
        pe.set_output_dir(root / "output")
        pe.OUTPUT_DIR.mkdir(parents=True)
        # an empty state file: no seeding from (absent) stored videos
        slack_state.save(slack_state.empty_state(), pe.SLACK_STATE_PATH)
        pe.SLACK_PAYLOAD_PATH = root / ".slack_payload.json"
        self.posted = []

    def tearDown(self):
        pe.set_output_dir(self.old_out)
        pe.SLACK_PAYLOAD_PATH = self.old_payload
        pe.RUN_ID = self.old_run_id
        self.tmp.cleanup()

    # ---- helpers ----
    def run_at(self, when: datetime, slot: str, items: list, pages: list | None = None):
        """One extractor run's Slack step, then the workflow's post step
        (always a successful post here)."""
        pe.RUN_ID = (f"{when:%Y-%m-%d}-overnight" if slot == "overnight" else f"{when:%Y-%m-%dT%H%MZ}-{slot}")
        if pe.SLACK_PAYLOAD_PATH.exists():
            pe.SLACK_PAYLOAD_PATH.unlink()
        today = pe.scheduled_date(pe.RUN_ID) or f"{when:%Y-%m-%d}"
        payload = pe.queue_slack(slot, today, when, items, pages or [], 10)
        if payload and payload["kind"] == "digest":
            self.posted.append(slack_notify.build_digest_text(payload, now=when))
            slack_notify.record_alerted(payload.get("alert_records") or {}, pe.SLACK_STATE_PATH)
        elif payload:
            self.posted.append("NO NEW")
        return payload

    def state(self) -> dict:
        return slack_state.load(pe.SLACK_STATE_PATH)

    def posted_ids(self, payload) -> list:
        return [it["video_id"] for g in payload["groups"] for it in g["items"]]

    # ---- 1. overnight batching ----
    def test_no_post_before_last_overnight_run_then_one_post(self):
        self.assertIsNone(self.run_at(at("02:05"), "overnight", [item("aaaaaaaaaa1", QUOTES[0])]))
        self.assertIsNone(self.run_at(at("05:40"), "overnight", [item("aaaaaaaaaa2", QUOTES[1])]))
        self.assertIsNone(self.run_at(at("09:10"), "overnight", []))
        self.assertEqual(self.posted, [])
        self.assertEqual(sorted(self.state()["pending"]), ["aaaaaaaaaa1", "aaaaaaaaaa2"])
        # the last overnight run (10 UTC): one post with the whole night
        p = self.run_at(at("10:31"), "overnight", [item("aaaaaaaaaa3", QUOTES[2])])
        self.assertEqual(sorted(self.posted_ids(p)), ["aaaaaaaaaa1", "aaaaaaaaaa2", "aaaaaaaaaa3"])
        self.assertEqual(len(self.posted), 1)
        self.assertIn(f"*NBA Pressers v2 — {DAY} overnight*", self.posted[0])
        self.assertEqual(self.state()["pending"], {})
        # a later overnight run with nothing new posts nothing
        self.assertIsNone(self.run_at(at("11:40"), "overnight", []))
        self.assertEqual(len(self.posted), 1)

    def test_catch_up_when_last_overnight_run_is_missed(self):
        self.run_at(at("03:00"), "overnight", [item("bbbbbbbbbb1", QUOTES[0])])
        self.run_at(at("08:50"), "overnight", [item("bbbbbbbbbb2", QUOTES[1])])
        self.assertEqual(self.posted, [])
        # no 10 UTC run; the 14:15 run (late, nothing new of its own) posts them
        p = self.run_at(at("16:20"), "14utc", [])
        self.assertEqual(p["kind"], "digest")
        self.assertEqual(sorted(self.posted_ids(p)), ["bbbbbbbbbb1", "bbbbbbbbbb2"])
        self.assertIn(f"{DAY} overnight", self.posted[0])
        self.assertEqual(self.state()["pending"], {})

    def test_catch_up_on_next_nights_overnight_run(self):
        self.run_at(at("04:00"), "overnight", [item("cccccccccc1", QUOTES[0])])
        # no run at all until the next night's first hourly run
        p = self.run_at(at("01:30", "2026-11-13"), "overnight", [item("cccccccccc2", QUOTES[1], date="2026-11-13")])
        self.assertEqual(self.posted_ids(p), ["cccccccccc1"])          # tonight's item still waits
        self.assertIn("2026-11-12 overnight, posted late", self.posted[0])
        self.assertEqual(list(self.state()["pending"]), ["cccccccccc2"])

    def test_failed_post_keeps_items_pending(self):
        pe.RUN_ID = OVERNIGHT_RUN
        p = pe.queue_slack("overnight", DAY, at("10:40"), [item("dddddddddd1", QUOTES[0])], [], 0)
        self.assertEqual(self.posted_ids(p), ["dddddddddd1"])
        # the webhook failed: nothing recorded, so the next run posts it again
        p = self.run_at(at("16:00"), "14utc", [])
        self.assertEqual(self.posted_ids(p), ["dddddddddd1"])

    # ---- 2. never re-alert ----
    def test_never_alerts_same_video_twice(self):
        p = self.run_at(at("16:00"), "14utc", [item("eeeeeeeeee1", QUOTES[0])])
        self.assertEqual(self.posted_ids(p), ["eeeeeeeeee1"])
        # the same id again (re-processed, force, re-filed...) is skipped
        p = self.run_at(at("21:00"), "20utc", [item("eeeeeeeeee1", QUOTES[3]), item("eeeeeeeeee2", QUOTES[1])])
        self.assertEqual(self.posted_ids(p), ["eeeeeeeeee2"])
        self.assertNotIn("eeeeeeeeee1", self.state()["pending"])

    def test_prune_after_14_days(self):
        self.run_at(at("16:00"), "14utc", [item("ffffffffff1", QUOTES[0])])
        self.assertIn("ffffffffff1", self.state()["alerted"])
        self.run_at(at("16:00", "2026-11-27"), "14utc", [])
        self.assertNotIn("ffffffffff1", self.state()["alerted"])

    def test_first_run_seeds_stored_videos_as_alerted(self):
        pe.SLACK_STATE_PATH.unlink()
        day = pe.OUTPUT_DIR / DAY
        day.mkdir()
        (day / "gggggggggg1.json").write_text(json.dumps({
            "video_id": "gggggggggg1", "quotes": [{"rank": 1, "quote": QUOTES[0]}],
            "processed_at": "2026-11-12T03:00:00Z"}), encoding="utf-8")
        p = self.run_at(at("16:00"), "14utc", [item("gggggggggg1", QUOTES[0]), item("gggggggggg2", QUOTES[2])])
        self.assertEqual(self.posted_ids(p), ["gggggggggg2"])

    # ---- 3. podcasts out of Slack ----
    def test_podcasts_excluded_but_page_link_kept(self):
        day = pe.OUTPUT_DIR / DAY
        day.mkdir()
        for name in ("digest-14utc.md", "podcasts-14utc.md"):
            (day / name).write_text("# page\n", encoding="utf-8")
        p = self.run_at(at("15:00"), "14utc", [item("hhhhhhhhhh1", QUOTES[0]),
                                               item("hhhhhhhhhh2", QUOTES[1], ctype="podcast", title="The Pod Show")])
        self.assertEqual(self.posted_ids(p), ["hhhhhhhhhh1"])
        text = self.posted[0]
        self.assertNotIn("The Pod Show", text)
        self.assertNotIn("Podcasts & shows*", text)
        self.assertIn(f"Podcasts &amp; shows: {slack_notify.pages_base()}/{DAY}/podcasts-14utc.html", text)
        self.assertNotIn("hhhhhhhhhh2", self.state()["pending"])

    def test_podcast_only_run_posts_no_items(self):
        p = self.run_at(at("15:00"), "14utc", [item("iiiiiiiiii1", QUOTES[0], ctype="podcast")])
        self.assertEqual(p["kind"], "no_new_videos")

    # ---- 4. quote dedupe ----
    def test_same_quote_keeps_single_speaker_video(self):
        full = item("jjjjjjjjjj1", QUOTES[0], single=False, title="Full Media Availability")
        single = item("jjjjjjjjjj2", QUOTES[0].upper().replace(",", "") + "!!", title="LeBron James postgame")
        p = self.run_at(at("15:00"), "14utc", [full, single, item("jjjjjjjjjj3", QUOTES[1])])
        self.assertEqual(self.posted_ids(p), ["jjjjjjjjjj2", "jjjjjjjjjj3"])
        st = self.state()
        self.assertEqual(st["alerted"]["jjjjjjjjjj1"].get("dup_of"), "jjjjjjjjjj2")   # never posted later
        self.assertEqual(st["pending"], {})

    def test_same_quote_as_one_already_alerted_today_is_dropped(self):
        self.run_at(at("15:00"), "14utc", [item("kkkkkkkkkk1", QUOTES[2])])
        near = QUOTES[2].replace("right way", "correct way")
        self.assertTrue(slack_state.same_quote(slack_state.normalize_quote(near),
                                               slack_state.normalize_quote(QUOTES[2])))
        p = self.run_at(at("21:00"), "20utc", [item("kkkkkkkkkk2", near), item("kkkkkkkkkk3", QUOTES[3])])
        self.assertEqual(self.posted_ids(p), ["kkkkkkkkkk3"])
        # only duplicates left: nothing posted beyond "no new", and they are settled
        p = self.run_at(at("22:00"), "20utc", [item("kkkkkkkkkk4", QUOTES[3])])
        self.assertEqual(p["kind"], "no_new_videos")
        self.assertIn("kkkkkkkkkk4", self.state()["alerted"])

    def test_different_quotes_not_deduped(self):
        self.assertFalse(slack_state.same_quote(slack_state.normalize_quote(QUOTES[0]),
                                                slack_state.normalize_quote(QUOTES[1])))
        self.assertFalse(slack_state.same_quote("yeah", "yeah"))   # too short to count

    def test_page_drops_repeated_quote_from_full_session(self):
        day = pe.OUTPUT_DIR / DAY
        day.mkdir()

        def video(vid, title, quotes):
            data = {"video_id": vid, "video_title": title, "channel_team": "Lakers", "run_id": "x",
                    "content_type": "presser",
                    "quotes": [{"rank": i, "speaker": sp, "timestamp": "0:10", "quote": q,
                                "summary_phrase": "s", "social_post": "p"} for i, (sp, q) in enumerate(quotes, 1)]}
            (day / f"{vid}.json").write_text(json.dumps(data), encoding="utf-8")
            (day / f"{vid}.md").write_text(pe.render_markdown(vid, "Lakers", data), encoding="utf-8")

        video("llllllllll1", "Lakers Full Media Availability",
              [("LeBron James", QUOTES[0]), ("JJ Redick", QUOTES[1])])
        video("llllllllll2", "LeBron James postgame", [("LeBron James", QUOTES[0] + " Yeah.")])
        page = pe.write_pages(DAY, "14utc")["presser"].read_text(encoding="utf-8")
        key = QUOTES[0][:50]
        # the list below "Best of the best" (that block repeats top quotes by design)
        body = page.split("\n---\n", 1)[1]
        self.assertEqual(body.count(key), 1, page)
        self.assertIn(QUOTES[1][:50], body)        # the session's other answer stays
        self.assertIn("## LeBron James postgame", body)
        self.assertIn("## Lakers Full Media Availability", body)

    # ---- 5/6. slimmer items, slot header ----
    def test_slack_item_has_no_draft_post(self):
        self.run_at(at("15:00"), "14utc", [item("mmmmmmmmmm1", QUOTES[0])])
        text = self.posted[0]
        self.assertIn("*Postgame mmmmmmmmmm1* (Lakers), 3 clip(s)", text)
        self.assertIn('Top quote: "', text)
        self.assertNotIn("Draft post", text)
        self.assertNotIn("DRAFT POST", text)

    def test_late_run_header_names_its_slot(self):
        # the 20:15 run of Nov 11 starting at 01:53 UTC on Nov 12
        self.run_at(at("01:53"), "20utc", [item("nnnnnnnnnn1", QUOTES[0], date="2026-11-11")])
        self.assertIn("*NBA Pressers v2 — 2026-11-11 20 UTC run, posted late*", self.posted[0])
        self.assertEqual(slack_state.slot_heading("2026-11-12", "14utc", at("16:00")), "2026-11-12 14 UTC run")


if __name__ == "__main__":
    unittest.main()
