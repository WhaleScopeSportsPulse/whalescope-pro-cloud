"""Outlier-style hit rates: L5 / L10 / L20 / current season / head-to-head."""
from __future__ import annotations

from typing import Optional, Sequence

from .espn import Game
from .stats import Stat

WINDOWS = (("l5", 5), ("l10", 10), ("l20", 20))


def window(values: Sequence[float], line: float) -> dict:
    """Over-side record for a list of game values against a line.

    `rate` is hits / (hits + misses); a game landing exactly on a whole-number
    line is a push and is left out of the rate, as the books grade it.
    """
    over = sum(1 for v in values if v > line)
    under = sum(1 for v in values if v < line)
    push = len(values) - over - under
    decided = over + under
    return {
        "games": len(values),
        "over": over,
        "under": under,
        "push": push,
        "rate": round(over / decided, 4) if decided else None,
        "avg": round(sum(values) / len(values), 2) if values else None,
    }


def hit_rates(games: Sequence[Game], stat: Stat, line: float, season: int,
              opponent_id: Optional[str] = None) -> dict:
    """`games` must be most-recent-first and already filtered to games played."""
    vals = [(g, v) for g in games if (v := stat.value(g.row)) is not None]
    allv = [v for _, v in vals]
    out = {name: window(allv[:n], line) for name, n in WINDOWS}
    out["season"] = window([v for g, v in vals if g.season == season], line)
    out["h2h"] = window([v for g, v in vals if opponent_id and g.opponent_id == opponent_id], line) \
        if opponent_id else None
    out["recent"] = [{"date": g.date[:10], "opp": g.opponent, "home": g.home, "value": v}
                     for g, v in vals[:10]]
    return out
