"""
backtest service — replay the strategy over windows of the stored history, judged against the fund.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import pandas as pd

from .. import analytics
from ..book import Book
from ..engine import Engine, EngineConfig
from ..exceptions import VelocityError
from ..market_data import bars_from_holdings
from ..strategy import make_strategy

Window = Tuple[str, str, str]          # (label, start, end)


def month_windows(dates: List[str], months: List[int], end: str | None = None) -> List[Window]:
    end = end or dates[-1]
    out = []
    for m in months:
        start_dt = (pd.Timestamp(end) - pd.DateOffset(months=m)).strftime("%Y-%m-%d")
        start = next((d for d in dates if d >= start_dt), dates[0])
        out.append((f"{m}m", start, end))
    return out


def prepare(conn: sqlite3.Connection, params: dict, cfg: EngineConfig) -> Engine:
    return Engine(make_strategy(**params), conn, cfg, bars=bars_from_holdings(conn))


def resolve_windows(dates: List[str], months: List[int] | None, start: str | None, end: str | None) -> List[Window]:
    if start or end:
        s, e = start or dates[0], end or dates[-1]
        if e < dates[0] or s > dates[-1]:
            raise VelocityError(f"window {s} → {e} is outside the stored history {dates[0]} → {dates[-1]}")
        return [("custom", s, e)]
    return month_windows(dates, months or [3, 6, 9])


@dataclass
class WindowResult:
    label: str
    start: str
    end: str
    n_days: int
    warm: int                       # sessions of the window inside the strategy's warm-up
    book: Book
    perf: dict
    trades: dict


@dataclass
class BacktestRun:
    engine: Engine
    windows: List[Window]
    results: List[WindowResult] = field(default_factory=list)


def run(conn: sqlite3.Connection, engine: Engine, windows: List[Window], fill: str, capital: float) -> BacktestRun:
    out = BacktestRun(engine, windows)
    bench = analytics.benchmark_returns(conn, engine.ind)
    sectors = analytics.sector_map(conn)
    dates = engine.dates
    for label, start, end in windows:
        book = engine.backtest(start, end, fill=fill, capital=capital)
        n_days = sum(1 for d in dates if start <= d <= end)
        warm = max(0, engine.strategy.warmup() - engine.index[start])
        out.results.append(WindowResult(label, start, end, n_days, warm, book,
                                        analytics.performance(book, bench), analytics.trade_stats(book, sectors)))
    return out


def summary_rows(res: WindowResult) -> Dict[str, object]:
    """One column of the plain-text comparison table."""
    p, t = res.perf, res.trades
    pct = lambda v: f"{v:+.1%}"
    wins = p.get("windows", [])
    return {
        "book return": pct(p["return"]), "fund return": pct(p.get("bench_return", 0.0)),
        "excess vs fund": pct(p.get("excess", 0.0)), "equal-weight return": pct(p.get("equal_return", 0.0)),
        "max drawdown": pct(p["max_drawdown"]), "fund max drawdown": pct(p.get("bench_max_drawdown", 0.0)),
        "sharpe": f"{p['sharpe']:.2f}", "fund sharpe": f"{p.get('bench_sharpe', 0.0):.2f}",
        "avg exposure": f"{p['avg_exposure']:.0%}", "beta to fund": f"{p.get('beta', 0.0):.2f}",
        "quarters beating fund": f"{sum(w['beat'] for w in wins)}/{len(wins)}" if wins else "n/a",
        "trades": t["trades"], "closed": t["closed"], "open at end": t["open"],
        "win rate": f"{t['win_rate']:.0%}", "payoff": f"{t['payoff']:.2f}", "profit factor": f"{t['profit_factor']:.2f}",
        "expectancy $/trade": f"{t['expectancy']:.0f}", "median hold (d)": t["median_hold"],
        "realized $": f"{t['realized']:.0f}", "unrealized $": f"{t['unrealized']:.0f}",
        "note": f"first {res.warm} sessions inside warm-up" if res.warm else "",
    }
