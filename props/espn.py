"""Player game logs from ESPN's public site API (also a Kalshi settlement source).

These endpoints are unofficial and undocumented; everything here goes through
`net.get_json` with caching so we stay light on them.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from . import net
from .stats import LEAGUES, Stat

SITE = os.environ.get("ESPN_SITE_BASE", "https://site.api.espn.com/apis/site/v2/sports/")
WEB = os.environ.get("ESPN_WEB_BASE", "https://site.web.api.espn.com/apis/common/v3/sports/")
CURRENT_TTL = float(os.environ.get("PROPS_GAMELOG_TTL", 1800))
PAST_TTL = 24 * 3600
ROSTER_TTL = 6 * 3600
ET = dt.timezone(dt.timedelta(hours=-5))  # ESPN scoreboard dates are US dates

# Exchange/app abbreviations that differ from ESPN's and can't be found by prefix.
TEAM_ALIASES = {
    "nba": {"WAS": "WSH", "PHO": "PHX", "BRK": "BKN"},
    "wnba": {"WAS": "WSH", "LVA": "LV", "LAS": "LA", "GSV": "GS", "PHO": "PHX", "NYL": "NY", "CON": "CONN"},
    "nfl": {"WAS": "WSH", "JAC": "JAX", "LA": "LAR", "OAK": "LV"},
    "mlb": {"CWS": "CHW", "WAS": "WSH", "OAK": "ATH", "AZ": "ARI", "KCR": "KC", "SDP": "SD", "SFG": "SF", "TBR": "TB"},
    "nhl": {"WAS": "WSH", "VEG": "VGK", "UTAH": "UTA", "ARI": "UTA"},
}


@dataclass
class Game:
    event_id: str
    date: str          # ISO timestamp
    season: int
    postseason: bool
    team_id: str
    opponent_id: str
    opponent: str      # ESPN abbreviation
    home: bool
    row: dict = field(repr=False)


def normalize_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z\s]", "", s.replace("-", " "))
    parts = [p for p in s.split() if p not in ("jr", "sr", "ii", "iii", "iv", "v")]
    return " ".join(parts)


def teams(league: str) -> dict[str, dict]:
    """ESPN abbreviation -> {"id", "abbr", "name"}."""
    data = net.get_json(f"{SITE}{LEAGUES[league]}/teams", {"limit": 1000}, ttl=ROSTER_TTL)  # cfb: 700+ teams
    out = {}
    for t in data["sports"][0]["leagues"][0]["teams"]:
        t = t["team"]
        out[t["abbreviation"].upper()] = {"id": str(t["id"]), "abbr": t["abbreviation"].upper(),
                                          "name": t.get("displayName", "")}
    return out


def match_team(league: str, abbr: str, table: Optional[dict] = None) -> Optional[dict]:
    """Resolve an exchange/app team code (NYK, SAS, GSW, NJD...) to an ESPN team."""
    table = table if table is not None else teams(league)
    a = abbr.upper().strip()
    if a in table:
        return table[a]
    alias = TEAM_ALIASES.get(league, {}).get(a)
    if alias and alias in table:
        return table[alias]
    cands = [t for k, t in table.items() if len(k) >= 2 and (a.startswith(k) or k.startswith(a))]
    return cands[0] if len(cands) == 1 else None


def current_season(league: str) -> int:
    data = net.get_json(f"{SITE}{LEAGUES[league]}/scoreboard", ttl=ROSTER_TTL)
    return int(data["leagues"][0]["season"]["year"])


def roster(league: str, team_id: str) -> list[dict]:
    data = net.get_json(f"{SITE}{LEAGUES[league]}/teams/{team_id}/roster", ttl=ROSTER_TTL)
    out = []
    for a in data.get("athletes", []):
        for p in a.get("items", [a]):
            if "id" in p:
                out.append({"id": str(p["id"]), "name": p.get("fullName") or p.get("displayName", "")})
    return out


def pick_player(name: str, candidates: list[dict]) -> Optional[dict]:
    target = normalize_name(name)
    exact = [c for c in candidates if normalize_name(c["name"]) == target]
    if len(exact) == 1:
        return exact[0]
    tp = target.split()
    if len(tp) >= 2:
        loose = [c for c in candidates
                 if (cp := normalize_name(c["name"]).split()) and cp[-1] == tp[-1] and cp[0][:1] == tp[0][:1]]
        if len(loose) == 1:
            return loose[0]
    return None


def find_player(league: str, name: str, team_ids: list[str]) -> Optional[dict]:
    for tid in team_ids:
        p = pick_player(name, roster(league, tid))
        if p:
            return {**p, "team_id": tid}
    return None


def gamelog(league: str, athlete_id: str, season: int, category: Optional[str], is_current: bool) -> list[Game]:
    url = f"{WEB}{LEAGUES[league]}/athletes/{athlete_id}/gamelog"

    def fetch() -> list[Game]:
        try:
            data = net.get_json(url, {"season": season, "category": category})
        except Exception as e:  # 404 for seasons a player didn't play
            if getattr(e, "code", None) in (400, 404):
                return []
            raise
        return parse_gamelog(data, season)
    # Cache parsed games, not ESPN's much larger raw JSON.
    return net.memo(f"gamelog:{url}:{season}:{category}", CURRENT_TTL if is_current else PAST_TTL, fetch)


def parse_gamelog(data: dict, season: int) -> list[Game]:
    names = data.get("names") or []
    events = data.get("events") or {}
    games: dict[str, Game] = {}
    for st in data.get("seasonTypes") or []:
        label = (st.get("displayName") or "").lower()
        if "preseason" in label or "all-star" in label:
            continue
        post = "postseason" in label or "playoff" in label
        for cat in st.get("categories") or []:
            for e in cat.get("events") or []:
                ev = events.get(e.get("eventId"))
                if not ev or e["eventId"] in games:
                    continue
                row = dict(zip(names, e.get("stats") or []))
                team_id = str((ev.get("team") or {}).get("id", ""))
                opp = ev.get("opponent") or {}
                games[e["eventId"]] = Game(
                    event_id=e["eventId"], date=ev.get("gameDate", ""), season=season, postseason=post,
                    team_id=team_id, opponent_id=str(opp.get("id", "")),
                    opponent=(opp.get("abbreviation") or "").upper(),
                    home=str(ev.get("homeTeamId", "")) == team_id, row=row)
    return list(games.values())


def played(league: str, g: Game) -> bool:
    """Drop DNP rows so they don't count as misses."""
    if league in ("nba", "wnba"):
        try:
            return float(g.row.get("minutes") or 0) > 0
        except ValueError:
            return False
    return bool(g.row)


