# Velocity

Tracks the daily holdings of BlackRock factor ETFs (MTUM, QUAL, DYNF — add more in
`src/config.py`), and runs **one pluggable trading strategy per session** against a single
global position book. Each day the strategy selected on the command line raises the buy/sell
signals; the engine sizes, caps and books them; the HTML report shows holdings changes and
the strategy's signal state for every holding.

```
python velocity.py --etf ALL --paper-trade --strategy trend_follow
```

---
## Strategies

| name | label | entry | risk / exit |
|---|---|---|---|
| `trend_follow` (default) | Trend Follower | close > SMA50 with SMA50 rising · 55‑day closing high (20‑day for names just added to an ETF) · 60‑day ROC in the top half of the universe · not a gap day · [RVOL ≥ 1.5 & OBV rising] | volatility‑scaled trailing stop (2.5σ√10, 8–25 %) · 20‑day low · 2 closes < SMA50 · [distribution day] · 30‑day time stop |
| `range_breakout` | Range Breakout | previous 20 bars form a base ≤ 10 % wide · close breaks the base high · close > SMA50 · not a gap day · [RVOL ≥ 1.5] | hard stop at the base low · at the measured‑move target (2× base height) the stop moves to break‑even and a trailing stop takes over · 40‑day time stop |
| `double_bottom` | Double Top / Bottom | two swing lows within 3 %, 10–60 bars apart, neckline ≥ 5 % above · first close above the neckline within 15 bars · not a gap day · [RVOL ≥ 1.3] | stop 1 % under the lows · target = neckline + pattern height · a completed **double top** on a held name exits it · 40‑day time stop |

Rules in [brackets] need bars with volume (see *Market data*); without them the strategy
runs in price‑only mode and says so in the log.

```bash
python velocity.py --list-strategies                       # names, labels, parameters
python velocity.py --etf ALL --paper-trade --strategy range_breakout
python velocity.py --etf ALL --paper-trade --strategy trend_follow --param breakout_len=40 --param rs_min_pct=0.6
```

### Writing a strategy

Subclass `src.strategies.base.Strategy`, implement `prepare()` (compute indicators once,
vectorised), `entries()` (ranked `EntrySignal`s with stop / trail / target), optionally
`exits()` / `manage()`, and `explain()` for the report; register the class in
`src/strategies/__init__.py::REGISTRY`. The engine supplies everything else: mark‑to‑market,
trailing‑stop ratchet, stop / target / time‑stop / dropped‑from‑ETF exits, re‑entry cooldown,
position limit, sizing and fills.

---

## Engine: book, sizing and the position limit

* **One book per strategy, across all ETFs.** An issuer (GOOG = GOOGL) can be held once,
  whichever ETFs contain it. State: `data/books.db`, table `books` (single source of truth);
  `report/strategy/{name}.json|html` are derived.
* **Sizing:** `$ = min(--max-purchase-amount, equity × --risk-per-trade / stop distance)` —
  the $1,000 cap is hard; tighter stops allow the full cap, wide stops get less.
* **`--max-positions N` (default 25) and `--max-positions-policy`:**
  * `skip` (default): when the book is full no new position is opened. Today's candidates are
    ranked by the strategy (`EntrySignal.rank`: relative strength for the trend follower, base
    tightness for range breakouts, pattern depth for double bottoms); only the best fill the
    free slots, the rest are logged as *"not taken (book full)"* and may re‑qualify tomorrow.
    **Nothing is ever sold to make room** — only the strategy's exit rules, the stop, the target,
    the time stop or removal from all ETFs close a position.
  * `replace`: a candidate that does not fit may replace the *weakest* open position — the one
    with the lowest return since entry — but only if that position is **losing**, has been held
    at least `min_hold` (5) sessions, and the trade is logged as `replaced by <ticker>`. Winners
    are never displaced.
* **Rising stars — where signals go when they can't be taken.** An entry signal that doesn't
  fit (book full, or not enough cash) is parked in the book's *rising stars* watchlist with the
  date it first signalled and the number of sessions it has kept signalling. It is shown on the
  strategy book page, on each ETF page (names in that ETF) and on the hub; it drops off once the
  setup stops firing for `rising_max_age` (10) sessions, or when it is finally bought. With
  `--rising-priority` the most persistent stars are taken first when a slot frees up; by default
  the strategy's own rank decides (see the comparison — priority helps the pattern strategies
  a little and hurts the trend follower, which is better off buying the *freshest* breakout).
