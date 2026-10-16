# Velocity

Paper‑trades one iShares ETF's daily holdings with a pluggable strategy, 24×7, and serves the
results as web pages. One process, one SQLite file, no external services.

## TL;DR

```bash
pip install -r requirements.txt                 # Python 3.12: requests, pandas, numpy
velocity backfill --months 15                   # once: the fund's holdings history -> data/velocity.db
velocity serve --strategy trend_follow          # forever: poll every 10 min, trade, serve http://127.0.0.1:8787/
```

* **`velocity serve`** is the application. Every `--poll-frequency` minutes (default 10) it fetches
  the fund's holdings CSV; when a new day is stored it runs the strategy's session (signals →
  sizing → paper fills) and records the session, every holding's signal state and the book.
  After downtime it catches up the missed days in order. It logs where it listens:
  `web: listening on http://127.0.0.1:8787/ (host …, port …, hostname …)`.
* **One strategy per run**, settings as flags (`--strategy`, `--param k=v`, `--capital`,
  `--max-positions`, `--max-positions-policy`, `--max-purchase-amount`, `--risk-per-trade`,
  `--rising-priority`); change anything by restarting. If the flags differ from the book's last
  session, `serve` refuses unless `--accept-param-change` (or `velocity resetbook` to start over).
* **One process at a time.** While `serve` runs it holds `data/velocity.lock`; `backfill`,
  `backtest`, `ticker` and `resetbook` are refused until you stop it (Ctrl‑C / SIGTERM — the
  current cycle finishes first). `status` and `strategies` always work.
* **All times are UTC.**

## Commands

| command | what | network |
|---|---|---|
| `serve` | poll · trade · web (`--poll-frequency 10 --host 127.0.0.1 --port 8787 --no-web --once --no-catchup`) | yes |
| `status` | holdings date + when updated, last/next poll, last cycle, books, last fetches | no |
| `backfill` | N months of history (`--months 12 --wipe --force --dry-run`); keeps stored rows, fills gaps | yes |
| `backtest` | replay strategies over the stored history, plain text (`--windows 3,6,9 --start --end --fill --trades`); `--save-book` makes the result the live book | no |
| `ticker T` | one ticker's signal history for a strategy, plain text | no |
| `resetbook` | delete a strategy's book and its session/signal history | no |
| `strategies` | names, parameters, warm‑up | no |

`velocity <command> --help` lists every flag. Bad input → usage message, exit 2, nothing touched.

## Web

| page | content |
|---|---|
| `/` | status strip (holdings date & update time, last/next poll, last cycle), the live book, the latest day, months |
| `/month/YYYY-MM` | a collapsible block per trading day; the current month adds top‑20, rising stars, all signals, sectors |
| `/day/YYYY-MM-DD` | one day |
| `/strategy/NAME` | the book: positions, rising stars, trades, sessions |
| `/strategy/NAME/session/DATE` | one session: exits, entries, not taken, rising stars after, every signal, the settings used |
| `/ticker/T` | signal history under the live strategy (every ticker link goes here) |
| `/status` | poller, books, fetch log, recent sessions |

JSON: `/api/health`, `/api/status`, `/api/dates`, `/api/day/DATE`, `/api/month/YYYY-MM`,
`/api/strategy/NAME`, `/api/strategy/NAME/sessions`, `/api/ticker/T`. Pages are rendered from the
database on request (read‑only) and reload when a cycle completes; historical days show signals
and rising stars as they were.

## Strategies

| name | entry | exit |
|---|---|---|
| `trend_follow` (default) | close > SMA50 rising · 55‑day high (20‑day for new names) · 60‑day ROC top half · no gap day | trailing stop 8–25 % · 20‑day low · 2 closes < SMA50 · 30‑day time stop |
| `range_breakout` | ≤ 10 % base over 20 bars · close breaks the base high · close > SMA50 · no gap day | stop at base low · break‑even + trail at the measured‑move target · 40‑day time stop |
| `double_bottom` | two lows within 3 %, 10–60 bars apart, neckline ≥ 5 % above · close above the neckline | stop 1 % under the lows · target = neckline + height · double top exits · 40‑day time stop |

Volume rules (RVOL/OBV) activate only when OHLCV bars exist in `daily_bars` (`src.market_data.store_bars`).
Engine: one position per issuer (GOOG = GOOGL), `$ = min(cap, equity × risk / stop distance)`,
`--max-positions` with `skip` (never sells to make room) or `replace` (weakest *losing* position,
held ≥ 5 sessions); signals that don't fit wait as **rising stars**. Fills at the signal day's close.
New strategy: subclass `src/strategies/base.py::Strategy`, register in `src/strategies/__init__.py`.

## Data

`data/velocity.db` (SQLite, WAL): `snapshots` / `holdings` / `weekly_closes` (history), `books`,
`sessions` (+ the parameters each ran with), `signals`, `fetch_log`, `meta`. Each session is one
transaction and idempotent per date. A new book starts on the latest date; seed one from history
with `backtest --save-book`.

Another fund: change `portfolioId` in `HOLDINGS_URL` (`src/config.py`), then
`velocity backfill --wipe --force` and `velocity resetbook --strategy …`. Nothing else names the fund.

Coming from the old per‑ETF layout (`data/<ETF>.db` + `data/books.db`):
`python scripts/migrate_single_db.py data/<ETF>.db` copies the holdings history into
`data/velocity.db`; books start fresh.

## Run as a service

```ini
# /etc/systemd/system/velocity.service
[Unit]
Description=Velocity
After=network-online.target
[Service]
WorkingDirectory=/home/you/velocity
ExecStart=/home/you/velocity/.venv/bin/python velocity.py serve --strategy trend_follow
Restart=always
RestartSec=10
TimeoutStopSec=60
[Install]
WantedBy=multi-user.target
```

`python -m pytest tests -q` runs the tests.
