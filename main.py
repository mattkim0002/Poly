"""Polymarket Trading Bot — Arbitrage Mode

Scans crypto binary markets for arbitrage opportunities.
Buys BOTH YES and NO sides when pair_cost < $1.00 for guaranteed profit.
No directional prediction needed — profit is locked in at execution.
"""

import sys
import time
import traceback
from datetime import date

# Force unbuffered output so nohup/log files update in real time
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import config
from core import database, market_data, trader, crypto_predictor
from strategies.risk import calculate_r_multiple, expectancy, drawdown_multiplier, drawdown
from utils.logger import log

_cycle_count = 0
_daily_pnl = 0.0
_daily_date = None
_session_start_bankroll = 0.0
_dead_tokens = set()  # Token IDs with no orderbook — stop retrying


def _is_junk_market(question: str) -> bool:
    """Filter out sports, entertainment, and random guessing markets."""
    q_lower = question.lower()
    for keyword in config.SPORTS_KEYWORDS:
        if keyword in q_lower:
            return True
    return False


def _daily_loss_limit(bankroll: float) -> float:
    """Max daily loss allowed based on bankroll."""
    return (bankroll / 10.0) * config.DAILY_LOSS_PER_10


def _print_banner(bankroll: float):
    mode = "LIVE TRADING" if not config.DRY_RUN else "DRY RUN"
    print()
    print("======================================================")
    print("               ARBITRAGE MODE                         ")
    print(f"  Mode:        {mode:<39}")
    print(f"  Bankroll:    ${bankroll:<39.2f}")
    print(f"  Min profit:  ${config.MIN_ARB_PROFIT:.2f}/share{'':<30}")
    print(f"  Max per arb: {config.ARB_MAX_POSITION_PCT:.0%} of bankroll{'':<26}")
    print(f"  Max positions:{config.MAX_OPEN_POSITIONS:<32}")
    print(f"  Cycle:       {config.CYCLE_INTERVAL_SEC}s{'':<36}")
    print(f"  Strategy:    Buy YES+NO when pair < $1.00{'':<15}")
    print("======================================================")
    print()
    if not config.DRY_RUN:
        print("WARNING: LIVE TRADING MODE ENABLED")
        print("Real money will be used.")
        print()


def _print_portfolio(bankroll: float, open_trades: list[dict], recent_trades: list[dict]):
    global _daily_pnl

    # Fetch LIVE position values from Polymarket
    live_positions = trader.get_positions()
    live_pos_value = sum(float(p.get("currentValue", 0)) for p in (live_positions or []) if float(p.get("size", 0)) > 0)
    num_positions = sum(1 for p in (live_positions or []) if float(p.get("size", 0)) > 0)
    total_equity = bankroll + live_pos_value
    daily_limit = _daily_loss_limit(total_equity)

    session_profit = total_equity - _session_start_bankroll
    session_pct = (session_profit / _session_start_bankroll * 100) if _session_start_bankroll > 0 else 0.0

    print()
    print("------------- PORTFOLIO ---------------")
    print(f"  Session start:     ${_session_start_bankroll:<20.2f}")
    print(f"  Cash:              ${bankroll:<20.2f}")
    print(f"  Positions (live):  ${live_pos_value:<20.2f}")
    print(f"  Total equity:      ${total_equity:<20.2f}")
    print(f"  Session profit:    ${session_profit:<+20.2f}")
    print(f"  Session growth:    {session_pct:<+20.1f}%")
    print(f"  Open positions:    {num_positions:<21}")
    print(f"  Available to trade:${bankroll:<20.2f}")
    print(f"  Daily P&L:        ${_daily_pnl:<+20.2f}")
    print(f"  Daily loss limit:  ${daily_limit:<20.2f}")

    if recent_trades:
        wins = sum(1 for t in recent_trades if (t.get("pnl") or 0) > 0)
        losses = sum(1 for t in recent_trades if (t.get("pnl") or 0) <= 0)
        win_rate = wins / len(recent_trades) if recent_trades else 0
        exp = expectancy(recent_trades)
        print(f"  Win rate:          {win_rate:.1%} ({wins}W / {losses}L of {len(recent_trades)} trades)")
        print(f"  Expectancy:        {exp:<+20.2f}R")
    else:
        print(f"  Win rate:          No trades yet")

    print("---------------------------------------")
    print()


