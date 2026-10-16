"""velocity resetbook — empty a strategy's position book (offline; no holdings fetch, no session)."""
import logging

from ..config import INITIAL_CAPITAL
from ..services import books as books_svc
from ..strategies import get_strategy
from ._common import add_strategy_args, positive, check_strategy_params, check_db, open_db

log = logging.getLogger(__name__)
HELP = "Delete a strategy's book and start fresh"


def configure(p) -> None:
    add_strategy_args(p, params=False)
    p.add_argument("--capital", type=positive(float), default=INITIAL_CAPITAL, help="Capital of the fresh book")


def validate(p, args) -> None:
    check_strategy_params(p, args)
    check_db(p)


def run(args) -> int:
    strategy = get_strategy(args.strategy)
    conn = open_db()
    try:
        books_svc.reset(conn, strategy.name, args.capital)
    finally:
        conn.close()
    log.info("[%s] fresh book with $%s — `velocity serve` starts trading it", strategy.name, f"{args.capital:,.0f}")
    return 0
