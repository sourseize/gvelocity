"""
`velocity <command>` — the sub-commands.

Each command is a module with:
    HELP                 one-line description for `velocity --help`
    configure(parser)    add its arguments
    validate(parser, args)   optional: reject bad input with parser.error() BEFORE any side effect
    run(args) -> int     do the work; expected failures raise VelocityError
"""
from . import trading, fetch, strategies, reset_book, backfill, backtest, ticker, rebuild

#: name -> (module, aliases)
COMMANDS = {
    "trading":    (trading,    []),
    "fetch":      (fetch,      []),
    "backtest":   (backtest,   []),
    "backfill":   (backfill,   []),
    "rebuild":    (rebuild,    ["report-rebuild"]),
    "ticker":     (ticker,     []),
    "strategies": (strategies, ["list-strategies"]),
    "reset-book": (reset_book, []),
}

DEFAULT_COMMAND = "trading"


def all_names():
    names = []
    for name, (_, aliases) in COMMANDS.items():
        names.append(name)
        names.extend(aliases)
    return names


__all__ = ["COMMANDS", "DEFAULT_COMMAND", "all_names"]
