# Problem Definition
The first live run after a backfill can create a large batch of paper trades.

**Why:**
- Backfill writes snapshots and reports but creates no portfolio trades.
- The next `velocity.py` run starts with an empty portfolio.
- It scores every current holding, not only new tickers.
- Every unheld ticker scoring ≥4 is bought.
- `store_snapshot()` also returns `False` for an already-backfilled date, but that result was previously ignored — so this could happen even if the "live" fetch is the same latest snapshot.

---

# Status

## ✅ Completed

### 1. Hard-disable paper trading (buy/sell)
- `should_execute_paper_trades()` in `velocity.py` always returns `False`.
- No orders can be created. Scoring, reports, and portfolio-state saving still run.
- The existing `--no-paper-trade` flag remains accepted (now redundant).
- The `execute_paper_trades_for_etf()` function is preserved for when trading is re-enabled.

### 2. Persist per-ETF signal state in SQLite
- `trading_state` table added to the schema (`etf`, `last_evaluated_snapshot`, `scores_json`, `updated_at`).
- `load_trading_state(conn, etf)` — reads back `{last_evaluated_snapshot, scores}` or `None`.
- `save_trading_state(conn, etf, date, scores)` — upserts via `ON CONFLICT`.
- `tests/test_trading_state.py` — 4 tests covering round-trip, upsert, None return, ETF isolation.

### 3. Score transition logic (`compute_score_transitions`)
- Added to `src/analyzer.py`.
- Implements the exact threshold rules from the plan:
  - **Buy** : `prior < 4 AND current >= 4` (new tickers treated as `prior = 0`)
  - **Sell** : `prior >= 4 AND current < 4`
  - **Unchanged** : everything else
- Output: sorted `buy_signals`, `sell_signals`, `unchanged` lists.
- `tests/test_score_transitions.py` — 11 tests covering all rule branches, mixed tickers, absent tickers, sorted output.

### 4. Score evaluation pipeline (`evaluate_and_persist_scores`)
- Replaces `record_trading_baseline()` in `velocity.py`.
- On every run: loads prior state, diffs scores via `compute_score_transitions`, logs all `WOULD BUY` / `WOULD SELL` signals, atomically saves new state.
- **Idempotent**: re-running the same snapshot date skips evaluation if state is already up to date.
- Bootstrap case (first run / no prior state): logs ticker count, no trades, saves initial state.
- Buy/sell is still fully gated off — signals are log-only.

---

## ⏳ Pending

### 5. Bootstrap mode (first post-backfill run creates no trades)
- `evaluate_and_persist_scores()` already handles this correctly — bootstrap path logs and saves but executes no trades.
- **Needs verification**: end-to-end test with a real or mock DB to confirm backfill → first live run = 0 trades.

### 6. Process only newly stored snapshots (use `store_snapshot()` return value)
- `snapshot_added` is already plumbed through to `evaluate_and_persist_scores()`.
- The `should_execute_paper_trades()` gate needs updating: when paper trading is re-enabled, trades must only execute when `snapshot_added=True`.
- Currently the outer `if/elif/else` in `run_etf()` handles this, but it should be reviewed once trading is re-enabled.

### 7. Re-enable paper trading with transition-based signals
- Flip `should_execute_paper_trades()` to use the transition result instead of scoring all holdings.
- `execute_paper_trades_for_etf()` needs to be updated to:
  - Accept the `transitions` dict from `evaluate_and_persist_scores()`.
  - Only act on `buy_signals` and `sell_signals` (not iterate all scored holdings).
  - Still process exits/stops independently of score transitions.

### 8. Regression tests (from the plan)
- `[ ]` Backfill → first live run produces **zero trades**.
- `[ ]` Score 2 → 4 creates **one buy**.
- `[ ]` Score 5 → 3 creates **one sell**.
- `[ ]` New ticker at score 4 creates **a buy**.
- `[ ]` Re-running the same snapshot creates **no duplicate trades**.

---

# Next Logical Steps

1. **Write end-to-end regression tests** (item 8 above) using a mock CSV + in-memory DB. These are the definitive correctness gate before re-enabling trading.

2. **Refactor `execute_paper_trades_for_etf()`** to consume the `transitions` dict rather than iterating all scored holdings. This is the core change needed to prevent the original "mass buy on first live run" problem from recurring once trading is turned back on.

3. **Re-enable `should_execute_paper_trades()`** after the regression tests pass. The function should return `True` when `--no-paper-trade` is not set (restoring original intent).

4. **Wire `transitions` return value from `evaluate_and_persist_scores()` into `execute_paper_trades_for_etf()`** — currently `evaluate_and_persist_scores()` returns the transitions dict but it is not yet passed to the trading function.

5. **Validate with a dry-run** against a real ETF DB (`--mock-csv` with a known CSV snapshot) and inspect logs for `WOULD BUY` / `WOULD SELL` output before committing to live trading.