"""
velocity ticker — replay a strategy's signal state for one ticker over its whole history

  velocity ticker NVDA                          # default strategy
  velocity ticker NVDA --strategy double_bottom
  velocity ticker NVDA --html /tmp/NVDA.html    # custom output path (skips index update)

Writes report/tickers/{TICKER}.html and updates report/tickers/index.html + manifest.json.
"""
import datetime
import json
from pathlib import Path

from ..engine import Engine
from ..exceptions import VelocityError
from ..market_data import load_market_bars
from ..reporter import CSS, state_badge, checks_html, fmt_f
from ..strategies import get_strategy, parse_params
from ._common import add_strategy_args, add_report_dir_arg, check_strategy_params, check_report_dir, open_etf_dbs

HELP = "Replay a strategy's signal state for one ticker over its whole history"


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


def configure(p) -> None:
    p.add_argument("ticker")
    add_strategy_args(p)
    add_report_dir_arg(p)
    p.add_argument("--html", default=None, help="custom output path (skips index update)")


def validate(p, args) -> None:
    if not args.ticker.strip():
        p.error("ticker must not be empty")
    check_strategy_params(p, args)
    if not args.html:
        check_report_dir(p, args)


def run(args) -> int:
    conns = open_etf_dbs()
    strategy = get_strategy(args.strategy, **parse_params(args.param))
    eng = Engine(strategy, conns, bars=load_market_bars(conns))
    t = args.ticker.strip().upper()
    if t not in eng.ind.tickers:
        raise VelocityError(f"{t}: no price history in the tracked ETFs")
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
        return 0
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
    return 0
