"""Binance-lag strategy — Binance leads, Polymarket lags.

Detects when Binance price clearly implies UP or DOWN but Polymarket
odds haven't caught up yet. Uses Claude Sonnet as a hard veto gate.

Only trades BTC, SOL, XRP on 5-minute "Up or Down" markets.
"""

import json
from core import crypto_predictor, trader
from utils.logger import log

# Coins this strategy is allowed to trade
SUPPORTED_COINS = {
    "bitcoin": "BTCUSDT",
    "btc": "BTCUSDT",
    "solana": "SOLUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
}

# Minimum move over 10-15 min to consider a trade
MIN_MOVE_PCT = 0.15      # 0.15% move required
# Polymarket must be cheap relative to our estimate
MAX_POLY_PRICE = 0.72    # Only buy if Poly price < 72c
MIN_TRUE_PROB = 0.60     # Our estimate must be >= 60%

CLAUDE_GATE_SYSTEM = """You are a risk filter for a Polymarket 5-minute crypto trading bot.

The bot detects when Binance price action implies UP or DOWN, but Polymarket
odds haven't caught up. Your job: decide if this lag is real and tradeable.
Be willing to approve MEDIUM confidence trades — the bot needs volume.
Only reject when something is clearly broken.

Reply with ONLY this JSON:
{"ok_to_trade": true or false, "reason": "one sentence", "confidence": "low|medium|high"}

Hard rejects:
1. Binance move < 0.10% over 10 min → REJECT (pure noise).
2. Trend directly contradicts trade (lower highs + UP, or higher lows + DOWN) → REJECT.
3. Volume < 0.5x average → REJECT (dead market).
4. NEVER approve a DOWN trade on SOL or XRP when Binance shows higher highs AND higher lows.

Medium-confidence approval is OK when:
- Directional move >= 0.15% with trend aligned, OR
- Volume >= 1.0x average with any positive signal, OR
- Edge >= 5% with no contradiction

High-confidence approval when: strong move (>= 0.3%), volume confirms (>= 1.5x),
trend clearly aligned, edge >= 8%.

Default to approving medium confidence if the setup is not broken.
No text outside the JSON."""


