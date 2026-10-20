"""
The Pressers section (preview): static pages built from the structured
quotes history (structured_quotes.py), published by GitHub Pages from
docs/pressers/ and meant to be served at https://hoopsmatic.com/pressers/.

  /pressers/                     today's pressers and the week's best quotes
  /pressers/players/<slug>/      what a player said, and what was said about him
  /pressers/teams/<abbr>/        a team's pressers

Every quote shows the speaker, who it's about, topic tags, the date and a
"Watch" link: a click loads the youtube-nocookie player at the quote's
second; nothing loads from YouTube before that. Headshots are the
nba-headshots head-uniform crop, linked from the nba-headshots GitHub
Pages site (not copied into this repo).

SEO: canonical, og:url and the sitemap use https://hoopsmatic.com/pressers/...
Each page has its own title, description and share image (og/<key>.png,
re-rendered only when that page's content changes). PREVIEW (default on)
puts noindex on every page; otherwise a player or team page is indexable
from INDEX_MIN_QUOTES quotes. Each URL's content hash and lastmod are kept
in data/pressers-site-state.json: lastmod moves only when the page's
content (its quotes) changes.

Usage (from repo root):
    python pressers_v2/pressers_site.py --headshots <nba-headshots checkout>
"""

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import structured_quotes as sq  # noqa: E402

REPO = ROOT.parent
SITE = "https://hoopsmatic.com"
OUT_DIR = REPO / "docs" / "pressers"
STATE_PATH = REPO / "data" / "pressers-site-state.json"
ASSETS = ROOT / "section_assets"
INDEX_MIN_QUOTES = 5
WEEK_DAYS = 7
BEST_SHOWN = 24
LIST_ROWS = 30
VISIBLE_PER_PRESSER = 3          # quotes shown per presser; the rest sit under "More"
HEAD_DIR = ("players", "headshots", "head-uniform")
# Faces and logos are not copied into this repo: pages link to the nba-headshots GitHub Pages
# files (the checkout is only read to know which files exist, and for the share images).
HEADSHOTS_PAGES = "https://jsierrahoopshype.github.io/nba-headshots/"
HEAD480_DIR = ("players", "headshots", "head-uniform-480")
ROLE_WORD = {"player": "player", "coach": "coach", "exec": "executive"}
TOPIC_LABEL = {t: t[0].upper() + t[1:] for t in sq.TOPICS}
FOOTER = ('<footer data-hm-footer class="foot"><p>Anything wrong? Email us at '
          '<a href="mailto:hoopshype@hoopshype.com">hoopshype@hoopshype.com</a></p>'
          '<p>For business inquiries, contact us at <a href="mailto:ads@hoopshype.com">ads@hoopshype.com</a></p></footer>')
TEAM_COLORS = {
    "ATL": "#E03A3E", "BOS": "#007A33", "BKN": "#000000", "CHA": "#1D1160", "CHI": "#CE1141", "CLE": "#860038",
    "DAL": "#00538C", "DEN": "#0E2240", "DET": "#C8102E", "GSW": "#1D428A", "HOU": "#CE1141", "IND": "#002D62",
    "LAC": "#C8102E", "LAL": "#552583", "MEM": "#5D76A9", "MIA": "#98002E", "MIL": "#00471B", "MIN": "#0C2340",
    "NOP": "#0C2340", "NYK": "#006BB6", "OKC": "#007AC1", "ORL": "#0077C0", "PHI": "#006BB6", "PHX": "#1D1160",
    "POR": "#E03A3E", "SAC": "#5A2D81", "SAS": "#5F6A72", "TOR": "#CE1141", "UTA": "#4E008E", "WAS": "#002B5C",
}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

esc = lambda s: html.escape(str(s or ""), quote=True)  # noqa: E731


def fmt_date(iso: str) -> str:
    return f"{MONTHS[int(iso[5:7]) - 1]} {int(iso[8:10])}, {iso[:4]}"


