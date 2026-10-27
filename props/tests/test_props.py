import unittest
from unittest import mock

import datetime as dt

from props import espn, kalshi, model, picks, service, underdog
from props.espn import Game, parse_gamelog
from props.hitrate import hit_rates, window
from props.props import Prop
from props.stats import get, resolve

NBA = {a: {"id": str(i), "abbr": a, "name": a} for i, a in enumerate(
    ["ATL", "BOS", "BKN", "DET", "GS", "LAC", "LAL", "NO", "NY", "SA", "SAC", "UTAH", "WSH"], 1)}


def game(i, pts, opp="BOS", opp_id="2", season=2027):
    return Game(event_id=str(i), date=f"2026-10-{30 - i:02d}", season=season, postseason=False,
                team_id="9", opponent_id=opp_id, opponent=opp, home=True,
                row={"points": str(pts), "totalRebounds": "5", "assists": "3", "minutes": "30",
                     "threePointFieldGoalsMade-threePointFieldGoalsAttempted": "2-6"})


class HitRates(unittest.TestCase):
    def test_window_counts_pushes_separately(self):
        w = window([10, 20, 20, 30], 20)
        self.assertEqual((w["over"], w["under"], w["push"], w["rate"]), (1, 1, 2, 0.5))
        self.assertIsNone(window([], 5.5)["rate"])

    def test_windows_season_and_h2h(self):
        games = [game(i, 30 if i % 2 else 10, season=2027 if i < 3 else 2026,
                      opp="NY" if i in (1, 4) else "BOS", opp_id="9" if i in (1, 4) else "2") for i in range(25)]
        hr = hit_rates(games, get("nba", "points"), 24.5, 2027, opponent_id="9")
        self.assertEqual(hr["l5"]["games"], 5)
        self.assertEqual(hr["l5"]["over"], 2)          # games 1 and 3
        self.assertEqual(hr["l20"]["games"], 20)
        self.assertEqual(hr["season"]["games"], 3)
        self.assertEqual((hr["h2h"]["games"], hr["h2h"]["over"]), (2, 1))
        self.assertEqual(len(hr["recent"]), 10)

    def test_combo_and_made_stats(self):
        row = game(0, 25).row
        self.assertEqual(get("nba", "pra").value(row), 33)
        self.assertEqual(get("nba", "threes").value(row), 2)
        self.assertEqual(get("mlb", "pitching_outs").value({"innings": "6.2"}), 20)
        self.assertEqual(get("mlb", "total_bases").value(
            {"hits": "3", "doubles": "1", "triples": "0", "homeRuns": "1"}), 7)

    def test_resolve_aliases(self):
        self.assertEqual(resolve("nba", "Pts + Rebs + Asts").key, "pra")
        self.assertEqual(resolve("mlb", "Strikeouts").key, "pitcher_ks")
        self.assertEqual(resolve("nhl", "Shots on Goal").key, "shots")
        self.assertIsNone(resolve("nba", "Fantasy Points"))


class Matching(unittest.TestCase):
    def test_team_codes(self):
        for code, want in [("NYK", "NY"), ("SAS", "SA"), ("GSW", "GS"), ("NOP", "NO"), ("UTA", "UTAH"),
                           ("WAS", "WSH"), ("LAL", "LAL"), ("LAC", "LAC")]:
            self.assertEqual(espn.match_team("nba", code, NBA)["abbr"], want, code)
        self.assertIsNone(espn.match_team("nba", "XYZ", NBA))

    def test_player_names(self):
        cands = [{"id": "1", "name": "De'Andre Hunter"}, {"id": "2", "name": "Jaren Jackson Jr."},
                 {"id": "3", "name": "Nikola Jokić"}, {"id": "4", "name": "Jalen Williams"},
                 {"id": "5", "name": "Jaylin Williams"}]
        self.assertEqual(espn.pick_player("DeAndre Hunter", cands)["id"], "1")
        self.assertEqual(espn.pick_player("Jaren Jackson", cands)["id"], "2")
        self.assertEqual(espn.pick_player("Nikola Jokic", cands)["id"], "3")
        self.assertIsNone(espn.pick_player("J. Williams", cands))


