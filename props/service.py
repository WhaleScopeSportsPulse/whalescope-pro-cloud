"""Join prop lines with player game logs and compute hit rates."""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Optional

from . import espn, kalshi, underdog
from .hitrate import hit_rates
from .props import Prop
from .stats import get as get_stat

log = logging.getLogger("props")
WORKERS = int(os.environ.get("PROPS_WORKERS", 6))
SEASONS_BACK = int(os.environ.get("PROPS_H2H_SEASONS_BACK", 2))
WINDOW_KEYS = ("l5", "l10", "l20", "season", "h2h")


def _resolve_player(p: Prop) -> Optional[dict]:
    ids = [i for i in (p.team_id, p.opponent_id) if i]
    found = espn.find_player(p.league, p.player, ids) if ids else None
    if not found and not p.team_id:
        found = espn.find_player(p.league, p.player, [t["id"] for t in espn.teams(p.league).values()])
    return found


def enrich(props: list[Prop]) -> list[dict]:
    """One output row per prop; players and game logs are fetched once per player."""
    groups: dict[tuple, list[Prop]] = {}
    for p in props:
        cat = get_stat(p.league, p.stat).category
        groups.setdefault((p.league, p.player.lower(), p.team_id, cat), []).append(p)

    def work(items: list[Prop]) -> list[dict]:
        first = items[0]
        stat0 = get_stat(first.league, first.stat)
        try:
            player = _resolve_player(first)
            if not player:
                return [_row(p, error="player not found on ESPN roster") for p in items]
            season, games = espn.player_games(first.league, player["id"], stat0, SEASONS_BACK)
        except Exception as e:  # keep one bad lookup from sinking the slate
            log.warning("lookup failed for %s: %s", first.player, e)
            return [_row(p, error=f"lookup failed: {e}") for p in items]
        out = []
        for p in items:
            if not p.team_id:
                p.team_id = player["team_id"]
                p.team = next((t["abbr"] for t in espn.teams(p.league).values() if t["id"] == p.team_id), p.team)
            hr = hit_rates(games, get_stat(p.league, p.stat), p.line, season, p.opponent_id)
            out.append(_row(p, athlete_id=player["id"], rates=hr))
        return out

    with ThreadPoolExecutor(WORKERS) as ex:
        return [r for rows in ex.map(work, groups.values()) for r in rows]


def _row(p: Prop, athlete_id: Optional[str] = None, rates: Optional[dict] = None,
         error: Optional[str] = None) -> dict:
    d = p.to_dict()
    d["stat_label"] = get_stat(p.league, p.stat).label
    d["athlete_id"] = athlete_id
    d["error"] = error
    if rates:
        d.update({k: rates[k] for k in (*WINDOW_KEYS, "recent")})
        l10 = rates["l10"]["rate"]
        # Kalshi YES ask is the market's implied over probability.
        d["l10_minus_price"] = round(l10 - p.over_price, 4) if l10 is not None and p.over_price else None
    return d


def collect(sources: Iterable[str] = ("kalshi", "underdog"), leagues: Optional[Iterable[str]] = None) -> list[Prop]:
    want = {l.lower() for l in leagues} if leagues else None
    props: list[Prop] = []
    if "kalshi" in sources:
        props += kalshi.fetch_props(want)
    if "underdog" in sources:
        props += [p for p in underdog.to_props() if not want or p.league in want]
    return props


class Snapshot:
    """Background-refreshed slate so API requests don't wait on hundreds of lookups."""

    def __init__(self, every: float = float(os.environ.get("PROPS_REFRESH_SECONDS", 300))):
        self.every, self.rows, self.updated, self.error = every, [], 0.0, None
        self.leagues = [l for l in os.environ.get("PROPS_LEAGUES", "").lower().split(",") if l] or None
        self._lock = threading.Lock()
        self._kick = threading.Event()

    def refresh(self) -> None:
        try:
            rows = enrich(collect(leagues=self.leagues))
            with self._lock:
                self.rows, self.updated, self.error = rows, time.time(), None
        except Exception as e:
            log.exception("refresh failed")
            self.error = str(e)

    def kick(self) -> None:
        self._kick.set()

    def start(self) -> None:
        def loop():
            while True:
                self.refresh()
                self._kick.wait(self.every)
                self._kick.clear()
        threading.Thread(target=loop, daemon=True, name="props-refresh").start()

    def query(self, league=None, source=None, stat=None, player=None, window="l10",
              min_rate=None, min_games=0, side="over", limit=500) -> list[dict]:
        with self._lock:
            rows = list(self.rows)
        return filter_rows(rows, league, source, stat, player, window, min_rate, min_games, side, limit)


def filter_rows(rows, league=None, source=None, stat=None, player=None, window="l10",
                min_rate=None, min_games=0, side="over", limit=500) -> list[dict]:
    if window not in WINDOW_KEYS:
        raise ValueError(f"window must be one of {', '.join(WINDOW_KEYS)}")

    def rate(r):
        w = r.get(window)
        if not w or w["rate"] is None or w["games"] < min_games:
            return None
        return w["rate"] if side == "over" else 1 - w["rate"]

    out = [r for r in rows
           if (not league or r["league"] == league)
           and (not source or r["source"] == source)
           and (not stat or r["stat"] == stat)
           and (not player or player.lower() in r["player"].lower())]
    if min_rate is not None:
        out = [r for r in out if (x := rate(r)) is not None and x >= min_rate]
    out.sort(key=lambda r: (rate(r) is None, -(rate(r) or 0), r["player"]))
    return out[:limit]