def fmt_secs(s: int) -> str:
    s = int(s or 0)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def add_days(iso: str, n: int) -> str:
    return (datetime.strptime(iso, "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d")


def rel_link(frm: str, to: str) -> str:
    """Relative link between two site-root folders ("pressers/players/x/"),
    so the files work on github.io/nba-pressers-digest/ and on hoopsmatic.com/."""
    f, t = [x for x in frm.split("/") if x], [x for x in to.split("/") if x]
    i = 0
    while i < len(f) and i < len(t) and f[i] == t[i]:
        i += 1
    down = "/".join(t[i:])
    return ("../" * (len(f) - i) + down + ("/" if down and to.endswith("/") else "")) or "./"


def initials(name: str) -> str:
    toks = [t for t in re.split(r"[\s\-]+", sq.TITLE_WORDS.sub("", name or "")) if t[:1].isalpha()]
    return ((toks[0][0] + (toks[-1][0] if len(toks) > 1 else "")) if toks else "?").upper()


def ascii_fold(text: str) -> str:
    """Share-image text: the bundled latin font has no glyph past U+00FF."""
    return "".join(c if ord(c) < 256 else unicodedata.normalize("NFKD", c).encode("ascii", "ignore").decode()
                   for c in text or "")


# --------------------------------------------------------------------------- #
# Headshots and logos (nba-headshots checkout)
# --------------------------------------------------------------------------- #

class Media:
    def __init__(self, headshots_dir=None):
        self.dir = Path(headshots_dir) if headshots_dir else None
        self.players = sq.Players(headshots_dir)
        self.logo_ids = {}
        tj = self.dir / "teams" / "metadata" / "teams.json" if self.dir else None
        if tj and tj.is_file():
            for t in json.loads(tj.read_text(encoding="utf-8")):
                self.logo_ids[t.get("abbrev")] = t.get("team_id")

    def _stem(self, name: str) -> str:
        p = self.players.get(name)
        return re.sub(r"\.[a-z0-9]+$", "", ((p or {}).get("headshot") or {}).get("filename") or "", flags=re.I)

    def face(self, name: str, big: bool = False) -> Path | None:
        stem = self._stem(name)
        if not stem or not self.dir:
            return None
        f = self.dir.joinpath(*(HEAD480_DIR if big else HEAD_DIR), stem + ".webp")
        return f if f.is_file() else None

    def url(self, f: Path) -> str:
        return HEADSHOTS_PAGES + f.relative_to(self.dir).as_posix()

    def logo(self, abbr: str) -> Path | None:
        tid = self.logo_ids.get(abbr)
        f = self.dir / "teams" / "logos" / "current" / "svg" / f"{tid}.svg" if self.dir and tid else None
        return f if f and f.is_file() else None


# --------------------------------------------------------------------------- #
# Model: who and what gets a page
# --------------------------------------------------------------------------- #

class Model:
    def __init__(self, view: list, media: Media, season: str):
        self.view, self.media, self.season = view, media, season
        spell = {}
        for r in view:
            for n in [r["speaker"]["name"]] + r["subjects"]:
                spell.setdefault(sq.name_key(n), Counter())[n] += 1
        # One spelling per person: the most used, accents first on a tie ("Rajaković").
        self.display = {k: sorted(c.items(), key=lambda x: (-x[1], -sum(ord(ch) > 127 for ch in x[0]), x[0]))[0][0]
                        for k, c in spell.items()}
        self.by_speaker, self.by_subject, self.by_team = {}, {}, {}
        player_keys = set()
        for r in view:
            k = sq.name_key(r["speaker"]["name"])
            self.by_speaker.setdefault(k, []).append(r)
            if (r["speaker"]["role"] == "player" and " " in k) or media.players.get(r["speaker"]["name"]):
                player_keys.add(k)
            for s in r["subjects"]:
                sk = sq.name_key(s)
                self.by_subject.setdefault(sk, []).append(r)
                if media.players.get(s):
                    player_keys.add(sk)
            for t in r["teams"]:
                self.by_team.setdefault(t, []).append(r)
        self.slug = {}
        used = set()
        for k in sorted(player_keys):
            p = media.players.get(self.display[k])
            base = (p or {}).get("slug") or sq.slugify(self.display[k])
            s, n = base, 2
            while s in used:
                s, n = f"{base}-{n}", n + 1
            used.add(s)
            self.slug[k] = s

    def name(self, n: str) -> str:
        return self.display.get(sq.name_key(n), n)

    def player_quotes(self, k: str) -> list:
        seen, out = set(), []
        for r in self.by_speaker.get(k, []) + self.by_subject.get(k, []):
            if r["id"] not in seen:
                seen.add(r["id"])
                out.append(r)
        return sorted(out, key=lambda r: (r["published_at"], -r["start_seconds"]), reverse=True)

    def player_path(self, n: str) -> str | None:
        s = self.slug.get(sq.name_key(n))
        return f"pressers/players/{s}/" if s else None

    def team_path(self, abbr: str) -> str | None:
        return f"pressers/teams/{abbr.lower()}/" if abbr in self.by_team else None


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

class Page:
    def __init__(self, path, title, description, body, core, eligible, share, key):
        self.path, self.title, self.description, self.body = path, title, description, body
        self.core, self.eligible, self.share, self.key = core, eligible, share, key


def layout(p: Page, robots: str, og_image: str) -> str:
    canon = f"{SITE}/{p.path}"
    r = lambda to: rel_link(p.path, to)  # noqa: E731
    tabs = [("pressers/", "Today"), ("pressers/teams/", "Teams")]
    active = "pressers/teams/" if p.path.startswith("pressers/teams/") else "pressers/" if p.path == "pressers/" else ""
    on = ' class="on"'
    nav = "".join(f'<a href="{r(to)}"{on if to == active else ""}>{esc(label)}</a>' for to, label in tabs)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(p.title)}</title>