class Kalshi(unittest.TestCase):
    MARKET = {
        "ticker": "KXNBAPTS-26OCT27DETNYK-NYKMMCBRIDE2-15", "event_ticker": "KXNBAPTS-26OCT27DETNYK",
        "status": "active", "yes_sub_title": "Miles McBride: 15+", "floor_strike": 14.5, "strike_type": "greater",
        "yes_ask_dollars": "0.1200", "no_ask_dollars": "0.9800", "occurrence_datetime": "2026-10-28T03:00:00Z",
    }

    def test_parse_market(self):
        p = kalshi.parse_market(self.MARKET, "nba", "points", NBA)
        self.assertEqual((p.player, p.line, p.team, p.opponent), ("Miles McBride", 14.5, "NY", "DET"))
        self.assertEqual((p.over_price, p.under_price), (0.12, 0.98))

    def test_parse_without_floor_strike(self):
        m = dict(self.MARKET, floor_strike=None, strike_type=None)
        self.assertEqual(kalshi.parse_market(m, "nba", "points", NBA).line, 14.5)

    def test_start_time_from_espn(self):
        p = kalshi.parse_market(self.MARKET, "nba", "points", NBA)   # occurrence 03:00Z
        tip = dt.datetime(2026, 10, 28, 0, 0, tzinfo=dt.timezone.utc)
        with mock.patch.object(espn, "find_event", return_value={"start": tip}) as fe:
            self.assertEqual(kalshi.game_start(p), "2026-10-28T00:00:00+00:00")
        self.assertEqual(fe.call_args[0][3], tip)                     # searched near 03:00Z - 3h
        with mock.patch.object(espn, "find_event", return_value=None):
            self.assertEqual(kalshi.game_start(p), "2026-10-28T00:00:00+00:00")  # estimate
        with mock.patch.object(espn, "find_event", side_effect=OSError("down")):
            self.assertEqual(kalshi.game_start(p), "2026-10-28T00:00:00+00:00")

    def test_two_letter_team_split(self):
        t, o = kalshi.split_teams("nba", "KXNBAPTS-26OCT28GSWNOP", "NOPZWILLIAMSON1", NBA)
        self.assertEqual((t["abbr"], o["abbr"]), ("NO", "GS"))


UD_FEED = {
    "games": [{"id": 1, "sport_id": "NBA", "abbreviated_title": "NY @ DET", "away_team_id": "t-ny",
               "home_team_id": "t-det", "scheduled_at": "2026-10-28T23:00:00Z"}],
    "solo_games": [{"id": 2, "sport_id": "TENNIS", "home_player_id": "p-tf", "home_player_name": "(6) Taylor Fritz",
                    "away_player_id": "p-rh", "away_player_name": "Rinky Hijikata",
                    "scheduled_at": "2026-10-28T17:00:00Z"}],
    "players": [{"id": "p-jb", "first_name": "Jalen", "last_name": "Brunson", "sport_id": "NBA", "team_id": "t-ny"},
                {"id": "p-rh", "first_name": "Rinky", "last_name": "Hijikata", "sport_id": "TENNIS"}],
    "appearances": [{"id": "a-jb", "player_id": "p-jb", "match_id": 1, "team_id": "t-ny"},
                    {"id": "a-rh", "player_id": "p-rh", "match_id": 2}],
    "over_under_lines": [
        {"id": "l1", "status": "active", "stat_value": "26.5",
         "over_under": {"appearance_stat": {"appearance_id": "a-jb", "stat": "points", "display_stat": "Points "}},
         "options": [{"choice": "higher", "american_price": "+102"}, {"choice": "lower", "american_price": "-139"}]},
        {"id": "l2", "status": "active", "stat_value": "40.5",
         "over_under": {"appearance_stat": {"appearance_id": "a-jb", "stat": "fantasy_points",
                                            "display_stat": "Fantasy Points"}},
         "options": [{"choice": "higher", "american_price": ""}]},
        {"id": "l3", "status": "active", "stat_value": "9.5",
         "over_under": {"appearance_stat": {"appearance_id": "a-rh", "stat": "games_won", "display_stat": "Games Won"}},
         "options": [{"choice": "higher", "american_price": "-120"}, {"choice": "lower", "american_price": "-110"}]},
    ],
}


