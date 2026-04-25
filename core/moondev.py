"""Moondev smart-money plugin — feeds signals into the Claude gates.

Thin adapter over `moondev_client`. Never raises — on missing key or API
failure, returns a neutral signal so the bot keeps trading.
"""

import time

import moondev_client
from utils.logger import log

_CACHE_TTL_SEC = 60
_cache: dict[str, tuple[float, dict]] = {}

_NEUTRAL = {
    "smart_money_net_flow": "neutral",
    "comment": "no moondev data",
}


def get_profitable_wallet_signal(market_id: str) -> dict:
    """Return {"smart_money_net_flow": "in"|"out"|"neutral", "comment": str}.

    Always safe — swallows config + API errors.
    """
    if not market_id:
        return dict(_NEUTRAL)

    now = time.time()
    hit = _cache.get(market_id)
    if hit and now - hit[0] < _CACHE_TTL_SEC:
        return dict(hit[1])

    try:
        # We use the price/orderbook endpoints as a crude proxy for now.
        # When Moondev exposes a smart-money endpoint, swap this block for it.
        book = moondev_client.get_orderbook(market_id[:12])
    except moondev_client.MoondevConfigError:
        return dict(_NEUTRAL)
    except moondev_client.MoondevAPIError as e:
        log.debug("moondev: %s", e)
        return dict(_NEUTRAL)
    except Exception as e:
        log.debug("moondev: unexpected %s", e)
        return dict(_NEUTRAL)

    flow = "neutral"
    comment = ""
    if isinstance(book, dict):
        bids = book.get("bid_volume") or book.get("bids_total")
        asks = book.get("ask_volume") or book.get("asks_total")
        try:
            b, a = float(bids or 0), float(asks or 0)
            if b + a > 0:
                ratio = b / (b + a)
                if ratio > 0.60:
                    flow, comment = "in", f"bid/(bid+ask)={ratio:.2f}"
                elif ratio < 0.40:
                    flow, comment = "out", f"bid/(bid+ask)={ratio:.2f}"
        except (TypeError, ValueError):
            pass

    result = {"smart_money_net_flow": flow, "comment": comment or "neutral depth"}
    _cache[market_id] = (now, result)
    return dict(result)
