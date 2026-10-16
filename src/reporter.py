"""
HTML rendering for Velocity — pure functions from data to HTML strings.

Nothing here touches the file system: the web server calls these renderers with data read from
the database (`day_log_entry()` turns a `build_day_data()` payload into the compact per-day record
the month page renders).
"""

import datetime
from typing import Optional

# Strategy signal state per holding (produced by Strategy.explain / the engine)
STATE_META = {
    "ENTRY": ("ENTRY", "#10b981"),
    "HOLD":  ("HOLD",  "#3b82f6"),
    "EXIT":  ("EXIT",  "#ef4444"),
    "WATCH": ("WATCH", "#f59e0b"),
    "NONE":  ("—",     "#6b7280"),
}

# Sector colors
SECTOR_COLORS = {
    "Information Technology": "#3b82f6",
    "Financials": "#10b981",
    "Health Care": "#8b5cf6",
    "Industrials": "#f59e0b",
    "Energy": "#ef4444",
    "Consumer Staples": "#06b6d4",
    "Consumer Discretionary": "#ec4899",
    "Communication": "#6366f1",
    "Materials": "#84cc16",
    "Utilities": "#f97316",
    "Real Estate": "#14b8a6",
}

def sc(sector: str) -> str:
    return SECTOR_COLORS.get(sector or "", "#6b7280")

