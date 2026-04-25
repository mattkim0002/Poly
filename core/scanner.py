"""Expanded market scanner — fetches a much larger Polymarket universe.

Pipeline:
  scan_universe() → list[Candidate] (canonical scanner output)

Candidate objects support dict-style access (c["key"], c.get("key"))
for backward compatibility with strategy code that hasn't migrated yet.
"""

import json
import math
from datetime import datetime, timezone

import httpx

import config
from core.candidate import Candidate
from utils.logger import log


def _fetch_page(offset: int) -> list[dict]:
    params = {
        "active": "true" if config.SCAN_ACTIVE_ONLY else None,
        "closed": "false",
        "limit": config.SCAN_LIMIT_PER_PAGE,
        "offset": offset,
        "order": config.SCAN_SORT_BY,
        "ascending": "false",
    }
    params = {k: v for k, v in params.items() if v is not None}
    try:
        resp = httpx.get(
            f"{config.GAMMA_HOST}/markets",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json() or []
    except Exception as e:
        log.warning("scanner: page offset=%d failed: %s", offset, e)
        return []


def _parse_prices(market: dict) -> tuple[float, float]:
    """Return (yes_price, no_price) or (0, 0) if unavailable."""
    op = market.get("outcomePrices")
    if isinstance(op, str):
        try:
            op = [float(p) for p in json.loads(op)]
        except (json.JSONDecodeError, ValueError):
            op = []
    if isinstance(op, list) and len(op) >= 2:
        return float(op[0]), float(op[1])
    return 0.0, 0.0


def _minutes_to_expiry(market: dict) -> float | None:
    end = market.get("endDate") or market.get("end_date_iso")
    if not end:
        return None
    try:
        end_dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        delta = (end_dt - datetime.now(timezone.utc)).total_seconds() / 60
        return round(delta, 1)
    except (ValueError, TypeError):
        return None


def _scanner_score(vol24: float, liquidity: float, minutes_left: float | None) -> float:
    """Score 0-100. Higher = better candidate for arb/endgame."""
    vol_score = min(40.0, math.log1p(vol24) * 3)
    liq_score = min(30.0, math.log1p(liquidity) * 2.5)
    # Endgame bonus: expiring in 60-480 min
    time_score = 0.0
    if minutes_left is not None and minutes_left > 0:
        if minutes_left <= 480:
            time_score = 30.0 * max(0, 1 - minutes_left / 480)
    return round(vol_score + liq_score + time_score, 2)


def _parse_token_ids(market: dict) -> list[str]:
    ids = market.get("clobTokenIds")
    if isinstance(ids, str):
        try:
            return json.loads(ids)
        except json.JSONDecodeError:
            return []
    return ids or []


def scan_universe(debug: bool = False) -> list[Candidate]:
    """Fetch and score a large universe of Polymarket markets.

    Returns list of Candidate objects sorted by scanner_score desc.
    Each Candidate supports dict-style access via raw_source for compat.
    """
    raw_total = 0
    candidates: list[Candidate] = []
    seen_ids: set = set()

    for page in range(config.SCAN_MAX_PAGES):
        offset = page * config.SCAN_LIMIT_PER_PAGE
        batch = _fetch_page(offset)
        if not batch:
            break
        raw_total += len(batch)

        for m in batch:
            mid = m.get("id") or m.get("conditionId") or ""
            if mid in seen_ids:
                continue
            seen_ids.add(mid)

            if not m.get("enableOrderBook"):
                continue

            question = (m.get("question") or "").strip()
            q_lower = question.lower()

            # Tag filters
            tags = [t.lower() for t in (m.get("tags") or m.get("category") or [])]
            if isinstance(tags, str):
                tags = [tags.lower()]
            if config.SCAN_ALLOWED_TAGS:
                if not any(t in tags for t in config.SCAN_ALLOWED_TAGS):
                    continue
            if config.SCAN_EXCLUDED_TAGS:
                if any(t in tags for t in config.SCAN_EXCLUDED_TAGS):
                    continue

            # Keyword filters
            if config.SCAN_KEYWORD_INCLUDE:
                if not any(kw.lower() in q_lower for kw in config.SCAN_KEYWORD_INCLUDE):
                    continue
            if config.SCAN_KEYWORD_EXCLUDE:
                if any(kw.lower() in q_lower for kw in config.SCAN_KEYWORD_EXCLUDE):
                    continue

            # Volume / liquidity
            vol24 = float(m.get("volume24hr") or m.get("volume", 0) or 0)
            liquidity = float(m.get("liquidity", 0) or 0)
            if vol24 < config.MIN_24H_VOLUME:
                continue
            if liquidity < config.SCAN_MIN_LIQUIDITY:
                continue

            token_ids = _parse_token_ids(m)
            if not token_ids:
                continue

            minutes_left = _minutes_to_expiry(m)
            if minutes_left is not None and minutes_left < 0:
                continue

            yes_price, no_price = _parse_prices(m)
            score = _scanner_score(vol24, liquidity, minutes_left)

            tag_label = (tags[0] if tags else "")
            end_date = m.get("endDate") or m.get("end_date_iso") or ""

            raw = {
                "market_id": mid,
                "id": mid,
                "question": question,
                "tag": tag_label,
                "end_date": end_date,
                "volume_24h": vol24,
                "liquidity": liquidity,
                "yes_price": yes_price,
                "no_price": no_price,
                "time_to_expiry_minutes": minutes_left,
                "scanner_score": score,
                "outcomes": json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else (m.get("outcomes") or ["Yes", "No"]),
                "outcome_prices": [yes_price, no_price],
                "token_ids": token_ids,
                "volume": float(m.get("volume", 0) or 0),
            }

            candidates.append(Candidate(
                market_id=mid,
                question=question,
                yes_token_id=token_ids[0],
                no_token_id=token_ids[1] if len(token_ids) > 1 else "",
                yes_price=yes_price,
                no_price=no_price,
                pair_cost=yes_price + no_price,
                minutes_to_expiry=minutes_left if minutes_left is not None else 999.0,
                liquidity=liquidity,
                volume=vol24,
                scanner_score=score,
                end_date=end_date,
                raw_source=raw,
            ))

    candidates.sort(key=lambda x: x.scanner_score, reverse=True)

    if debug:
        print(f"\n[SCANNER] Raw fetched: {raw_total} | After filters: {len(candidates)}")
        print(f"[SCANNER] Top 20 by score:")
        for c in candidates[:20]:
            exp = f"{c['time_to_expiry_minutes']:.0f}m" if c["time_to_expiry_minutes"] else "?"
            print(
                f"  score={c['scanner_score']:5.1f} | vol24=${c['volume_24h']:>8,.0f} | "
                f"liq=${c['liquidity']:>7,.0f} | expires={exp:>6} | {c['question'][:55]}"
            )

    log.info("Scanner: %d raw → %d candidates (pages=%d)",
             raw_total, len(candidates), min(config.SCAN_MAX_PAGES, math.ceil(raw_total / max(config.SCAN_LIMIT_PER_PAGE, 1))))
    return candidates
