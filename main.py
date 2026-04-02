"""Polymarket Trading Bot — Crypto Sniper

Real-time crypto 5-minute market sniper.
Detects Binance price momentum and trades Polymarket binary markets
before they reprice. Pure data-driven — no Claude AI calls.
"""

import sys
import time
import traceback
from datetime import date

# Force unbuffered output so nohup/log files update in real time
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import config
from core import database, market_data, trader, crypto_predictor
from strategies import ev as ev_mod, filters, position_sizing
from strategies.learner import (
    get_edge_adjustment, should_skip_market, get_size_multiplier,
    print_learning_report, classify_market,
)
from strategies.risk import calculate_r_multiple, expectancy, drawdown_multiplier, drawdown
from utils.logger import log

_cycle_count = 0
_daily_pnl = 0.0
_daily_date = None
_session_start_bankroll = 0.0
_dead_tokens = set()  # Token IDs with no orderbook — stop retrying


def _is_junk_market(question: str) -> bool:
    """Filter out sports, entertainment, and random guessing markets."""
    q_lower = question.lower()
    for keyword in config.SPORTS_KEYWORDS:
        if keyword in q_lower:
            return True
    return False


def _has_data_edge(question: str) -> bool:
    """Only trade crypto up/down markets — we have real-time Binance edge.

    In crypto-only mode (MAX_CLAUDE_CALLS == 0), we ONLY trade markets
    where we can detect real-time price momentum from exchanges.
    """
    q_lower = question.lower()

    # Crypto up/down markets — always have Binance momentum edge
    crypto_terms = {"bitcoin", "btc", "ethereum", "eth", "solana", "sol",
                    "xrp", "dogecoin", "doge", "cardano", "crypto",
                    "up or down"}
    if any(t in q_lower for t in crypto_terms):
        return True

    # Everything else — no edge without Claude
    return False


def _daily_loss_limit(bankroll: float) -> float:
    """Max daily loss allowed based on bankroll."""
    return (bankroll / 10.0) * config.DAILY_LOSS_PER_10


def _print_banner(bankroll: float):
    mode = "LIVE TRADING" if not config.DRY_RUN else "DRY RUN"
    mode_icon = "LIVE TRADING" if not config.DRY_RUN else "DRY RUN"
    print()
    print("======================================================")
    print("               CRYPTO SNIPER                          ")
    print(f"  Mode:        {mode_icon:<39}")
    print(f"  Bankroll:    ${bankroll:<39.2f}")
    print(f"  Min edge:    {config.MIN_EDGE:.0%}{'':<37}")
    print(f"  Kelly:       {config.KELLY_FRACTION:.0%} (Quarter-Kelly){'':<24}")
    print(f"  Max position: {config.MAX_POSITION_PCT:.0%} of bankroll{'':<26}")
    print(f"  Cycle:       {config.CYCLE_INTERVAL_SEC}s{'':<36}")
    print(f"  Momentum:    >{config.MOMENTUM_THRESHOLD_MEDIUM}% (med) >{config.MOMENTUM_THRESHOLD_STRONG}% (strong)")
    print(f"  Claude:      DISABLED (pure data-driven){'':<16}")
    print("======================================================")
    print()
    if not config.DRY_RUN:
        print("WARNING: LIVE TRADING MODE ENABLED")
        print("Real money will be used.")
        print()


