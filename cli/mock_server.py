#!/usr/bin/env python3
"""
mock_server — a tiny HTTP server that answers like the iShares holdings endpoint, with dummy data,
to validate cli/holdings_report.py offline.

    python cli/mock_server.py --port 8765 --weekdays 30        # serves the last 30 weekdays
    python cli/holdings_report.py --url "http://127.0.0.1:8765/holdings.csv?fund=TEST" --days 60 \\
        --data-dir /tmp/hr/data --report-dir /tmp/hr/report --pause 0

Any path is accepted; the `asOfDate=YYYYMMDD` query parameter selects the day.  Weekends, days
before the synthetic history and days after "today" get BlackRock's placeholder (`Fund Holdings
as of,"-"`, no rows).  The synthetic fund has ~30 names; each day every holding's share count is
scaled by a fund-flow ratio (creations/redemptions), and on scripted days names are added /
removed / rebalanced.  `--truth` prints the scripted changes as JSON so the report can be checked.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List
from urllib.parse import parse_qs, urlsplit

BASE = ["NVDA", "AAPL", "MSFT", "AMZN", "JPM", "XOM", "META", "GOOGL", "AVGO", "BAC", "JNJ", "MU", "AMD", "CRM", "WMT",
        "V", "MA", "HD", "PG", "KO", "PEP", "CVX", "MRK", "ABBV", "LLY", "UNH", "CAT", "DE", "GE", "HON"]
EXTRA = ["TEAM", "ACN", "PLTR", "SPCX", "DELL", "STX", "WDC", "ALAB"]
SECTORS = ["Information Technology", "Financials", "Health Care", "Energy", "Industrials", "Consumer Staples"]


def weekdays_back(n: int, end: dt.date) -> List[dt.date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return list(reversed(out))


def synthesize(days: List[dt.date], seed: int = 7):
    """{date: {ticker: shares}} plus the truth log of scripted changes per date."""
    rng = random.Random(seed)
    shares: Dict[str, float] = {t: rng.uniform(1e5, 5e6) for t in BASE}
    price: Dict[str, float] = {t: rng.uniform(20, 900) for t in BASE + EXTRA}
    sector: Dict[str, str] = {t: rng.choice(SECTORS) for t in BASE + EXTRA}
    pool = list(EXTRA)
    history, truth = {}, {}
    for i, d in enumerate(days):
        changes = []
        flow = 1 + rng.uniform(-0.004, 0.004)              # creations / redemptions: every name scales alike
        for t in shares:
            shares[t] *= flow
            price[t] *= 1 + rng.gauss(0.0005, 0.012)
        if i > 0:
            if i % 7 == 3 and pool:                         # an entrant
                t = pool.pop(0); shares[t] = rng.uniform(2e5, 2e6); changes.append((t, "added"))
            if i % 11 == 5 and len(shares) > 20:            # a removal
                t = rng.choice(sorted(shares)); shares.pop(t); pool.append(t); changes.append((t, "removed"))
            if i % 5 == 2:                                  # a rebalance of a few names
                for t in rng.sample(sorted(shares), 3):
                    k = rng.choice([0.85, 0.9, 1.12, 1.25])
                    shares[t] *= k; changes.append((t, "increase" if k > 1 else "reduced"))
        history[d] = ({t: (shares[t], price[t], sector[t]) for t in sorted(shares)}, flow)
        truth[d.isoformat()] = changes
    return history, truth


def render_csv(d: dt.date, holdings) -> str:
    rows, _ = holdings
    head = (f'iShares Synthetic Factor ETF\nFund Holdings as of,"{d:%b %d, %Y}"\nInception Date,"Mar 19, 2019"\n'
            f'Shares Outstanding,"12,345,678.00"\nStock,"-"\nBond,"-"\nCash,"-"\nOther,"-"\n \n')
    cols = ("Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Shares,Price,Location,Exchange,"
            "Currency,FX Rate,Market Currency,Accrual Date\n")
    total = sum(s * p for s, p, _ in rows.values())
    body = ""
    for t, (s, p, sec) in rows.items():
        mv = s * p
        body += (f'"{t}","{t} INC","{sec}","Equity","{mv:,.2f}","{mv / total * 100:.2f}","{mv:,.2f}","{s:,.2f}","{p:.2f}",'
                 f'"United States","NASDAQ","USD","1.00","USD","-"\n')
    body += '"USD","USD CASH","Cash and/or Derivatives","Cash","1,000.00","0.00","1,000.00","1,000.00","1.00","United States","-","USD","1.00","USD","-"\n'
    tail = ' \n"The content contained herein is owned or licensed by BlackRock and/or its third-party information providers."\n'
    return head + cols + body + tail


PLACEHOLDER = ('iShares Synthetic Factor ETF\nFund Holdings as of,"-"\nInception Date,"Mar 19, 2019"\n \n'
               'Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Shares,Price,Location,Exchange,'
               'Currency,FX Rate,Market Currency,Accrual Date\n')


def make_handler(history):
    class H(BaseHTTPRequestHandler):
        hits = 0

        def do_GET(self):
            q = parse_qs(urlsplit(self.path).query)
            asof = (q.get("asOfDate") or [""])[0]
            try:
                d = dt.datetime.strptime(asof, "%Y%m%d").date()
            except ValueError:
                d = max(history)                                  # no asOfDate: the latest day (like the live endpoint)
            body = render_csv(d, history[d]) if d in history else PLACEHOLDER
            raw = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            H.hits += 1

        def log_message(self, fmt, *args):
            pass
    return H


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--weekdays", type=int, default=30, help="synthetic history length in weekdays, ending --end (default 30)")
    ap.add_argument("--end", type=dt.date.fromisoformat, default=dt.date.today(), help="last synthetic day (default today)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--truth", type=argparse.FileType("w"), help="write the scripted changes per date as JSON to this file")
    a = ap.parse_args()
    history, truth = synthesize(weekdays_back(a.weekdays, a.end), a.seed)
    if a.truth:
        json.dump(truth, a.truth, indent=1); a.truth.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(history))
    print(f"mock holdings server on http://127.0.0.1:{a.port}/holdings.csv  ({len(history)} weekdays, "
          f"{min(history)} → {max(history)}); Ctrl-C to stop", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
