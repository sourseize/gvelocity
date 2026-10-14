"""Tests for the strategy framework.  Run:  python -m pytest tests -q"""
import sqlite3, sys
from pathlib import Path
import numpy as np, pandas as pd, pytest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import market_data as md
from src.indicators import Indicators
from src.strategies import get_strategy, available, parse_params, REGISTRY
from src.strategies.base import Strategy, EntrySignal
from src.engine import Engine, EngineConfig, MaxPositionsPolicy
from src.book import Book
from src.config import ETF_CONFIG
from src.database import default_db_path

DATA = Path(__file__).resolve().parent.parent / "data"
ETFS = [e for e in sorted(ETF_CONFIG) if (DATA / f"{e}.db").exists()]
needs_db = pytest.mark.skipif(not ETFS, reason="no ETF databases present")


def conns():
    return {e: sqlite3.connect(DATA / f"{e}.db") for e in ETFS}


def synthetic_bars(n=160, seed=0, tickers=("AAA", "BBB", "CCC")):
    rng = np.random.default_rng(seed)
    dates = [str(d.date()) for d in pd.bdate_range("2026-01-01", periods=n)]
    rows = []
    for t in tickers:
        px = 100 * np.cumprod(1 + rng.normal(0.002, 0.01, n))
        for d, p in zip(dates, px):
            rows.append((d, t, p, p * 1.01, p * 0.99, 1e6 * (1 + rng.random())))
    return pd.DataFrame(rows, columns=["date", "ticker", "close", "high", "low", "volume"])


# ── factory ──
def test_factory_and_params():
    assert set(available()) == {"trend_follow", "range_breakout", "double_bottom"}
    s = get_strategy("trend_follow", breakout_len=40)
    assert s.p["breakout_len"] == 40 and isinstance(s, Strategy)
    with pytest.raises(ValueError):
        get_strategy("nope")
    with pytest.raises(ValueError):
        get_strategy("trend_follow", bogus=1)
    assert parse_params(["a=1", "b=0.5", "c=true", "d=x"]) == {"a": 1, "b": 0.5, "c": True, "d": "x"}


def test_every_strategy_implements_interface():
    for name, cls in REGISTRY.items():
        for m in ("prepare", "entries", "exits", "explain", "manage"):
            assert callable(getattr(cls, m)), f"{name} lacks {m}"
        assert cls.label and cls.description and isinstance(cls.defaults, dict)


def test_issuer_dedup_and_symbol_filter():
    assert md.issuer_key("GOOGL") == md.issuer_key("GOOG")
    assert not md.is_tradeable_symbol("-") and md.is_tradeable_symbol("BRK.B")


# ── indicators ──
def test_indicators_degrade_without_volume():
    ind = Indicators(synthetic_bars())
    assert ind.has_volume and ind.rvol() is not None and ind.atr_pct() is not None
    ind2 = Indicators(synthetic_bars().drop(columns=["volume", "high", "low"]))
    assert not ind2.has_volume and ind2.rvol() is None and ind2.obv_up() is None
    assert ind2.vol_trail().stack().between(0.08, 0.25).all()


def test_swing_points_and_double_bottom_detection():
    # hand-made W pattern: down to 90, up to 100, down to 91, then break above 100
    seq = list(np.linspace(110, 90, 15)) + list(np.linspace(90, 100, 15)) + list(np.linspace(100, 91, 15)) \
          + list(np.linspace(91, 99, 12)) + [101, 103, 104, 105, 106, 107, 108, 109, 110, 111]
    dates = [str(d.date()) for d in pd.bdate_range("2026-01-01", periods=len(seq))]
    bars = pd.DataFrame({"date": dates, "ticker": "WWW", "close": seq})
    ind = Indicators(bars)
    s = get_strategy("double_bottom")
    s.prepare(ind, None)
    assert s.entry["WWW"].any(), "double bottom should complete when the neckline (100) breaks"
    d = s.entry["WWW"].idxmax()
    ex = s.explain(d, "WWW")
    assert ex["state"] == "ENTRY" and 99 < s.neck.at[d, "WWW"] <= 101


# ── engine semantics ──
class Always(Strategy):
    """buys everything every day with a 10% stop — to test the engine's limits and policies"""
    name = "always"; label = "Always"; description = "test"; defaults = {}
    def prepare(self, ind, universe): self.ind = ind
    def entries(self, date, univ, book):
        out = []
        for iss, meta in univ.items():
            if iss in book.positions: continue
            px = float(self.ind.at(self.ind.close, date, meta["ticker"]))
            if px == px:
                out.append(EntrySignal(iss, meta["ticker"], px, stop_price=px * 0.9, rank=-px, etfs=meta["etfs"]))
        return sorted(out, key=lambda s: -s.rank)


class FakeUniverse:
    def __init__(self, tickers): self.t = tickers
    def on(self, date): return {t: {"ticker": t, "etfs": ["X"], "recent_add": False} for t in self.t}


