"""Test order placement with different signature types."""

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
import config
from core import market_data

print("Testing order placement...")
print()

markets = market_data.get_active_markets(limit=5)
if not markets:
    print("No markets found!")
    exit()

m = markets[0]
token_id = m['token_ids'][0]
print(f"Market: {m['question']}")
print(f"Token: {token_id[:20]}...")
print()

for sig_type in [0, 1, 2]:
    print(f"--- Signature type {sig_type} ---")
    try:
        client = ClobClient(
            host=config.CLOB_HOST,
            key=config.POLYMARKET_PRIVATE_KEY,
            chain_id=config.CHAIN_ID,
            signature_type=sig_type,
            funder=config.POLYMARKET_FUNDER_ADDRESS,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

        order_args = OrderArgs(
            token_id=token_id,
            price=0.02,
            size=1.0,
            side='BUY',
        )
        signed = client.create_order(order_args)
        resp = client.post_order(signed, orderType=OrderType.GTC)
        print(f"SUCCESS: {resp}")
        oid = resp.get('orderID') or resp.get('id')
        if oid:
            client.cancel(oid)
            print(f"Cancelled test order {oid}")
    except Exception as e:
        print(f"FAILED: {e}")
    print()
