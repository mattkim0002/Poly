"""Moondev client — local shim.

Moondev's actual API surface wasn't reachable with the key we have, so this
file implements the same public functions using data we already pull from
Binance (prices + orderbook) and Anthropic (AI). Same signatures as the
earlier HTTP client — callers don't need to change.

Public surface:
  - get_prices()
  - get_price(symbol)
  - get_orderbook(symbol)
  - call_moondev_ai(messages, max_tokens=400)
"""

import os
from typing import Any

import httpx


BINANCE_BASE = "https://data-api.binance.vision"
DEFAULT_TIMEOUT = 5.0


class MoondevConfigError(RuntimeError):
    """Raised when a required key is missing (e.g. Anthropic for AI)."""


class MoondevAPIError(RuntimeError):
    """Raised when an upstream call fails."""


def _binance_symbol(symbol: str) -> str:
    """Normalize: 'btc', 'BTC', 'BTCUSDT' → 'BTCUSDT'."""
    s = symbol.strip().upper()
    return s if s.endswith("USDT") else f"{s}USDT"


def get_prices() -> list[dict]:
    """Return live prices for a basket of tracked coins from Binance."""
    coins = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "LINK"]
    out = []
    try:
        resp = httpx.get(f"{BINANCE_BASE}/api/v3/ticker/price", timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        by_sym = {row["symbol"]: float(row["price"]) for row in resp.json()}
    except httpx.HTTPError as e:
        raise MoondevAPIError(f"binance prices failed: {e}") from e

    for c in coins:
        sym = _binance_symbol(c)
        if sym in by_sym:
            out.append({"symbol": c, "price": by_sym[sym]})
    return out


def get_price(symbol: str) -> dict:
    """Single-symbol price from Binance."""
    if not symbol:
        raise ValueError("symbol required")
    sym = _binance_symbol(symbol)
    try:
        resp = httpx.get(
            f"{BINANCE_BASE}/api/v3/ticker/price",
            params={"symbol": sym}, timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        raise MoondevAPIError(f"binance price failed: {e}") from e
    return {"symbol": symbol.upper(), "price": float(data["price"])}


def get_orderbook(symbol: str, depth: int = 20) -> dict:
    """Binance orderbook for a symbol."""
    if not symbol:
        raise ValueError("symbol required")
    sym = _binance_symbol(symbol)
    try:
        resp = httpx.get(
            f"{BINANCE_BASE}/api/v3/depth",
            params={"symbol": sym, "limit": depth}, timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        raise MoondevAPIError(f"binance depth failed: {e}") from e

    bids = [[float(p), float(q)] for p, q in data.get("bids", [])]
    asks = [[float(p), float(q)] for p, q in data.get("asks", [])]
    bid_volume = sum(q for _, q in bids)
    ask_volume = sum(q for _, q in asks)
    return {
        "symbol": symbol.upper(),
        "bids": bids[:10],
        "asks": asks[:10],
        "bid_volume": bid_volume,
        "ask_volume": ask_volume,
    }


def call_moondev_ai(messages: list[dict], max_tokens: int = 400) -> Any:
    """Route 'AI' calls to Anthropic Claude.

    Returns a dict shaped like OpenAI-style chat completions so callers can
    read `resp["choices"][0]["message"]["content"]`.
    """
    if not messages:
        raise ValueError("messages list cannot be empty")
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        raise MoondevConfigError("ANTHROPIC_API_KEY not set")

    try:
        import anthropic
        import config as _cfg
    except ImportError as e:
        raise MoondevAPIError(f"anthropic SDK missing: {e}") from e

    # Pull out an optional system message if the caller supplied one
    system = ""
    convo: list[dict] = []
    for m in messages:
        role = m.get("role", "user")
        if role == "system":
            system = m.get("content", "")
        else:
            convo.append({"role": role, "content": m.get("content", "")})

    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=getattr(_cfg, "CLAUDE_MODEL", "claude-sonnet-4-6"),
            max_tokens=max_tokens,
            system=system or "You are a crypto trading assistant.",
            messages=convo or [{"role": "user", "content": ""}],
        )
        text = resp.content[0].text if resp.content else ""
    except Exception as e:
        raise MoondevAPIError(f"anthropic call failed: {e}") from e

    return {"choices": [{"message": {"role": "assistant", "content": text}}]}
