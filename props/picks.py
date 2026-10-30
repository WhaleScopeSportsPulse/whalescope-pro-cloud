"""Log qualifying picks to props.picks_log and grade them from ESPN box scores.

Two rules run on every refresh; a prop side is logged once per market_id + side
+ rule_version when its game starts within the next two hours, the player isn't
listed Out or Doubtful, and:

v1_hitrate:
  - L5, L10 and L20 each have a full sample (5, 10, 20 games),
  - that side's hit rate is at least 0.70 in all three,
  - 0 < price < 1, and score = min(L5, L10, L20) - price >= 0.10.
v2_model (see model.py):
  - at least V2_MIN_GAMES games in L20, 0 < price < 1,
  - edge = model probability - price - fee >= V2_MIN_EDGE on that side.

The game and its start come from ESPN's scoreboard (the board's own start
time only narrows the search). The ESPN event is stored with the pick and graded once ESPN marks it final: hit / miss, push
when the stat lands on the line, void when the player didn't play or the
game was postponed or canceled.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from typing import Optional

from . import db, espn, net
from .stats import LEAGUES, get as get_stat

log = logging.getLogger("props")

MIN_RATE = 0.70
MIN_SCORE = 0.10
V2_MIN_EDGE = 0.05
V2_MIN_GAMES = 10
EXCLUDED = ("Out", "Doubtful")
WINDOW = dt.timedelta(hours=2)
FULL = (("l5", 5), ("l10", 10), ("l20", 20))
# ESPN can take many hours to add a final game to a player's game log, so a missing
# log entry alone never voids a pick: the box score must show the player didn't play.
# Only if ESPN never publishes a box score do we void after this long.
NO_BOX_AFTER = dt.timedelta(hours=48)

SCHEMA = [
    "CREATE SCHEMA IF NOT EXISTS props",
    """CREATE TABLE IF NOT EXISTS props.picks_log (
        id bigserial PRIMARY KEY,
        logged_at timestamptz NOT NULL DEFAULT now(),
        source text NOT NULL,
        league text NOT NULL,
        player text NOT NULL,
        team text,
        opponent text,
        stat_label text,
        line double precision NOT NULL,
        side text NOT NULL CHECK (side IN ('over', 'under')),
        price double precision NOT NULL,
        l5 double precision,
        l10 double precision,
        l20 double precision,
        score double precision,
        start_time timestamptz NOT NULL,
        market_id text NOT NULL,
        result text CHECK (result IN ('hit', 'miss', 'push', 'void')),
        actual double precision,
        graded_at timestamptz,
        stat text NOT NULL,
        athlete_id text NOT NULL,
        event_id text NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS picks_log_ungraded ON props.picks_log (start_time) WHERE result IS NULL",
    # v2 columns; v1 rows logged before them read as v1_hitrate.
    "ALTER TABLE props.picks_log ADD COLUMN IF NOT EXISTS rule_version text NOT NULL DEFAULT 'v1_hitrate'",
    *(f"ALTER TABLE props.picks_log ADD COLUMN IF NOT EXISTS {c}" for c in (
        "p_model double precision", "edge double precision", "model_version text", "injury_status text",
        "starter boolean", "min_trend double precision")),
    *(f"ALTER TABLE props.picks_log ALTER COLUMN {c} DROP NOT NULL" for c in ("l5", "l10", "l20", "score")),
    "ALTER TABLE props.picks_log DROP CONSTRAINT IF EXISTS picks_log_market_id_side_key",
    "CREATE UNIQUE INDEX IF NOT EXISTS picks_log_market_side_rule ON props.picks_log (market_id, side, rule_version)",
]

_COLS = ("source text, league text, player text, team text, opponent text, stat_label text, "
         "line double precision, side text, price double precision, l5 double precision, "
         "l10 double precision, l20 double precision, score double precision, start_time timestamptz, "
         "market_id text, stat text, athlete_id text, event_id text, rule_version text, p_model double precision, "
         "edge double precision, model_version text, injury_status text, starter boolean, min_trend double precision")
_NAMES = ", ".join(c.split()[0] for c in _COLS.split(", "))
INSERT = (f"INSERT INTO props.picks_log ({_NAMES}) "
          f"SELECT {_NAMES} FROM jsonb_to_recordset($1::jsonb) AS r({_COLS}) "
          "ON CONFLICT (market_id, side, rule_version) DO NOTHING RETURNING id")


def side_rates(row: dict, side: str) -> Optional[dict]:
    """{"l5": r, "l10": r, "l20": r} for the side, or None unless every window is full."""
    out = {}
    for key, n in FULL:
        w = row.get(key) or {}
        if w.get("games") != n or w.get("rate") is None:
            return None
        # Rates are stored to 4 places; round so 1 - 0.3 or 0.75 - 0.65 compare as written.
        out[key] = round(w["rate"] if side == "over" else 1 - w["rate"], 4)
    return out


