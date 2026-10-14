"""
indicators — shared, vectorised indicator set on wide (date x ticker) frames.

`Indicators(bars)` pivots a long bar DataFrame once; every accessor is cached so strategies
can ask for `ind.sma(50)`, `ind.hh(55)`, `ind.rs_pct(60)` etc. without recomputation.
Volume- and range-based indicators return None when the bars carry no volume / high / low
(the BlackRock fallback series), so strategies can degrade to price-only mode explicitly.
"""
from __future__ import annotations

from functools import lru_cache
from typing import List, Optional

import numpy as np
import pandas as pd


def _wide(bars: pd.DataFrame, col: str) -> Optional[pd.DataFrame]:
    if col not in bars.columns or bars[col].isna().all():
        return None
    return bars.pivot(index="date", columns="ticker", values=col).sort_index()


class Indicators:
    def __init__(self, bars: pd.DataFrame):
        self.close: pd.DataFrame = _wide(bars, "close")
        self.high: Optional[pd.DataFrame] = _wide(bars, "high")
        self.low: Optional[pd.DataFrame] = _wide(bars, "low")
        self.volume: Optional[pd.DataFrame] = _wide(bars, "volume")
        self.dates: List[str] = list(self.close.index)
        self.tickers: List[str] = list(self.close.columns)
        self.ret: pd.DataFrame = self.close.pct_change(fill_method=None)

    @property
    def has_volume(self) -> bool:
        return self.volume is not None

    @property
    def has_range(self) -> bool:
        return self.high is not None and self.low is not None

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
        """Cross-sectional percentile of n-day ROC (relative strength within the universe)."""
        return self.roc(n).rank(axis=1, pct=True)

    # ── ranges ──
    @lru_cache(maxsize=None)
    def hh(self, n: int) -> pd.DataFrame:
        """Highest close of the PREVIOUS n bars (today excluded)."""
        src = self.high if self.high is not None else self.close
        return src.shift().rolling(n, min_periods=n).max()

    @lru_cache(maxsize=None)
    def ll(self, n: int) -> pd.DataFrame:
        """Lowest close of the PREVIOUS n bars (today excluded)."""
        src = self.low if self.low is not None else self.close
        return src.shift().rolling(n, min_periods=n).min()

    @lru_cache(maxsize=None)
    def range_width(self, n: int) -> pd.DataFrame:
        """(hh - ll) / ll over the previous n bars — tightness of a base."""
        return (self.hh(n) - self.ll(n)) / self.ll(n)

    # ── volatility ──
    @lru_cache(maxsize=None)
    def sigma(self, n: int = 20) -> pd.DataFrame:
        return self.ret.rolling(n, min_periods=int(n * 0.75)).std()

    @lru_cache(maxsize=None)
    def atr_pct(self, n: int = 14) -> Optional[pd.DataFrame]:
        if not self.has_range:
            return None
        pc = self.close.shift()
        tr = pd.concat([(self.high - self.low), (self.high - pc).abs(), (self.low - pc).abs()]).groupby(level=0).max()
        return tr.rolling(n, min_periods=n).mean() / self.close

    def vol_trail(self, mult: float = 2.5, horizon: int = 10, lo: float = 0.08, hi: float = 0.25, n: int = 20) -> pd.DataFrame:
        """Volatility-scaled trailing-stop fraction, clamped."""
        return (mult * self.sigma(n) * np.sqrt(horizon)).clip(lo, hi)

    @lru_cache(maxsize=None)
    def gap_ok(self, k: float = 3.0, n: int = 20) -> pd.DataFrame:
        """Today's move is not a blow-off: ret < k * sigma (yesterday's sigma)."""
        return self.ret < k * self.sigma(n).shift()

    @lru_cache(maxsize=None)
    def below_sma_days(self, n: int) -> pd.DataFrame:
        below = (self.close < self.sma(n)).astype(int)
        return below.apply(lambda col: col * (col.groupby((col == 0).cumsum()).cumcount() + 1))

    # ── volume ──
    @lru_cache(maxsize=None)
    def rvol(self, n: int = 20) -> Optional[pd.DataFrame]:
        if not self.has_volume:
            return None
        return self.volume / self.volume.rolling(n, min_periods=n).mean()

    @lru_cache(maxsize=None)
    def obv_up(self, n: int = 20) -> Optional[pd.DataFrame]:
        if not self.has_volume:
            return None
        obv = (np.sign(self.ret.fillna(0)) * self.volume).cumsum()
        return obv > obv.rolling(n, min_periods=n).mean()

    @lru_cache(maxsize=None)
    def range_pos(self) -> Optional[pd.DataFrame]:
        """Where the close sits in the day's range: 0 = at the low, 1 = at the high."""
        if not self.has_range:
            return None
        return (self.close - self.low) / (self.high - self.low).replace(0, np.nan)

    # ── swing points (for pattern strategies) ──
    @lru_cache(maxsize=None)
    def swing_low(self, w: int = 5) -> pd.DataFrame:
        """True where the close is the minimum of the +/- w bar window (confirmed w bars later)."""
        src = self.low if self.low is not None else self.close
        win = src.rolling(2 * w + 1, center=True, min_periods=2 * w + 1).min()
        return (src == win) & win.notna()

    @lru_cache(maxsize=None)
    def swing_high(self, w: int = 5) -> pd.DataFrame:
        src = self.high if self.high is not None else self.close
        win = src.rolling(2 * w + 1, center=True, min_periods=2 * w + 1).max()
        return (src == win) & win.notna()

    # ── scalar access ──
    def at(self, frame: Optional[pd.DataFrame], date: str, ticker: str):
        if frame is None or ticker not in frame.columns or date not in frame.index:
            return np.nan
        return frame.at[date, ticker]

    def flag(self, frame: Optional[pd.DataFrame], date: str, ticker: str) -> bool:
        v = self.at(frame, date, ticker)
        return bool(v) and not pd.isna(v)
