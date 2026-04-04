"""Polymarket Trading Bot — Crypto Sniper Mode

Scans crypto binary markets for momentum-based trading opportunities.
Uses real-time Binance data + Claude AI confirmation to detect edges
before Polymarket reprices.
"""

import sys
import time
import traceback
from datetime import date, datetime, timezone

# Force unbuffered output so nohup/log files update in real time
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import config
from core import database, market_data, trader, crypto_predictor
from strategies.risk import calculate_r_multiple, expectancy, drawdown_multiplier, drawdown
from strategies.arbitrage import scan_all_markets, execute_arb
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
    if config.PAPER_TRADING:
        mode = "PAPER TRADING"
    elif config.DRY_RUN:
        mode = "DRY RUN"
    else:
        mode = "LIVE TRADING"
    print()
    print("======================================================")
    print("               CRYPTO SNIPER                          ")
    print(f"  Mode:        {mode:<39}")
    print(f"  Bankroll:    ${bankroll:<39.2f}")
    print(f"  Min edge:    {config.MIN_EDGE_CRYPTO:.0%}{'':<35}")
    print(f"  Kelly:       {config.KELLY_FRACTION:.0%} Kelly{'':<31}")
    print(f"  Max per trade:{config.CRYPTO_MAX_POSITION_PCT:.0%} of bankroll{'':<25}")
    print(f"  Max positions:{config.MAX_OPEN_POSITIONS:<32}")
    print(f"  Cycle:       {config.CYCLE_INTERVAL_SEC}s{'':<36}")
    print(f"  Strategy:    Momentum + Claude confirmation{'':<12}")
    print("======================================================")
    print()
    if config.PAPER_TRADING:
        print("=" * 50)
        print("  *** PAPER TRADING MODE — NO REAL MONEY ***")
        print("=" * 50)
        print()
    elif not config.DRY_RUN:
        print("WARNING: LIVE TRADING MODE ENABLED")
        print("Real money will be used.")
        print()


def _print_portfolio(bankroll: float, open_trades: list[dict], recent_trades: list[dict]):
    global _daily_pnl

    if config.PAPER_TRADING:
        from core import paper_trader
        print(paper_trader.paper_portfolio_summary())
        print(f"  (Real account balance: ${bankroll:.2f})")
        print()
        return

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


def _evaluate_market(market: dict, bankroll: float) -> dict | None:
    """Evaluate a crypto market for momentum-based trading opportunity.

    Returns trade dict if edge found, None otherwise.
    """
    question = market["question"]
    token_ids = market.get("token_ids", [])

    if len(token_ids) < 2:
        return None

    # Check if this is a crypto up/down market
    if not crypto_predictor.is_crypto_updown_market(question):
        return None

    # Skip markets resolving in the past or within 5 minutes
    end_date = market.get("end_date") or market.get("endDate") or ""
    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            minutes_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 60
            if minutes_left < 0:
                print(f"  [SKIP] expired: '{question[:40]}'")
                return None
            if minutes_left < 5:
                print(f"  [SKIP] {minutes_left:.0f}m left: '{question[:40]}'")
                return None
        except (ValueError, TypeError):
            pass

    yes_token = token_ids[0]
    no_token = token_ids[1]

    # Get current market prices
    yes_price = trader.get_midpoint(yes_token)
    no_price = trader.get_midpoint(no_token)

    if not yes_price or not no_price:
        return None

    # Evaluate YES outcome
    yes_result = crypto_predictor.estimate_crypto_probability(question, yes_price, "Yes")
    # Evaluate NO outcome
    no_result = crypto_predictor.estimate_crypto_probability(question, no_price, "No")

    # Take the better signal (higher edge)
    best = None
    best_edge = 0.0

    if yes_result:
        yes_edge = abs(yes_result["probability"] - yes_price)
        if yes_edge > best_edge:
            best_edge = yes_edge
            best = {
                "market_id": market["id"],
                "question": question,
                "token_id": yes_token,
                "outcome": "Yes",
                "price": yes_price,
                "probability": yes_result["probability"],
                "edge": yes_edge,
                "signal": yes_result,
            }

    if no_result:
        no_edge = abs(no_result["probability"] - no_price)
        if no_edge > best_edge:
            best_edge = no_edge
            best = {
                "market_id": market["id"],
                "question": question,
                "token_id": no_token,
                "outcome": "No",
                "price": no_price,
                "probability": no_result["probability"],
                "edge": no_edge,
                "signal": no_result,
            }

    if not best or best_edge < config.MIN_EDGE_CRYPTO:
        return None

    return best


