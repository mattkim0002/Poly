"""Close ALL open positions in both DB and paper state.

Run on the droplet:
    cd ~/Poly && python3 tools/cleanup_positions.py
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "polybot.db")
PAPER_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "paper_trades.json")


def clean_db():
    """Force-close ALL open trades in the DB."""
    if not os.path.exists(DB_PATH):
        print("No polybot.db found")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    open_trades = conn.execute("SELECT * FROM trades WHERE status = 'open'").fetchall()
    print(f"\nFound {len(open_trades)} open trades in DB")

    for t in open_trades:
        trade_id = t["id"]
        question = t["market_question"] or ""
        entry_price = float(t["entry_price"] or 0)
        size = float(t["size"] or 0)
        cost = float(t["cost"] or 0)

        # Check if there's already a resolved version of this trade
        resolved = conn.execute(
            "SELECT exit_price, pnl, status FROM trades WHERE token_id = ? AND status != 'open' ORDER BY closed_at DESC LIMIT 1",
            (t["token_id"],)
        ).fetchone()

        if resolved:
            exit_price = float(resolved["exit_price"] or 0)
            pnl = float(resolved["pnl"] or 0)
            status = resolved["status"]
        else:
            exit_price = 0.0
            pnl = -cost
            status = "lost"

        conn.execute(
            "UPDATE trades SET status = ?, exit_price = ?, pnl = ?, closed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, exit_price, pnl, trade_id)
        )
        print(f"  CLOSED #{trade_id}: {question[:55]} | {status} pnl=${pnl:+.2f}")

    conn.commit()
    conn.close()
    print(f"\nClosed {len(open_trades)} trades in DB")


def clean_paper():
    """Close ALL open positions in paper_trades.json."""
    if not os.path.exists(PAPER_PATH):
        print("\nNo paper_trades.json found")
        return

    with open(PAPER_PATH) as f:
        state = json.load(f)

    positions = state.get("open_positions", [])
    print(f"\nFound {len(positions)} open paper positions")

    for pos in positions:
        question = pos.get("question", "")
        entry = float(pos.get("entry_price", 0))
        size = float(pos.get("size", 0))
        cost = entry * size

        state["total_pnl"] = state.get("total_pnl", 0) - cost
        state["losses"] = state.get("losses", 0) + 1
        pos["status"] = "force_closed"
        pos["exit_price"] = 0.0
        pos["pnl"] = -cost
        pos["exit_time"] = datetime.now(timezone.utc).isoformat()
        state.setdefault("trades", []).append(pos)
        print(f"  CLOSED: {question[:55]} | pnl=${-cost:+.2f}")

    state["open_positions"] = []
    with open(PAPER_PATH, "w") as f:
        json.dump(state, f, indent=2)
    print(f"\nClosed {len(positions)} paper positions")


if __name__ == "__main__":
    print("=== FORCE CLOSE ALL POSITIONS ===")
    clean_db()
    clean_paper()
    print("\nDone! Restart the bot + dashboard to see changes.")
