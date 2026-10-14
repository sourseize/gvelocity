"""
engine — runs a Strategy over trading sessions against a Book.

Generic, strategy-independent rules live here so every strategy gets them for free:
  * mark to market, trailing-stop ratchet
  * exits: dropped from all tracked ETFs · close below stop · target reached · time stop
  * re-entry cooldown after an exit
  * position limit + the `max_positions` policy (see MaxPositionsPolicy)
  * rising stars: signals not taken (book full / no cash) are parked in the book's watchlist,
    ranked first when a slot frees up, and expire once they stop signalling
  * sizing: min($ cap, equity x risk_per_trade / stop distance)
  * fills: "same" books the signal-day close; "next" books the next session's close

The strategy only says *what* to buy/sell and *why*; the engine decides *whether it fits*.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .book import Book, log_session, log_signals
from .indicators import Indicators
from .market_data import Universe, load_market_bars
from .strategies.base import Strategy, EntrySignal, ExitSignal

log = logging.getLogger(__name__)


@dataclass
class MaxPositionsPolicy:
    """What happens when a new entry signal arrives and the book is full.

    mode = "skip"     : nothing is sold; the best-ranked candidates fill the free slots and the rest
                        are dropped (they may re-qualify tomorrow if the signal persists).
    mode = "replace"  : a candidate may replace the weakest open position — the one with the lowest
                        return since entry — but only if that position is losing money, has been held
                        at least `min_hold` sessions, and the candidate is not the same issuer.
                        One replacement per candidate, best-ranked candidates first.
    Winners are never sold to make room: the strategy's own exit rules decide when they go.
    """
    mode: str = "skip"
    min_hold: int = 5


@dataclass
class EngineConfig:
    max_positions: int = 25
    max_amount: float = 1000.0
    risk_per_trade: float = 0.0025
    commission: float = 1.0
    cooldown_days: int = 10
    policy: MaxPositionsPolicy = field(default_factory=MaxPositionsPolicy)
    rising_priority: bool = False     # waiting signals go to the front of the queue for free slots (else strategy rank)
    rising_max_age: int = 10          # sessions without a repeat signal before a rising star is dropped


class Engine:
    def __init__(self, strategy: Strategy, conns: Dict[str, sqlite3.Connection], cfg: EngineConfig = EngineConfig(),
                 bars: Optional[pd.DataFrame] = None, book_conn: Optional[sqlite3.Connection] = None):
        self.strategy = strategy
        self.conns = conns
        self.cfg = cfg
        self.bars = bars if bars is not None else load_market_bars(conns, book_conn)
        self.ind = Indicators(self.bars)
        if not self.ind.has_volume:
            log.warning("%s: bars carry no volume — volume rules are inactive (price-only mode)", strategy.name)
        self.dates: List[str] = self.ind.dates
        self.universe = Universe(conns, self.dates)
        strategy.prepare(self.ind, self.universe)

    # ── helpers ──
    def price(self, date: str, ticker: str) -> float:
        return float(self.ind.at(self.ind.close, date, ticker))

    def fill_price(self, date: str, ticker: str, fill: str) -> float:
        if fill == "next":
            i = self.dates.index(date)
            return self.price(self.dates[i + 1], ticker) if i + 1 < len(self.dates) else float("nan")
        return self.price(date, ticker)

    # ── one session ──
    def step(self, book: Book, date: str, fill: str = "next", execute: bool = True) -> dict:
        cfg, strat = self.cfg, self.strategy
        i = self.dates.index(date)
        univ = self.universe.on(date)

        # 1. mark to market (ratchets trailing stops)
        for pos in book.positions.values():
            px = self.price(date, pos.ticker)
            if not np.isnan(px):
                pos.update(px)

        strat.manage(date, book)

        # 2. generic exits, then strategy exits
        exits: Dict[str, ExitSignal] = {}
        for issuer, pos in book.positions.items():
            px = pos.last_price
            reason = None
            if issuer not in univ:
                reason = "dropped from all tracked ETFs"
            elif px < pos.stop_price:
                reason = (f"trailing stop {pos.trail:.0%} (stop {pos.stop_price:.2f})" if pos.trail
                          else f"stop {pos.stop_price:.2f} hit")
            elif pos.target_price and px >= pos.target_price:
                reason = f"target {pos.target_price:.2f} reached"
            elif strat.time_stop_days and pos.days_held(date, self.dates) >= strat.time_stop_days and px < pos.entry_price:
                reason = f"time stop: underwater after {strat.time_stop_days}d"
            if reason:
                exits[issuer] = ExitSignal(issuer, pos.ticker, px, reason)
        for sig in strat.exits(date, book):
            exits.setdefault(sig.issuer, sig)

        # 3. entries (strategy ranks them), cooldown, position limit policy
        cands = [s for s in strat.entries(date, univ, book)
                 if s.issuer not in exits
                 and not (s.issuer in book.last_exit and book.last_exit[s.issuer] in self.dates
                          and i - self.dates.index(book.last_exit[s.issuer]) < cfg.cooldown_days)]
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
                           if iss not in exits and p.pnl_pct < 0 and p.days_held(date, self.dates) >= cfg.policy.min_hold]
                if not victims:
                    break
                worst = min(victims, key=lambda p: p.pnl_pct)
                ex = ExitSignal(worst.issuer, worst.ticker, worst.last_price,
                                f"replaced by {cand.ticker} (weakest: {worst.pnl_pct:+.1%} after {worst.days_held(date, self.dates)}d)")
                exits[worst.issuer] = ex; replaced.append(ex); entries.append(cand)
        skipped = [s for s in cands if s not in entries]

        if execute:
            unfilled = self._execute(book, date, list(exits.values()), entries, fill)
            entries = [s for s in entries if s not in unfilled]
            for s in skipped:
                book.note_rising(s.issuer, s.ticker, date, s.ref_price, s.rank, s.reason, "book full", s.etfs)
            for s in unfilled:
                book.note_rising(s.issuer, s.ticker, date, s.ref_price, s.rank, s.reason, "capital exhausted", s.etfs)
            skipped = skipped + unfilled
            book.prune_rising(date, self.dates, cfg.rising_max_age)
            book.last_date = date
            book.record_equity(date)
        return {"date": date, "exits": list(exits.values()), "entries": entries, "replaced": replaced,
                "skipped": skipped, "rising": book.rising_list()}

    def _execute(self, book: Book, date: str, exits: List[ExitSignal], entries: List[EntrySignal], fill: str) -> List[EntrySignal]:
        """Book exits then entries; returns the entries that could not be filled (no cash / no price)."""
        cfg = self.cfg
        unfilled: List[EntrySignal] = []
        i = self.dates.index(date)
        fill_date = date if fill == "same" or i + 1 >= len(self.dates) else self.dates[i + 1]
        for e in exits:
            pos = book.positions[e.issuer]
            px = self.fill_price(date, pos.ticker, fill)
            if np.isnan(px):
                px = e.ref_price
            book.close(e.issuer, px, fill_date, cfg.commission, e.reason, pos.days_held(date, self.dates))
        for s in entries:
            px = self.fill_price(date, s.ticker, fill)
            if np.isnan(px):
                continue
            dollars = book.size_dollars(s.stop_distance, cfg.max_amount, cfg.risk_per_trade)
            # keep the stop/target levels as signalled; re-anchor a trailing stop to the actual fill
            stop = s.stop_price if s.trail is None else max(s.stop_price, px * (1 - s.trail))
            if book.open(s.issuer, s.ticker, px, fill_date, dollars, cfg.commission, stop, s.trail, s.target_price,
                         s.reason, s.etfs, s.meta) is None:
                unfilled.append(s)
        return unfilled

    # ── backtest ──
    def backtest(self, start: str, end: str, fill: str = "next", capital: float = 100000.0) -> Book:
        book = Book(self.strategy.name, capital)
        for d in self.dates:
            if start <= d <= end:
                self.step(book, d, fill=fill, execute=True)
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
    def run_session(self, book: Book, date: str, log_conn: Optional[sqlite3.Connection] = None) -> dict:
        """Evaluate one live session, booking today's close as the reference fill.  Idempotent per date.
        With `log_conn`, the session result and the per-ticker signal states are written to the
        `sessions` / `signals` tables (the audit trail)."""
        if book.last_date == date:
            log.info("[%s] %s already processed — skipping", self.strategy.name, date)
            return {"date": date, "exits": [], "entries": [], "replaced": [], "skipped": [], "skipped_session": True}
        if date not in self.dates:
            log.warning("[%s] %s not in bar data", self.strategy.name, date)
            return {"date": date, "exits": [], "entries": [], "replaced": [], "skipped": [], "skipped_session": True}
        res = self.step(book, date, fill="same", execute=True)
        if log_conn is not None:
            log_session(log_conn, self.strategy.name, date, res, book)
            log_signals(log_conn, self.strategy.name, date, self.signal_rows(book, date))
        for e in res["exits"]:
            log.info("[%s] SELL %-6s @ %.2f — %s", self.strategy.name, e.ticker, e.ref_price, e.reason)
        for s in res["entries"]:
            log.info("[%s] BUY  %-6s @ %.2f — %s", self.strategy.name, s.ticker, s.ref_price, s.reason)
        if res["skipped"]:
            log.info("[%s] %d signal(s) not taken -> rising stars: %s", self.strategy.name, len(res["skipped"]),
                     ", ".join(s.ticker for s in res["skipped"]))
        if res["rising"]:
            log.info("[%s] rising stars waiting: %s", self.strategy.name,
                     ", ".join(f"{r['ticker']}x{r['seen']}" for r in res["rising"][:12]))
        log.info("[%s] %s: %d exits, %d entries, %d open, equity $%.2f", self.strategy.name, date,
                 len(res["exits"]), len(res["entries"]), len(book.positions), book.total_value())
        return res
