"""Third-party (quotes-only) channels, per-channel title rules and the daily
cap for Pelicans/Suns (Jorge, Nov 16 2026). Titles are real uploads from the
week of Nov 9-16, 2026."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import presser_extractor as pe  # noqa: E402
import make_presser_clips as mc  # noqa: E402

CONFIG = json.loads((HERE.parent / "config.json").read_text(encoding="utf-8"))


def channel(team, cid=None):
    for c in CONFIG["channels"]:
        if c["team"] == team and c.get("third_party") and (cid is None or c["channel_id"] == cid):
            return c
    raise KeyError(team)


class TitleRuleTests(unittest.TestCase):
    def keep(self, title, ch):
        return pe.title_filter(title, CONFIG, ch) == ""

    def test_global_rules_unchanged_without_channel(self):
        self.assertTrue(self.keep("Hornets at Clippers: Coach Lamar Skeeter Postgame Media Availability | 11/15/2026", None))
        self.assertFalse(self.keep("Game Highlights | Hornets at Clippers | 11/15/2026", None))
        self.assertTrue(self.keep("Deni Avdija Walkoff Interview | Portland Trail Blazers 120, Sacramento Kings 107", None))

    def test_nbc_split_by_team(self):
        gsw, sac = channel("Golden State Warriors"), channel("Sacramento Kings")
        t1 = "Steve Kerr, Yaxel Lendeborg break down Warriors' loss to Spurs | NBC Sports Bay Area"
        t2 = "Zach LaVine admits Kings' turnovers were 'self-inflicted wound' | NBC Sports California"
        self.assertTrue(self.keep(t1, gsw))
        self.assertFalse(self.keep(t1, sac))
        self.assertTrue(self.keep(t2, sac))
        self.assertFalse(self.keep(t2, gsw))

    def test_nbc_studio_and_game_call_segments_dropped(self):
        # Nov 17 2026 overnight: these two got in and were analysts, not pressers.
        gsw, sac = channel("Golden State Warriors"), channel("Sacramento Kings")
        self.assertFalse(self.keep("Warriors outpaced by Pelicans forward Saddiq Bey in first half | NBC Sports Bay Area", gsw))
        self.assertFalse(self.keep("Warriors can’t overcome another slow start in loss to Pelicans | NBC Sports Bay Area", gsw))
        self.assertTrue(self.keep("Steve Kerr states first quarter ‘deflated’ Warriors vs. Pelicans | NBC Sports Bay Area", gsw))
        self.assertFalse(self.keep("Kings’ defensive lapses continue during loss at home vs. Thunder | NBC Sports California", sac))
        self.assertTrue(self.keep("Doug Christie reveals Keegan Murray was sick in Kings’ loss | NBC Sports California", sac))

    def test_lakers_fam_highlights_allowed_only_with_presser_word(self):
        lal = channel("Los Angeles Lakers")
        self.assertTrue(self.keep("Lakers vs Suns | Postgame Interviews x Highlights: November 15, 2026", lal))
        self.assertFalse(self.keep("Lakers vs Suns Highlights | November 15, 2026", lal))

    def test_dallas_sports_journal(self):
        dal = channel("Dallas Mavericks")
        self.assertTrue(self.keep("Cooper Flagg Postgame Interview | Mavericks vs. Celtics | Nov. 15, 2026", dal))
        self.assertFalse(self.keep("Mavs vs. Celtics Full Postgame | Dusty May, Cooper Flagg & Zaccharie Risacher", dal))

    def test_chgo_bulls_locker_room_only_bulls(self):
        chi = channel("Chicago Bulls")
        self.assertTrue(self.keep("INSIDE THE LOCKER ROOM: Tiago Splitter, Norman Powell & Josh Giddey Celebrate Bulls' FIRST Home Win!", chi))
        self.assertFalse(self.keep("INSIDE THE LOCKER ROOM: Bears celebrate win over Packers", chi))

    def test_third_party_channels_flagged(self):
        for c in CONFIG["channels"]:
            if c.get("third_party"):
                self.assertTrue(c.get("active"))
                self.assertIn("exclude_title_keywords", c)


class DailyCapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = pe.OUTPUT_DIR
        pe.set_output_dir(Path(self.tmp.name))

    def tearDown(self):
        pe.set_output_dir(self.old)
        self.tmp.cleanup()

    def v(self, vid, title, published):
        return {"video_id": vid, "title": title, "published": published}

    def test_tiers(self):
        nop = "New Orleans Pelicans"
        self.assertEqual(pe.video_tier("Saddiq Bey | Pelicans vs. Grizzlies Postgame Interview 11/13/26", nop), 0)
        self.assertEqual(pe.video_tier("LIVE: Pelicans vs. Grizzlies Postgame Interviews 11/13/2026", nop), 1)
        self.assertEqual(pe.video_tier("Jamahl Mosley | Pelicans vs. Grizzlies Postgame Interview 11/13/26", nop), 2)
        self.assertEqual(pe.video_tier("LIVE: Pelicans vs. Grizzlies Pregame w/ Jamahl Mosley 11/13/2026", nop), 3)
        self.assertEqual(pe.video_tier("Ryan Dunn Practice Media Availability | 11-14-26", "Phoenix Suns"), 3)

    def test_pelicans_game_night_keeps_player_and_session(self):
        nop = "New Orleans Pelicans"
        # Evening run: the pregame stream is processed on its own.
        day = Path(self.tmp.name) / "2026-11-13"
        day.mkdir()
        (day / "RfaqD9CLzf0.json").write_text(json.dumps({
            "channel_team": nop, "video_title": "LIVE: Pelicans vs. Grizzlies Pregame w/ Jamahl Mosley 11/13/2026",
            "published": "2026-11-13T23:30:00Z"}), encoding="utf-8")
        # Overnight run: three postgame videos, room for two more today.
        q = [(nop, self.v("lzyC38jH_t4", "LIVE: Pelicans vs. Grizzlies Postgame Interviews 11/13/2026", "2026-11-14T03:40:00Z")),
             (nop, self.v("qs2pu9tqn4I", "Jamahl Mosley | Pelicans vs. Grizzlies Postgame Interview 11/13/26", "2026-11-14T03:50:00Z")),
             (nop, self.v("Ynw51plEkWY", "Saddiq Bey | Pelicans vs. Grizzlies Postgame Interview 11/13/26", "2026-11-14T03:55:00Z")),
             ("Toronto Raptors", self.v("mrVkQQyGfwU", "Jamal Shead | Post Game vs New York Knicks", "2026-11-16T03:00:00Z"))]
        kept = {v["video_id"] for _, v in pe.apply_daily_caps(q, {nop: (3, 1)})}
        # Mosley's postgame answers are inside the full session that was kept.
        self.assertEqual(kept, {"lzyC38jH_t4", "Ynw51plEkWY", "mrVkQQyGfwU"})

    def test_suns_practice_day_one_only_and_counts_earlier_runs(self):
        phx = "Phoenix Suns"
        day = Path(self.tmp.name) / "2026-11-14"
        day.mkdir()
        (day / "lp2JBkYnxxY.json").write_text(json.dumps({
            "channel_team": phx, "video_title": "Head Coach Jordan Ott Practice Media Availability | 11-14-26",
            "published": "2026-11-14T20:00:00Z"}), encoding="utf-8")
        q = [(phx, self.v("iJoY-PAM8W4", "Jordan Goodwin Practice Media Availability | 11-14-26", "2026-11-14T20:10:00Z")),
             (phx, self.v("bM6DdEimtro", "Ryan Dunn Practice Media Availability | 11-14-26", "2026-11-14T20:20:00Z"))]
        self.assertEqual(pe.apply_daily_caps(q, {phx: (3, 1)}), [])


class NoClipsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = pe.OUTPUT_DIR
        pe.set_output_dir(Path(self.tmp.name))
        pe._NO_CLIP_IDS = None

    def tearDown(self):
        pe.set_output_dir(self.old)
        pe._NO_CLIP_IDS = None
        self.tmp.cleanup()

    def test_no_clip_links_and_not_in_manifest(self):
        day = Path(self.tmp.name) / "2026-11-16"
        day.mkdir()
        quote = {"rank": 1, "speaker": "Jayson Tatum", "speaker_confidence": "named", "quote": "I'm still not 100%.",
                 "start_seconds": 30, "end_seconds": 45, "timestamp": "0:30"}
        (day / "AAAAAAAAAAA.json").write_text(json.dumps({
            "channel_team": "Boston Celtics", "video_title": "Jayson Tatum Postgame", "no_clips": True, "third_party": True,
            "published": pe.datetime.now(pe.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "quotes": [quote]}),
            encoding="utf-8")
        md = ("**1. Tatum**\n\n[https://www.youtube.com/watch?v=AAAAAAAAAAA&t=30s]"
              "(https://www.youtube.com/watch?v=AAAAAAAAAAA&t=30s)\n\n**2. Other**\n\n"
              "[https://www.youtube.com/watch?v=BBBBBBBBBBB&t=5s](https://www.youtube.com/watch?v=BBBBBBBBBBB&t=5s)")
        out = pe.add_clip_links(md)
        self.assertNotIn("presserclips://clip?v=AAAAAAAAAAA", out)
        self.assertIn("presserclips://clip?v=BBBBBBBBBBB", out)
        manifest = pe.build_clip_manifest("test", 48)
        self.assertEqual([c for c in manifest["clips"] if c["video_id"] == "AAAAAAAAAAA"], [])
        self.assertIsNone(mc.clip_from_video_json(json.loads((day / "AAAAAAAAAAA.json").read_text()),
                                                  "2026-11-16", "AAAAAAAAAAA", 30))


if __name__ == "__main__":
    unittest.main()
