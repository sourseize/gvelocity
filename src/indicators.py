"""
indicators — vectorised indicator set on a wide (date x ticker) close-price frame.

`Indicators(bars)` pivots the long bar frame once; every accessor is cached so the strategy and
the analytics can ask for `ind.sma(50)`, `ind.hh(55)`, `ind.rs_pct(60)` ... without recomputation.
Velocity's prices are the fund's valuation prices (one close per ticker per session), so every
indicator here is close-only.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Dict, List, Optional

import numpy as np
import pandas as pd


class Indicators:
    def __init__(self, bars: pd.DataFrame):
        if "close" not in bars.columns or bars.empty or bars["close"].isna().all():
            raise ValueError("no price bars: the bar frame has no 'close' values")
        self.close: pd.DataFrame = bars.pivot(index="date", columns="ticker", values="close").sort_index()
        self.dates: List[str] = list(self.close.index)
        self.tickers: List[str] = list(self.close.columns)
        self.index: Dict[str, int] = {d: i for i, d in enumerate(self.dates)}
        self.ret: pd.DataFrame = self.close.pct_change(fill_method=None)

    # ── trend / momentum ──
    @lru_cache(maxsize=None)
    def sma(self, n: int) -> pd.DataFrame:
        return self.close.rolling(n, min_periods=n).mean()

    @lru_cache(maxsize=None)
    def sma_rising(self, n: int, lag: int = 5) -> pd.DataFrame:
        s = self.sma(n)
        return s > s.shift(lag)

    @lru_cache(maxsize=None)
    def roc(self, n: int) -> pd.DataFrame:
        return self.close / self.close.shift(n) - 1

    @lru_cache(maxsize=None)
    def rs_pct(self, n: int) -> pd.DataFrame:
        """Cross-sectional percentile of the n-day ROC (relative strength within the fund)."""
        return self.roc(n).rank(axis=1, pct=True)

    # ── ranges ──
    @lru_cache(maxsize=None)
    def hh(self, n: int) -> pd.DataFrame:
        """Highest close of the PREVIOUS n bars (today excluded)."""
        return self.close.shift().rolling(n, min_periods=n).max()

    @lru_cache(maxsize=None)
    def ll(self, n: int) -> pd.DataFrame:
        """Lowest close of the PREVIOUS n bars (today excluded)."""
        return self.close.shift().rolling(n, min_periods=n).min()

    # ── volatility ──
    @lru_cache(maxsize=None)
    def sigma(self, n: int = 20) -> pd.DataFrame:
        return self.ret.rolling(n, min_periods=int(n * 0.75)).std()

    def vol_trail(self, mult: float = 2.5, horizon: int = 10, lo: float = 0.08, hi: float = 0.25, n: int = 20) -> pd.DataFrame:
        """Volatility-scaled trailing-stop fraction, clamped to [lo, hi]."""
        return (mult * self.sigma(n) * np.sqrt(horizon)).clip(lo, hi)

    @lru_cache(maxsize=None)
    def gap_ok(self, k: float = 3.0, n: int = 20) -> pd.DataFrame:
        """Today's move is not a blow-off: ret < k * yesterday's sigma."""
        return self.ret < k * self.sigma(n).shift()

    @lru_cache(maxsize=None)
    def below_sma_days(self, n: int) -> pd.DataFrame:
        """Consecutive closes below the n-day SMA (0 when above)."""
        below = (self.close < self.sma(n)).astype(int)
        return below.apply(lambda col: col * (col.groupby((col == 0).cumsum()).cumcount() + 1))

    # ── scalar access ──
    def at(self, frame: Optional[pd.DataFrame], date: str, ticker: str):
        if frame is None or ticker not in frame.columns or date not in frame.index:
            return np.nan
        return frame.at[date, ticker]

    def flag(self, frame: Optional[pd.DataFrame], date: str, ticker: str) -> bool:
        v = self.at(frame, date, ticker)
        return bool(v) and not pd.isna(v)
