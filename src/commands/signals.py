"""
velocity signals — how much does each rule of the strategy actually select?

For every rule (and a few reference signals) the report shows on how many ticker-days it fired
in the stored history and the mean forward return of those ticker-days IN EXCESS of the
equal-weight universe, 5 to 60 sessions ahead, with the share of positive outcomes.  A rule
that selects nothing shows an excess near 0 % and a hit rate near 50 %.  Judge a rule here
before giving it to the engine — position management (stops, exits) is measured by `backtest`.

  velocity signals
  velocity signals --param breakout_len=40
"""
import pandas as pd

from .. import analytics
from ..engine import Engine, EngineConfig
from ..market_data import bars_from_holdings
from ..strategy import make_strategy
from ._common import add_param_arg, check_strategy_params, check_db, open_db, require_history

HELP = "Signal-quality report: forward excess return of every rule of the strategy"


def configure(p) -> None:
    add_param_arg(p)


def validate(p, args) -> None:
    args.params = check_strategy_params(p, args)
    check_db(p)


def run(args) -> int:
    conn = open_db(must_exist=True)
    try:
        require_history(conn)
        engine = Engine(make_strategy(**args.params), conn, EngineConfig(), bars=bars_from_holdings(conn))
        rows = analytics.signal_quality(engine.ind, analytics.strategy_rules(engine.strategy))
    finally:
        conn.close()
    dates = engine.dates
    print(f"Signal quality — {engine.strategy.label} · {dates[0]} → {dates[-1]} ({len(dates)} sessions, {len(engine.ind.tickers)} tickers)")
    print("mean forward return in excess of the equal-weight universe (hit = share > 0); n = ticker-days the rule fired on\n")
    table = []
    for r in rows:
        row = {"rule": r["rule"], "n": r["n"]}
        for h, v in r["horizons"].items():
            row[f"{h}d"] = f"{v['excess']:+.2%} ({v['hit']:.0%})" if v["n"] else "—"
        table.append(row)
    pd.set_option("display.width", 250)
    print(pd.DataFrame(table).to_string(index=False))
    return 0