CSS = """
/* Purple/Lilac Theme - Flat Design */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

:root {
    --bg: #1a1025;
    --sf: #2d1f3e;
    --bd: #4a3b5c;
    --mt: #b8a9c9;
    --ac: #a78bfa;
    --ac-hover: #c4b5fd;
    --tx: #f5f3ff;
    --pos: #34d399;
    --neg: #f87171;
    --lilac-light: #ddd6fe;
    --lilac-dark: #7c3aed;
    --purple-glow: rgba(167, 139, 250, 0.15);
}

* {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
}

body {
    background: #0f172a;
    color: var(--tx);
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 14px;
    line-height: 1.6;
    min-height: 100vh;
}

a {
    color: var(--ac);
    text-decoration: none;
    transition: color 0.2s ease;
}

a:hover {
    color: var(--ac-hover);
}

.wrap {
    max-width: 1180px;
    margin: 0 auto;
    padding: 24px 18px 40px;
}

h1 {
    font-size: 1.75rem;
    font-weight: 700;
    margin-bottom: 8px;
    background: linear-gradient(135deg, var(--lilac-light) 0%, var(--ac) 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
}

h2 {
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--ac);
    margin: 32px 0 12px;
    border-bottom: 2px solid var(--bd);
    padding-bottom: 8px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
}

.sub {
    color: var(--mt);
    font-size: 0.85rem;
    margin-bottom: 24px;
}

.nav {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    margin-bottom: 24px;
    font-size: 0.85rem;
}

.nav a {
    color: var(--ac);
    padding: 6px 12px;
    border-radius: 6px;
    background: var(--purple-glow);
    border: 1px solid transparent;
    transition: all 0.2s ease;
}

.nav a:hover {
    border-color: var(--ac);
    background: rgba(167, 139, 250, 0.2);
}

.stats {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    margin-bottom: 24px;
}

.stat {
    background: var(--sf);
    border: 1px solid var(--bd);
    border-radius: 10px;
    padding: 12px 14px;
    min-width: 112px;
    box-shadow: 0 4px 12px var(--purple-glow);
}

.sv {
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--ac);
}

.sl {
    font-size: 0.7rem;
    color: var(--mt);
    text-transform: uppercase;
    letter-spacing: 0.06em;
}

.tip {
    background: linear-gradient(135deg, #2d1f3e 0%, #3d2852 100%);
    border: 1px solid var(--bd);
    border-radius: 10px;
    padding: 12px 14px;
    margin-bottom: 16px;
    font-size: 0.85rem;
    color: var(--mt);
}

.tip b {
    color: var(--tx);
}

/* Day block */
.dayblock {
    border: 1px solid var(--bd);
    border-radius: 12px;
    margin-bottom: 16px;
    overflow: hidden;
    background: var(--sf);
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
}

.dayhead {
    background: linear-gradient(135deg, var(--sf) 0%, #3d2852 100%);
    padding: 12px 14px;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    border-bottom: 1px solid var(--bd);
}

.daydate {
    font-size: 1rem;
    font-weight: 700;
    color: var(--lilac-light);
}

.daybadge {
    font-size: 0.72rem;
    padding: 3px 10px;
    border-radius: 6px;
    font-weight: 600;
}

.day-body {
    padding: 12px;
}

.ecard {
    background: var(--sf);
    border: 1px solid var(--bd);
    border-radius: 10px;
    margin-bottom: 10px;
    overflow: hidden;
    transition: all 0.2s ease;
}

.ecard:hover {
    border-color: var(--ac);
    box-shadow: 0 4px 16px var(--purple-glow);
}

.ect {
    padding: 12px 14px 10px;
}

.ectitle {
    display: flex;
    align-items: baseline;
    gap: 10px;
    flex-wrap: wrap;
    margin-bottom: 8px;
}

.tk {
    font-size: 1.15rem;
    font-weight: 700;
    color: var(--lilac-light);
}

.tkl {
    color: inherit;
    text-decoration: none;
    transition: all 0.2s ease;
}

.tkl:hover {
    color: var(--ac-hover);
}

.cn {
    color: var(--mt);
    font-size: 0.85rem;
}

.badge {
    border-radius: 6px;
    padding: 3px 10px;
    font-size: 0.72rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
}

.ecmeta {
    display: flex;
    gap: 18px;
    flex-wrap: wrap;
}

.ecmeta span {
    display: flex;
    flex-direction: column;
}

.ml {
    font-size: 0.68rem;
    color: var(--mt);
    text-transform: uppercase;
    letter-spacing: 0.05em;
}

.ecs {
    padding: 8px 14px;
    border-top: 1px solid var(--bd);
    background: rgba(255, 255, 255, 0.02);
}

.sbadge {
    border-radius: 8px;
    padding: 4px 12px;
    font-size: 0.8rem;
    font-weight: 700;
    display: inline-block;
    margin-bottom: 8px;
}

.sigs {
    display: flex;
    flex-direction: column;
    gap: 3px;
}

.sig {
    font-size: 0.8rem;
    color: var(--mt);
}

.ecg {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    border-top: 1px solid var(--bd);
}

.egcol {
    padding: 12px 16px;
    border-right: 1px solid var(--bd);
}

.egcol:last-child {
    border-right: none;
}

.egt {
    font-size: 0.68rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--ac);
    margin-bottom: 8px;
    font-weight: 600;
}

.mr {
    display: flex;
    justify-content: space-between;
    padding: 3px 0;
    font-size: 0.82rem;
}

.mr span:first-child {
    color: var(--mt);
}

.espark {
    display: flex;
    gap: 24px;
    padding: 10px 14px;
    border-top: 1px solid var(--bd);
    flex-wrap: wrap;
}

.spl {
    font-size: 0.68rem;
    color: var(--mt);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 4px;
}

/* exits / movers inline */
.mini-exit {
    font-size: 0.85rem;
    color: var(--mt);
    padding: 6px 0;
    border-bottom: 1px solid var(--bd);
}

.mini-exit:last-child {
    border-bottom: none;
}

.mini-exit .tk {
    font-size: 0.95rem;
    color: var(--neg);
}

.mini-mover {
    font-size: 0.85rem;
    color: var(--mt);
    padding: 6px 0;
    border-bottom: 1px solid var(--bd);
}

.mini-mover:last-child {
    border-bottom: none;
}

/* table */
table {
    width: 100%;
    border-collapse: collapse;
}

th {
    text-align: left;
    padding: 10px 12px;
    color: var(--mt);
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    border-bottom: 2px solid var(--bd);
}

td {
    padding: 8px 12px;
    border-bottom: 1px solid #2d1f3e;
    vertical-align: middle;
    font-size: 0.82rem;
}

tr:hover td {
    background: rgba(167, 139, 250, 0.08);
}

.tw {
    overflow-x: auto;
}

.pos {
    color: var(--pos);
    font-weight: 600;
}

.neg {
    color: var(--neg);
    font-weight: 600;
}

.bar {
    height: 4px;
    border-radius: 2px;
    margin-top: 4px;
}

.none {
    color: var(--mt);
    font-style: italic;
    font-size: 0.85rem;
    padding: 10px 0;
}

footer {
    margin-top: 48px;
    color: var(--mt);
    font-size: 0.75rem;
    border-top: 1px solid var(--bd);
    padding-top: 16px;
    text-align: center;
}

/* Flat icons placeholder styles */
.icon {
    display: inline-block;
    width: 20px;
    height: 20px;
    vertical-align: middle;
    margin-right: 8px;
    background-size: contain;
    background-repeat: no-repeat;
    background-position: center;
}

.icon-portfolio {
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%23a78bfa'%3E%3Crect x='3' y='3' width='18' height='18' rx='2'/%3E%3Cline x1='3' y1='10' x2='21' y2='10' stroke='%231a1025' stroke-width='2'/%3E%3Cline x1='12' y1='3' x2='12' y2='21' stroke='%231a1025' stroke-width='2'/%3E%3C/svg%3E");
}

"""

