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

Consumes core.candidate.Candidate directly. Orderbook asks/bids populated
onto the Candidate via the per-token orderbook fetch.
"""

from core import trader
from core.candidate import Candidate
from strategies.ev import get_fee_rate, is_fee_free_market
import config

# Arb spread thresholds
ARB_THRESHOLD_NORMAL = 0.995
ARB_THRESHOLD_NEAR_EXPIRY = 0.998
NEAR_EXPIRY_MINUTES = 60


def _evaluate_arb_candidate(cand: Candidate) -> dict | None:
    """Evaluate a Candidate for arb potential. Returns opportunity dict with all stats.

    Returns None only if orderbook is unavailable. Otherwise returns a dict
    with 'viable' flag indicating whether it passes all checks.
    Populates cand.yes_ask/bid and cand.no_ask/bid from the orderbook fetch.
    """
    if not cand.yes_token_id or not cand.no_token_id:
        return None

    yes_book = trader.get_orderbook(cand.yes_token_id)
    no_book = trader.get_orderbook(cand.no_token_id)

    if not yes_book or not no_book or not yes_book.get("asks") or not no_book.get("asks"):
        return None

    yes_best_ask = float(yes_book["asks"][0]["price"])
    no_best_ask = float(no_book["asks"][0]["price"])
    yes_ask_size = float(yes_book["asks"][0]["size"])
    no_ask_size = float(no_book["asks"][0]["size"])

    # Enrich Candidate with live orderbook data (best bid/ask on each side).
    cand.yes_ask = yes_best_ask
    cand.no_ask = no_best_ask
    if yes_book.get("bids"):
        cand.yes_bid = float(yes_book["bids"][0]["price"])
    if no_book.get("bids"):
        cand.no_bid = float(no_book["bids"][0]["price"])

    total_cost = yes_best_ask + no_best_ask

    minutes_left = cand.minutes_to_expiry if cand.minutes_to_expiry is not None else 999.0
    near_expiry = minutes_left < NEAR_EXPIRY_MINUTES
    threshold = ARB_THRESHOLD_NEAR_EXPIRY if near_expiry else ARB_THRESHOLD_NORMAL

    # Fee calculation
    fee_free = is_fee_free_market(cand.question)
    if fee_free:
        yes_fee = no_fee = fee_rate = 0.0
    else:
        fee_rate = get_fee_rate(cand.yes_token_id)
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
        "question": cand.question,
        "market_id": cand.market_id,
        "yes_token": cand.yes_token_id,
        "no_token": cand.no_token_id,
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
        "end_date": cand.end_date,
        "viable": skip_reason is None,
        "skip_reason": skip_reason,
    }


def scan_all_markets(markets: list[Candidate]) -> list[dict]:
    """Scan all Candidates for arbitrage opportunities.

    Logs ALL candidates (viable or not), returns only viable ones sorted by profit %.
    """
    opportunities = []
    candidates_found = 0
    best_spread = 999.0
    scanned = 0

    for cand in markets:
        opp = _evaluate_arb_candidate(cand)
        if not opp:
            continue

        scanned += 1
        if opp["total_cost"] < best_spread:
            best_spread = opp["total_cost"]

        # Only log markets where spread is remotely close (< loosest threshold)
        if opp["total_cost"] >= ARB_THRESHOLD_NEAR_EXPIRY:
            continue

        candidates_found += 1
        q_short = opp["question"][:40]
        if opp["viable"]:
            thresh_label = f"threshold={opp['threshold']}"
            if opp["near_expiry"]:
                thresh_label += " (near expiry)"
            print(f"  [ARB CANDIDATE] {q_short} | yes={opp['yes_price']:.2f} no={opp['no_price']:.2f} "
                  f"total={opp['total_cost']:.3f} net_after_fees=${opp['profit_per_share']:+.3f} "
                  f"→ EXECUTE | {thresh_label}")
            opportunities.append(opp)
        else:
            print(f"  [ARB CANDIDATE] {q_short} | yes={opp['yes_price']:.2f} no={opp['no_price']:.2f} "
                  f"total={opp['total_cost']:.3f} net_after_fees=${opp['profit_per_share']:+.3f} "
                  f"→ SKIP ({opp['skip_reason']})")

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
