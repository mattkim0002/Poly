"""Loss diagnostic — read polybot.db and report which strategy is bleeding.

Read-only. No network calls. No DB writes. Run on the droplet:

    python tools/analyze_losses.py

Segments closed trades by strategy prefix in `market_question`:
  [ARB-YES] / [ARB-NO]  → arbitrage
  [LAG]                 → Binance-Lag directional
  [SNIPE]               → resolution sniper
  (no prefix)           → momentum + Claude

Then prints:
  1. Per-strategy P&L (all-time)
  2. Per-strategy P&L (today only — answers "where did today's loss go")
  3. Top 10 biggest losing trades all-time
  4. Correlated-loss clusters (same asset, within 5 minutes)
  5. Worst 3 hours-of-day by P&L
  6. One-line summary per strategy (paste-friendly)
"""

import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

# Allow running from repo root or tools/
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "polybot.db")


# ── strategy classification ──────────────────────────────────────────────────

def classify_strategy(question: str) -> str:
    q = question or ""
    if "[ARB-YES]" in q or "[ARB-NO]" in q or q.startswith("[ARB"):
        return "ARB"
    if "[LAG]" in q:
        return "LAG"
    if "[SNIPE]" in q:
        return "SNIPE"
    return "MOMENTUM"


# ── asset key (mirrors main.py::extract_asset_key) ───────────────────────────

_ASSET_PATTERNS = [
    ("BTC", r"\b(btc|bitcoin)\b"),
    ("ETH", r"\b(eth|ethereum|ether)\b"),
    ("SOL", r"\b(sol|solana)\b"),
    ("XRP", r"\b(xrp|ripple)\b"),
    ("BNB", r"\b(bnb|binance coin)\b"),
    ("DOGE", r"\b(doge|dogecoin)\b"),
    ("ADA", r"\b(ada|cardano)\b"),
    ("MATIC", r"\b(matic|polygon)\b"),
]


def extract_asset_key(question: str) -> str | None:
    if not question:
        return None
    q = question.lower()
    for key, pat in _ASSET_PATTERNS:
        if re.search(pat, q):
            return key
    return None


# ── data loading ─────────────────────────────────────────────────────────────

def load_trades() -> list[dict]:
    if not os.path.exists(DB_PATH):
        print(f"ERROR: {DB_PATH} not found")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """
        SELECT id, market_question, outcome, entry_price, exit_price,
               size, cost, pnl, status, created_at, closed_at
        FROM trades
        WHERE status IN ('won', 'lost')
        ORDER BY created_at ASC
        """
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    for r in rows:
        r["strategy"] = classify_strategy(r.get("market_question") or "")
        r["asset"] = extract_asset_key(r.get("market_question") or "")
        try:
            r["pnl"] = float(r.get("pnl") or 0.0)
        except (TypeError, ValueError):
            r["pnl"] = 0.0
    return rows


# ── reporting helpers ────────────────────────────────────────────────────────

def fmt_money(x: float) -> str:
    sign = "-" if x < 0 else "+" if x > 0 else " "
    return f"{sign}${abs(x):>6.2f}"


