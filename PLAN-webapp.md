# Velocity → 24×7 web application: conversion plan

Status: proposal · based on a full read of the `vc-3` tree (`velocity.py`, `src/`, `velocity.sh`,
`data/*.db`, `report/`).

---

## 0. TL;DR

* Replace the cron‑triggered `velocity.sh` → `velocity trading` → static HTML/JSON → git push → CDN
  pipeline with **one long‑running process** (`velocity serve`) that contains
  (a) a **poller/scheduler** thread that fetches holdings every N minutes, runs the strategy
  session(s) as soon as a new common snapshot date exists, and mirrors orders to Alpaca, and
  (b) a **web server** (FastAPI + Jinja2, JSON API + server‑rendered pages) that reads straight
  from SQLite. Nothing is written to `report/` anymore; `reporter.py`, the month‑log JSON files,
  `rebuild`, `velocity.sh` and GitHub Pages go away.
* SQLite stays, but is switched to **WAL** with a `busy_timeout`, a **single writer** discipline,
  an **app‑level maintenance lock**, and a **`data_version` counter** so the running server notices
  when a CLI command has changed the data underneath it.
* Every CLI verb keeps working. Mutating verbs become **dual‑mode**: if a server is running they are
  forwarded to its local admin API (so the server's in‑memory state, scheduler and order outbox
  stay consistent); if no server is running they execute in‑process exactly as today (headless
  mode), through the same service layer. Read‑only verbs always run in‑process (WAL readers never
  block).
* Five pre‑requisite fixes are needed *before* anything goes 24×7, because they are latent bugs
  that the once‑a‑day cron hides (§3.1): as‑of‑date bugs in `detect_*`, non‑atomic session
  persistence, no Alpaca order record, no DB timeouts, per‑invocation strategy settings.

---

## 1. Where we are (what matters for the conversion)

