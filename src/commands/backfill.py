"""
velocity backfill — one-off historical populator

Pulls a configured ETF's historical holdings via BlackRock's `asOfDate` CSV
parameter for the trailing N months (default 12), stores them in that ETF's
own SQLite DB, then replays the day-by-day analysis/report pipeline so the
monthly HTML archive and JSON logs come out looking exactly as if Velocity
had been running the whole time. Also regenerates the top-level hub page.

IMPORTANT — why this isn't a bulk fetch-then-analyze:
Velocity's new-entry/exit/mover detection (detect_new_entries, detect_exits,
recent_dates, etc.) always looks at "the N most recent dates
in the whole snapshots table" — it does NOT filter by "on or before the date
being analyzed". That's fine for live daily cron (today IS the most recent row),
but it means analysis MUST happen immediately after each day's snapshot is
stored, in strict chronological order, before the next day's snapshot goes in.
This script does exactly that: fetch → store → analyze → log, one day at a time,
oldest first.

Usage:
  velocity backfill --etf MTUM           # backfill MTUM, last 12 months (fills gaps, keeps existing rows)
  velocity backfill --etf ALL            # backfill all configured ETFs
  velocity backfill --etf MTUM --months 6
  velocity backfill --etf MTUM --dry-run # show dates, fetch nothing
  velocity backfill --etf MTUM --wipe    # start over: delete stored rows + report archive first

Existing data is KEPT by default: dates already in the DB are not re-requested, only the gaps
are fetched.  --wipe is opt-in and refuses to delete history older than the window being
re-fetched unless --force is given (the strategies need 60+ bars of warm-up; a short re-fill
silently starves them).
"""

import argparse
import datetime
import logging
import time
from pathlib import Path
from typing import Optional

from ..config import ETF_CONFIG, FETCH_HEADERS
from ..database import get_db, default_db_path, etf_report_dir, store_snapshot
from ..day_data import build_day_data
from ..exceptions import NoDataForDateError, VelocityError
from ..fetcher import parse_csv
from ..reporter import (get_all_months, load_month_log, generate_monthly_page, generate_hub_index, append_day_to_log)
from ._common import add_etf_arg, add_report_dir_arg, iso_date, check_db_arg, check_report_dir, check_etf_dbs, etfs_of

log = logging.getLogger(__name__)
HELP = "Fetch N months of historical holdings into the ETF databases (keeps stored rows, fills gaps)"


def fetch_csv_for_date(base_url: str, date_obj: datetime.date, retries: int = 3, backoff: float = 5.0):
    """Fetch+parse an ETF's holdings CSV as of a given calendar date.
    Returns (holdings_date, rows) or (None, None) on failure/no data.
    """
    import requests
    url = f"{base_url}&asOfDate={date_obj:%Y%m%d}"
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=FETCH_HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning("Network error attempt %d/%d for %s: %s", attempt, retries, date_obj, e)
            if attempt < retries:
                time.sleep(backoff)
                continue
            log.error("Giving up on %s after %d network-error attempts", date_obj, retries)
            return None, None

        try:
            hdate, rows = parse_csv(resp.text)
        except NoDataForDateError as e:
            # Expected for weekends/holidays/dates outside BlackRock's retention window
            log.info("No data for %s: %s", date_obj, e)
            return None, None
        except ValueError as e:
            log.warning("Parse error for %s: %s — skipping", date_obj, e)
            return None, None
        except Exception as e:  # unexpected payload (HTML, binary, encoding) must not abort the run
            log.error("Unexpected error parsing %s: %s: %s — skipping (first 200 chars: %r)",
                      date_obj, type(e).__name__, e, resp.text[:200])
            return None, None

        return hdate, rows

    return None, None


def business_days(start: datetime.date, end: datetime.date) -> list[datetime.date]:
    """Return list of business days (Mon-Fri) between start and end inclusive."""
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
            days.append(d)
        d += datetime.timedelta(days=1)
    return days


