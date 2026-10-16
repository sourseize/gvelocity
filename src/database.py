"""
Database layer: the one SQLite file (`data/velocity.db`), its schema, transactions, and the
holdings queries.

Connections
-----------
Every connection goes through `connect()` / `get_db()`:
  * WAL journal (readers never block the writer and vice versa), `busy_timeout` (a second writer
    waits instead of failing with "database is locked"), `synchronous=NORMAL`.
  * autocommit mode (`isolation_level=None`): nothing is implicitly left open.  Multi-statement
    writes use `with transaction(conn):` which issues BEGIN IMMEDIATE / COMMIT / ROLLBACK and
    *joins* an enclosing transaction instead of nesting, so a service can wrap several
    lower-level writes into one atomic unit.
  * `readonly=True` opens the file with `mode=ro` — request handlers of the web server use it
    so they structurally cannot write.

Schema
------
`ensure_schema()` creates every table (holdings history, books, sessions, signals, fetch_log,
meta) and applies the numbered `MIGRATIONS` recorded in `meta.schema_version`.

As-of-date queries
------------------
All history queries take the date being analysed and only look at snapshots on or before it, so
`build_day_data(conn, date)` is correct for ANY stored date, not just the latest one.
"""

import contextlib
import datetime
import sqlite3
from pathlib import Path
from typing import Optional

from .config import DB_PATH

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fetch_date  TEXT UNIQUE NOT NULL,
    fetched_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holdings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date   TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    name            TEXT NOT NULL,
    sector          TEXT,
    weight_pct      REAL,
    market_value    REAL,
    shares          REAL,
    price           REAL,
    currency        TEXT,
    exchange        TEXT,
    location        TEXT,
    UNIQUE(snapshot_date, ticker)
);
CREATE TABLE IF NOT EXISTS weekly_closes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    week_ending TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    name        TEXT NOT NULL,
    sector      TEXT,
    weight_pct  REAL,
    price       REAL,
    currency    TEXT,
    UNIQUE(week_ending, ticker)
);
CREATE INDEX IF NOT EXISTS idx_h_date        ON holdings(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_h_ticker_date ON holdings(ticker, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_wc_ticker     ON weekly_closes(ticker, week_ending);
CREATE INDEX IF NOT EXISTS idx_wc_week       ON weekly_closes(week_ending);

-- one row per strategy: the position book (single source of truth)
CREATE TABLE IF NOT EXISTS books (
    strategy        TEXT PRIMARY KEY,
    initial_capital REAL NOT NULL,
    cash            REAL NOT NULL,
    last_date       TEXT,
    positions_json  TEXT NOT NULL,
    trades_json     TEXT NOT NULL,
    equity_json     TEXT NOT NULL,
    last_exit_json  TEXT NOT NULL,
    rising_json     TEXT NOT NULL DEFAULT '{}',
    note            TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL
);
-- what each session did (audit trail, queryable without recomputation)
CREATE TABLE IF NOT EXISTS sessions (
    strategy     TEXT NOT NULL,
    date         TEXT NOT NULL,
    n_entries    INTEGER NOT NULL,
    n_exits      INTEGER NOT NULL,
    n_skipped    INTEGER NOT NULL,
    n_open       INTEGER NOT NULL,
    n_rising     INTEGER NOT NULL,
    cash         REAL NOT NULL,
    equity       REAL NOT NULL,
    entries_json TEXT NOT NULL,
    exits_json   TEXT NOT NULL,
    skipped_json TEXT NOT NULL,
    run_at       TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'live',      -- live | catchup | backtest
    params_json  TEXT NOT NULL DEFAULT '{}',        -- strategy parameters the session ran with
    engine_json  TEXT NOT NULL DEFAULT '{}',        -- engine configuration (limits, sizing, policy)
    rising_json  TEXT NOT NULL DEFAULT '[]',        -- the rising-star watchlist after the session
    PRIMARY KEY (strategy, date)
);
-- every ticker's ENTRY / HOLD / WATCH / EXIT state per session
CREATE TABLE IF NOT EXISTS signals (
    strategy    TEXT NOT NULL,
    date        TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    state       TEXT NOT NULL,
    note        TEXT,
    checks_json TEXT,
    PRIMARY KEY (strategy, date, ticker)
);
CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(strategy, ticker, date);
-- every holdings fetch attempt (the poller's observability)
CREATE TABLE IF NOT EXISTS fetch_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    attempted_at  TEXT NOT NULL,
    outcome       TEXT NOT NULL,      -- stored | already | no-data | http-error | connection-error | parse-error
    holdings_date TEXT,
    http_status   INTEGER,
    ms            INTEGER,
    message       TEXT
);
-- key / value: schema_version and the daemon's status fields
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# (version, [statements]) applied in order when meta.schema_version is older.  Version 1 brings a
# pre-versioned database (tables created by earlier releases) up to the schema above.
MIGRATIONS = [
    (1, [
        ("books", "rising_json", "ALTER TABLE books ADD COLUMN rising_json TEXT NOT NULL DEFAULT '{}'"),
        ("books", "note", "ALTER TABLE books ADD COLUMN note TEXT NOT NULL DEFAULT ''"),
        ("sessions", "kind", "ALTER TABLE sessions ADD COLUMN kind TEXT NOT NULL DEFAULT 'live'"),
        ("sessions", "params_json", "ALTER TABLE sessions ADD COLUMN params_json TEXT NOT NULL DEFAULT '{}'"),
        ("sessions", "engine_json", "ALTER TABLE sessions ADD COLUMN engine_json TEXT NOT NULL DEFAULT '{}'"),
        ("sessions", "rising_json", "ALTER TABLE sessions ADD COLUMN rising_json TEXT NOT NULL DEFAULT '[]'"),
    ]),
]


class SchemaTooNew(Exception):
    """The database was written by a newer release of Velocity."""


# ── connections & transactions ──────────────────────────────────────────────
def connect(db_path: Path = DB_PATH, readonly: bool = False, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    """Open the application database with the standard pragmas (see module docstring)."""
    # check_same_thread=False: `velocity serve` opens the writer connection in the main thread and
    # uses it from the poller thread (one writer by design; request handlers open their own).
    if readonly:
        conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True, isolation_level=None,
                               check_same_thread=False)
    else:
        conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    if not readonly:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection):
    """`with transaction(conn):` — BEGIN IMMEDIATE … COMMIT, ROLLBACK on any exception.
    Joins an already open transaction (so callers can compose atomic units from smaller ones)."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError:        # no meta table yet
        return 0
    return int(row[0]) if row else 0


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create missing tables and apply pending migrations.  Raises SchemaTooNew when the file was
    written by a newer release (the older code must not touch it)."""
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise SchemaTooNew(f"database schema version {current} is newer than this release supports ({SCHEMA_VERSION})")
    with transaction(conn):
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                conn.execute(stmt)
        for version, steps in MIGRATIONS:
            if version <= current:
                continue
            for table, column, sql in steps:
                if column not in _columns(conn, table):
                    conn.execute(sql)
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA_VERSION),))


