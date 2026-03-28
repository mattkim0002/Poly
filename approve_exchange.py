"""Approve Polymarket exchange contracts to spend USDC.e."""

import httpx
from eth_account import Account

import config

RPC = "https://polygon-bor-rpc.publicnode.com"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
MAX_UINT256 = "0x" + "f" * 64  # unlimited approval

# Exchange contracts that need approval
EXCHANGES = [
    ("Exchange", "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"),
    ("NegRisk Exchange", "0xC5d563A36AE78145C45a50134d48A1215220f80a"),
]

acct = Account.from_key(config.POLYMARKET_PRIVATE_KEY)
print(f"Wallet: {acct.address}")


def rpc_call(method, params):
    resp = httpx.post(RPC, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1}, timeout=30)
    return resp.json()


def get_nonce(addr):
    result = rpc_call("eth_getTransactionCount", [addr, "latest"])
    return int(result["result"], 16)


def get_gas_price():
    result = rpc_call("eth_gasPrice", [])
    return int(result["result"], 16)


def approve(spender, label, nonce):
    """Send an ERC20 approve transaction."""
    # approve(address,uint256) selector = 0x095ea7b3
    spender_padded = spender[2:].lower().zfill(64)
    amount_padded = "f" * 64  # max uint256
    data = f"0x095ea7b3{spender_padded}{amount_padded}"

    gas_price = get_gas_price()
    # Add 20% to gas price for faster confirmation
    gas_price = int(gas_price * 1.2)

    tx = {
        "to": USDC_E,
        "data": data,
        "gas": 60000,
        "gasPrice": gas_price,
        "nonce": nonce,
        "chainId": 137,
        "value": 0,
    }

    signed = acct.sign_transaction(tx)
    raw_tx = signed.raw_transaction.hex()
    if not raw_tx.startswith("0x"):
        raw_tx = "0x" + raw_tx

    result = rpc_call("eth_sendRawTransaction", [raw_tx])
    if "error" in result:
        print(f"  ❌ {label}: {result['error']}")
        return False
    else:
        tx_hash = result["result"]
        print(f"  ✅ {label}: tx {tx_hash}")
        return True


nonce = get_nonce(acct.address)
print(f"Nonce: {nonce}")
print(f"Gas price: {get_gas_price()} wei\n")

print("Approving USDC.e for exchange contracts...")
for label, exchange_addr in EXCHANGES:
    success = approve(exchange_addr, label, nonce)
    if success:
        nonce += 1

print("\nWaiting for confirmations...")
import time
time.sleep(5)

# Verify allowances
print("\nVerifying allowances...")
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

client = ClobClient(
    host=config.CLOB_HOST,
    key=config.POLYMARKET_PRIVATE_KEY,
    chain_id=config.CHAIN_ID,
    signature_type=0,
)
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)

params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=0)
result = client.get_balance_allowance(params)
bal = int(result.get("balance", "0")) / 1e6
print(f"Balance: ${bal:.2f}")
print(f"Allowances: {result.get('allowances', {})}")