<meta name="description" content="{esc(p.description)}">
<meta name="robots" content="{robots}">
<link rel="canonical" href="{canon}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="HoopsMatic">
<meta property="og:title" content="{esc(p.title)}">
<meta property="og:description" content="{esc(p.description)}">
<meta property="og:url" content="{canon}">
<meta property="og:image" content="{og_image}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{esc(p.title)}">
<meta name="twitter:description" content="{esc(p.description)}">
<meta name="twitter:image" content="{og_image}">
<link rel="stylesheet" href="{r('pressers/assets/pressers.css')}">
<script src="{r('pressers/assets/watch.js')}" defer></script>
</head>
<body>
<div class="wrap">
<nav class="tm-tabs">{nav}</nav>
{p.body}
{FOOTER}
</div>
</body>
</html>
"""


class Renderer:
    def __init__(self, M: Model):
        self.M = M
        self.used_faces, self.used_logos = {}, {}

    def face(self, path: str, name: str, size: str = "md") -> str:
        f = self.M.media.face(name)
        if f:
            self.used_faces[f.stem] = f
            px = {"md": 44, "xl": 112}.get(size, 28)
            return (f'<span class="pic face {size}"><img src="{self.M.media.url(f)}" '
                    f'alt="" width="{px}" height="{px}" loading="lazy"></span>')
        return f'<span class="pic face {size}" aria-hidden="true">{esc(initials(name))}</span>'

    def logo(self, path: str, abbr: str, size: str = "") -> str:
        f = self.M.media.logo(abbr)
        cls = f"pic logo {size}".strip()
        if f:
            self.used_logos[abbr] = f
            return f'<span class="{cls}"><img src="{self.M.media.url(f)}" alt="" loading="lazy"></span>'
        return f'<span class="{cls} ph" aria-hidden="true">{esc(abbr)}</span>'

    def person(self, path: str, name: str, cls: str = "") -> str:
        n = self.M.name(name)
        to = self.M.player_path(n)
        c = f' class="{cls}"' if cls else ""
        return f'<a{c} href="{rel_link(path, to)}">{esc(n)}</a>' if to and to != path else f'<span{c}>{esc(n)}</span>'

    def role_line(self, r: dict) -> str:
        team = sq.TEAM_BY_ABBR.get((r["teams"] or [""])[0])
        word = ROLE_WORD.get(r["speaker"]["role"], "")
        return f"{team['nickname']} {word}" if team else word.capitalize()

    def card(self, path: str, r: dict, show_video: bool = True, anchor: bool = False) -> str:
        sp = r["speaker"]["name"]
        about = ", ".join(self.person(path, s) for s in r["subjects"])
        tags = "".join(f'<span class="tag">{esc(TOPIC_LABEL[t])}</span>' for t in r["topics"])
        meta = f'<time datetime="{r["date"]}">{fmt_date(r["date"])}</time>'
        if show_video:
            meta += f' · {esc(r["video_title"])}'
        watch = (f'<a class="watch" href="{esc(sq.watch_url(r["video_id"], r["start_seconds"]))}" rel="nofollow noopener" '
                 f'target="_blank" data-v="{esc(r["video_id"])}" data-t="{int(r["start_seconds"])}" '
                 f'data-title="{esc(r["video_title"])}">&#9654; Watch at {fmt_secs(r["start_seconds"])}</a>')
        aid = f' id="q-{esc(r["id"])}"' if anchor else ""
        return (f'<article class="qc{" best" if r.get("best") else ""}"{aid}>'
                f'<div class="top">{self.face(path, self.M.name(sp))}<div class="who">{self.person(path, sp, "nm")}'
                f'<span class="role">{esc(self.role_line(r))}</span></div></div>'
                + (f'<p class="ctx">{esc(r["context"])}</p>' if r.get("context") else "")
                + f'<blockquote>“{esc(r["quote"])}”</blockquote>'
                + (f'<p class="about">On {about}</p>' if about else "")
                + (f'<div class="tags">{tags}</div>' if tags else "")
                + f'<div class="foot-row"><span class="qm">{meta}</span>{watch}</div></article>')

    def presser_block(self, path: str, recs: list) -> str:
        """One video: its header, then its quotes, best first."""
        r0 = recs[0]
        team = (r0["teams"] or [""])[0]
        ordered = sorted(recs, key=lambda r: (not r.get("best"), -(r.get("news_score") or 0), r["start_seconds"]))
        shown, rest = ordered[:VISIBLE_PER_PRESSER], ordered[VISIBLE_PER_PRESSER:]
        team_html = self.logo(path, team) if team else ""
        tlink = self.M.team_path(team)
        tname = sq.TEAM_BY_ABBR.get(team, {}).get("full", "")
        tmeta = f'<a href="{rel_link(path, tlink)}">{esc(tname)}</a>' if tlink and tlink != path else esc(tname)
        more = (f'<details><summary>{len(rest)} more from this presser</summary><div class="cards">'
                + "".join(self.card(path, r, show_video=False) for r in rest) + "</div></details>") if rest else ""
        return (f'<section class="presser"><div class="presser-head">{team_html}<div><h3>{esc(r0["video_title"])}</h3>'
                f'<span class="meta">{tmeta} · {fmt_date(r0["date"])}</span></div></div>'
                f'<div class="cards">{"".join(self.card(path, r, show_video=False) for r in shown)}</div>{more}</section>')


def by_video(recs: list) -> list:
    groups = {}
    for r in recs:
        groups.setdefault(r["video_id"], []).append(r)
    return sorted(groups.values(), key=lambda g: g[0]["published_at"], reverse=True)


def core_of(recs: list, extra=()) -> str:
    """What a page's lastmod follows: the quotes it shows (not layout, faces or share images)."""
    return json.dumps([list(extra)] + [[r["id"], r["quote"], r["context"], r["subjects"], r["topics"], r["best"],
                                        r["speaker"]["name"], r["date"]] for r in recs], ensure_ascii=False)


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def index_page(R: Renderer) -> Page:
    M, path = R.M, "pressers/"
    view = M.view
    latest = max((r["date"] for r in view), default="")
    today = [r for r in view if r["date"] == latest]
    week = [r for r in view if r.get("best") and r["date"] >= add_days(latest, -(WEEK_DAYS - 1))][:BEST_SHOWN] if latest else []
    pressers = by_video(today)
    body = [f'<header class="hdr"><h1>NBA press conference quotes</h1><p class="subtitle">What players, coaches and '
            f'executives said at team press conferences and media availabilities, with the video at the moment they '
            f'said it. {M.season} season.</p></header>']
    if not view:
        body.append('<p class="msg">No press conference quotes yet.</p>')
    else:
        body.append(f'<section class="block"><div class="block-head"><h2>Pressers on {fmt_date(latest)}</h2>'
                    f'<span class="ct">{plural(len(pressers), "presser")}, {plural(len(today), "quote")}</span></div>'
                    + "".join(R.presser_block(path, g) for g in pressers) + "</section>")
        if week:
            body.append(f'<section class="block"><div class="block-head"><h2>Best quotes of the last {WEEK_DAYS} days</h2>'
                        f'<span class="ct">{plural(len(week), "quote")}</span></div><div class="cards">'
                        + "".join(R.card(path, r) for r in week) + "</div></section>")
        players = sorted(((k, len(M.player_quotes(k))) for k in M.slug), key=lambda x: (-x[1], M.display[x[0]]))[:LIST_ROWS]
        rows = "".join(f'<div class="row">{R.face(path, M.display[k])}<span class="nm">{R.person(path, M.display[k])}</span>'
                       f'<span class="v">{n}</span></div>' for k, n in players)
        teams = sorted(M.by_team.items(), key=lambda x: (-len(x[1]), x[0]))
        trows = "".join(f'<div class="row">{R.logo(path, a)}<span class="nm"><a href="{rel_link(path, M.team_path(a))}">'
                        f'{esc(sq.TEAM_BY_ABBR[a]["full"])}</a></span><span class="v">{len(l)}</span></div>'
                        for a, l in teams if a in sq.TEAM_BY_ABBR)
        body.append(f'<div class="grid2"><section class="section" id="players"><h2>Most quoted players</h2><div class="rows">{rows}</div></section>'
                    f'<section class="section" id="teams"><h2>Teams</h2><div class="rows">{trows}</div></section></div>')
    faces = []
    for r in sorted(today, key=lambda r: (not r.get("best"), -(r.get("news_score") or 0))):
        n = M.name(r["speaker"]["name"])
        if n not in faces and M.media.face(n, big=True):
            faces.append(n)
    desc = (f"What NBA players and coaches said at press conferences on {fmt_date(latest)}: "
            f"{plural(len(today), 'quote')} from {plural(len(pressers), 'presser')}, each with the video at that moment."
            if latest else "What NBA players and coaches said at press conferences, with the video at that moment.")
    return Page(path, "NBA Press Conference Quotes Today | HoopsMatic", desc, "\n".join(body),
                core_of(today + week, ("index", latest)), True,
                {"title": "NBA press conference quotes", "line": f"{fmt_date(latest)} · {plural(len(today), 'quote')}" if latest else M.season,
                 "faces": faces[:3], "team": None}, "index")


