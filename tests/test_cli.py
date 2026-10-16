"""Tests for the `velocity <command>` entry point.  Run:  python -m pytest tests -q"""
import subprocess, sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
needs_db = pytest.mark.skipif(not (DATA / "velocity.db").exists(), reason="no database present")


def velocity(*args, timeout=300):
    r = subprocess.run([sys.executable, str(ROOT / "velocity.py"), *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def test_help_lists_every_command():
    rc, out = velocity("--help")
    assert rc == 0
    for cmd in ("backtest", "backfill", "ticker", "strategies", "resetbook", "status", "serve"):
        assert cmd in out
    for gone in ("rebuild", "\n    fetch ", "\n    trading ", "--etf", "alpaca", "velocity trading --paper-trade"):
        assert gone not in out, gone            # no removed verbs, no docstring block


def test_no_command_prints_help_and_exits_2():
    rc, out = velocity()
    assert rc == 2 and "commands:" in out


def test_unknown_command_is_usage_error():
    rc, out = velocity("frobnicate")
    assert rc == 2 and "invalid choice" in out


def test_strategies_is_numbered_and_has_no_alias():
    rc, out = velocity("strategies")
    assert rc == 0 and " 1. double_bottom" in out and " 3. trend_follow" in out and "(default)" in out
    assert velocity("list-strategies")[0] == 2
    assert "list-strategies" not in velocity("--help")[1]


def test_bad_input_is_rejected_before_any_work():
    for args, expect in [
        (["serve", "--capital", "0"], "must be > 0"),
        (["serve", "--port", "70000"], "between 1 and 65535"),
        (["serve", "--etf", "ALL"], "unrecognized arguments"),
        (["trading", "--paper-trade"], "invalid choice"),
        (["backtest", "--save-book"], "single --strategy"),
        (["backtest", "--windows", "x"], "comma-separated"),
        (["backtest", "--start", "2026/01/01"], "YYYY-MM-DD"),
        (["backfill", "--months", "0"], ">= 1"),
        (["resetbook", "--strategy", "nope"], "invalid choice"),
        (["rebuild"], "invalid choice"),
        (["fetch"], "invalid choice"),
        (["reset-book", "--strategy", "trend_follow"], "invalid choice"),
        (["serve", "--poll-frequency", "0"], "must be > 0"),
        (["serve", "--param", "breakout_len=abc"], "expects an integer"),
    ]:
        rc, out = velocity(*args)
        assert rc == 2, (args, out)
        assert expect in out, (args, out)
        assert "Traceback" not in out


def test_no_command_fallback_is_gone():
    rc, out = velocity("--offline", "--no-trade")
    assert rc == 2 and "Traceback" not in out


@needs_db
def test_backtest_prints_header_with_command_and_writes_no_files():
    before = {p for p in ROOT.rglob("*") if p.suffix in (".html", ".json", ".png")}
    rc, out = velocity("backtest", "--strategy", "double_bottom", "--windows", "2")
    assert rc == 0, out
    assert "velocity backtest --strategy double_bottom --windows 2" in out
    assert "Velocity backtest — Double Top / Bottom" in out and "2m (" in out
    after = {p for p in ROOT.rglob("*") if p.suffix in (".html", ".json", ".png")}
    assert after == before, "backtest must not write report files"


@needs_db
def test_ticker_unknown_symbol_is_clean_error():
    rc, out = velocity("ticker", "ZZZZZ")
    assert rc == 1 and "no price history" in out and "Traceback" not in out


def test_cli_is_refused_while_the_lock_is_held(tmp_path):
    """Strict offline rule: any verb that needs the database is refused while `velocity serve`
    (or another command) holds the instance lock; `strategies` and `status` still work."""
    import subprocess as sp, time
    holder = sp.Popen([sys.executable, "-c",
                       "import sys, time; sys.path.insert(0, %r); from src.lock import InstanceLock\n"
                       "with InstanceLock('serve'):\n    print('held', flush=True); time.sleep(30)" % str(ROOT)],
                      stdout=sp.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        for args in (["backtest", "--windows", "2"], ["ticker", "AAPL"],
                     ["resetbook", "--strategy", "trend_follow"], ["backfill", "--dry-run"], ["serve", "--once"]):
            rc, out = velocity(*args)
            assert rc == 1 and "velocity serve is running" in out and "Traceback" not in out, (args, out)
        rc, out = velocity("status")
        assert rc == 0 and "velocity serve is running" in out
        assert velocity("strategies")[0] == 0
    finally:
        holder.kill(); holder.wait()
    rc, out = velocity("status")
    assert rc == 0 and "not running" in out


@needs_db
def test_status_reports_history_and_books():
    rc, out = velocity("status")
    assert rc == 0 and "holdings         : 321 sessions" in out and "holdings updated :" in out and "books" in out
