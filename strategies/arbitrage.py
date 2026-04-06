"""Arbitrage scanner — finds ANY markets where Yes + No < $1.00.

Strategy:
1. Scan ALL active markets (crypto, politics, events, anything)
2. Check REAL orderbook prices (actual fillable asks)
3. If best_ask(Yes) + best_ask(No) < $1.00 after fees → guaranteed profit
4. Buy both sides → wait for resolution → collect $1.00 per share pair
5. Profit = $1.00 - cost_yes - cost_no - fee (per share)

Geopolitics/world events = ZERO fees → best arb targets.
"""

from core import trader
from strategies.ev import get_fee_rate, calculate_taker_fee, is_fee_free_market
from utils.logger import log
import config


def scan_arb_opportunity(market: dict) -> dict | None:
    """Check if a market has an arbitrage opportunity.

    Post Feb 18 2026: uses dynamic fee model per token.
    Prioritizes fee-free markets (geopolitics) where taker arb is still viable.

    Returns dict with trade details if arb exists, None otherwise.
    """
    question = market.get("question", "")
    token_ids = market.get("token_ids", [])

    if len(token_ids) < 2:
        return None

    yes_token = token_ids[0]
    no_token = token_ids[1]

    # Get REAL orderbooks — we need actual fillable ask prices, not midpoints
    yes_book = trader.get_orderbook(yes_token)
    no_book = trader.get_orderbook(no_token)

    if not yes_book.get("asks") or not no_book.get("asks"):
        return None

    # Best ask = cheapest price we can BUY at
    yes_best_ask = yes_book["asks"][0]["price"]
    no_best_ask = no_book["asks"][0]["price"]

    # Available size at best ask
    yes_ask_size = yes_book["asks"][0]["size"]
    no_ask_size = no_book["asks"][0]["size"]

    # Total cost to buy 1 share of each side
    total_cost = yes_best_ask + no_best_ask

    # === DYNAMIC FEE CALCULATION (post Feb 18, 2026) ===
    # Fee = shares × feeRate × p × (1-p), applied to winning side only
    # For arb, one side always wins → calculate fee on both, take the max
    fee_free = is_fee_free_market(question)

    if fee_free:
        # Geopolitics = zero fees! Taker arb is fully viable
        yes_fee_per_share = 0.0
        no_fee_per_share = 0.0
        fee_rate = 0.0
    else:
        # Fetch actual fee rate from Polymarket API
        fee_rate = get_fee_rate(yes_token)
        yes_fee_per_share = fee_rate * yes_best_ask * (1.0 - yes_best_ask)
        no_fee_per_share = fee_rate * no_best_ask * (1.0 - no_best_ask)

    # Worst-case fee (whichever side wins, we pay that fee)
    max_fee = max(yes_fee_per_share, no_fee_per_share)

    # Payout is always $1.00, minus the fee on the winning side
    profit_per_share = 1.0 - total_cost - max_fee

    if profit_per_share <= 0:
        return None

    # How many shares can we buy? Limited by:
    # 1. Orderbook depth (minimum of yes/no available)
    # 2. Our bankroll
    max_shares_by_book = min(yes_ask_size, no_ask_size)

    # Minimum edge threshold
    profit_pct = profit_per_share / total_cost
    if profit_pct < config.MIN_ARB_PROFIT:
        return None

    return {
        "question": question,
        "market_id": market.get("id", ""),
        "yes_token": yes_token,
        "no_token": no_token,
        "yes_price": yes_best_ask,
        "no_price": no_best_ask,
        "total_cost": total_cost,
        "profit_per_share": profit_per_share,
        "profit_pct": profit_pct,
        "max_shares": max_shares_by_book,
        "yes_book_depth": yes_ask_size,
        "no_book_depth": no_ask_size,
        "fee_rate": fee_rate,
        "fee_per_share": max_fee,
        "fee_free": fee_free,
    }


def scan_all_markets(markets: list[dict]) -> list[dict]:
    """Scan all markets for arbitrage opportunities.

    Returns list of arb opportunities sorted by profit %.
    """
    opportunities = []

    for market in markets:
        arb = scan_arb_opportunity(market)
        if arb:
            opportunities.append(arb)

    # Sort by profit % descending
    opportunities.sort(key=lambda x: x["profit_pct"], reverse=True)
    return opportunities


def execute_arb(arb: dict, bankroll: float) -> dict | None:
    """Execute an arbitrage trade — buy both sides simultaneously.

    Returns dict with order details or None if failed.
    """
    # Calculate position size
    max_cost = bankroll * config.ARB_MAX_POSITION_PCT
    cost_per_pair = arb["total_cost"]

    # How many share pairs can we afford?
    max_by_bankroll = max_cost / cost_per_pair if cost_per_pair > 0 else 0
    max_by_book = arb["max_shares"]

    shares = min(max_by_bankroll, max_by_book)
    shares = max(5, int(shares))  # Polymarket minimum 5 shares

    total_cost = shares * cost_per_pair
    if total_cost > bankroll * 0.90:  # Don't use more than 90% of bankroll
        shares = max(5, int(bankroll * 0.90 / cost_per_pair))
        total_cost = shares * cost_per_pair

    expected_profit = shares * arb["profit_per_share"]

    print(f"\n  === ARB TRADE ===")
    print(f"  Market:  {arb['question'][:50]}")
    print(f"  YES:     {shares} shares @ ${arb['yes_price']:.3f}")
    print(f"  NO:      {shares} shares @ ${arb['no_price']:.3f}")
    print(f"  Cost:    ${total_cost:.2f}")
    print(f"  Profit:  ${expected_profit:.2f} ({arb['profit_pct']:.1%})")
    print(f"  =================")

    # Place YES order
    yes_order = trader.place_limit_order(
        token_id=arb["yes_token"],
        price=arb["yes_price"],
        size=shares,
        side="BUY",
    )

    if not yes_order:
        print(f"  FAILED: YES order failed")
        return None

    # Place NO order
    no_order = trader.place_limit_order(
        token_id=arb["no_token"],
        price=arb["no_price"],
        size=shares,
        side="BUY",
    )

    if not no_order:
        print(f"  WARNING: NO order failed — YES order is naked!")
        print(f"  Cancelling YES order {yes_order}...")
        trader.cancel_order(yes_order)
        return None

    print(f"  YES order: {yes_order}")
    print(f"  NO order:  {no_order}")

    return {
        "question": arb["question"],
        "market_id": arb["market_id"],
        "yes_token": arb["yes_token"],
        "no_token": arb["no_token"],
        "yes_price": arb["yes_price"],
        "no_price": arb["no_price"],
        "shares": shares,
        "total_cost": total_cost,
        "expected_profit": expected_profit,
        "profit_pct": arb["profit_pct"],
        "yes_order": yes_order,
        "no_order": no_order,
    }