def player_page(R: Renderer, k: str) -> Page:
    M = R.M
    name = M.display[k]
    path = M.player_path(name)
    recs = M.player_quotes(k)
    said = [r for r in recs if sq.name_key(r["speaker"]["name"]) == k]
    about = [r for r in recs if r not in said]
    best = [r for r in recs if r.get("best")]
    p = M.media.players.get(name) or {}
    team = sq.TEAM_BY_ABBR.get(p.get("team_abbrev") or "") or sq.TEAM_BY_ABBR.get(
        Counter(r["teams"][0] for r in said if r["teams"]).most_common(1)[0][0] if any(r["teams"] for r in said) else "")
    sub = [f"{team['nickname']} player" if team else "Player", plural(len(recs), "press conference quote"), M.season]
    body = [f'<header class="ehead">{R.face(path, name, "xl")}<div><h1>{esc(name)}</h1>'
            f'<p class="subtitle">{" · ".join(esc(x) for x in sub)}</p></div></header>']
    if best:
        body.append('<section class="block"><div class="block-head"><h2>Best quotes</h2></div><div class="cards">'
                    + "".join(R.card(path, r) for r in best[:6]) + "</div></section>")
    if said:
        body.append(f'<section class="block"><div class="block-head"><h2>What {esc(name)} said</h2>'
                    f'<span class="ct">{plural(len(said), "quote")}</span></div><div class="cards">'
                    + "".join(R.card(path, r, anchor=True) for r in said) + "</div></section>")
    if about:
        body.append(f'<section class="block"><div class="block-head"><h2>What others said about {esc(name)}</h2>'
                    f'<span class="ct">{plural(len(about), "quote")}</span></div><div class="cards">'
                    + "".join(R.card(path, r, anchor=True) for r in about) + "</div></section>")
    latest = recs[0]["date"] if recs else ""
    desc = (f"{name} press conference quotes from the {M.season} NBA season: {plural(len(said), 'quote')} by him and "
            f"{plural(len(about), 'quote')} about him, latest {fmt_date(latest)}, each with the video at that moment.")
    return Page(path, f"{name} Press Conference Quotes {M.season} | HoopsMatic", desc, "\n".join(body),
                core_of(recs, ("player", name)), len(recs) >= INDEX_MIN_QUOTES,
                {"title": f"{name}: press conference quotes", "line": f"{plural(len(recs), 'quote')} · {M.season}",
                 "faces": [name], "team": None}, f"player-{M.slug[k]}")