def wipe_existing(conn, fund_dir: Path) -> None:
    """Wipe existing data from database and report files."""
    log.info("Wiping existing snapshots/holdings/weekly_closes rows …")
    conn.execute("DELETE FROM holdings")
    conn.execute("DELETE FROM snapshots")
    conn.execute("DELETE FROM weekly_closes")
    conn.commit()

    removed = 0
    for pattern in ("????-??.json", "????-??.html"):
        for p in fund_dir.glob(pattern):
            p.unlink()
            removed += 1
    idx = fund_dir / "index.html"
    if idx.exists():
        idx.unlink()
        removed += 1
    log.info("Wiped DB rows and removed %d existing report file(s)", removed)


def regenerate_all_pages(conn, fund_dir: Path, latest_date: str, etf: str, etf_name: str) -> None:
    """Regenerate all monthly HTML pages."""
    all_months = get_all_months(fund_dir)
    if not all_months:
        log.error("No month logs were written — nothing to render")
        return

    live_ym = all_months[-1]
    latest_day_data = build_day_data(conn, latest_date, etf=etf)

    for idx, ym in enumerate(all_months):
        prev_ym = all_months[idx - 1] if idx > 0 else None
        next_ym = all_months[idx + 1] if idx < len(all_months) - 1 else None
        is_current = (ym == live_ym)
        entries = load_month_log(fund_dir, ym)
        html = generate_monthly_page(
            ym=ym,
            entries=entries,
            prev_ym=prev_ym,
            next_ym=next_ym,
            is_current=is_current,
            report_dir=fund_dir,
            day_data=latest_day_data if is_current else None,
            live_ym=live_ym,
            etf=etf,
            etf_name=etf_name,
        )
        filename = "index.html" if is_current else f"{ym}.html"
        (fund_dir / filename).write_text(html, encoding="utf-8")
        log.info("Wrote %s (%d day entries)", filename, len(entries))


def backfill_etf(etf: str, args, report_root: Path) -> Optional[str]:
    """Runs the full backfill for a single ETF. Returns the last stored date,
    or None if nothing was stored."""
    etf_cfg = ETF_CONFIG[etf]
    etf_url = etf_cfg["url"]
    etf_name = etf_cfg["name"]

    db_path = Path(args.db) if args.db else default_db_path(etf)
    fund_dir = etf_report_dir(report_root, etf)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fund_dir.mkdir(parents=True, exist_ok=True)

    end_date = (
        datetime.datetime.strptime(args.end_date, "%Y-%m-%d").date()
        if args.end_date else datetime.date.today()
    )

    # BlackRock publishes holdings with a lag — requesting asOfDate for today
    # (or often yesterday) is guaranteed to hit the "-" no-data placeholder.
    PUBLISH_LAG_DAYS = 2
    safe_end = datetime.date.today() - datetime.timedelta(days=PUBLISH_LAG_DAYS)
    if end_date > safe_end:
        log.warning(
            "Clamping end date from %s to %s — BlackRock publishes holdings with "
            "roughly a %d-day lag, so more recent dates will never have data yet",
            end_date, safe_end, PUBLISH_LAG_DAYS,
        )
        end_date = safe_end

    start_date = (end_date - datetime.timedelta(days=args.months * 31))
    dates = business_days(start_date, end_date)

    log.info("[%s] Backfill range: %s → %s (%d weekdays)", etf, start_date, end_date, len(dates))

    if args.dry_run:
        for d in dates:
            print(f"{etf} {d.isoformat()}")
        log.info("[%s] Dry run — no requests made", etf)
        return None

    conn = get_db(db_path)
    existing = [r[0] for r in conn.execute("SELECT fetch_date FROM snapshots ORDER BY fetch_date")]

    if args.wipe:
        if existing and existing[0] < start_date.isoformat() and not args.force:
            conn.close()
            log.error("[%s] refusing to wipe: the database holds %d sessions back to %s, but this run only re-fetches "
                      "from %s (--months %d) — the strategies would lose warm-up history. Re-run with a larger "
                      "--months, without --wipe (fills gaps only), or add --force to wipe anyway.",
                      etf, len(existing), existing[0], start_date, args.months)
            return None
        wipe_existing(conn, fund_dir)
        existing = []
    elif existing:
        log.info("[%s] keeping %d stored sessions (%s → %s); only missing dates are requested (--wipe to start over)",
                 etf, len(existing), existing[0], existing[-1])

    fetched, skipped = 0, 0
    last_stored_date = existing[-1] if existing else None
    already = set(existing)

    for i, d in enumerate(dates, 1):
        if d.isoformat() in already:
            continue
        hdate, rows = fetch_csv_for_date(etf_url, d)
        if not hdate:
            skipped += 1
            time.sleep(args.sleep)
            continue

        try:
            store_snapshot(conn, hdate, rows)
            today = hdate.isoformat()

            if today != last_stored_date:
                day_data = build_day_data(conn, today, etf=etf)
                append_day_to_log(fund_dir, day_data, commentary={})
                last_stored_date = today
                fetched += 1
        except Exception:
            log.exception("[%s] Failed to store/analyse %s (requested %s) — continuing", etf, hdate, d)
            skipped += 1

        if i % 20 == 0:
            log.info("[%s] Progress: %d/%d requested dates processed", etf, i, len(dates))

        time.sleep(args.sleep)

    log.info("[%s] Fetch pass complete: %d trading days stored, %d requests skipped/failed",
              etf, fetched, skipped)

    if not last_stored_date:
        log.error("[%s] Nothing was stored — skipping report generation for this ETF", etf)
        conn.close()
        return None
    if not fetched:
        log.info("[%s] nothing new to fetch — history already complete up to %s", etf, last_stored_date)

    regenerate_all_pages(conn, fund_dir, last_stored_date, etf, etf_name)
    conn.close()
    log.info("[%s] Backfill done.", etf)
    return last_stored_date


