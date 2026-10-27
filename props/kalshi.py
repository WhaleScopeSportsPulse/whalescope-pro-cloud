"""Open player-prop markets from Kalshi's official public API (no auth needed)."""
from __future__ import annotations

import os
import re
from typing import Iterable, Optional

from . import espn, net
from .props import Prop

BASE = os.environ.get("KALSHI_BASE", "https://api.elections.kalshi.com/trade-api/v2/")
MARKET_TTL = float(os.environ.get("PROPS_KALSHI_TTL", 60))

# Kalshi series ticker -> (league, stat key). Every series here is a
# per-game player threshold ("Player: N+") market.
SERIES = {
    "KXNBAPTS": ("nba", "points"), "KXNBAREB": ("nba", "rebounds"), "KXNBAAST": ("nba", "assists"),
    "KXNBA3PT": ("nba", "threes"), "KXNBASTL": ("nba", "steals"), "KXNBABLK": ("nba", "blocks"),
    "KXNBASTOCK": ("nba", "stocks"), "KXNBASTOCKS": ("nba", "stocks"), "KXNBAPRA": ("nba", "pra"),
    "KXNBAPR": ("nba", "pr"), "KXNBAPA": ("nba", "pa"), "KXNBARA": ("nba", "ra"), "KXNBAFTM": ("nba", "ftm"),
    "KXWNBAPTS": ("wnba", "points"), "KXWNBAREB": ("wnba", "rebounds"), "KXWNBAAST": ("wnba", "assists"),
    "KXWNBA3PT": ("wnba", "threes"),
    "KXNFLPASSYDS": ("nfl", "pass_yds"), "KXNFLPASSTDS": ("nfl", "pass_tds"),
    "KXNFLPASSATT": ("nfl", "pass_att"), "KXNFLPASSCOMP": ("nfl", "pass_comp"),
    "KXNFLPASSINT": ("nfl", "pass_int"), "KXNFLRSHYDS": ("nfl", "rush_yds"), "KXNFLRSHATT": ("nfl", "rush_att"),
    "KXNFLRECYDS": ("nfl", "rec_yds"), "KXNFLREC": ("nfl", "receptions"), "KXNFLRRYDS": ("nfl", "rush_rec_yds"),
    "KXMLBHIT": ("mlb", "hits"), "KXMLBHR": ("mlb", "home_runs"), "KXMLBRBI": ("mlb", "rbis"),
    "KXMLBTB": ("mlb", "total_bases"), "KXMLBHRR": ("mlb", "hrr"), "KXMLBSB": ("mlb", "stolen_bases"),
    "KXMLBKS": ("mlb", "pitcher_ks"), "KXMLBOUTS": ("mlb", "pitching_outs"), "KXMLBHA": ("mlb", "hits_allowed"),
    "KXMLBERA": ("mlb", "earned_runs"),
    "KXNHLPTS": ("nhl", "points"), "KXNHLAST": ("nhl", "assists"), "KXNHLGOAL": ("nhl", "goals"),
    "KXNHLSAVES": ("nhl", "saves"),
}

_DATE = re.compile(r"^\d{2}[A-Z]{3}\d{2}(\d{4})?")


def _price(m: dict, key: str) -> Optional[float]:
    v = m.get(f"{key}_dollars")
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if 0 < f < 1 else None


def split_teams(league: str, event_ticker: str, player_seg: str, table: dict) -> tuple[Optional[dict], Optional[dict]]:
    """(player team, opponent) from e.g. event "KXNBAPTS-26OCT27DETNYK" and player "NYKMMCBRIDE2"."""
    tail = event_ticker.split("-", 1)[-1]
    pair = _DATE.sub("", tail)
    for i in range(2, len(pair) - 1):
        a, b = pair[:i], pair[i:]
        ta, tb = espn.match_team(league, a, table), espn.match_team(league, b, table)
        if not (ta and tb) or ta["id"] == tb["id"]:
            continue
        if player_seg.startswith(a):
            return ta, tb
        if player_seg.startswith(b):
            return tb, ta
    return None, None


def parse_market(m: dict, league: str, stat_key: str, table: dict) -> Optional[Prop]:
    if m.get("status") not in (None, "active", "open"):
        return None
    sub = m.get("yes_sub_title") or m.get("title") or ""
    name, _, rest = sub.partition(":")
    if not rest:
        return None
    line = m.get("floor_strike")
    if line is None or m.get("strike_type") not in (None, "greater"):
        n = re.search(r"(\d+(?:\.\d+)?)\s*\+", rest)
        if not n:
            return None
        line = float(n.group(1)) - 0.5
    parts = m["ticker"].split("-")
    seg = parts[2] if len(parts) >= 4 else ""
    team, opp = split_teams(league, m.get("event_ticker", ""), seg, table)
    yes, no = _price(m, "yes_ask"), _price(m, "no_ask")
    return Prop(
        source="kalshi", league=league, stat=stat_key, player=name.strip(), line=float(line),
        team=team["abbr"] if team else None, team_id=team["id"] if team else None,
        opponent=opp["abbr"] if opp else None, opponent_id=opp["id"] if opp else None,
        start_time=m.get("occurrence_datetime") or m.get("expected_expiration_time"),
        market_id=m["ticker"], over_price=yes, under_price=no,
        url=f"https://kalshi.com/markets/{m.get('event_ticker', '').split('-')[0].lower()}",
    )


def markets(series: str) -> list[dict]:
    out, cursor = [], None
    while True:
        data = net.get_json(f"{BASE}markets", {"series_ticker": series, "status": "open", "limit": 1000,
                                               "cursor": cursor}, ttl=MARKET_TTL)
        out += data.get("markets") or []
        cursor = data.get("cursor")
        if not cursor or not data.get("markets"):
            return out


def fetch_props(leagues: Optional[Iterable[str]] = None) -> list[Prop]:
    want = {l.lower() for l in leagues} if leagues else None
    props: list[Prop] = []
    tables: dict[str, dict] = {}
    for series, (league, stat_key) in SERIES.items():
        if want and league not in want:
            continue
        ms = markets(series)
        if not ms:
            continue
        table = tables.get(league) or tables.setdefault(league, espn.teams(league))
        props += [p for m in ms if (p := parse_market(m, league, stat_key, table))]
    return props
