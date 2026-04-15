"""Web dashboard — live P&L viewer at http://<droplet>:8080

Run on the droplet:
    python3 tools/dashboard.py

Auto-refreshes every 5 seconds. Read-only. No DB writes.
"""

import os
import sqlite3
from datetime import datetime
from flask import Flask, Response

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "polybot.db")

app = Flask(__name__)


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


def color(val: float) -> str:
    if val > 0:
        return "#00e676"
    if val < 0:
        return "#ff5252"
    return "#aaa"


def fmt(val: float) -> str:
    sign = "+" if val > 0 else ""
    return f"{sign}${val:.2f}"


@app.route("/")
def index():
    trades = load_trades()
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

    def row_bg(pnl):
        if pnl > 0: return "rgba(0,230,118,0.08)"
        if pnl < 0: return "rgba(255,82,82,0.08)"
        return "transparent"

    # Build open positions rows
    open_rows = ""
    for t in open_trades[:10]:
        q = (t.get("market_question") or "")[:60]
        ep = float(t.get("entry_price") or 0.0)
        sz = float(t.get("size") or 0.0)
        cost = float(t.get("cost") or 0.0)
        ts = (t.get("created_at") or "")[:16]
        open_rows += f"""
        <tr>
          <td>{ts}</td>
          <td>{t.get('outcome','?')}</td>
          <td>${ep:.2f}</td>
          <td>{sz:.0f}</td>
          <td>${cost:.2f}</td>
          <td>{q}</td>
        </tr>"""

    if not open_trades:
        open_rows = '<tr><td colspan="6" style="color:#666">No open positions</td></tr>'

    # Build last 20 closed trades
    closed_rows = ""
    for t in closed[:20]:
        q = (t.get("market_question") or "")[:55]
        ep = float(t.get("entry_price") or 0.0)
        xp = float(t.get("exit_price") or 0.0)
        ts = (t.get("closed_at") or t.get("created_at") or "")[:16]
        tag = "WIN" if t["pnl"] > 0 else "LOSS"
        tag_color = "#00e676" if t["pnl"] > 0 else "#ff5252"
        closed_rows += f"""
        <tr style="background:{row_bg(t['pnl'])}">
          <td>{ts}</td>
          <td style="color:{tag_color};font-weight:bold">{tag}</td>
          <td>${ep:.2f} → ${xp:.2f}</td>
          <td style="color:{color(t['pnl'])};font-weight:bold">{fmt(t['pnl'])}</td>
          <td>{q}</td>
        </tr>"""

    if not closed:
        closed_rows = '<tr><td colspan="5" style="color:#666">No closed trades yet</td></tr>'

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="5">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Polybot Dashboard</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #0a0a0a; color: #e0e0e0; font-family: monospace; font-size: 14px; padding: 16px; }}
    h2 {{ color: #fff; margin-bottom: 8px; font-size: 16px; letter-spacing: 1px; }}
    .card {{ background: #141414; border: 1px solid #222; border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
    .stat-row {{ display: flex; flex-wrap: wrap; gap: 24px; margin-bottom: 8px; }}
    .stat {{ display: flex; flex-direction: column; }}
    .stat-label {{ color: #666; font-size: 11px; text-transform: uppercase; }}
    .stat-value {{ font-size: 22px; font-weight: bold; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th {{ color: #666; text-align: left; padding: 4px 8px; font-size: 11px; text-transform: uppercase; border-bottom: 1px solid #222; }}
    td {{ padding: 6px 8px; border-bottom: 1px solid #1a1a1a; font-size: 13px; word-break: break-word; }}
    .ts {{ color: #888; font-size: 11px; }}
    .refresh {{ color: #444; font-size: 11px; text-align: right; margin-top: 8px; }}
  </style>
</head>
<body>

<div class="card">
  <h2>POLYBOT DASHBOARD</h2>
  <div class="stat-row" style="margin-top:12px">
    <div class="stat">
      <span class="stat-label">Today P&L</span>
      <span class="stat-value" style="color:{color(today_pnl)}">{fmt(today_pnl)}</span>
    </div>
    <div class="stat">
      <span class="stat-label">All-Time P&L</span>
      <span class="stat-value" style="color:{color(total_pnl)}">{fmt(total_pnl)}</span>
    </div>
    <div class="stat">
      <span class="stat-label">Win Rate</span>
      <span class="stat-value">{win_rate:.1f}%</span>
    </div>
    <div class="stat">
      <span class="stat-label">Wins / Losses</span>
      <span class="stat-value"><span style="color:#00e676">{wins}W</span> / <span style="color:#ff5252">{losses}L</span></span>
    </div>
    <div class="stat">
      <span class="stat-label">Today</span>
      <span class="stat-value"><span style="color:#00e676">{today_wins}W</span> / <span style="color:#ff5252">{today_losses}L</span></span>
    </div>
  </div>
</div>

<div class="card">
  <h2>OPEN POSITIONS ({len(open_trades)}) &nbsp; At Risk: ${open_cost:.2f}</h2>
  <table style="margin-top:10px">
    <tr><th>Time</th><th>Side</th><th>Entry</th><th>Shares</th><th>Cost</th><th>Market</th></tr>
    {open_rows}
  </table>
</div>

<div class="card">
  <h2>LAST 20 CLOSED</h2>
  <table style="margin-top:10px">
    <tr><th>Time</th><th>Result</th><th>Price</th><th>P&L</th><th>Market</th></tr>
    {closed_rows}
  </table>
</div>

<div class="refresh">Updated: {now} &nbsp;·&nbsp; Auto-refresh every 5s</div>
</body>
</html>"""

    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    print("Dashboard running at http://178.128.229.98:8080")
    app.run(host="0.0.0.0", port=8080, debug=False)
