"""
books service — load, reset and register strategy books.
"""
from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
from dataclasses import asdict
from typing import Callable, Optional

from ..book import Book, log_session
from ..database import transaction, set_meta
from ..engine import Engine
from ..exceptions import VelocityError

log = logging.getLogger(__name__)


def load(conn: sqlite3.Connection, strategy: str, capital: float) -> Book:
    try:
        return Book.load(conn, strategy, capital)
    except (sqlite3.Error, ValueError, TypeError) as e:
        raise VelocityError(f"book database is unusable ({e}) — restore it or move it aside") from e


def reset(conn: sqlite3.Connection, strategy: str, capital: float) -> Book:
    """Delete the book and its session / signal audit trail atomically.  Returns the fresh book."""
    old = load(conn, strategy, capital)
    with transaction(conn):
        Book.delete(conn, strategy)
        conn.execute("DELETE FROM sessions WHERE strategy=?", (strategy,))
        conn.execute("DELETE FROM signals WHERE strategy=?", (strategy,))
        set_meta(conn, "last_session_date", None)
    if old.last_date or old.trades:
        log.info("[%s] book reset: dropped %d trades, %d open positions (last session %s)",
                 strategy, len(old.trades), len(old.positions), old.last_date)
    else:
        log.info("[%s] book reset (it was already empty)", strategy)
    return load(conn, strategy, capital)


def register_backtest(conn: sqlite3.Connection, engine: Engine, start: str, end: str, fill: str, capital: float,
                      command: str = "") -> tuple[Book, Optional[Book]]:
    """Replace the strategy's live book with a replay of [start, end] — old book out, replay plus
    its `sessions` audit trail in, one transaction.  Returns (new book, replaced book or None)."""
    old = load(conn, engine.strategy.name, capital)
    replaced = old if (old.last_date or old.trades) else None
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d")
    params, cfg = engine.strategy.params_dict(), asdict(engine.cfg)
    with transaction(conn):
        conn.execute("DELETE FROM sessions WHERE strategy=?", (engine.strategy.name,))
        conn.execute("DELETE FROM signals WHERE strategy=?", (engine.strategy.name,))
        book = engine.backtest(start, end, fill=fill, capital=capital,
                               on_session=lambda d, res, b: log_session(conn, b.strategy, d, res, b, kind="backtest",
                                                                        params=params, engine_cfg=cfg))
        if not book.equity:
            raise VelocityError("nothing to register: the backtest produced no sessions")
        book.note = f"Book registered from backtest on {stamp} ({start} → {end}, fill={fill})" + (f" · {command}" if command else "")
        book.save(conn)
    return book, replaced


def summaries(conn: sqlite3.Connection) -> list[dict]:
    """One line per stored book, for `velocity status`."""
    out = []
    for name in Book.strategies(conn):
        b = Book.load(conn, name)
        last = conn.execute("SELECT kind, run_at FROM sessions WHERE strategy=? ORDER BY date DESC LIMIT 1", (name,)).fetchone()
        out.append({"strategy": name, "last_date": b.last_date, "equity": b.total_value(), "cash": b.cash,
                    "open": len(b.positions), "trades": len(b.trades), "rising": len(b.rising),
                    "sessions": conn.execute("SELECT COUNT(*) FROM sessions WHERE strategy=?", (name,)).fetchone()[0],
                    "last_kind": last["kind"] if last else None, "last_run_at": last["run_at"] if last else None,
                    "note": b.note})
    return out
