"""Binance-lag strategy — Binance leads, Polymarket lags.

Detects when Binance price clearly implies UP or DOWN but Polymarket
odds haven't caught up yet. Uses Claude Sonnet as a hard veto gate.

Only trades BTC, SOL, XRP on 5-minute "Up or Down" markets.
"""

import json
from core import crypto_predictor, news, trader
from utils.logger import log

# Coins this strategy is allowed to trade
SUPPORTED_COINS = {
    "bitcoin": "BTCUSDT",
    "btc": "BTCUSDT",
    "solana": "SOLUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
    "ethereum": "ETHUSDT",
    "eth": "ETHUSDT",
    "bnb": "BNBUSDT",
    "dogecoin": "DOGEUSDT",
    "doge": "DOGEUSDT",
}

# Minimum move over 10-15 min to consider a trade
MIN_MOVE_PCT = 0.08      # 0.08% move required (very loose — take more trades)
# Polymarket must be cheap relative to our estimate
MAX_POLY_PRICE = 0.78    # Only buy if Poly price < 78c
MIN_TRUE_PROB = 0.55     # Our estimate must be >= 55%
MIN_EDGE = 0.03          # Only need 3% edge

CLAUDE_GATE_SYSTEM = """You are a risk filter for a Polymarket 5-minute crypto trading bot.

The bot detects when Binance price action implies UP or DOWN, but Polymarket
odds haven't caught up. Your job: decide if this lag is real and tradeable.

IMPORTANT: The user wants the bot TAKING TRADES. Default to APPROVING medium
confidence trades. Only reject when the setup is clearly broken.

Inputs you'll see:
- binance_price + coinbase_ref_price = two independent exchange feeds
- cross_exchange_divergence_pct = % difference (oracle check)
- cross_exchange_agrees = true if within 0.3% (sanity confirmation)
- recent_headlines = crypto news sentiment context
- move_10m, volume_ratio, trend, edge

Reply with ONLY this JSON:
{"ok_to_trade": true or false, "reason": "one sentence citing data", "confidence": "low|medium|high"}

Hard rejects (only these):
1. Binance move < 0.05% AND no trend signal → REJECT (pure noise).
2. Trend directly contradicts trade (e.g. clear lower highs + UP trade) → REJECT.
3. cross_exchange_agrees=false AND divergence > 0.5% → REJECT (Binance glitch).
4. News headlines show clearly opposing catalyst (e.g. SEC lawsuit just filed, exchange hack) → REJECT.
5. NEVER approve a DOWN trade on SOL or XRP when Binance shows higher highs AND higher lows.

Approve with MEDIUM confidence when:
- Move >= 0.08% with trend aligned (or neutral), OR
- Volume >= 1.0x average with any positive signal, OR
- Edge >= 3% with no contradiction, OR
- Cross-exchange oracle agrees with Binance

Approve with HIGH confidence when: move >= 0.25%, volume >= 1.5x, trend clearly
aligned, edge >= 6%, coinbase and binance agree, no contradicting news.

When in doubt with reasonable data → APPROVE medium. Do NOT reject over missing
headlines or missing coinbase data.
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

        # Trend: use majority-direction over last 5 candles (not strict monotonic).
        # Strict monotonic was blocking ~95% of real moves.
        highs = [c["high"] for c in candles[-5:]]
        lows = [c["low"] for c in candles[-5:]]
        hh_count = sum(1 for i in range(1, len(highs)) if highs[i] >= highs[i - 1])
        hl_count = sum(1 for i in range(1, len(lows)) if lows[i] >= lows[i - 1])
        lh_count = sum(1 for i in range(1, len(highs)) if highs[i] <= highs[i - 1])
        ll_count = sum(1 for i in range(1, len(lows)) if lows[i] <= lows[i - 1])

        # Strict flags kept for Claude gate context
        higher_highs = hh_count >= 3  # 3/4 = majority
        higher_lows = hl_count >= 3
        lower_highs = lh_count >= 3
        lower_lows = ll_count >= 3

        # Net direction — most reliable when 5 candles are choppy
        net_move = (candles[-1]["close"] - candles[-5]["open"]) / candles[-5]["open"] * 100
        uptrend = (hh_count >= 2 or hl_count >= 2) and net_move > 0
        downtrend = (lh_count >= 2 or ll_count >= 2) and net_move < 0

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

            if true_prob >= MIN_TRUE_PROB and edge >= MIN_EDGE:
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
                skip_msg = f"UP edge={edge:+.1%} true_prob={true_prob:.2f} (need edge>={MIN_EDGE:.0%}, prob>={MIN_TRUE_PROB})"
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

            if true_prob >= MIN_TRUE_PROB and edge >= MIN_EDGE:
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
                skip_msg = f"DOWN edge={edge:+.1%} true_prob={true_prob:.2f} (need edge>={MIN_EDGE:.0%}, prob>={MIN_TRUE_PROB})"
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

    Enriches the payload with:
    - Cross-exchange reference price (Coinbase as oracle) to validate the move
    - Recent crypto news headlines (Google News RSS) for sentiment context

    Returns parsed JSON result or None on failure.
    News/reference-price fetch failures NEVER block — they're informational.
    """
    try:
        import anthropic
        client = anthropic.Anthropic()

        # Cross-exchange "oracle" check — Coinbase reference
        cross = crypto_predictor.get_cross_exchange_agreement(
            candidate["symbol"], candidate["binance_price"]
        )

        # News headlines for this coin (best-effort, never blocks)
        try:
            headlines = news.fetch_headlines(f"{candidate['coin']} crypto price", max_results=3)
        except Exception:
            headlines = []

        payload_dict = {
            "coin": candidate["coin"],
            "side": candidate["side"],
            "polymarket_price": candidate["poly_price"],
            "edge": f"{candidate['edge']:.1%}",
            "binance_price": candidate["binance_price"],
            "coinbase_ref_price": cross["coinbase_price"],
            "cross_exchange_divergence_pct": cross["divergence_pct"],
            "cross_exchange_agrees": cross["agrees"],
            "move_10m": f"{candidate['move_10m']:+.2f}%",
            "move_15m": f"{candidate['move_15m']:+.2f}%",
            "volume_ratio": round(candidate["vol_ratio"], 2),
            "buy_pressure": round(candidate["buy_pressure"], 2),
            "trend": candidate["trend"],
            "higher_highs": candidate["higher_highs"],
            "higher_lows": candidate["higher_lows"],
            "market": candidate["question"][:80],
            "recent_headlines": headlines[:3] if headlines else [],
        }

        payload = json.dumps(payload_dict)

        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=150,
            system=CLAUDE_GATE_SYSTEM,
            messages=[{"role": "user", "content": payload}],
        )
        text = resp.content[0].text.strip()

        # Log what we sent for diagnostics
        cb_str = f"${cross['coinbase_price']}" if cross["coinbase_price"] else "n/a"
        div_str = f"{cross['divergence_pct']:+.2f}%" if cross["divergence_pct"] is not None else "n/a"
        log.info("[BINANCE-LAG] Claude input: coinbase=%s divergence=%s headlines=%d",
                 cb_str, div_str, len(headlines))

        try:
            result = json.loads(text)
            return result
        except json.JSONDecodeError:
            log.error("[BINANCE-LAG] Claude returned invalid JSON: %s", text[:80])
            return None

    except Exception as e:
        log.error("[BINANCE-LAG] Claude gate failed: %s", e)
        return None