def qualifies(row: dict, side: str) -> Optional[dict]:
    """Rates, price and score when this side passes the rule (start time aside), else None."""
    rates = side_rates(row, side)
    price = row.get("over_price" if side == "over" else "under_price")
    if rates is None or price is None or not 0 < price < 1 or min(rates.values()) < MIN_RATE:
        return None
    score = round(min(rates.values()) - price, 4)
    if score < MIN_SCORE:
        return None
    return {**rates, "price": price, "score": score}


def model_qualifies(row: dict, side: str) -> Optional[dict]:
    """The v2 rule for one side: enough games, a usable price and edge >= V2_MIN_EDGE."""
    price = row.get("over_price" if side == "over" else "under_price")
    if (row.get("edge_side") != side or row.get("edge") is None or row["edge"] < V2_MIN_EDGE
            or price is None or not 0 < price < 1 or ((row.get("l20") or {}).get("games") or 0) < V2_MIN_GAMES):
        return None
    rates = {k: (None if (w := row.get(k) or {}).get("rate") is None
                 else round(w["rate"] if side == "over" else 1 - w["rate"], 4)) for k, _ in FULL}
    return {**rates, "price": price, "score": None}


RULES = (("v1_hitrate", qualifies), ("v2_model", model_qualifies))


def select(rows: list[dict], now: Optional[dt.datetime] = None) -> list[dict]:
    """Pick records for rows that pass a rule and whose ESPN game starts within the next 2 hours."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    for r in rows:
        if (r.get("error") or not r.get("market_id") or not r.get("athlete_id") or not r.get("team_id")
                or r.get("injury_status") in EXCLUDED):
            continue
        sides = [(s, rule, q) for rule, fn in RULES for s in ("over", "under") if (q := fn(r, s))]
        if not sides:
            continue
        listed = espn.parse_time(r.get("start_time"))
        # ESPN's scoreboard is the authority on the start; the board's time only skips far-off games.
        if not listed or not now - dt.timedelta(hours=6) <= listed <= now + WINDOW + dt.timedelta(hours=6):
            continue
        ev = espn.find_event(r["league"], r["team_id"], r.get("opponent_id"), listed)
        if not ev or ev["state"] != "pre" or not now <= ev["start"] <= now + WINDOW:
            continue
        for side, rule, q in sides:
            out.append({"source": r["source"], "league": r["league"], "player": r["player"], "team": r.get("team"),
                        "opponent": r.get("opponent"), "stat_label": r.get("stat_label"), "line": r["line"],
                        "side": side, "start_time": ev["start"].isoformat(), "market_id": r["market_id"],
                        "stat": r["stat"], "athlete_id": r["athlete_id"], "event_id": ev["id"], "rule_version": rule,
                        "p_model": r.get("p_model"), "model_version": r.get("model_version"),
                        "edge": _side_edge(r, side), "injury_status": r.get("injury_status"),
                        "starter": r.get("starter"), "min_trend": r.get("min_trend"), **q})
    return out


def _side_edge(row: dict, side: str) -> Optional[float]:
    """Model edge for this side (after the Kalshi fee), whichever rule picked it."""
    from .model import kalshi_fee
    p, price = row.get("p_model"), row.get("over_price" if side == "over" else "under_price")
    if p is None or not price or not 0 < price < 1:
        return None
    prob = p if side == "over" else 1 - p
    return round(prob - price - (kalshi_fee(price) if row["source"] == "kalshi" else 0.0), 4)


def ensure_schema(url: Optional[str] = None) -> None:
    db._post({"queries": [{"query": s, "params": []} for s in SCHEMA]}, url or os.environ["DATABASE_URL"])


def log_picks(rows: list[dict], now: Optional[dt.datetime] = None, url: Optional[str] = None) -> int:
    """Insert new picks; returns how many were new (repeats of market_id + side + rule are skipped)."""
    picks = select(rows, now)
    if not picks:
        return 0
    return len(db.query(INSERT, [json.dumps(picks)], url))


def grade_value(actual: float, line: float, side: str) -> str:
    if actual == line:
        return "push"
    return "hit" if (actual > line) == (side == "over") else "miss"


def _player_game(league: str, athlete_id: str, stat_key: str, event_id: str):
    """(played, value) for the event from a fresh game-log fetch; (False, None) if the event isn't listed."""
    stat = get_stat(league, stat_key)
    season = espn.current_season(league)
    try:
        data = net.get_json(f"{espn.WEB}{LEAGUES[league]}/athletes/{athlete_id}/gamelog",
                            {"season": season, "category": stat.category})
    except Exception as e:  # 404 when the player has no games this season
        if getattr(e, "code", None) in (400, 404):
            return False, None
        raise
    for g in espn.parse_gamelog(data, season):
        if g.event_id == event_id:
            return espn.played(league, g), stat.value(g.row)
    return False, None