def per_strategy_breakdown(trades: list[dict]) -> dict:
    out = defaultdict(lambda: {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0,
                               "win_pnl": 0.0, "loss_pnl": 0.0, "biggest_loss": 0.0,
                               "biggest_win": 0.0})
    for t in trades:
        s = t["strategy"]
        b = out[s]
        b["n"] += 1
        b["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            b["wins"] += 1
            b["win_pnl"] += t["pnl"]
            if t["pnl"] > b["biggest_win"]:
                b["biggest_win"] = t["pnl"]
        elif t["pnl"] < 0:
            b["losses"] += 1
            b["loss_pnl"] += t["pnl"]
            if t["pnl"] < b["biggest_loss"]:
                b["biggest_loss"] = t["pnl"]
    return out


def print_breakdown(title: str, breakdown: dict) -> None:
    print(f"\n{'─' * 72}")
    print(title)
    print("─" * 72)
    print(f"{'STRATEGY':<10} {'N':>4} {'WINS':>5} {'LOSS':>5} {'WIN%':>6} "
          f"{'TOTAL':>10} {'AVG WIN':>10} {'AVG LOSS':>10} {'BIG LOSS':>10}")
    keys = sorted(breakdown.keys(), key=lambda k: breakdown[k]["pnl"])
    grand_pnl = 0.0
    for s in keys:
        b = breakdown[s]
        if b["n"] == 0:
            continue
        win_rate = b["wins"] / b["n"] if b["n"] else 0.0
        avg_win = b["win_pnl"] / b["wins"] if b["wins"] else 0.0
        avg_loss = b["loss_pnl"] / b["losses"] if b["losses"] else 0.0
        grand_pnl += b["pnl"]
        print(f"{s:<10} {b['n']:>4} {b['wins']:>5} {b['losses']:>5} "
              f"{win_rate*100:>5.1f}% {fmt_money(b['pnl']):>10} "
              f"{fmt_money(avg_win):>10} {fmt_money(avg_loss):>10} "
              f"{fmt_money(b['biggest_loss']):>10}")
    print("─" * 72)
    print(f"{'TOTAL':<10} {' ':>4} {' ':>5} {' ':>5} {' ':>6} {fmt_money(grand_pnl):>10}")


def biggest_losers(trades: list[dict], n: int = 10) -> None:
    print(f"\n{'─' * 72}")
    print(f"TOP {n} BIGGEST LOSING TRADES (all-time)")
    print("─" * 72)
    losers = sorted([t for t in trades if t["pnl"] < 0], key=lambda t: t["pnl"])[:n]
    if not losers:
        print("  (none)")
        return
    for t in losers:
        q = (t.get("market_question") or "")[:55]
        ep = t.get("entry_price") or 0.0
        xp = t.get("exit_price") or 0.0
        ts = (t.get("created_at") or "")[:19]
        print(f"  {ts}  {t['strategy']:<8} {fmt_money(t['pnl'])}  "
              f"${ep:.2f}→${xp:.2f}  {q}")


def correlated_losses(trades: list[dict]) -> None:
    print(f"\n{'─' * 72}")
    print("CORRELATED LOSS CLUSTERS (same asset within 5-minute window)")
    print("─" * 72)
    losers = [t for t in trades if t["pnl"] < 0 and t["asset"] and t.get("created_at")]
    parsed = []
    for t in losers:
        try:
            ts = datetime.fromisoformat(str(t["created_at"]).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        parsed.append((ts, t))
    parsed.sort(key=lambda x: x[0])

    clusters = []
    used = set()
    for i, (ts_i, t_i) in enumerate(parsed):
        if i in used:
            continue
        group = [(ts_i, t_i)]
        for j in range(i + 1, len(parsed)):
            if j in used:
                continue
            ts_j, t_j = parsed[j]
            if t_j["asset"] != t_i["asset"]:
                continue
            if (ts_j - ts_i).total_seconds() > 300:
                break
            group.append((ts_j, t_j))
            used.add(j)
        if len(group) >= 2:
            clusters.append((t_i["asset"], group))
            used.add(i)

    if not clusters:
        print("  (none — duplicate-exposure gate appears to be working)")
        return
    for asset, group in clusters[:15]:
        cluster_pnl = sum(t["pnl"] for _, t in group)
        ts0 = group[0][0].strftime("%Y-%m-%d %H:%M")
        print(f"  {ts0}  {asset}  ×{len(group)} trades  total {fmt_money(cluster_pnl)}")
        for _, t in group:
            print(f"      └─ {t['strategy']:<8} {fmt_money(t['pnl'])}  "
                  f"{(t.get('market_question') or '')[:50]}")


def hour_of_day_breakdown(trades: list[dict]) -> None:
    print(f"\n{'─' * 72}")
    print("WORST 3 HOURS OF DAY (by total P&L)")
    print("─" * 72)
    by_hour = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for t in trades:
        ts = t.get("created_at")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        h = dt.hour
        by_hour[h]["n"] += 1
        by_hour[h]["pnl"] += t["pnl"]
    if not by_hour:
        print("  (no timestamps)")
        return
    worst = sorted(by_hour.items(), key=lambda kv: kv[1]["pnl"])[:3]
    for h, b in worst:
        print(f"  {h:02d}:00 UTC  n={b['n']:>3}  pnl={fmt_money(b['pnl'])}")


def one_line_summary(breakdown: dict) -> None:
    print(f"\n{'─' * 72}")
    print("PASTE-FRIENDLY SUMMARY")
    print("─" * 72)
    for s in sorted(breakdown.keys(), key=lambda k: breakdown[k]["pnl"]):
        b = breakdown[s]
        if b["n"] == 0:
            continue
        wr = b["wins"] / b["n"] * 100 if b["n"] else 0.0
        avg_loss = b["loss_pnl"] / b["losses"] if b["losses"] else 0.0
        print(f"[{s}] {b['n']} trades, win rate {wr:.0f}%, "
              f"P&L {fmt_money(b['pnl'])}, avg loss {fmt_money(avg_loss)}")


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    trades = load_trades()
    print(f"\nLoaded {len(trades)} closed trades from {DB_PATH}")

    if not trades:
        print("(no trades — nothing to analyze)")
        return

    today = datetime.utcnow().strftime("%Y-%m-%d")
    today_trades = [t for t in trades if (t.get("created_at") or "").startswith(today)]

    all_breakdown = per_strategy_breakdown(trades)
    today_breakdown = per_strategy_breakdown(today_trades)

    print_breakdown("ALL-TIME P&L BY STRATEGY", all_breakdown)
    print_breakdown(f"TODAY'S P&L BY STRATEGY ({today} UTC, {len(today_trades)} trades)",
                    today_breakdown)
    biggest_losers(trades, n=10)
    correlated_losses(trades)
    hour_of_day_breakdown(trades)
    one_line_summary(all_breakdown)
    print()


if __name__ == "__main__":
    main()