def get_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Read-write connection with the schema ensured (what every command uses)."""
    conn = connect(db_path)
    ensure_schema(conn)
    return conn


# ── meta ────────────────────────────────────────────────────────────────────
def get_meta(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    with transaction(conn):
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, None if value is None else str(value)))


# ── holdings storage ────────────────────────────────────────────────────────
def week_ending(day: datetime.date) -> str:
    """ISO date of the Friday of `day`'s week."""
    return (day + datetime.timedelta(days=4 - day.weekday())).isoformat()


def _normalize_ticker(value) -> str:
    return (value or "").strip().strip('"').upper()


def store_snapshot(conn, holdings_date, rows) -> bool:
    """Store one day's holdings (snapshot row, holdings rows, the week's weekly_closes) atomically.

    Returns:
        True if new data was stored, False if the date already existed
    """
    ds = holdings_date.isoformat()
    with transaction(conn):
        if conn.execute("SELECT id FROM snapshots WHERE fetch_date=?", (ds,)).fetchone():
            return False

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn.execute("INSERT INTO snapshots(fetch_date,fetched_at) VALUES(?,?)", (ds, now))

        normalized_rows = []
        seen_tickers = set()
        for row in rows:
            normalized = {**row}
            ticker = _normalize_ticker(normalized.get("ticker"))
            if not ticker:
                continue
            if ticker in seen_tickers:
                continue
            normalized["ticker"] = ticker
            normalized_rows.append({**normalized, "snapshot_date": ds})
            seen_tickers.add(ticker)

        conn.executemany(
            """INSERT OR IGNORE INTO holdings
               (snapshot_date,ticker,name,sector,weight_pct,market_value,shares,
                price,currency,exchange,location)
               VALUES(:snapshot_date,:ticker,:name,:sector,:weight_pct,:market_value,
                      :shares,:price,:currency,:exchange,:location)""",
            normalized_rows
        )

        # Weekly close = the LAST snapshot of the week (so a Friday holiday does not lose the week).
        # Every snapshot overwrites its week's row; later days in the week win.
        we = week_ending(holdings_date)
        last = conn.execute("SELECT MAX(snapshot_date) FROM holdings WHERE snapshot_date<=? AND snapshot_date>=?",
                            (we, (holdings_date - datetime.timedelta(days=holdings_date.weekday())).isoformat())).fetchone()[0]
        if last == ds:
            conn.execute("DELETE FROM weekly_closes WHERE week_ending=?", (we,))
            conn.executemany(
                """INSERT OR REPLACE INTO weekly_closes
                   (week_ending,ticker,name,sector,weight_pct,price,currency)
                   VALUES(:week_ending,:ticker,:name,:sector,:weight_pct,:price,:currency)""",
                [{**r, "week_ending": we} for r in normalized_rows]
            )
    return True


