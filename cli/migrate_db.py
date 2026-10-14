#!/usr/bin/env python3
"""
migrate_db.py — back up the ETF databases, rebuild them on the current schema with only the
holdings (backfill) data, and replay the strategy sessions into the books database.

  python cli/migrate_db.py                          # all ETFs, default strategy, replay all history
  python cli/migrate_db.py --strategy all           # build a book for every registered strategy
  python cli/migrate_db.py --replay-from 2026-01-02 # start the session replay later
  python cli/migrate_db.py --no-replay              # schema/data migration only
  python cli/migrate_db.py --dry-run

Steps
  1. copy data/{ETF}.db (and data/books.db if present) to data/backup/<timestamp>/
  2. create a fresh data/{ETF}.db with the current schema; copy ONLY snapshots + holdings;
     derive weekly_closes from holdings (last snapshot of each ISO week); VACUUM
     — the retired per-ETF paper-trading tables are not carried over
  3. rebuild data/books.db: replay the chosen strategy session by session from the first date
     after its warm-up, persisting the book (positions, trades, equity, rising stars) and the
     sessions / signals audit tables exactly as the daily runner would have
Run cli/report_rebuild.py afterwards to regenerate the HTML from the new databases.
"""
import argparse
import datetime
import logging
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.config import ETF_CONFIG, INITIAL_CAPITAL, MAX_PURCHASE_AMOUNT, MAX_POSITIONS, RISK_PER_TRADE, COMMISSION_PER_TRADE
from src.database import get_db, default_db_path, rebuild_weekly_closes, LEGACY_TABLES
from src.market_data import load_market_bars
from src.strategies import get_strategy, available, DEFAULT_STRATEGY
from src.engine import Engine, EngineConfig, MaxPositionsPolicy
from src.book import Book, log_session, log_signals

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("migrate")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
BOOKS_DB = DATA_DIR / "books.db"


def backup(paths, dry_run: bool) -> Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = DATA_DIR / "backup" / stamp
    for p in paths:
        if p.exists():
            log.info("backup %s -> %s", p.name, dest / p.name)
            if not dry_run:
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest / p.name)
    return dest


def rebuild_etf_db(etf: str, old_path: Path, dry_run: bool) -> dict:
    old = sqlite3.connect(old_path)
    tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    n_snap = old.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    n_hold = old.execute("SELECT COUNT(*) FROM holdings").fetchone()[0]
    legacy = sorted(tables & set(LEGACY_TABLES))
    log.info("[%s] %d snapshots, %d holdings rows; dropping legacy tables %s", etf, n_snap, n_hold, legacy or "none")
    if dry_run:
        old.close()
        return {"snapshots": n_snap, "holdings": n_hold}
    new_path = old_path.with_suffix(".new")
    new_path.unlink(missing_ok=True)
    new = get_db(new_path)                      # current schema + indexes
    new.execute("ATTACH DATABASE ? AS old", (str(old_path),))
    new.execute("INSERT INTO snapshots (fetch_date, fetched_at) SELECT fetch_date, fetched_at FROM old.snapshots ORDER BY fetch_date")
    new.execute("""INSERT INTO holdings (snapshot_date, ticker, name, sector, weight_pct, market_value, shares, price,
                                         currency, exchange, location)
                   SELECT snapshot_date, UPPER(TRIM(ticker)), name, sector, weight_pct, market_value, shares, price,
                          currency, exchange, location
                   FROM old.holdings WHERE TRIM(ticker) != '' ORDER BY snapshot_date, ticker""")
    new.commit()
    new.execute("DETACH DATABASE old")
    n_week = rebuild_weekly_closes(new)
    new.execute("VACUUM")
    new.commit()
    stats = {"snapshots": new.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
             "holdings": new.execute("SELECT COUNT(*) FROM holdings").fetchone()[0],
             "weekly_rows": n_week,
             "weeks": new.execute("SELECT COUNT(DISTINCT week_ending) FROM weekly_closes").fetchone()[0]}
    new.close(); old.close()
    old_path.unlink()
    new_path.rename(old_path)
    log.info("[%s] rebuilt: %s (%.1f MB)", etf, stats, old_path.stat().st_size / 1e6)
    return stats


