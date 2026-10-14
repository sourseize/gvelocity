"""
velocity backtest — replay one or all strategies over the stored history and compare them.

  velocity backtest                                  # all strategies, 3/6/9-month windows
  velocity backtest --strategy trend_follow --windows 3,6
  velocity backtest --start 2026-02-13 --end 2026-08-13 --fill next
  velocity backtest --max-positions 40 --max-positions-policy replace
  velocity backtest --param breakout_len=40 --strategy trend_follow
  velocity backtest --out analysis/compare.md        # markdown + csv + png
  velocity backtest --strategy trend_follow --windows 6 --save-book
                                                          # register the replayed trades as the live book

Every strategy is run with the same bars, capital, $ cap per issuer, position limit and
commission, and all books are marked to market at the window's last date.

Every run writes an HTML results page (summary per window, equity chart, trade lists and the
exact command line) to report/backtest/<strategy>.html (+ .json) — override with --html.

--save-book (one strategy, one window) REPLACES that strategy's book in data/books.db with the
backtest's positions, trades and equity curve, so the next `velocity trading --paper-trade` session
carries on from the backtest's end date.  The replaced book is backed up as JSON first.
No network access is needed: everything runs on the stored ETF history.
"""
import base64
import datetime as _dt
import html
import io
import json
import shlex
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from ..book import Book
from ..config import INITIAL_CAPITAL, MAX_PURCHASE_AMOUNT, MAX_POSITIONS, RISK_PER_TRADE, COMMISSION_PER_TRADE
from ..engine import Engine, EngineConfig, MaxPositionsPolicy
from ..exceptions import VelocityError
from ..market_data import load_market_bars
from ..reporter import generate_hub_index, CSS, ticker_link, fmt_f
from ..strategies import get_strategy, available, parse_params
from ._common import (add_strategy_args, add_report_dir_arg, positive, fraction, iso_date, check_strategy_params,
                      check_report_dir, open_etf_dbs, default_book_db)

HELP = "Replay one or all strategies over the stored history; optionally register the result as the live book"


def month_windows(dates, months, end=None):
    end = end or dates[-1]
    out = []
    for m in months:
        start_dt = (pd.Timestamp(end) - pd.DateOffset(months=m)).strftime("%Y-%m-%d")
        start = next((d for d in dates if d >= start_dt), dates[0])
        out.append((f"{m}m", start, end))
    return out


def summarize(label, book, n_days, warmup_note=""):
    tv = pd.DataFrame([vars(t) for t in book.trades])
    sells = tv[tv.side == "sell"] if len(tv) else pd.DataFrame(columns=["pnl", "hold_days"])
    wins, losses = sells[sells.pnl > 0], sells[sells.pnl <= 0]
    eq = pd.Series([e["value"] for e in book.equity]) if book.equity else pd.Series([book.initial_capital])
    dd = (eq / eq.cummax() - 1).min()
    daily = eq.pct_change().dropna()
    peak = max((e["value"] - e["cash"]) for e in book.equity) if book.equity else 0.0
    total = book.realized() + book.unrealized()
    return {
        "strategy": label,
        "trades": len(tv), "trades/day": round(len(tv) / max(n_days, 1), 2),
        "closed": len(sells), "win rate": f"{(len(wins) / len(sells)):.0%}" if len(sells) else "n/a",
        "med hold (d)": int(sells.hold_days.median()) if len(sells) else 0,
        "avg win $": round(wins.pnl.mean()) if len(wins) else 0,
        "avg loss $": round(losses.pnl.mean()) if len(losses) else 0,
        "payoff": round(wins.pnl.mean() / abs(losses.pnl.mean()), 2) if len(wins) and len(losses) and losses.pnl.mean() else "n/a",
        "expectancy $/trade": round(sells.pnl.mean(), 1) if len(sells) else 0,
        "realized $": round(book.realized()), "unrealized $": round(book.unrealized()), "total P&L $": round(total),
        "open": len(book.positions), "peak $ deployed": round(peak),
        "P&L / peak deployed": f"{total / peak:+.1%}" if peak else "n/a",
        "max drawdown": f"{dd:.2%}", "sharpe": round(daily.mean() / daily.std() * np.sqrt(252), 2) if daily.std() else "n/a",
    }


def command_line() -> str:
    """The command as typed, for the report (so a result can be reproduced)."""
    return shlex.join(["velocity"] + sys.argv[1:])


