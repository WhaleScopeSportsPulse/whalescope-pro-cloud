"""Unit tests for the Signal Lab v2 decision model: python -m unittest test_signal_lab"""
import math
import random
import unittest
from unittest import mock

import signal_lab
import signal_lab_model as slm


def path(start=80000.0, n=80, step=5, drift=0.0, seed=1):
    rnd = random.Random(seed)
    pts, p = [], start
    for i in range(n):
        pts.append((1000 + i * step, p))
        p *= math.exp(drift + rnd.gauss(0, 2e-4))
    return pts


class FairValue(unittest.TestCase):
    def test_settlement_average_shrinks_remaining_variance(self):
        self.assertEqual(slm.effective_seconds(540), 500)
        self.assertEqual(slm.effective_seconds(60), 20)
        self.assertLess(slm.effective_seconds(30), 30)

    def test_fair_value(self):
        self.assertAlmostEqual(slm.fair_value(0, 80000, 3e-5, 540), 0.5)
        up = slm.fair_value(50, 80000, 3e-5, 540)
        # z = 50 / (3e-5 * 80000 * sqrt(500)) = 0.9317
        self.assertAlmostEqual(up, 0.5 * math.erfc(-0.93169 / math.sqrt(2)), places=4)
        self.assertAlmostEqual(slm.fair_value(-50, 80000, 3e-5, 540), 1 - up, places=9)
        self.assertGreater(slm.fair_value(50, 80000, 3e-5, 120), up)   # less time left: more certain
        self.assertIsNone(slm.fair_value(50, 80000, None, 540))

    def test_realized_vol_and_momentum(self):
        pts = path()
        now = pts[-1][0]
        vol = slm.realized_vol(pts, now)
        self.assertAlmostEqual(vol, 2e-4 / math.sqrt(5), delta=1.5e-5)   # per-second sd of 5 s returns
        self.assertIsNone(slm.realized_vol(pts[:3], now))
        self.assertEqual(slm.momentum(pts, now, 60), pts[-1][1] - slm.price_at(pts, now - 60))
        self.assertIsNone(slm.momentum(pts, now, 10_000))


class Trading(unittest.TestCase):
    def test_fee_and_pnl(self):
        self.assertAlmostEqual(slm.fee(0.55), 0.07 * 0.55 * 0.45)
        self.assertAlmostEqual(slm.pnl(True, 0.55), 0.45 - 0.017325)
        self.assertAlmostEqual(slm.pnl(False, 0.55), -0.55 - 0.017325)

    def test_rule_band(self):
        self.assertEqual(slm.rule_trade(80010, 80000, 0.55, 0.47), ("UP", 0.55))
        self.assertEqual(slm.rule_trade(79990, 80000, 0.45, 0.59), ("DOWN", 0.59))
        self.assertIsNone(slm.rule_trade(80010, 80000, 0.60, 0.41))
        self.assertIsNone(slm.rule_trade(80010, 80000, 0.49, 0.52))
        self.assertIsNone(slm.rule_trade(80010, 80000, None, 0.52))

    def test_signal_needs_fee_plus_three_cents(self):
        fee = slm.fee(0.55)
        self.assertIsNone(slm.signal(0.55 + fee + 0.03, 0.55, 0.47))           # exactly at the bar
        self.assertEqual(slm.signal(0.55 + fee + 0.031, 0.55, 0.47)[0], "UP")
        side, ask, edge = slm.signal(0.30, 0.31, 0.60)
        self.assertEqual((side, ask), ("DOWN", 0.60))
        self.assertAlmostEqual(edge, round(0.70 - 0.60 - slm.fee(0.60), 4))
        self.assertIsNone(slm.signal(None, 0.55, 0.47))


def synthetic(days=6, per_day=60, seed=7, market_noise=0.0):
    """Rounds whose outcome follows the fair value; the market is the fair value plus noise."""
    rnd = random.Random(seed)
    rows = []
    for d in range(days):
        for i in range(per_day):
            vol = rnd.uniform(1.5e-5, 4e-5)
            dist = rnd.gauss(0, 60)
            p = slm.fair_value(dist, 80000, vol, 540)
            pm = min(max(p + rnd.gauss(0, market_noise), 0.03), 0.97)
            rows.append({"day": f"2026-10-{10 + d:02d}", "up": int(rnd.random() < p), "btc": 80000 + dist,
                         "reference": 80000, "distance": dist, "seconds_left": 540, "realized_vol": vol,
                         "up_bid": round(pm - 0.01, 2), "up_ask": round(pm + 0.01, 2), "down_ask": round(1 - pm + 0.01, 2),
                         "mom_60": rnd.gauss(0, 10), "mom_180": rnd.gauss(0, 20), "volume": 1e6})
    return rows


