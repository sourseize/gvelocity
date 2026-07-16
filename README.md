# Velocity

Tracks daily holdings for multiple iShares factor ETFs -- momentum, quality,
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
|-- src/
|   |-- velocity.py          <- daily/live tracker (main entry point)
|   |-- backfill.py           <- one-off historical populator
|   |-- ticker.py     <- per-ticker score-history tool
|   |-- velocity.sh           <- cron wrapper (loops all ETFs, commits, pushes)
|   `-- data/
|       |-- IWMO.db           <- one SQLite DB per ETF
|       |-- IWQU.db
|       `-- DYNF.db
`-- report/                   <- separate git checkout, `report` branch,
                                   published via GitHub Pages
    |-- index.html             <- hub page: links + daily summary per ETF
    |-- IWMO/
    |   |-- index.html         <- current month, always up to date
    |   |-- 2026-06.html        <- sealed previous months
    |   |-- 2026-06.json
    |   `-- tickers/
    |       |-- index.html     <- tracked-ticker dashboard for this ETF
    |       |-- manifest.json
    |       `-- NVDA.html       <- per-ticker score-history chart
    |-- IWQU/  (same structure)
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

## Adding or changing a tracked ETF

Everything about a fund lives in one place: `ETF_CONFIG` at the top of
`velocity.py`.

```python
ETF_CONFIG = {
    "IWMO": {
        "name": "iShares MSCI World Momentum Factor UCITS ETF",
        "url": "https://www.ishares.com/uk/.../IWMO_holdings...",
    },
    ...
}
```

Two URL shapes have been seen in practice:

1. **ishares.com ajax CSV** (IWMO, IWQU):
   `https://www.ishares.com/{region}/products/{id}/{slug}/{ajax_id}.ajax?fileType=csv&fileName={TICKER}_holdings&dataType=fund`
   The numeric `{ajax_id}` is a shared per-locale resource version, not
   fund-specific -- funds on the same site often share one.

2. **BlackRock varnish-api** (DYNF) -- a completely different domain/API
   shape:
   `https://www.blackrock.com/varnish-api/blk-one01-product-data/product-data/api/v1/get-fund-document?appType=PRODUCT_PAGE&appSubType=ISHARES&targetSite=us-ishares&locale=en_US&portfolioId={id}&userType=individual&component=holdings`
   Store this **without** `asOfDate` (that's for the live/current fetch --
   `backfill.py` appends `&asOfDate=YYYYMMDD` itself for historical runs).

Before trusting a new fund's URL in cron, sanity-check it manually:

```bash
curl -s "URL_HERE" | head -20
```

The parser tolerates a fair amount of cross-fund variation already
(different preamble lengths, quoted vs. unquoted header rows, several
date formats, extra/missing columns like `Shares` vs `Quantity`). If a
brand-new fund still fails, `parse_csv` raises a `ValueError` naming the
problem rather than silently corrupting data -- check the error and adjust
`velocity.py` accordingly. A raised `NoDataForDateError`, by contrast, is
expected/benign (weekend, holiday, not published yet).

Keep `ETFS=(...)` in `velocity.sh` in sync with `ETF_CONFIG`.

---

## Command reference

### `velocity.py` -- daily/live tracker

```
python3 velocity.py --etf {IWMO|IWQU|DYNF|ALL} [options]
```

| Flag | Required? | Default | Purpose |
|---|---|---|---|
| `--etf` | **Yes, always** | none | Which fund to run, or `ALL` to run every configured fund in one invocation |
| `--db PATH` | No | `src/data/{ETF}.db` | Override the DB location. Cannot combine with `--etf ALL`. |
| `--report-dir PATH` | No | `velocity/report` | Shared report root -- each ETF writes its own subfolder here |
| `--report-only` | No | off | Skip fetching, just rebuild HTML from whatever's already in the DB |
| `--mock-csv PATH` | No | none | Use a local CSV file instead of fetching live (for testing) |

```bash
# Run current/live data, one fund
python3 velocity.py --etf IWMO

# Run current/live data, every fund
python3 velocity.py --etf ALL

# Rebuild HTML only, no fetch (e.g. after tweaking templates)
python3 velocity.py --etf IWMO --report-only
python3 velocity.py --etf ALL --report-only

# Test against a saved CSV without touching the network
python3 velocity.py --etf DYNF --mock-csv /tmp/DYNF_holdings.csv
```

### `backfill.py` -- historical populator

```
python3 backfill.py --etf {IWMO|IWQU|DYNF|ALL} [options]
```

| Flag | Required? | Default | Purpose |
|---|---|---|---|
| `--etf` | **Yes, always** | none | Which fund to backfill, or `ALL` for every configured fund |
| `--db PATH` | No | `src/data/{ETF}.db` | Override DB location. Cannot combine with `--etf ALL`. |
| `--report-dir PATH` | No | `velocity/report` | Same shared report root as `velocity.py` |
| `--months N` | No | `12` | How many trailing months to backfill |
| `--end-date YYYY-MM-DD` | No | today | End of range (auto-clamped to `today - 2 days` -- iShares publishes with a lag) |
| `--sleep SECONDS` | No | `1.5` | Delay between requests |
| `--no-wipe` | No | off | Keep existing DB/report rows, just fill gaps, instead of wiping first |
| `--dry-run` | No | off | Print the date list only, no network calls |

```bash
# Backdate one fund, last 12 months, fresh start
python3 backfill.py --etf IWMO

# Backdate everything at once
python3 backfill.py --etf ALL

# Just 6 months, for a newer fund
python3 backfill.py --etf DYNF --months 6

# Preview the date range before committing to it
python3 backfill.py --etf IWQU --dry-run

# Fill in gaps without wiping existing data
python3 backfill.py --etf IWMO --no-wipe
```

