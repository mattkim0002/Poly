"""Paper trading simulator — tracks simulated trades without real money."""

import json
import os
import time
from datetime import datetime, timezone

import config
from utils.logger import log

PAPER_DB_FILE = "paper_trades.json"


def load_paper_state() -> dict:
    """Load paper trading state from JSON file."""
    if os.path.exists(PAPER_DB_FILE):
        with open(PAPER_DB_FILE) as f:
            return json.load(f)
    return {
        "balance": config.PAPER_STARTING_BALANCE,
        "peak_balance": config.PAPER_STARTING_BALANCE,
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
    """Simulate a BUY order."""
    state = load_paper_state()
    cost = price * size

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
        "entry_price": price,
        "size": size,
        "cost": cost,
        "edge": edge,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "open",
    }
    state["open_positions"].append(position)
    state["total_trades"] += 1
    save_paper_state(state)

    log.info("[PAPER] BUY %s shares of '%s' (%s) @ $%.3f | Cost: $%.2f | Balance: $%.2f",
             size, question[:40], outcome, price, cost, state["balance"])
    return True


def paper_check_positions(get_price_fn) -> list:
    """Check open positions for resolution. Returns list of resolved positions."""
    state = load_paper_state()
    resolved = []
    still_open = []

    for pos in state["open_positions"]:
        try:
            current_price = get_price_fn(pos["token_id"])
        except Exception:
            still_open.append(pos)
            continue

        # Check if resolved
        if current_price is None or current_price <= 0:
            still_open.append(pos)
            continue

        won = current_price >= 0.95   # Resolved YES
        lost = current_price <= 0.05  # Resolved NO

        # Take profit / stop loss
        pnl_pct = (current_price - pos["entry_price"]) / pos["entry_price"] if pos["entry_price"] > 0 else 0
        take_profit = pnl_pct >= 0.10    # 10% gain
        stop_loss = pnl_pct <= -0.0615   # 6.15% loss

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
                pnl = (current_price - pos["entry_price"]) * pos["size"]
                reason = f"TAKE_PROFIT ({pnl_pct:+.1%})"
            else:
                exit_price = current_price
                pnl = (current_price - pos["entry_price"]) * pos["size"]
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
