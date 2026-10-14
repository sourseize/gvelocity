"""
market_data — price bars and the tradeable universe.

Bars come from one of two places:
  * `daily_bars` table (OHLCV with volume, split-adjusted) — filled by `fetch_daily_bars()`
    from Alpaca Market Data; this enables the volume rules of every strategy.
  * fallback: the BlackRock valuation price in the `holdings` tables of all tracked ETFs,
    merged into one close series per ticker and split-adjusted (price / k with shares * k
    on the same day is a split, not a crash).

The universe is issuer-level: GOOG and GOOGL are one company and may be held once.
"""
from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
from typing import Dict, Iterable, List, Optional

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
def bars_from_holdings(conns: Dict[str, sqlite3.Connection]) -> pd.DataFrame:
    """Long DataFrame(date, ticker, close) from the holdings tables, split-adjusted."""
    frames = [pd.read_sql(
        "SELECT snapshot_date AS date, ticker, price AS close, shares FROM holdings "
        "WHERE price IS NOT NULL AND price > 0", conn) for conn in conns.values()]
    cols = ["date", "ticker", "close"]
    if not frames:
        return pd.DataFrame(columns=cols)
    h = pd.concat(frames, ignore_index=True)
    h = h[h.ticker.map(is_tradeable_symbol)]
    if h.empty:                       # databases exist but hold no priced holdings rows
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


def fetch_daily_bars(tickers: Iterable[str], start: _dt.date, end: Optional[_dt.date] = None,
                     api_key: str = "", secret_key: str = "") -> pd.DataFrame:
    """Daily OHLCV from Alpaca Market Data (free IEX feed), split/dividend adjusted."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import Adjustment, DataFeed
    client = StockHistoricalDataClient(api_key or None, secret_key or None)
    tickers = sorted({t for t in tickers if is_tradeable_symbol(t)})
    frames = []
    for j in range(0, len(tickers), 200):
        req = StockBarsRequest(symbol_or_symbols=tickers[j:j + 200], timeframe=TimeFrame.Day,
                               start=start, end=end, adjustment=Adjustment.ALL, feed=DataFeed.IEX)
        df = client.get_stock_bars(req).df
        if df.empty:
            continue
        df = df.reset_index().rename(columns={"symbol": "ticker", "timestamp": "date"})
        df["date"] = pd.to_datetime(df["date"]).dt.date.astype(str)
        frames.append(df[["date", "ticker", "open", "high", "low", "close", "volume"]])
    cols = ["date", "ticker", "open", "high", "low", "close", "volume"]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols)


def store_bars(conn: sqlite3.Connection, bars: pd.DataFrame) -> int:
    conn.executescript(BARS_SCHEMA)
    rows = bars[["date", "ticker", "open", "high", "low", "close", "volume"]].itertuples(index=False, name=None)
    cur = conn.executemany("INSERT OR REPLACE INTO daily_bars VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    return cur.rowcount


def load_bars(conn: sqlite3.Connection) -> Optional[pd.DataFrame]:
    try:
        df = pd.read_sql("SELECT date, ticker, open, high, low, close, volume FROM daily_bars", conn)
    except Exception:
        return None
    return df if len(df) else None


def load_market_bars(conns: Dict[str, sqlite3.Connection], book_conn: Optional[sqlite3.Connection] = None) -> pd.DataFrame:
    """Prefer real OHLCV bars from the book DB; fall back to the holdings prices."""
    if book_conn is not None:
        bars = load_bars(book_conn)
        if bars is not None:
            log.info("using %d OHLCV bars from daily_bars", len(bars))
            return bars
    return bars_from_holdings(conns)


# ── universe ────────────────────────────────────────────────────────────────
def membership_start(conn: sqlite3.Connection) -> Dict[str, str]:
    """Start date of each ticker's CURRENT membership spell in an ETF."""
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
    """Issuer-level membership of all tracked ETFs, per date."""

    def __init__(self, conns: Dict[str, sqlite3.Connection], dates: List[str], recent_add_days: int = 20):
        self.conns = conns
        self.dates = dates
        self.recent_add_days = recent_add_days
        self.starts = {etf: membership_start(c) for etf, c in conns.items()}
        self._cache: Dict[str, Dict[str, dict]] = {}

    def on(self, date: str) -> Dict[str, dict]:
        """{issuer: {"ticker", "etfs", "recent_add"}} for holdings on `date`."""
        if date in self._cache:
            return self._cache[date]
        univ: Dict[str, dict] = {}
        i = self.dates.index(date) if date in self.dates else None
        for etf, conn in self.conns.items():
            for (t,) in conn.execute("SELECT ticker FROM holdings WHERE snapshot_date=?", (date,)):
                if not is_tradeable_symbol(t):
                    continue
                k = issuer_key(t)
                fs = self.starts.get(etf, {}).get(t)
                recent = bool(i is not None and fs and fs in self.dates and fs != self.dates[0]
                              and i - self.dates.index(fs) <= self.recent_add_days)
                e = univ.setdefault(k, {"ticker": t, "etfs": [], "recent_add": False})
                if etf not in e["etfs"]:
                    e["etfs"].append(etf)
                e["recent_add"] = e["recent_add"] or recent
        self._cache[date] = univ
        return univ
