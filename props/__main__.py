"""CLI and HTTP server.

    python -m props serve                      # JSON API + web table on $PORT (default 8080)
    python -m props slate --league nba --window l10 --min-rate 0.7
    python -m props player --league nba --name "Zach LaVine" --stat points --line 24.5 --opponent SA
    python -m props load                       # one refresh, written to props.props_board ($DATABASE_URL)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import espn, service
from .props import Prop
from .stats import LEAGUES, STATS, resolve

PAGE = os.path.join(os.path.dirname(__file__), "static", "index.html")


def player_report(league: str, name: str, stat: str, line: float, team=None, opponent=None) -> dict:
    s = resolve(league, stat)
    if not s:
        raise ValueError(f"unsupported stat {stat!r} for {league}")
    table = espn.teams(league)
    t = espn.match_team(league, team, table) if team else None
    o = espn.match_team(league, opponent, table) if opponent else None
    p = Prop(source="manual", league=league, stat=s.key, player=name, line=line,
             team=t and t["abbr"], team_id=t and t["id"], opponent=o and o["abbr"], opponent_id=o and o["id"])
    return service.enrich([p])[0]


class Handler(BaseHTTPRequestHandler):
    snapshot: service.Snapshot

    def _send(self, code: int, body, ctype="application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        u = urllib.parse.urlparse(self.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                with open(PAGE, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if u.path == "/health":
                s = self.snapshot
                return self._send(200, {"ok": s.error is None, "props": len(s.rows),
                                        "updated": s.updated, "error": s.error})
            if u.path == "/api/props":
                s = self.snapshot
                rows = s.query(league=q.get("league"), source=q.get("source"), stat=q.get("stat"),
                               player=q.get("player"), window=q.get("window", "l10"),
                               min_rate=float(q["min_rate"]) if q.get("min_rate") else None,
                               min_games=int(q.get("min_games", 0)), side=q.get("side", "over"),
                               limit=int(q.get("limit", 500)))
                return self._send(200, {"updated": s.updated, "refreshing_error": s.error,
                                        "leagues": sorted({r["league"] for r in s.rows}),
                                        "count": len(rows), "props": rows})
            if u.path == "/api/player":
                return self._send(200, player_report(q["league"].lower(), q["name"], q["stat"], float(q["line"]),
                                                     q.get("team"), q.get("opponent")))
            if u.path == "/api/stats":
                return self._send(200, [{"league": l, "key": k, "label": s.label} for (l, k), s in STATS.items()])
            self._send(404, {"error": "not found"})
        except (KeyError, ValueError) as e:
            self._send(400, {"error": f"bad request: {e}"})
        except Exception as e:
            logging.exception("request failed")
            self._send(502, {"error": str(e)})

    def log_message(self, fmt, *args) -> None:
        logging.info("%s - %s", self.address_string(), fmt % args)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="python -m props")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    sv.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))

    sl = sub.add_parser("slate", help="print the current slate with hit rates")
    sl.add_argument("--source", choices=service.SOURCES, action="append")
    sl.add_argument("--league", action="append", help="nba, nfl, ... or any Underdog sport (cfb, fifa, tennis)")
    sl.add_argument("--stat")
    sl.add_argument("--window", default="l10", choices=service.WINDOW_KEYS)
    sl.add_argument("--side", default="over", choices=["over", "under"])
    sl.add_argument("--min-rate", type=float)
    sl.add_argument("--min-games", type=int, default=5)
    sl.add_argument("--limit", type=int, default=50)
    sl.add_argument("--json", action="store_true")

    pl = sub.add_parser("player", help="hit rates for one player/stat/line")
    pl.add_argument("--league", required=True, choices=sorted(LEAGUES))
    pl.add_argument("--name", required=True)
    pl.add_argument("--stat", required=True)
    pl.add_argument("--line", required=True, type=float)
    pl.add_argument("--team")
    pl.add_argument("--opponent")

    ld = sub.add_parser("load", help="refresh once and replace props.props_board in $DATABASE_URL")
    ld.add_argument("--league", action="append")
    ld.add_argument("--from-json", help="load rows saved by `slate --json` instead of fetching")

    a = ap.parse_args(argv)
    if a.cmd == "serve":
        snap = service.Snapshot()
        snap.start()
        Handler.snapshot = snap
        logging.info("props service on http://%s:%d", a.host, a.port)
        ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
    elif a.cmd == "slate":
        rows = service.enrich(service.collect(a.source or service.SOURCES, a.league))
        rows = service.filter_rows(rows, stat=a.stat, window=a.window, min_rate=a.min_rate,
                                   min_games=a.min_games, side=a.side, limit=a.limit)
        if a.json:
            print(json.dumps(rows, indent=1))
        else:
            print_table(rows, a.side)
    elif a.cmd == "player":
        print(json.dumps(player_report(a.league, a.name, a.stat, a.line, a.team, a.opponent), indent=1))
    elif a.cmd == "load":
        from . import db
        if not os.environ.get("DATABASE_URL"):
            print("DATABASE_URL is not set", file=sys.stderr)
            return 2
        if a.from_json:
            rows, ok = json.load(open(a.from_json)), service.SOURCES
        else:
            props, ok, errors = service.collect_sources(leagues=a.league)
            for src, err in errors.items():
                print(f"skipped {src}: {err}", file=sys.stderr)
            if not ok:
                return 1
            rows = service.enrich(props)
        n = db.save(rows, ok, a.league)
        print(f"saved {n} props to props.props_board ({', '.join(ok)})")
    return 0


def _fmt(w, side="over") -> str:
    if not w or w["rate"] is None:
        return "   -   "
    r = w["rate"] if side == "over" else 1 - w["rate"]
    return f"{r * 100:3.0f}% {w['games']:>2}"


def print_table(rows: list[dict], side: str = "over") -> None:
    hdr = f"{'SRC':8} {'LG':4} {'PLAYER':24} {'TM':4} {'OPP':4} {'STAT':20} {'LINE':>6} {'PRICE':>5}  " \
          f"{'L5':7} {'L10':7} {'L20':7} {'SEASON':7} {'H2H':7}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        price = r["over_price"] if side == "over" else r["under_price"]
        cells = "  ".join(_fmt(r.get(k), side) for k in service.WINDOW_KEYS)
        print(f"{r['source']:8} {r['league']:4} {r['player'][:24]:24} {r['team'] or '':4} {r['opponent'] or '':4} "
              f"{r['stat_label'][:20]:20} {r['line']:>6} {('%.2f' % price) if price else '':>5}  "
              f"{r['error'] or cells}")
    print(f"\n{len(rows)} props · {side} hit rate % and games in window · {time.strftime('%Y-%m-%d %H:%M')}")


if __name__ == "__main__":
    sys.exit(main())
