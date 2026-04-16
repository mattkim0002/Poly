"""Moondev plugin — optional smart-money signal for the Claude gates.

Reads MOONDEV_API_KEY from the environment. If the key is missing or the
HTTP call fails, returns a neutral signal so the bot keeps trading.

The plugin NEVER decides trades on its own — it just ships an extra field
into the Claude gate payload so Claude can weigh it alongside edge, flow,
news, and macro bias.
"""

import os
import time

import httpx

from utils.logger import log

MOONDEV_BASE_URL = os.getenv("MOONDEV_BASE_URL", "https://api.moondev.com")
MOONDEV_TIMEOUT = 4.0
_CACHE_TTL_SEC = 60

_cache: dict[str, tuple[float, dict]] = {}

_NEUTRAL = {
    "smart_money_net_flow": "neutral",
    "comment": "no moondev data",
}


def _api_key() -> str:
    return os.getenv("MOONDEV_API_KEY", "").strip()


def get_profitable_wallet_signal(market_id: str) -> dict:
    """Fetch smart-money activity for a Polymarket market.

    Returns a dict like:
        {"smart_money_net_flow": "in" | "out" | "neutral",
         "comment": "short explanation"}

    Always returns (never raises). Neutral = no data / not configured.
    """
    if not market_id:
        return dict(_NEUTRAL)

    key = _api_key()
    if not key:
        return dict(_NEUTRAL)

    now = time.time()
    hit = _cache.get(market_id)
    if hit and now - hit[0] < _CACHE_TTL_SEC:
        return dict(hit[1])

    try:
        resp = httpx.get(
            f"{MOONDEV_BASE_URL}/v1/polymarket/smart-money",
            params={"market_id": market_id},
            headers={"Authorization": f"Bearer {key}"},
            timeout=MOONDEV_TIMEOUT,
        )
        if resp.status_code != 200:
            log.debug("moondev: HTTP %s for %s", resp.status_code, market_id[:16])
            return dict(_NEUTRAL)

        data = resp.json()
        flow = str(data.get("net_flow", "neutral")).lower()
        if flow not in ("in", "out", "neutral"):
            flow = "neutral"
        result = {
            "smart_money_net_flow": flow,
            "comment": str(data.get("comment", ""))[:160],
        }
        _cache[market_id] = (now, result)
        return dict(result)

    except Exception as e:
        log.debug("moondev: fetch failed (%s)", e)
        return dict(_NEUTRAL)