def team_page(R: Renderer, abbr: str) -> Page:
    M = R.M
    t = sq.TEAM_BY_ABBR[abbr]
    path = M.team_path(abbr)
    recs = sorted(M.by_team[abbr], key=lambda r: (r["published_at"], -r["start_seconds"]), reverse=True)
    best = [r for r in recs if r.get("best")]
    pressers = by_video(recs)
    speakers = Counter(M.name(r["speaker"]["name"]) for r in recs if abbr == (r["teams"] or [""])[0])
    body = [f'<header class="ehead">{R.logo(path, abbr, "xl")}<div><h1>{esc(t["full"])} press conference quotes</h1>'
            f'<p class="subtitle">{plural(len(recs), "quote")} from {plural(len(pressers), "presser")} · {M.season}</p></div></header>']
    if best:
        body.append('<section class="block"><div class="block-head"><h2>Best quotes</h2></div><div class="cards">'
                    + "".join(R.card(path, r) for r in best[:6]) + "</div></section>")
    body.append('<section class="block"><div class="block-head"><h2>Latest pressers</h2></div>'
                + "".join(R.presser_block(path, g) for g in pressers) + "</section>")
    if speakers:
        rows = "".join(f'<div class="row">{R.face(path, n)}<span class="nm">{R.person(path, n)}</span><span class="v">{c}</span></div>'
                       for n, c in sorted(speakers.items(), key=lambda x: (-x[1], x[0]))[:LIST_ROWS])
        body.append(f'<section class="section"><h2>Who spoke</h2><div class="rows">{rows}</div></section>')
    latest = recs[0]["date"] if recs else ""
    desc = (f"What the {t['full']} said at press conferences in the {M.season} NBA season: {plural(len(recs), 'quote')} "
            f"from {plural(len(pressers), 'presser')}, latest {fmt_date(latest)}, each with the video at that moment.")
    return Page(path, f"{t['full']} Press Conference Quotes {M.season} | HoopsMatic", desc, "\n".join(body),
                core_of(recs, ("team", abbr)), len(recs) >= INDEX_MIN_QUOTES,
                {"title": f"{t['full']}: press conference quotes", "line": f"{plural(len(recs), 'quote')} · {M.season}",
                 "faces": [], "team": abbr}, f"team-{abbr.lower()}")


