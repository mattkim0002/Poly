"""Polymarket Trading Bot — Arb Scanner + Resolution Sniper

Two strategies with real edge:
1. ARBITRAGE: Buy Yes+No when total < $1.00 after fees → guaranteed profit
2. RESOLUTION SNIPER: Buy near-certain outcomes priced below $0.95 → near-guaranteed profit

No Claude API calls. No momentum. No coin flips. Pure math.
"""

import sys
import time
import traceback
from datetime import date, datetime, timezone

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import config
from core import database, market_data, trader, crypto_predictor
from strategies.risk import calculate_r_multiple, expectancy, drawdown_multiplier, drawdown
from strategies.arbitrage import scan_all_markets, execute_arb
from strategies.ev import get_fee_rate, is_fee_free_market
from utils.logger import log

_cycle_count = 0
_daily_pnl = 0.0
_daily_date = None
_session_start_bankroll = 0.0
_dead_tokens = set()


def _is_junk_market(question: str) -> bool:
    q_lower = question.lower()
    for keyword in config.SPORTS_KEYWORDS:
        if keyword in q_lower:
            return True
    return False


# ──────────────────────────────────────────────────────────
# STRATEGY 2: RESOLUTION SNIPER
# Find markets where outcome is near-certain but price < $0.95
# Example: "Will BTC be above $50k on April 7?" and BTC is at $84k
#          → Yes token trading at $0.92 → buy, collect $1.00 at resolution
# ──────────────────────────────────────────────────────────

def scan_resolution_snipes(markets: list[dict]) -> list[dict]:
    """Find near-certain outcomes priced below face value.

    Looks for:
    - Outcomes priced $0.90-$0.96 (market is ~sure but not $1.00 yet)
    - OR outcomes priced $0.04-$0.10 on the OTHER side (same thing)

    Profit = $1.00 - buy_price - fees.
    """
    snipes = []

    for market in markets:
        question = market.get("question", "")
        token_ids = market.get("token_ids", [])
        outcome_prices = market.get("outcome_prices", [])

        if len(token_ids) < 2 or len(outcome_prices) < 2:
            continue

        yes_price = outcome_prices[0]
        no_price = outcome_prices[1] if len(outcome_prices) > 1 else (1.0 - yes_price)

        # Check each side for near-certain pricing
        for i, (token_id, price, outcome) in enumerate([
            (token_ids[0], yes_price, "Yes"),
            (token_ids[1], no_price, "No"),
        ]):
            # We want to buy tokens priced 0.90-0.96
            # Below 0.90 = market isn't sure enough
            # Above 0.96 = not enough profit margin
            if price < 0.90 or price > 0.96:
                continue

            # Get REAL orderbook ask (what we can actually buy at)
            book = trader.get_orderbook(token_id)
            if not book or not book.get("asks"):
                continue

            real_ask = book["asks"][0]["price"]
            ask_size = book["asks"][0]["size"]

            if real_ask > 0.96 or real_ask < 0.90:
                continue

            # Calculate profit after fees
            fee_free = is_fee_free_market(question)
            if fee_free:
                fee_per_share = 0.0
            else:
                fee_rate = get_fee_rate(token_id)
                fee_per_share = fee_rate * real_ask * (1.0 - real_ask)

            profit_per_share = 1.0 - real_ask - fee_per_share

            if profit_per_share < 0.02:  # Need at least 2% profit
                continue

            profit_pct = profit_per_share / real_ask

            snipes.append({
                "question": question,
                "market_id": market.get("id", ""),
                "token_id": token_id,
                "outcome": outcome,
                "ask_price": real_ask,
                "ask_size": ask_size,
                "profit_per_share": profit_per_share,
                "profit_pct": profit_pct,
                "fee_per_share": fee_per_share,
                "fee_free": fee_free,
            })

    snipes.sort(key=lambda x: x["profit_pct"], reverse=True)
    return snipes


