"""Expected Value calculation, edge detection, and Polymarket fee model.

Fee formula (post Feb 18, 2026):
  fee = C × feeRate × p × (1 - p)
  where C = number of shares, feeRate varies by market type.

Fee rates by market category:
  - Crypto 5-min/15-min: feeRate = 0.072 (dynamic, ~1.8% at 50¢)
  - Politics/finance: feeRate = 0.04 (~1% at 50¢)
  - Geopolitics/world events: feeRate = 0.0 (ZERO FEES)
"""

import httpx
from utils.logger import log

# Cache fee rates to avoid hammering the API
_fee_rate_cache: dict[str, float] = {}


def get_fee_rate(token_id: str) -> float:
    """Fetch the fee rate for a specific token from Polymarket.

    Falls back to worst-case 0.072 if API call fails.
    """
    if token_id in _fee_rate_cache:
        return _fee_rate_cache[token_id]

    try:
        resp = httpx.get(
            f"https://clob.polymarket.com/fee-rate",
            params={"token_id": token_id},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        rate = float(data.get("fee_rate", data.get("feeRate", 0.072)))
        _fee_rate_cache[token_id] = rate
        return rate
    except Exception as e:
        log.debug("Failed to fetch fee rate for %s: %s — using 0.072", token_id[:16], e)
        return 0.072  # Worst case: crypto fee rate


def calculate_taker_fee(shares: float, price: float, fee_rate: float = 0.072) -> float:
    """Calculate the actual taker fee for a trade.

    fee = shares × feeRate × price × (1 - price)

    Examples at feeRate=0.072:
      100 shares @ $0.50 → $1.80 fee (1.8%)
      100 shares @ $0.10 → $0.65 fee (0.65%)
      100 shares @ $0.90 → $0.65 fee (0.65%)
    """
    return shares * fee_rate * price * (1.0 - price)


def is_fee_free_market(question: str) -> bool:
    """Check if a market is likely fee-free (geopolitics/world events)."""
    q = question.lower()
    geo_terms = {"iran", "russia", "ukraine", "china", "taiwan", "nato",
                 "sanctions", "ceasefire", "missile", "nuclear", "war ",
                 "invasion", "israel", "gaza", "north korea", "military",
                 "troops", "strike", "bombing", "peace", "treaty",
                 "humanitarian", "refugee", "un ", "united nations"}
    return any(t in q for t in geo_terms)


def expected_value(true_prob: float, market_price: float, fee_rate: float = 0.0) -> float:
    """EV per dollar risked, accounting for taker fees.

    EV = (p_win * profit_after_fee) - (p_lose * cost)
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0
    # Fee is paid on winnings (the payout side)
    fee_per_share = fee_rate * market_price * (1.0 - market_price)
    profit_if_win = 1.0 - market_price - fee_per_share
    ev = (true_prob * profit_if_win) - ((1.0 - true_prob) * market_price)
    return ev


def edge(true_prob: float, market_price: float) -> float:
    """Raw edge = estimated probability - market probability."""
    return true_prob - market_price


def ev_per_dollar(true_prob: float, market_price: float, fee_rate: float = 0.0) -> float:
    """EV normalized per dollar of cost."""
    if market_price <= 0:
        return 0.0
    return expected_value(true_prob, market_price, fee_rate) / market_price

