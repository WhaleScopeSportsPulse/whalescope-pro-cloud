"""Walk-forward backtest of the Signal Lab v2 decision model on settled Kalshi rounds.

    python signal_lab_backtest.py --days 21 [--json out.json] [--write-db]

Rebuilds the 9:00-left snapshot for every settled KXBTC15M round in the window:
  - Kalshi: the 1-minute candle ending at 9:00 left (bid/ask close, cumulative volume),
    the round's reference (floor_strike) and result. Order-book depth has no history,
    so it only exists in live snapshots.
  - BTC: Coinbase 1-minute candles. Volatility uses the five 1-minute returns before the
    decision (live snapshots use 5-second data), momentum the 60 s and 180 s change.
Then runs signal_lab_model.walk_forward: every day is predicted by a model trained only
on earlier days. --write-db stores the rebuilt rows in signal_lab_decisions_v2 with
source='backfill' so the live model can train on them.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import signal_lab_model as slm

KALSHI = "https://external-api.kalshi.com/trade-api/v2/"
COINBASE = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
KALSHI_GAP = 0.35  # seconds between Kalshi requests


def get(url: str, tries: int = 8) -> dict:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "SignalLab/backtest"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
            time.sleep(min(2 ** i, 30))
        except (urllib.error.URLError, TimeoutError):
            time.sleep(min(2 ** i, 30))
    raise RuntimeError(f"gave up on {url}")


def settled_rounds(since: int) -> list[dict]:
    out, cursor = [], ""
    while True:
        d = get(KALSHI + "markets?series_ticker=KXBTC15M&status=settled&limit=1000"
                + ("&cursor=" + urllib.parse.quote(cursor) if cursor else ""))
        ms = d.get("markets") or []
        for m in ms:
            close = int(dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp())
            if close >= since and m.get("result") in ("yes", "no") and m.get("floor_strike") is not None:
                out.append({"ticker": m["ticker"], "close_ts": close, "reference": float(m["floor_strike"]),
                            "up": int(m["result"] == "yes")})
        cursor = d.get("cursor") or ""
        oldest = min((m["close_time"] for m in ms), default="")
        if not cursor or not ms or oldest < dt.datetime.fromtimestamp(since, dt.timezone.utc).isoformat():
            return sorted(out, key=lambda r: r["close_ts"])
        time.sleep(KALSHI_GAP)


def btc_minutes(start: int, end: int) -> dict[int, float]:
    """Coinbase 1-minute closes keyed by the minute's END timestamp."""
    out = {}
    t = start
    while t < end:
        hi = min(t + 300 * 60, end)
        q = urllib.parse.urlencode({"granularity": 60, "start": dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(),
                                    "end": dt.datetime.fromtimestamp(hi, dt.timezone.utc).isoformat()})
        for c in get(f"{COINBASE}?{q}"):
            out[int(c[0]) + 60] = float(c[4])
        t = hi
        time.sleep(0.15)
    return out


def candles_for(ticker: str, close_ts: int, cache: str | None) -> list[dict]:
    """Kalshi 1-minute candles up to the decision minute; cached per round so reruns resume."""
    path = os.path.join(cache, ticker + ".json") if cache else None
    if path and os.path.exists(path):
        return json.load(open(path))
    q = urllib.parse.urlencode({"start_ts": close_ts - 900, "end_ts": close_ts - slm.DECISION_LEFT, "period_interval": 1})
    out = get(f"{KALSHI}series/KXBTC15M/markets/{ticker}/candlesticks?{q}").get("candlesticks") or []
    if path:
        json.dump(out, open(path, "w"))
    time.sleep(KALSHI_GAP)
    return out


def snapshot(r: dict, btc: dict[int, float], cache: str | None = None) -> dict | None:
    decision = r["close_ts"] - slm.DECISION_LEFT
    candles = candles_for(r["ticker"], r["close_ts"], cache)
    at = [c for c in candles if c.get("end_period_ts") == decision]
    if not at:
        return None
    c = at[0]

    def px(side):
        v = (c.get(side) or {}).get("close_dollars")
        return float(v) if v not in (None, "") else None
    up_bid, up_ask = px("yes_bid"), px("yes_ask")
    points = [(t, btc[t]) for t in range(decision - slm.VOL_WINDOW, decision + 1, 60) if t in btc]
    spot = btc.get(decision)
    if up_bid is None or up_ask is None or spot is None or not 0 < up_ask < 1 or up_bid <= 0:
        return None
    volume = sum(float(x.get("volume_fp") or 0) for x in candles if x.get("end_period_ts", 0) <= decision)
    return {"ticker": r["ticker"], "source": "backfill", "close_ts": r["close_ts"], "decided_at": decision,
            "day": dt.datetime.fromtimestamp(r["close_ts"], dt.timezone.utc).date().isoformat(),
            "seconds_left": slm.DECISION_LEFT, "btc": spot, "reference": r["reference"],
            "distance": spot - r["reference"], "up_bid": up_bid, "up_ask": up_ask,
            "down_bid": round(1 - up_ask, 4), "down_ask": round(1 - up_bid, 4), "spread": round(up_ask - up_bid, 4),
            "volume": volume, "realized_vol": slm.realized_vol(points, decision),
            "mom_60": slm.momentum(points, decision, 60), "mom_180": slm.momentum(points, decision, 180),
            "up": r["up"], "result": "yes" if r["up"] else "no"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--json", help="write the dataset and results here")
    ap.add_argument("--write-db", action="store_true", help="store rows in signal_lab_decisions_v2 (source=backfill)")
    ap.add_argument("--min-train-days", type=int, default=3)
    ap.add_argument("--cache", help="directory for per-round Kalshi candles (lets a rerun resume)")
    a = ap.parse_args(argv)
    since = int(time.time()) - a.days * 86400
    rounds = settled_rounds(since)
    print(f"{len(rounds)} settled rounds since {dt.datetime.fromtimestamp(since, dt.timezone.utc):%Y-%m-%d %H:%M}Z",
          file=sys.stderr)
    btc = btc_minutes(since - 900, int(time.time()))
    if a.cache:
        os.makedirs(a.cache, exist_ok=True)
    rows = []
    for i, r in enumerate(rounds):
        s = snapshot(r, btc, a.cache)
        if s:
            rows.append(s)
        if i % 100 == 0:
            print(f"  {i}/{len(rounds)} rounds, {len(rows)} snapshots", file=sys.stderr)
    res = slm.walk_forward(rows, min_train_days=a.min_train_days)
    rule_all = [slm.pnl((t[0] == "UP") == bool(r["up"]), t[1]) for r in rows
                if (t := slm.rule_trade(r["btc"], r["reference"], r["up_ask"], r["down_ask"]))]
    res["leader_50_59_rule_all_days"] = slm.trade_summary(rule_all)
    res["snapshots"] = len(rows)
    print(json.dumps({k: v for k, v in res.items()}, indent=1))
    if a.json:
        json.dump({"rows": rows, "results": res}, open(a.json, "w"))
    if a.write_db:
        import signal_lab
        signal_lab.setup()
        print("stored", signal_lab.store_decisions(rows), "backfill rows", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
