# Velocity → 24×7 paper‑trading web app — round 1 plan (v2, simplified scope)

> Progress: Step 0 ✅ · Step 1 ✅ · Step 2 ✅ (absorbed into Step 0) · Step 3 ✅ · Step 4 ✅ · Step 5 ⏳ · Step 6 · Step 7
> Decisions since v2: paper trade only; no `rebuild`, no `fetch`; single fund identified only by
> `HOLDINGS_URL`; one DB file; settings are CLI flags only; strict lock (any DB verb refused while
> `serve` runs); poll every 10 min round the clock; web on localhost; backtest = plain text.

Scope changes vs. v1 (`PLAN-webapp.md`):
* **Paper trade only.** Everything Alpaca is removed (client, `--alpaca-trade`, order outbox,
  reconciliation, `.env` keys, `alpaca-py` dependency).
* **No `rebuild`.** Every page is computed from the database.
* **Offline CLI.** The server is the only process that writes while it runs. To use a CLI command
  you stop the server first. One lock file enforces this; no admin API, no dual‑mode, no
  maintenance lock, no `data_version` watcher in round 1.
* **Single ETF: DYNF.** No `--etf`, no "latest common date across ETFs", no per‑ETF DB files,
  no multi‑ETF universe merging. MTUM/QUAL data is archived, not deleted.

---

## 1. End state after round 1

```
velocity serve                      ← systemd, 24×7
   ├─ poller thread  : every N min fetch DYNF holdings → store if new date →
   │                   run the session for each active strategy (catch‑up in order) →
   │                   refresh in‑memory MarketState
   └─ web (FastAPI)  : HTML pages (Jinja2, existing theme) + JSON API, read‑only connections

velocity fetch | backfill | backtest [--save-book] | ticker | strategies | resetbook | trading | status
   ← run only when the server is down (lock file); same service code as the poller
```

Data: **one file `data/velocity.db`** (holdings tables + books/sessions/signals + new tables),
WAL mode — *pending your answer to Q1; otherwise `DYNF.db` + `books.db` stay as two files.*

---

## 2. Work breakdown, in build order

