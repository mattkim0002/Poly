"""Risk management — Chan drawdown protection and Simons rolling win rate."""

import config


def drawdown(current_equity: float, peak_equity: float) -> float:
    """Calculate current drawdown as a fraction."""
    if peak_equity <= 0:
        return 0.0
    return max(0.0, (peak_equity - current_equity) / peak_equity)


def drawdown_multiplier(current_equity: float, peak_equity: float) -> float:
    """Chan drawdown-based Kelly multiplier.

    DD < 20% -> 1.0 (full size)
    DD 20-30% -> 0.5 (half size)
    DD > 30% -> 0.0 (stop trading)
    """
    dd = drawdown(current_equity, peak_equity)
    if dd < config.DD_THRESHOLD_HALF:
        return 1.0
    if dd < config.DD_THRESHOLD_STOP:
        return 0.5
    return 0.0


def signal_health_multiplier(recent_trades: list[dict]) -> float:
    """Simons rolling win rate multiplier.

    If win rate over last N trades drops below threshold, halve position sizes.
    """
    if len(recent_trades) < config.MIN_TRADES_FOR_SIGNAL:
        return 1.0  # not enough data to judge

    window = recent_trades[-config.WIN_RATE_WINDOW:]
    wins = sum(1 for t in window if (t.get("pnl") or 0) > 0)
    win_rate = wins / len(window)

    return 1.0 if win_rate >= config.WIN_RATE_THRESHOLD else 0.5


def calculate_r_multiple(entry_price: float, exit_price: float, initial_risk: float) -> float:
    """Van Tharp R-Multiple: measure trade result in units of risk.

    R = PnL / initial_risk
    initial_risk = entry_price (max you can lose per share on a binary)
    """
    if initial_risk <= 0:
        return 0.0
    pnl_per_share = exit_price - entry_price
    return pnl_per_share / initial_risk


def expectancy(trades: list[dict]) -> float:
    """System expectancy in R-multiples.

    Expectancy = (Win% * Avg_Win_R) - (Loss% * Avg_Loss_R)
    """
    if not trades:
        return 0.0

    trades_with_r = [t for t in trades if t.get("r_multiple") is not None]
    if not trades_with_r:
        return 0.0

    wins = [t for t in trades_with_r if t["r_multiple"] > 0]
    losses = [t for t in trades_with_r if t["r_multiple"] <= 0]

    total = len(trades_with_r)
    win_rate = len(wins) / total

    avg_win_r = (sum(t["r_multiple"] for t in wins) / len(wins)) if wins else 0.0
    avg_loss_r = (abs(sum(t["r_multiple"] for t in losses)) / len(losses)) if losses else 0.0

    return (win_rate * avg_win_r) - ((1.0 - win_rate) * avg_loss_r)
