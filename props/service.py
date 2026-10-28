"""Join prop lines with player game logs: hit rates, availability and the v2 model."""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Optional

from . import espn, kalshi, model, underdog
from .hitrate import hit_rates
from .props import Prop
from .stats import STATS, get as get_stat

log = logging.getLogger("props")
WORKERS = int(os.environ.get("PROPS_WORKERS", 6))
SEASONS_BACK = int(os.environ.get("PROPS_H2H_SEASONS_BACK", 2))
WINDOW_KEYS = ("l5", "l10", "l20", "season", "h2h")
AVAILABILITY_KEYS = ("injury_status", "starter", "min_l5", "min_season", "min_trend")
MODEL_KEYS = ("p_model", "edge", "edge_side", "model_version")
LINEUP_WINDOW = dt.timedelta(minutes=90)  # look for posted NBA starters this close to tip-off


def _resolve_player(p: Prop) -> Optional[dict]:
    ids = [i for i in (p.team_id, p.opponent_id) if i]
    found = espn.find_player(p.league, p.player, ids) if ids else None
    if not found and not p.team_id:
        found = espn.find_player(p.league, p.player, [t["id"] for t in espn.teams(p.league).values()])
    return found


def enrich(props: list[Prop]) -> list[dict]:
    """One output row per prop; players and game logs are fetched once per player."""
    groups: dict[tuple, list[Prop]] = {}
    bare: list[Prop] = []  # league or stat ESPN doesn't cover: no hit rates
    for p in props:
        if (p.league, p.stat) not in STATS:
            bare.append(p)
            continue
        cat = get_stat(p.league, p.stat).category
        groups.setdefault((p.league, p.player.lower(), p.team_id, cat), []).append(p)
    hurt: dict[str, dict[str, str]] = {}
    for league in {k[0] for k in groups}:
        try:
            hurt[league] = espn.injuries(league)
        except Exception as e:  # availability is best effort; rates still load
            log.warning("injuries unavailable for %s: %s", league, e)
            hurt[league] = {}

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
        avail = {"injury_status": hurt.get(first.league, {}).get(player["id"]), "starter": None,
                 **model.minutes_trend(first.league, games, season)}
        out = []
        for p in items:
            if not p.team_id:
                p.team_id = player["team_id"]
                p.team = next((t["abbr"] for t in espn.teams(p.league).values() if t["id"] == p.team_id), p.team)
            if p.league == "nba" and avail["starter"] is None:
                avail["starter"] = _starter(p, player["id"])
            stat = get_stat(p.league, p.stat)
            hr = hit_rates(games, stat, p.line, season, p.opponent_id)
            row = _row(p, athlete_id=player["id"], rates=hr)
            row.update(avail)
            row.update(model.evaluate(row, p.league, stat, games, season))
            out.append(row)
        return out

    with ThreadPoolExecutor(WORKERS) as ex:
        return [r for rows in ex.map(work, groups.values()) for r in rows] + [_row(p) for p in bare]


def _starter(p: Prop, athlete_id: str) -> Optional[bool]:
    """Whether ESPN lists the player in the starting lineup, None until a lineup is posted."""
    when = espn.parse_time(p.start_time)
    now = dt.datetime.now(dt.timezone.utc)
    if not p.team_id or not when or not now - dt.timedelta(hours=4) <= when <= now + LINEUP_WINDOW:
        return None
    try:
        ev = espn.find_event(p.league, p.team_id, p.opponent_id, when)
        ids = espn.starters(p.league, ev["id"], p.team_id) if ev else None
    except Exception as e:
        log.warning("lineup unavailable for %s: %s", p.player, e)
        return None
    return None if ids is None else athlete_id in ids


def _row(p: Prop, athlete_id: Optional[str] = None, rates: Optional[dict] = None,
         error: Optional[str] = None) -> dict:
    d = p.to_dict()
    d["stat_label"] = p.stat_label or get_stat(p.league, p.stat).label
    d["athlete_id"] = athlete_id
    d["error"] = error
    d.update({k: None for k in (*AVAILABILITY_KEYS, *MODEL_KEYS)})
    if rates:
        d.update({k: rates[k] for k in (*WINDOW_KEYS, "recent")})
        l10 = rates["l10"]["rate"]
        # Kalshi YES ask is the market's implied over probability.
        d["l10_minus_price"] = round(l10 - p.over_price, 4) if l10 is not None and p.over_price else None
    return d


SOURCES = ("kalshi", "underdog")


def collect_sources(sources: Iterable[str] = SOURCES, leagues: Optional[Iterable[str]] = None
                    ) -> tuple[list[Prop], list[str], dict[str, str]]:
    """(props, sources that loaded, {source: error}). One failing source doesn't stop the others."""
    want = {l.lower() for l in leagues} if leagues else None
    fetchers = {"kalshi": kalshi.fetch_props, "underdog": underdog.fetch_props}
    props: list[Prop] = []
    ok, errors = [], {}
    for src in sources:
        try:
            props += fetchers[src](want)
            ok.append(src)
        except Exception as e:
            log.warning("skipping %s this refresh: %s", src, e)
            errors[src] = str(e)
    now = dt.datetime.now(dt.timezone.utc)
    return [p for p in props if not started(p.start_time, now)], ok, errors


def started(start_time: Optional[str], now: dt.datetime) -> bool:
    """True once the game has begun; props for started games leave the board."""
    t = espn.parse_time(start_time)
    return bool(t and t <= now)


def collect(sources: Iterable[str] = SOURCES, leagues: Optional[Iterable[str]] = None) -> list[Prop]:
    return collect_sources(sources, leagues)[0]


class Snapshot:
    """Background-refreshed slate so API requests don't wait on hundreds of lookups."""

    def __init__(self, every: float = float(os.environ.get("PROPS_REFRESH_SECONDS", 300))):
        self.every, self.rows, self.updated, self.error = every, [], 0.0, None
        self.leagues = [l for l in os.environ.get("PROPS_LEAGUES", "").lower().split(",") if l] or None
        self._lock = threading.Lock()
        self._kick = threading.Event()

    def refresh(self) -> None:
        try:
            props, ok, errors = collect_sources(leagues=self.leagues)
            if not ok:
                raise RuntimeError("; ".join(f"{k}: {v}" for k, v in errors.items()))
            rows = enrich(props)
            # Keep the last good rows for a source that failed this time.
            with self._lock:
                now = dt.datetime.now(dt.timezone.utc)
                rows += [r for r in self.rows if r["source"] in errors and not started(r.get("start_time"), now)]
                self.rows, self.updated = rows, time.time()
                self.error = "; ".join(f"{k}: {v}" for k, v in errors.items()) or None
        except Exception as e:
            log.exception("refresh failed")
            self.error = str(e)
            return
        if os.environ.get("DATABASE_URL"):
            from . import db
            try:
                db.save([r for r in rows if r["source"] in ok], ok, self.leagues)
                if errors:
                    db.drop_started(errors)
            except Exception as e:
                log.exception("saving props_board failed")
                self.error = f"db: {e}"
            from . import picks
            try:
                picks.run(rows)
            except Exception:
                log.exception("picks_log failed")

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