def player_games(league: str, athlete_id: str, stat: Stat, seasons_back: int = 2) -> tuple[int, list[Game]]:
    """Games (most recent first) across the current and previous seasons."""
    cur = current_season(league)
    games: list[Game] = []
    for s in range(cur, cur - seasons_back - 1, -1):
        games += gamelog(league, athlete_id, s, stat.category, s == cur)
    seen, out = set(), []
    for g in sorted(games, key=lambda g: g.date, reverse=True):
        if g.event_id not in seen and played(league, g):
            seen.add(g.event_id)
            out.append(g)
    return cur, out


def parse_time(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def scoreboard(league: str, day: dt.date) -> list[dict]:
    """ESPN events for one US date: id, start, state ("pre"/"in"/"post"), status name, team ids."""
    data = net.get_json(f"{SITE}{LEAGUES[league]}/scoreboard", {"dates": day.strftime("%Y%m%d"), "limit": 300},
                        ttl=60)
    out = []
    for e in data.get("events") or []:
        comp = (e.get("competitions") or [{}])[0]
        st = (e.get("status") or comp.get("status") or {}).get("type") or {}
        out.append({"id": str(e["id"]), "start": parse_time(e.get("date")), "state": st.get("state"),
                    "name": st.get("name") or "", "completed": bool(st.get("completed")),
                    "teams": {str((c.get("team") or {}).get("id")) for c in comp.get("competitors") or []},
                    "abbrs": {str((c.get("team") or {}).get("id")): ((c.get("team") or {}).get("abbreviation") or "").upper()
                              for c in comp.get("competitors") or []}})
    return out


def find_event(league: str, team_id: str, opponent_id: Optional[str], near: dt.datetime) -> Optional[dict]:
    """The team's ESPN game closest to `near` on the surrounding US dates."""
    day = near.astimezone(ET).date()
    best = None
    for d in (day - dt.timedelta(days=1), day, day + dt.timedelta(days=1)):
        for e in scoreboard(league, d):
            if team_id not in e["teams"] or (opponent_id and opponent_id not in e["teams"]) or not e["start"]:
                continue
            if best is None or abs(e["start"] - near) < abs(best["start"] - near):
                best = e
    return best


def match_game(league: str, team: str, opp: str, start: Optional[str], table: dict) -> Optional[tuple[dict, dict]]:
    """(team, opponent) from the ESPN game at `start` in which at least one code matches a side."""
    when = parse_time(start)
    if not when:
        return None
    by_id = {t["id"]: t for t in table.values()}
    want = {code.upper(): code for code in (team, opp)}
    day = when.astimezone(ET).date()
    for e in scoreboard(league, day) + scoreboard(league, day - dt.timedelta(days=1)):
        if not e["start"] or abs(e["start"] - when) > dt.timedelta(hours=1) or len(e["abbrs"]) != 2:
            continue
        hits = {tid: a for tid, a in e["abbrs"].items()
                if a in want or (m := match_team(league, a, table)) and m["abbr"] in want}
        if len(hits) != 1:  # need exactly one anchoring side; two exact hits fall through to match_team
            continue
        known, code = next(iter(hits.items()))
        other = next(tid for tid in e["abbrs"] if tid != known)
        a, b = by_id.get(known), by_id.get(other)
        if not (a and b):
            continue
        return (a, b) if code == team.upper() else (b, a)
    return None


INJURY_TTL = 600


def _designation(status: str, comment: str) -> Optional[str]:
    """ESPN injury status -> Out / Doubtful / Questionable / Probable / Day-To-Day, or None if active."""
    s, c = (status or "").lower(), (comment or "").lower()
    if s in ("active", ""):
        return None
    if s == "out" or "il" in s.split("-") or "reserve" in s or "suspension" in s:
        return "Out"
    if s in ("doubtful", "questionable", "probable"):
        return s.title()
    # NBA lists the official tag only in the comment ("X is questionable for Wednesday's game").
    for tag, words in (("Out", ("ruled out", " is out ", "will not play", "won't play")),
                       ("Doubtful", ("doubtful",)), ("Questionable", ("questionable",)), ("Probable", ("probable",))):
        if any(w in f" {c} " for w in words):
            return tag
    return "Day-To-Day"


def injuries(league: str) -> dict[str, str]:
    """ESPN athlete id -> injury designation for every listed player in the league."""
    data = net.get_json(f"{SITE}{LEAGUES[league]}/injuries", ttl=INJURY_TTL)
    out = {}
    for team in data.get("injuries") or []:
        for i in team.get("injuries") or []:
            links = (i.get("athlete") or {}).get("links") or []
            m = next((re.search(r"/id/(\d+)", l.get("href", "")) for l in links if "/id/" in l.get("href", "")), None)
            tag = _designation(i.get("status"), i.get("shortComment"))
            if m and tag:
                out[m.group(1)] = tag
    return out


CORE = os.environ.get("ESPN_CORE_BASE", "https://sports.core.api.espn.com/v2/sports/")


def starters(league: str, event_id: str, team_id: str) -> Optional[set[str]]:
    """Athlete ids ESPN lists as starting, or None until ESPN posts the lineup (usually near tip-off)."""
    sport, lg = LEAGUES[league].split("/")
    data = net.get_json(f"{CORE}{sport}/leagues/{lg}/events/{event_id}/competitions/{event_id}"
                        f"/competitors/{team_id}/roster", ttl=300)
    ids = {str(e.get("playerId")) for e in data.get("entries") or [] if e.get("starter")}
    return ids or None


def minutes(league: str, g: Game) -> Optional[float]:
    """Minutes played (basketball) or time on ice (hockey) in one game."""
    v = g.row.get("minutes") if league in ("nba", "wnba") else g.row.get("timeOnIcePerGame") if league == "nhl" else None
    if v in (None, "", "-", "--"):
        return None
    try:
        if ":" in str(v):
            mm, ss = str(v).split(":", 1)
            return int(mm) + int(ss) / 60
        return float(v)
    except ValueError:
        return None


def box_played(league: str, event_id: str, athlete_id: str) -> Optional[bool]:
    """Whether the final box score shows the player in the game: True, False (absent or DNP),
    or None when ESPN has no box score with players yet."""
    data = net.get_json(f"{SITE}{LEAGUES[league]}/summary", {"event": event_id})
    teams = (data.get("boxscore") or {}).get("players") or []
    seen = False
    for team in teams:
        for group in team.get("statistics") or []:
            for a in group.get("athletes") or []:
                seen = True
                if str((a.get("athlete") or {}).get("id")) == str(athlete_id):
                    return not a.get("didNotPlay") and bool(a.get("stats"))
    return False if seen else None