def grade(now: Optional[dt.datetime] = None, url: Optional[str] = None, limit: int = 200) -> dict:
    """Grade picks whose games have finished. Returns counts by result."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = db.query("SELECT id, league, stat, athlete_id, event_id, line, side, extract(epoch FROM start_time) AS start "
                    "FROM props.picks_log "
                    "WHERE result IS NULL AND start_time < $1 ORDER BY start_time LIMIT $2",
                    [(now - dt.timedelta(hours=2)).isoformat(), limit], url)
    done: dict[str, int] = {}
    boards: dict[tuple, dict] = {}
    for p in rows:
        start = dt.datetime.fromtimestamp(float(p["start"]), dt.timezone.utc)
        try:
            key = (p["league"], start.astimezone(espn.ET).date())
            if key not in boards:
                boards[key] = {e["id"]: e for d in (key[1] - dt.timedelta(days=1), key[1])
                               for e in espn.scoreboard(p["league"], d)}
            ev = boards[key].get(p["event_id"])
            if ev is None:
                continue
            if any(x in ev["name"] for x in ("POSTPONED", "CANCELED", "CANCELLED")):
                result, actual = "void", None
            elif not ev["completed"]:
                continue
            else:
                played, actual = _player_game(p["league"], p["athlete_id"], p["stat"], p["event_id"])
                if played and actual is not None:
                    result = grade_value(actual, p["line"], p["side"])
                else:
                    box = espn.box_played(p["league"], p["event_id"], p["athlete_id"])
                    if box is False or (box is None and now - start >= NO_BOX_AFTER):
                        result, actual = "void", None
                    else:  # played (or no box score yet): wait for the game log
                        continue
        except Exception as e:  # one bad lookup shouldn't stop the rest
            log.warning("grading pick %s failed: %s", p["id"], e)
            continue
        db.query("UPDATE props.picks_log SET result = $1, actual = $2, graded_at = now() "
                 "WHERE id = $3 AND result IS NULL", [result, actual, p["id"]], url)
        done[result] = done.get(result, 0) + 1
    return done


def run(rows: list[dict], url: Optional[str] = None) -> tuple[int, dict]:
    """Log this refresh's picks and grade finished ones: (new picks, graded counts)."""
    ensure_schema(url)
    return log_picks(rows, url=url), grade(url=url)


# One pick per player + stat + line + side + game, so the same bet listed on
# Kalshi and Underdog (or picked by both rules) counts once.
_DEDUPED = """
    SELECT DISTINCT ON (player, stat, line, side, event_id{extra})
           rule_version, league, result,
           CASE WHEN side = 'over' THEN p_model ELSE 1 - p_model END AS p_side
    FROM props.picks_log WHERE logged_at >= $1
    ORDER BY player, stat, line, side, event_id{extra}, id
"""
BINS = (0.0, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 1.0001)


def report(since: dt.datetime, url: Optional[str] = None) -> dict:
    """Deduplicated results per rule and a calibration table (model probability bins vs. hit rate)."""
    per_rule = db.query(_DEDUPED.format(extra=", rule_version"), [since.isoformat()], url)
    rules: dict[str, dict] = {}
    for r in per_rule:
        d = rules.setdefault(r["rule_version"], {"picks": 0, "graded": 0, "hit": 0, "miss": 0, "push": 0, "void": 0})
        d["picks"] += 1
        if r["result"]:
            d["graded"] += 1
            d[r["result"]] += 1
    for d in rules.values():
        decided = d["hit"] + d["miss"]
        d["hit_rate"] = round(d["hit"] / decided, 3) if decided else None
    # Calibration pools both rules: one model probability per distinct bet.
    bets = [r for r in db.query(_DEDUPED.format(extra=""), [since.isoformat()], url)
            if r["p_side"] is not None and r["result"] in ("hit", "miss")]
    table = []
    for lo, hi in zip(BINS, BINS[1:]):
        inbin = [r for r in bets if lo <= float(r["p_side"]) < hi]
        if inbin:
            table.append({"bin": f"{lo:.2f}-{min(hi, 1):.2f}", "n": len(inbin),
                          "avg_p": round(sum(float(r["p_side"]) for r in inbin) / len(inbin), 3),
                          "hit_rate": round(sum(r["result"] == "hit" for r in inbin) / len(inbin), 3)})
    brier = (round(sum((float(r["p_side"]) - (r["result"] == "hit")) ** 2 for r in bets) / len(bets), 4)
             if bets else None)
    return {"since": since.isoformat(), "rules": rules, "calibration": table, "calibrated_bets": len(bets),
            "brier": brier}
