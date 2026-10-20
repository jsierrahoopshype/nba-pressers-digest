"""
Speaker and subject resolution for the structured presser quotes
(structured_quotes.py). Nothing here touches the extractor, the digest or Slack.

Inputs:
  teams_2026_27.json   committed mapping: team -> YouTube channel and the
                       2026-27 head coach, plus a short nickname list
  nba-headshots        players_all.json (names, July 2026 teams)
  the stored videos    each team channel's own video titles this season

A team's roster = every known NBA player the team's own channel named this
season (in a video title, or as a named speaker: the channel is the team's
own), plus the players nba-headshots (July 2026) lists on it, until another
team's channel names them (offseason moves and trades).

Speakers: the speaker's team is the video's channel. A coach, a lone name or a
"Coach X" title resolves to that team's head coach only when it fits (the full
name, or X / the lone name is the head coach's surname); otherwise the speaker
is unresolved. Players resolve against the channel team's roster (exact, a
short first name or suffix, or a misspelling one roster player clearly fits).
A coach whose full name the video title gives (an assistant at a media
availability) is resolved by the title. Unresolved speakers are never "best"
and never published in the public JSON.

Subjects: suffixes (Jr., Sr., II, III, IV) are ignored when matching;
misspellings are fixed against the rosters of the teams in the video; a lone
first name or a nickname resolves only when exactly one roster player fits.
"""

import hashlib
import json
import re
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAPPING_PATH = ROOT / "teams_2026_27.json"
TITLE_PREFIX = re.compile(r"^(?:head coach|coach|hc)\s+", re.I)
FIRST_NAME_MIN = 0.6     # first-name similarity for a misspelling with the same surname ("Avita"/"Ivica")
FULL_NAME_MIN = 0.88     # whole-name similarity otherwise ("Jaylen Green"/"Jalen Green")


def load_mapping(path: Path | None = None) -> dict:
    return json.loads(Path(path or MAPPING_PATH).read_text(encoding="utf-8"))


def rules_id(mapping: dict) -> str:
    """Short id of the resolution rules + mapping: records built with another
    id are rebuilt (a new history snapshot) when it changes."""
    blob = json.dumps(mapping, sort_keys=True, ensure_ascii=False) + "|speakers-1"
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:8]


def _tokens(name_key: str) -> list:
    return name_key.split()


class Rosters:
    """2026-27 team rosters (see module doc). A player is on team T for a video
    dated d when T's own channel named him this season (in a video title, or as
    a named speaker), or nba-headshots (July 2026) lists him on T and no other
    team's channel had named him before d."""

    def __init__(self, players, videos: list, name_key, team_abbr):
        self.key = name_key
        self.evidence = {}      # team -> {key: full name}
        first_seen = {}         # key -> {team: earliest date a channel named him}
        full = {k: p["full_name"] for k, p in players.by_key.items() if p}
        by_len = {}
        for k in full:
            by_len.setdefault(len(k.split()), set()).add(k)

        def note(team, k, date):
            self.evidence.setdefault(team, {})[k] = full[k]
            seen = first_seen.setdefault(k, {})
            if date < seen.get(team, "9999"):
                seen[team] = date

        for data in videos:
            team = team_abbr(data.get("channel_team") or "")
            if not team:
                continue
            date = (data.get("published") or "")[:10]
            toks = name_key(data.get("video_title") or "").split()
            for n, keys in by_len.items():
                for i in range(len(toks) - n + 1):
                    k = " ".join(toks[i:i + n])
                    if k in keys:
                        note(team, k, date)
            for q in data.get("quotes") or []:
                k = name_key(q.get("speaker") or "")
                if q.get("speaker_confidence") == "named" and k in full:
                    note(team, k, date)
        self.july, self.moved = {}, {}
        for team, plist in players.by_team.items():
            for p in plist:
                k = name_key(p["full_name"])
                self.july.setdefault(team, {})[k] = p["full_name"]
                other = [d for t, d in first_seen.get(k, {}).items() if t != team]
                if other:
                    self.moved[k] = min(other)

    def members(self, team: str, date: str = "") -> dict:
        m = {k: n for k, n in self.july.get(team, {}).items()
             if k not in self.moved or (date and date < self.moved[k])}
        m.update(self.evidence.get(team, {}))
        return m

    def on(self, team: str, name: str, date: str = "") -> str:
        """The roster spelling when this exact person (suffix ignored) is on the team."""
        return self.members(team, date).get(self.key(name), "")

    def lone(self, token: str, teams: list, date: str = "") -> str:
        """A lone first or last name -> the one roster player of these teams it fits."""
        k = self.key(token)
        if not k or " " in k:
            return ""
        hits = {n for t in teams for kk, n in self.members(t, date).items() if k in kk.split()}
        return hits.pop() if len(hits) == 1 else ""

    def fuzzy(self, name: str, teams: list, date: str = "") -> str:
        """A misspelled full name -> the one roster player of these teams it
        clearly fits: same surname and a close first name, or a close whole name."""
        k = self.key(name)
        toks = _tokens(k)
        if len(toks) < 2:
            return ""
        cands = {}
        for t in teams:
            cands.update(self.members(t, date))
        hits = set()
        for kk, n in cands.items():
            kt = _tokens(kk)
            if len(kt) < 2:
                continue
            same_last = kt[-1] == toks[-1] or (len(toks[-1]) >= 4 and kt[-1].startswith(toks[-1]))
            if same_last and SequenceMatcher(None, kt[0], toks[0]).ratio() >= FIRST_NAME_MIN:
                hits.add(n)
            elif SequenceMatcher(None, kk, k).ratio() >= FULL_NAME_MIN:
                hits.add(n)
        return hits.pop() if len(hits) == 1 else ""


