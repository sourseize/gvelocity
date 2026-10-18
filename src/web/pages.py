"""
HTML pages of the web service — pure functions from data to an HTML string.

  hub       status strip, the book's headline numbers vs the fund, positions, the latest holdings day
  book      the performance page: equity vs fund, drawdown, exposure, positions, sector exposure,
            rolling quarters, monthly returns, trade analytics, recent trades, rising stars, sessions
  session   one session: exits / entries / not taken / rising stars / every signal / settings
  ticker    price chart with the book's trades and the stop, current checks, signal history
  signals   the signal-quality table (forward excess return per rule)
  month/day the holdings days (from html.py)
  status    poller, fetch log, sessions
"""
from __future__ import annotations

import datetime
import html
import json
from typing import Optional

from ..book import Book
from ..util import fmt_utc
from . import charts
from .html import (CSS, CHART_CSS, render_day_block, generate_monthly_page, state_badge, checks_html, fmt_f, ticker_link,
                   external_quote_link, rising_table, month_key, sc)

# reload the page when the poller has completed a new cycle (cheap: /api/health is tiny)
LIVE_JS = """<script>
(function(){var seen=null;function tick(){fetch('/api/health',{cache:'no-store'}).then(function(r){return r.json()})
.then(function(j){if(seen===null){seen=j.last_cycle_at;}else if(j.last_cycle_at!==seen){location.reload();}})
.catch(function(){});}tick();setInterval(tick,60000);})();
</script>"""

EXTRA_CSS = """
.sitenav{display:flex;flex-wrap:wrap;gap:14px;font-size:.82rem;margin:0 0 14px;padding:8px 0;border-bottom:1px solid var(--bd)}
.sitenav a{color:var(--mt);text-decoration:none}.sitenav a:hover,.sitenav a.on{color:var(--ac)}
.strip{display:flex;flex-wrap:wrap;gap:8px 22px;font-size:.78rem;color:var(--mt);margin:4px 0 16px}
.strip b{color:var(--tx);font-weight:500}
.etfcard{background:var(--sf);border:1px solid var(--bd);border-radius:14px;padding:16px 18px;margin-bottom:12px}
.etfcard h2{margin:0 0 6px;font-size:1.02rem}
.hubrow{font-size:.82rem;margin-top:6px;color:var(--mt)}
.chip{display:inline-block;background:var(--bg);border:1px solid var(--bd);border-radius:999px;padding:2px 8px;margin:2px 4px 2px 0;font-size:.76rem;color:var(--tx)}
.ok{color:var(--pos)}.warn{color:#f59e0b}.err{color:var(--neg)}
"""


def pct(v, plus=True, dp=1) -> str:
    if v is None or v != v:
        return "—"
    return f"{v:+.{dp}%}" if plus else f"{v:.{dp}%}"


def cls(v) -> str:
    return "pos" if (v or 0) >= 0 else "neg"


def page(title: str, body: str, live: bool = False) -> str:
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>'
            f'<style>{CSS}{CHART_CSS}{EXTRA_CSS}</style></head><body><div class="wrap">{body}'
            f'<footer style="margin-top:18px;color:var(--mt);font-size:.75rem">Velocity · rendered {fmt_utc(datetime.datetime.now(datetime.timezone.utc).isoformat())}</footer>'
            f'</div>{LIVE_JS if live else ""}</body></html>')


def site_nav(active: str, current_ym: Optional[str], strategy: Optional[dict]) -> str:
    items = [("/", "Home", "home")]
    if current_ym:
        items.append((f"/month/{current_ym}", datetime.datetime.strptime(current_ym + "-01", "%Y-%m-%d").strftime("%B %Y"), "month"))
    items += [("/book", "Book", "book"), ("/signals", "Signal quality", "signals"), ("/status", "Status", "status"),
              ("/api/status", "API", "api")]
    return '<nav class="sitenav">' + "".join(
        f'<a href="{href}" class="{"on" if key == active else ""}">{html.escape(label)}</a>' for href, label, key in items) + "</nav>"


