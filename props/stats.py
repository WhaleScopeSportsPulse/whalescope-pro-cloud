"""Stat definitions: how to read one prop stat out of an ESPN game-log row.

Each row is a dict keyed by ESPN's stat `names` (e.g. "points",
"threePointFieldGoalsMade-threePointFieldGoalsAttempted"). A stat function
returns a number, or None when the row doesn't carry what it needs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

# league key -> ESPN sport/league path
LEAGUES = {
    "nba": "basketball/nba",
    "wnba": "basketball/wnba",
    "nfl": "football/nfl",
    "mlb": "baseball/mlb",
    "nhl": "hockey/nhl",
}


def num(row: dict, name: str) -> Optional[float]:
    v = row.get(name)
    if v is None:
        return None
    v = str(v).strip()
    if v in ("", "-", "--"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def made(row: dict, name: str) -> Optional[float]:
    """First half of a "made-attempted" cell such as "3-10"."""
    v = row.get(name)
    if v is None:
        return None
    m = re.match(r"\s*(\d+)\s*-\s*\d+", str(v))
    return float(m.group(1)) if m else None


def total(*parts: Callable[[dict], Optional[float]]) -> Callable[[dict], Optional[float]]:
    def f(row: dict) -> Optional[float]:
        vals = [p(row) for p in parts]
        return None if any(v is None for v in vals) else sum(vals)
    return f


def field(name: str) -> Callable[[dict], Optional[float]]:
    return lambda row: num(row, name)


def innings_to_outs(row: dict) -> Optional[float]:
    v = row.get("innings")
    if v is None:
        return None
    m = re.match(r"\s*(\d+)(?:\.(\d))?\s*$", str(v))
    if not m:
        return None
    return float(int(m.group(1)) * 3 + int(m.group(2) or 0))


def total_bases(row: dict) -> Optional[float]:
    h, d, t, hr = (num(row, k) for k in ("hits", "doubles", "triples", "homeRuns"))
    if None in (h, d, t, hr):
        return None
    return h + d + 2 * t + 3 * hr


@dataclass(frozen=True)
class Stat:
    league: str
    key: str
    label: str
    value: Callable[[dict], Optional[float]]
    category: Optional[str] = None  # ESPN gamelog ?category= (MLB batting/pitching)


_pts, _reb, _ast = field("points"), field("totalRebounds"), field("assists")
_stl, _blk = field("steals"), field("blocks")
_threes = lambda r: made(r, "threePointFieldGoalsMade-threePointFieldGoalsAttempted")
_ftm = lambda r: made(r, "freeThrowsMade-freeThrowsAttempted")


def _basketball(league: str) -> list[Stat]:
    return [
        Stat(league, "points", "Points", _pts),
        Stat(league, "rebounds", "Rebounds", _reb),
        Stat(league, "assists", "Assists", _ast),
        Stat(league, "threes", "3-Pointers Made", _threes),
        Stat(league, "steals", "Steals", _stl),
        Stat(league, "blocks", "Blocks", _blk),
        Stat(league, "stocks", "Steals + Blocks", total(_stl, _blk)),
        Stat(league, "pra", "Pts + Rebs + Asts", total(_pts, _reb, _ast)),
        Stat(league, "pr", "Points + Rebounds", total(_pts, _reb)),
        Stat(league, "pa", "Points + Assists", total(_pts, _ast)),
        Stat(league, "ra", "Rebounds + Assists", total(_reb, _ast)),
        Stat(league, "ftm", "Free Throws Made", _ftm),
    ]


STATS: dict[tuple[str, str], Stat] = {}
for _s in (
    _basketball("nba")
    + _basketball("wnba")
    + [
        Stat("nfl", "pass_yds", "Passing Yards", field("passingYards")),
        Stat("nfl", "pass_tds", "Passing TDs", field("passingTouchdowns")),
        Stat("nfl", "pass_att", "Pass Attempts", field("passingAttempts")),
        Stat("nfl", "pass_comp", "Completions", field("completions")),
        Stat("nfl", "pass_int", "Interceptions Thrown", field("interceptions")),
        Stat("nfl", "rush_yds", "Rushing Yards", field("rushingYards")),
        Stat("nfl", "rush_att", "Rushing Attempts", field("rushingAttempts")),
        Stat("nfl", "rec_yds", "Receiving Yards", field("receivingYards")),
        Stat("nfl", "receptions", "Receptions", field("receptions")),
        Stat("nfl", "rush_rec_yds", "Rush + Rec Yards",
             total(lambda r: num(r, "rushingYards") or 0.0, lambda r: num(r, "receivingYards") or 0.0)),
        Stat("mlb", "hits", "Hits", field("hits"), "batting"),
        Stat("mlb", "home_runs", "Home Runs", field("homeRuns"), "batting"),
        Stat("mlb", "rbis", "RBIs", field("RBIs"), "batting"),
        Stat("mlb", "runs", "Runs", field("runs"), "batting"),
        Stat("mlb", "total_bases", "Total Bases", total_bases, "batting"),
        Stat("mlb", "hrr", "Hits + Runs + RBIs",
             total(field("hits"), field("runs"), field("RBIs")), "batting"),
        Stat("mlb", "stolen_bases", "Stolen Bases", field("stolenBases"), "batting"),
        Stat("mlb", "pitcher_ks", "Pitcher Strikeouts", field("strikeouts"), "pitching"),
        Stat("mlb", "pitching_outs", "Pitching Outs", innings_to_outs, "pitching"),
        Stat("mlb", "hits_allowed", "Hits Allowed", field("hits"), "pitching"),
        Stat("mlb", "earned_runs", "Earned Runs Allowed", field("earnedRuns"), "pitching"),
        Stat("nhl", "goals", "Goals", field("goals")),
        Stat("nhl", "assists", "Assists", field("assists")),
        Stat("nhl", "points", "Points", field("points")),
        Stat("nhl", "shots", "Shots on Goal", field("shotsTotal")),
        Stat("nhl", "saves", "Goalie Saves", field("saves")),
    ]
):
    STATS[(_s.league, _s.key)] = _s


def get(league: str, key: str) -> Stat:
    try:
        return STATS[(league, key)]
    except KeyError:
        raise KeyError(f"unknown stat {league}/{key}") from None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+]", "", s.lower().replace("&", "+"))


# Free-text stat names (as Underdog and similar apps label them) -> stat key.
_ALIASES = {
    "points": "points", "pts": "points",
    "rebounds": "rebounds", "rebs": "rebounds",
    "assists": "assists", "asts": "assists",
    "3pointersmade": "threes", "3ptmade": "threes", "threes": "threes", "3pm": "threes",
    "threepointersmade": "threes",
    "steals": "steals", "blocks": "blocks", "blks": "blocks",
    "blks+stls": "stocks", "steals+blocks": "stocks", "stocks": "stocks", "blocks+steals": "stocks",
    "pts+rebs+asts": "pra", "points+rebounds+assists": "pra", "pra": "pra",
    "points+rebounds": "pr", "pts+rebs": "pr",
    "points+assists": "pa", "pts+asts": "pa",
    "rebounds+assists": "ra", "rebs+asts": "ra",
    "freethrowsmade": "ftm",
    "passingyards": "pass_yds", "passyards": "pass_yds",
    "passingtds": "pass_tds", "passingtouchdowns": "pass_tds",
    "passattempts": "pass_att", "passingattempts": "pass_att",
    "completions": "pass_comp", "passcompletions": "pass_comp",
    "interceptions": "pass_int", "interceptionsthrown": "pass_int",
    "rushingyards": "rush_yds", "rushyards": "rush_yds",
    "rushingattempts": "rush_att", "rushattempts": "rush_att",
    "receivingyards": "rec_yds", "recyards": "rec_yds",
    "receptions": "receptions",
    "rush+recyards": "rush_rec_yds", "rushing+receivingyards": "rush_rec_yds",
    "hits": "hits", "homeruns": "home_runs", "rbis": "rbis", "runs": "runs",
    "totalbases": "total_bases", "hits+runs+rbis": "hrr", "stolenbases": "stolen_bases",
    "strikeouts": "pitcher_ks", "pitcherstrikeouts": "pitcher_ks",
    "pitchingouts": "pitching_outs", "outs": "pitching_outs",
    "hitsallowed": "hits_allowed", "earnedrunsallowed": "earned_runs", "earnedruns": "earned_runs",
    "goals": "goals", "shots": "shots", "shotsongoal": "shots", "sog": "shots", "saves": "saves",
}


def resolve(league: str, name: str) -> Optional[Stat]:
    """Map a stat key or display name to a Stat for the league, if supported."""
    league = league.lower()
    if (league, name) in STATS:
        return STATS[(league, name)]
    key = _ALIASES.get(_norm(name))
    if league == "nhl" and _norm(name) == "points":
        key = "points"
    return STATS.get((league, key)) if key else None