def equity_chart_png(curves: dict, windows, engines, names) -> str:
    """Equity curves as a base64 PNG ('' when matplotlib is unavailable)."""
    try:
        import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    except Exception:
        return ""
    fig, axes = plt.subplots(1, len(windows), figsize=(6 * len(windows), 4.2), squeeze=False)
    for ax, (wlabel, start, end) in zip(axes[0], windows):
        for n in names:
            c = curves.get((wlabel, n))
            if c is not None:
                ax.plot(range(len(c)), c.values, label=engines[n].strategy.label)
        ax.axhline(0, color="grey", lw=0.5); ax.set_title(f"{wlabel}: {start} → {end}"); ax.set_ylabel("P&L $")
        ax.set_xlabel("sessions"); ax.legend(fontsize=8)
    plt.tight_layout()
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=110); plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def trades_table(trades) -> str:
    rows = ""
    for t in trades:
        cls = "pos" if t.side == "buy" else "neg"
        pnl = f"<span class='{'pos' if t.pnl >= 0 else 'neg'}'>{t.pnl:+.0f}</span>" if t.side == "sell" else "—"
        rows += (f"<tr><td>{t.date}</td><td><b>{ticker_link(t.ticker)}</b></td><td class='{cls}'>{t.side.upper()}</td>"
                 f"<td>{fmt_f(t.price)}</td><td>{fmt_f(t.value, 0)}</td><td>{pnl}</td><td>{t.hold_days or '—'}</td>"
                 f"<td style='max-width:420px;text-transform:none'>{html.escape(t.reason)}</td><td>{', '.join(t.etfs)}</td></tr>")
    if not rows:
        rows = "<tr><td colspan='9' class='none'>No trades</td></tr>"
    return ("<div class='tw'><table><tr><th>Date</th><th>Ticker</th><th>Side</th><th>Price</th><th>Value</th><th>P&L</th>"
            f"<th>Held</th><th>Reason</th><th>ETFs</th></tr>{rows}</table></div>")


def write_html(path: Path, df, books: dict, windows, engines, names, args, cfg, png: str, saved: dict | None) -> None:
    """The backtest results page: settings + command, one summary table per window, equity chart,
    trade lists.  A .json sidecar with the same facts sits next to it (read by the hub page)."""
    ran_at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cmd = command_line()
    sections = ""
    for w in df.window.unique():
        sub = df[df.window == w].drop(columns=["window"]).set_index("strategy").T
        head = "".join(f"<th>{html.escape(str(c))}</th>" for c in sub.columns)
        body = "".join(f"<tr><td><b>{html.escape(str(idx))}</b></td>" +
                       "".join(f"<td style='text-transform:none'>{html.escape(str(v))}</td>" for v in row) + "</tr>"
                       for idx, row in sub.iterrows())
        sections += f"<h2>{html.escape(w)}</h2><div class='tw'><table><tr><th></th>{head}</tr>{body}</table></div>"
    chart = f"<h2>Equity curves</h2><img src='data:image/png;base64,{png}' style='max-width:100%'>" if png else ""
    trades = ""
    for (wlabel, n), book in books.items():
        trades += (f"<details><summary>{html.escape(wlabel)} · {html.escape(engines[n].strategy.label)} — "
                   f"{len(book.trades)} trades, {len(book.positions)} open at the end</summary>{trades_table(book.trades)}</details>")
    banner = ""
    if saved:
        banner = (f"<div class='etfcard'><h2>Book registered</h2><div class='hubrow' style='text-transform:none'>"
                  f"This run's trades and positions now form the <a href='../strategy/{saved['strategy']}.html'>{saved['strategy']} book</a> "
                  f"(last session {saved['last_date']}); the previous book was backed up to <code>{html.escape(saved['backup'])}</code>.</div></div>")
    settings = (f"fill={args.fill} · capital ${args.capital:,.0f} · ${cfg.max_amount:,.0f}/issuer · risk {cfg.risk_per_trade:.2%}/trade · "
                f"max {cfg.max_positions} positions ({cfg.policy.mode}, rising priority {'on' if cfg.rising_priority else 'off'}) · "
                f"params {html.escape(str(next(iter(engines.values())).strategy.params_dict())) if len(engines) == 1 else 'defaults'}")
    page = f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Velocity — backtest {html.escape(path.stem)}</title><style>{CSS}</style></head><body><div class="wrap">
<h1>Backtest — {html.escape(', '.join(engines[n].strategy.label for n in names))}</h1>
<p class="sub">ran {ran_at} · <a href="../index.html">← All ETFs</a></p>
<div class="etfcard"><h2>Command</h2><div class="hubrow"><code style="text-transform:none;user-select:all">{html.escape(cmd)}</code></div>
<div class="hubrow" style="text-transform:none">{settings}</div></div>
{banner}{sections}{chart}
<h2>Trades</h2>{trades}
</div></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps({
        "ran_at": ran_at, "command": cmd, "strategies": names,
        "windows": [{"label": w, "start": s0, "end": e0} for w, s0, e0 in windows],
        "settings": {"fill": args.fill, "capital": args.capital, "max_amount": cfg.max_amount, "risk_per_trade": cfg.risk_per_trade,
                     "max_positions": cfg.max_positions, "policy": cfg.policy.mode, "rising_priority": cfg.rising_priority,
                     "params": parse_params(args.param)},
        "results": df.to_dict(orient="records"), "saved_book": saved}, indent=2, default=str))


