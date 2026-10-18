"""
charts — server-rendered SVG: pure functions from series to an <svg> string.  No JavaScript,
no external library; the pages embed the result inline.

    line_chart(series, ...)        one or more (label, [(x_label, y)]) lines with axes, legend, zero line
    area_chart(points, ...)        a filled area (drawdown, exposure)
    bar_chart(rows, ...)           horizontal bars with labels and values (P&L by reason / sector)
    price_chart(...)               close + SMA line with entry / exit markers and the stop level

Dates are plotted by position (one step per session), which keeps the charts readable
regardless of holidays; the x-axis shows the first session of every month.
"""
from __future__ import annotations

import html
import math
from typing import Iterable, List, Optional, Sequence, Tuple

Point = Tuple[str, float]          # (date, value)

COLORS = ["#a78bfa", "#34d399", "#f59e0b", "#60a5fa", "#f87171", "#e879f9"]
POS, NEG, MUTED, GRID = "#34d399", "#f87171", "#b8a9c9", "#4a3b5c"


def _nice_ticks(lo: float, hi: float, n: int = 5) -> List[float]:
    if hi <= lo:
        hi = lo + 1e-9
    raw = (hi - lo) / max(n, 1)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if step >= raw:
            break
    first = int(lo / step) * step
    ticks = []
    v = first
    while v <= hi + step * 0.5:
        if v >= lo - step * 0.5:
            ticks.append(round(v, 10))
        v += step
    return ticks


def _month_ticks(dates: Sequence[str], min_gap: int = 12) -> List[Tuple[int, str]]:
    """(index, 'YYYY-MM') of the first session of every month; the first tick is dropped when the
    next one is closer than `min_gap` sessions (labels would overlap); many months are thinned."""
    out, seen = [], None
    for i, d in enumerate(dates):
        m = d[:7]
        if m != seen:
            out.append((i, d[:7]))
            seen = m
    if len(out) >= 2 and out[1][0] - out[0][0] < min_gap:
        out = out[1:]
    if len(out) > 14:
        keep = max(1, len(out) // 12)
        out = out[::keep]
    return out


def _svg(w: int, h: int, body: str, cls: str = "chart") -> str:
    return (f'<svg class="{cls}" viewBox="0 0 {w} {h}" preserveAspectRatio="xMidYMid meet" '
            f'style="width:100%;max-width:{w}px;height:auto;display:block" xmlns="http://www.w3.org/2000/svg">{body}</svg>')


def line_chart(series: List[Tuple[str, List[Point]]], height: int = 260, width: int = 1140, fmt: str = "{:+.0%}",
               baseline: Optional[float] = None, title: str = "", colors: Optional[List[str]] = None) -> str:
    """Several lines on a shared x (dates of the FIRST series) and y axis.  Values are plotted as given;
    `fmt` formats the y ticks (e.g. '{:+.0%}' for returns, '${:,.0f}' for dollars)."""
    series = [(lbl, pts) for lbl, pts in series if pts]
    if not series:
        return '<p class="none">no data to chart</p>'
    colors = colors or COLORS
    dates = [d for d, _ in series[0][1]]
    n = len(dates)
    L, R, T, B = 58, 14, 22 if title else 10, 28
    W, H = width, height
    pw, ph = W - L - R, H - T - B
    ys = [v for _, pts in series for _, v in pts if v == v]
    lo, hi = min(ys), max(ys)
    if baseline is not None:
        lo, hi = min(lo, baseline), max(hi, baseline)
    pad = (hi - lo) * 0.06 or 0.01
    lo, hi = lo - pad, hi + pad
    sx = lambda i: L + (i / max(n - 1, 1)) * pw
    sy = lambda v: T + (1 - (v - lo) / (hi - lo)) * ph
    out = []
    if title:
        out.append(f'<text x="{L}" y="14" fill="{MUTED}" font-size="11">{html.escape(title)}</text>')
    for tv in _nice_ticks(lo, hi, 5):
        y = sy(tv)
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}" stroke-width="0.5"/>')
        out.append(f'<text x="{L - 6}" y="{y + 3.5:.1f}" fill="{MUTED}" font-size="10" text-anchor="end">{fmt.format(tv)}</text>')
    if baseline is not None and lo <= baseline <= hi:
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{sy(baseline):.1f}" y2="{sy(baseline):.1f}" stroke="{MUTED}" stroke-width="0.8" stroke-dasharray="3,3"/>')
    for i, lbl in _month_ticks(dates):
        x = sx(i)
        out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{T}" y2="{H - B}" stroke="{GRID}" stroke-width="0.5"/>')
        out.append(f'<text x="{x:.1f}" y="{H - B + 14}" fill="{MUTED}" font-size="10" text-anchor="middle">{lbl}</text>')
    legend_x = W - R
    for k, (lbl, pts) in enumerate(reversed(series)):
        col = colors[(len(series) - 1 - k) % len(colors)]
        last_v = [v for _, v in pts if v == v][-1]
        text = f"{lbl} {fmt.format(last_v)}"
        out.append(f'<text x="{legend_x}" y="{T + 12}" fill="{col}" font-size="11" text-anchor="end">{html.escape(text)}</text>')
        legend_x -= len(text) * 6.5 + 14
    for k, (lbl, pts) in enumerate(series):
        col = colors[k % len(colors)]
        idx = {d: i for i, d in enumerate(dates)}
        coords = [f"{sx(idx[d]):.1f},{sy(v):.1f}" for d, v in pts if d in idx and v == v]
        if coords:
            out.append(f'<polyline points="{" ".join(coords)}" fill="none" stroke="{col}" stroke-width="{2 if k == 0 else 1.4}" '
                       f'stroke-linejoin="round"/>')
    return _svg(W, H, "".join(out))