class Underdog(unittest.TestCase):
    def test_implied(self):
        self.assertEqual(underdog.implied("+102"), 0.495)
        self.assertEqual(underdog.implied("-139"), 0.5816)
        self.assertEqual(underdog.implied("+100"), 0.5)
        self.assertIsNone(underdog.implied(""))
        self.assertIsNone(underdog.implied(None))

    def test_parse_feed(self):
        with mock.patch.object(espn, "teams", return_value=NBA):
            props = {p.market_id: p for p in underdog.parse(UD_FEED)}
        p = props["l1"]
        self.assertEqual((p.source, p.league, p.stat, p.player, p.line), ("underdog", "nba", "points", "Jalen Brunson", 26.5))
        self.assertEqual((p.team, p.team_id, p.opponent, p.opponent_id), ("NY", "9", "DET", "4"))
        self.assertEqual((p.over_price, p.under_price), (0.495, 0.5816))
        self.assertEqual(p.url, "https://underdogfantasy.com/pick-em/higher-lower/all/nba")
        f = props["l2"]  # NBA stat ESPN doesn't cover: own key + label, no odds
        self.assertEqual((f.stat, f.stat_label, f.over_price, f.under_price),
                         ("fantasy_points", "Fantasy Points", None, None))
        t = props["l3"]
        self.assertEqual((t.league, t.stat, t.opponent, t.team), ("tennis", "games_won", "Taylor Fritz", None))

    def test_enrich_skips_unsupported(self):
        with mock.patch.object(espn, "teams", return_value=NBA):
            props = underdog.parse(UD_FEED, leagues=["tennis"])
        rows = service.enrich(props)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["stat_label"], rows[0]["error"], rows[0].get("l10")), ("Games Won", None, None))

    def test_home_vs_away_title(self):
        game = {"abbreviated_title": "BARCA vs ALA", "home_team_id": "h", "away_team_id": "a"}
        self.assertEqual(underdog._sides(game, {"team_id": "a"}, {}), ("ALA", "BARCA"))

    def test_started_games_are_dropped(self):
        now = dt.datetime.now(dt.timezone.utc)
        live = Prop("kalshi", "nba", "points", "A", 9.5, start_time=(now - dt.timedelta(minutes=1)).isoformat())
        soon = Prop("kalshi", "nba", "points", "B", 9.5, start_time=(now + dt.timedelta(minutes=5)).isoformat())
        unknown = Prop("kalshi", "nba", "points", "C", 9.5)
        with mock.patch.object(kalshi, "fetch_props", return_value=[live, soon, unknown]), \
                mock.patch.object(underdog, "fetch_props", return_value=[]):
            props, ok, _ = service.collect_sources()
        self.assertEqual([p.player for p in props], ["B", "C"])

    def test_failed_source_is_skipped(self):
        with mock.patch.object(underdog, "fetch_props", side_effect=OSError("426")), \
                mock.patch.object(kalshi, "fetch_props", return_value=[]):
            props, ok, errors = service.collect_sources()
        self.assertEqual((props, ok, list(errors)), ([], ["kalshi"], ["underdog"]))


def board_row(r5=0.8, r10=0.8, r20=0.75, g=(5, 10, 20), over=0.55, under=0.47, **kw):
    rates = dict(zip(("l5", "l10", "l20"), (r5, r10, r20)))
    row = {"source": "kalshi", "league": "nba", "stat": "points", "stat_label": "Points", "player": "Jalen Brunson",
           "team": "NY", "team_id": "18", "opponent": "DET", "opponent_id": "8", "line": 24.5,
           "over_price": over, "under_price": under, "start_time": "2026-10-28T03:00:00Z",
           "market_id": "KXNBAPTS-X-NYKJBRUNSON11-25", "athlete_id": "3934672", "error": None}
    row.update({k: {"games": n, "rate": rates[k]} for k, n in zip(("l5", "l10", "l20"), g)})
    row.update(kw)
    return row


NOW = dt.datetime(2026, 10, 27, 23, 0, tzinfo=dt.timezone.utc)


def event(start, state="pre", name="STATUS_SCHEDULED", completed=False, teams=("18", "8"), eid="401909878"):
    return {"id": eid, "start": start, "state": state, "name": name, "completed": completed, "teams": set(teams)}


