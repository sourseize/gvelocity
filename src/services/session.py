"""
session service — run a strategy session and persist it atomically.

One transaction per (strategy, date): the `sessions` row, the `signals` rows and the book are
written together, so a crash at any point leaves either "nothing for that date" or "everything"
— and `Engine.run_session` is idempotent per date, so the next run simply redoes the missing one.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict
from typing import List, Optional

from ..book import Book, log_session, log_signals
from ..database import transaction, latest_date
from ..engine import Engine

log = logging.getLogger(__name__)


def persist_session(conn: sqlite3.Connection, engine: Engine, book: Book, result: dict, kind: str = "live") -> None:
    """Store one session's result and the updated book in a single transaction."""
    strategy = engine.strategy
    with transaction(conn):
        log_session(conn, strategy.name, result["date"], result, book, kind=kind,
                    params=strategy.params_dict(), engine_cfg=asdict(engine.cfg))
        log_signals(conn, strategy.name, result["date"], result.get("signals", []))
        book.save(conn)


def run_and_persist(conn: sqlite3.Connection, engine: Engine, book: Book, date: str, kind: str = "live") -> dict:
    """`Engine.run_session` + `persist_session`; a skipped session (already done / no bars) writes nothing."""
    result = engine.run_session(book, date)
    if not result.get("skipped_session"):
        persist_session(conn, engine, book, result, kind=kind)
    return result


def due_dates(conn: sqlite3.Connection, engine: Engine, book: Book, upto: Optional[str] = None) -> List[str]:
    """Session dates the book has not processed yet: every bar date after `book.last_date` up to
    `upto` (default: the latest stored holdings date), in order.  More than one date means the
    book missed sessions (downtime) and catches up.  A book without any session yet starts on
    `upto` only — seeding a book from history is what `backtest --save-book` is for."""
    upto = upto or latest_date(conn)
    if upto is None:
        return []
    if book.last_date is None:
        return [upto] if upto in engine.dates else []
    return [d for d in engine.dates if book.last_date < d <= upto]


def run_due_sessions(conn: sqlite3.Connection, engine: Engine, book: Book, upto: Optional[str] = None,
                     catchup: bool = True) -> List[dict]:
    """Run every due session in order (only the latest one when `catchup` is False).  Each session
    is persisted on its own, so an interruption keeps the ones already done."""
    dates = due_dates(conn, engine, book, upto)
    if not dates:
        return []
    if not catchup:
        dates = dates[-1:]
    if len(dates) > 1:
        log.info("[%s] catching up %d sessions: %s → %s", engine.strategy.name, len(dates), dates[0], dates[-1])
    results = []
    for i, d in enumerate(dates):
        kind = "catchup" if i < len(dates) - 1 else "live"
        results.append(run_and_persist(conn, engine, book, d, kind=kind))
    return results