def execute_snipe(snipe: dict, bankroll: float) -> bool:
    """Buy a near-certain outcome at a discount."""
    max_spend = min(bankroll * 0.30, bankroll - 2.0)  # Keep $2 reserve
    if max_spend < config.MIN_ORDER_SIZE_USD:
        return False

    price = snipe["ask_price"]
    max_shares_by_bank = int(max_spend / price) if price > 0 else 0
    max_shares_by_book = int(snipe["ask_size"])
    shares = min(max_shares_by_bank, max_shares_by_book)
    shares = max(shares, 5)  # Polymarket minimum

    cost = shares * price
    expected_profit = shares * snipe["profit_per_share"]

    print(f"\n  === RESOLUTION SNIPE ===")
    print(f"  Market:  {snipe['question'][:60]}")
    print(f"  Side:    {snipe['outcome']} @ ${price:.3f}")
    print(f"  Shares:  {shares}")
    print(f"  Cost:    ${cost:.2f}")
    print(f"  Profit:  ${expected_profit:.2f} ({snipe['profit_pct']:.1%})")
    fee_str = "FREE" if snipe['fee_free'] else f"${snipe['fee_per_share']:.4f}/share"
    print(f"  Fees:    {fee_str}")
    print(f"  =========================")

    order_id = trader.place_limit_order(
        token_id=snipe["token_id"],
        price=price,
        size=shares,
        side="BUY",
    )

    if not order_id:
        print(f"  [FAIL] Order failed")
        return False

    print(f"  [SUCCESS] Order placed: {order_id}")

    database.record_trade(
        market_id=snipe["market_id"],
        market_question=f"[SNIPE] {snipe['question'][:80]}",
        token_id=snipe["token_id"],
        side="BUY",
        outcome=snipe["outcome"],
        entry_price=price,
        size=shares,
        cost=cost,
        order_id=order_id,
        claude_probability=0.0,
        market_probability=price,
        edge=snipe["profit_pct"],
        kelly_frac=0.0,
        dd_mult=1.0,
        signal_mult=1.0,
    )
    return True


# ──────────────────────────────────────────────────────────
# POSITION MANAGEMENT
# ──────────────────────────────────────────────────────────

def _check_existing_positions(open_trades: list[dict]):
    """Check open positions for resolution or exit conditions."""
    global _daily_pnl

    live_positions = trader.get_positions()
    if not live_positions:
        return

    live_by_token = {}
    for p in live_positions:
        if float(p.get("size", 0)) > 0:
            live_by_token[p.get("asset", "")] = p

    for trade in open_trades:
        token_id = trade["token_id"]
        entry_price = trade["entry_price"]
        question = trade.get("market_question", "")

        if token_id in _dead_tokens:
            continue

        live_pos = live_by_token.get(token_id)
        if not live_pos:
            if trade.get("order_id") != "imported":
                database.update_trade_result(trade["id"], entry_price, 0.0, 0.0, "cancelled")
                print(f"  [CANCEL] Never filled: '{question[:40]}'")
            continue

        real_size = float(live_pos.get("size", 0))
        avg_price = float(live_pos.get("avgPrice", entry_price))
        current_price = float(live_pos.get("curPrice", 0))

        if current_price <= 0:
            current_price = trader.get_midpoint(token_id)
        if current_price <= 0:
            _dead_tokens.add(token_id)
            continue

        # Market resolved → WIN
        if current_price >= 0.95:
            pnl = (1.0 - avg_price) * real_size
            r_mult = calculate_r_multiple(avg_price, 1.0, avg_price)
            database.update_trade_result(trade["id"], 1.0, pnl, r_mult, "won")
            _daily_pnl += pnl
            print(f"  WIN: '{question[:40]}' | PnL=${pnl:+.2f}")

        # Market resolved → LOSS
        elif current_price <= 0.05:
            pnl = -avg_price * real_size
            r_mult = calculate_r_multiple(avg_price, 0.0, avg_price)
            database.update_trade_result(trade["id"], 0.0, pnl, r_mult, "lost")
            _daily_pnl += pnl
            print(f"  LOSS: '{question[:40]}' | PnL=${pnl:.2f}")

        # Stop loss for non-arb positions
        elif "[ARB" not in question and "[SNIPE]" not in question:
            loss_pct = (avg_price - current_price) / avg_price if avg_price > 0 else 0
            if loss_pct >= config.STOP_LOSS_PCT:
                pnl = (current_price - avg_price) * real_size
                r_mult = calculate_r_multiple(avg_price, current_price, avg_price)
                database.update_trade_result(trade["id"], current_price, pnl, r_mult, "lost")
                _daily_pnl += pnl
                print(f"  STOP LOSS: '{question[:40]}' | PnL=${pnl:.2f}")
                if not config.DRY_RUN:
                    trader.place_limit_order(token_id, current_price, real_size, "SELL")


def _record_equity(bankroll: float, positions_value: float = 0.0):
    total_eq = bankroll + positions_value
    peak = database.get_peak_equity()
    new_peak = max(peak, total_eq) if peak > 0 else total_eq
    dd = drawdown(total_eq, new_peak)
    database.record_equity_snapshot(
        balance=bankroll, positions_value=positions_value,
        total_equity=total_eq, drawdown=dd, peak_equity=new_peak,
    )