class Picks(unittest.TestCase):
    def test_rule(self):
        q = picks.qualifies(board_row(), "over")
        self.assertEqual((q["l5"], q["l20"], q["price"], q["score"]), (0.8, 0.75, 0.55, 0.2))
        self.assertIsNone(picks.qualifies(board_row(), "under"))            # under rates 0.2-0.25
        self.assertIsNone(picks.qualifies(board_row(g=(5, 10, 19)), "over"))  # L20 not full
        self.assertIsNone(picks.qualifies(board_row(r20=0.69), "over"))      # one window under 0.70
        self.assertIsNone(picks.qualifies(board_row(over=0.66), "over"))     # score 0.09
        self.assertIsNotNone(picks.qualifies(board_row(over=0.65), "over"))  # score 0.10
        self.assertIsNone(picks.qualifies(board_row(over=None), "over"))
        self.assertIsNone(picks.qualifies(board_row(over=1.0), "over"))
        u = picks.qualifies(board_row(r5=0.2, r10=0.1, r20=0.25, under=0.6), "under")
        self.assertEqual((u["l5"], u["l10"], u["l20"], u["score"]), (0.8, 0.9, 0.75, 0.15))

    def test_select_uses_espn_start(self):
        # Kalshi lists 03:00Z, ESPN tips at 00:00Z: within 2h of 23:00Z only by ESPN's time.
        tip = dt.datetime(2026, 10, 28, 0, 0, tzinfo=dt.timezone.utc)
        with mock.patch.object(espn, "scoreboard", return_value=[event(tip)]):
            out = picks.select([board_row()], NOW)
            late = picks.select([board_row()], NOW - dt.timedelta(hours=1, minutes=1))
        self.assertEqual([(p["side"], p["event_id"], p["start_time"]) for p in out],
                         [("over", "401909878", "2026-10-28T00:00:00+00:00")])
        self.assertEqual(late, [])
        with mock.patch.object(espn, "scoreboard", return_value=[event(tip, state="in")]):
            self.assertEqual(picks.select([board_row()], NOW), [])  # already started

    def test_select_skips_rows_without_ids(self):
        with mock.patch.object(espn, "scoreboard", return_value=[]) as sb:
            self.assertEqual(picks.select([board_row(market_id=None), board_row(athlete_id=None),
                                           board_row(error="player not found on ESPN roster")], NOW), [])
        sb.assert_not_called()

    def test_grade_value(self):
        self.assertEqual(picks.grade_value(27, 24.5, "over"), "hit")
        self.assertEqual(picks.grade_value(20, 24.5, "over"), "miss")
        self.assertEqual(picks.grade_value(20, 24.5, "under"), "hit")
        self.assertEqual(picks.grade_value(25, 25, "under"), "push")

    def test_grade(self):
        tip = dt.datetime(2026, 10, 27, 0, 0, tzinfo=dt.timezone.utc)
        pending = [{"id": i, "league": "nba", "stat": "points", "athlete_id": "1", "event_id": str(i), "line": 24.5,
                    "side": "over", "start": str(tip.timestamp())} for i in (1, 2, 3, 4, 5)]
        board = [event(tip, "post", "STATUS_FINAL", True, eid="1"), event(tip, "post", "STATUS_FINAL", True, eid="2"),
                 event(tip, "post", "STATUS_POSTPONED", False, eid="3"), event(tip, "in", eid="4"),
                 event(tip, "post", "STATUS_FINAL", True, eid="5")]
        games = {"1": (True, 30.0), "2": (False, None), "5": (False, None)}
        updates = []

        def query(sql, params=None, url=None):
            if sql.startswith("SELECT"):
                return pending
            updates.append(params)
            return []

        with mock.patch.object(picks.db, "query", side_effect=query), \
                mock.patch.object(espn, "scoreboard", return_value=board), \
                mock.patch.object(picks, "_player_game", side_effect=lambda l, a, s, e: games[e]):
            counts = picks.grade(tip + dt.timedelta(hours=7))
            self.assertEqual(counts, {"hit": 1, "void": 3})
            self.assertEqual(sorted((u[2], u[0], u[1]) for u in updates),
                             [(1, "hit", 30.0), (2, "void", None), (3, "void", None), (5, "void", None)])
            updates.clear()
            # Final but missing from the log only 3h after tip: wait for ESPN to update.
            self.assertEqual(picks.grade(tip + dt.timedelta(hours=3)), {"hit": 1, "void": 1})

    def test_no_gamelog_this_season_is_not_played(self):
        err = picks.net.urllib.error.HTTPError("u", 404, "Not Found", None, None)
        with mock.patch.object(picks.espn, "current_season", return_value=2027), \
                mock.patch.object(picks.net, "get_json", side_effect=err):
            self.assertEqual(picks._player_game("nhl", "1", "shots", "9"), (False, None))


