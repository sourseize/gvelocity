"""
HTML generation and reporting logic for Velocity tracker
"""

import datetime
import json
import os
from pathlib import Path
from typing import Optional, List, Dict, Any

# Import from our modules (will be resolved when imported correctly)
# For now, we'll define constants inline or import from config later
SCORE_META = {
    5: ("STRONG", "#10b981"),
    4: ("GOOD", "#22c55e"),
    3: ("WATCH", "#f59e0b"),
    2: ("WEAK", "#ef4444"),
    1: ("AVOID", "#ef4444"),
    0: ("AVOID", "#6b7280"),
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

.icon-rising {
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%2334d399'%3E%3Cpath d='M13 2L3 14h9v8l10-12h-9z'/%3E%3C/svg%3E");
}
"""

RISING_STAR_LOOKBACK_DAYS = 60
RISING_STAR_MIN_GROWTH = 0.30

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
    """Create a Google search link for a ticker with Yahoo Finance."""
    query = f"{ticker} {name} stock price Yahoo Finance".strip()
    import urllib.parse
    url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
    return f'<a class="tkl" href="{url}" target="_blank" rel="noopener">{ticker}</a>'


def score_badge(s: int) -> str:
    """Generate HTML for score badge."""
    lbl, col = SCORE_META.get(s, ("?", "#6b7280"))
    return (f'<span class="sbadge" style="background:{col}22;color:{col};'
            f'border:1px solid {col}44">{s}/5 — {lbl}</span>')


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
    s = m.get("confirmation_score", 0)
    sigs = "".join(f'<div class="sig">{sg}</div>' for sg in m.get("confirmation_signals", []))
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
      <span><span class="ml">Days in ETF</span><b>{m.get("days_in_etf","?")}</b></span>
      <span><span class="ml">First seen</span><b>{m.get("first_seen","?")}</b></span>
    </div>
  </div>
  <div class="ecs">
    {score_badge(s)}
    <div class="sigs">{sigs}</div>
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
        s = ne.get("score", 0) or 0
        lbl, scol = SCORE_META.get(s, ("?", "#6b7280"))
        sigs_html = "".join(f'<div class="sig">{sg}</div>' for sg in ne.get("signals", []))

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
    <span class="sbadge" style="background:{scol}22;color:{scol};border:1px solid {scol}44">{s}/5 — {lbl}</span>
    <div class="sigs">{sigs_html}</div>
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
            s = h.get("score", 0) or 0
            scol = SCORE_META.get(s, ("?", "#6b7280"))[1]
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
                f"<td><span style='color:{scol};font-weight:700'>{s}/5</span></td>"
                f"</tr>"
            )
        top20_html = f"""
<details class="top20block">
  <summary style="cursor:pointer;color:var(--mt);font-size:.8rem">Top 20 holdings — {date}</summary>
  <div class="tw"><table>
  <tr><th>Ticker</th><th>Sector</th><th>Weight</th><th>20d ROC</th><th>Wt pctile</th><th>52w DD</th><th>Score</th></tr>
  {trows}
  </table></div>
</details>"""

    # Rising stars for this specific day (nested, collapsed by default)
    rising_e = entry.get("rising_stars", [])
    rising_html = ""
    if rising_e:
        rrows = ""
        for m in rising_e:
            col = sc(m.get("sector", ""))
            wroc = m.get("weight_roc") or 0
            proc = m.get("price_roc")
            pcls = "pos" if (proc or 0) >= 0 else "neg"
            s = m.get("score", 0) or 0
            scol = SCORE_META.get(s, ("?", "#6b7280"))[1]
            w_then = m.get("weight_at_entry") or 0
            w_now = m.get("current_weight") or 0
            rrows += (
                f"<tr>"
                f"<td><b>{ticker_link(m['ticker'], m.get('name',''))}</b></td>"
                f"<td style=\"color:{col}\">{m.get('sector','')[:16]}</td>"
                f"<td>{m.get('first_seen','?')}</td>"
                f"<td>{w_then:.2f}% → {w_now:.2f}%</td>"
                f"<td class='pos'>+{wroc*100:.0f}%</td>"
                f"<td class='{pcls}'>{fmt_pct(proc, True)}</td>"
                f"<td><span style='color:{scol};font-weight:700'>{s}/5</span></td>"
                f"</tr>"
            )
        rising_html = f"""
<details class="top20block">
  <summary style="cursor:pointer;color:var(--mt);font-size:.8rem">🌱 Rising stars — {date}</summary>
  <div class="tw"><table>
  <tr><th>Ticker</th><th>Sector</th><th>First seen</th><th>Weight then → now</th><th>Weight growth</th><th>Price since entry</th><th>Score</th></tr>
  {rrows}
  </table></div>
</details>"""

    body_content = ""
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
    report_dir: Path,
    day_data: Optional[dict] = None,
    live_ym: Optional[str] = None,
    etf: str = "",
    etf_name: str = "",
) -> str:
    """
    Generate the full HTML for a monthly report page.
    entries: list of day-log dicts, newest first.
    live_ym: the YYYY-MM of whichever month is currently being served as
    index.html (i.e. today's month). Needed because a "next month" link
    can point at the live month from an archive page, and that month's
    file is index.html, not "{live_ym}.html" (that file doesn't exist
    until the month is sealed on rollover).
    """
    month_label = datetime.datetime.strptime(ym + "-01", "%Y-%m-%d").strftime("%B %Y")
    all_new = sum(len(e.get("new_entries", [])) for e in entries)
    all_exit = sum(len(e.get("exits", [])) for e in entries)

    nav_parts = []
    if prev_ym:
        prev_label = datetime.datetime.strptime(prev_ym + "-01", "%Y-%m-%d").strftime("%b %Y")
        prev_href = "index.html" if prev_ym == live_ym else f"{prev_ym}.html"
        nav_parts.append(f'<a href="{prev_href}">← {prev_label}</a>')
    if next_ym:
        next_label = datetime.datetime.strptime(next_ym + "-01", "%Y-%m-%d").strftime("%b %Y")
        next_href = "index.html" if next_ym == live_ym else f"{next_ym}.html"
        nav_parts.append(f'<a href="{next_href}">{next_label} →</a>')
    if not is_current:
        nav_parts.append('<a href="index.html">Latest ↑</a>')
    nav_parts.append('<a href="../index.html">← All ETFs</a>')
    if (report_dir / "tickers" / "index.html").exists():
        nav_parts.append('<a href="tickers/index.html">Tracked tickers →</a>')
    # Add portfolio link if it exists
    if (report_dir / "portfolio.html").exists():
        nav_parts.append('<a href="portfolio.html"><span class="icon icon-portfolio"></span>Portfolio</a>')
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
        # Rising stars — recent entrants building position
        rising = day_data.get("rising_stars", [])
        if rising:
            rrows = ""
            for m in rising:
                t = m["ticker"]
                col = sc(m.get("sector", ""))
                wroc = m.get("weight_roc_since_entry") or 0
                proc = m.get("price_roc_since_entry")
                pcls = "pos" if (proc or 0) >= 0 else "neg"
                s = m.get("confirmation_score", 0) or 0
                scol = SCORE_META.get(s, ("?", "#6b7280"))[1]
                w_then = m.get("weight_at_entry") or 0
                w_now = m.get("current_weight") or 0
                fs = m.get("first_seen", "?")
                rrows += (
                    f"<tr>"
                    f"<td><b>{ticker_link(t, m.get('name',''))}</b></td>"
                    f"<td style=\"color:{col}\">{m.get('sector','')[:16]}</td>"
                    f"<td>{fs}</td>"
                    f"<td>{w_then:.2f}% → {w_now:.2f}%</td>"
                    f"<td class='pos'>+{wroc*100:.0f}%</td>"
                    f"<td class='{pcls}'>{fmt_pct(proc, True)}</td>"
                    f"<td><span style='color:{scol};font-weight:700'>{s}/5</span></td>"
                    f"</tr>"
                )
            watchlist_html += f"""
<h2><span class="icon icon-rising"></span>Rising stars — building position</h2>
<p class="sub" style="margin-top:-8px">Entered within the last {RISING_STAR_LOOKBACK_DAYS} trading days,
weight up {RISING_STAR_MIN_GROWTH*100:.0f}%+ since entry — includes names outside the top-20 that a
single day's snapshot would otherwise miss entirely.</p>
<div class="tw"><table>
<tr><th>Ticker</th><th>Sector</th><th>First seen</th><th>Weight then → now</th><th>Weight growth</th><th>Price since entry</th><th>Score</th></tr>
{rrows}
</table></div>"""

        top30 = day_data.get("top20", [])
        scored = day_data.get("scored", {})
        wrows = ""
        for h in top30:
            t = h["ticker"]
            col = sc(h.get("sector", ""))
            m = scored.get(t, {})
            s = m.get("confirmation_score", 0) or 0
            scol = SCORE_META.get(s, ("?","#6b7280"))[1]
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
                f"<td><span style='color:{scol};font-weight:700'>{s}/5</span></td>"
                f"</tr>"
            )
        if wrows:
            watchlist_html += f"""
<h2>Top 20 holdings — {day_data["date"]}</h2>
<div class="tw"><table>
<tr><th>Ticker</th><th>Sector</th><th>Weight</th><th>20d ROC</th><th>Wt pctile</th><th>52w DD</th><th>Score</th></tr>
{wrows}
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

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Velocity Tracker — {etf} — {month_label}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<h1>Velocity Tracker — {etf} — {month_label}
  <span style="font-size:.75rem;background:{status_color};color:{status_text_color};border-radius:4px;padding:2px 8px;margin-left:8px;vertical-align:middle">{status_label}</span>
</h1>
<p class="sub">{etf_name} · {n_days} trading days · {all_new} new entries · {all_exit} exits</p>

<div class="nav">
  {nav_html if nav_html else '<span style="color:var(--mt)">No other months yet</span>'}
</div>

<div class="tip">
  <b>Score 0–5:</b> ✅ Price ROC (20d) > 0 · ✅ Weight growing since entry ·
  ✅ Weight > 50th pctile · ✅ Price above entry · ✅ Within 20% of 52w high.
  <b>4–5 = act · 3 = watch · 0–2 = wait</b>
</div>

<div class="stats">
  <div class="stat"><div class="sv">{n_days}</div><div class="sl">Trading days</div></div>
  <div class="stat"><div class="sv">{all_new}</div><div class="sl">New entries</div></div>
  <div class="stat"><div class="sv">{all_exit}</div><div class="sl">Exits</div></div>
</div>

{blocks}

{watchlist_html}

<footer>Velocity · Pure maths · Source: iShares {etf} CSV · {month_label}</footer>
</div>
</body>
</html>"""


def get_all_months(report_dir: Path) -> list[str]:
    """Return sorted list of all YYYY-MM months with a .json log file."""
    months = sorted(
        p.stem for p in report_dir.glob("????-??.json")
    )
    return months


def load_month_log(report_dir: Path, ym: str) -> list[dict]:
    """Load the JSON data log for a given YYYY-MM."""
    p = report_dir / f"{ym}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_month_log(report_dir: Path, ym: str, entries: list[dict]) -> None:
    """Save monthly log to JSON file."""
    p = report_dir / f"{ym}.json"
    p.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def append_day_to_log(report_dir: Path, day_data: dict, commentary: dict = None) -> None:
    """Append today's structured summary to the monthly JSON log."""
    ym = month_key(day_data["date"])
    entries = load_month_log(report_dir, ym)

    # Dedup — replace if same date already exists
    entries = [e for e in entries if e.get("date") != day_data["date"]]

    new_entry_details = []
    for t in day_data["new_tickers"]:
        m = day_data["scored"].get(t, {})
        new_entry_details.append({
            "ticker": t,
            "name": m.get("name", t),
            "sector": m.get("sector", ""),
            "weight": m.get("current_weight"),
            "score": m.get("confirmation_score"),
            "signals": m.get("confirmation_signals", []),
            "price": m.get("current_price"),
            "currency": m.get("currency", ""),
            "price_roc_20d": m.get("price_roc_20d"),
            "weight_percentile": m.get("weight_percentile"),
            "drawdown": m.get("drawdown_from_52w_high"),
        })

    exit_details = [
        {"ticker": e["ticker"], "name": e["name"],
         "sector": e.get("sector",""), "weight": e.get("weight_pct")}
        for e in day_data["exits"]
    ]

    # Top 5 weight movers summary
    mover_summary = [
        {"ticker": m["ticker"], "name": m["name"],
         "delta": m["weight_delta"], "weight": m.get("weight_pct")}
        for m in day_data["movers"][:5]
    ]

    # Top 20 holdings snapshot, merged with score metrics, persisted per day
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
            "score": m.get("confirmation_score"),
            "price_roc_20d": m.get("price_roc_20d"),
            "weight_percentile": m.get("weight_percentile"),
            "drawdown": m.get("drawdown_from_52w_high"),
        })

    # Rising stars: recent entrants whose weight has grown meaningfully
    # since entry — persisted daily so this history is actually captured
    rising_details = []
    for m in day_data["rising_stars"]:
        rising_details.append({
            "ticker": m["ticker"],
            "name": m.get("name", m["ticker"]),
            "sector": m.get("sector", ""),
            "first_seen": m.get("first_seen"),
            "weight_at_entry": m.get("weight_at_entry"),
            "current_weight": m.get("current_weight"),
            "weight_roc": m.get("weight_roc_since_entry"),
            "price_roc": m.get("price_roc_since_entry"),
            "weight_percentile": m.get("weight_percentile"),
            "score": m.get("confirmation_score"),
        })

    entries.append({
        "date": day_data["date"],
        "new_entries": new_entry_details,
        "exits": exit_details,
        "movers": mover_summary,
        "top20": top20_details,
        "rising_stars": rising_details,
        "n_holdings": len(day_data["top20"]),
        "n_snapshots": day_data["n_snapshots"],
    })

    # Sort newest-first for the log
    entries.sort(key=lambda x: x["date"], reverse=True)
    save_month_log(report_dir, ym, entries)


