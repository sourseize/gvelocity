"""
Helpers shared by the `velocity` sub-commands: argument types, path checks, database access.
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
from pathlib import Path
from typing import Dict

from ..config import ETF_CONFIG, DEFAULT_REPORT_DIR
from ..database import get_db, default_db_path
from ..exceptions import VelocityError
from ..strategies import get_strategy, available, parse_params, DEFAULT_STRATEGY

ROOT = Path(__file__).resolve().parent.parent.parent


def default_book_db() -> Path:
    return ROOT / "data" / "books.db"


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
    """argparse type: a number in (0, 1]."""
    try:
        v = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number, got {text!r}")
    if not math.isfinite(v) or not 0 < v <= 1:
        raise argparse.ArgumentTypeError(f"must be between 0 and 1, got {text!r}")
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
def add_etf_arg(p: argparse.ArgumentParser, required: bool = False, help: str = "Which ETF, or ALL") -> None:
    p.add_argument("--etf", choices=sorted(ETF_CONFIG) + ["ALL"], required=required, help=help)


def add_strategy_args(p: argparse.ArgumentParser, params: bool = True, allow_all: bool = False) -> None:
    choices = available() + (["all"] if allow_all else [])
    p.add_argument("--strategy", choices=choices, default="all" if allow_all else DEFAULT_STRATEGY,
                   help=f"Strategy (default: {'all' if allow_all else DEFAULT_STRATEGY})")
    if params:
        p.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                       help="Override a strategy parameter (repeatable), e.g. --param breakout_len=40")


def add_report_dir_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR), help="Report root (default: report/)")


# ── validation (call from a command's validate(); uses parser.error -> exit 2) ──
def check_strategy_params(p: argparse.ArgumentParser, args) -> dict:
    """Build the strategy once to surface unknown/ill-typed --param values as a usage error."""
    try:
        params = parse_params(getattr(args, "param", []))
        if getattr(args, "strategy", None) not in (None, "all"):
            get_strategy(args.strategy, **params)
    except ValueError as e:
        p.error(str(e))
    return params


def check_report_dir(p: argparse.ArgumentParser, args) -> Path:
    root = Path(args.report_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        p.error(f"--report-dir: cannot create {root} ({e.strerror or e})")
    if not os.access(root, os.W_OK):
        p.error(f"--report-dir: {root} is not writable")
    return root


def is_sqlite_file(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            head = f.read(16)
    except OSError:
        return False
    return head == b"" or head == b"SQLite format 3\x00"


def check_db_arg(p: argparse.ArgumentParser, args) -> None:
    """--db: must be a SQLite file (or creatable), and only with a single --etf."""
    if not getattr(args, "db", None):
        return
    if getattr(args, "etf", None) == "ALL":
        p.error("--db needs a single --etf (each ETF has its own database)")
    path = Path(args.db)
    if path.is_dir():
        p.error(f"--db: {path} is a directory")
    if path.exists() and not is_sqlite_file(path):
        p.error(f"--db: {path} is not a SQLite database")
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            p.error(f"--db: cannot create directory {path.parent} ({e.strerror or e})")
        if not os.access(path.parent, os.W_OK):
            p.error(f"--db: directory {path.parent} is not writable")


def check_etf_dbs(p: argparse.ArgumentParser, etfs) -> None:
    for etf in etfs:
        dbp = default_db_path(etf)
        if dbp.exists() and not is_sqlite_file(dbp):
            p.error(f"holdings database {dbp} is not a SQLite database — restore it or move it aside")


def etfs_of(args) -> list:
    return sorted(ETF_CONFIG) if args.etf == "ALL" else [args.etf]


# ── databases ──
def open_etf_dbs(db_override: str | None = None, etf: str | None = None, require: bool = True) -> Dict[str, sqlite3.Connection]:
    """Connections to every existing ETF database (schema ensured); `db_override` replaces `etf`'s path."""
    conns = {}
    for e in sorted(ETF_CONFIG):
        path = Path(db_override) if (db_override and etf == e) else default_db_path(e)
        if path.exists():
            try:
                conns[e] = get_db(path)
            except sqlite3.Error as ex:
                raise VelocityError(f"cannot open holdings database {path}: {ex}") from ex
    if require and not conns:
        raise VelocityError("no ETF databases in data/ — run `velocity backfill --etf ALL` first")
    return conns


def open_book_db(must_exist: bool = False) -> sqlite3.Connection:
    path = default_book_db()
    if must_exist and not path.exists():
        raise VelocityError(f"no book database at {path} yet — run `velocity trading --paper-trade` first")
    if path.exists() and not is_sqlite_file(path):
        raise VelocityError(f"book database {path} is not a SQLite database — restore it from backup or move it aside")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(path)
    except (sqlite3.Error, OSError) as e:
        raise VelocityError(f"cannot open book database {path}: {e}") from e
