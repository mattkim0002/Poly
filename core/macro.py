"""Macro bias snapshot — cached global crypto outlook refreshed every 3 hours.

Synthesizes BTC/ETH/SOL higher-timeframe trends + top crypto news headlines
into a single {"bias", "confidence", "reasoning"} dict that the trading
strategies use as a SOFT gate (only blocks directional trades when confidence
is "high" and bias strongly opposes the trade direction).

Design notes:
- One Claude Sonnet call per refresh (cheap) vs per-trade news fetches (wasteful).
- Never raises — on any failure, returns a neutral bias so the bot keeps trading.
- Thread-safe via module-level dict; single writer (background thread).
"""

import json
import threading
import time

import config
from core import crypto_predictor, news
from utils.logger import log


MACRO_BIAS_SYSTEM = """You analyze the crypto macro environment for a Polymarket 5-minute binary trading bot.

You receive:
- BTC/ETH/SOL 1h and 4h trend directions + strengths
- Recent headlines for Bitcoin, Ethereum, and crypto regulation

Your job: return a SINGLE JSON object describing the overall bias. Be conservative — say "neutral" unless evidence clearly points one way.

Reply with ONLY this JSON, nothing else:

{"bias": "bullish" | "neutral" | "bearish",
 "confidence": "low" | "medium" | "high",
 "btc_leads_alts": true | false,
 "reasoning": "one sentence, 25 words or fewer"}

Rules:
- "high" confidence only when 4h trends AND headlines AND 1h trends all agree.
- "btc_leads_alts" = true if BTC direction looks likely to drag ETH/SOL with it in the next few hours.
- When in doubt, pick "neutral" with "low" confidence.
"""


_NEUTRAL: dict = {
    "bias": "neutral",
    "confidence": "low",
    "btc_leads_alts": True,
    "reasoning": "no data yet",
    "updated_at": 0.0,
    "expires_at": 0.0,
}

_macro_state: dict = dict(_NEUTRAL)
_lock = threading.Lock()


def get_macro_bias() -> dict:
    """Return the cached macro bias (always safe to call)."""
    with _lock:
        return dict(_macro_state)


def refresh_macro_bias() -> dict:
    """Fetch fresh macro data, call Claude Sonnet, update the cache, return the new state.

    Never raises: on any failure, caches a neutral state and returns it.
    """
    try:
        # 1. Gather BTC/ETH/SOL higher-timeframe trends
        trends = {}
        for label, symbol in [("BTC", "BTCUSDT"), ("ETH", "ETHUSDT"), ("SOL", "SOLUSDT")]:
            try:
                t = crypto_predictor.get_higher_timeframe_trend(symbol)
                trends[label] = {
                    "direction": t.get("direction", "sideways"),
                    "strength": round(float(t.get("strength", 0.0)), 3),
                }
            except Exception as e:
                log.debug("macro: HTF fetch failed for %s: %s", symbol, e)
                trends[label] = {"direction": "sideways", "strength": 0.0}

        # 2. Gather headlines (best-effort, never blocks)
        headlines = {}
        for topic in ["Bitcoin price", "Ethereum price", "crypto regulation"]:
            try:
                headlines[topic] = news.fetch_headlines(topic, max_results=4) or []
            except Exception:
                headlines[topic] = []

        # 3. Claude Sonnet synthesis
        import anthropic
        client = anthropic.Anthropic()
        payload = json.dumps({"trends": trends, "headlines": headlines})

        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=500,
            system=MACRO_BIAS_SYSTEM,
            messages=[{"role": "user", "content": payload}],
        )
        text = response.content[0].text.strip()

        # Strip any ```json fences and extract the JSON object if Claude wrapped it
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            log.warning("macro: Claude returned non-JSON: %s", text[:300])
            parsed = {}

        now = time.time()
        new_state = {
            "bias": parsed.get("bias", "neutral"),
            "confidence": parsed.get("confidence", "low"),
            "btc_leads_alts": bool(parsed.get("btc_leads_alts", True)),
            "reasoning": parsed.get("reasoning", "parse failed"),
            "trends": trends,
            "updated_at": now,
            "expires_at": now + config.MACRO_REFRESH_SEC,
        }

        with _lock:
            _macro_state.clear()
            _macro_state.update(new_state)

        return dict(new_state)

    except Exception as e:
        log.warning("macro: refresh failed (%s) — keeping neutral bias", e)
        now = time.time()
        fallback = dict(_NEUTRAL)
        fallback["updated_at"] = now
        fallback["expires_at"] = now + 600  # retry in 10 min on failure
        with _lock:
            _macro_state.clear()
            _macro_state.update(fallback)
        return fallback
