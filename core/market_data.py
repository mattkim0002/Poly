"""Gamma API client — fetch and search active Polymarket markets."""

import httpx

import config
from utils.logger import log


def get_active_markets(limit: int = 100) -> list[dict]:
    """Fetch active, tradeable markets from the Gamma API.

    Filters for markets with orderbook enabled, sufficient volume and liquidity.
    """
    params = {
        "active": "true",
        "closed": "false",
        "limit": limit,
        "order": "volume",
        "ascending": "false",
    }

    try:
        resp = httpx.get(f"{config.GAMMA_HOST}/markets", params=params, timeout=15)
        resp.raise_for_status()
        markets = resp.json()
    except (httpx.HTTPError, Exception) as e:
        log.error("Failed to fetch markets from Gamma API: %s", e)
        return []

    # Filter for tradeable markets with enough activity
    filtered = []
    for m in markets:
        if not m.get("enableOrderBook"):
            continue

        volume = float(m.get("volume", 0) or 0)
        liquidity = float(m.get("liquidity", 0) or 0)

        if volume < config.MIN_VOLUME:
            continue
        if liquidity < config.MIN_LIQUIDITY:
            continue

        # Skip markets that resolve too far out (more than MAX_DAYS_TO_RESOLUTION)
        end_date = m.get("endDate") or m.get("end_date_iso")
        if end_date and hasattr(config, "MAX_DAYS_TO_RESOLUTION"):
            from datetime import datetime, timezone
            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                days_out = (end_dt - datetime.now(timezone.utc)).days
                if days_out > config.MAX_DAYS_TO_RESOLUTION:
                    continue
            except (ValueError, TypeError):
                pass

        # Parse token IDs from clobTokenIds
        clob_token_ids = m.get("clobTokenIds")
        if not clob_token_ids:
            continue

        # clobTokenIds is a JSON string like '["token1", "token2"]'
        if isinstance(clob_token_ids, str):
            import json
            try:
                clob_token_ids = json.loads(clob_token_ids)
            except (json.JSONDecodeError, TypeError):
                continue

        outcomes = m.get("outcomes")
        if isinstance(outcomes, str):
            import json
            try:
                outcomes = json.loads(outcomes)
            except (json.JSONDecodeError, TypeError):
                outcomes = ["Yes", "No"]

        outcome_prices = m.get("outcomePrices")
        if isinstance(outcome_prices, str):
            import json
            try:
                outcome_prices = [float(p) for p in json.loads(outcome_prices)]
            except (json.JSONDecodeError, TypeError, ValueError):
                outcome_prices = []

        filtered.append({
            "id": m.get("id"),
            "question": m.get("question", ""),
            "outcomes": outcomes,
            "outcome_prices": outcome_prices,
            "token_ids": clob_token_ids,
            "volume": volume,
            "liquidity": liquidity,
        })

    log.info("Fetched %d markets, %d pass filters", len(markets), len(filtered))
    return filtered


def search_markets(query: str, limit: int = 20) -> list[dict]:
    """Search markets by keyword."""
    try:
        resp = httpx.get(
            f"{config.GAMMA_HOST}/markets",
            params={"active": "true", "closed": "false", "limit": limit},
            timeout=15,
        )
        resp.raise_for_status()
        markets = resp.json()
    except (httpx.HTTPError, Exception) as e:
        log.error("Market search failed: %s", e)
        return []

    query_lower = query.lower()
    return [m for m in markets if query_lower in (m.get("question", "") or "").lower()]
