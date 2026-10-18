"""Tests for the strategy, the engine (sizing, limits, rising stars), the book and the analytics.
Run:  python -m pytest tests -q"""
import sqlite3, sys
from pathlib import Path
import numpy as np, pandas as pd, pytest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import market_data as md, analytics
from src.indicators import Indicators
from src.strategy import TrendFollow, make_strategy, parse_params, EntrySignal, STRATEGY_NAME
from src.engine import Engine, EngineConfig, MaxPositionsPolicy
from src.book import Book
from src.config import DB_PATH
from src.database import get_db

needs_db = pytest.mark.skipif(not DB_PATH.exists(), reason="no database present")


def ro_conn():
    """Read-only connection to the real database (tests must not modify it)."""
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def synthetic_bars(n=160, seed=0, tickers=("AAA", "BBB", "CCC")):
    rng = np.random.default_rng(seed)
    dates = [str(d.date()) for d in pd.bdate_range("2026-01-01", periods=n)]
    rows = []
    for t in tickers:
        px = 100 * np.cumprod(1 + rng.normal(0.002, 0.01, n))
        for d, p in zip(dates, px):
            rows.append((d, t, p, p * 1.01, p * 0.99, 1e6 * (1 + rng.random())))
    return pd.DataFrame(rows, columns=["date", "ticker", "close", "high", "low", "volume"])


# ── the strategy ──
def test_params_and_validation():
    s = make_strategy(breakout_len=40)
    assert s.p["breakout_len"] == 40 and isinstance(s, TrendFollow) and s.name == STRATEGY_NAME
    with pytest.raises(ValueError):
        make_strategy(bogus=1)
    with pytest.raises(ValueError):
        make_strategy(breakout_len="x")
    with pytest.raises(ValueError):
        make_strategy(rs_min_pct=-1)
    assert parse_params(["a=1", "b=0.5", "c=true", "d=x"]) == {"a": 1, "b": 0.5, "c": True, "d": "x"}
    assert make_strategy().warmup() == 61


def test_strategy_entry_exit_and_explain_on_a_trend():
    # a steady uptrend: every day is a new high -> ENTRY once warm; a crash -> EXIT
    n = 120
    dates = [str(d.date()) for d in pd.bdate_range("2026-01-01", periods=n)]
    rng = np.random.default_rng(3)
    up = 100 * np.cumprod(1 + np.r_[np.full(100, 0.004), np.full(20, -0.03)] + rng.normal(0, 0.003, n))
    flat = np.full(n, 50.0) + np.sin(np.arange(n)) * 0.1
    bars = pd.DataFrame({"date": dates * 2, "ticker": ["UP"] * n + ["FLAT"] * n, "close": np.r_[up, flat]})
    st = make_strategy()
    st.prepare(Indicators(bars))
    entry_days = [d for d in dates[70:100] if st.explain(d, "UP")["state"] == "ENTRY"]
    assert len(entry_days) > 10, "a steady uptrend must produce entries once warm"
    assert st.explain(dates[-1], "UP")["state"] == "EXIT"
    assert sum(st.explain(d, "FLAT")["state"] == "ENTRY" for d in dates[70:100]) <= 3
    assert st.explain(dates[90], "NOPE")["state"] == "NONE"
    book = Book("t", 1000)
    sigs = st.entries(entry_days[0], {"UP": {"ticker": "UP"}, "FLAT": {"ticker": "FLAT"}}, book)
    assert [x.ticker for x in sigs] == ["UP"] and 0.08 <= sigs[0].trail <= 0.25 and sigs[0].stop_price < sigs[0].ref_price
    assert st.exits(dates[-1], _held(book, "UP", up[-1]))[0].reason.startswith("close below")


def test_issuer_dedup_and_symbol_filter():
    assert md.issuer_key("GOOGL") == md.issuer_key("GOOG")
    assert not md.is_tradeable_symbol("-") and md.is_tradeable_symbol("BRK.B")


# ── indicators ──
def test_indicators_basics():
    ind = Indicators(synthetic_bars())
    assert ind.vol_trail().stack().between(0.08, 0.25).all()
    assert ind.hh(5).iloc[10]["AAA"] == ind.close["AAA"].iloc[5:10].max()        # previous 5 bars, today excluded
    assert ind.rs_pct(20).iloc[-1].max() == 1.0
    assert ind.below_sma_days(10)["AAA"].max() >= 1 and (ind.below_sma_days(10) >= 0).all().all()
    with pytest.raises(ValueError):
        Indicators(pd.DataFrame(columns=["date", "ticker", "close"]))


