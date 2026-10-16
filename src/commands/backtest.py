"""
velocity backtest — replay one or all strategies over the stored history and compare them.

  velocity backtest                                  # all strategies, 3/6/9-month windows
  velocity backtest --strategy trend_follow --windows 3,6
  velocity backtest --start 2026-02-13 --end 2026-08-13 --fill next
  velocity backtest --max-positions 40 --max-positions-policy replace
  velocity backtest --param breakout_len=40 --strategy trend_follow
  velocity backtest --trades > results.txt           # plain text, redirect to keep it
  velocity backtest --strategy trend_follow --windows 6 --save-book
                                                          # register the replayed trades as the live book

Every strategy is run with the same bars, capital, $ cap per issuer, position limit and
commission, and all books are marked to market at the window's last date.  The output is plain
text: a header describing the setup and the exact command, one summary table per window and,
with --trades, every trade.

--save-book (one strategy, one window) REPLACES that strategy's book in the database with the
backtest's positions, trades, equity curve and session audit trail, so the next
`velocity serve` session carries on from the backtest's end date.
No network access is needed: everything runs on the stored history.
"""
import datetime as _dt
import shlex
import sys

import pandas as pd

from ..engine import EngineConfig
from ..services import backtest as bt_svc, books as books_svc
from ..strategies import available
from ._common import (add_strategy_args, add_engine_args, engine_config, iso_date, check_strategy_params, check_db,
                      open_db, require_history)

HELP = "Replay one or all strategies over the stored history (plain-text results); optionally register the result as the live book"


def command_line() -> str:
    """The command as typed, so a result can be reproduced."""
    return shlex.join(["velocity"] + sys.argv[1:])


def print_header(args, cfg: EngineConfig, engines: dict, names: list, dates: list, windows: list) -> None:
    ran_at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    bars = next(iter(engines.values())).ind
    print("=" * 100)
    print(f"Velocity backtest — {', '.join(engines[n].strategy.label for n in names)}")
    print(f"ran       : {ran_at}")
    print(f"command   : {command_line()}")
    print(f"history   : {dates[0]} → {dates[-1]} ({len(dates)} sessions, {len(bars.tickers)} tickers, "
          f"{'OHLCV bars' if bars.has_volume else 'price-only (holdings prices, volume rules inactive)'})")
    print(f"windows   : " + "; ".join(f"{w} = {s0} → {e0}" for w, s0, e0 in windows))
    print(f"fill      : {args.fill} ({'signal-day close, what the live session does' if args.fill == 'same' else 'next session close'})")
    print(f"capital   : ${args.capital:,.0f} · ${cfg.max_amount:,.0f}/issuer cap · risk {cfg.risk_per_trade:.2%}/trade · "
          f"commission ${cfg.commission:.2f}")
    print(f"positions : max {cfg.max_positions} ({cfg.policy.mode}) · rising priority {'on' if cfg.rising_priority else 'off'} · "
          f"cooldown {cfg.cooldown_days}d")
    for n in names:
        st = engines[n].strategy
        print(f"{n:<10}: warm-up {st.warmup()} bars · params {st.params_dict()}")
    print("=" * 100)


def configure(p) -> None:
    add_strategy_args(p, allow_all=True)
    p.add_argument("--windows", default="3,6,9", help="trailing windows in months (default 3,6,9)")
    p.add_argument("--start", type=iso_date, help="YYYY-MM-DD (custom window instead of --windows)")
    p.add_argument("--end", type=iso_date, help="YYYY-MM-DD")
    p.add_argument("--fill", choices=["same", "next"], default="same",
                   help="same = fill at the signal day's close (default; what the live session does); "
                        "next = fill at the next session's close")
    add_engine_args(p, rising_choice=True)
    p.add_argument("--trades", action="store_true", help="print the trade list of each run")
    p.add_argument("--save-book", action="store_true",
                   help="register this backtest's trades/positions as the strategy's live book "
                        "(one strategy, one window; replaces the existing book)")


def validate(p, args) -> None:
    args.months = None
    if args.save_book:
        if args.strategy == "all":
            p.error("--save-book needs a single --strategy")
        if not (args.start or args.end) and len(args.windows.split(",")) != 1:
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
        names = available() if args.strategy == "all" else [args.strategy]
        cfg = engine_config(args, short_history="error")
        engines, dates = bt_svc.prepare(conn, names, args.params, cfg)
        windows = bt_svc.resolve_windows(dates, args.months, args.start, args.end)
        print_header(args, cfg, engines, names, dates, windows)

        result = bt_svc.run(engines, dates, windows, args.fill, args.capital)

        pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
        df = pd.DataFrame(result.rows)
        for w in df.window.unique():
            sub = df[df.window == w].drop(columns=["window"]).set_index("strategy").T
            print(f"\n--- {w} ---")
            print(sub.to_string())

        if args.trades:
            for (wlabel, n), book in result.books.items():
                print(f"\n--- {wlabel} {engines[n].strategy.label} trades ({len(book.trades)}, {len(book.positions)} open at the end) ---")
                tv = pd.DataFrame([vars(t) for t in book.trades])
                if len(tv):
                    print(tv[["date", "side", "ticker", "price", "shares", "value", "pnl", "hold_days", "reason"]]
                          .round(2).to_string(index=False))
                else:
                    print("no trades")

        if args.save_book:
            wlabel, start, end = windows[0]
            book, replaced = books_svc.register_backtest(conn, engines[names[0]], start, end, args.fill, args.capital,
                                                         command=command_line())
            if replaced:
                print(f"\nreplaced the existing {book.strategy} book (last session {replaced.last_date}, "
                      f"{len(replaced.trades)} trades, {len(replaced.positions)} open)")
            print(f"registered {book.strategy} book: {len(book.trades)} trades, {len(book.positions)} open, "
                  f"equity ${book.total_value():,.0f}, last session {book.last_date}")
        return 0
    finally:
        conn.close()
