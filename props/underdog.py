"""Underdog Fantasy lines, imported from a file you supply.

Underdog has no official public API, so this service doesn't call or scrape
it. Load the slate you're looking at as CSV or JSON with these columns:

    league, player, team, opponent, stat, line[, start_time]

`stat` may be a key from stats.py ("pra") or Underdog's label
("Pts + Rebs + Asts"). Imported lines are stored in PROPS_DATA_DIR.
"""
from __future__ import annotations

import csv
import io
import json
import os
from typing import Optional

from . import espn
from .props import Prop
from .stats import LEAGUES, resolve

DATA_DIR = os.environ.get("PROPS_DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
STORE = os.path.join(DATA_DIR, "underdog_lines.json")
REQUIRED = ("league", "player", "stat", "line")


def parse(text: str) -> tuple[list[dict], list[str]]:
    """Validate rows from CSV or JSON text. Returns (rows, errors)."""
    text = text.strip()
    if text.startswith("[") or text.startswith("{"):
        raw = json.loads(text)
        if isinstance(raw, dict):
            raw = raw.get("lines") or raw.get("props") or []
    else:
        raw = list(csv.DictReader(io.StringIO(text)))
    rows, errors = [], []
    for i, r in enumerate(raw, 1):
        r = {str(k).strip().lower(): (str(v).strip() if v is not None else "") for k, v in r.items()}
        missing = [k for k in REQUIRED if not r.get(k)]
        if missing:
            errors.append(f"row {i}: missing {', '.join(missing)}")
            continue
        league = r["league"].lower()
        if league not in LEAGUES:
            errors.append(f"row {i}: unsupported league {r['league']!r}")
            continue
        stat = resolve(league, r["stat"])
        if not stat:
            errors.append(f"row {i}: unsupported stat {r['stat']!r} for {league}")
            continue
        try:
            line = float(r["line"])
        except ValueError:
            errors.append(f"row {i}: bad line {r['line']!r}")
            continue
        rows.append({"league": league, "player": r["player"], "team": r.get("team", "").upper() or None,
                     "opponent": r.get("opponent", "").upper() or None, "stat": stat.key, "line": line,
                     "start_time": r.get("start_time") or None})
    return rows, errors


def save(rows: list[dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rows, f, indent=1)
    os.replace(tmp, STORE)


def load() -> list[dict]:
    try:
        with open(STORE) as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def to_props(rows: Optional[list[dict]] = None) -> list[Prop]:
    rows = load() if rows is None else rows
    tables: dict[str, dict] = {}
    out = []
    for r in rows:
        league = r["league"]
        table = tables.get(league) or tables.setdefault(league, espn.teams(league))
        team = espn.match_team(league, r["team"], table) if r.get("team") else None
        opp = espn.match_team(league, r["opponent"], table) if r.get("opponent") else None
        out.append(Prop(
            source="underdog", league=league, stat=r["stat"], player=r["player"], line=r["line"],
            team=team["abbr"] if team else r.get("team"), team_id=team["id"] if team else None,
            opponent=opp["abbr"] if opp else r.get("opponent"), opponent_id=opp["id"] if opp else None,
            start_time=r.get("start_time")))
    return out
