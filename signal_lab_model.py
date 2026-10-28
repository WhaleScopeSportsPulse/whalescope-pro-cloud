"""Signal Lab v2 decision model (paper trading only; never places orders). Stdlib only.

At 9:00 left in a 15-minute round:
  fair value  P(up) = Phi(distance / (sigma * spot * sqrt(T - 40)))
              sigma = realized per-second volatility of log price over the last 5 minutes.
              Kalshi settles on the 60-second average of the index, so the variance still
              to come is sigma^2 * (T - 60 + 60/3) = sigma^2 * (T - 40) for T >= 60 s left.
  logistic    regression on the fair-value z-score, the market's log-odds, spread,
              volatility-scaled momentum (60 s, 180 s) and log volume, fit with L2 penalty.
  evaluation  walk-forward only: each test day is predicted by a model trained on earlier
              days. Compared with Kalshi's own price (bid/ask midpoint) by Brier score,
              log loss and calibration bins.
  signal      only when model probability - ask beats the fee plus MIN_EDGE (3 cents).
"""
from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

FEE_RATE = 0.07
DECISION_LEFT = 540            # seconds left at the decision snapshot (9:00)
SETTLE_AVG = 60                # Kalshi settles on a 60-second average
VOL_WINDOW = 300
MIN_EDGE = 0.03
RULE_BAND = (0.50, 0.59)       # paper rule: buy the leader when its ask is 50-59 cents
FEATURES = ("z_fair", "market_logit", "spread", "mom60_z", "mom180_z", "log_volume")


def fee(price: float) -> float:
    """Kalshi taker fee per contract, unrounded: 0.07 * P * (1 - P)."""
    return FEE_RATE * price * (1 - price)


def pnl(side_won: bool, price: float) -> float:
    """Profit per contract bought at `price` after the fee."""
    return (1.0 if side_won else 0.0) - price - fee(price)


def realized_vol(points: Sequence[tuple[float, float]], now: float, window: float = VOL_WINDOW) -> Optional[float]:
    """Per-second volatility of log price from (ts, price) points in the last `window` seconds."""
    pts = sorted((t, p) for t, p in points if now - window <= t <= now and p > 0)
    sq, span, n = 0.0, 0.0, 0
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        if t1 > t0:
            sq += math.log(p1 / p0) ** 2
            span += t1 - t0
            n += 1
    return math.sqrt(sq / span) if n >= 4 and span > 0 else None


def price_at(points: Sequence[tuple[float, float]], ts: float) -> Optional[float]:
    """Last price at or before ts."""
    before = [p for t, p in sorted(points) if t <= ts]
    return before[-1] if before else None


def momentum(points: Sequence[tuple[float, float]], now: float, seconds: float) -> Optional[float]:
    a, b = price_at(points, now), price_at(points, now - seconds)
    return None if a is None or b is None else a - b


def _phi(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def effective_seconds(seconds_left: float) -> float:
    """Seconds of variance left when settlement averages the final SETTLE_AVG seconds."""
    if seconds_left >= SETTLE_AVG:
        return seconds_left - SETTLE_AVG * 2 / 3
    return max(seconds_left ** 3 / (3 * SETTLE_AVG ** 2), 1e-9)


def z_fair(distance: float, spot: float, vol: Optional[float], seconds_left: float) -> Optional[float]:
    if not vol or vol <= 0 or spot <= 0:
        return None
    return max(-8.0, min(8.0, distance / (vol * spot * math.sqrt(effective_seconds(seconds_left)))))


def fair_value(distance: float, spot: float, vol: Optional[float], seconds_left: float) -> Optional[float]:
    z = z_fair(distance, spot, vol, seconds_left)
    return None if z is None else _phi(z)


def market_prob(up_bid: Optional[float], up_ask: Optional[float]) -> Optional[float]:
    if up_bid is None or up_ask is None:
        return None
    return min(max((up_bid + up_ask) / 2, 0.01), 0.99)


def features(row: dict) -> Optional[list[float]]:
    """The model's inputs for one snapshot, or None when any is missing."""
    vol, spot, left = row.get("realized_vol"), row.get("btc"), row.get("seconds_left")
    z = z_fair(row["distance"], spot, vol, left) if row.get("distance") is not None and spot else None
    pm = market_prob(row.get("up_bid"), row.get("up_ask"))
    if z is None or pm is None or row.get("mom_60") is None or row.get("mom_180") is None:
        return None
    scale = vol * spot
    return [z, math.log(pm / (1 - pm)), row["up_ask"] - row["up_bid"],
            max(-8.0, min(8.0, row["mom_60"] / (scale * math.sqrt(60)))),
            max(-8.0, min(8.0, row["mom_180"] / (scale * math.sqrt(180)))),
            math.log1p(max(row.get("volume") or 0.0, 0.0)) / 10]


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        if abs(m[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] if abs(m[i][i]) > 1e-12 else 0.0 for i in range(n)]


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x)) if x >= 0 else math.exp(x) / (1 + math.exp(x))


