# Changelog

## 2026-04-07

### Strategy Changes
- **Disable threshold strategy** — mean-reversion on crypto "Up or Down" markets kept betting against confirmed trends (e.g., buying DOWN when SOL was clearly pumping). Turned OFF by default.
- **Disable intraday momentum** — 5-min/15-min "Up or Down by 3PM" trades were coin flips at 50¢. Turned OFF by default.
- **Add strategy toggle flags** in `config.py`:
  - `ENABLE_THRESHOLD = False`
  - `ENABLE_ARB = True`
  - `ENABLE_SNIPE = True`
  - `ENABLE_MOMENTUM_INTRADAY = False`
- **Allow momentum in choppy regime** (if re-enabled) — was previously restricted to trending only.

### Claude News Gate (Upgraded)
- Structured system prompt requiring JSON response: `{ok_to_trade, reason, confidence}`
- Now passes Binance context: 60s/180s price change, volume ratio, buy pressure, 4h/1h HTF trend
- Conservative default: blocks trade on API failure (was: approve by default)
- Still Haiku-only, only called on momentum trades (~1-3/day)

### P&L Watcher Thread
- **New daemon thread** checks open positions every 3 seconds
- Auto-sell rules:
  - Cut loss at -30% P&L
  - Time exit: sell losing positions with <30s left
  - Lock profit: sell winning positions (>=15%) with <60s left
- Dead token handling: catches 404 "No orderbook" errors, marks position as closed in DB, never retries
- Sell failure handling: logs CRITICAL on 400 (insufficient balance) but keeps position open in DB

### Dead Token Fix
- `trader.py`: Added `DEAD_TOKENS` set at module level
- `get_midpoint()` and `get_orderbook()` return `None` on 404 instead of logging error every 3 seconds
- All callers (watcher, arb scanner, threshold) handle `None` gracefully

### Risk Management
- **Max positions reduced**: 4 → 2 (keeps buffer cash for sell order collateral)
- **Market blacklist**: skip markets with "election", "president", "vote", etc.
- **Threshold Binance check** (if re-enabled): won't bet against trend if Binance confirms the move (>65% implied prob)

### Active Strategies
1. **Arbitrage** — Buy Yes+No when total < $1.00 after fees (guaranteed profit)
2. **Resolution Sniper** — Buy near-certain outcomes priced at $0.90-$0.96 (near-guaranteed profit)
