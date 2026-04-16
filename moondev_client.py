"""Moondev API client — thin wrapper around the Moondev data + AI endpoints.

Auth:
  - Data endpoints use `X-API-Key: <MOONDEV_API_KEY>` header.
  - AI endpoint uses `Authorization: Bearer <MOONDEV_API_KEY>` header.

The key is read from the `MOONDEV_API_KEY` environment variable. No secrets
live in the repo.

Public surface:
  - get_prices()
  - get_price(symbol)
  - get_orderbook(symbol)
  - call_moondev_ai(messages, max_tokens=400)

Errors:
  - MoondevConfigError: API key missing.
  - MoondevAPIError: HTTP error or malformed response.
"""

import os
from typing import Any

import httpx


DATA_BASE_URL = os.getenv("MOONDEV_BASE_URL", "https://api.moondev.com")
AI_URL = f"{DATA_BASE_URL}/api/ai/v1/chat/completions"
DEFAULT_TIMEOUT = 8.0


class MoondevConfigError(RuntimeError):
    """Raised when the Moondev API key is missing."""


class MoondevAPIError(RuntimeError):
    """Raised when a Moondev API call fails."""


def _api_key() -> str:
    key = os.getenv("MOONDEV_API_KEY", "").strip()
    if not key:
        raise MoondevConfigError(
            "MOONDEV_API_KEY not set — add it to your .env or shell environment"
        )
    return key


def _data_headers() -> dict[str, str]:
    return {"X-API-Key": _api_key(), "Accept": "application/json"}


def _ai_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _get(path: str, params: dict | None = None) -> Any:
    url = f"{DATA_BASE_URL}{path}"
    try:
        resp = httpx.get(url, headers=_data_headers(), params=params, timeout=DEFAULT_TIMEOUT)
    except httpx.HTTPError as e:
        raise MoondevAPIError(f"network error calling {path}: {e}") from e
    if resp.status_code != 200:
        raise MoondevAPIError(f"{path} returned HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as e:
        raise MoondevAPIError(f"{path} returned non-JSON: {resp.text[:200]}") from e


def get_prices() -> Any:
    """Return the full price list from Moondev."""
    return _get("/api/data/prices")


def get_price(symbol: str) -> Any:
    """Return the current price for a single symbol."""
    if not symbol:
        raise ValueError("symbol required")
    return _get(f"/api/data/prices/{symbol.upper()}")


def get_orderbook(symbol: str) -> Any:
    """Return the orderbook for a single symbol."""
    if not symbol:
        raise ValueError("symbol required")
    return _get(f"/api/data/orderbook/{symbol.upper()}")


def call_moondev_ai(messages: list[dict], max_tokens: int = 400) -> Any:
    """Call the Moondev AI chat completions endpoint.

    Raises MoondevConfigError if the key is missing, MoondevAPIError on
    HTTP failures. Callers should catch MoondevAPIError if AI access may
    not be enabled on their account.
    """
    if not messages:
        raise ValueError("messages list cannot be empty")

    body = {"messages": messages, "max_tokens": max_tokens}
    try:
        resp = httpx.post(AI_URL, headers=_ai_headers(), json=body, timeout=DEFAULT_TIMEOUT)
    except httpx.HTTPError as e:
        raise MoondevAPIError(f"network error calling AI: {e}") from e
    if resp.status_code != 200:
        raise MoondevAPIError(f"AI returned HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as e:
        raise MoondevAPIError(f"AI returned non-JSON: {resp.text[:200]}") from e
