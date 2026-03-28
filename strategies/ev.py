"""Expected Value calculation and edge detection."""


def expected_value(true_prob: float, market_price: float) -> float:
    """EV per dollar risked.

    EV = (p_win * profit) - (p_lose * stake)
    For a market priced at `market_price`, buying costs market_price and pays 1.0 if win.
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0
    profit_if_win = 1.0 - market_price
    ev = (true_prob * profit_if_win) - ((1.0 - true_prob) * market_price)
    return ev


def edge(true_prob: float, market_price: float) -> float:
    """Raw edge = estimated probability - market probability."""
    return true_prob - market_price


def ev_per_dollar(true_prob: float, market_price: float) -> float:
    """EV normalized per dollar of cost."""
    if market_price <= 0:
        return 0.0
    return expected_value(true_prob, market_price) / market_price
