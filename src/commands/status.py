"""
velocity status — what state is Velocity in?

Works whether or not the web service is running.  While `velocity serve` holds the database it
only reports that (and who holds the lock); otherwise it reads the database: holdings history and
when it was last updated, the poller's last/next poll and last cycle, each strategy's book and the
last fetch attempts.  All times are UTC.
"""
import json

from ..config import DB_PATH
from ..database import get_meta, latest_date, schema_version
from ..lock import current_holder, describe
from ..services import books as books_svc, holdings as holdings_svc
from ..util import fmt_utc
from ._common import open_db

HELP = "Show the database, books and poller state (works while the web service runs)"
NEEDS_DB = False          # handles the lock itself: reports the holder instead of waiting for the database


def configure(p) -> None:
    pass


def poller_lines(conn) -> list[str]:
    """The poller / holdings freshness block (shared with the web status page)."""
    out = []
    latest = latest_date(conn)
    fetched_at = conn.execute("SELECT fetched_at FROM snapshots WHERE fetch_date=?", (latest,)).fetchone() if latest else None
    out.append(f"holdings updated : {fmt_utc(fetched_at[0]) if fetched_at else '—'}" + (f"  (holdings date {latest})" if latest else ""))
    out.append(f"last poll        : {fmt_utc(get_meta(conn, 'last_poll_at'))}" +
               (f"  — {get_meta(conn, 'last_poll_outcome')}" if get_meta(conn, "last_poll_outcome") else ""))
    if get_meta(conn, "last_poll_detail"):
        out.append(f"{'':<17}  {get_meta(conn, 'last_poll_detail')}")
    if get_meta(conn, "next_poll_at") and not get_meta(conn, "serve_stopped_at"):
        out.append(f"next poll        : {fmt_utc(get_meta(conn, 'next_poll_at'))}")
    out.append(f"last cycle       : {fmt_utc(get_meta(conn, 'last_cycle_at'))}" +
               (f"  — {get_meta(conn, 'last_cycle_status')}" if get_meta(conn, "last_cycle_status") else ""))
    if get_meta(conn, "last_session_date"):
        out.append(f"last session     : {get_meta(conn, 'last_session_date')}")
    if get_meta(conn, "serve_strategy"):
        settings = json.loads(get_meta(conn, "serve_settings") or "{}")
        stopped = get_meta(conn, "serve_stopped_at")
        out.append(f"serve            : {get_meta(conn, 'serve_strategy')} · started {fmt_utc(get_meta(conn, 'serve_started_at'))}" +
                   (f" · stopped {fmt_utc(stopped)}" if stopped else "") +
                   (f" · poll every {settings.get('poll_frequency_min')} min" if settings else ""))
    return out


def run(args) -> int:
    holder = current_holder()
    if holder:
        print(describe(holder))
        print("(the database is in use — stop it to see book and history details)")
        return 0
    print("velocity serve   : not running")
    if not DB_PATH.exists():
        print(f"database         : {DB_PATH} — not created yet (run `velocity backfill`)")
        return 0
    conn = open_db()
    try:
        n = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        first = conn.execute("SELECT MIN(fetch_date) FROM snapshots").fetchone()[0]
        print(f"database         : {DB_PATH} (schema v{schema_version(conn)}, {DB_PATH.stat().st_size / 1e6:.1f} MB)")
        print(f"holdings         : {n} sessions" + (f", {first} → {latest_date(conn)}" if n else " — run `velocity backfill`"))
        for line in poller_lines(conn):
            print(line)
        books = books_svc.summaries(conn)
        print("books            : " + ("none — `velocity serve` creates one" if not books else ""))
        for b in books:
            print(f"  {b['strategy']:<15} last session {b['last_date'] or '—'} ({b['last_kind'] or '—'}) · "
                  f"equity ${b['equity']:,.0f} · cash ${b['cash']:,.0f} · {b['open']} open · {b['trades']} trades · "
                  f"{b['rising']} rising · {b['sessions']} sessions")
            if b["note"]:
                print(f"  {'':<15} {b['note']}")
        fetches = holdings_svc.last_fetches(conn, 5)
        print("last fetches     : " + ("none" if not fetches else ""))
        for f in fetches:
            extra = f" {f['holdings_date']}" if f["holdings_date"] else ""
            print(f"  {fmt_utc(f['attempted_at'])}  {f['outcome']:<16}{extra}  {f['message'] or ''}")
        return 0
    finally:
        conn.close()
