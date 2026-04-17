"""Dry-run scanner test — shows expanded universe without placing any trades.

Usage:
    python tools/test_scanner.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.scanner import scan_universe
import config

if __name__ == "__main__":
    print(f"[TEST] Scanner settings:")
    print(f"  Pages:     {config.SCAN_MAX_PAGES} × {config.SCAN_LIMIT_PER_PAGE} = up to {config.SCAN_MAX_PAGES * config.SCAN_LIMIT_PER_PAGE} markets")
    print(f"  Min vol24: ${config.MIN_24H_VOLUME:,}")
    print(f"  Min liq:   ${config.SCAN_MIN_LIQUIDITY:,}")
    print(f"  Excl tags: {config.SCAN_EXCLUDED_TAGS}")
    print()

    candidates = scan_universe(debug=True)

    print(f"\n[TEST] Candidates passed to strategy engine: {len(candidates)}")

    # Arb candidates (yes + no < 1.00)
    arb = [c for c in candidates if c["yes_price"] > 0 and c["no_price"] > 0
           and c["yes_price"] + c["no_price"] < 0.998]
    print(f"[TEST] Arb candidates (yes+no < 0.998): {len(arb)}")
    for c in arb[:5]:
        spread = 1 - c["yes_price"] - c["no_price"]
        print(f"  spread={spread:.3f} | yes={c['yes_price']:.2f} no={c['no_price']:.2f} | {c['question'][:60]}")

    # Endgame candidates (one side >= 0.85)
    endgame = [c for c in candidates
               if (0.85 <= c["yes_price"] <= 0.99) or (0.85 <= c["no_price"] <= 0.99)]
    print(f"[TEST] Endgame candidates (one side 0.85-0.99): {len(endgame)}")
    for c in endgame[:5]:
        price = c["yes_price"] if c["yes_price"] >= 0.85 else c["no_price"]
        exp = f"{c['time_to_expiry_minutes']:.0f}m" if c["time_to_expiry_minutes"] else "?"
        print(f"  price={price:.2f} | expires={exp} | {c['question'][:60]}")
