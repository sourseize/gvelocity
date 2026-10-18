"""
analytics — performance, trade and signal-quality statistics (shared by `velocity backtest`,
`velocity signals`, and the web pages).

* benchmark: the fund itself — its weight-averaged daily return from the stored holdings
  (weights of the previous session applied to today's price changes), and the equal-weight
  universe.  The book is always judged against the fund.
* performance(): total / benchmark / excess return, drawdowns, Sharpe, exposure, rolling
  quarterly windows (did the book beat the fund in each?), monthly returns.
* trade_stats(): win rate, payoff, expectancy, hold times, P&L by exit reason / sector / ticker.
* signal_quality(): forward excess return of every ticker-day a rule fires — the cleanest
  measure of whether a rule *selects* anything, independent of position management.
"""
from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .book import Book
from .indicators import Indicators

WINDOW = 63          # rolling window length in sessions (~ a quarter)
STEP = 21            # rolling window step (~ a month)


# ── benchmark ───────────────────────────────────────────────────────────────
def benchmark_returns(conn: sqlite3.Connection, ind: Indicators) -> pd.DataFrame:
    """DataFrame(date) with columns `fund` (weight-averaged) and `equal` (equal-weight) daily returns."""
    w = pd.read_sql("SELECT snapshot_date AS date, ticker, weight_pct FROM holdings WHERE weight_pct IS NOT NULL", conn)
    ret = ind.ret
    if w.empty:
        return pd.DataFrame({"fund": ret.mean(axis=1).fillna(0.0), "equal": ret.mean(axis=1).fillna(0.0)})
    w = w.drop_duplicates(["date", "ticker"]).pivot(index="date", columns="ticker", values="weight_pct").sort_index()
    w = w.reindex(index=ret.index, columns=ret.columns)
    wprev = w.shift()
    valid = wprev.where(ret.notna())
    fund = (ret * wprev).sum(axis=1) / valid.sum(axis=1).replace(0, np.nan)
    return pd.DataFrame({"fund": fund.fillna(0.0), "equal": ret.mean(axis=1).fillna(0.0)})


def sector_map(conn: sqlite3.Connection) -> Dict[str, str]:
    """ticker -> sector (latest known)."""
    out: Dict[str, str] = {}
    for t, s in conn.execute("SELECT ticker, sector FROM holdings ORDER BY snapshot_date"):
        out[t] = s or ""
    return out


# ── equity curve & performance ──────────────────────────────────────────────
def equity_series(book: Book) -> pd.Series:
    if not book.equity:
        return pd.Series(dtype=float)
    return pd.Series([e["value"] for e in book.equity], index=[e["date"] for e in book.equity], dtype=float)


def exposure_series(book: Book) -> pd.Series:
    if not book.equity:
        return pd.Series(dtype=float)
    return pd.Series([(e["value"] - e["cash"]) / e["value"] if e["value"] else 0.0 for e in book.equity],
                     index=[e["date"] for e in book.equity], dtype=float)


def drawdown(curve: pd.Series) -> pd.Series:
    return curve / curve.cummax() - 1


