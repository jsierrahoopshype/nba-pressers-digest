"""
Offline tests for speakers.py (speaker and subject resolution), made-up and
public names only, no network.

    python -m unittest discover -s pressers_v2/tests -p test_speakers.py -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import speakers as spk  # noqa: E402
import structured_quotes as sq  # noqa: E402

PLAYERS = [
    ("Kris Dunn", "LAC"), ("Ivica Zubac", "LAC"), ("Kevin Knox II", "DET"), ("Devin Booker", "PHX"),
    ("Kawhi Leonard", "LAC"), ("Kelly Olynyk", "SAS"), ("Dylan Harper", "SAS"), ("Jalen Green", "PHX"),
    ("Miles Bridges", "CHA"), ("Miles McBride", "NYK"),
]


def players_fixture(tmp: Path) -> sq.Players:
    meta = tmp / "players" / "metadata"
    meta.mkdir(parents=True)
    (meta / "players_all.json").write_text(json.dumps({"players": [
        {"full_name": n, "slug": sq.slugify(n), "team_abbrev": t, "active": True,
         "headshot": {"filename": f"{i}-{sq.slugify(n)}.png"}} for i, (n, t) in enumerate(PLAYERS)]}), encoding="utf-8")
    return sq.Players(tmp)


def vid(team, title, date, speakers=()):
    return {"channel_team": team, "video_title": title, "published": date + "T20:00:00Z",
            "quotes": [{"speaker": s, "speaker_confidence": "named"} for s in speakers]}


class SpeakerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.players = players_fixture(Path(cls.tmp.name))
        videos = [
            vid("Toronto Raptors", "Toronto Raptors Full Media Availability", "2026-10-06", ["Kawhi Leonard", "Kelly Olynyk"]),
            vid("LA Clippers", "Training Camp Media Availability: Kawhi Leonard", "2026-10-03"),
            vid("Indiana Pacers", "Ivica Zubac Media Availability", "2026-10-02"),
        ]
        cls.rosters = spk.Rosters(cls.players, videos, sq.name_key, sq.team_abbr)
        cls.r = spk.Resolver(spk.load_mapping(), cls.players, cls.rosters, sq.name_key, sq.title_names_full)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_mapping_has_every_team_and_channel(self):
        m = spk.load_mapping()
        self.assertEqual(len(m["teams"]), 30)
        config = json.loads((HERE.parent / "config.json").read_text(encoding="utf-8"))
        # all_uploads channels (HoopsHype's own) aren't team channels, so they
        # have no roster/coach mapping; speakers.py skips them (no team abbr).
        team_channels = {c["channel_id"] for c in config["channels"] if not c.get("all_uploads")}
        self.assertEqual(team_channels, {t["channel_id"] for t in m["teams"].values()})
        self.assertTrue(all(len(t["head_coach"].split()) >= 2 for t in m["teams"].values()))

    def test_coaches(self):
        s = self.r.speaker
        self.assertEqual(s("Coach Adelman", "coach", "DEN", "Denver Nuggets Practice", "2026-10-09")["name"], "David Adelman")
        self.assertEqual(s("Coach Sweeney", "coach", "ORL", "Postgame sound", "2026-10-09")["name"], "Sean Sweeney")
        self.assertEqual(s("Kerr", "coach", "GSW", "Warriors practice", "2026-10-09")["name"], "Steve Kerr")
        self.assertEqual(s("Darko Rajakovic", "coach", "TOR", "x", "2026-10-09")["name"], "Darko Rajaković")
        for raw, team in (("Michael Malone", "DEN"), ("Coach Smith", "GSW"), ("Jamahl Mosley", "ORL"), ("Nurse", "DEN")):
            got = s(raw, "coach", team, "Team practice availability", "2026-10-09")
            self.assertEqual((got["resolved"], got["name"]), (False, raw), raw)
        # An assistant the title names keeps his name.
        got = s("Jarrett Jack", "coach", "DET", "Post Game Press Conference l Jarrett Jack l Pistons", "2026-10-09")
        self.assertEqual((got["resolved"], got["how"]), (True, "title"))
        # A player's lone surname that is also the head coach's: the roster decides, not the coach.
        self.assertFalse(self.r.speaker("Johnson", "player", "SAS", "x", "2026-10-09")["resolved"])

    def test_players_against_the_channel_roster(self):
        s = self.r.speaker
        self.assertEqual(s("Chris Dunn", "player", "LAC", "Practice", "2026-10-13")["name"], "Kris Dunn")
        self.assertEqual(s("Kevin Knox", "player", "DET", "x", "2026-10-13")["name"], "Kevin Knox II")
        self.assertEqual(s("Dylan", "player", "SAS", "x", "2026-10-13")["name"], "Dylan Harper")
        self.assertFalse(s("Devin Booker", "player", "SAS", "x", "2026-10-13")["resolved"])   # not on that roster
        self.assertFalse(s("Miles", "player", "BOS", "x", "2026-10-13")["resolved"])
        # Kawhi Leonard: the Raptors' channel had him from Oct 6; the Clippers' title named him on Oct 3.
        self.assertTrue(s("Kawhi Leonard", "player", "TOR", "x", "2026-10-13")["resolved"])
        self.assertTrue(s("Kawhi Leonard", "player", "LAC", "x", "2026-10-03")["resolved"])
        # Kelly Olynyk: July list says SAS, the Raptors' channel named him on Oct 6.
        self.assertTrue(s("Kelly Olynyk", "player", "SAS", "x", "2026-10-01")["resolved"])
        self.assertFalse(s("Kelly Olynyk", "player", "SAS", "x", "2026-10-10")["resolved"])

    def test_subjects(self):
        sub = self.r.subject
        self.assertEqual(sub("Avita Zubac", ["IND"], "2026-10-09"), "Ivica Zubac")
        self.assertEqual(sub("Kevin Knox", ["DET"], "2026-10-09"), "Kevin Knox II")
        self.assertEqual(sub("Book", ["PHX"], "2026-10-09"), "Devin Booker")
        self.assertEqual(sub("Book", ["SAS"], "2026-10-09"), "Book")                 # nickname not on that roster
        self.assertEqual(sub("Jaylen Green", ["PHX"], "2026-10-09"), "Jalen Green")
        self.assertEqual(sub("Miles", ["CHA"], "2026-10-09"), "Miles Bridges")
        self.assertEqual(sub("Miles", ["CHA", "NYK"], "2026-10-09"), "Miles Bridges")  # the channel team first
        self.assertEqual(sub("Miles", ["BOS", "CHA", "NYK"], "2026-10-09"), "Miles")   # two fit: left as is
        self.assertEqual(sub("Kerr", ["GSW"], "2026-10-09"), "Steve Kerr")


class RecordTests(unittest.TestCase):
    def test_unresolved_speaker_never_best_or_public(self):
        from test_structured_quotes import stored_video
        import presser_extractor as pe
        data = stored_video(title="Denver Nuggets Practice Media Availability")
        data["channel_team"] = "Denver Nuggets"
        data["quotes"][0].update(speaker="Michael Malone", structured={
            "speaker_role": "coach", "subjects": ["John Roe"], "teams": ["DEN"], "topics": [], "context": "x.",
            "best": True, "prompt": pe.STRUCTURED_PROMPT_ID})
        recs = sq.records_for_video(data, sq.Players())
        r = recs[0]
        self.assertEqual((r["speaker"]["resolved"], r["speaker"]["team"], r["best"]), (False, "DEN", False))
        r["best"] = True   # even if something set it, the public JSON leaves it out
        self.assertEqual(sq.public_payload([r], "2026-27")["count"], 0)


if __name__ == "__main__":
    unittest.main()
