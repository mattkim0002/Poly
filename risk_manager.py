"""Risk manager — mandatory hard pre-trade gate between strategy and execution.

Pipeline:
    scanner → strategy → risk_manager.evaluate_trade(...) → execution.execute_buy(...)

Every trade proposal must pass through evaluate_trade(). Nothing reaches
the exchange without approval from this module.
"""

import config
from utils.logger import log


TRADING_PAUSED = False


def _fetch_live_state() -> dict:
    """Pull bankroll, open exposure, daily P&L, consecutive losses from the live bot.

    Swallows all errors — returns safe neutral state if DB/trader unreachable.
    """
    state = {
        "bankroll": 0.0,
        "open_exposure_usdc": 0.0,
        "per_market_exposure": {},
        "daily_pnl_usdc": 0.0,
        "consecutive_losses": 0,
    }
    try:
        from core import trader, database
        state["bankroll"] = float(trader.get_balance() or 0.0)

        open_trades = database.get_open_trades() or []
        for t in open_trades:
            cost = float(t.get("cost") or 0.0)
            state["open_exposure_usdc"] += cost
            mid = t.get("market_id") or t.get("market_question", "")
            state["per_market_exposure"][mid] = state["per_market_exposure"].get(mid, 0.0) + cost

        closed = database.get_closed_trades_today() if hasattr(database, "get_closed_trades_today") else []
        state["daily_pnl_usdc"] = sum(float(t.get("pnl") or 0.0) for t in closed)

        recent = database.get_recent_closed_trades(10) if hasattr(database, "get_recent_closed_trades") else []
        streak = 0
        for t in recent:
            if float(t.get("pnl") or 0.0) < 0:
                streak += 1
            else:
                break
        state["consecutive_losses"] = streak
    except Exception:
        pass
    return state


def build_proposal(
    strategy: str,
    market_id: str,
    side: str,
    proposed_size_usdc: float,
    expected_profit_usdc: float,
    net_edge_pct: float = 0.0,
    confidence: str = "medium",
    time_to_expiry_minutes: float = 0.0,
) -> dict:
    """Build a standardized proposal dict for evaluate_trade()."""
    return {
        "strategy": strategy,
        "market_id": market_id,
        "side": side,
        "proposed_size_usdc": proposed_size_usdc,
        "expected_profit_usdc": expected_profit_usdc,
        "net_edge_pct": net_edge_pct,
        "confidence": confidence,
        "time_to_expiry_minutes": time_to_expiry_minutes,
    }


