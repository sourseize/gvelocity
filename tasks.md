# Alpaca Integration Plan

## Requirement
Integrate Alpaca paper trading API to execute real trades alongside Velocity's simulated paper trading. Each ETF (MTUM, QUAL, DYNF) will have its own separate Alpaca portfolio to handle ticker overlap and prevent false positives.

## Proposed Solution
Create an `AlpacaClient` module that uses Alpaca's Portfolio API to create and manage separate portfolios per ETF, enabling market order execution for paper trading.

---

## Implementation Tasks

### Phase 1: Alpaca Client Module
- [ ] Create `src/alpaca_client.py` with `AlpacaClient` class
  - [ ] Implement `__init__()` with API key loading from environment variables
  - [ ] Implement `get_or_create_portfolio(etf)` to create/retrieve ETF-specific portfolio
  - [ ] Implement `submit_order(etf, ticker, qty, side, order_type="market")` method
  - [ ] Implement `get_positions(etf)` to fetch positions for specific portfolio
  - [ ] Implement `get_account()` to fetch account cash/buying power
  - [ ] Implement `get_order_history(etf)` to fetch order history for portfolio
  - [ ] Add error handling for API failures and rate limits

### Phase 2: Configuration
- [ ] Add Alpaca configuration to `src/config.py`
  - [ ] Add `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` environment variable loading
  - [ ] Add `ALPACA_PAPER` boolean flag (default True)
  - [ ] Add `ALPACA_BASE_URL` for API endpoint selection

### Phase 3: Portfolio Integration
- [ ] Modify `src/trader.py` to integrate Alpaca
  - [ ] Add optional `alpaca_client` parameter to `Portfolio.__init__()`
  - [ ] Modify `Portfolio.buy()` to submit order to Alpaca after local state update
  - [ ] Modify `Portfolio.sell()` to submit order to Alpaca after local state update
  - [ ] Add `sync_alpaca_positions(etf)` method to import existing positions for specific ETF
  - [ ] Add logging for Alpaca order submissions

### Phase 4: CLI Integration
- [ ] Add CLI flags to `velocity.py`
  - [ ] Add `--alpaca-trade` flag to enable Alpaca order execution
  - [ ] Add `--alpaca-paper` flag (default True) to use paper vs live API
  - [ ] Add `--sync-alpaca-positions` flag to import existing positions on startup

### Phase 5: Dependencies
- [ ] Add `alpaca-py` to `requirements.txt`
- [ ] Add `.env` to `.gitignore` for API key security

### Phase 6: Testing
- [ ] Create `tests/test_alpaca_client.py`
  - [ ] Test `submit_order()` with mock API responses
  - [ ] Test `get_positions()` with mock data
  - [ ] Test error handling for API failures
- [ ] Add integration test for `execute_paper_trades_for_etf()` with Alpaca enabled

### Phase 7: Documentation
- [ ] Update `README.md` with Alpaca setup instructions
- [ ] Document environment variable configuration
- [ ] Add example `.env.example` file

---

## Technical Details

### Alpaca Portfolio API
- Alpaca supports multiple portfolios per account via the Portfolio API
- Each ETF (MTUM, QUAL, DYNF) will have its own portfolio
- Portfolio names: `velocity-mtum`, `velocity-qual`, `velocity-dynf`
- This prevents ticker overlap issues (same stock in multiple ETFs)

### Alpaca API Endpoints
- **Paper Trading**: `https://paper-api.alpaca.markets`
- **Live Trading**: `https://api.alpaca.markets`
- **Portfolios**: `GET/POST /v2/portfolios`
- **Orders**: `POST /v2/orders`
- **Positions**: `GET /v2/positions`
- **Account**: `GET /v2/account`

### Environment Variables
Environment variables will be stored in `.env` file at the source root:
```
ALPACA_API_KEY=your_api_key_here
ALPACA_SECRET_KEY=your_secret_key_here
ALPACA_PAPER=true  # Set to false for live trading
```

### Order Flow
1. Velocity detects buy/sell signal for specific ETF
2. `Portfolio.buy()` or `Portfolio.sell()` updates local state
3. If `alpaca_client` is configured, get/create ETF-specific portfolio
4. Submit order to Alpaca with portfolio context
5. Log order ID and status from Alpaca response
6. Continue with existing state persistence

### Safety Features
- Alpaca paper trading uses virtual money (no real risk)
- Orders are logged before submission
- API errors are caught and logged without crashing the pipeline
- Position sync is opt-in via CLI flag
