"""Paper trading simulator — tracks simulated trades without real money.

Simulates realistic execution: slippage, partial fills, and latency
so paper results approximate live performance.
"""

import json
import os
import random
import time
from datetime import datetime, timezone

from utils.logger import log

PAPER_DB_FILE = "paper_trades.json"

SLIPPAGE_BPS = 30       # 0.3% avg slippage (spread + market impact)
FILL_RATE_MIN = 0.70    # worst case: 70% of requested size fills
FILL_RATE_MAX = 1.0     # best case: full fill
LATENCY_MS_MIN = 200    # min simulated latency
LATENCY_MS_MAX = 800    # max simulated latency


def load_paper_state() -> dict:
    """Load paper trading state from JSON file."""
    if os.path.exists(PAPER_DB_FILE):
        with open(PAPER_DB_FILE) as f:
            return json.load(f)
    from config import PAPER_STARTING_BALANCE
    return {
        "balance": PAPER_STARTING_BALANCE,
        "peak_balance": PAPER_STARTING_BALANCE,
        "trades": [],
        "open_positions": [],
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl": 0.0,
    }


def save_paper_state(state: dict):
    """Persist paper trading state to JSON file."""
    with open(PAPER_DB_FILE, "w") as f:
        json.dump(state, f, indent=2)


def paper_buy(market_id: str, question: str, token_id: str, outcome: str,
              price: float, size: float, edge: float) -> bool:
    """Simulate a BUY order with realistic execution.

    Applies slippage (slightly worse fill price), random partial fills,
    and simulated latency to approximate live trading conditions.
    """
    # Simulate network + exchange latency
    latency_s = random.randint(LATENCY_MS_MIN, LATENCY_MS_MAX) / 1000
    time.sleep(latency_s)

    # Slippage: buy fills slightly higher than requested
    slip = price * (SLIPPAGE_BPS / 10000) * random.uniform(0.5, 1.5)
    fill_price = min(round(price + slip, 4), 0.99)

    # Partial fill: not all shares may be available at this price
    fill_rate = random.uniform(FILL_RATE_MIN, FILL_RATE_MAX)
    fill_size = max(1, int(size * fill_rate))

    state = load_paper_state()
    cost = fill_price * fill_size

    if cost > state["balance"]:
        log.info("[PAPER] Insufficient balance: need $%.2f, have $%.2f", cost, state["balance"])
        return False

    state["balance"] -= cost
    position = {
        "id": f"paper_{int(time.time())}",
        "market_id": market_id,
        "question": question,
        "token_id": token_id,
        "outcome": outcome,
        "entry_price": fill_price,
        "size": fill_size,
        "cost": cost,
        "edge": edge,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "open",
    }
    state["open_positions"].append(position)
    state["total_trades"] += 1
    save_paper_state(state)

    slip_info = ""
    if fill_size < size:
        slip_info = f" (partial: {fill_size}/{int(size)} filled)"
    if fill_price != price:
        slip_info += f" (slip: ${price:.3f}→${fill_price:.3f})"

    log.info("[PAPER] BUY %s shares of '%s' (%s) @ $%.3f | Cost: $%.2f | Balance: $%.2f%s",
             fill_size, question[:40], outcome, fill_price, cost, state["balance"], slip_info)
    return True


def paper_check_positions(get_price_fn) -> list:
    """Check open positions for resolution. Returns list of resolved positions."""
    from config import TAKE_PROFIT_PCT, STOP_LOSS_PCT

    state = load_paper_state()
    resolved = []
    still_open = []

    for pos in state["open_positions"]:
        try:
            current_price = get_price_fn(pos["token_id"])
        except Exception:
            still_open.append(pos)
            continue

        if current_price is None or current_price <= 0:
            still_open.append(pos)
            continue

        won = current_price >= 0.95   # Resolved YES
        lost = current_price <= 0.05  # Resolved NO

        pnl_pct = (current_price - pos["entry_price"]) / pos["entry_price"] if pos["entry_price"] > 0 else 0
        take_profit = pnl_pct >= TAKE_PROFIT_PCT
        stop_loss = pnl_pct <= -STOP_LOSS_PCT

        if won or lost or take_profit or stop_loss:
            if won:
                exit_price = 1.0
                pnl = (1.0 - pos["entry_price"]) * pos["size"]
                reason = "WON"
            elif lost:
                exit_price = 0.0
                pnl = -pos["cost"]
                reason = "LOST"
            elif take_profit:
                exit_price = current_price
                pnl = (exit_price - pos["entry_price"]) * pos["size"]
                reason = f"TAKE_PROFIT ({pnl_pct:+.1%})"
            else:
                exit_price = current_price
                pnl = (exit_price - pos["entry_price"]) * pos["size"]
                reason = f"STOP_LOSS ({pnl_pct:+.1%})"

            proceeds = exit_price * pos["size"]
            state["balance"] += proceeds
            state["peak_balance"] = max(state["peak_balance"], state["balance"])
            state["total_pnl"] += pnl

            if pnl > 0:
                state["wins"] += 1
            else:
                state["losses"] += 1

            pos["status"] = reason
            pos["exit_price"] = exit_price
            pos["pnl"] = pnl
            pos["exit_time"] = datetime.now(timezone.utc).isoformat()
            state["trades"].append(pos)
            resolved.append(pos)

            win_rate = state["wins"] / max(state["total_trades"], 1)
            log.info("[PAPER] CLOSED '%s' | %s | PnL: $%+.2f | Balance: $%.2f | WR: %.0f%%",
                     pos["question"][:40], reason, pnl, state["balance"], win_rate * 100)
        else:
            still_open.append(pos)

    state["open_positions"] = still_open
    save_paper_state(state)
    return resolved


