"""
velocity ticker — replay a strategy's signal state for one ticker over its whole history

  velocity ticker NVDA                          # default strategy
  velocity ticker NVDA --strategy double_bottom
  velocity ticker NVDA --all                    # every session, not only the ones with a signal

Plain-text output (redirect it to keep it).  The web server serves the same history as a page.
"""
from ..engine import Engine
from ..exceptions import VelocityError
from ..market_data import load_market_bars
from ..strategies import get_strategy, parse_params
from ._common import add_strategy_args, check_strategy_params, check_db, open_db, require_history

HELP = "Replay a strategy's signal state for one ticker over its whole history"


def configure(p) -> None:
    p.add_argument("ticker")
    add_strategy_args(p)
    p.add_argument("--all", action="store_true", help="print every session, not only those with a signal state")


def validate(p, args) -> None:
    if not args.ticker.strip():
        p.error("ticker must not be empty")
    check_strategy_params(p, args)
    check_db(p)


def signal_history(conn, strategy, ticker: str) -> list:
    """[{date, close, state, checks, note}] for every session the ticker has a price."""
    eng = Engine(strategy, conn, bars=load_market_bars(conn))
    if ticker not in eng.ind.tickers:
        raise VelocityError(f"{ticker}: no price history in the fund")
    closes = eng.ind.close[ticker].dropna()
    return [{"date": d, "close": float(c), **strategy.explain(d, ticker)} for d, c in closes.items()]


def run(args) -> int:
    conn = open_db(must_exist=True)
    try:
        require_history(conn)
        strategy = get_strategy(args.strategy, **parse_params(args.param))
        t = args.ticker.strip().upper()
        history = signal_history(conn, strategy, t)
    finally:
        conn.close()
    print(f"{t} — {strategy.label} ({strategy.name}) signal history")
    print(f"{'Date':<12}{'Close':>10}  {'State':<7} checks")
    for h in history:
        if args.all or h["state"] != "NONE":
            ok = ", ".join(k for k, v in h["checks"].items() if v)
            print(f"{h['date']:<12}{h['close']:>10.2f}  {h['state']:<7} {ok[:90]} {h.get('note', '')}")
    print(f"\n{t}: {len(history)} sessions · {sum(h['state'] == 'ENTRY' for h in history)} ENTRY · "
          f"{sum(h['state'] == 'EXIT' for h in history)} EXIT · {sum(h['state'] == 'WATCH' for h in history)} WATCH")
    return 0
