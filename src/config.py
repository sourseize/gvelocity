"""
Configuration and constants for Velocity.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── the tracked fund ───────────────────────────────────────────────────────
# Velocity tracks ONE iShares ETF: the daily holdings CSV behind this URL.  The fund is selected
# by the `portfolioId` parameter; change it to track another fund — nothing else in the code or
# the database refers to a specific ticker, so the switch is transparent (start a fresh database:
# `velocity backfill --wipe --force`, then `velocity resetbook` for each strategy).
HOLDINGS_URL = (
    "https://www.blackrock.com/varnish-api/blk-one01-product-data/"
    "product-data/api/v1/get-fund-document?appType=PRODUCT_PAGE"
    "&appSubType=ISHARES&targetSite=us-ishares&locale=en_US"
    "&portfolioId=307283&userType=individual&component=holdings"
)

FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.blackrock.com/",
}

# ── storage ────────────────────────────────────────────────────────────────
# One SQLite file holds everything: holdings history (snapshots / holdings / weekly_closes),
# the strategy books, the session and signal audit trail and optional daily_bars.
DB_PATH = ROOT / "data" / "velocity.db"

NEW_ENTRY_LOOKBACK_DAYS = 20

# ── strategy / book defaults (override on the command line) ────────────────
MAX_PURCHASE_AMOUNT = 1000.0         # hard $ cap per issuer
MAX_POSITIONS = 25                   # open positions per strategy book
RISK_PER_TRADE = 0.0025              # fraction of equity at risk at the initial stop
COMMISSION_PER_TRADE = 1.0
INITIAL_CAPITAL = 100000.0           # capital of a strategy book