def _execute_trade(trade: dict, bankroll: float) -> bool:
    """Execute a momentum-based trade with Kelly sizing."""
    edge = trade["edge"]
    probability = trade["probability"]

    # In paper mode, use paper balance for sizing
    if config.PAPER_TRADING:
        from core import paper_trader
        bankroll = paper_trader.paper_get_balance()

    # Kelly position sizing
    size_dollars = bankroll * config.KELLY_FRACTION * edge
    # Cap at max position size
    max_size = config.CRYPTO_MAX_POSITION_PCT * bankroll
    size_dollars = min(size_dollars, max_size)

    if size_dollars < config.MIN_ORDER_SIZE_USD:
        print(f"  [SKIP] Size too small: ${size_dollars:.2f}")
        return False

    # Convert dollars to shares
    price = trade["price"]
    shares = int(size_dollars / price) if price > 0 else 0
    if shares < 5:
        shares = 5  # Polymarket minimum

    cost = shares * price

    prefix = "[PAPER] " if config.PAPER_TRADING else ""
    print(f"\n  == {prefix}MOMENTUM TRADE ========================")
    print(f"  Market:     {trade['question'][:45]}")
    print(f"  Outcome:    {trade['outcome']}")
    print(f"  Price:      ${price:.3f}")
    print(f"  Probability:{probability:.2f}")
    print(f"  Edge:       {edge:.1%}")
    print(f"  Shares:     {shares}")
    print(f"  Cost:       ${cost:.2f}")
    print(f"  Signal:     {trade['signal'].get('reasoning', '')[:80]}")
    print(f"  ============================================")

    if config.PAPER_TRADING:
        from core import paper_trader
        success = paper_trader.paper_buy(
            market_id=trade["market_id"],
            question=trade["question"],
            token_id=trade["token_id"],
            outcome=trade["outcome"],
            price=price,
            size=shares,
            edge=edge,
        )
        return success

    # Place BUY limit order (live trading only)
    order_id = trader.place_limit_order(
        token_id=trade["token_id"],
        price=price,
        size=shares,
        side="BUY",
    )

    if not order_id:
        print("  [FAIL] Order failed")
        return False

    print(f"  [SUCCESS] Order placed: {order_id}")

    # Record in database
    dd_mult = drawdown_multiplier(bankroll, database.get_peak_equity())
    database.record_trade(
        market_id=trade["market_id"],
        market_question=trade["question"],
        token_id=trade["token_id"],
        side="BUY",
        outcome=trade["outcome"],
        entry_price=price,
        size=shares,
        cost=cost,
        order_id=order_id,
        claude_probability=probability,
        market_probability=price,
        edge=edge,
        kelly_frac=config.KELLY_FRACTION,
        dd_mult=dd_mult,
        signal_mult=1.0,
    )

    return True


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

    if config.PAPER_TRADING:
        from core import paper_trader
        paper_balance = paper_trader.paper_get_balance()
        paper_positions = paper_trader.paper_get_open_positions()
        paper_pos_value = sum(p["entry_price"] * p["size"] for p in paper_positions)
        total_equity = paper_balance + paper_pos_value
        live_pos_value = 0.0

        print(f"[PAPER] Balance: ${paper_balance:.2f} | Open: {len(paper_positions)} | Total: ${total_equity:.2f} | (Real: ${bankroll:.2f})")

        # Check paper positions for exit conditions
        _check_existing_positions([])

        # Paper drawdown check
        state = paper_trader.load_paper_state()
        peak = state["peak_balance"]
        if peak > 0 and total_equity < peak:
            dd_pct = (peak - total_equity) / peak
            if dd_pct >= config.DD_THRESHOLD_STOP:
                print(f"[PAPER] DRAWDOWN HALT: {dd_pct:.1%} exceeds {config.DD_THRESHOLD_STOP:.0%}")
                _print_portfolio(bankroll, [], [])
                return

        # Paper position limit check
        if len(paper_positions) >= config.MAX_OPEN_POSITIONS:
            print(f"[PAPER] Max positions ({config.MAX_OPEN_POSITIONS}) reached, monitoring only")
            _print_portfolio(bankroll, [], [])
            return

        # Use paper balance for trade sizing
        effective_bankroll = paper_balance
        open_token_ids = {p["token_id"] for p in paper_positions}
        recent_trades = []
        open_trades = []
    else:
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

        # Daily loss limit — hard stop at 10% of bankroll
        daily_loss = _session_start_bankroll - total_equity  # How much we've lost today
        daily_loss_limit = bankroll * config.DAILY_LOSS_LIMIT_PCT  # Hard stop at 10% daily loss
        if daily_loss > daily_loss_limit:
            print(f"  [HALT] Daily loss limit hit (${daily_loss:.2f} > ${daily_loss_limit:.2f}) — no new trades today")
            _check_existing_positions(database.get_open_trades())
            _record_equity(bankroll, live_pos_value)
            _print_portfolio(bankroll, database.get_open_trades(), recent_trades)
            return  # Skip Steps 3-6, only monitor existing positions

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

        effective_bankroll = bankroll
        open_token_ids = {t["token_id"] for t in open_trades}

    # --- Step 3: Scan crypto markets ---
    print("[INFO] Step 3: Scanning crypto markets...")
    markets = market_data.get_active_markets(limit=config.MAX_MARKETS_PER_CYCLE)
    if not markets:
        print("[INFO] No markets pass filters")
        if not config.PAPER_TRADING:
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
    markets = [m for m in markets if not any(tid in open_token_ids for tid in m.get("token_ids", []))]

    # CRYPTO ONLY — only keep crypto up/down markets
    crypto_markets = [m for m in markets if crypto_predictor.is_crypto_updown_market(m.get("question", ""))]
    print(f"[INFO] {len(crypto_markets)} crypto up/down markets to evaluate")

    # --- Step 4: Check position limit ---
    if not config.PAPER_TRADING and len(open_trades) >= config.MAX_OPEN_POSITIONS:
        print(f"[INFO] Max positions ({config.MAX_OPEN_POSITIONS}) reached, monitoring only")
        _record_equity(bankroll, live_pos_value)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    # === STRATEGY 1: ARBITRAGE (guaranteed profit) ===
    # Scan ALL crypto markets for Yes+No < $0.98 opportunities
    print("[INFO] Step 4a: Scanning for arbitrage (guaranteed profit)...")
    arb_opportunities = scan_all_markets(crypto_markets)
    arb_trades_placed = 0

    if arb_opportunities:
        print(f"  Found {len(arb_opportunities)} arb opportunities!")
        for arb in arb_opportunities[:2]:  # Max 2 arb trades per cycle
            print(f"  ARB: {arb['question'][:45]} | Yes ${arb['yes_price']:.3f} + No ${arb['no_price']:.3f} = ${arb['total_cost']:.3f} | Profit: {arb['profit_pct']:.1%}")
            if not config.PAPER_TRADING:
                result = execute_arb(arb, effective_bankroll)
                if result:
                    arb_trades_placed += 1
                    effective_bankroll -= result["total_cost"]
                    # Record both sides in DB
                    database.record_trade(
                        market_id=result["market_id"],
                        market_question=f"[ARB-YES] {result['question'][:80]}",
                        token_id=result["yes_token"],
                        side="BUY", outcome="Yes",
                        entry_price=result["yes_price"],
                        size=result["shares"],
                        cost=result["shares"] * result["yes_price"],
                        order_id=result["yes_order"],
                        claude_probability=0.0, market_probability=result["yes_price"],
                        edge=result["profit_pct"], kelly_frac=0.0,
                        dd_mult=1.0, signal_mult=1.0,
                    )
                    database.record_trade(
                        market_id=result["market_id"],
                        market_question=f"[ARB-NO] {result['question'][:80]}",
                        token_id=result["no_token"],
                        side="BUY", outcome="No",
                        entry_price=result["no_price"],
                        size=result["shares"],
                        cost=result["shares"] * result["no_price"],
                        order_id=result["no_order"],
                        claude_probability=0.0, market_probability=result["no_price"],
                        edge=result["profit_pct"], kelly_frac=0.0,
                        dd_mult=1.0, signal_mult=1.0,
                    )
            else:
                print(f"  [PAPER] Would execute arb — skipping in paper mode")
    else:
        print("[INFO] No arb opportunities (spreads are tight)")

    if arb_trades_placed:
        print(f"[INFO] {arb_trades_placed} arb trade(s) placed (guaranteed profit)")

    # === STRATEGY 2: MOMENTUM (directional bets with Claude gate) ===
    # Only if we have bankroll left and position slots available
    if effective_bankroll < 5:
        print("[INFO] Not enough bankroll for momentum trades after arb")
        if not config.PAPER_TRADING:
            _record_equity(bankroll, live_pos_value)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    print("[INFO] Step 4b: Evaluating momentum signals (Claude gate)...")
    opportunities = []
    for market in crypto_markets:
        trade = _evaluate_market(market, effective_bankroll)
        if trade:
            opportunities.append(trade)
            print(f"  SIGNAL: {trade['question'][:50]} | {trade['outcome']} @ {trade['price']:.3f} | Edge: {trade['edge']:.1%}")

    if not opportunities:
        print("[INFO] No momentum signals found")
        if not config.PAPER_TRADING:
            _record_equity(bankroll, live_pos_value)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    # Sort by edge descending — best opportunity first
    opportunities.sort(key=lambda t: t["edge"], reverse=True)
    best = opportunities[0]

    print(f"\n[INFO] Step 5: Executing best momentum trade (edge: {best['edge']:.1%})...")
    _execute_trade(best, effective_bankroll)

    # --- Step 7: Record & summarize ---
    if not config.PAPER_TRADING:
        _record_equity(bankroll, live_pos_value)
    _print_portfolio(bankroll, open_trades, recent_trades)


