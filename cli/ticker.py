#!/usr/bin/env python3
"""
ticker.py — replay a strategy's signal state for one ticker over its whole history (CLI tool)

  python cli/ticker.py NVDA                          # default strategy
  python cli/ticker.py NVDA --strategy double_bottom
  python cli/ticker.py NVDA --html /tmp/NVDA.html    # custom output path (skips index update)

Writes report/tickers/{TICKER}.html and updates report/tickers/index.html + manifest.json.
"""
import argparse
import datetime
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.config import ETF_CONFIG, DEFAULT_REPORT_DIR
from src.database import default_db_path
from src.market_data import load_market_bars
from src.strategies import get_strategy, available, parse_params, DEFAULT_STRATEGY
from src.engine import Engine
from src.reporter import CSS, state_badge, checks_html, fmt_f


def build_html(ticker: str, strategy, history: list, generated_at: str) -> str:
    rows = "".join(
        f"<tr><td>{h['date']}</td><td>{fmt_f(h['close'])}</td><td>{state_badge(h['state'])}</td>"
        f"<td><div class='sigs'>{checks_html(h['checks'], h['note'])}</div></td></tr>"
        for h in reversed(history))
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>{ticker} — {strategy.label}</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>{ticker} — {strategy.label} signal history</h1>
<p class="sub">{strategy.description}<br>Generated {generated_at} · <a href="index.html">← tracked tickers</a></p>
<div class="tw"><table><tr><th>Date</th><th>Close</th><th>State</th><th>Checks</th></tr>{rows}</table></div>
</div></body></html>"""


def main():
    p = argparse.ArgumentParser(description="Replay a strategy's signal state for one ticker (MANUAL TOOL)")
    p.add_argument("ticker")
    p.add_argument("--strategy", choices=available(), default=DEFAULT_STRATEGY)
    p.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    p.add_argument("--html", default=None, help="custom output path (skips index update)")
    args = p.parse_args()

    conns = {e: sqlite3.connect(default_db_path(e)) for e in sorted(ETF_CONFIG) if default_db_path(e).exists()}
    strategy = get_strategy(args.strategy, **parse_params(args.param))
    eng = Engine(strategy, conns, bars=load_market_bars(conns))
    t = args.ticker.upper()
    if t not in eng.ind.tickers:
        print(f"{t}: no price history in the tracked ETFs")
        sys.exit(1)
    closes = eng.ind.close[t].dropna()
    history = []
    print(f"{'Date':<12}{'Close':>10}  {'State':<7} checks")
    for d, c in closes.items():
        ex = strategy.explain(d, t)
        history.append({"date": d, "close": float(c), **ex})
        if ex["state"] != "NONE":
            ok = ", ".join(k for k, v in ex["checks"].items() if v)
            print(f"{d:<12}{c:>10.2f}  {ex['state']:<7} {ok[:90]} {ex.get('note','')}")
    print(f"\n{t}: {len(history)} sessions · {sum(h['state']=='ENTRY' for h in history)} ENTRY · "
          f"{sum(h['state']=='EXIT' for h in history)} EXIT · {sum(h['state']=='WATCH' for h in history)} WATCH")

    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if args.html:
        out = Path(args.html); out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(build_html(t, strategy, history, generated_at), encoding="utf-8")
        print(f"wrote {out}")
        return
    tdir = Path(args.report_dir) / "tickers"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / f"{t}.html").write_text(build_html(t, strategy, history, generated_at), encoding="utf-8")
    mpath = tdir / "manifest.json"
    manifest = json.loads(mpath.read_text()) if mpath.exists() else {}
    last = history[-1]
    manifest[t] = {"strategy": strategy.name, "first": history[0]["date"], "last": last["date"], "state": last["state"]}
    mpath.write_text(json.dumps(manifest, indent=2))
    items = "".join(f"<div class='hubrow'><a href='{k}.html'>{k}</a> · {v['strategy']} · {v['first']} → {v['last']} · {state_badge(v['state'])}</div>"
                    for k, v in sorted(manifest.items()))
    (tdir / "index.html").write_text(f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>Tracked tickers</title>
<style>{CSS}</style></head><body><div class="wrap"><h1>Tracked tickers ({len(manifest)})</h1>
<p class="sub"><a href="../index.html">← hub</a></p>{items}</div></body></html>""", encoding="utf-8")
    print(f"wrote {tdir / (t + '.html')} and index")


if __name__ == "__main__":
    main()