def month_key(date_str: str) -> str:
    """Convert YYYY-MM-DD to YYYY-MM."""
    return date_str[:7]

def fmt_pct(v, plus=False) -> str:
    """Format percentage for display."""
    if v is None:
        return "—"
    sign = "+" if plus and v >= 0 else ""
    return f"{sign}{v*100:.1f}%"


def fmt_f(v, dp=2) -> str:
    """Format float for display."""
    return f"{v:.{dp}f}" if v is not None else "—"


def ticker_link(ticker: str, name: str = "") -> str:
    """Link to the ticker's signal-history page."""
    return f'<a class="tkl" href="/ticker/{ticker}" title="{name}">{ticker}</a>'


def external_quote_link(ticker: str, name: str = "") -> str:
    """Google search for the ticker's quote (used on the ticker page)."""
    import urllib.parse
    query = f"{ticker} {name} stock price Yahoo Finance".strip()
    return ('<a class="tkl" href="https://www.google.com/search?q=' + urllib.parse.quote(query) +
            '" target="_blank" rel="noopener">quote ↗</a>')


def state_badge(state: str) -> str:
    """Badge for a holding's strategy signal state."""
    lbl, col = STATE_META.get(state or "NONE", STATE_META["NONE"])
    return (f'<span class="sbadge" style="background:{col}22;color:{col};'
            f'border:1px solid {col}44">{lbl}</span>')


def rising_table(stars: list, title: str, collapsed: bool = False) -> str:
    """Table of signals waiting for a slot in the strategy book."""
    if not stars:
        return ""
    rows = ""
    for r in stars:
        chg = (r.get("ref_price", 0) / r["first_price"] - 1) if r.get("first_price") else 0.0
        rows += (f"<tr><td><b>{ticker_link(r['ticker'])}</b></td><td>{r.get('first_seen','')}</td>"
                 f"<td>{r.get('seen', 0)}</td><td>{fmt_f(r.get('ref_price'))}</td>"
                 f"<td class='{'pos' if chg >= 0 else 'neg'}'>{chg:+.1%}</td><td>{r.get('why','')}</td>"
                 f"<td style='max-width:380px'>{r.get('reason','')}</td></tr>")
    table = (f'<div class="tw"><table><tr><th>Ticker</th><th>Waiting since</th><th>Sessions signalled</th>'
             f'<th>Price</th><th>Since first signal</th><th>Why waiting</th><th>Signal</th></tr>{rows}</table></div>')
    if collapsed:
        return (f'<details class="top20block"><summary style="cursor:pointer;color:var(--mt);font-size:.8rem">'
                f'🌱 {title} ({len(stars)})</summary>{table}</details>')
    return (f'<h2>🌱 {title} ({len(stars)})</h2><p class="sub" style="margin-top:-8px">Entry signals the book could not '
            f'take (full, or capital exhausted). They keep their place while the signal repeats, get priority for the '
            f'next free slot, and drop off when the setup stops firing.</p>{table}')


def checks_html(checks: dict, note: str = "") -> str:
    items = "".join(f'<div class="sig">{"✅" if ok else "❌"} {name}</div>' for name, ok in (checks or {}).items())
    if note:
        items += f'<div class="sig" style="opacity:.8">{note}</div>'
    return items


