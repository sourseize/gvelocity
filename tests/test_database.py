"""Database layer: connections, transactions, schema migration, as-of-date queries, atomic sessions."""
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import database as db                                       # noqa: E402
from src.book import Book                                            # noqa: E402
from src.config import DB_PATH                                       # noqa: E402
from src.day_data import build_day_data                              # noqa: E402
from src.engine import EngineConfig                                  # noqa: E402
from src.services import session as svc                              # noqa: E402
from test_strategies import Always, make_engine, synthetic_bars      # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
needs_db = pytest.mark.skipif(not DB_PATH.exists(), reason="no database present")


def row(ticker, px, w, shares=100.0, name=None):
    return {"ticker": ticker, "name": name or ticker, "sector": "S", "weight_pct": w, "market_value": px * shares,
            "shares": shares, "price": px, "currency": "USD", "exchange": "X", "location": "US"}


# ── connections / schema ──
def test_connect_sets_wal_and_busy_timeout(tmp_path):
    c = db.get_db(tmp_path / "x.db")
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert not c.in_transaction and c.isolation_level is None
    assert db.schema_version(c) == db.SCHEMA_VERSION
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"snapshots", "holdings", "weekly_closes", "books", "sessions", "signals", "fetch_log", "meta"} <= tables
    db.get_db(tmp_path / "x.db")                                      # idempotent


def test_readonly_connection_cannot_write(tmp_path):
    db.get_db(tmp_path / "x.db").close()
    ro = db.connect(tmp_path / "x.db", readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO meta VALUES ('k', 'v')")


def test_transaction_joins_and_rolls_back(tmp_path):
    c = db.get_db(tmp_path / "x.db")
    with db.transaction(c):
        c.execute("INSERT INTO meta VALUES ('a', '1')")
        with db.transaction(c):                                       # joins, does not nest
            c.execute("INSERT INTO meta VALUES ('b', '2')")
        assert c.in_transaction
    assert db.get_meta(c, "a") == "1" and db.get_meta(c, "b") == "2"
    with pytest.raises(RuntimeError):
        with db.transaction(c):
            c.execute("INSERT INTO meta VALUES ('c', '3')")
            raise RuntimeError("boom")
    assert db.get_meta(c, "c") is None and not c.in_transaction


def test_schema_migration_from_pre_versioned_database(tmp_path):
    p = tmp_path / "old.db"
    old = sqlite3.connect(p)
    old.executescript("""
        CREATE TABLE books (strategy TEXT PRIMARY KEY, initial_capital REAL NOT NULL, cash REAL NOT NULL, last_date TEXT,
            positions_json TEXT NOT NULL, trades_json TEXT NOT NULL, equity_json TEXT NOT NULL, last_exit_json TEXT NOT NULL,
            updated_at TEXT NOT NULL);
        CREATE TABLE sessions (strategy TEXT NOT NULL, date TEXT NOT NULL, n_entries INTEGER NOT NULL, n_exits INTEGER NOT NULL,
            n_skipped INTEGER NOT NULL, n_open INTEGER NOT NULL, n_rising INTEGER NOT NULL, cash REAL NOT NULL, equity REAL NOT NULL,
            entries_json TEXT NOT NULL, exits_json TEXT NOT NULL, skipped_json TEXT NOT NULL, run_at TEXT NOT NULL,
            PRIMARY KEY (strategy, date));
        INSERT INTO sessions VALUES ('s', '2026-01-02', 0, 0, 0, 0, 0, 1.0, 1.0, '[]', '[]', '[]', 'now');""")
    old.close()
    c = db.get_db(p)
    cols = {r[1] for r in c.execute("PRAGMA table_info(sessions)")}
    assert {"kind", "params_json", "engine_json", "rising_json"} <= cols
    assert {r[1] for r in c.execute("PRAGMA table_info(books)")} >= {"rising_json", "note"}
    assert tuple(c.execute("SELECT kind, params_json FROM sessions").fetchone()) == ("live", "{}")
    assert db.schema_version(c) == db.SCHEMA_VERSION


def test_newer_schema_is_refused(tmp_path):
    c = db.get_db(tmp_path / "x.db")
    db.set_meta(c, "schema_version", db.SCHEMA_VERSION + 1)
    c.close()
    with pytest.raises(db.SchemaTooNew):
        db.get_db(tmp_path / "x.db")


def test_store_snapshot_is_atomic(tmp_path):
    c = db.get_db(tmp_path / "x.db")
    bad_rows = [row("AAA", 10, 1.0), {"ticker": "BBB"}]               # second row lacks required columns -> fails mid-way
    with pytest.raises(sqlite3.Error):
        db.store_snapshot(c, dt.date(2026, 1, 5), bad_rows)
    assert c.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] == 0
    assert not c.in_transaction


