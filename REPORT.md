# Props service: pre-deploy load report

Run on 2026-10-27, ~17:20 UTC, from branch `claude/outlier-props-service-qo8o83` (commit `c5a738e`)
plus the database writer added in this change. Network access: Full.

## 1. DATABASE_URL

**Set and connected.** It points at Neon database `neondb` (role `neondb_owner`, pooler endpoint
in `us-east-2`).

- Direct Postgres on port 5432 **timed out** from the build container, because outbound traffic
  goes through an HTTPS proxy. Render does not have this limit.
- Neon's HTTPS SQL endpoint (`https://<host>/sql`) **worked** with the same `DATABASE_URL`, so
  the writer uses it. That keeps the props package stdlib-only and works in both places.

Before this run, Neon had only the `public.signal_lab_*` tables. **`props.props_board` did not
exist, and the props service had no database code.** This change adds:

- `props/db.py`. It creates `props` / `props.props_board` if needed, then replaces rows for the
  given sources and leagues in one transaction.
- `python -m props load`, which runs one refresh and saves it.
- An automatic save after every background refresh in `python -m props serve` when `DATABASE_URL`
  is set.

No Signal Lab tables were touched.

## 2. Sources

| Source | Result |
| --- | --- |
| **Kalshi** (`api.elections.kalshi.com`) | ✅ HTTP 200. 3,029 open player-prop markets across NBA, NFL, MLB and NHL. |
| **Player stats** (ESPN `site.api.espn.com`, `site.web.api.espn.com`) | ✅ HTTP 200. 3,028 of 3,029 props matched to a game log. One miss: Dante Fabbro (NHL, goals 0.5), "player not found on ESPN roster". |
| **Underdog** | ❌ **No data.** The service doesn't fetch Underdog by design: it has no official public API, so lines come from CSV/JSON import (`import-underdog` or `POST /api/underdog`), and nothing was imported. A probe of `api.underdogfantasy.com/beta/v5/over_under_lines` returned **HTTP 426 Upgrade Required**: the host was reachable but refused the request. |

No host was blocked by the network policy. The only failure was direct Postgres on port 5432
(see above).

WNBA returned no Kalshi markets (off-season).

## 3. Rows in `props.props_board`

| Sport | Source | Rows | With hit rates |
| --- | --- | ---: | ---: |
| MLB | kalshi | 197 | 197 |
| NBA | kalshi | 821 | 821 |
| NFL | kalshi | 387 | 387 |
| NHL | kalshi | 1,624 | 1,623 |
| **Total** | | **3,029** | **3,028** |

Underdog: 0 rows in every sport. WNBA: 0 rows.

I loaded the board twice: once from a saved `slate --json` run, then with a full live
`python -m props load` (about 1m52s). Both gave the same counts. Sample row: Cade Cunningham
(DET vs NY), Assists 3.5, Kalshi YES ask $0.96. L5/L10 100%, L20 95%, H2H 12/13.

## Before deploying

- Deploy a branch that has `props/db.py`. `claude/outlier-props-service-qo8o83` on its own has no
  database writes. This work is on `claude/outlier-props-service-qo8o83-r1kdmf`, a fast-forward of
  that branch.
- Set `DATABASE_URL` on the Render service. Optionally set `PROPS_ADMIN_TOKEN` to protect Underdog
  imports.
- To get Underdog rows, import a slate. They then appear as `source = 'underdog'` on the next
  refresh.
- `props/data/` (Underdog imports) is on local disk and is lost on Render redeploys unless a disk
  is attached.