def status_strip(st: dict) -> str:
    parts = [f"holdings <b>{st.get('holdings_date') or '—'}</b> updated <b>{fmt_utc(st.get('holdings_updated_at'))}</b>",
             f"last poll <b>{fmt_utc(st.get('last_poll_at'))}</b>"]
    if st.get("next_poll_at") and not st.get("serve_stopped_at"):
        parts.append(f"next poll <b>{fmt_utc(st.get('next_poll_at'))}</b>")
    parts.append(f"last session <b>{st.get('last_session_date') or '—'}</b>")
    cyc, level = st.get("last_cycle_status") or "—", st.get("last_cycle_level") or "ok"
    c = {"ok": "ok", "warn": "warn", "error": "err"}.get(level, "ok")
    detail = st.get("last_poll_detail") or ""
    parts.append(f"last cycle <b class='{c}' title='{html.escape(detail)}'>{html.escape(cyc)}</b>")
    return '<div class="strip">' + " · ".join(parts) + "</div>"


def stat(value: str, label: str, c: str = "") -> str:
    return f'<div class="stat"><div class="sv {c}">{value}</div><div class="sl">{label}</div></div>'


def kpis(view: dict) -> str:
    b, p, t = view["book"], view["perf"], view["trades"]
    if not p.get("sessions"):
        return '<div class="hubrow">no sessions yet</div>'
    ret, fund = p["return"], p.get("bench_return")
    return ('<div class="kpi">' + stat(f"${fmt_f(view['equity'], 0)}", "Equity") +
            stat(pct(ret), "Return", cls(ret)) +
            (stat(pct(fund), "Fund", cls(fund)) + stat(pct(p.get("excess")), "Excess vs fund", cls(p.get("excess"))) if fund is not None else "") +
            stat(pct(p["max_drawdown"]), "Max drawdown", "neg" if p["max_drawdown"] < 0 else "") +
            stat(f"{p['sharpe']:.2f}", "Sharpe") +
            stat(pct(view["exposure"], plus=False, dp=0), "Invested") +
            stat(f"${fmt_f(view['cash'], 0)}", "Cash") +
            stat(str(len(b.positions)), "Open") + stat(str(t["closed"]), "Closed") +
            stat(pct(t["win_rate"], plus=False, dp=0), "Win rate") +
            stat(f"${fmt_f(t['realized'], 0)}", "Realized", cls(t["realized"])) +
            stat(f"${fmt_f(t['unrealized'], 0)}", "Unrealized", cls(t["unrealized"])) + "</div>")


def equity_chart(p: dict, height: int = 240) -> str:
    if not p.get("sessions"):
        return ""
    series = [("book", [(d, v - 1) for d, v in p["curve"].items()])]
    if "bench_curve" in p:
        series.append(("fund", [(d, v - 1) for d, v in p["bench_curve"].items()]))
    return charts.line_chart(series, height=height, baseline=0.0, title="Return since the book started — book vs the fund")


def book_card(view: Optional[dict], strategy: Optional[dict]) -> str:
    if not strategy:
        return '<div class="etfcard"><h2>Book</h2><div class="hubrow">starting up…</div></div>'
    if view is None:
        return f'<div class="etfcard"><h2>{html.escape(strategy["label"])} book</h2><div class="hubrow">no sessions yet</div></div>'
    book = view["book"]
    pos = "".join(f"<span class='chip'>{ticker_link(p['ticker'])} <span class='{cls(p['pnl_pct'])}'>{p['pnl_pct']:+.1%}</span></span>"
                  for p in sorted(view["positions"], key=lambda p: -p["pnl_pct"])) or "<span class='hubrow'>no open positions</span>"
    rising = "".join(f"<span class='chip'>🌱 {ticker_link(r['ticker'])} <span class='mt'>x{r['seen']}</span></span>" for r in book.rising_list()[:8])
    return f"""<div class="etfcard"><h2><a href="/book">{html.escape(strategy['label'])} book</a>
<span style="font-size:.78rem;color:var(--mt);font-weight:400;margin-left:8px">last session {book.last_date or '—'}</span></h2>
{kpis(view)}
{equity_chart(view['perf'], height=200)}
<div class="hubrow"><b>Positions ({len(book.positions)}):</b> {pos}</div>
{f"<div class='hubrow'><b>Waiting for a slot:</b> {rising}</div>" if rising else ""}
</div>"""