def rebuild_weekly_closes(conn) -> int:
    """Derive weekly_closes from holdings: one row per (ISO week, ticker) = the week's last snapshot."""
    with transaction(conn):
        conn.execute("DELETE FROM weekly_closes")
        dates = [r[0] for r in conn.execute("SELECT fetch_date FROM snapshots ORDER BY fetch_date")]
        last_of_week = {}
        for d in dates:
            last_of_week[week_ending(datetime.date.fromisoformat(d))] = d
        n = 0
        for we, d in sorted(last_of_week.items()):
            cur = conn.execute(
                """INSERT INTO weekly_closes (week_ending,ticker,name,sector,weight_pct,price,currency)
                   SELECT ?, ticker, name, sector, weight_pct, price, currency FROM holdings WHERE snapshot_date=?""",
                (we, d))
            n += cur.rowcount
    return n


def wipe_holdings(conn) -> None:
    """Delete the whole holdings history (snapshots / holdings / weekly_closes) atomically."""
    with transaction(conn):
        conn.execute("DELETE FROM holdings")
        conn.execute("DELETE FROM snapshots")
        conn.execute("DELETE FROM weekly_closes")


# ── fetch log ───────────────────────────────────────────────────────────────
def log_fetch(conn, outcome: str, holdings_date: Optional[str] = None, http_status: Optional[int] = None,
              ms: Optional[int] = None, message: str = "") -> None:
    with transaction(conn):
        conn.execute("INSERT INTO fetch_log (attempted_at, outcome, holdings_date, http_status, ms, message) VALUES (?,?,?,?,?,?)",
                     (datetime.datetime.now(datetime.timezone.utc).isoformat(), outcome, holdings_date, http_status, ms, message[:500]))


# ── as-of-date history queries ──────────────────────────────────────────────
def all_dates(conn, asof: Optional[str] = None) -> list[str]:
    """Every snapshot date on or before `asof` (all of them when None), ascending."""
    if asof:
        return [r[0] for r in conn.execute("SELECT fetch_date FROM snapshots WHERE fetch_date<=? ORDER BY fetch_date", (asof,))]
    return [r[0] for r in conn.execute("SELECT fetch_date FROM snapshots ORDER BY fetch_date")]


def latest_date(conn) -> Optional[str]:
    return conn.execute("SELECT MAX(fetch_date) FROM snapshots").fetchone()[0]


def recent_dates(conn, n: int, asof: Optional[str] = None) -> list[str]:
    """The n most recent snapshot dates on or before `asof`, newest first."""
    if asof:
        return [r[0] for r in conn.execute(
            "SELECT fetch_date FROM snapshots WHERE fetch_date<=? ORDER BY fetch_date DESC LIMIT ?", (asof, n))]
    return [r[0] for r in conn.execute("SELECT fetch_date FROM snapshots ORDER BY fetch_date DESC LIMIT ?", (n,))]