def performance(book: Book, bench: Optional[pd.DataFrame]) -> dict:
    """Everything the performance pages / backtest tables show.  `bench` is benchmark_returns()."""
    eq = equity_series(book)
    if eq.empty:
        return {"sessions": 0}
    start_value = book.initial_capital
    curve = eq / start_value
    r = eq.pct_change().fillna(0.0)
    r.iloc[0] = eq.iloc[0] / start_value - 1
    out = {"sessions": len(eq), "start": eq.index[0], "end": eq.index[-1],
           "return": float(curve.iloc[-1] - 1), "max_drawdown": float(drawdown(curve).min()),
           "volatility": float(r.std() * np.sqrt(252)) if len(r) > 1 else 0.0,
           "sharpe": float(r.mean() / r.std() * np.sqrt(252)) if len(r) > 1 and r.std() else 0.0,
           "avg_exposure": float(exposure_series(book).mean()),
           "curve": curve, "drawdown": drawdown(curve), "exposure": exposure_series(book)}
    if bench is not None:
        b = bench["fund"].reindex(eq.index).fillna(0.0)
        b.iloc[0] = 0.0                       # the book starts flat on its first session
        bcurve = (1 + b).cumprod()
        out.update({"bench_return": float(bcurve.iloc[-1] - 1), "bench_curve": bcurve,
                    "bench_max_drawdown": float(drawdown(bcurve).min()),
                    "excess": float(curve.iloc[-1] - bcurve.iloc[-1]),
                    "bench_sharpe": float(b.mean() / b.std() * np.sqrt(252)) if len(b) > 1 and b.std() else 0.0})
        e = bench["equal"].reindex(eq.index).fillna(0.0); e.iloc[0] = 0.0
        out["equal_return"] = float((1 + e).prod() - 1)
        out["windows"] = rolling_windows(curve, bcurve)
        out["monthly"] = monthly_table(curve, bcurve)
        corr = r.corr(b) if len(r) > 2 else float("nan")
        out["beta"] = float(r.cov(b) / b.var()) if len(r) > 2 and b.var() else 0.0
        out["correlation"] = float(corr) if corr == corr else 0.0
    return out


def rolling_windows(curve: pd.Series, bcurve: pd.Series, window: int = WINDOW, step: int = STEP) -> List[dict]:
    """Rolling ~quarterly windows: book vs benchmark return in each."""
    out = []
    n = len(curve)
    for i in range(0, n - window, step):
        j = i + window
        out.append({"start": curve.index[i], "end": curve.index[j],
                    "book": float(curve.iloc[j] / curve.iloc[i] - 1),
                    "bench": float(bcurve.iloc[j] / bcurve.iloc[i] - 1)})
    for w in out:
        w["excess"] = w["book"] - w["bench"]
        w["beat"] = w["book"] > w["bench"]
    return out


def monthly_table(curve: pd.Series, bcurve: pd.Series) -> List[dict]:
    """Calendar-month returns of book and benchmark."""
    out = []
    months = sorted({d[:7] for d in curve.index})
    prev_b, prev_f = 1.0, 1.0
    for m in months:
        idx = [d for d in curve.index if d.startswith(m)]
        b, f = float(curve[idx[-1]]), float(bcurve[idx[-1]])
        out.append({"month": m, "book": b / prev_b - 1, "bench": f / prev_f - 1})
        prev_b, prev_f = b, f
    return out


# ── trades ──────────────────────────────────────────────────────────────────
def _reason_key(reason: str) -> str:
    """'trailing stop 12% (stop 101.20)' -> 'trailing stop' ; 'close below 20d low' -> 'close below 20d low'."""
    r = reason or ""
    for key in ("trailing stop", "stop ", "target", "time stop", "dropped from the fund", "replaced by",
                "closes below SMA", "close below"):
        if key in r:
            return {"stop ": "initial stop", "replaced by": "replaced", "closes below SMA": "closes below SMA",
                    "close below": r.split(" (")[0]}.get(key, key)
    return r[:40] or "—"


