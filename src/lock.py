"""
Instance lock — one process at a time may use the database.

`velocity serve` holds the lock for its whole lifetime; every CLI command that touches the database
takes it for the duration of the command.  So, while the web service runs, the CLI refuses with a
clear message (stop the service first), and `serve` refuses to start while a CLI command is in the
middle of its work.  The lock is an `flock` on `data/velocity.lock`: the kernel releases it when
the holder exits, however it exits — no stale locks.  The file's content (holder, pid, since)
is informational, for the error message and `velocity status`.
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
from pathlib import Path
from typing import Optional

from .config import DB_PATH

LOCK_PATH = DB_PATH.with_name("velocity.lock")


class LockHeld(Exception):
    """Somebody else holds the lock; `holder` describes them (may be None when unreadable)."""
    def __init__(self, holder: Optional[dict]):
        self.holder = holder
        super().__init__(describe(holder))


def describe(holder: Optional[dict]) -> str:
    if not holder:
        return "another velocity process is using the database"
    who = holder.get("holder", "?")
    since = holder.get("since", "")[:19].replace("T", " ")
    if who == "serve":
        return f"velocity serve is running (pid {holder.get('pid')}, since {since} UTC) — stop it before using the CLI"
    return f"another velocity command is running: `{who}` (pid {holder.get('pid')}, since {since} UTC) — wait for it to finish"


class InstanceLock:
    def __init__(self, holder: str, path: Path = LOCK_PATH):
        self.holder = holder
        self.path = path
        self._fh = None

    def acquire(self) -> "InstanceLock":
        """Take the lock or raise LockHeld (never waits)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise LockHeld(current_holder(self.path)) from None
        fh.seek(0); fh.truncate()
        json.dump({"holder": self.holder, "pid": os.getpid(),
                   "since": _dt.datetime.now(_dt.timezone.utc).isoformat()}, fh)
        fh.flush()
        self._fh = fh
        return self

    def release(self) -> None:
        if self._fh is not None:
            try:
                self._fh.seek(0); self._fh.truncate(); self._fh.flush()
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


def current_holder(path: Path = LOCK_PATH) -> Optional[dict]:
    """Who holds the lock right now (None when nobody does)."""
    if not path.exists():
        return None
    try:
        with open(path, "r") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.seek(0)
                try:
                    return json.loads(fh.read() or "{}") or {"holder": "?"}
                except json.JSONDecodeError:
                    return {"holder": "?"}
            fcntl.flock(fh, fcntl.LOCK_UN)
            return None
    except OSError:
        return None
