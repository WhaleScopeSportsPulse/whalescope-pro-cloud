"""Static integrity checks for Signal Lab's research branch.

Run: python signal_lab_integrity_test.py
These tests do not require a running server or external network.
"""
from pathlib import Path
import ast

source = Path(__file__).with_name("signal_lab.py").read_text(encoding="utf-8")
ast.parse(source)
assert "UNVERIFIED_KALSHI_TARGET" in source, "Missing explicit unverified target guard"
assert 'target=float(bars[0][5])' not in source, "Kraken VWAP proxy still assigned as Kalshi strike"
assert 'm["estimated_reference"]=True' not in source, "Proxy marker still active"
assert "result not in (\"yes\",\"no\")" in source, "Settlement validation unexpectedly removed"
assert "ON CONFLICT(ticker) DO NOTHING" in source, "Immutable per-round tracking unexpectedly removed"
print("PASS: syntax, no Kraken proxy strike, settlement guard and immutable round tracking")