def hub(st: dict, strategy: Optional[dict], view: Optional[dict], latest_entry: Optional[dict], current_ym: Optional[str],
        all_months: list[str]) -> str:
    months_html = " · ".join(f'<a href="/month/{m}">{datetime.datetime.strptime(m + "-01", "%Y-%m-%d").strftime("%b %Y")}</a>'
                             for m in reversed(all_months))
    latest = render_day_block(latest_entry, is_latest=True) if latest_entry else '<p class="none">No holdings stored yet.</p>'
    body = (site_nav("home", current_ym, strategy) + "<h1>Velocity</h1>" + status_strip(st) + book_card(view, strategy) +
            f"<h2>Latest day</h2>{latest}" +
            (f'<h2>Months</h2><div class="hubrow">{months_html}</div>' if all_months else ""))
    return page("Velocity", body, live=True)


def month(ym: str, entries: list[dict], all_months: list[str], is_current: bool, day_data: Optional[dict],
          strategy: Optional[dict], current_ym: Optional[str]) -> str:
    i = all_months.index(ym)
    return generate_monthly_page(ym=ym, entries=entries, prev_ym=all_months[i - 1] if i > 0 else None,
                                 next_ym=all_months[i + 1] if i + 1 < len(all_months) else None, is_current=is_current,
                                 day_data=day_data if is_current else None, book_label=strategy["label"] if strategy else None,
                                 site_nav=site_nav("month" if is_current else "", current_ym, strategy))


def day(date: str, entry: dict, strategy: Optional[dict], current_ym: Optional[str]) -> str:
    body = (site_nav("", current_ym, strategy) + f"<h1>Velocity — {date}</h1>"
            f'<p class="sub"><a href="/month/{month_key(date)}">← month</a></p>' + render_day_block(entry, is_latest=True))
    return page(f"Velocity — {date}", body)


# ── the book page ──
def _positions_table(view: dict) -> str:
    rows = ""
    for p in view["positions"]:
        col = sc(p["sector"])
        rows += (f"<tr><td><b>{ticker_link(p['ticker'])}</b></td><td style='color:{col}'>{html.escape(p['sector'][:18])}</td>"
                 f"<td>{p['entry_date']}</td><td>{p['days']}</td><td>{fmt_f(p['entry_price'])}</td><td>{fmt_f(p['last_price'])}</td>"
                 f"<td class='{cls(p['pnl_pct'])}'>{p['pnl_pct']:+.1%}</td><td class='{cls(p['pnl'])}'>{p['pnl']:+,.0f}</td>"
                 f"<td>{p['weight']:.1%}</td><td>{fmt_f(p['stop'])}</td><td>{p['stop_distance']:.1%}</td>"
                 f"<td>{f'{p['trail']:.0%}' if p['trail'] else '—'}</td></tr>")
    if not rows:
        rows = "<tr><td colspan='12' class='none'>No open positions</td></tr>"
    return (f"<div class='tw'><table class='compact'><tr><th>Ticker</th><th>Sector</th><th>Entry</th><th>Days</th><th>Entry px</th>"
            f"<th>Last</th><th>P&L %</th><th>P&L $</th><th>Weight</th><th>Stop</th><th>To stop</th><th>Trail</th></tr>{rows}</table></div>")


def _sector_bars(view: dict) -> str:
    items = view["sector_exposure"]
    if not items:
        return "<p class='none'>nothing invested</p>"
    mx = max(w for _, w in items) or 1
    return "".join(f"<div class='mini-mover' style='display:flex;justify-content:space-between;align-items:center'>"
                   f"<span style='color:{sc(s)}'>{html.escape(s)}</span>"
                   f"<span><span class='minibar' style='width:{int(w / mx * 160)}px;background:{sc(s)}'></span> {w:.1%}</span></div>"
                   for s, w in items) + f"<div class='hubrow'>cash {1 - view['exposure']:.1%}</div>"


def _windows_table(p: dict) -> str:
    wins = p.get("windows") or []
    if not wins:
        return "<p class='none'>the book is younger than a quarter — rolling comparisons start after 63 sessions</p>"
    rows = "".join(f"<tr><td>{w['start']} → {w['end']}</td><td class='{cls(w['book'])}'>{w['book']:+.1%}</td>"
                   f"<td class='{cls(w['bench'])}'>{w['bench']:+.1%}</td><td class='{cls(w['excess'])}'>{w['excess']:+.1%}</td>"
                   f"<td>{'✅' if w['beat'] else '❌'}</td></tr>" for w in reversed(wins))
    n = sum(w["beat"] for w in wins)
    return (f"<p class='sub' style='margin-top:-6px'>beat the fund in <b>{n} of {len(wins)}</b> rolling quarters (63 sessions, stepped monthly)</p>"
            f"<div class='tw'><table class='compact'><tr><th>Quarter</th><th>Book</th><th>Fund</th><th>Excess</th><th>Beat</th></tr>{rows}</table></div>")


