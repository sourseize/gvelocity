#!/usr/bin/env python3
"""
Velocity — daily ETF holdings tracker + strategy session runner

  velocity trading --etf ALL --paper-trade [--strategy NAME]   daily run (the main action)
  velocity fetch --etf ALL                                     store today's holdings only
  velocity backtest [--strategy NAME] [--save-book]            replay strategies; register the result as the book
  velocity backfill --etf ALL --months 12                      historical holdings
  velocity rebuild --etf ALL                                   regenerate the HTML reports
  velocity ticker NVDA --strategy double_bottom                one ticker's signal history
  velocity strategies                                          list strategies and parameters
  velocity reset-book --strategy NAME                          empty a strategy's book

  velocity <command> --help                                    options of a command
"""
import argparse
import logging
import sqlite3
import sys

from src.commands import COMMANDS, DEFAULT_COMMAND, all_names
from src.exceptions import VelocityError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("velocity")


def build_parser():
    p = argparse.ArgumentParser(prog="velocity", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging; show tracebacks for unexpected errors")
    subs = p.add_subparsers(dest="command", metavar="<command>", title="commands")
    parsers = {}
    for name, (mod, aliases) in COMMANDS.items():
        sp = subs.add_parser(name, aliases=aliases, help=mod.HELP, description=mod.__doc__,
                             formatter_class=argparse.RawDescriptionHelpFormatter)
        sp.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        mod.configure(sp)
        parsers[name] = sp
        for a in aliases:
            parsers[a] = sp
    return p, parsers


def canonical(name: str) -> str:
    for cname, (_, aliases) in COMMANDS.items():
        if name == cname or name in aliases:
            return cname
    return name


def legacy_argv(argv):
    """Pre-command invocations keep working:  `velocity.py --etf ALL --paper-trade`  ->  `trading …`
    and  `velocity.py --list-strategies`  ->  `strategies`."""
    if not argv:
        return argv
    if "--list-strategies" in argv:
        log.info("`--list-strategies` is now `velocity strategies`")
        return ["strategies"] + [a for a in argv if a != "--list-strategies"]
    first = next((a for a in argv if a not in ("-v", "--verbose")), None)
    if first is not None and first.startswith("-") and first not in ("-h", "--help"):
        log.info("no command given — assuming `velocity %s` (say it explicitly; this fallback is for old scripts)", DEFAULT_COMMAND)
        i = argv.index(first)
        return argv[:i] + [DEFAULT_COMMAND] + argv[i:]
    return argv


def main(argv=None) -> int:
    argv = legacy_argv(sys.argv[1:] if argv is None else list(argv))
    p, parsers = build_parser()
    args = p.parse_args(argv)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    if not args.command:
        p.print_help()
        return 2
    name = canonical(args.command)
    mod, _ = COMMANDS[name]
    sp = parsers[name]

    if hasattr(mod, "validate"):
        mod.validate(sp, args)            # exits 2 on bad input, before any side effect
    try:
        return mod.run(args) or 0
    except KeyboardInterrupt:
        log.warning("interrupted — nothing further was written")
        return 130
    except VelocityError as e:
        log.error("%s", e)
        return 1
    except (sqlite3.Error, OSError) as e:
        log.error("%s: %s", type(e).__name__, e, exc_info=args.verbose)
        return 1
    except Exception as e:  # a genuine bug: one line by default, full traceback with --verbose
        log.error("unexpected error: %s: %s%s", type(e).__name__, e,
                  "" if args.verbose else " (re-run with --verbose for the traceback)", exc_info=args.verbose)
        return 1


if __name__ == "__main__":
    sys.exit(main())
