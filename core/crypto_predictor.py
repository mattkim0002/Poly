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

import json
import os
import time
from datetime import datetime, timezone
import httpx
import config
from utils.logger import log

# Strategy config loaded from supervisor output
_strategy_config = None
_strategy_config_mtime = 0
STRATEGY_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "strategy_config.json")


def _load_strategy_config() -> dict:
    """Load strategy config from JSON, reload if file changed."""
    global _strategy_config, _strategy_config_mtime
    try:
        mtime = os.path.getmtime(STRATEGY_CONFIG_PATH)
        if _strategy_config is None or mtime > _strategy_config_mtime:
            with open(STRATEGY_CONFIG_PATH) as f:
                _strategy_config = json.load(f)
            _strategy_config_mtime = mtime
            log.info("Loaded strategy config (updated: %s)", _strategy_config.get("updated_at", "unknown"))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        if _strategy_config is None:
            log.warning("No strategy_config.json found, using defaults: %s", e)
            _strategy_config = {}
    return _strategy_config or {}


def get_regime(symbol: str = "BTCUSDT") -> str:
    """Returns 'trending', 'choppy', or 'dead' based on last 10 five-minute candles.

    Uses ATR as volatility proxy + directional consistency.
    - dead: ATR < 0.01% — truly no movement, skip
    - trending: 6+/9 candles same direction — trade it
    - choppy: mixed candles — only trade with strong signals
    """
    try:
        resp = httpx.get(
            "https://data-api.binance.vision/api/v3/klines",
            params={"symbol": symbol, "interval": "5m", "limit": 10},
            timeout=10,
        )
        resp.raise_for_status()
        candles = resp.json()
    except Exception as e:
        log.error("Failed to fetch 5m candles for regime: %s", e)
        return "choppy"

    if not candles or len(candles) < 5:
        return "choppy"

    # ATR = average of (high - low) per candle
    ranges = [float(c[2]) - float(c[3]) for c in candles]
    atr = sum(ranges) / len(ranges)
    price = float(candles[-1][4])  # close price
    atr_pct = atr / price if price > 0 else 0

    # Directional consistency: are candles mostly going one way?
    closes = [float(c[4]) for c in candles]
    ups = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1])

    log.info("[REGIME] %s=$%.0f ATR=$%.1f (%.4f%%) ups=%d/9 (threshold: 0.03%%)",
             symbol, price, atr, atr_pct * 100, ups)

    if atr_pct < 0.0003:
        return "dead"          # Below 0.03% — no meaningful movement
    elif ups >= 6 or ups <= 3:
        return "trending"      # 6+/9 same direction = good enough
    else:
        return "choppy"        # 4-5 ups out of 9 = no clear direction


def get_candle_position() -> dict:
    """Returns where we are inside the current 5-minute candle.

    Polymarket 5m markets start at :00, :05, :10... aligned to UTC.
    - entry_window: first 90s — best time to enter
    - mid_candle: 90-210s — only enter on strong signals
    - late_danger: >210s (3:30 in) — never enter
    """
    now = datetime.now(timezone.utc)
    seconds_into_candle = (now.minute % 5) * 60 + now.second

    phase = (
        "entry_window" if seconds_into_candle <= 90 else
        "mid_candle" if seconds_into_candle <= 210 else
        "late_danger"
    )

    return {
        "seconds_elapsed": seconds_into_candle,
        "seconds_remaining": 300 - seconds_into_candle,
        "phase": phase,
    }


def should_exit_early(entry_price: float, current_price: float, size: float) -> bool:
    """Time-based early exit for 5-min crypto positions.

    - Exit if profitable AND less than 45 seconds left (lock in gains)
    - Exit if losing >15% AND less than 30 seconds left (cut loss, free capital)
    """
    candle = get_candle_position()
    entry_cost = entry_price * size
    current_value = current_price * size
    pnl_pct = (current_value - entry_cost) / entry_cost if entry_cost > 0 else 0

    # Lock in any gain near expiry
    if candle["seconds_remaining"] < 45 and pnl_pct > 0.05:
        return True

    # Cut loss near expiry to free capital for next candle
    if candle["seconds_remaining"] < 30 and pnl_pct < -0.15:
        return True

    return False

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
    "bnb": "BNBUSDT",
}

