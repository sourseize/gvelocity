#!/usr/bin/env python3
"""
backtest.py — replay one or all strategies over the stored history and compare them.

  python cli/backtest.py                                  # all strategies, 3/6/9-month windows
  python cli/backtest.py --strategy trend_follow --windows 3,6
  python cli/backtest.py --start 2026-02-13 --end 2026-08-13 --fill same
  python cli/backtest.py --max-positions 40 --max-positions-policy replace
  python cli/backtest.py --param breakout_len=40 --strategy trend_follow
  python cli/backtest.py --out analysis/compare.md        # markdown + csv + png

Every strategy is run with the same bars, capital, $ cap per issuer, position limit and
commission, and all books are marked to market at the window's last date.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.config import ETF_CONFIG, INITIAL_CAPITAL, MAX_PURCHASE_AMOUNT, MAX_POSITIONS, RISK_PER_TRADE, COMMISSION_PER_TRADE
from src.database import default_db_path
from src.market_data import load_market_bars
from src.strategies import get_strategy, available, parse_params
from src.engine import Engine, EngineConfig, MaxPositionsPolicy


def month_windows(dates, months, end=None):
    end = end or dates[-1]
    out = []
    for m in months:
        start_dt = (pd.Timestamp(end) - pd.DateOffset(months=m)).strftime("%Y-%m-%d")
        start = next((d for d in dates if d >= start_dt), dates[0])
        out.append((f"{m}m", start, end))
    return out


def summarize(label, book, n_days, warmup_note=""):
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", default="all", help="strategy name or 'all' (default)")
    ap.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--windows", default="3,6,9", help="trailing windows in months (default 3,6,9)")
    ap.add_argument("--start"); ap.add_argument("--end")
    ap.add_argument("--fill", choices=["same", "next"], default="next",
                    help="next = fill at the next session's close (default, realistic); same = signal-day close")
    ap.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    ap.add_argument("--max-positions", type=int, default=MAX_POSITIONS)
    ap.add_argument("--max-positions-policy", choices=["skip", "replace"], default="skip")
    ap.add_argument("--max-purchase-amount", type=float, default=MAX_PURCHASE_AMOUNT)
    ap.add_argument("--risk-per-trade", type=float, default=RISK_PER_TRADE)
    ap.add_argument("--rising-priority", choices=["on", "off"], default=None,
                    help="give waiting rising stars priority for free slots (default: engine default)")
    ap.add_argument("--out", default=None, help="write markdown (+csv of trades, +png chart) here")
    ap.add_argument("--trades", action="store_true", help="print the trade list of each run")
    args = ap.parse_args()

    conns = {e: sqlite3.connect(default_db_path(e)) for e in sorted(ETF_CONFIG) if default_db_path(e).exists()}
    bars = load_market_bars(conns)
    names = available() if args.strategy == "all" else [args.strategy]
    cfg = EngineConfig(max_positions=args.max_positions, max_amount=args.max_purchase_amount,
                       risk_per_trade=args.risk_per_trade, commission=COMMISSION_PER_TRADE,
                       policy=MaxPositionsPolicy(mode=args.max_positions_policy))
    if args.rising_priority:
        cfg.rising_priority = args.rising_priority == "on"
    engines = {n: Engine(get_strategy(n, **parse_params(args.param)), conns, cfg, bars=bars) for n in names}
    dates = next(iter(engines.values())).dates

    if args.start or args.end:
        windows = [("custom", args.start or dates[0], args.end or dates[-1])]
    else:
        windows = month_windows(dates, [int(x) for x in args.windows.split(",")])

    rows, curves, all_trades = [], {}, []
    for wlabel, start, end in windows:
        n_days = sum(1 for d in dates if start <= d <= end)
        first_idx = dates.index(start)
        for n, eng in engines.items():
            book = eng.backtest(start, end, fill=args.fill, capital=args.capital)
            warm = max(0, eng.strategy.warmup_days - first_idx)
            r = summarize(eng.strategy.label, book, n_days)
            r = {"window": f"{wlabel} ({start} → {end}, {n_days}d)", **r,
                 "note": f"first {warm} sessions inside warm-up" if warm else ""}
            rows.append(r)
            curves[(wlabel, n)] = pd.DataFrame(book.equity).set_index("date")["value"] - args.capital if book.equity else None
            for t in book.trades:
                all_trades.append({"window": wlabel, "strategy": n, **vars(t)})
            if args.trades:
                print(f"\n--- {wlabel} {eng.strategy.label} trades ---")
                tv = pd.DataFrame([vars(t) for t in book.trades])
                if len(tv):
                    tv["etfs"] = tv.etfs.map(",".join)
                    print(tv[["date", "side", "ticker", "price", "value", "pnl", "hold_days", "reason", "etfs"]].round(2).to_string(index=False))

    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
    df = pd.DataFrame(rows)
    for w in df.window.unique():
        sub = df[df.window == w].drop(columns=["window"]).set_index("strategy").T
        print(f"\n=== {w}  fill={args.fill}  capital=${args.capital:,.0f}  cap=${args.max_purchase_amount:,.0f}/issuer  "
              f"max {args.max_positions} positions ({args.max_positions_policy}, rising priority {'on' if cfg.rising_priority else 'off'}) ===")
        print(sub.to_string())

    if args.out:
        out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
        md = [f"# Strategy comparison — fill={args.fill}, capital ${args.capital:,.0f}, ${args.max_purchase_amount:,.0f}/issuer, "
              f"max {args.max_positions} positions ({args.max_positions_policy})\n"]
        for w in df.window.unique():
            sub = df[df.window == w].drop(columns=["window"]).set_index("strategy").T
            md += [f"\n## {w}\n", sub.to_markdown()]
        out.write_text("\n".join(md))
        pd.DataFrame(all_trades).to_csv(out.with_suffix(".csv"), index=False)
        try:
            import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
            fig, axes = plt.subplots(1, len(windows), figsize=(6 * len(windows), 4.2), squeeze=False)
            for ax, (wlabel, start, end) in zip(axes[0], windows):
                for n in names:
                    c = curves.get((wlabel, n))
                    if c is not None:
                        ax.plot(range(len(c)), c.values, label=engines[n].strategy.label)
                ax.axhline(0, color="grey", lw=0.5); ax.set_title(f"{wlabel}: {start} → {end}"); ax.set_ylabel("P&L $")
                ax.set_xlabel("sessions"); ax.legend(fontsize=8)
            plt.tight_layout(); fig.savefig(out.with_suffix(".png"), dpi=110)
            print(f"\nwrote {out}, {out.with_suffix('.csv')}, {out.with_suffix('.png')}")
        except Exception as ex:
            print(f"\nwrote {out}, {out.with_suffix('.csv')} (chart skipped: {ex})")


if __name__ == "__main__":
    main()
