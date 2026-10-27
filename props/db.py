"""Write the enriched slate to Postgres table props.props_board (stdlib only).

Uses Neon's HTTPS SQL endpoint (https://<host>/sql), so it needs no driver
and works where outbound port 5432 is blocked. DATABASE_URL must be a Neon
connection string. Each save replaces the rows for the sources it was given
in one transaction, so readers never see a half-written board.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable, Optional

from .service import WINDOW_KEYS

# Added after the table first shipped; ADD COLUMN IF NOT EXISTS upgrades existing boards in place.
EXTRA_COLS = ("injury_status text, starter boolean, min_l5 double precision, min_season double precision, "
              "min_trend double precision, p_model double precision, edge double precision, edge_side text, "
              "model_version text")

CHUNK = 1000

SCHEMA = [
    "CREATE SCHEMA IF NOT EXISTS props",
    """CREATE TABLE IF NOT EXISTS props.props_board (
        source text NOT NULL,
        league text NOT NULL,
        stat text NOT NULL,
        stat_label text,
        player text NOT NULL,
        team text,
        opponent text,
        line double precision NOT NULL,
        over_price double precision,
        under_price double precision,
        start_time timestamptz,
        market_id text,
        url text,
        athlete_id text,
        l5_rate double precision, l5_games int,
        l10_rate double precision, l10_games int,
        l20_rate double precision, l20_games int,
        season_rate double precision, season_games int,
        h2h_rate double precision, h2h_games int,
        l10_minus_price double precision,
        windows jsonb,
        recent jsonb,
        error text,
        updated_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS props_board_league_source ON props.props_board (league, source)",
    *(f"ALTER TABLE props.props_board ADD COLUMN IF NOT EXISTS {c}" for c in EXTRA_COLS.split(", ")),
]

_COLS = ("source text, league text, stat text, stat_label text, player text, team text, opponent text, "
         "line double precision, over_price double precision, under_price double precision, "
         "start_time timestamptz, market_id text, url text, athlete_id text, "
         + ", ".join(f"{k}_rate double precision, {k}_games int" for k in WINDOW_KEYS)
         + ", l10_minus_price double precision, windows jsonb, recent jsonb, error text, " + EXTRA_COLS)
_NAMES = ", ".join(c.split()[0] for c in _COLS.split(", "))
INSERT = (f"INSERT INTO props.props_board ({_NAMES}) "
          f"SELECT {_NAMES} FROM jsonb_to_recordset($1::jsonb) AS r({_COLS})")


def _flat(r: dict) -> dict:
    d = {k: r.get(k) for k in ("source", "league", "stat", "stat_label", "player", "team", "opponent", "line",
                               "over_price", "under_price", "start_time", "market_id", "url", "athlete_id",
                               "l10_minus_price", "recent", "error",
                               *(c.split()[0] for c in EXTRA_COLS.split(", ")))}
    for k in WINDOW_KEYS:
        w = r.get(k) or {}
        d[f"{k}_rate"], d[f"{k}_games"] = w.get("rate"), w.get("games")
    d["windows"] = {k: r.get(k) for k in WINDOW_KEYS}
    return d


def _arr(xs: Iterable[str]) -> str:
    return "{" + ",".join(sorted({x.lower() for x in xs})) + "}"


def _post(body: dict, url: str) -> dict:
    host = urllib.parse.urlparse(url).hostname
    req = urllib.request.Request(f"https://{host}/sql", data=json.dumps(body).encode(), headers={
        "Neon-Connection-String": url, "Neon-Batch-Isolation-Level": "ReadCommitted",
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Neon SQL {e.code}: {e.read().decode(errors='replace')[:500]}") from None


def query(sql: str, params: list | None = None, url: str | None = None) -> list[dict]:
    return _post({"query": sql, "params": params or []}, url or os.environ["DATABASE_URL"])["rows"]


def save(rows: list[dict], sources: Iterable[str], leagues: Optional[Iterable[str]] = None,
         url: str | None = None) -> int:
    """Replace the board rows for `sources` (and `leagues`, default all) with `rows` in one transaction."""
    url = url or os.environ["DATABASE_URL"]
    flat = [_flat(r) for r in rows]
    qs = [{"query": s, "params": []} for s in SCHEMA]
    qs.append({"query": "DELETE FROM props.props_board WHERE source = ANY($1::text[])"
                        " AND ($2::text[] IS NULL OR league = ANY($2::text[]))",
               "params": [_arr(sources), _arr(leagues) if leagues else None]})
    qs += [{"query": INSERT, "params": [json.dumps(flat[i:i + CHUNK])]} for i in range(0, len(flat), CHUNK)]
    _post({"queries": qs}, url)
    return len(flat)
