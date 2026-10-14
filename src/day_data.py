"""
day_data — one day's report payload for an ETF: new entries / exits / weight movers / top-20 /
sectors, descriptive metrics per holding, and the active strategy's signal state per holding.
Shared by velocity.py, cli/backfill.py and cli/report_rebuild.py.
"""
from __future__ import annotations

from typing import Optional

from .analyzer import compute_metrics
from .config import NEW_ENTRY_LOOKBACK_DAYS
from .database import detect_new_entries, detect_exits, detect_weight_movers, recent_dates
from .market_data import issuer_key


def build_day_data(conn, today: str, etf: str = "", strategy=None, book=None, trades: Optional[list] = None) -> dict:
    new_tickers = detect_new_entries(conn, today, NEW_ENTRY_LOOKBACK_DAYS)
    exits = detect_exits(conn, today)
    movers = detect_weight_movers(conn, today, NEW_ENTRY_LOOKBACK_DAYS)

    current_holdings = [r["ticker"] for r in conn.execute(
        "SELECT ticker FROM holdings WHERE snapshot_date=?", (today,))]
    to_score = list(dict.fromkeys(new_tickers + current_holdings))
    scored = {t: compute_metrics(conn, t, today) for t in to_score}

    # strategy signal state per holding (HOLD overrides when the issuer is in the book)
    held = set(book.positions) if book is not None else set()
    for t, m in scored.items():
        if strategy is not None and strategy.ind is not None:
            sig = strategy.explain(today, t)
        else:
            sig = {"state": "NONE", "checks": {}, "note": ""}
        if issuer_key(t) in held:
            pos = book.positions[issuer_key(t)]
            note = f"open since {pos.entry_date} @ {pos.entry_price:.2f} · stop {pos.stop_price:.2f}"
            if sig.get("state") != "EXIT":
                sig = {**sig, "state": "HOLD", "note": note}
            else:
                sig = {**sig, "note": (sig.get("note", "") + " · " + note).strip(" ·")}
        elif sig.get("state") == "EXIT":
            sig = {**sig, "state": "NONE"}      # exit rules only matter for names we hold
        m["signal"] = sig

    top20 = [dict(r) for r in conn.execute(
        """SELECT ticker,name,sector,weight_pct,price,currency
           FROM holdings WHERE snapshot_date=? ORDER BY weight_pct DESC LIMIT 20""", (today,))]
    sectors = [dict(r) for r in conn.execute(
        """SELECT sector, COUNT(*) as count, SUM(weight_pct) as total_weight
           FROM holdings WHERE snapshot_date=? GROUP BY sector ORDER BY total_weight DESC""", (today,))]

    strat_info = None
    if strategy is not None:
        strat_info = {"name": strategy.name, "label": strategy.label, "description": strategy.description}

    day_trades = []
    for t in (trades or []):
        d = t if isinstance(t, dict) else vars(t)
        if d.get("date") == today and (not etf or etf in (d.get("etfs") or [])):
            day_trades.append(d)

    rising = []
    if book is not None:
        for r in book.rising_list():
            if not etf or etf in r.get("etfs", []):
                rising.append(r)

    return {
        "date": today,
        "rising_stars": rising,
        "new_tickers": new_tickers,
        "exits": exits,
        "movers": movers,
        "scored": scored,
        "top20": top20,
        "sectors": sectors,
        "strategy": strat_info,
        "trades": day_trades,
        "n_snapshots": len(recent_dates(conn, 9999)),
        "lookback": NEW_ENTRY_LOOKBACK_DAYS,
    }