def teams_index_page(R: Renderer) -> Page:
    M, path = R.M, "pressers/teams/"
    teams = sorted((a for a in M.by_team if a in sq.TEAM_BY_ABBR), key=lambda a: sq.TEAM_BY_ABBR[a]["full"])
    rows = "".join(f'<div class="row">{R.logo(path, a)}<span class="nm"><a href="{rel_link(path, M.team_path(a))}">'
                   f'{esc(sq.TEAM_BY_ABBR[a]["full"])}</a></span><span class="v">{len(M.by_team[a])}</span></div>' for a in teams)
    body = (f'<header class="hdr"><h1>NBA press conference quotes by team</h1><p class="subtitle">{M.season} season, '
            f'quotes from each team\'s press conferences and media availabilities.</p></header>'
            f'<section class="section"><div class="rows">{rows}</div></section>')
    return Page(path, f"NBA Press Conference Quotes by Team {M.season} | HoopsMatic",
                f"Press conference quotes from all {len(teams)} NBA teams in the {M.season} season, each with the video at that moment.",
                body, json.dumps(["teams"] + [[a, len(M.by_team[a])] for a in teams]), True,
                {"title": "NBA press conference quotes by team", "line": M.season, "faces": [], "team": None}, "teams")


# --------------------------------------------------------------------------- #
# Share images (Pillow; skipped when it isn't installed)
# --------------------------------------------------------------------------- #

