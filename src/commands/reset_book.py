"""velocity reset-book — empty a strategy's position book (offline; no holdings fetch, no session)."""
import logging
import sqlite3
from pathlib import Path

from ..book import Book
from ..config import INITIAL_CAPITAL
from ..exceptions import VelocityError
from ..reporter import generate_hub_index
from ..strategies import get_strategy, parse_params
from ._common import (add_strategy_args, add_report_dir_arg, positive, check_strategy_params, check_report_dir,
                      open_book_db)

log = logging.getLogger(__name__)
HELP = "Delete a strategy's book and start fresh (offline)"


def configure(p) -> None:
    add_strategy_args(p, params=False)
    p.add_argument("--capital", type=positive(float), default=INITIAL_CAPITAL, help="Capital of the fresh book")
    add_report_dir_arg(p)


def validate(p, args) -> None:
    check_strategy_params(p, args)
    check_report_dir(p, args)


def reset(strategy_name: str, label: str, capital: float, report_root: Path) -> Book:
    """Delete the book, write its (now empty) report page and refresh the hub.  Returns the fresh book."""
    book_conn = open_book_db()
    try:
        old = Book.load(book_conn, strategy_name, capital)
        Book.delete(book_conn, strategy_name)
        book = Book.load(book_conn, strategy_name, capital)
    except (sqlite3.Error, ValueError, TypeError) as e:
        raise VelocityError(f"book database is unusable ({e}) — restore it from backup or move it aside") from e
    finally:
        book_conn.close()
    if old.last_date or old.trades:
        log.info("[%s] book reset: dropped %d trades, %d open positions (last session %s)",
                 strategy_name, len(old.trades), len(old.positions), old.last_date)
    else:
        log.info("[%s] book reset (it was already empty)", strategy_name)
    log.info("[%s] fresh book with $%s — `velocity trading --paper-trade` starts trading it", strategy_name, f"{capital:,.0f}")
    book.write_report(report_root / "strategy", label)
    (report_root / "index.html").write_text(generate_hub_index(report_root), encoding="utf-8")
    return book


def run(args) -> int:
    strategy = get_strategy(args.strategy)
    reset(strategy.name, strategy.label, args.capital, Path(args.report_dir))
    return 0