def area_chart(points: List[Point], height: int = 120, width: int = 900, fmt: str = "{:.0%}", color: str = NEG,
               title: str = "", lo: Optional[float] = None, hi: Optional[float] = None) -> str:
    pts = [(d, v) for d, v in points if v == v]
    if not pts:
        return '<p class="none">no data to chart</p>'
    dates = [d for d, _ in pts]
    n = len(dates)
    L, R, T, B = 58, 14, 22 if title else 8, 24
    W, H = width, height
    pw, ph = W - L - R, H - T - B
    ys = [v for _, v in pts]
    lo = min(ys) if lo is None else lo
    hi = max(ys) if hi is None else hi
    if hi <= lo:
        hi = lo + 1e-9
    sx = lambda i: L + (i / max(n - 1, 1)) * pw
    sy = lambda v: T + (1 - (v - lo) / (hi - lo)) * ph
    zero = sy(min(max(0.0, lo), hi))
    out = []
    if title:
        out.append(f'<text x="{L}" y="14" fill="{MUTED}" font-size="11">{html.escape(title)}</text>')
    for tv in _nice_ticks(lo, hi, 3):
        y = sy(tv)
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}" stroke-width="0.5"/>')
        out.append(f'<text x="{L - 6}" y="{y + 3.5:.1f}" fill="{MUTED}" font-size="10" text-anchor="end">{fmt.format(tv)}</text>')
    for i, lbl in _month_ticks(dates):
        out.append(f'<text x="{sx(i):.1f}" y="{H - B + 14}" fill="{MUTED}" font-size="10" text-anchor="middle">{lbl}</text>')
    path = f"M{sx(0):.1f},{zero:.1f} " + " ".join(f"L{sx(i):.1f},{sy(v):.1f}" for i, (_, v) in enumerate(pts)) + f" L{sx(n - 1):.1f},{zero:.1f} Z"
    out.append(f'<path d="{path}" fill="{color}" fill-opacity="0.25" stroke="{color}" stroke-width="1.2"/>')
    return _svg(W, H, "".join(out))


def bar_chart(rows: Iterable[Tuple[str, float, str]], width: int = 560, fmt: str = "{:+,.0f}") -> str:
    """Horizontal bars: rows of (label, value, note).  Positive values green, negative red; the
    value and note are written to the right of the bar (or of the axis, for negative bars)."""
    rows = list(rows)
    if not rows:
        return '<p class="none">nothing to chart</p>'
    rh, gap, L, R = 18, 6, 150, 190
    W = width
    H = len(rows) * (rh + gap) + gap
    vals = [v for _, v, _ in rows]
    neg, pos = max(0.0, -min(vals)), max(0.0, max(vals))
    span = (neg + pos) or 1.0
    pw = W - L - R
    scale = pw / span
    x0 = L + neg * scale
    out = [f'<line x1="{x0:.1f}" x2="{x0:.1f}" y1="0" y2="{H}" stroke="{MUTED}" stroke-width="0.8"/>']
    for i, (lbl, v, note) in enumerate(rows):
        y = gap + i * (rh + gap)
        w = abs(v) * scale
        x = x0 if v >= 0 else x0 - w
        col = POS if v >= 0 else NEG
        out.append(f'<rect x="{x:.1f}" y="{y}" width="{max(w, 0.5):.1f}" height="{rh}" fill="{col}" fill-opacity="0.8" rx="2"/>')
        out.append(f'<text x="{L - 8}" y="{y + rh - 5}" fill="#f5f3ff" font-size="11" text-anchor="end">{html.escape(str(lbl)[:24])}</text>')
        tx = (x0 + w + 6) if v >= 0 else (x0 + 6)
        out.append(f'<text x="{tx:.1f}" y="{y + rh - 5}" fill="{MUTED}" font-size="10">{fmt.format(v)}{(" · " + html.escape(note)) if note else ""}</text>')
    return (f'<svg viewBox="0 0 {W} {H}" style="width:100%;max-width:{W}px;height:auto;display:block" '
            f'xmlns="http://www.w3.org/2000/svg">{"".join(out)}</svg>')


