"""
HTTP server of `velocity serve` — standard library only (ThreadingHTTPServer), bound to
localhost by default.

Routes
  /                                  hub: status strip, the book vs the fund, the latest day, months
  /book                              the performance page: equity vs fund, drawdown, exposure, positions,
                                     sector exposure, rolling quarters, trade analytics, trades, sessions
  /session/{date}                    one session: entries / exits / not taken / waiting / signals / settings
  /ticker/{T}                        price chart with the book's trades, current checks, signal history
  /signals                           signal quality: forward excess return per rule
  /month/{YYYY-MM}                   one month of holdings days (+ live sections for the current month)
  /day/{YYYY-MM-DD}                  one holdings day
  /status                            poller, book, fetch log, sessions
  /api/health  /api/status  /api/dates  /api/day/{date}  /api/month/{ym}  /api/book  /api/performance
  /api/sessions  /api/session/{date}  /api/ticker/{T}  /api/signals                         JSON

Requests read the database through their own read-only connection and the poller's immutable
`MarketState`; nothing here writes.
"""
from __future__ import annotations

import json
import logging
import re
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import unquote, urlsplit

from ..book import Book
from ..database import latest_date
from ..scheduler import Scheduler
from ..services import books as books_svc
from . import data, pages

log = logging.getLogger(__name__)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")


class NotFound(Exception):
    pass