class Resolver:
    def __init__(self, mapping: dict, players, rosters: Rosters, name_key, title_names_full):
        self.coaches = {a: t["head_coach"] for a, t in mapping["teams"].items()}
        self.nicknames = {name_key(k): v for k, v in (mapping.get("nicknames") or {}).items()}
        self.players, self.rosters, self.key = players, rosters, name_key
        self.title_names_full = title_names_full
        self.id = rules_id(mapping)

    def _coach_fits(self, bare: str, team: str) -> bool:
        hc = self.coaches.get(team) or ""
        if not hc:
            return False
        k, hk = self.key(bare), self.key(hc)
        return k == hk or (" " not in k and k == hk.split()[-1])

    def speaker(self, raw: str, role: str, team: str, title: str, date: str = "") -> dict:
        """-> {name, role, team, resolved, how}."""
        raw = " ".join((raw or "").split())
        titled = bool(TITLE_PREFIX.match(raw))
        bare = TITLE_PREFIX.sub("", raw)
        lone = " " not in self.key(bare)
        out = {"name": raw, "role": role, "team": team, "resolved": False, "how": "unresolved"}
        if self._coach_fits(bare, team) and (role == "coach" or titled or self.key(bare) == self.key(self.coaches[team])):
            return dict(out, name=self.coaches[team], role="coach", resolved=True, how="head coach")
        if titled or (role == "coach" and lone):
            return out                                           # "Coach X" / a lone coach name that isn't the head coach
        if role in ("coach", "exec"):
            if not lone and self.title_names_full(title, bare):
                return dict(out, name=bare, resolved=True, how="title")
            if role == "exec" and not lone:
                return dict(out, name=bare, resolved=True, how="exec")
            return out
        # Players: the channel team's roster.
        if lone:
            n = self.rosters.lone(bare, [team], date) or self._nickname(bare, [team], date)
            return dict(out, name=n, role="player", resolved=True, how="roster: lone name") if n else out
        n = self.rosters.on(team, bare, date)
        if not n:
            p = self.players.get(bare)                           # short first name / suffix ("Cam" Johnson)
            n = self.rosters.on(team, p["full_name"], date) if p else ""
        if n:
            return dict(out, name=n, role="player", resolved=True, how="roster")
        n = self.rosters.fuzzy(bare, [team], date)
        if n:
            return dict(out, name=n, role="player", resolved=True, how="roster: spelling")
        if self.title_names_full(title, bare):
            p = self.players.get(bare)
            return dict(out, name=(p or {}).get("full_name") or bare, role="player", resolved=True, how="title")
        return out

    def _nickname(self, token: str, teams: list, date: str = "") -> str:
        n = self.nicknames.get(self.key(token))
        return n if n and any(self.rosters.on(t, n, date) for t in teams) else ""

    def subject(self, name: str, teams: list, date: str = "") -> str:
        """Best full name for a subject, or the name as given."""
        name = " ".join((name or "").split())
        bare = TITLE_PREFIX.sub("", name)
        if " " not in self.key(bare):
            for t in teams:                                       # head coach by surname ("Kerr")
                if self._coach_fits(bare, t):
                    return self.coaches[t]
            return self.rosters.lone(bare, teams[:1], date) or self.rosters.lone(bare, teams, date) \
                or self._nickname(bare, teams, date) or name
        for t in teams:
            n = self.rosters.on(t, bare, date)
            if n:
                return n
            if self.key(self.coaches.get(t, "")) == self.key(bare):
                return self.coaches[t]
        p = self.players.get(bare)
        if p:
            return p["full_name"]
        return self.rosters.fuzzy(bare, teams, date) or name