def save_book(book: Book, engine, args, report_root: Path, start: str, end: str) -> dict:
    """Register the backtest book as the strategy's live book in data/books.db (backing up the old one)."""
    book_path = default_book_db()
    conn = sqlite3.connect(book_path)
    old = Book.load(conn, book.strategy)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_dir = book_path.parent / "book-backups"; backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"{book.strategy}-{stamp}.json"
    backup.write_text(json.dumps(old.to_dict(), indent=2))
    if old.last_date or old.trades:
        print(f"\nreplacing the existing {book.strategy} book (last session {old.last_date}, {len(old.trades)} trades, "
              f"{len(old.positions)} open) — backed up to {backup}")
    conn.execute("DELETE FROM sessions WHERE strategy=?", (book.strategy,))
    conn.execute("DELETE FROM signals WHERE strategy=?", (book.strategy,))
    # replay once more with the audit trail switched on, so `sessions` matches the registered book
    book = engine.backtest(start, end, fill=args.fill, capital=args.capital, log_conn=conn)
    book.note = f"Book registered from backtest on {stamp[:8]} ({start} → {end}, fill={args.fill}) · {command_line()}"
    book.save(conn)
    conn.close()
    book.write_report(report_root / "strategy", engine.strategy.label)
    print(f"registered {book.strategy} book: {len(book.trades)} trades, {len(book.positions)} open, "
          f"equity ${book.total_value():,.0f}, last session {book.last_date} → {report_root / 'strategy' / (book.strategy + '.html')}")
    return {"strategy": book.strategy, "last_date": book.last_date, "backup": str(backup), "trades": len(book.trades),
            "open": len(book.positions), "equity": book.total_value()}


def configure(p) -> None:
    add_strategy_args(p, allow_all=True)
    p.add_argument("--windows", default="3,6,9", help="trailing windows in months (default 3,6,9)")
    p.add_argument("--start", type=iso_date, help="YYYY-MM-DD (custom window instead of --windows)")
    p.add_argument("--end", type=iso_date, help="YYYY-MM-DD")
    p.add_argument("--fill", choices=["same", "next"], default="same",
                   help="same = fill at the signal day's close (default; what the live session does); "
                        "next = fill at the next session's close")
    p.add_argument("--capital", type=positive(float), default=INITIAL_CAPITAL)
    p.add_argument("--max-positions", type=positive(int), default=MAX_POSITIONS)
    p.add_argument("--max-positions-policy", choices=["skip", "replace"], default="skip")
    p.add_argument("--max-purchase-amount", type=positive(float), default=MAX_PURCHASE_AMOUNT)
    p.add_argument("--risk-per-trade", type=fraction, default=RISK_PER_TRADE)
    p.add_argument("--rising-priority", choices=["on", "off"], default=None,
                   help="give waiting rising stars priority for free slots (default: engine default)")
    p.add_argument("--out", default=None, help="write markdown (+csv of trades, +png chart) here")
    p.add_argument("--trades", action="store_true", help="print the trade list of each run")
    add_report_dir_arg(p)
    p.add_argument("--html", default=None, help="results page path (default: <report-dir>/backtest/<strategy>.html)")
    p.add_argument("--save-book", action="store_true",
                   help="register this backtest's trades/positions as the strategy's live book in data/books.db "
                        "(one strategy, one window; the previous book is backed up)")


def validate(p, args) -> None:
    if args.save_book:
        if args.strategy == "all":
            p.error("--save-book needs a single --strategy")
        if not (args.start or args.end) and len(args.windows.split(",")) != 1:
            p.error("--save-book needs a single window: --windows N or --start/--end")
    if not (args.start or args.end):
        try:
            args.months = [int(x) for x in args.windows.split(",")]
        except ValueError:
            p.error(f"--windows expects comma-separated month counts, got {args.windows!r}")
        if any(m < 1 for m in args.months):
            p.error("--windows: month counts must be >= 1")
    if args.start and args.end and args.start > args.end:
        p.error(f"--start {args.start} is after --end {args.end}")
    args.params = check_strategy_params(p, args)
    check_report_dir(p, args)


