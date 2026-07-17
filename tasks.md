# Current
Refactor velocity.py to a single responsibility: daily automated run only.
Step 0 — Explore: Look through the full repo structure first (not just velocity.py) to understand what modules/files currently exist and how they're wired together, before proposing changes.
Goal: velocity.py should do exactly one thing — the daily unattended job: pull latest data → run scoring/confirmation logic → apply buy/sell rules → execute/log the decision. Nothing else stays in it.
Scope of work:

Audit the current codebase and list every responsibility velocity.py (and any tightly-coupled helper files) currently handles — one line per responsibility with the function/file it lives in.
Classify each responsibility as:

Stays — part of the daily run (scoring, rating, buy/sell decision, execution)
Moves — manual/on-demand tooling (backfill, report generation, per-ticker history, ETF onboarding, ad-hoc commentary pulls, etc.)


Extract "Moves" items into their own modules/scripts (e.g. backfill.py, reports.py, manage_etfs.py), each runnable standalone with proper argparse CLI args — ticker, date range, ETF, dry-run, etc. as applicable.
Update README.md with a section per extracted tool:

What it does
When you'd run it manually
Exact command + example invocation
Any prerequisites/ordering (e.g. "run backfill before the first daily run for a new ETF")


Leave velocity.py's CLI surface minimal — no args needed for the daily cron run, or just a --dry-run/--etf override if genuinely needed.

Deliverable: refactored file layout + updated README.md, plus a short before/after summary of what moved where. Don't change scoring/buy-sell logic itself — this is a structural refactor, not a logic change.

# Future
1. Fix the paper trading on backfill, paper trading should happen only on daily run