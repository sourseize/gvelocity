#!/usr/bin/env python3
"""
ticker.py — replay a single ticker's confirmation score over time (CLI Tool)
=============================================================================

Replays compute_metrics() for every date a ticker has appeared in the holdings,
regardless of whether it was in the top-20 that day. Updates the tickers index
and manifest.json so every ticker you've checked stays linked and browsable.

Usage (run from the velocity directory):
  python cli/ticker.py NVDA --etf IWMO
  python cli/ticker.py AAPL --etf IWQU --html /custom/path.html
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import ETF_CONFIG, SCORE_META, DEFAULT_REPORT_DIR
from src.database import get_db, default_db_path, etf_report_dir
from src.analyzer import compute_metrics
from src.reporter import fmt_pct, fmt_f, ticker_link, score_badge


def build_html(ticker: str, history: list, generated_at: str) -> str:
    """Generate HTML for a single ticker's history page."""
    rows = ""
    for entry in history:
        d = entry["date"]
        m = entry["metrics"]
        score = entry["score"]
        roc20 = m.get("price_roc_20d")
        wpct = m.get("weight_percentile")
        dd = m.get("drawdown_from_52w_high")

        def fnum(v, width, prec=1, suffix=""):
            if v is None:
                return f"{'—':<{width}}"
            return f"{v:<{width}.{prec}f}{suffix}"

        rows += f"""<tr>
            <td>{d}</td>
            <td><span class="score-{min(5, max(0, score))}">{score}</span></td>
            <td>{fnum(m.get('current_weight'), 10, 3)}</td>
            <td>{fnum((roc20*100 if roc20 is not None else None), 10, 1)}</td>
            <td>{fnum(wpct, 10, 0)}</td>
            <td>{fnum((dd*100 if dd is not None else None), 8, 1)}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{ticker} — Velocity History</title>
<style>
body {{ font-family: sans-serif; margin: 20px; background: #0f172a; color: #f8fafc; }}
h1 {{ color: #3b82f6; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 20px; }}
th, td {{ border: 1px solid #334155; padding: 8px; text-align: left; }}
th {{ background: #1e293b; color: #94a3b8; }}
.score-5 {{ color: #10b981; font-weight: bold; }}
.score-4 {{ color: #22c55e; font-weight: bold; }}
.score-3 {{ color: #f59e0b; }}
.score-2 {{ color: #ef4444; }}
.score-1 {{ color: #ef4444; }}
.score-0 {{ color: #6b7280; }}
</style>
</head>
<body>
<h1>{ticker} — Confirmation Score History</h1>
<p>Generated: {generated_at}</p>
<table>
<tr><th>Date</th><th>Score</th><th>Weight%</th><th>20dROC%</th><th>WtPctile</th><th>52wDD%</th></tr>
{rows}
</table>
</body>
</html>"""


def build_index_html(manifest: dict) -> str:
    """Generate index.html listing all tracked tickers."""
    items = ""
    for ticker, data in sorted(manifest.items()):
        items += f"""<div style="margin-bottom: 10px;">
            <a href="{ticker}.html">{ticker}</a> —
            {data.get('name', '')} |
            First: {data.get('first_seen', '?')} |
            Last: {data.get('last_date', '?')} |
            Score: {data.get('last_score', '?')}/5
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tracked Tickers — Velocity</title>
<style>
body {{ font-family: sans-serif; margin: 20px; background: #0f172a; color: #f8fafc; }}
h1 {{ color: #3b82f6; }}
</style>
</head>
<body>
<h1>Tracked Tickers ({len(manifest)})</h1>
{items}
</body>
</html>"""


def get_ticker_dates(conn, ticker: str) -> list[str]:
    """Get all dates a ticker has appeared in the holdings."""
    rows = conn.execute(
        "SELECT DISTINCT snapshot_date FROM holdings WHERE ticker=? ORDER BY snapshot_date",
        (ticker,),
    ).fetchall()
    return [r["snapshot_date"] for r in rows]


