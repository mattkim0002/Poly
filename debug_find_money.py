"""Find where the $47 actually is and check proxy relationships."""

import httpx

EOA_METAMASK = "0x5f474D47B106254513a8ddf3E38F434D7B013430"
EOA_EXPORTED = "0x2bc8AeaC744A4B812D4CB049EF6A30Ff52a16C80"
POLYMARKET_ADDR = "0xa1A623585f0D860c3156c8d2b6ADFFc066922c69"

USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
RPC = "https://polygon-rpc.com"

def rpc_call(method, params):
    resp = httpx.post(RPC, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1}, timeout=15)
    return resp.json().get("result", "0x0")

def get_usdc_balance(token, wallet):
    addr_padded = wallet[2:].lower().zfill(64)
    data = f"0x70a08231{addr_padded}"
    result = rpc_call("eth_call", [{"to": token, "data": data}, "latest"])
    return int(result, 16) / 1e6

def is_contract(addr):
    """Check if address is a smart contract (has code) or EOA (no code)."""
    code = rpc_call("eth_getCode", [addr, "latest"])
    return code != "0x" and code != "0x0" and len(code) > 4

print("=== Address type check ===")
for label, addr in [("MetaMask", EOA_METAMASK), ("Exported key", EOA_EXPORTED), ("Polymarket", POLYMARKET_ADDR)]:
    contract = is_contract(addr)
    print(f"  {label}: {addr}")
    print(f"    Type: {'SMART CONTRACT (proxy wallet)' if contract else 'EOA (regular wallet)'}")
print()

print("=== USDC balances (all 3 addresses) ===")
for label, addr in [("MetaMask", EOA_METAMASK), ("Exported key", EOA_EXPORTED), ("Polymarket", POLYMARKET_ADDR)]:
    usdc_e = get_usdc_balance(USDC_E, addr)
    usdc_n = get_usdc_balance(USDC_NATIVE, addr)
    print(f"  {label}: USDC.e=${usdc_e:.2f}  USDC=${usdc_n:.2f}")
print()

# Check Polymarket exchange contracts for deposited funds
EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

print("=== Exchange contract USDC balances ===")
for label, addr in [("Exchange", EXCHANGE), ("NegRisk Exchange", NEG_RISK_EXCHANGE)]:
    usdc_e = get_usdc_balance(USDC_E, addr)
    print(f"  {label}: USDC.e=${usdc_e:.2f}")
print()

# Check proxy factory for registered proxies
# Polymarket Proxy Factory on Polygon
PROXY_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"

print("=== Proxy wallet lookup ===")
for label, addr in [("MetaMask", EOA_METAMASK), ("Exported key", EOA_EXPORTED)]:
    # getProxyWalletAddress(address) selector = 0x...
    # Try common function selectors for proxy lookup
    addr_padded = addr[2:].lower().zfill(64)

    # Try: getProxy(address) - selector depends on exact function name
    # Common proxy factory methods
    for selector, fname in [
        ("0xeae8c2d5", "getProxy(address)"),
        ("0x5a580e72", "getProxyWalletAddress(address)"),
        ("0x15cc36f2", "proxies(address)"),
    ]:
        data = f"{selector}{addr_padded}"
        try:
            result = rpc_call("eth_call", [{"to": PROXY_FACTORY, "data": data}, "latest"])
            if result and result != "0x" and int(result, 16) != 0:
                proxy_addr = "0x" + result[-40:]
                print(f"  {label} -> {fname} = {proxy_addr}")
        except:
            pass
print()

# Check Polymarket data API for all 3 addresses
print("=== Polymarket data API profiles ===")
for label, addr in [("MetaMask", EOA_METAMASK), ("Exported key", EOA_EXPORTED), ("Polymarket", POLYMARKET_ADDR)]:
    try:
        resp = httpx.get(f"https://gamma-api.polymarket.com/users/{addr.lower()}", timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            print(f"  {label}: Found! Name={data.get('name', '?')} ProxyAddr={data.get('proxyAddress', '?')}")
        else:
            print(f"  {label}: {resp.status_code} (not found)")
    except Exception as e:
        print(f"  {label}: ERROR - {e}")

# Also try the CLOB server profile
print()
print("=== CLOB API balance check (exported key, sig=0, no funder) ===")
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
import config

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
print(f"  Balance/Allowance: {result}")
