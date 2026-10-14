# Alpaca Integration Plan

## Requirement
Integrate Alpaca paper trading API to execute real trades alongside Velocity's simulated paper trading. Each ETF (MTUM, QUAL, DYNF) will have its own separate Alpaca portfolio to handle ticker overlap and prevent false positives.

## Proposed Solution
Create an `AlpacaClient` module that uses Alpaca's Portfolio API to create and manage separate portfolios per ETF, enabling market order execution for paper trading.

---

## Implementation Tasks

### Phase 1: Alpaca Client Module
- [x] Create `src/alpaca_client.py` with `AlpacaClient` class
  - [x] Implement `__init__()` with API key loading from environment variables
  - [x] Implement `get_or_create_portfolio(etf)` to create/retrieve ETF-specific portfolio
  - [x] Implement `submit_order(etf, ticker, qty, side, order_type="market")` method
  - [x] Implement `get_positions(etf)` to fetch positions for specific portfolio
  - [x] Implement `get_account()` to fetch account cash/buying power
  - [x] Implement `get_order_history(etf)` to fetch order history for portfolio
  - [x] Add error handling for API failures and rate limits

### Phase 2: Configuration
- [x] Add Alpaca configuration to `src/config.py`
  - [x] Add `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` environment variable loading
  - [x] Add `ALPACA_PAPER` boolean flag (default True)
  - [x] Add `ALPACA_BASE_URL` for API endpoint selection

### Phase 3: Portfolio Integration
- [x] `velocity.py::submit_to_alpaca()` submits the session's entries/exits after the global book is updated
  - [x] DAY market orders, fractional qty (4 dp); one position per issuer is enforced by the book, not by Alpaca portfolios

### Phase 4: CLI Integration
- [x] Add CLI flags to `velocity.py`
  - [x] Add `--alpaca-trade` flag to enable Alpaca order execution
  - [x] Add `--alpaca-paper` flag (default True) to use paper vs live API
  - [x] Add `--sync-alpaca-positions` flag to import existing positions on startup

### Phase 5: Dependencies
- [x] Add `alpaca-py` to `requirements.txt`

### Phase 6: Testing
- [x] Create `tests/test_alpaca_client.py`
  - [x] Test `submit_order()` with mock API responses
  - [x] Test `get_positions()` with mock data
  - [x] Test error handling for API failures

### Phase 7: Documentation
- [x] Update `README.md` with Alpaca setup instructions
- [x] Document environment variable configuration

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
