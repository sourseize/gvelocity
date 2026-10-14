"""velocity fetch — store today's holdings of the tracked ETFs (step 1 of `trading`, on its own)."""
import logging
import re
import sqlite3
from pathlib import Path
from typing import Optional

import requests

from ..config import ETF_CONFIG
from ..database import get_db, default_db_path, store_snapshot
from ..exceptions import NoDataForDateError, VelocityError
from ..fetcher import fetch_csv, parse_csv
from ._common import add_etf_arg, check_db_arg, check_etf_dbs, etfs_of

log = logging.getLogger(__name__)
HELP = "Fetch and store today's holdings (no session, no reports)"


def configure(p) -> None:
    add_etf_arg(p, required=True, help="Which ETF to fetch, or ALL")
    p.add_argument("--db", default=None, help="DB path override (single --etf only)")
    p.add_argument("--mock-csv", help="Use a local CSV instead of fetching (single --etf only)")


def validate(p, args) -> None:
    if args.etf == "ALL" and args.mock_csv:
        p.error("--mock-csv needs a single --etf")
    if args.mock_csv:
        path = Path(args.mock_csv)
        if not path.is_file():
            p.error(f"--mock-csv: {path} is not a readable file")
    check_db_arg(p, args)
    check_etf_dbs(p, etfs_of(args))


def describe_error(e: Exception) -> str:
    """One readable line for a requests failure (the raw message is several hundred chars)."""
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code} {e.response.reason}"
    if isinstance(e, requests.Timeout):
        return "request timed out"
    if isinstance(e, requests.ConnectionError):
        inner = e.args[0] if e.args else e
        reason = str(getattr(inner, "reason", None) or inner)
        reason = re.sub(r"<urllib3\.[^>]*>: ", "", reason)          # drop the '<HTTPSConnection object at 0x..>: ' noise
        return f"connection failed ({reason})"
    return f"{type(e).__name__}: {e}"


def fetch_etf(etf: str, db: Optional[str], mock_csv: Optional[str]) -> Optional[str]:
    """Fetch + store today's holdings for one ETF.  Returns the holdings date, or None when nothing
    new could be stored (no data yet / network problem) — callers then work on the stored history.
    A bad --mock-csv or an unusable database is the user's problem and raises VelocityError."""
    db_path = Path(db) if db else default_db_path(etf)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = get_db(db_path)
    except (sqlite3.Error, OSError) as e:
        raise VelocityError(f"[{etf}] cannot open holdings database {db_path}: {e}") from e
    try:
        if mock_csv:
            try:
                raw = Path(mock_csv).read_text(encoding="utf-8-sig")
                hdate, rows = parse_csv(raw)
            except (OSError, UnicodeDecodeError) as e:
                raise VelocityError(f"--mock-csv {mock_csv}: cannot read file ({e})") from e
            except NoDataForDateError as e:
                raise VelocityError(f"--mock-csv {mock_csv}: no holdings data in file ({e})") from e
            except ValueError as e:
                raise VelocityError(f"--mock-csv {mock_csv}: not a holdings CSV ({e})") from e
        else:
            try:
                hdate, rows = fetch_csv(ETF_CONFIG[etf]["url"], etf)
            except NoDataForDateError as e:
                log.warning("[%s] no holdings data available yet (%s) — retried next cycle", etf, e)
                return None
            except requests.RequestException as e:
                log.error("[%s] holdings fetch failed: %s — using stored history, retried next cycle", etf, describe_error(e))
                return None
            except ValueError as e:
                log.error("[%s] holdings CSV could not be parsed (%s) — using stored history, retried next cycle", etf, e)
                return None
        try:
            added = store_snapshot(conn, hdate, rows)
        except sqlite3.Error as e:
            raise VelocityError(f"[{etf}] cannot store holdings in {db_path}: {e}") from e
        log.info("[%s] holdings %s %s", etf, hdate, "stored" if added else "already stored")
        return hdate.isoformat()
    finally:
        conn.close()


def fetch_all(etfs, db: Optional[str], mock_csv: Optional[str]) -> dict:
    """{etf: holdings date} for the ETFs that stored (or already had) today's holdings."""
    fetched = {}
    for etf in etfs:
        d = fetch_etf(etf, db, mock_csv)
        if d:
            fetched[etf] = d
    if not fetched:
        log.warning("no holdings stored this run (%s)", ", ".join(etfs))
    return fetched


def run(args) -> int:
    fetched = fetch_all(etfs_of(args), args.db, args.mock_csv)
    return 0 if fetched else 1