def price_chart(dates: Sequence[str], close: Sequence[float], sma: Sequence[float], trades: List[dict],
                stop: Optional[float] = None, height: int = 300, width: int = 1140) -> str:
    """Close line, SMA line, BUY (▲) / SELL (▼) markers from `trades` [{date, side, price}], and the
    current stop level (dashed)."""
    pts = [(d, c) for d, c in zip(dates, close) if c == c]
    if len(pts) < 2:
        return '<p class="none">not enough price history to chart</p>'
    n = len(pts)
    idx = {d: i for i, (d, _) in enumerate(pts)}
    L, R, T, B = 58, 14, 10, 28
    W, H = width, height
    pw, ph = W - L - R, H - T - B
    ys = [c for _, c in pts] + [v for v in sma if v == v] + ([stop] if stop else [])
    lo, hi = min(ys), max(ys)
    pad = (hi - lo) * 0.06 or 0.5
    lo, hi = lo - pad, hi + pad
    sx = lambda i: L + (i / max(n - 1, 1)) * pw
    sy = lambda v: T + (1 - (v - lo) / (hi - lo)) * ph
    out = []
    for tv in _nice_ticks(lo, hi, 5):
        y = sy(tv)
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}" stroke-width="0.5"/>')
        out.append(f'<text x="{L - 6}" y="{y + 3.5:.1f}" fill="{MUTED}" font-size="10" text-anchor="end">{tv:,.2f}</text>')
    for i, lbl in _month_ticks([d for d, _ in pts]):
        x = sx(i)
        out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{T}" y2="{H - B}" stroke="{GRID}" stroke-width="0.5"/>')
        out.append(f'<text x="{x:.1f}" y="{H - B + 14}" fill="{MUTED}" font-size="10" text-anchor="middle">{lbl}</text>')
    sma_pts = [f"{sx(idx[d]):.1f},{sy(v):.1f}" for d, v in zip(dates, sma) if d in idx and v == v]
    if sma_pts:
        out.append(f'<polyline points="{" ".join(sma_pts)}" fill="none" stroke="{COLORS[2]}" stroke-width="1.2" stroke-opacity="0.9"/>')
    out.append(f'<polyline points="{" ".join(f"{sx(i):.1f},{sy(c):.1f}" for i, (_, c) in enumerate(pts))}" fill="none" '
               f'stroke="{COLORS[0]}" stroke-width="1.8" stroke-linejoin="round"/>')
    if stop:
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{sy(stop):.1f}" y2="{sy(stop):.1f}" stroke="{NEG}" stroke-width="1" stroke-dasharray="4,3"/>')
        out.append(f'<text x="{W - R}" y="{sy(stop) - 4:.1f}" fill="{NEG}" font-size="10" text-anchor="end">stop {stop:,.2f}</text>')
    for t in trades:
        d = t.get("date")
        if d not in idx:
            continue
        x, y = sx(idx[d]), sy(float(t.get("price") or pts[idx[d]][1]))
        if (t.get("side") or "").lower() == "buy":
            out.append(f'<polygon points="{x:.1f},{y + 12:.1f} {x - 6:.1f},{y + 22:.1f} {x + 6:.1f},{y + 22:.1f}" fill="{POS}"><title>BUY {d} @ {float(t.get("price") or 0):.2f}</title></polygon>')
        else:
            out.append(f'<polygon points="{x:.1f},{y - 12:.1f} {x - 6:.1f},{y - 22:.1f} {x + 6:.1f},{y - 22:.1f}" fill="{NEG}"><title>SELL {d} @ {float(t.get("price") or 0):.2f} — {html.escape(str(t.get("reason", "")))}</title></polygon>')
    out.append(f'<text x="{L}" y="{T + 10}" fill="{COLORS[0]}" font-size="10">close</text>')
    out.append(f'<text x="{L + 40}" y="{T + 10}" fill="{COLORS[2]}" font-size="10">SMA</text>')
    return _svg(W, H, "".join(out))
