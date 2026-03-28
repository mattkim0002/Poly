"""Check on-chain USDC balances for both addresses via Polygon RPC."""

import httpx
import json

EOA = "0x5f474D47B106254513a8ddf3E38F434D7B013430"
PROXY = "0xa1A623585f0D860c3156c8d2b6ADFFc066922c69"
USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e on Polygon
USDCE_NEW = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # native USDC on Polygon

RPC = "https://polygon-rpc.com"

def get_erc20_balance(token_addr, wallet_addr):
    """Call balanceOf(address) on an ERC20 contract."""
    # balanceOf selector = 0x70a08231, padded address
    addr_padded = wallet_addr[2:].lower().zfill(64)
    data = f"0x70a08231{addr_padded}"

    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": token_addr, "data": data}, "latest"],
        "id": 1,
    }
    resp = httpx.post(RPC, json=payload, timeout=15)
    result = resp.json().get("result", "0x0")
    return int(result, 16)

def get_matic_balance(wallet_addr):
    """Get native MATIC/POL balance."""
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getBalance",
        "params": [wallet_addr, "latest"],
        "id": 1,
    }
    resp = httpx.post(RPC, json=payload, timeout=15)
    result = resp.json().get("result", "0x0")
    return int(result, 16)

print("=== On-chain balances ===\n")

for label, addr in [("EOA (MetaMask)", EOA), ("Proxy wallet", PROXY)]:
    print(f"--- {label}: {addr} ---")

    matic = get_matic_balance(addr)
    print(f"  MATIC/POL:  {matic / 1e18:.6f}")

    usdc_e = get_erc20_balance(USDC, addr)
    print(f"  USDC.e:     {usdc_e / 1e6:.6f}")

    usdc_native = get_erc20_balance(USDCE_NEW, addr)
    print(f"  USDC:       {usdc_native / 1e6:.6f}")

    print()

# Also check the Polymarket data API for positions
print("=== Polymarket data API ===\n")
for label, addr in [("EOA", EOA), ("Proxy", PROXY)]:
    try:
        resp = httpx.get(
            "https://data-api.polymarket.com/positions",
            params={"user": addr},
            timeout=15,
        )
        positions = resp.json()
        print(f"{label} positions: {len(positions)} found")
        for p in positions[:5]:
            print(f"  Market: {p.get('title', p.get('market', '?'))[:50]}")
            print(f"  Size: {p.get('size', '?')} | Value: {p.get('currentValue', '?')}")
    except Exception as e:
        print(f"{label} positions: ERROR - {e}")
    print()

# Check profile/account info
print("=== Polymarket profile API ===\n")
for label, addr in [("EOA", EOA), ("Proxy", PROXY)]:
    try:
        resp = httpx.get(
            f"https://gamma-api.polymarket.com/users/{addr.lower()}",
            timeout=15,
        )
        print(f"{label}: {resp.status_code} - {resp.text[:200]}")
    except Exception as e:
        print(f"{label}: ERROR - {e}")
    print()
