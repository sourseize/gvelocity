"""Tests for the `velocity <command>` entry point.  Run:  python -m pytest tests -q"""
import subprocess, sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
needs_db = pytest.mark.skipif(not (DATA / "MTUM.db").exists(), reason="no ETF databases present")


def velocity(*args, timeout=300):
    r = subprocess.run([sys.executable, str(ROOT / "velocity.py"), *args], capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


def test_help_lists_every_command():
    rc, out = velocity("--help")
    assert rc == 0
    for cmd in ("trading", "fetch", "backtest", "backfill", "rebuild", "ticker", "strategies", "reset-book"):
        assert cmd in out


def test_no_command_prints_help_and_exits_2():
    rc, out = velocity()
    assert rc == 2 and "commands:" in out


def test_unknown_command_is_usage_error():
    rc, out = velocity("frobnicate")
    assert rc == 2 and "invalid choice" in out


def test_strategies_is_numbered_and_aliases_work():
    rc, out = velocity("strategies")
    assert rc == 0 and " 1. double_bottom" in out and " 3. trend_follow" in out and "(default)" in out
    assert velocity("list-strategies")[0] == 0
    assert velocity("--list-strategies")[0] == 0          # legacy flag


def test_bad_input_is_rejected_before_any_work():
    for args, expect in [
        (["trading", "--etf", "ALL", "--offline", "--param", "sma_len=abc"], "expects an integer"),
        (["trading", "--etf", "ALL", "--capital", "0"], "must be > 0"),
        (["trading", "--etf", "ALL", "--paper-trade", "--no-trade"], "not allowed with"),
        (["trading", "--etf", "ALL", "--alpaca-trade"], "requires --paper-trade"),
        (["trading", "--etf", "MTUM", "--offline", "--mock-csv", "x.csv"], "exclusive"),
        (["backtest", "--save-book"], "single --strategy"),
        (["backtest", "--windows", "x"], "comma-separated"),
        (["backtest", "--start", "2026/01/01"], "YYYY-MM-DD"),
        (["backfill", "--etf", "MTUM", "--months", "0"], ">= 1"),
        (["reset-book", "--strategy", "nope"], "invalid choice"),
    ]:
        rc, out = velocity(*args)
        assert rc == 2, (args, out)
        assert expect in out, (args, out)
        assert "Traceback" not in out


@needs_db
def test_legacy_invocation_maps_to_trading(tmp_path):
    rc, out = velocity("--etf", "ALL", "--offline", "--no-trade", "--report-dir", str(tmp_path))
    assert rc == 0 and "assuming `velocity trading`" in out and "Traceback" not in out


@needs_db
def test_backtest_writes_html_with_command(tmp_path):
    rc, out = velocity("backtest", "--strategy", "double_bottom", "--windows", "2", "--report-dir", str(tmp_path))
    assert rc == 0, out
    page = (tmp_path / "backtest" / "double_bottom.html").read_text()
    assert "velocity backtest --strategy double_bottom --windows 2" in page
    assert (tmp_path / "backtest" / "double_bottom.json").exists()


@needs_db
def test_ticker_unknown_symbol_is_clean_error(tmp_path):
    rc, out = velocity("ticker", "ZZZZZ", "--report-dir", str(tmp_path))
    assert rc == 1 and "no price history" in out and "Traceback" not in out
