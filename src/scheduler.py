"""
scheduler — the 24×7 poller behind `velocity serve`.

One thread, one writer.  Every `poll_frequency` minutes, round the clock:

    cycle():
      1. fetch the latest holdings CSV and store it if it is a new date    (services.holdings)
      2. if anything changed (or on the first cycle) rebuild the market state:
         bars → indicators → prepared strategy → Engine                      (≈ 2 s)
      3. run every session the book has not processed yet, oldest first,
         each persisted atomically                                           (services.session)
      4. record what happened in `meta` (last poll, last cycle, status) for `velocity status`
         and the web pages

A failing cycle is logged and recorded — the loop never dies; the next cycle retries.
Timestamps are UTC ISO-8601 like everything else in the database.

The web server reads `scheduler.state` — an immutable `MarketState` that is swapped
as a whole after each rebuild — to explain signals; everything else it reads from the database.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import random
import sqlite3
import threading
import traceback
from dataclasses import asdict, dataclass
from typing import Callable, Optional

from . import analytics
from .book import Book
from .database import get_meta, set_meta, latest_date, transaction
from .engine import Engine, EngineConfig
from .exceptions import VelocityError
from .services import books as books_svc, holdings as holdings_svc, session as session_svc
from .strategy import TrendFollow, make_strategy, STRATEGY_NAME
from .util import utcnow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarketState:
    """Everything derived from the holdings history that pages need: built once per change."""
    engine: Engine
    book: Book
    built_at: str
    latest_date: Optional[str]
    bench: object = None          # analytics.benchmark_returns() frame (fund / equal-weight daily returns)

    @property
    def strategy(self) -> TrendFollow:
        return self.engine.strategy


class ParamsChanged(VelocityError):
    """The strategy / engine settings differ from those the book's last session ran with."""


def settings_diff(conn: sqlite3.Connection, strategy: TrendFollow, cfg: EngineConfig) -> list[str]:
    """Human-readable differences between the given settings and the ones recorded on the
    strategy's most recent session (empty when equal or when there is no session yet)."""
    row = conn.execute("SELECT params_json, engine_json FROM sessions WHERE strategy=? ORDER BY date DESC LIMIT 1",
                       (strategy.name,)).fetchone()
    if not row:
        return []
    diffs = []
    old_p, new_p = json.loads(row["params_json"] or "{}"), strategy.params_dict()
    for k in sorted(set(old_p) | set(new_p)):
        if old_p.get(k) != new_p.get(k):
            diffs.append(f"--param {k}: {old_p.get(k)!r} → {new_p.get(k)!r}")
    old_e, new_e = json.loads(row["engine_json"] or "{}"), asdict(cfg)
    for k in sorted(set(old_e) & set(new_e)):
        if k == "short_history":            # a mode of operation (live warns, backtests refuse), not a setting
            continue
        if old_e.get(k) != new_e.get(k):
            diffs.append(f"{k}: {old_e.get(k)!r} → {new_e.get(k)!r}")
    return diffs


