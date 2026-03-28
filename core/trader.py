"""CLOB client wrapper — order execution, positions, balance."""

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, BalanceAllowanceParams, OrderArgs, OrderType

import config
from utils.logger import log

SIGNATURE_TYPE = 2  # Gnosis Safe proxy wallet

_client: ClobClient | None = None


def get_client() -> ClobClient:
    """Initialize or return the singleton CLOB client."""
    global _client
    if _client is not None:
        return _client

    _client = ClobClient(
        host=config.CLOB_HOST,
        key=config.POLYMARKET_PRIVATE_KEY,
        chain_id=config.CHAIN_ID,
        signature_type=SIGNATURE_TYPE,
        funder=config.POLYMARKET_FUNDER_ADDRESS,
    )

    # Derive and set API credentials
    try:
        creds = _client.create_or_derive_api_creds()
        _client.set_api_creds(creds)
        log.info("CLOB client initialized, API creds set")
    except Exception as e:
        log.error("Failed to derive API creds: %s", e)
        raise

    return _client


def get_balance() -> float:
    """Get available USDC balance (converted from wei)."""
    client = get_client()
    try:
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=SIGNATURE_TYPE,
        )
        result = client.get_balance_allowance(params)
        raw = float(result.get("balance", 0))
        return raw / 1e6
    except Exception as e:
        log.error("Failed to get balance: %s", e)
        return 0.0


def get_price(token_id: str, side: str = "BUY") -> float:
    """Get best bid or ask price for a token."""
    client = get_client()
    try:
        price = client.get_price(token_id=token_id, side=side)
        return float(price)
    except Exception as e:
        log.error("Failed to get price for %s: %s", token_id, e)
        return 0.0


def get_midpoint(token_id: str) -> float:
    """Get midpoint price for a token."""
    client = get_client()
    try:
        mid = client.get_midpoint(token_id=token_id)
        return float(mid)
    except Exception as e:
        log.error("Failed to get midpoint for %s: %s", token_id, e)
        return 0.0


def get_orderbook(token_id: str) -> dict:
    """Get full orderbook for a token."""
    client = get_client()
    try:
        return client.get_order_book(token_id)
    except Exception as e:
        log.error("Failed to get orderbook for %s: %s", token_id, e)
        return {"bids": [], "asks": []}


def get_positions() -> list[dict]:
    """Get current open positions via data API."""
    try:
        import httpx
        addr = config.POLYMARKET_FUNDER_ADDRESS
        resp = httpx.get(
            f"https://data-api.polymarket.com/positions",
            params={"user": addr},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.error("Failed to get positions: %s", e)
        return []


def get_open_orders() -> list[dict]:
    """Get all open orders."""
    client = get_client()
    try:
        orders = client.get_orders()
        if isinstance(orders, list):
            return orders
        return []
    except Exception as e:
        log.error("Failed to get open orders: %s", e)
        return []


def place_limit_order(token_id: str, price: float, size: float, side: str) -> str | None:
    """Place a GTC limit order. Returns order_id or None on failure.

    Args:
        token_id: The outcome token to trade
        price: Limit price (0.01 - 0.99)
        size: Number of shares
        side: 'BUY' or 'SELL'
    """
    if config.DRY_RUN:
        log.info("[DRY RUN] Would place %s %s %.2f shares @ $%.2f", side, token_id[:12], size, price)
        return "dry_run_order"

    client = get_client()
    try:
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
            order_type=OrderType.GTC,
        )
        resp = client.create_and_post_order(order_args)
        order_id = resp.get("orderID") or resp.get("id")
        log.info("Order placed: %s %s %.2f @ $%.2f -> %s", side, token_id[:12], size, price, order_id)
        return order_id
    except Exception as e:
        log.error("Failed to place order: %s", e)
        return None


def cancel_order(order_id: str) -> bool:
    """Cancel an open order."""
    if config.DRY_RUN:
        log.info("[DRY RUN] Would cancel order %s", order_id)
        return True

    client = get_client()
    try:
        client.cancel(order_id)
        log.info("Cancelled order %s", order_id)
        return True
    except Exception as e:
        log.error("Failed to cancel order %s: %s", order_id, e)
        return False
