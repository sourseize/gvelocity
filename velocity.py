#!/usr/bin/env python3
"""Velocity — fund holdings tracker + paper-trading strategy runner.  `velocity <command> --help` for a command's options."""
import argparse
import logging
import sqlite3
import sys

from src.commands import COMMANDS
from src.exceptions import VelocityError
from src.lock import InstanceLock, LockHeld

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("velocity")


def build_parser():
    p = argparse.ArgumentParser(prog="velocity", description=__doc__)
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


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
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
    lock = None
    try:
        if getattr(mod, "NEEDS_DB", True):
            lock = InstanceLock(name).acquire()     # refused while `velocity serve` (or another command) runs
        return mod.run(args) or 0
    except LockHeld as e:
        log.error("%s", e)
        return 1
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
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    sys.exit(main())
