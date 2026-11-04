import unittest

from props import espn, kalshi, underdog
from props.espn import Game, parse_gamelog
from props.hitrate import hit_rates, window
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

    def test_two_letter_team_split(self):
        t, o = kalshi.split_teams("nba", "KXNBAPTS-26OCT28GSWNOP", "NOPZWILLIAMSON1", NBA)
        self.assertEqual((t["abbr"], o["abbr"]), ("NO", "GS"))


class Underdog(unittest.TestCase):
    def test_csv(self):
        rows, errors = underdog.parse(
            "league,player,team,opponent,stat,line\n"
            "NBA,Jalen Brunson,NYK,DET,Points,26.5\n"
            "nba,Someone,NYK,DET,Fantasy Points,40\n"
            "nfl,Josh Allen,BUF,MIA,Passing Yards,abc\n")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["stat"], rows[0]["line"], rows[0]["team"]), ("points", 26.5, "NYK"))
        self.assertEqual(len(errors), 2)

    def test_json(self):
        rows, errors = underdog.parse('[{"league":"nhl","player":"Kirill Kaprizov","stat":"Shots","line":3.5}]')
        self.assertEqual((rows[0]["stat"], errors), ("shots", []))


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