def _held(book, ticker, px):
    book.open(ticker, ticker, px, "2026-01-02", 100, 1.0, px * 0.5, None, None, "r")
    return book


# ── engine semantics ──
class Always(TrendFollow):
    """buys everything every day with a 10% stop — to test the engine's limits and policies"""
    name = "always"; label = "Always"
    def prepare(self, ind): self.ind = ind
    def exits(self, date, book): return []
    def explain(self, date, ticker): return {"state": "NONE", "checks": {}, "note": ""}
    def entries(self, date, univ, book):
        out = []
        for iss, meta in univ.items():
            if iss in book.positions: continue
            px = float(self.ind.at(self.ind.close, date, meta["ticker"]))
            if px == px:
                out.append(EntrySignal(iss, meta["ticker"], px, stop_price=px * 0.9, rank=-px))
        return sorted(out, key=lambda s: -s.rank)


class FakeUniverse:
    def __init__(self, tickers): self.t = tickers
    def on(self, date): return {t: {"ticker": t, "sector": "S"} for t in self.t}


def make_engine(bars, strategy, cfg):
    eng = Engine.__new__(Engine)
    eng.strategy, eng.conn, eng.cfg, eng.bars = strategy, None, cfg, bars
    eng.ind = Indicators(bars); eng.dates = eng.ind.dates; eng.index = eng.ind.index
    eng.universe = FakeUniverse(eng.ind.tickers)
    strategy.prepare(eng.ind)
    return eng


def test_max_positions_skip_policy_never_sells_to_make_room():
    bars = synthetic_bars(tickers=tuple(f"T{i}" for i in range(8)))
    eng = make_engine(bars, Always(), EngineConfig(max_positions=3, policy=MaxPositionsPolicy("skip")))
    book = Book("always", 100000)
    r = eng.step(book, eng.dates[5], fill="same")
    assert len(r["entries"]) == 3 and len(r["skipped"]) == 5 and len(book.positions) == 3
    r2 = eng.step(book, eng.dates[6], fill="same")
    assert r2["entries"] == [] and r2["replaced"] == [] and len(book.positions) == 3


def test_max_positions_replace_policy_replaces_only_losing_aged_positions():
    bars = synthetic_bars(tickers=tuple(f"T{i}" for i in range(6)))
    eng = make_engine(bars, Always(), EngineConfig(max_positions=2, policy=MaxPositionsPolicy("replace", min_hold=3)))
    book = Book("always", 100000)
    eng.step(book, eng.dates[5], fill="same")
    assert len(book.positions) == 2
    # force one position into a loss and age it
    loser = next(iter(book.positions.values()))
    loser.entry_price = loser.last_price * 2
    for k in range(6, 10):
        r = eng.step(book, eng.dates[k], fill="same")
    assert any("replaced by" in t.reason for t in book.trades if t.side == "sell")
    assert len(book.positions) == 2
    # winners are never replaced
    for p in book.positions.values():
        p.entry_price = p.last_price * 0.5
    r = eng.step(book, eng.dates[10], fill="same")
    assert r["replaced"] == []


def test_budget_sizing_cash_reserve_and_ledger_reconciliation():
    bars = synthetic_bars(tickers=tuple(f"T{i}" for i in range(5)))
    cfg = EngineConfig(max_positions=4, cash_reserve=0.10)
    eng = make_engine(bars, Always(), cfg)
    book = eng.backtest(eng.dates[5], eng.dates[40], fill="next", capital=50000)
    buys = [t for t in book.trades if t.side == "buy"]
    assert len(book.positions) == 4 and buys
    # every position is an equal share of the investable equity: 90% / 4 = 22.5% of equity at the time
    assert abs(buys[0].value - 50000 * 0.9 / 4) < 1.0
    # the reserve is never spent: after the first session's fills, 10% of equity is still cash (commissions aside)
    first = book.equity[0]
    assert first["cash"] >= first["value"] * 0.10 - 5.0 and first["cash"] <= first["value"] * 0.10 + 1.0
    cash = 50000 - sum(t.value + t.commission for t in book.trades if t.side == "buy") \
           + sum(t.value - t.commission for t in book.trades if t.side == "sell")
    assert abs(cash - book.cash) < 1e-6


