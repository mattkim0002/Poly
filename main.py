"""Polymarket Trading Bot — Bot Trader

Runs a 5-minute cycle with full quant stack:
Kelly Criterion, drawdown protection, long-shot bias,
correlation filter, rolling win rate, R-multiples.
"""

import sys
import time
import traceback
from datetime import date

# Force unbuffered output so nohup/log files update in real time
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import config
from core import database, market_data, trader, analyzer, crypto_predictor
from strategies import ev as ev_mod, filters, position_sizing
from strategies.risk import calculate_r_multiple, expectancy, drawdown_multiplier, drawdown
from utils.logger import log

_cycle_count = 0
_daily_pnl = 0.0
_daily_date = None


def _is_sports_market(question: str) -> bool:
    """Filter out sports markets."""
    q_lower = question.lower()
    for keyword in config.SPORTS_KEYWORDS:
        if keyword in q_lower:
            return True
    return False


def _is_preferred_market(question: str) -> bool:
    """Check if market matches preferred categories (crypto, politics, etc)."""
    q_lower = question.lower()
    for keyword in config.PREFERRED_KEYWORDS:
        if keyword in q_lower:
            return True
    return False


def _daily_loss_limit(bankroll: float) -> float:
    """Max daily loss allowed based on bankroll."""
    return (bankroll / 10.0) * config.DAILY_LOSS_PER_10


def _print_banner(bankroll: float):
    mode = "LIVE TRADING" if not config.DRY_RUN else "DRY RUN"
    mode_icon = "⚠️  LIVE TRADING ⚠️" if not config.DRY_RUN else "🧪 DRY RUN"
    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║                    BOT TRADER                       ║")
    print(f"║  Mode:        {mode_icon:<39}║")
    print(f"║  Bankroll:    ${bankroll:<39.2f}║")
    print(f"║  Min edge:    {config.MIN_EDGE:.0%}{'':<37}║")
    print(f"║  Kelly:       {config.KELLY_FRACTION:.0%} (Quarter-Kelly){'':<24}║")
    print(f"║  Max position: {config.MAX_POSITION_PCT:.0%} of bankroll{'':<26}║")
    print("╚══════════════════════════════════════════════════════╝")
    print()
    if not config.DRY_RUN:
        print("⚠️  WARNING: LIVE TRADING MODE ENABLED")
        print("Real money will be used.")
        print()


def _print_portfolio(bankroll: float, open_trades: list[dict], recent_trades: list[dict]):
    global _daily_pnl

    total_exposure = sum(t.get("cost", 0) for t in open_trades)
    available = bankroll
    daily_limit = _daily_loss_limit(bankroll + total_exposure)

    total_pnl = sum(t.get("pnl") or 0 for t in recent_trades)

    print()
    print("┌─────────────── PORTFOLIO ───────────────┐")
    print(f"│  Open positions:    {len(open_trades):<21}│")
    print(f"│  Total exposure:    ${total_exposure:<20.2f}│")
    print(f"│  Available capital: ${available:<20.2f}│")
    print(f"│  Daily P&L:        ${_daily_pnl:<+20.2f}│")
    print(f"│  Daily loss limit:  ${daily_limit:<20.2f}│")

    if recent_trades:
        wins = sum(1 for t in recent_trades if (t.get("pnl") or 0) > 0)
        win_rate = wins / len(recent_trades) if recent_trades else 0
        exp = expectancy(recent_trades)
        print(f"│  Win rate:          {win_rate:<20.1%}│")
        print(f"│  Expectancy:        {exp:<+20.2f}R │")

    print("└─────────────────────────────────────────┘")
    print()


