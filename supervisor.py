"""Offline Strategy Supervisor — Claude reviews daily performance and updates config.

Run once per day (via cron or manually):
    python3 supervisor.py

What it does:
1. Reads recent trade history from the database
2. Calculates performance metrics (win rate, expectancy, per-combo stats)
3. Asks Claude to analyze performance and recommend config changes
4. Writes updated strategy_config.json for the bot to use next cycle

The bot reads strategy_config.json at startup and on each cycle.
Claude never runs in the hot path — only here, offline, once per day.
"""

import json
import os
import sys
from datetime import datetime, timezone

import anthropic

import config
from core import database
from utils.logger import log

STRATEGY_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "strategy_config.json")


def load_current_config() -> dict:
    """Load the current strategy config."""
    try:
        with open(STRATEGY_CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_trade_stats() -> dict:
    """Pull trade performance from DB for Claude to analyze."""
    database.init_db()
    recent = database.get_recent_trades(50)

    if not recent:
        return {"total_trades": 0, "message": "No trades yet"}

    wins = [t for t in recent if (t.get("pnl") or 0) > 0]
    losses = [t for t in recent if (t.get("pnl") or 0) <= 0 and t.get("status") != "cancelled"]
    cancelled = [t for t in recent if t.get("status") == "cancelled"]

    total_pnl = sum(t.get("pnl") or 0 for t in recent if t.get("pnl") is not None)
    avg_win = sum(t.get("pnl", 0) for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.get("pnl", 0) for t in losses) / len(losses) if losses else 0

    # Per-edge breakdown
    edge_buckets = {"0-5%": [], "5-10%": [], "10-15%": [], "15%+": []}
    for t in recent:
        edge = t.get("edge") or 0
        if edge < 0.05:
            edge_buckets["0-5%"].append(t)
        elif edge < 0.10:
            edge_buckets["5-10%"].append(t)
        elif edge < 0.15:
            edge_buckets["10-15%"].append(t)
        else:
            edge_buckets["15%+"].append(t)

    edge_stats = {}
    for bucket, trades in edge_buckets.items():
        if trades:
            bucket_wins = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
            bucket_pnl = sum(t.get("pnl") or 0 for t in trades if t.get("pnl") is not None)
            edge_stats[bucket] = {
                "count": len(trades),
                "win_rate": bucket_wins / len(trades),
                "total_pnl": round(bucket_pnl, 2),
            }

    # Recent trade details (last 20)
    trade_details = []
    for t in recent[-20:]:
        trade_details.append({
            "question": (t.get("market_question") or "")[:60],
            "outcome": t.get("outcome"),
            "edge": round(t.get("edge") or 0, 3),
            "entry": round(t.get("entry_price") or 0, 3),
            "pnl": round(t.get("pnl") or 0, 2),
            "status": t.get("status"),
            "r_multiple": round(t.get("r_multiple") or 0, 2),
        })

    return {
        "total_trades": len(recent),
        "wins": len(wins),
        "losses": len(losses),
        "cancelled": len(cancelled),
        "win_rate": len(wins) / max(len(wins) + len(losses), 1),
        "total_pnl": round(total_pnl, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "edge_stats": edge_stats,
        "recent_trades": trade_details,
    }


def run_supervisor():
    """Run Claude supervisor to analyze performance and update config."""
    current_config = load_current_config()
    trade_stats = get_trade_stats()

    print("=" * 60)
    print("  STRATEGY SUPERVISOR — Daily Review")
    print("=" * 60)
    print(f"\nTrade stats: {trade_stats['total_trades']} trades | "
          f"Win rate: {trade_stats.get('win_rate', 0):.0%} | "
          f"P&L: ${trade_stats.get('total_pnl', 0):.2f}")

    if trade_stats["total_trades"] < 3:
        print("\nNot enough trades to analyze. Keeping current config.")
        return

    prompt = f"""You are the offline strategy supervisor for a Polymarket crypto trading bot.

The bot trades 5-minute crypto Up/Down binary markets using momentum signals from Binance.
It runs deterministically using a JSON config that YOU control. Review the performance data
and output an updated config.

== CURRENT CONFIG ==
{json.dumps(current_config, indent=2)}

== PERFORMANCE DATA ==
{json.dumps(trade_stats, indent=2)}

== YOUR JOB ==
Analyze the data and output ONLY a valid JSON config with these fields:

1. "regime": "trending" or "choppy" — based on recent market behavior
2. "signal_combos": dict of signal combinations and their thresholds
   - Keys are combos like "volume_spike+accelerating+whale"
   - Available signals: volume_spike, accelerating, whale, orderbook_imbalance,
     taker_buy, funding_confirms, thin_wall, trend_180s
   - Values: {{"min_edge": float, "kelly_mult": float, "note": str}}
3. "skip_combos": list of combos to NEVER trade (proven losers)
4. "regime_thresholds": {{
     "atr_trending_min": float (ATR % threshold for trending),
     "atr_choppy_max": float (ATR % threshold for choppy),
     "min_boosters_choppy": int (min signals needed in choppy regime),
     "min_boosters_trending": int (min signals needed in trending regime),
     "disable_momentum_choppy": bool
   }}
5. "risk_params": {{
     "max_position_pct": float (max % of bankroll per trade),
     "kelly_fraction": float (Kelly multiplier),
     "stop_loss_pct": float,
     "take_profit_pct": float,
     "max_open_positions": int,
     "daily_loss_limit_pct": float,
     "min_edge": float
   }}

Rules:
- If win rate < 40%, tighten thresholds (higher min_edge, more boosters required)
- If win rate > 60%, can loosen slightly
- If a signal combo has negative P&L, add it to skip_combos
- If trades with edge < 5% are losing, raise min_edge
- If cancelled > 30% of trades, something is wrong with order execution
- Keep max_position_pct between 0.10 and 0.30
- Keep kelly_fraction between 0.20 and 0.50
- Be conservative — survival matters more than growth

Output ONLY the JSON config, no other text."""

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()

        # Extract JSON from response (handle markdown code blocks)
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        new_config = json.loads(text)

        # Add metadata
        new_config["updated_at"] = datetime.now(timezone.utc).isoformat()
        new_config["updated_by"] = "claude_supervisor"

        # Validate required fields
        required = ["regime", "signal_combos", "regime_thresholds", "risk_params"]
        for field in required:
            if field not in new_config:
                print(f"\nERROR: Claude output missing '{field}'. Keeping current config.")
                return

        # Write updated config
        with open(STRATEGY_CONFIG_PATH, "w") as f:
            json.dump(new_config, f, indent=2)

        print(f"\nConfig updated successfully!")
        print(f"  Regime: {new_config['regime']}")
        print(f"  Signal combos: {len(new_config['signal_combos'])}")
        print(f"  Skip combos: {new_config.get('skip_combos', [])}")
        print(f"  Min edge: {new_config['risk_params'].get('min_edge', 'N/A')}")
        print(f"  Kelly: {new_config['risk_params'].get('kelly_fraction', 'N/A')}")
        print(f"\nFull config written to: {STRATEGY_CONFIG_PATH}")

    except json.JSONDecodeError as e:
        print(f"\nERROR: Claude returned invalid JSON: {e}")
        print(f"Raw response:\n{text[:500]}")
    except Exception as e:
        print(f"\nERROR: Supervisor failed: {e}")


if __name__ == "__main__":
    run_supervisor()