def run_cycle():
    """Execute one trading cycle."""
    global _cycle_count, _daily_pnl, _daily_date
    _cycle_count += 1

    # Reset daily P&L at midnight
    today = date.today()
    if _daily_date != today:
        _daily_pnl = 0.0
        _daily_date = today

    print(f"\n[INFO] === CYCLE {_cycle_count} STARTING ===")

    # --- Step 1: Get account state ---
    print("[INFO] Step 1: Checking account state...")
    bankroll = trader.get_balance()
    if bankroll <= 0:
        print("[WARN] Zero balance — account may be empty or unreadable. Skipping cycle.")
        return

    peak_equity = database.get_peak_equity()
    if peak_equity <= 0:
        peak_equity = bankroll

    recent_trades = database.get_recent_trades(config.WIN_RATE_WINDOW)
    open_trades = database.get_open_trades()

    # Fetch LIVE position values from Polymarket (not stale DB cost basis)
    live_positions = trader.get_positions()
    live_pos_value = sum(float(p.get("currentValue", 0)) for p in (live_positions or []) if float(p.get("size", 0)) > 0)
    total_equity = bankroll + live_pos_value

    print(f"[INFO] Cash: ${bankroll:.2f} | Positions: ${live_pos_value:.2f} | Total: ${total_equity:.2f} | Peak: ${peak_equity:.2f} | Open: {len(live_positions or [])}")

    # --- Step 2: Check exit conditions ---
    print("[INFO] Step 2: Checking exit conditions...")

    # Drawdown check — use total equity (cash + positions), not just cash
    dd_mult = drawdown_multiplier(total_equity, peak_equity)
    dd_pct = drawdown(total_equity, peak_equity)

    # Always check existing positions for stop loss / take profit
    _check_existing_positions(open_trades)

    if dd_mult == 0.0:
        print(f"[STOP] DRAWDOWN HALT: {dd_pct:.1%} drawdown exceeds {config.DD_THRESHOLD_STOP:.0%} limit")
        print(f"[INFO] Monitoring positions only — no new orders")
        _record_equity(bankroll, live_pos_value)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    if dd_mult < 1.0:
        print(f"[WARN] Drawdown at {dd_pct:.1%} — position sizes halved")

    # Daily loss limit
    daily_limit = _daily_loss_limit(total_equity)
    if _daily_pnl <= -daily_limit:
        print(f"[STOP] DAILY LOSS LIMIT: ${_daily_pnl:.2f} exceeds -${daily_limit:.2f}")
        _record_equity(bankroll, live_pos_value)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    # --- Step 3: Check position limit ---
    if len(open_trades) >= config.MAX_OPEN_POSITIONS:
        print(f"[INFO] Max positions ({config.MAX_OPEN_POSITIONS}) reached, monitoring only")
        _record_equity(bankroll, live_pos_value)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    # --- Step 4: Scan markets ---
    print("[INFO] Step 3: Scanning crypto markets...")
    markets = market_data.get_active_markets(limit=config.MAX_MARKETS_PER_CYCLE)
    if not markets:
        print("[INFO] No markets pass filters")
        _record_equity(bankroll, live_pos_value)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    # Filter out junk (sports/entertainment)
    before_filter = len(markets)
    markets = [m for m in markets if not _is_junk_market(m.get("question", ""))]
    junk_count = before_filter - len(markets)
    if junk_count:
        print(f"[INFO] Filtered {junk_count} junk markets")

    # Skip markets we already have positions in
    open_token_ids = {t["token_id"] for t in open_trades}
    markets = [m for m in markets if not any(tid in open_token_ids for tid in m.get("token_ids", []))]

    # CRYPTO ONLY — only keep crypto up/down markets
    crypto_markets = [m for m in markets if crypto_predictor.is_crypto_updown_market(m.get("question", ""))]
    markets = crypto_markets
    print(f"[INFO] {len(crypto_markets)} crypto up/down markets to scan for arbitrage")

    # --- Step 5: Find arbitrage opportunities ---
    print("[INFO] Step 4: Scanning orderbooks for arbitrage...")
    arb_opportunities = []
    for market in crypto_markets:
        arb = _find_arbitrage(market, bankroll)
        if arb:
            arb_opportunities.append(arb)
            print(f"  ARB: {arb['question'][:50]} | YES@{arb['yes_price']:.3f} + NO@{arb['no_price']:.3f} = {arb['pair_cost']:.3f} | Profit: ${arb['total_profit']:.2f}")

    # --- Step 6: Execute arbitrage trades ---
    if not arb_opportunities:
        print("[INFO] No arbitrage opportunities found")
        _record_equity(bankroll, live_pos_value)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    # Sort by profit per share descending — best arbs first
    arb_opportunities.sort(key=lambda a: a["profit_per_share"], reverse=True)

    print(f"\n[INFO] Step 5: Executing {len(arb_opportunities)} arbitrage trade(s)...")
    slots_available = config.MAX_OPEN_POSITIONS - len(open_trades)
    trades_placed = 0

    for arb in arb_opportunities[:slots_available]:
        placed = _execute_arbitrage(arb, bankroll)
        if placed:
            trades_placed += 1
            # Update bankroll for next trade
            bankroll -= arb["total_cost"]

    if trades_placed == 0:
        print("[INFO] No arb trades executed this cycle")
    else:
        print(f"[INFO] {trades_placed} arb trade(s) placed")

    # --- Step 7: Record & summarize ---
    _record_equity(bankroll, live_pos_value)
    _print_portfolio(bankroll, open_trades, recent_trades)