def scan_binance_lag(markets: list[dict]) -> list[dict]:
    """Scan markets for Binance-lag opportunities.

    Returns list of candidate trades with all data for Claude gate.
    Logs per-coin diagnostics (why skipped) so dry cycles are visible.
    """
    candidates = []
    seen_symbols: set[str] = set()

    for market in markets:
        question = market.get("question", "")
        q_lower = question.lower()
        token_ids = market.get("token_ids", [])

        if "up or down" not in q_lower or len(token_ids) < 2:
            continue

        # Must be a supported coin
        symbol = None
        coin_name = None
        for coin, sym in SUPPORTED_COINS.items():
            if coin in q_lower:
                symbol = sym
                coin_name = coin.upper()
                break
        if not symbol:
            continue

        # Only log once per symbol per cycle
        first_seen = symbol not in seen_symbols
        seen_symbols.add(symbol)

        # Get 15 minutes of 1m candles from Binance
        candles = crypto_predictor._fetch_candles(symbol, "1m", 15)
        if not candles or len(candles) < 10:
            if first_seen:
                print(f"  [BINANCE-LAG] {coin_name} → no candle data")
            continue

        current_price = candles[-1]["close"]
        price_10m_ago = candles[-10]["open"]
        price_15m_ago = candles[0]["open"]

        # Calculate move
        move_10m = (current_price - price_10m_ago) / price_10m_ago * 100
        move_15m = (current_price - price_15m_ago) / price_15m_ago * 100

        # Volume analysis
        avg_vol = sum(c["volume"] for c in candles[:-3]) / max(len(candles) - 3, 1)
        recent_vol = sum(c["volume"] for c in candles[-3:]) / 3
        vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0

        # Trend: higher highs/lows or lower highs/lows
        highs = [c["high"] for c in candles[-5:]]
        lows = [c["low"] for c in candles[-5:]]
        higher_highs = all(highs[i] >= highs[i - 1] for i in range(1, len(highs)))
        higher_lows = all(lows[i] >= lows[i - 1] for i in range(1, len(lows)))
        lower_highs = all(highs[i] <= highs[i - 1] for i in range(1, len(highs)))
        lower_lows = all(lows[i] <= lows[i - 1] for i in range(1, len(lows)))

        uptrend = higher_highs or higher_lows
        downtrend = lower_highs or lower_lows

        if first_seen:
            trend_lbl = "up" if uptrend else ("down" if downtrend else "flat")
            print(f"  [BINANCE-LAG] {coin_name} move_10m={move_10m:+.2f}% trend={trend_lbl} vol={vol_ratio:.2f}x")

        # Early diagnostic: move too small
        if abs(move_10m) < MIN_MOVE_PCT:
            if first_seen:
                print(f"  [BINANCE-LAG] {coin_name} move={move_10m:+.2f}% → too small (need {MIN_MOVE_PCT}%)")
            continue

        # Get Polymarket prices
        yes_price = trader.get_midpoint(token_ids[0])
        no_price = trader.get_midpoint(token_ids[1])
        if not yes_price or not no_price:
            continue

        # Buy pressure
        total_vol = sum(c["volume"] for c in candles[-5:])
        total_buy = sum(c["buy_volume"] for c in candles[-5:])
        buy_pressure = total_buy / total_vol if total_vol > 0 else 0.5

        skip_msg = None
        added = False

        # === UP candidate ===
        if (move_10m >= MIN_MOVE_PCT and uptrend and vol_ratio >= 1.0
                and yes_price < MAX_POLY_PRICE):
            # Estimate true prob based on move magnitude + trend
            true_prob = min(0.95, 0.55 + abs(move_10m) * 0.15 + (0.05 if higher_highs and higher_lows else 0))
            edge = true_prob - yes_price

            if true_prob >= MIN_TRUE_PROB and edge >= 0.05:
                candidates.append({
                    "market": market,
                    "question": question,
                    "coin": coin_name,
                    "symbol": symbol,
                    "side": "UP",
                    "token_id": token_ids[0],
                    "outcome": "Yes",
                    "poly_price": yes_price,
                    "true_prob": true_prob,
                    "edge": edge,
                    "move_10m": move_10m,
                    "move_15m": move_15m,
                    "vol_ratio": vol_ratio,
                    "buy_pressure": buy_pressure,
                    "trend": "uptrend",
                    "higher_highs": higher_highs,
                    "higher_lows": higher_lows,
                    "binance_price": current_price,
                })
                added = True
            else:
                skip_msg = f"UP edge={edge:+.1%} true_prob={true_prob:.2f} (need edge>=5%, prob>={MIN_TRUE_PROB})"
        elif move_10m >= MIN_MOVE_PCT and uptrend:
            if yes_price >= MAX_POLY_PRICE:
                skip_msg = f"UP yes={yes_price:.2f} too rich (need <{MAX_POLY_PRICE})"
            elif vol_ratio < 1.0:
                skip_msg = f"UP vol={vol_ratio:.2f}x too low"

        # === DOWN candidate ===
        # Hard rule: never DOWN on SOL/XRP when uptrend
        if symbol in ("SOLUSDT", "XRPUSDT") and uptrend:
            if first_seen and not added:
                if skip_msg:
                    print(f"  [BINANCE-LAG] {coin_name} {skip_msg}")
                else:
                    print(f"  [BINANCE-LAG] {coin_name} DOWN blocked (SOL/XRP + uptrend)")
            continue

        if (move_10m <= -MIN_MOVE_PCT and downtrend and vol_ratio >= 1.0
                and no_price < MAX_POLY_PRICE):
            true_prob = min(0.95, 0.55 + abs(move_10m) * 0.15 + (0.05 if lower_highs and lower_lows else 0))
            edge = true_prob - no_price

            if true_prob >= MIN_TRUE_PROB and edge >= 0.05:
                candidates.append({
                    "market": market,
                    "question": question,
                    "coin": coin_name,
                    "symbol": symbol,
                    "side": "DOWN",
                    "token_id": token_ids[1],
                    "outcome": "No",
                    "poly_price": no_price,
                    "true_prob": true_prob,
                    "edge": edge,
                    "move_10m": move_10m,
                    "move_15m": move_15m,
                    "vol_ratio": vol_ratio,
                    "buy_pressure": buy_pressure,
                    "trend": "downtrend",
                    "higher_highs": higher_highs,
                    "higher_lows": higher_lows,
                    "binance_price": current_price,
                })
                added = True
            else:
                skip_msg = f"DOWN edge={edge:+.1%} true_prob={true_prob:.2f} (need edge>=5%, prob>={MIN_TRUE_PROB})"
        elif move_10m <= -MIN_MOVE_PCT and downtrend:
            if no_price >= MAX_POLY_PRICE:
                skip_msg = f"DOWN no={no_price:.2f} too rich (need <{MAX_POLY_PRICE})"
            elif vol_ratio < 1.0:
                skip_msg = f"DOWN vol={vol_ratio:.2f}x too low"

        if first_seen and not added and skip_msg:
            print(f"  [BINANCE-LAG] {coin_name} {skip_msg}")

    # Sort by edge descending
    candidates.sort(key=lambda x: x["edge"], reverse=True)
    return candidates


def claude_gate(candidate: dict) -> dict | None:
    """Send candidate to Claude Sonnet for approval.

    Returns parsed JSON result or None on failure.
    Only approves if ok_to_trade=true AND confidence >= medium.
    """
    try:
        import anthropic
        client = anthropic.Anthropic()

        payload = json.dumps({
            "coin": candidate["coin"],
            "side": candidate["side"],
            "polymarket_price": candidate["poly_price"],
            "edge": f"{candidate['edge']:.1%}",
            "binance_price": candidate["binance_price"],
            "move_10m": f"{candidate['move_10m']:+.2f}%",
            "move_15m": f"{candidate['move_15m']:+.2f}%",
            "volume_ratio": round(candidate["vol_ratio"], 2),
            "buy_pressure": round(candidate["buy_pressure"], 2),
            "trend": candidate["trend"],
            "higher_highs": candidate["higher_highs"],
            "higher_lows": candidate["higher_lows"],
            "market": candidate["question"][:80],
        })

        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=100,
            system=CLAUDE_GATE_SYSTEM,
            messages=[{"role": "user", "content": payload}],
        )
        text = resp.content[0].text.strip()

        try:
            result = json.loads(text)
            return result
        except json.JSONDecodeError:
            log.error("[BINANCE-LAG] Claude returned invalid JSON: %s", text[:80])
            return None

    except Exception as e:
        log.error("[BINANCE-LAG] Claude gate failed: %s", e)
        return None
