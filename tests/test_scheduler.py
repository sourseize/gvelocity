"""The poller: cycles, catch-up after downtime, survival of failures, settings guard, the loop."""
import datetime as dt
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import database as db                                   # noqa: E402
from src.book import Book                                        # noqa: E402
from src.engine import EngineConfig                              # noqa: E402
from src.scheduler import Scheduler, ParamsChanged               # noqa: E402
from src.services import session as svc                          # noqa: E402


def bdays(start: dt.date, n: int):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


class Fund:
    """Synthetic fund: 6 tickers, random-walk prices, fixed shares; `publish()` stores the next day."""
    def __init__(self, conn, n_days=160, seed=1):
        self.conn, self.rng = conn, np.random.default_rng(seed)
        self.tickers = [f"T{i}" for i in range(6)]
        self.px = {t: 100.0 for t in self.tickers}
        self.days = iter(bdays(dt.date(2026, 1, 2), 400))
        for _ in range(n_days):
            self.publish()

    def rows(self):
        for t in self.tickers:
            self.px[t] *= 1 + self.rng.normal(0.001, 0.015)
        return [{"ticker": t, "name": t, "sector": "S", "weight_pct": 100 / len(self.tickers), "market_value": 1.0,
                 "shares": 1000.0, "price": round(self.px[t], 2), "currency": "USD", "exchange": "X", "location": "US"}
                for t in self.tickers]

    def publish(self) -> str:
        d = next(self.days)
        db.store_snapshot(self.conn, d, self.rows())
        return d.isoformat()


@pytest.fixture
def env(tmp_path):
    conn = db.get_db(tmp_path / "v.db")
    return conn, Fund(conn)


def make(conn, fetch, **kw):
    cfg = kw.pop("cfg", EngineConfig(max_positions=3))
    return Scheduler(conn, kw.pop("strategy", "trend_follow"), kw.pop("params", {}), cfg, 50000.0,
                     poll_frequency=kw.pop("poll_frequency", 10), fetch=fetch, **kw)


def test_first_cycle_starts_a_fresh_book_on_the_latest_date_only(env):
    conn, fund = env
    latest = db.latest_date(conn)
    s = make(conn, fetch=lambda c: None)                         # fetch finds nothing
    out = s.cycle()
    assert out["stored"] is None and out["sessions"] == [latest]      # NOT a replay of the whole history
    assert out["status"].startswith("fetch failed") and out["level"] == "warn" and "session " + latest in out["status"]
    assert Book.load(conn, "trend_follow").last_date == latest
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert db.get_meta(conn, "last_poll_at") and db.get_meta(conn, "last_cycle_at") and db.get_meta(conn, "last_session_date") == latest


def test_new_holdings_trigger_a_rebuild_and_one_session(env):
    conn, fund = env
    s = make(conn, fetch=lambda c: db.latest_date(c))           # "already stored"
    s.cycle()
    built = s.state.built_at
    s.cycle()
    assert s.state.built_at == built                              # nothing new: no rebuild, no session
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1

    s._fetch = lambda c: fund.publish()                           # the fund publishes a new day
    time.sleep(1.05)
    out = s.cycle()
    assert out["stored"] == db.latest_date(conn) and out["sessions"] == [out["stored"]]
    assert s.state.built_at != built and s.state.latest_date == out["stored"]
    assert "new holdings" in db.get_meta(conn, "last_poll_outcome")
    assert Book.load(conn, "trend_follow").last_date == out["stored"]


def test_downtime_is_caught_up_in_order(env):
    conn, fund = env
    s = make(conn, fetch=lambda c: None)
    s.cycle()
    missed = [fund.publish() for _ in range(3)]                   # three publications while we were down
    s._fetch = lambda c: db.latest_date(c)
    out = s.cycle()
    assert out["sessions"] == missed
    kinds = [r[0] for r in conn.execute("SELECT kind FROM sessions WHERE date IN (?,?,?) ORDER BY date", missed)]
    assert kinds == ["catchup", "catchup", "live"]
    assert "3 sessions" in out["status"]


def test_without_catchup_only_the_latest_session_runs(env):
    conn, fund = env
    s = make(conn, fetch=lambda c: None, catchup=False)
    s.cycle()
    missed = [fund.publish() for _ in range(3)]
    s._fetch = lambda c: db.latest_date(c)
    assert s.cycle()["sessions"] == missed[-1:]


def test_a_failing_cycle_is_recorded_and_the_next_one_recovers(env):
    conn, fund = env

    def boom(c):
        raise RuntimeError("blackrock exploded")
    s = make(conn, fetch=boom)
    out = s.cycle()
    assert out["status"].startswith("cycle failed: RuntimeError") and out["level"] == "error" and not conn.in_transaction
    assert db.get_meta(conn, "last_cycle_level") == "error"
    s._fetch = lambda c: db.latest_date(c)
    out = s.cycle()
    assert out["level"] == "ok" and out["status"].startswith("holdings") and "already stored" in out["status"]


def test_settings_guard(env):
    conn, fund = env
    make(conn, fetch=lambda c: None).cycle()                      # a session with max_positions=3 is recorded
    same = make(conn, fetch=lambda c: None)
    assert same.check_settings() == []
    changed = make(conn, fetch=lambda c: None, cfg=EngineConfig(max_positions=5), params={"breakout_len": 40})
    with pytest.raises(ParamsChanged) as e:
        changed.check_settings()
    assert "max_positions: 3 → 5" in str(e.value) and "--param breakout_len: 55 → 40" in str(e.value)
    diffs = changed.check_settings(accept_change=True)
    assert len(diffs) == 2
    assert make(conn, fetch=lambda c: None, strategy="range_breakout").check_settings() == []   # no session yet


def test_run_forever_polls_and_stops(env):
    conn, fund = env
    s = make(conn, fetch=lambda c: None, poll_frequency=0.002)    # ~0.1 s between cycles
    s.on_cycle.append(lambda summary: s.stop() if s.cycles >= 3 else None)
    t0 = time.monotonic()
    s.run_forever()
    assert s.cycles == 3 and time.monotonic() - t0 < 20
    assert db.get_meta(conn, "next_poll_at")


def test_due_dates_semantics(env):
    conn, fund = env
    s = make(conn, fetch=lambda c: None)
    st = s.rebuild_state()
    latest = db.latest_date(conn)
    assert svc.due_dates(conn, st.engine, Book("x")) == [latest]                   # fresh book: today only
    b = Book("x"); b.last_date = st.engine.dates[-4]
    assert svc.due_dates(conn, st.engine, b) == st.engine.dates[-3:]              # behind: every missed date
    b.last_date = latest
    assert svc.due_dates(conn, st.engine, b) == []                                # up to date