def log_game(i, value, minutes="30", season=2027, key="points"):
    return Game(event_id=str(i), date=f"2026-{10 - i // 28:02d}-{28 - i % 28:02d}", season=season, postseason=False,
                team_id="9", opponent_id="2", opponent="BOS", home=True, row={key: str(value), "minutes": minutes})


class ModelV2(unittest.TestCase):
    def test_count_distributions(self):
        self.assertAlmostEqual(model._poisson_cdf(2, 2.0), 0.6767, places=4)
        self.assertAlmostEqual(model._nbinom_cdf(2, 2.0, 4.0), 0.6875, places=4)   # r=2, p=0.5
        # Whole-number line: the push is removed from both sides.
        over = model._count_over(2, 2.0, 2.0)
        p_eq = model._poisson_cdf(2, 2.0) - model._poisson_cdf(1, 2.0)
        self.assertAlmostEqual(over, (1 - model._poisson_cdf(2, 2.0)) / (1 - p_eq), places=6)
        self.assertAlmostEqual(model._normal_over(70.5, 70.5, 20), 0.5)

    def test_recent_games_weigh_more(self):
        hot = [log_game(i, 30 if i < 10 else 15) for i in range(20)]
        cold = [log_game(i, 15 if i < 10 else 30) for i in range(20)]
        stat = get("nba", "points")
        p_hot, n = model.p_over("nba", stat, 22.5, hot, 2027)
        p_cold, _ = model.p_over("nba", stat, 22.5, cold, 2027)
        self.assertEqual(n, 20)
        older = [log_game(i, 20, season=2027 if i < 25 else 2026) for i in range(30)]
        self.assertEqual(model.p_over("nba", stat, 22.5, older, 2027)[1], 25)  # last 20 + rest of this season
        # Same values, opposite order: the hot-recent player is well above the cold-recent one.
        self.assertGreater(p_hot - p_cold, 0.2)
        self.assertIsNone(model.p_over("nba", stat, 22.5, hot[:4], 2027))

    def test_minutes_trend_scales_the_mean(self):
        stat = get("nba", "points")
        flat = [log_game(i, 20) for i in range(20)]
        more = [log_game(i, 20, minutes="36" if i < 5 else "30") for i in range(20)]
        self.assertGreater(model.p_over("nba", stat, 21.5, more, 2027)[0], model.p_over("nba", stat, 21.5, flat, 2027)[0])
        t = model.minutes_trend("nba", more, 2027)
        self.assertEqual((t["min_l5"], t["min_season"], t["min_trend"]), (36.0, 31.5, 1.143))

    def test_evaluate_blends_market_and_charges_kalshi_fee(self):
        games = [log_game(i, v) for i, v in enumerate([25, 28, 30, 22, 27, 26, 24, 29, 31, 23] * 2)]
        row = {"source": "kalshi", "line": 24.5, "over_price": 0.55, "under_price": 0.47}
        out = model.evaluate(row, "nba", get("nba", "points"), games, 2027)
        p_stat, n = model.p_over("nba", get("nba", "points"), 24.5, games, 2027)
        w = model.W_MAX * n / (n + model.K)
        self.assertAlmostEqual(out["p_model"], round(w * p_stat + (1 - w) * 0.54, 4), places=4)
        self.assertEqual(out["edge_side"], "over")
        self.assertAlmostEqual(out["edge"], round(out["p_model"] - 0.55 - 0.07 * 0.55 * 0.45, 4), places=4)
        ud = model.evaluate(dict(row, source="underdog"), "nba", get("nba", "points"), games, 2027)
        self.assertAlmostEqual(model.market_prob(0.55, 0.47, "underdog"), 0.55 / 1.02)
        self.assertIsNotNone(ud["edge"])


