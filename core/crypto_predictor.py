"""Crypto price predictor for 5-minute Up/Down markets on Polymarket.

Uses real-time Binance data with full technical analysis:
- Multiple timeframes (1m, 5m, 15m)
- RSI (Relative Strength Index)
- MACD (Moving Average Convergence Divergence)
- Bollinger Bands
- Volume analysis (buying vs selling pressure)
- EMA crossovers
- Support/Resistance levels
"""

import httpx
from utils.logger import log

BINANCE_SYMBOLS = {
    "bitcoin": "BTCUSDT",
    "btc": "BTCUSDT",
    "ethereum": "ETHUSDT",
    "eth": "ETHUSDT",
    "solana": "SOLUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
    "dogecoin": "DOGEUSDT",
    "doge": "DOGEUSDT",
    "cardano": "ADAUSDT",
    "ada": "ADAUSDT",
}


def _fetch_candles(symbol: str, interval: str, limit: int) -> list[dict] | None:
    """Fetch OHLCV candles from Binance."""
    try:
        resp = httpx.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json()
        return [
            {
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
                "buy_volume": float(c[9]),  # Taker buy base volume
            }
            for c in raw
        ]
    except Exception as e:
        log.error("Failed to fetch %s %s candles: %s", symbol, interval, e)
        return None


def _calc_rsi(closes: list[float], period: int = 14) -> float:
    """Calculate RSI (Relative Strength Index)."""
    if len(closes) < period + 1:
        return 50.0

    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))

    # Use last `period` values
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _calc_ema(values: list[float], period: int) -> list[float]:
    """Calculate Exponential Moving Average."""
    if not values:
        return []
    multiplier = 2 / (period + 1)
    ema = [values[0]]
    for i in range(1, len(values)):
        ema.append((values[i] - ema[-1]) * multiplier + ema[-1])
    return ema


def _calc_macd(closes: list[float]) -> dict:
    """Calculate MACD (12, 26, 9)."""
    if len(closes) < 26:
        return {"macd": 0, "signal": 0, "histogram": 0}

    ema12 = _calc_ema(closes, 12)
    ema26 = _calc_ema(closes, 26)

    macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
    signal_line = _calc_ema(macd_line, 9)

    return {
        "macd": macd_line[-1],
        "signal": signal_line[-1] if signal_line else 0,
        "histogram": macd_line[-1] - (signal_line[-1] if signal_line else 0),
    }


def _calc_bollinger(closes: list[float], period: int = 20) -> dict:
    """Calculate Bollinger Bands."""
    if len(closes) < period:
        return {"upper": 0, "middle": 0, "lower": 0, "position": 0.5}

    recent = closes[-period:]
    middle = sum(recent) / period
    std = (sum((x - middle) ** 2 for x in recent) / period) ** 0.5
    upper = middle + 2 * std
    lower = middle - 2 * std

    current = closes[-1]
    if upper == lower:
        position = 0.5
    else:
        position = (current - lower) / (upper - lower)

    return {"upper": upper, "middle": middle, "lower": lower, "position": position}


def _calc_volume_pressure(candles: list[dict]) -> dict:
    """Analyze buying vs selling volume pressure."""
    if not candles:
        return {"buy_ratio": 0.5, "volume_trend": 0}

    recent = candles[-10:]
    total_vol = sum(c["volume"] for c in recent)
    total_buy = sum(c["buy_volume"] for c in recent)

    buy_ratio = total_buy / total_vol if total_vol > 0 else 0.5

    # Volume trend: is volume increasing or decreasing?
    if len(candles) >= 20:
        vol_recent = sum(c["volume"] for c in candles[-5:])
        vol_prior = sum(c["volume"] for c in candles[-10:-5])
        volume_trend = (vol_recent - vol_prior) / vol_prior if vol_prior > 0 else 0
    else:
        volume_trend = 0

    return {"buy_ratio": buy_ratio, "volume_trend": volume_trend}


def _find_support_resistance(candles: list[dict]) -> dict:
    """Find nearby support and resistance from recent highs/lows."""
    if len(candles) < 10:
        return {"support": 0, "resistance": 0, "near_support": False, "near_resistance": False}

    recent = candles[-30:] if len(candles) >= 30 else candles
    highs = [c["high"] for c in recent]
    lows = [c["low"] for c in recent]
    current = candles[-1]["close"]

    resistance = max(highs)
    support = min(lows)
    price_range = resistance - support if resistance > support else 1

    near_support = (current - support) / price_range < 0.15
    near_resistance = (resistance - current) / price_range < 0.15

    return {
        "support": support,
        "resistance": resistance,
        "near_support": near_support,
        "near_resistance": near_resistance,
    }


