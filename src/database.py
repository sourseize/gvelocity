"""
Database operations for Velocity tracker
"""

import sqlite3
from pathlib import Path
from typing import Optional
import datetime

from .config import ETF_CONFIG
from .exceptions import NoDataForDateError

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
"""

# tables of the retired per-ETF paper-trading system; dropped by cli/migrate_db.py
LEGACY_TABLES = ("portfolio_state", "trading_state")


def week_ending(day: datetime.date) -> str:
    """ISO date of the Friday of `day`'s week."""
    return (day + datetime.timedelta(days=4 - day.weekday())).isoformat()

def get_db(db_path: Path):
    """Get database connection with row factory and schema initialized."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn

def default_db_path(etf: str) -> Path:
    """Get default database path for an ETF."""
    return Path(__file__).parent.parent / "data" / f"{etf}.db"

def etf_report_dir(report_root: Path, etf: str) -> Path:
    """Get report directory for an ETF."""
    return report_root / etf

def _normalize_ticker(value) -> str:
    return (value or "").strip().strip('"').upper()


def store_snapshot(conn, holdings_date, rows) -> bool:
    """Store snapshot and holdings in database.

    Returns:
        True if new data was stored, False if data already existed
    """
    ds = holdings_date.isoformat()

    # Check if we already have data for this date
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

    # Insert holdings
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

    conn.commit()
    return True


def rebuild_weekly_closes(conn) -> int:
    """Derive weekly_closes from holdings: one row per (ISO week, ticker) = the week's last snapshot."""
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
    conn.commit()
    return n


def recent_dates(conn, n: int) -> list[str]:
    """Get the n most recent fetch dates."""
    return [r["fetch_date"] for r in conn.execute(
        "SELECT fetch_date FROM snapshots ORDER BY fetch_date DESC LIMIT ?", (n,)
    )]

def daily_history(conn, ticker: str, n: int = 25) -> list[dict]:
    """Get daily price/weight history for a ticker."""
    dates = recent_dates(conn, n)
    if not dates:
        return []

    ph = ",".join("?" * len(dates))
    return [dict(r) for r in conn.execute(
        f"""SELECT snapshot_date,weight_pct,price,currency FROM holdings
            WHERE ticker=? AND snapshot_date IN ({ph}) ORDER BY snapshot_date DESC""",
        [ticker] + dates
    )]

def weekly_history(conn, ticker: str, n: int = 52) -> list[dict]:
    """Get weekly price/weight history for a ticker."""
    return [dict(r) for r in conn.execute(
        """SELECT week_ending,weight_pct,price,currency FROM weekly_closes
           WHERE ticker=? ORDER BY week_ending DESC LIMIT ?""",
        (ticker, n)
    )]

def detect_new_entries(conn, today: str, lookback_days: int) -> list[str]:
    """Detect tickers that are new entries in the last lookback_days period."""
    lb = recent_dates(conn, lookback_days + 1)
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
    """Detect tickers that exited between the previous snapshot and today."""
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
    """Detect significant weight changes over lookback period."""
    dates = recent_dates(conn, lookback_days)
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

    movers.sort(key=lambda x: abs(x["weight_delta"]), reverse=True)
    return movers