class Availability(unittest.TestCase):
    def test_designations(self):
        d = espn._designation
        self.assertEqual(d("Out", ""), "Out")
        self.assertEqual(d("Injured Reserve", ""), "Out")
        self.assertEqual(d("60-Day-IL", ""), "Out")
        self.assertEqual(d("Questionable", ""), "Questionable")
        self.assertEqual(d("Day-To-Day", "Wiggins is questionable for Wednesday's game."), "Questionable")
        self.assertEqual(d("Day-To-Day", "Brunson is doubtful for Friday."), "Doubtful")
        self.assertEqual(d("Day-To-Day", "He was ruled out for Wednesday."), "Out")
        self.assertEqual(d("Day-To-Day", "Listed as day-to-day."), "Day-To-Day")
        self.assertIsNone(d("Active", ""))

    def test_injuries_feed(self):
        feed = {"injuries": [{"injuries": [
            {"status": "Out", "shortComment": "", "athlete": {"links": [{"href": "https://www.espn.com/nba/player/_/id/123/x"}]}},
            {"status": "Day-To-Day", "shortComment": "X is doubtful Friday.",
             "athlete": {"links": [{"href": "https://www.espn.com/nba/player/_/id/456/y"}]}},
            {"status": "Active", "athlete": {"links": [{"href": "https://www.espn.com/nfl/player/_/id/789/z"}]}}]}]}
        with mock.patch.object(espn.net, "get_json", return_value=feed):
            self.assertEqual(espn.injuries("nba"), {"123": "Out", "456": "Doubtful"})

    def test_hockey_time_on_ice(self):
        g = Game("1", "2026-10-26", 2027, False, "9", "2", "BOS", True, {"timeOnIcePerGame": "18:30"})
        self.assertEqual(espn.minutes("nhl", g), 18.5)
        self.assertIsNone(espn.minutes("nfl", g))


CFB = {"NMSU": {"id": "166", "abbr": "NMSU", "name": "New Mexico State"},
       "JXST": {"id": "55", "abbr": "JXST", "name": "Jacksonville State"},
       "JSU": {"id": "2296", "abbr": "JSU", "name": "Jackson State"}}


class CollegeFootball(unittest.TestCase):
    def test_codes_paired_through_scoreboard(self):
        ev = {"id": "1", "start": dt.datetime(2026, 10, 29, 0, 0, tzinfo=dt.timezone.utc), "state": "pre", "name": "",
              "completed": False, "teams": {"166", "55"}, "abbrs": {"166": "NMSU", "55": "JXST"}}
        with mock.patch.object(espn, "scoreboard", return_value=[ev]):
            t, o = espn.match_game("cfb", "JSU", "NMSU", "2026-10-29T00:00:00Z", CFB)
            self.assertEqual((t["abbr"], o["abbr"]), ("JXST", "NMSU"))
            t, o = espn.match_game("cfb", "NMSU", "JSU", "2026-10-29T00:00:00Z", CFB)
            self.assertEqual((t["abbr"], o["abbr"]), ("NMSU", "JXST"))
            self.assertIsNone(espn.match_game("cfb", "JSU", "NMSU", "2026-10-30T00:00:00Z", CFB))

    def test_football_stats(self):
        qb = {"passingYards": "250", "rushingYards": "30", "rushingTouchdowns": "1", "longPassing": "45"}
        wr = {"receivingYards": "80", "rushingYards": "5", "receivingTouchdowns": "1", "rushingTouchdowns": "0",
              "receivingTargets": "9", "longReception": "31", "fumblesLost": "0"}
        k = {"fieldGoalsMade-fieldGoalAttempts": "2-3", "extraPointsMade-extraPointAttempts": "4-4",
             "totalKickingPoints": "10"}
        self.assertEqual(get("nfl", "anytime_td").value(qb), 1)
        self.assertEqual(get("cfb", "anytime_td").value(wr), 1)
        self.assertEqual(get("nfl", "pass_rush_yds").value(qb), 280)
        self.assertEqual(get("nfl", "rush_rec_yds").value(wr), 85)
        self.assertIsNone(get("nfl", "rush_rec_yds").value(k))       # no rushing/receiving columns at all
        self.assertEqual((get("nfl", "targets").value(wr), get("nfl", "rec_long").value(wr)), (9, 31))
        self.assertEqual((get("nfl", "fg_made").value(k), get("nfl", "xp_made").value(k),
                          get("nfl", "kicking_points").value(k)), (2, 4, 10))
        self.assertTrue(get("cfb", "rec_yds").continuous)
        self.assertEqual(underdog.STAT_MAP["cfb"]["rush_rec_tds"], "anytime_td")