def make_engine(bars, strategy, cfg):
    eng = Engine.__new__(Engine)
    eng.strategy, eng.conns, eng.cfg, eng.bars = strategy, {}, cfg, bars
    eng.ind = Indicators(bars); eng.dates = eng.ind.dates
    eng.universe = FakeUniverse(eng.ind.tickers)
    strategy.prepare(eng.ind, eng.universe)
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


def test_book_cap_sizing_and_ledger_reconciliation():
    bars = synthetic_bars(tickers=tuple(f"T{i}" for i in range(5)))
    cfg = EngineConfig(max_positions=10, max_amount=1000.0, risk_per_trade=0.0025)
    eng = make_engine(bars, Always(), cfg)
    book = eng.backtest(eng.dates[5], eng.dates[40], fill="next", capital=50000)
    for t in book.trades:
        if t.side == "buy":
            assert t.value <= 1000.0 + 1e-6
    cash = 50000 - sum(t.value + t.commission for t in book.trades if t.side == "buy") \
           + sum(t.value - t.commission for t in book.trades if t.side == "sell")
    assert abs(cash - book.cash) < 1e-6


def test_book_persistence_roundtrip(tmp_path):
    conn = sqlite3.connect(tmp_path / "b.db")
    b = Book("x", 1000)
    b.open("AAA", "AAA", 10.0, "2026-01-02", 500, 1.0, 9.0, 0.1, None, "r", ["MTUM"], {"k": 1})
    b.last_date = "2026-01-02"; b.save(conn)
    c = Book.load(conn, "x")
    assert c.cash == b.cash and c.positions["AAA"].meta == {"k": 1} and c.last_date == "2026-01-02"
    Book.delete(conn, "x")
    assert Book.load(conn, "x").trades == []


# ── on the real databases ──
@needs_db
def test_split_adjustment_removes_fake_crashes():
    bars = md.bars_from_holdings(conns())
    c = bars.pivot(index="date", columns="ticker", values="close")
    assert c.pct_change(fill_method=None).min().min() > -0.6


@needs_db
@pytest.mark.parametrize("name", available())
def test_strategy_runs_on_real_data_with_one_position_per_issuer(name):
    eng = Engine(get_strategy(name), conns(), EngineConfig(max_positions=20))
    book = eng.backtest(eng.dates[-60], eng.dates[-1], fill="next", capital=100000)
    assert len(book.positions) <= 20
    assert len({md.issuer_key(p.ticker) for p in book.positions.values()}) == len(book.positions)
    for t in book.trades:
        if t.side == "buy":
            assert t.value <= 1000.0 + 1e-6


@needs_db
def test_live_session_is_idempotent(tmp_path):
    cs = conns()
    eng = Engine(get_strategy("trend_follow"), cs, EngineConfig(max_positions=5))
    bc = sqlite3.connect(tmp_path / "books.db")
    book = Book.load(bc, "trend_follow", 100000)
    today = eng.dates[-1]
    r1 = eng.run_session(book, today); book.save(bc)
    r2 = eng.run_session(Book.load(bc, "trend_follow"), today)
    assert not r1.get("skipped_session") and r2.get("skipped_session")
    assert len(Book.load(bc, "trend_follow").trades) == len(r1["entries"]) + len(r1["exits"])


def test_alpaca_order_request_has_time_in_force():
    pytest.importorskip("alpaca.trading.requests")
    from src.alpaca_client import AlpacaClient
    cl = AlpacaClient("k", "s", paper=True)
    req = cl._MarketOrderRequest(symbol="AAPL", qty=1.5, side=cl._OrderSide.BUY,
                                 type=cl._OrderType.MARKET, time_in_force=cl._TimeInForce.DAY)
    assert req.time_in_force == cl._TimeInForce.DAY


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
    cfg = EngineConfig(max_positions=10, max_amount=1000.0, risk_per_trade=1.0, rising_max_age=2)  # always the full $1k
    eng = make_engine(bars, Always(), cfg)
    book = Book("always", 1500)                       # cash for one position only
    r = eng.step(book, eng.dates[5], fill="same")
    assert len(book.positions) == 1 and len(r["skipped"]) == 2
    assert all(s["why"] == "capital exhausted" for s in book.rising.values())
    # stop signalling: Always() keeps signalling, so emulate by freezing last_seen and stepping past max_age
    for s in book.rising.values():
        s["last_seen"] = eng.dates[0]
    book.prune_rising(eng.dates[5], eng.dates, max_age=2)
    assert book.rising == {}


def test_rising_stars_persist(tmp_path):
    conn = sqlite3.connect(tmp_path / "b.db")
    b = Book("x", 1000)
    b.note_rising("AAA", "AAA", "2026-01-02", 10.0, 0.5, "sig", "book full", ["MTUM"])
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
    conn.execute("DELETE FROM weekly_closes"); conn.commit()
    assert rebuild_weekly_closes(conn) == 1
    assert tuple(conn.execute("SELECT week_ending, price FROM weekly_closes").fetchone()) == ("2026-01-09", 12.0)
