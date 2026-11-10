"""
Offline tests for the overnight self-retrigger (tools/overnight_chain.py) and
the NBA-only filter on imported podcasts. No network.

    python -m unittest discover -s pressers_v2/tests -v
"""

import json
import os
import sys
import tempfile
import types as pytypes
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "tools"))
sys.path.insert(0, str(HERE))

import overnight_chain as oc  # noqa: E402
import presser_extractor as pe  # noqa: E402
from test_pressers import TempOutput  # noqa: E402

WORKFLOW = (HERE.parent.parent / ".github" / "workflows" / "pressers-v2.yml").read_text(encoding="utf-8")


def at(h, m=0):
    return datetime(2026, 11, 10, h, m, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self, tz=None):
        return self.now


class ChainDecisionTests(unittest.TestCase):
    def run_chain(self, now, result="success", failures="0", runs=()):
        calls = []

        def api(method, path, params=None, data=None):
            calls.append((method, path, json.loads(data) if data else None))
            if method == "GET":
                return oc.Resp(200, json.dumps({"workflow_runs": list(runs) if params["status"] == "in_progress" else []}))
            return oc.Resp(204, "")
        env = {"EXTRACT_RESULT": result, "CHAIN_FAILURES": failures, "GITHUB_RUN_ID": "1", "CHAIN_SLEEP_SECS": "3000"}
        with mock.patch.object(oc, "api", api), mock.patch.dict(os.environ, env), \
                mock.patch.object(oc, "datetime", mock.Mock(now=FakeClock(now))), \
                mock.patch.object(sys, "argv", ["overnight_chain.py"]):
            self.assertEqual(oc.main(), 0)
        return [c for c in calls if c[0] == "POST"]

    def test_starts_the_next_run_overnight(self):
        posts = self.run_chain(at(2, 10))
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["inputs"], {"chain": "true", "chain_failures": "0"})

    def test_never_starts_a_run_that_would_extract_after_ten(self):
        self.assertEqual(len(self.run_chain(at(9, 5))), 1)    # 09:05 + 50 min wait = 09:55: still fine
        self.assertEqual(self.run_chain(at(9, 15)), [])       # wait would end at 10:05
        self.assertEqual(self.run_chain(at(10, 30)), [])
        self.assertEqual(self.run_chain(at(23, 41)), [])      # a late 20:15 cron before midnight starts nothing
        self.assertEqual(len(self.run_chain(at(0, 6))), 1)    # ... but one landing after midnight seeds the chain

    def test_one_chain_at_a_time(self):
        other = {"id": 2, "status": "in_progress", "display_title": oc.CHAIN_TITLE}
        cron = {"id": 3, "status": "in_progress", "display_title": "Pressers v2 schedule 23 1-10 * * *"}
        self.assertEqual(self.run_chain(at(3), runs=[other]), [])
        self.assertEqual(len(self.run_chain(at(3), runs=[cron])), 1)   # a cron run doesn't carry the chain

    def test_two_failures_in_a_row_stop_the_chain(self):
        posts = self.run_chain(at(3), result="failure", failures="0")
        self.assertEqual(posts[0][2]["inputs"]["chain_failures"], "1")
        self.assertEqual(self.run_chain(at(3), result="failure", failures="1"), [])
        self.assertEqual(self.run_chain(at(3), result="success", failures="1")[0][2]["inputs"]["chain_failures"], "0")

    def test_skipped_or_cancelled_extract_stops(self):
        self.assertEqual(self.run_chain(at(3), result="skipped"), [])
        self.assertEqual(self.run_chain(at(3), result="cancelled"), [])

    def test_gate_waits_then_checks_the_clock(self):
        for now, want in ((at(4), "go=true"), (at(10, 1), "go=false")):
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "out"
                with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(out), "CHAIN_SLEEP_SECS": "0"}), \
                        mock.patch.object(oc, "datetime", mock.Mock(now=FakeClock(now))), \
                        mock.patch.object(sys, "argv", ["overnight_chain.py", "--gate"]):
                    oc.main()
                self.assertEqual(out.read_text(encoding="utf-8").strip(), want)

    def test_check_mode_starts_nothing(self):
        def api(method, path, params=None, data=None):
            body = json.loads(data)
            self.assertNotIn("chain", body["inputs"])
            return oc.Resp(422, '{"message":"Unexpected inputs provided"}')
        with mock.patch.object(oc, "api", api), mock.patch.object(sys, "argv", ["x", "--check"]):
            self.assertEqual(oc.main(), 0)

    def test_workflow_wiring(self):
        self.assertIn("actions: write", WORKFLOW)
        self.assertNotIn("secrets.GH_", WORKFLOW)                     # built-in token only
        self.assertIn("run-name: ${{ inputs.chain == 'true' && '" + oc.CHAIN_TITLE + "'", WORKFLOW)
        self.assertIn("inputs.chain == 'true') && 'overnight'", WORKFLOW)
        # the extract job, not the whole workflow, holds the concurrency group
        self.assertNotIn("\nconcurrency:", WORKFLOW)
        self.assertIn("  extract:\n    needs: gate\n    if: needs.gate.outputs.go == 'true'", WORKFLOW)
        self.assertIn("      group: pressers-v2\n      cancel-in-progress: false", WORKFLOW)