class App:
    """Request-independent context: the scheduler (for its market state) and the page cache."""

    def __init__(self, scheduler: Scheduler, bind: str):
        self.scheduler = scheduler
        self.bind = bind
        self.cache = data.DayEntryCache()
        self.sigcache = data.SignalQualityCache()
        scheduler.on_cycle.append(lambda summary: self.cache.clear() if summary.get("stored") or summary.get("sessions") else None)

    # ── shared bits ──
    @property
    def state(self):
        return self.scheduler.state

    def strategy_info(self) -> Optional[dict]:
        st = self.state
        if st is None:
            return None
        s = st.strategy
        return {"name": s.name, "label": s.label, "description": s.description}

    def book(self, conn) -> Optional[Book]:
        st = self.state
        return data.load_book(conn, st.strategy.name) if st else None

    def current_month(self, conn) -> Optional[str]:
        d = latest_date(conn)
        return d[:7] if d else None

    def entry(self, conn, date: str, book: Optional[Book]) -> dict:
        return self.cache.get(conn, date, self.state, book)

    # ── pages ──
    def handle(self, path: str) -> tuple[int, str, str]:
        """-> (status, content-type, body)."""
        conn = data.ro()
        try:
            if path.startswith("/api/"):
                return self.api(conn, path[5:])
            return self.html(conn, path)
        finally:
            conn.close()

    def html(self, conn, path: str) -> tuple[int, str, str]:
        ct = "text/html; charset=utf-8"
        strat, ym = self.strategy_info(), self.current_month(conn)
        parts = [p for p in path.split("/") if p]
        if not parts:
            st = data.status_payload(conn, self.state)
            book = self.book(conn)
            view = data.book_view(conn, self.state, book) if book is not None and book.last_date else None
            latest = latest_date(conn)
            entry = self.entry(conn, latest, book) if latest else None
            return 200, ct, pages.hub(st, strat, view, entry, ym, data.months(conn))
        if parts == ["book"]:
            if strat is None:
                return 503, ct, pages.error(503, "starting up — the market state is not built yet", ym, strat)
            book = self.book(conn)
            view = data.book_view(conn, self.state, book)
            settings = data.status_payload(conn, self.state).get("serve_settings") or {}
            return 200, ct, pages.book_page(view, strat, data.sessions(conn, strat["name"]), ym, settings)
        if parts[0] == "session" and len(parts) == 2 and DATE_RE.match(parts[1]) and strat:
            row = data.session_row(conn, strat["name"], parts[1])
            if row is None:
                raise NotFound(f"no session on {parts[1]}")
            return 200, ct, pages.session(strat, row, data.stored_signals(conn, strat["name"], parts[1]) or {}, ym)
        if parts == ["signals"]:
            st = self.state
            if st is None:
                return 503, ct, pages.error(503, "starting up — the market state is not built yet", ym, strat)
            rows = self.sigcache.get(st)
            return 200, ct, pages.signals_page(rows, strat, st.engine.dates, len(st.engine.ind.tickers), ym)
        if parts[0] == "month" and len(parts) == 2 and MONTH_RE.match(parts[1]):
            months = data.months(conn)
            if parts[1] not in months:
                raise NotFound(f"no holdings in {parts[1]}")
            book = self.book(conn)
            dates = data.dates_in_month(conn, parts[1])
            entries = [self.entry(conn, d, book) for d in reversed(dates)]
            is_current = parts[1] == ym
            dd = data.day_data(conn, dates[-1], self.state, book) if is_current else None
            return 200, ct, pages.month(parts[1], entries, months, is_current, dd, strat, ym)
        if parts[0] == "day" and len(parts) == 2 and DATE_RE.match(parts[1]):
            if parts[1] not in data.all_dates(conn):
                raise NotFound(f"no holdings stored for {parts[1]}")
            return 200, ct, pages.day(parts[1], self.entry(conn, parts[1], self.book(conn)), strat, ym)
        if parts[0] == "ticker" and len(parts) == 2 and NAME_RE.match(parts[1]):
            st = self.state
            if st is None:
                return 503, ct, pages.error(503, "starting up — the market state is not built yet", ym, strat)
            t = parts[1].upper()
            view = data.ticker_view(st, self.book(conn), t)
            if view is None:
                raise NotFound(f"{t}: no price history in the fund")
            row = conn.execute("SELECT name, sector FROM holdings WHERE ticker=? ORDER BY snapshot_date DESC LIMIT 1", (t,)).fetchone()
            return 200, ct, pages.ticker(view, strat, row["name"] if row else "", (row["sector"] if row else "") or "", ym)
        if parts == ["status"]:
            st = data.status_payload(conn, self.state)
            sessions = data.sessions(conn, strat["name"], 20) if strat else []
            return 200, ct, pages.status(st, strat, books_svc.summaries(conn), data.fetch_log(conn, 20), sessions, ym, self.bind)
        raise NotFound(path)

    # ── JSON ──
    def api(self, conn, path: str) -> tuple[int, str, str]:
        ct = "application/json"
        parts = [p for p in path.split("/") if p]
        dump = lambda obj: (200, ct, json.dumps(data.jsonable(obj), indent=1, default=str))
        strat = self.strategy_info()
        if parts == ["health"]:
            st = data.status_payload(conn, self.state)
            return dump({"ok": True, "strategy": (strat or {}).get("name"), "last_cycle_at": st["last_cycle_at"],
                         "last_cycle_status": st["last_cycle_status"], "holdings_date": st["holdings_date"],
                         "holdings_updated_at": st["holdings_updated_at"], "last_poll_at": st["last_poll_at"]})
        if parts == ["status"]:
            return dump({**data.status_payload(conn, self.state), "strategy": strat,
                         "books": books_svc.summaries(conn), "fetch_log": data.fetch_log(conn, 20)})
        if parts == ["dates"]:
            return dump(data.all_dates(conn))
        if parts[0] == "day" and len(parts) == 2 and DATE_RE.match(parts[1]):
            if parts[1] not in data.all_dates(conn):
                raise NotFound(parts[1])
            return dump(data.day_data(conn, parts[1], self.state, self.book(conn)))
        if parts[0] == "month" and len(parts) == 2 and MONTH_RE.match(parts[1]):
            dates = data.dates_in_month(conn, parts[1])
            if not dates:
                raise NotFound(parts[1])
            book = self.book(conn)
            return dump([self.entry(conn, d, book) for d in reversed(dates)])
        if parts == ["book"] and strat:
            return dump(data.load_book(conn, strat["name"]).to_dict())
        if parts == ["performance"] and strat:
            view = data.book_view(conn, self.state, self.book(conn))
            return dump({k: v for k, v in view.items() if k != "book"})
        if parts == ["sessions"] and strat:
            return dump(data.sessions(conn, strat["name"]))
        if parts[0] == "session" and len(parts) == 2 and DATE_RE.match(parts[1]) and strat:
            row = data.session_row(conn, strat["name"], parts[1])
            if row is None:
                raise NotFound(parts[1])
            return dump({**row, "signals": data.stored_signals(conn, strat["name"], parts[1]) or {}})
        if parts[0] == "ticker" and len(parts) == 2 and NAME_RE.match(parts[1]) and self.state is not None:
            view = data.ticker_view(self.state, self.book(conn), parts[1].upper())
            if view is None:
                raise NotFound(parts[1])
            return dump(view)
        if parts == ["signals"] and self.state is not None:
            return dump(self.sigcache.get(self.state))
        raise NotFound("/api/" + path)


class Handler(BaseHTTPRequestHandler):
    server_version = "Velocity/1.0"
    app: App = None     # set by make_server

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        try:
            status, ct, body = self.app.handle(path)
        except NotFound as e:
            status, ct, body = 404, "text/html; charset=utf-8", pages.error(404, f"not found: {e}")
        except Exception as e:       # a broken page must not take the server down
            log.exception("request %s failed", path)
            status, ct, body = 500, "text/html; charset=utf-8", pages.error(500, f"{type(e).__name__}: {e}")
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):      # quiet access log (DEBUG), errors still surface via log.exception
        log.debug("%s %s", self.address_string(), fmt % args)


class WebServer:
    def __init__(self, scheduler: Scheduler, host: str, port: int):
        self.host, self.port = host, port
        handler = type("BoundHandler", (Handler,), {})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        bound_host, bound_port = self.httpd.server_address[:2]
        self.url = f"http://{bound_host}:{bound_port}/"
        handler.app = App(scheduler, f"{bound_host}:{bound_port}")
        self.app = handler.app
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="web", daemon=True)
        self._thread.start()
        log.info("web: listening on %s (host %s, port %d, hostname %s)", self.url, self.host, self.port, socket.gethostname())

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        log.info("web: stopped")