def run(args) -> int:
    params, report_root = args.params, Path(args.report_dir)
    conns = open_etf_dbs()
    bars = load_market_bars(conns)
    names = available() if args.strategy == "all" else [args.strategy]
    cfg = EngineConfig(max_positions=args.max_positions, max_amount=args.max_purchase_amount,
                       risk_per_trade=args.risk_per_trade, commission=COMMISSION_PER_TRADE,
                       policy=MaxPositionsPolicy(mode=args.max_positions_policy), short_history="error")
    if args.rising_priority:
        cfg.rising_priority = args.rising_priority == "on"
    engines = {n: Engine(get_strategy(n, **params), conns, cfg, bars=bars) for n in names}
    dates = next(iter(engines.values())).dates

    if args.start or args.end:
        start, end = args.start or dates[0], args.end or dates[-1]
        if end < dates[0] or start > dates[-1]:
            raise VelocityError(f"window {start} → {end} is outside the stored history {dates[0]} → {dates[-1]}")
        windows = [("custom", start, end)]
    else:
        windows = month_windows(dates, args.months)

    rows, curves, all_trades, books = [], {}, [], {}
    for wlabel, start, end in windows:
        n_days = sum(1 for d in dates if start <= d <= end)
        first_idx = dates.index(start)
        for n, eng in engines.items():
            book = eng.backtest(start, end, fill=args.fill, capital=args.capital)
            books[(wlabel, n)] = book
            warm = max(0, eng.strategy.warmup() - first_idx)
            r = summarize(eng.strategy.label, book, n_days)
            r = {"window": f"{wlabel} ({start} → {end}, {n_days}d)", **r,
                 "note": f"first {warm} sessions inside warm-up" if warm else ""}
            rows.append(r)
            curves[(wlabel, n)] = pd.DataFrame(book.equity).set_index("date")["value"] - args.capital if book.equity else None
            for t in book.trades:
                all_trades.append({"window": wlabel, "strategy": n, **vars(t)})
            if args.trades:
                print(f"\n--- {wlabel} {eng.strategy.label} trades ---")
                tv = pd.DataFrame([vars(t) for t in book.trades])
                if len(tv):
                    tv["etfs"] = tv.etfs.map(",".join)
                    print(tv[["date", "side", "ticker", "price", "value", "pnl", "hold_days", "reason", "etfs"]].round(2).to_string(index=False))

    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
    df = pd.DataFrame(rows)
    for w in df.window.unique():
        sub = df[df.window == w].drop(columns=["window"]).set_index("strategy").T
        print(f"\n=== {w}  fill={args.fill}  capital=${args.capital:,.0f}  cap=${args.max_purchase_amount:,.0f}/issuer  "
              f"max {args.max_positions} positions ({args.max_positions_policy}, rising priority {'on' if cfg.rising_priority else 'off'}) ===")
        print(sub.to_string())

    saved = None
    if args.save_book:
        (wlabel, n), book = next(iter(books.items()))
        if not book.equity:
            raise VelocityError("nothing to register: the backtest produced no sessions")
        saved = save_book(book, engines[n], args, report_root, windows[0][1], windows[0][2])

    html_path = Path(args.html) if args.html else report_root / "backtest" / f"{args.strategy}.html"
    png = equity_chart_png(curves, windows, engines, names)
    write_html(html_path, df, books, windows, engines, names, args, cfg, png, saved)
    print(f"\nwrote {html_path} (+ .json) — command recorded: {command_line()}")
    if html_path.parent.parent == report_root or saved:
        (report_root / "index.html").write_text(generate_hub_index(report_root), encoding="utf-8")

    if args.out:
        out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
        md = [f"# Strategy comparison — fill={args.fill}, capital ${args.capital:,.0f}, ${args.max_purchase_amount:,.0f}/issuer, "
              f"max {args.max_positions} positions ({args.max_positions_policy})\n"]
        for w in df.window.unique():
            sub = df[df.window == w].drop(columns=["window"]).set_index("strategy").T
            md += [f"\n## {w}\n", sub.to_markdown()]
        out.write_text("\n".join(md))
        pd.DataFrame(all_trades).to_csv(out.with_suffix(".csv"), index=False)
        try:
            import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
            fig, axes = plt.subplots(1, len(windows), figsize=(6 * len(windows), 4.2), squeeze=False)
            for ax, (wlabel, start, end) in zip(axes[0], windows):
                for n in names:
                    c = curves.get((wlabel, n))
                    if c is not None:
                        ax.plot(range(len(c)), c.values, label=engines[n].strategy.label)
                ax.axhline(0, color="grey", lw=0.5); ax.set_title(f"{wlabel}: {start} → {end}"); ax.set_ylabel("P&L $")
                ax.set_xlabel("sessions"); ax.legend(fontsize=8)
            plt.tight_layout(); fig.savefig(out.with_suffix(".png"), dpi=110)
            print(f"\nwrote {out}, {out.with_suffix('.csv')}, {out.with_suffix('.png')}")
        except Exception as ex:
            print(f"\nwrote {out}, {out.with_suffix('.csv')} (chart skipped: {ex})")
    return 0
