"""
double_bottom — Double Top / Double Bottom reversal patterns (long-only book).

PATTERN  two swing lows (window +/- 5 bars) within 3 % of each other, 10–60 bars apart, with
         the intermediate peak ("neckline") at least 5 % above the lows.
ENTRY    first close above the neckline within 15 bars of the second low being confirmed
         · not a gap day · [volume: RVOL >= 1.3 on the neckline break — when bars carry volume]
STOP     1 % below the lower of the two lows
TARGET   neckline + 1 x (neckline - low)  (measured move)
EXIT     target · stop · a confirmed DOUBLE TOP on a held name (close below the trough between
         the two tops) · time stop 40 d underwater

A double top is the mirror image; in this long-only book it is used as an exit (and the
report flags it), never as a short.
"""
from __future__ import annotations

from typing import Dict, List

import numpy as np
import pandas as pd

from .base import Strategy, EntrySignal, ExitSignal


class DoubleBottom(Strategy):
    name = "double_bottom"
    label = "Double Top / Bottom"
    description = ("Buys the neckline break of a completed double bottom with a stop under the lows and a "
                   "measured-move target; a completed double top closes an open position.")
    defaults = dict(swing_w=5, tol=0.03, sep_min=10, sep_max=60, min_depth=0.05, max_wait=15,
                    stop_buffer=0.01, target_mult=1.0, gap_sigma=3.0, rvol_entry=1.3, time_stop_days=40)
    warmup_days = 30

    def warmup(self) -> int:
        # first swing low needs `swing_w` bars on each side, the second one `sep_min` bars later,
        # confirmation `swing_w` bars after that; gap filter needs ~20 bars of returns
        return max(2 * self.p["swing_w"] + self.p["sep_min"] + 1, self.warmup_days)

    # ── pattern scan ──
    def _scan(self, c: np.ndarray, lows: np.ndarray, highs: np.ndarray):
        p = self.p; n = len(c); w = int(p["swing_w"])
        entry = np.zeros(n, bool); pending = np.zeros(n, bool); dtop = np.zeros(n, bool)
        neck = np.full(n, np.nan); low = np.full(n, np.nan)
        sl = np.flatnonzero(lows); sh = np.flatnonzero(highs)

        def pairs(idx):
            for k in range(len(idx)):
                for j in (k + 1, k + 2):
                    if j < len(idx):
                        yield idx[k], idx[j]

        for a, b in pairs(sl):
            if not p["sep_min"] <= b - a <= p["sep_max"]:
                continue
            la, lb = c[a], c[b]
            if np.isnan(la) or np.isnan(lb) or abs(lb - la) / la > p["tol"]:
                continue
            seg = c[a + 1:b]
            if np.isnan(seg).any():
                continue
            nl = seg.max(); lo = min(la, lb)
            if nl / lo - 1 < p["min_depth"]:
                continue
            confirm = b + w
            if confirm >= n:
                continue
            for t in range(confirm, min(n, confirm + int(p["max_wait"]) + 1)):
                if np.isnan(c[t]):
                    continue
                if c[t] > nl:
                    entry[t] = True; neck[t] = nl; low[t] = lo
                    break
                pending[t] = True
        for a, b in pairs(sh):
            if not p["sep_min"] <= b - a <= p["sep_max"]:
                continue
            ha, hb = c[a], c[b]
            if np.isnan(ha) or np.isnan(hb) or abs(hb - ha) / ha > p["tol"]:
                continue
            seg = c[a + 1:b]
            if np.isnan(seg).any():
                continue
            trough = seg.min(); hi = max(ha, hb)
            if hi / trough - 1 < p["min_depth"]:
                continue
            confirm = b + w
            for t in range(confirm, min(n, confirm + int(p["max_wait"]) + 1)):
                if not np.isnan(c[t]) and c[t] < trough:
                    dtop[t] = True
                    break
        return entry, pending, dtop, neck, low

    def prepare(self, ind, universe):
        super().prepare(ind, universe)
        p = self.p
        self.time_stop_days = int(p["time_stop_days"])
        w = int(p["swing_w"])
        sl = ind.swing_low(w); sh = ind.swing_high(w)
        cols = ind.tickers; idx = ind.dates
        E, P, D, N, L = ({} for _ in range(5))
        for t in cols:
            e, pe, d, nk, lo = self._scan(ind.close[t].values.astype(float), sl[t].values, sh[t].values)
            E[t], P[t], D[t], N[t], L[t] = e, pe, d, nk, lo
        self.entry = pd.DataFrame(E, index=idx); self.pending = pd.DataFrame(P, index=idx)
        self.dtop = pd.DataFrame(D, index=idx); self.neck = pd.DataFrame(N, index=idx); self.low = pd.DataFrame(L, index=idx)
        self.gap_ok = ind.gap_ok(p["gap_sigma"])
        self.vol_ok = (ind.rvol() >= p["rvol_entry"]) if ind.has_volume else None

    def _checks(self, date, ticker) -> Dict[str, bool]:
        ind = self.ind
        c = {
            "double bottom completed (neckline break)": ind.flag(self.entry, date, ticker),
            "not a gap day": ind.flag(self.gap_ok, date, ticker),
        }
        if self.vol_ok is not None:
            c["volume on the break (RVOL)"] = ind.flag(self.vol_ok, date, ticker)
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
            nl = float(ind.at(self.neck, date, t)); lo = float(ind.at(self.low, date, t))
            stop = lo * (1 - self.p["stop_buffer"])
            target = nl + self.p["target_mult"] * (nl - lo)
            depth = nl / lo - 1
            reason = f"double bottom {lo:.2f} / neckline {nl:.2f} ({depth:.0%} deep) · stop {stop:.2f} · target {target:.2f}"
            out.append(EntrySignal(issuer, t, px, stop_price=stop, trail=None, target_price=target,
                                   rank=depth, reason=reason, checks=checks,
                                   meta={"neckline": nl, "pattern_low": lo}))
        out.sort(key=lambda s: -s.rank)      # deepest pattern first
        return out

    def exits(self, date, book) -> List[ExitSignal]:
        ind = self.ind
        out = []
        for issuer, pos in book.positions.items():
            t = pos.ticker
            if ind.flag(self.dtop, date, t):
                out.append(ExitSignal(issuer, t, float(ind.at(ind.close, date, t)), "double top completed (close below trough)"))
        return out

    def explain(self, date, ticker) -> dict:
        ind = self.ind
        if ticker not in ind.tickers or date not in ind.dates:
            return {"state": "NONE", "checks": {}, "note": "no bar data"}
        checks = self._checks(date, ticker)
        if all(checks.values()):
            state, note = "ENTRY", f"neckline {ind.at(self.neck, date, ticker):.2f}"
        elif ind.flag(self.dtop, date, ticker):
            state, note = "EXIT", "double top completed"
        elif ind.flag(self.pending, date, ticker):
            state, note = "WATCH", "double bottom confirmed — waiting for neckline break"
        else:
            state, note = "NONE", ""
        return {"state": state, "checks": checks, "note": note}