def run_cycle():
    """Execute one trading cycle."""
    global _cycle_count, _daily_pnl, _daily_date
    _cycle_count += 1

    # Reset daily P&L at midnight
    today = date.today()
    if _daily_date != today:
        _daily_pnl = 0.0
        _daily_date = today

    print(f"\n[INFO] ═══ CYCLE {_cycle_count} STARTING ═══")

    # --- Step 1: Get account state ---
    print("[INFO] Step 1: Checking account state...")
    bankroll = trader.get_balance()
    if bankroll <= 0:
        print("[WARN] Zero balance — account may be empty or unreadable. Skipping cycle.")
        return

    peak_equity = database.get_peak_equity()
    if peak_equity <= 0:
        peak_equity = bankroll

    recent_trades = database.get_recent_trades(config.WIN_RATE_WINDOW)
    open_trades = database.get_open_trades()

    print(f"[INFO] Bankroll: ${bankroll:.2f} | Peak: ${peak_equity:.2f} | Open: {len(open_trades)}")

    # --- Step 2: Check exit conditions ---
    print("[INFO] Step 2: Checking exit conditions...")

    # Drawdown check
    dd_mult = drawdown_multiplier(bankroll, peak_equity)
    dd_pct = drawdown(bankroll, peak_equity)
    if dd_mult == 0.0:
        print(f"[STOP] DRAWDOWN HALT: {dd_pct:.1%} drawdown exceeds {config.DD_THRESHOLD_STOP:.0%} limit")
        _record_equity(bankroll)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    if dd_mult < 1.0:
        print(f"[WARN] Drawdown at {dd_pct:.1%} — position sizes halved")

    # Daily loss limit
    daily_limit = _daily_loss_limit(bankroll)
    if _daily_pnl <= -daily_limit:
        print(f"[STOP] DAILY LOSS LIMIT: ${_daily_pnl:.2f} exceeds -${daily_limit:.2f}")
        _record_equity(bankroll)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    # Check existing positions for stop loss / resolution
    _check_existing_positions(open_trades)

    # --- Step 3: Check position limit ---
    if len(open_trades) >= config.MAX_OPEN_POSITIONS:
        print(f"[INFO] Max positions ({config.MAX_OPEN_POSITIONS}) reached, monitoring only")
        _record_equity(bankroll)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    # --- Step 4: Scan markets ---
    print("[INFO] Step 3: Scanning markets...")
    markets = market_data.get_active_markets(limit=config.MAX_MARKETS_PER_CYCLE)
    if not markets:
        print("[INFO] No markets pass filters")
        _record_equity(bankroll)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    # Filter out sports markets
    before_sports = len(markets)
    markets = [m for m in markets if not _is_sports_market(m.get("question", ""))]
    if before_sports != len(markets):
        print(f"[INFO] Filtered {before_sports - len(markets)} sports markets")

    # Skip markets we already have positions in
    open_token_ids = {t["token_id"] for t in open_trades}
    markets = [m for m in markets if not any(tid in open_token_ids for tid in m.get("token_ids", []))]

    # Prioritize preferred markets (crypto, politics, climate, geopolitics)
    preferred = [m for m in markets if _is_preferred_market(m.get("question", ""))]
    other = [m for m in markets if not _is_preferred_market(m.get("question", ""))]
    markets = preferred + other
    if preferred:
        print(f"[INFO] {len(preferred)} preferred markets (crypto/politics/climate)")

    print(f"[INFO] {len(markets)} markets to evaluate")

    # --- Step 5: Estimate probabilities ---
    print("[INFO] Step 4: Estimating probabilities with Claude...")
    candidates = []
    for i, market in enumerate(markets[:config.MAX_MARKETS_PER_CYCLE]):
        candidate = _evaluate_market(market, bankroll, peak_equity, recent_trades)
        if candidate:
            candidates.append(candidate)

    # --- Step 6: Top opportunities ---
    if not candidates:
        print("[INFO] No opportunities found with sufficient edge")
        _record_equity(bankroll)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    # Correlation filter
    if len(candidates) > 1:
        before_corr = len(candidates)
        candidates = filters.filter_correlated(candidates)
        if before_corr != len(candidates):
            print(f"[INFO] Filtered {before_corr - len(candidates)} correlated markets")

    print(f"\n[INFO] Step 5: Top opportunities found ({len(candidates)}):")
    for c in candidates:
        print(f"  → {c['question'][:55]}")
        print(f"    {c['outcome']} @ {c['market_price']:.2f} | Edge: {c['edge']:.1%} | EV: ${c['ev']:.3f}")

    # --- Step 7: Execute trades ---
    print("\n[INFO] Step 6: Executing trades...")
    slots_available = config.MAX_OPEN_POSITIONS - len(open_trades)
    trades_placed = 0
    for candidate in candidates[:slots_available]:
        placed = _execute_trade(candidate, bankroll, peak_equity, recent_trades)
        if placed:
            trades_placed += 1

    if trades_placed == 0:
        print("[INFO] No trades executed this cycle")
    else:
        print(f"[INFO] {trades_placed} trade(s) placed")

    # --- Step 8: Record & summarize ---
    _record_equity(bankroll)
    _print_portfolio(bankroll, open_trades, recent_trades)


