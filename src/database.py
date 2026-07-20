"""
Database operations for Velocity tracker
"""

import sqlite3
import json
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
CREATE TABLE IF NOT EXISTS portfolio_state (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    etf                TEXT NOT NULL UNIQUE,
    initial_capital    REAL NOT NULL,
    cash               REAL NOT NULL,
    commission_per_trade REAL NOT NULL,
    positions_json     TEXT NOT NULL,
    trade_history_json TEXT NOT NULL,
    daily_values_json  TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trading_state (
    etf                     TEXT PRIMARY KEY,
    last_evaluated_snapshot TEXT NOT NULL,
    scores_json             TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_h_date   ON holdings(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_h_ticker ON holdings(ticker);
CREATE INDEX IF NOT EXISTS idx_wc       ON weekly_closes(ticker);
"""

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

    # Store weekly closes if it's a Friday
    if holdings_date.weekday() == 4:  # Friday
        conn.executemany(
            """INSERT OR IGNORE INTO weekly_closes
               (week_ending,ticker,name,sector,weight_pct,price,currency)
               VALUES(:week_ending,:ticker,:name,:sector,:weight_pct,:price,:currency)""",
            [{**r, "week_ending": ds} for r in rows]
        )

    conn.commit()
    return True


def load_trading_state(conn, etf: str) -> Optional[dict]:
    """Load the latest evaluated snapshot, score map, and signal-bit map for an ETF.

    Returns a dict with keys:
        last_evaluated_snapshot : str
        scores                  : {ticker: int}
        signals                 : {ticker: [bool, ...]}  # 5-bit confirmation vector

    The persisted payload is ``{ticker: {"score": int, "bits": [bool*5]}}``.
    Legacy rows that stored a plain ``{ticker: int}`` map are coerced to the
    new shape (``bits=[]``), so existing DBs load without a migration; tickers
    with no prior bits produce no flipped-signal detail on their first
    post-upgrade score crossing.
    """
    row = conn.execute(
        "SELECT last_evaluated_snapshot, scores_json FROM trading_state WHERE etf=?",
        (etf,),
    ).fetchone()
    if not row:
        return None

    raw = json.loads(row["scores_json"])
    scores: dict[str, int] = {}
    signals: dict[str, list[bool]] = {}
    for ticker, value in raw.items():
        if isinstance(value, dict):
            scores[ticker] = int(value.get("score", 0))
            signals[ticker] = [bool(b) for b in value.get("bits", [])]
        else:  # legacy {ticker: int}
            scores[ticker] = int(value)
            signals[ticker] = []
    return {
        "last_evaluated_snapshot": row["last_evaluated_snapshot"],
        "scores": scores,
        "signals": signals,
    }


def save_trading_state(
    conn,
    etf: str,
    snapshot_date: str,
    scores: dict[str, int],
    signals: Optional[dict[str, list[bool]]] = None,
) -> None:
    """Persist an ETF's evaluated snapshot, per-ticker scores, and signal bits.

    The payload is stored as ``{ticker: {"score": int, "bits": [bool*5]}}`` so
    the next run can diff the bit vectors to derive which signal flipped a buy.
    ``signals`` is optional (defaults to no bits) — legacy/test callers and the
    `<list`-missing path persist empty bits.
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    packed: dict[str, dict] = {}
    for ticker, score in scores.items():
        value = score["score"] if isinstance(score, dict) else int(score)
        bits = [bool(b) for b in (signals or {}).get(ticker, [])]
        packed[ticker] = {"score": value, "bits": bits}
    conn.execute(
        """
        INSERT INTO trading_state (etf, last_evaluated_snapshot, scores_json, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(etf) DO UPDATE SET
            last_evaluated_snapshot=excluded.last_evaluated_snapshot,
            scores_json=excluded.scores_json,
            updated_at=excluded.updated_at
        """,
        (etf, snapshot_date, json.dumps(packed, sort_keys=True), now),
    )
    conn.commit()

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

def find_rising_stars(conn, today: str, scored: dict, lookback_days: int = 60, min_growth: float = 0.30) -> list[dict]:
    """Find recently-added holdings whose weight has grown meaningfully since entry."""
    recent = recent_dates(conn, lookback_days)
    if not recent:
        return []

    cutoff = recent[-1]  # Oldest date in lookback window

    all_current = [r["ticker"] for r in conn.execute(
        "SELECT ticker FROM holdings WHERE snapshot_date=?", (today,)
    )]

    # Import compute_metrics here to avoid circular import
    from .analyzer import compute_metrics
    rising = []
    for ticker in all_current:
        m = scored.get(ticker) or compute_metrics(conn, ticker, today)
        first_seen = m.get("first_seen")

        # Skip if entered before lookback window
        if not first_seen or first_seen < cutoff:
            continue

        weight_roc = m.get("weight_roc_since_entry")
        if weight_roc is None or weight_roc < min_growth:
            continue

        rising.append(m)

    rising.sort(key=lambda m: m.get("weight_roc_since_entry") or 0, reverse=True)
    return rising