def test_position_is_scaled_to_what_fits_above_the_reserve_or_refused():
    b = Book("x", 10000)
    budget = b.position_budget(2, 0.10)                  # 4500
    assert abs(budget - 4500) < 1e-9 and abs(b.reserve(0.10) - 1000) < 1e-9
    assert b.open("A", "A", 10.0, "d", budget, 1.0, 9.0, 0.1, None, "r", min_cash=b.reserve(0.10)) is not None
    assert b.open("B", "B", 10.0, "d", budget, 1.0, 9.0, 0.1, None, "r", min_cash=b.reserve(0.10)) is not None
    assert b.cash >= 1000 - 2.0                          # reserve intact (commissions aside)
    # a third position: only ~0 left above the reserve -> refused
    assert b.open("C", "C", 10.0, "d", budget, 1.0, 9.0, 0.1, None, "r", min_cash=b.reserve(0.10)) is None
    # half a budget fits -> scaled down rather than refused
    c = Book("y", 10000)
    c.cash = 3600
    assert c.open("A", "A", 10.0, "d", 4500, 1.0, 9.0, 0.1, None, "r", min_cash=1000).value < 4500


def test_analytics_performance_and_trade_stats():
    bars = synthetic_bars(tickers=tuple(f"T{i}" for i in range(5)))
    eng = make_engine(bars, Always(), EngineConfig(max_positions=3))
    book = eng.backtest(eng.dates[5], eng.dates[100], fill="same", capital=50000)
    bench = pd.DataFrame({"fund": eng.ind.ret.mean(axis=1).fillna(0.0), "equal": eng.ind.ret.mean(axis=1).fillna(0.0)})
    p = analytics.performance(book, bench)
    assert p["sessions"] == 96 and abs(p["return"] - (book.total_value() / 50000 - 1)) < 1e-9
    assert -1 < p["max_drawdown"] <= 0 and "bench_return" in p and len(p["windows"]) >= 1 and p["monthly"]
    assert abs(p["excess"] - (p["return"] - p["bench_return"])) < 1e-9
    t = analytics.trade_stats(book, {"T0": "S"})
    assert t["closed"] == sum(1 for x in book.trades if x.side == "sell") and 0 <= t["win_rate"] <= 1
    rules = {"always": eng.ind.close.notna()}
    q = analytics.signal_quality(eng.ind, rules, horizons=(5,))
    assert q[0]["n"] > 0 and abs(q[0]["horizons"][5]["excess"]) < 1e-9    # everything selected -> zero excess


def test_book_persistence_roundtrip(tmp_path):
    conn = get_db(tmp_path / "b.db")
    b = Book("x", 1000)
    b.open("AAA", "AAA", 10.0, "2026-01-02", 500, 1.0, 9.0, 0.1, None, "r", {"k": 1})
    b.last_date = "2026-01-02"; b.save(conn)
    c = Book.load(conn, "x")
    assert c.cash == b.cash and c.positions["AAA"].meta == {"k": 1} and c.last_date == "2026-01-02"
    Book.delete(conn, "x")
    assert Book.load(conn, "x").trades == []


# ── on the real databases ──
@needs_db
def test_split_adjustment_removes_fake_crashes():
    bars = md.bars_from_holdings(ro_conn())
    c = bars.pivot(index="date", columns="ticker", values="close")
    assert c.pct_change(fill_method=None).min().min() > -0.6


@needs_db
def test_strategy_runs_on_real_data_with_one_position_per_issuer():
    eng = Engine(make_strategy(), ro_conn(), EngineConfig(max_positions=20))
    book = eng.backtest(eng.dates[-60], eng.dates[-1], fill="next", capital=100000)
    assert len(book.positions) <= 20
    assert len({md.issuer_key(p.ticker) for p in book.positions.values()}) == len(book.positions)
    for t in book.trades:
        if t.side == "buy":
            assert t.value <= 100000 * 1.5 * 0.9 / 20      # an equal share of equity, never more


@needs_db
def test_live_session_is_idempotent(tmp_path):
    eng = Engine(make_strategy(), ro_conn(), EngineConfig(max_positions=5))
    bc = get_db(tmp_path / "books.db")
    book = Book.load(bc, "trend_follow", 100000)
    today = eng.dates[-1]
    r1 = eng.run_session(book, today); book.save(bc)
    r2 = eng.run_session(Book.load(bc, "trend_follow"), today)
    assert not r1.get("skipped_session") and r2.get("skipped_session")
    assert len(Book.load(bc, "trend_follow").trades) == len(r1["entries"]) + len(r1["exits"])