def _print_portfolio(bankroll: float, open_trades: list[dict], recent_trades: list[dict]):
    global _daily_pnl

    # Fetch LIVE position values from Polymarket
    live_positions = trader.get_positions()
    live_pos_value = sum(float(p.get("currentValue", 0)) for p in (live_positions or []) if float(p.get("size", 0)) > 0)
    num_positions = sum(1 for p in (live_positions or []) if float(p.get("size", 0)) > 0)
    total_equity = bankroll + live_pos_value
    daily_limit = _daily_loss_limit(total_equity)

    session_profit = total_equity - _session_start_bankroll
    session_pct = (session_profit / _session_start_bankroll * 100) if _session_start_bankroll > 0 else 0.0

    print()
    print("------------- PORTFOLIO ---------------")
    print(f"  Session start:     ${_session_start_bankroll:<20.2f}")
    print(f"  Cash:              ${bankroll:<20.2f}")
    print(f"  Positions (live):  ${live_pos_value:<20.2f}")
    print(f"  Total equity:      ${total_equity:<20.2f}")
    print(f"  Session profit:    ${session_profit:<+20.2f}")
    print(f"  Session growth:    {session_pct:<+20.1f}%")
    print(f"  Open positions:    {num_positions:<21}")
    print(f"  Available to trade:${bankroll:<20.2f}")
    print(f"  Daily P&L:        ${_daily_pnl:<+20.2f}")
    print(f"  Daily loss limit:  ${daily_limit:<20.2f}")

    if recent_trades:
        wins = sum(1 for t in recent_trades if (t.get("pnl") or 0) > 0)
        losses = sum(1 for t in recent_trades if (t.get("pnl") or 0) <= 0)
        win_rate = wins / len(recent_trades) if recent_trades else 0
        exp = expectancy(recent_trades)
        print(f"  Win rate:          {win_rate:.1%} ({wins}W / {losses}L of {len(recent_trades)} trades)")
        print(f"  Expectancy:        {exp:<+20.2f}R")
    else:
        print(f"  Win rate:          No trades yet")

    print("---------------------------------------")
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

    print(f"\n[INFO] === CYCLE {_cycle_count} STARTING ===")

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

    # Fetch LIVE position values from Polymarket (not stale DB cost basis)
    live_positions = trader.get_positions()
    live_pos_value = sum(float(p.get("currentValue", 0)) for p in (live_positions or []) if float(p.get("size", 0)) > 0)
    total_equity = bankroll + live_pos_value

    print(f"[INFO] Cash: ${bankroll:.2f} | Positions: ${live_pos_value:.2f} | Total: ${total_equity:.2f} | Peak: ${peak_equity:.2f} | Open: {len(live_positions or [])}")

    # --- Step 2: Check exit conditions ---
    print("[INFO] Step 2: Checking exit conditions...")

    # Drawdown check — use total equity (cash + positions), not just cash
    dd_mult = drawdown_multiplier(total_equity, peak_equity)
    dd_pct = drawdown(total_equity, peak_equity)

    # Always check existing positions for stop loss / take profit
    _check_existing_positions(open_trades)

    if dd_mult == 0.0:
        print(f"[STOP] DRAWDOWN HALT: {dd_pct:.1%} drawdown exceeds {config.DD_THRESHOLD_STOP:.0%} limit")
        print(f"[INFO] Monitoring positions only — no new orders")
        _record_equity(bankroll, live_pos_value)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    if dd_mult < 1.0:
        print(f"[WARN] Drawdown at {dd_pct:.1%} — position sizes halved")

    # Daily loss limit
    daily_limit = _daily_loss_limit(total_equity)
    if _daily_pnl <= -daily_limit:
        print(f"[STOP] DAILY LOSS LIMIT: ${_daily_pnl:.2f} exceeds -${daily_limit:.2f}")
        _record_equity(bankroll, live_pos_value)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    # --- Step 3: Check position limit ---
    if len(open_trades) >= config.MAX_OPEN_POSITIONS:
        print(f"[INFO] Max positions ({config.MAX_OPEN_POSITIONS}) reached, monitoring only")
        _record_equity(bankroll, live_pos_value)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    # --- Step 4: Scan markets ---
    print("[INFO] Step 3: Scanning crypto markets...")
    markets = market_data.get_active_markets(limit=config.MAX_MARKETS_PER_CYCLE)
    if not markets:
        print("[INFO] No markets pass filters")
        _record_equity(bankroll, live_pos_value)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    # Filter out junk (sports/entertainment)
    before_filter = len(markets)
    markets = [m for m in markets if not _is_junk_market(m.get("question", ""))]
    junk_count = before_filter - len(markets)

    # ONLY trade markets where we have a real data edge
    before_edge = len(markets)
    markets = [m for m in markets if _has_data_edge(m.get("question", ""))]
    no_edge_count = before_edge - len(markets)

    if junk_count or no_edge_count:
        print(f"[INFO] Filtered {junk_count} junk + {no_edge_count} no-edge markets")

    # Skip markets we already have positions in
    open_token_ids = {t["token_id"] for t in open_trades}
    markets = [m for m in markets if not any(tid in open_token_ids for tid in m.get("token_ids", []))]

    # CRYPTO ONLY — only keep crypto up/down markets
    crypto_markets = [m for m in markets if crypto_predictor.is_crypto_updown_market(m.get("question", ""))]
    markets = crypto_markets
    print(f"[INFO] {len(crypto_markets)} crypto up/down markets to evaluate")

    # --- Step 5: Evaluate crypto markets (Binance momentum — no Claude) ---
    print("[INFO] Step 4: Evaluating crypto markets (Binance momentum)...")
    candidates = []
    for market in crypto_markets:
        candidate = _evaluate_market(market, bankroll, peak_equity, recent_trades)
        if candidate:
            candidates.append(candidate)
            print(f"  CRYPTO HIT: {candidate['question'][:50]} | Edge: {candidate['edge']:.1%}")

    # --- Step 6: Top opportunities ---
    if not candidates:
        print("[INFO] No opportunities found with sufficient edge")
        _record_equity(bankroll, live_pos_value)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    print(f"\n[INFO] Step 5: Top opportunities found ({len(candidates)}):")
    for c in candidates:
        print(f"  CRYPTO {c['question'][:50]}")
        print(f"    {c['outcome']} @ {c['market_price']:.2f} | Edge: {c['edge']:.1%} | EV: ${c['ev']:.3f}")

    # --- Step 7: Execute trades ---
    print("\n[INFO] Step 6: Executing trades...")
    slots_available = config.MAX_OPEN_POSITIONS - len(open_trades)
    trades_placed = 0

    for candidate in candidates[:slots_available]:
        placed = _execute_trade(candidate, bankroll, peak_equity, recent_trades, total_equity)
        if placed:
            trades_placed += 1

    if trades_placed == 0:
        print("[INFO] No trades executed this cycle")
    else:
        print(f"[INFO] {trades_placed} trade(s) placed")

    # --- Step 8: Record & summarize ---
    _record_equity(bankroll, live_pos_value)
    _print_portfolio(bankroll, open_trades, recent_trades)

    # Print learning report every 5 cycles
    if _cycle_count % 5 == 0:
        print_learning_report()


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

    # Skip markets resolving in the past or within 2 minutes (too late to trade)
    end_date = market.get("end_date") or market.get("endDate") or ""
    if end_date:
        from datetime import datetime, timezone
        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            minutes_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 60
            if minutes_left < 2:
                print(f"  [SKIP] Too close to resolution ({minutes_left:.0f}m left): '{question[:40]}'")
                return None
        except (ValueError, TypeError):
            pass

    # Only evaluate crypto up/down markets
    if not crypto_predictor.is_crypto_updown_market(question):
        return None

    # LEARNING: Skip categories that have been losing money
    skip, reason = should_skip_market(question)
    if skip:
        print(f"  [LEARN] Skipping '{question[:40]}' — {reason}")
        return None

    # Use crypto momentum predictor
    estimate = crypto_predictor.estimate_crypto_probability(
        question, yes_price, outcomes[0] if outcomes else "Yes"
    )
    if not estimate:
        return None

    claude_prob = estimate["probability"]

    # Determine which side to trade
    yes_edge = ev_mod.edge(claude_prob, yes_price)
    no_edge = ev_mod.edge(1.0 - claude_prob, no_price)

    # Apply long-shot bias correction
    yes_edge_adj = filters.longshot_bias_correction(yes_price, yes_edge)
    no_edge_adj = filters.longshot_bias_correction(no_price, no_edge)

    # LEARNING: Adjust edge based on historical category performance
    edge_adj = get_edge_adjustment(question, yes_price)
    if edge_adj != 0:
        cat = classify_market(question)
        yes_edge_adj += edge_adj
        no_edge_adj += edge_adj
        if edge_adj > 0:
            print(f"  [LEARN] Edge boost +{edge_adj:.2%} for '{cat}' (winning category)")
        else:
            print(f"  [LEARN] Edge penalty {edge_adj:.2%} for '{cat}' (losing category)")

    min_edge = config.MIN_EDGE_CRYPTO

    # Pick the better side
    if yes_edge_adj > no_edge_adj and yes_edge_adj >= min_edge:
        side = "BUY"
        outcome = outcomes[0]
        token_id = token_ids[0]
        market_price = yes_price
        true_prob = claude_prob
        edge_val = yes_edge_adj
    elif no_edge_adj >= min_edge:
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