def _print_portfolio(bankroll: float, open_trades: list[dict], recent_trades: list[dict]):
    global _daily_pnl
    live_positions = trader.get_positions()
    live_pos_value = sum(float(p.get("currentValue", 0)) for p in (live_positions or []) if float(p.get("size", 0)) > 0)
    num_positions = sum(1 for p in (live_positions or []) if float(p.get("size", 0)) > 0)
    total_equity = bankroll + live_pos_value

    session_profit = total_equity - _session_start_bankroll
    session_pct = (session_profit / _session_start_bankroll * 100) if _session_start_bankroll > 0 else 0.0

    print()
    print("------------- PORTFOLIO ---------------")
    print(f"  Cash:              ${bankroll:<20.2f}")
    print(f"  Positions (live):  ${live_pos_value:<20.2f}")
    print(f"  Total equity:      ${total_equity:<20.2f}")
    print(f"  Session profit:    ${session_profit:<+20.2f}")
    print(f"  Open positions:    {num_positions:<21}")
    print(f"  Daily P&L:        ${_daily_pnl:<+20.2f}")

    if recent_trades:
        wins = sum(1 for t in recent_trades if (t.get("pnl") or 0) > 0)
        losses = sum(1 for t in recent_trades if (t.get("pnl") or 0) <= 0)
        exp = expectancy(recent_trades)
        print(f"  Win rate:          {wins}/{wins+losses} ({wins/(wins+losses)*100:.0f}%)" if wins+losses > 0 else "")
        print(f"  Expectancy:        {exp:<+20.2f}R")
    print("---------------------------------------")
    print()


def _execute_momentum_trade(trade: dict, bankroll: float) -> bool:
    """Execute a momentum trade with Kelly sizing."""
    edge = trade["edge"]
    price = trade["price"]
    kelly_mult = trade.get("signal", {}).get("kelly_mult", 1.0)

    size_dollars = bankroll * config.KELLY_FRACTION * edge * kelly_mult
    max_size = config.CRYPTO_MAX_POSITION_PCT * bankroll
    size_dollars = min(size_dollars, max_size)

    if size_dollars < config.MIN_ORDER_SIZE_USD:
        return False

    shares = max(5, int(size_dollars / price)) if price > 0 else 5
    cost = shares * price

    print(f"\n  === MOMENTUM TRADE ===")
    print(f"  Market:  {trade['question'][:50]}")
    print(f"  Side:    {trade['outcome']} @ ${price:.3f}")
    print(f"  Edge:    {edge:.1%}")
    print(f"  Shares:  {shares} (${cost:.2f})")
    print(f"  ======================")

    order_id = trader.place_limit_order(
        token_id=trade["token_id"],
        price=price,
        size=shares,
        side="BUY",
    )

    if not order_id:
        print(f"  [FAIL] Order failed")
        return False

    print(f"  [SUCCESS] Order placed: {order_id}")

    database.record_trade(
        market_id=trade["market_id"],
        market_question=trade["question"],
        token_id=trade["token_id"],
        side="BUY", outcome=trade["outcome"],
        entry_price=price, size=shares, cost=cost,
        order_id=order_id,
        claude_probability=trade["probability"],
        market_probability=price,
        edge=edge, kelly_frac=config.KELLY_FRACTION,
        dd_mult=1.0, signal_mult=1.0,
    )
    return True


# ──────────────────────────────────────────────────────────
# MAIN CYCLE
# ──────────────────────────────────────────────────────────