class NbaFilterTests(unittest.TestCase):
    def test_non_nba_quotes_left_off_podcast_pages(self):
        with TempOutput() as out:
            src = out / "ytq" / "2026-11-09"
            src.mkdir(parents=True)
            mixed = {"video_title": "Warriors + the NFL contender that needs Maxx Crosby", "speakers_seen": ["Host A"],
                     "quotes": [{"rank": 1, "speaker": "Host A", "timestamp": "01:00", "summary_phrase": "s",
                                 "names_mentioned": [], "excerpt": "", "quote": "Steph is back and the Warriors are a threat"},
                                {"rank": 2, "speaker": "Host A", "timestamp": "09:00", "summary_phrase": "s",
                                 "names_mentioned": [], "excerpt": "", "quote": "Maxx Crosby would fix that pass rush"}]}
            nfl = {"video_title": "The NFL Contender That Needs Maxx Crosby the Most", "speakers_seen": ["Host B"],
                   "quotes": [{"rank": 1, "speaker": "Host B", "timestamp": "01:00", "summary_phrase": "s",
                               "names_mentioned": [], "excerpt": "", "quote": "Crosby to the Bills makes sense"}]}
            for vid, d in (("0TJRHXX9moo", mixed), ("wi-Axz6yBtg", nfl)):
                (src / f"{vid}.json").write_text(json.dumps(d), encoding="utf-8")
                (src / f"{vid}.md").write_text(f"# {d['video_title']} — *Show*\n", encoding="utf-8")
            prompts = []

            class Models:
                def generate_content(self, model, contents, config):
                    prompts.append(contents[0])
                    payload = json.loads(contents[1])
                    nba = "Warriors" in payload["quotes"][0]["text"]
                    quotes = [{"rank": q["rank"], "speaker_confidence": "named", "news_score": 6, "social_score": 5, "social_post": "x",
                               "nba": nba and "Crosby" not in q["text"]} for q in payload["quotes"]]
                    return pytypes.SimpleNamespace(text=json.dumps({"quotes": quotes}), usage_metadata=None)
            client = pytypes.SimpleNamespace(models=Models())
            items = pe.import_ytq_podcasts(client, "", out / "ytq", datetime(2026, 11, 7, tzinfo=timezone.utc),
                                           run_id="2026-11-09-overnight")
            stored = json.loads((out / "2026-11-09" / "0TJRHXX9moo.json").read_text(encoding="utf-8"))
            nfl_stored = (out / "2026-11-09" / "wi-Axz6yBtg.json").is_file()
            page = pe.write_pages("2026-11-09", "overnight")["podcast"].read_text(encoding="utf-8")
            manifest = pe.build_clip_manifest("t", 10 ** 6)
        self.assertEqual(len(prompts), 2)                              # one call per video, no extra call
        self.assertTrue(all(p == pe.CLIP_FIELDS_PROMPT + pe.PODCAST_NBA_RULE for p in prompts))
        self.assertEqual([it["video_id"] for it in items], ["0TJRHXX9moo"])   # the NFL-only show isn't posted
        self.assertTrue(nfl_stored)                                    # ... but stored, so never imported again
        self.assertTrue(stored["quotes"][1]["non_nba"])
        self.assertIn("Steph is back", page)
        self.assertNotIn("pass rush", page)                            # the NFL quote of the mixed show
        self.assertNotIn("The NFL Contender", page)                    # a video with no NBA quote gets no block
        self.assertEqual({(c["video_id"], c["rank"]) for c in manifest["clips"]}, {("0TJRHXX9moo", 1)})

    def test_missing_flag_keeps_the_quote_and_pressers_unchanged(self):
        self.assertTrue(pe._is_nba(None) is not False and pe._is_nba(True) and pe._is_nba("true"))
        self.assertFalse(pe._is_nba(False) or pe._is_nba("false"))
        self.assertNotIn('"nba"', pe.CLIP_FIELDS_PROMPT)              # the press-conference prompt is unchanged


