"""Set up exchange approvals and test order placement."""

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    AssetType, BalanceAllowanceParams, OrderArgs, OrderType, PartialCreateOrderOptions,
)
import config
from core import market_data

print("=== Setting up Polymarket CLOB trading ===\n")

# 1. Initialize client
client = ClobClient(
    host=config.CLOB_HOST,
    key=config.POLYMARKET_PRIVATE_KEY,
    chain_id=config.CHAIN_ID,
    signature_type=0,
)
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)
print(f"Client initialized. Signer: {client.signer.address()}")

# 2. Check balance before approvals
params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=0)
result = client.get_balance_allowance(params)
bal = int(result.get("balance", "0")) / 1e6
print(f"Balance: ${bal:.2f}")
print(f"Allowances: {result.get('allowances', {})}")

# 3. Set up approvals
print("\nSetting up exchange approvals...")
try:
    client.update_balance_allowance(params)
    print("  COLLATERAL approval submitted")
except Exception as e:
    print(f"  COLLATERAL approval error: {e}")

# 4. Re-check balance/allowance
import time
time.sleep(3)
result = client.get_balance_allowance(params)
bal = int(result.get("balance", "0")) / 1e6
print(f"\nAfter approval - Balance: ${bal:.2f}")
print(f"Allowances: {result.get('allowances', {})}")

if bal < 1:
    print("\n❌ Balance still $0 — something is wrong")
    exit()

# 5. Test order
print("\n=== Testing order placement ===")
markets = market_data.get_active_markets(limit=5)
if not markets:
    print("No markets found!")
    exit()

m = markets[0]
token_id = m['token_ids'][0]
print(f"Market: {m['question'][:60]}")

try:
    tick_size = client.get_tick_size(token_id)
except:
    tick_size = "0.01"
try:
    neg_risk = client.get_neg_risk(token_id)
except:
    neg_risk = False

order_args = OrderArgs(token_id=token_id, price=0.02, size=1.0, side='BUY')
options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)

try:
    signed = client.create_order(order_args, options)
    resp = client.post_order(signed, orderType=OrderType.GTC)
    print(f"✅ ORDER SUCCESS: {resp}")
    oid = resp.get('orderID') or resp.get('id')
    if oid:
        client.cancel(oid)
        print(f"Cancelled test order {oid}")
except Exception as e:
    print(f"❌ Order failed: {e}")