def run_cycle():
    global _cycle_count, _daily_pnl, _daily_date
    _cycle_count += 1

    today = date.today()
    if _daily_date != today:
        _daily_pnl = 0.0
        _daily_date = today

    print(f"\n[INFO] === CYCLE {_cycle_count} ===")

    bankroll = trader.get_balance()
    if bankroll <= 0:
        print("[WARN] Zero balance — skipping cycle")
        return

    # Check existing positions
    open_trades = database.get_open_trades()
    recent_trades = database.get_recent_trades(config.WIN_RATE_WINDOW)

    live_positions = trader.get_positions()
    live_pos_value = sum(float(p.get("currentValue", 0)) for p in (live_positions or []) if float(p.get("size", 0)) > 0)
    num_positions = sum(1 for p in (live_positions or []) if float(p.get("size", 0)) > 0)
    total_equity = bankroll + live_pos_value

    print(f"[INFO] Cash: ${bankroll:.2f} | Positions: ${live_pos_value:.2f} ({num_positions}) | Total: ${total_equity:.2f}")

    # Always check exits
    if open_trades:
        _check_existing_positions(open_trades)

    # Daily loss limit
    daily_loss = _session_start_bankroll - total_equity
    if daily_loss > bankroll * config.DAILY_LOSS_LIMIT_PCT:
        print(f"[HALT] Daily loss limit hit (${daily_loss:.2f})")
        _record_equity(bankroll, live_pos_value)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    # Max positions check
    if num_positions >= config.MAX_OPEN_POSITIONS:
        print(f"[INFO] Max positions ({config.MAX_OPEN_POSITIONS}) reached — monitoring only")
        _record_equity(bankroll, live_pos_value)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    # Fetch ALL markets
    print("[INFO] Scanning all markets...")
    markets = market_data.get_active_markets(limit=config.MAX_MARKETS_PER_CYCLE)
    if not markets:
        print("[INFO] No markets found")
        _record_equity(bankroll, live_pos_value)
        return

    # Filter junk
    markets = [m for m in markets if not _is_junk_market(m.get("question", ""))]

    # Skip markets we already hold
    open_token_ids = {t["token_id"] for t in open_trades}
    markets = [m for m in markets if not any(tid in open_token_ids for tid in m.get("token_ids", []))]

    print(f"[INFO] {len(markets)} markets to scan")

    trades_placed = 0

    # === STRATEGY 1: ARBITRAGE (guaranteed profit) ===
    print("[INFO] Strategy 1: Scanning for arbitrage...")
    arb_opportunities = scan_all_markets(markets)

    if arb_opportunities:
        print(f"  Found {len(arb_opportunities)} arb opportunities!")
        for arb in arb_opportunities[:2]:
            if bankroll < config.MIN_ORDER_SIZE_USD * 2:
                break
            print(f"  ARB: {arb['question'][:50]} | Yes ${arb['yes_price']:.3f} + No ${arb['no_price']:.3f} = ${arb['total_cost']:.3f} | Profit: {arb['profit_pct']:.1%}")
            result = execute_arb(arb, bankroll)
            if result:
                trades_placed += 1
                bankroll -= result["total_cost"]
                database.record_trade(
                    market_id=result["market_id"],
                    market_question=f"[ARB-YES] {result['question'][:80]}",
                    token_id=result["yes_token"],
                    side="BUY", outcome="Yes",
                    entry_price=result["yes_price"], size=result["shares"],
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
                    entry_price=result["no_price"], size=result["shares"],
                    cost=result["shares"] * result["no_price"],
                    order_id=result["no_order"],
                    claude_probability=0.0, market_probability=result["no_price"],
                    edge=result["profit_pct"], kelly_frac=0.0,
                    dd_mult=1.0, signal_mult=1.0,
                )
    else:
        print("[INFO] No arb opportunities (spreads are tight)")

    # === STRATEGY 2: RESOLUTION SNIPER (near-certain at discount) ===
    if bankroll >= config.MIN_ORDER_SIZE_USD:
        print("[INFO] Strategy 2: Scanning for resolution snipes...")
        snipes = scan_resolution_snipes(markets)

        if snipes:
            print(f"  Found {len(snipes)} snipe opportunities!")
            for snipe in snipes[:2]:
                if bankroll < config.MIN_ORDER_SIZE_USD:
                    break
                print(f"  SNIPE: {snipe['question'][:50]} | {snipe['outcome']} @ ${snipe['ask_price']:.3f} | Profit: {snipe['profit_pct']:.1%}")
                if execute_snipe(snipe, bankroll):
                    trades_placed += 1
                    bankroll -= snipe["ask_price"] * 5  # Approximate cost
        else:
            print("[INFO] No snipe opportunities (nothing near-certain at a discount)")

    # === STRATEGY 3: MOMENTUM (strict — trending + entry window + 3+ boosters) ===
    if bankroll >= config.MIN_ORDER_SIZE_USD and trades_placed == 0:
        regime = crypto_predictor.get_regime()
        candle = crypto_predictor.get_candle_position()
        print(f"[INFO] Strategy 3: Momentum check | regime={regime} candle={candle['phase']} ({candle['seconds_elapsed']}s in)")

        if regime == "trending" and candle["phase"] == "entry_window":
            # Only scan crypto up/down markets for momentum
            crypto_markets = [m for m in markets
                              if crypto_predictor.is_crypto_updown_market(m.get("question", ""))]

            if crypto_markets:
                print(f"[INFO] Evaluating {len(crypto_markets)} crypto markets (trending + entry window)...")
                best_trade = None
                best_edge = 0.0

                for market in crypto_markets:
                    question = market["question"]
                    token_ids = market.get("token_ids", [])
                    if len(token_ids) < 2:
                        continue

                    yes_token, no_token = token_ids[0], token_ids[1]
                    yes_price = trader.get_midpoint(yes_token)
                    no_price = trader.get_midpoint(no_token)
                    if not yes_price or not no_price:
                        continue

                    # Check both sides
                    for outcome, token_id, price in [("Yes", yes_token, yes_price), ("No", no_token, no_price)]:
                        result = crypto_predictor.estimate_crypto_probability(question, price, outcome)
                        if not result:
                            continue
                        # STRICT: only high confidence (3+ boosters)
                        if result.get("confidence") != "high":
                            continue
                        edge = abs(result["probability"] - price)
                        if edge > best_edge and edge >= 0.05:
                            best_edge = edge
                            best_trade = {
                                "market_id": market["id"],
                                "question": question,
                                "token_id": token_id,
                                "outcome": outcome,
                                "price": price,
                                "probability": result["probability"],
                                "edge": edge,
                                "signal": result,
                            }

                if best_trade:
                    combo = best_trade["signal"].get("combo", "?")
                    print(f"  MOMENTUM: {best_trade['question'][:50]} | {best_trade['outcome']} @ {best_trade['price']:.3f} | Edge: {best_trade['edge']:.1%} | combo={combo}")
                    _execute_momentum_trade(best_trade, bankroll)
                    trades_placed += 1
                else:
                    print("[INFO] No momentum signals pass strict filters")
        else:
            skip_reason = f"regime={regime}" if regime != "trending" else f"candle={candle['phase']}"
            print(f"[INFO] Momentum skipped ({skip_reason})")

    if trades_placed:
        print(f"\n[INFO] {trades_placed} trade(s) placed this cycle")

    _record_equity(bankroll, live_pos_value)
    _print_portfolio(bankroll, open_trades, recent_trades)