if __name__ == "__main__":
    unittest.main()


class BestMinimumAndOneoffTests(unittest.TestCase):
    """Nov 10 2026: every page leads with at least 3 picks (fewer only when
    fewer than 3 speakers qualify); yt-quotes ad hoc videos are one-offs."""

    def _store(self, day, vid, ctype, quotes):
        data = {"video_id": vid, "video_title": f"{vid} title", "channel_team": "Show", "content_type": ctype,
                "run_id": "2026-11-10-overnight", "quotes": quotes,
                "published": "2026-11-10T04:00:00Z", "processed_at": "2026-11-10T08:00:00Z"}
        day.mkdir(parents=True, exist_ok=True)
        (day / f"{vid}.json").write_text(json.dumps(data), encoding="utf-8")
        (day / f"{vid}.md").write_text(pe.render_markdown(vid, "Show", data), encoding="utf-8")

    @staticmethod
    def quote(rank, speaker, news=None, **kw):
        q = {"rank": rank, "speaker": speaker, "timestamp": f"0{rank}:00", "quote": f"{speaker} line {rank} here",
             "summary_phrase": "", "excerpt": "", "names_mentioned": []}
        if news is not None:
            q["news_score"] = news
        q.update(kw)
        return q

    def picks(self, page):
        block = page[page.index("# Best of the best"):page.index("\n---\n")]
        return len([l for l in block.split("\n") if l.startswith("**") and l[2:3].isdigit()])

    def test_small_page_gets_three_picks_even_unscored(self):
        with TempOutput() as out:
            day = out / "2026-11-10"
            # an unscored one-off: 4 quotes, 3 speakers, a coach without news
            self._store(day, "AAAAAAAAAA1", "oneoff", [
                self.quote(1, "Mike Conley"), self.quote(2, "Mike Conley"),
                self.quote(3, "Host A"), self.quote(4, "Chris Finch", news=3, structured={"speaker_role": "coach"})])
            page = pe.write_pages("2026-11-10", "overnight")["oneoff"].read_text(encoding="utf-8")
        self.assertEqual(self.picks(page), 3)
        self.assertEqual(page[:page.index("\n---\n")].count("Mike Conley:"), 1)   # one per speaker

    def test_one_speaker_page_and_tiny_page(self):
        with TempOutput() as out:
            day = out / "2026-11-09"
            self._store(day, "AAAAAAAAAA2", "podcast", [self.quote(r, "Theo Pinson", news=5) for r in range(1, 8)])
            one = pe.write_pages("2026-11-09", "20utc")["podcast"].read_text(encoding="utf-8")
            day2 = out / "2026-11-08"
            self._store(day2, "AAAAAAAAAA3", "presser", [self.quote(1, "Player A", news=2),
                                                         self.quote(2, "Player B", news=2)])
            two = pe.write_pages("2026-11-08", None)["presser"].read_text(encoding="utf-8")
        self.assertEqual(self.picks(one), 3)        # one speaker on the page: repeated to reach 3
        self.assertEqual(self.picks(two), 2)        # fewer than 3 quotes: all of them

    def test_big_page_keeps_ten_percent_and_rules(self):
        with TempOutput() as out:
            day = out / "2026-11-10"
            quotes = [self.quote(r, f"Player {chr(64 + r)}", news=5) for r in range(1, 26)]
            quotes += [self.quote(26 + r, f"Player {chr(64 + r)}", news=9) for r in range(1, 26)]
            self._store(day, "AAAAAAAAAA4", "presser", quotes[:25])
            self._store(day, "AAAAAAAAAA5", "presser", quotes[25:])
            page = pe.write_pages("2026-11-10", None)["presser"].read_text(encoding="utf-8")
        self.assertEqual(self.picks(page), 4)        # 10% of 50 = 5, but still at most 2 per video

    def test_ytq_oneoffs_imported_as_oneoffs_with_one_call(self):
        with TempOutput() as out:
            rot = out / "ytq" / "2026-11-09"
            adhoc = out / "ytq" / "oneoffs" / "2026-11-09"
            for d in (rot, adhoc):
                d.mkdir(parents=True)
            wnba = {"video_title": "Former WNBA Star Gets REAL", "speakers_seen": ["Guest"],
                    "quotes": [{"rank": 1, "speaker": "Guest", "timestamp": "01:00", "summary_phrase": "s",
                                "names_mentioned": [], "excerpt": "", "quote": "Leaving the league was hard"}]}
            conley = dict(wnba, video_title="Mike Conley Talks 20 Years in the NBA")
            for d, vid, v in ((rot, "UMeQRvuE--g", wnba), (adhoc, "UMeQRvuE--g", wnba), (adhoc, "a_n1fYIF6hU", conley)):
                (d / f"{vid}.json").write_text(json.dumps(v), encoding="utf-8")
                (d / f"{vid}.md").write_text(f"# {v['video_title']} — *Show*\n", encoding="utf-8")
            prompts = []

            class Models:
                def generate_content(self, model, contents, config):
                    prompts.append(contents[0])
                    return pytypes.SimpleNamespace(text=json.dumps({"quotes": [
                        {"rank": 1, "speaker_confidence": "named", "news_score": 6, "social_score": 7,
                         "social_post": "x", "nba": False}]}), usage_metadata=None)
            client = pytypes.SimpleNamespace(models=Models())
            items = pe.import_ytq_podcasts(client, "", out / "ytq", datetime(2026, 11, 9, tzinfo=timezone.utc),
                                           run_id="2026-11-10-overnight")
            stored = json.loads((out / "2026-11-09" / "UMeQRvuE--g.json").read_text(encoding="utf-8"))
            pages = pe.write_pages("2026-11-09", "overnight")
            oneoff_page = pages["oneoff"].read_text(encoding="utf-8")
        self.assertEqual(len(prompts), 2)                                  # one call per video, no more
        self.assertTrue(all(p == pe.CLIP_FIELDS_PROMPT for p in prompts))  # one-offs: no NBA filter
        self.assertEqual(sorted(it["content_type"] for it in items), ["oneoff", "oneoff"])
        self.assertEqual((stored["content_type"], stored["is_one_off"]), ("oneoff", True))
        self.assertNotIn("non_nba", stored["quotes"][0])
        self.assertEqual(stored["quotes"][0]["news_score"], 6)
        self.assertEqual(sorted(pages), ["oneoff"])                        # nothing on the podcasts page
        self.assertEqual(self.picks(oneoff_page), 2)                       # 2 quotes in all: both, same speaker

    def test_podcast_copy_of_an_adhoc_video_is_refiled(self):
        with TempOutput() as out:
            day = out / "2026-11-09"
            self._store(day, "UMeQRvuE--g", "podcast", [self.quote(1, "Guest", news=5, non_nba=True)])
            js = day / "UMeQRvuE--g.json"
            d = json.loads(js.read_text(encoding="utf-8"))
            d["source"] = pe.YTQ_SOURCE
            js.write_text(json.dumps(d), encoding="utf-8")
            changed = pe.refile_ytq_oneoffs({"UMeQRvuE--g"})
            again = pe.refile_ytq_oneoffs({"UMeQRvuE--g"})
            d = json.loads(js.read_text(encoding="utf-8"))
        self.assertEqual([c["video_id"] for c in changed], ["UMeQRvuE--g"])
        self.assertEqual(again, [])
        self.assertEqual(d["content_type"], "oneoff")
        self.assertNotIn("non_nba", d["quotes"][0])