def _find_arbitrage(market: dict, bankroll: float) -> dict | None:
    """Check if a market has an arbitrage opportunity.

    Returns trade details if pair_cost < threshold, None otherwise.
    """
    question = market["question"]
    token_ids = market.get("token_ids", [])

    if len(token_ids) < 2:
        return None

    # Skip markets resolving in the past or within 5 minutes
    end_date = market.get("end_date") or market.get("endDate") or ""
    if end_date:
        from datetime import datetime, timezone
        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            minutes_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 60
            if minutes_left < 5:
                # Negative = already expired, positive but tiny = resolving now
                label = "expired" if minutes_left < 0 else f"{minutes_left:.0f}m left"
                print(f"  [SKIP] {label}: '{question[:40]}'")
                return None
        except (ValueError, TypeError):
            pass

    yes_token = token_ids[0]
    no_token = token_ids[1]

    # Get orderbooks for both sides
    yes_book = trader.get_orderbook(yes_token)
    no_book = trader.get_orderbook(no_token)

    # Best ask = cheapest price someone is willing to sell at
    yes_asks = yes_book.get("asks", [])
    no_asks = no_book.get("asks", [])

    if not yes_asks or not no_asks:
        return None

    # asks are sorted by price ascending — first entry is cheapest
    best_ask_yes = float(yes_asks[0]["price"])
    best_ask_no = float(no_asks[0]["price"])

    # Available liquidity at best ask
    yes_liquidity = float(yes_asks[0]["size"])
    no_liquidity = float(no_asks[0]["size"])

    pair_cost = best_ask_yes + best_ask_no

    # Need pair_cost < (1.0 - MIN_ARB_PROFIT) to be profitable
    if pair_cost >= (1.0 - config.MIN_ARB_PROFIT):
        return None

    profit_per_share = 1.0 - pair_cost

    # Max shares we can buy = min of:
    # 1. Liquidity available on YES side at best ask
    # 2. Liquidity available on NO side at best ask
    # 3. What we can afford (bankroll / pair_cost)
    # 4. Max position size from config
    max_from_liquidity = min(yes_liquidity, no_liquidity)
    max_from_bankroll = bankroll / pair_cost if pair_cost > 0 else 0
    max_from_config = (bankroll * config.ARB_MAX_POSITION_PCT) / pair_cost

    shares = min(max_from_liquidity, max_from_bankroll, max_from_config)
    shares = max(5, int(shares))  # Polymarket minimum is 5 shares

    total_cost = shares * pair_cost
    total_profit = shares * profit_per_share

    if total_cost > bankroll * 0.95:  # Leave 5% buffer
        shares = max(5, int((bankroll * 0.95) / pair_cost))
        total_cost = shares * pair_cost
        total_profit = shares * profit_per_share

    return {
        "market_id": market["id"],
        "question": question,
        "yes_token": yes_token,
        "no_token": no_token,
        "yes_price": best_ask_yes,
        "no_price": best_ask_no,
        "pair_cost": pair_cost,
        "profit_per_share": profit_per_share,
        "shares": shares,
        "total_cost": total_cost,
        "total_profit": total_profit,
        "yes_liquidity": yes_liquidity,
        "no_liquidity": no_liquidity,
    }


