"""Crypto price predictor for 5-minute Up/Down markets on Polymarket.

Uses real-time price data and momentum analysis to predict whether
BTC/ETH/SOL will be above or below a target price in 5 minutes.
"""

import re
import httpx
from utils.logger import log

BINANCE_SYMBOLS = {
    "bitcoin": "BTCUSDT",
    "btc": "BTCUSDT",
    "ethereum": "ETHUSDT",
    "eth": "ETHUSDT",
    "solana": "SOLUSDT",
    "sol": "SOLUSDT",
}


def get_price_and_momentum(symbol: str) -> dict | None:
    """Get current price, recent momentum, and RSI-like signal."""
    try:
        # Get last 15 one-minute candles
        resp = httpx.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "limit": 15},
            timeout=10,
        )
        resp.raise_for_status()
        candles = resp.json()

        closes = [float(c[4]) for c in candles]
        current_price = closes[-1]

        # Short-term momentum (last 3 minutes)
        momentum_3m = (closes[-1] - closes[-4]) / closes[-4] * 100

        # Medium-term momentum (last 10 minutes)
        momentum_10m = (closes[-1] - closes[-11]) / closes[-11] * 100

        # Simple RSI-like: count up vs down candles in last 10
        ups = sum(1 for i in range(-10, 0) if closes[i] > closes[i - 1])
        downs = 10 - ups

        # Trend strength: how consistent is the direction?
        trend_strength = abs(ups - downs) / 10.0

        return {
            "price": current_price,
            "momentum_3m": momentum_3m,
            "momentum_10m": momentum_10m,
            "ups": ups,
            "downs": downs,
            "trend_strength": trend_strength,
            "direction": "up" if momentum_3m > 0 else "down",
        }
    except Exception as e:
        log.error("Failed to get price data for %s: %s", symbol, e)
        return None


def is_crypto_updown_market(question: str) -> bool:
    """Check if this is a 5-minute crypto Up/Down market."""
    q = question.lower()
    return "up or down" in q and any(
        coin in q for coin in ["bitcoin", "ethereum", "solana", "btc", "eth", "sol"]
    )


def parse_target_price(question: str) -> float | None:
    """Try to extract target price from market question/metadata."""
    # These markets usually have the target in the question or description
    # e.g., "Bitcoin Up or Down - 5 Minutes" with target shown separately
    return None  # Target comes from market data, not question text


def estimate_crypto_probability(question: str, market_price: float, outcome: str) -> dict | None:
    """Estimate probability for a crypto Up/Down market.

    Args:
        question: Market question
        market_price: Current market price for this outcome (e.g., 0.38 for Up)
        outcome: "Up" or "Down" or similar

    Returns:
        dict with probability, confidence, reasoning
    """
    q = question.lower()

    # Determine which crypto
    symbol = None
    for coin, sym in BINANCE_SYMBOLS.items():
        if coin in q:
            symbol = sym
            break

    if not symbol:
        return None

    data = get_price_and_momentum(symbol)
    if not data:
        return None

    is_up_outcome = outcome.lower() in ["yes", "up"]

    # Base probability from momentum
    mom_3m = data["momentum_3m"]
    mom_10m = data["momentum_10m"]

    # Momentum-based probability
    # Strong upward momentum → higher probability of "Up"
    # The idea: recent momentum tends to persist for short periods
    if mom_3m > 0.1:
        up_prob = 0.62  # Strong up momentum
    elif mom_3m > 0.03:
        up_prob = 0.57  # Mild up momentum
    elif mom_3m > -0.03:
        up_prob = 0.50  # Sideways
    elif mom_3m > -0.1:
        up_prob = 0.43  # Mild down momentum
    else:
        up_prob = 0.38  # Strong down momentum

    # Adjust with medium-term trend
    if (mom_10m > 0 and mom_3m > 0):
        up_prob += 0.03  # Consistent uptrend
    elif (mom_10m < 0 and mom_3m < 0):
        up_prob -= 0.03  # Consistent downtrend

    # Trend strength adjustment
    if data["trend_strength"] > 0.6:
        # Strong trend — push probability further
        if data["direction"] == "up":
            up_prob += 0.03
        else:
            up_prob -= 0.03

    # Clamp
    up_prob = max(0.30, min(0.70, up_prob))

    if is_up_outcome:
        prob = up_prob
    else:
        prob = 1.0 - up_prob

    # Only bet if momentum is clear — skip sideways/weak signals
    if abs(mom_3m) < 0.03 and data["trend_strength"] < 0.4:
        log.info("Crypto SKIP '%s' — sideways, no clear momentum", question[:40])
        return None

    confidence = "high" if data["trend_strength"] > 0.5 else "medium" if data["trend_strength"] > 0.3 else "low"

    # Reject low confidence crypto — no point guessing on a coin flip
    if confidence == "low":
        log.info("Crypto SKIP '%s' — low confidence, weak trend", question[:40])
        return None

    reasoning = (
        f"{symbol}: ${data['price']:.2f} | "
        f"3m momentum: {mom_3m:+.3f}% | "
        f"10m momentum: {mom_10m:+.3f}% | "
        f"Up candles: {data['ups']}/10 | "
        f"Direction: {data['direction']}"
    )

    log.info("Crypto estimate for '%s' (%s): prob=%.2f conf=%s | %s",
             question[:50], outcome, prob, confidence, reasoning)

    return {
        "probability": prob,
        "confidence": confidence,
        "reasoning": reasoning,
    }