**Why backfill can't just bulk-fetch-then-analyze:** new-entry/exit
detection always looks at "the N most recent dates in the whole
snapshots table" -- not "on or before the date being analyzed." That's
fine for live daily cron (today IS the most recent row), but for a
backfill it means every day must be fetched, stored, and analyzed
*immediately*, in strict chronological order, before the next day's
snapshot goes in. `backfill.py` already does this correctly -- just don't
reimplement the fetch loop without preserving that ordering.

### `ticker.py` -- per-ticker score replay

```
python3 ticker.py TICKER --etf {IWMO|IWQU|DYNF} [options]
```

| Flag | Required? | Default | Purpose |
|---|---|---|---|
| `--etf` | **Yes, always** | none | Which fund's DB this ticker belongs to (no `ALL` -- a ticker belongs to one fund) |
| `--db PATH` | No | `src/data/{ETF}.db` | Override DB location |
| `--report-dir PATH` | No | `velocity/report` | Determines where `{ETF}/tickers/` lives |
| `--html PATH` | No | `{ETF}/tickers/{TICKER}.html` | Write to a custom path instead (skips updating the tickers index) |

```bash
python3 ticker.py NVDA --etf IWMO
```

Replays `compute_metrics()` for every date the ticker has ever appeared,
regardless of whether it was in the top-20 that day -- since the score
isn't otherwise persisted daily. Also updates `{ETF}/tickers/index.html`
and `manifest.json`, so every ticker you've ever checked stays linked
and browsable, with a "last run" timestamp on both the ticker page and
the index row.

---


## Paper Trading

Velocity includes a built‑in paper‑trading simulator that lets you test the scoring‑based strategy on historical or live data without risking real capital.

### How it works

* A separate `Portfolio` object is maintained for each ETF (stored in `src/data/{ETF}.db` for holdings and `{report_dir}/{ETF}/portfolio.json` for state).
* At the end of each daily run, the script evaluates every scored holding:
  * **BUY** when the confirmation score reaches **4 or 5**.
  * **SELL** when the score drops to **3 or below**.
  * A **15 % trailing stop‑loss** (configurable) protects open positions.
* Position size is configurable (default 10 % of the portfolio per trade).
* The portfolio is persisted after each run, so you can stop and resume the simulation.

### Command‑line flags

| Flag | Default | Purpose |
|------|---------|---------|
| `--initial-capital FLOAT` | `100000.0` | Starting cash for the paper‑trading portfolio (per ETF). |
| `--allocation-pct FLOAT` | `0.10` | Fraction of the portfolio to allocate to each new trade (e.g., `0.10` = 10 %). |
| `--trail-percent FLOAT` | `0.15` | Trailing stop‑loss percentage (e.g., `0.15` = 15 %). |
| `--no-paper-trade` | `off` | Disable the paper‑trading engine (useful for report‑only runs). |

### Example usage

```bash
# Run live trading with a $50 k starting budget and 15 % position size
python3 velocity.py --etf IWMO --initial-capital 50000 --allocation-pct 0.15

# Run a backfill with paper trading enabled (the default)
python3 backfill.py --etf IWMO --months 6

# Generate reports only, no trading
python3 velocity.py --etf IWMO --report-only --no-paper-trade
```

### Viewing results

* After each run, the file `{report_dir}/{ETF}/portfolio.json` contains:
  * `cash`, `positions` (ticker → quantity, entry price, etc.),
  * `total_value`, `total_return_pct`, and a list of all trades.
* The console log shows a summary line at the end of each run, e.g.:
  ```
  [IWMO] Portfolio value: $102,345.67 (Return: +2.35%)
  ```
* You can also inspect the trade history in the JSON file or plug it into your own analysis.

### Notes

* Paper trading is **enabled by default**. Use `--no-paper-trade` to turn it off.
* Each ETF maintains an **independent** portfolio—trades in IWMO do not affect IWQU or DYNF.
* The simulator uses the **closing price** from the day's snapshot for all trades.
* Because the engine runs after the daily analysis, orders are executed at the same day's close (mirroring how a real end‑of‑day signal would be acted on).

## Typical sequences

**Brand new fund, from scratch:**
```bash
cd ~/velocity/src
python3 backfill.py --etf IWQU --months 12   # 1. get history
python3 velocity.py --etf IWQU                # 2. run today live, catch up to now
```

**Full fresh rebuild of everything (destructive -- deletes all DBs and report content first):**
```bash
rm ~/velocity/src/data/*.db
cd ~/velocity/report && git rm -rf --ignore-unmatch '*.html' '*.json' && rm -rf tickers
cd ~/velocity/src
python3 backfill.py --etf ALL
cd ~/velocity/report && git add . && git commit -m "Full rebuild" && git push origin report
```

**Daily cron** (already wired up in `velocity.sh`, loops `ETFS=(IWMO IWQU DYNF)`):
```
0 20 * * 1-5 $HOME/velocity/src/velocity.sh >> $HOME/velocity/logs/velocity.log 2>&1
```

---

## Known caveats

- **DYNF's live endpoint is unverified end-to-end.** Its CSV structure was
  confirmed against a real sample, but the BlackRock `varnish-api` domain
  itself -- headers, anti-bot behaviour, whether omitting `asOfDate` really
  returns "latest" -- hasn't been tested live. If it 403s, try changing
  the `Referer` header in `FETCH_HEADERS` first.
- **iShares publishes with a ~2-day lag.** `backfill.py` clamps its end
  date automatically; don't request `asOfDate` for today or yesterday.
- **New date/header formats can still appear.** The parser tries several
  known variants and fails loudly (`ValueError`) rather than guessing --
  treat that as a signal to inspect the raw CSV, not a bug to route
  around.
