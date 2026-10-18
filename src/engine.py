"""
engine — runs the strategy over trading sessions against a Book.

Generic, strategy-independent rules live here:
  * mark to market, trailing-stop ratchet
  * exits: dropped from the fund · close below stop · target reached · time stop
  * re-entry cooldown after an exit
  * position limit + the `max_positions` policy (see MaxPositionsPolicy)
  * rising stars: signals not taken (book full / no cash) are parked in the book's watchlist,
    ranked first when a slot frees up, and expire once they stop signalling
  * sizing: every position gets an equal share of the investable equity —
        budget = equity x (1 - cash_reserve) / max_positions
    and the cash reserve is never spent (dry powder); a fill that would dip into it is refused
  * fills: "same" books the signal-day close (what the live session does — the holdings price is
    the execution price of this paper book); "next" books the next session's close

The strategy only says *what* to buy/sell and *why*; the engine decides *whether it fits*.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from .book import Book
from .indicators import Indicators
from .exceptions import VelocityError
from .market_data import Universe, bars_from_holdings
from .strategy import TrendFollow, EntrySignal, ExitSignal

log = logging.getLogger(__name__)


@dataclass
class MaxPositionsPolicy:
    """What happens when a new entry signal arrives and the book is full.

    mode = "skip"     : nothing is sold; the best-ranked candidates fill the free slots and the rest
                        wait as rising stars (they re-qualify while the signal persists).
    mode = "replace"  : a candidate may replace the weakest open position — the one with the lowest
                        return since entry — but only if that position is losing money, has been held
                        at least `min_hold` sessions, and the candidate is not the same issuer.
    Winners are never sold to make room: the strategy's own exit rules decide when they go.
    """
    mode: str = "skip"
    min_hold: int = 5


@dataclass
class EngineConfig:
    max_positions: int = 25
    cash_reserve: float = 0.10        # share of equity kept as dry powder, never invested
    commission: float = 1.0
    cooldown_days: int = 10
    policy: MaxPositionsPolicy = field(default_factory=MaxPositionsPolicy)
    rising_priority: bool = False     # waiting signals go to the front of the queue for free slots (else strategy rank)
    rising_max_age: int = 10          # sessions without a repeat signal before a rising star is dropped
    short_history: str = "warn"       # history shorter than the strategy's warm-up: "warn" (live: keep managing
                                      # open positions) or "error" (backtests: the result would be meaningless)


class Engine:
    def __init__(self, strategy: TrendFollow, conn: sqlite3.Connection, cfg: EngineConfig = EngineConfig(),
                 bars: Optional[pd.DataFrame] = None):
        self.strategy = strategy
        self.conn = conn
        self.cfg = cfg
        self.bars = bars if bars is not None else bars_from_holdings(conn)
        try:
            self.ind = Indicators(self.bars)
        except ValueError as e:
            raise VelocityError(f"no price history in the database ({e}) — run `velocity backfill` first") from e
        self.dates: List[str] = self.ind.dates
        self.index: Dict[str, int] = self.ind.index
        need = strategy.warmup()
        if len(self.dates) < need:
            msg = (f"{strategy.name}: only {len(self.dates)} sessions of price history ({self.dates[0]} → {self.dates[-1]}) "
                   f"but the strategy needs {need} bars of warm-up with the current parameters — its entry rules cannot "
                   f"fire until the history is deeper (`velocity backfill --months N`)")
            if cfg.short_history == "error":
                raise VelocityError(msg)
            log.warning(msg)
        self.universe = Universe(conn, self.dates)
        strategy.prepare(self.ind)

    # ── helpers ──
    def price(self, date: str, ticker: str) -> float:
        return float(self.ind.at(self.ind.close, date, ticker))

    def fill_price(self, date: str, ticker: str, fill: str) -> float:
        if fill == "next":
            i = self.index[date]
            return self.price(self.dates[i + 1], ticker) if i + 1 < len(self.dates) else float("nan")
        return self.price(date, ticker)

    def days_held(self, pos, date: str) -> int:
        return pos.days_held(date, self.index)

    # ── one session ──
    def step(self, book: Book, date: str, fill: str = "same", execute: bool = True) -> dict:
        cfg, strat = self.cfg, self.strategy
        i = self.index[date]
        univ = self.universe.on(date)

        # 1. mark to market (ratchets trailing stops)
        for pos in book.positions.values():
            px = self.price(date, pos.ticker)
            if not np.isnan(px):
                pos.update(px)

        # 2. generic exits, then strategy exits
        exits: Dict[str, ExitSignal] = {}
        for issuer, pos in book.positions.items():
            px = pos.last_price
            reason = None
            if issuer not in univ:
                reason = "dropped from the fund"
            elif px < pos.stop_price:
                reason = (f"trailing stop {pos.trail:.0%} (stop {pos.stop_price:.2f})" if pos.trail
                          else f"stop {pos.stop_price:.2f} hit")
            elif pos.target_price and px >= pos.target_price:
                reason = f"target {pos.target_price:.2f} reached"
            elif strat.time_stop_days and self.days_held(pos, date) >= strat.time_stop_days and px < pos.entry_price:
                reason = f"time stop: underwater after {strat.time_stop_days}d"
            if reason:
                exits[issuer] = ExitSignal(issuer, pos.ticker, px, reason)
        for sig in strat.exits(date, book):
            exits.setdefault(sig.issuer, sig)

        # 3. entries (strategy ranks them), cooldown, position limit policy
        cands = [s for s in strat.entries(date, univ, book)
                 if s.issuer not in exits
                 and not (s.issuer in book.last_exit and book.last_exit[s.issuer] in self.index
                          and i - self.index[book.last_exit[s.issuer]] < cfg.cooldown_days)]
        if cfg.rising_priority and book.rising:
            # stable sort: stars that have signalled on more sessions first, strategy order within
            cands.sort(key=lambda s: -book.rising.get(s.issuer, {}).get("seen", 0))
        open_after_exits = len(book.positions) - len(exits)
        slots = max(0, cfg.max_positions - open_after_exits)
        entries: List[EntrySignal] = cands[:slots]
        replaced: List[ExitSignal] = []
        if cfg.policy.mode == "replace":
            for cand in cands[slots:]:
                victims = [p for iss, p in book.positions.items()
                           if iss not in exits and p.pnl_pct < 0 and self.days_held(p, date) >= cfg.policy.min_hold]
                if not victims:
                    break
                worst = min(victims, key=lambda p: p.pnl_pct)
                ex = ExitSignal(worst.issuer, worst.ticker, worst.last_price,
                                f"replaced by {cand.ticker} (weakest: {worst.pnl_pct:+.1%} after {self.days_held(worst, date)}d)")
                exits[worst.issuer] = ex; replaced.append(ex); entries.append(cand)
        skipped = [s for s in cands if s not in entries]

        if execute:
            unfilled = self._execute(book, date, list(exits.values()), entries, fill)
            entries = [s for s in entries if s not in unfilled]
            for s in skipped:
                book.note_rising(s.issuer, s.ticker, date, s.ref_price, s.rank, s.reason, "book full")
            for s in unfilled:
                book.note_rising(s.issuer, s.ticker, date, s.ref_price, s.rank, s.reason, "capital exhausted")
            skipped = skipped + unfilled
            book.prune_rising(date, self.index, cfg.rising_max_age)
            book.last_date = date
            book.record_equity(date)
        return {"date": date, "exits": list(exits.values()), "entries": entries, "replaced": replaced,
                "skipped": skipped, "rising": book.rising_list()}

    def _execute(self, book: Book, date: str, exits: List[ExitSignal], entries: List[EntrySignal], fill: str) -> List[EntrySignal]:
        """Book exits then entries; returns the entries that could not be filled (no cash / no price)."""
        cfg = self.cfg
        unfilled: List[EntrySignal] = []
        i = self.index[date]
        fill_date = date if fill == "same" or i + 1 >= len(self.dates) else self.dates[i + 1]
        for e in exits:
            pos = book.positions[e.issuer]
            px = self.fill_price(date, pos.ticker, fill)
            if np.isnan(px):
                px = e.ref_price
            book.close(e.issuer, px, fill_date, cfg.commission, e.reason, self.days_held(pos, date))
        for s in entries:
            px = self.fill_price(date, s.ticker, fill)
            if np.isnan(px):
                continue
            dollars = book.position_budget(cfg.max_positions, cfg.cash_reserve)
            # keep the stop/target levels as signalled; re-anchor a trailing stop to the actual fill
            stop = s.stop_price if s.trail is None else max(s.stop_price, px * (1 - s.trail))
            if book.open(s.issuer, s.ticker, px, fill_date, dollars, cfg.commission, stop, s.trail, s.target_price,
                         s.reason, s.meta, min_cash=book.reserve(cfg.cash_reserve)) is None:
                unfilled.append(s)
        return unfilled

    # ── backtest ──
    def backtest(self, start: str, end: str, fill: str = "same", capital: float = 100000.0,
                 on_session: Optional[Callable[[str, dict, Book], None]] = None) -> Book:
        """Replay every session in [start, end] into a fresh book.  `on_session(date, result, book)`
        is called after each session (used to record the audit trail when a backtest is registered
        as the live book)."""
        book = Book(self.strategy.name, capital)
        for d in self.dates:
            if start <= d <= end:
                res = self.step(book, d, fill=fill, execute=True)
                if on_session is not None:
                    on_session(d, res, book)
        return book

    # ── signal snapshot for the audit log / report ──
    def signal_rows(self, book: Book, date: str) -> List[tuple]:
        """(ticker, state, note, checks) for every issuer in today's universe; HOLD for held names."""
        rows = []
        for issuer, meta in self.universe.on(date).items():
            t = meta["ticker"]
            ex = self.strategy.explain(date, t)
            state = ex.get("state", "NONE")
            if issuer in book.positions:
                pos = book.positions[issuer]
                note = f"open since {pos.entry_date} @ {pos.entry_price:.2f} · stop {pos.stop_price:.2f}"
                if state != "EXIT":
                    state = "HOLD"
                ex = {**ex, "note": (ex.get("note", "") + " · " + note).strip(" ·")}
            elif state == "EXIT":
                state = "NONE"
            rows.append((t, state, ex.get("note", ""), ex.get("checks", {})))
        return rows

    # ── live session ──
    def run_session(self, book: Book, date: str) -> dict:
        """Evaluate one live session in memory, booking the holdings price of `date` as the fill.
        Idempotent per date.  Nothing is written: the result carries the per-ticker signal states
        (`signals`) and `services.session.persist_session` stores result + book atomically."""
        if book.last_date == date:
            log.info("[%s] %s already processed — skipping", self.strategy.name, date)
            return {"date": date, "exits": [], "entries": [], "replaced": [], "skipped": [], "skipped_session": True}
        if date not in self.index:
            log.warning("[%s] %s not in bar data", self.strategy.name, date)
            return {"date": date, "exits": [], "entries": [], "replaced": [], "skipped": [], "skipped_session": True}
        res = self.step(book, date, fill="same", execute=True)
        res["signals"] = self.signal_rows(book, date)
        for e in res["exits"]:
            log.info("[%s] SELL %-6s @ %.2f — %s", self.strategy.name, e.ticker, e.ref_price, e.reason)
        for s in res["entries"]:
            log.info("[%s] BUY  %-6s @ %.2f — %s", self.strategy.name, s.ticker, s.ref_price, s.reason)
        if res["skipped"]:
            log.info("[%s] %d signal(s) not taken -> rising stars: %s", self.strategy.name, len(res["skipped"]),
                     ", ".join(s.ticker for s in res["skipped"]))
        log.info("[%s] %s: %d exits, %d entries, %d open, equity $%.2f (%.0f%% invested)", self.strategy.name, date,
                 len(res["exits"]), len(res["entries"]), len(book.positions), book.total_value(), book.exposure() * 100)
        return res
