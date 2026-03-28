"""Simple Flask dashboard for the Polymarket trading bot."""

import sqlite3
import json
import os
from flask import Flask, jsonify

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "polybot.db")

app = Flask(__name__)


def get_db():
    """Get a read-only SQLite connection."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_midpoints(token_ids):
    """Fetch current midpoint prices for a list of token IDs.

    Uses the CLOB client from core.trader if available, otherwise
    falls back to the public Polymarket midpoint endpoint.
    """
    prices = {}
    if not token_ids:
        return prices

    # Try the public CLOB REST API (no auth needed for price data)
    try:
        import httpx
        for tid in token_ids:
            try:
                resp = httpx.get(
                    f"https://clob.polymarket.com/midpoint",
                    params={"token_id": tid},
                    timeout=5,
                )
                resp.raise_for_status()
                data = resp.json()
                mid = float(data.get("mid", 0))
                if mid > 0:
                    prices[tid] = mid
            except Exception:
                pass
    except ImportError:
        # httpx not available, try requests
        try:
            import requests
            for tid in token_ids:
                try:
                    resp = requests.get(
                        f"https://clob.polymarket.com/midpoint",
                        params={"token_id": tid},
                        timeout=5,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    mid = float(data.get("mid", 0))
                    if mid > 0:
                        prices[tid] = mid
                except Exception:
                    pass
        except ImportError:
            pass

    return prices


@app.route("/api/data")
def api_data():
    """Return all dashboard data as JSON."""
    conn = get_db()
    try:
        cur = conn.cursor()

        # Current balance from latest equity snapshot
        cur.execute(
            "SELECT balance, total_equity, peak_equity, drawdown "
            "FROM equity_snapshots ORDER BY id DESC LIMIT 1"
        )
        snap = cur.fetchone()
        balance = snap["balance"] if snap else 0
        total_equity = snap["total_equity"] if snap else 0
        peak_equity = snap["peak_equity"] if snap else 0
        drawdown = snap["drawdown"] if snap else 0

        # Trade stats
        cur.execute("SELECT COUNT(*) as cnt FROM trades WHERE status != 'cancelled'")
        total_trades = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) as cnt FROM trades WHERE status = 'won'")
        wins = cur.fetchone()["cnt"]

        cur.execute(
            "SELECT COUNT(*) as cnt FROM trades WHERE status IN ('won','lost')"
        )
        closed_decided = cur.fetchone()["cnt"]
        win_rate = (wins / closed_decided * 100) if closed_decided > 0 else 0

        cur.execute(
            "SELECT COALESCE(SUM(pnl), 0) as total_pnl FROM trades "
            "WHERE status IN ('won','lost')"
        )
        total_pnl = cur.fetchone()["total_pnl"]

        # Open positions
        cur.execute(
            "SELECT id, market_question, token_id, outcome, side, entry_price, "
            "size, cost, created_at FROM trades WHERE status = 'open' "
            "ORDER BY created_at DESC"
        )
        open_rows = cur.fetchall()
        open_positions = [dict(r) for r in open_rows]

        # Fetch live midpoints for open positions
        token_ids = list({p["token_id"] for p in open_positions if p.get("token_id")})
        midpoints = fetch_midpoints(token_ids)

        for pos in open_positions:
            tid = pos.get("token_id", "")
            mid = midpoints.get(tid)
            if mid is not None:
                pos["current_price"] = mid
                if pos["side"] == "BUY":
                    pos["unrealized_pnl"] = round(
                        (mid - pos["entry_price"]) * pos["size"], 4
                    )
                else:
                    pos["unrealized_pnl"] = round(
                        (pos["entry_price"] - mid) * pos["size"], 4
                    )
            else:
                pos["current_price"] = None
                pos["unrealized_pnl"] = None

        # Recent closed trades
        cur.execute(
            "SELECT market_question, outcome, side, entry_price, exit_price, "
            "size, pnl, r_multiple, status, created_at, closed_at "
            "FROM trades WHERE status IN ('won','lost','cancelled') "
            "ORDER BY closed_at DESC LIMIT 50"
        )
        closed_trades = [dict(r) for r in cur.fetchall()]

        # Equity curve
        cur.execute(
            "SELECT timestamp, total_equity, balance, drawdown "
            "FROM equity_snapshots ORDER BY id ASC"
        )
        equity_curve = [dict(r) for r in cur.fetchall()]

        return jsonify({
            "balance": balance,
            "total_equity": total_equity,
            "peak_equity": peak_equity,
            "drawdown": drawdown,
            "total_pnl": total_pnl,
            "win_rate": win_rate,
            "total_trades": total_trades,
            "wins": wins,
            "closed_decided": closed_decided,
            "open_positions": open_positions,
            "closed_trades": closed_trades,
            "equity_curve": equity_curve,
        })
    finally:
        conn.close()


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
    background: #0d1117; color: #c9d1d9; padding: 16px;
  }
  h1 { color: #58a6ff; font-size: 1.4rem; margin-bottom: 12px; }
  .refresh-note { color: #484f58; font-size: 0.75rem; margin-bottom: 16px; }
  .cards { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }
  .card {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 16px 20px; flex: 1; min-width: 140px;
  }
  .card .label { font-size: 0.75rem; color: #8b949e; text-transform: uppercase; }
  .card .value { font-size: 1.6rem; font-weight: 700; margin-top: 4px; }
  .positive { color: #3fb950; }
  .negative { color: #f85149; }
  .neutral { color: #c9d1d9; }
  .section-title {
    font-size: 1rem; color: #58a6ff; margin: 20px 0 8px;
    border-bottom: 1px solid #21262d; padding-bottom: 4px;
  }
  table {
    width: 100%; border-collapse: collapse; font-size: 0.82rem;
    margin-bottom: 16px;
  }
  th {
    text-align: left; padding: 8px; background: #161b22;
    border-bottom: 2px solid #30363d; color: #8b949e;
    font-weight: 600; text-transform: uppercase; font-size: 0.7rem;
  }
  td {
    padding: 7px 8px; border-bottom: 1px solid #21262d;
    max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  tr:hover { background: #161b22; }
  .chart-container {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 16px; margin-bottom: 20px;
  }
  .status-badge {
    padding: 2px 8px; border-radius: 10px; font-size: 0.7rem;
    font-weight: 600; text-transform: uppercase;
  }
  .status-won { background: #0f2d1a; color: #3fb950; }
  .status-lost { background: #2d0f0f; color: #f85149; }
  .status-open { background: #0f1d2d; color: #58a6ff; }
  .status-cancelled { background: #1c1c1c; color: #8b949e; }
  .empty { color: #484f58; font-style: italic; padding: 20px; text-align: center; }
  @media (max-width: 600px) {
    .card .value { font-size: 1.2rem; }
    table { font-size: 0.75rem; }
    td, th { padding: 5px 4px; }
  }
</style>
</head>
<body>
<h1>Polybot Dashboard</h1>
<div class="refresh-note" id="refresh-note">Auto-refreshes every 30s</div>

<div class="cards" id="cards">
  <div class="card"><div class="label">Balance</div><div class="value" id="balance">--</div></div>
  <div class="card"><div class="label">Total Equity</div><div class="value" id="equity">--</div></div>
  <div class="card"><div class="label">Total P&L</div><div class="value" id="pnl">--</div></div>
  <div class="card"><div class="label">Win Rate</div><div class="value" id="winrate">--</div></div>
  <div class="card"><div class="label">Trades</div><div class="value neutral" id="trades">--</div></div>
  <div class="card"><div class="label">Drawdown</div><div class="value" id="drawdown">--</div></div>
</div>

<div class="section-title">Open Positions</div>
<div style="overflow-x:auto">
<table id="open-table">
  <thead><tr>
    <th>Market</th><th>Outcome</th><th>Side</th><th>Entry</th><th>Current</th><th>Size</th><th>Unrealized P&L</th>
  </tr></thead>
  <tbody id="open-body"></tbody>
</table>
</div>

<div class="section-title">Equity Curve</div>
<div class="chart-container"><canvas id="equityChart" height="220"></canvas></div>

<div class="section-title">Recent Closed Trades</div>
<div style="overflow-x:auto">
<table id="closed-table">
  <thead><tr>
    <th>Market</th><th>Outcome</th><th>Side</th><th>Entry</th><th>Exit</th><th>P&L</th><th>R</th><th>Status</th><th>Closed</th>
  </tr></thead>
  <tbody id="closed-body"></tbody>
</table>
</div>

<script>
let chart = null;

function pnlClass(v) { return v > 0 ? 'positive' : v < 0 ? 'negative' : 'neutral'; }
function fmt(v, d=2) { return v != null ? '$' + Number(v).toFixed(d) : '--'; }
function pct(v) { return v != null ? Number(v).toFixed(1) + '%' : '--'; }
function shortQ(q) { return q && q.length > 60 ? q.slice(0, 57) + '...' : (q || '--'); }
function shortDate(d) {
  if (!d) return '--';
  try { return new Date(d).toLocaleDateString('en-US', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}); }
  catch(e) { return d; }
}

function refresh() {
  fetch('/api/data').then(r => r.json()).then(d => {
    document.getElementById('balance').textContent = fmt(d.balance);
    document.getElementById('equity').textContent = fmt(d.total_equity);

    let pnlEl = document.getElementById('pnl');
    pnlEl.textContent = fmt(d.total_pnl);
    pnlEl.className = 'value ' + pnlClass(d.total_pnl);

    document.getElementById('winrate').textContent = pct(d.win_rate);
    document.getElementById('trades').textContent = d.total_trades + ' (' + d.wins + 'W)';

    let ddEl = document.getElementById('drawdown');
    ddEl.textContent = d.drawdown != null ? (Number(d.drawdown)*100).toFixed(1) + '%' : '--';
    ddEl.className = 'value ' + (d.drawdown > 0.1 ? 'negative' : 'neutral');

    // Open positions
    let ob = document.getElementById('open-body');
    if (d.open_positions.length === 0) {
      ob.innerHTML = '<tr><td colspan="7" class="empty">No open positions</td></tr>';
    } else {
      ob.innerHTML = d.open_positions.map(p => {
        let upnl = p.unrealized_pnl;
        return '<tr>' +
          '<td title="' + (p.market_question||'').replace(/"/g,'&quot;') + '">' + shortQ(p.market_question) + '</td>' +
          '<td>' + (p.outcome||'--') + '</td>' +
          '<td>' + (p.side||'--') + '</td>' +
          '<td>' + fmt(p.entry_price) + '</td>' +
          '<td>' + (p.current_price != null ? fmt(p.current_price) : '--') + '</td>' +
          '<td>' + Number(p.size).toFixed(1) + '</td>' +
          '<td class="' + pnlClass(upnl) + '">' + (upnl != null ? fmt(upnl) : '--') + '</td>' +
          '</tr>';
      }).join('');
    }

    // Closed trades
    let cb = document.getElementById('closed-body');
    if (d.closed_trades.length === 0) {
      cb.innerHTML = '<tr><td colspan="9" class="empty">No closed trades yet</td></tr>';
    } else {
      cb.innerHTML = d.closed_trades.map(t => {
        return '<tr>' +
          '<td title="' + (t.market_question||'').replace(/"/g,'&quot;') + '">' + shortQ(t.market_question) + '</td>' +
          '<td>' + (t.outcome||'--') + '</td>' +
          '<td>' + (t.side||'--') + '</td>' +
          '<td>' + fmt(t.entry_price) + '</td>' +
          '<td>' + fmt(t.exit_price) + '</td>' +
          '<td class="' + pnlClass(t.pnl) + '">' + fmt(t.pnl) + '</td>' +
          '<td>' + (t.r_multiple != null ? Number(t.r_multiple).toFixed(2) + 'R' : '--') + '</td>' +
          '<td><span class="status-badge status-' + t.status + '">' + t.status + '</span></td>' +
          '<td>' + shortDate(t.closed_at) + '</td>' +
          '</tr>';
      }).join('');
    }

    // Equity chart
    if (d.equity_curve.length > 0) {
      let labels = d.equity_curve.map(e => {
        try { return new Date(e.timestamp).toLocaleDateString('en-US',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}); }
        catch(x) { return e.timestamp; }
      });
      let values = d.equity_curve.map(e => e.total_equity);

      if (chart) chart.destroy();
      chart = new Chart(document.getElementById('equityChart'), {
        type: 'line',
        data: {
          labels: labels,
          datasets: [{
            label: 'Total Equity',
            data: values,
            borderColor: '#58a6ff',
            backgroundColor: 'rgba(88,166,255,0.08)',
            fill: true,
            tension: 0.3,
            pointRadius: values.length > 100 ? 0 : 2,
            borderWidth: 2,
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: {
              ticks: { color: '#484f58', maxTicksLimit: 10, maxRotation: 0 },
              grid: { color: '#21262d' },
            },
            y: {
              ticks: { color: '#484f58', callback: v => '$' + v.toFixed(2) },
              grid: { color: '#21262d' },
            }
          }
        }
      });
    }

    document.getElementById('refresh-note').textContent =
      'Last updated: ' + new Date().toLocaleTimeString() + ' — auto-refreshes every 30s';
  }).catch(e => {
    document.getElementById('refresh-note').textContent = 'Error loading data: ' + e;
  });
}

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return HTML


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