class Logistic:
    """L2-penalized logistic regression on standardized features, fit by Newton's method."""

    def __init__(self, l2: float = 1.0):
        self.l2, self.w, self.mu, self.sd = l2, None, None, None

    def fit(self, xs: Sequence[Sequence[float]], ys: Sequence[int], iters: int = 25) -> "Logistic":
        k = len(xs[0])
        self.mu = [sum(x[j] for x in xs) / len(xs) for j in range(k)]
        self.sd = [math.sqrt(sum((x[j] - self.mu[j]) ** 2 for x in xs) / len(xs)) or 1.0 for j in range(k)]
        zs = [[1.0] + [(x[j] - self.mu[j]) / self.sd[j] for j in range(k)] for x in xs]
        w = [0.0] * (k + 1)
        for _ in range(iters):
            grad = [self.l2 * w[j] if j else 0.0 for j in range(k + 1)]
            hess = [[(self.l2 if i == j and i else 0.0) for j in range(k + 1)] for i in range(k + 1)]
            for z, y in zip(zs, ys):
                p = _sigmoid(sum(a * b for a, b in zip(w, z)))
                for i in range(k + 1):
                    grad[i] += (p - y) * z[i]
                    for j in range(k + 1):
                        hess[i][j] += p * (1 - p) * z[i] * z[j]
            step = _solve(hess, grad)
            w = [a - b for a, b in zip(w, step)]
            if max(abs(s) for s in step) < 1e-8:
                break
        self.w = w
        return self

    def predict(self, x: Sequence[float]) -> float:
        z = [1.0] + [(x[j] - self.mu[j]) / self.sd[j] for j in range(len(x))]
        return min(max(_sigmoid(sum(a * b for a, b in zip(self.w, z))), 0.001), 0.999)


def signal(p_up: Optional[float], up_ask: Optional[float], down_ask: Optional[float]) -> Optional[tuple[str, float, float]]:
    """(side, ask, edge) when model probability - ask beats the fee + MIN_EDGE on a side."""
    if p_up is None:
        return None
    best = None
    for side, p, ask in (("UP", p_up, up_ask), ("DOWN", 1 - p_up, down_ask)):
        if ask is None or not 0 < ask < 1:
            continue
        edge = round(p - ask - fee(ask), 6)  # round so exactly fee + 3 cents doesn't pass on float noise
        if edge > MIN_EDGE and (best is None or edge > best[2]):
            best = (side, ask, round(edge, 4))
    return best


def rule_trade(btc: float, reference: float, up_ask: Optional[float], down_ask: Optional[float]) -> Optional[tuple[str, float]]:
    """The paper rule: buy the leading side at its ask when that ask is 50-59 cents."""
    side, ask = ("UP", up_ask) if btc >= reference else ("DOWN", down_ask)
    if ask is None or not RULE_BAND[0] <= round(ask, 4) <= RULE_BAND[1]:
        return None
    return side, ask


# --- evaluation -------------------------------------------------------------

def brier(ps: Sequence[float], ys: Sequence[int]) -> Optional[float]:
    return round(sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ps), 4) if ps else None