class PicksV2(unittest.TestCase):
    def test_both_rules_and_injury_exclusion(self):
        tip = dt.datetime(2026, 10, 28, 0, 0, tzinfo=dt.timezone.utc)
        row = board_row(p_model=0.68, edge=0.093, edge_side="over", model_version="v2.0", injury_status="Questionable")
        with mock.patch.object(espn, "scoreboard", return_value=[event(tip)]):
            out = picks.select([row], NOW)
            self.assertEqual(sorted((p["rule_version"], p["side"]) for p in out),
                             [("v1_hitrate", "over"), ("v2_model", "over")])
            v2 = next(p for p in out if p["rule_version"] == "v2_model")
            self.assertEqual((v2["score"], v2["injury_status"], v2["edge"]), (None, "Questionable", round(0.68 - 0.55 - 0.07 * 0.55 * 0.45, 4)))
            for tag in ("Out", "Doubtful"):
                self.assertEqual(picks.select([dict(row, injury_status=tag)], NOW), [])
            self.assertEqual([p["rule_version"] for p in picks.select([dict(row, edge=0.049)], NOW)], ["v1_hitrate"])

    def test_report_dedupes_and_bins(self):
        rows = [{"rule_version": "v1_hitrate", "league": "nba", "result": "hit", "p_side": 0.62},
                {"rule_version": "v2_model", "league": "nba", "result": "miss", "p_side": 0.58},
                {"rule_version": "v2_model", "league": "nhl", "result": None, "p_side": 0.7}]
        calls = []

        def query(sql, params=None, url=None):
            calls.append(sql)
            return rows if "rule_version}" not in sql and ", rule_version" in sql else rows[:2]
        with mock.patch.object(picks.db, "query", side_effect=query):
            rep = picks.report(NOW)
        self.assertIn("DISTINCT ON (player, stat, line, side, event_id, rule_version)", calls[0])
        self.assertIn("DISTINCT ON (player, stat, line, side, event_id)", calls[1])
        self.assertEqual(rep["rules"]["v2_model"]["picks"], 2)
        self.assertEqual(rep["rules"]["v2_model"]["graded"], 1)
        self.assertEqual(rep["rules"]["v1_hitrate"]["hit_rate"], 1.0)
        self.assertEqual([(b["bin"], b["n"], b["hit_rate"]) for b in rep["calibration"]],
                         [("0.55-0.60", 1, 0.0), ("0.60-0.65", 1, 1.0)])


class Gamelog(unittest.TestCase):
    def test_parse_skips_preseason(self):
        data = {
            "names": ["minutes", "points"],
            "events": {
                "1": {"gameDate": "2026-10-25T00:00Z", "team": {"id": "9"}, "homeTeamId": "9",
                      "opponent": {"id": "2", "abbreviation": "BOS"}},
                "2": {"gameDate": "2026-10-10T00:00Z", "team": {"id": "9"}, "homeTeamId": "2",
                      "opponent": {"id": "2", "abbreviation": "BOS"}},
            },
            "seasonTypes": [
                {"displayName": "2026-27 Regular Season", "categories": [{"events": [{"eventId": "1", "stats": ["30", "21"]}]}]},
                {"displayName": "2026-27 Preseason", "categories": [{"events": [{"eventId": "2", "stats": ["20", "9"]}]}]},
            ],
        }
        games = parse_gamelog(data, 2027)
        self.assertEqual([g.event_id for g in games], ["1"])
        self.assertTrue(games[0].home)
        self.assertEqual(games[0].row["points"], "21")


if __name__ == "__main__":
    unittest.main()
