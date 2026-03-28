"""Kelly Criterion — optimal bet sizing for long-run growth."""


def kelly_fraction(market_price: float, true_prob: float) -> float:
    """Calculate raw Kelly fraction.

    f* = (bp - q) / b
    where b = net odds, p = win prob, q = lose prob
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0
    if true_prob <= 0 or true_prob >= 1:
        return 0.0

    b = (1.0 - market_price) / market_price  # net odds (profit per $1 risked)
    p = true_prob
    q = 1.0 - p

    f = (b * p - q) / b
    return max(f, 0.0)  # never bet negative
