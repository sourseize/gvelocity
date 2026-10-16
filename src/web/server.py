"""
HTTP server of `velocity serve` — standard library only (ThreadingHTTPServer), bound to
localhost by default.

Routes
  /                                  hub: status strip, the live book, the latest day, months
  /month/{YYYY-MM}                   one month of days (+ live sections for the current month)
  /day/{YYYY-MM-DD}                  one day
  /strategy/{name}                   the position book + its sessions
  /strategy/{name}/session/{date}    one session: entries / exits / not taken / rising / signals / settings
  /ticker/{T}                        a ticker's signal history under the live strategy
  /status                            poller, books, fetch log, sessions
  /api/health  /api/status  /api/dates  /api/day/{date}  /api/month/{ym}
  /api/strategy/{name}  /api/strategy/{name}/sessions  /api/ticker/{T}      JSON

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
from ..reporter import day_log_entry
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
            latest = latest_date(conn)
            entry = self.entry(conn, latest, book) if latest else None
            return 200, ct, pages.hub(st, strat, book, entry, ym, data.months(conn))
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
        if parts[0] == "strategy" and len(parts) >= 2 and NAME_RE.match(parts[1]):
            name = parts[1]
            info = strat if strat and strat["name"] == name else self._other_strategy(name)
            if info is None:
                raise NotFound(f"unknown strategy {name}")
            if len(parts) == 2:
                return 200, ct, pages.strategy_book(data.load_book(conn, name), info, data.sessions(conn, name), ym)
            if len(parts) == 4 and parts[2] == "session" and DATE_RE.match(parts[3]):
                row = data.session_row(conn, name, parts[3])
                if row is None:
                    raise NotFound(f"{name} has no session on {parts[3]}")
                return 200, ct, pages.session(info, row, data.stored_signals(conn, name, parts[3]) or {}, ym)
        if parts[0] == "ticker" and len(parts) == 2 and NAME_RE.match(parts[1]):
            st = self.state
            if st is None:
                return 503, ct, pages.error(503, "starting up — the market state is not built yet", ym, strat)
            t = parts[1].upper()
            history = self.ticker_history(t)
            if history is None:
                raise NotFound(f"{t}: no price history in the fund")
            row = conn.execute("SELECT name FROM holdings WHERE ticker=? ORDER BY snapshot_date DESC LIMIT 1", (t,)).fetchone()
            return 200, ct, pages.ticker(t, strat, history, row["name"] if row else "", ym)
        if parts == ["status"]:
            st = data.status_payload(conn, self.state)
            sessions = data.sessions(conn, strat["name"], 20) if strat else []
            return 200, ct, pages.status(st, strat, books_svc.summaries(conn), data.fetch_log(conn, 20), sessions, ym, self.bind)
        raise NotFound(path)

    def _other_strategy(self, name: str) -> Optional[dict]:
        from ..strategies import REGISTRY
        cls = REGISTRY.get(name)
        return {"name": name, "label": cls.label, "description": cls.description} if cls else None

    def ticker_history(self, t: str) -> Optional[list[dict]]:
        st = self.state
        ind, strategy = st.engine.ind, st.strategy
        if t not in ind.tickers:
            return None
        closes = ind.close[t].dropna()
        return [{"date": d, "close": float(c), **strategy.explain(d, t)} for d, c in closes.items()]

    # ── JSON ──
    def api(self, conn, path: str) -> tuple[int, str, str]:
        ct = "application/json"
        parts = [p for p in path.split("/") if p]
        dump = lambda obj: (200, ct, json.dumps(obj, indent=1, default=str))
        if parts == ["health"]:
            st = data.status_payload(conn, self.state)
            return dump({"ok": True, "strategy": (self.strategy_info() or {}).get("name"), "last_cycle_at": st["last_cycle_at"],
                         "last_cycle_status": st["last_cycle_status"], "holdings_date": st["holdings_date"],
                         "holdings_updated_at": st["holdings_updated_at"], "last_poll_at": st["last_poll_at"]})
        if parts == ["status"]:
            return dump({**data.status_payload(conn, self.state), "strategy": self.strategy_info(),
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
        if parts[0] == "strategy" and len(parts) >= 2 and NAME_RE.match(parts[1]):
            if len(parts) == 2:
                return dump(data.load_book(conn, parts[1]).to_dict())
            if parts[2:] == ["sessions"]:
                return dump(data.sessions(conn, parts[1]))
        if parts[0] == "ticker" and len(parts) == 2 and NAME_RE.match(parts[1]) and self.state is not None:
            history = self.ticker_history(parts[1].upper())
            if history is None:
                raise NotFound(parts[1])
            return dump(history)
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
