"""Debug: check allowances and try to set them up for both addresses."""

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
import config

EOA = "0x5f474D47B106254513a8ddf3E38F434D7B013430"
PROXY = config.POLYMARKET_FUNDER_ADDRESS

# Use sig_type=0 with EOA (the only combo that gives valid signatures)
client = ClobClient(
    host=config.CLOB_HOST,
    key=config.POLYMARKET_PRIVATE_KEY,
    chain_id=config.CHAIN_ID,
    signature_type=0,
    funder=EOA,
)
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)

print("=== Checking balance/allowance for EOA ===")
for asset in [AssetType.COLLATERAL, AssetType.CONDITIONAL]:
    try:
        params = BalanceAllowanceParams(asset_type=asset, signature_type=0)
        result = client.get_balance_allowance(params)
        print(f"  {asset}: {result}")
    except Exception as e:
        print(f"  {asset}: ERROR - {e}")

print()
print("=== Trying to set allowances for EOA (update_balance_allowance) ===")
for asset in [AssetType.COLLATERAL, AssetType.CONDITIONAL]:
    try:
        params = BalanceAllowanceParams(asset_type=asset, signature_type=0)
        result = client.update_balance_allowance(params)
        print(f"  {asset}: {result}")
    except Exception as e:
        print(f"  {asset}: ERROR - {e}")

print()
print("=== Now checking balance/allowance with proxy funder ===")
client2 = ClobClient(
    host=config.CLOB_HOST,
    key=config.POLYMARKET_PRIVATE_KEY,
    chain_id=config.CHAIN_ID,
    signature_type=1,
    funder=PROXY,
)
creds2 = client2.create_or_derive_api_creds()
client2.set_api_creds(creds2)

for asset in [AssetType.COLLATERAL, AssetType.CONDITIONAL]:
    try:
        params = BalanceAllowanceParams(asset_type=asset, signature_type=1)
        result = client2.get_balance_allowance(params)
        print(f"  {asset}: {result}")
    except Exception as e:
        print(f"  {asset}: ERROR - {e}")

print()
print("=== Trying to set allowances for proxy ===")
for asset in [AssetType.COLLATERAL, AssetType.CONDITIONAL]:
    try:
        params = BalanceAllowanceParams(asset_type=asset, signature_type=1)
        result = client2.update_balance_allowance(params)
        print(f"  {asset}: {result}")
    except Exception as e:
        print(f"  {asset}: ERROR - {e}")
