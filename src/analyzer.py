"""
Descriptive per-holding metrics (price ROC, weight rank, 52-week drawdown), as of the date analysed.
Trading signals are NOT produced here — see src/strategy.py.
"""

import math
from typing import Optional, Tuple, List, Dict
from .database import daily_history, weekly_history

def roc(values: list[float], n: int) -> Optional[float]:
    """
    Rate of change: (v[0] - v[n]) / v[n]

    Args:
        values: List of values (most recent first)
        n: Number of periods to look back

    Returns:
        Rate of change or None if not enough data or division by zero
    """
    if len(values) < n + 1 or values[n] == 0:
        return None
    return (values[0] - values[n]) / values[n]


def max_drawdown(prices: list[float]) -> Optional[float]:
    """
    Calculate maximum drawdown: (current - peak) / peak

    Args:
        prices: List of prices (most recent first)

    Returns:
        Drawdown as negative number (or zero) or None if no data
    """
    if not prices:
        return None
    peak = max(prices)
    return (prices[0] - peak) / peak if peak else None


def momentum_consistency(prices: list[float], lag: int = 4) -> Optional[float]:
    """
    Fraction of periods where price > price lag periods ago

    Args:
        prices: List of prices (most recent first)
        lag: Number of periods to look back

    Returns:
        Ratio of up periods or None if not enough data
    """
    if len(prices) < lag + 1:
        return None
    hits = sum(1 for i in range(len(prices) - lag) if prices[i] > prices[i + lag])
    return hits / (len(prices) - lag)


class DayWeights:
    """The weight distribution of one snapshot, computed once per day and shared by every holding
    (percentile and z-score of a ticker's weight among all holdings that day)."""

    def __init__(self, conn, today: str):
        self.w = {r["ticker"]: r["weight_pct"] for r in conn.execute(
            "SELECT ticker, weight_pct FROM holdings WHERE snapshot_date=? AND weight_pct IS NOT NULL", (today,))}
        self.sorted = sorted(self.w.values())
        n = len(self.sorted)
        self.mean = sum(self.sorted) / n if n else None
        var = sum((x - self.mean) ** 2 for x in self.sorted) / n if n else 0.0
        self.std = math.sqrt(var) if var else None

    def percentile(self, ticker: str) -> Optional[float]:
        """Percentile (0-100) of the ticker's weight among all holdings of the day."""
        if len(self.sorted) < 2 or ticker not in self.w:
            return None
        import bisect
        below = bisect.bisect_left(self.sorted, self.w[ticker])
        return below / len(self.sorted) * 100

    def zscore(self, ticker: str) -> Optional[float]:
        if len(self.sorted) < 2 or ticker not in self.w or not self.std:
            return None
        return (self.w[ticker] - self.mean) / self.std


def weight_percentile(conn, ticker: str, today: str) -> Optional[float]:
    return DayWeights(conn, today).percentile(ticker)


def weight_zscore(conn, ticker: str, today: str) -> Optional[float]:
    return DayWeights(conn, today).zscore(ticker)


def compute_metrics(conn, ticker: str, today: str, weights: Optional[DayWeights] = None) -> dict:
    """
    Calculate all metrics for a given ticker and date.

    Args:
        conn: Database connection
        ticker: Ticker symbol
        today: Date in YYYY-MM-DD format
        weights: the day's DayWeights (computed once per day by the caller; built here when omitted)

    Returns:
        Dictionary of calculated metrics
    """
    weights = weights or DayWeights(conn, today)
    # Get daily and weekly history up to `today`
    dh = daily_history(conn, ticker, 25, asof=today)
    wh = weekly_history(conn, ticker, 52, asof=today)

    # Extract price arrays
    dp = [r["price"] for r in dh if r.get("price") is not None]
    wp = [r["price"] for r in wh if r.get("price") is not None]

    # First seen date (on or before today)
    first_seen_row = conn.execute(
        "SELECT MIN(snapshot_date) as d FROM holdings WHERE ticker=? AND snapshot_date<=?", (ticker, today)
    ).fetchone()
    first_seen = first_seen_row["d"] if first_seen_row else None

    # Helper to get value for specific date/column
    def fetch_val(date, col):
        row = conn.execute(
            f"SELECT {col} FROM holdings WHERE ticker=? AND snapshot_date=?",
            (ticker, date)
        ).fetchone()
        return row[col] if row else None

    # Current values (most recent)
    price_now = dp[0] if dp else None
    price_entry = fetch_val(first_seen, "price") if first_seen else None
    weight_now = fetch_val(today, "weight_pct")
    weight_entry = fetch_val(first_seen, "weight_pct") if first_seen else None

    # Calculate returns since entry
    price_roc_entry = None
    weight_roc_entry = None
    if first_seen and first_seen != today:
        if price_now and price_entry and price_entry > 0:
            price_roc_entry = (price_now - price_entry) / price_entry
        if weight_now and weight_entry and weight_entry > 0:
            weight_roc_entry = (weight_now - weight_entry) / weight_entry

    # Get current record for name, sector, currency
    current_record = conn.execute(
        "SELECT currency, name, sector FROM holdings WHERE ticker=? AND snapshot_date=?",
        (ticker, today)
    ).fetchone()

    # Build metrics dictionary
    metrics = {
        "ticker": ticker,
        "name": current_record["name"] if current_record else ticker,
        "sector": current_record["sector"] if current_record else "",
        "currency": current_record["currency"] if current_record else "",
        "first_seen": first_seen,
        "days_in_etf": len(dh),
        "current_price": price_now,
        "price_at_entry": price_entry,
        "current_weight": weight_now,
        "weight_at_entry": weight_entry,
        "price_roc_since_entry": price_roc_entry,
        "weight_roc_since_entry": weight_roc_entry,
        "price_roc_5d": roc(dp, 5) if len(dp) > 5 else None,
        "price_roc_10d": roc(dp, 10) if len(dp) > 10 else None,
        "price_roc_20d": roc(dp, 20) if len(dp) > 20 else None,
        "price_roc_52w": roc(wp, 51) if len(wp) > 51 else None,
        "weight_percentile": weights.percentile(ticker),
        "weight_zscore": weights.zscore(ticker),
        "drawdown_from_52w_high": max_drawdown(wp) if wp else None,
        "momentum_consistency": momentum_consistency(wp),
        "daily_history": dh[:20],  # Last 20 days for display
        "weekly_history": wh[:52],  # Last 52 weeks for display
    }

    return metrics