"""
HTML pages of the web service — thin wrappers around the renderers in `reporter.py` and
`Book.to_html`, plus the hub, status, ticker and session pages.  Everything is a pure function
from data to an HTML string.
"""
from __future__ import annotations

import datetime
import html
import json
from typing import Optional

from ..book import Book
from ..reporter import (CSS, render_day_block, generate_monthly_page, state_badge, checks_html, fmt_f, ticker_link,
                        external_quote_link, rising_table, month_key)
from ..util import fmt_utc

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


def page(title: str, body: str, live: bool = False) -> str:
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>'
            f'<style>{CSS}{EXTRA_CSS}</style></head><body><div class="wrap">{body}'
            f'<footer style="margin-top:18px;color:var(--mt);font-size:.75rem">Velocity · rendered {fmt_utc(datetime.datetime.now(datetime.timezone.utc).isoformat())}</footer>'
            f'</div>{LIVE_JS if live else ""}</body></html>')


def site_nav(active: str, current_ym: Optional[str], strategy: Optional[dict]) -> str:
    items = [("/", "Home", "home")]
    if current_ym:
        items.append((f"/month/{current_ym}", datetime.datetime.strptime(current_ym + "-01", "%Y-%m-%d").strftime("%B %Y"), "month"))
    if strategy:
        items.append((f"/strategy/{strategy['name']}", f"{strategy['label']} book", "book"))
    items.append(("/status", "Status", "status"))
    items.append(("/api/status", "API", "api"))
    return '<nav class="sitenav">' + "".join(
        f'<a href="{href}" class="{"on" if key == active else ""}">{html.escape(label)}</a>' for href, label, key in items) + "</nav>"


def status_strip(st: dict) -> str:
    parts = [f"holdings <b>{st.get('holdings_date') or '—'}</b> updated <b>{fmt_utc(st.get('holdings_updated_at'))}</b>",
             f"last poll <b>{fmt_utc(st.get('last_poll_at'))}</b>"]
    if st.get("next_poll_at") and not st.get("serve_stopped_at"):
        parts.append(f"next poll <b>{fmt_utc(st.get('next_poll_at'))}</b>")
    parts.append(f"last session <b>{st.get('last_session_date') or '—'}</b>")
    cyc, level = st.get("last_cycle_status") or "—", st.get("last_cycle_level") or "ok"
    cls = {"ok": "ok", "warn": "warn", "error": "err"}.get(level, "ok")
    detail = st.get("last_poll_detail") or ""
    parts.append(f"last cycle <b class='{cls}' title='{html.escape(detail)}'>{html.escape(cyc)}</b>")
    return '<div class="strip">' + " · ".join(parts) + "</div>"


def book_card(book: Optional[Book], strategy: Optional[dict]) -> str:
    if not strategy:
        return '<div class="etfcard"><h2>Strategy book</h2><div class="hubrow">starting up…</div></div>'
    if book is None:
        return f'<div class="etfcard"><h2>{html.escape(strategy["label"])} book</h2><div class="hubrow">no sessions yet</div></div>'
    ret = book.total_value() / book.initial_capital - 1
    closed = [t for t in book.trades if t.side == "sell"]
    wins = [t for t in closed if t.pnl > 0]
    pos = "".join(f"<span class='chip'>{ticker_link(p.ticker)} <span class='{'pos' if p.pnl_pct >= 0 else 'neg'}'>{p.pnl_pct:+.1%}</span></span>"
                  for p in sorted(book.positions.values(), key=lambda p: -p.pnl_pct)) or "<span class='hubrow'>no open positions</span>"
    rising = "".join(f"<span class='chip'>🌱 {ticker_link(r['ticker'])} <span class='mt'>x{r['seen']}</span></span>" for r in book.rising_list()[:10])
    return f"""<div class="etfcard"><h2><a href="/strategy/{strategy['name']}">{html.escape(strategy['label'])} book</a>
<span class="etfname" style="font-size:.78rem;color:var(--mt);font-weight:400;margin-left:8px">last session {book.last_date or '—'}</span></h2>
<div class="stats">
  <div class="stat"><div class="sv">${fmt_f(book.total_value(), 0)}</div><div class="sl">Equity</div></div>
  <div class="stat"><div class="sv {'pos' if ret >= 0 else 'neg'}">{ret:+.2%}</div><div class="sl">Return</div></div>
  <div class="stat"><div class="sv">${fmt_f(book.cash, 0)}</div><div class="sl">Cash</div></div>
  <div class="stat"><div class="sv">{len(book.positions)}</div><div class="sl">Open</div></div>
  <div class="stat"><div class="sv">{len(closed)}</div><div class="sl">Closed</div></div>
  <div class="stat"><div class="sv">{(len(wins) / len(closed)) if closed else 0:.0%}</div><div class="sl">Win rate</div></div>
  <div class="stat"><div class="sv">${fmt_f(book.realized(), 0)}</div><div class="sl">Realized</div></div>
  <div class="stat"><div class="sv">${fmt_f(book.unrealized(), 0)}</div><div class="sl">Unrealized</div></div>
</div>
<div class="hubrow"><b>Positions:</b> {pos}</div>
{f"<div class='hubrow'><b>Rising stars:</b> {rising}</div>" if rising else ""}
</div>"""