def rollover_if_needed(report_dir: Path, today: str) -> None:
    """
    On the 1st of a new month: index.html already contains last month's data.
    We seal it as YYYY-MM.html and the caller will create a fresh index.html.
    This function just checks if a seal is needed and does it.
    """
    index_path = report_dir / "index.html"
    if not index_path.exists():
        return

    # Detect what month index.html currently represents by checking JSON logs
    today_ym = month_key(today)
    # Find most recent json that isn't today's month
    months = get_all_months(report_dir)
    if not months:
        return

    latest_logged_ym = months[-1]
    if latest_logged_ym < today_ym:
        # We've rolled into a new month — seal the old index.html
        sealed = report_dir / f"{latest_logged_ym}.html"
        if not sealed.exists():
            import shutil
            shutil.copy(index_path, sealed)
            # Note: In production, we'd use the logger from main module


def find_tracked_etf_dirs(report_dir: Path) -> list[Path]:
    """Any subfolder of report_dir containing at least one monthly JSON log
    counts as a tracked ETF — this picks up anything on disk, not just
    entries currently in ETF_CONFIG, so old data isn't orphaned if a fund
    is later removed from the config."""
    if not report_dir.exists():
        return []
    return sorted(
        d for d in report_dir.iterdir()
        if d.is_dir() and get_all_months(d)
    )