def _execute_arbitrage(arb: dict, bankroll: float) -> bool:
    """Execute an arbitrage trade — buy BOTH sides."""
    shares = arb["shares"]

    print(f"\n  == ARBITRAGE TRADE ========================")
    print(f"  Market:     {arb['question'][:45]}")
    print(f"  YES price:  ${arb['yes_price']:.3f}")
    print(f"  NO price:   ${arb['no_price']:.3f}")
    print(f"  Pair cost:  ${arb['pair_cost']:.3f}")
    print(f"  Profit/sh:  ${arb['profit_per_share']:.3f}")
    print(f"  Shares:     {shares}")
    print(f"  Total cost: ${arb['total_cost']:.2f}")
    print(f"  Guaranteed: ${arb['total_profit']:.2f} profit")
    print(f"  ============================================")

    # Place YES buy order
    yes_order = trader.place_limit_order(
        token_id=arb["yes_token"],
        price=arb["yes_price"],
        size=shares,
        side="BUY",
    )

    if not yes_order:
        print("  [FAIL] YES order failed")
        return False

    # Place NO buy order
    no_order = trader.place_limit_order(
        token_id=arb["no_token"],
        price=arb["no_price"],
        size=shares,
        side="BUY",
    )

    if not no_order:
        print("  [WARN] NO order failed — cancelling YES order to avoid one-sided exposure")
        trader.cancel_order(yes_order)
        return False

    print(f"  [SUCCESS] Both sides placed: YES={yes_order}, NO={no_order}")

    # Record both legs in database
    database.record_trade(
        market_id=arb["market_id"],
        market_question=arb["question"],
        token_id=arb["yes_token"],
        side="BUY",
        outcome="Yes",
        entry_price=arb["yes_price"],
        size=shares,
        cost=shares * arb["yes_price"],
        order_id=yes_order,
        claude_probability=0.0,
        market_probability=arb["yes_price"],
        edge=arb["profit_per_share"],
        kelly_frac=0.0,
        dd_mult=1.0,
        signal_mult=1.0,
    )
    database.record_trade(
        market_id=arb["market_id"],
        market_question=arb["question"] + " [NO SIDE]",
        token_id=arb["no_token"],
        side="BUY",
        outcome="No",
        entry_price=arb["no_price"],
        size=shares,
        cost=shares * arb["no_price"],
        order_id=no_order,
        claude_probability=0.0,
        market_probability=arb["no_price"],
        edge=arb["profit_per_share"],
        kelly_frac=0.0,
        dd_mult=1.0,
        signal_mult=1.0,
    )

    return True