def _execute_trade(candidate: dict, bankroll: float, peak_equity: float, recent_trades: list[dict], total_equity: float = 0.0) -> bool:
    """Size and execute a trade. Returns True if trade was placed."""
    sizing = position_sizing.calculate_position(
        bankroll=bankroll,
        market_price=candidate["market_price"],
        true_prob=candidate["true_prob"],
        peak_equity=peak_equity,
        recent_trades=recent_trades,
        current_equity=total_equity if total_equity > 0 else bankroll,
    )

    if sizing["position_usd"] <= 0:
        return False

    price = candidate["market_price"] + config.PRICE_IMPROVEMENT
    price = max(0.01, min(0.99, round(price, 2)))
    shares = sizing["shares"]

    # LEARNING: Adjust size based on category performance
    size_mult = get_size_multiplier(candidate["question"])
    if size_mult != 1.0:
        shares = max(5, shares * size_mult)
        cat = classify_market(candidate["question"])
        label = "boosted" if size_mult > 1.0 else "reduced"
        print(f"  [LEARN] Size {label} to {size_mult:.0%} for '{cat}'")

    cost = shares * price

    # Cap crypto 5-min market bets at CRYPTO_MAX_POSITION_PCT of bankroll
    max_cost = bankroll * config.CRYPTO_MAX_POSITION_PCT
    if cost > max_cost:
        shares = max(5, max_cost / price)
        cost = shares * price
        print(f"  [CAP] Crypto 5-min bet capped: ${cost:.2f} (max {config.CRYPTO_MAX_POSITION_PCT:.0%} of bankroll)")

    print(f"\n  == PLACING ORDER ===========================")
    print(f"  Market:  {candidate['question'][:42]}")
    print(f"  Side:    {candidate['side']} {candidate['outcome']}")
    print(f"  Price:   ${price:.2f}")
    print(f"  Shares:  {shares:.1f}")
    print(f"  Cost:    ${cost:.2f}")
    print(f"  Edge:    {candidate['edge']:.1%}")
    print(f"  Kelly:   {sizing['raw_kelly']:.3f}")
    print(f"  ============================================")

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
        print(f"  Order placed: {order_id}")
        return True
    else:
        print("  Order failed")
        return False


