# Velocity

Paper‑trades one iShares ETF's daily holdings with a trend‑following strategy, 24×7, and serves the
results as web pages — always measured against the fund itself. One process, one SQLite file,
no external services, no market‑data feed: the fund's own valuation prices are the prices.

## TL;DR

```bash
pip install -r requirements.txt                 # Python 3.12: requests, pandas, numpy
velocity backfill --months 15                   # once: the fund's holdings history -> data/velocity.db
velocity backtest --windows 6,12                # how the strategy did against the fund on that history
velocity serve                                  # forever: poll every 10 min, trade, serve http://127.0.0.1:8787/
```

* **`velocity serve`** is the application. Every `--poll-frequency` minutes (default 10) it fetches
  the fund's holdings CSV; when a new day is stored it runs the session (signals → sizing → paper
  fills at that day's holdings prices) and records the session, every holding's signal state and the
  book. After downtime it catches up the missed days in order. It logs where it listens:
  `web: listening on http://127.0.0.1:8787/ (host …, port …, hostname …)`.
* **One strategy, one book.** Settings are flags (`--param k=v`, `--capital`, `--max-positions`,
  `--cash-reserve`, `--max-positions-policy`, `--rising-priority`); change anything by restarting.
  If the flags differ from the book's last session, `serve` refuses unless `--accept-param-change`
  (or `velocity resetbook` to start over).
* **One process at a time.** While `serve` runs it holds `data/velocity.lock`; `backfill`,
  `backtest`, `signals`, `ticker` and `resetbook` are refused until you stop it (Ctrl‑C / SIGTERM —
  the current cycle finishes first). `status` always works.
* **All times are UTC.**

## Commands

| command | what | network |
|---|---|---|
| `serve` | poll · trade · web (`--poll-frequency 10 --host 127.0.0.1 --port 8787 --no-web --once --no-catchup`) | yes |
| `status` | holdings date + when updated, last/next poll, last cycle, the book, last fetches | no |
| `backfill` | N months of history (`--months 12 --wipe --force --dry-run`); keeps stored rows, fills gaps | yes |
| `backtest` | replay the strategy over the stored history **against the fund** (`--windows 3,6,9 --start --end --fill --trades`); `--save-book` makes the result the live book | no |
| `signals` | signal‑quality report: forward excess return of every rule (does the rule *select* anything?) | no |
| `ticker T` | one ticker's signal history, plain text | no |
| `resetbook` | delete the book and its session/signal history | no |

`velocity <command> --help` lists every flag. Bad input → usage message, exit 2, nothing touched.

## Web

| page | content |
|---|---|
| `/` | status strip, the book's headline numbers vs the fund with the equity curve, positions, the latest holdings day |
| `/book` | the performance page: equity vs fund, drawdown, exposure, open positions (weight, stop distance), sector exposure, open risk, rolling quarters vs the fund, monthly returns, trade analytics (exit reasons, P&L by sector, best/worst), recent trades, waiting signals, sessions |
| `/session/DATE` | one session: exits, entries, not taken, waiting signals after, every signal, the settings used |
| `/ticker/T` | price chart with the book's buys/sells and the current stop, today's checks, signal history |
| `/signals` | the signal‑quality table |
| `/month/YYYY-MM`, `/day/DATE` | the holdings days: new entries, exits, weight movers, top‑20, sectors, signal states |
| `/status` | poller, fetch log, recent sessions |

JSON: `/api/health`, `/api/status`, `/api/dates`, `/api/day/DATE`, `/api/month/YYYY-MM`, `/api/book`,
`/api/performance`, `/api/sessions`, `/api/session/DATE`, `/api/ticker/T`, `/api/signals`.
Pages are rendered from the database on request (read‑only), charts are server‑rendered SVG, and
pages reload when a cycle completes; historical days show signals and waiting lists as they were.

## Strategy

`trend_follow` — Donchian breakout inside an established uptrend, ranked by relative strength.

| | rule |
|---|---|
| entry | close > SMA50 and SMA50 rising · close ≥ 55‑day high · 60‑day ROC percentile ≥ 50 % · not a gap day (< 3 σ) |
| rank | relative‑strength percentile (best first when slots are limited) |
| stop | volatility‑scaled trailing stop 2.5 σ₂₀ √10, clamped 8–25 %, ratcheted on the highest close |
| exit | close < 20‑day low · 2 closes < SMA50 · 30‑day time stop when underwater · dropped from the fund |
| new names | a name has prices only while it is in the fund; until the full rules can be evaluated (61 sessions) a name with ≥ `freeze_days` (10) sessions is judged by the same rules with look‑backs shortened to 10 (SMA10 rising · new high since joining · 9‑day RS vs the whole fund · gap filter · exits on the 10‑day low / SMA10) |