def evaluate_trade(proposal: dict, state: dict | None = None) -> dict:
    """Evaluate a proposed trade against hard risk rules.

    Returns:
        {"approved": bool, "final_size_usdc": float, "reason": str,
         "triggered_rules": list[str]}
    """
    if state is None:
        state = _fetch_live_state()

    triggered: list[str] = []
    proposed_size = float(proposal.get("proposed_size_usdc") or 0.0)
    final_size = proposed_size
    bankroll = float(state.get("bankroll") or 0.0)

    max_trade_size = getattr(config, "MAX_TRADE_SIZE_USDC", 1.0)
    max_risk_pct = getattr(config, "MAX_RISK_PCT_PER_TRADE", 0.05)
    min_bankroll = getattr(config, "MIN_BANKROLL_TO_TRADE", 10.0)
    min_profit = getattr(config, "MIN_EXPECTED_PROFIT_USDC", 0.02)
    max_expo_pct = getattr(config, "MAX_TOTAL_OPEN_EXPOSURE_PCT", 0.25)
    max_per_market_pct = getattr(config, "MAX_EXPOSURE_PER_MARKET_PCT", 0.10)
    daily_loss_pct = getattr(config, "DAILY_LOSS_CAP_PCT", 0.05)
    max_consec = getattr(config, "MAX_CONSECUTIVE_LOSSES", 3)

    if TRADING_PAUSED:
        return _reject("TRADING_PAUSED flag is set", "trading_paused", ["trading_paused"])

    if bankroll < min_bankroll:
        return _reject(
            f"bankroll ${bankroll:.2f} < ${min_bankroll:.2f}",
            "bankroll_below_min",
            ["min_bankroll"],
        )

    losses = int(state.get("consecutive_losses") or 0)
    if losses >= max_consec:
        return _reject(
            f"{losses} consecutive losses (limit {max_consec})",
            "consecutive_loss_pause",
            ["consecutive_losses"],
        )

    daily_pnl = float(state.get("daily_pnl_usdc") or 0.0)
    daily_loss_cap = bankroll * daily_loss_pct
    if daily_pnl < 0 and abs(daily_pnl) >= daily_loss_cap:
        return _reject(
            f"daily loss ${abs(daily_pnl):.2f} >= cap ${daily_loss_cap:.2f}",
            "daily_loss_cap_hit",
            ["daily_loss_cap"],
        )

    exp_profit = float(proposal.get("expected_profit_usdc") or 0.0)
    if exp_profit < min_profit:
        return _reject(
            f"expected profit ${exp_profit:.4f} < min ${min_profit:.2f}",
            "min_profit",
            ["min_expected_profit"],
        )

    open_expo = float(state.get("open_exposure_usdc") or 0.0)
    expo_cap = bankroll * max_expo_pct
    headroom = max(0.0, expo_cap - open_expo)
    if headroom <= 0:
        return _reject(
            f"open exposure ${open_expo:.2f} >= cap ${expo_cap:.2f}",
            "total_exposure_cap",
            ["total_exposure"],
        )
    if final_size > headroom:
        triggered.append("total_exposure_resize")
        final_size = headroom

    mid = proposal.get("market_id", "")
    per_market_now = float(state.get("per_market_exposure", {}).get(mid, 0.0))
    per_market_cap = bankroll * max_per_market_pct
    per_market_headroom = max(0.0, per_market_cap - per_market_now)
    if per_market_headroom <= 0:
        return _reject(
            f"market exposure ${per_market_now:.2f} >= cap ${per_market_cap:.2f}",
            "per_market_exposure_cap",
            ["per_market_exposure"],
        )
    if final_size > per_market_headroom:
        triggered.append("per_market_resize")
        final_size = per_market_headroom

    pct_cap = bankroll * max_risk_pct
    hard_cap = min(max_trade_size, pct_cap)
    if final_size > hard_cap:
        triggered.append("max_trade_size_resize")
        final_size = hard_cap

    if final_size < 0.01:
        return _reject(
            f"final size ${final_size:.4f} too small after caps",
            "size_too_small_after_caps",
            triggered + ["size_floor"],
        )

    if proposed_size > 0:
        scaled_profit = exp_profit * (final_size / proposed_size)
        if scaled_profit < min_profit:
            return _reject(
                f"scaled profit ${scaled_profit:.4f} < min after resize",
                "min_profit_after_resize",
                triggered + ["min_expected_profit"],
            )

    reason = (
        f"approved at ${final_size:.2f}"
        + (f" (resized from ${proposed_size:.2f})" if final_size < proposed_size else "")
    )
    return {
        "approved": True,
        "final_size_usdc": round(final_size, 4),
        "reason": reason,
        "triggered_rules": triggered,
    }


def _reject(reason: str, rule_key: str, triggered: list[str]) -> dict:
    return {
        "approved": False,
        "final_size_usdc": 0.0,
        "reason": reason,
        "triggered_rules": triggered,
    }


def log_decision(proposal: dict, decision: dict) -> None:
    """One-line summary of proposal → decision."""
    sym = proposal.get("market_id", "?")[:16]
    side = proposal.get("side", "?")
    strat = proposal.get("strategy", "?")
    proposed = float(proposal.get("proposed_size_usdc") or 0.0)
    final = float(decision.get("final_size_usdc") or 0.0)
    status = "APPROVED" if decision["approved"] else "REJECTED"
    rules = ",".join(decision.get("triggered_rules") or []) or "-"
    print(
        f"[RISK] {status} {strat:>7} {side:>3} {sym:<16} "
        f"proposed=${proposed:.2f} final=${final:.2f} rules={rules} "
        f"reason={decision['reason']}"
    )
