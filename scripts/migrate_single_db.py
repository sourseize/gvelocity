#!/usr/bin/env python3
"""
One-off migration: copy the holdings history of an old per-ETF database (data/<ETF>.db) into the
single application database (data/velocity.db).

    python scripts/migrate_single_db.py data/OLD.db             # -> data/velocity.db
    python scripts/migrate_single_db.py old.db --to /tmp/v.db

Only the holdings tables are copied (snapshots, holdings, weekly_closes); the old book / session /
signal tables are NOT migrated — the books start fresh.  Existing rows in the target are kept
(INSERT OR IGNORE), so the script is safe to re-run.  The source file is left untouched.
"""
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import DB_PATH          # noqa: E402
from src.database import get_db, transaction   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="old per-ETF database (data/<ETF>.db)")
    ap.add_argument("--to", default=str(DB_PATH), help=f"target database (default: {DB_PATH})")
    a = ap.parse_args()
    src, dst = Path(a.source), Path(a.to)
    if not src.is_file():
        ap.error(f"{src} is not a file")
    if src.resolve() == dst.resolve():
        ap.error("source and target are the same file")

    conn = get_db(dst)                                  # creates the schema if needed
    conn.execute("ATTACH DATABASE ? AS old", (str(src),))
    before = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    with transaction(conn):
        conn.execute("INSERT OR IGNORE INTO snapshots (fetch_date, fetched_at) "
                     "SELECT fetch_date, fetched_at FROM old.snapshots ORDER BY fetch_date")
        conn.execute("INSERT OR IGNORE INTO holdings (snapshot_date, ticker, name, sector, weight_pct, market_value, "
                     "shares, price, currency, exchange, location) "
                     "SELECT snapshot_date, ticker, name, sector, weight_pct, market_value, shares, price, currency, "
                     "exchange, location FROM old.holdings ORDER BY snapshot_date, ticker")
        conn.execute("INSERT OR IGNORE INTO weekly_closes (week_ending, ticker, name, sector, weight_pct, price, currency) "
                     "SELECT week_ending, ticker, name, sector, weight_pct, price, currency FROM old.weekly_closes "
                     "ORDER BY week_ending, ticker")
    conn.execute("DETACH DATABASE old")
    n, lo, hi = conn.execute("SELECT COUNT(*), MIN(fetch_date), MAX(fetch_date) FROM snapshots").fetchone()
    h = conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0]
    conn.execute("VACUUM")
    conn.close()
    print(f"{dst}: {n} snapshots ({lo} → {hi}, {n - before} added), {h} holdings rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
