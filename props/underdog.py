"""Underdog Fantasy higher/lower lines from its open v1 lines endpoint.

The endpoint is unofficial and undocumented; it can change or start refusing
requests at any time. `fetch_props` raises on any failure and the caller
skips Underdog for that refresh.

Every line becomes one Prop. Leagues are Underdog's sport ids lowercased
(nba, nfl, cfb, fifa, tennis, ...). Lines whose league and stat ESPN covers
in stats.py get hit rates; the rest keep Underdog's stat key and label with
no hit rates.
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Optional

from . import espn, net
from .props import Prop
from .stats import LEAGUES

URL = os.environ.get("UNDERDOG_LINES_URL", "https://api.underdogfantasy.com/v1/over_under_lines")
SITE = "https://underdogfantasy.com/pick-em/higher-lower/all/"

_BASKETBALL = {
    "points": "points", "rebounds": "rebounds", "assists": "assists", "three_points_made": "threes",
    "steals": "steals", "blocks": "blocks", "blks_stls": "stocks", "pts_rebs_asts": "pra",
    "pts_rebs": "pr", "pts_asts": "pa", "rebs_asts": "ra", "free_throws_made": "ftm",
}
# Full-game stats only: quarter/half, first/last scorer and fantasy lines need data game logs don't have.
_FOOTBALL = {
    "passing_yds": "pass_yds", "passing_tds": "pass_tds", "passing_att": "pass_att",
    "passing_comps": "pass_comp", "passing_ints": "pass_int", "passing_long": "pass_long",
    "rushing_yds": "rush_yds", "rushing_att": "rush_att", "rushing_long": "rush_long",
    "receiving_yds": "rec_yds", "receiving_rec": "receptions", "receiving_tgts": "targets",
    "receiving_long": "rec_long", "rush_rec_yds": "rush_rec_yds", "passing_and_rushing_yds": "pass_rush_yds",
    "rush_rec_tds": "anytime_td", "fumbles_lost": "fumbles_lost", "field_goals_made": "fg_made",
    "extra_points_made": "xp_made", "kicking_points": "kicking_points",
}
# Underdog stat key -> stats.py key, per league ESPN covers.
STAT_MAP = {
    "nba": _BASKETBALL,
    "wnba": _BASKETBALL,
    "nfl": _FOOTBALL,
    "cfb": _FOOTBALL,
    "mlb": {
        "hits": "hits", "home_runs": "home_runs", "rbis": "rbis", "runs": "runs", "total_bases": "total_bases",
        "hits_runs_rbis": "hrr", "stolen_bases": "stolen_bases", "strikeouts": "pitcher_ks",
        "pitch_outs": "pitching_outs", "hits_allowed": "hits_allowed", "runs_allowed": "earned_runs",
    },
    "nhl": {"goals": "goals", "assists": "assists", "points": "points", "shots": "shots", "saves": "saves"},
}


def implied(american) -> Optional[float]:
    """American odds ("+102", "-139") -> implied probability 0..1, or None."""
    try:
        a = float(str(american).strip())
    except (TypeError, ValueError):
        return None
    if a <= -100:
        return round(-a / (-a + 100), 4)
    if a >= 100:
        return round(100 / (a + 100), 4)
    return None


def parse(data: dict, leagues: Optional[Iterable[str]] = None) -> list[Prop]:
    want = {l.lower() for l in leagues} if leagues else None
    apps = {a["id"]: a for a in data.get("appearances") or []}
    players = {p["id"]: p for p in data.get("players") or []}
    games = {g["id"]: g for g in (data.get("games") or []) + (data.get("solo_games") or [])}
    tables: dict[str, dict] = {}
    out = []
    for ln in data.get("over_under_lines") or []:
        if ln.get("status") not in (None, "active"):
            continue
        st = (ln.get("over_under") or {}).get("appearance_stat") or {}
        app = apps.get(st.get("appearance_id")) or {}
        pl = players.get(app.get("player_id")) or {}
        game = games.get(app.get("match_id")) or {}
        sport = (pl.get("sport_id") or game.get("sport_id") or "").lower()
        if not sport or (want and sport not in want):
            continue
        try:
            line = float(ln["stat_value"])
        except (KeyError, TypeError, ValueError):
            continue
        name = " ".join(x for x in (pl.get("first_name"), pl.get("last_name")) if x)
        team, opp = _sides(game, app, pl)
        prices = {o.get("choice"): implied(o.get("american_price")) for o in ln.get("options") or []}
        stat = STAT_MAP.get(sport, {}).get(st.get("stat"))
        p = Prop(source="underdog", league=sport, stat=stat or st.get("stat") or "",
                 stat_label=None if stat else (st.get("display_stat") or st.get("stat") or "").strip(),
                 player=name or (ln.get("options") or [{}])[0].get("selection_header") or "?", line=line,
                 team=team, opponent=opp, start_time=game.get("scheduled_at"), market_id=ln.get("id"),
                 over_price=prices.get("higher"), under_price=prices.get("lower"), url=SITE + sport)
        if sport in LEAGUES:
            table = tables.get(sport) or tables.setdefault(sport, espn.teams(sport))
            t = espn.match_team(sport, team, table) if team else None
            o = espn.match_team(sport, opp, table) if opp else None
            # College codes collide (Underdog's JSU is ESPN's JXST; ESPN's JSU is Jackson State),
            # so pair them with the game on ESPN's scoreboard instead.
            if team and opp and (sport == "cfb" or not (t and o)):
                t, o = espn.match_game(sport, team, opp, game.get("scheduled_at"), table) or (t, o)
            p.team, p.team_id = (t["abbr"], t["id"]) if t else (team, None)
            p.opponent, p.opponent_id = (o["abbr"], o["id"]) if o else (opp, None)
        out.append(p)
    return out


def _sides(game: dict, app: dict, pl: dict) -> tuple[Optional[str], Optional[str]]:
    """(player's team, opponent): abbreviations from "AWAY @ HOME" or "HOME vs AWAY",
    or the other player in a solo game."""
    if "away_player_id" in game:
        me = pl.get("id")
        other = game.get("away_player_name") if game.get("home_player_id") == me else game.get("home_player_name")
        return None, re.sub(r"^\(\d+\)\s*", "", other or "") or None
    title = game.get("abbreviated_title") or ""
    away, sep, home = title.partition(" @ ")
    if not sep:
        home, sep, away = title.partition(" vs ")
    if not sep:
        return None, None
    tid = app.get("team_id") or pl.get("team_id")
    if tid and tid == game.get("away_team_id"):
        return away.strip(), home.strip()
    if tid and tid == game.get("home_team_id"):
        return home.strip(), away.strip()
    return None, None


def fetch_props(leagues: Optional[Iterable[str]] = None) -> list[Prop]:
    return parse(net.get_json(URL, timeout=60), leagues)  # ~20 MB: read once per refresh, not cached