def test_book_load_ignores_fields_from_older_schemas(tmp_path):
    import json
    conn = get_db(tmp_path / "b.db")
    b = Book("x", 1000); b.save(conn)
    pos = {"AAA": {"issuer": "AAA", "ticker": "AAA", "shares": 1.0, "entry_price": 10.0, "entry_date": "2026-01-02",
                   "stop_price": 9.0, "etfs": ["OLD"]}}
    conn.execute("UPDATE books SET positions_json=? WHERE strategy='x'", (json.dumps(pos),))
    assert Book.load(conn, "x").positions["AAA"].ticker == "AAA"


# ── rising stars ──
def test_rising_stars_collect_skipped_signals_and_get_priority():
    bars = synthetic_bars(tickers=tuple(f"T{i}" for i in range(8)))
    cfg = EngineConfig(max_positions=3, policy=MaxPositionsPolicy("skip"), rising_max_age=3)
    eng = make_engine(bars, Always(), cfg)
    book = Book("always", 100000)
    r = eng.step(book, eng.dates[5], fill="same")
    assert len(r["skipped"]) == 5 and len(book.rising) == 5
    assert all(s["why"] == "book full" and s["seen"] == 1 for s in book.rising.values())
    eng.step(book, eng.dates[6], fill="same")
    assert all(s["seen"] == 2 for s in book.rising.values())          # still signalling -> refreshed
    # free a slot: the most persistent waiting star must be taken first, and leave the watchlist
    victim = next(iter(book.positions))
    book.close(victim, book.positions[victim].last_price, eng.dates[6], 1.0, "test", 1)
    star = max(book.rising.values(), key=lambda s: s["seen"])["issuer"]
    for s in book.rising.values():
        s["seen"] = 1
    book.rising[star]["seen"] = 9
    r = eng.step(book, eng.dates[7], fill="same")
    assert [e.issuer for e in r["entries"]] == [star] and star not in book.rising


def test_rising_stars_expire_and_capture_capital_exhaustion():
    bars = synthetic_bars(tickers=("T0", "T1", "T2"))
    cfg = EngineConfig(max_positions=10, cash_reserve=0.0, rising_max_age=2)
    eng = make_engine(bars, Always(), cfg)
    book = Book("always", 1500)
    px = float(eng.ind.close["T0"].iloc[5])
    book.open("T0", "T0", px, eng.dates[4], 1300, 1.0, px * 0.5, None, None, "big")     # most of the cash is already invested
    r = eng.step(book, eng.dates[5], fill="same")     # budget 10% of ~1500 = 150: T1 fits, T2 does not (< half a budget left)
    assert len(book.positions) == 2 and len(r["skipped"]) == 1
    assert all(s["why"] == "capital exhausted" for s in book.rising.values())
    # stop signalling: Always() keeps signalling, so emulate by freezing last_seen and stepping past max_age
    for s in book.rising.values():
        s["last_seen"] = eng.dates[0]
    book.prune_rising(eng.dates[5], eng.index, max_age=2)
    assert book.rising == {}


def test_rising_stars_persist(tmp_path):
    conn = get_db(tmp_path / "b.db")
    b = Book("x", 1000)
    b.note_rising("AAA", "AAA", "2026-01-02", 10.0, 0.5, "sig", "book full")
    b.save(conn)
    c = Book.load(conn, "x")
    assert c.rising["AAA"]["seen"] == 1 and c.rising_list()[0]["ticker"] == "AAA"


# ── database: weekly closes = last snapshot of the week ──
def test_weekly_close_is_last_snapshot_of_week(tmp_path):
    import datetime as dt
    from src.database import get_db, store_snapshot, rebuild_weekly_closes
    conn = get_db(tmp_path / "x.db")
    row = lambda px: [{"ticker": "AAA", "name": "A", "sector": "S", "weight_pct": 1.0, "market_value": 1, "shares": 1,
                       "price": px, "currency": "USD", "exchange": "X", "location": "US"}]
    store_snapshot(conn, dt.date(2026, 1, 5), row(10))     # Monday
    store_snapshot(conn, dt.date(2026, 1, 8), row(12))     # Thursday (Friday missing)
    wk = [tuple(r) for r in conn.execute("SELECT week_ending, price FROM weekly_closes")]
    assert wk == [("2026-01-09", 12.0)]
    store_snapshot(conn, dt.date(2026, 1, 6), row(11))     # a late-arriving Tuesday must not overwrite Thursday
    assert conn.execute("SELECT price FROM weekly_closes").fetchone()[0] == 12.0
    conn.execute("DELETE FROM weekly_closes")
    assert rebuild_weekly_closes(conn) == 1
    assert tuple(conn.execute("SELECT week_ending, price FROM weekly_closes").fetchone()) == ("2026-01-09", 12.0)
