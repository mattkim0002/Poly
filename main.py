"""Polymarket Trading Bot — entrypoint.

main.py owns only startup:
  - DB init
  - execution.log_mode() (paper/live)
  - Moondev smoke test
  - Cancel stale open orders
  - Clean ghost trades in DB
  - Sync existing positions
  - Compute + set session-start bankroll on bot_runtime
  - Print the startup banner

The trading cycle, strategy dispatch, and watcher threads live in bot_runtime.py.
All BUY-side entries in bot_runtime.py route through risk_manager → execution.py.
"""

import sys

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import config
from core import database, trader
import execution
import bot_runtime


def main():
    database.init_db()
    execution.log_mode()

    # Moondev smoke-test — log a small sample so we can see the client works.
    try:
        import moondev_client
        prices = moondev_client.get_prices()
        sample = (prices[:3] if isinstance(prices, list) else
                  dict(list(prices.items())[:3]) if isinstance(prices, dict) else
                  prices)
        print(f"[MOONDEV] get_prices() sample: {sample}")
        try:
            resp = moondev_client.call_moondev_ai(
                [{"role": "user", "content": "ping"}], max_tokens=16
            )
            print(f"[MOONDEV] AI ok: {str(resp)[:80]}")
        except moondev_client.MoondevAPIError as e:
            print(f"[MOONDEV] AI not available: {e}")
    except moondev_client.MoondevConfigError as e:
        print(f"[MOONDEV] skipped — {e}")
    except Exception as e:
        print(f"[MOONDEV] smoke-test failed: {e}")

    # Cancel stale orders (live only — paper state has no real CLOB orders)
    if not config.PAPER_TRADING:
        print("[INFO] Cancelling stale open orders...")
        try:
            client = trader.get_client()
            client.cancel_all()
            print("[INFO] All open orders cancelled")
        except Exception as e:
            print(f"[WARN] Failed to cancel orders: {e}")
    else:
        print("[INFO] Paper mode — skipping live order cancel")

    # Clean ghost trades (only against live positions; paper DB doesn't diverge)
    if not config.PAPER_TRADING:
        db_open = database.get_open_trades()
        live_positions = execution.get_positions()
        live_tokens = {p.get("asset", "") for p in (live_positions or []) if float(p.get("size", 0)) > 0}
        cleaned = 0
        for t in db_open:
            if t["token_id"] not in live_tokens and t.get("order_id") != "imported":
                database.update_trade_result(t["id"], t["entry_price"], 0.0, 0.0, "cancelled")
                cleaned += 1
        if cleaned:
            print(f"[INFO] Cleaned {cleaned} ghost trades from DB")

    # Sync existing positions (paper mode reads from paper state via execution)
    positions = execution.get_positions()
    pos_value = 0.0
    for p in (positions or []):
        size = float(p.get("size", 0))
        if size <= 0:
            continue
        token_id = p.get("asset", "")
        title = p.get("title", p.get("market", "unknown"))
        avg_price = float(p.get("avgPrice", p.get("price", 0.5)))
        cur_value = float(p.get("currentValue", 0))
        pos_value += cur_value

        already_tracked = any(t["token_id"] == token_id for t in database.get_open_trades())
        if not already_tracked and token_id:
            database.record_trade(
                market_id=p.get("conditionId", "imported"),
                market_question=title, token_id=token_id,
                side="BUY", outcome=p.get("outcome", "Yes"),
                entry_price=avg_price, size=size, cost=size * avg_price,
                order_id="imported", claude_probability=0.0,
                market_probability=avg_price, edge=0.0,
                kelly_frac=0.0, dd_mult=1.0, signal_mult=1.0,
            )
            print(f"  [SYNC] {title[:50]} | {size:.1f} shares @ ${avg_price:.3f}")

    bankroll = execution.get_balance()
    total_equity = bankroll + pos_value

    # Hand the session baseline to bot_runtime before the cycle starts.
    bot_runtime._session_start_bankroll = total_equity

    database.reset_peak_equity(total_equity)

    print()
    print("=" * 55)
    print("          POLYMARKET TRADING BOT")
    print(f"  Mode:        {'LIVE TRADING' if not config.DRY_RUN else 'DRY RUN'}")
    print(f"  Cash:        ${bankroll:.2f}")
    print(f"  Positions:   ${pos_value:.2f}")
    print(f"  Total:       ${total_equity:.2f}")
    active = bot_runtime._get_active_strategies()
    print(f"  Active:      {active}")
    from strategies.arbitrage import ARB_THRESHOLD_NORMAL, ARB_THRESHOLD_NEAR_EXPIRY, NEAR_EXPIRY_MINUTES
    print(f"  Arb:         {'ON' if config.ENABLE_ARB else 'OFF'} (normal<{ARB_THRESHOLD_NORMAL}, near-expiry<{ARB_THRESHOLD_NEAR_EXPIRY} within {NEAR_EXPIRY_MINUTES}min)")
    print(f"  Binance-Lag: {'ON (Sonnet gate)' if config.ENABLE_BINANCE_LAG else 'OFF'}")
    print(f"  Momentum:    {'ON (Sonnet gate)' if config.ENABLE_MOMENTUM_CLAUDE else 'OFF'}")
    print(f"  Sniper:      {'ON' if config.ENABLE_SNIPE else 'OFF'}")
    print(f"  Markets:     {', '.join(config.ALLOWED_MARKET_DURATIONS)} only")
    print(f"  Cycle:       {config.CYCLE_INTERVAL_SEC}s")
    print("=" * 55)
    print()

    bot_runtime.start()


if __name__ == "__main__":
    main()