# Cross-exchange reference (Coinbase) — acts as oracle/second data source
COINBASE_SYMBOLS = {
    "BTCUSDT": "BTC-USD",
    "ETHUSDT": "ETH-USD",
    "SOLUSDT": "SOL-USD",
    "XRPUSDT": "XRP-USD",
    "DOGEUSDT": "DOGE-USD",
    "ADAUSDT": "ADA-USD",
    "BNBUSDT": "BNB-USD",
}

# 15-second in-memory cache so we don't hammer Coinbase every scan
_coinbase_cache: dict[str, tuple[float, float]] = {}


def get_coinbase_price(binance_symbol: str) -> float | None:
    """Fetch spot price from Coinbase as a cross-exchange reference.

    Acts as an "oracle" to validate that a Binance move is real market-wide,
    not a Binance-only glitch. Returns None on any failure (informational only,
    never blocks a trade).
    """
    import time
    import httpx

    cb_symbol = COINBASE_SYMBOLS.get(binance_symbol)
    if not cb_symbol:
        return None

    # Serve from cache if < 15s old
    now = time.time()
    cached = _coinbase_cache.get(cb_symbol)
    if cached and (now - cached[1]) < 15:
        return cached[0]

    try:
        url = f"https://api.coinbase.com/v2/prices/{cb_symbol}/spot"
        resp = httpx.get(url, timeout=3)
        resp.raise_for_status()
        data = resp.json()
        price = float(data["data"]["amount"])
        _coinbase_cache[cb_symbol] = (price, now)
        return price
    except Exception:
        return None


