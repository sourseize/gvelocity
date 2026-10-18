"""velocity resetbook — empty the position book (offline; no holdings fetch, no session)."""
import logging

from ..config import INITIAL_CAPITAL
from ..services import books as books_svc
from ..strategy import STRATEGY_NAME
from ._common import positive, check_db, open_db

log = logging.getLogger(__name__)
HELP = "Delete the book and its session history and start fresh"


def configure(p) -> None:
    p.add_argument("--capital", type=positive(float), default=INITIAL_CAPITAL, help="Capital of the fresh book")


def validate(p, args) -> None:
    check_db(p)


def run(args) -> int:
    conn = open_db()
    try:
        books_svc.reset(conn, STRATEGY_NAME, args.capital)
    finally:
        conn.close()
    log.info("fresh book with $%s — `velocity serve` starts trading it on the next session", f"{args.capital:,.0f}")
    return 0
