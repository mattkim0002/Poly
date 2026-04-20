"""Web dashboard — live P&L viewer at http://<droplet>:8080

Run on the droplet:
    python3 tools/dashboard.py

Auto-refreshes every 5 seconds. Read-only. No DB writes.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime
from flask import Flask, Response

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "polybot.db")
LOG_PATH = os.path.join(ROOT, "bot.log")
PAPER_PATH = os.path.join(ROOT, "paper_trades.json")
LOG_TAIL_LINES = 80

sys.path.insert(0, ROOT)

app = Flask(__name__)


def tail_log(n: int = LOG_TAIL_LINES) -> str:
    """Return the last n lines of bot.log, or a notice if missing."""
    if not os.path.exists(LOG_PATH):
        return "(bot.log not found — is the bot running?)"
    try:
        with open(LOG_PATH, "rb") as f:
            try:
                f.seek(-16384, os.SEEK_END)
            except OSError:
                f.seek(0)
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()[-n:]
        return "\n".join(lines) if lines else "(empty log)"
    except Exception as e:
        return f"(error reading log: {e})"


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


def load_paper_state() -> dict | None:
    """Return paper_trades.json contents or None if missing."""
    if not os.path.exists(PAPER_PATH):
        return None
    try:
        with open(PAPER_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def load_config_flags() -> dict:
    """Pull live config flags without importing live trader side effects."""
    try:
        import config
        return {
            "paper": bool(getattr(config, "PAPER_TRADING", False)),
            "max_positions": int(getattr(config, "MAX_OPEN_POSITIONS", 0)),
            "arb": bool(getattr(config, "ENABLE_ARB", False)),
            "snipe": bool(getattr(config, "ENABLE_SNIPE", False)),
            "lag": bool(getattr(config, "ENABLE_BINANCE_LAG", False)),
            "momentum": bool(getattr(config, "ENABLE_MOMENTUM_CLAUDE", False)),
            "threshold": bool(getattr(config, "ENABLE_THRESHOLD", False)),
        }
    except Exception:
        return {"paper": False, "max_positions": 0, "arb": False, "snipe": False,
                "lag": False, "momentum": False, "threshold": False}


def load_macro_from_log() -> dict | None:
    """Parse the most recent [MACRO] line from bot.log."""
    if not os.path.exists(LOG_PATH):
        return None
    try:
        with open(LOG_PATH, "rb") as f:
            try:
                f.seek(-65536, os.SEEK_END)
            except OSError:
                f.seek(0)
            data = f.read().decode("utf-8", errors="replace")
        for line in reversed(data.splitlines()):
            if "[MACRO]" in line and "bias" in line.lower():
                start = line.find("{")
                end = line.rfind("}")
                if start != -1 and end > start:
                    try:
                        return json.loads(line[start:end + 1])
                    except Exception:
                        continue
    except Exception:
        return None
    return None


def color(val: float) -> str:
    if val > 0:
        return "#00e676"
    if val < 0:
        return "#ff5252"
    return "#aaa"


def fmt(val: float) -> str:
    sign = "+" if val > 0 else ""
    return f"{sign}${val:.2f}"


@app.route("/log")
def log_raw():
    """Raw log tail as plain text — polled by the dashboard."""
    return Response(tail_log(), mimetype="text/plain")


@app.route("/")
def index():
    trades = load_trades()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    today = datetime.utcnow().strftime("%Y-%m-%d")

    paper_state = load_paper_state()
    flags = load_config_flags()
    macro = load_macro_from_log()

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

    # Mode banner
    mode_label = "PAPER" if flags["paper"] else "LIVE"
    mode_color = "#ffb74d" if flags["paper"] else "#00e676"

    # Strategies enabled
    strat_parts = []
    for name, on in [("ARB", flags["arb"]), ("SNIPE", flags["snipe"]),
                     ("LAG", flags["lag"]), ("MOMENTUM", flags["momentum"]),
                     ("THRESHOLD", flags["threshold"])]:
        c = "#00e676" if on else "#444"
        strat_parts.append(f'<span style="color:{c}">{name}</span>')
    strat_html = " · ".join(strat_parts)

    # Paper card (only if paper state exists)
    paper_card = ""
    if paper_state:
        bal = float(paper_state.get("balance", 0.0))
        peak = float(paper_state.get("peak_balance", 0.0))
        total_pnl_p = float(paper_state.get("total_pnl", 0.0))
        tot = int(paper_state.get("total_trades", 0))
        w = int(paper_state.get("wins", 0))
        l = int(paper_state.get("losses", 0))
        open_val = sum(float(p.get("entry_price", 0)) * float(p.get("size", 0))
                       for p in paper_state.get("open_positions", []))
        equity = bal + open_val
        wr = (w / max(tot, 1)) * 100
        paper_card = f"""
