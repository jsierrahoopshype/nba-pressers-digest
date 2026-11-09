"""
Overnight self-retrigger for pressers-v2.yml (Jorge, Nov 10 2026).

GitHub fires only a few of the ten hourly overnight crons (Nov 9: one, at
08:03 UTC; the first one usually lands at 07-08 UTC). So every scheduled run
that ends between 00:00 and ~09:10 UTC (the 20:15 cron often lands after
midnight) and every run this script started ends by starting the next one through the workflow_dispatch
API with the run's built-in GITHUB_TOKEN (no new secret). The new run waits
~50 min in its gate job before it extracts, so each run still finishes (and
the Pressers section workflow still follows it) right after its extract.

  - never after 10:00 UTC: no run is started when its wait would end at or
    after 10:00, and the gate checks the clock again after the wait
  - never more than one at a time: no run is started while another chain
    run is queued, waiting or running (so there is only ever one chain),
    and the extract job's concurrency group makes any extract that overlaps
    a cron or manual run wait for it
  - two failed runs in a row stop the chain (the count travels in the
    chain_failures input)

Usage:
    python pressers_v2/tools/overnight_chain.py            # chain job: start the next run or stop
    python pressers_v2/tools/overnight_chain.py --gate     # a chain run's gate: wait, then go or not
    python pressers_v2/tools/overnight_chain.py --check    # only prove the token may dispatch
Env: GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_RUN_ID, EXTRACT_RESULT,
CHAIN_FAILURES, CHAIN_SLEEP_SECS (default 3000), CHAIN_REF (default main).
Never fails the workflow except --check with a token that can't dispatch.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

WORKFLOW = "pressers-v2.yml"
LAST_START_HOUR_UTC = 10          # no run is started at or after 10:00 UTC
MAX_FAILURES_IN_A_ROW = 2
DEFAULT_SLEEP_SECS = 3000         # the next run's ~50 min wait + ~2 min run = about one run an hour
CHAIN_TITLE = "Pressers v2 overnight chain"
ACTIVE = ("queued", "in_progress", "waiting", "pending", "requested")


def log(msg: str) -> None:
    print(f"[chain] {msg}", flush=True)


class Resp:
    def __init__(self, status_code: int, text: str):
        self.status_code, self.text = status_code, text

    def json(self):
        return json.loads(self.text or "{}")


def api(method: str, path: str, params: dict | None = None, data: str | None = None) -> Resp:
    """GitHub REST call with the run's token (standard library only, so the
    runner's own python3 can run this without installing anything)."""
    repo = os.environ.get("GITHUB_REPOSITORY") or "jsierrahoopshype/nba-pressers-digest"
    url = f"https://api.github.com/repos/{repo}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method, data=data.encode("utf-8") if data else None, headers={
        "Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN', '')}",
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return Resp(r.status, r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return Resp(e.code, e.read().decode("utf-8", "replace"))


def next_failures(extract_result: str, failures: int) -> int | None:
    """Failure count to hand on, or None when the chain should stop here."""
    r = (extract_result or "").strip().lower()
    if r == "success":
        return 0
    if r == "failure":
        return failures + 1
    return None   # skipped (too late), cancelled (superseded or stopped by hand): stop


def may_start_now(now: datetime) -> bool:
    """00:00-09:59 UTC (a run ending at 23:xx is "after 10:00" and starts nothing)."""
    return now.hour < LAST_START_HOUR_UTC


def other_chain_run_active(runs: list, own_id: str) -> bool:
    """True when another chain run is queued, waiting in its gate or running."""
    for r in runs:
        if str(r.get("id")) == str(own_id) or r.get("status") not in ACTIVE:
            continue
        if (r.get("display_title") or "") == CHAIN_TITLE:
            return True
    return False


def active_runs() -> list:
    out = []
    for status in ("queued", "in_progress", "waiting", "pending", "requested"):
        resp = api("GET", f"/actions/workflows/{WORKFLOW}/runs", params={"status": status, "per_page": 50})
        if resp.status_code == 200:
            out.extend(resp.json().get("workflow_runs") or [])
    return out


def dispatch(failures: int) -> int:
    ref = os.environ.get("CHAIN_REF") or "main"
    resp = api("POST", f"/actions/workflows/{WORKFLOW}/dispatches",
               data=json.dumps({"ref": ref, "inputs": {"chain": "true", "chain_failures": str(failures)}}))
    return resp.status_code


def check() -> int:
    """Prove the token may dispatch without starting a run: an input the
    workflow doesn't declare is refused with 422 only after the permission
    check (403 = no permission)."""
    resp = api("POST", f"/actions/workflows/{WORKFLOW}/dispatches",
               data=json.dumps({"ref": os.environ.get("CHAIN_REF") or "main",
                                "inputs": {"chain_permission_probe": "x"}}))
    log(f"permission probe: HTTP {resp.status_code} {resp.text[:200]}")
    if resp.status_code == 422:
        log("OK: the built-in token may start this workflow (probe refused for its unknown input; no run started)")
        return 0
    log("FAIL: the token could not start the workflow")
    return 1


def gate() -> int:
    """Wait, then write go=true|false to GITHUB_OUTPUT."""
    secs = int(os.environ.get("CHAIN_SLEEP_SECS") or DEFAULT_SLEEP_SECS)
    log(f"overnight chain run: waiting {secs}s before extracting")
    time.sleep(secs)
    now = datetime.now(timezone.utc)
    go = may_start_now(now)   # an overlapping cron run is fine: the extract concurrency group queues behind it
    if not go:
        log(f"{now:%H:%M} UTC, past 10:00: nothing to do")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"go={'true' if go else 'false'}\n")
    log(f"go={go}")
    return 0


def main() -> int:
    if "--check" in sys.argv:
        return check()
    if "--gate" in sys.argv:
        return gate()
    failures = int(os.environ.get("CHAIN_FAILURES") or 0)
    result = os.environ.get("EXTRACT_RESULT") or ""
    nxt = next_failures(result, failures)
    if nxt is None:
        log(f"this run's extract job ended '{result}': chain stops here")
        return 0
    if nxt >= MAX_FAILURES_IN_A_ROW:
        log(f"::warning::{nxt} failed runs in a row: overnight chain stopped")
        print(f"::warning::Overnight chain stopped after {nxt} failed runs in a row")
        return 0
    secs = int(os.environ.get("CHAIN_SLEEP_SECS") or DEFAULT_SLEEP_SECS)
    now = datetime.now(timezone.utc)
    if not may_start_now(now) or not may_start_now(now + timedelta(seconds=secs)):
        log(f"{now:%H:%M} UTC: the next run would extract at or after 10:00 UTC; chain stops (the crons take over)")
        return 0
    try:
        if other_chain_run_active(active_runs(), os.environ.get("GITHUB_RUN_ID") or ""):
            log("another chain run is already queued or running: it carries the chain")
            return 0
        code = dispatch(nxt)
    except (urllib.error.URLError, OSError, ValueError) as e:
        log(f"::warning::could not start the next run ({type(e).__name__}); chain stops")
        return 0
    log(f"started the next overnight run (HTTP {code})" if code == 204 else
        f"::warning::dispatch refused (HTTP {code}); chain stops")
    return 0


if __name__ == "__main__":
    sys.exit(main())
