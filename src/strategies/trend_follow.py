"""
trend_follow — Trend Follower (Donchian breakout inside an established uptrend).

ENTRY  close > SMA50 and SMA50 rising · close >= 55-day high (20-day for names just added
       to an ETF) · 60-day ROC in the top half of the universe · not a gap day (< 3 sigma)
       · [volume: RVOL >= 1.5 and OBV above its 20-day mean — when bars carry volume]
STOP   volatility-scaled trailing stop (2.5 * sigma20 * sqrt(10), clamped 8–25 %)
EXIT   close < 20-day low · 2 closes < SMA50 · [distribution day] · time stop 30 d underwater
"""
from __future__ import annotations

from typing import Dict, List

import numpy as np

from .base import Strategy, EntrySignal, ExitSignal


class TrendFollow(Strategy):
    name = "trend_follow"
    label = "Trend Follower"
    description = ("Buys 55-day breakouts of relative-strength leaders in an uptrend, rides them with a "
                   "volatility-scaled trailing stop and exits on a 20-day low or a broken 50-day average.")
    defaults = dict(sma_len=50, sma_slope_lag=5, breakout_len=55, breakout_len_new=20, exit_low_len=20,
                    rs_len=60, rs_min_pct=0.50, gap_sigma=3.0, rvol_entry=1.5, rvol_exit=2.0,
                    trail_mult=2.5, trail_min=0.08, trail_max=0.25, sma_break_days=2, time_stop_days=30)
    warmup_days = 60

    def prepare(self, ind, universe):
        super().prepare(ind, universe)
        p = self.p
        self.time_stop_days = int(p["time_stop_days"])
        self.trend = (ind.close > ind.sma(p["sma_len"])) & ind.sma_rising(p["sma_len"], p["sma_slope_lag"])
        self.brk = ind.close >= ind.hh(p["breakout_len"])
        self.brk_new = ind.close >= ind.hh(p["breakout_len_new"])
        self.rs = ind.rs_pct(p["rs_len"])
        self.rs_ok = self.rs >= p["rs_min_pct"]
        self.gap_ok = ind.gap_ok(p["gap_sigma"])
        self.trail = ind.vol_trail(p["trail_mult"], 10, p["trail_min"], p["trail_max"])
        self.don_low = ind.close < ind.ll(p["exit_low_len"])
        self.trend_break = ind.below_sma_days(p["sma_len"]) >= p["sma_break_days"]
        self.vol_ok = None
        self.distribution = None
        if ind.has_volume:
            self.vol_ok = (ind.rvol() >= p["rvol_entry"]) & ind.obv_up()
            if ind.has_range:
                self.distribution = (ind.ret < 0) & (ind.rvol() >= p["rvol_exit"]) & (ind.range_pos() <= 0.25) & (~ind.obv_up())

    def _checks(self, date: str, ticker: str, recent_add: bool = False) -> Dict[str, bool]:
        ind = self.ind
        c = {
            "uptrend (close > SMA50, SMA50 rising)": ind.flag(self.trend, date, ticker),
            f"{self.p['breakout_len_new'] if recent_add else self.p['breakout_len']}d breakout": ind.flag(self.brk_new if recent_add else self.brk, date, ticker),
            "relative strength top half": ind.flag(self.rs_ok, date, ticker),
            "not a gap day": ind.flag(self.gap_ok, date, ticker),
        }
        if self.vol_ok is not None:
            c["volume confirmation (RVOL, OBV)"] = ind.flag(self.vol_ok, date, ticker)
        return c

    def entries(self, date, univ, book) -> List[EntrySignal]:
        ind = self.ind
        out = []
        for issuer, meta in univ.items():
            if issuer in book.positions:
                continue
            t = meta["ticker"]
            if t not in ind.tickers:
                continue
            checks = self._checks(date, t, meta.get("recent_add", False))
            if not all(checks.values()):
                continue
            px = float(ind.at(ind.close, date, t))
            trail = float(ind.at(self.trail, date, t))
            if np.isnan(trail):
                trail = 0.15
            rs = float(ind.at(self.rs, date, t))
            reason = f"{self.p['breakout_len_new'] if meta.get('recent_add') else self.p['breakout_len']}d breakout · RS pctile {rs:.0%} · trail {trail:.0%}"
            if self.vol_ok is not None:
                reason += f" · RVOL {float(ind.at(ind.rvol(), date, t)):.1f}x"
            if meta.get("recent_add"):
                reason += " · recent ETF add"
            out.append(EntrySignal(issuer, t, px, stop_price=px * (1 - trail), trail=trail, rank=rs,
                                   reason=reason, etfs=meta["etfs"], checks=checks))
        out.sort(key=lambda s: -s.rank)
        return out

    def exits(self, date, book) -> List[ExitSignal]:
        ind = self.ind
        out = []
        for issuer, pos in book.positions.items():
            t = pos.ticker
            px = float(ind.at(ind.close, date, t))
            if np.isnan(px):
                continue
            if ind.flag(self.don_low, date, t):
                out.append(ExitSignal(issuer, t, px, f"close below {self.p['exit_low_len']}d low"))
            elif ind.flag(self.trend_break, date, t):
                out.append(ExitSignal(issuer, t, px, f"{self.p['sma_break_days']} closes below SMA{self.p['sma_len']}"))
            elif self.distribution is not None and ind.flag(self.distribution, date, t):
                out.append(ExitSignal(issuer, t, px, "distribution day (heavy volume, weak close, OBV falling)"))
        return out

    def explain(self, date, ticker) -> dict:
        ind = self.ind
        if ticker not in ind.tickers or date not in ind.dates:
            return {"state": "NONE", "checks": {}, "note": "no bar data"}
        checks = self._checks(date, ticker)
        if all(checks.values()):
            state = "ENTRY"
        elif ind.flag(self.don_low, date, ticker) or ind.flag(self.trend_break, date, ticker):
            state = "EXIT"
        elif checks["uptrend (close > SMA50, SMA50 rising)"] and checks["relative strength top half"]:
            state = "WATCH"
        else:
            state = "NONE"
        rs = ind.at(self.rs, date, ticker)
        note = f"RS pctile {rs:.0%}" if not np.isnan(rs) else ""
        return {"state": state, "checks": checks, "note": note}