def sparkline(values: list[float], w=180, h=36, colour="#3b82f6") -> str:
    """Generate SVG sparkline for price series."""
    if len(values) < 2:
        return ""
    mn, mx = min(values), max(values)
    rng = mx - mn or 1
    n = len(values)
    pts = " ".join(
        f"{int(i*w/(n-1))},{int(h - ((v-mn)/rng)*(h-4) - 2)}"
        for i, v in enumerate(values)
    )
    return (f'<svg viewBox="0 0 {w} {h}" style="width:{w}px;height:{h}px;display:block">'
            f'<polyline points="{pts}" fill="none" stroke="{colour}" stroke-width="1.5"/>'
            f'</svg>')


def entry_card(ticker: str, m: dict) -> str:
    """Generate HTML card for a single ETF holding."""
    col = sc(m.get("sector", ""))
    dp = [r["price"] for r in reversed(m.get("daily_history", [])) if r.get("price")]
    wp = [r["price"] for r in reversed(m.get("weekly_history", [])) if r.get("price")]
    sd = sparkline(dp, colour="#3b82f6")
    sw = sparkline(wp, colour="#10b981")
    sig = m.get("signal", {}) or {}
    dd = m.get("drawdown_from_52w_high")
    mc = m.get("momentum_consistency")

    roc_items = [
        ("Since entry", m.get("price_roc_since_entry")),
        ("5-day", m.get("price_roc_5d")),
        ("10-day", m.get("price_roc_10d")),
        ("20-day", m.get("price_roc_20d")),
        ("52-week", m.get("price_roc_52w")),
    ]
    roc_rows = "".join(
        f'<div class="mr"><span>{lbl}</span>'
        f'<span class={"pos" if (v or 0)>=0 else "neg"}">{fmt_pct(v, True)}</span></div>'
        for lbl, v in roc_items
    )

    return f"""
<div class="ecard" id="{ticker}">
  <div class="ect" style="border-left:4px solid {col}">
    <div class="ectitle">
      <span class="tk">{ticker_link(ticker, m.get("name",""))}</span>
      <span class="cn">{m.get("name","")}</span>
      <span class="badge" style="background:{col}22;color:{col}">{m.get("sector","")}</span>
    </div>
    <div class="ecmeta">
      <span><span class="ml">Weight</span><b>{fmt_f(m.get("current_weight"))}%</b></span>
      <span><span class="ml">Price</span><b>{fmt_f(m.get("current_price"))} {m.get("currency","")}</b></span>
      <span><span class="ml">Days in fund</span><b>{m.get("days_in_etf","?")}</b></span>
      <span><span class="ml">First seen</span><b>{m.get("first_seen","?")}</b></span>
    </div>
  </div>
  <div class="ecs">
    {state_badge(sig.get("state"))}
    <div class="sigs">{checks_html(sig.get("checks"), sig.get("note", ""))}</div>
  </div>
  <div class="ecg">
    <div class="egcol"><div class="egt">Price ROC</div>{roc_rows}</div>
    <div class="egcol">
      <div class="egt">Weight</div>
      <div class="mr"><span>Current</span><span>{fmt_f(m.get("current_weight"))}%</span></div>
      <div class="mr"><span>At entry</span><span>{fmt_f(m.get("weight_at_entry"))}%</span></div>
      <div class="mr"><span>Change</span><span class={"pos" if (m.get("weight_roc_since_entry") or 0)>=0 else "neg"}">{fmt_pct(m.get("weight_roc_since_entry"),True)}</span></div>
      <div class="mr"><span>Percentile</span><span>{fmt_f(m.get("weight_percentile"),1)}th</span></div>
      <div class="mr"><span>Z-score</span><span>{fmt_f(m.get("weight_zscore"),2)}</span></div>
    </div>
    <div class="egcol">
      <div class="egt">52-week</div>
      <div class="mr"><span>Drawdown</span><span class={"pos" if (dd or 0)>=0 else "neg"}">{fmt_pct(dd)}</span></div>
      <div class="mr"><span>Consistency</span><span>{f"{mc*100:.0f}%" if mc is not None else "—"}</span></div>
      <div class="mr"><span>Weekly pts</span><span>{len(wp)}</span></div>
    </div>
  </div>
  {"" if not (sd or sw) else f'<div class="espark"><div><div class="spl">Daily price (20d)</div>{sd}</div><div><div class="spl">Weekly price (52w)</div>{sw}</div></div>'}
</div>"""


