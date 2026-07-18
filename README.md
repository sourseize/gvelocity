# Velocity

Tracks daily holdings for multiple BlackRock factor ETFs -- momentum, quality,
factor-rotation, or any others you add. Detects new entries and exits,
scores 5 mathematical confirmation signals per holding, and publishes a
rolling HTML report per fund plus a hub page linking all of them, via
GitHub Pages.

Each day is a collapsible block in a monthly page, with its own Top-20
holdings table. On the 1st of each month, the current `index.html` is
sealed as `YYYY-MM.html` and a fresh one starts. Every day is permanently
archived -- you never miss a signal.

**Confirmation score (0-5)** -- one point per signal:

| # | Signal | What it checks |
|---|---|---|
| 1 | Price ROC (20d) > 0 | Momentum still running post-inclusion |
| 2 | Weight growing since entry | ETF adding conviction |
| 3 | Weight > 50th percentile | Meaningful position, not a token entry |
| 4 | Price above entry price | Not fading post-inclusion |
| 5 | Within 20% of 52-week high | Not overextended |

**4-5 = act, 3 = watch, 0-2 = wait**


---

## Layout on disk

```
velocity/
|-- velocity.py             <- daily runner (fetch, score, trade, report)
|-- velocity.sh             <- cron wrapper (loops all ETFs, commits, pushes)
|-- backfill.py             <- DEPRECATED: redirects to cli/backfill.py
|-- ticker.py               <- DEPRECATED: redirects to cli/ticker.py
|-- cli/
|   |-- backfill.py         <- historical backfill (manual)
|   |-- ticker.py           <- per-ticker score history (manual)
|   `-- report_rebuild.py   <- rebuild HTML from DB (manual)
|-- src/
|   |-- config.py           <- ETF config, scoring constants
|   |-- database.py         <- SQLite operations
|   |-- analyzer.py         <- metrics computation, scoring logic
|   |-- reporter.py         <- HTML generation
|   |-- fetcher.py          <- CSV fetching and parsing
|   |-- trader.py           <- Portfolio, Position, Trade classes
|   `-- data/
|       |-- MTUM.db         <- one SQLite DB per ETF
|       |-- QUAL.db
|       `-- DYNF.db
`-- report/                 <- separate git checkout, `report` branch,
                                   published via GitHub Pages
    |-- index.html          <- hub page: links + daily summary per ETF
    |-- MTUM/
    |   |-- index.html      <- current month, always up to date
    |   |-- 2026-06.html    <- sealed previous months
    |   |-- 2026-06.json
    |   `-- tickers/
    |       |-- index.html  <- tracked-ticker dashboard for this ETF
    |       |-- manifest.json
    |       `-- NVDA.html   <- per-ticker score-history chart
    |-- QUAL/  (same structure)
    `-- DYNF/  (same structure)
```

`src/` and `report/` are siblings -- `report/` is **not** nested inside
`src/`. This matters because `DEFAULT_REPORT_DIR` in `velocity.py` is
computed as `Path(__file__).parent.parent / "report"`, i.e. relative to
wherever `velocity.py` itself lives.

---

## Prerequisites

- Linux server (or your own machine) with Python 3.9+
- `pip3 install requests`
- GitHub account, with `report/` set up as its own checkout on a `report`
  branch (GitHub Pages serves that branch)

---

---

## Command Reference

### Daily Run: `velocity.py`

**Purpose:** Fetch today's holdings, score positions, execute paper trades, generate reports.
**When:** Daily cron (weekdays), or manual test runs.

```
python velocity.py --etf {MTUM|QUAL|DYNF|ALL} [options]
```

| Flag | Required? | Default | Purpose |
|---|---|---|---|
| `--etf` | **Yes** | none | Which fund to run, or `ALL` for all configured funds |
| `--db PATH` | No | `data/{ETF}.db` | Override DB location (single ETF only) |
| `--report-dir PATH` | No | `velocity/report` | Shared report root |
| `--mock-csv PATH` | No | none | Use local CSV instead of fetching (testing only) |
| `--initial-capital` | No | `100000` | Starting cash for paper trading |
| `--allocation-pct` | No | `0.10` | Portfolio % per trade (10% = 0.10) |
| `--max-purchase-amount` | No | `1000` | Max $ per ticker |
| `--trail-percent` | No | `0.15` | Trailing stop loss (15% = 0.15) |
| `--no-paper-trade` | No | off | Disable paper trading |
| `--reset-portfolio` | No | off | Reset portfolio state on this run |

```bash
# Daily run, single fund
python velocity.py --etf MTUM

