"""Gamma API client — fetch and search active Polymarket markets."""

import httpx

import config
from utils.logger import log


def get_active_markets(limit: int = 100) -> list[dict]:
    """Fetch active, tradeable markets from the Gamma API.

    Filters for markets with orderbook enabled, sufficient volume and liquidity.
    Fetches multiple pages to find enough non-sports markets.
    """
    all_markets = []

    # Fetch multiple batches to get past the sports-dominated top results
    for offset in range(0, 300, 100):
        params = {
            "active": "true",
            "closed": "false",
            "limit": 100,
            "offset": offset,
            "order": "volume",
            "ascending": "false",
        }

        try:
            resp = httpx.get(f"{config.GAMMA_HOST}/markets", params=params, timeout=15)
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            all_markets.extend(batch)
        except (httpx.HTTPError, Exception) as e:
            log.error("Failed to fetch markets from Gamma API: %s", e)
            break

    markets = all_markets

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

        # Skip markets already expired OR resolving too far out
        end_date = m.get("endDate") or m.get("end_date_iso")
        if end_date:
            from datetime import datetime, timezone
            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                minutes_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 60
                # Skip if already expired (past resolution)
                if minutes_left < 0:
                    continue
                # Skip if resolves too far out
                if hasattr(config, "MAX_DAYS_TO_RESOLUTION"):
                    if minutes_left > config.MAX_DAYS_TO_RESOLUTION * 1440:
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
            "end_date": m.get("endDate") or m.get("end_date_iso") or "",
        })

    # Filter to markets we have edge in: crypto up/down + geopolitics (zero fees) + politics/finance
    def _has_edge(m):
        q = (m.get("question", "") or "").lower()
        # Crypto up/down — momentum edge
        if "up or down" in q:
            return True
        # Geopolitics — ZERO taker fees, Claude analysis edge
        geo_terms = {"iran", "russia", "ukraine", "china", "taiwan", "nato",
                     "sanctions", "ceasefire", "missile", "nuclear", "war ",
                     "invasion", "israel", "gaza", "north korea", "military",
                     "troops", "strike", "bombing", "peace", "treaty"}
        if any(t in q for t in geo_terms):
            return True
        # US politics — Claude + news edge (low fees)
        pol_terms = {"trump", "biden", "congress", "senate", "supreme court",
                     "executive order", "impeach", "white house", "tariff"}
        if any(t in q for t in pol_terms):
            return True
        # Finance — Claude + data edge
        fin_terms = {"fed ", "interest rate", "inflation", "gdp",
                     "oil price", "s&p 500", "nasdaq", "recession"}
        if any(t in q for t in fin_terms):
            return True
        return False

    filtered = [m for m in filtered if _has_edge(m)]

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
