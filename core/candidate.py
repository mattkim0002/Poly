"""Normalized market candidate — canonical scanner output shape.

Candidate is a drop-in replacement for raw market dicts:
  - New code uses attribute access: c.question, c.yes_token_id
  - Old code uses dict access: c["question"], c.get("token_ids")
    (delegates to raw_source for backward compatibility)
  - .to_dict() returns the raw_source dict unchanged

Strategies progressively migrate from dict access to attribute access.
Until a strategy is migrated, dict-compat keeps it working with zero changes.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Candidate:
    market_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    pair_cost: float
    minutes_to_expiry: float
    yes_bid: float = 0.0
    yes_ask: float = 0.0
    no_bid: float = 0.0
    no_ask: float = 0.0
    liquidity: float = 0.0
    volume: float = 0.0
    scanner_score: float = 0.0
    end_date: str = ""
    raw_source: dict = field(default_factory=dict, repr=False)

    # ── dict-compat adapter ──────────────────────────────

    def __getitem__(self, key):
        if self.raw_source and key in self.raw_source:
            return self.raw_source[key]
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def get(self, key, default=None):
        if self.raw_source and key in self.raw_source:
            return self.raw_source[key]
        return getattr(self, key, default)

    def __contains__(self, key):
        if self.raw_source and key in self.raw_source:
            return True
        return hasattr(self, key)

    def to_dict(self) -> dict:
        """Return the raw_source dict. Compatibility shim for strategies
        that haven't been migrated to Candidate attribute access yet."""
        return self.raw_source

    # ── factory ──────────────────────────────────────────

    @classmethod
    def from_api_dict(cls, market: dict):
        """Build a Candidate from a raw Polymarket/Gamma API market dict.

        Returns None if the market lacks the minimum viable shape
        (two outcomes with token ids).
        """
        token_ids = market.get("token_ids", [])
        if len(token_ids) < 2:
            return None

        outcome_prices = market.get("outcome_prices", []) or []
        try:
            yes_price = float(outcome_prices[0]) if len(outcome_prices) >= 1 else 0.0
            no_price = float(outcome_prices[1]) if len(outcome_prices) >= 2 else max(0.0, 1.0 - yes_price)
        except (TypeError, ValueError):
            yes_price, no_price = 0.0, 0.0

        end_date = market.get("end_date") or market.get("endDate") or ""
        minutes_left = market.get("time_to_expiry_minutes")
        if minutes_left is None:
            minutes_left = 999.0
            if end_date:
                try:
                    end_dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
                    minutes_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 60
                except (ValueError, TypeError):
                    pass

        try:
            volume = float(market.get("volume_24h") or market.get("volume", 0) or 0)
        except (TypeError, ValueError):
            volume = 0.0
        try:
            liquidity = float(market.get("liquidity", 0) or 0)
        except (TypeError, ValueError):
            liquidity = 0.0

        return cls(
            market_id=market.get("market_id") or market.get("id", ""),
            question=market.get("question", ""),
            yes_token_id=token_ids[0],
            no_token_id=token_ids[1],
            yes_price=yes_price,
            no_price=no_price,
            pair_cost=yes_price + no_price,
            minutes_to_expiry=minutes_left,
            volume=volume,
            liquidity=liquidity,
            scanner_score=float(market.get("scanner_score", 0) or 0),
            end_date=end_date,
            raw_source=market,
        )
