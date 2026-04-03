"""Flask dashboard for the Polymarket trading bot — runs on port 8080."""

import sqlite3
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, request

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "polybot.db")
WALLET_ADDRESS = "0xa1A623585f0D860c3156c8d2b6ADFFc066922c69"

app = Flask(__name__)


def get_db():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_live_positions():
    try:
        import httpx
        resp = httpx.get(
            "https://data-api.polymarket.com/positions",
            params={"user": WALLET_ADDRESS},
            timeout=15,
        )
        resp.raise_for_status()
        return [p for p in resp.json() if float(p.get("size", 0)) > 0]
    except Exception as e:
        print(f"[dashboard] Failed to fetch positions: {e}")
        return []


def get_learning_stats():
    """Get performance stats by category from the DB."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT market_question, entry_price, market_probability, pnl, status "
            "FROM trades WHERE status IN ('won','lost') ORDER BY closed_at DESC LIMIT 100"
        ).fetchall()
    finally:
        conn.close()

    # Import category classification
    try:
        from strategies.learner import classify_market, classify_price_range
    except ImportError:
        return {"categories": {}, "overall": {}}

    categories = {}
    overall = {"wins": 0, "losses": 0, "total_pnl": 0.0}

    for row in rows:
        row = dict(row)
        question = row.get("market_question", "")
        pnl = row.get("pnl", 0) or 0
        status = row.get("status", "")
        is_win = status == "won" or pnl > 0
        cat = classify_market(question)

        if cat not in categories:
            categories[cat] = {"wins": 0, "losses": 0, "total_pnl": 0.0, "trades": 0}
        categories[cat]["trades"] += 1
        categories[cat]["total_pnl"] += pnl
        if is_win:
            categories[cat]["wins"] += 1
            overall["wins"] += 1
        else:
            categories[cat]["losses"] += 1
            overall["losses"] += 1
        overall["total_pnl"] += pnl

    # Derived stats
    for cat, data in categories.items():
        total = data["wins"] + data["losses"]
        data["win_rate"] = (data["wins"] / total * 100) if total > 0 else 0
        data["avg_pnl"] = data["total_pnl"] / total if total > 0 else 0
        # Status label
        if data["win_rate"] == 0 and total >= 3:
            data["label"] = "SKIPPING"
        elif data["win_rate"] < 25 and total >= 5:
            data["label"] = "SKIPPING"
        elif data["win_rate"] >= 65 and data["total_pnl"] > 0:
            data["label"] = "BOOSTED"
        elif data["win_rate"] < 40 and total >= 3:
            data["label"] = "HALVED"
        else:
            data["label"] = "NORMAL"

    total = overall["wins"] + overall["losses"]
    overall["total_trades"] = total
    overall["win_rate"] = (overall["wins"] / total * 100) if total > 0 else 0

    return {"categories": categories, "overall": overall}


@app.route("/api/data")
def api_data():
    conn = get_db()
    try:
        cur = conn.cursor()

        # Latest equity snapshot
        cur.execute(
            "SELECT balance, total_equity, peak_equity, drawdown "
            "FROM equity_snapshots ORDER BY id DESC LIMIT 1"
        )
        snap = cur.fetchone()
        balance = snap["balance"] if snap else 0
        peak_equity = snap["peak_equity"] if snap else 0
        drawdown_val = snap["drawdown"] if snap else 0

        # Trade stats
        cur.execute("SELECT COUNT(*) as cnt FROM trades WHERE status != 'cancelled'")
        total_trades = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) as cnt FROM trades WHERE status = 'won'")
        wins = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) as cnt FROM trades WHERE status IN ('won','lost')")
        closed_decided = cur.fetchone()["cnt"]
        win_rate = (wins / closed_decided * 100) if closed_decided > 0 else 0

        cur.execute("SELECT COALESCE(SUM(pnl), 0) as total_pnl FROM trades WHERE status IN ('won','lost')")
        total_pnl = cur.fetchone()["total_pnl"]

        # Today's P&L
        cur.execute(
            "SELECT COALESCE(SUM(pnl), 0) as today_pnl FROM trades "
            "WHERE status IN ('won','lost') AND date(closed_at) = date('now')"
        )
        today_pnl = cur.fetchone()["today_pnl"]

        # Best & worst trade
        cur.execute("SELECT MAX(pnl) as best FROM trades WHERE status IN ('won','lost')")
        best_trade = cur.fetchone()["best"] or 0
        cur.execute("SELECT MIN(pnl) as worst FROM trades WHERE status IN ('won','lost')")
        worst_trade = cur.fetchone()["worst"] or 0

        # Live positions
        live_positions = fetch_live_positions()
        positions_value = sum(float(p.get("currentValue", 0)) for p in live_positions)
        total_equity = balance + positions_value

        open_positions = []
        for p in live_positions:
            size = float(p.get("size", 0))
            cur_val = float(p.get("currentValue", 0))
            initial_val = float(p.get("initialValue", 0))
            entry_price = initial_val / size if size > 0 else 0
            cur_price = cur_val / size if size > 0 else 0
            unrealized_pnl = cur_val - initial_val
            pnl_pct = (unrealized_pnl / initial_val * 100) if initial_val > 0 else 0

            open_positions.append({
                "title": p.get("title", p.get("market", "Unknown")),
                "outcome": p.get("outcome", "?"),
                "side": "BUY",
                "entry_price": round(entry_price, 4),
                "current_price": round(cur_price, 4),
                "size": round(size, 2),
                "cost": round(initial_val, 2),
                "current_value": round(cur_val, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "pnl_pct": round(pnl_pct, 1),
                "token_id": p.get("asset", ""),
                "condition_id": p.get("conditionId", ""),
            })

        # Recent closed trades
        cur.execute(
            "SELECT market_question, outcome, side, entry_price, exit_price, "
            "size, cost, pnl, r_multiple, status, created_at, closed_at "
            "FROM trades WHERE status IN ('won','lost','cancelled') "
            "ORDER BY closed_at DESC LIMIT 50"
        )
        closed_trades = [dict(r) for r in cur.fetchall()]

        # Equity curve
        cur.execute(
            "SELECT timestamp, total_equity, balance, positions_value, drawdown "
            "FROM equity_snapshots ORDER BY id ASC"
        )
        equity_curve = [dict(r) for r in cur.fetchall()]

        # Learning stats
        learning = get_learning_stats()

        # Bot status — check if equity snapshot is recent (within 2 minutes)
        cur.execute("SELECT timestamp FROM equity_snapshots ORDER BY id DESC LIMIT 1")
        last_snap = cur.fetchone()
        bot_active = False
        last_update = None
        if last_snap:
            last_update = last_snap["timestamp"]
            try:
                from datetime import datetime, timezone
                # SQLite timestamps
                ts = datetime.fromisoformat(last_update.replace("Z", "+00:00")) if last_update else None
                if ts:
                    age = (datetime.now(timezone.utc) - ts.replace(tzinfo=timezone.utc)).total_seconds()
                    bot_active = age < 120
            except Exception:
                bot_active = False

        return jsonify({
            "balance": balance,
            "total_equity": total_equity,
            "positions_value": positions_value,
            "peak_equity": peak_equity,
            "drawdown": drawdown_val,
            "total_pnl": total_pnl,
            "today_pnl": today_pnl,
            "best_trade": best_trade,
            "worst_trade": worst_trade,
            "win_rate": win_rate,
            "total_trades": total_trades,
            "wins": wins,
            "closed_decided": closed_decided,
            "open_positions": open_positions,
            "closed_trades": closed_trades,
            "equity_curve": equity_curve,
            "learning": learning,
            "bot_active": bot_active,
            "last_update": last_update,
        })
    finally:
        conn.close()


@app.route("/api/sell", methods=["POST"])
def api_sell():
    data = request.json
    token_id = data.get("token_id")
    size = float(data.get("size", 0))
    price = float(data.get("price", 0))

    if not token_id or size <= 0 or price <= 0:
        return jsonify({"error": "Missing token_id, size, or price"}), 400

    try:
        from core.trader import place_limit_order, get_client
        # Cancel existing orders first to free balance
        try:
            get_client().cancel_all()
        except Exception:
            pass
        order_id = place_limit_order(token_id=token_id, price=price, size=size, side="SELL")
        if order_id:
            return jsonify({"success": True, "order_id": order_id})
        else:
            return jsonify({"error": "Order failed — check bot log"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sell_all", methods=["POST"])
def api_sell_all():
    """Sell all open positions."""
    positions = fetch_live_positions()
    if not positions:
        return jsonify({"error": "No open positions"}), 400

    try:
        from core.trader import place_limit_order, get_client
        try:
            get_client().cancel_all()
        except Exception:
            pass

        results = []
        for p in positions:
            token_id = p.get("asset", "")
            size = float(p.get("size", 0))
            cur_val = float(p.get("currentValue", 0))
            cur_price = cur_val / size if size > 0 else 0.5
            sell_price = max(0.01, min(0.99, round(cur_price - 0.02, 2)))

            order_id = place_limit_order(token_id=token_id, price=sell_price, size=size, side="SELL")
            results.append({
                "title": p.get("title", "?")[:40],
                "order_id": order_id,
                "success": order_id is not None,
            })

        return jsonify({"success": True, "results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cancel_all", methods=["POST"])
def api_cancel_all():
    try:
        from core.trader import get_client
        result = get_client().cancel_all()
        return jsonify({"success": True, "result": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polybot Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0d1117; color: #c9d1d9; padding: 20px; max-width: 1200px; margin: 0 auto;
  }
  .header { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
  h1 { color: #58a6ff; font-size: 1.5rem; }
  .bot-status {
    padding: 4px 12px; border-radius: 12px; font-size: 0.75rem;
    font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px;
  }
  .bot-online { background: #0f2d1a; color: #3fb950; border: 1px solid #3fb950; }
  .bot-offline { background: #2d0f0f; color: #f85149; border: 1px solid #f85149; }
  .refresh-note { color: #484f58; font-size: 0.72rem; margin-bottom: 16px; }

  .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; margin-bottom: 20px; }
  .card {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 14px 16px;
  }
  .card .label { font-size: 0.7rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
  .card .value { font-size: 1.5rem; font-weight: 700; margin-top: 4px; }
  .card .sub { font-size: 0.7rem; color: #8b949e; margin-top: 2px; }
  .positive { color: #3fb950; }
  .negative { color: #f85149; }
  .neutral { color: #c9d1d9; }

  .dd-bar-container {
    background: #21262d; border-radius: 4px; height: 8px; margin-top: 8px; overflow: hidden;
  }
  .dd-bar {
    height: 100%; border-radius: 4px; transition: width 0.5s;
  }
  .dd-safe { background: #3fb950; }
  .dd-warn { background: #d29922; }
  .dd-danger { background: #f85149; }

  .section-title {
    font-size: 1rem; color: #58a6ff; margin: 24px 0 10px;
    border-bottom: 1px solid #21262d; padding-bottom: 6px;
    display: flex; align-items: center; gap: 10px;
  }

  table {
    width: 100%; border-collapse: collapse; font-size: 0.8rem;
    margin-bottom: 16px;
  }
  th {
    text-align: left; padding: 8px; background: #161b22;
    border-bottom: 2px solid #30363d; color: #8b949e;
    font-weight: 600; text-transform: uppercase; font-size: 0.68rem;
  }
  td {
    padding: 7px 8px; border-bottom: 1px solid #21262d;
    max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  tr:hover { background: #161b22; }

  .chart-container {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 16px; margin-bottom: 20px; height: 280px;
  }

  .learning-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 10px; margin-bottom: 20px;
  }
  .learn-card {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 12px 14px;
  }
  .learn-card .cat-name {
    font-size: 0.85rem; font-weight: 700; text-transform: capitalize;
    margin-bottom: 6px;
  }
  .learn-card .stat-row {
    display: flex; justify-content: space-between; font-size: 0.75rem;
    margin-bottom: 3px;
  }
  .learn-label {
    display: inline-block; padding: 1px 6px; border-radius: 8px;
    font-size: 0.65rem; font-weight: 700; margin-left: 6px;
  }
  .label-BOOSTED { background: #0f2d1a; color: #3fb950; }
  .label-HALVED { background: #2d1f0f; color: #d29922; }
  .label-SKIPPING { background: #2d0f0f; color: #f85149; }
  .label-NORMAL { background: #1c1c1c; color: #8b949e; }

  .status-badge {
    padding: 2px 8px; border-radius: 10px; font-size: 0.68rem;
    font-weight: 600; text-transform: uppercase;
  }
  .status-won { background: #0f2d1a; color: #3fb950; }
  .status-lost { background: #2d0f0f; color: #f85149; }
  .status-open { background: #0f1d2d; color: #58a6ff; }
  .status-cancelled { background: #1c1c1c; color: #8b949e; }

  .empty { color: #484f58; font-style: italic; padding: 20px; text-align: center; }

  .btn {
    border: none; border-radius: 6px; padding: 6px 14px;
    font-size: 0.75rem; font-weight: 600; cursor: pointer;
    transition: all 0.2s;
  }
  .btn-sell { background: #f85149; color: #fff; }
  .btn-sell:hover { background: #da3633; }
  .btn-sell:disabled { background: #484f58; cursor: not-allowed; }
  .btn-danger-outline {
    background: transparent; color: #f85149; border: 1px solid #f85149;
  }
  .btn-danger-outline:hover { background: #2d0f0f; }
  .btn-warn {
    background: #d29922; color: #0d1117;
  }
  .btn-warn:hover { background: #e3b341; }

  .btn-group { display: flex; gap: 8px; }

  @media (max-width: 600px) {
    body { padding: 10px; }
    .card .value { font-size: 1.1rem; }
    .cards { grid-template-columns: repeat(2, 1fr); }
    table { font-size: 0.72rem; }
    td, th { padding: 5px 4px; }
  }
</style>
</head>
<body>

<div class="header">
  <h1>Polybot Dashboard</h1>
  <span class="bot-status bot-offline" id="bot-status">OFFLINE</span>
</div>
<div class="refresh-note" id="refresh-note">Loading...</div>

<div class="cards" id="cards">
  <div class="card">
    <div class="label">Cash</div>
    <div class="value neutral" id="balance">--</div>
  </div>
  <div class="card">
    <div class="label">Positions</div>
    <div class="value neutral" id="posval">--</div>
  </div>
  <div class="card">
    <div class="label">Total Equity</div>
    <div class="value" id="equity">--</div>
  </div>
  <div class="card">
    <div class="label">Total P&L</div>
    <div class="value" id="pnl">--</div>
    <div class="sub" id="today-pnl"></div>
  </div>
  <div class="card">
    <div class="label">Win Rate</div>
    <div class="value" id="winrate">--</div>
    <div class="sub" id="win-detail"></div>
  </div>
  <div class="card">
    <div class="label">Drawdown</div>
    <div class="value" id="drawdown">--</div>
    <div class="dd-bar-container"><div class="dd-bar dd-safe" id="dd-bar" style="width:0%"></div></div>
  </div>
  <div class="card">
    <div class="label">Best Trade</div>
    <div class="value positive" id="best-trade">--</div>
  </div>
  <div class="card">
    <div class="label">Worst Trade</div>
    <div class="value negative" id="worst-trade">--</div>
  </div>
</div>

<div class="section-title">
  Live Positions
  <div class="btn-group">
    <button class="btn btn-warn" onclick="sellAll()">SELL ALL</button>
    <button class="btn btn-danger-outline" onclick="cancelAll()">Cancel Orders</button>
  </div>
</div>
<div style="overflow-x:auto">
<table>
  <thead><tr>
    <th>Market</th><th>Outcome</th><th>Entry</th><th>Current</th><th>Size</th><th>Cost</th><th>Value</th><th>P&L</th><th>%</th><th>Action</th>
  </tr></thead>
  <tbody id="open-body"></tbody>
</table>
</div>

<div class="section-title">Equity Curve</div>
<div class="chart-container"><canvas id="equityChart"></canvas></div>

<div class="section-title">Bot Learning (by Category)</div>
<div class="learning-grid" id="learning-grid"></div>

<div class="section-title">Recent Closed Trades</div>
<div style="overflow-x:auto">
<table>
  <thead><tr>
    <th>Market</th><th>Outcome</th><th>Side</th><th>Entry</th><th>Exit</th><th>Cost</th><th>P&L</th><th>R</th><th>Status</th><th>Closed</th>
  </tr></thead>
  <tbody id="closed-body"></tbody>
</table>
</div>

<script>
let chart = null;

function cls(v) { return v > 0 ? 'positive' : v < 0 ? 'negative' : 'neutral'; }
function fmt(v, d=2) { return v != null ? '$' + Number(v).toFixed(d) : '--'; }
function pct(v) { return v != null ? Number(v).toFixed(1) + '%' : '--'; }
function shortQ(q, n=45) { return q && q.length > n ? q.slice(0, n-3) + '...' : (q || '--'); }
function shortDate(d) {
  if (!d) return '--';
  try { return new Date(d).toLocaleDateString('en-US', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}); }
  catch(e) { return d; }
}

function sellPosition(tokenId, size, price, btn) {
  if (!confirm('Sell ' + size + ' shares at $' + price.toFixed(2) + '?')) return;
  btn.disabled = true; btn.textContent = '...';
  fetch('/api/sell', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({token_id: tokenId, size: size, price: price})
  }).then(r => r.json()).then(d => {
    if (d.success) { btn.textContent = 'Done'; btn.style.background = '#3fb950'; setTimeout(refresh, 3000); }
    else { alert('Failed: ' + (d.error||'?')); btn.disabled = false; btn.textContent = 'SELL'; }
  }).catch(e => { alert('Error: ' + e); btn.disabled = false; btn.textContent = 'SELL'; });
}

function sellAll() {
  if (!confirm('SELL ALL open positions?')) return;
  fetch('/api/sell_all', {method: 'POST'}).then(r => r.json()).then(d => {
    if (d.success) {
      let msg = d.results.map(r => r.title + ': ' + (r.success ? 'OK' : 'FAILED')).join('\\n');
      alert('Sell orders placed:\\n' + msg);
      setTimeout(refresh, 3000);
    } else alert('Failed: ' + (d.error||'?'));
  }).catch(e => alert('Error: ' + e));
}

function cancelAll() {
  if (!confirm('Cancel ALL open orders?')) return;
  fetch('/api/cancel_all', {method: 'POST'}).then(r => r.json()).then(d => {
    if (d.success) { alert('All orders cancelled'); refresh(); }
    else alert('Failed: ' + (d.error||'?'));
  }).catch(e => alert('Error: ' + e));
}

function refresh() {
  fetch('/api/data').then(r => r.json()).then(d => {
    // Bot status
    let statusEl = document.getElementById('bot-status');
    if (d.bot_active) {
      statusEl.textContent = 'ONLINE';
      statusEl.className = 'bot-status bot-online';
    } else {
      statusEl.textContent = 'OFFLINE';
      statusEl.className = 'bot-status bot-offline';
    }

    // Cards
    document.getElementById('balance').textContent = fmt(d.balance);
    document.getElementById('posval').textContent = fmt(d.positions_value);

    let eqEl = document.getElementById('equity');
    eqEl.textContent = fmt(d.total_equity);

    let pnlEl = document.getElementById('pnl');
    pnlEl.textContent = (d.total_pnl >= 0 ? '+' : '') + fmt(d.total_pnl);
    pnlEl.className = 'value ' + cls(d.total_pnl);

    document.getElementById('today-pnl').textContent = 'Today: ' + (d.today_pnl >= 0 ? '+' : '') + '$' + Number(d.today_pnl).toFixed(2);
    document.getElementById('today-pnl').className = 'sub ' + cls(d.today_pnl);

    let wrEl = document.getElementById('winrate');
    wrEl.textContent = pct(d.win_rate);
    wrEl.className = 'value ' + (d.win_rate >= 50 ? 'positive' : d.win_rate > 0 ? 'negative' : 'neutral');
    document.getElementById('win-detail').textContent = d.wins + 'W / ' + (d.closed_decided - d.wins) + 'L of ' + d.total_trades + ' total';

    // Drawdown
    let ddPct = (d.drawdown || 0) * 100;
    document.getElementById('drawdown').textContent = ddPct.toFixed(1) + '%';
    document.getElementById('drawdown').className = 'value ' + (ddPct < 20 ? 'positive' : ddPct < 30 ? 'negative' : 'negative');
    let ddBar = document.getElementById('dd-bar');
    ddBar.style.width = Math.min(ddPct * 3.33, 100) + '%'; // 30% = full bar
    ddBar.className = 'dd-bar ' + (ddPct < 20 ? 'dd-safe' : ddPct < 30 ? 'dd-warn' : 'dd-danger');

    document.getElementById('best-trade').textContent = '+' + fmt(d.best_trade);
    document.getElementById('worst-trade').textContent = fmt(d.worst_trade);

    // Open positions
    let ob = document.getElementById('open-body');
    if (d.open_positions.length === 0) {
      ob.innerHTML = '<tr><td colspan="10" class="empty">No open positions</td></tr>';
    } else {
      ob.innerHTML = d.open_positions.map(p => {
        let sellPrice = Math.max(0.01, Math.min(0.99, p.current_price - 0.02));
        return '<tr>' +
          '<td title="' + (p.title||'').replace(/"/g,'&quot;') + '">' + shortQ(p.title) + '</td>' +
          '<td>' + (p.outcome||'--') + '</td>' +
          '<td>' + fmt(p.entry_price,3) + '</td>' +
          '<td>' + fmt(p.current_price,3) + '</td>' +
          '<td>' + p.size + '</td>' +
          '<td>' + fmt(p.cost) + '</td>' +
          '<td>' + fmt(p.current_value) + '</td>' +
          '<td class="' + cls(p.unrealized_pnl) + '">' + (p.unrealized_pnl>=0?'+':'') + fmt(p.unrealized_pnl) + '</td>' +
          '<td class="' + cls(p.pnl_pct) + '">' + (p.pnl_pct>=0?'+':'') + p.pnl_pct + '%</td>' +
          '<td><button class="btn btn-sell" onclick="sellPosition(\'' + p.token_id + '\',' + p.size + ',' + sellPrice + ',this)">SELL</button></td>' +
          '</tr>';
      }).join('');
    }

    // Learning grid
    let lg = document.getElementById('learning-grid');
    let cats = d.learning && d.learning.categories ? d.learning.categories : {};
    let catKeys = Object.keys(cats).sort((a,b) => cats[b].total_pnl - cats[a].total_pnl);
    if (catKeys.length === 0) {
      lg.innerHTML = '<div class="empty">No learning data yet — bot needs completed trades</div>';
    } else {
      lg.innerHTML = catKeys.map(cat => {
        let c = cats[cat];
        return '<div class="learn-card">' +
          '<div class="cat-name">' + cat + '<span class="learn-label label-' + c.label + '">' + c.label + '</span></div>' +
          '<div class="stat-row"><span>Win Rate</span><span class="' + (c.win_rate>=50?'positive':'negative') + '">' + c.win_rate.toFixed(0) + '%</span></div>' +
          '<div class="stat-row"><span>Record</span><span>' + c.wins + 'W / ' + c.losses + 'L</span></div>' +
          '<div class="stat-row"><span>Total P&L</span><span class="' + cls(c.total_pnl) + '">' + (c.total_pnl>=0?'+':'') + '$' + c.total_pnl.toFixed(2) + '</span></div>' +
          '<div class="stat-row"><span>Avg P&L</span><span class="' + cls(c.avg_pnl) + '">' + (c.avg_pnl>=0?'+':'') + '$' + c.avg_pnl.toFixed(2) + '</span></div>' +
          '</div>';
      }).join('');
    }

    // Closed trades
    let cb = document.getElementById('closed-body');
    let closedNonCancelled = d.closed_trades.filter(t => t.status !== 'cancelled');
    if (closedNonCancelled.length === 0) {
      cb.innerHTML = '<tr><td colspan="10" class="empty">No closed trades yet</td></tr>';
    } else {
      cb.innerHTML = closedNonCancelled.map(t => {
        return '<tr>' +
          '<td title="' + (t.market_question||'').replace(/"/g,'&quot;') + '">' + shortQ(t.market_question) + '</td>' +
          '<td>' + (t.outcome||'--') + '</td>' +
          '<td>' + (t.side||'--') + '</td>' +
          '<td>' + fmt(t.entry_price,3) + '</td>' +
          '<td>' + fmt(t.exit_price,3) + '</td>' +
          '<td>' + fmt(t.cost) + '</td>' +
          '<td class="' + cls(t.pnl) + '">' + (t.pnl!=null ? (t.pnl>=0?'+':'') + fmt(t.pnl) : '--') + '</td>' +
          '<td>' + (t.r_multiple != null ? Number(t.r_multiple).toFixed(2) + 'R' : '--') + '</td>' +
          '<td><span class="status-badge status-' + t.status + '">' + t.status + '</span></td>' +
          '<td>' + shortDate(t.closed_at) + '</td>' +
          '</tr>';
      }).join('');
    }

    // Equity chart
    if (d.equity_curve && d.equity_curve.length > 0) {
      let labels = d.equity_curve.map(e => {
        try { return new Date(e.timestamp).toLocaleDateString('en-US',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}); }
        catch(x) { return e.timestamp; }
      });
      let eqVals = d.equity_curve.map(e => e.total_equity);
      let cashVals = d.equity_curve.map(e => e.balance);

      if (chart) chart.destroy();
      chart = new Chart(document.getElementById('equityChart'), {
        type: 'line',
        data: {
          labels: labels,
          datasets: [
            {
              label: 'Total Equity',
              data: eqVals,
              borderColor: '#58a6ff',
              backgroundColor: 'rgba(88,166,255,0.06)',
              fill: true,
              tension: 0.3,
              pointRadius: eqVals.length > 80 ? 0 : 2,
              borderWidth: 2,
            },
            {
              label: 'Cash',
              data: cashVals,
              borderColor: '#8b949e',
              borderDash: [4, 4],
              fill: false,
              tension: 0.3,
              pointRadius: 0,
              borderWidth: 1,
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { labels: { color: '#8b949e', boxWidth: 12 } },
            tooltip: {
              callbacks: { label: ctx => ctx.dataset.label + ': $' + ctx.parsed.y.toFixed(2) }
            }
          },
          scales: {
            x: {
              ticks: { color: '#484f58', maxTicksLimit: 8, maxRotation: 0 },
              grid: { color: '#21262d' },
            },
            y: {
              ticks: { color: '#484f58', callback: v => '$' + v.toFixed(0) },
              grid: { color: '#21262d' },
            }
          }
        }
      });
    }

    document.getElementById('refresh-note').textContent =
      'Last updated: ' + new Date().toLocaleTimeString() + ' | Auto-refresh every 15s | Last bot snapshot: ' + (d.last_update || 'never');
  }).catch(e => {
    document.getElementById('refresh-note').textContent = 'Error loading data: ' + e;
  });
}

refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return HTML


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    print("[DASHBOARD] Starting on http://0.0.0.0:8080")
    app.run(host="0.0.0.0", port=8080, debug=False)
