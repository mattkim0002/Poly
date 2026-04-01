"""Crypto price predictor for 5-minute Up/Down markets on Polymarket.

Uses TWO data sources for maximum accuracy:
1. TradingView — professional-grade buy/sell signals across timeframes
2. Binance — real-time candles, volume, order flow

Technical analysis includes:
- TradingView recommendations (oscillators + moving averages)
- RSI, MACD, Bollinger Bands, Stochastic
- Volume analysis (buying vs selling pressure)
- EMA crossovers, support/resistance
- Multi-timeframe confirmation (1m, 5m, 15m)
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

# TradingView symbol mapping (exchange: BINANCE for crypto)
TV_SYMBOLS = {
    "BTCUSDT": "BTCUSDT",
    "ETHUSDT": "ETHUSDT",
    "SOLUSDT": "SOLUSDT",
    "XRPUSDT": "XRPUSDT",
    "DOGEUSDT": "DOGEUSDT",
    "ADAUSDT": "ADAUSDT",
}


# === TradingView Integration ===

def get_tradingview_analysis(symbol: str) -> dict | None:
    """Get TradingView buy/sell signals for multiple timeframes.

    Returns dict with recommendation, oscillators, and moving averages
    for 1m, 5m, and 15m timeframes.
    """
    try:
        from tradingview_ta import TA_Handler, Interval

        tv_symbol = TV_SYMBOLS.get(symbol, symbol)
        results = {}

        for label, interval in [("1m", Interval.INTERVAL_1_MINUTE),
                                 ("5m", Interval.INTERVAL_5_MINUTES),
                                 ("15m", Interval.INTERVAL_15_MINUTES)]:
            try:
                handler = TA_Handler(
                    symbol=tv_symbol,
                    screener="crypto",
                    exchange="BINANCE",
                    interval=interval,
                )
                analysis = handler.get_analysis()

                results[label] = {
                    "recommendation": analysis.summary.get("RECOMMENDATION", "NEUTRAL"),
                    "buy": analysis.summary.get("BUY", 0),
                    "sell": analysis.summary.get("SELL", 0),
                    "neutral": analysis.summary.get("NEUTRAL", 0),
                    "oscillators": analysis.oscillators.get("RECOMMENDATION", "NEUTRAL"),
                    "moving_averages": analysis.moving_averages.get("RECOMMENDATION", "NEUTRAL"),
                    "rsi": analysis.indicators.get("RSI"),
                    "macd_hist": analysis.indicators.get("MACD.macd"),
                    "macd_signal": analysis.indicators.get("MACD.signal"),
                    "stoch_k": analysis.indicators.get("Stoch.K"),
                    "stoch_d": analysis.indicators.get("Stoch.D"),
                    "cci": analysis.indicators.get("CCI20"),
                    "adx": analysis.indicators.get("ADX"),
                    "ao": analysis.indicators.get("AO"),
                    "bb_upper": analysis.indicators.get("BB.upper"),
                    "bb_lower": analysis.indicators.get("BB.lower"),
                    "ema_10": analysis.indicators.get("EMA10"),
                    "ema_20": analysis.indicators.get("EMA20"),
                    "ema_50": analysis.indicators.get("EMA50"),
                    "close": analysis.indicators.get("close"),
                }
            except Exception as e:
                log.debug("TradingView %s %s failed: %s", tv_symbol, label, e)
                continue

        return results if results else None

    except ImportError:
        log.warning("tradingview_ta not installed — using Binance only")
        return None
    except Exception as e:
        log.debug("TradingView analysis failed for %s: %s", symbol, e)
        return None


# === Binance Data ===

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
                "buy_volume": float(c[9]),
            }
            for c in raw
        ]
    except Exception as e:
        log.error("Failed to fetch %s %s candles: %s", symbol, interval, e)
        return None


def _calc_volume_pressure(candles: list[dict]) -> dict:
    """Analyze buying vs selling volume pressure."""
    if not candles:
        return {"buy_ratio": 0.5, "volume_trend": 0}

    recent = candles[-10:]
    total_vol = sum(c["volume"] for c in recent)
    total_buy = sum(c["buy_volume"] for c in recent)
    buy_ratio = total_buy / total_vol if total_vol > 0 else 0.5

    if len(candles) >= 20:
        vol_recent = sum(c["volume"] for c in candles[-5:])
        vol_prior = sum(c["volume"] for c in candles[-10:-5])
        volume_trend = (vol_recent - vol_prior) / vol_prior if vol_prior > 0 else 0
    else:
        volume_trend = 0

    return {"buy_ratio": buy_ratio, "volume_trend": volume_trend}


def is_crypto_updown_market(question: str) -> bool:
    """Check if this is a 5-minute crypto Up/Down market."""
    q = question.lower()
    return "up or down" in q and any(
        coin in q for coin in ["bitcoin", "ethereum", "solana", "btc", "eth", "sol",
                               "xrp", "dogecoin", "doge", "cardano", "ada"]
    )


def estimate_crypto_probability(question: str, market_price: float, outcome: str) -> dict | None:
    """Estimate probability using TradingView + Binance analysis.

    Signal weights:
    - TradingView 1m recommendation (25%) — professional consensus
    - TradingView 5m recommendation (15%) — trend confirmation
    - TradingView 15m recommendation (10%) — bigger picture
    - Binance 3m momentum (15%) — immediate price action
    - Binance volume pressure (10%) — buying vs selling
    - TradingView RSI (10%) — overbought/oversold
    - TradingView MACD (10%) — trend direction
    - TradingView oscillators consensus (5%) — combined oscillator vote
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

    is_up_outcome = outcome.lower() in ["yes", "up"]

    # === Get TradingView signals ===
    tv = get_tradingview_analysis(symbol)

    # === Get Binance data for momentum + volume ===
    candles_1m = _fetch_candles(symbol, "1m", 30)
    if not candles_1m:
        return None

    closes = [c["close"] for c in candles_1m]
    current_price = closes[-1]
    mom_3m = (closes[-1] - closes[-4]) / closes[-4] * 100
    vol = _calc_volume_pressure(candles_1m)

    # === SCORING SYSTEM ===
    signals = []
    signal_details = []

    # --- TradingView signals (55% total weight) ---
    if tv:
        # 1. TradingView 1-minute recommendation (25%)
        if "1m" in tv:
            rec = tv["1m"]["recommendation"]
            buy_count = tv["1m"]["buy"]
            sell_count = tv["1m"]["sell"]
            total_votes = buy_count + sell_count + tv["1m"]["neutral"]

            if "BUY" in rec:
                score = 0.55 + (buy_count / max(total_votes, 1)) * 0.20
            elif "SELL" in rec:
                score = 0.45 - (sell_count / max(total_votes, 1)) * 0.20
            else:
                score = 0.50
            signals.append(("tv_1m", score, 0.25))
            signal_details.append(f"TV-1m:{rec} ({buy_count}B/{sell_count}S)")

        # 2. TradingView 5-minute recommendation (15%)
        if "5m" in tv:
            rec = tv["5m"]["recommendation"]
            if "STRONG_BUY" in rec:
                signals.append(("tv_5m", 0.68, 0.15))
            elif "BUY" in rec:
                signals.append(("tv_5m", 0.60, 0.15))
            elif "STRONG_SELL" in rec:
                signals.append(("tv_5m", 0.32, 0.15))
            elif "SELL" in rec:
                signals.append(("tv_5m", 0.40, 0.15))
            else:
                signals.append(("tv_5m", 0.50, 0.15))
            signal_details.append(f"TV-5m:{rec}")

        # 3. TradingView 15-minute recommendation (10%)
        if "15m" in tv:
            rec = tv["15m"]["recommendation"]
            if "STRONG_BUY" in rec:
                signals.append(("tv_15m", 0.65, 0.10))
            elif "BUY" in rec:
                signals.append(("tv_15m", 0.58, 0.10))
            elif "STRONG_SELL" in rec:
                signals.append(("tv_15m", 0.35, 0.10))
            elif "SELL" in rec:
                signals.append(("tv_15m", 0.42, 0.10))
            else:
                signals.append(("tv_15m", 0.50, 0.10))
            signal_details.append(f"TV-15m:{rec}")

        # 4. TradingView RSI (10%)
        rsi = tv.get("1m", {}).get("rsi")
        if rsi is not None:
            if rsi > 70:
                signals.append(("tv_rsi", 0.32, 0.10))
                signal_details.append(f"RSI:{rsi:.0f} OVERBOUGHT")
            elif rsi < 30:
                signals.append(("tv_rsi", 0.68, 0.10))
                signal_details.append(f"RSI:{rsi:.0f} OVERSOLD")
            elif rsi > 60:
                signals.append(("tv_rsi", 0.45, 0.10))
                signal_details.append(f"RSI:{rsi:.0f}")
            elif rsi < 40:
                signals.append(("tv_rsi", 0.55, 0.10))
                signal_details.append(f"RSI:{rsi:.0f}")
            else:
                signals.append(("tv_rsi", 0.50, 0.10))
                signal_details.append(f"RSI:{rsi:.0f}")

        # 5. TradingView MACD (10%)  -- use from best available timeframe
        for tf in ["1m", "5m"]:
            if tf in tv and tv[tf].get("macd_hist") is not None:
                macd_h = tv[tf]["macd_hist"]
                macd_s = tv[tf].get("macd_signal", 0)
                if macd_h > 0 and (macd_h > macd_s if macd_s else True):
                    signals.append(("tv_macd", 0.62, 0.10))
                    signal_details.append(f"MACD:bullish")
                elif macd_h < 0:
                    signals.append(("tv_macd", 0.38, 0.10))
                    signal_details.append(f"MACD:bearish")
                else:
                    signals.append(("tv_macd", 0.50, 0.10))
                break

        # 6. TradingView oscillators consensus (5%)
        osc = tv.get("1m", {}).get("oscillators", "NEUTRAL")
        if "BUY" in osc:
            signals.append(("tv_osc", 0.60, 0.05))
        elif "SELL" in osc:
            signals.append(("tv_osc", 0.40, 0.05))
        else:
            signals.append(("tv_osc", 0.50, 0.05))

    # --- Binance signals (25% total weight, or 80% if no TradingView) ---
    binance_weight_mom = 0.15 if tv else 0.40
    binance_weight_vol = 0.10 if tv else 0.20

    # 7. Binance 3m momentum
    if mom_3m > 0.1:
        signals.append(("momentum", 0.68, binance_weight_mom))
        signal_details.append(f"Mom:{mom_3m:+.3f}% UP")
    elif mom_3m > 0.03:
        signals.append(("momentum", 0.58, binance_weight_mom))
        signal_details.append(f"Mom:{mom_3m:+.3f}% up")
    elif mom_3m > -0.03:
        signals.append(("momentum", 0.50, binance_weight_mom))
        signal_details.append(f"Mom:{mom_3m:+.3f}% flat")
    elif mom_3m > -0.1:
        signals.append(("momentum", 0.42, binance_weight_mom))
        signal_details.append(f"Mom:{mom_3m:+.3f}% down")
    else:
        signals.append(("momentum", 0.32, binance_weight_mom))
        signal_details.append(f"Mom:{mom_3m:+.3f}% DOWN")

    # 8. Binance volume pressure
    buy_ratio = vol["buy_ratio"]
    if buy_ratio > 0.58:
        signals.append(("volume", 0.62, binance_weight_vol))
        signal_details.append(f"Vol:{buy_ratio:.0%}buy")
    elif buy_ratio < 0.42:
        signals.append(("volume", 0.38, binance_weight_vol))
        signal_details.append(f"Vol:{buy_ratio:.0%}buy")
    else:
        signals.append(("volume", 0.50, binance_weight_vol))

    # === Combine all signals ===
    if not signals:
        return None

    total_weight = sum(s[2] for s in signals)
    up_prob = sum(s[1] * s[2] for s in signals) / total_weight

    # Clamp
    up_prob = max(0.25, min(0.75, up_prob))

    # === Confidence: how many signals agree? ===
    bullish = sum(1 for s in signals if s[1] > 0.55)
    bearish = sum(1 for s in signals if s[1] < 0.45)
    agreement = max(bullish, bearish)

    if agreement >= 6:
        confidence = "high"
    elif agreement >= 4:
        confidence = "medium"
    else:
        confidence = "low"

    # Skip unclear signals
    if confidence == "low":
        log.info("Crypto SKIP '%s' — signals mixed (%d bull/%d bear)", question[:40], bullish, bearish)
        return None

    if abs(up_prob - 0.50) < 0.03:
        log.info("Crypto SKIP '%s' — too close to 50/50 (%.2f)", question[:40], up_prob)
        return None

    if is_up_outcome:
        prob = up_prob
    else:
        prob = 1.0 - up_prob

    # Build reasoning string
    tv_status = "TV+Binance" if tv else "Binance-only"
    details_str = " | ".join(signal_details[:6])
    reasoning = f"{symbol}: ${current_price:.2f} [{tv_status}] | {details_str}"

    log.info("Crypto TA for '%s' (%s): prob=%.2f conf=%s | %s",
             question[:50], outcome, prob, confidence, reasoning)

    return {
        "probability": prob,
        "confidence": confidence,
        "reasoning": reasoning,
    }
