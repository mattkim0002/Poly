"""Polymarket Trading Bot — Main entry point.

Runs a 5-minute cycle:
1. Fetch active markets
2. Estimate true probabilities with Claude
3. Calculate edge, EV, position size through full quant pipeline
4. Place orders on profitable opportunities
5. Monitor existing positions
6. Track performance metrics
"""

import time
import traceback

import config
from core import database, market_data, trader, analyzer
from strategies import ev as ev_mod, filters, position_sizing
from strategies.risk import calculate_r_multiple, expectancy
from utils.logger import log


def run_cycle():
    """Execute one trading cycle."""
    log.info("=" * 60)
    log.info("Starting trading cycle")

    # --- 1. Get account state ---
    bankroll = trader.get_balance()
    if bankroll <= 0:
        log.warning("Zero balance detected — balance may not be readable or account is empty. Skipping cycle.")
        return

    peak_equity = database.get_peak_equity()
    if peak_equity <= 0:
        peak_equity = bankroll  # first run — current balance is the peak

    recent_trades = database.get_recent_trades(config.WIN_RATE_WINDOW)
    open_trades = database.get_open_trades()

    log.info("Bankroll: $%.2f | Peak: $%.2f | Open trades: %d", bankroll, peak_equity, len(open_trades))

    # --- 2. Check drawdown — might stop trading entirely ---
    from strategies.risk import drawdown_multiplier
    dd_mult = drawdown_multiplier(bankroll, peak_equity)
    if dd_mult == 0.0:
        log.warning("DRAWDOWN STOP: DD > %.0f%%, halting all trading", config.DD_THRESHOLD_STOP * 100)
        _record_equity(bankroll)
        return

    # --- 3. Check open position count ---
    if len(open_trades) >= config.MAX_OPEN_POSITIONS:
        log.info("Max open positions (%d) reached, skipping new trades", config.MAX_OPEN_POSITIONS)
        _check_existing_positions(open_trades)
        _record_equity(bankroll)
        return

    # --- 4. Fetch and evaluate markets ---
    markets = market_data.get_active_markets(limit=config.MAX_MARKETS_PER_CYCLE)
    if not markets:
        log.info("No markets found")
        _check_existing_positions(open_trades)
        _record_equity(bankroll)
        return

    # Skip markets we already have positions in
    open_token_ids = {t["token_id"] for t in open_trades}
    markets = [m for m in markets if not any(tid in open_token_ids for tid in m.get("token_ids", []))]

    candidates = []
    for market in markets[:config.MAX_MARKETS_PER_CYCLE]:
        candidate = _evaluate_market(market, bankroll, peak_equity, recent_trades)
        if candidate:
            candidates.append(candidate)

    log.info("Found %d trade candidates from %d markets", len(candidates), len(markets))

    # --- 5. Correlation filter ---
    if len(candidates) > 1:
        candidates = filters.filter_correlated(candidates)
        log.info("%d candidates after correlation filter", len(candidates))

    # --- 6. Execute trades ---
    slots_available = config.MAX_OPEN_POSITIONS - len(open_trades)
    for candidate in candidates[:slots_available]:
        _execute_trade(candidate, bankroll, peak_equity, recent_trades)

    # --- 7. Check existing positions ---
    _check_existing_positions(open_trades)

    # --- 8. Record equity ---
    _record_equity(bankroll)

    # --- 9. Performance summary ---
    _log_performance(recent_trades)


def _evaluate_market(market: dict, bankroll: float, peak_equity: float, recent_trades: list[dict]) -> dict | None:
    """Evaluate a single market for trading opportunity."""
    question = market["question"]
    outcomes = market.get("outcomes", ["Yes", "No"])
    outcome_prices = market.get("outcome_prices", [])
    token_ids = market.get("token_ids", [])

    if not outcome_prices or not token_ids or len(token_ids) < 2:
        return None

    # Get YES price (first outcome)
    yes_price = outcome_prices[0] if outcome_prices else 0.0
    no_price = outcome_prices[1] if len(outcome_prices) > 1 else (1.0 - yes_price)

    if yes_price <= 0.01 or yes_price >= 0.99:
        return None  # too extreme, no edge possible

    # Ask Claude for true probability
    estimate = analyzer.estimate_probability(question, outcomes, outcome_prices)
    if not estimate:
        return None

    # Only trade on medium+ confidence
    if estimate["confidence"] == "low":
        return None

    claude_prob = estimate["probability"]

    # Determine which side to trade
    # If Claude thinks YES is more likely than market, buy YES
    # If Claude thinks YES is less likely than market, buy NO
    yes_edge = ev_mod.edge(claude_prob, yes_price)
    no_edge = ev_mod.edge(1.0 - claude_prob, no_price)

    # Apply long-shot bias correction
    yes_edge_adj = filters.longshot_bias_correction(yes_price, yes_edge)
    no_edge_adj = filters.longshot_bias_correction(no_price, no_edge)

    # Pick the better side
    if yes_edge_adj > no_edge_adj and yes_edge_adj >= config.MIN_EDGE:
        side = "BUY"
        outcome = outcomes[0]  # Yes
        token_id = token_ids[0]
        market_price = yes_price
        true_prob = claude_prob
        edge_val = yes_edge_adj
    elif no_edge_adj >= config.MIN_EDGE:
        side = "BUY"
        outcome = outcomes[1] if len(outcomes) > 1 else "No"
        token_id = token_ids[1] if len(token_ids) > 1 else token_ids[0]
        market_price = no_price
        true_prob = 1.0 - claude_prob
        edge_val = no_edge_adj
    else:
        return None  # no edge on either side

    # Check EV threshold
    ev_val = ev_mod.expected_value(true_prob, market_price)
    if ev_val < config.MIN_EV_PER_DOLLAR:
        return None

    # Extract keywords for correlation filter
    keywords = filters.extract_keywords(question)
    database.cache_market_keywords(token_id, question, keywords)

    log.info(
        "Candidate: '%s' | %s %s @ %.2f | edge=%.3f EV=%.3f | Claude=%.2f",
        question[:50], side, outcome, market_price, edge_val, ev_val, true_prob,
    )

    return {
        "market_id": market["id"],
        "question": question,
        "token_id": token_id,
        "side": side,
        "outcome": outcome,
        "market_price": market_price,
        "true_prob": true_prob,
        "edge": edge_val,
        "ev": ev_val,
        "keywords": keywords,
        "claude_prob": claude_prob,
    }


