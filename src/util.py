"""Small shared helpers."""
import datetime as _dt
from typing import Optional


def utcnow() -> str:
    """UTC timestamp, ISO-8601 with seconds — the format used everywhere in the database."""
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def fmt_utc(iso: Optional[str], default: str = "—") -> str:
    """'2026-10-15T20:46:14+00:00' (or without offset / with microseconds) -> '2026-10-15 20:46:14 UTC'."""
    if not iso:
        return default
    try:
        d = _dt.datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if d.tzinfo is not None:
        d = d.astimezone(_dt.timezone.utc)
    return d.strftime("%Y-%m-%d %H:%M:%S UTC")
