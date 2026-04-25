"""Live P&L watcher — reads polybot.db and refreshes every 5 seconds.

Run on the droplet (or via ssh):
    python3 tools/watch_pnl.py

Prints a single-screen dashboard that auto-refreshes:
  - Open positions (entry price, size, cost)
  - Today's P&L (wins/losses/net)
  - All-time P&L and win rate
  - Last 5 closed trades

Read-only. No network calls. No DB writes. Ctrl+C to quit.
"""

import os
import sqlite3
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "polybot.db")
REFRESH_SEC = 5


def load_trades() -> list[dict]:
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """
        SELECT id, market_question, outcome, entry_price, size, cost,
               exit_price, pnl, status, created_at, closed_at
        FROM trades
        ORDER BY created_at DESC
        """
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    for r in rows:
        try:
            r["pnl"] = float(r.get("pnl") or 0.0)
        except (TypeError, ValueError):
            r["pnl"] = 0.0
    return rows


def fmt_money(x: float) -> str:
    sign = "-" if x < 0 else "+" if x > 0 else " "
    return f"{sign}${abs(x):>6.2f}"


def render(trades: list[dict]) -> None:
    # Clear screen + move cursor home
    sys.stdout.write("\033[2J\033[H")
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    today = datetime.utcnow().strftime("%Y-%m-%d")

    open_trades = [t for t in trades if t.get("status") == "open"]
    closed = [t for t in trades if t.get("status") in ("won", "lost")]
    today_closed = [t for t in closed if (t.get("created_at") or "").startswith(today)]

    total_pnl = sum(t["pnl"] for t in closed)
    today_pnl = sum(t["pnl"] for t in today_closed)
    wins = sum(1 for t in closed if t["pnl"] > 0)
    losses = sum(1 for t in closed if t["pnl"] < 0)
    win_rate = (wins / len(closed) * 100) if closed else 0.0
    today_wins = sum(1 for t in today_closed if t["pnl"] > 0)
    today_losses = sum(1 for t in today_closed if t["pnl"] < 0)

    open_cost = sum(float(t.get("cost") or 0.0) for t in open_trades)

    print(f"┌─ POLY P&L WATCHER ─ {now} ─ Ctrl+C to quit ─")
    print(f"│")
    print(f"│  OPEN POSITIONS: {len(open_trades)}   At risk: ${open_cost:>6.2f}")
    if open_trades:
        print(f"│")
        for t in open_trades[:10]:
            q = (t.get("market_question") or "")[:48]
            ep = float(t.get("entry_price") or 0.0)
            sz = float(t.get("size") or 0.0)
            cost = float(t.get("cost") or 0.0)
            ts = (t.get("created_at") or "")[:16]
            print(f"│    {ts}  {t.get('outcome','?'):<3} @ ${ep:.2f}  x{sz:>5.0f}  (${cost:>6.2f})  {q}")
    print(f"│")
    print(f"├─ TODAY ({today}) ─")
    print(f"│  Trades: {len(today_closed)}   Wins: {today_wins}   Losses: {today_losses}")
    print(f"│  P&L:    {fmt_money(today_pnl)}")
    print(f"│")
    print(f"├─ ALL-TIME ─")
    print(f"│  Trades: {len(closed)}   Wins: {wins}   Losses: {losses}   Win rate: {win_rate:.1f}%")
    print(f"│  P&L:    {fmt_money(total_pnl)}")
    print(f"│")
    print(f"├─ LAST 5 CLOSED ─")
    last5 = closed[:5]
    if not last5:
        print(f"│  (none)")
    for t in last5:
        q = (t.get("market_question") or "")[:44]
        ep = float(t.get("entry_price") or 0.0)
        xp = float(t.get("exit_price") or 0.0)
        ts = (t.get("closed_at") or t.get("created_at") or "")[:16]
        tag = "WIN " if t["pnl"] > 0 else "LOSS"
        print(f"│    {ts}  {tag}  ${ep:.2f}→${xp:.2f}  {fmt_money(t['pnl'])}  {q}")
    print(f"└─")
    sys.stdout.flush()


def main() -> None:
    if not os.path.exists(DB_PATH):
        print(f"ERROR: {DB_PATH} not found. Is the bot running from {ROOT}?")
        sys.exit(1)
    try:
        while True:
            trades = load_trades()
            render(trades)
            time.sleep(REFRESH_SEC)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        print("bye")


if __name__ == "__main__":
    main()