def load_manifest(tickers_dir: Path) -> dict:
    """Load the manifest.json file that tracks which tickers have been processed."""
    mpath = tickers_dir / "manifest.json"
    if mpath.exists():
        return json.loads(mpath.read_text(encoding="utf-8"))
    return {}


def save_manifest(tickers_dir: Path, manifest: dict) -> None:
    """Save the manifest.json file."""
    mpath = tickers_dir / "manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def fmt_ts(iso_ts: str) -> str:
    """Format ISO timestamp for display."""
    if not iso_ts:
        return "—"
    try:
        dt = datetime.datetime.fromisoformat(iso_ts)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso_ts


def main():
    """Main entry point."""
    p = argparse.ArgumentParser(
        description="Replay a ticker's confirmation score over time (MANUAL TOOL)",
        epilog="Examples:\n"
               "  python cli/ticker.py NVDA --etf IWMO\n"
               "  python cli/ticker.py AAPL --etf IWQU --html /custom/path.html\n",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("ticker", help="Ticker symbol to analyze")
    p.add_argument("--etf", required=True, choices=sorted(ETF_CONFIG.keys()),
                    help="Which tracked ETF this ticker belongs to. Required.")
    p.add_argument("--db", default=None,
                    help="DB path override. Default: data/{ETF}.db")
    p.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR),
                    help="Shared report root. Default: velocity/report")
    p.add_argument("--html", default=None,
                    help="Write to custom path instead of {ETF}/tickers/{TICKER}.html")
    args = p.parse_args()

    db_path = Path(args.db) if args.db else default_db_path(args.etf)
    conn = get_db(db_path)
    ticker = args.ticker.upper()
    dates = get_ticker_dates(conn, ticker)
    if not dates:
        print(f"No holdings history found for {ticker}")
        sys.exit(1)

    print(f"{ticker} — {len(dates)} trading days on record (first seen {dates[0]})\n")
    print(f"{'Date':<12}{'Score':<7}{'Weight%':<10}{'Price':<10}{'20dROC%':<10}{'WtPctile':<10}{'52wDD%':<8}")

    history = []
    for d in dates:
        m = compute_metrics(conn, ticker, d)
        score = m["confirmation_score"]
        history.append({"date": d, "score": score, "metrics": m})

        roc20 = m.get("price_roc_20d")
        wpct = m.get("weight_percentile")
        dd = m.get("drawdown_from_52w_high")
        print(
            f"{d:<12}{score:<7}"
            f"{fmt_f(m.get('current_weight'), 10, 3)}"
            f"{fmt_f((roc20*100 if roc20 is not None else None), 10, 1)}"
            f"{fmt_f(wpct, 10, 0)}"
            f"{fmt_f((dd*100 if dd is not None else None), 8, 1)}"
        )

    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if args.html:
        # Custom path — write and stop, don't update index
        out_path = Path(args.html)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(build_html(ticker, history, fmt_ts(generated_at)), encoding="utf-8")
        print(f"\nWrote {out_path}")
        conn.close()
        return

    # Default: report_dir/{ETF}/tickers/{TICKER}.html
    fund_dir = etf_report_dir(Path(args.report_dir), args.etf)
    tickers_dir = fund_dir / "tickers"
    tickers_dir.mkdir(parents=True, exist_ok=True)

    out_path = tickers_dir / f"{ticker}.html"
    out_path.write_text(build_html(ticker, history, fmt_ts(generated_at)), encoding="utf-8")
    print(f"\nWrote {out_path}")

    last = history[-1]
    manifest = load_manifest(tickers_dir)
    manifest[ticker] = {
        "name": last["metrics"].get("name", ticker),
        "first_seen": history[0]["date"],
        "last_date": last["date"],
        "last_score": last["score"],
        "n_days": len(history),
        "generated_at": generated_at,
    }
    save_manifest(tickers_dir, manifest)

    index_path = tickers_dir / "index.html"
    index_path.write_text(build_index_html(manifest), encoding="utf-8")
    print(f"Updated {index_path} ({len(manifest)} ticker(s) tracked)")

    conn.close()


if __name__ == "__main__":
    main()