# Daily run, all funds
python velocity.py --etf ALL

# Test with local CSV (no network)
python velocity.py --etf MTUM --mock-csv /tmp/MTUM_holdings.csv

# Run without paper trading (report only)
python velocity.py --etf MTUM --no-paper-trade
```

---

### Manual CLI Tools

These are **one-off operations** -- not part of daily cron.

#### `cli/backfill.py` -- Historical backfill

Populates database with N months of historical data, analyzing each date
in chronological order so new-entry/exit detection works correctly.

```
python cli/backfill.py --etf {MTUM|QUAL|DYNF|ALL} [options]
```

| Flag | Required? | Default | Purpose |
|---|---|---|---|
| `--etf` | **Yes** | none | Which fund to backfill, or `ALL` |
| `--db PATH` | No | `data/{ETF}.db` | Override DB location (single ETF only) |
| `--report-dir PATH` | No | `velocity/report` | Shared report root |
| `--months N` | No | `12` | How many trailing months |
| `--end-date YYYY-MM-DD` | No | today | End of range (auto-clamped to today-2days) |
| `--sleep SECONDS` | No | `1.5` | Delay between requests |
| `--no-wipe` | No | off | Keep existing data, fill gaps only |
| `--dry-run` | No | off | Print date list, no network calls |

```bash
# Backfill one fund, last 12 months (fresh start)
python cli/backfill.py --etf MTUM

# Backfill all funds at once
python cli/backfill.py --etf ALL

# Just 6 months for a newer fund
python cli/backfill.py --etf DYNF --months 6

# Preview dates before committing
python cli/backfill.py --etf QUAL --dry-run

# Fill gaps without wiping existing data
python cli/backfill.py --etf MTUM --no-wipe
```

**Why backfill can't bulk-fetch:** New-entry/exit detection looks at "the N
most recent dates in the snapshots table" -- not "on or before the date
being analyzed." This is fine for daily cron (today IS the most recent
row), but for backfill it means each day must be fetched, stored, and
analyzed immediately in strict chronological order.

---

#### `cli/report_rebuild.py` -- Rebuild HTML from database

Regenerates all HTML pages from existing database data without fetching.
Use after tweaking report templates or when you need to regenerate reports.

```
python cli/report_rebuild.py --etf {MTUM|QUAL|DYNF|ALL} [options]
```

| Flag | Required? | Default | Purpose |
|---|---|---|---|
| `--etf` | **Yes** | none | Which fund to rebuild, or `ALL` |
| `--db PATH` | No | `data/{ETF}.db` | Override DB location (single ETF only) |
| `--report-dir PATH` | No | `velocity/report` | Shared report root |

```bash
# Rebuild one fund from existing DB
python cli/report_rebuild.py --etf MTUM

# Rebuild all funds
python cli/report_rebuild.py --etf ALL
```

---

#### `cli/ticker.py` -- Per-ticker score history

Replays `compute_metrics()` for every date a ticker has appeared,
generating a score-history HTML page and updating the tickers index.

```
python cli/ticker.py TICKER --etf {MTUM|QUAL|DYNF} [options]
```

| Flag | Required? | Default | Purpose |
|---|---|---|---|
| `TICKER` | **Yes** | none | Ticker symbol to analyze |
| `--etf` | **Yes** | none | Which fund's DB (no `ALL` -- one ticker, one fund) |
| `--db PATH` | No | `data/{ETF}.db` | Override DB location |
| `--report-dir PATH` | No | `velocity/report` | Determines `{ETF}/tickers/` location |
| `--html PATH` | No | `{ETF}/tickers/{TICKER}.html` | Custom output path |

```bash
# Generate score history for NVDA in MTUM
python cli/ticker.py NVDA --etf MTUM