def _evaluate_market(market: dict, bankroll: float, peak_equity: float, recent_trades: list[dict]) -> dict | None:
    """Evaluate a single market for trading opportunity."""
    question = market["question"]
    outcomes = market.get("outcomes", ["Yes", "No"])
    outcome_prices = market.get("outcome_prices", [])
    token_ids = market.get("token_ids", [])

    if not outcome_prices or not token_ids or len(token_ids) < 2:
        return None

    yes_price = outcome_prices[0] if outcome_prices else 0.0
    no_price = outcome_prices[1] if len(outcome_prices) > 1 else (1.0 - yes_price)

    if yes_price <= 0.01 or yes_price >= 0.99:
        return None

    # Use crypto predictor for Up/Down markets, Claude for everything else
    if crypto_predictor.is_crypto_updown_market(question):
        estimate = crypto_predictor.estimate_crypto_probability(
            question, yes_price, outcomes[0] if outcomes else "Yes"
        )
        if not estimate:
            return None
    else:
        estimate = analyzer.estimate_probability(question, outcomes, outcome_prices)
        if not estimate:
            return None
        if estimate["confidence"] == "low":
            return None

    claude_prob = estimate["probability"]

    # Determine which side to trade
    yes_edge = ev_mod.edge(claude_prob, yes_price)
    no_edge = ev_mod.edge(1.0 - claude_prob, no_price)

    # Apply long-shot bias correction
    yes_edge_adj = filters.longshot_bias_correction(yes_price, yes_edge)
    no_edge_adj = filters.longshot_bias_correction(no_price, no_edge)

    # Pick the better side
    if yes_edge_adj > no_edge_adj and yes_edge_adj >= config.MIN_EDGE:
        side = "BUY"
        outcome = outcomes[0]
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
        return None

    ev_val = ev_mod.expected_value(true_prob, market_price)
    if ev_val < config.MIN_EV_PER_DOLLAR:
        return None

    keywords = filters.extract_keywords(question)
    database.cache_market_keywords(token_id, question, keywords)

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


def _execute_trade(candidate: dict, bankroll: float, peak_equity: float, recent_trades: list[dict]) -> bool:
    """Size and execute a trade. Returns True if trade was placed."""
    sizing = position_sizing.calculate_position(
        bankroll=bankroll,
        market_price=candidate["market_price"],
        true_prob=candidate["true_prob"],
        peak_equity=peak_equity,
        recent_trades=recent_trades,
    )

    if sizing["position_usd"] <= 0:
        return False

    price = candidate["market_price"] + config.PRICE_IMPROVEMENT
    price = max(0.01, min(0.99, round(price, 2)))
    shares = sizing["shares"]
    cost = shares * price

    print(f"\n  ╔═ PLACING ORDER ═══════════════════════════════════╗")
    print(f"  ║  Market:  {candidate['question'][:42]:<43}║")
    print(f"  ║  Side:    {candidate['side']} {candidate['outcome']:<40}║")
    print(f"  ║  Price:   ${price:<42.2f}║")
    print(f"  ║  Shares:  {shares:<42.1f}║")
    print(f"  ║  Cost:    ${cost:<42.2f}║")
    print(f"  ║  Edge:    {candidate['edge']:<42.1%}║")
    print(f"  ║  Kelly:   {sizing['raw_kelly']:<42.3f}║")
    print(f"  ╚═════════════════════════════════════════════════════╝")

    order_id = trader.place_limit_order(
        token_id=candidate["token_id"],
        price=price,
        size=shares,
        side=candidate["side"],
    )

    if order_id:
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
        print(f"  ✅ Order placed: {order_id}")
        return True
    else:
        print("  ❌ Order failed")
        return False