def generate_hub_index(report_dir: Path) -> str:
    """Generate the top-level hub index.html linking all tracked ETFs."""
    etf_dirs = find_tracked_etf_dirs(report_dir)

    cards = ""
    for d in etf_dirs:
        code = d.name
        # In production, we'd get ETF_CONFIG from config module
        # For now, using a fallback
        try:
            from config import ETF_CONFIG
            cfg = ETF_CONFIG.get(code, {})
        except ImportError:
            cfg = {}
        name = cfg.get("name", code)

        months = get_all_months(d)
        latest_ym = months[-1] if months else None
        day_entries = load_month_log(d, latest_ym) if latest_ym else []
        if not day_entries:
            continue
        latest = day_entries[0]  # newest first

        new_e = latest.get("new_entries", [])
        exits_e = latest.get("exits", [])
        movers = latest.get("movers", [])[:5]
        top20 = latest.get("top20", [])[:5]
        rising = latest.get("rising_stars", [])[:5]

        new_html = ""
        if new_e:
            chips = "".join(
                f"<span class='chip'>{ticker_link(e['ticker'], e.get('name',''))}</span>"
                for e in new_e[:8]
            )
            more = f" +{len(new_e)-8} more" if len(new_e) > 8 else ""
            new_html = f"<div class='hubrow'><b>New:</b> {chips}{more}</div>"

        exit_html = ""
        if exits_e:
            chips = "".join(
                f"<span class='chip' style='opacity:.6'>{ticker_link(e['ticker'], e.get('name',''))}</span>"
                for e in exits_e[:8]
            )
            more = f" +{len(exits_e)-8} more" if len(exits_e) > 8 else ""
            exit_html = f"<div class='hubrow'><b>Exits:</b> {chips}{more}</div>"

        movers_html = ""
        if movers:
            mrows = ""
            for m in movers:
                d_ = m.get("delta", 0) or 0
                cls = "pos" if d_ >= 0 else "neg"
                sign = "+" if d_ >= 0 else ""
                mrows += (
                    f"<span class='chip'>{ticker_link(m['ticker'], m.get('name',''))} "
                    f"<span class='{cls}'>{sign}{d_:.2f}%</span></span>"
                )
            movers_html = f"<div class='hubrow'><b>Top movers:</b> {mrows}</div>"

        top20_html = ""
        if top20:
            trows = "".join(
                f"<span class='chip'>{ticker_link(h['ticker'], h.get('name',''))} "
                f"<span class='mt'>{(h.get('weight_pct') or 0):.2f}%</span></span>"
                for h in top20
            )
            top20_html = f"<div class='hubrow'><b>Top 5:</b> {trows}</div>"

        rising_html = ""
        if rising:
            rrows = "".join(
                f"<span class='chip'>🌱 {ticker_link(m['ticker'], m.get('name',''))} "
                f"<span class='pos'>+{(m.get('weight_roc') or 0)*100:.0f}%</span></span>"
                for m in rising
            )
            rising_html = f"<div class='hubrow'><b>Rising stars:</b> {rrows}</div>"

        if not (new_html or exit_html or movers_html or top20_html or rising_html):
            body = "<div class='hubrow none'>No changes today.</div>"
        else:
            body = top20_html + rising_html + new_html + exit_html + movers_html

        tickers_link = ""
        if (d / "tickers" / "index.html").exists():
            tickers_link = f" · <a href='{code}/tickers/index.html'>Tickers</a>"
        
        # Add portfolio link if it exists
        portfolio_link = ""
        if (d / "portfolio.html").exists():
            portfolio_link = f" · <a href='{code}/portfolio.html'><span class='icon icon-portfolio' style='width:16px;height:16px'></span>Portfolio</a>"

        cards += f"""
<div class="etfcard">
  <h2><a href="{code}/index.html">{code}</a> <span class="etfname">{name}</span></h2>
  <p class="sub">Last update: {latest['date']}{tickers_link}{portfolio_link}</p>
  {body}
</div>"""

    if not cards:
        cards = '<p class="none">No ETFs tracked yet — run velocity.py --etf CODE to get started.</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Velocity — tracked ETFs</title>
<style>{CSS}
.etfcard{{background:var(--sf);border:1px solid var(--bd);border-radius:10px;padding:18px 20px;margin-bottom:16px}}
.etfcard h2{{margin:0 0 4px;font-size:1.1rem}}
.etfcard h2 a{{color:var(--tx);text-decoration:none}}
.etfcard h2 a:hover{{color:#3b82f6}}
.etfname{{font-size:.8rem;color:var(--mt);font-weight:400;margin-left:8px}}
.hubrow{{font-size:.85rem;margin-top:6px}}
.hubrow.none{{color:var(--mt)}}
.hubrow .mt{{color:var(--mt)}}
.chip{{display:inline-block;background:#1e293b;border:1px solid var(--bd);border-radius:5px;
       padding:2px 8px;margin:2px 4px 2px 0;font-size:.78rem}}
</style>
</head>
<body>
<div class="wrap">
<h1>Velocity — tracked ETFs</h1>
<p class="sub">{len(etf_dirs)} fund(s) tracked</p>
{cards}
<footer>Velocity · Pure maths</footer>
</div>
</body>
</html>"""