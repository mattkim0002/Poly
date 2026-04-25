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
        "SELECT id, market_question, outcome, entry_price, size, cost, "
        "exit_price, pnl, status, created_at, closed_at FROM trades ORDER BY created_at DESC"
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
    if not os.path.exists(PAPER_PATH):
        return None
    try:
        with open(PAPER_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def load_config_flags() -> dict:
    try:
        import config
        return {
            "paper": bool(getattr(config, "PAPER_TRADING", False)),
            "max_positions": int(getattr(config, "MAX_OPEN_POSITIONS", 0)),
            "arb": bool(getattr(config, "ENABLE_ARB", False)),
            "endgame": bool(getattr(config, "ENABLE_SNIPE", False)),
            "lag": bool(getattr(config, "ENABLE_BINANCE_LAG", False)),
            "momentum": bool(getattr(config, "ENABLE_MOMENTUM_CLAUDE", False)),
        }
    except Exception:
        return {"paper": False, "max_positions": 0, "arb": False, "endgame": False,
                "lag": False, "momentum": False}


def load_macro_from_log() -> dict | None:
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


def c(val: float) -> str:
    if val > 0: return "#00e676"
    if val < 0: return "#ff5252"
    return "#aaa"


def f(val: float) -> str:
    return f"{'+' if val > 0 else ''}${val:.2f}"


@app.route("/log")
def log_raw():
    return Response(tail_log(), mimetype="text/plain")


@app.route("/markets")
def markets_api():
    try:
        from core import yfinance_data
        snap = yfinance_data.get_market_snapshot()
        return Response(json.dumps(snap), mimetype="application/json")
    except Exception:
        return Response("{}", mimetype="application/json")


@app.route("/")
def index():
    trades = load_trades()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    today = datetime.utcnow().strftime("%Y-%m-%d")
    paper = load_paper_state()
    flags = load_config_flags()
    macro = load_macro_from_log()

    open_t = [t for t in trades if t.get("status") == "open"]
    closed = [t for t in trades if t.get("status") in ("won", "lost")]
    today_c = [t for t in closed if (t.get("created_at") or "").startswith(today)]

    total_pnl = sum(t["pnl"] for t in closed)
    today_pnl = sum(t["pnl"] for t in today_c)
    wins = sum(1 for t in closed if t["pnl"] > 0)
    losses = sum(1 for t in closed if t["pnl"] < 0)
    wr = (wins / len(closed) * 100) if closed else 0.0
    tw = sum(1 for t in today_c if t["pnl"] > 0)
    tl = sum(1 for t in today_c if t["pnl"] < 0)
    open_cost = sum(float(t.get("cost") or 0) for t in open_t)

    # Strategy P&L breakdown
    strat_pnl = {"ARB": 0.0, "ENDGAME": 0.0, "LAG": 0.0, "MOMENTUM": 0.0, "OTHER": 0.0}
    strat_count = {"ARB": 0, "ENDGAME": 0, "LAG": 0, "MOMENTUM": 0, "OTHER": 0}
    for t in closed:
        q = (t.get("market_question") or "").upper()
        if "[ARB" in q:
            k = "ARB"
        elif "[ENDGAME]" in q:
            k = "ENDGAME"
        elif "[LAG]" in q:
            k = "LAG"
        else:
            k = "MOMENTUM"
        strat_pnl[k] += t["pnl"]
        strat_count[k] += 1

    # Mode
    mode = "PAPER" if flags["paper"] else "LIVE"
    mc = "#ffb74d" if flags["paper"] else "#00e676"

    # Strategies
    strats = []
    for name, on in [("ARB", flags["arb"]), ("ENDGAME", flags["endgame"]),
                     ("LAG", flags["lag"]), ("MOMENTUM", flags["momentum"])]:
        sc = "#00e676" if on else "#333"
        strats.append(f'<span style="color:{sc};font-weight:bold">{name}</span>')

    # Open rows
    open_rows = ""
    for t in open_t[:10]:
        q = (t.get("market_question") or "")[:65]
        ep = float(t.get("entry_price") or 0)
        sz = float(t.get("size") or 0)
        cost = float(t.get("cost") or 0)
        ts = (t.get("created_at") or "")[:16]
        open_rows += f'<tr><td>{ts}</td><td>{t.get("outcome","?")}</td><td>${ep:.2f}</td><td>{sz:.0f}</td><td>${cost:.2f}</td><td>{q}</td></tr>'
    if not open_t:
        open_rows = '<tr><td colspan="6" style="color:#555;text-align:center;padding:20px">No open positions</td></tr>'

    # Closed rows
    closed_rows = ""
    for t in closed[:25]:
        q = (t.get("market_question") or "")[:60]
        ep = float(t.get("entry_price") or 0)
        xp = float(t.get("exit_price") or 0)
        ts = (t.get("closed_at") or t.get("created_at") or "")[:16]
        tag = "WIN" if t["pnl"] > 0 else "LOSS"
        tc = "#00e676" if t["pnl"] > 0 else "#ff5252"
        bg = "rgba(0,230,118,0.06)" if t["pnl"] > 0 else "rgba(255,82,82,0.06)"
        closed_rows += f'<tr style="background:{bg}"><td>{ts}</td><td style="color:{tc};font-weight:bold">{tag}</td><td>${ep:.2f} → ${xp:.2f}</td><td style="color:{c(t["pnl"])};font-weight:bold">{f(t["pnl"])}</td><td>{q}</td></tr>'
    if not closed:
        closed_rows = '<tr><td colspan="5" style="color:#555;text-align:center;padding:20px">No closed trades yet</td></tr>'

    # Strategy breakdown rows
    strat_rows = ""
    for k in ["ENDGAME", "ARB", "LAG", "MOMENTUM"]:
        if strat_count[k] == 0:
            continue
        strat_rows += f'<tr><td style="font-weight:bold">{k}</td><td>{strat_count[k]}</td><td style="color:{c(strat_pnl[k])};font-weight:bold">{f(strat_pnl[k])}</td></tr>'

    # Paper card
    paper_card = ""
    if paper:
        bal = float(paper.get("balance", 0))
        pk = float(paper.get("peak_balance", 0))
        pnlp = float(paper.get("total_pnl", 0))
        pw = int(paper.get("wins", 0))
        pl = int(paper.get("losses", 0))
        pt = int(paper.get("total_trades", 0))
        op = paper.get("open_positions", [])
        ov = sum(float(p.get("entry_price", 0)) * float(p.get("size", 0)) for p in op)
        eq = bal + ov
        pwr = (pw / max(pt, 1)) * 100
        paper_card = f"""
<div class="card">
  <h2>PAPER ACCOUNT</h2>
  <div class="stat-row">
    <div class="stat"><span class="stat-label">Cash</span><span class="stat-value">${bal:,.2f}</span></div>
    <div class="stat"><span class="stat-label">Equity</span><span class="stat-value">${eq:,.2f}</span></div>
    <div class="stat"><span class="stat-label">Peak</span><span class="stat-value">${pk:,.2f}</span></div>
    <div class="stat"><span class="stat-label">P&L</span><span class="stat-value" style="color:{c(pnlp)}">{f(pnlp)}</span></div>
    <div class="stat"><span class="stat-label">Record</span><span class="stat-value"><span style="color:#00e676">{pw}W</span> / <span style="color:#ff5252">{pl}L</span> ({pwr:.0f}%)</span></div>
    <div class="stat"><span class="stat-label">Positions</span><span class="stat-value">{len(op)} / {flags['max_positions']}</span></div>
  </div>
</div>"""

    # Markets card
    trad_card = ""
    try:
        from core import yfinance_data
        snap = yfinance_data.get_market_snapshot()
        risk = yfinance_data.get_risk_off_signal()
        if snap:
            tp = ""
            for label in ["SPY", "VIX", "DXY", "GOLD", "BTC_YF"]:
                s = snap.get(label)
                if not s: continue
                vc = c(s["change_pct"]) if label != "VIX" else ("#ff5252" if s["change_pct"] > 5 else "#00e676" if s["change_pct"] < -5 else "#aaa")
                tp += f'<div class="stat"><span class="stat-label">{label}</span><span class="stat-value">${s["price"]:,.0f}</span><span style="color:{vc};font-size:13px">{s["change_pct"]:+.1f}%</span></div>'
            rb = ""
            if risk.get("risk_off"):
                rb = ' <span style="color:#ff5252;font-size:12px">⚠ RISK-OFF</span>'
            trad_card = f'<div class="card"><h2>MARKETS{rb}</h2><div class="stat-row">{tp}</div></div>'
    except Exception:
        pass

    # Macro card
    macro_card = ""
    if macro:
        bias = macro.get("bias", "neutral")
        conf = macro.get("confidence", "low")
        reason = macro.get("reasoning", "")
        bc = {"bullish": "#00e676", "bearish": "#ff5252"}.get(bias, "#ffb74d")
        macro_card = f"""
<div class="card">
  <h2>MACRO BIAS</h2>
  <div class="stat-row">
    <div class="stat"><span class="stat-label">Bias</span><span class="stat-value" style="color:{bc};text-transform:uppercase">{bias}</span></div>
    <div class="stat"><span class="stat-label">Confidence</span><span class="stat-value" style="text-transform:uppercase">{conf}</span></div>
  </div>
  <div style="color:#888;margin-top:6px;font-size:12px">{reason}</div>
</div>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polybot</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0a0a0a;color:#e0e0e0;font-family:'SF Mono',Monaco,Consolas,monospace;font-size:13px;padding:20px;max-width:1200px;margin:0 auto}}
h2{{color:#fff;margin-bottom:10px;font-size:14px;letter-spacing:1.5px;text-transform:uppercase}}
.card{{background:#111;border:1px solid #1a1a1a;border-radius:10px;padding:18px;margin-bottom:14px}}
.stat-row{{display:flex;flex-wrap:wrap;gap:28px;margin-top:10px}}
.stat{{display:flex;flex-direction:column;gap:2px}}
.stat-label{{color:#555;font-size:10px;text-transform:uppercase;letter-spacing:1px}}
.stat-value{{font-size:20px;font-weight:bold}}
.header{{display:flex;justify-content:space-between;align-items:center}}
.mode{{font-size:12px;padding:3px 10px;border-radius:12px;font-weight:bold}}
table{{width:100%;border-collapse:collapse;margin-top:10px}}
th{{color:#444;text-align:left;padding:6px 8px;font-size:10px;text-transform:uppercase;letter-spacing:1px;border-bottom:1px solid #1a1a1a}}
td{{padding:7px 8px;border-bottom:1px solid #111;font-size:12px}}
pre#log{{background:#080808;color:#7ec87e;padding:14px;border-radius:8px;max-height:400px;overflow-y:auto;white-space:pre-wrap;word-break:break-word;font-size:11px;line-height:1.5;border:1px solid #1a1a1a}}
.footer{{color:#333;font-size:10px;text-align:right;margin-top:10px}}
</style>
</head>
<body>

<div class="card">
  <div class="header">
    <h2>POLYBOT DASHBOARD</h2>
    <span class="mode" style="background:{mc};color:#000">{mode}</span>
  </div>
  <div class="stat-row">
    <div class="stat"><span class="stat-label">Today</span><span class="stat-value" style="color:{c(today_pnl)}">{f(today_pnl)}</span></div>
    <div class="stat"><span class="stat-label">All-Time</span><span class="stat-value" style="color:{c(total_pnl)}">{f(total_pnl)}</span></div>
    <div class="stat"><span class="stat-label">Win Rate</span><span class="stat-value">{wr:.0f}%</span></div>
    <div class="stat"><span class="stat-label">Record</span><span class="stat-value"><span style="color:#00e676">{wins}W</span> / <span style="color:#ff5252">{losses}L</span></span></div>
    <div class="stat"><span class="stat-label">Today</span><span class="stat-value"><span style="color:#00e676">{tw}W</span> / <span style="color:#ff5252">{tl}L</span></span></div>
    <div class="stat"><span class="stat-label">Strategies</span><span class="stat-value" style="font-size:14px">{" · ".join(strats)}</span></div>
  </div>
</div>

{paper_card}
{trad_card}
{macro_card}

<div class="card" style="display:flex;gap:20px;flex-wrap:wrap">
  <div style="flex:1;min-width:280px">
    <h2>OPEN ({len(open_t)}) &nbsp; <span style="color:#888;font-size:12px;font-weight:normal">At risk: ${open_cost:.2f}</span></h2>
    <table><tr><th>Time</th><th>Side</th><th>Entry</th><th>Qty</th><th>Cost</th><th>Market</th></tr>{open_rows}</table>
  </div>
</div>

<div class="card" style="display:flex;gap:20px;flex-wrap:wrap">
  <div style="flex:2;min-width:400px">
    <h2>RECENT TRADES</h2>
    <table><tr><th>Time</th><th>Result</th><th>Price</th><th>P&L</th><th>Market</th></tr>{closed_rows}</table>
  </div>
  <div style="flex:1;min-width:200px">
    <h2>BY STRATEGY</h2>
    <table><tr><th>Strategy</th><th>Trades</th><th>P&L</th></tr>{strat_rows}</table>
  </div>
</div>

<div class="card">
  <h2>BOT LOG</h2>
  <pre id="log">loading...</pre>
</div>

<div class="footer">Updated: <span id="now">{now}</span> · Auto-refresh 5s</div>

<script>
async function refreshLog(){{try{{const r=await fetch('/log');const t=await r.text();const el=document.getElementById('log');const b=el.scrollTop+el.clientHeight>=el.scrollHeight-20;el.textContent=t;if(b)el.scrollTop=el.scrollHeight;document.getElementById('now').textContent=new Date().toISOString().replace('T',' ').slice(0,19)+' UTC'}}catch(e){{}}}}
refreshLog();setInterval(refreshLog,2000);setInterval(()=>location.reload(),5000);
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    print("Dashboard running at http://178.128.229.98:8080")
    app.run(host="0.0.0.0", port=8080, debug=False)
