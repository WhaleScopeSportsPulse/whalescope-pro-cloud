# Props service: load and deploy report

Branch `claude/outlier-props-service-qo8o83-r1kdmf`. Last load: 2026-10-27, ~18:10 UTC, run from
the build container against Neon.

## Database

`DATABASE_URL` connects to Neon `neondb`. Writes use Neon's HTTPS SQL endpoint because outbound
port 5432 is blocked in the build container.

`props.props_board` is created on first save. Each save replaces only the rows for the sources
that loaded, in one transaction.

## Sources

| Source | Status |
| --- | --- |
| Kalshi (official public API) | ✅ Loaded |
| Underdog (`api.underdogfantasy.com/v1/over_under_lines`, unofficial) | ✅ Loaded on every refresh. If it fails, Underdog is skipped, Kalshi still loads, and the last good Underdog rows stay in the table. |
| ESPN player stats | ✅ Loaded. A few players aren't on ESPN rosters (e.g. Dante Fabbro, Deuce McBride). |

How Underdog rows are stored:

- **League:** Underdog's sport id, lowercased. `fifa` is soccer (EPL, La Liga, Serie A, Ligue 1),
  `basketball` is EuroLeague, and `lol` / `cs` / `val` are esports.
- **Prices:** each option's `american_price`, converted to implied probability. A side Underdog
  doesn't offer is null. About half the lines have no "lower" side, and every soccer line is
  higher-only.
- **URL:** `https://underdogfantasy.com/pick-em/higher-lower/all/<league>`.
- **Hit rates:** filled for NBA, WNBA, NFL, MLB and NHL stats that ESPN covers. Other leagues and
  stats (fantasy points, 1H/1Q, anytime TD, etc.) keep Underdog's own stat key and label, and their
  hit-rate columns are empty.

## Rows in `props.props_board`

| Sport | Underdog | Underdog with hit rates | Kalshi |
| --- | ---: | ---: | ---: |
| nfl | 2,042 | 382 | 387 |
| nhl | 1,778 | 1,050 | 1,634 |
| cfb | 1,030 | — | — |
| fifa (soccer) | 916 | — | — |
| nba | 448 | 371 | 836 |
| tennis | 420 | — | — |
| lol | 180 | — | — |
| mlb | 164 | 123 | 197 |
| basketball (EuroLeague) | 131 | — | — |
| wnba | 70 | 62 | — |
| cs | 68 | — | — |
| mma | 44 | — | — |
| val | 36 | — | — |
| **Total** | **7,327** | **1,988** | **3,054** |

## Memory fix

A full refresh used to peak at **3 GB**, because every raw ESPN game-log response was cached.
Parsed game logs are now cached instead, and the 20 MB Underdog feed is not cached. Peak memory is
now **230 MB** for a cold refresh and **340 MB** for a warm one, under Render's 512 MB limit on
the Free and Starter plans.

A refresh takes about 1m45s from cold and about 35s warm.

## Render deploy: not done (needs payment)

A free-tier deploy that refreshes every 10–15 minutes isn't possible in this workspace:

- **Render cron jobs have no free plan.**
- **A free web service sleeps** after 15 minutes without inbound traffic, and the refresh loop
  stops while it sleeps. Keeping it awake would use about 744 of the workspace's **750 free
  instance hours per month**.
- **`signal-lab-pro` already uses about the same.** It is kept awake around the clock by the paid
  `signal-lab-5min-healthcheck` cron job. Running both all month would exceed the 750 hours, and
  Render then **suspends every free web service in the workspace** (`signal-lab-live`,
  `signal-lab-pro`, …) until the next month.

Options:

| Option | Cost |
| --- | --- |
| **Render cron job** (Starter, 512 MB, $0.00016/min) running `python -m props load` every 12–15 min. Each run is a cold refresh, about 2–2.5 min including startup. | About **$1.15–1.45 / month** (7,200–9,000 run-minutes; $1/month minimum) |
| **Starter web service** (always on, serves the board, saves on each refresh) | **$7 / month** |
| Free web service with no keep-awake | $0, but it only refreshes while someone has the page open, so it doesn't meet the 10–15 minute requirement |

Settings to use for either paid option:

- **Build command:** `true` (stdlib only)
- **Start command:** `python -m props load` (cron), or `python -m props serve` with
  `PROPS_REFRESH_SECONDS=720` (web)
- **Environment:** `DATABASE_URL`