<div class="card">
  <h2>PAPER STATE &nbsp; <span style="color:{mode_color};font-size:13px">● {mode_label}</span></h2>
  <div class="stat-row" style="margin-top:12px">
    <div class="stat">
      <span class="stat-label">Cash</span>
      <span class="stat-value">${bal:.2f}</span>
    </div>
    <div class="stat">
      <span class="stat-label">Equity</span>
      <span class="stat-value">${equity:.2f}</span>
    </div>
    <div class="stat">
      <span class="stat-label">Peak</span>
      <span class="stat-value">${peak:.2f}</span>
    </div>
    <div class="stat">
      <span class="stat-label">Session P&L</span>
      <span class="stat-value" style="color:{color(total_pnl_p)}">{fmt(total_pnl_p)}</span>
    </div>
    <div class="stat">
      <span class="stat-label">Record</span>
      <span class="stat-value"><span style="color:#00e676">{w}W</span>/<span style="color:#ff5252">{l}L</span> ({wr:.0f}%)</span>
    </div>
    <div class="stat">
      <span class="stat-label">Open / Max</span>
      <span class="stat-value">{len(paper_state.get('open_positions', []))} / {flags['max_positions']}</span>
    </div>
  </div>
</div>"""

    # Macro card
    macro_card = ""
    if macro:
        bias = macro.get("bias", "neutral")
        conf = macro.get("confidence", "low")
        btc_leads = macro.get("btc_leads_alts", False)
        reasoning = macro.get("reasoning", "")
        bias_color = {"bullish": "#00e676", "bearish": "#ff5252"}.get(bias, "#ffb74d")
        macro_card = f"""
<div class="card">
  <h2>MACRO BIAS</h2>
  <div class="stat-row" style="margin-top:12px">
    <div class="stat">
      <span class="stat-label">Bias</span>
      <span class="stat-value" style="color:{bias_color};text-transform:uppercase">{bias}</span>
    </div>
    <div class="stat">
      <span class="stat-label">Confidence</span>
      <span class="stat-value" style="text-transform:uppercase">{conf}</span>
    </div>
    <div class="stat">
      <span class="stat-label">BTC Leads Alts</span>
      <span class="stat-value">{'YES' if btc_leads else 'NO'}</span>
    </div>
  </div>
  <div style="color:#aaa;margin-top:8px;font-size:13px">{reasoning}</div>
</div>"""

    strategies_card = f"""
<div class="card">
  <h2>STRATEGIES</h2>
  <div style="margin-top:10px;font-size:15px;letter-spacing:1px">{strat_html}</div>
</div>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
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
    pre#log {{
      background: #000; color: #9be89b; padding: 12px; border-radius: 6px;
      max-height: 480px; overflow-y: auto; white-space: pre-wrap; word-break: break-word;
      font-size: 12px; line-height: 1.4; border: 1px solid #222;
    }}
  </style>
</head>
<body>

<div class="card">
  <h2>POLYBOT DASHBOARD &nbsp; <span style="color:{mode_color};font-size:13px">● {mode_label}</span></h2>
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

{paper_card}
{macro_card}
{strategies_card}

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

<div class="card">
  <h2>LIVE BOT LOG</h2>
  <pre id="log">loading...</pre>
</div>

<div class="refresh">Updated: <span id="now">{now}</span> &nbsp;·&nbsp; Log refreshes every 2s, stats every 10s</div>

<script>
async function refreshLog() {{
  try {{
    const r = await fetch('/log');
    const t = await r.text();
    const el = document.getElementById('log');
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
    el.textContent = t;
    if (atBottom) el.scrollTop = el.scrollHeight;
    document.getElementById('now').textContent = new Date().toISOString().replace('T',' ').slice(0,19) + ' UTC';
  }} catch (e) {{}}
}}
refreshLog();
setInterval(refreshLog, 2000);
setInterval(() => location.reload(), 10000);
</script>
</body>
</html>"""

    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    print("Dashboard running at http://178.128.229.98:8080")
    app.run(host="0.0.0.0", port=8080, debug=False)
