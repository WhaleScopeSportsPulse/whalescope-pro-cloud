# Signal Lab — Research and Validation Track

This branch is isolated from the production main branch. Do not deploy to the live Signal Lab services until checks pass.

## Workstream 1: Market integrity
- [ ] Capture canonical Kalshi ticker, opening target/strike and settlement source.
- [ ] Mark estimated Kraken reference as PROXY, never verified.
- [ ] Block proxy-target rounds from official accuracy and model training.
- [ ] Compare market timestamps and displayed countdown to Kalshi.

## Workstream 2: Dataset
- [ ] Persist time-stamped features, price observations, market bid/ask and depth at minute 2.
- [ ] Preserve every locked prediction, model version and subsequent Kalshi result.
- [ ] Check for missing intervals, duplicate rounds, future-data leakage, and survivorship bias.

## Workstream 3: Challenger model
- [ ] Implement a separately versioned baseline and challenger.
- [ ] Use chronological walk-forward evaluation with untouched holdout periods.
- [ ] Measure accuracy, calibration, sample sizes, confidence intervals, and drift.
- [ ] Promote only on reproducible improvement; do not tune on holdout data.

## Workstream 4: Trading feasibility
- [ ] Measure entry asks, executable exit bids, fees, spread and order-book depth.
- [ ] Evaluate simulated net P&L and drawdown, not just win rate.
- [ ] Shadow-test before any live model switch.

## Initial code audit — 2026-10-19
- signal_lab.py uses 5-minute Kraken OHLC, RSI, EMA, ATR and nearest historical patterns.
- historical_analysis currently uses up to 160 completed five-minute candles.
- adaptive_weights adjusts a small fixed grid of weights, not a separately trained ML model.
- Missing Kalshi strike can fall back to a Kraken minute VWAP marked estimated_reference.
- paper_backtest is explicitly a 5-minute candle momentum proxy, not a Kalshi execution backtest.
- Render signal-lab-pro (btc-signal-lab-oxmel) has autoDeploy off; signal-lab-live has autoDeploy on.
- Never imply 90% accuracy is achievable or guaranteed.

## Acceptance criteria
At least 300 chronologically collected, verified settled rounds for a preliminary comparison; expand to 500+ for more confidence. A challenger must outperform baseline on untouched out-of-sample data and show realistic net results after fees. Record both wins and losses without resetting the underlying audit history.
