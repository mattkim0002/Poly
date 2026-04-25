"""Master position sizer — combines all strategy multipliers."""

import config
from strategies.kelly import kelly_fraction
from strategies.sizing import drawdown_multiplier, signal_health_multiplier
from utils.logger import log


def calculate_position(
    bankroll: float,
    market_price: float,
    true_prob: float,
    peak_equity: float,
    recent_trades: list[dict],
    current_equity: float | None = None,
) -> dict:
    """Calculate final position size through the full quant pipeline.

    Final_size = Raw_Kelly * DD_mult * Signal_mult * Config_mult
    Capped at: min(Final_size, bankroll * MAX_POSITION_PCT)

    Args:
        bankroll: Available cash for trading
        current_equity: Total equity (cash + positions) for drawdown calc.
                       Falls back to bankroll if not provided.

    Returns dict with size and all intermediate values for logging/storage.
    """
    raw_kelly = kelly_fraction(market_price, true_prob)
    equity_for_dd = current_equity if current_equity is not None else bankroll
    dd_mult = drawdown_multiplier(equity_for_dd, peak_equity)
    sig_mult = signal_health_multiplier(recent_trades)
    config_mult = config.KELLY_FRACTION

    final_fraction = raw_kelly * dd_mult * sig_mult * config_mult
    position_usd = bankroll * final_fraction
    max_usd = bankroll * config.MAX_POSITION_PCT
    position_usd = min(position_usd, max_usd)

    # Enforce minimum order size
    if position_usd < config.MIN_ORDER_SIZE_USD:
        position_usd = 0.0

    shares = position_usd / market_price if market_price > 0 else 0.0

    result = {
        "position_usd": round(position_usd, 2),
        "shares": round(shares, 2),
        "raw_kelly": round(raw_kelly, 4),
        "dd_multiplier": dd_mult,
        "signal_multiplier": sig_mult,
        "config_multiplier": config_mult,
        "final_fraction": round(final_fraction, 4),
    }

    log.info(
        "Position sizing: $%.2f | Kelly=%.3f DD=%.1f Sig=%.1f Cfg=%.2f -> frac=%.4f",
        position_usd, raw_kelly, dd_mult, sig_mult, config_mult, final_fraction,
    )

    return result
