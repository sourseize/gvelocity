"""
Strategy interface.

A strategy is a pure signal generator.  It never touches cash, orders or persistence —
the Engine and Book do that.  Every strategy implements:

    prepare(ind, universe)      compute whatever it needs once, for all dates
    entries(date, univ, book)   ranked list of EntrySignal for `date`
    manage(date, book)          optional: adjust stops/targets of open positions
    exits(date, book)           list of ExitSignal for positions in `book`
    explain(date, ticker)       {"state": ..., "checks": {...}, "note": ...} for the HTML report

Entry signals carry the risk parameters (initial stop, optional trailing fraction, optional
target) so the Book can size the position and the Engine can manage generic exits
(stop / trail / target / time stop / dropped-from-ETF) uniformly for every strategy.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, TYPE_CHECKING

from ..indicators import Indicators

if TYPE_CHECKING:  # pragma: no cover
    from ..book import Book
    from ..market_data import Universe


@dataclass
class EntrySignal:
    issuer: str
    ticker: str
    ref_price: float
    stop_price: float                 # initial hard stop (exit on close below)
    trail: Optional[float] = None     # trailing fraction from highest close (None = no trailing)
    target_price: Optional[float] = None
    rank: float = 0.0                 # higher = better; used to fill limited slots
    reason: str = ""
    etfs: List[str] = field(default_factory=list)
    checks: Dict[str, bool] = field(default_factory=dict)
    meta: Dict[str, float] = field(default_factory=dict)   # strategy-specific levels kept on the position

    @property
    def stop_distance(self) -> float:
        return max((self.ref_price - self.stop_price) / self.ref_price, 1e-4)


@dataclass
class ExitSignal:
    issuer: str
    ticker: str
    ref_price: float
    reason: str


class Strategy:
    name: str = "base"
    label: str = "Base"
    description: str = ""
    #: parameters with defaults; overridable via --param key=value
    defaults: Dict[str, object] = {}
    #: bars needed before the first signal can be produced
    warmup_days: int = 0
    #: engine-level time stop (days underwater) — 0 disables
    time_stop_days: int = 0

    def __init__(self, **params):
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"{self.name}: unknown parameter(s) {sorted(unknown)}; valid: {sorted(self.defaults)}")
        for k, v in params.items():
            self._check_param(k, v)
        self.p: Dict[str, object] = {**self.defaults, **params}
        self.ind: Optional[Indicators] = None

    def _check_param(self, key: str, value) -> None:
        """A parameter must have the same type as its default (bool / int / float / str) and, for
        numbers, be finite and non-negative — so a bad --param fails here with a clear message
        instead of deep inside pandas during prepare()."""
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
        elif isinstance(default, str) and not isinstance(value, str):
            raise ValueError(f"{what} expects text, got {value!r}")

    # ── lifecycle ──
    def prepare(self, ind: Indicators, universe: "Universe") -> None:
        self.ind = ind

    def entries(self, date: str, univ: Dict[str, dict], book: "Book") -> List[EntrySignal]:
        raise NotImplementedError

    def manage(self, date: str, book: "Book") -> None:
        """Optional: adjust stops/targets of open positions (e.g. move to break-even, start trailing)."""
        return None

    def exits(self, date: str, book: "Book") -> List[ExitSignal]:
        return []

    def explain(self, date: str, ticker: str) -> dict:
        """Per-holding signal state for the report: state in {ENTRY, HOLD, EXIT, WATCH, NONE}."""
        return {"state": "NONE", "checks": {}, "note": ""}

    def warmup(self) -> int:
        """Bars of history needed before the first signal can fire, for the CURRENT parameters
        (strategies override this from their look-backs; `warmup_days` is the documented default)."""
        return self.warmup_days

    # ── helpers ──
    def params_dict(self) -> dict:
        return dict(self.p)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.p}>"