# ── as-of-date queries ──
def test_day_data_for_a_past_date_equals_what_was_computed_at_the_time(tmp_path):
    """The property that lets pages be rendered from the database for ANY date: building the day
    payload after later snapshots were stored gives the same answer as building it on the day."""
    c = db.get_db(tmp_path / "x.db")
    days = [dt.date(2026, 1, 2) + dt.timedelta(days=i) for i in range(50)]
    days = [d for d in days if d.weekday() < 5]
    live = {}
    for i, d in enumerate(days):
        rows = [row("AAA", 10 + i * 0.1, 5.0 + (0.02 * i)), row("BBB", 20 - i * 0.05, 3.0)]
        if i >= 5:
            rows.append(row("CCC", 30 + i, 2.0 + 0.1 * (i - 5)))       # joins on day 5, weight climbs
        if i < 5 or i > 29:
            rows.append(row("DDD", 40.0, 1.0))                          # leaves on day 5, back on day 30 (> 20 sessions away: "new" again)
        db.store_snapshot(c, d, rows)
        live[d.isoformat()] = build_day_data(c, d.isoformat())
    for ds, then in live.items():
        now = build_day_data(c, ds)
        for key in ("new_tickers", "exits", "movers", "top20", "sectors", "n_snapshots"):
            assert now[key] == then[key], (ds, key)
        for t in then["scored"]:
            for key in ("first_seen", "days_in_etf", "current_price", "current_weight", "price_roc_5d", "price_roc_20d",
                        "weight_percentile", "drawdown_from_52w_high", "daily_history", "weekly_history"):
                assert now["scored"][t][key] == then["scored"][t][key], (ds, t, key)
    # and the as-of semantics themselves
    d5, d30 = days[5].isoformat(), days[30].isoformat()
    assert live[d5]["new_tickers"] == ["CCC"] and live[d30]["new_tickers"] == ["DDD"]
    assert [e["ticker"] for e in live[d5]["exits"]] == ["DDD"]
    assert live[days[29].isoformat()]["new_tickers"] == []
    assert live[days[-1].isoformat()]["scored"]["CCC"]["first_seen"] == d5
    assert db.recent_dates(c, 3, asof=d5) == [d5, days[4].isoformat(), days[3].isoformat()]
    assert db.weekly_history(c, "AAA", asof=d5)[0]["week_ending"] == db.week_ending(days[5])


@needs_db
def test_movers_and_exits_match_the_old_report_pipeline():
    """Cross-check against a month log the retired static pipeline wrote on the day.  Exits and
    20-day weight deltas only depend on the trailing sessions, so they are comparable wherever the
    old database had the full 20-session window (its `n_snapshots` tells us); ties in |delta| were
    ordered arbitrarily back then, so the top-5 is compared as values, not as an ordered list."""
    c = db.connect(DB_PATH, readonly=True)
    month = json.loads((FIXTURES / "month-2026-09.json").read_text())
    stored = set(db.all_dates(c))
    checked = 0
    for entry in month:
        d = entry["date"]
        if d not in stored:
            continue
        assert sorted(e["ticker"] for e in db.detect_exits(c, d)) == sorted(e["ticker"] for e in entry["exits"]), d
        if entry["n_snapshots"] < 20:
            continue
        movers = db.detect_weight_movers(c, d, 20)
        by_ticker = {m["ticker"]: m["weight_delta"] for m in movers}
        for f in entry["movers"]:
            assert abs(by_ticker[f["ticker"]] - f["delta"]) < 1e-9, (d, f["ticker"])
        assert sorted(round(abs(m["weight_delta"]), 6) for m in movers[:5]) == \
               sorted(round(abs(f["delta"]), 6) for f in entry["movers"]), d
        checked += 1
    assert checked >= 10


# ── atomic sessions ──
def _db_with_snapshots(tmp_path, dates):
    c = db.get_db(tmp_path / "s.db")
    for d in dates:
        db.store_snapshot(c, dt.date.fromisoformat(d), [row("AAA", 1.0, 1.0)])
    return c


def test_persist_session_writes_session_signals_and_book_together(tmp_path, monkeypatch):
    bars = synthetic_bars(tickers=("T0", "T1", "T2"))
    eng = make_engine(bars, Always(), EngineConfig(max_positions=2))
    c = _db_with_snapshots(tmp_path, eng.dates[:10])
    book = Book("always", 10000)
    d = eng.dates[5]
    res = svc.run_and_persist(c, eng, book, d)
    assert len(res["entries"]) == 2
    s = dict(c.execute("SELECT * FROM sessions WHERE strategy='always'").fetchone())
    assert s["date"] == d and s["kind"] == "live" and s["n_entries"] == 2
    assert json.loads(s["engine_json"])["max_positions"] == 2 and json.loads(s["rising_json"])[0]["ticker"]
    assert Book.load(c, "always").last_date == d
    # second run of the same date: idempotent, nothing new written
    assert svc.run_and_persist(c, eng, book, d)["skipped_session"]
    assert c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1

    # a failure while writing the signals rolls back the session row AND the book
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(svc, "log_signals", boom)
    book2 = Book.load(c, "always")
    with pytest.raises(sqlite3.OperationalError):
        svc.run_and_persist(c, eng, book2, eng.dates[6])
    assert Book.load(c, "always").last_date == d
    assert c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert not c.in_transaction


def test_run_due_sessions_catches_up_in_order(tmp_path):
    bars = synthetic_bars(tickers=("T0", "T1"))
    eng = make_engine(bars, Always(), EngineConfig(max_positions=5))
    c = _db_with_snapshots(tmp_path, eng.dates[:8])
    book = Book("always", 10000)
    book.last_date = eng.dates[3]
    results = svc.run_due_sessions(c, eng, book)
    assert [r["date"] for r in results] == eng.dates[4:8]
    kinds = [r[0] for r in c.execute("SELECT kind FROM sessions ORDER BY date")]
    assert kinds == ["catchup", "catchup", "catchup", "live"]
    assert Book.load(c, "always").last_date == eng.dates[7]
    assert svc.run_due_sessions(c, eng, book) == []
    # without catch-up only the latest date runs
    book.last_date = eng.dates[3]
    assert [r["date"] for r in svc.run_due_sessions(c, eng, book, catchup=False)] == [eng.dates[7]]
