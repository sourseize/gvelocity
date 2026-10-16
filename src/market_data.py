"""
market_data — price bars and the tradeable universe.

Bars come from one of two places:
  * `daily_bars` table (OHLCV with volume, split-adjusted) — optional, filled by an external
    provider through `store_bars()`; this enables the volume rules of every strategy.
  * fallback: the BlackRock valuation price in the `holdings` table, one close series per
    ticker, split-adjusted (price / k with shares * k on the same day is a split, not a crash).

The universe is issuer-level: GOOG and GOOGL are one company and may be held once.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Dict, List, Optional

import pandas as pd

log = logging.getLogger(__name__)

ISSUER_ALIASES = {
    "GOOGL": "GOOG", "BRK.B": "BRK", "BRK/B": "BRK", "BRKB": "BRK",
    "FOXA": "FOX", "NWSA": "NWS", "LBTYK": "LBTYA", "CMCSK": "CMCSA", "ZG": "Z",
}


def issuer_key(ticker: str) -> str:
    t = (ticker or "").strip().upper()
    return ISSUER_ALIASES.get(t, t)


def is_tradeable_symbol(ticker: str) -> bool:
    t = (ticker or "").strip()
    return bool(t) and t != "-" and t.replace(".", "").replace("/", "").isalnum()


# ── bars ────────────────────────────────────────────────────────────────────
def bars_from_holdings(conn: sqlite3.Connection) -> pd.DataFrame:
    """Long DataFrame(date, ticker, close) from the holdings table, split-adjusted."""
    h = pd.read_sql("SELECT snapshot_date AS date, ticker, price AS close, shares FROM holdings "
                    "WHERE price IS NOT NULL AND price > 0", conn)
    cols = ["date", "ticker", "close"]
    h = h[h.ticker.map(is_tradeable_symbol)]
    if h.empty:                       # database exists but holds no priced holdings rows
        return pd.DataFrame(columns=cols)
    h = h.sort_values("shares").drop_duplicates(["date", "ticker"], keep="last")
    close = h.pivot(index="date", columns="ticker", values="close").sort_index()
    shares = h.pivot(index="date", columns="ticker", values="shares").sort_index()
    pr = close / close.shift()
    sr = shares / shares.shift()
    split = ((pr * sr - 1).abs() < 0.08) & ((pr - 1).abs() > 0.3)
    cum = pr.where(split, 1.0).fillna(1.0).cumprod()
    adj = close * (cum.iloc[-1] / cum)
    n = int(split.sum().sum())
    if n:
        log.info("bars_from_holdings: adjusted %d split events", n)
    out = adj.stack().reset_index()
    out.columns = cols
    return out


BARS_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_bars (
    date TEXT NOT NULL, ticker TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    UNIQUE(date, ticker)
);
CREATE INDEX IF NOT EXISTS idx_bars_ticker ON daily_bars(ticker);
"""


def store_bars(conn: sqlite3.Connection, bars: pd.DataFrame) -> int:
    from .database import transaction
    rows = bars[["date", "ticker", "open", "high", "low", "close", "volume"]].itertuples(index=False, name=None)
    with transaction(conn):
        for stmt in BARS_SCHEMA.split(";"):
            if stmt.strip():
                conn.execute(stmt)
        cur = conn.executemany("INSERT OR REPLACE INTO daily_bars VALUES (?,?,?,?,?,?,?)", rows)
    return cur.rowcount


def load_bars(conn: sqlite3.Connection) -> Optional[pd.DataFrame]:
    try:
        df = pd.read_sql("SELECT date, ticker, open, high, low, close, volume FROM daily_bars", conn)
    except Exception:
        return None
    return df if len(df) else None


def load_market_bars(conn: sqlite3.Connection) -> pd.DataFrame:
    """Prefer real OHLCV bars (`daily_bars`); fall back to the holdings prices."""
    bars = load_bars(conn)
    if bars is not None:
        log.info("using %d OHLCV bars from daily_bars", len(bars))
        return bars
    return bars_from_holdings(conn)


# ── universe ────────────────────────────────────────────────────────────────
def membership_start(conn: sqlite3.Connection) -> Dict[str, str]:
    """Start date of each ticker's CURRENT membership spell in the fund."""
    dates = [r[0] for r in conn.execute("SELECT fetch_date FROM snapshots ORDER BY fetch_date")]
    idx = {d: i for i, d in enumerate(dates)}
    rows = conn.execute("SELECT ticker, snapshot_date FROM holdings ORDER BY ticker, snapshot_date").fetchall()
    start: Dict[str, str] = {}
    prev_t, prev_i = None, None
    for t, d in rows:
        i = idx.get(d)
        if i is None:
            continue
        if t != prev_t or prev_i is None or i - prev_i > 1:
            start[t] = d
        prev_t, prev_i = t, i
    return start


class Universe:
    """Issuer-level membership of the fund, per date."""

    def __init__(self, conn: sqlite3.Connection, dates: List[str], recent_add_days: int = 20):
        self.conn = conn
        self.dates = dates
        self.recent_add_days = recent_add_days
        self.starts = membership_start(conn)
        self._cache: Dict[str, Dict[str, dict]] = {}

    def on(self, date: str) -> Dict[str, dict]:
        """{issuer: {"ticker", "recent_add"}} for holdings on `date`.  Share classes of one
        issuer (GOOG/GOOGL) collapse to one entry; `recent_add` is true when the ticker joined
        the fund within the last `recent_add_days` sessions."""
        if date in self._cache:
            return self._cache[date]
        univ: Dict[str, dict] = {}
        i = self.dates.index(date) if date in self.dates else None
        for (t,) in self.conn.execute("SELECT ticker FROM holdings WHERE snapshot_date=?", (date,)):
            if not is_tradeable_symbol(t):
                continue
            k = issuer_key(t)
            fs = self.starts.get(t)
            recent = bool(i is not None and fs and fs in self.dates and fs != self.dates[0]
                          and i - self.dates.index(fs) <= self.recent_add_days)
            e = univ.setdefault(k, {"ticker": t, "recent_add": False})
            e["recent_add"] = e["recent_add"] or recent
        self._cache[date] = univ
        return univ