def _monthly_table(p: dict) -> str:
    rows = p.get("monthly") or []
    if not rows:
        return ""
    return ("<div class='tw'><table class='compact'><tr><th>Month</th><th>Book</th><th>Fund</th><th>Excess</th></tr>" +
            "".join(f"<tr><td>{m['month']}</td><td class='{cls(m['book'])}'>{m['book']:+.1%}</td><td class='{cls(m['bench'])}'>{m['bench']:+.1%}</td>"
                    f"<td class='{cls(m['book'] - m['bench'])}'>{m['book'] - m['bench']:+.1%}</td></tr>" for m in reversed(rows)) + "</table></div>")


def _trade_analytics(t: dict) -> str:
    if not t["closed"]:
        return "<p class='none'>no closed trades yet</p>"
    reasons = charts.bar_chart([(d["reason"], d["pnl"], f"{d['n']} trades · {d['wins'] / d['n']:.0%} won") for d in t["by_reason"]])
    sectors = charts.bar_chart([(d["sector"], d["pnl"] + d.get("unrealized", 0.0), f"{d['n']} closed, {d.get('open', 0)} open") for d in t["by_sector"]])
    top = "".join(f"<span class='chip'>{ticker_link(k)} <span class='{cls(v)}'>{v:+,.0f}</span></span>" for k, v in t["top"])
    bottom = "".join(f"<span class='chip'>{ticker_link(k)} <span class='{cls(v)}'>{v:+,.0f}</span></span>" for k, v in t["bottom"])
    hold = " · ".join(f"{h['label']}: <b>{h['n']}</b>" for h in t["hold_hist"])
    pf = "∞" if t["profit_factor"] == float("inf") else f"{t['profit_factor']:.2f}"
    head = ("<div class='kpi'>" + stat(str(t["closed"]), "Closed trades") + stat(pct(t["win_rate"], False, 0), "Win rate") +
            stat(f"{t['payoff']:.2f}", "Payoff (avg win / avg loss)") + stat(pf, "Profit factor") +
            stat(f"${t['expectancy']:+,.0f}", "Expectancy / trade", cls(t["expectancy"])) +
            stat(f"${t['avg_win']:,.0f}", "Avg win", "pos") + stat(f"${t['avg_loss']:,.0f}", "Avg loss", "neg") +
            stat(f"{t['median_hold']}d", "Median hold") + "</div>")
    return (head +
            f"<div class='grid2'><div><h3>P&L by exit reason</h3>{reasons}</div><div><h3>P&L by sector (realized + open)</h3>{sectors}</div></div>"
            f"<div class='hubrow'><b>Best:</b> {top}</div><div class='hubrow'><b>Worst:</b> {bottom}</div>"
            f"<div class='hubrow'><b>Holding time:</b> {hold}</div>")


def _trades_table(book: Book, n: int = 60) -> str:
    trows = ""
    for t in reversed(book.trades[-n:]):
        c = "pos" if t.side == "buy" else "neg"
        pnl = f"<span class='{cls(t.pnl)}'>{t.pnl:+,.0f}</span>" if t.side == "sell" else "—"
        trows += (f"<tr><td>{t.date}</td><td><b>{ticker_link(t.ticker)}</b></td><td class='{c}'>{t.side.upper()}</td>"
                  f"<td>{fmt_f(t.price)}</td><td>{fmt_f(t.value, 0)}</td><td>{pnl}</td><td>{t.hold_days if t.side == 'sell' else ''}</td>"
                  f"<td style='max-width:420px'>{html.escape(t.reason)}</td></tr>")
    if not trows:
        trows = "<tr><td colspan='8' class='none'>No trades yet</td></tr>"
    return (f"<div class='tw'><table class='compact'><tr><th>Date</th><th>Ticker</th><th>Side</th><th>Price</th><th>Value</th>"
            f"<th>P&L</th><th>Held</th><th>Reason</th></tr>{trows}</table></div>")


