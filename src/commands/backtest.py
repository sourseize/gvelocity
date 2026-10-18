"""
velocity backtest — replay the strategy over the stored history, judged against the fund.

  velocity backtest                                  # 3/6/9-month windows ending on the latest date
  velocity backtest --windows 6,12
  velocity backtest --start 2026-02-13 --end 2026-08-13 --fill next
  velocity backtest --max-positions 20 --cash-reserve 0.2 --max-positions-policy replace
  velocity backtest --param breakout_len=40
  velocity backtest --trades > results.txt           # plain text, redirect to keep it
  velocity backtest --windows 6 --save-book          # register the replayed trades as the live book

Every window starts a fresh book with --capital and is marked to market at the window's last
date.  The output is plain text: a header describing the setup and the exact command, one
column per window (book vs the fund: return, drawdown, Sharpe, how many rolling quarters beat
the fund, trade statistics), then the exit-reason and sector breakdowns and, with --trades,
every trade.

--save-book (one window) REPLACES the live book with the backtest's positions, trades, equity
curve and session audit trail, so the next `velocity serve` session carries on from the
backtest's end date.  No network access is needed.
"""
import datetime as _dt
import shlex
import sys

import pandas as pd

from ..engine import EngineConfig
from ..services import backtest as bt_svc, books as books_svc
from ._common import (add_param_arg, add_engine_args, engine_config, iso_date, check_strategy_params, check_db,
                      open_db, require_history)

HELP = "Replay the strategy over the stored history against the fund (plain text); optionally register the result as the live book"


def command_line() -> str:
    """The command as typed, so a result can be reproduced."""
    return shlex.join(["velocity"] + sys.argv[1:])


def print_header(args, cfg: EngineConfig, engine, windows: list) -> None:
    ran_at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    st, dates = engine.strategy, engine.dates
    print("=" * 100)
    print(f"Velocity backtest — {st.label}")
    print(f"ran       : {ran_at}")
    print(f"command   : {command_line()}")
    print(f"history   : {dates[0]} → {dates[-1]} ({len(dates)} sessions, {len(engine.ind.tickers)} tickers, "
          f"fund valuation prices)")
    print(f"windows   : " + "; ".join(f"{w} = {s0} → {e0}" for w, s0, e0 in windows))
    print(f"fill      : {args.fill} ({'signal-day holdings price, what the live session does' if args.fill == 'same' else 'next session close'})")
    print(f"capital   : ${args.capital:,.0f} · {cfg.max_positions} positions x {(1 - cfg.cash_reserve) / cfg.max_positions:.1%} of equity · "
          f"cash reserve {cfg.cash_reserve:.0%} · commission ${cfg.commission:.2f}")
    print(f"positions : policy {cfg.policy.mode} · rising priority {'on' if cfg.rising_priority else 'off'} · cooldown {cfg.cooldown_days}d")
    print(f"strategy  : warm-up {st.warmup()} bars · params {st.params_dict()}")
    print("=" * 100)


def configure(p) -> None:
    add_param_arg(p)
    p.add_argument("--windows", default="3,6,9", help="trailing windows in months (default 3,6,9)")
    p.add_argument("--start", type=iso_date, help="YYYY-MM-DD (custom window instead of --windows)")
    p.add_argument("--end", type=iso_date, help="YYYY-MM-DD")
    p.add_argument("--fill", choices=["same", "next"], default="same",
                   help="same = fill at the signal day's holdings price (default; what the live session does); "
                        "next = fill at the next session's price")
    add_engine_args(p, rising_choice=True)
    p.add_argument("--trades", action="store_true", help="print the trade list of each window")
    p.add_argument("--save-book", action="store_true",
                   help="register this backtest's trades/positions as the live book (one window; replaces the existing book)")


def validate(p, args) -> None:
    args.months = None
    if args.save_book and not (args.start or args.end) and len(args.windows.split(",")) != 1:
        p.error("--save-book needs a single window: --windows N or --start/--end")
    if not (args.start or args.end):
        try:
            args.months = [int(x) for x in args.windows.split(",")]
        except ValueError:
            p.error(f"--windows expects comma-separated month counts, got {args.windows!r}")
        if any(m < 1 for m in args.months):
            p.error("--windows: month counts must be >= 1")
    if args.start and args.end and args.start > args.end:
        p.error(f"--start {args.start} is after --end {args.end}")
    args.params = check_strategy_params(p, args)
    check_db(p)


def run(args) -> int:
    conn = open_db(must_exist=True)
    try:
        require_history(conn)
        cfg = engine_config(args, short_history="error")
        engine = bt_svc.prepare(conn, args.params, cfg)
        windows = bt_svc.resolve_windows(engine.dates, args.months, args.start, args.end)
        print_header(args, cfg, engine, windows)

        result = bt_svc.run(conn, engine, windows, args.fill, args.capital)

        pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
        table = pd.DataFrame({f"{r.label} ({r.start} → {r.end}, {r.n_days}d)": bt_svc.summary_rows(r) for r in result.results})
        print("\n--- book vs fund ---")
        print(table.to_string())

        for r in result.results:
            print(f"\n--- {r.label}: rolling quarters (63 sessions, stepped monthly) ---")
            wins = r.perf.get("windows", [])
            if wins:
                print(pd.DataFrame([{"from": w["start"], "to": w["end"], "book": f"{w['book']:+.1%}", "fund": f"{w['bench']:+.1%}",
                                     "excess": f"{w['excess']:+.1%}", "beat": "yes" if w["beat"] else "no"} for w in wins]).to_string(index=False))
            else:
                print("window too short for a rolling quarter")
            print(f"\n--- {r.label}: exits by reason ---")
            br = r.trades["by_reason"]
            print(pd.DataFrame([{"reason": d["reason"], "n": d["n"], "win rate": f"{d['wins'] / d['n']:.0%}", "P&L $": f"{d['pnl']:.0f}",
                                 "avg $": f"{d['avg']:.0f}"} for d in br]).to_string(index=False) if br else "no closed trades")
            print(f"\n--- {r.label}: P&L by sector (realized + unrealized) ---")
            bs = r.trades["by_sector"]
            print(pd.DataFrame([{"sector": d["sector"], "closed": d["n"], "open": d.get("open", 0), "realized $": f"{d['pnl']:.0f}",
                                 "unrealized $": f"{d.get('unrealized', 0.0):.0f}"} for d in bs]).to_string(index=False) if bs else "none")

        if args.trades:
            for r in result.results:
                book = r.book
                print(f"\n--- {r.label} trades ({len(book.trades)}, {len(book.positions)} open at the end) ---")
                tv = pd.DataFrame([vars(t) for t in book.trades])
                if len(tv):
                    print(tv[["date", "side", "ticker", "price", "shares", "value", "pnl", "hold_days", "reason"]]
                          .round(2).to_string(index=False))
                else:
                    print("no trades")

        if args.save_book:
            wlabel, start, end = windows[0]
            book, replaced = books_svc.register_backtest(conn, engine, start, end, args.fill, args.capital,
                                                         command=command_line())
            if replaced:
                print(f"\nreplaced the existing book (last session {replaced.last_date}, "
                      f"{len(replaced.trades)} trades, {len(replaced.positions)} open)")
            print(f"registered the book: {len(book.trades)} trades, {len(book.positions)} open, "
                  f"equity ${book.total_value():,.0f}, last session {book.last_date}")
        return 0
    finally:
        conn.close()
