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
- ~~`should_execute_paper_trades()` always returns `False`~~
- Now restored to normal: returns `True` unless `--no-paper-trade` is passed.
- No-paper-trade flag remains available for manual override.

### 2. Persist per-ETF signal state in SQLite
- `trading_state` table added to the schema (`etf`, `last_evaluated_snapshot`, `scores_json`, `updated_at`).
- `load_trading_state(conn, etf)` — reads back `{last_evaluated_snapshot, scores}` or `None`.
- `save_trading_state(conn, etf, date, scores)` — upserts via `ON CONFLICT`.
- `tests/test_trading_state.py` — 4 tests covering round-trip, upsert, None return, ETF isolation.

### 3. Score transition logic (`compute_score_transitions`)
- Added to `src/analyzer.py`.
- Implements exact threshold rules:
  - **Buy** : `prior < 4 AND current >= 4` (new tickers treated as `prior = 0`)
  - **Sell** : `prior >= 4 AND current < 4`
  - **Unchanged** : everything else
- `BUY_THRESHOLD = 4` exported as a named constant.
- `tests/test_score_transitions.py` — 11 tests covering all rule branches, mixed tickers, absent tickers, sorted output.

### 4. Score evaluation pipeline (`evaluate_and_persist_scores`)
- Replaces `record_trading_baseline()` in `velocity.py`.
- On every new snapshot: loads prior state, diffs scores via `compute_score_transitions`, logs all `WOULD BUY` / `WOULD SELL` signals, atomically saves new state.
- **Idempotent**: re-running the same snapshot date skips evaluation entirely if state is already up to date.
- Bootstrap (first run, no prior state): logs ticker count, no trades, saves initial state.
- Always runs — decoupled from the trading enable/disable gate.

### 5. Transition-based trade execution (`execute_paper_trades_for_etf`)
- Fully refactored: no longer iterates all scored holdings.
- Accepts a `transitions` dict from `evaluate_and_persist_scores()`.
- **Buy** only from `transitions["buy_signals"]` (score crossed up through threshold).
- **Sell** only from `transitions["sell_signals"]` (score fell below threshold).
- ETF-exit sells (ticker removed from ETF) run independently of score transitions.
- Trailing stop-losses run independently of score transitions.
- `transitions=None` (bootstrap run) → no buy/sell, returns early.

### 6. `run_etf()` pipeline wiring
- `evaluate_and_persist_scores()` runs **unconditionally** (always updates score state).
- Its returned `transitions` dict is passed to `execute_paper_trades_for_etf()`.
- `execute_paper_trades_for_etf()` only fires when `should_execute_paper_trades() AND snapshot_added=True`.

### 7. Regression tests (all 6 scenarios pass)
- `tests/test_trading_regression.py` — 6 end-to-end tests using in-memory DB + synthetic data:
  - **R1**: Backfill → first live run → **zero trades** ✅
  - **R2**: Score 2 → 4 → **one buy** ✅
  - **R3**: Score 5 → 3 → **one sell** ✅
  - **R4**: New ticker enters at score 4 → **one buy** ✅
  - **R5**: Re-running same snapshot → **no duplicate trades** ✅
  - **R6**: Ticker exits ETF → **exit sell fires** ✅

**Total: 21 tests, all passing.**

---

## ⏳ Pending

### 8. Dry-run validation against a real ETF DB
- Run `python velocity.py --etf MTUM --mock-csv <path>` and inspect logs.
- Verify `WOULD BUY` / `WOULD SELL` log lines appear with sensible tickers.
- Confirm no mass buys on first run post-backfill.

### 9. Review `execute_paper_trade()` in `src/trader.py`
- The standalone `execute_paper_trade()` function (score-based buy/sell) is now unused by the main pipeline.
- Decide: keep for external/CLI use, or remove to avoid confusion.

---

# Next Logical Steps

1. **Dry-run with real data** (item 8): run `velocity.py --etf MTUM` (or `--mock-csv`) and review the log output. Check `WOULD BUY` / `WOULD SELL` lines and confirm the score history in the DB looks correct.

2. **Validate trailing stop-loss behaviour**: add a regression test (or manual run) to confirm stops fire correctly when price drops below the trailing stop, and that they are independent of score transitions.

3. **Clean up `execute_paper_trade()` in `trader.py`**: the score-gated version is now bypassed. Mark it deprecated or remove it — keeping it risks confusion if future developers reuse it.

4. **Integration test across multiple ETFs** (if running `--etf ALL`): confirm score states are stored per-ETF independently and trades don't cross-contaminate.

5. **Consider a `--dry-run` CLI flag**: would allow `run_etf()` to print all transitions + intended orders without modifying any DB state — useful for manual validation before going live.