def book_page(view: dict, strategy: dict, sessions: list[dict], current_ym: Optional[str], settings: dict) -> str:
    book, p, t = view["book"], view["perf"], view["trades"]
    srows = "".join(
        f"<tr><td><a href='/session/{s['date']}'>{s['date']}</a></td><td>{s['kind']}</td>"
        f"<td>{s['n_entries']}</td><td>{s['n_exits']}</td><td>{s['n_skipped']}</td><td>{s['n_open']}</td>"
        f"<td>{fmt_f(s['equity'], 0)}</td><td>{fmt_utc(s['run_at'])}</td></tr>" for s in sessions[:120])
    sess = (f"<details><summary style='cursor:pointer;color:var(--mt);font-size:.85rem'>Sessions ({len(sessions)})</summary>"
            f"<div class='tw'><table class='compact'><tr><th>Date</th><th>Kind</th><th>Entries</th><th>Exits</th><th>Not taken</th>"
            f"<th>Open</th><th>Equity</th><th>Run at</th></tr>{srows or '<tr><td colspan=8 class=none>No sessions yet</td></tr>'}</table></div></details>")
    warn = ""
    if p.get("sessions", 0) and p["sessions"] < 21:
        warn = (f"<div class='warnbox'>The book is {p['sessions']} session(s) old — returns, drawdown and Sharpe are not meaningful yet. "
                f"Use <code>velocity backtest</code> for the strategy's history.</div>")
    charts_html = ""
    if p.get("sessions"):
        charts_html = (equity_chart(p) +
                       "<div class='grid2'>" +
                       charts.area_chart([(d, v) for d, v in p["drawdown"].items()], title="Drawdown from the book's peak", hi=0.0, width=560, height=150) +
                       charts.area_chart([(d, v) for d, v in p["exposure"].items()], title="Share of equity invested", color="#a78bfa", lo=0.0, hi=1.0, width=560, height=150) +
                       "</div>")
    engine = settings.get("engine") or {}
    params = settings.get("params") or {}
    setup = (f"{engine.get('max_positions', '—')} positions × {(1 - engine.get('cash_reserve', 0)) / max(engine.get('max_positions', 1), 1):.1%} of equity · "
             f"cash reserve {engine.get('cash_reserve', 0):.0%} · policy {(engine.get('policy') or {}).get('mode', '—')} · "
             f"fill at the holdings price of the signal day" if engine else "—")
    body = (site_nav("book", current_ym, strategy) +
            f"<h1>{html.escape(strategy['label'])} — book</h1>"
            f"<p class='sub'>{html.escape(strategy['description'])}<br><small>{setup} · last session {book.last_date or '—'}"
            f"{' · ' + html.escape(book.note) if book.note else ''}</small></p>" + warn + kpis(view) + charts_html +
            f"<h2>Open positions ({len(book.positions)})</h2>{_positions_table(view)}"
            f"<div class='grid2'><div><h3>Sector exposure (share of equity)</h3>{_sector_bars(view)}</div>"
            f"<div><h3>Risk</h3><div class='hubrow'>open risk to stops: <b>{view['open_risk']:.1%}</b> of equity if every position closed at its stop</div>"
            f"<div class='hubrow'>cash ${fmt_f(view['cash'], 0)} of which reserve ${fmt_f(view['equity'] * engine.get('cash_reserve', 0), 0)} · "
            f"beta to fund {p.get('beta', 0):.2f} · correlation {p.get('correlation', 0):.2f} · volatility {p.get('volatility', 0):.1%}</div></div></div>"
            f"<h2>Rolling quarters vs the fund</h2>{_windows_table(p)}"
            f"<h2>Monthly returns</h2>{_monthly_table(p) or '<p class=none>—</p>'}"
            f"<h2>Trade analytics</h2>{_trade_analytics(t)}"
            f"<h2>Recent trades</h2>{_trades_table(book)}" +
            rising_table(book.rising_list(), "Signals waiting for a slot", collapsed=True) +
            f"<h2>History</h2>{sess}"
            f"<details><summary style='cursor:pointer;color:var(--mt);font-size:.8rem'>Strategy parameters</summary>"
            f"<pre style='font-size:.75rem;color:var(--mt)'>{html.escape(json.dumps(params, indent=1))}</pre></details>")
    return page(f"Velocity — {strategy['label']} book", body, live=True)