def render_day_block(entry: dict, is_latest: bool = False) -> str:
    """Render one day's data as a collapsible block within the monthly report."""
    date = entry["date"]
    new_entries = entry.get("new_entries", [])
    exits_e = entry.get("exits", [])
    movers_e = entry.get("movers", [])
    trades_e = entry.get("trades", [])

    badge_parts = []
    if new_entries:
        badge_parts.append(
            f'<span class="daybadge" style="background:#10b98122;color:#10b981">'
            f'{len(new_entries)} new</span>'
        )
    if exits_e:
        badge_parts.append(
            f'<span class="daybadge" style="background:#ef444422;color:#ef4444">'
            f'{len(exits_e)} exit{"s" if len(exits_e)>1 else ""}</span>'
        )
    if not new_entries and not exits_e:
        badge_parts.append(
            '<span class="daybadge" style="background:#334155;color:#94a3b8">no changes</span>'
        )
    badges = " ".join(badge_parts)

    # New entry mini-cards (full detail)
    cards_html = ""
    for ne in new_entries:
        t = ne["ticker"]
        col = sc(ne.get("sector", ""))
        sig = ne.get("signal", {}) or {}

        roc20 = ne.get("price_roc_20d")
        wpct = ne.get("weight_percentile")
        dd = ne.get("drawdown")

        cards_html += f"""
<div class="ecard" id="{date}-{t}">
  <div class="ect" style="border-left:4px solid {col}">
    <div class="ectitle">
      <span class="tk">{ticker_link(t, ne.get("name",""))}</span>
      <span class="cn">{ne.get("name","")}</span>
      <span class="badge" style="background:{col}22;color:{col}">{ne.get("sector","")}</span>
    </div>
    <div class="ecmeta">
      <span><span class="ml">Weight</span><b>{fmt_f(ne.get("weight"))}%</b></span>
      <span><span class="ml">Price</span><b>{fmt_f(ne.get("price"))} {ne.get("currency","")}</b></span>
      <span><span class="ml">20d ROC</span><b class={"pos" if (roc20 or 0)>=0 else "neg"}">{fmt_pct(roc20, True)}</b></span>
      <span><span class="ml">Wt pctile</span><b>{fmt_f(wpct,1)}th</b></span>
      <span><span class="ml">52w DD</span><b>{fmt_pct(dd)}</b></span>
    </div>
  </div>
  <div class="ecs">
    {state_badge(sig.get("state"))}
    <div class="sigs">{checks_html(sig.get("checks"), sig.get("note", ""))}</div>
  </div>
</div>"""

    # Exits
    exits_html = ""
    if exits_e:
        exits_html = '<h2>Exits</h2>'
        for ex in exits_e:
            col = sc(ex.get("sector", ""))
            exits_html += (
                f'<div class="mini-exit">'
                f'<span class="tk">{ticker_link(ex["ticker"], ex.get("name",""))}</span> '
                f'<span>{ex["name"][:32]}</span> '
                f'<span style="color:{col};font-size:.75rem;margin-left:6px">{ex.get("sector","")}</span>'
                f'</div>'
            )

    # Top movers (compact)
    movers_html = ""
    if movers_e:
        movers_html = '<h2>Top weight movers (20d)</h2>'
        for mv in movers_e:
            delta = mv.get("delta", 0) or 0
            dcls = "pos" if delta > 0 else "neg"
            dsign = "+" if delta > 0 else ""
            movers_html += (
                f'<div class="mini-mover">'
                f'<b>{ticker_link(mv["ticker"], mv.get("name",""))}</b> {mv["name"][:28]} '
                f'<span class="{dcls}">{dsign}{delta:.3f}%</span>'
                f'</div>'
            )

    # Top 20 holdings for this specific day (nested, collapsed by default)
    top20_e = entry.get("top20", [])
    top20_html = ""
    if top20_e:
        trows = ""
        for h in top20_e:
            col = sc(h.get("sector", ""))
            roc20 = h.get("price_roc_20d")
            rcls = "pos" if (roc20 or 0) >= 0 else "neg"
            wpct = h.get("weight_percentile")
            dd = h.get("drawdown")
            w = h.get("weight_pct") or 0
            bw = min(100, int(w / 10 * 100))
            trows += (
                f"<tr>"
                f"<td><b>{ticker_link(h['ticker'], h.get('name',''))}</b></td>"
                f"<td style=\"color:{col}\">{h.get('sector','')[:16]}</td>"
                f"<td>{w:.2f}%<div class='bar' style='width:{bw}px;background:{col}'></div></td>"
                f"<td class='{rcls}'>{fmt_pct(roc20, True)}</td>"
                f"<td>{fmt_f(wpct,1)}th</td>"
                f"<td>{fmt_pct(dd)}</td>"
                f"<td>{state_badge(h.get('state'))}</td>"
                f"</tr>"
            )
        top20_html = f"""
<details class="top20block">
  <summary style="cursor:pointer;color:var(--mt);font-size:.8rem">Top 20 holdings — {date}</summary>
  <div class="tw"><table>
  <tr><th>Ticker</th><th>Sector</th><th>Weight</th><th>20d ROC</th><th>Wt pctile</th><th>52w DD</th><th>Signal</th></tr>
  {trows}
  </table></div>
</details>"""

    rising_html = rising_table(entry.get("rising_stars", []), f"Rising stars — {date}", collapsed=True)

    body_content = ""

    # Trades executed today (the actionable output) — shown first.
    trades_html = ""
    if trades_e:
        trade_rows = ""
        for tr in trades_e:
            typ = (tr.get("side") or tr.get("type") or "").upper()
            tcls = "pos" if typ == "BUY" else "neg"
            reason_text = tr.get("reason") or "—"
            trade_rows += (
                f'<div class="mini-exit">'
                f'<span class="{tcls}" style="font-weight:700">{typ}</span> '
                f'<b>{ticker_link(tr.get("ticker", ""), "")}</b> '
                f'<span class="ml">@ {fmt_f(tr.get("price"))}</span> '
                f'<span class="ml">{fmt_f(tr.get("shares", 0), 4)} sh</span> '
                f'<span class="ml" title="{reason_text}">{reason_text}</span>'
                f'</div>'
            )
        trades_html = f'<h2>Trades executed ({len(trades_e)})</h2>{trade_rows}'

    body_content += trades_html
    if new_entries:
        body_content += f'<h2>New entries ({len(new_entries)})</h2>{cards_html}'
    body_content += exits_html
    body_content += movers_html
    body_content += rising_html
    body_content += top20_html
    if not body_content:
        body_content = '<p class="none">No entries, exits, or significant weight changes today.</p>'

    open_attr = " open" if is_latest else ""
    return f"""
<details class="dayblock"{open_attr}>
  <summary class="dayhead" style="cursor:pointer;list-style:none">
    <span class="daydate">{date}</span>
    {badges}
  </summary>
  <div class="day-body">{body_content}</div>
</details>"""