class Scheduler:
    def __init__(self, conn: sqlite3.Connection, params: dict, cfg: EngineConfig,
                 capital: float, poll_frequency: float = 10.0, catchup: bool = True,
                 fetch: Optional[Callable[[sqlite3.Connection], Optional[str]]] = None):
        self.conn = conn
        self.strategy_name, self.params, self.cfg, self.capital = STRATEGY_NAME, params, cfg, capital
        self.poll_frequency = poll_frequency
        self.catchup = catchup
        self._fetch = fetch or holdings_svc.fetch_and_store
        self.state: Optional[MarketState] = None
        self.stop_event = threading.Event()
        self.cycles = 0
        self.on_cycle: list[Callable[[dict], None]] = []      # listeners (the web server's live refresh)

    # ── start-up ──
    def check_settings(self, accept_change: bool = False) -> list[str]:
        """Refuse to run a book with different settings than its last session unless told to."""
        strategy = make_strategy(**self.params)
        diffs = settings_diff(self.conn, strategy, self.cfg)
        if diffs and not accept_change:
            raise ParamsChanged(f"{strategy.name}: settings differ from the book's last session:\n  " + "\n  ".join(diffs) +
                                "\n  re-run with --accept-param-change to continue the book with the new settings "
                                "(recorded on every session), or `velocity resetbook` to start over")
        if diffs:
            log.warning("[%s] continuing the book with changed settings: %s", strategy.name, "; ".join(diffs))
        return diffs

    def rebuild_state(self) -> MarketState:
        strategy = make_strategy(**self.params)
        engine = Engine(strategy, self.conn, self.cfg)
        book = books_svc.load(self.conn, strategy.name, self.capital)
        bench = analytics.benchmark_returns(self.conn, engine.ind)
        self.state = MarketState(engine=engine, book=book, built_at=utcnow(), latest_date=latest_date(self.conn), bench=bench)
        return self.state

    # ── one cycle ──
    def cycle(self) -> dict:
        """fetch → (rebuild) → due sessions → meta.  Returns a summary dict; never raises.

        summary["level"] is "ok" (fetched fine), "warn" (the fetch failed; sessions still ran on
        the stored history) or "error" (the cycle itself broke); summary["status"] is the one-line
        description and summary["detail"] the full fetch error, if any."""
        started = utcnow()
        summary = {"started_at": started, "stored": None, "sessions": [], "status": "", "detail": "", "level": "ok"}
        try:
            before = latest_date(self.conn)
            hdate = self._fetch(self.conn)
            after = latest_date(self.conn)
            if hdate and after != before:
                summary["stored"] = after
            outcome, detail = self._poll_outcome(self.conn, hdate, summary["stored"])
            summary["detail"] = detail
            if hdate is None:
                summary["level"] = "warn"
            with transaction(self.conn):
                set_meta(self.conn, "last_poll_at", started)
                set_meta(self.conn, "last_poll_outcome", outcome)
                set_meta(self.conn, "last_poll_detail", detail)
            if self.state is None or self.state.latest_date != after:       # new history -> new bars/indicators
                self.rebuild_state()
            st = self.state
            results = session_svc.run_due_sessions(self.conn, st.engine, st.book, catchup=self.catchup)
            summary["sessions"] = [r["date"] for r in results]
            if results:
                sess = f"{len(results)} sessions {results[0]['date']} → {results[-1]['date']}" if len(results) > 1 \
                    else f"session {results[0]['date']}"
            else:
                sess = "no session due"
            summary["status"] = f"{outcome} · {sess}"
        except Exception as e:          # the loop must survive anything; the next cycle retries
            log.debug("%s", traceback.format_exc())
            summary["level"] = "error"
            summary["status"] = f"cycle failed: {type(e).__name__}: {e}"
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
        finally:
            self.cycles += 1
            try:
                with transaction(self.conn):
                    set_meta(self.conn, "last_cycle_at", started)
                    set_meta(self.conn, "last_cycle_status", summary["status"])
                    set_meta(self.conn, "last_cycle_level", summary["level"])
                    if self.state and self.state.book.last_date:
                        set_meta(self.conn, "last_session_date", self.state.book.last_date)
            except sqlite3.Error as e:
                log.error("could not record the cycle in meta: %s", e)
        log.log({"ok": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}[summary["level"]],
                "cycle %d: %s%s", self.cycles, summary["status"], f" — {summary['detail']}" if summary["detail"] else "")
        for fn in self.on_cycle:
            try:
                fn(summary)
            except Exception:       # a listener must not break the loop
                log.exception("cycle listener failed")
        return summary

    @staticmethod
    def _poll_outcome(conn: sqlite3.Connection, hdate: Optional[str], stored: Optional[str]) -> tuple[str, str]:
        """(short outcome, detail) — the detail is the full fetch error message, empty on success."""
        if stored:
            return f"new holdings {stored} stored", ""
        if hdate:
            return f"holdings {hdate} already stored", ""
        last = conn.execute("SELECT outcome, message FROM fetch_log ORDER BY id DESC LIMIT 1").fetchone()
        if last:
            return f"fetch failed ({last['outcome']})", last["message"] or ""
        return "fetch failed", ""

    # ── the loop ──
    def run_forever(self) -> None:
        """Cycle, then sleep `poll_frequency` minutes (± a little jitter), until stopped."""
        log.info("poller: every %g min, strategy %s, catch-up %s", self.poll_frequency, self.strategy_name,
                 "on" if self.catchup else "off")
        while not self.stop_event.is_set():
            self.cycle()
            wait = self.poll_frequency * 60 + random.uniform(-5, 5) if self.poll_frequency >= 1 else self.poll_frequency * 60
            set_meta(self.conn, "next_poll_at",
                     (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=wait)).isoformat(timespec="seconds"))
            if self.stop_event.wait(max(wait, 0.0)):
                break
        log.info("poller stopped")

    def stop(self) -> None:
        self.stop_event.set()


def record_start(conn: sqlite3.Connection, strategy: TrendFollow, cfg: EngineConfig, capital: float, poll_frequency: float) -> None:
    with transaction(conn):
        set_meta(conn, "serve_started_at", utcnow())
        set_meta(conn, "serve_strategy", strategy.name)
        set_meta(conn, "serve_settings", json.dumps({"params": strategy.params_dict(), "engine": asdict(cfg),
                                                     "capital": capital, "poll_frequency_min": poll_frequency}))
        for k in ("serve_stopped_at",):
            set_meta(conn, k, None)


def record_stop(conn: sqlite3.Connection) -> None:
    set_meta(conn, "serve_stopped_at", utcnow())
