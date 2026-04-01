"""Crypto price predictor — real-time momentum edge detection.

Strategy: Detect price movements on Binance that haven't been priced
into Polymarket's 5-minute binary markets yet.

Data sources:
1. Binance 1-second/1-minute candles — detect momentum in real-time
2. Binance order book — confirm the move has buying/selling pressure
3. Binance trade stream — volume confirmation

The edge: Polymarket binary markets reprice slowly. When BTC moves +0.15%
in 60 seconds on Binance, the "Up" contract is still at ~50 cents.
We buy before the market catches up.
"""

import httpx
import config
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


# === TradingView Integration (optional confirmation only) ===

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


# === Real-Time Momentum Detection ===

def get_realtime_momentum(symbol: str) -> dict | None:
    """Fetch real-time price momentum from Binance.

    Returns momentum metrics for the last 60-180 seconds.
    """
    candles_1m = _fetch_candles(symbol, "1m", 5)
    if not candles_1m or len(candles_1m) < 3:
        return None

    current_price = candles_1m[-1]["close"]
    price_60s_ago = candles_1m[-2]["open"]  # Open of previous 1m candle ~ 60s ago
    price_180s_ago = candles_1m[-4]["open"] if len(candles_1m) >= 4 else candles_1m[0]["open"]

    price_change_60s = (current_price - price_60s_ago) / price_60s_ago * 100
    price_change_180s = (current_price - price_180s_ago) / price_180s_ago * 100

    # Volume analysis
    avg_volume = sum(c["volume"] for c in candles_1m[:-1]) / max(len(candles_1m) - 1, 1)
    current_volume = candles_1m[-1]["volume"]
    volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0

    # Buy pressure
    total_vol = sum(c["volume"] for c in candles_1m[-3:])
    total_buy = sum(c["buy_volume"] for c in candles_1m[-3:])
    buy_pressure = total_buy / total_vol if total_vol > 0 else 0.5

    # Acceleration: is the move getting stronger?
    recent_move = candles_1m[-1]["close"] - candles_1m[-1]["open"]
    prior_move = candles_1m[-2]["close"] - candles_1m[-2]["open"]
    is_accelerating = (recent_move > 0 and prior_move > 0 and abs(recent_move) > abs(prior_move)) or \
                      (recent_move < 0 and prior_move < 0 and abs(recent_move) > abs(prior_move))

    return {
        "current_price": current_price,
        "price_change_60s": price_change_60s,
        "price_change_180s": price_change_180s,
        "volume_ratio": volume_ratio,
        "buy_pressure": buy_pressure,
        "is_accelerating": is_accelerating,
    }