def log_loss(ps: Sequence[float], ys: Sequence[int]) -> Optional[float]:
    if not ps:
        return None
    eps = 1e-6
    return round(-sum(y * math.log(max(p, eps)) + (1 - y) * math.log(max(1 - p, eps)) for p, y in zip(ps, ys)) / len(ps), 4)


def calibration(ps: Sequence[float], ys: Sequence[int], bins: int = 10) -> list[dict]:
    out = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, p in enumerate(ps) if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if idx:
            out.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(idx), "avg_p": round(sum(ps[i] for i in idx) / len(idx), 3),
                        "freq_up": round(sum(ys[i] for i in idx) / len(idx), 3)})
    return out


def trade_summary(pnls: Sequence[float]) -> dict:
    n = len(pnls)
    if not n:
        return {"trades": 0, "total": 0.0, "avg": None, "ci95": None, "win_rate": None}
    avg = sum(pnls) / n
    sd = math.sqrt(sum((x - avg) ** 2 for x in pnls) / (n - 1)) if n > 1 else 0.0
    half = 1.96 * sd / math.sqrt(n) if n > 1 else None
    return {"trades": n, "total": round(sum(pnls), 2), "avg": round(avg, 4),
            "ci95": [round(avg - half, 4), round(avg + half, 4)] if half is not None else None,
            "win_rate": round(sum(x > 0 for x in pnls) / n, 3)}


def walk_forward(rows: Sequence[dict], min_train_days: int = 3, min_train_rows: int = 200) -> dict:
    """Train on all days before each test day, predict that day; never look ahead.

    Each row needs: day (sortable), up (0/1 outcome), btc, reference, distance, seconds_left,
    realized_vol, up_bid, up_ask, down_ask, mom_60, mom_180, volume."""
    data = [(r, x) for r in rows if (x := features(r)) is not None]
    days = sorted({r["day"] for r, _ in data})
    preds = []
    for i, day in enumerate(days):
        train = [(r, x) for r, x in data if r["day"] < day]
        if i < min_train_days or len(train) < min_train_rows:
            continue
        model = Logistic().fit([x for _, x in train], [r["up"] for r, _ in train])
        for r, x in data:
            if r["day"] == day:
                preds.append({"day": day, "up": r["up"], "p_market": market_prob(r["up_bid"], r["up_ask"]),
                              "p_fair": fair_value(r["distance"], r["btc"], r["realized_vol"], r["seconds_left"]),
                              "p_lr": model.predict(x), "row": r})
    ys = [p["up"] for p in preds]
    out = {"days": len(days), "test_days": len({p["day"] for p in preds}), "test_rounds": len(preds),
           "train_rows_total": len(data), "models": {}, "trades": {}}
    for name in ("p_market", "p_fair", "p_lr"):
        ps = [p[name] for p in preds]
        out["models"][name] = {"brier": brier(ps, ys), "log_loss": log_loss(ps, ys), "calibration": calibration(ps, ys)}
    for name in ("p_fair", "p_lr"):
        trades = []
        for p in preds:
            s = signal(p[name], p["row"]["up_ask"], p["row"]["down_ask"])
            if s:
                trades.append(pnl((s[0] == "UP") == bool(p["up"]), s[1]))
        out["trades"][name + "_signal"] = trade_summary(trades)
    rule = []
    for p in preds:
        t = rule_trade(p["row"]["btc"], p["row"]["reference"], p["row"]["up_ask"], p["row"]["down_ask"])
        if t:
            rule.append(pnl((t[0] == "UP") == bool(p["up"]), t[1]))
    out["trades"]["leader_50_59_rule"] = trade_summary(rule)
    by_day = {}
    for p in preds:
        d = by_day.setdefault(p["day"], {"n": 0, "market": [], "lr": [], "y": []})
        d["n"] += 1
        d["market"].append(p["p_market"])
        d["lr"].append(p["p_lr"])
        d["y"].append(p["up"])
    out["per_day"] = [{"day": d, "n": v["n"], "brier_market": brier(v["market"], v["y"]), "brier_lr": brier(v["lr"], v["y"])}
                      for d, v in sorted(by_day.items())]
    return out
