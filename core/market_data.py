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

    # CRYPTO ONLY: Filter for up/down markets — we only trade crypto
    filtered = [m for m in filtered if "up or down" in (m.get("question", "") or "").lower()]

    # Filter out 5-min markets early — only keep 15-min or longer
    import re
    def _market_duration_min(question: str) -> int:
        """Return duration in minutes from title time range. Returns 999 if no range (daily)."""
        m = re.search(r'(\d{1,2}):?(\d{2})?(AM|PM)-(\d{1,2}):?(\d{2})?(AM|PM)', question)
        if not m:
            return 999
        h1, mi1, ap1, h2, mi2, ap2 = m.groups()
        h1, mi1, h2, mi2 = int(h1), int(mi1 or 0), int(h2), int(mi2 or 0)
        if ap1 == 'PM' and h1 != 12: h1 += 12
        if ap1 == 'AM' and h1 == 12: h1 = 0
        if ap2 == 'PM' and h2 != 12: h2 += 12
        if ap2 == 'AM' and h2 == 12: h2 = 0
        t1 = h1 * 60 + mi1
        t2 = h2 * 60 + mi2
        if t2 < t1: t2 += 24 * 60
        return t2 - t1

    filtered = [m for m in filtered if _market_duration_min(m.get("question", "")) >= 15]

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