def generate_monthly_page(
    ym: str,
    entries: list[dict],
    prev_ym: Optional[str],
    next_ym: Optional[str],
    is_current: bool,
    day_data: Optional[dict] = None,
    book_label: Optional[str] = None,
    site_nav: str = "",
) -> str:
    """
    The HTML of one month: a collapsible block per trading day (newest first) and, for the
    current month, the live sections (top-20, rising stars, signals across all holdings, sectors).
    entries: list of `day_log_entry()` dicts, newest first.
    """
    month_label = datetime.datetime.strptime(ym + "-01", "%Y-%m-%d").strftime("%B %Y")
    all_new = sum(len(e.get("new_entries", [])) for e in entries)
    all_exit = sum(len(e.get("exits", [])) for e in entries)

    nav_parts = []
    if prev_ym:
        prev_label = datetime.datetime.strptime(prev_ym + "-01", "%Y-%m-%d").strftime("%b %Y")
        nav_parts.append(f'<a href="/month/{prev_ym}">← {prev_label}</a>')
    if next_ym:
        next_label = datetime.datetime.strptime(next_ym + "-01", "%Y-%m-%d").strftime("%b %Y")
        nav_parts.append(f'<a href="/month/{next_ym}">{next_label} →</a>')
    if not is_current:
        nav_parts.append('<a href="/">Latest ↑</a>')
    strat = (day_data or {}).get("strategy") or {}
    if strat.get("name"):
        nav_parts.append(f'<a href="/strategy/{strat["name"]}">{book_label or strat.get("label", strat["name"])} book</a>')
    nav_html = " · ".join(p for p in nav_parts if p)

    # Day blocks — first entry is always open
    blocks = "".join(
        render_day_block(e, is_latest=(i == 0))
        for i, e in enumerate(entries)
    )
    if not blocks:
        blocks = '<p class="none">No data yet for this month.</p>'

    # Watchlist table from most recent day_data (only on current month)
    watchlist_html = ""
    if day_data and is_current:
        top30 = day_data.get("top20", [])
        scored = day_data.get("scored", {})
        wrows = ""
        for h in top30:
            t = h["ticker"]
            col = sc(h.get("sector", ""))
            m = scored.get(t, {})
            state = (m.get("signal") or {}).get("state")
            roc20 = m.get("price_roc_20d")
            rcls = "pos" if (roc20 or 0) >= 0 else "neg"
            wpct = m.get("weight_percentile")
            dd = m.get("drawdown_from_52w_high")
            w = h.get("weight_pct") or 0
            bw = min(100, int(w / 10 * 100))
            wrows += (
                f"<tr>"
                f"<td><b>{ticker_link(t, h.get('name',''))}</b></td>"
                f"<td style=\"color:{col}\">{h.get('sector','')[:16]}</td>"
                f"<td>{w:.2f}%<div class='bar' style='width:{bw}px;background:{col}'></div></td>"
                f"<td class='{rcls}'>{fmt_pct(roc20, True)}</td>"
                f"<td>{fmt_f(wpct,1)}th</td>"
                f"<td>{fmt_pct(dd)}</td>"
                f"<td>{state_badge(state)}</td>"
                f"</tr>"
            )
        if wrows:
            watchlist_html += f"""
<h2>Top 20 holdings — {day_data["date"]}</h2>
<div class="tw"><table>
<tr><th>Ticker</th><th>Sector</th><th>Weight</th><th>20d ROC</th><th>Wt pctile</th><th>52w DD</th><th>Signal</th></tr>
{wrows}
</table></div>"""

        watchlist_html += rising_table(day_data.get("rising_stars", []), "Rising stars — signals waiting for a slot")

        # Strategy signals across ALL holdings (not just the top 20)
        sig_rows = ""
        order = {"ENTRY": 0, "EXIT": 1, "HOLD": 2, "WATCH": 3}
        flagged = [(t, m) for t, m in scored.items() if (m.get("signal") or {}).get("state") in order]
        flagged.sort(key=lambda x: (order[x[1]["signal"]["state"]], x[0]))
        for t, m in flagged:
            sig = m["signal"]
            col = sc(m.get("sector", ""))
            sig_rows += (
                f"<tr><td><b>{ticker_link(t, m.get('name',''))}</b></td>"
                f"<td style=\"color:{col}\">{m.get('sector','')[:16]}</td>"
                f"<td>{fmt_f(m.get('current_price'))}</td>"
                f"<td>{state_badge(sig['state'])}</td>"
                f"<td><div class='sigs'>{checks_html(sig.get('checks'), sig.get('note',''))}</div></td></tr>"
            )
        if sig_rows:
            watchlist_html += f"""
<h2>{strat.get('label', 'Strategy')} signals — {day_data["date"]}</h2>
<p class="sub" style="margin-top:-8px">{strat.get('description', '')}</p>
<div class="tw"><table>
<tr><th>Ticker</th><th>Sector</th><th>Price</th><th>State</th><th>Checks</th></tr>
{sig_rows}
</table></div>"""

        # Sector breakdown
        sec_rows = ""
        for s_row in day_data.get("sectors", []):
            col = sc(s_row.get("sector", ""))
            tw = s_row.get("total_weight") or 0
            bw = min(120, int(tw / 40 * 120))
            sec_rows += (
                f"<tr>"
                f"<td style=\"color:{col}\">{s_row['sector']}</td>"
                f"<td>{s_row['count']}</td>"
                f"<td>{tw:.2f}%<div class='bar' style='width:{bw}px;background:{col}'></div></td>"
                f"</tr>"
            )
        if sec_rows:
            watchlist_html += f"""
<h2>Sector breakdown</h2>
<div class="tw"><table>
<tr><th>Sector</th><th>Holdings</th><th>Total weight</th></tr>
{sec_rows}
</table></div>"""

    n_days = len(entries)
    status_label = "Current month" if is_current else "Archived"
    status_color = "#10b98122" if is_current else "#334155"
    status_text_color = "#10b981" if is_current else "#94a3b8"

    generated_at = datetime.datetime.now(datetime.timezone.utc).strftime("%d %b %Y %H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Velocity — {month_label}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
{site_nav}
<h1>Velocity — {month_label}
  <span style="font-size:.75rem;background:{status_color};color:{status_text_color};border-radius:4px;padding:2px 8px;margin-left:8px;vertical-align:middle">{status_label}</span>
</h1>
<p class="sub">{n_days} trading days · {all_new} new entries · {all_exit} exits</p>

<div class="nav">
  {nav_html if nav_html else '<span style="color:var(--mt)">No other months yet</span>'}
</div>

<div class="tip">
  <b>Signal states:</b> ENTRY = all entry checks pass today · HOLD = open in the book ·
  EXIT = an exit rule fired · WATCH = setup forming. Strategy: <b>{strat.get('label', '—')}</b>
  {('— ' + strat['description']) if strat.get('description') else ''}
</div>

<div class="stats">
  <div class="stat"><div class="sv">{n_days}</div><div class="sl">Trading days</div></div>
  <div class="stat"><div class="sv">{all_new}</div><div class="sl">New entries</div></div>
  <div class="stat"><div class="sv">{all_exit}</div><div class="sl">Exits</div></div>
</div>

{blocks}

{watchlist_html}

<footer>Velocity · Generated: {generated_at} · Source: BlackRock fund holdings · {month_label}</footer>
</div>
</body>
</html>"""


def day_log_entry(day_data: dict) -> dict:
    """The compact per-day record the month page renders, from a `build_day_data()` payload."""
    new_entry_details = []
    for t in day_data["new_tickers"]:
        m = day_data["scored"].get(t, {})
        new_entry_details.append({
            "ticker": t,
            "name": m.get("name", t),
            "sector": m.get("sector", ""),
            "weight": m.get("current_weight"),
            "signal": m.get("signal"),
            "price": m.get("current_price"),
            "currency": m.get("currency", ""),
            "price_roc_20d": m.get("price_roc_20d"),
            "weight_percentile": m.get("weight_percentile"),
            "drawdown": m.get("drawdown_from_52w_high"),
        })

    exit_details = [
        {"ticker": e["ticker"], "name": e["name"],
         "sector": e.get("sector", ""), "weight": e.get("weight_pct")}
        for e in day_data["exits"]
    ]

    # Top 5 weight movers summary
    mover_summary = [
        {"ticker": m["ticker"], "name": m["name"],
         "delta": m["weight_delta"], "weight": m.get("weight_pct")}
        for m in day_data["movers"][:5]
    ]

    # Top 20 holdings snapshot, merged with score metrics
    top20_details = []
    for h in day_data["top20"]:
        t = h["ticker"]
        m = day_data["scored"].get(t, {})
        top20_details.append({
            "ticker": t,
            "name": h.get("name", ""),
            "sector": h.get("sector", ""),
            "weight_pct": h.get("weight_pct"),
            "price": h.get("price"),
            "currency": h.get("currency", ""),
            "state": (m.get("signal") or {}).get("state"),
            "price_roc_20d": m.get("price_roc_20d"),
            "weight_percentile": m.get("weight_percentile"),
            "drawdown": m.get("drawdown_from_52w_high"),
        })

    return {
        "date": day_data["date"],
        "new_entries": new_entry_details,
        "exits": exit_details,
        "movers": mover_summary,
        "top20": top20_details,
        "strategy": day_data.get("strategy"),
        "rising_stars": [
            {k: r.get(k) for k in ("ticker", "first_seen", "last_seen", "seen", "ref_price", "first_price", "why", "reason")}
            for r in day_data.get("rising_stars", [])
        ],
        "trades": day_data.get("trades", []),
        "n_holdings": len(day_data["top20"]),
        "n_snapshots": day_data["n_snapshots"],
    }
