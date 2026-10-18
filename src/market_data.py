"""
market_data — price bars and the tradeable universe, both from the fund's holdings history.

Bars: the fund's valuation price in the `holdings` table, one close per ticker per session,
split-adjusted (price / k with shares * k on the same day is a split, not a crash).  A ticker
has prices only while it is in the fund, so a newly added name starts with no history and
cannot signal until it has been held for the strategy's warm-up.

The universe is issuer-level: GOOG and GOOGL are one company and may be held once.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Dict, List

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


load_market_bars = bars_from_holdings


# ── universe ────────────────────────────────────────────────────────────────
class Universe:
    """Issuer-level membership of the fund, per date."""

    def __init__(self, conn: sqlite3.Connection, dates: List[str]):
        self.conn = conn
        self.dates = dates
        self._cache: Dict[str, Dict[str, dict]] = {}

    def on(self, date: str) -> Dict[str, dict]:
        """{issuer: {"ticker", "sector"}} for the holdings on `date`; share classes of one issuer
        (GOOG/GOOGL) collapse to one entry."""
        if date in self._cache:
            return self._cache[date]
        univ: Dict[str, dict] = {}
        for t, sector in self.conn.execute("SELECT ticker, sector FROM holdings WHERE snapshot_date=?", (date,)):
            if not is_tradeable_symbol(t):
                continue
            univ.setdefault(issuer_key(t), {"ticker": t, "sector": sector or ""})
        self._cache[date] = univ
        return univ