def _check_existing_positions(open_trades: list[dict]):
    """Check open trades for stop loss, take profit, or resolution."""
    global _daily_pnl

    for trade in open_trades:
        token_id = trade["token_id"]
        entry_price = trade["entry_price"]

        current_price = trader.get_midpoint(token_id)
        if current_price <= 0:
            continue

        # Stop loss: exit if down 20%
        loss_pct = (entry_price - current_price) / entry_price if entry_price > 0 else 0
        if loss_pct >= config.STOP_LOSS_PCT:
            pnl = (current_price - entry_price) * trade["size"]
            r_mult = calculate_r_multiple(entry_price, current_price, entry_price)
            database.update_trade_result(trade["id"], current_price, pnl, r_mult, "lost")
            _daily_pnl += pnl
            print(f"  🛑 STOP LOSS: '{trade['market_question'][:40]}' | PnL=${pnl:.2f}")

            # Place sell order to exit
            if not config.DRY_RUN:
                trader.place_limit_order(token_id, current_price, trade["size"], "SELL")
            continue

        # Check if market resolved (price near 0 or 1)
        if current_price >= 0.95:
            pnl = (1.0 - entry_price) * trade["size"]
            r_mult = calculate_r_multiple(entry_price, 1.0, entry_price)
            database.update_trade_result(trade["id"], 1.0, pnl, r_mult, "won")
            _daily_pnl += pnl
            print(f"  ✅ WIN: '{trade['market_question'][:40]}' | PnL=${pnl:+.2f} | R={r_mult:+.2f}")
        elif current_price <= 0.05:
            pnl = -entry_price * trade["size"]
            r_mult = calculate_r_multiple(entry_price, 0.0, entry_price)
            database.update_trade_result(trade["id"], 0.0, pnl, r_mult, "lost")
            _daily_pnl += pnl
            print(f"  ❌ LOSS: '{trade['market_question'][:40]}' | PnL=${pnl:.2f} | R={r_mult:.2f}")


def _record_equity(bankroll: float):
    """Snapshot current equity state."""
    peak = database.get_peak_equity()
    new_peak = max(peak, bankroll) if peak > 0 else bankroll
    dd = drawdown(bankroll, new_peak)

    database.record_equity_snapshot(
        balance=bankroll,
        positions_value=0.0,
        total_equity=bankroll,
        drawdown=dd,
        peak_equity=new_peak,
    )


def main():
    """Main loop — run trading cycle every 5 minutes."""
    database.init_db()

    # Get initial balance for banner
    bankroll = trader.get_balance()
    _print_banner(bankroll)

    print(f"[INFO] Bot started | Cycle interval: {config.CYCLE_INTERVAL_SEC}s")
    print(f"[INFO] Markets: Politics, Economics, Crypto, Climate, Finance, World events")
    print(f"[INFO] Excluded: Sports markets")
    print()

    while True:
        try:
            run_cycle()
        except KeyboardInterrupt:
            print("\n[INFO] Shutting down Bot Trader...")
            break
        except Exception:
            print(f"[ERROR] Cycle failed:\n{traceback.format_exc()}")

        print(f"\n[INFO] Next cycle in {config.CYCLE_INTERVAL_SEC}s...")
        try:
            time.sleep(config.CYCLE_INTERVAL_SEC)
        except KeyboardInterrupt:
            print("\n[INFO] Shutting down Bot Trader...")
            break


if __name__ == "__main__":
    main()
