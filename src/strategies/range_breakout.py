"""
range_breakout — Range Breakout (tight consolidation, breakout, measured-move target).

SETUP  the previous 20 bars form a tight base: (high - low) / low <= 10 %
ENTRY  close breaks above the base high (> +0.2 %) · close > SMA50 · not a gap day
       · [volume: RVOL >= 1.5 — when bars carry volume]
STOP   fixed at the base low (a close back below the base = failed breakout)
TARGET base high + 2 x base height (measured move).  When `run_after_target` is on (default)
       the target is not sold: the stop moves to break-even and a volatility-scaled trailing
       stop takes over, so a breakout that keeps going is ridden instead of capped.
EXIT   stop · target (if run_after_target is off) · "failed breakout": close back under the
       breakout level after `fail_days` (0 disables) · time stop 40 d underwater
"""
from __future__ import annotations

from typing import Dict, List

import numpy as np

from .base import Strategy, EntrySignal, ExitSignal


class RangeBreakout(Strategy):
    name = "range_breakout"
    label = "Range Breakout"
    description = ("Buys the breakout from a tight 20-day consolidation with a hard stop at the base low "
                   "and a measured-move target of twice the base height.")
    defaults = dict(base_len=20, max_width=0.10, buffer=0.002, sma_len=50, gap_sigma=3.0,
                    rvol_entry=1.5, target_mult=2.0, fail_days=0, run_after_target=True,
                    trail_mult=2.5, trail_min=0.08, trail_max=0.25, time_stop_days=40)
    warmup_days = 50

    def prepare(self, ind, universe):
        super().prepare(ind, universe)
        p = self.p
        self.time_stop_days = int(p["time_stop_days"])
        self.base_hh = ind.hh(p["base_len"])
        self.base_ll = ind.ll(p["base_len"])
        self.width = ind.range_width(p["base_len"])
        self.tight = self.width <= p["max_width"]
        self.brk = ind.close > self.base_hh * (1 + p["buffer"])
        self.above_sma = ind.close > ind.sma(p["sma_len"])
        self.gap_ok = ind.gap_ok(p["gap_sigma"])
        self.vol_ok = (ind.rvol() >= p["rvol_entry"]) if ind.has_volume else None
        self.trail = ind.vol_trail(p["trail_mult"], 10, p["trail_min"], p["trail_max"])

    def manage(self, date, book):
        """At the measured-move target: lock in break-even and switch to a trailing stop."""
        if not self.p["run_after_target"]:
            return
        ind = self.ind
        for pos in book.positions.values():
            if pos.target_price and pos.last_price >= pos.target_price:
                trail = float(ind.at(self.trail, date, pos.ticker))
                pos.trail = trail if not np.isnan(trail) else 0.15
                pos.stop_price = max(pos.stop_price, pos.entry_price, pos.highest_close * (1 - pos.trail))
                pos.target_price = None
                pos.meta["trailing_since"] = date

    def _checks(self, date, ticker) -> Dict[str, bool]:
        ind = self.ind
        c = {
            f"tight {self.p['base_len']}d base (width <= {self.p['max_width']:.0%})": ind.flag(self.tight, date, ticker),
            "close above base high": ind.flag(self.brk, date, ticker),
            f"close above SMA{self.p['sma_len']}": ind.flag(self.above_sma, date, ticker),
            "not a gap day": ind.flag(self.gap_ok, date, ticker),
        }
        if self.vol_ok is not None:
            c["breakout volume (RVOL)"] = ind.flag(self.vol_ok, date, ticker)
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
            checks = self._checks(date, t)
            if not all(checks.values()):
                continue
            px = float(ind.at(ind.close, date, t))
            hh = float(ind.at(self.base_hh, date, t)); ll = float(ind.at(self.base_ll, date, t))
            height = hh - ll
            width = float(ind.at(self.width, date, t))
            target = hh + self.p["target_mult"] * height
            reason = f"breakout from {width:.1%} base [{ll:.2f}–{hh:.2f}] · stop {ll:.2f} · target {target:.2f}"
            if self.vol_ok is not None:
                reason += f" · RVOL {float(ind.at(ind.rvol(), date, t)):.1f}x"
            out.append(EntrySignal(issuer, t, px, stop_price=ll, trail=None, target_price=target,
                                   rank=-width, reason=reason, etfs=meta["etfs"], checks=checks,
                                   meta={"breakout_level": hh, "base_low": ll}))
        out.sort(key=lambda s: -s.rank)      # tightest base first
        return out

    def exits(self, date, book) -> List[ExitSignal]:
        ind = self.ind
        out = []
        for issuer, pos in book.positions.items():
            t = pos.ticker
            px = float(ind.at(ind.close, date, t))
            if np.isnan(px):
                continue
            level = pos.meta.get("breakout_level")
            if self.p["fail_days"] and level and pos.days_held(date, ind.dates) >= self.p["fail_days"] and px < level:
                out.append(ExitSignal(issuer, t, px, f"failed breakout: back below {level:.2f}"))
        return out

    def explain(self, date, ticker) -> dict:
        ind = self.ind
        if ticker not in ind.tickers or date not in ind.dates:
            return {"state": "NONE", "checks": {}, "note": "no bar data"}
        checks = self._checks(date, ticker)
        width = ind.at(self.width, date, ticker)
        if all(checks.values()):
            state = "ENTRY"
        elif checks[f"tight {self.p['base_len']}d base (width <= {self.p['max_width']:.0%})"] and checks[f"close above SMA{self.p['sma_len']}"]:
            state = "WATCH"
        else:
            state = "NONE"
        note = f"base width {width:.1%}" if not np.isnan(width) else ""
        return {"state": state, "checks": checks, "note": note}