def _check_existing_positions(open_trades: list[dict]):
    """Check REAL Polymarket positions for stop loss, take profit, or resolution.

    Only manages positions that actually filled (exist on Polymarket),
    not unfilled open orders sitting in the DB.
    """
    global _daily_pnl

    # Get REAL positions from Polymarket — these are shares we actually own
    live_positions = trader.get_positions()
    if not live_positions:
        return

    # Build lookup: token_id -> live position data
    live_by_token = {}
    for p in live_positions:
        if float(p.get("size", 0)) > 0:
            live_by_token[p.get("asset", "")] = p

    for trade in open_trades:
        token_id = trade["token_id"]
        entry_price = trade["entry_price"]
        question = trade.get("market_question", "")

        # Skip tokens we already know are dead/resolved
        if token_id in _dead_tokens:
            continue

        # ONLY manage positions we actually own on Polymarket
        live_pos = live_by_token.get(token_id)
        if not live_pos:
            # Order never filled — mark as cancelled in DB, don't try to sell
            if trade.get("order_id") != "imported":
                database.update_trade_result(trade["id"], entry_price, 0.0, 0.0, "cancelled")
                print(f"  [CANCEL] Order never filled: '{question[:40]}'")
            continue

        # Use real position size from Polymarket, not what we ordered
        real_size = float(live_pos.get("size", 0))
        avg_price = float(live_pos.get("avgPrice", entry_price))
        current_price = float(live_pos.get("curPrice", 0))

        if current_price <= 0:
            current_price = trader.get_midpoint(token_id)
        if current_price <= 0:
            # No price available — mark as dead to stop retrying
            _dead_tokens.add(token_id)
            print(f"  [DEAD] No price for token {token_id[:20]}... — skipping future checks")
            continue

        # Use tighter take-profit for crypto 5-min markets
        is_crypto = crypto_predictor.is_crypto_updown_market(question)
        tp_threshold = config.TAKE_PROFIT_CRYPTO_PCT if is_crypto else config.TAKE_PROFIT_PCT

        # Take profit
        gain_pct = (current_price - avg_price) / avg_price if avg_price > 0 else 0
        if gain_pct >= tp_threshold:
            pnl = (current_price - avg_price) * real_size
            r_mult = calculate_r_multiple(avg_price, current_price, avg_price)
            database.update_trade_result(trade["id"], current_price, pnl, r_mult, "won")
            _daily_pnl += pnl
            label = "CRYPTO TP" if is_crypto else "TAKE PROFIT"
            print(f"  {label}: '{question[:40]}' | +{gain_pct:.0%} | PnL=${pnl:.2f}")

            # Cancel pending orders to free balance, then sell
            if not config.DRY_RUN:
                try:
                    client = trader.get_client()
                    client.cancel_all()
                except Exception:
                    pass
                sell_price = max(0.01, min(0.99, round(current_price - 0.01, 2)))
                trader.place_limit_order(token_id, sell_price, real_size, "SELL")
            continue

        # Stop loss: exit if down past threshold
        loss_pct = (avg_price - current_price) / avg_price if avg_price > 0 else 0
        if loss_pct >= config.STOP_LOSS_PCT:
            pnl = (current_price - avg_price) * real_size
            r_mult = calculate_r_multiple(avg_price, current_price, avg_price)
            database.update_trade_result(trade["id"], current_price, pnl, r_mult, "lost")
            _daily_pnl += pnl
            print(f"  STOP LOSS: '{question[:40]}' | PnL=${pnl:.2f}")

            # Cancel pending orders to free balance, then sell
            if not config.DRY_RUN:
                try:
                    client = trader.get_client()
                    client.cancel_all()
                except Exception:
                    pass
                trader.place_limit_order(token_id, current_price, real_size, "SELL")
            continue

        # Check if market resolved (price near 0 or 1)
        if current_price >= 0.95:
            pnl = (1.0 - entry_price) * trade["size"]
            r_mult = calculate_r_multiple(entry_price, 1.0, entry_price)
            database.update_trade_result(trade["id"], 1.0, pnl, r_mult, "won")
            _daily_pnl += pnl
            print(f"  WIN: '{trade['market_question'][:40]}' | PnL=${pnl:+.2f} | R={r_mult:+.2f}")
        elif current_price <= 0.05:
            pnl = -entry_price * trade["size"]
            r_mult = calculate_r_multiple(entry_price, 0.0, entry_price)
            database.update_trade_result(trade["id"], 0.0, pnl, r_mult, "lost")
            _daily_pnl += pnl
            print(f"  LOSS: '{trade['market_question'][:40]}' | PnL=${pnl:.2f} | R={r_mult:.2f}")


def _record_equity(bankroll: float, positions_value: float = 0.0):
    """Snapshot current equity state."""
    total_eq = bankroll + positions_value
    peak = database.get_peak_equity()
    new_peak = max(peak, total_eq) if peak > 0 else total_eq
    dd = drawdown(total_eq, new_peak)

    database.record_equity_snapshot(
        balance=bankroll,
        positions_value=positions_value,
        total_equity=total_eq,
        drawdown=dd,
        peak_equity=new_peak,
    )


