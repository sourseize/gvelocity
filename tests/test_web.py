"""The web service: every route, JSON API, 404s, caching, and that it never writes."""
import json
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import database as db                                   # noqa: E402
from src.engine import EngineConfig                              # noqa: E402
from src.scheduler import Scheduler                              # noqa: E402
from src.web import data as webdata                              # noqa: E402
from src.web.server import WebServer                             # noqa: E402
from test_scheduler import Fund                                  # noqa: E402


@pytest.fixture
def site(tmp_path, monkeypatch):
    conn = db.get_db(tmp_path / "v.db")
    fund = Fund(conn)
    monkeypatch.setattr(webdata, "DB_PATH", tmp_path / "v.db")          # request connections open this file read-only
    sched = Scheduler(conn, "trend_follow", {}, EngineConfig(max_positions=3), 50000.0, poll_frequency=10, fetch=lambda c: None)
    sched.cycle()
    web = WebServer(sched, "127.0.0.1", 0)                               # port 0: any free port
    web.start()
    yield web, sched, fund, conn
    web.stop()


def get(web, path):
    try:
        with urllib.request.urlopen(web.url.rstrip("/") + path, timeout=30) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read().decode()


def test_pages_render(site):
    web, sched, fund, conn = site
    latest = db.latest_date(conn)
    ym = latest[:7]
    for path, needle in [("/", "Trend Follower book"), (f"/month/{ym}", "Signal states"), (f"/day/{latest}", latest),
                         ("/strategy/trend_follow", "position book"), (f"/strategy/trend_follow/session/{latest}", "Signals ("),
                         ("/ticker/T0", "signal history"), ("/status", "listening on 127.0.0.1"),
                         ("/strategy/double_bottom", "No sessions yet")]:
        status, ct, body = get(web, path)
        assert status == 200 and ct.startswith("text/html") and needle in body, (path, status)
    assert "UTC" in get(web, "/")[2] and "holdings" in get(web, "/")[2]


def test_api(site):
    web, sched, fund, conn = site
    latest = db.latest_date(conn)
    status, ct, body = get(web, "/api/health")
    assert status == 200 and ct == "application/json"
    h = json.loads(body)
    assert h["ok"] and h["strategy"] == "trend_follow" and h["last_cycle_at"] and h["holdings_date"] == latest
    day = json.loads(get(web, f"/api/day/{latest}")[2])
    assert day["date"] == latest and set(day["scored"]) == set(fund.tickers) and day["strategy"]["name"] == "trend_follow"
    month = json.loads(get(web, f"/api/month/{latest[:7]}")[2])
    assert month[0]["date"] == latest and {"new_entries", "exits", "movers", "top20", "trades", "rising_stars"} <= set(month[0])
    book = json.loads(get(web, "/api/strategy/trend_follow")[2])
    assert book["strategy"] == "trend_follow" and book["last_date"] == latest
    assert json.loads(get(web, "/api/strategy/trend_follow/sessions")[2])[0]["date"] == latest
    assert json.loads(get(web, "/api/dates")[2])[-1] == latest
    assert json.loads(get(web, "/api/ticker/T1")[2])[-1]["date"] == latest
    st = json.loads(get(web, "/api/status")[2])
    assert st["holdings_date"] == latest and st["books"][0]["strategy"] == "trend_follow" and "last_poll_at" in st


def test_not_found_and_bad_paths(site):
    web = site[0]
    for path in ("/nope", "/month/1999-01", "/day/1999-01-01", "/ticker/ZZZZ", "/strategy/unknown", "/api/day/1999-01-01",
                 "/strategy/trend_follow/session/1999-01-01", "/api/nothing", "/month/x", "/day/../etc/passwd"):
        status, ct, body = get(web, path)
        assert status == 404, path


def test_historical_day_uses_stored_signals_and_cache_refreshes_after_a_cycle(site):
    web, sched, fund, conn = site
    first = db.latest_date(conn)
    status, _, body1 = get(web, f"/day/{first}")
    assert status == 200
    # a new publication + cycle: the hub moves on, the old day is still served from the audit rows
    sched._fetch = lambda c: fund.publish()
    out = sched.cycle()
    new = out["stored"]
    assert new != first
    assert new in get(web, "/")[2] and json.loads(get(web, "/api/health")[2])["holdings_date"] == new
    day_old = json.loads(get(web, f"/api/day/{first}")[2])
    stored = webdata.stored_signals(conn, "trend_follow", first)
    assert stored is not None
    for t, sig in stored.items():
        assert day_old["scored"][t]["signal"]["state"] == sig["state"]


def test_requests_never_write(site):
    web, sched, fund, conn = site
    before = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    for path in ("/", "/status", "/strategy/trend_follow", "/api/status"):
        get(web, path)
    after = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert before == after
