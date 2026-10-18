"""
`velocity <command>` — the sub-commands.

Each command is a module with:
    HELP                 one-line description for `velocity --help`
    NEEDS_DB             optional (default True): take the instance lock — refused while `velocity serve` runs
    configure(parser)    add its arguments
    validate(parser, args)   optional: reject bad input with parser.error() BEFORE any side effect
    run(args) -> int     do the work; expected failures raise VelocityError
"""
from . import serve, resetbook, backfill, backtest, signals, ticker, status

#: name -> (module, aliases)
COMMANDS = {
    "serve":      (serve,      []),
    "status":     (status,     []),
    "backtest":   (backtest,   []),
    "signals":    (signals,    []),
    "backfill":   (backfill,   []),
    "ticker":     (ticker,     []),
    "resetbook":  (resetbook,  []),
}


__all__ = ["COMMANDS"]
