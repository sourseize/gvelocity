"""
Helpers shared by the `velocity` sub-commands: argument types, validation, database access.
"""
from __future__ import annotations

import argparse
import math
import sqlite3

from ..config import DB_PATH, INITIAL_CAPITAL, MAX_POSITIONS, CASH_RESERVE, COMMISSION_PER_TRADE
from ..database import get_db, SchemaTooNew
from ..engine import EngineConfig, MaxPositionsPolicy
from ..exceptions import VelocityError
from ..strategy import make_strategy, parse_params


# ── argparse types ──
def positive(kind):
    """argparse type: a finite number > 0 (kind = int or float)."""
    def conv(text):
        try:
            v = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected {'an integer' if kind is int else 'a number'}, got {text!r}")
        if not math.isfinite(v) or v <= 0:
            raise argparse.ArgumentTypeError(f"must be > 0, got {text!r}")
        return v
    conv.__name__ = f"positive {kind.__name__}"
    return conv


def fraction(text):
    """argparse type: a number in [0, 1)."""
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number, got {text!r}")
    if not math.isfinite(v) or not 0 <= v < 1:
        raise argparse.ArgumentTypeError(f"must be between 0 and 1 (exclusive), got {text!r}")
    return v


def iso_date(text):
    """argparse type: YYYY-MM-DD."""
    import datetime
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {text!r}")
    return text


# ── common argument groups ──
def add_param_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                   help="Override a strategy parameter (repeatable), e.g. --param breakout_len=40")


def add_engine_args(p: argparse.ArgumentParser, rising_choice: bool = False) -> None:
    """Book / sizing / position-limit settings (the same on `backtest` and `serve`)."""
    p.add_argument("--capital", type=positive(float), default=INITIAL_CAPITAL, help="Book capital when first created")
    p.add_argument("--max-positions", type=positive(int), default=MAX_POSITIONS,
                   help=f"Open positions in the book; each gets an equal share of the investable equity (default {MAX_POSITIONS})")
    p.add_argument("--cash-reserve", type=fraction, default=CASH_RESERVE,
                   help=f"Share of equity kept in cash and never invested (default {CASH_RESERVE:.2f})")
    p.add_argument("--max-positions-policy", choices=["skip", "replace"], default="skip",
                   help="When the book is full: skip new signals, or replace the weakest losing position")
    if rising_choice:
        p.add_argument("--rising-priority", choices=["on", "off"], default=None,
                       help="give waiting rising stars priority for free slots (default: engine default)")
    else:
        p.add_argument("--rising-priority", action="store_true",
                       help="Give waiting rising stars priority over fresh signals for free slots (default: strategy rank)")


def engine_config(args, short_history: str = "warn") -> EngineConfig:
    cfg = EngineConfig(max_positions=args.max_positions, cash_reserve=args.cash_reserve, commission=COMMISSION_PER_TRADE,
                       policy=MaxPositionsPolicy(mode=args.max_positions_policy), short_history=short_history)
    rp = getattr(args, "rising_priority", None)
    if isinstance(rp, str):
        cfg.rising_priority = rp == "on"
    elif rp:
        cfg.rising_priority = True
    return cfg


# ── validation (call from a command's validate(); uses parser.error -> exit 2) ──
def check_strategy_params(p: argparse.ArgumentParser, args) -> dict:
    """Build the strategy once to surface unknown/ill-typed --param values as a usage error."""
    try:
        params = parse_params(getattr(args, "param", []))
        make_strategy(**params)
    except ValueError as e:
        p.error(str(e))
    return params


def is_sqlite_file(path) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return False
    return head == b"" or head == b"SQLite format 3\x00"


def check_db(p: argparse.ArgumentParser) -> None:
    """The application database, if present, must be a SQLite file (usage error otherwise)."""
    if DB_PATH.exists() and not is_sqlite_file(DB_PATH):
        p.error(f"database {DB_PATH} is not a SQLite database — restore it or move it aside")


# ── database ──
def open_db(must_exist: bool = False) -> sqlite3.Connection:
    """Connection to data/velocity.db (created with the holdings schema when missing)."""
    if must_exist and not DB_PATH.exists():
        raise VelocityError(f"no database at {DB_PATH} yet — run `velocity backfill` first")
    if DB_PATH.exists() and not is_sqlite_file(DB_PATH):
        raise VelocityError(f"database {DB_PATH} is not a SQLite database — restore it or move it aside")
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        return get_db(DB_PATH)
    except SchemaTooNew as e:
        raise VelocityError(f"{e} — upgrade Velocity before using this database") from e
    except (sqlite3.Error, OSError) as e:
        raise VelocityError(f"cannot open database {DB_PATH}: {e}") from e


def require_history(conn: sqlite3.Connection) -> None:
    """Raise when the database holds no holdings snapshots yet."""
    if conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0:
        raise VelocityError(f"no holdings history in {DB_PATH} — run `velocity backfill` first")