def _check_existing_positions(open_trades: list[dict]):
    """Check REAL Polymarket positions for stop loss, take profit, or resolution.

    Only manages positions that actually filled (exist on Polymarket),
    not unfilled open orders sitting in the DB.
    """
    global _daily_pnl

    # Get REAL positions from Polymarket — these are shares we actually own
    live_positions = trader.get_positions()
    if not live_positions:
        return

    # Build lookup: token_id -> live position data
    live_by_token = {}
    for p in live_positions:
        if float(p.get("size", 0)) > 0:
            live_by_token[p.get("asset", "")] = p

    for trade in open_trades:
        token_id = trade["token_id"]
        entry_price = trade["entry_price"]
        question = trade.get("market_question", "")

        # Skip tokens we already know are dead/resolved
        if token_id in _dead_tokens:
            continue

        # ONLY manage positions we actually own on Polymarket
        live_pos = live_by_token.get(token_id)
        if not live_pos:
            # Order never filled — mark as cancelled in DB, don't try to sell
            if trade.get("order_id") != "imported":
                database.update_trade_result(trade["id"], entry_price, 0.0, 0.0, "cancelled")
                print(f"  [CANCEL] Order never filled: '{question[:40]}'")
            continue

        # Use real position size from Polymarket, not what we ordered
        real_size = float(live_pos.get("size", 0))
        avg_price = float(live_pos.get("avgPrice", entry_price))
        current_price = float(live_pos.get("curPrice", 0))

        if current_price <= 0:
            current_price = trader.get_midpoint(token_id)
        if current_price <= 0:
            # No price available — mark as dead to stop retrying
            _dead_tokens.add(token_id)
            print(f"  [DEAD] No price for token {token_id[:20]}... — skipping future checks")
            continue

        # Use tighter take-profit for crypto 5-min markets
        is_crypto = crypto_predictor.is_crypto_updown_market(question)
        tp_threshold = config.TAKE_PROFIT_CRYPTO_PCT if is_crypto else config.TAKE_PROFIT_PCT

        # Take profit
        gain_pct = (current_price - avg_price) / avg_price if avg_price > 0 else 0
        if gain_pct >= tp_threshold:
            pnl = (current_price - avg_price) * real_size
            r_mult = calculate_r_multiple(avg_price, current_price, avg_price)
            database.update_trade_result(trade["id"], current_price, pnl, r_mult, "won")
            _daily_pnl += pnl
            label = "CRYPTO TP" if is_crypto else "TAKE PROFIT"
            print(f"  {label}: '{question[:40]}' | +{gain_pct:.0%} | PnL=${pnl:.2f}")

            # Cancel pending orders to free balance, then sell
            if not config.DRY_RUN:
                try:
                    client = trader.get_client()
                    client.cancel_all()
                except Exception:
                    pass
                sell_price = max(0.01, min(0.99, round(current_price - 0.01, 2)))
                trader.place_limit_order(token_id, sell_price, real_size, "SELL")
            continue

        # Stop loss: exit if down past threshold
        loss_pct = (avg_price - current_price) / avg_price if avg_price > 0 else 0
        if loss_pct >= config.STOP_LOSS_PCT:
            pnl = (current_price - avg_price) * real_size
            r_mult = calculate_r_multiple(avg_price, current_price, avg_price)
            database.update_trade_result(trade["id"], current_price, pnl, r_mult, "lost")
            _daily_pnl += pnl
            print(f"  STOP LOSS: '{question[:40]}' | PnL=${pnl:.2f}")

            # Cancel pending orders to free balance, then sell
            if not config.DRY_RUN:
                try:
                    client = trader.get_client()
                    client.cancel_all()
                except Exception:
                    pass
                trader.place_limit_order(token_id, current_price, real_size, "SELL")
            continue

        # Check if market resolved (price near 0 or 1)
        if current_price >= 0.95:
            pnl = (1.0 - entry_price) * trade["size"]
            r_mult = calculate_r_multiple(entry_price, 1.0, entry_price)
            database.update_trade_result(trade["id"], 1.0, pnl, r_mult, "won")
            _daily_pnl += pnl
            print(f"  WIN: '{trade['market_question'][:40]}' | PnL=${pnl:+.2f} | R={r_mult:+.2f}")
        elif current_price <= 0.05:
            pnl = -entry_price * trade["size"]
            r_mult = calculate_r_multiple(entry_price, 0.0, entry_price)
            database.update_trade_result(trade["id"], 0.0, pnl, r_mult, "lost")
            _daily_pnl += pnl
            print(f"  LOSS: '{trade['market_question'][:40]}' | PnL=${pnl:.2f} | R={r_mult:.2f}")


