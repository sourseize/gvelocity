"""
book — ONE position book across all tracked ETFs, one per strategy.

* one position per issuer (GOOG == GOOGL), whatever number of ETFs contain it
* hard $ cap per issuer; risk-based sizing below the cap
* SQLite table `books` (keyed by strategy name) is the single source of truth;
  json/html in the report dir are derived artefacts and never read back
* rising stars: entry signals the book could NOT take (book full / capital exhausted) are
  kept as a watchlist with how many sessions they have kept signalling; they get priority
  for the next free slot and expire when the signal stops
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import sqlite3
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

BOOK_SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    strategy        TEXT PRIMARY KEY,
    initial_capital REAL NOT NULL,
    cash            REAL NOT NULL,
    last_date       TEXT,
    positions_json  TEXT NOT NULL,
    trades_json     TEXT NOT NULL,
    equity_json     TEXT NOT NULL,
    last_exit_json  TEXT NOT NULL,
    rising_json     TEXT NOT NULL DEFAULT '{}',
    note            TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL
);
"""


SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    strategy     TEXT NOT NULL,
    date         TEXT NOT NULL,
    n_entries    INTEGER NOT NULL,
    n_exits      INTEGER NOT NULL,
    n_skipped    INTEGER NOT NULL,
    n_open       INTEGER NOT NULL,
    n_rising     INTEGER NOT NULL,
    cash         REAL NOT NULL,
    equity       REAL NOT NULL,
    entries_json TEXT NOT NULL,
    exits_json   TEXT NOT NULL,
    skipped_json TEXT NOT NULL,
    run_at       TEXT NOT NULL,
    PRIMARY KEY (strategy, date)
);
CREATE TABLE IF NOT EXISTS signals (
    strategy    TEXT NOT NULL,
    date        TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    state       TEXT NOT NULL,
    note        TEXT,
    checks_json TEXT,
    PRIMARY KEY (strategy, date, ticker)
);
CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(strategy, ticker, date);
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(BOOK_SCHEMA)
    conn.executescript(SESSION_SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(books)")}
    if "rising_json" not in cols:
        conn.execute("ALTER TABLE books ADD COLUMN rising_json TEXT NOT NULL DEFAULT '{}'")
        conn.commit()
    if "note" not in cols:          # provenance, e.g. "populated by backtest: <command>"
        conn.execute("ALTER TABLE books ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        conn.commit()


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
    etfs: List[str] = field(default_factory=list)
    meta: Dict[str, float] = field(default_factory=dict)

    def days_held(self, date: str, dates: List[str]) -> int:
        if self.entry_date in dates and date in dates:
            return dates.index(date) - dates.index(self.entry_date)
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
    etfs: List[str] = field(default_factory=list)


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
                    reason: str, why: str, etfs: List[str]) -> None:
        """Record a signal that could not be taken today (or refresh it if already waiting)."""
        r = self.rising.get(issuer)
        if r is None:
            r = {"issuer": issuer, "ticker": ticker, "first_seen": date, "seen": 0, "etfs": list(etfs)}
            self.rising[issuer] = r
        r.update({"last_seen": date, "seen": r["seen"] + 1, "ref_price": ref_price, "rank": rank,
                  "reason": reason, "why": why, "first_price": r.get("first_price", ref_price),
                  "etfs": sorted(set(r.get("etfs", [])) | set(etfs))})

    def prune_rising(self, date: str, dates: List[str], max_age: int) -> List[dict]:
        """Drop stars that were taken, or whose signal has not repeated for `max_age` sessions."""
        i = dates.index(date) if date in dates else None
        dropped = []
        for issuer, r in list(self.rising.items()):
            stale = i is not None and r["last_seen"] in dates and i - dates.index(r["last_seen"]) >= max_age
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

    # ── sizing ──
    def size_dollars(self, stop_distance: float, max_amount: float, risk_per_trade: float) -> float:
        """Dollars = min(cap, equity * risk / stop distance)."""
        return min(max_amount, self.total_value() * risk_per_trade / max(stop_distance, 1e-4))

    # ── mutations ──
    def open(self, issuer: str, ticker: str, price: float, date: str, dollars: float, commission: float,
             stop_price: float, trail: Optional[float], target_price: Optional[float], reason: str,
             etfs: List[str], meta: Optional[dict] = None) -> Optional[Trade]:
        shares = dollars / price
        cost = shares * price + commission
        if cost > self.cash or issuer in self.positions:
            return None
        self.cash -= cost
        self.positions[issuer] = Position(issuer, ticker, shares, price, date, stop_price, trail, target_price,
                                          highest_close=price, last_price=price, etfs=list(etfs), meta=dict(meta or {}))
        t = Trade(date, issuer, ticker, "buy", price, shares, shares * price, commission, reason, 0.0, 0, list(etfs))
        self.trades.append(t)
        return t

    def close(self, issuer: str, price: float, date: str, commission: float, reason: str, hold_days: int) -> Optional[Trade]:
        pos = self.positions.pop(issuer, None)
        if pos is None:
            return None
        self.cash += pos.shares * price - commission
        pnl = (price - pos.entry_price) * pos.shares - 2 * commission
        t = Trade(date, issuer, pos.ticker, "sell", price, pos.shares, pos.shares * price, commission, reason,
                  pnl, hold_days, list(pos.etfs))
        self.trades.append(t)
        self.last_exit[issuer] = date
        return t

    def record_equity(self, date: str) -> None:
        self.equity = [e for e in self.equity if e["date"] != date]
        self.equity.append({"date": date, "value": self.total_value(), "cash": self.cash, "positions": len(self.positions)})

    # ── persistence ──
    def save(self, conn: sqlite3.Connection) -> None:
        _ensure_schema(conn)
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
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
        conn.commit()

    @classmethod
    def load(cls, conn: sqlite3.Connection, strategy: str, initial_capital: float = 100000.0) -> "Book":
        _ensure_schema(conn)
        cur = conn.execute("SELECT * FROM books WHERE strategy=?", (strategy,))
        row = cur.fetchone()
        b = cls(strategy, initial_capital)
        if not row:
            return b
        row = dict(zip([d[0] for d in cur.description], row))
        b.initial_capital = row["initial_capital"]; b.cash = row["cash"]; b.last_date = row["last_date"]
        b.positions = {k: Position(**v) for k, v in json.loads(row["positions_json"]).items()}
        b.trades = [Trade(**t) for t in json.loads(row["trades_json"])]
        b.equity = json.loads(row["equity_json"]); b.last_exit = json.loads(row["last_exit_json"])
        b.rising = json.loads(row.get("rising_json") or "{}")
        b.note = row.get("note") or ""
        return b

    @staticmethod
    def delete(conn: sqlite3.Connection, strategy: str) -> None:
        _ensure_schema(conn)
        conn.execute("DELETE FROM books WHERE strategy=?", (strategy,))
        conn.commit()

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

    # ── report ──
    def write_report(self, out_dir: Path, label: str = "") -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{self.strategy}.json").write_text(json.dumps(self.to_dict(), indent=2))
        (out_dir / f"{self.strategy}.html").write_text(self.to_html(label), encoding="utf-8")

    def to_html(self, label: str = "") -> str:
        from .reporter import ticker_link, fmt_f, CSS
        rows = ""
        for p in sorted(self.positions.values(), key=lambda p: p.entry_date):
            cls = "pos" if p.pnl_pct >= 0 else "neg"
            rows += (f"<tr><td><b>{ticker_link(p.ticker)}</b></td><td>{p.entry_date}</td><td>{fmt_f(p.entry_price)}</td>"
                     f"<td>{fmt_f(p.last_price)}</td><td class='{cls}'>{p.pnl_pct:+.1%}</td><td>{fmt_f(p.stop_price)}</td>"
                     f"<td>{fmt_f(p.target_price) if p.target_price else '—'}</td><td>{', '.join(p.etfs)}</td></tr>")
        if not rows:
            rows = "<tr><td colspan='8' class='none'>No open positions</td></tr>"
        trows = ""
        for t in reversed(self.trades[-60:]):
            cls = "pos" if t.side == "buy" else "neg"
            pnl = f"<span class='{'pos' if t.pnl >= 0 else 'neg'}'>{t.pnl:+.0f}</span>" if t.side == "sell" else "—"
            trows += (f"<tr><td>{t.date}</td><td><b>{ticker_link(t.ticker)}</b></td><td class='{cls}'>{t.side.upper()}</td>"
                      f"<td>{fmt_f(t.price)}</td><td>{fmt_f(t.value, 0)}</td><td>{pnl}</td><td style='max-width:420px'>{t.reason}</td></tr>")
        if not trows:
            trows = "<tr><td colspan='7' class='none'>No trades yet</td></tr>"
        rrows = ""
        for r in self.rising_list():
            chg = (r["ref_price"] / r["first_price"] - 1) if r.get("first_price") else 0.0
            rrows += (f"<tr><td><b>{ticker_link(r['ticker'])}</b></td><td>{r['first_seen']}</td><td>{r['last_seen']}</td>"
                      f"<td>{r['seen']}</td><td>{fmt_f(r['ref_price'])}</td><td class='{'pos' if chg >= 0 else 'neg'}'>{chg:+.1%}</td>"
                      f"<td>{r['why']}</td><td style='max-width:380px'>{r['reason']}</td><td>{', '.join(r['etfs'])}</td></tr>")
        if not rrows:
            rrows = "<tr><td colspan='9' class='none'>No signals waiting</td></tr>"
        closed = [t for t in self.trades if t.side == "sell"]
        wins = [t for t in closed if t.pnl > 0]
        ret = self.total_value() / self.initial_capital - 1
        return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Velocity — {label or self.strategy} book</title><style>{CSS}</style></head><body><div class="wrap">
<h1>{label or self.strategy} — position book</h1>
<p class="sub">Strategy <code>{self.strategy}</code> · last session {self.last_date or '—'} · <a href="../index.html">← All ETFs</a></p>
{f'<p class="sub note">{html.escape(self.note)}</p>' if self.note else ''}
<div class="stats">
  <div class="stat"><div class="sv">${fmt_f(self.total_value(), 0)}</div><div class="sl">Equity</div></div>
  <div class="stat"><div class="sv">{ret:+.2%}</div><div class="sl">Return</div></div>
  <div class="stat"><div class="sv">${fmt_f(self.cash, 0)}</div><div class="sl">Cash</div></div>
  <div class="stat"><div class="sv">{len(self.positions)}</div><div class="sl">Open</div></div>
  <div class="stat"><div class="sv">{len(closed)}</div><div class="sl">Closed trades</div></div>
  <div class="stat"><div class="sv">{(len(wins) / len(closed)) if closed else 0:.0%}</div><div class="sl">Win rate</div></div>
  <div class="stat"><div class="sv">${fmt_f(self.realized(), 0)}</div><div class="sl">Realized</div></div>
  <div class="stat"><div class="sv">${fmt_f(self.unrealized(), 0)}</div><div class="sl">Unrealized</div></div>
</div>
<h2>Open positions</h2><div class="tw"><table>
<tr><th>Ticker</th><th>Entry</th><th>Entry px</th><th>Last</th><th>P&L</th><th>Stop</th><th>Target</th><th>ETFs</th></tr>{rows}</table></div>
<h2>Rising stars — signals waiting for a slot ({len(self.rising)})</h2>
<p class="sub" style="margin-top:-8px">Entry signals the book could not take (full, or capital exhausted). They keep their place while the
signal repeats, get priority for the next free slot, and drop off when the setup stops firing.</p>
<div class="tw"><table>
<tr><th>Ticker</th><th>First</th><th>Last</th><th>Sessions</th><th>Price</th><th>Since first</th><th>Why waiting</th><th>Signal</th><th>ETFs</th></tr>{rrows}</table></div>
<h2>Recent trades</h2><div class="tw"><table>
<tr><th>Date</th><th>Ticker</th><th>Side</th><th>Price</th><th>Value</th><th>P&L</th><th>Reason</th></tr>{trows}</table></div>
</div></body></html>"""


# ── session / signal history (audit trail, queryable without recomputation) ──
def log_session(conn: sqlite3.Connection, strategy: str, date: str, result: dict, book: "Book") -> None:
    _ensure_schema(conn)
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    pack = lambda items: json.dumps([asdict(x) if hasattr(x, "__dataclass_fields__") else x for x in items])
    conn.execute(
        """INSERT OR REPLACE INTO sessions (strategy, date, n_entries, n_exits, n_skipped, n_open, n_rising, cash, equity,
                                            entries_json, exits_json, skipped_json, run_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (strategy, date, len(result["entries"]), len(result["exits"]), len(result["skipped"]), len(book.positions),
         len(book.rising), book.cash, book.total_value(), pack(result["entries"]), pack(result["exits"]),
         pack(result["skipped"]), now))
    conn.commit()


def log_signals(conn: sqlite3.Connection, strategy: str, date: str, rows: List[tuple]) -> None:
    """rows: (ticker, state, note, checks_dict) — only non-NONE states are worth storing."""
    _ensure_schema(conn)
    conn.execute("DELETE FROM signals WHERE strategy=? AND date=?", (strategy, date))
    conn.executemany("INSERT INTO signals VALUES (?,?,?,?,?,?)",
                     [(strategy, date, t, st, note, json.dumps(ch)) for t, st, note, ch in rows if st != "NONE"])
    conn.commit()
