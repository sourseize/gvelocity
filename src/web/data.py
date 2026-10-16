"""
Read side of the web service.

Every request opens its own read-only connection (`database.connect(readonly=True)`): request
handlers cannot write, and they never touch the poller's writer connection.  The only shared
object is `scheduler.state` (an immutable `MarketState`), used for the prepared strategy
(`explain()` is pure reads on precomputed frames) and the bar history on the ticker page.

Historical days are rendered from the audit tables (`signals`, `sessions.rising_json`) so they
show the state as it was; the latest day — if its session has not run yet — is explained live.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Optional

from ..book import Book
from ..config import DB_PATH
from ..database import connect, all_dates, latest_date, get_meta
from ..day_data import build_day_data
from ..reporter import day_log_entry
from ..scheduler import MarketState


def ro() -> sqlite3.Connection:
    return connect(DB_PATH, readonly=True)


def months(conn) -> list[str]:
    return sorted({d[:7] for d in all_dates(conn)})


def dates_in_month(conn, ym: str) -> list[str]:
    return [d for d in all_dates(conn) if d.startswith(ym)]


def load_book(conn, strategy: str) -> Book:
    return Book.load(conn, strategy)


def book_version(conn, strategy: str) -> str:
    row = conn.execute("SELECT updated_at FROM books WHERE strategy=?", (strategy,)).fetchone()
    return row[0] if row else ""


def session_row(conn, strategy: str, date: str) -> Optional[dict]:
    r = conn.execute("SELECT * FROM sessions WHERE strategy=? AND date=?", (strategy, date)).fetchone()
    return dict(r) if r else None


def sessions(conn, strategy: str, limit: int = 500) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT date, kind, n_entries, n_exits, n_skipped, n_open, n_rising, cash, equity, run_at FROM sessions "
        "WHERE strategy=? ORDER BY date DESC LIMIT ?", (strategy, limit))]


def stored_signals(conn, strategy: str, date: str) -> Optional[dict]:
    """{ticker: {state, note, checks}} from the session's audit rows, or None when that day has no session."""
    if session_row(conn, strategy, date) is None:
        return None
    out = {}
    for r in conn.execute("SELECT ticker, state, note, checks_json FROM signals WHERE strategy=? AND date=?", (strategy, date)):
        out[r["ticker"]] = {"state": r["state"], "note": r["note"] or "", "checks": json.loads(r["checks_json"] or "{}")}
    return out


def day_data(conn, date: str, state: Optional[MarketState], book: Optional[Book]) -> dict:
    """The full payload of one day (see day_data.build_day_data)."""
    strategy = state.strategy if state else None
    signals = rising = None
    if strategy is not None:
        signals = stored_signals(conn, strategy.name, date)
        srow = session_row(conn, strategy.name, date)
        if srow is not None:
            rising = json.loads(srow.get("rising_json") or "[]")
    return build_day_data(conn, date, strategy=strategy, book=book, trades=book.trades if book else None,
                          signals=signals, rising=rising)


class DayEntryCache:
    """`day_log_entry()` per (date, strategy, history version, book version) — a month page needs ~20."""
    def __init__(self, limit: int = 2000):
        self._d: dict = {}
        self._lock = threading.Lock()
        self.limit = limit

    def get(self, conn, date: str, state: Optional[MarketState], book: Optional[Book]) -> dict:
        key = (date, state.strategy.name if state else None, latest_date(conn), book_version(conn, state.strategy.name) if state else "")
        with self._lock:
            hit = self._d.get(key)
        if hit is not None:
            return hit
        entry = day_log_entry(day_data(conn, date, state, book))
        with self._lock:
            if len(self._d) >= self.limit:
                self._d.clear()
            self._d[key] = entry
        return entry

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


def status_payload(conn, state: Optional[MarketState]) -> dict:
    latest = latest_date(conn)
    fetched_at = conn.execute("SELECT fetched_at FROM snapshots WHERE fetch_date=?", (latest,)).fetchone() if latest else None
    keys = ("last_poll_at", "last_poll_outcome", "last_poll_detail", "next_poll_at", "last_cycle_at", "last_cycle_status",
            "last_cycle_level", "last_session_date", "serve_strategy", "serve_started_at", "serve_stopped_at")
    out = {k: get_meta(conn, k) for k in keys}
    out.update({
        "holdings_date": latest,
        "holdings_updated_at": fetched_at[0] if fetched_at else None,
        "n_snapshots": conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
        "first_date": conn.execute("SELECT MIN(fetch_date) FROM snapshots").fetchone()[0],
        "state_built_at": state.built_at if state else None,
        "serve_settings": json.loads(get_meta(conn, "serve_settings") or "{}"),
    })
    return out


def fetch_log(conn, n: int = 20) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT attempted_at, outcome, holdings_date, http_status, ms, message FROM fetch_log ORDER BY id DESC LIMIT ?", (n,))]