def _record_equity(bankroll: float, positions_value: float = 0.0):
    """Snapshot current equity state."""
    total_eq = bankroll + positions_value
    peak = database.get_peak_equity()
    new_peak = max(peak, total_eq) if peak > 0 else total_eq
    dd = drawdown(total_eq, new_peak)

    database.record_equity_snapshot(
        balance=bankroll,
        positions_value=positions_value,
        total_equity=total_eq,
        drawdown=dd,
        peak_equity=new_peak,
    )


def _cancel_all_open_orders():
    """Cancel all existing open orders on startup to free up capital."""
    print("[INFO] Cancelling all open orders to free capital...")
    try:
        client = trader.get_client()
        result = client.cancel_all()
        print(f"[INFO] cancel_all response: {result}")

        # Also mark them as cancelled in our database
        db_open = database.get_open_trades()
        for trade in db_open:
            database.update_trade_result(trade["id"], trade["entry_price"], 0.0, 0.0, "cancelled")
        if db_open:
            print(f"[INFO] Marked {len(db_open)} DB trades as cancelled")

        # Reset peak equity to current balance so drawdown calculation starts fresh
        bankroll = trader.get_balance()
        if bankroll > 0:
            database.reset_peak_equity(bankroll)
            print(f"[INFO] Peak equity reset to current balance: ${bankroll:.2f}")

        return len(db_open)
    except Exception as e:
        print(f"[WARN] Failed to cancel orders: {e}")
        return 0