Each step leaves the repo working and the tests green; steps 1–4 can ship while the old cron still
runs (they don't change trading behaviour).

### Step 0 — Scope cut (½ day)
* Delete: `src/alpaca_client.py`, `src/trader.py` (dead), `src/commands/rebuild.py`,
  `velocity.sh`, `report/` (stale artefacts), `data/MTUM.db`, `data/QUAL.db` → moved to
  `data/archive/` (not committed).
* Remove from code: `--alpaca-trade`, `submit_to_alpaca`, `get_alpaca_client`, `ALPACA_*` in
  `config.py`, `fetch_daily_bars` (Alpaca market data) in `market_data.py` — the `daily_bars`
  table + `load_bars/store_bars` stay provider‑agnostic so volume bars can be added later.
* `ETF_CONFIG` → single `ETF = {"code": "DYNF", "name": ..., "url": ...}`; remove `--etf`, `--db`,
  `--report-dir`, `--html`, `--mock-csv` stays (testing). Legacy `velocity.py --etf ALL
  --paper-trade` keeps mapping to `trading` but `--etf` is accepted‑and‑ignored with a note for
  one release (old cron lines won't crash).
* `requirements.txt`: drop `alpaca-py`; add `fastapi`, `uvicorn[standard]`, `jinja2`.
* Tests: update `test_cli.py` expectations (no `rebuild`, no `--etf`, no `--alpaca-trade`).

### Step 1 — Database foundation (1 day)
* `database.connect(path, readonly=False)`: `journal_mode=WAL` (once), `busy_timeout=5000`,
  `synchronous=NORMAL`, `row_factory=Row`, URI `mode=ro` for readers. Replace every
  `sqlite3.connect`/`get_db` call.
* `meta` table: `schema_version`, `last_cycle_at`, `last_cycle_status`, `last_session_date`.
* **As‑of‑date fix**: `recent_dates(conn, n, asof)`, `daily_history`, `weekly_history`,
  `detect_new_entries`, `detect_weight_movers`, `compute_metrics` (`first_seen`, `days_in_etf`)
  all bounded by `<= date`. Regression test: `build_day_data(conn, d)` for the days in
  `report/DYNF/2026‑09.json` (kept as a test fixture) must reproduce new‑entries/exits/movers.
* **Atomic session write**: `Book.save(conn, commit=False)`; new
  `services/session.py::persist(conn, strategy, date, result, book)` does `BEGIN IMMEDIATE` →
  `log_session` → `log_signals` → `book.save` → `COMMIT`.
* New columns/tables: `sessions.params_json`, `sessions.engine_json`, `sessions.rising_json`,
  `sessions.kind ('live'|'catchup'|'backtest')`; `fetch_log(attempted_at, outcome, holdings_date,
  http_status, ms, message)`; `backtests(id, ran_at, strategy, command, settings_json,
  windows_json, results_json, trades_json, equity_json, saved_book)`; `book_backups(id, strategy,
  taken_at, reason, book_json)` (replaces `data/book-backups/*.json`).
* (If Q1 = yes) `scripts/migrate_single_db.py`: copies `DYNF.db` tables + `books.db` tables into
  `data/velocity.db`, sets `schema_version`, leaves the old files in place until you delete them.

### Step 2 — Single‑ETF engine simplification (½ day)
* `trading.session_date()` → `MAX(fetch_date)`.
* `Universe(conn, dates)` over one DB; `Engine(strategy, conn, ...)`. Issuer aliasing
  (GOOG/GOOGL) kept — it still matters inside one fund.
* Exit reason "dropped from all tracked ETFs" → "dropped from DYNF".
* `Position.etfs` / `Trade.etfs` / rising `etfs` kept in the JSON (always `["DYNF"]`) so existing
  book rows load unchanged; no longer shown in the UI.
* `load_market_bars(conn)`: holdings prices from the single DB (+ `daily_bars` if present).

### Step 3 — Service layer + settings (1 day)
* `src/services/holdings.py` (`fetch_and_store(conn, mock_csv=None) -> FetchResult`, writes
  `fetch_log`), `services/session.py` (`run_due_sessions(conn, settings) -> list[SessionResult]`:
  for each active strategy, every date with `book.last_date < d <= MAX(fetch_date)` in order —
  catch‑up is just "more than one date"), `services/books.py` (load/reset‑with‑backup/
  register‑backtest), `services/backtest.py` (run + persist to `backtests`),
  `services/settings.py`.
* **Settings file `velocity.toml`** (*pending Q4*): `[poller]` interval/window, `[engine]`
  max_positions/policy/max_purchase_amount/risk_per_trade/capital/rising_priority,
  `[strategies] active = [...]`, `[strategy.trend_follow] params…`, `[web] host/port`.
  The daemon and `velocity trading` both read it; CLI flags override for that invocation and the
  parameters actually used are recorded in `sessions.params_json`.
* Commands become thin shells: `trading` = fetch → `run_due_sessions` → print summary (no
  reports); `fetch`, `backfill` (store only — no page generation; the "analyse oldest‑first"
  constraint is gone thanks to Step 1), `backtest` (persist + print; `--save-book` via
  `services/books.register_backtest`), `resetbook`, `ticker` (print only), `strategies`.

### Step 4 — Offline‑mode lock + `status` (½ day)
* `data/.velocity.lock` (`fcntl.flock`). `velocity serve` holds it for its lifetime; every CLI
  verb except `strategies`/`status` tries to take it non‑blocking and exits 1 with
  "velocity serve is running (pid N, since …) — stop it first" if it can't. Read‑only verbs
  (`backtest` without `--save-book`, `ticker`) — *Q6: refuse too, or allow?*
* The lock file records pid/start time; a dead pid → stale lock is taken over with a warning.
* Second `velocity serve` → refused the same way.
* `velocity status`: works with or without the server (reads `meta`, `fetch_log`, books):
  latest holdings date, last cycle, last session per strategy, open positions/equity, recent
  fetch outcomes, whether the server is running.

### Step 5 — Scheduler / poller (1 day)
* `src/scheduler.py`: one thread, `cycle()` = fetch → `run_due_sessions` → rebuild
  `MarketState` (bars, `Indicators`, prepared strategies, books; ≈2 s) → update `meta` → notify
  SSE listeners. Runs at start‑up and on the timer.
* Cadence (*Q5*): every 15 min 16:00–23:59 America/New_York on weekdays, hourly otherwise; once
  today's holdings date is stored, no more fetches that day; exponential backoff per HTTP
  error/HTML‑anti‑bot response (max 4 h); jitter ±60 s. All outcomes in `fetch_log`.
* Staleness warning in `status`/UI when no new date for 3 weekdays.
* Crash safety: the session transaction is atomic and `run_session` is idempotent per date, so
  kill ‑9 at any point leaves a consistent DB and the next cycle resumes.
* `velocity serve --no-web` for a soak run before the UI exists.

### Step 6 — Web application (2–3 days)
* `src/web/app.py` (FastAPI + uvicorn, in the same process as the scheduler), read‑only
  connections per request, `MarketState` shared by reference (immutable swap).
* Pages (Jinja2; CSS/markup ported from `reporter.py`, which is then deleted):
  `/` hub (latest day, books summary, poller status strip) · `/month/{YYYY-MM}` and `/day/{date}`
  (day blocks as today, any historical date) · `/strategy/{name}` (book: stats, positions, rising
  stars, all trades paginated, equity chart, sessions list with params) ·
  `/strategy/{name}/session/{date}` · `/ticker/{T}?strategy=` · `/backtests`, `/backtests/{id}` ·
  `/status` (fetch log, sessions, lock, log tail).
* JSON API mirroring the old static JSON: `/api/health`, `/api/status`, `/api/dates`,
  `/api/day/{date}` (= `build_day_data`), `/api/month/{ym}` (= old `YYYY-MM.json` shape),
  `/api/strategy/{name}` (= `Book.to_dict`), `/api/strategy/{name}/sessions`,
  `/api/signals?date=&strategy=`, `/api/ticker/{T}`, `/api/backtests[/{id}]`.
  `ETag` = `meta.last_cycle_at` so browser auto‑refresh is cheap; `/events` SSE pushes
  "cycle finished" to open pages.
* No auth in round 1; bind `127.0.0.1` by default (`[web] host` to change).
* Small LRU for `build_day_data` keyed `(date, strategy, last_cycle_at)`.

### Step 7 — Cut‑over, deployment, docs (½ day)
* `deploy/velocity.service` (`Restart=always`, `TimeoutStopSec=30`, `WorkingDirectory`, venv).
* Cut‑over checklist: stop cron → archive MTUM/QUAL → run migration (Q1) → decide books (Q2) →
  `velocity status` → `systemctl start velocity` → open `/`.
* README rewrite (commands, offline rule, settings file, routes).
* Nightly `VACUUM INTO data/backups/` from the scheduler (never `cp` a WAL database).

Total ≈ 7–8 developer days.

---

## 3. Edge cases still in scope (paper‑only, single ETF)

| # | Case | Handling |
|---|---|---|
| 1 | Server down / paused across several publication days | Catch‑up: every missed date in order (stops, time‑stops, cooldowns, rising‑star ageing are per‑session). Sessions tagged `catchup` and shown as such. |
| 2 | Crash mid‑cycle | Atomic session txn + idempotent `run_session`; next cycle redoes only what is missing. |
| 3 | CLI run while server up | Lock → refused with a clear message; no tracebacks, no `database is locked`. |
| 4 | Server started while a CLI write is in progress | Same lock, other direction → `serve` exits 1; systemd retries (`RestartSec`) until the CLI finishes. |
| 5 | `backfill --wipe` | Only offline (lock) so the daemon can never see an empty DB; warm‑up warning kept. |
| 6 | BlackRock republishes a date | `store_snapshot` idempotent; `fetch_log` notes `already(diff)` if the row count differs. |
| 7 | Anti‑bot / HTTP errors at higher polling frequency | Window + jitter + per‑day stop + backoff; visible in `fetch_log` and `status`. |
| 8 | Strategy params changed in `velocity.toml` with an open book | Allowed; recorded in `sessions.params_json`; `status` shows "params changed since last session". |
| 9 | Strategy removed from `active` | Book frozen (positions unmanaged) and shown as "inactive" — distinct from `resetbook`. |
| 10 | Historical day pages | Correct only after the as‑of‑date fix (Step 1), guarded by the fixture test. |
| 11 | Existing books were built on the 3‑ETF universe | See Q2 — must be decided before cut‑over. |
| 12 | Time zones / holidays | Scheduling in America/New_York; sessions are data‑driven; staleness counts weekdays only (no market calendar dependency). |
| 13 | Backups | `VACUUM INTO`, includes WAL content. |
| 14 | Schema change | `schema_version` + start‑up migration; older code refuses a newer schema. |

---

## 4. Clarifications needed before I start

1. **One database file?** Merge `DYNF.db` + `books.db` into `data/velocity.db` (one WAL, one
   lock, one atomic transaction for holdings + book; migration script provided) — or keep the two
   files? *Recommend: merge.*
2. **Existing books.** `double_bottom` and `range_breakout` (last session 2026‑08‑13) were traded
   on the MTUM+QUAL+DYNF universe; `trend_follow` has sessions but no book row. With DYNF only,
   positions in non‑DYNF names would exit as "dropped" on the first catch‑up session, and ~40
   catch‑up sessions would run. Options: (a) reset all books at cut‑over, (b) re‑register each from
   a DYNF‑only `backtest --save-book` over the stored history, (c) keep as is and let catch‑up
   sort it out. *Recommend (b), or (a) if you want a clean start.*
3. **Which strategies run live?** All three books every cycle (cheap, gives the comparison), or
   only one as the cron did (`trend_follow`)? *Recommend all three, `active` list in settings.*
4. **Settings in a `velocity.toml` file** (read at start; CLI flags override per invocation) —
   OK, or do you prefer env vars / DB table? *Recommend TOML.*
5. **Polling defaults**: 15 min in the 16:00–24:00 ET weekday window, hourly otherwise, stop for
   the day once today's date is stored — OK? Or a flat "every 30 minutes"?
6. **Offline rule strictness**: refuse *every* CLI verb while the server runs (simplest, exactly
   what you described), or allow the read‑only ones (`backtest` without `--save-book`, `ticker`)
   since WAL readers are safe? *Recommend strict for round 1.*
7. **Web exposure**: bind to localhost only (you SSH‑tunnel / reverse‑proxy yourself), or listen
   on the LAN? No authentication in round 1 either way.
8. **Backtest history**: persist runs in the `backtests` table so the UI lists them (recommended),
   or print‑only?
9. **Python version** to target (3.12? I saw both 3.12 and 3.14 bytecode) and is FastAPI +
   uvicorn + Jinja2 acceptable as the three new dependencies?