def get_cross_exchange_agreement(binance_symbol: str, binance_price: float) -> dict:
    """Compare Binance vs Coinbase spot. Returns divergence info.

    Used as context for the Claude gate — NOT a hard blocker. If divergence
    is > 0.3%, Claude can decide whether to skip or trade anyway.
    """
    cb_price = get_coinbase_price(binance_symbol)
    if cb_price is None or binance_price <= 0:
        return {"coinbase_price": None, "divergence_pct": None, "agrees": True}

    divergence = (binance_price - cb_price) / cb_price * 100
    agrees = abs(divergence) < 0.30  # within 0.3% is "agreement"
    return {
        "coinbase_price": round(cb_price, 2),
        "divergence_pct": round(divergence, 3),
        "agrees": agrees,
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
            "https://data-api.binance.vision/api/v3/klines",
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
    """Check if this is a crypto Up/Down market."""
    q = question.lower()
    return "up or down" in q and any(
        coin in q for coin in ["bitcoin", "ethereum", "solana", "btc", "eth", "sol",
                               "xrp", "dogecoin", "doge", "cardano", "ada", "bnb"]
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
            "https://data-api.binance.vision/api/v3/depth",
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
            "https://data-api.binance.vision/api/v3/aggTrades",
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



def get_binance_implied_probability(symbol: str, direction: str = "up") -> float | None:
    """Calculate implied probability that the coin goes UP based on Binance data.

    Uses last 60 seconds of 1-minute candles:
    - Price vs open: if current > open, lean UP
    - Volume pressure: buy volume / total volume
    - Rate of change: (current - 60s_ago) / 60s_ago

    Returns float 0.0-1.0 representing implied UP probability.
    """
    candles = _fetch_candles(symbol, "1m", 3)
    if not candles or len(candles) < 2:
        return None

    current_price = candles[-1]["close"]
    open_price = candles[-2]["open"]  # ~60 seconds ago

    # Component 1: price vs open (0.0-1.0)
    if open_price <= 0:
        return None
    price_ratio = (current_price - open_price) / open_price
    # Map to probability: +0.1% move → ~0.65, -0.1% → ~0.35, flat → 0.50
    price_signal = 0.5 + (price_ratio * 500)  # ±0.1% → ±0.05 shift
    price_signal = max(0.1, min(0.9, price_signal))

    # Component 2: buy volume pressure (0.0-1.0)
    total_vol = sum(c["volume"] for c in candles[-2:])
    total_buy = sum(c["buy_volume"] for c in candles[-2:])
    buy_pressure = total_buy / total_vol if total_vol > 0 else 0.5

    # Component 3: rate of change magnitude
    roc = abs(price_ratio) * 100  # as percentage
    roc_weight = min(roc / 0.1, 1.0)  # 0.1% move = full weight

    # Weighted combination
    implied_up = (price_signal * 0.5) + (buy_pressure * 0.3) + (0.5 * 0.2)

    # Strengthen signal if rate of change is significant
    if roc_weight > 0.5:
        if price_ratio > 0:
            implied_up = min(0.90, implied_up + roc_weight * 0.1)
        else:
            implied_up = max(0.10, implied_up - roc_weight * 0.1)

    implied_up = max(0.05, min(0.95, implied_up))

    log.info("[GAP SIGNAL] %s: price=$%.2f open=$%.2f roc=%.4f%% buy_pressure=%.0f%% implied_up=%.0f%%",
             symbol, current_price, open_price, price_ratio * 100, buy_pressure * 100, implied_up * 100)

    return implied_up


def get_higher_timeframe_trend(symbol: str) -> dict:
    """Check 1h and 4h trends to determine the dominant direction.

    Only take momentum trades aligned with the bigger trend.
    Returns {"direction": "up"/"down"/"sideways", "strength": float}
    """
    result = {"direction": "sideways", "strength": 0.0, "details": ""}

    try:
        # Fetch 4h candles (last 6 = 24 hours)
        resp_4h = httpx.get(
            "https://data-api.binance.vision/api/v3/klines",
            params={"symbol": symbol, "interval": "4h", "limit": 6},
            timeout=10,
        )
        resp_4h.raise_for_status()
        candles_4h = resp_4h.json()

        # Fetch 1h candles (last 12 = 12 hours)
        resp_1h = httpx.get(
            "https://data-api.binance.vision/api/v3/klines",
            params={"symbol": symbol, "interval": "1h", "limit": 12},
            timeout=10,
        )
        resp_1h.raise_for_status()
        candles_1h = resp_1h.json()
    except Exception as e:
        log.debug("Failed to fetch HTF candles for %s: %s", symbol, e)
        return result

    if not candles_4h or not candles_1h:
        return result

    # 4h trend: compare first open to last close
    open_4h = float(candles_4h[0][1])
    close_4h = float(candles_4h[-1][4])
    change_4h = (close_4h - open_4h) / open_4h * 100

    # 1h trend: compare first open to last close
    open_1h = float(candles_1h[0][1])
    close_1h = float(candles_1h[-1][4])
    change_1h = (close_1h - open_1h) / open_1h * 100

    # Higher highs / higher lows check on 1h
    closes_1h = [float(c[4]) for c in candles_1h]
    higher_count = sum(1 for i in range(1, len(closes_1h)) if closes_1h[i] > closes_1h[i - 1])
    lower_count = len(closes_1h) - 1 - higher_count

    # Determine direction — both timeframes should agree
    if change_4h > 0.1 and change_1h > 0.05 and higher_count >= 7:
        direction = "up"
        strength = min(abs(change_4h), 3.0) / 3.0  # 0-1 scale
    elif change_4h < -0.1 and change_1h < -0.05 and lower_count >= 7:
        direction = "down"
        strength = min(abs(change_4h), 3.0) / 3.0
    else:
        direction = "sideways"
        strength = 0.0

    details = f"4h:{change_4h:+.2f}% 1h:{change_1h:+.2f}% ups:{higher_count}/11"
    log.info("HTF trend %s: %s (strength=%.1f) | %s", symbol, direction, strength, details)

    return {"direction": direction, "strength": strength, "details": details}


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

    # === CHECK HIGHER TIMEFRAME TREND ===
    htf = get_higher_timeframe_trend(symbol)

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

    # === HIGHER TIMEFRAME ALIGNMENT ===
    # Only trade in the direction of the 1h/4h trend
    if htf["direction"] == "up" and not signal_up:
        log.info("REJECT '%s': signal=DOWN but HTF trend=UP (%s)",
                 question[:40], htf["details"])
        return None
    if htf["direction"] == "down" and signal_up:
        log.info("REJECT '%s': signal=UP but HTF trend=DOWN (%s)",
                 question[:40], htf["details"])
        return None
    if htf["direction"] == "sideways":
        log.info("ALLOW '%s': HTF trend=SIDEWAYS — trading 60s momentum without HTF boost (%s)",
                 question[:40], htf["details"])
        # No longer a hard reject — just skip HTF alignment boost. User wants trades.

    # HTF alignment confirmed
    # === Confidence boosters ===
    boosters = 0
    booster_details = [f"htf_{htf['direction']}:{htf['details']}"]

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

    # === REGIME DETECTION ===
    cfg = _load_strategy_config()
    regime = get_regime(symbol)
    regime_thresholds = cfg.get("regime_thresholds", {})

    # In choppy regime, require more boosters or skip entirely
    if regime == "choppy" and regime_thresholds.get("disable_momentum_choppy", True):
        log.info("Crypto SKIP '%s' — choppy regime, momentum disabled", question[:40])
        return None

    min_boosters_needed = regime_thresholds.get(
        f"min_boosters_{regime}",
        regime_thresholds.get("min_boosters_trending", 2)
    )

    # === SIGNAL COMBO MATCHING ===
    # Build the active signal set from booster_details
    active_signals = set()
    for detail in booster_details:
        tag = detail.split(":")[0]
        if tag.startswith("vol_spike"):
            active_signals.add("volume_spike")
        elif tag == "accelerating":
            active_signals.add("accelerating")
        elif tag.startswith("whale_") or tag.startswith("taker_"):
            active_signals.add("whale")
        elif tag.startswith("ob_") or tag.startswith("thin_"):
            active_signals.add("orderbook_imbalance")
        elif tag.startswith("funding_confirms"):
            active_signals.add("funding_confirms")
        elif tag.startswith("trend_"):
            active_signals.add("trend_180s")

    # Check against approved signal combos from config
    signal_combos = cfg.get("signal_combos", {})
    skip_combos = set(cfg.get("skip_combos", []))

    best_combo = None
    best_combo_key = None

    for combo_key, combo_params in signal_combos.items():
        if combo_key in skip_combos:
            continue
        required_signals = set(combo_key.split("+"))
        if required_signals.issubset(active_signals):
            # This combo is active — use the one with the most signals (most specific)
            if best_combo is None or len(required_signals) > len(best_combo_key.split("+")):
                best_combo = combo_params
                best_combo_key = combo_key

    # Fallback: if no combo matches but enough boosters, use defaults
    if not best_combo:
        if boosters >= min_boosters_needed and signal_strength == "strong":
            best_combo = {"min_edge": cfg.get("risk_params", {}).get("min_edge", 0.05), "kelly_mult": 0.6}
            best_combo_key = "fallback"
        else:
            log.info("Crypto SKIP '%s' — no approved signal combo (%s, %d boosters, regime=%s)",
                     question[:40], "+".join(sorted(active_signals)) or "none", boosters, regime)
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

    # === Apply direction ===
    if signal_up:
        up_prob = base_prob
    else:
        up_prob = 1.0 - base_prob

    if is_up_outcome:
        prob = up_prob
    else:
        prob = 1.0 - up_prob

    # Build reasoning
    direction = "UP" if signal_up else "DOWN"
    boosters_str = " | ".join(booster_details) if booster_details else "none"
    reasoning = (f"{symbol}: ${current_price:.2f} [{tv_status}] regime={regime} | "
                 f"60s:{price_change_60s:+.4f}% {direction} ({signal_strength}) | "
                 f"combo: {best_combo_key} | boosters: {boosters_str}")

    log.info("Crypto MOMENTUM for '%s' (%s): prob=%.2f combo=%s regime=%s | %s",
             question[:50], outcome, prob, best_combo_key, regime, reasoning)

    # === DETERMINISTIC GATE ===

    # Rule 1: Reject if momentum is fading
    if abs(price_change_180s) > 0 and abs(price_change_60s) < abs(price_change_180s) * 0.3:
        log.info("REJECT '%s': momentum fading (60s=%.4f%% vs 180s=%.4f%%)",
                 question[:40], price_change_60s, price_change_180s)
        return None

    # Rule 2: Reject if signals contradict
    if signal_up and buy_pressure < 0.35:
        log.info("REJECT '%s': price up but sell pressure %.0f%%",
                 question[:40], buy_pressure * 100)
        return None
    if not signal_up and buy_pressure > 0.65:
        log.info("REJECT '%s': price down but buy pressure %.0f%%",
                 question[:40], buy_pressure * 100)
        return None

    # Rule 3: Reject if edge too thin after fees (use combo-specific threshold)
    estimated_fee_pct = 0.072 * market_price * (1.0 - market_price)
    net_edge = abs(prob - market_price) - estimated_fee_pct
    combo_min_edge = best_combo.get("min_edge", 0.05)
    if net_edge < combo_min_edge:
        log.info("REJECT '%s': net_edge %.1f%% < combo threshold %.1f%% (%s)",
                 question[:40], net_edge * 100, combo_min_edge * 100, best_combo_key)
        return None

    kelly_mult = best_combo.get("kelly_mult", 1.0)
    log.info("APPROVED '%s': combo=%s net_edge=%.1f%% kelly_mult=%.1f regime=%s",
             question[:40], best_combo_key, net_edge * 100, kelly_mult, regime)

    return {
        "probability": prob,
        "confidence": "high" if boosters >= 3 else "medium",
        "reasoning": reasoning,
        "combo": best_combo_key,
        "kelly_mult": kelly_mult,
        "regime": regime,
    }


def get_btc_confirmation(direction: str) -> dict:
    """BTC-leads-alts confirmation: is BTC's 180s move aligned with the proposed direction?

    Use on NON-BTC trades to avoid buying ETH/SOL UP while BTC is dumping. Returns
    {"agrees": bool, "btc_move_180s_pct": float, "strength": "strong|weak|none"}.

    On any failure returns a neutral-pass result (agrees=True, strength=none) so
    BTC-API flakiness never blocks trading.
    """
    try:
        m = get_realtime_momentum("BTCUSDT")
    except Exception:
        m = None
    if not m:
        return {"agrees": True, "btc_move_180s_pct": 0.0, "strength": "none"}

    move = float(m.get("price_change_180s", 0.0))
    direction = (direction or "").upper()

    if direction == "UP":
        agrees = move >= -0.05  # Not actively dumping
        if move >= 0.10:
            strength = "strong"
        elif move >= 0.0:
            strength = "weak"
        else:
            strength = "none"
    else:  # DOWN
        agrees = move <= 0.05
        if move <= -0.10:
            strength = "strong"
        elif move <= 0.0:
            strength = "weak"
        else:
            strength = "none"

    return {"agrees": agrees, "btc_move_180s_pct": round(move, 3), "strength": strength}
