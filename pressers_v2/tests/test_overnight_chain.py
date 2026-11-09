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