def daily_history(conn, ticker: str, n: int = 25, asof: Optional[str] = None) -> list[dict]:
    """Daily price/weight rows of a ticker over the n most recent sessions up to `asof`, newest first."""
    dates = recent_dates(conn, n, asof)
    if not dates:
        return []
    ph = ",".join("?" * len(dates))
    return [dict(r) for r in conn.execute(
        f"""SELECT snapshot_date,weight_pct,price,currency FROM holdings
            WHERE ticker=? AND snapshot_date IN ({ph}) ORDER BY snapshot_date DESC""",
        [ticker] + dates
    )]


def weekly_history(conn, ticker: str, n: int = 52, asof: Optional[str] = None) -> list[dict]:
    """Weekly price/weight rows of a ticker, newest first.  As of `asof`, the current week's point is
    the `asof` day's own holdings row (what the weekly close WAS on that day — the stored row for
    that week may come from a later day), followed by the stored closes of the earlier weeks."""
    if not asof:
        return [dict(r) for r in conn.execute(
            """SELECT week_ending,weight_pct,price,currency FROM weekly_closes
               WHERE ticker=? ORDER BY week_ending DESC LIMIT ?""", (ticker, n))]
    we = week_ending(datetime.date.fromisoformat(asof))
    cur = conn.execute("SELECT weight_pct,price,currency FROM holdings WHERE ticker=? AND snapshot_date=?",
                       (ticker, asof)).fetchone()
    out = [{"week_ending": we, **dict(cur)}] if cur else []
    out += [dict(r) for r in conn.execute(
        """SELECT week_ending,weight_pct,price,currency FROM weekly_closes
           WHERE ticker=? AND week_ending<? ORDER BY week_ending DESC LIMIT ?""", (ticker, we, n))]
    return out[:n]


def detect_new_entries(conn, today: str, lookback_days: int) -> list[str]:
    """Tickers held on `today` that were in none of the previous `lookback_days` snapshots."""
    lb = recent_dates(conn, lookback_days + 1, asof=today)
    if len(lb) < 2:
        return []

    prior = lb[1:]  # Exclude today
    today_set = {r["ticker"] for r in conn.execute(
        "SELECT ticker FROM holdings WHERE snapshot_date=?", (today,)
    )}

    ph = ",".join("?" * len(prior))
    prior_set = {r["ticker"] for r in conn.execute(
        f"SELECT DISTINCT ticker FROM holdings WHERE snapshot_date IN ({ph})", prior
    )}

    return sorted(today_set - prior_set)


def detect_exits(conn, today: str) -> list[dict]:
    """Tickers held in the snapshot before `today` but not on `today`."""
    if not today:
        return []

    prev_date = conn.execute(
        "SELECT fetch_date FROM snapshots WHERE fetch_date < ? ORDER BY fetch_date DESC LIMIT 1",
        (today,)
    ).fetchone()
    if not prev_date:
        return []

    prev_date = prev_date["fetch_date"]

    today_set = {r["ticker"] for r in conn.execute(
        "SELECT ticker FROM holdings WHERE snapshot_date=?", (today,)
    )}

    prev_rows = [dict(r) for r in conn.execute(
        "SELECT ticker,name,sector,weight_pct FROM holdings WHERE snapshot_date=?",
        (prev_date,)
    )]

    return [row for row in prev_rows if row["ticker"] not in today_set]


def detect_weight_movers(conn, today: str, lookback_days: int, threshold: float = 0.05) -> list[dict]:
    """Holdings whose weight changed by more than `threshold` points over the `lookback_days`
    sessions ending on `today`."""
    dates = recent_dates(conn, lookback_days, asof=today)
    if len(dates) < 2:
        return []

    oldest = dates[-1]
    today_map = {r["ticker"]: dict(r) for r in conn.execute(
        "SELECT ticker,name,sector,weight_pct FROM holdings WHERE snapshot_date=?", (today,)
    )}
    old_map = {r["ticker"]: r["weight_pct"] for r in conn.execute(
        "SELECT ticker,weight_pct FROM holdings WHERE snapshot_date=?", (oldest,)
    )}

    movers = []
    for ticker, row in today_map.items():
        if ticker in old_map and row["weight_pct"] and old_map[ticker]:
            delta = row["weight_pct"] - old_map[ticker]
            if abs(delta) > threshold:
                movers.append({**row, "weight_then": old_map[ticker], "weight_delta": delta})

    movers.sort(key=lambda x: (-abs(x["weight_delta"]), x["ticker"]))       # deterministic on ties
    return movers