def trade_stats(book: Book, sectors: Optional[Dict[str, str]] = None) -> dict:
    trades = [vars(t) for t in book.trades]
    sells = [t for t in trades if t["side"] == "sell"]
    out = {"trades": len(trades), "closed": len(sells), "open": len(book.positions)}
    if not sells:
        return {**out, "win_rate": 0.0, "payoff": 0.0, "expectancy": 0.0, "median_hold": 0, "avg_win": 0.0, "avg_loss": 0.0,
                "by_reason": [], "by_sector": [], "top": [], "bottom": [], "realized": book.realized(),
                "unrealized": book.unrealized(), "profit_factor": 0.0, "hold_hist": []}
    pnl = np.array([t["pnl"] for t in sells]); wins = pnl[pnl > 0]; losses = pnl[pnl <= 0]
    out.update({
        "win_rate": float(len(wins) / len(pnl)),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "payoff": float(wins.mean() / abs(losses.mean())) if len(wins) and len(losses) and losses.mean() else 0.0,
        "profit_factor": float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() else float("inf") if len(wins) else 0.0,
        "expectancy": float(pnl.mean()),
        "median_hold": int(np.median([t["hold_days"] for t in sells])),
        "realized": book.realized(), "unrealized": book.unrealized(),
    })
    by = {}
    for t in sells:
        k = _reason_key(t["reason"])
        d = by.setdefault(k, {"reason": k, "n": 0, "pnl": 0.0, "wins": 0})
        d["n"] += 1; d["pnl"] += t["pnl"]; d["wins"] += t["pnl"] > 0
    out["by_reason"] = sorted(({**d, "avg": d["pnl"] / d["n"]} for d in by.values()), key=lambda d: -d["n"])
    sectors = sectors or {}
    bs = {}
    for t in sells:
        k = sectors.get(t["ticker"], "") or "—"
        d = bs.setdefault(k, {"sector": k, "n": 0, "pnl": 0.0})
        d["n"] += 1; d["pnl"] += t["pnl"]
    for p in book.positions.values():
        k = sectors.get(p.ticker, "") or "—"
        d = bs.setdefault(k, {"sector": k, "n": 0, "pnl": 0.0})
        d["open"] = d.get("open", 0) + 1; d["unrealized"] = d.get("unrealized", 0.0) + (p.last_price - p.entry_price) * p.shares
    out["by_sector"] = sorted(bs.values(), key=lambda d: -(d["pnl"] + d.get("unrealized", 0.0)))
    bt = {}
    for t in sells:
        bt[t["ticker"]] = bt.get(t["ticker"], 0.0) + t["pnl"]
    ranked = sorted(bt.items(), key=lambda kv: -kv[1])
    out["top"] = ranked[:8]; out["bottom"] = ranked[-8:][::-1]
    holds = [t["hold_days"] for t in sells]
    edges = [0, 5, 10, 20, 40, 80, 10 ** 6]
    out["hold_hist"] = [{"label": f"{a}–{b - 1}d" if b < 10 ** 6 else f"{a}d+", "n": sum(a <= h < b for h in holds)}
                        for a, b in zip(edges[:-1], edges[1:])]
    return out


# ── signal quality ──────────────────────────────────────────────────────────
def signal_quality(ind: Indicators, rules: Dict[str, pd.DataFrame], horizons=(5, 10, 20, 40, 60)) -> List[dict]:
    """For every rule (a wide boolean frame): how many ticker-days it fires on, and the mean
    forward excess return (vs the equal-weight universe) and hit rate at each horizon."""
    c = ind.close
    fwd = {}
    for n in horizons:
        f = c.shift(-n) / c - 1
        fwd[n] = f.sub(f.mean(axis=1), axis=0)
    out = []
    for name, mask in rules.items():
        mask = mask.fillna(False).astype(bool) & c.notna()
        row = {"rule": name, "n": int(mask.values.sum()), "horizons": {}}
        for n, f in fwd.items():
            v = f.where(mask).stack()
            row["horizons"][n] = {"excess": float(v.mean()) if len(v) else float("nan"),
                                  "hit": float((v > 0).mean()) if len(v) else float("nan"), "n": int(len(v))}
        out.append(row)
    return out


def strategy_rules(strategy) -> Dict[str, pd.DataFrame]:
    """The strategy's component rules plus reference signals, for signal_quality()."""
    ind = strategy.ind
    p = strategy.p
    rs = strategy.rs
    mom6 = ind.roc(120).rank(axis=1, pct=True)
    return {
        "ENTRY (all checks)": strategy.entry_ok,
        f"uptrend (close > SMA{p['sma_len']} rising)": strategy.trend,
        f"{p['breakout_len']}d breakout": strategy.brk,
        f"RS >= {p['rs_min_pct']:.0%}": strategy.rs_ok,
        "RS >= 80%": rs >= 0.8,
        "not a gap day": strategy.gap_ok,
        "ref: 6-month momentum top decile": mom6 >= 0.9,
        "ref: close within 3% of 250d high": ind.close / ind.close.rolling(250, min_periods=120).max() >= 0.97,
        f"EXIT: close < {p['exit_low_len']}d low": strategy.don_low,
        f"EXIT: {p['sma_break_days']} closes < SMA{p['sma_len']}": strategy.trend_break,
    }