def get_full_analysis(symbol: str) -> dict | None:
    """Run full technical analysis on a crypto symbol.

    Fetches multiple timeframes and calculates all indicators.
    """
    # Fetch 1-minute candles (for short-term signals)
    candles_1m = _fetch_candles(symbol, "1m", 60)
    # Fetch 5-minute candles (for trend confirmation)
    candles_5m = _fetch_candles(symbol, "5m", 50)

    if not candles_1m or not candles_5m:
        return None

    closes_1m = [c["close"] for c in candles_1m]
    closes_5m = [c["close"] for c in candles_5m]

    current_price = closes_1m[-1]

    # === Momentum ===
    mom_3m = (closes_1m[-1] - closes_1m[-4]) / closes_1m[-4] * 100
    mom_10m = (closes_1m[-1] - closes_1m[-11]) / closes_1m[-11] * 100
    mom_30m = (closes_1m[-1] - closes_1m[-31]) / closes_1m[-31] * 100 if len(closes_1m) > 30 else 0

    # === RSI ===
    rsi_1m = _calc_rsi(closes_1m, 14)
    rsi_5m = _calc_rsi(closes_5m, 14)

    # === MACD ===
    macd_1m = _calc_macd(closes_1m)
    macd_5m = _calc_macd(closes_5m)

    # === Bollinger Bands ===
    bb_1m = _calc_bollinger(closes_1m, 20)

    # === Volume ===
    vol = _calc_volume_pressure(candles_1m)

    # === Support/Resistance ===
    sr = _find_support_resistance(candles_5m)

    # === EMA crossover ===
    ema_8 = _calc_ema(closes_1m, 8)
    ema_21 = _calc_ema(closes_1m, 21)
    ema_cross = "bullish" if ema_8[-1] > ema_21[-1] else "bearish"
    ema_cross_strength = abs(ema_8[-1] - ema_21[-1]) / current_price * 100

    # === Candle pattern: last 3 candles ===
    last3 = candles_1m[-3:]
    bullish_candles = sum(1 for c in last3 if c["close"] > c["open"])
    bearish_candles = 3 - bullish_candles

    return {
        "price": current_price,
        "momentum_3m": mom_3m,
        "momentum_10m": mom_10m,
        "momentum_30m": mom_30m,
        "rsi_1m": rsi_1m,
        "rsi_5m": rsi_5m,
        "macd_1m": macd_1m,
        "macd_5m": macd_5m,
        "bollinger": bb_1m,
        "volume": vol,
        "support_resistance": sr,
        "ema_cross": ema_cross,
        "ema_cross_strength": ema_cross_strength,
        "bullish_candles": bullish_candles,
        "bearish_candles": bearish_candles,
    }


def is_crypto_updown_market(question: str) -> bool:
    """Check if this is a 5-minute crypto Up/Down market."""
    q = question.lower()
    return "up or down" in q and any(
        coin in q for coin in ["bitcoin", "ethereum", "solana", "btc", "eth", "sol",
                               "xrp", "dogecoin", "doge", "cardano", "ada"]
    )


