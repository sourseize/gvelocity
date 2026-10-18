"""
strategy — Velocity's ONE strategy: a trend follower (Donchian breakout inside an established
uptrend, ranked by relative strength, ridden with a volatility-scaled trailing stop).

ENTRY  close > SMA50 and SMA50 rising · close >= 55-day high · relative strength (60-day ROC
       percentile within the fund) >= rs_min_pct · not a gap day (today's move < 3 sigma)
RANK   relative-strength percentile (higher first when slots are limited)
STOP   volatility-scaled trailing stop (2.5 * sigma20 * sqrt(10), clamped 8–25 %), ratcheted on
       the highest close
EXIT   close < 20-day low · 2 closes < SMA50 · time stop 30 d underwater (engine)

The strategy is a pure signal generator on close prices: it never touches cash, orders or
persistence — the Engine and Book do that.  Entry signals carry the risk parameters (initial
stop, trailing fraction) so the Engine manages stops / time stops / dropped-from-fund exits
uniformly.  `explain()` gives the per-holding state the pages show.

Why these rules (see docs/REVIEW-AND-PLAN.md): on the stored history the system's edge comes
from the asymmetric exits (cut losers on the 20-day low / broken SMA50, let winners run on the
trailing stop), not from the entry filter — every parameter below sits on a plateau, and
variants that replaced the entry with pure momentum ranking were not more consistent across
rolling windows.  Volume rules and the "recently added" shortcut were removed: the holdings
file carries no volume and a new name has no history, so neither could ever fire.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from .indicators import Indicators

STRATEGY_NAME = "trend_follow"


@dataclass
class EntrySignal:
    issuer: str
    ticker: str
    ref_price: float
    stop_price: float                 # initial hard stop (exit on close below)
    trail: Optional[float] = None     # trailing fraction from the highest close (None = no trailing)
    target_price: Optional[float] = None
    rank: float = 0.0                 # higher = better; fills limited slots first
    reason: str = ""
    checks: Dict[str, bool] = field(default_factory=dict)
    meta: Dict[str, float] = field(default_factory=dict)

    @property
    def stop_distance(self) -> float:
        return max((self.ref_price - self.stop_price) / self.ref_price, 1e-4)


@dataclass
class ExitSignal:
    issuer: str
    ticker: str
    ref_price: float
    reason: str


def parse_params(items) -> dict:
    """['breakout_len=40', 'rs_min_pct=0.6'] -> {'breakout_len': 40, 'rs_min_pct': 0.6}"""
    out = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--param expects key=value, got {item!r}")
        k, v = item.split("=", 1)
        try:
            val = int(v)
        except ValueError:
            try:
                val = float(v)
            except ValueError:
                val = {"true": True, "false": False}.get(v.lower(), v)
        out[k.strip()] = val
    return out


class TrendFollow:
    name = STRATEGY_NAME
    label = "Trend Follower"
    description = ("Buys 55-day breakouts of relative-strength leaders in an uptrend, rides them with a "
                   "volatility-scaled trailing stop and exits on a 20-day low or a broken 50-day average.")
    defaults: Dict[str, object] = dict(sma_len=50, sma_slope_lag=5, breakout_len=55, exit_low_len=20, rs_len=60,
                                       rs_min_pct=0.50, gap_sigma=3.0, trail_mult=2.5, trail_min=0.08, trail_max=0.25,
                                       sma_break_days=2, time_stop_days=30)

    def __init__(self, **params):
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"{self.name}: unknown parameter(s) {sorted(unknown)}; valid: {sorted(self.defaults)}")
        for k, v in params.items():
            self._check_param(k, v)
        self.p: Dict[str, object] = {**self.defaults, **params}
        self.time_stop_days = int(self.p["time_stop_days"])
        self.ind: Optional[Indicators] = None

    def _check_param(self, key: str, value) -> None:
        """A parameter must have the type of its default and, for numbers, be finite and >= 0 — so a
        bad --param fails here with a clear message instead of deep inside pandas."""
        default = self.defaults[key]
        what = f"{self.name}: parameter {key!r}"
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{what} expects true/false, got {value!r}")
        elif isinstance(default, int):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{what} expects an integer (default {default}), got {value!r}")
            if value < 0:
                raise ValueError(f"{what} must be >= 0, got {value}")
        elif isinstance(default, float):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{what} expects a number (default {default}), got {value!r}")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{what} must be a finite number >= 0, got {value}")

    def params_dict(self) -> dict:
        return dict(self.p)

    def warmup(self) -> int:
        """Bars of history a ticker needs before its first signal can fire."""
        p = self.p
        return max(p["sma_len"] + p["sma_slope_lag"], p["breakout_len"] + 1, p["rs_len"] + 1, p["exit_low_len"] + 1, 21)

    # ── lifecycle ──
    def prepare(self, ind: Indicators) -> None:
        """Compute every rule once, for all dates and tickers (wide boolean frames)."""
        self.ind = ind
        p = self.p
        self.trend = (ind.close > ind.sma(p["sma_len"])) & ind.sma_rising(p["sma_len"], p["sma_slope_lag"])
        self.brk = ind.close >= ind.hh(p["breakout_len"])
        self.rs = ind.rs_pct(p["rs_len"])
        self.rs_ok = self.rs >= p["rs_min_pct"]
        self.gap_ok = ind.gap_ok(p["gap_sigma"])
        self.trail = ind.vol_trail(p["trail_mult"], 10, p["trail_min"], p["trail_max"])
        self.don_low = ind.close < ind.ll(p["exit_low_len"])
        self.trend_break = ind.below_sma_days(p["sma_len"]) >= p["sma_break_days"]
        self.entry_ok = self.trend & self.brk & self.rs_ok & self.gap_ok

    # ── rules ──
    def checks(self, date: str, ticker: str) -> Dict[str, bool]:
        ind = self.ind
        return {
            f"uptrend (close > SMA{self.p['sma_len']}, rising)": ind.flag(self.trend, date, ticker),
            f"{self.p['breakout_len']}d breakout": ind.flag(self.brk, date, ticker),
            f"relative strength >= {self.p['rs_min_pct']:.0%}": ind.flag(self.rs_ok, date, ticker),
            "not a gap day": ind.flag(self.gap_ok, date, ticker),
        }

    def entries(self, date: str, univ: Dict[str, dict], book) -> List[EntrySignal]:
        """Ranked entry candidates for `date` among the fund's issuers not already held."""
        ind = self.ind
        out = []
        for issuer, meta in univ.items():
            if issuer in book.positions:
                continue
            t = meta["ticker"]
            if t not in ind.tickers or not ind.flag(self.entry_ok, date, t):
                continue
            px = float(ind.at(ind.close, date, t))
            trail = float(ind.at(self.trail, date, t))
            if np.isnan(trail):
                trail = 0.15
            rs = float(ind.at(self.rs, date, t))
            reason = f"{self.p['breakout_len']}d breakout · RS pctile {rs:.0%} · trail {trail:.0%}"
            out.append(EntrySignal(issuer, t, px, stop_price=px * (1 - trail), trail=trail, rank=rs,
                                   reason=reason, checks=self.checks(date, t)))
        out.sort(key=lambda s: -s.rank)
        return out

    def exits(self, date: str, book) -> List[ExitSignal]:
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
        return out

    def explain(self, date: str, ticker: str) -> dict:
        """Per-holding signal state for the pages: ENTRY / EXIT / WATCH / NONE (+ HOLD is the engine's)."""
        ind = self.ind
        if ind is None or ticker not in ind.tickers or date not in ind.dates:
            return {"state": "NONE", "checks": {}, "note": "no bar data"}
        checks = self.checks(date, ticker)
        vals = list(checks.values())
        if all(vals):
            state = "ENTRY"
        elif ind.flag(self.don_low, date, ticker) or ind.flag(self.trend_break, date, ticker):
            state = "EXIT"
        elif vals[0] and vals[2]:           # in an uptrend with relative strength: the setup is forming
            state = "WATCH"
        else:
            state = "NONE"
        rs = ind.at(self.rs, date, ticker)
        note = f"RS pctile {rs:.0%}" if not np.isnan(rs) else ""
        return {"state": state, "checks": checks, "note": note}

    def __repr__(self) -> str:
        return f"<TrendFollow {self.p}>"


def make_strategy(**params) -> TrendFollow:
    return TrendFollow(**params)
