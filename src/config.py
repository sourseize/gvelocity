"""
Configuration and constants for Velocity tracker
"""

import os
from pathlib import Path

# ── ETF config ─────────────────────────────────────────────────────────────
# Add a new fund by adding an entry here.
#
# Two URL patterns have been confirmed in use so far:
#
# BlackRock varnish-api (MTUM, QUAL, and DYNF — US-domiciled ETFs):
#      https://www.blackrock.com/varnish-api/blk-one01-product-data/
#          product-data/api/v1/get-fund-document?appType=PRODUCT_PAGE
#          &appSubType=ISHARES&targetSite=us-ishares&locale=en_US
#          &portfolioId={id}&userType=individual&component=holdings
#
# MTUM, QUAL, and DYNF URLs
# and CSV structure were confirmed against a real sample the user fetched
# manually — but the live endpoint itself (headers/anti-bot behaviour on
# blackrock.com, and whether omitting asOfDate really returns "latest")
# has NOT been tested end-to-end here. Test with a plain curl before
# relying on it in cron:
#   curl -s "URL_HERE" | head -20
ETF_CONFIG = {
    "MTUM": {
        "name": "iShares MSCI USA Momentum Factor ETF",
        "url": (
            "https://www.blackrock.com/varnish-api/blk-one01-product-data/"
            "product-data/api/v1/get-fund-document?appType=PRODUCT_PAGE"
            "&appSubType=ISHARES&targetSite=us-ishares&locale=en_US"
            "&portfolioId=251614&userType=individual&component=holdings"
        ),
    },
    "QUAL": {
        "name": "iShares MSCI USA Quality Factor ETF",
        "url": (
            "https://www.blackrock.com/varnish-api/blk-one01-product-data/"
            "product-data/api/v1/get-fund-document?appType=PRODUCT_PAGE"
            "&appSubType=ISHARES&targetSite=us-ishares&locale=en_US"
            "&portfolioId=256101&userType=individual&component=holdings"
        ),
    },
    "DYNF": {
        "name": "iShares U.S. Equity Factor Rotation Active ETF",
        "url": (
            "https://www.blackrock.com/varnish-api/blk-one01-product-data/"
            "product-data/api/v1/get-fund-document?appType=PRODUCT_PAGE"
            "&appSubType=ISHARES&targetSite=us-ishares&locale=en_US"
            "&portfolioId=307283&userType=individual&component=holdings"
        ),
    },
}

FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.blackrock.com/",
}

NEW_ENTRY_LOOKBACK_DAYS = 20

# Layout: velocity/src/velocity.py (this file), velocity/src/data/{ETF}.db,
# velocity/report/ (report output — a separate git checkout on the `report`
# branch, one level up from src/, not nested inside it).
DEFAULT_REPORT_DIR = Path(__file__).resolve().parent.parent / "report"

# Sector colours
SECTOR_COLORS = {
    "Information Technology": "#3b82f6",
    "Financials":              "#10b981",
    "Health Care":             "#8b5cf6",
    "Industrials":             "#f59e0b",
    "Energy":                  "#ef4444",
    "Consumer Staples":        "#06b6d4",
    "Consumer Discretionary":  "#ec4899",
    "Communication":           "#6366f1",
    "Materials":               "#84cc16",
    "Utilities":               "#f97316",
    "Real Estate":             "#14b8a6",
}

def sc(sector: str) -> str:
    return SECTOR_COLORS.get(sector or "", "#6b7280")

# Confirmation score mapping
SCORE_META = {
    5: ("STRONG",  "#10b981"),
    4: ("GOOD",    "#22c55e"),
    3: ("WATCH",   "#f59e0b"),
    2: ("WEAK",    "#ef4444"),
    1: ("AVOID",   "#ef4444"),
    0: ("AVOID",   "#6b7280"),
}

# Trading parameters for paper trading
RISING_STAR_LOOKBACK_DAYS = 60    # how far back "recently entered" reaches
RISING_STAR_MIN_GROWTH    = 0.30  # min weight growth since entry (30%) to qualify
TRAILING_STOP_PERCENT = 0.15      # 15% trailing stop loss

# Paper trading capital and position limits
INITIAL_CAPITAL_PER_ETF = 100000.0   # $100k pool for each ETF
MAX_PURCHASE_AMOUNT = 1000.0         # $1k max purchase on one ticker
ALLOCATION_PCT = 0.1                 # 10% allocation per trade (before max cap applied)

# ── Alpaca configuration ───────────────────────────────────────────────────
# Load from .env file or environment variables
# The .env file should be at the project root and contain:
#   ALPACA_API_KEY=your_api_key
#   ALPACA_SECRET_KEY=your_secret_key
#   ALPACA_PAPER=true

def _load_env_file():
    """Load environment variables from .env file if it exists."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

_load_env_file()

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")