def session(strategy: dict, row: dict, signals: dict, current_ym: Optional[str]) -> str:
    def trades(items, c):
        return "".join(f"<div class='mini-exit'><span class='{c}' style='font-weight:700'>{html.escape(side)}</span> "
                       f"<b>{ticker_link(x.get('ticker', ''))}</b> <span class='ml'>@ {fmt_f(x.get('ref_price'))}</span> "
                       f"<span class='ml'>{html.escape(str(x.get('reason', '')))}</span></div>"
                       for side, x in items) or "<p class='none'>none</p>"
    entries = [("BUY", x) for x in json.loads(row["entries_json"])]
    exits = [("SELL", x) for x in json.loads(row["exits_json"])]
    skipped = [("SKIP", x) for x in json.loads(row["skipped_json"])]
    order = {"ENTRY": 0, "EXIT": 1, "HOLD": 2, "WATCH": 3}
    srows = "".join(f"<tr><td><b>{ticker_link(t)}</b></td><td>{state_badge(s['state'])}</td>"
                    f"<td><div class='sigs'>{checks_html(s['checks'], s['note'])}</div></td></tr>"
                    for t, s in sorted(signals.items(), key=lambda kv: (order.get(kv[1]["state"], 9), kv[0])))
    params = json.loads(row.get("params_json") or "{}"); eng = json.loads(row.get("engine_json") or "{}")
    body = (site_nav("", current_ym, strategy) +
            f"<h1>Session {row['date']}</h1>"
            f"<p class='sub'>{row['kind']} · run {fmt_utc(row['run_at'])} · <a href='/book'>← book</a> · "
            f"<a href='/day/{row['date']}'>holdings that day →</a></p>"
            f"<div class='stats'>{stat('$' + fmt_f(row['equity'], 0), 'Equity')}{stat('$' + fmt_f(row['cash'], 0), 'Cash')}"
            f"{stat(str(row['n_open']), 'Open')}{stat(str(row['n_rising']), 'Waiting')}</div>"
            f"<h2>Exits ({len(exits)})</h2>{trades(exits, 'neg')}<h2>Entries ({len(entries)})</h2>{trades(entries, 'pos')}"
            f"<h2>Not taken ({len(skipped)})</h2>{trades(skipped, 'mt')}"
            + rising_table(json.loads(row.get("rising_json") or "[]"), f"Signals waiting after {row['date']}", collapsed=True) +
            f"<h2>Signals ({len(signals)})</h2><div class='tw'><table><tr><th>Ticker</th><th>State</th><th>Checks</th></tr>"
            f"{srows or '<tr><td colspan=3 class=none>none stored</td></tr>'}</table></div>"
            f"<details><summary style='cursor:pointer;color:var(--mt);font-size:.8rem'>Settings of this session</summary>"
            f"<pre style='font-size:.75rem;color:var(--mt)'>{html.escape(json.dumps({'params': params, 'engine': eng}, indent=1))}</pre></details>")
    return page(f"Session {row['date']}", body)