* **Fills:** the live session books today's close as the reference price and (with
  `--alpaca-trade`) submits DAY market orders for the next open; backtests default to the next
  session's close (`--fill next`).

---

## Daily run

```bash
python velocity.py --etf ALL --paper-trade                      # fetch, session, reports
python velocity.py --etf ALL --paper-trade --alpaca-trade       # + submit orders to Alpaca
python velocity.py --etf MTUM --no-trade                        # report only, no session
python velocity.py --etf ALL --paper-trade --reset-book         # start the strategy's book over
```

| Flag | Default | Purpose |
|---|---|---|
| `--etf` | — | ETF to fetch, or `ALL` |
| `--strategy` | `trend_follow` | strategy for this session |
| `--param K=V` | — | override a strategy parameter (repeatable) |
| `--capital` | 100000 | book capital when first created |
| `--max-purchase-amount` | 1000 | hard $ cap per issuer |
| `--max-positions` | 25 | open positions per book |
| `--max-positions-policy` | `skip` | `skip` or `replace` (see above) |
| `--rising-priority` | off | waiting rising stars get the next free slot before fresh signals |
| `--risk-per-trade` | 0.0025 | fraction of equity at risk at the initial stop |
| `--paper-trade` / `--no-trade` | no‑trade | execute the session in the local book, or report only |
| `--alpaca-trade` | off | also submit the session's orders to Alpaca (needs `.env`) |
| `--reset-book` | off | delete this strategy's book |
| `--db`, `--report-dir`, `--mock-csv` | | as before |

The session runs on the latest date **every** tracked ETF has a snapshot for and is idempotent
per date, so `velocity.sh`'s per‑ETF loop is safe: the first invocation that completes the set
trades, the others skip.

Cron (`velocity.sh`): `VELOCITY_STRATEGY=range_breakout` selects the strategy.

---

## Backtesting and comparing strategies

```bash
python cli/backtest.py                                   # all strategies, trailing 3/6/9-month windows
python cli/backtest.py --strategy trend_follow --windows 6 --trades
python cli/backtest.py --start 2026-02-13 --end 2026-08-13 --fill same
python cli/backtest.py --max-positions 40 --max-positions-policy replace
python cli/backtest.py --rising-priority on
python cli/backtest.py --out analysis/strategy_comparison.md   # + .csv (trades) + .png (P&L curves)
```

All strategies get identical bars, capital, caps, limits and commissions; every book is marked
to market at the window's last date. Latest results: `analysis/STRATEGY_COMPARISON.md`.

---

## Market data

Strategies run on a split‑adjusted close series built from the BlackRock holdings files
(no volume). To enable the volume rules store real daily bars:

```python
from src.market_data import fetch_daily_bars, store_bars
bars = fetch_daily_bars(tickers, start=date(2025, 9, 1), api_key=..., secret_key=...)  # Alpaca Market Data, IEX feed
store_bars(sqlite3.connect("data/books.db"), bars)      # table daily_bars — picked up automatically
```

---

## Databases

* `data/{ETF}.db` — `snapshots`, `holdings`, `weekly_closes`. A weekly close is the **last
  snapshot of the ISO week** (a Friday holiday no longer loses the week).
* `data/books.db` — `books` (positions, trades, equity, rising stars; one row per strategy),
  `sessions` (what each session did: entries / exits / skipped / equity), `signals` (every
  ticker's ENTRY / HOLD / WATCH state per session — queryable history without recomputation),
  optional `daily_bars`.

```bash
python cli/backfill.py --etf ALL --months 12             # historical holdings
python cli/report_rebuild.py --etf ALL [--strategy X]    # rebuild HTML with a strategy's signal states
python cli/ticker.py NVDA --strategy double_bottom       # one ticker's signal history
python -m pytest tests -q
```

## Prerequisites

Python 3.9+, `pip install -r requirements.txt` (requests, pandas, numpy, tabulate, alpaca‑py).
