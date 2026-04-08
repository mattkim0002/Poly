"""Market structure filter — lightweight trend/pressure/danger checks.

Applied to directional crypto trades (binance_lag, momentum_claude) only.
NOT applied to arbitrage.
"""

from core.crypto_predictor import _fetch_candles, get_realtime_momentum, get_orderbook_imbalance


def _candle_trend(candles: list[dict]) -> str:
    """Label a candle series as up/down/flat."""
    if not candles or len(candles) < 3:
        return "flat"
    first = candles[0]["open"]
    last = candles[-1]["close"]
    if first <= 0:
        return "flat"
    change = (last - first) / first
    if change > 0.001:
        return "up"
    elif change < -0.001:
        return "down"
    return "flat"


def get_trend(symbol: str) -> dict:
    """Check trend on 1m, 5m, 15m timeframes. Returns label + details."""
    c1 = _fetch_candles(symbol, "1m", 5)
    c5 = _fetch_candles(symbol, "5m", 3)
    c15 = _fetch_candles(symbol, "15m", 3)

    t1 = _candle_trend(c1)
    t5 = _candle_trend(c5)
    t15 = _candle_trend(c15)

    ups = sum(1 for t in [t1, t5, t15] if t == "up")
    downs = sum(1 for t in [t1, t5, t15] if t == "down")

    if ups >= 2:
        label = "trending_up"
    elif downs >= 2:
        label = "trending_down"
    else:
        label = "choppy"

    return {"label": label, "1m": t1, "5m": t5, "15m": t15}


def get_pressure(symbol: str) -> dict:
    """Pressure score from -1.0 to +1.0 using price change, volume, orderbook."""
    score = 0.0

    # Price change component
    mom = get_realtime_momentum(symbol)
    if mom:
        change = mom["price_change_60s"]
        score += max(-0.4, min(0.4, change * 5))  # ±0.08% → ±0.4
        vol_r = mom["volume_ratio"]
        if vol_r > 1.5:
            score += 0.1 if mom["price_change_60s"] > 0 else -0.1
        bp = mom["buy_pressure"]
        score += (bp - 0.5) * 0.4  # 0.6 buy → +0.04, 0.4 → -0.04

    # Orderbook component
    ob = get_orderbook_imbalance(symbol)
    if ob:
        bid_bias = (ob["bid_ratio"] - 0.5) * 0.6  # 0.65 → +0.09
        score += bid_bias

    score = max(-1.0, min(1.0, score))

    if score > 0.3:
        label = "bullish"
    elif score < -0.3:
        label = "bearish"
    else:
        label = "neutral"

    return {"score": round(score, 2), "label": label}


def get_danger(symbol: str) -> bool:
    """Check for sudden sharp moves, volume spikes, or extreme imbalance."""
    mom = get_realtime_momentum(symbol)
    if mom:
        if abs(mom["price_change_60s"]) > 0.15:
            return True
        if mom["volume_ratio"] > 4.0:
            return True

    ob = get_orderbook_imbalance(symbol)
    if ob:
        if ob["bid_ratio"] > 0.80 or ob["bid_ratio"] < 0.20:
            return True
        if ob.get("thin_asks") or ob.get("thin_bids"):
            return True

    return False


def check_trade(symbol: str, side: str) -> dict:
    """Run all 3 checks. Returns dict with results and whether trade is allowed.

    side: "UP" or "DOWN"
    """
    trend = get_trend(symbol)
    pressure = get_pressure(symbol)
    danger = get_danger(symbol)

    allowed = True
    block_reason = None

    # Trend blocks
    if trend["label"] == "trending_up" and side == "DOWN":
        allowed = False
        block_reason = "trend filter (trending_up blocks DOWN)"
    elif trend["label"] == "trending_down" and side == "UP":
        allowed = False
        block_reason = "trend filter (trending_down blocks UP)"

    # Pressure blocks (can be overridden by Claude)
    needs_claude = False
    if pressure["label"] == "bullish" and side == "DOWN":
        needs_claude = True
    elif pressure["label"] == "bearish" and side == "UP":
        needs_claude = True

    # Danger blocks (can be overridden only if all signals align + Claude)
    if danger:
        signals_align = (
            (side == "UP" and trend["label"] == "trending_up" and pressure["label"] == "bullish") or
            (side == "DOWN" and trend["label"] == "trending_down" and pressure["label"] == "bearish")
        )
        if not signals_align:
            allowed = False
            block_reason = "danger flag (sharp move/spike, signals don't align)"
        else:
            needs_claude = True

    return {
        "trend": trend,
        "pressure": pressure,
        "danger": danger,
        "allowed": allowed,
        "needs_claude": needs_claude,
        "block_reason": block_reason,
    }
