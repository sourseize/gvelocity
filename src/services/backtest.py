"""
backtest service — replay strategies over windows of the stored history.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from ..book import Book
from ..engine import Engine, EngineConfig
from ..exceptions import VelocityError
from ..market_data import load_market_bars
from ..strategies import get_strategy

Window = Tuple[str, str, str]          # (label, start, end)


def month_windows(dates: List[str], months: List[int], end: str | None = None) -> List[Window]:
    end = end or dates[-1]
    out = []
    for m in months:
        start_dt = (pd.Timestamp(end) - pd.DateOffset(months=m)).strftime("%Y-%m-%d")
        start = next((d for d in dates if d >= start_dt), dates[0])
        out.append((f"{m}m", start, end))
    return out


def summarize(label: str, book: Book, n_days: int) -> dict:
    tv = pd.DataFrame([vars(t) for t in book.trades])
    sells = tv[tv.side == "sell"] if len(tv) else pd.DataFrame(columns=["pnl", "hold_days"])
    wins, losses = sells[sells.pnl > 0], sells[sells.pnl <= 0]
    eq = pd.Series([e["value"] for e in book.equity]) if book.equity else pd.Series([book.initial_capital])
    dd = (eq / eq.cummax() - 1).min()
    daily = eq.pct_change().dropna()
    peak = max((e["value"] - e["cash"]) for e in book.equity) if book.equity else 0.0
    total = book.realized() + book.unrealized()
    return {
        "strategy": label,
        "trades": len(tv), "trades/day": round(len(tv) / max(n_days, 1), 2),
        "closed": len(sells), "win rate": f"{(len(wins) / len(sells)):.0%}" if len(sells) else "n/a",
        "med hold (d)": int(sells.hold_days.median()) if len(sells) else 0,
        "avg win $": round(wins.pnl.mean()) if len(wins) else 0,
        "avg loss $": round(losses.pnl.mean()) if len(losses) else 0,
        "payoff": round(wins.pnl.mean() / abs(losses.pnl.mean()), 2) if len(wins) and len(losses) and losses.pnl.mean() else "n/a",
        "expectancy $/trade": round(sells.pnl.mean(), 1) if len(sells) else 0,
        "realized $": round(book.realized()), "unrealized $": round(book.unrealized()), "total P&L $": round(total),
        "open": len(book.positions), "peak $ deployed": round(peak),
        "P&L / peak deployed": f"{total / peak:+.1%}" if peak else "n/a",
        "max drawdown": f"{dd:.2%}", "sharpe": round(daily.mean() / daily.std() * np.sqrt(252), 2) if daily.std() else "n/a",
    }


@dataclass
class BacktestRun:
    engines: Dict[str, Engine]
    dates: List[str]
    windows: List[Window]
    rows: List[dict] = field(default_factory=list)                 # one summary row per (window, strategy)
    books: Dict[Tuple[str, str], Book] = field(default_factory=dict)


def prepare(conn: sqlite3.Connection, names: List[str], params: dict, cfg: EngineConfig) -> Tuple[Dict[str, Engine], List[str]]:
    bars = load_market_bars(conn)
    engines = {n: Engine(get_strategy(n, **params), conn, cfg, bars=bars) for n in names}
    return engines, next(iter(engines.values())).dates


def resolve_windows(dates: List[str], months: List[int] | None, start: str | None, end: str | None) -> List[Window]:
    if start or end:
        s, e = start or dates[0], end or dates[-1]
        if e < dates[0] or s > dates[-1]:
            raise VelocityError(f"window {s} → {e} is outside the stored history {dates[0]} → {dates[-1]}")
        return [("custom", s, e)]
    return month_windows(dates, months or [3, 6, 9])


def run(engines: Dict[str, Engine], dates: List[str], windows: List[Window], fill: str, capital: float) -> BacktestRun:
    out = BacktestRun(engines, dates, windows)
    for wlabel, start, end in windows:
        n_days = sum(1 for d in dates if start <= d <= end)
        first_idx = dates.index(start)
        for n, eng in engines.items():
            book = eng.backtest(start, end, fill=fill, capital=capital)
            out.books[(wlabel, n)] = book
            warm = max(0, eng.strategy.warmup() - first_idx)
            out.rows.append({"window": f"{wlabel} ({start} → {end}, {n_days}d)", **summarize(eng.strategy.label, book, n_days),
                             "note": f"first {warm} sessions inside warm-up" if warm else ""})
    return out