# Custom output path (skips index update)
python cli/ticker.py AAPL --etf QUAL --html /tmp/AAPL.html
```

Output includes:
- `{ETF}/tickers/{TICKER}.html` -- score history table
- `{ETF}/tickers/index.html` -- list of all tracked tickers
- `{ETF}/tickers/manifest.json` -- metadata for tickers index

---

## Paper Trading

Velocity includes a built-in paper-trading simulator that lets you test
the scoring-based strategy on historical or live data.

### How it works

- A separate `Portfolio` object is maintained for each ETF (stored in
  `src/data/{ETF}.db` for holdings and `{report_dir}/{ETF}/portfolio.json`
  for state).
- At the end of each daily run, the script evaluates every scored holding:
  - **BUY** when the confirmation score reaches **4 or 5**.
  - **SELL** when the score drops to **3 or below**.
  - A **15% trailing stop-loss** (configurable) protects open positions.
- Position size is configurable (default 10% of the portfolio per trade).
- The portfolio is persisted after each run, so you can stop and resume.

### Command-line flags

| Flag | Default | Purpose |
|---|---|---|
| `--initial-capital FLOAT` | `100000.0` | Starting cash per ETF |
| `--allocation-pct FLOAT` | `0.10` | Portfolio fraction per trade (0.10 = 10%) |
| `--trail-percent FLOAT` | `0.15` | Trailing stop loss (0.15 = 15%) |
| `--no-paper-trade` | off | Disable paper trading |

### Example usage

```bash
# Run live trading with $50k starting budget, 15% position size
python velocity.py --etf MTUM --initial-capital 50000 --allocation-pct 0.15

# Run daily with paper trading disabled (report generation only)
python velocity.py --etf MTUM --no-paper-trade
```

### Viewing results

After each run:
- `{report_dir}/{ETF}/portfolio.json` contains cash, positions, trade
  history, and performance metrics.
- `{report_dir}/{ETF}/portfolio.html` is a human-readable HTML report.
- Console output shows summary lines:
  ```
  [MTUM] Portfolio value: $102,345.67 (Return: +2.35%)
  ```

### Notes

- Paper trading is **enabled by default** in `velocity.py`.
- Each ETF maintains an **independent** portfolio.
- The simulator uses **closing prices** from the daily snapshot.
- Orders execute at end-of-day, mirroring how real signals would be acted on.

---

## Adding or Changing a Tracked ETF

Everything about a fund lives in one place: `ETF_CONFIG` in
`src/config.py`.

```python
ETF_CONFIG = {
    "MTUM": {
        "name": "iShares MSCI USA Momentum Factor ETF",
        "url": "https://www.blackrock.com/varnish-api/...portfolioId=251614...",
    },
    ...
}
```

All tracked funds use the **BlackRock varnish-api**. Store each base URL
**without** `asOfDate`: live runs retrieve the latest available holdings and
`backfill.py` appends `&asOfDate=YYYYMMDD` for historical runs.

Before trusting a new fund's URL in cron, sanity-check it manually:

```bash
curl -s "URL_HERE" | head -20
```

The parser tolerates cross-fund variation already (different preamble
lengths, quoted vs. unquoted header rows, several date formats,
extra/missing columns). If a new fund fails, `parse_csv` raises a
`ValueError` naming the problem rather than silently corrupting data.
A `NoDataForDateError` is expected/benign (weekend, holiday, not
published yet).

---

## Typical Sequences

### Brand new fund, from scratch
```bash
# 1. Backfill 12 months of history
python cli/backfill.py --etf MTUM --months 12

# 2. Run today live (catches up to current date)
python velocity.py --etf MTUM
```

### Full fresh rebuild (destructive)
```bash
# Delete all DBs
rm data/*.db

# Clean report directory
cd report && git rm -rf --ignore-unmatch '*.html' '*.json' && rm -rf tickers

# Backfill everything
python cli/backfill.py --etf ALL

# Push to GitHub Pages
cd report && git add . && git commit -m "Full rebuild" && git push origin report
```

### Daily cron (via velocity.sh)
```
0 20 * * 1-5 $HOME/velocity/velocity.sh >> $HOME/velocity/logs/velocity.log 2>&1
```

---

## Known Caveats

- **BlackRock's live endpoint is unverified end-to-end.** The holdings CSV
  structure is confirmed against a real sample, but headers, anti-bot
  behaviour, and whether omitting `asOfDate` returns "latest" have not been
  tested live. If it 403s, try changing the `Referer` header in
  `FETCH_HEADERS` first.

- **BlackRock publishes with a ~2-day lag.** `backfill.py` clamps its end
  date automatically; don't request `asOfDate` for today or yesterday.

- **New date/header formats can still appear.** The parser tries several
  known variants and fails loudly (`ValueError`) rather than guessing --
  treat that as a signal to inspect the raw CSV, not a bug to route
  around.