class LateRunFilingTests(unittest.TestCase):
    """Nov 10 2026: a late daytime run is filed under the day it was scheduled for."""

    def test_scheduled_date(self):
        self.assertEqual(pe.scheduled_date("2026-11-10T0243Z-20utc"), "2026-11-09")   # 20:15 cron at 02:43 next day
        self.assertEqual(pe.scheduled_date("2026-11-09T2341Z-20utc"), "2026-11-09")   # late, same day
        self.assertEqual(pe.scheduled_date("2026-11-08T1849Z-14utc"), "2026-11-08")
        self.assertEqual(pe.scheduled_date("2026-11-10-overnight"), "2026-11-10")
        self.assertEqual(pe.scheduled_date("2026-11-10T1118Z-manual-1118"), "2026-11-10")
        self.assertEqual(pe.filed_date("2026-11-10", "2026-11-10T0243Z-20utc"), "2026-11-09")
        self.assertEqual(pe.filed_date("2026-11-08", "2026-11-10T0243Z-20utc"), "2026-11-08")  # older video: own day
        self.assertEqual(pe.filed_date("2026-11-10", "2026-11-10-overnight"), "2026-11-10")

    def test_live_run_files_videos_under_the_scheduled_day(self):
        old = (pe.RUN_ID, pe.SLOT_DATE)
        try:
            pe.RUN_ID, pe.SLOT_DATE = "2026-11-10T0243Z-20utc", "2026-11-09"
            self.assertEqual(pe.video_day_dir({"published": "2026-11-10T01:42:31Z"}).name, "2026-11-09")
            self.assertEqual(pe.video_day_dir({"published": "2026-11-09T19:00:00Z"}).name, "2026-11-09")
        finally:
            pe.RUN_ID, pe.SLOT_DATE = old

    def test_rebuild_moves_late_run_videos_and_drops_stale_pages(self):
        with TempOutput() as out:
            d9, d10 = out / "2026-11-09", out / "2026-11-10"
            for day, vid, rid in ((d10, "2ywtsVHfdgc", "2026-11-10T0243Z-20utc"),
                                  (d10, "Zam8Hpx3QP0", "2026-11-10-overnight"),
                                  (d9, "AAAAAAAAAA1", "2026-11-09T1849Z-14utc")):
                data = {"video_id": vid, "video_title": f"{vid} Postgame", "channel_team": "T",
                        "content_type": "presser", "run_id": rid, "published": "2026-11-10T01:00:00Z",
                        "quotes": [{"rank": 1, "speaker": f"Player {vid[:3]}", "timestamp": "00:10",
                                    "quote": "x y z", "news_score": 5}]}
                day.mkdir(parents=True, exist_ok=True)
                (day / f"{vid}.json").write_text(json.dumps(data), encoding="utf-8")
                (day / f"{vid}.md").write_text(pe.render_markdown(vid, "T", data), encoding="utf-8")
            pe.write_pages("2026-11-10", "20utc", ["2ywtsVHfdgc"])          # the misfiled page, as on Nov 10
            touched = pe.refile_late_runs(["2026-11-09", "2026-11-10"])
            for d in touched:
                pe.rebuild_day(d)
            left10 = sorted(p.name for p in d10.iterdir())
            has9 = sorted(p.name for p in d9.iterdir())
        self.assertEqual(touched, ["2026-11-09", "2026-11-10"])
        self.assertNotIn("digest-20utc.md", left10)
        self.assertNotIn("2ywtsVHfdgc.json", left10)
        self.assertIn("digest-overnight.md", left10)
        self.assertIn("2ywtsVHfdgc.json", has9)
        self.assertIn("digest-20utc.md", has9)
        self.assertIn("digest-14utc.md", has9)