def get_orderbook_imbalance(symbol: str) -> dict | None:
    """Fetch order book and calculate bid/ask imbalance.

    A bid_ratio > 0.60 means more buy pressure (bullish).
    A bid_ratio < 0.40 means more sell pressure (bearish).
    """
    try:
        resp = httpx.get(
            "https://api.binance.com/api/v3/depth",
            params={"symbol": symbol, "limit": 20},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()

        # Sum bid and ask volumes (top 20 levels)
        bid_volume = sum(float(level[1]) for level in data.get("bids", []))
        ask_volume = sum(float(level[1]) for level in data.get("asks", []))
        total = bid_volume + ask_volume

        if total <= 0:
            return None

        bid_ratio = bid_volume / total  # > 0.5 = more buyers

        # Check for thin walls — if asks are very thin, price can move up easily
        top_5_asks = sum(float(level[1]) for level in data.get("asks", [])[:5])
        top_5_bids = sum(float(level[1]) for level in data.get("bids", [])[:5])
        thin_asks = top_5_asks < top_5_bids * 0.3  # Asks are <30% of bids at top levels
        thin_bids = top_5_bids < top_5_asks * 0.3  # Bids are <30% of asks at top levels

        return {
            "bid_ratio": bid_ratio,
            "bid_volume": bid_volume,
            "ask_volume": ask_volume,
            "thin_asks": thin_asks,  # Easy to push price up
            "thin_bids": thin_bids,  # Easy to push price down
        }
    except Exception as e:
        log.debug("Failed to fetch orderbook for %s: %s", symbol, e)
        return None


def get_large_trades(symbol: str) -> dict | None:
    """Detect large/whale trades in the last minute.

    A large trade is one that's > 5x the median trade size.
    """
    try:
        resp = httpx.get(
            "https://api.binance.com/api/v3/aggTrades",
            params={"symbol": symbol, "limit": 200},
            timeout=5,
        )
        resp.raise_for_status()
        trades = resp.json()

        if not trades:
            return None

        # Calculate trade sizes in quote currency (price * qty)
        trade_sizes = []
        buy_volume = 0.0
        sell_volume = 0.0

        for t in trades:
            qty = float(t["q"])
            price = float(t["p"])
            size_usd = qty * price
            trade_sizes.append(size_usd)

            # m=True means the buyer is the maker (so it's a SELL/taker sell)
            if t.get("m", False):
                sell_volume += size_usd
            else:
                buy_volume += size_usd

        if not trade_sizes:
            return None

        # Find large trades (> 5x median)
        sorted_sizes = sorted(trade_sizes)
        median_size = sorted_sizes[len(sorted_sizes) // 2]
        large_threshold = median_size * 5

        large_buys = sum(1 for i, t in enumerate(trades) if trade_sizes[i] > large_threshold and not t.get("m", False))
        large_sells = sum(1 for i, t in enumerate(trades) if trade_sizes[i] > large_threshold and t.get("m", False))

        total_volume = buy_volume + sell_volume
        taker_buy_ratio = buy_volume / total_volume if total_volume > 0 else 0.5

        # Trade frequency — number of trades in the sample
        trade_count = len(trades)

        return {
            "large_buys": large_buys,
            "large_sells": large_sells,
            "taker_buy_ratio": taker_buy_ratio,
            "trade_count": trade_count,
            "net_large": large_buys - large_sells,  # Positive = whale buying
        }
    except Exception as e:
        log.debug("Failed to fetch aggTrades for %s: %s", symbol, e)
        return None


def get_funding_rate(symbol: str) -> dict | None:
    """Fetch funding rate from Binance futures.

    Positive funding = longs pay shorts (overleveraged long, bearish signal)
    Negative funding = shorts pay longs (overleveraged short, bullish signal)
    Extreme values (> 0.01% or < -0.01%) are contrarian signals.
    """
    try:
        resp = httpx.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol": symbol},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()

        funding_rate = float(data.get("lastFundingRate", 0))
        mark_price = float(data.get("markPrice", 0))

        # Extreme funding = contrarian signal
        is_extreme_long = funding_rate > 0.0005   # > 0.05% = overleveraged long
        is_extreme_short = funding_rate < -0.0005  # < -0.05% = overleveraged short

        return {
            "funding_rate": funding_rate,
            "mark_price": mark_price,
            "is_extreme_long": is_extreme_long,   # Contrarian bearish
            "is_extreme_short": is_extreme_short,  # Contrarian bullish
        }
    except Exception as e:
        log.debug("Failed to fetch funding rate for %s: %s", symbol, e)
        return None


def get_open_interest(symbol: str) -> dict | None:
    """Fetch open interest from Binance futures.

    Rising OI + rising price = strong trend (new money entering)
    Rising OI + falling price = strong downtrend
    Falling OI = positions closing, move may not sustain
    """
    try:
        resp = httpx.get(
            "https://fapi.binance.com/fapi/v1/openInterest",
            params={"symbol": symbol},
            timeout=5,
        )
        resp.raise_for_status()
        current = resp.json()
        current_oi = float(current.get("openInterest", 0))

        if current_oi <= 0:
            return None

        return {
            "open_interest": current_oi,
        }
    except Exception as e:
        log.debug("Failed to fetch open interest for %s: %s", symbol, e)
        return None


def estimate_crypto_probability(question: str, market_price: float, outcome: str) -> dict | None:
    """Estimate probability using real-time momentum from Binance.

    Momentum-based signal detection:
    1. Get real-time price momentum from Binance
    2. Determine signal strength (strong/medium/none)
    3. Apply confidence boosters (volume, acceleration, trend, pressure)
    4. Optional TradingView confirmation (small boost if agrees, no penalty if disagrees)
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

    # === Get real-time momentum from Binance ===
    momentum = get_realtime_momentum(symbol)
    if not momentum:
        return None

    price_change_60s = momentum["price_change_60s"]
    price_change_180s = momentum["price_change_180s"]
    volume_ratio = momentum["volume_ratio"]
    buy_pressure = momentum["buy_pressure"]
    is_accelerating = momentum["is_accelerating"]
    current_price = momentum["current_price"]

    # === Determine signal direction and strength ===
    abs_change = abs(price_change_60s)

    if abs_change >= config.MOMENTUM_THRESHOLD_STRONG:
        signal_strength = "strong"
        base_prob = 0.68
    elif abs_change >= config.MOMENTUM_THRESHOLD_MEDIUM:
        signal_strength = "medium"
        base_prob = 0.60
    else:
        # No signal — price not moving enough
        log.info("Crypto SKIP '%s' — momentum too weak (%.4f%%)", question[:40], price_change_60s)
        return None

    # Direction: positive change = UP signal, negative = DOWN signal
    signal_up = price_change_60s > 0

    # === Confidence boosters ===
    boosters = 0
    booster_details = []

    # Volume spike confirms the move
    if volume_ratio > config.VOLUME_SPIKE_THRESHOLD:
        base_prob += 0.05
        boosters += 1
        booster_details.append(f"vol_spike:{volume_ratio:.1f}x")

    # Acceleration — move is getting stronger
    if is_accelerating:
        base_prob += 0.03
        boosters += 1
        booster_details.append("accelerating")

    # Sustained trend — 180s move in same direction as 60s
    if (price_change_180s > 0) == (price_change_60s > 0) and abs(price_change_180s) > abs(price_change_60s) * 0.5:
        base_prob += 0.03
        boosters += 1
        booster_details.append(f"trend_180s:{price_change_180s:+.4f}%")

    # Buy/sell pressure confirms direction
    if signal_up and buy_pressure > 0.60:
        base_prob += 0.03
        boosters += 1
        booster_details.append(f"buy_pressure:{buy_pressure:.0%}")
    elif not signal_up and buy_pressure < 0.40:
        base_prob += 0.03
        boosters += 1
        booster_details.append(f"sell_pressure:{1-buy_pressure:.0%}")

    # === Additional data sources as boosters ===

    # Order book imbalance
    orderbook = get_orderbook_imbalance(symbol)
    if orderbook:
        if signal_up and orderbook["bid_ratio"] > 0.60:
            base_prob += 0.03
            boosters += 1
            booster_details.append(f"ob_bid:{orderbook['bid_ratio']:.0%}")
        elif not signal_up and orderbook["bid_ratio"] < 0.40:
            base_prob += 0.03
            boosters += 1
            booster_details.append(f"ob_ask:{1-orderbook['bid_ratio']:.0%}")
        # Thin wall detection — strong signal
        if signal_up and orderbook.get("thin_asks"):
            base_prob += 0.02
            boosters += 1
            booster_details.append("thin_asks")
        elif not signal_up and orderbook.get("thin_bids"):
            base_prob += 0.02
            boosters += 1
            booster_details.append("thin_bids")

    # Large trade / whale detection
    large_trades = get_large_trades(symbol)
    if large_trades:
        net_large = large_trades["net_large"]
        if signal_up and net_large >= 2:
            base_prob += 0.04
            boosters += 1
            booster_details.append(f"whale_buy:{net_large}")
        elif not signal_up and net_large <= -2:
            base_prob += 0.04
            boosters += 1
            booster_details.append(f"whale_sell:{abs(net_large)}")
        # Taker buy ratio confirmation
        tbr = large_trades["taker_buy_ratio"]
        if signal_up and tbr > 0.60:
            base_prob += 0.02
            boosters += 1
            booster_details.append(f"taker_buy:{tbr:.0%}")
        elif not signal_up and tbr < 0.40:
            base_prob += 0.02
            boosters += 1
            booster_details.append(f"taker_sell:{1-tbr:.0%}")

    # Funding rate — CONTRARIAN signal (filter, not booster)
    # If momentum says UP but funding is extremely positive, the move might reverse
    funding = get_funding_rate(symbol)
    if funding:
        if signal_up and funding["is_extreme_long"]:
            base_prob -= 0.03  # Penalize — overleveraged longs may get squeezed down
            booster_details.append(f"funding_warn:{funding['funding_rate']:.4%}")
        elif not signal_up and funding["is_extreme_short"]:
            base_prob -= 0.03  # Penalize — overleveraged shorts may get squeezed up
            booster_details.append(f"funding_warn:{funding['funding_rate']:.4%}")
        elif signal_up and funding["is_extreme_short"]:
            base_prob += 0.02  # Confirms — shorts overleveraged, squeeze up likely
            boosters += 1
            booster_details.append(f"funding_confirms:{funding['funding_rate']:.4%}")
        elif not signal_up and funding["is_extreme_long"]:
            base_prob += 0.02  # Confirms — longs overleveraged, squeeze down likely
            boosters += 1
            booster_details.append(f"funding_confirms:{funding['funding_rate']:.4%}")

    # Open interest — confirmation signal
    oi_data = get_open_interest(symbol)
    if oi_data:
        # We can't easily compare to previous OI in a stateless check,
        # so just log it in the reasoning for now
        booster_details.append(f"oi:{oi_data['open_interest']:.0f}")

    # Cap probability at 0.85 (was 0.80 — more data sources justify higher confidence)
    base_prob = max(0.20, min(0.85, base_prob))

    # === Confidence level ===
    if signal_strength == "strong" and boosters >= 2:
        confidence = "high"
    elif (signal_strength == "medium" and boosters >= 1) or signal_strength == "strong":
        confidence = "medium"
    else:
        log.info("Crypto SKIP '%s' — low confidence (%s signal, %d boosters)",
                 question[:40], signal_strength, boosters)
        return None

    # === Optional TradingView confirmation ===
    tv = get_tradingview_analysis(symbol)
    tv_status = "Binance-only"
    if tv and "1m" in tv:
        rec = tv["1m"]["recommendation"]
        tv_agrees = (signal_up and "BUY" in rec) or (not signal_up and "SELL" in rec)
        if tv_agrees:
            base_prob = min(0.80, base_prob + 0.02)
            tv_status = f"TV-confirms({rec})"
            booster_details.append(f"tv_1m:{rec}")
        else:
            tv_status = f"TV-disagrees({rec})"
            # No penalty — TV lags, momentum is more reliable

    # === Apply direction ===
    # base_prob is probability that the signal direction is correct
    if signal_up:
        up_prob = base_prob
    else:
        up_prob = 1.0 - base_prob

    # Flip for outcome
    if is_up_outcome:
        prob = up_prob
    else:
        prob = 1.0 - up_prob

    # Build reasoning
    direction = "UP" if signal_up else "DOWN"
    boosters_str = " | ".join(booster_details) if booster_details else "none"
    reasoning = (f"{symbol}: ${current_price:.2f} [{tv_status}] | "
                 f"60s:{price_change_60s:+.4f}% {direction} ({signal_strength}) | "
                 f"boosters: {boosters_str}")

    log.info("Crypto MOMENTUM for '%s' (%s): prob=%.2f conf=%s | %s",
             question[:50], outcome, prob, confidence, reasoning)

    return {
        "probability": prob,
        "confidence": confidence,
        "reasoning": reasoning,
    }