def paper_portfolio_summary() -> str:
    """Return a formatted portfolio summary string."""
    state = load_paper_state()
    win_rate = state["wins"] / max(state["total_trades"], 1) * 100
    open_value = sum(p["entry_price"] * p["size"] for p in state["open_positions"])
    total = state["balance"] + open_value

    lines = [
        "",
        "-------- PAPER PORTFOLIO --------",
        f"  Balance:     ${state['balance']:.2f}",
        f"  Open value:  ${open_value:.2f}",
        f"  Total:       ${total:.2f}",
        f"  Peak:        ${state['peak_balance']:.2f}",
        f"  Total P&L:   ${state['total_pnl']:+.2f}",
        f"  Trades:      {state['total_trades']} ({state['wins']}W / {state['losses']}L)",
        f"  Win rate:    {win_rate:.0f}%",
        "---------------------------------",
        "",
    ]
    return "\n".join(lines)


def paper_get_balance() -> float:
    """Return the current paper trading balance."""
    state = load_paper_state()
    return state["balance"]


def paper_get_open_positions() -> list:
    """Return the list of open paper positions."""
    state = load_paper_state()
    return state["open_positions"]


def paper_close_position(token_id: str, exit_price: float, size: float | None = None) -> bool:
    """Close (fully or partially) a paper position at the given price.

    Applies sell-side slippage (fills slightly lower) to simulate real
    execution. Credits balance, records the closed trade, and removes
    the position. Returns True if a matching open position was found.
    """
    # Sell-side slippage: fill slightly below requested exit price
    slip = exit_price * (SLIPPAGE_BPS / 10000) * random.uniform(0.5, 1.5)
    exit_price = max(round(exit_price - slip, 4), 0.01)

    state = load_paper_state()
    remaining = []
    closed = False
    for pos in state["open_positions"]:
        if pos.get("token_id") != token_id or closed:
            remaining.append(pos)
            continue

        pos_size = float(pos.get("size", 0))
        close_size = float(size) if size is not None else pos_size
        close_size = min(close_size, pos_size)
        if close_size <= 0:
            remaining.append(pos)
            continue

        entry = float(pos.get("entry_price", 0))
        proceeds = exit_price * close_size
        pnl = (exit_price - entry) * close_size

        state["balance"] += proceeds
        state["peak_balance"] = max(state["peak_balance"], state["balance"])
        state["total_pnl"] += pnl
        if pnl > 0:
            state["wins"] += 1
        else:
            state["losses"] += 1

        closed_pos = dict(pos)
        closed_pos["status"] = "closed"
        closed_pos["exit_price"] = exit_price
        closed_pos["pnl"] = pnl
        closed_pos["exit_time"] = datetime.now(timezone.utc).isoformat()
        state["trades"].append(closed_pos)

        leftover = pos_size - close_size
        if leftover > 0:
            pos = dict(pos)
            pos["size"] = leftover
            pos["cost"] = entry * leftover
            remaining.append(pos)

        closed = True
        log.info("[PAPER] CLOSED '%s' @ $%.3f x%.1f | PnL: $%+.2f | Balance: $%.2f",
                 (pos.get("question") or "")[:40], exit_price, close_size, pnl, state["balance"])

    state["open_positions"] = remaining
    save_paper_state(state)
    return closed