def ticker(view: dict, strategy: dict, name: str, sector: str, current_ym: Optional[str]) -> str:
    t, history = view["ticker"], view["history"]
    pos = view.get("position")
    chart = charts.price_chart(view["dates"], view["close"], view["sma"], view["trades"], stop=pos["stop_price"] if pos else None)
    last = history[-1] if history else {"state": "NONE", "checks": {}, "note": ""}
    n = {s: sum(h["state"] == s for h in history) for s in ("ENTRY", "EXIT", "WATCH")}
    rows = "".join(f"<tr><td>{h['date']}</td><td>{fmt_f(h['close'])}</td><td>{state_badge(h['state'])}</td>"
                   f"<td><div class='sigs'>{checks_html(h['checks'], h.get('note', ''))}</div></td></tr>" for h in reversed(history))
    trows = "".join(f"<tr><td>{x['date']}</td><td class='{'pos' if x['side'] == 'buy' else 'neg'}'>{x['side'].upper()}</td><td>{fmt_f(x['price'])}</td>"
                    f"<td>{fmt_f(x['value'], 0)}</td><td class='{cls(x['pnl'])}'>{x['pnl']:+,.0f}</td><td>{html.escape(x['reason'])}</td></tr>"
                    for x in reversed(view["trades"]))
    posinfo = ""
    if pos:
        chg = pos["last_price"] / pos["entry_price"] - 1 if pos["entry_price"] else 0.0
        posinfo = (f"<div class='warnbox' style='border-color:#3b82f666;background:#3b82f614;color:#93c5fd'>OPEN since {pos['entry_date']} @ {pos['entry_price']:.2f} · "
                   f"last {pos['last_price']:.2f} (<span class='{cls(chg)}'>{chg:+.1%}</span>) · stop {pos['stop_price']:.2f} "
                   f"({(pos['last_price'] - pos['stop_price']) / pos['last_price']:.1%} away) · highest close {pos['highest_close']:.2f}</div>")
    body = (site_nav("", current_ym, strategy) + f"<h1>{html.escape(t)} <span class='cn'>{html.escape(name)}</span> "
            f"<span class='badge' style='background:{sc(sector)}22;color:{sc(sector)}'>{html.escape(sector)}</span></h1>"
            f"<p class='sub'>{len(history)} sessions in the fund · {n['ENTRY']} ENTRY · {n['EXIT']} EXIT · {n['WATCH']} WATCH · "
            f"{external_quote_link(t, name)}</p>" + posinfo + chart +
            f"<div class='grid2'><div><h3>Today: {state_badge(last['state'])}</h3><div class='sigs'>{checks_html(last['checks'], last.get('note', ''))}</div></div>"
            f"<div><h3>Trades in the book ({len(view['trades'])})</h3>"
            + (f"<div class='tw'><table class='compact'><tr><th>Date</th><th>Side</th><th>Price</th><th>Value</th><th>P&L</th><th>Reason</th></tr>{trows}</table></div>" if trows else "<p class='none'>none</p>")
            + "</div></div>"
            f"<details><summary style='cursor:pointer;color:var(--mt);font-size:.85rem'>Signal history ({len(history)} sessions)</summary>"
            f"<div class='tw'><table class='compact'><tr><th>Date</th><th>Close</th><th>State</th><th>Checks</th></tr>{rows}</table></div></details>")
    return page(f"{t} — {strategy['label']}", body)


def signals_page(rows: list[dict], strategy: dict, dates: list[str], n_tickers: int, current_ym: Optional[str]) -> str:
    horizons = list(rows[0]["horizons"].keys()) if rows else []
    def cell(v):
        if not v["n"]:
            return "<td>—</td>"
        e = v["excess"]
        c = "pos" if e > 0.005 else "neg" if e < -0.005 else ""
        return f"<td class='{c}'>{e:+.2%} <small style='color:var(--mt)'>({v['hit']:.0%})</small></td>"
    trs = "".join(f"<tr><td>{html.escape(r['rule'])}</td><td>{r['n']:,}</td>" + "".join(cell(r["horizons"][h]) for h in horizons) + "</tr>" for r in rows)
    body = (site_nav("signals", current_ym, strategy) + "<h1>Signal quality</h1>"
            f"<p class='sub'>For every rule: on how many ticker-days it fired over {dates[0]} → {dates[-1]} ({len(dates)} sessions, {n_tickers} tickers), "
            f"and the mean forward return of those ticker-days <b>in excess of the equal-weight universe</b> (share positive in brackets). "
            f"A rule that selects nothing shows ~0% and ~50%. This measures selection only — position management (stops, exits) shows up in the book, not here.</p>"
            f"<div class='tw'><table class='compact'><tr><th>Rule</th><th>Ticker-days</th>" + "".join(f"<th>{h} sessions</th>" for h in horizons) + f"</tr>{trs}</table></div>"
            "<div class='warnbox'>Everything here is in-sample on the stored history (one market regime). Reference rows are not traded; they show what a well-known selector scores on the same data.</div>")
    return page("Velocity — signal quality", body)