def _sync_polymarket_positions():
    """Fetch real positions from Polymarket and sync into local DB.
    Returns total position value."""
    positions = trader.get_positions()
    total_value = 0.0
    synced = 0
    for p in (positions or []):
        size = float(p.get("size", 0))
        if size <= 0:
            continue
        token_id = p.get("asset", "")
        title = p.get("title", p.get("market", "unknown"))
        avg_price = float(p.get("avgPrice", p.get("price", 0.5)))
        cur_value = float(p.get("currentValue", 0))
        total_value += cur_value

        # Check if we already track this position in DB
        open_trades = database.get_open_trades()
        already_tracked = any(t["token_id"] == token_id for t in open_trades)
        if not already_tracked and token_id:
            # Import existing Polymarket position into our DB
            database.record_trade(
                market_id=p.get("conditionId", "imported"),
                market_question=title,
                token_id=token_id,
                side="BUY",
                outcome=p.get("outcome", "Yes"),
                entry_price=avg_price,
                size=size,
                cost=size * avg_price,
                order_id="imported",
                claude_probability=0.0,
                market_probability=avg_price,
                edge=0.0,
                kelly_frac=0.0,
                dd_mult=1.0,
                signal_mult=1.0,
            )
            synced += 1
            print(f"  [SYNC] {title[:50]} | {size:.1f} shares @ ${avg_price:.3f} | Value: ${cur_value:.2f}")

    if synced > 0:
        print(f"[INFO] Synced {synced} existing Polymarket positions into DB")
    return total_value


def main():
    """Main loop — crypto sniper running every 15 seconds."""
    global _session_start_bankroll
    database.init_db()

    # Get initial balance AND positions for true total equity
    bankroll = trader.get_balance()
    print(f"[INFO] Cash balance: ${bankroll:.2f}")
    print("[INFO] Syncing existing Polymarket positions...")
    positions_value = _sync_polymarket_positions()
    total_equity = bankroll + positions_value
    _session_start_bankroll = total_equity
    print(f"[INFO] Total equity (cash + positions): ${total_equity:.2f}")
    _print_banner(total_equity)

    # Always reset peak equity to current total equity on startup
    # This prevents stale high peaks from blocking all trades via DD=0.0
    database.reset_peak_equity(total_equity)
    print(f"[INFO] Peak equity reset to ${total_equity:.2f}")

    print(f"[INFO] Bot started | Cycle interval: {config.CYCLE_INTERVAL_SEC}s")
    print(f"[INFO] Markets: Crypto 5-min Up/Down ONLY")
    print(f"[INFO] Strategy: Real-time Binance momentum detection")
    print(f"[INFO] Claude: DISABLED (pure data-driven)")
    print()

    while True:
        try:
            run_cycle()
        except KeyboardInterrupt:
            print("\n[INFO] Shutting down Crypto Sniper...")
            break
        except Exception:
            print(f"[ERROR] Cycle failed:\n{traceback.format_exc()}")

        # Between full cycles, do fast checks on crypto positions
        print(f"\n[INFO] Next cycle in {config.CYCLE_INTERVAL_SEC}s...")
        elapsed = 0
        try:
            while elapsed < config.CYCLE_INTERVAL_SEC:
                sleep_time = min(config.CRYPTO_CHECK_INTERVAL_SEC, config.CYCLE_INTERVAL_SEC - elapsed)
                time.sleep(sleep_time)
                elapsed += sleep_time

                # Quick check crypto positions for take-profit/stop-loss
                if elapsed < config.CYCLE_INTERVAL_SEC:
                    open_trades = database.get_open_trades()
                    crypto_trades = [t for t in open_trades if crypto_predictor.is_crypto_updown_market(t.get("market_question", ""))]
                    if crypto_trades:
                        _check_existing_positions(crypto_trades)
        except KeyboardInterrupt:
            print("\n[INFO] Shutting down Crypto Sniper...")
            break


if __name__ == "__main__":
    main()