def _check_existing_positions(open_trades: list[dict]):
    """Check positions for stop loss, take profit, or resolution.

    In paper mode, delegates to paper_trader.
    In live mode, only manages positions that actually filled (exist on Polymarket).
    """
    global _daily_pnl

    if config.PAPER_TRADING:
        from core import paper_trader
        paper_trader.paper_check_positions(trader.get_midpoint)
        return

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
    """Main loop — momentum scanner running every cycle."""
    global _session_start_bankroll
    database.init_db()

    if config.PAPER_TRADING:
        from core import paper_trader
        paper_balance = paper_trader.paper_get_balance()
        _session_start_bankroll = paper_balance
        # Still fetch real balance for reference display
        bankroll = trader.get_balance()
        print(f"[INFO] Real account balance: ${bankroll:.2f} (reference only)")
        print(f"[INFO] Paper balance: ${paper_balance:.2f}")
        _print_banner(paper_balance)
    else:
        # Cancel ALL unfilled orders on startup to free locked capital
        print("[INFO] Cancelling stale open orders...")
        try:
            client = trader.get_client()
            client.cancel_all()
            print("[INFO] All open orders cancelled — capital freed")
        except Exception as e:
            print(f"[WARN] Failed to cancel orders: {e}")

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
    print(f"[INFO] Strategy: Momentum + Claude confirmation")
    print(f"[INFO] Kelly: {config.KELLY_FRACTION:.0%} | Max position: {config.CRYPTO_MAX_POSITION_PCT:.0%} | Min edge: {config.MIN_EDGE_CRYPTO:.0%}")
    print()

    while True:
        try:
            run_cycle()
        except KeyboardInterrupt:
            print("\n[INFO] Shutting down Crypto Sniper...")
            break
        except Exception:
            print(f"[ERROR] Cycle failed:\n{traceback.format_exc()}")

        print(f"\n[INFO] Next cycle in {config.CYCLE_INTERVAL_SEC}s...")
        try:
            time.sleep(config.CYCLE_INTERVAL_SEC)
        except KeyboardInterrupt:
            print("\n[INFO] Shutting down Crypto Sniper...")
            break


if __name__ == "__main__":
    main()
