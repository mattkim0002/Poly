"""Debug signature issue - verify key/address relationships and test all combos."""

from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
import config

# 1. Verify what address the private key resolves to
acct = Account.from_key(config.POLYMARKET_PRIVATE_KEY)
print(f"Private key resolves to EOA: {acct.address}")
print(f"Config funder address:       {config.POLYMARKET_FUNDER_ADDRESS}")
print()

# 2. Get a token to test with
from core import market_data
markets = market_data.get_active_markets(limit=5)
if not markets:
    print("No markets found!")
    exit()

m = markets[0]
token_id = m['token_ids'][0]
print(f"Test market: {m['question'][:60]}")
print(f"Token: {token_id[:30]}...")
print()

# 3. Test all combinations
combos = [
    (0, acct.address, "sig=0 funder=EOA"),
    (0, config.POLYMARKET_FUNDER_ADDRESS, "sig=0 funder=proxy"),
    (1, acct.address, "sig=1 funder=EOA"),
    (1, config.POLYMARKET_FUNDER_ADDRESS, "sig=1 funder=proxy"),
]

for sig_type, funder, label in combos:
    print(f"--- {label} ---")
    try:
        client = ClobClient(
            host=config.CLOB_HOST,
            key=config.POLYMARKET_PRIVATE_KEY,
            chain_id=config.CHAIN_ID,
            signature_type=sig_type,
            funder=funder,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

        # Get tick_size and neg_risk
        try:
            tick_size = client.get_tick_size(token_id)
        except:
            tick_size = "0.01"
        try:
            neg_risk = client.get_neg_risk(token_id)
        except:
            neg_risk = False

        print(f"  tick_size={tick_size}, neg_risk={neg_risk}")

        order_args = OrderArgs(
            token_id=token_id,
            price=0.02,
            size=1.0,
            side='BUY',
        )
        options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
        signed = client.create_order(order_args, options)
        print(f"  Order maker:  {signed.order.maker}")
        print(f"  Order signer: {signed.order.signer}")
        print(f"  Order sigType: {signed.order.signatureType}")

        resp = client.post_order(signed, orderType=OrderType.GTC)
        print(f"  SUCCESS: {resp}")
        oid = resp.get('orderID') or resp.get('id')
        if oid:
            client.cancel(oid)
            print(f"  Cancelled {oid}")
    except Exception as e:
        print(f"  FAILED: {e}")
    print()
