"""Arbitrage scanner — finds ANY markets where Yes + No < $1.00.

Strategy:
1. Scan ALL active markets (crypto, politics, events, anything)
2. Check REAL orderbook prices (actual fillable asks)
3. If best_ask(Yes) + best_ask(No) < threshold after fees → guaranteed profit
4. Buy both sides → wait for resolution → collect $1.00 per share pair
5. Profit = $1.00 - cost_yes - cost_no - fee (per share)

Thresholds:
- Normal: total_cost < 0.99
- Near-expiry (<60 min): total_cost < 0.995

Geopolitics/world events = ZERO fees → best arb targets.
"""

from datetime import datetime, timezone
from core import trader  # still needed for get_orderbook in scanner
from strategies.ev import get_fee_rate, calculate_taker_fee, is_fee_free_market
from utils.logger import log
import config

# Arb spread thresholds
ARB_THRESHOLD_NORMAL = 0.995
ARB_THRESHOLD_NEAR_EXPIRY = 0.998
NEAR_EXPIRY_MINUTES = 60


def _evaluate_arb_candidate(market: dict) -> dict | None:
    """Evaluate a market for arb potential. Returns candidate dict with all stats.

    Returns None only if orderbook is unavailable. Otherwise returns a dict
    with 'viable' flag indicating whether it passes all checks.
    """
    question = market.get("question", "")
    token_ids = market.get("token_ids", [])

    if len(token_ids) < 2:
        return None

    yes_token = token_ids[0]
    no_token = token_ids[1]

    yes_book = trader.get_orderbook(yes_token)
    no_book = trader.get_orderbook(no_token)

    if not yes_book or not no_book or not yes_book.get("asks") or not no_book.get("asks"):
        return None

    yes_best_ask = yes_book["asks"][0]["price"]
    no_best_ask = no_book["asks"][0]["price"]
    yes_ask_size = yes_book["asks"][0]["size"]
    no_ask_size = no_book["asks"][0]["size"]

    total_cost = yes_best_ask + no_best_ask

    # Time to resolution
    end_date = market.get("end_date") or market.get("endDate") or ""
    minutes_left = 999
    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            minutes_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 60
        except (ValueError, TypeError):
            pass

    near_expiry = minutes_left < NEAR_EXPIRY_MINUTES
    threshold = ARB_THRESHOLD_NEAR_EXPIRY if near_expiry else ARB_THRESHOLD_NORMAL

    # Fee calculation
    fee_free = is_fee_free_market(question)
    if fee_free:
        yes_fee = no_fee = fee_rate = 0.0
    else:
        fee_rate = get_fee_rate(yes_token)
        yes_fee = fee_rate * yes_best_ask * (1.0 - yes_best_ask)
        no_fee = fee_rate * no_best_ask * (1.0 - no_best_ask)

    # Arb buys BOTH legs, so we pay BOTH fees (max() was a 50% under-count).
    fee_pair = yes_fee + no_fee
    net_profit = 1.0 - total_cost - fee_pair

    # Determine if viable
    skip_reason = None
    if total_cost >= threshold:
        skip_reason = f"total >= {threshold}"
    elif net_profit <= 0:
        skip_reason = "negative after fees"
    elif (net_profit / total_cost) < config.MIN_ARB_PROFIT:
        skip_reason = f"profit {net_profit/total_cost:.2%} < min {config.MIN_ARB_PROFIT:.2%}"

    return {
        "question": question,
        "market_id": market.get("id", ""),
        "yes_token": yes_token,
        "no_token": no_token,
        "yes_price": yes_best_ask,
        "no_price": no_best_ask,
        "total_cost": total_cost,
        "profit_per_share": net_profit,
        "profit_pct": net_profit / total_cost if total_cost > 0 else 0,
        "max_shares": min(yes_ask_size, no_ask_size),
        "yes_book_depth": yes_ask_size,
        "no_book_depth": no_ask_size,
        "fee_rate": fee_rate,
        "fee_per_share": fee_pair,
        "fee_free": fee_free,
        "threshold": threshold,
        "near_expiry": near_expiry,
        "minutes_left": minutes_left,
        "viable": skip_reason is None,
        "skip_reason": skip_reason,
    }


def scan_all_markets(markets: list[dict]) -> list[dict]:
    """Scan all markets for arbitrage opportunities.

    Logs ALL candidates (viable or not), returns only viable ones sorted by profit %.
    """
    opportunities = []
    candidates_found = 0
    best_spread = 999.0
    scanned = 0

    for market in markets:
        cand = _evaluate_arb_candidate(market)
        if not cand:
            continue

        scanned += 1
        if cand["total_cost"] < best_spread:
            best_spread = cand["total_cost"]

        # Only log markets where spread is remotely close (< loosest threshold)
        if cand["total_cost"] >= ARB_THRESHOLD_NEAR_EXPIRY:
            continue

        candidates_found += 1
        q_short = cand["question"][:40]
        if cand["viable"]:
            thresh_label = f"threshold={cand['threshold']}"
            if cand["near_expiry"]:
                thresh_label += " (near expiry)"
            print(f"  [ARB CANDIDATE] {q_short} | yes={cand['yes_price']:.2f} no={cand['no_price']:.2f} "
                  f"total={cand['total_cost']:.3f} net_after_fees=${cand['profit_per_share']:+.3f} "
                  f"→ EXECUTE | {thresh_label}")
            opportunities.append(cand)
        else:
            print(f"  [ARB CANDIDATE] {q_short} | yes={cand['yes_price']:.2f} no={cand['no_price']:.2f} "
                  f"total={cand['total_cost']:.3f} net_after_fees=${cand['profit_per_share']:+.3f} "
                  f"→ SKIP ({cand['skip_reason']})")

    if candidates_found == 0:
        print(f"  [ARB] {scanned} markets scanned | best spread = {best_spread:.3f} | none under threshold {ARB_THRESHOLD_NEAR_EXPIRY}")

    opportunities.sort(key=lambda x: x["profit_pct"], reverse=True)
    return opportunities


def size_arb(arb: dict, bankroll: float) -> dict | None:
    """Compute arb sizing without placing orders.

    Returns a sized plan dict, or None if sizing fails.
    Caller is responsible for routing through risk_manager → execution.
    """
    max_cost = bankroll * config.ARB_MAX_POSITION_PCT
    cost_per_pair = arb["total_cost"]

    max_by_bankroll = max_cost / cost_per_pair if cost_per_pair > 0 else 0
    max_by_book = arb["max_shares"]

    shares = min(max_by_bankroll, max_by_book)
    shares = max(5, int(shares))

    total_cost = shares * cost_per_pair
    if total_cost > bankroll * 0.90:
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
    }