def replay_strategy(name: str, conns, args) -> None:
    t0 = time.time()
    strategy = get_strategy(name)
    cfg = EngineConfig(max_positions=args.max_positions, max_amount=args.max_purchase_amount,
                       risk_per_trade=args.risk_per_trade, commission=COMMISSION_PER_TRADE,
                       policy=MaxPositionsPolicy(mode=args.max_positions_policy), rising_priority=args.rising_priority)
    bars = load_market_bars(conns)
    eng = Engine(strategy, conns, cfg, bars=bars)
    start = args.replay_from or eng.dates[min(strategy.warmup_days, len(eng.dates) - 1)]
    dates = [d for d in eng.dates if d >= start]
    log.info("[%s] replaying %d sessions %s -> %s", name, len(dates), dates[0], dates[-1])
    if args.dry_run:
        return
    bc = sqlite3.connect(BOOKS_DB)
    Book.delete(bc, name)
    bc.execute("DELETE FROM sessions WHERE strategy=?", (name,))
    bc.execute("DELETE FROM signals WHERE strategy=?", (name,))
    bc.commit()
    book = Book(name, args.capital)
    for d in dates:
        res = eng.step(book, d, fill="same", execute=True)
        log_session(bc, name, d, res, book)
        log_signals(bc, name, d, eng.signal_rows(book, d))
    book.save(bc)
    closed = [t for t in book.trades if t.side == "sell"]
    log.info("[%s] done in %.0fs: %d trades (%d closed, win %.0f%%), %d open, %d rising stars, equity $%.2f (%+.2f%%)",
             name, time.time() - t0, len(book.trades), len(closed),
             100 * sum(t.pnl > 0 for t in closed) / max(1, len(closed)), len(book.positions), len(book.rising),
             book.total_value(), 100 * (book.total_value() / book.initial_capital - 1))
    bc.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", default=DEFAULT_STRATEGY, help="strategy to replay, or 'all'")
    ap.add_argument("--replay-from", default=None, help="first session date to replay (default: after warm-up)")
    ap.add_argument("--no-replay", action="store_true", help="migrate the ETF databases only")
    ap.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    ap.add_argument("--max-positions", type=int, default=MAX_POSITIONS)
    ap.add_argument("--max-positions-policy", choices=["skip", "replace"], default="skip")
    ap.add_argument("--max-purchase-amount", type=float, default=MAX_PURCHASE_AMOUNT)
    ap.add_argument("--risk-per-trade", type=float, default=RISK_PER_TRADE)
    ap.add_argument("--rising-priority", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    etf_paths = {e: default_db_path(e) for e in sorted(ETF_CONFIG) if default_db_path(e).exists()}
    if not etf_paths:
        log.error("no ETF databases in %s", DATA_DIR); sys.exit(1)

    # 1. backup
    dest = backup(list(etf_paths.values()) + [BOOKS_DB], args.dry_run)
    # 2. rebuild ETF databases
    for etf, path in etf_paths.items():
        rebuild_etf_db(etf, path, args.dry_run)
    # 3. books: fresh file, replay sessions
    if not args.dry_run:
        BOOKS_DB.unlink(missing_ok=True)
    if not args.no_replay:
        conns = {e: get_db(p) for e, p in etf_paths.items()}
        for name in (available() if args.strategy == "all" else [args.strategy]):
            replay_strategy(name, conns, args)
        for c in conns.values():
            c.close()
    log.info("migration %s. Backup in %s. Next: python cli/report_rebuild.py --etf ALL",
             "previewed (dry run)" if args.dry_run else "complete", dest)


if __name__ == "__main__":
    main()