def estimate_crypto_probability(question: str, market_price: float, outcome: str) -> dict | None:
    """Estimate probability using full technical analysis.

    Combines multiple signals into a weighted score:
    - Momentum (3m, 10m, 30m)
    - RSI (overbought/oversold)
    - MACD (trend direction + crossovers)
    - Bollinger Bands (mean reversion)
    - Volume (buying vs selling pressure)
    - Support/Resistance proximity
    - EMA crossover
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

    data = get_full_analysis(symbol)
    if not data:
        return None

    is_up_outcome = outcome.lower() in ["yes", "up"]

    # === SCORING SYSTEM: each signal votes up or down ===
    signals = []
    signal_details = []

    # 1. Short-term momentum (weight: 25%)
    mom = data["momentum_3m"]
    if mom > 0.1:
        signals.append(("momentum_3m", 0.70, 0.25))
        signal_details.append(f"3m mom strong UP {mom:+.3f}%")
    elif mom > 0.03:
        signals.append(("momentum_3m", 0.60, 0.25))
        signal_details.append(f"3m mom mild UP {mom:+.3f}%")
    elif mom > -0.03:
        signals.append(("momentum_3m", 0.50, 0.25))
        signal_details.append(f"3m mom FLAT {mom:+.3f}%")
    elif mom > -0.1:
        signals.append(("momentum_3m", 0.40, 0.25))
        signal_details.append(f"3m mom mild DOWN {mom:+.3f}%")
    else:
        signals.append(("momentum_3m", 0.30, 0.25))
        signal_details.append(f"3m mom strong DOWN {mom:+.3f}%")

    # 2. Medium-term momentum confirms trend (weight: 15%)
    mom10 = data["momentum_10m"]
    if mom10 > 0.05:
        signals.append(("momentum_10m", 0.65, 0.15))
    elif mom10 < -0.05:
        signals.append(("momentum_10m", 0.35, 0.15))
    else:
        signals.append(("momentum_10m", 0.50, 0.15))

    # 3. RSI — overbought/oversold (weight: 15%)
    rsi = data["rsi_1m"]
    if rsi > 70:
        # Overbought — likely to reverse DOWN
        signals.append(("rsi", 0.35, 0.15))
        signal_details.append(f"RSI overbought {rsi:.0f}")
    elif rsi < 30:
        # Oversold — likely to bounce UP
        signals.append(("rsi", 0.65, 0.15))
        signal_details.append(f"RSI oversold {rsi:.0f}")
    elif rsi > 60:
        signals.append(("rsi", 0.45, 0.15))
        signal_details.append(f"RSI high {rsi:.0f}")
    elif rsi < 40:
        signals.append(("rsi", 0.55, 0.15))
        signal_details.append(f"RSI low {rsi:.0f}")
    else:
        signals.append(("rsi", 0.50, 0.15))
        signal_details.append(f"RSI neutral {rsi:.0f}")

    # 4. MACD (weight: 15%)
    macd = data["macd_1m"]
    if macd["histogram"] > 0 and macd["macd"] > macd["signal"]:
        signals.append(("macd", 0.62, 0.15))
        signal_details.append("MACD bullish")
    elif macd["histogram"] < 0 and macd["macd"] < macd["signal"]:
        signals.append(("macd", 0.38, 0.15))
        signal_details.append("MACD bearish")
    else:
        signals.append(("macd", 0.50, 0.15))
        signal_details.append("MACD neutral")

    # 5. Volume pressure (weight: 10%)
    buy_ratio = data["volume"]["buy_ratio"]
    if buy_ratio > 0.55:
        signals.append(("volume", 0.60, 0.10))
        signal_details.append(f"Volume buying {buy_ratio:.0%}")
    elif buy_ratio < 0.45:
        signals.append(("volume", 0.40, 0.10))
        signal_details.append(f"Volume selling {buy_ratio:.0%}")
    else:
        signals.append(("volume", 0.50, 0.10))

    # 6. Bollinger Band position (weight: 10%)
    bb_pos = data["bollinger"]["position"]
    if bb_pos > 0.95:
        # At upper band — mean reversion DOWN likely
        signals.append(("bollinger", 0.35, 0.10))
        signal_details.append("BB upper — reversal risk")
    elif bb_pos < 0.05:
        # At lower band — mean reversion UP likely
        signals.append(("bollinger", 0.65, 0.10))
        signal_details.append("BB lower — bounce likely")
    else:
        signals.append(("bollinger", 0.50, 0.10))

    # 7. EMA crossover (weight: 10%)
    if data["ema_cross"] == "bullish" and data["ema_cross_strength"] > 0.01:
        signals.append(("ema", 0.60, 0.10))
        signal_details.append("EMA bullish cross")
    elif data["ema_cross"] == "bearish" and data["ema_cross_strength"] > 0.01:
        signals.append(("ema", 0.40, 0.10))
        signal_details.append("EMA bearish cross")
    else:
        signals.append(("ema", 0.50, 0.10))

    # === Combine signals with weighted average ===
    total_weight = sum(s[2] for s in signals)
    up_prob = sum(s[1] * s[2] for s in signals) / total_weight

    # === Support/Resistance adjustment ===
    sr = data["support_resistance"]
    if sr["near_support"]:
        up_prob += 0.03  # Near support — likely to bounce
        signal_details.append("Near support — bounce")
    elif sr["near_resistance"]:
        up_prob -= 0.03  # Near resistance — likely to reject
        signal_details.append("Near resistance — reject")

    # === 5m MACD confirms or contradicts ===
    macd_5m = data["macd_5m"]
    if macd_5m["histogram"] > 0 and up_prob > 0.50:
        up_prob += 0.02  # 5m trend confirms
        signal_details.append("5m MACD confirms UP")
    elif macd_5m["histogram"] < 0 and up_prob < 0.50:
        up_prob += -0.02  # 5m trend confirms down (make more bearish)
        signal_details.append("5m MACD confirms DOWN")

    # Clamp
    up_prob = max(0.25, min(0.75, up_prob))

    # === Confidence based on signal agreement ===
    bullish_signals = sum(1 for s in signals if s[1] > 0.55)
    bearish_signals = sum(1 for s in signals if s[1] < 0.45)
    neutral_signals = len(signals) - bullish_signals - bearish_signals

    agreement = max(bullish_signals, bearish_signals)
    if agreement >= 5:
        confidence = "high"
    elif agreement >= 3:
        confidence = "medium"
    else:
        confidence = "low"

    # === Require clear signals — no coin flips ===
    if confidence == "low":
        log.info("Crypto SKIP '%s' — signals mixed, no clear direction", question[:40])
        return None

    if abs(up_prob - 0.50) < 0.03:
        log.info("Crypto SKIP '%s' — too close to 50/50 (%.2f)", question[:40], up_prob)
        return None

    if is_up_outcome:
        prob = up_prob
    else:
        prob = 1.0 - up_prob

    details_str = " | ".join(signal_details[:6])
    reasoning = (
        f"{symbol}: ${data['price']:.2f} | RSI:{data['rsi_1m']:.0f} | "
        f"MACD:{'+'if macd['histogram']>0 else '-'} | "
        f"Vol:{buy_ratio:.0%}buy | BB:{bb_pos:.2f} | "
        f"{details_str}"
    )

    log.info("Crypto TA for '%s' (%s): prob=%.2f conf=%s | %s",
             question[:50], outcome, prob, confidence, reasoning)

    return {
        "probability": prob,
        "confidence": confidence,
        "reasoning": reasoning,
    }