Parameters: `velocity backtest --param breakout_len=40 …` (`src/strategy.py::TrendFollow.defaults`).
Why these rules and not others — including what was tried — is in `docs/REVIEW-AND-PLAN.md`.

**Engine** (`src/engine.py`): one position per issuer (GOOG = GOOGL); every position gets an equal
share of the investable equity, `equity × (1 − cash_reserve) / max_positions` (defaults: 25
positions, 10 % reserve → 3.6 % each); the reserve is dry powder that is never spent — a fill that
would dip into it is scaled down or refused. `--max-positions-policy skip` never sells to make room,
`replace` swaps the weakest *losing* position held ≥ 5 sessions. Signals that don't fit wait as
**rising stars**. Fills at the signal day's holdings price (the paper book's execution price).

## Data

`data/velocity.db` (SQLite, WAL): `snapshots` / `holdings` / `weekly_closes` (history), `books`,
`sessions` (+ the parameters each ran with), `signals`, `fetch_log`, `meta`. Each session is one
transaction and idempotent per date. A new book starts on the latest date; seed one from history
with `backtest --save-book`.

Prices are the fund's valuation prices, split‑adjusted, **only while a stock is in the fund**: a
newly added name has no history and is frozen for `freeze_days` sessions (10), after which the
shortened rules apply until the full 61‑session history exists. There is no volume, no OHLC, no
dividend adjustment.

Another fund: change `portfolioId` in `HOLDINGS_URL` (`src/config.py`), then
`velocity backfill --wipe --force` and `velocity resetbook`. Nothing else names the fund.

Coming from the old per‑ETF layout (`data/<ETF>.db` + `data/books.db`):
`python scripts/migrate_single_db.py data/<ETF>.db` copies the holdings history into
`data/velocity.db`; the book starts fresh.

## Layout

```
velocity.py            CLI entry point           src/commands/     one module per command
src/strategy.py        the strategy              src/engine.py     sessions, sizing, limits
src/book.py            positions, trades, ledger src/analytics.py  benchmark, performance, trades, signal quality
src/indicators.py      vectorised indicators     src/market_data.py bars + universe from the holdings
src/scheduler.py       the 24x7 poller           src/services/     holdings fetch, sessions, books, backtest
src/web/               server, data, pages, charts (SVG), html blocks
tests/                 python -m pytest tests -q
```

## Standalone holdings‑changes report (`cli/`)

`python cli/holdings_report.py` is self‑contained (`pip install -r cli/requirements.txt` — just
`requests`; it imports nothing from `src/` and never touches the database; the fund's `portfolioId`
is set at the top of the script). It downloads the
fund's daily holdings CSVs into `cli/data/holdings-YYYY-MM-DD.csv` (every weekday after the
latest file on disk, or one calendar year back on first use — `--since` / `--days`), and keeps one
report per year in `cli/report/Index-YYYY.html`: a table per day (ticker · change) of new entrants,
removals and share‑count changes of more than `--min-change` (default 1 %) *net of the fund‑wide
creation/redemption flow*, newest day first, grouped by month, 3–4 columns on a desktop and one on
a phone. Re‑running only adds the missing days (`--rebuild` regenerates, `--no-fetch` skips the
download). `python cli/mock_server.py` serves synthetic CSVs in the iShares layout to try it
offline: `python cli/holdings_report.py --url "http://127.0.0.1:8765/holdings.csv?fund=TEST" --days 60 --pause 0`.

**Published with GitHub Actions** (`.github/workflows/holdings-report.yml`): runs at 07:00, 11:00,
15:00 and 19:00 UTC (and on demand, with an optional rebuild), fetches the missing days, updates the
reports and pushes CSVs + HTML to the `gh-pages` branch, which is the site's state and its source.
One‑time setup: Settings → Pages → Source "Deploy from a branch", branch `gh-pages`, folder `/ (root)`;
the site is then at `https://<user>.github.io/<repo>/` (`index.html` links the yearly reports).

## Run as a service

See `deploy/velocity.service` (systemd): adjust the two paths, `sudo cp … /etc/systemd/system/`,
`sudo systemctl enable --now velocity`, `journalctl -u velocity -f`.
