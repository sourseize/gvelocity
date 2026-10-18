"""
book — ONE position book per strategy.

* one position per issuer (GOOG == GOOGL)
* equal-budget sizing: equity x (1 - cash_reserve) / max_positions per position; the cash
  reserve is dry powder that is never spent
* SQLite table `books` (keyed by strategy name) is the single source of truth
* rising stars: entry signals the book could NOT take (book full / capital exhausted) are
  kept as a watchlist with how many sessions they have kept signalling; they get priority
  for the next free slot and expire when the signal stops
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from dataclasses import dataclass, field, fields, asdict
from typing import Dict, List, Optional

from .database import transaction

def _from_json(cls, d: dict):
    """Build a dataclass from a stored dict, ignoring keys the class no longer has."""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Position:
    issuer: str
    ticker: str
    shares: float
    entry_price: float
    entry_date: str
    stop_price: float
    trail: Optional[float] = None
    target_price: Optional[float] = None
    highest_close: float = 0.0
    last_price: float = 0.0
    meta: Dict[str, float] = field(default_factory=dict)

    def days_held(self, date: str, index: Dict[str, int]) -> int:
        """Sessions since entry (`index`: date -> position in the session list)."""
        if self.entry_date in index and date in index:
            return index[date] - index[self.entry_date]
        return 0

    @property
    def pnl_pct(self) -> float:
        return self.last_price / self.entry_price - 1 if self.entry_price else 0.0

    def update(self, price: float) -> None:
        """Mark to market and ratchet the trailing stop."""
        self.last_price = price
        if price > self.highest_close:
            self.highest_close = price
        if self.trail:
            self.stop_price = max(self.stop_price, self.highest_close * (1 - self.trail))


@dataclass
class Trade:
    date: str
    issuer: str
    ticker: str
    side: str                 # buy / sell
    price: float
    shares: float
    value: float
    commission: float
    reason: str
    pnl: float = 0.0          # realised on sells, net of both commissions
    hold_days: int = 0


class Book:
    def __init__(self, strategy: str, initial_capital: float = 100000.0):
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.equity: List[dict] = []
        self.last_exit: Dict[str, str] = {}
        self.rising: Dict[str, dict] = {}          # issuer -> rising-star record
        self.last_date: Optional[str] = None
        self.note: str = ""                        # provenance shown in the report (set by backtest --save-book)

    # ── rising stars ──
    def note_rising(self, issuer: str, ticker: str, date: str, ref_price: float, rank: float,
                    reason: str, why: str) -> None:
        """Record a signal that could not be taken today (or refresh it if already waiting)."""
        r = self.rising.get(issuer)
        if r is None:
            r = {"issuer": issuer, "ticker": ticker, "first_seen": date, "seen": 0}
            self.rising[issuer] = r
        r.update({"last_seen": date, "seen": r["seen"] + 1, "ref_price": ref_price, "rank": rank,
                  "reason": reason, "why": why, "first_price": r.get("first_price", ref_price)})

    def prune_rising(self, date: str, index: Dict[str, int], max_age: int) -> List[dict]:
        """Drop stars that were taken, or whose signal has not repeated for `max_age` sessions."""
        i = index.get(date)
        dropped = []
        for issuer, r in list(self.rising.items()):
            stale = i is not None and r["last_seen"] in index and i - index[r["last_seen"]] >= max_age
            if issuer in self.positions or stale:
                dropped.append(self.rising.pop(issuer))
        return dropped

    # ── valuation ──
    def market_value(self) -> float:
        return sum(p.shares * p.last_price for p in self.positions.values())

    def total_value(self) -> float:
        return self.cash + self.market_value()

    def unrealized(self) -> float:
        return sum((p.last_price - p.entry_price) * p.shares for p in self.positions.values())

    def realized(self) -> float:
        return sum(t.pnl for t in self.trades if t.side == "sell")

    def exposure(self) -> float:
        """Share of equity invested (0 = all cash)."""
        tv = self.total_value()
        return self.market_value() / tv if tv else 0.0

    # ── sizing ──
    def reserve(self, cash_reserve: float) -> float:
        """Dollars of dry powder that must stay in cash."""
        return self.total_value() * cash_reserve

    def position_budget(self, max_positions: int, cash_reserve: float) -> float:
        """Equal share of the investable equity: equity x (1 - reserve) / max_positions."""
        return self.total_value() * (1.0 - cash_reserve) / max(int(max_positions), 1)

    # ── mutations ──
    def open(self, issuer: str, ticker: str, price: float, date: str, dollars: float, commission: float,
             stop_price: float, trail: Optional[float], target_price: Optional[float], reason: str,
             meta: Optional[dict] = None, min_cash: float = 0.0) -> Optional[Trade]:
        """Open a position of `dollars` (the budget).  When the budget does not fit above `min_cash`
        (the reserve) the position is scaled down to what fits; below half a budget it is refused
        (-> "capital exhausted", the signal waits as a rising star)."""
        if issuer in self.positions or price <= 0:
            return None
        available = self.cash - min_cash - commission
        if available < dollars * 0.5:
            return None
        dollars = min(dollars, available)
        shares = dollars / price
        cost = shares * price + commission
        self.cash -= cost
        self.positions[issuer] = Position(issuer, ticker, shares, price, date, stop_price, trail, target_price,
                                          highest_close=price, last_price=price, meta=dict(meta or {}))
        t = Trade(date, issuer, ticker, "buy", price, shares, shares * price, commission, reason, 0.0, 0)
        self.trades.append(t)
        return t

    def close(self, issuer: str, price: float, date: str, commission: float, reason: str, hold_days: int) -> Optional[Trade]:
        pos = self.positions.pop(issuer, None)
        if pos is None:
            return None
        self.cash += pos.shares * price - commission
        pnl = (price - pos.entry_price) * pos.shares - 2 * commission
        t = Trade(date, issuer, pos.ticker, "sell", price, pos.shares, pos.shares * price, commission, reason,
                  pnl, hold_days)
        self.trades.append(t)
        self.last_exit[issuer] = date
        return t

    def record_equity(self, date: str) -> None:
        self.equity = [e for e in self.equity if e["date"] != date]
        self.equity.append({"date": date, "value": self.total_value(), "cash": self.cash, "positions": len(self.positions)})

    # ── persistence ──
    def save(self, conn: sqlite3.Connection) -> None:
        """Write the book (its own transaction, or part of the caller's — see database.transaction)."""
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        with transaction(conn):
            conn.execute(
                """INSERT INTO books (strategy, initial_capital, cash, last_date, positions_json, trades_json,
                                      equity_json, last_exit_json, rising_json, note, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(strategy) DO UPDATE SET initial_capital=excluded.initial_capital, cash=excluded.cash,
                     last_date=excluded.last_date, positions_json=excluded.positions_json, trades_json=excluded.trades_json,
                     equity_json=excluded.equity_json, last_exit_json=excluded.last_exit_json,
                     rising_json=excluded.rising_json, note=excluded.note, updated_at=excluded.updated_at""",
                (self.strategy, self.initial_capital, self.cash, self.last_date,
                 json.dumps({k: asdict(v) for k, v in self.positions.items()}),
                 json.dumps([asdict(t) for t in self.trades]), json.dumps(self.equity),
                 json.dumps(self.last_exit), json.dumps(self.rising), self.note, now))

    @classmethod
    def load(cls, conn: sqlite3.Connection, strategy: str, initial_capital: float = 100000.0) -> "Book":
        cur = conn.execute("SELECT * FROM books WHERE strategy=?", (strategy,))
        row = cur.fetchone()
        b = cls(strategy, initial_capital)
        if not row:
            return b
        row = dict(zip([d[0] for d in cur.description], row))
        b.initial_capital = row["initial_capital"]; b.cash = row["cash"]; b.last_date = row["last_date"]
        b.positions = {k: _from_json(Position, v) for k, v in json.loads(row["positions_json"]).items()}
        b.trades = [_from_json(Trade, t) for t in json.loads(row["trades_json"])]
        b.equity = json.loads(row["equity_json"]); b.last_exit = json.loads(row["last_exit_json"])
        b.rising = json.loads(row.get("rising_json") or "{}")
        b.note = row.get("note") or ""
        return b

    @staticmethod
    def delete(conn: sqlite3.Connection, strategy: str) -> None:
        with transaction(conn):
            conn.execute("DELETE FROM books WHERE strategy=?", (strategy,))

    @staticmethod
    def strategies(conn: sqlite3.Connection) -> List[str]:
        """Names of the strategies that have a book row."""
        return [r[0] for r in conn.execute("SELECT strategy FROM books ORDER BY strategy")]

    def to_dict(self) -> dict:
        return {"strategy": self.strategy, "initial_capital": self.initial_capital, "cash": self.cash,
                "last_date": self.last_date, "total_value": self.total_value(), "realized": self.realized(),
                "unrealized": self.unrealized(), "note": self.note,
                "positions": {k: asdict(v) for k, v in self.positions.items()},
                "rising_stars": self.rising_list(),
                "trades": [asdict(t) for t in self.trades], "equity": self.equity}

    def rising_list(self) -> List[dict]:
        """Rising stars, most persistent first (ties: strategy rank)."""
        return sorted(self.rising.values(), key=lambda r: (-r["seen"], -r.get("rank", 0)))


# ── session / signal history (audit trail, queryable without recomputation) ──
def log_session(conn: sqlite3.Connection, strategy: str, date: str, result: dict, book: "Book",
                kind: str = "live", params: Optional[dict] = None, engine_cfg: Optional[dict] = None) -> None:
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    pack = lambda items: json.dumps([asdict(x) if hasattr(x, "__dataclass_fields__") else x for x in items])
    with transaction(conn):
        conn.execute(
            """INSERT OR REPLACE INTO sessions (strategy, date, n_entries, n_exits, n_skipped, n_open, n_rising, cash, equity,
                                                entries_json, exits_json, skipped_json, run_at, kind, params_json, engine_json,
                                                rising_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (strategy, date, len(result["entries"]), len(result["exits"]), len(result["skipped"]), len(book.positions),
             len(book.rising), book.cash, book.total_value(), pack(result["entries"]), pack(result["exits"]),
             pack(result["skipped"]), now, kind, json.dumps(params or {}), json.dumps(engine_cfg or {}),
             json.dumps(book.rising_list())))


def log_signals(conn: sqlite3.Connection, strategy: str, date: str, rows: List[tuple]) -> None:
    """rows: (ticker, state, note, checks_dict) — only non-NONE states are worth storing."""
    with transaction(conn):
        conn.execute("DELETE FROM signals WHERE strategy=? AND date=?", (strategy, date))
        conn.executemany("INSERT INTO signals VALUES (?,?,?,?,?,?)",
                         [(strategy, date, t, st, note, json.dumps(ch)) for t, st, note, ch in rows if st != "NONE"])