class WalkForward(unittest.TestCase):
    def test_never_trains_on_the_test_day_or_later(self):
        rows = synthetic()
        seen = []
        real_fit = slm.Logistic.fit

        def spy(self, xs, ys, iters=25):
            seen.append(len(xs))
            return real_fit(self, xs, ys, iters)
        with mock.patch.object(slm.Logistic, "fit", spy):
            res = slm.walk_forward(rows, min_train_days=3, min_train_rows=100)
        self.assertEqual(seen, [180, 240, 300])        # days 4, 5, 6 trained on 3, 4, 5 earlier days
        self.assertEqual((res["test_days"], res["test_rounds"]), (3, 180))

    def test_logistic_recovers_a_known_relationship(self):
        rnd = random.Random(3)
        xs = [[rnd.gauss(0, 1), rnd.gauss(0, 1)] for _ in range(3000)]
        ys = [int(rnd.random() < 1 / (1 + math.exp(-(0.5 + 2 * a)))) for a, _ in xs]
        m = slm.Logistic(l2=0.01).fit(xs, ys)
        self.assertAlmostEqual(m.w[1], 2.0, delta=0.25)
        self.assertAlmostEqual(m.w[2], 0.0, delta=0.15)

    def test_noisy_market_scores_worse_than_fair_value(self):
        res = slm.walk_forward(synthetic(days=8, per_day=120, market_noise=0.15), min_train_days=3, min_train_rows=100)
        b = {k: v["brier"] for k, v in res["models"].items()}
        self.assertLess(b["p_fair"], b["p_market"])
        self.assertLess(b["p_lr"], b["p_market"])


class Snapshot(unittest.TestCase):
    MARKET = {"yes_ask_dollars": "0.5500", "no_ask_dollars": "0.4700", "yes_bid_dollars": "0.5300",
              "no_bid_dollars": "0.4500", "yes_ask_size_fp": "1200.00", "yes_bid_size_fp": "800.50", "volume_fp": "250000"}

    def test_snapshot_fields_and_paper_trades(self):
        hist = [[t, p] for t, p in path(n=80)]
        now = hist[-1][0]
        with mock.patch.dict(signal_lab.LR, {"model": None, "rows": 0}):
            d = signal_lab.decision_snapshot("T", now + 540, now, 540, 80030.0, 80000.0, self.MARKET, hist, False)
        self.assertEqual((d["seconds_left"], d["distance"], d["spread"]), (540, 30.0, 0.02))
        self.assertEqual((d["up_ask_size"], d["up_bid_size"], d["volume"]), (1200.0, 800.5, 250000.0))
        self.assertAlmostEqual(d["p_market"], 0.54)
        self.assertIsNotNone(d["realized_vol"])
        self.assertIsNotNone(d["p_fair"])
        self.assertEqual((d["rule_side"], d["rule_price"]), ("UP", 0.55))
        self.assertIsNone(d["p_lr"])
        self.assertNotIn("model_side", d)                       # no model signal without a trained model
        with mock.patch.dict(signal_lab.LR, {"model": None, "rows": 0}):
            est = signal_lab.decision_snapshot("T", now + 540, now, 540, 80030.0, 80000.0, self.MARKET, hist, True)
        self.assertNotIn("rule_side", est)                      # no paper trades on an estimated reference


class Reference(unittest.TestCase):
    OPEN = 1_800_000_000

    def test_own_reference_averages_the_minute_before_open(self):
        samples = [(self.OPEN - 65, 1.0)] + [(self.OPEN - 60 + 5 * i, 80000.0 + i) for i in range(12)] + [(self.OPEN, 9.0)]
        self.assertAlmostEqual(signal_lab.own_reference(samples, self.OPEN), 80005.5)

    def test_own_reference_needs_samples_across_the_window(self):
        self.assertIsNone(signal_lab.own_reference([(self.OPEN - 20 + i, 80000.0) for i in range(20)], self.OPEN))
        self.assertIsNone(signal_lab.own_reference([(self.OPEN - 60 + 10 * i, 80000.0) for i in range(5)], self.OPEN))
        self.assertIsNone(signal_lab.own_reference([(self.OPEN - 60 + 5 * i, 0.0) for i in range(12)], self.OPEN))

    def test_kalshi_strike_ignores_zero_and_missing(self):
        self.assertEqual(signal_lab.kalshi_strike({"floor_strike": 77679.24}), 77679.24)
        self.assertIsNone(signal_lab.kalshi_strike({"floor_strike": 0}))
        self.assertIsNone(signal_lab.kalshi_strike({"floor_strike": "0.0"}))
        self.assertIsNone(signal_lab.kalshi_strike({}))

    def test_kalshi_strike_wins_when_present(self):
        self.assertEqual(signal_lab.choose_reference(80000.0, 80050.0, None, 80010.0, True), (80000.0, "kalshi", None))

    def test_kalshi_strike_far_from_price_is_skipped(self):
        ref, src, why = signal_lab.choose_reference(70000.0, None, None, 80000.0, True)
        self.assertIsNone(ref)
        self.assertIn("from price", why)

    def test_fallback_is_provisional_before_the_lock(self):
        self.assertEqual(signal_lab.choose_reference(None, 80000.0, 79900.0, 80010.0, False), (80000.0, "own", None))

    def test_fallback_checked_against_last_floor_strike_at_the_lock(self):
        self.assertEqual(signal_lab.choose_reference(None, 80003.0, 80000.0, 80010.0, True), (80003.0, "own", None))
        ref, _, why = signal_lab.choose_reference(None, 80030.0, 80000.0, 80010.0, True)
        self.assertIsNone(ref)
        self.assertIn("last floor_strike", why)
        self.assertIsNone(signal_lab.choose_reference(None, 80030.0, None, 80010.0, True)[0])

    def test_no_reference_at_all(self):
        self.assertIsNone(signal_lab.choose_reference(None, None, 80000.0, 80010.0, False)[0])


if __name__ == "__main__":
    unittest.main()