def configure(p) -> None:
    add_etf_arg(p, required=True, help="Which configured ETF to backfill, or ALL for every configured ETF")
    p.add_argument("--db", default=None, help="DB path override. Default: data/{ETF}.db")
    add_report_dir_arg(p)
    p.add_argument("--months", type=int, default=12, help="How many trailing months to backfill (default: 12)")
    p.add_argument("--end-date", type=iso_date, default=None, help="YYYY-MM-DD end of range, default today (auto-clamped)")
    p.add_argument("--sleep", type=float, default=1.5, help="Seconds between requests (default: 1.5)")
    p.add_argument("--wipe", action="store_true",
                   help="Delete the ETF's stored rows and report archive before fetching (default: keep them, fill gaps)")
    p.add_argument("--force", action="store_true",
                   help="With --wipe: allow deleting history that is older than the window being re-fetched")
    p.add_argument("--no-wipe", action="store_true", help=argparse.SUPPRESS)   # former default; kept as a no-op
    p.add_argument("--dry-run", action="store_true", help="Print the date list and exit, no network calls")


def validate(p, args) -> None:
    if args.no_wipe:
        log.info("--no-wipe is now the default behaviour (flag ignored)")
    if args.months < 1:
        p.error("--months must be >= 1")
    if args.sleep < 0:
        p.error("--sleep must be >= 0")
    check_db_arg(p, args)
    check_report_dir(p, args)
    check_etf_dbs(p, etfs_of(args))


def run(args) -> int:
    report_root = Path(args.report_dir)
    stored = [etf for etf in etfs_of(args) if backfill_etf(etf, args, report_root)]
    if args.dry_run:
        return 0
    if not stored:
        raise VelocityError("nothing was stored for any ETF — hub page not regenerated")
    (report_root / "index.html").write_text(generate_hub_index(report_root), encoding="utf-8")
    log.info("Hub index.html updated")
    log.info("Backfill done. Run `velocity trading --etf ALL --paper-trade` for daily runs from here on.")
    return 0
