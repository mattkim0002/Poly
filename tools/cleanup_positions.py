"""One-time cleanup: close stale open positions in both DB and paper state.

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
    """Close any DB trades that are 'open' but the market clearly expired."""
    if not os.path.exists(DB_PATH):
        print("No polybot.db found")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    open_trades = conn.execute("SELECT * FROM trades WHERE status = 'open'").fetchall()
    print(f"\nFound {len(open_trades)} open trades in DB")

    now = datetime.now(timezone.utc)
    closed_count = 0

    for t in open_trades:
        trade_id = t["id"]
        question = t["market_question"] or ""
        end_date = t["end_date"] or ""
        entry_price = float(t["entry_price"] or 0)
        size = float(t["size"] or 0)
        cost = float(t["cost"] or 0)

        should_close = False
        reason = ""

        # Check end_date
        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                if now > end_dt:
                    should_close = True
                    reason = f"end_date {end_date} passed"
            except (ValueError, TypeError):
                pass

        # Check date in question title
        if not should_close:
            import re
            q_lower = question.lower()
            date_match = re.search(
                r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})',
                q_lower
            )
            if date_match:
                try:
                    months = ["january","february","march","april","may","june","july",
                              "august","september","october","november","december"]
                    month_num = months.index(date_match.group(1)) + 1
                    day = int(date_match.group(2))
                    market_date = datetime(now.year, month_num, day, 23, 59, tzinfo=timezone.utc)
                    if now > market_date:
                        should_close = True
                        reason = f"title date '{date_match.group(0)}' passed"
                except (ValueError, IndexError):
                    pass

        # Check if already resolved (duplicate row)
        if not should_close:
            dupes = conn.execute(
                "SELECT id, status, pnl FROM trades WHERE token_id = ? AND status != 'open' ORDER BY closed_at DESC LIMIT 1",
                (t["token_id"],)
            ).fetchone()
            if dupes:
                should_close = True
                reason = f"duplicate of resolved trade #{dupes['id']} ({dupes['status']}, pnl=${dupes['pnl']:.2f})"

        if should_close:
            # Use exit_price=0 for expired, but check if there's a resolved version
            exit_price = 0.0
            pnl = -cost
            status = "lost"

            resolved = conn.execute(
                "SELECT exit_price, pnl, status FROM trades WHERE token_id = ? AND status != 'open' ORDER BY closed_at DESC LIMIT 1",
                (t["token_id"],)
            ).fetchone()
            if resolved:
                exit_price = float(resolved["exit_price"] or 0)
                pnl = float(resolved["pnl"] or 0)
                status = resolved["status"]

            conn.execute(
                "UPDATE trades SET status = ?, exit_price = ?, pnl = ?, closed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, exit_price, pnl, trade_id)
            )
            closed_count += 1
            print(f"  CLOSED #{trade_id}: {question[:50]} | {reason} | {status} pnl=${pnl:+.2f}")
        else:
            print(f"  KEEP   #{trade_id}: {question[:50]}")

    conn.commit()
    conn.close()
    print(f"\nClosed {closed_count} stale trades in DB")


def clean_paper():
    """Remove expired positions from paper_trades.json."""
    if not os.path.exists(PAPER_PATH):
        print("\nNo paper_trades.json found")
        return

    with open(PAPER_PATH) as f:
        state = json.load(f)

    positions = state.get("open_positions", [])
    print(f"\nFound {len(positions)} open paper positions")

    now = datetime.now(timezone.utc)
    keep = []
    closed = 0

    for pos in positions:
        question = pos.get("question", "")
        expired = False

        import re
        q_lower = question.lower()
        date_match = re.search(
            r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})',
            q_lower
        )
        if date_match:
            try:
                months = ["january","february","march","april","may","june","july",
                          "august","september","october","november","december"]
                month_num = months.index(date_match.group(1)) + 1
                day = int(date_match.group(2))
                market_date = datetime(now.year, month_num, day, 23, 59, tzinfo=timezone.utc)
                if now > market_date:
                    expired = True
            except (ValueError, IndexError):
                pass

        if expired:
            entry = float(pos.get("entry_price", 0))
            size = float(pos.get("size", 0))
            state["balance"] += 0  # expired at $0 (conservative)
            state["total_pnl"] -= entry * size
            state["losses"] = state.get("losses", 0) + 1
            pos["status"] = "expired"
            pos["exit_price"] = 0.0
            pos["pnl"] = -(entry * size)
            state.setdefault("trades", []).append(pos)
            closed += 1
            print(f"  EXPIRED: {question[:50]}")
        else:
            keep.append(pos)
            print(f"  KEEP:    {question[:50]}")

    state["open_positions"] = keep
    with open(PAPER_PATH, "w") as f:
        json.dump(state, f, indent=2)
    print(f"\nClosed {closed} expired paper positions")


if __name__ == "__main__":
    print("=== POSITION CLEANUP ===")
    clean_db()
    clean_paper()
    print("\nDone! Restart the bot + dashboard to see changes.")
