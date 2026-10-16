"""
holdings service — fetch the fund's latest holdings CSV and store it; record every attempt.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from typing import Optional

import requests

from ..config import HOLDINGS_URL
from ..database import store_snapshot, log_fetch
from ..exceptions import NoDataForDateError, VelocityError
from ..fetcher import fetch_csv

log = logging.getLogger(__name__)


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


def fetch_and_store(conn: sqlite3.Connection) -> Optional[str]:
    """Fetch + store the latest holdings; every attempt is recorded in `fetch_log`.  Returns the
    holdings date, or None when nothing could be stored (no data yet / network problem) —
    callers then work on the stored history.  An unusable database raises VelocityError."""
    t0 = time.monotonic()
    ms = lambda: int((time.monotonic() - t0) * 1000)
    try:
        hdate, rows = fetch_csv(HOLDINGS_URL)
    except NoDataForDateError as e:
        log_fetch(conn, "no-data", ms=ms(), message=str(e))
        log.warning("no holdings data available yet (%s) — retried next poll", e)
        return None
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        log_fetch(conn, "http-error", http_status=status, ms=ms(), message=describe_error(e))
        log.error("holdings fetch failed: %s — retried next poll", describe_error(e))
        return None
    except requests.RequestException as e:
        log_fetch(conn, "connection-error", ms=ms(), message=describe_error(e))
        log.error("holdings fetch failed: %s — retried next poll", describe_error(e))
        return None
    except ValueError as e:
        log_fetch(conn, "parse-error", ms=ms(), message=str(e))
        log.error("holdings CSV could not be parsed (%s) — retried next poll", e)
        return None
    try:
        added = store_snapshot(conn, hdate, rows)
    except sqlite3.Error as e:
        raise VelocityError(f"cannot store holdings: {e}") from e
    outcome = "stored" if added else "already"
    log_fetch(conn, outcome, holdings_date=hdate.isoformat(), ms=ms(), message=f"{len(rows)} rows")
    log.info("holdings %s %s (%d rows)", hdate, "stored" if added else "already stored", len(rows))
    return hdate.isoformat()


def last_fetches(conn: sqlite3.Connection, n: int = 5) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT attempted_at, outcome, holdings_date, http_status, ms, message FROM fetch_log ORDER BY id DESC LIMIT ?", (n,))]
