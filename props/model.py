"""Model v2: P(over) from the player's own recent stat values, blended with the market.

1. Sample: the last 20 games played plus the rest of the current season, weighted
   by recency (weight DECAY ** games-ago), so recent games count more.
2. Minutes: the mean (and spread) scale by the last 5 games' minutes over the
   sample's weighted minutes, clipped to MINUTES_CLIP (basketball and hockey only).
3. Distribution: count stats use Poisson, or negative binomial when the weighted
   variance exceeds the mean; yardage-like stats use a normal with the player's
   own weighted standard deviation. On a whole-number line, P(over) excludes the push.
4. Blend: p_model = w * p_stat + (1 - w) * p_market, w = W_MAX * n / (n + K), where
   p_market is the price with the vig removed (Kalshi: YES ask / NO ask midpoint;
   Underdog: over / (over + under)).
5. Edge per side = p(side) - price - fee; Kalshi's taker fee is 0.07 * P * (1 - P) per contract.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

from . import espn
from .espn import Game
from .stats import Stat

MODEL_VERSION = "v2.0"
DECAY = 0.9
RECENT = 20
MIN_GAMES = 5
MINUTES_CLIP = (0.8, 1.2)
W_MAX, K = 0.8, 10
P_CLIP = (0.02, 0.98)
KALSHI_FEE = 0.07


def kalshi_fee(price: float) -> float:
    return KALSHI_FEE * price * (1 - price)


def _wstats(values: Sequence[float], weights: Sequence[float]) -> tuple[float, float]:
    tw = sum(weights)
    mean = sum(w * v for w, v in zip(weights, values)) / tw
    var = sum(w * (v - mean) ** 2 for w, v in zip(weights, values)) / tw
    n_eff = tw ** 2 / sum(w * w for w in weights)
    return mean, var * n_eff / (n_eff - 1) if n_eff > 1 else var


def _poisson_cdf(k: int, lam: float) -> float:
    if k < 0:
        return 0.0
    term = total = math.exp(-lam)
    for i in range(1, k + 1):
        term *= lam / i
        total += term
    return min(total, 1.0)


def _nbinom_cdf(k: int, mean: float, var: float) -> float:
    r = mean * mean / (var - mean)
    p = r / (r + mean)                       # success probability; P(X=0) = p**r
    if k < 0:
        return 0.0
    term = total = p ** r
    for i in range(1, k + 1):
        term *= (r + i - 1) / i * (1 - p)
        total += term
    return min(total, 1.0)


def _count_over(line: float, mean: float, var: float) -> float:
    if mean <= 0:
        return 0.0
    cdf = (lambda k: _nbinom_cdf(k, mean, var)) if var > mean * 1.05 else (lambda k: _poisson_cdf(k, mean))
    k = math.floor(line)
    over = 1 - cdf(k)
    if line == k:                            # whole-number line: a push is neither side
        at = cdf(k) - cdf(k - 1)
        return over / (1 - at) if at < 1 else 0.5
    return over


def _normal_over(line: float, mean: float, sd: float) -> float:
    return 0.5 * math.erfc((line - mean) / (sd * math.sqrt(2)))


def sample(games: Sequence[Game], stat: Stat, season: int) -> list[tuple[Game, float]]:
    """(game, value) for the last RECENT games plus the rest of `season`, most recent first."""
    vals = [(g, v) for g in games if (v := stat.value(g.row)) is not None]
    return [(g, v) for i, (g, v) in enumerate(vals) if i < RECENT or g.season == season]


def minutes_trend(league: str, games: Sequence[Game], season: int) -> dict:
    """Average minutes over the last 5 games and the current season, and their ratio."""
    mins = [(g, m) for g in games if (m := espn.minutes(league, g)) is not None]
    l5 = [m for _, m in mins[:5]]
    ssn = [m for g, m in mins if g.season == season]
    a5 = sum(l5) / len(l5) if l5 else None
    asn = sum(ssn) / len(ssn) if ssn else None
    return {"min_l5": round(a5, 1) if a5 is not None else None,
            "min_season": round(asn, 1) if asn is not None else None,
            "min_trend": round(a5 / asn, 3) if a5 and asn else None}


def market_prob(over_price: Optional[float], under_price: Optional[float], source: str) -> Optional[float]:
    """The market's over probability with the vig removed."""
    if over_price and under_price:
        if source == "kalshi":                     # YES ask and 1 - NO ask bracket the fair price
            return (over_price + 1 - under_price) / 2
        return over_price / (over_price + under_price)
    return over_price or (1 - under_price if under_price else None)


def p_over(league: str, stat: Stat, line: float, games: Sequence[Game], season: int) -> Optional[tuple[float, int]]:
    """(stat-only P(over), games used), or None with fewer than MIN_GAMES values."""
    pts = sample(games, stat, season)
    if len(pts) < MIN_GAMES:
        return None
    weights = [DECAY ** i for i in range(len(pts))]
    mean, var = _wstats([v for _, v in pts], weights)
    mins = [(espn.minutes(league, g), w) for (g, _), w in zip(pts, weights)]
    mins = [(m, w) for m, w in mins if m]
    recent = [m for m, _ in mins[:5]]
    if len(mins) >= MIN_GAMES and recent:
        base = sum(m * w for m, w in mins) / sum(w for _, w in mins)
        ratio = min(max((sum(recent) / len(recent)) / base, MINUTES_CLIP[0]), MINUTES_CLIP[1])
        mean, var = mean * ratio, var * ratio * ratio
    if stat.continuous:
        sd = max(math.sqrt(var), 0.1 * abs(mean), 1.0)
        p = _normal_over(line, mean, sd)
    else:
        p = _count_over(line, mean, var)
    return min(max(p, P_CLIP[0]), P_CLIP[1]), len(pts)


def evaluate(row: dict, league: str, stat: Stat, games: Sequence[Game], season: int) -> dict:
    """p_model (over), the better side's edge after fees, and model_version for one prop row."""
    out = {"p_model": None, "edge": None, "edge_side": None, "model_version": MODEL_VERSION}
    est = p_over(league, stat, row["line"], games, season)
    if not est:
        return out
    p_stat, n = est
    over, under = row.get("over_price"), row.get("under_price")
    p_mkt = market_prob(over, under, row["source"])
    w = W_MAX * n / (n + K)
    p = p_stat if p_mkt is None else w * p_stat + (1 - w) * p_mkt
    out["p_model"] = round(p, 4)
    fee = kalshi_fee if row["source"] == "kalshi" else (lambda price: 0.0)
    edges = {side: round(prob - price - fee(price), 4)
             for side, prob, price in (("over", p, over), ("under", 1 - p, under)) if price and 0 < price < 1}
    if edges:
        out["edge_side"] = max(edges, key=edges.get)
        out["edge"] = edges[out["edge_side"]]
    return out