| Area | Today | Consequence for a 24×7 app |
|---|---|---|
| Trigger | cron → `velocity.sh` loops `velocity trading --etf X --paper-trade` per ETF, one strategy (`VELOCITY_STRATEGY`) | Replaced by an in‑process scheduler. Per‑ETF loop becomes one cycle over all ETFs. |
| Data | `data/{ETF}.db` (snapshots/holdings/weekly_closes, 321 sessions 2025‑07‑07 → 2026‑10‑12), `data/books.db` (books, sessions, signals; optional `daily_bars`) | Kept. |
| SQLite mode | `journal_mode=delete`, no `busy_timeout`, default isolation, many small commits | Two processes touching the DB now fail with `database is locked` immediately; a reader blocks a writer and vice‑versa. Must change (§3.2). |
| Session | `Engine.run_session` on `session_date = min(MAX(fetch_date) per ETF)`; idempotent per date via `book.last_date`; fills at the signal day's close; Alpaca DAY market orders submitted afterwards | Logic is reusable as‑is. The *persistence sequence* is not atomic: `log_session` commit → `log_signals` commit → `book.save` commit → Alpaca `submit_order` (no record). A crash in between leaves inconsistent state (§3.4, §4.5). |
| Reports | `reporter.py` (1 267 lines) writes `report/{ETF}/{YYYY‑MM}.json|html`, `index.html`, `strategy/*.json|html`, `backtest/*.html|json`, `tickers/*.html` + `manifest.json`; the month JSON logs are the **only persisted copy** of per‑day "new entries / exits / movers / top‑20 / rising stars" | All of it is removed. Per‑day data must be derivable from the DB (§3.3) or persisted in a table (§3.5). |
| `detect_new_entries / detect_exits / detect_weight_movers / compute_metrics` | Use "the N most recent dates in the whole table", **not** "dates ≤ the date being analysed" (documented in `backfill.py`'s docstring; verified: for 2026‑06‑15 it reports nearly every holding as "new") | Correct only for the latest date. A web page for any historical day computed from the DB would be wrong → must be made as‑of‑date (§3.1). |
| Strategy settings | CLI flags on every invocation (`--strategy`, `--param`, `--max-positions`, `--risk-per-trade`, `--capital`, …) | A daemon must *remember* them → settings table (§3.5). |
| Engine cost | bars load ≈1.6 s, `Engine()` per strategy 0.4–0.6 s, `build_day_data` per ETF‑day 0.09 s, `signal_rows` 0.04 s (measured on this data) | Cheap enough to recompute on every cycle and to serve most pages on demand; keep a small in‑memory cache of the prepared `Indicators`/strategies. |
| Dead code | `src/trader.py` (old per‑ETF paper trader) is not imported anywhere; `report/` in the repo is stale (n_snapshots 44 vs 321 in DB) | Delete both. |
| Books present | `books`: double_bottom, range_breakout (last session 2026‑08‑13); `sessions` also has trend_follow | Fine — `Book.load` returns an empty book for missing rows. |

---

## 2. Target architecture

```
                         ┌─────────────────────────────────────────────────────────┐
   BlackRock CSV ───────►│  velocity serve  (one process, systemd, Restart=always)  │
   Alpaca (orders,       │                                                         │
   clock, bars) ◄───────►│  ┌───────────────┐   job queue   ┌──────────────────┐   │
                         │  │ Scheduler/    │◄─────────────►│ Web (FastAPI)    │◄──┼── browser (HTML pages)
                         │  │ Worker thread │               │  /        pages  │   │   curl / JS (JSON /api)
                         │  │ (sole writer) │               │  /api     JSON   │◄──┼── velocity CLI (admin API,
                         │  └──────┬────────┘               │  /api/admin (tok)│   │   localhost only)
                         │         │ writes                 └────────┬─────────┘   │
                         │         ▼                                 │ reads (ro)  │
                         │   MarketState cache (bars, Indicators, prepared strategies, books)
                         └─────────┼─────────────────────────────────┼─────────────┘
                                   ▼                                 ▼
                          data/{ETF}.db  data/books.db   (SQLite, WAL, busy_timeout, data_version)
                                   ▲
                                   │ headless mode (no server running): same service layer, in‑process
                          velocity fetch / backfill / backtest --save-book / reset-book / trading
```

### 2.1 Components (new/changed modules)

| Module | Role |
|---|---|
| `src/services/` *(new)* | The only place with business logic that mutates state. `holdings.py` (fetch+store one ETF, `fetch_all`), `session.py` (run sessions for all active strategies, catch‑up, outbox), `books.py` (load/save/reset/register‑backtest), `backtest.py` (run + persist), `settings.py`, `locks.py`, `versions.py`. Both the CLI and the web layer call these; nothing else touches SQLite for writes. |
| `src/scheduler.py` *(new)* | The worker thread: poll loop, job queue (`fetch`, `session`, `cycle`, `reload`, `backtest`, `reset_book`…), maintenance‑lock awareness, backoff, market‑calendar awareness. Single consumer → all writes are serialised in‑process. |
| `src/market_state.py` *(new)* | Read‑mostly cache: bars → `Indicators` → `Universe` → prepared strategies; keyed by `data_version`. Rebuilt by the worker after each write; request handlers only read it. |
| `src/web/` *(new)* | `app.py` (FastAPI), `routes_pages.py`, `routes_api.py`, `routes_admin.py`, `templates/` (Jinja2; port the existing `CSS` + markup from `reporter.py`), `static/`. |
| `src/cli_client.py` *(new)* | Detects a running server (`data/server.json` with pid/port/token; verifies `GET /api/health`) and forwards admin calls; falls back to headless. |
| `src/database.py` | WAL/busy_timeout, `connect(path, readonly=False)`, as‑of‑date fixes, `meta` table (`data_version`, `schema_version`). |
| `src/book.py` | Remove `write_report`/`to_html`; add `save(conn, commit=False)` so the caller owns the transaction. |
| `src/engine.py` | Unchanged logic; `run_session` returns the result and lets the service persist atomically. |
| `src/reporter.py`, `src/day_data.py` | `reporter.py` deleted (CSS and tiny formatters move to `src/web/templates` and `src/web/fmt.py`). `day_data.build_day_data` stays as the per‑ETF‑day payload builder (it becomes the JSON API's payload). |
| `src/commands/*` | Thin argparse shells over services; `rebuild` becomes a deprecated alias; new `serve`, `status`, `pause`, `resume`, `config`, `jobs`, `orders`. |
| `velocity.sh` | Deleted. Replaced by `deploy/velocity.service` (+ optional `deploy/Caddyfile`). |

### 2.2 Process model — why one process

* SQLite has one writer at a time. Putting the scheduler and the web server in **one process** with
  **one worker thread** makes the "single writer" rule structural rather than a convention, and
  lets the web layer share the prepared `Indicators`/strategies in memory (no IPC, no cache
  invalidation between processes).
* Request handlers open **read‑only** connections (`file:…?mode=ro`) → it is impossible for a page
  to accidentally write. Admin endpoints do not write either: they **enqueue jobs** for the worker
  and (optionally) wait for the result.
* The one unavoidable second process is the **CLI**. It is handled by dual‑mode (§5) plus the DB
  guards in §3.2 so that even a "rogue" headless write while the server is up cannot corrupt
  anything — it can only be rejected or noticed.
* Scale is not a concern: one user, three ETFs, ~100 k bar rows, sub‑second computations.
  `uvicorn` with one process; pandas work happens in the worker thread (GIL contention is
  negligible at ~3 s per cycle).

---

## 3. Data layer

### 3.1 Pre‑requisite fixes (do these first; they are bugs today, independent of the web app)

1. **As‑of‑date detection.** `recent_dates(conn, n)` → `recent_dates(conn, n, asof)` with
   `WHERE fetch_date <= ?`. Thread `asof` through `detect_new_entries`, `detect_exits` (already
   as‑of), `detect_weight_movers`, `daily_history`, `weekly_history`, `compute_metrics`
   (`first_seen`, `days_in_etf` must also be ≤ date). Then `build_day_data(conn, date)` is correct
   for *any* date and the web app can render any historical day straight from the DB — which is
   what makes deleting the month‑log JSON files safe. `backfill` no longer needs its
   "analyse immediately after each store, oldest first" constraint.
2. **Atomic session persistence.** One transaction per (strategy, date):
   `BEGIN IMMEDIATE` → `log_session` → `log_signals` → insert order intents into `orders` (§3.5)
   → `book.save` → `COMMIT`. Today these are four separate commits; a crash between them leaves
   `sessions` saying "done" while `books.last_date` says "not yet" (harmless thanks to
   idempotency) or — worse — the book saved but orders never sent with no trace.
3. **Alpaca order outbox** (`orders` table). Intents are written in the session transaction;
   submission happens after commit and updates `status`/`alpaca_id`; on start‑up pending intents
   are re‑submitted if still valid (§4.5). Also record the order *result* — today `submit_order`'s
   return value is discarded.
4. **Connection hygiene.** `PRAGMA journal_mode=WAL` (once, persistent), `PRAGMA busy_timeout=5000`
   (every connection), `PRAGMA synchronous=NORMAL`, `foreign_keys=ON`; all opened through one
   `database.connect()` helper; `get_db()` and the raw `sqlite3.connect(...)` calls in
   `_common.open_book_db`, `backtest.save_book`, `rebuild.load_strategy_context` are replaced.
5. **Settings persistence.** Strategy/engine parameters move from CLI flags to a `settings` store
   (§3.5). The CLI flags remain as one‑off overrides for backtests and for headless runs, but the
   daemon runs from the stored settings and records the parameter snapshot it used on every
   session (`sessions.params_json`).

### 3.2 Concurrency rules

| Rule | Mechanism |
|---|---|
| Readers never block the writer, writer never blocks readers | WAL. Note: WAL requires all processes on the same host/filesystem (true here); the `-wal`/`-shm` side files must be included in backups or, better, backups use `VACUUM INTO` / the backup API (§9). |
| Writers from two processes | Second writer waits up to `busy_timeout` (5 s), then fails with a clear `VelocityError("database busy — is `velocity serve` running a cycle? …")`. Writes are short (ms) so this only happens under the maintenance lock anyway. |
| Long, multi‑step mutations (backfill, `--wipe`, `--save-book`, reset‑book, schema migration) | **Maintenance lock**: row in `meta` (`lock_owner`, `lock_pid`, `lock_reason`, `lock_acquired_at`, `heartbeat_at`) taken with `BEGIN IMMEDIATE`; plus `fcntl.flock` on `data/.maintenance.lock` as belt‑and‑braces. The scheduler checks it at the top of each cycle and **skips the cycle** (status page shows "maintenance: backfill MTUM by pid 1234 since …"). A stale lock (no heartbeat for 10 min and pid dead) is broken automatically with a warning. |
| Server notices external writes | `meta.data_version` (integer) bumped by every mutating service call in the same transaction. The worker compares it every tick (and every request handler compares the cached `MarketState.version`); mismatch → rebuild `MarketState`, re‑read books. Backfill bumps once per stored snapshot (cheap) — the worker debounces rebuilds to at most one per 30 s while the lock is held, and one final rebuild when it is released. |
| Only one server | `data/server.json` (pid, host, port, token, started_at) + `flock` on `data/.server.lock`. A second `velocity serve` exits 1: "already running (pid …)". |
| Single writer inside the server | One worker thread consuming one `queue.Queue`; request handlers never write. |

### 3.3 What gets deleted from persistence

* `report/**` (month JSON logs, HTML, `strategy/*.json`, `backtest/*`, `tickers/*`,
  `manifest.json`), `DEFAULT_REPORT_DIR`, `--report-dir`, `--html`, `etf_report_dir`,
  `rollover_if_needed`, `find_tracked_etf_dirs`, `generate_hub_index`, `Book.write_report`/`to_html`.
* The per‑day payload is recomputed from the DB on request (0.09 s/ETF‑day) **after** fix 3.1‑1.
  To keep pages snappy: an in‑memory LRU keyed by `(etf, date, strategy, data_version)`; the
  worker pre‑warms the latest date after each cycle.
* Information that *only* existed in the month logs and is **not** derivable from the DB:
  the per‑day rising‑star list (only `n_rising` is in `sessions`) and the strategy that was
  active on a given historical day. Add `rising_json` to `sessions` (going forward) and use
  `sessions.strategy` as the record of what ran. Historical rising stars before the cut‑over
  are lost unless we import them once from the existing month JSONs (optional one‑shot
  migration script; recommended since it is ~20 lines).

### 3.4 Session write sequence (new)

```
worker: cycle()
  1. for etf in ETFS: holdings.fetch_store(etf)                 # each its own short txn in data/{ETF}.db
  2. date = session_date(conns)                                 # min(MAX(fetch_date)) — unchanged rule
  3. for strat in settings.active_strategies():
       for d in dates_to_run(book.last_date, date):             # catch‑up, see §4.3
         res = engine.run_session(book, d)                      # pure in‑memory
         with books_db.transaction():                           # BEGIN IMMEDIATE
             log_session(...); log_signals(...)
             outbox.add_intents(strat, d, res, book)            # only if strat is the mirrored one
             book.save(conn, commit=False); bump data_version
  4. outbox.submit_pending()                                    # network; updates orders.status
  5. market_state.rebuild(); notify SSE clients
```

### 3.5 New tables (all in `data/books.db`, created by `_ensure_schema`; `schema_version` in `meta`)

| Table | Columns (abridged) | Purpose |
|---|---|---|
| `meta` | `key TEXT PK, value TEXT` — `schema_version`, `data_version`, `lock_*`, `scheduler_paused`, `last_cycle_at`, `last_cycle_status` | Version/lock/status. |
| `settings` | `scope TEXT, key TEXT, value_json TEXT, updated_at` — scopes `engine`, `strategy:<name>`, `alpaca`, `poller`, `web` | Persistent configuration (§4.1). |
| `orders` | `id, strategy, session_date, ticker, side, qty, ref_price, reason, status (pending/submitted/filled/partially_filled/cancelled/rejected/expired/skipped), alpaca_id, filled_qty, filled_avg_price, created_at, submitted_at, updated_at, error` | Alpaca outbox + reconciliation. |
| `jobs` | `id, kind, args_json, status (queued/running/done/failed/cancelled), requested_by (web/cli/scheduler), created_at, started_at, finished_at, result_json, log_text` | Admin actions & audit; CLI tails `log_text`. |
| `fetch_log` | `etf, attempted_at, outcome (stored/already/no-data/http-error/parse-error/blocked), holdings_date, http_status, ms, message` | Poller observability, backoff decisions, "last successful fetch" on the status page. |
| `backtests` | `id, ran_at, strategy, command, settings_json, windows_json, results_json, trades_json, equity_json, saved_book INTEGER` | Replaces `report/backtest/*.html|json`. |
| `sessions` (+) | add `params_json`, `rising_json`, `engine_json` | Record what configuration produced the session. |
| `book_backups` | `id, strategy, taken_at, reason, book_json` | Replaces `data/book-backups/*.json` (or keep files; table is simpler to list in the UI). |
| `alerts` | `id, at, level, kind, message, acknowledged` | Staleness, fetch failures, rejected orders, lock broken, etc. Shown on the status page; optional webhook/email. |

---

## 4. Scheduler / poller

### 4.1 Settings (stored; editable via `velocity config` and `/admin/settings`)

```
poller.interval_active_min   = 15      # weekdays 16:00–23:59 America/New_York (publication window)
poller.interval_idle_min     = 60      # otherwise (weekends/holidays: 180)
poller.jitter_s              = 60
poller.backoff               = 2x up to 4 h on HTTP 403/429/5xx or HTML‑instead‑of‑CSV
poller.stale_after_days      = 3       # trading days without a new common date → alert
strategies.active            = ["trend_follow"]           # books that get a live session each cycle
strategies.mirrored          = "trend_follow" | null      # exactly one (or none) sends Alpaca orders
strategy:<name>.params       = {...}                      # --param equivalents
engine.max_positions / max_positions_policy / max_purchase_amount / risk_per_trade / rising_priority / capital
alpaca.enabled, alpaca.paper, alpaca.catchup_policy = "latest-only" | "all" | "never"
session.catchup              = true    # run every missed date in order (see §4.3)
```

`--param`, `--max-positions` etc. on `velocity trading` remain as **one‑shot overrides for headless
runs and backtests**; when a server is running, `velocity trading --param x=y` is refused with
"the daemon runs from stored settings; use `velocity config set strategy:trend_follow x=y`" (to
prevent the book being advanced with different parameters from what the daemon will use tomorrow).

### 4.2 Cycle and session trigger

* A **cycle** = fetch all ETFs → compute `session_date` → run due sessions → submit outbox → rebuild
  cache. Cycles run on the timer, on `velocity trading`/`fetch` forwarding, on the admin "Run now"
  button, and once at start‑up.
* **Session trigger rule is unchanged**: the latest date *every* tracked ETF has a snapshot for.
  This rule is load‑bearing: `Universe.on(date)` reads holdings for that exact date per ETF, so
  running a session on a date one ETF has not published yet would make every position held only
  via that ETF look "dropped from all tracked ETFs" and **force‑exit it**. Therefore a lagging ETF
  must *delay* the session, never be skipped. The status page shows per‑ETF latest date and the
  lag so the operator sees *why* no session ran.
* **Staleness**: if no new common date for `stale_after_days` trading days (use Alpaca's calendar
  endpoint — already a dependency — so holidays do not alert), raise an alert. Manual remedy is an
  operator decision (untrack the ETF in config), not an automatic fallback.
* Market‑calendar awareness is for polling cadence and alerting only; the session runs whenever
  data appears, exactly as today.

### 4.3 Catch‑up after downtime

If the server was down (or the maintenance lock held) across several publication days there are
several new dates. Trailing‑stop ratchets, time stops, cooldowns and rising‑star ageing are all
per‑session, so skipping dates silently changes strategy behaviour. Policy:

* `session.catchup = true` (default): run every date in `engine.dates` with
  `book.last_date < d ≤ session_date`, in order, each as a normal `run_session` (same `fill=same`).
  Each is persisted atomically and logged as `sessions.kind = "catchup"`.
* Alpaca mirroring during catch‑up follows `alpaca.catchup_policy`:
  `latest-only` (default — only the last date's entries/exits create order intents; earlier
  catch‑up trades are booked in the paper book and flagged "not mirrored" so the operator can
  reconcile by hand), `all` (every trade becomes an intent; fills will be at today's prices, which
  diverge from the paper book — warn), `never`.
* Catch‑up sessions and un‑mirrored trades are listed on the strategy page and in `velocity status`.

### 4.4 Idempotency & restart safety

* `run_session` already skips when `book.last_date == date`. With atomic persistence, a crash can
  only leave "nothing for date d" or "everything for date d"; the next cycle re‑evaluates.
* Restart steps: acquire server lock → run migrations → check maintenance lock → reconcile outbox
  (§4.5) → build `MarketState` → start web → run one cycle.
* Graceful shutdown (SIGTERM from systemd): stop accepting jobs, finish the current transaction
  (ms), skip network steps that have not started, exit. `TimeoutStopSec=30`.

### 4.5 Alpaca outbox & reconciliation

* Intents are written with the session; `submit_pending()` runs after commit. Each submission
  updates `orders` (`submitted`, `alpaca_id`) or `rejected` (+ error, + alert).
* On start‑up and once per cycle: poll Alpaca for the status of `submitted` orders → `filled` /
  `partially_filled` / `cancelled` / `expired`. `pending` intents whose `session_date` is not the
  most recent session are marked `expired` and raise an alert instead of being sent blind
  (a DAY market order meant for "the next open" two days ago must not be fired now).
* `orders` does **not** feed back into the paper book (the book remains the model, fills at the
  signal‑day close, exactly as today) — but the strategy page shows paper vs. Alpaca fill price per
  trade and the positions view shows paper quantity vs. Alpaca quantity so drift is visible.
* Only `strategies.mirrored` creates intents. Changing the mirrored strategy, `reset-book` or
  `backtest --save-book` on the mirrored strategy **orphans Alpaca positions**: the CLI/UI refuses
  without `--i-understand-alpaca-drift` (name TBD) and logs an alert listing the Alpaca positions
  that no longer correspond to the book. Optional later: `velocity orders flatten`.

### 4.6 Fetch politeness (new risk compared to once‑a‑day)

15‑min polling is ~30–50 requests/ETF/day against blackrock.com vs. 1 today; the `fetch_csv` code
already detects the HTML anti‑bot response. Mitigations: the active window (holdings are
published in the evening ET), jitter, exponential backoff per ETF, stop polling an ETF for the rest
of the day once today's date is stored (there is nothing newer to find), and send
`If-Modified-Since`/`If-None-Match` when the server returns validators (record in `fetch_log` whether
it does). Consider learning the typical publish time from `fetch_log` and tightening the window.

---

## 5. CLI verbs under the new regime

### 5.1 Dual‑mode resolution (`src/cli_client.py`)

```
server = discover()            # data/server.json + GET http://127.0.0.1:<port>/api/health (200, pid alive)
if command is read-only:       run in-process (WAL read never blocks)
elif server is None:           run in-process via services (headless) — takes maintenance lock for long ops
else:                          POST /api/admin/jobs {kind, args}; stream job log; exit with job's rc
```

`--local` forces in‑process even when a server is up (for emergencies; it takes the maintenance
lock so the scheduler pauses, and bumps `data_version`). `--remote` forces the API (errors if no
server). Token: read from `data/server.json` (mode 600) so only the same user can drive the
server; the admin API binds to 127.0.0.1 only.

### 5.2 Verb by verb

| Verb | Server running | No server (headless) | Changes / edge cases |
|---|---|---|---|
| `serve` *(new)* | — | Starts the daemon: `--host --port --no-web` (worker only) `--no-poll` (web only, e.g. read‑only mirror). | Single‑instance lock. systemd unit calls this. |
| `status` *(new)* | `GET /api/status` | Reads `meta`, `fetch_log`, books directly; reports "server: not running". | Shows per‑ETF latest date, session_date, last cycle, lock holder, paused, pending orders, alerts, data_version. Exit 1 if alerts unacknowledged (cron‑friendly). |
| `pause` / `resume` *(new)* | Sets `meta.scheduler_paused`; the worker finishes the current job and idles. | Sets the flag in the DB so the next start comes up paused. | Use before manual surgery. Status page banner. |
| `config get/set/unset` *(new)* | `PATCH /api/admin/settings` → worker applies, bumps version, rebuilds. | Writes `settings` directly. | Validation reuses `Strategy._check_param` / argparse types. Changing params of a strategy with an open book logs an alert "parameters changed mid‑book" and records it in the next `sessions.params_json`. |
| `trading` | Enqueues a `cycle` job and tails it. `--etf` ignored with a note (the daemon always fetches all tracked ETFs — a per‑ETF cycle could never produce a session anyway). `--paper-trade` implied for active strategies; `--no-trade` → `fetch` only + preview (no book mutation); `--strategy/--param/--capital/--max-*` → refused unless `--local` (see §4.1); `--alpaca-trade` → refused, use `config set strategies.mirrored`; `--reset-book` → forwarded as `reset_book` job; `--offline` → run sessions without fetch; `--mock-csv` → uploaded with the job (multipart) for testing. `--report-dir` → removed (warning if passed, for old scripts). | Exactly today's behaviour minus reports; runs from stored settings unless overridden; takes the maintenance lock for the duration so a server started mid‑run cannot interleave. | Legacy `velocity.py --etf ALL --paper-trade` fallback kept. `velocity.sh` deleted; if an old cron line still fires `velocity trading` while the server is up it just triggers a harmless extra cycle (idempotent). |
| `fetch` | `fetch` job (one or all ETFs). | In‑process, short write txns, bumps version. | Unchanged semantics. |
| `backfill` | **Runs in the CLI process** (it is long: months × 1.5 s sleeps) under the maintenance lock. The server skips cycles and shows the banner; on release it rebuilds and runs a cycle. `--wipe` additionally requires the lock (it is already implied) and, if the wiped ETF's history is needed by an open book, prints the warm‑up warning as today. No page regeneration. `--dry-run` unchanged. | Same. | Rationale: streaming a 20‑minute job through the server's queue adds nothing; the lock gives the same safety. Alternative (not recommended): `backfill` job in the server. Backfill inserts *older* dates so `session_date` does not move; but signals/indicators change → the version bump + rebuild handles it. |
| `backtest` | Read‑only: in‑process (CPU‑heavy, fine in a second process thanks to WAL). Result persisted to `backtests` (instead of `report/backtest/*.html|json`) — a small write txn, bumps version so the UI lists it. `--save-book` → forwarded as `register_backtest` job (needs the worker so the live book swap, backup, outbox and cache stay consistent) *or* `--local` under the lock. `--out` (markdown/csv/png) unchanged; `--html` removed — the page is `/backtests/{id}`. | `--save-book` in‑process under the lock. | `--save-book` on the **mirrored** strategy → Alpaca drift guard (§4.5). `--save-book` while a catch‑up is pending → the next cycle runs catch‑up from the backtest's `last_date`, which is the intended semantics. |
| `rebuild` / `report-rebuild` | Deprecated alias: prints "static reports no longer exist — pages are served live at <url>; this command now warms the cache" and enqueues a `reload` job. | Prints the message; no‑op. | Remove in a later release; keep for old muscle memory. |
| `ticker` | Read‑only, in‑process; prints the table as today. `--html` removed; URL of the live page printed instead. | Same. | |
| `strategies` | Unchanged. | Unchanged. | Could additionally show which are active/mirrored. |
| `reset-book` | `reset_book` job (backup → delete → fresh book → bump → rebuild). | In‑process under the lock, with backup. | Mirrored‑strategy guard (§4.5). Removing a strategy from `strategies.active` is *not* a reset — its book simply stops advancing; make this distinction clear in `--help`. |
| `jobs` *(new)* | `GET /api/admin/jobs`, `jobs tail <id>`, `jobs cancel <id>`. | Lists from the `jobs` table. | |
| `orders` *(new)* | `orders list/reconcile/ack`. | Same (reconcile talks to Alpaca directly). | |

### 5.3 What must not happen (and how it's prevented)

| Hazard | Prevention |
|---|---|
| Headless `velocity trading --paper-trade --param x=1` advances the book with different parameters while the daemon runs with others | Refused when a server is detected unless `--local`; `--local` takes the lock and the daemon pauses; parameters are recorded per session so the discrepancy is at least visible. |
| `backfill --wipe` empties an ETF DB while the daemon runs a session on "no holdings" | Lock held for the whole backfill; scheduler skips cycles while locked; additionally the session service refuses to run if any ETF has < `strategy.warmup()` sessions **and** previously had more (sudden history loss = sanity check). |
| Two servers | `flock` + `server.json`. |
| Server reads a half‑written book | Impossible: WAL snapshot isolation + single write txn per session. |
| CLI times out on a busy DB | `busy_timeout` 5 s; writes are ms; clear error message. |
| Stale in‑memory state after a CLI write | `data_version` check per tick and per request. |
| `fetch` stores a date, server runs the session, then `backfill` re‑requests the same date | `store_snapshot` is idempotent (returns False). |
| Stale maintenance lock after a killed backfill | Heartbeat + dead‑pid detection → lock broken with a warning + alert. |

---

## 6. Web application

### 6.1 Pages (server‑rendered Jinja2, existing purple theme ported; auto‑refresh via SSE `/events`)

| Route | Content (maps to today's static page) |
|---|---|
| `/` | Hub: per‑ETF card (latest date, new/exits/movers/top‑5/trades/rising), strategy books, recent backtests, **status strip** (last cycle, next poll, lag, paused/maintenance, alerts). |
| `/etf/{ETF}` and `/etf/{ETF}/{YYYY-MM}` | Month view (day blocks newest‑first), and for the live month the "Top‑20 / rising stars / strategy signals across all holdings / sectors" sections. Same `build_day_data` payload as before. Month navigation from `snapshots`, no sealing/rollover logic. |
| `/etf/{ETF}/day/{date}` | One day (what a day block expands to today), linkable. |
| `/strategy/{name}` | Book page: stats, open positions (paper vs Alpaca qty), rising stars, trades (paginated, not last‑60 only), equity chart (client‑side from `/api/strategy/{name}/equity`), sessions list with params snapshot, catch‑up markers. |
| `/strategy/{name}/session/{date}` | Session detail: entries/exits/skipped/signals for that day (from `sessions` + `signals`). |
| `/ticker/{T}` (+ `?strategy=`) | Signal history (what `velocity ticker` writes today); list page `/tickers` = tickers in current universe. |
| `/backtests`, `/backtests/{id}` | From the `backtests` table (command line, settings, tables, equity chart, trades). |
| `/status` | Poller/fetch log, jobs, orders, alerts, lock, data_version, log tail (ring buffer). |
| `/admin/*` (token) | Run cycle now, pause/resume, settings form, reset book (with confirmations), backtest launcher. |

### 6.2 JSON API (replaces the static JSON; all under `/api`, read‑only, cacheable with `ETag = data_version`)

`/api/health`, `/api/status`, `/api/etfs`, `/api/etf/{ETF}/dates`, `/api/etf/{ETF}/day/{date}`
(= `build_day_data`), `/api/etf/{ETF}/month/{ym}` (= the old `YYYY-MM.json` array, for any tooling
that consumed it), `/api/strategy/{name}` (= old `strategy/{name}.json` = `Book.to_dict`),
`/api/strategy/{name}/sessions`, `/api/strategy/{name}/signals?date=`, `/api/ticker/{T}`,
`/api/backtests`, `/api/backtests/{id}`, `/api/orders`, `/api/alerts`.
Admin (POST, token): `/api/admin/jobs`, `/api/admin/jobs/{id}`, `/api/admin/settings`,
`/api/admin/pause|resume`, `/api/admin/books/{name}/reset`, `/api/admin/reload`.

### 6.3 Caching & cost

* `MarketState` (bars + `Indicators` + prepared strategies + `Universe`) ≈ 3 s to build; rebuilt by
  the worker only when `data_version` changes. Request handlers take a reference (immutable
  object swap — no locking).
* `build_day_data` per (etf, date, strategy) ≈ 0.09 s → LRU(512) keyed with `data_version`;
  worker pre‑warms the latest day for every ETF after each cycle.
* `Book.load` parses ~200 KB JSON; cache per strategy keyed on `books.updated_at`.
* `ETag`/`If-None-Match` on API responses using `data_version` so browser auto‑refresh is cheap.

### 6.4 Security / exposure

* Bind `127.0.0.1`; expose via Caddy/nginx (TLS) or Cloudflare Tunnel/Tailscale. Read pages can be
  public or behind basic auth (config). Admin routes require `Authorization: Bearer <token>`;
  HTML admin forms post via JS with the token from a login cookie (HttpOnly, SameSite=Strict) to
  avoid CSRF. No secrets in pages; `.env` stays the source of Alpaca keys.
* The replacement for the CDN: GitHub Pages cannot host this; a small VPS/home box running systemd
  is the minimum. If a public read‑only copy is still wanted without exposing the box, a later
  option is a **snapshot exporter** that writes the API responses to the CDN — but that is
  explicitly *out of scope* per the request to remove static generation.

---

## 7. Edge‑case catalogue (consolidated)

1. **Lagging ETF** → no session until all ETFs have the date; alert after N trading days; never run
   on a partial universe (forced "dropped from ETF" exits). Status shows per‑ETF lag.
2. **ETF publishes a date the others skip** (e.g. one fund has 10‑10, others jump 10‑09 → 10‑13) →
   `session_date` = latest *common* date; the orphan date is never a session date but *is* in the
   bar series and in the ETF's day pages. Same as today; document it.
3. **Downtime / paused across several days** → catch‑up (§4.3) with the Alpaca policy.
4. **Server restarts mid‑cycle** → atomic txn + idempotent `run_session` + outbox reconciliation.
5. **Crash between commit and Alpaca submission** → `pending` intents re‑submitted if still for the
   latest session, else `expired` + alert.
6. **Alpaca rejects / partially fills** → recorded, alerted, visible as paper‑vs‑Alpaca drift; the
   paper book is never altered by broker events.
7. **Alpaca closed (weekend) when data arrives** → DAY market orders queue to the next open — same
   as today; if data arrives intraday (late publication) the order fills immediately — same as
   today; note it in the UI.
8. **BlackRock anti‑bot / 403 / HTML page** → backoff per ETF, alert after K consecutive failures,
   never stored as data (existing check kept).
9. **BlackRock republishes a date with corrected rows** → `store_snapshot` ignores (idempotent);
   log a `fetch_log` outcome `already(diff)` if the row count differs, so it is at least visible.
10. **CLI write while server running** → dual‑mode forwarding; `--local` takes the lock; DB timeouts
    give a clear error rather than a traceback.
11. **backfill `--wipe`** → lock + history‑loss sanity check before any session.
12. **Parameter change mid‑book** → allowed, alerted, recorded per session.
13. **Strategy removed from `active`** → book frozen, not reset; positions stop being managed
    (stops not evaluated!) → the UI marks the book "inactive — positions unmanaged"; removing the
    *mirrored* strategy is refused while it has Alpaca positions.
14. **Adding an ETF to `ETF_CONFIG`** → until it has a snapshot, `session_date` is `None` and no
    session runs (today's behaviour, now very visible) → the status page says "waiting for first
    snapshot of X; run `velocity backfill --etf X`". Consider a per‑ETF `tracked` flag in settings
    so a new ETF can be backfilled before it participates.
15. **Removing an ETF** → its historical pages remain (data is in its DB); the universe shrinks →
    positions only held via it exit as "dropped from all tracked ETFs" at the next session. Warn on
    config change.
16. **Time zones / DST** → all scheduling in `America/New_York`; dates remain ISO trading dates;
    server clock can be UTC.
17. **Holidays** → Alpaca calendar for cadence and staleness; sessions are data‑driven, not
    calendar‑driven.
18. **Two servers / second `serve`** → refused.
19. **Stale lock** → heartbeat + dead‑pid detection.
20. **Backups while running** → `VACUUM INTO` / `sqlite3 .backup` from the worker (nightly), never
    `cp` of a WAL database.
21. **Schema migration** → `schema_version` in `meta`; migrations run under the maintenance lock at
    start‑up; the old CLI refuses to run on a newer schema.
22. **Memory/size growth** → `trades_json`/`equity_json` single‑row blobs grow forever (164 KB
    today). Acceptable for years at this trade rate; note a future normalisation (`trades` table)
    as a non‑blocking improvement.
23. **Historical day pages** → correct only after the as‑of‑date fix; covered by a test that
    compares `build_day_data(conn, d)` for a past `d` against the month‑log JSON from the old
    pipeline for the same day (we have `report/*/2026‑0{8,9}.json` as fixtures).
24. **`--mock-csv` / tests** → stays available headless; in server mode as an uploaded job payload;
    the test‑suite runs the services against temp DBs with a fake fetcher and a fake Alpaca client.
25. **Legacy invocation** (`velocity.py --etf ALL --paper-trade`, `--list-strategies`) → kept.

---

## 8. Delivery plan

### Phase 0 — Foundations (no behaviour change visible to cron; can ship while cron still runs)
* `database.connect()` with WAL/busy_timeout; replace all raw `sqlite3.connect`/`get_db`.
* As‑of‑date fix + regression test against the existing month JSON fixtures.
* `meta` (schema/data_version), `settings`, `orders`, `jobs`, `fetch_log`, `backtests`, `alerts`,
  `book_backups`; `sessions.params_json/rising_json`.
* Extract `src/services/*` from `commands/*` (commands become shells; existing CLI tests stay green).
* Atomic session persistence + outbox (submission still immediate, as today).
* Delete `src/trader.py`; stop committing `report/`.
* **Exit criteria**: `python -m pytest` green; `velocity trading --offline --paper-trade` produces
  byte‑identical `books` rows to before (replay on a copy of `data/`).

### Phase 1 — Daemon (headless)
* `src/scheduler.py`, `market_state.py`, `velocity serve --no-web`, `status`, `pause/resume`,
  `config`, maintenance lock, `data_version` watcher, catch‑up, staleness alerts, fetch politeness.
* systemd unit; retire the cron line (the two must not both run; idempotency makes an overlap
  harmless but pointless).
* **Exit criteria**: 5 consecutive trading days where the daemon stored each ETF's snapshot and ran
  the session within one poll interval of publication; `velocity status` shows it; kill ‑9 during
  a cycle leaves a consistent DB.

### Phase 2 — Web (read‑only)
* FastAPI app, templates ported from `reporter.py` (then delete `reporter.py`), JSON API,
  SSE refresh, status page. `rebuild` becomes the deprecated alias.
* **Exit criteria**: every page the static site had exists at its new route; `/api/etf/{ETF}/month/{ym}`
  equals the old month JSON for the live month (modulo the as‑of fix).

### Phase 3 — Admin API + dual‑mode CLI
* `routes_admin.py`, `jobs`, `cli_client.py`, forwarding for `trading/fetch/reset-book/
  backtest --save-book/config`, `--local/--remote`, `server.json` + token.
* **Exit criteria**: concurrency test — server running a cycle every 10 s while
  `velocity fetch`, `velocity backtest --save-book`, `velocity reset-book`, `velocity backfill --months 1`
  run from the CLI; no `database is locked`, books consistent, `data_version` observed.

### Phase 4 — Hardening
* Order reconciliation (status polling, drift view, guards), alerts webhook, nightly backup job,
  auth, log ring buffer, README rewrite, remove `rebuild`/`--report-dir` compatibility shims.

Rough effort: Phase 0 ≈ 2–3 days, Phase 1 ≈ 3 days, Phase 2 ≈ 3–4 days, Phase 3 ≈ 2 days,
Phase 4 ≈ 2–3 days (one developer, including tests).

---

## 9. Deployment & operations

* `deploy/velocity.service`: `ExecStart=<venv>/bin/python velocity.py serve --host 127.0.0.1 --port 8787`,
  `Restart=always`, `RestartSec=5`, `TimeoutStopSec=30`, `EnvironmentFile=.env`,
  `WorkingDirectory=~/velocity/main`. Logs to journald + rotating file (`data/logs/`).
* Reverse proxy (Caddy) for TLS / basic auth if exposed; or Tailscale/Cloudflare Tunnel.
* Backups: nightly `VACUUM INTO data/backups/<db>-<date>.db` from the worker, keep 14; book
  backups on every reset/register.
* Monitoring: `/api/health` for an uptime checker; `velocity status` exit code for a watchdog
  cron; alerts table + optional webhook (ntfy/Slack) for stale data, fetch failures, rejected
  orders, broken lock.
* Requirements: add `fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart`, `httpx` (CLI
  client), optionally `apscheduler` (or a 40‑line loop — recommended to keep it dependency‑free).

---

## 10. Decisions needed from you

1. **Multiple live strategies?** Books are already per strategy; running all three live is free.
   Proposed default: `active = [trend_follow]`, `mirrored = trend_follow` (matches today's cron).
2. **Catch‑up + Alpaca policy** (§4.3): proposed `latest-only`.
3. **`backfill` in‑process under lock (proposed) vs. as a server job.**
4. **Public read‑only exposure** (proxy + basic auth vs. Tailscale‑only).
5. **Polling window/interval** defaults (15 min, weekday evenings ET) and whether to use the Alpaca
   calendar or a simple weekday rule.
6. **Import historical rising‑star lists** from the existing month JSONs (one‑off) — yes/no.
7. **Keep `--mock-csv`** in server mode (upload) or headless‑only.