# ──────────────────────────────────────────────────────────
# STARTUP
# ──────────────────────────────────────────────────────────

def main():
    global _session_start_bankroll
    database.init_db()

    # Cancel stale orders
    print("[INFO] Cancelling stale open orders...")
    try:
        client = trader.get_client()
        client.cancel_all()
        print("[INFO] All open orders cancelled")
    except Exception as e:
        print(f"[WARN] Failed to cancel orders: {e}")

    # Clean ghost trades
    db_open = database.get_open_trades()
    live_positions = trader.get_positions()
    live_tokens = {p.get("asset", "") for p in (live_positions or []) if float(p.get("size", 0)) > 0}
    cleaned = 0
    for t in db_open:
        if t["token_id"] not in live_tokens and t.get("order_id") != "imported":
            database.update_trade_result(t["id"], t["entry_price"], 0.0, 0.0, "cancelled")
            cleaned += 1
    if cleaned:
        print(f"[INFO] Cleaned {cleaned} ghost trades from DB")

    # Sync existing positions
    positions = trader.get_positions()
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

    bankroll = trader.get_balance()
    total_equity = bankroll + pos_value
    _session_start_bankroll = total_equity

    database.reset_peak_equity(total_equity)

    print()
    print("=" * 55)
    print("          ARB SCANNER + RESOLUTION SNIPER")
    print(f"  Mode:        {'LIVE TRADING' if not config.DRY_RUN else 'DRY RUN'}")
    print(f"  Cash:        ${bankroll:.2f}")
    print(f"  Positions:   ${pos_value:.2f}")
    print(f"  Total:       ${total_equity:.2f}")
    print(f"  Strategy 1:  Arbitrage (Yes+No < $1.00)")
    print(f"  Strategy 2:  Resolution snipe ($0.90-$0.96)")
    print(f"  Strategy 3:  Momentum (trending + entry window + 3+ signals)")
    print(f"  Claude API:  NONE (zero credit burn)")
    print(f"  Cycle:       {config.CYCLE_INTERVAL_SEC}s")
    print("=" * 55)
    print()

    while True:
        try:
            run_cycle()
        except KeyboardInterrupt:
            print("\n[INFO] Shutting down...")
            break
        except Exception:
            print(f"[ERROR] Cycle failed:\n{traceback.format_exc()}")

        print(f"\n[INFO] Next cycle in {config.CYCLE_INTERVAL_SEC}s...")
        try:
            time.sleep(config.CYCLE_INTERVAL_SEC)
        except KeyboardInterrupt:
            print("\n[INFO] Shutting down...")
            break


if __name__ == "__main__":
    main()