def render_share(spec: dict, media: Media, dest: Path) -> bool:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False
    fonts = ASSETS / "fonts"
    bold = ImageFont.truetype(str(fonts / "dm-sans-latin-700-normal.woff"), 60)
    reg = ImageFont.truetype(str(fonts / "dm-sans-latin-400-normal.woff"), 34)
    brand = ImageFont.truetype(str(fonts / "dm-sans-latin-700-normal.woff"), 30)
    img = Image.new("RGB", (1200, 630), "#f5f5f7")
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((40, 40, 1160, 590), radius=28, fill="#ffffff", outline="#d1d1d6", width=2)
    x = 90
    faces = [f for f in (media.face(n, big=True) or media.face(n) for n in spec.get("faces") or []) if f]
    if faces:
        size = 260 if len(faces) == 1 else 200
        fx = x
        for f in faces[:3]:
            try:
                im = Image.open(f).convert("RGBA").resize((size, size))
            except OSError:
                continue
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
            bg = Image.new("RGBA", (size, size), "#f0f0f2")
            bg.alpha_composite(im)
            img.paste(bg.convert("RGB"), (fx, 315 - size // 2), mask)
            fx += size - (40 if len(faces) > 1 else 0)
        x = fx + 50
    elif spec.get("team"):
        d.rounded_rectangle((x, 185, x + 260, 445), radius=40, fill=TEAM_COLORS.get(spec["team"], "#3b82f6"))
        w = d.textlength(spec["team"], font=bold)
        d.text((x + 130 - w / 2, 280), spec["team"], font=bold, fill="#ffffff")
        x += 310
    width = 1110 - x
    words, lines, line = ascii_fold(spec["title"]).split(), [], ""
    for w in words:
        trial = (line + " " + w).strip()
        if d.textlength(trial, font=bold) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = w
    lines.append(line)
    lines = lines[:3]
    y = 315 - (len(lines) * 72 + 56) // 2
    for ln in lines:
        d.text((x, y), ln, font=bold, fill="#1d1d1f")
        y += 72
    d.text((x, y + 8), ascii_fold(spec.get("line") or ""), font=reg, fill="#6e6e73")
    d.text((90, 520), "HoopsMatic", font=brand, fill="#3b82f6")
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 256-colour palette: a third of the size, no visible loss on this flat design.
    img.quantize(colors=256, method=Image.Quantize.FASTOCTREE).save(dest, "PNG", optimize=True)
    return True


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #

def page_hash(p: Page) -> str:
    return hashlib.sha1((p.title + "\n" + p.core).encode("utf-8")).hexdigest()


def build_site(view: list, season: str, media: Media, out_dir: Path | None = None, state: dict | None = None,
               preview: bool = True, today: str | None = None, share_images: bool = True) -> tuple[dict, dict]:
    """Writes the section into out_dir. Returns (new state, summary)."""
    out = Path(out_dir or OUT_DIR)
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = state or {}
    M = Model(view, media, season)
    R = Renderer(M)
    pages = [index_page(R), teams_index_page(R)]
    pages += [player_page(R, k) for k in sorted(M.slug, key=lambda k: M.slug[k])]
    pages += [team_page(R, a) for a in sorted(M.by_team) if a in sq.TEAM_BY_ABBR]

    # Rebuild the page folders from scratch (api/ and og/ are kept: og images are reused;
    # img/ is only removed, from builds that still copied faces).
    for sub in ("players", "teams", "img", "assets"):
        shutil.rmtree(out / sub, ignore_errors=True)
    (out / "assets" / "fonts").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ASSETS / "pressers.css", out / "assets" / "pressers.css")
    shutil.copyfile(ASSETS / "watch.js", out / "assets" / "watch.js")
    for f in sorted((ASSETS / "fonts").glob("*.woff2")):
        shutil.copyfile(f, out / "assets" / "fonts" / f.name)

    old_pages, old_og = state.get("pages", {}), state.get("og", {})
    new_pages, new_og = {}, {}
    summary = {"pages": len(pages), "eligible": 0, "changed": 0, "share_rendered": 0, "share_kept": 0}
    for p in pages:
        h = page_hash(p)
        old = old_pages.get(p.path)
        if old and old.get("hash") == h:
            lastmod = old["lastmod"]
        else:
            # Changed (or new): the newest quote's date, unless the page already had that
            # date or a later one (a quote removed or edited today): then today.
            newest = max(re.findall(r'"(\d{4}-\d{2}-\d{2})"\]', p.core) or [today])
            lastmod = newest if not old or newest > old.get("lastmod", "") else today
            summary["changed"] += 1
        new_pages[p.path] = {"hash": h, "lastmod": lastmod, "eligible": p.eligible}
        summary["eligible"] += p.eligible
        og_file = out / "og" / f"{p.key}.png"
        if share_images and (old_og.get(p.key) != h or not og_file.is_file()):
            if render_share(p.share, media, og_file):
                summary["share_rendered"] += 1
        elif og_file.is_file():
            summary["share_kept"] += 1
        new_og[p.key] = h if og_file.is_file() else old_og.get(p.key, "")
        og_url = f"{SITE}/pressers/og/{p.key}.png"
        robots = "noindex, follow" if preview or not p.eligible else "index, follow"
        dest = out / p.path[len("pressers/"):] / "index.html"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(layout(p, robots, og_url), encoding="utf-8", newline="\n")
    keys = {p.key for p in pages}
    for f in (out / "og").glob("*.png") if (out / "og").is_dir() else []:
        if f.stem not in keys:
            f.unlink()

    summary.update(faces=len(R.used_faces), logos=len(R.used_logos),
                   players=len(M.slug), teams=len([a for a in M.by_team if a in sq.TEAM_BY_ABBR]))

    # Sitemap: the pages eligible for indexing, with their own lastmod.
    urls = "".join(f"<url><loc>{SITE}/{path}</loc><lastmod>{v['lastmod']}</lastmod></url>\n"
                   for path, v in sorted(new_pages.items()) if v["eligible"])
    (out / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                                     '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                                     + urls + "</urlset>\n", encoding="utf-8", newline="\n")
    return {"pages": dict(sorted(new_pages.items())), "og": dict(sorted(new_og.items()))}, summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--headshots", help="nba-headshots checkout (metadata, head-uniform images, team logos)")
    args = ap.parse_args(argv)
    preview = os.getenv("PRESSERS_PREVIEW", "true").strip().lower() not in ("0", "false", "no")
    season = sq.current_season()
    view = sq.current_view(sq.read_history(season))
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    media = Media(args.headshots)
    new_state, s = build_site(view, season, media, state=state, preview=preview)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(new_state, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(f"[pressers-site] {season}: {s['pages']} pages ({s['players']} players, {s['teams']} teams), "
          f"{s['eligible']} eligible for indexing, preview={'on' if preview else 'off'}; content changed on {s['changed']}; "
          f"share images rendered {s['share_rendered']}, kept {s['share_kept']}; faces {s['faces']}, logos {s['logos']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
