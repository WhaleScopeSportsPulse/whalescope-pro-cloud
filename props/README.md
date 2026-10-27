# WhaleScope Props

An Outlier-style props service. It pulls player-prop lines you can bet on
**Kalshi** and **Underdog**, matches each player to their ESPN game log, and
reports how often they went over the line in the:

| Window | What counts |
| --- | --- |
| L5 / L10 / L20 | Last 5, 10, 20 games played. Spans seasons when the current one is short. |
| Season | Games played in the current season. |
| H2H | Games vs. tonight's opponent over the current and previous two seasons. |

Regular season and playoffs count. Preseason and DNPs don't. A result exactly
on a whole-number line is a push and stays out of the rate. Under rate = 1 − over rate.

Standard library only (Python 3.9+). No changes to Signal Lab.

## Run it

```bash
python -m props serve                    # web board + JSON API on $PORT (default 8080)
python -m props slate --league nba --window l10 --min-rate 0.7
python -m props slate --source underdog --side under --window h2h
python -m props player --league nba --name "Zach LaVine" --stat points --line 22.5 --opponent SAS
python -m props import-underdog lines.csv
python -m props load                     # refresh once and write props.props_board ($DATABASE_URL)
python -m unittest discover -s props/tests -t .
```

Run commands from the repo root. The server refreshes the whole slate in the
background every `PROPS_REFRESH_SECONDS` (300). A cold refresh of every league
takes about two minutes, mostly ESPN game-log lookups. Later ones mostly hit the cache.

## Sources

- **Kalshi**: official public API (`/trade-api/v2/markets`, no auth). It reads every
  open market in the per-game player series listed in `kalshi.py` (`KXNBAPTS`,
  `KXNFLRECYDS`, `KXMLBKS`, `KXNHLPTS`, …). Each `Player: N+` market becomes one
  prop with line `N − 0.5`. The price is the YES ask for over and the NO ask for under,
  in dollars, which is also the implied probability.
- **Underdog**: Underdog has no official public API, so the service doesn't call or
  scrape it. Import the lines you see in the app as CSV or JSON:

  ```csv
  league,player,team,opponent,stat,line,start_time
  nba,Jalen Brunson,NYK,DET,Pts + Rebs + Asts,34.5,
  nfl,Josh Jacobs,GB,CAR,Rush + Rec Yards,84.5,
  ```

  Use the CLI, or `POST /api/underdog` with the file as the body. Each import
  replaces the stored slate. Set `PROPS_ADMIN_TOKEN` to require `Authorization: Bearer <token>`.
  `stat` accepts Underdog's labels or the keys in `stats.py`.
- **Stats**: ESPN's public site API for team rosters and player game logs. Kalshi lists
  ESPN as a settlement source. These endpoints are undocumented and could change.

## Database

When `DATABASE_URL` is set, every server refresh (and `python -m props load`) replaces
the rows in `props.props_board` for the leagues it covers, in one transaction. The schema
and table are created on first save. Each row is one prop: line, prices, `*_rate` and
`*_games` per window, the full windows in `windows` (jsonb), the last 10 games in `recent`,
and `updated_at`. Writes go through Neon's HTTPS SQL endpoint (`https://<host>/sql`), so
no driver is needed and they work where port 5432 is blocked. `DATABASE_URL` must be a
Neon connection string.

## Supported props

NBA/WNBA: points, rebounds, assists, threes, steals, blocks, stocks, PRA, P+R, P+A, R+A, FTM ·
NFL: pass yds/TDs/att/comp/INT, rush yds/att, rec yds, receptions, rush+rec yds ·
MLB: hits, HR, RBI, runs, total bases, H+R+RBI, SB, pitcher Ks, outs, hits allowed, ER ·
NHL: goals, assists, points, shots, saves.

## API

| Route | Notes |
| --- | --- |
| `GET /` | Web board with filters and a last-10 bar chart |
| `GET /api/props` | `league`, `source`, `stat`, `player`, `window` (l5/l10/l20/season/h2h), `side` (over/under), `min_rate`, `min_games`, `limit`. Sorted by the window's rate. |
| `GET /api/player` | `league`, `name`, `stat`, `line`, optional `team`, `opponent` |
| `GET/POST /api/underdog` | Read or replace imported Underdog lines |
| `GET /api/stats` | Supported stat keys per league |
| `GET /health` | Slate size, last refresh, last error |

Each prop row has `l5`, `l10`, `l20`, `season`, `h2h` (`games`, `over`, `under`,
`push`, `rate`, `avg`), the last 10 game values in `recent`, and for Kalshi
`l10_minus_price` (L10 over rate minus YES ask).

## Settings

| Env var | Default |
| --- | --- |
| `PORT`, `HOST` | `8080`, `0.0.0.0` |
| `PROPS_LEAGUES` | all (`nba,wnba,nfl,mlb,nhl`) |
| `PROPS_REFRESH_SECONDS` | `300` |
| `PROPS_GAMELOG_TTL` | `1800` s for current-season logs (past seasons: 24 h) |
| `PROPS_KALSHI_TTL` | `60` |
| `PROPS_WORKERS` | `6` concurrent player lookups |
| `PROPS_H2H_SEASONS_BACK` | `2` |
| `PROPS_DATA_DIR` | `props/data` (Underdog imports) |
| `PROPS_ADMIN_TOKEN` | unset (imports open) |
| `DATABASE_URL` | unset (no database writes) |
| `KALSHI_BASE` | `https://api.elections.kalshi.com/trade-api/v2/` |

Hit rates describe what already happened. They don't predict results.
