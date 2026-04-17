"""Execution chokepoint — the ONLY module that places orders.

All buy orders flow through execute_buy(). It enforces:
  1. A risk_manager decision must be provided and approved.
  2. Routes to live (core/trader.py) or paper (core/paper_trader.py) based on config.
  3. Logs every order attempt with mode, strategy, and outcome.

No other module should call trader.place_limit_order() or paper_trader.paper_buy()
for entry orders. Exit/sell orders still go through trader directly since they
reduce exposure (not increase it).
"""

import config
from core import trader, database
from core.paper_trader import paper_buy
from utils.logger import log


_mode: str | None = None


def get_mode() -> str:
    """Return 'paper' or 'live' based on config."""
    return "paper" if config.PAPER_TRADING else "live"


def log_mode():
    """Print which execution mode is active. Call once at startup."""
    mode = get_mode()
    label = "PAPER (no real money)" if mode == "paper" else "LIVE (real funds)"
    print(f"[EXECUTION] Mode: {label}")


def execute_buy(
    risk_decision: dict,
    token_id: str,
    price: float,
    shares: float,
    market_id: str = "",
    question: str = "",
    outcome: str = "",
    strategy: str = "",
    edge: float = 0.0,
    claude_probability: float = 0.0,
    market_probability: float = 0.0,
    kelly_frac: float = 0.0,
    dd_mult: float = 1.0,
    signal_mult: float = 1.0,
    end_date: str = "",
    extra_db_fields: dict | None = None,
) -> str | None:
    """Place a BUY order only if risk_decision is approved.

    Returns order_id on success, None on failure or rejection.
    """
    if not risk_decision.get("approved"):
        print(f"[EXECUTION] BLOCKED: risk not approved — {risk_decision.get('reason', 'unknown')}")
        return None

    mode = get_mode()
    cost = shares * price

    if mode == "paper":
        ok = paper_buy(
            market_id=market_id,
            question=question,
            token_id=token_id,
            outcome=outcome,
            price=price,
            size=shares,
            edge=edge,
        )
        order_id = f"paper_{int(__import__('time').time())}" if ok else None
    else:
        order_id = trader.place_limit_order(
            token_id=token_id,
            price=price,
            size=shares,
            side="BUY",
        )

    if order_id:
        print(f"[EXECUTION] [{mode.upper()}] {strategy} BUY {outcome} {shares:.0f} shares @ ${price:.3f} "
              f"(${cost:.2f}) → {order_id}")
        database.record_trade(
            market_id=market_id,
            market_question=question,
            token_id=token_id,
            side="BUY",
            outcome=outcome,
            entry_price=price,
            size=shares,
            cost=cost,
            order_id=order_id,
            claude_probability=claude_probability,
            market_probability=market_probability,
            edge=edge,
            kelly_frac=kelly_frac,
            dd_mult=dd_mult,
            signal_mult=signal_mult,
            end_date=end_date,
        )
    else:
        print(f"[EXECUTION] [{mode.upper()}] FAILED: {strategy} BUY {outcome} @ ${price:.3f}")

    return order_id


def execute_arb_pair(
    risk_decision: dict,
    yes_token: str,
    no_token: str,
    yes_price: float,
    no_price: float,
    shares: float,
    market_id: str = "",
    question: str = "",
    profit_pct: float = 0.0,
    end_date: str = "",
) -> dict | None:
    """Place a YES+NO arb pair only if risk_decision is approved.

    Returns result dict on success, None on failure.
    """
    if not risk_decision.get("approved"):
        print(f"[EXECUTION] BLOCKED: risk not approved — {risk_decision.get('reason', 'unknown')}")
        return None

    mode = get_mode()
    total_cost = shares * (yes_price + no_price)

    if mode == "paper":
        ok_yes = paper_buy(market_id, f"[ARB-YES] {question[:80]}", yes_token, "Yes", yes_price, shares, profit_pct)
        if not ok_yes:
            return None
        ok_no = paper_buy(market_id, f"[ARB-NO] {question[:80]}", no_token, "No", no_price, shares, profit_pct)
        if not ok_no:
            return None
        yes_order = f"paper_yes_{int(__import__('time').time())}"
        no_order = f"paper_no_{int(__import__('time').time())}"
    else:
        yes_order = trader.place_limit_order(yes_token, yes_price, shares, "BUY")
        if not yes_order:
            print(f"[EXECUTION] [{mode.upper()}] FAILED: ARB YES order")
            return None
        no_order = trader.place_limit_order(no_token, no_price, shares, "BUY")
        if not no_order:
            print(f"[EXECUTION] [{mode.upper()}] FAILED: ARB NO order — cancelling YES")
            trader.cancel_order(yes_order)
            return None

    print(f"[EXECUTION] [{mode.upper()}] ARB {shares:.0f} shares YES@${yes_price:.3f} NO@${no_price:.3f} "
          f"(${total_cost:.2f}) → {yes_order}, {no_order}")

    database.record_trade(
        market_id=market_id,
        market_question=f"[ARB-YES] {question[:80]}",
        token_id=yes_token,
        side="BUY", outcome="Yes",
        entry_price=yes_price, size=shares,
        cost=shares * yes_price,
        order_id=yes_order,
        claude_probability=0.0, market_probability=yes_price,
        edge=profit_pct, kelly_frac=0.0,
        dd_mult=1.0, signal_mult=1.0,
    )
    database.record_trade(
        market_id=market_id,
        market_question=f"[ARB-NO] {question[:80]}",
        token_id=no_token,
        side="BUY", outcome="No",
        entry_price=no_price, size=shares,
        cost=shares * no_price,
        order_id=no_order,
        claude_probability=0.0, market_probability=no_price,
        edge=profit_pct, kelly_frac=0.0,
        dd_mult=1.0, signal_mult=1.0,
    )

    return {
        "question": question,
        "market_id": market_id,
        "yes_token": yes_token,
        "no_token": no_token,
        "yes_price": yes_price,
        "no_price": no_price,
        "shares": shares,
        "total_cost": total_cost,
        "expected_profit": shares * (1.0 - yes_price - no_price),
        "profit_pct": profit_pct,
        "yes_order": yes_order,
        "no_order": no_order,
    }
