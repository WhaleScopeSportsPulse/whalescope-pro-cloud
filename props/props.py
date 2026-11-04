"""The common prop record every source produces."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class Prop:
    source: str                     # "kalshi" | "underdog"
    league: str                     # nba, wnba, nfl, mlb, nhl
    stat: str                       # key in stats.STATS
    player: str
    line: float                     # over = stat > line
    team: Optional[str] = None      # ESPN abbreviation
    team_id: Optional[str] = None
    opponent: Optional[str] = None
    opponent_id: Optional[str] = None
    start_time: Optional[str] = None
    market_id: Optional[str] = None
    over_price: Optional[float] = None   # Kalshi: YES ask in dollars (= implied prob.)
    under_price: Optional[float] = None  # Kalshi: NO ask
    url: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)