def status(st: dict, strategy: Optional[dict], books: list[dict], fetches: list[dict], sessions: list[dict],
           current_ym: Optional[str], bind: str) -> str:
    def kv(label, value):
        return f"<tr><td style='color:var(--mt)'>{label}</td><td>{value}</td></tr>"
    settings = st.get("serve_settings") or {}
    info = (kv("serve", f"{st.get('serve_strategy') or '—'} · started {fmt_utc(st.get('serve_started_at'))} · "
                        f"poll every {settings.get('poll_frequency_min', '—')} min · listening on {html.escape(bind)}") +
            kv("holdings", f"{st.get('n_snapshots')} sessions, {st.get('first_date')} → {st.get('holdings_date')}") +
            kv("holdings updated", f"{fmt_utc(st.get('holdings_updated_at'))} (holdings date {st.get('holdings_date') or '—'})") +
            kv("last poll", f"{fmt_utc(st.get('last_poll_at'))} — <span class='{_lvl(st)}'>{html.escape(st.get('last_poll_outcome') or '')}</span>"
                            + (f"<div style='text-transform:none;color:var(--mt)'>{html.escape(st.get('last_poll_detail'))}</div>" if st.get("last_poll_detail") else "")) +
            kv("next poll", fmt_utc(st.get("next_poll_at"))) +
            kv("last cycle", f"{fmt_utc(st.get('last_cycle_at'))} — <span class='{_lvl(st)}'>{html.escape(st.get('last_cycle_status') or '')}</span>") +
            kv("last session", st.get("last_session_date") or "—") +
            kv("market state built", fmt_utc(st.get("state_built_at"))) +
            kv("settings", f"<code style='font-size:.75rem'>{html.escape(json.dumps(settings))}</code>"))
    brows = "".join(f"<tr><td><a href='/book'>{b['strategy']}</a></td><td>{b['last_date'] or '—'} ({b['last_kind'] or '—'})</td>"
                    f"<td>{fmt_f(b['equity'], 0)}</td><td>{fmt_f(b['cash'], 0)}</td><td>{b['open']}</td><td>{b['trades']}</td>"
                    f"<td>{b['rising']}</td><td>{b['sessions']}</td></tr>" for b in books)
    frows = "".join(f"<tr><td>{fmt_utc(f['attempted_at'])}</td><td class='{'ok' if f['outcome'] in ('stored', 'already') else 'err'}'>{f['outcome']}</td>"
                    f"<td>{f['holdings_date'] or ''}</td><td>{f['http_status'] or ''}</td><td>{f['ms'] or ''}</td>"
                    f"<td style='text-transform:none'>{html.escape(f['message'] or '')}</td></tr>" for f in fetches)
    srows = "".join(f"<tr><td><a href='/session/{s['date']}'>{s['date']}</a></td><td>{s['kind']}</td>"
                    f"<td>{s['n_entries']}</td><td>{s['n_exits']}</td><td>{s['n_open']}</td><td>{fmt_f(s['equity'], 0)}</td>"
                    f"<td>{fmt_utc(s['run_at'])}</td></tr>" for s in sessions) if strategy else ""
    body = (site_nav("status", current_ym, strategy) + "<h1>Status</h1>"
            f"<div class='tw'><table>{info}</table></div>"
            f"<h2>Book</h2><div class='tw'><table><tr><th>Strategy</th><th>Last session</th><th>Equity</th><th>Cash</th><th>Open</th>"
            f"<th>Trades</th><th>Waiting</th><th>Sessions</th></tr>{brows or '<tr><td colspan=8 class=none>none</td></tr>'}</table></div>"
            f"<h2>Last fetches</h2><div class='tw'><table><tr><th>At</th><th>Outcome</th><th>Holdings</th><th>HTTP</th><th>ms</th><th>Message</th></tr>"
            f"{frows or '<tr><td colspan=6 class=none>none</td></tr>'}</table></div>"
            f"<h2>Recent sessions</h2><div class='tw'><table><tr><th>Date</th><th>Kind</th><th>Entries</th><th>Exits</th><th>Open</th>"
            f"<th>Equity</th><th>Run at</th></tr>{srows or '<tr><td colspan=7 class=none>none</td></tr>'}</table></div>")
    return page("Velocity — status", body, live=True)


def _lvl(st: dict) -> str:
    return {"ok": "ok", "warn": "warn", "error": "err"}.get(st.get("last_cycle_level") or "ok", "ok")


def error(code: int, message: str, current_ym: Optional[str] = None, strategy: Optional[dict] = None) -> str:
    return page(f"{code}", site_nav("", current_ym, strategy) + f"<h1>{code}</h1><p class='sub'>{html.escape(message)}</p>")