def _execute_trade(candidate: dict, bankroll: float, peak_equity: float, recent_trades: list[dict]):
    """Size and execute a trade."""
    sizing = position_sizing.calculate_position(
        bankroll=bankroll,
        market_price=candidate["market_price"],
        true_prob=candidate["true_prob"],
        peak_equity=peak_equity,
        recent_trades=recent_trades,
    )

    if sizing["position_usd"] <= 0:
        log.info("Position too small, skipping: %s", candidate["question"][:50])
        return

    # Place limit order slightly better than midpoint
    price = candidate["market_price"] - config.PRICE_IMPROVEMENT
    price = max(0.01, min(0.99, round(price, 2)))
    shares = sizing["shares"]

    order_id = trader.place_limit_order(
        token_id=candidate["token_id"],
        price=price,
        size=shares,
        side=candidate["side"],
    )

    if order_id:
        cost = shares * price
        database.record_trade(
            market_id=candidate["market_id"],
            market_question=candidate["question"],
            token_id=candidate["token_id"],
            side=candidate["side"],
            outcome=candidate["outcome"],
            entry_price=price,
            size=shares,
            cost=cost,
            order_id=order_id,
            claude_probability=candidate["claude_prob"],
            market_probability=candidate["market_price"],
            edge=candidate["edge"],
            kelly_frac=sizing["raw_kelly"],
            dd_mult=sizing["dd_multiplier"],
            signal_mult=sizing["signal_multiplier"],
        )
        log.info(
            "TRADE: %s %s %.2f shares @ $%.2f ($%.2f) | %s",
            candidate["side"], candidate["outcome"], shares, price, cost,
            candidate["question"][:50],
        )


def _check_existing_positions(open_trades: list[dict]):
    """Check open trades — update status if market resolved or edge flipped."""
    for trade in open_trades:
        token_id = trade["token_id"]
        entry_price = trade["entry_price"]

        current_price = trader.get_midpoint(token_id)
        if current_price <= 0:
            continue

        # Check if market resolved (price near 0 or 1)
        if current_price >= 0.95:
            # Won — resolved in our favor
            pnl = (1.0 - entry_price) * trade["size"]
            r_mult = calculate_r_multiple(entry_price, 1.0, entry_price)
            database.update_trade_result(trade["id"], 1.0, pnl, r_mult, "won")
            log.info("RESOLVED WIN: '%s' | PnL=$%.2f R=%.2f", trade["market_question"][:40], pnl, r_mult)
        elif current_price <= 0.05:
            # Lost — resolved against us
            pnl = -entry_price * trade["size"]
            r_mult = calculate_r_multiple(entry_price, 0.0, entry_price)
            database.update_trade_result(trade["id"], 0.0, pnl, r_mult, "lost")
            log.info("RESOLVED LOSS: '%s' | PnL=$%.2f R=%.2f", trade["market_question"][:40], pnl, r_mult)


def _record_equity(bankroll: float):
    """Snapshot current equity state."""
    peak = database.get_peak_equity()
    new_peak = max(peak, bankroll) if peak > 0 else bankroll
    from strategies.risk import drawdown
    dd = drawdown(bankroll, new_peak)

    database.record_equity_snapshot(
        balance=bankroll,
        positions_value=0.0,  # simplified — would need to price positions
        total_equity=bankroll,
        drawdown=dd,
        peak_equity=new_peak,
    )


def _log_performance(recent_trades: list[dict]):
    """Log performance metrics."""
    if not recent_trades:
        log.info("No closed trades yet — no performance to report")
        return

    wins = sum(1 for t in recent_trades if (t.get("pnl") or 0) > 0)
    total = len(recent_trades)
    win_rate = wins / total if total > 0 else 0

    total_pnl = sum(t.get("pnl") or 0 for t in recent_trades)
    exp = expectancy(recent_trades)

    log.info(
        "Performance (last %d): Win rate=%.1f%% | Total PnL=$%.2f | Expectancy=%.2fR",
        total, win_rate * 100, total_pnl, exp,
    )


def main():
    """Main loop — run trading cycle every 5 minutes."""
    log.info("Polymarket Bot starting")
    log.info("DRY_RUN=%s | Cycle=%ds | Kelly=%.0f%%",
             config.DRY_RUN, config.CYCLE_INTERVAL_SEC, config.KELLY_FRACTION * 100)

    database.init_db()

    while True:
        try:
            run_cycle()
        except KeyboardInterrupt:
            log.info("Shutting down")
            break
        except Exception:
            log.error("Cycle failed:\n%s", traceback.format_exc())

        log.info("Sleeping %d seconds until next cycle...", config.CYCLE_INTERVAL_SEC)
        try:
            time.sleep(config.CYCLE_INTERVAL_SEC)
        except KeyboardInterrupt:
            log.info("Shutting down")
            break


if __name__ == "__main__":
    main()
