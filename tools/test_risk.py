"""Dry-run test for the risk module.

Usage:
    python tools/test_risk.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import risk_manager as risk

# Simulate a $38 bankroll with $3 open exposure, flat P&L, no recent losses.
STATE = {
    "bankroll": 38.0,
    "open_exposure_usdc": 3.0,
    "per_market_exposure": {"0x_market_A": 1.5},
    "daily_pnl_usdc": 0.0,
    "consecutive_losses": 0,
}

PROPOSALS = [
    {
        "name": "healthy Tier A arb",
        "proposal": {
            "market_id": "0x_market_A",
            "strategy": "arb",
            "side": "YES",
            "proposed_size_usdc": 1.50,
            "net_edge_pct": 3.2,
            "expected_profit_usdc": 0.05,
            "confidence": "high",
            "time_to_expiry_minutes": 180,
        },
    },
    {
        "name": "over max trade size",
        "proposal": {
            "market_id": "0x_market_B",
            "strategy": "arb",
            "side": "NO",
            "proposed_size_usdc": 5.00,
            "net_edge_pct": 2.1,
            "expected_profit_usdc": 0.10,
            "confidence": "medium",
            "time_to_expiry_minutes": 60,
        },
    },
    {
        "name": "expected profit too low",
        "proposal": {
            "market_id": "0x_market_C",
            "strategy": "endgame",
            "side": "YES",
            "proposed_size_usdc": 0.80,
            "net_edge_pct": 1.2,
            "expected_profit_usdc": 0.005,
            "confidence": "low",
            "time_to_expiry_minutes": 30,
        },
    },
    {
        "name": "per-market cap hit",
        "proposal": {
            "market_id": "0x_market_A",
            "strategy": "endgame",
            "side": "NO",
            "proposed_size_usdc": 3.00,
            "net_edge_pct": 2.0,
            "expected_profit_usdc": 0.08,
            "confidence": "medium",
            "time_to_expiry_minutes": 120,
        },
    },
    {
        "name": "consecutive loss pause (simulated)",
        "proposal": {
            "market_id": "0x_market_D",
            "strategy": "arb",
            "side": "YES",
            "proposed_size_usdc": 1.00,
            "net_edge_pct": 2.5,
            "expected_profit_usdc": 0.04,
            "confidence": "high",
            "time_to_expiry_minutes": 90,
        },
        "state_override": {**STATE, "consecutive_losses": 3},
    },
    {
        "name": "daily loss cap breached",
        "proposal": {
            "market_id": "0x_market_E",
            "strategy": "arb",
            "side": "NO",
            "proposed_size_usdc": 1.00,
            "net_edge_pct": 2.0,
            "expected_profit_usdc": 0.03,
            "confidence": "medium",
            "time_to_expiry_minutes": 240,
        },
        "state_override": {**STATE, "daily_pnl_usdc": -2.10},
    },
]

if __name__ == "__main__":
    print(f"=== Risk module dry-run ({len(PROPOSALS)} scenarios) ===\n")
    print(f"Limits: max_trade=${risk.MAX_TRADE_SIZE_USDC}, "
          f"max_pct={risk.MAX_RISK_PCT_PER_TRADE*100:.0f}%, "
          f"daily_cap={risk.DAILY_LOSS_CAP_PCT*100:.0f}%, "
          f"max_losses={risk.MAX_CONSECUTIVE_LOSSES}\n")

    approved = 0
    rejected = 0
    for case in PROPOSALS:
        print(f"--- {case['name']} ---")
        state = case.get("state_override") or STATE
        decision = risk.evaluate_trade(case["proposal"], state=state)
        risk.log_decision(case["proposal"], decision)
        if decision["approved"]:
            approved += 1
        else:
            rejected += 1
        print()

    print(f"=== Summary: {approved} approved, {rejected} rejected ===")