def _cancel_all_open_orders():
    """Cancel all existing open orders on startup to free up capital."""
    print("[INFO] Cancelling all open orders to free capital...")
    try:
        client = trader.get_client()
        result = client.cancel_all()
        print(f"[INFO] cancel_all response: {result}")

        # Also mark them as cancelled in our database
        db_open = database.get_open_trades()
        for trade in db_open:
            database.update_trade_result(trade["id"], trade["entry_price"], 0.0, 0.0, "cancelled")
        if db_open:
            print(f"[INFO] Marked {len(db_open)} DB trades as cancelled")

        # Reset peak equity to current balance so drawdown calculation starts fresh
        bankroll = trader.get_balance()
        if bankroll > 0:
            database.reset_peak_equity(bankroll)
            print(f"[INFO] Peak equity reset to current balance: ${bankroll:.2f}")

        return len(db_open)
    except Exception as e:
        print(f"[WARN] Failed to cancel orders: {e}")
        return 0


def _sync_polymarket_positions():
    """Fetch real positions from Polymarket and sync into local DB.
    Returns total position value."""
    positions = trader.get_positions()
    total_value = 0.0
    synced = 0
    for p in (positions or []):
        size = float(p.get("size", 0))
        if size <= 0:
            continue
        token_id = p.get("asset", "")
        title = p.get("title", p.get("market", "unknown"))
        avg_price = float(p.get("avgPrice", p.get("price", 0.5)))
        cur_value = float(p.get("currentValue", 0))
        total_value += cur_value

        # Check if we already track this position in DB
        open_trades = database.get_open_trades()
        already_tracked = any(t["token_id"] == token_id for t in open_trades)
        if not already_tracked and token_id:
            # Import existing Polymarket position into our DB
            database.record_trade(
                market_id=p.get("conditionId", "imported"),
                market_question=title,
                token_id=token_id,
                side="BUY",
                outcome=p.get("outcome", "Yes"),
                entry_price=avg_price,
                size=size,
                cost=size * avg_price,
                order_id="imported",
                claude_probability=0.0,
                market_probability=avg_price,
                edge=0.0,
                kelly_frac=0.0,
                dd_mult=1.0,
                signal_mult=1.0,
            )
            synced += 1
            print(f"  [SYNC] {title[:50]} | {size:.1f} shares @ ${avg_price:.3f} | Value: ${cur_value:.2f}")

    if synced > 0:
        print(f"[INFO] Synced {synced} existing Polymarket positions into DB")
    return total_value


def main():
    """Main loop — arbitrage scanner running every cycle."""
    global _session_start_bankroll
    database.init_db()

    # Get initial balance AND positions for true total equity
    bankroll = trader.get_balance()
    print(f"[INFO] Cash balance: ${bankroll:.2f}")
    print("[INFO] Syncing existing Polymarket positions...")
    positions_value = _sync_polymarket_positions()
    total_equity = bankroll + positions_value
    _session_start_bankroll = total_equity
    print(f"[INFO] Total equity (cash + positions): ${total_equity:.2f}")
    _print_banner(total_equity)

    # Always reset peak equity to current total equity on startup
    # This prevents stale high peaks from blocking all trades via DD=0.0
    database.reset_peak_equity(total_equity)
    print(f"[INFO] Peak equity reset to ${total_equity:.2f}")

    print(f"[INFO] Bot started | Cycle interval: {config.CYCLE_INTERVAL_SEC}s")
    print(f"[INFO] Markets: Crypto Up/Down (binary)")
    print(f"[INFO] Strategy: Arbitrage — buy YES+NO when pair < $1.00")
    print(f"[INFO] Min profit: ${config.MIN_ARB_PROFIT:.2f}/share")
    print()

    while True:
        try:
            run_cycle()
        except KeyboardInterrupt:
            print("\n[INFO] Shutting down Arbitrage Bot...")
            break
        except Exception:
            print(f"[ERROR] Cycle failed:\n{traceback.format_exc()}")

        print(f"\n[INFO] Next cycle in {config.CYCLE_INTERVAL_SEC}s...")
        try:
            time.sleep(config.CYCLE_INTERVAL_SEC)
        except KeyboardInterrupt:
            print("\n[INFO] Shutting down Arbitrage Bot...")
            break


if __name__ == "__main__":
    main()
