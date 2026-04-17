"""Risk module — hard pre-trade limits enforced between strategy and execution.

Pipeline:
    scanner → strategy → risk.evaluate_trade(...) → executor

Never places trades. Only approves, rejects, or resizes a proposed trade.
"""

from typing import Any

# === Risk limits (small-bankroll defaults — $38) ===
MAX_TRADE_SIZE_USDC = 1.0
MAX_RISK_PCT_PER_TRADE = 0.05
MIN_BANKROLL_TO_TRADE = 10.0
MIN_EXPECTED_PROFIT_USDC = 0.02
MAX_TOTAL_OPEN_EXPOSURE_PCT = 0.25
MAX_EXPOSURE_PER_MARKET_PCT = 0.10
DAILY_LOSS_CAP_PCT = 0.05
MAX_CONSECUTIVE_LOSSES = 3
TRADING_PAUSED = False


def _fetch_live_state() -> dict:
    """Pull bankroll, open exposure, daily P&L, consecutive losses from the live bot.

    Returns dict with keys: bankroll, open_exposure_usdc, per_market_exposure,
    daily_pnl_usdc, consecutive_losses.
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

        # Daily P&L + consecutive losses from closed trades
        closed = database.get_closed_trades_today() if hasattr(database, "get_closed_trades_today") else []
        state["daily_pnl_usdc"] = sum(float(t.get("pnl") or 0.0) for t in closed)

        # Consecutive losses: scan most-recent closed trades backwards
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


def evaluate_trade(proposal: dict, state: dict | None = None) -> dict:
    """Evaluate a proposed trade against hard risk rules.

    Args:
        proposal: dict with keys market_id, strategy, side, proposed_size_usdc,
                  net_edge_pct, expected_profit_usdc, confidence,
                  time_to_expiry_minutes.
        state:   optional dict with bankroll/exposure/pnl/losses. If None,
                 fetched live from the bot.

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

    # Rule 1: global pause flag
    if TRADING_PAUSED:
        return _reject("trading_paused", "TRADING_PAUSED flag is set", ["trading_paused"])

    # Rule 2: bankroll minimum
    if bankroll < MIN_BANKROLL_TO_TRADE:
        return _reject(
            f"bankroll ${bankroll:.2f} < ${MIN_BANKROLL_TO_TRADE:.2f}",
            f"bankroll_below_min",
            ["min_bankroll"],
        )

    # Rule 3: consecutive-loss pause
    losses = int(state.get("consecutive_losses") or 0)
    if losses >= MAX_CONSECUTIVE_LOSSES:
        return _reject(
            f"{losses} consecutive losses (limit {MAX_CONSECUTIVE_LOSSES})",
            "consecutive_loss_pause",
            ["consecutive_losses"],
        )

    # Rule 4: daily loss cap
    daily_pnl = float(state.get("daily_pnl_usdc") or 0.0)
    daily_loss_cap = bankroll * DAILY_LOSS_CAP_PCT
    if daily_pnl < 0 and abs(daily_pnl) >= daily_loss_cap:
        return _reject(
            f"daily loss ${abs(daily_pnl):.2f} >= cap ${daily_loss_cap:.2f}",
            "daily_loss_cap_hit",
            ["daily_loss_cap"],
        )

    # Rule 5: min expected profit
    exp_profit = float(proposal.get("expected_profit_usdc") or 0.0)
    if exp_profit < MIN_EXPECTED_PROFIT_USDC:
        return _reject(
            f"expected profit ${exp_profit:.4f} < min ${MIN_EXPECTED_PROFIT_USDC:.2f}",
            "min_profit",
            ["min_expected_profit"],
        )

    # Rule 6: total-exposure cap (after this trade)
    open_expo = float(state.get("open_exposure_usdc") or 0.0)
    expo_cap = bankroll * MAX_TOTAL_OPEN_EXPOSURE_PCT
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

    # Rule 7: per-market exposure cap
    mid = proposal.get("market_id", "")
    per_market_now = float(state.get("per_market_exposure", {}).get(mid, 0.0))
    per_market_cap = bankroll * MAX_EXPOSURE_PER_MARKET_PCT
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

    # Rule 8: max trade size (absolute + % of bankroll)
    pct_cap = bankroll * MAX_RISK_PCT_PER_TRADE
    hard_cap = min(MAX_TRADE_SIZE_USDC, pct_cap)
    if final_size > hard_cap:
        triggered.append("max_trade_size_resize")
        final_size = hard_cap

    # After all resizing, is the trade still worth doing?
    if final_size < 0.01:
        return _reject(
            f"final size ${final_size:.4f} too small after caps",
            "size_too_small_after_caps",
            triggered + ["size_floor"],
        )

    # Scale expected profit by shrink ratio; re-check min profit.
    if proposed_size > 0:
        scaled_profit = exp_profit * (final_size / proposed_size)
        if scaled_profit < MIN_EXPECTED_PROFIT_USDC:
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
    """Dry-run friendly log: one-line summary of proposal → decision."""
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
