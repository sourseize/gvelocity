"""
velocity backfill — one-off historical populator

Pulls the fund's historical holdings via BlackRock's `asOfDate` CSV parameter for the trailing
N months (default 12) and stores them in the database, one trading day at a time, oldest first.
Nothing else is derived here: reports are computed from the database on demand.

Usage:
  velocity backfill                      # last 12 months (fills gaps, keeps existing rows)
  velocity backfill --months 6
  velocity backfill --dry-run            # show dates, fetch nothing
  velocity backfill --wipe               # start over: delete stored holdings first

Existing data is KEPT by default: dates already in the DB are not re-requested, only the gaps
are fetched.  --wipe is opt-in and refuses to delete history older than the window being
re-fetched unless --force is given (the strategies need 60+ bars of warm-up; a short re-fill
silently starves them).
"""

import argparse
import datetime
import logging
import time
from typing import Optional

from ..config import HOLDINGS_URL, FETCH_HEADERS
from ..database import store_snapshot, wipe_holdings
from ..exceptions import NoDataForDateError, VelocityError
from ..fetcher import parse_csv
from ._common import iso_date, check_db, open_db

log = logging.getLogger(__name__)
HELP = "Fetch N months of historical holdings into the database (keeps stored rows, fills gaps)"
def fetch_csv_for_date(base_url: str, date_obj: datetime.date, retries: int = 3, backoff: float = 5.0):
    """Fetch+parse the fund's holdings CSV as of a given calendar date.
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


def backfill(conn, args) -> Optional[str]:
    """Runs the full backfill. Returns the last stored date, or None if nothing was stored."""
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

    log.info("Backfill range: %s → %s (%d weekdays)", start_date, end_date, len(dates))

    if args.dry_run:
        for d in dates:
            print(d.isoformat())
        log.info("Dry run — no requests made")
        return None

    existing = [r[0] for r in conn.execute("SELECT fetch_date FROM snapshots ORDER BY fetch_date")]

    if args.wipe:
        if existing and existing[0] < start_date.isoformat() and not args.force:
            raise VelocityError(
                f"refusing to wipe: the database holds {len(existing)} sessions back to {existing[0]}, but this run "
                f"only re-fetches from {start_date} (--months {args.months}) — the strategies would lose warm-up "
                f"history. Re-run with a larger --months, without --wipe (fills gaps only), or add --force to wipe anyway.")
        log.info("Wiping existing snapshots/holdings/weekly_closes rows …")
        wipe_holdings(conn)
        existing = []
    elif existing:
        log.info("keeping %d stored sessions (%s → %s); only missing dates are requested (--wipe to start over)",
                 len(existing), existing[0], existing[-1])

    fetched, skipped = 0, 0
    last_stored_date = existing[-1] if existing else None
    already = set(existing)

    for i, d in enumerate(dates, 1):
        if d.isoformat() in already:
            continue
        hdate, rows = fetch_csv_for_date(HOLDINGS_URL, d)
        if not hdate:
            skipped += 1
            time.sleep(args.sleep)
            continue

        try:
            if store_snapshot(conn, hdate, rows):
                fetched += 1
            today = hdate.isoformat()
            if last_stored_date is None or today > last_stored_date:
                last_stored_date = today
        except Exception:
            log.exception("Failed to store %s (requested %s) — continuing", hdate, d)
            skipped += 1

        if i % 20 == 0:
            log.info("Progress: %d/%d requested dates processed", i, len(dates))

        time.sleep(args.sleep)

    log.info("Fetch pass complete: %d trading days stored, %d requests skipped/failed", fetched, skipped)

    if not last_stored_date:
        log.error("Nothing was stored")
        return None
    if not fetched:
        log.info("nothing new to fetch — history already complete up to %s", last_stored_date)
    log.info("Backfill done.")
    return last_stored_date


def configure(p) -> None:
    p.add_argument("--months", type=int, default=12, help="How many trailing months to backfill (default: 12)")
    p.add_argument("--end-date", type=iso_date, default=None, help="YYYY-MM-DD end of range, default today (auto-clamped)")
    p.add_argument("--sleep", type=float, default=1.5, help="Seconds between requests (default: 1.5)")
    p.add_argument("--wipe", action="store_true",
                   help="Delete the stored holdings history before fetching (default: keep it, fill gaps)")
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
    check_db(p)


def run(args) -> int:
    conn = open_db()
    try:
        stored = backfill(conn, args)
    finally:
        conn.close()
    if args.dry_run:
        return 0
    if not stored:
        raise VelocityError("nothing was stored")
    log.info("Run `velocity serve` from here on.")
    return 0