def hub(st: dict, strategy: Optional[dict], book: Optional[Book], latest_entry: Optional[dict], current_ym: Optional[str],
        all_months: list[str]) -> str:
    months_html = " · ".join(f'<a href="/month/{m}">{datetime.datetime.strptime(m + "-01", "%Y-%m-%d").strftime("%b %Y")}</a>'
                             for m in reversed(all_months))
    latest = render_day_block(latest_entry, is_latest=True) if latest_entry else '<p class="none">No holdings stored yet.</p>'
    body = (site_nav("home", current_ym, strategy) + "<h1>Velocity</h1>" + status_strip(st) + book_card(book, strategy) +
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


def strategy_book(book: Book, strategy: dict, sessions: list[dict], current_ym: Optional[str]) -> str:
    rows = "".join(
        f"<tr><td><a href='/strategy/{strategy['name']}/session/{s['date']}'>{s['date']}</a></td><td>{s['kind']}</td>"
        f"<td>{s['n_entries']}</td><td>{s['n_exits']}</td><td>{s['n_skipped']}</td><td>{s['n_open']}</td><td>{s['n_rising']}</td>"
        f"<td>{fmt_f(s['equity'], 0)}</td><td>{fmt_utc(s['run_at'])}</td></tr>" for s in sessions)
    extra = (f"<h2>Sessions ({len(sessions)})</h2><div class='tw'><table><tr><th>Date</th><th>Kind</th><th>Entries</th><th>Exits</th>"
             f"<th>Skipped</th><th>Open</th><th>Rising</th><th>Equity</th><th>Run at</th></tr>"
             f"{rows or '<tr><td colspan=9 class=none>No sessions yet</td></tr>'}</table></div>")
    return book.to_html(strategy["label"], extra_html=extra + LIVE_JS, nav_html=site_nav("book", current_ym, strategy))


def session(strategy: dict, row: dict, signals: dict, current_ym: Optional[str]) -> str:
    def trades(items, cls):
        return "".join(f"<div class='mini-exit'><span class='{cls}' style='font-weight:700'>{html.escape(side)}</span> "
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
            f"<h1>{html.escape(strategy['label'])} — session {row['date']}</h1>"
            f"<p class='sub'>{row['kind']} · run {fmt_utc(row['run_at'])} · <a href='/strategy/{strategy['name']}'>← book</a> · "
            f"<a href='/day/{row['date']}'>holdings that day →</a></p>"
            f"<div class='stats'><div class='stat'><div class='sv'>${fmt_f(row['equity'], 0)}</div><div class='sl'>Equity</div></div>"
            f"<div class='stat'><div class='sv'>${fmt_f(row['cash'], 0)}</div><div class='sl'>Cash</div></div>"
            f"<div class='stat'><div class='sv'>{row['n_open']}</div><div class='sl'>Open</div></div>"
            f"<div class='stat'><div class='sv'>{row['n_rising']}</div><div class='sl'>Rising</div></div></div>"
            f"<h2>Exits ({len(exits)})</h2>{trades(exits, 'neg')}<h2>Entries ({len(entries)})</h2>{trades(entries, 'pos')}"
            f"<h2>Not taken ({len(skipped)})</h2>{trades(skipped, 'mt')}"
            + rising_table(json.loads(row.get("rising_json") or "[]"), f"Rising stars after {row['date']}", collapsed=True) +
            f"<h2>Signals ({len(signals)})</h2><div class='tw'><table><tr><th>Ticker</th><th>State</th><th>Checks</th></tr>"
            f"{srows or '<tr><td colspan=3 class=none>none stored</td></tr>'}</table></div>"
            f"<details><summary style='cursor:pointer;color:var(--mt);font-size:.8rem'>Settings of this session</summary>"
            f"<pre style='font-size:.75rem;color:var(--mt)'>{html.escape(json.dumps({'params': params, 'engine': eng}, indent=1))}</pre></details>")
    return page(f"{strategy['label']} — {row['date']}", body)


def ticker(t: str, strategy: dict, history: list[dict], name: str, current_ym: Optional[str]) -> str:
    rows = "".join(f"<tr><td>{h['date']}</td><td>{fmt_f(h['close'])}</td><td>{state_badge(h['state'])}</td>"
                   f"<td><div class='sigs'>{checks_html(h['checks'], h.get('note', ''))}</div></td></tr>" for h in reversed(history))
    n = {s: sum(h["state"] == s for h in history) for s in ("ENTRY", "EXIT", "WATCH")}
    body = (site_nav("", current_ym, strategy) + f"<h1>{html.escape(t)} <span class='cn'>{html.escape(name)}</span></h1>"
            f"<p class='sub'>{html.escape(strategy['label'])} signal history · {len(history)} sessions · {n['ENTRY']} ENTRY · "
            f"{n['EXIT']} EXIT · {n['WATCH']} WATCH · {external_quote_link(t, name)}</p>"
            f"<div class='tw'><table><tr><th>Date</th><th>Close</th><th>State</th><th>Checks</th></tr>{rows}</table></div>")
    return page(f"{t} — {strategy['label']}", body)


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
    brows = "".join(f"<tr><td><a href='/strategy/{b['strategy']}'>{b['strategy']}</a></td><td>{b['last_date'] or '—'} ({b['last_kind'] or '—'})</td>"
                    f"<td>{fmt_f(b['equity'], 0)}</td><td>{fmt_f(b['cash'], 0)}</td><td>{b['open']}</td><td>{b['trades']}</td>"
                    f"<td>{b['rising']}</td><td>{b['sessions']}</td></tr>" for b in books)
    frows = "".join(f"<tr><td>{fmt_utc(f['attempted_at'])}</td><td class='{'ok' if f['outcome'] in ('stored', 'already') else 'err'}'>{f['outcome']}</td>"
                    f"<td>{f['holdings_date'] or ''}</td><td>{f['http_status'] or ''}</td><td>{f['ms'] or ''}</td>"
                    f"<td style='text-transform:none'>{html.escape(f['message'] or '')}</td></tr>" for f in fetches)
    srows = "".join(f"<tr><td><a href='/strategy/{strategy['name']}/session/{s['date']}'>{s['date']}</a></td><td>{s['kind']}</td>"
                    f"<td>{s['n_entries']}</td><td>{s['n_exits']}</td><td>{s['n_open']}</td><td>{fmt_f(s['equity'], 0)}</td>"
                    f"<td>{fmt_utc(s['run_at'])}</td></tr>" for s in sessions) if strategy else ""
    body = (site_nav("status", current_ym, strategy) + "<h1>Status</h1>"
            f"<div class='tw'><table>{info}</table></div>"
            f"<h2>Books</h2><div class='tw'><table><tr><th>Strategy</th><th>Last session</th><th>Equity</th><th>Cash</th><th>Open</th>"
            f"<th>Trades</th><th>Rising</th><th>Sessions</th></tr>{brows or '<tr><td colspan=8 class=none>none</td></tr>'}</table></div>"
            f"<h2>Last fetches</h2><div class='tw'><table><tr><th>At</th><th>Outcome</th><th>Holdings</th><th>HTTP</th><th>ms</th><th>Message</th></tr>"
            f"{frows or '<tr><td colspan=6 class=none>none</td></tr>'}</table></div>"
            f"<h2>Recent sessions</h2><div class='tw'><table><tr><th>Date</th><th>Kind</th><th>Entries</th><th>Exits</th><th>Open</th>"
            f"<th>Equity</th><th>Run at</th></tr>{srows or '<tr><td colspan=7 class=none>none</td></tr>'}</table></div>")
    return page("Velocity — status", body, live=True)


def _lvl(st: dict) -> str:
    return {"ok": "ok", "warn": "warn", "error": "err"}.get(st.get("last_cycle_level") or "ok", "ok")


def error(code: int, message: str, current_ym: Optional[str] = None, strategy: Optional[dict] = None) -> str:
    return page(f"{code}", site_nav("", current_ym, strategy) + f"<h1>{code}</h1><p class='sub'>{html.escape(message)}</p>")
