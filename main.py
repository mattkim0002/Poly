"""Polymarket Trading Bot — 5-Minute Crypto Markets

Active strategies:
1. ARBITRAGE: Buy Yes+No when total < $1.00 after fees → guaranteed profit
2. MOMENTUM + CLAUDE GATE: Binance signals + Claude Sonnet approval required
3. RESOLUTION SNIPER: Near-certain outcomes at $0.90-$0.96 (optional, off by default)
"""

import sys
import time
import threading
import traceback
from datetime import date, datetime, timezone

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import config
from core import database, market_data, trader, crypto_predictor, news, macro
from core.market_filter import check_trade as market_filter_check
from strategies.risk import calculate_r_multiple, expectancy, drawdown_multiplier, drawdown
from strategies.arbitrage import scan_all_markets, execute_arb
from strategies.binance_lag import scan_binance_lag, claude_gate as binance_lag_claude_gate
from strategies.ev import get_fee_rate, is_fee_free_market
from utils.logger import log

_cycle_count = 0
_daily_pnl = 0.0
_daily_date = None
_session_start_bankroll = 0.0
_dead_tokens = set()

# Post-loss cooldown for Binance-Lag: asset_key -> unix_ts when cooldown expires
_lag_cooldown_until: dict = {}

# Asset key extraction — normalizes coin references in market questions
_ASSET_KEYS = [
    ("bitcoin", "BTC"), ("btc", "BTC"),
    ("ethereum", "ETH"), ("eth", "ETH"),
    ("solana", "SOL"), ("sol", "SOL"),
    ("xrp", "XRP"),
    ("bnb", "BNB"),
    ("dogecoin", "DOGE"), ("doge", "DOGE"),
]


def extract_asset_key(question: str):
    """Return a normalized asset key (BTC/ETH/SOL/XRP/BNB/DOGE) from a market question, or None."""
    q = (question or "").lower()
    for needle, key in _ASSET_KEYS:
        if needle in q:
            return key
    return None


def is_duplicate_exposure(question: str, outcome: str, open_trades: list) -> bool:
    """True if any open trade already holds the same asset + same Yes/No direction.

    This blocks the 'bot stacks 3 BTC bets in one candle' pattern that wiped out
    yesterday's $16 gains. Non-crypto markets (asset key = None) are never blocked.
    """
    key = extract_asset_key(question)
    if not key:
        return False
    out = (outcome or "").lower()
    for t in open_trades:
        if extract_asset_key(t.get("market_question", "")) == key \
                and (t.get("outcome") or "").lower() == out:
            return True
    return False


# ──────────────────────────────────────────────────────────
# STRATEGY 0: THRESHOLD (mean-reversion on extreme prices)
# Buy YES when price <= 28¢ (market too bearish)
# Buy NO when YES price >= 72¢ (market too bullish)
# Only enter after 2.5 minutes into candle (150s)
# ──────────────────────────────────────────────────────────

THRESHOLD_YES_MAX = 0.28   # Buy YES when ask <= this
THRESHOLD_NO_MIN = 0.72    # Buy NO when YES ask >= this
THRESHOLD_TP_YES = 0.95    # Take profit for YES buys
THRESHOLD_TP_NO = 0.05     # Take profit for NO buys
THRESHOLD_SL_YES = 0.45    # Stop loss for YES buys
THRESHOLD_SL_NO = 0.55     # Stop loss for NO buys


def scan_threshold_opportunities(markets: list[dict]) -> list[dict]:
    """Scan crypto markets for extreme-price mean-reversion entries.

    Only enters after 150 seconds into the current 5-minute candle.
    CRITICAL: checks Binance real price before betting against the trend.
    """
    now = datetime.now(timezone.utc)
    seconds_into_candle = (now.minute % 5) * 60 + now.second

    if seconds_into_candle < 150:
        print(f"[THRESHOLD] Waiting for candle maturity ({seconds_into_candle}s < 150s)")
        return []

    opportunities = []

    for market in markets:
        question = (market.get("question") or "")
        q_lower = question.lower()
        token_ids = market.get("token_ids", [])

        # Only crypto up/down markets
        if "up or down" not in q_lower or len(token_ids) < 2:
            continue

        # Check resolution window (2-30 min)
        end_date = market.get("end_date") or market.get("endDate") or ""
        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                minutes_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 60
                if minutes_left < 2 or minutes_left > 30:
                    continue
            except (ValueError, TypeError):
                continue

        yes_token = token_ids[0]
        no_token = token_ids[1]

        # Get real orderbook ask for YES side
        book = trader.get_orderbook(yes_token)
        if not book or not book.get("asks"):
            continue

        yes_ask = book["asks"][0]["price"]
        yes_ask_size = book["asks"][0]["size"]

        # Detect symbol for Binance trend check
        symbol = None
        for coin, sym in crypto_predictor.BINANCE_SYMBOLS.items():
            if coin in q_lower:
                symbol = sym
                break

        if yes_ask <= THRESHOLD_YES_MAX:
            # Market thinks DOWN is very likely — buy YES (mean reversion)
            # BUT: check Binance — if price IS falling, don't fight it
            if symbol:
                binance_prob = crypto_predictor.get_binance_implied_probability(symbol)
                if binance_prob is not None and binance_prob < 0.35:
                    print(f"[THRESHOLD] SKIP YES {question[:50]} — Binance confirms DOWN ({binance_prob:.0%})")
                    continue

            print(f"[THRESHOLD] BUY YES {question[:50]} at {yes_ask*100:.0f}c — TP: 95c SL: 45c")
            opportunities.append({
                "question": question,
                "market_id": market.get("id", ""),
                "token_id": yes_token,
                "outcome": "Yes",
                "ask_price": yes_ask,
                "ask_size": yes_ask_size,
                "tp": THRESHOLD_TP_YES,
                "sl": THRESHOLD_SL_YES,
            })

        elif yes_ask >= THRESHOLD_NO_MIN:
            # Market thinks UP is very likely — buy NO (mean reversion)
            # BUT: check Binance — if price IS rising, don't fight it
            if symbol:
                binance_prob = crypto_predictor.get_binance_implied_probability(symbol)
                if binance_prob is not None and binance_prob > 0.65:
                    print(f"[THRESHOLD] SKIP NO {question[:50]} — Binance confirms UP ({binance_prob:.0%})")
                    continue

            no_book = trader.get_orderbook(no_token)
            if not no_book or not no_book.get("asks"):
                continue
            no_ask = no_book["asks"][0]["price"]
            no_ask_size = no_book["asks"][0]["size"]

            print(f"[THRESHOLD] BUY NO {question[:50]} at {no_ask*100:.0f}c — TP: 5c SL: 55c")
            opportunities.append({
                "question": question,
                "market_id": market.get("id", ""),
                "token_id": no_token,
                "outcome": "No",
                "ask_price": no_ask,
                "ask_size": no_ask_size,
                "tp": THRESHOLD_TP_NO,
                "sl": THRESHOLD_SL_NO,
            })

    return opportunities


def execute_threshold_trade(opp: dict, bankroll: float) -> bool:
    """Execute a threshold mean-reversion trade with a limit order."""
    max_spend = min(bankroll * 0.25, bankroll - 2.0)
    if max_spend < config.MIN_ORDER_SIZE_USD:
        return False

    price = opp["ask_price"]
    max_shares_by_bank = int(max_spend / price) if price > 0 else 0
    max_shares_by_book = int(opp["ask_size"])
    shares = min(max_shares_by_bank, max_shares_by_book)
    shares = max(shares, 5)

    cost = shares * price

    print(f"\n  === THRESHOLD TRADE ===")
    print(f"  Market:  {opp['question'][:55]}")
    print(f"  Side:    {opp['outcome']} @ ${price:.3f} (LIMIT)")
    print(f"  Shares:  {shares} (${cost:.2f})")
    print(f"  TP:      ${opp['tp']:.2f}  SL: ${opp['sl']:.2f}")
    print(f"  =========================")

    order_id = trader.place_limit_order(
        token_id=opp["token_id"],
        price=price,
        size=shares,
        side="BUY",
    )

    if not order_id:
        print(f"  [FAIL] Order failed")
        return False

    print(f"  [SUCCESS] Order placed: {order_id}")

    database.record_trade(
        market_id=opp["market_id"],
        market_question=f"[THRESH] {opp['question'][:80]}",
        token_id=opp["token_id"],
        side="BUY", outcome=opp["outcome"],
        entry_price=price, size=shares, cost=cost,
        order_id=order_id,
        claude_probability=0.0, market_probability=price,
        edge=(1.0 - price) if opp["outcome"] == "Yes" else price,
        kelly_frac=0.0, dd_mult=1.0, signal_mult=1.0,
    )
    return True


def _is_junk_market(question: str) -> bool:
    q_lower = question.lower()
    for keyword in config.SPORTS_KEYWORDS:
        if keyword in q_lower:
            return True
    return False


def _is_allowed_market_duration(question: str) -> bool:
    """Filter for crypto up/down markets.

    Allow ALL crypto up/down markets — Polymarket no longer puts "5 minutes"
    in the question text. They use time ranges like "3:00PM-3:05PM ET" instead.
    """
    return True  # All markets pass — strategy-level filters handle the rest


def _hard_filter_market(market: dict) -> tuple[bool, str]:
    """Hard filters applied before any strategy touches the market.

    Returns (passes, reason). Reason is a short human-readable skip string.

    Filters:
      1) Crypto-only: if CRYPTO_ONLY, skip markets with no crypto keyword.
      2) Anti-coinflip: "Up or Down" markets must resolve in >= MIN_RESOLUTION_HOURS.
      3) Anti-lottery-ticket: no contract priced below MIN_PRICE_FLOOR.
    """
    question = market.get("question", "") or ""
    q_lower = question.lower()

    # 0) Crypto-only filter (runs first — cheapest)
    if getattr(config, "CRYPTO_ONLY", False):
        if not any(k in q_lower for k in config.CRYPTO_KEYWORDS):
            return (False, "not crypto")

    # 1) Resolution-time filter for Up/Down markets
    if "up or down" in q_lower:
        end_date = market.get("end_date") or ""
        if end_date:
            try:
                from datetime import datetime, timezone
                end_dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
                hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
                if hours_left < config.MIN_RESOLUTION_HOURS:
                    mins_left = hours_left * 60.0
                    return (False, f"resolves in {mins_left:.0f} min (under {config.MIN_RESOLUTION_HOURS:.0f}h minimum)")
            except (ValueError, TypeError):
                pass  # Unparseable end_date → let strategy logic handle it

    # 2) Price-floor filter: any contract below MIN_PRICE_FLOOR → skip market
    prices = market.get("outcome_prices") or []
    for price in prices:
        try:
            p = float(price)
        except (TypeError, ValueError):
            continue
        if 0 < p < config.MIN_PRICE_FLOOR:
            return (False, f"price {p*100:.1f}\u00a2 below {config.MIN_PRICE_FLOOR*100:.0f}\u00a2 minimum")

    return (True, "")


def _get_active_strategies() -> list[str]:
    """Return list of currently enabled strategy names."""
    active = []
    if config.ENABLE_ARB:
        active.append("ARB")
    if config.ENABLE_BINANCE_LAG:
        active.append("BINANCE_LAG")
    if config.ENABLE_MOMENTUM_CLAUDE:
        active.append("MOMENTUM_CLAUDE")
    if config.ENABLE_SNIPE:
        active.append("SNIPE")
    return active


# ──────────────────────────────────────────────────────────
# STRATEGY 2: RESOLUTION SNIPER
# Find markets where outcome is near-certain but price < $0.95
# Example: "Will BTC be above $50k on April 7?" and BTC is at $84k
#          → Yes token trading at $0.92 → buy, collect $1.00 at resolution
# ──────────────────────────────────────────────────────────

def scan_resolution_snipes(markets: list[dict]) -> list[dict]:
    """Find near-certain outcomes priced below face value.

    Looks for:
    - Outcomes priced $0.90-$0.96 (market is ~sure but not $1.00 yet)
    - OR outcomes priced $0.04-$0.10 on the OTHER side (same thing)

    Profit = $1.00 - buy_price - fees.
    """
    snipes = []

    for market in markets:
        question = market.get("question", "")
        token_ids = market.get("token_ids", [])
        outcome_prices = market.get("outcome_prices", [])

        if len(token_ids) < 2 or len(outcome_prices) < 2:
            continue

        yes_price = outcome_prices[0]
        no_price = outcome_prices[1] if len(outcome_prices) > 1 else (1.0 - yes_price)

        # Check each side for near-certain pricing
        for i, (token_id, price, outcome) in enumerate([
            (token_ids[0], yes_price, "Yes"),
            (token_ids[1], no_price, "No"),
        ]):
            # We want to buy tokens priced 0.90-0.96
            # Below 0.90 = market isn't sure enough
            # Above 0.96 = not enough profit margin
            if price < 0.90 or price > 0.96:
                continue

            # Get REAL orderbook ask (what we can actually buy at)
            book = trader.get_orderbook(token_id)
            if not book or not book.get("asks"):
                continue

            real_ask = book["asks"][0]["price"]
            ask_size = book["asks"][0]["size"]

            if real_ask > 0.96 or real_ask < 0.90:
                continue

            # Calculate profit after fees
            fee_free = is_fee_free_market(question)
            if fee_free:
                fee_per_share = 0.0
            else:
                fee_rate = get_fee_rate(token_id)
                fee_per_share = fee_rate * real_ask * (1.0 - real_ask)

            profit_per_share = 1.0 - real_ask - fee_per_share

            if profit_per_share < 0.02:  # Need at least 2% profit
                continue

            profit_pct = profit_per_share / real_ask

            snipes.append({
                "question": question,
                "market_id": market.get("id", ""),
                "token_id": token_id,
                "outcome": outcome,
                "ask_price": real_ask,
                "ask_size": ask_size,
                "profit_per_share": profit_per_share,
                "profit_pct": profit_pct,
                "fee_per_share": fee_per_share,
                "fee_free": fee_free,
            })

    snipes.sort(key=lambda x: x["profit_pct"], reverse=True)
    return snipes


def execute_snipe(snipe: dict, bankroll: float) -> bool:
    """Buy a near-certain outcome at a discount."""
    max_spend = min(bankroll * 0.30, bankroll - 2.0)  # Keep $2 reserve
    if max_spend < config.MIN_ORDER_SIZE_USD:
        return False

    price = snipe["ask_price"]
    max_shares_by_bank = int(max_spend / price) if price > 0 else 0
    max_shares_by_book = int(snipe["ask_size"])
    shares = min(max_shares_by_bank, max_shares_by_book)
    shares = max(shares, 5)  # Polymarket minimum

    cost = shares * price
    expected_profit = shares * snipe["profit_per_share"]

    print(f"\n  === RESOLUTION SNIPE ===")
    print(f"  Market:  {snipe['question'][:60]}")
    print(f"  Side:    {snipe['outcome']} @ ${price:.3f}")
    print(f"  Shares:  {shares}")
    print(f"  Cost:    ${cost:.2f}")
    print(f"  Profit:  ${expected_profit:.2f} ({snipe['profit_pct']:.1%})")
    fee_str = "FREE" if snipe['fee_free'] else f"${snipe['fee_per_share']:.4f}/share"
    print(f"  Fees:    {fee_str}")
    print(f"  =========================")

    order_id = trader.place_limit_order(
        token_id=snipe["token_id"],
        price=price,
        size=shares,
        side="BUY",
    )

    if not order_id:
        print(f"  [FAIL] Order failed")
        return False

    print(f"  [SUCCESS] Order placed: {order_id}")

    database.record_trade(
        market_id=snipe["market_id"],
        market_question=f"[SNIPE] {snipe['question'][:80]}",
        token_id=snipe["token_id"],
        side="BUY",
        outcome=snipe["outcome"],
        entry_price=price,
        size=shares,
        cost=cost,
        order_id=order_id,
        claude_probability=0.0,
        market_probability=price,
        edge=snipe["profit_pct"],
        kelly_frac=0.0,
        dd_mult=1.0,
        signal_mult=1.0,
    )
    return True


# ──────────────────────────────────────────────────────────
# POSITION MANAGEMENT
# ──────────────────────────────────────────────────────────

def _check_existing_positions(open_trades: list[dict]):
    """Check open positions for resolution or exit conditions."""
    global _daily_pnl

    live_positions = trader.get_positions()
    if not live_positions:
        return

    live_by_token = {}
    for p in live_positions:
        if float(p.get("size", 0)) > 0:
            live_by_token[p.get("asset", "")] = p

    for trade in open_trades:
        token_id = trade["token_id"]
        entry_price = trade["entry_price"]
        question = trade.get("market_question", "")

        if token_id in _dead_tokens:
            continue

        live_pos = live_by_token.get(token_id)
        if not live_pos:
            if trade.get("order_id") != "imported":
                database.update_trade_result(trade["id"], entry_price, 0.0, 0.0, "cancelled")
                print(f"  [CANCEL] Never filled: '{question[:40]}'")
            continue

        real_size = float(live_pos.get("size", 0))
        avg_price = float(live_pos.get("avgPrice", entry_price))
        current_price = float(live_pos.get("curPrice", 0))

        if current_price <= 0:
            current_price = trader.get_midpoint(token_id)
        if not current_price or current_price <= 0:
            _dead_tokens.add(token_id)
            continue

        # Market resolved → WIN
        if current_price >= 0.95:
            pnl = (1.0 - avg_price) * real_size
            r_mult = calculate_r_multiple(avg_price, 1.0, avg_price)
            database.update_trade_result(trade["id"], 1.0, pnl, r_mult, "won")
            _daily_pnl += pnl
            print(f"  WIN: '{question[:40]}' | PnL=${pnl:+.2f}")

        # Market resolved → LOSS
        elif current_price <= 0.05:
            pnl = -avg_price * real_size
            r_mult = calculate_r_multiple(avg_price, 0.0, avg_price)
            database.update_trade_result(trade["id"], 0.0, pnl, r_mult, "lost")
            _daily_pnl += pnl
            print(f"  LOSS: '{question[:40]}' | PnL=${pnl:.2f}")

        # Threshold trades — custom TP/SL levels
        elif "[THRESH]" in question:
            is_yes = trade.get("outcome") == "Yes"
            tp_level = THRESHOLD_TP_YES if is_yes else THRESHOLD_TP_NO
            sl_level = THRESHOLD_SL_YES if is_yes else THRESHOLD_SL_NO

            # Take profit
            if (is_yes and current_price >= tp_level) or (not is_yes and current_price <= tp_level):
                pnl = (current_price - avg_price) * real_size if is_yes else (avg_price - current_price) * real_size
                pnl = (1.0 - avg_price) * real_size  # Resolution payout
                r_mult = calculate_r_multiple(avg_price, current_price, avg_price)
                database.update_trade_result(trade["id"], current_price, pnl, r_mult, "won")
                _daily_pnl += pnl
                print(f"  [THRESHOLD TP] '{question[:40]}' | PnL=${pnl:+.2f}")
                if not config.DRY_RUN:
                    sell_price = max(0.01, min(0.99, round(current_price - 0.01, 2)))
                    trader.place_limit_order(token_id, sell_price, real_size, "SELL")

            # Stop loss
            elif (is_yes and current_price >= sl_level) or (not is_yes and current_price <= sl_level):
                pnl = (current_price - avg_price) * real_size
                r_mult = calculate_r_multiple(avg_price, current_price, avg_price)
                database.update_trade_result(trade["id"], current_price, pnl, r_mult, "lost")
                _daily_pnl += pnl
                print(f"  [THRESHOLD SL] '{question[:40]}' | PnL=${pnl:.2f}")
                if not config.DRY_RUN:
                    trader.place_limit_order(token_id, current_price, real_size, "SELL")

        # Stop loss for other non-arb positions
        elif "[ARB" not in question and "[SNIPE]" not in question:
            loss_pct = (avg_price - current_price) / avg_price if avg_price > 0 else 0
            if loss_pct >= config.STOP_LOSS_PCT:
                pnl = (current_price - avg_price) * real_size
                r_mult = calculate_r_multiple(avg_price, current_price, avg_price)
                database.update_trade_result(trade["id"], current_price, pnl, r_mult, "lost")
                _daily_pnl += pnl
                print(f"  STOP LOSS: '{question[:40]}' | PnL=${pnl:.2f}")
                if not config.DRY_RUN:
                    trader.place_limit_order(token_id, current_price, real_size, "SELL")


def _record_equity(bankroll: float, positions_value: float = 0.0):
    total_eq = bankroll + positions_value
    peak = database.get_peak_equity()
    new_peak = max(peak, total_eq) if peak > 0 else total_eq
    dd = drawdown(total_eq, new_peak)
    database.record_equity_snapshot(
        balance=bankroll, positions_value=positions_value,
        total_equity=total_eq, drawdown=dd, peak_equity=new_peak,
    )


def _print_portfolio(bankroll: float, open_trades: list[dict], recent_trades: list[dict]):
    global _daily_pnl
    live_positions = trader.get_positions()
    live_pos_value = sum(float(p.get("currentValue", 0)) for p in (live_positions or []) if float(p.get("size", 0)) > 0)
    num_positions = sum(1 for p in (live_positions or []) if float(p.get("size", 0)) > 0)
    total_equity = bankroll + live_pos_value

    session_profit = total_equity - _session_start_bankroll
    session_pct = (session_profit / _session_start_bankroll * 100) if _session_start_bankroll > 0 else 0.0

    print()
    print("------------- PORTFOLIO ---------------")
    print(f"  Cash:              ${bankroll:<20.2f}")
    print(f"  Positions (live):  ${live_pos_value:<20.2f}")
    print(f"  Total equity:      ${total_equity:<20.2f}")
    print(f"  Session profit:    ${session_profit:<+20.2f}")
    print(f"  Open positions:    {num_positions:<21}")
    print(f"  Daily P&L:        ${_daily_pnl:<+20.2f}")

    if recent_trades:
        wins = sum(1 for t in recent_trades if (t.get("pnl") or 0) > 0)
        losses = sum(1 for t in recent_trades if (t.get("pnl") or 0) <= 0)
        exp = expectancy(recent_trades)
        print(f"  Win rate:          {wins}/{wins+losses} ({wins/(wins+losses)*100:.0f}%)" if wins+losses > 0 else "")
        print(f"  Expectancy:        {exp:<+20.2f}R")
    print("---------------------------------------")
    print()


NEWS_GATE_SYSTEM = """You are a risk filter for a Polymarket 5-minute crypto Up/Down trading bot.

You receive raw data: coin, side (UP/DOWN), Polymarket price, Binance momentum (60s/180s price change,
volume ratio, buy pressure, acceleration, 1h/4h trend), orderbook imbalance, recent_headlines (crypto news),
and macro_bias (global crypto outlook from a separate 3h analysis).

Your ONLY job: decide if the bot should APPROVE or REJECT this specific trade.

Reply with ONLY this JSON, nothing else:

{"ok_to_trade": true or false, "reason": "single short sentence", "confidence": "low|medium|high"}

Rules:
1. If Binance 1h/4h trend is AGAINST the trade direction → ok_to_trade = false.
2. If Binance 60s/180s momentum is AGAINST the trade direction → ok_to_trade = false.
3. If volume ratio < 1.0 (no volume spike) → ok_to_trade = false.
4. If buy pressure < 0.45 for UP trades or > 0.55 for DOWN trades → ok_to_trade = false.
5. If a recent headline describes a clear opposing catalyst (hack, SEC action, exchange outage) → ok_to_trade = false.
6. If macro_bias is bearish (high confidence) and side = UP, or bullish (high) and side = DOWN → ok_to_trade = false.
7. If signals are mixed, weak, or unclear → ok_to_trade = false.
8. ONLY ok_to_trade = true when ALL of:
   - Binance short-term momentum supports the trade direction
   - Binance 1h and 4h trends are not against it
   - Volume spike confirms the move (ratio >= 1.5)
   - Buy pressure aligns with direction
   - No contradicting headline
   - Macro bias is neutral or aligned
   - Edge >= 5%

Be extremely conservative. When in doubt, REJECT.
Never write anything outside the JSON."""


def _claude_news_check(question: str, direction: str, edge: float,
                       symbol: str = None, poly_price: float = 0.0) -> bool:
    """Claude risk gate: sends all raw data to Sonnet, requires explicit APPROVE.

    Called on every momentum trade. If Claude rejects, is uncertain, or API fails → skip trade.
    Returns True only if Claude explicitly returns ok_to_trade = true.
    """
    try:
        import json as _json
        import anthropic
        client = anthropic.Anthropic()

        # Gather ALL Binance data for Claude to evaluate
        binance_summary = {}
        if symbol:
            momentum = crypto_predictor.get_realtime_momentum(symbol)
            if momentum:
                binance_summary = {
                    "price": momentum["current_price"],
                    "change_60s": f"{momentum['price_change_60s']:+.3f}%",
                    "change_180s": f"{momentum['price_change_180s']:+.3f}%",
                    "volume_ratio": round(momentum["volume_ratio"], 2),
                    "buy_pressure": round(momentum["buy_pressure"], 2),
                    "accelerating": momentum["is_accelerating"],
                }
            htf = crypto_predictor.get_higher_timeframe_trend(symbol)
            if htf:
                binance_summary["htf_trend_4h"] = htf.get("trend_4h", "unknown")
                binance_summary["htf_trend_1h"] = htf.get("trend_1h", "unknown")
            ob = crypto_predictor.get_orderbook_imbalance(symbol)
            if ob:
                binance_summary["orderbook_imbalance"] = round(ob.get("imbalance", 0.5), 2)

        # Fetch recent crypto headlines for this coin (best-effort, never blocks)
        try:
            coin_label = symbol.replace("USDT", "") if symbol else "Bitcoin"
            recent_headlines = news.fetch_headlines(f"{coin_label} crypto price", max_results=3)
        except Exception:
            recent_headlines = []

        # Current macro bias snapshot
        try:
            bias_snapshot = macro.get_macro_bias()
            macro_payload = {
                "bias": bias_snapshot.get("bias", "neutral"),
                "confidence": bias_snapshot.get("confidence", "low"),
                "reasoning": bias_snapshot.get("reasoning", ""),
            }
        except Exception:
            macro_payload = {"bias": "neutral", "confidence": "low", "reasoning": ""}

        # BTC confirmation (context on whether BTC is dragging alts)
        try:
            btc_confirm = crypto_predictor.get_btc_confirmation(direction)
        except Exception:
            btc_confirm = {"agrees": True, "btc_move_180s_pct": 0.0, "strength": "none"}

        payload = _json.dumps({
            "coin": symbol or "BTC",
            "side": direction,
            "polymarket_price": poly_price,
            "edge": f"{edge:.1%}",
            "market": question[:80],
            "binance": binance_summary,
            "recent_headlines": recent_headlines,
            "macro_bias": macro_payload,
            "btc_confirmation": btc_confirm,
        })

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=120,
            system=NEWS_GATE_SYSTEM,
            messages=[{"role": "user", "content": payload}],
        )
        text = response.content[0].text.strip()
        print(f"  [CLAUDE GATE] {text[:100]}")

        try:
            result = _json.loads(text)
            approved = result.get("ok_to_trade", False) is True
            if not approved:
                print(f"  [CLAUDE GATE] REJECTED: {result.get('reason', 'no reason')}")
            return approved
        except _json.JSONDecodeError:
            print(f"  [CLAUDE GATE] Bad response format — blocking trade")
            return False

    except Exception as e:
        print(f"  [CLAUDE GATE] Failed ({e}) — blocking trade (conservative)")
        return False


def _execute_momentum_trade(trade: dict, bankroll: float) -> bool:
    """Execute a momentum trade with Kelly sizing."""
    edge = trade["edge"]
    price = trade["price"]
    kelly_mult = trade.get("signal", {}).get("kelly_mult", 1.0)

    size_dollars = bankroll * config.KELLY_FRACTION * edge * kelly_mult
    max_size = config.CRYPTO_MAX_POSITION_PCT * bankroll
    size_dollars = min(size_dollars, max_size)

    if size_dollars < config.MIN_ORDER_SIZE_USD:
        return False

    shares = max(5, int(size_dollars / price)) if price > 0 else 5
    cost = shares * price

    print(f"\n  === MOMENTUM TRADE ===")
    print(f"  Market:  {trade['question'][:50]}")
    print(f"  Side:    {trade['outcome']} @ ${price:.3f}")
    print(f"  Edge:    {edge:.1%}")
    print(f"  Shares:  {shares} (${cost:.2f})")

    # Detect symbol from question
    direction = "UP" if trade["outcome"] == "Yes" else "DOWN"
    q_lower = trade["question"].lower()
    symbol = None
    coin_name = ""
    for coin, sym in crypto_predictor.BINANCE_SYMBOLS.items():
        if coin in q_lower:
            symbol = sym
            coin_name = coin.upper()
            break

    # Market structure filter
    if symbol:
        mf = market_filter_check(symbol, direction)
        print(f"  [FILTER] {coin_name} trend={mf['trend']['label']} "
              f"pressure={mf['pressure']['score']:+.2f} {mf['pressure']['label']} "
              f"danger={mf['danger']}")
        if not mf["allowed"]:
            print(f"  [BLOCK] {coin_name} {direction} blocked by {mf['block_reason']}")
            return False
        # Pressure/danger need Claude override
        if mf["needs_claude"]:
            print(f"  [FILTER] Pressure/danger conflict — requiring Claude approval")

    # Claude news gate — reject if news/data contradicts signal
    if not _claude_news_check(trade["question"], direction, edge,
                              symbol=symbol, poly_price=price):
        print(f"  [BLOCKED] Claude rejected — news/data contradicts signal")
        return False

    print(f"  ======================")

    order_id = trader.place_limit_order(
        token_id=trade["token_id"],
        price=price,
        size=shares,
        side="BUY",
    )

    if not order_id:
        print(f"  [FAIL] Order failed")
        return False

    print(f"  [SUCCESS] Order placed: {order_id}")

    database.record_trade(
        market_id=trade["market_id"],
        market_question=trade["question"],
        token_id=trade["token_id"],
        side="BUY", outcome=trade["outcome"],
        entry_price=price, size=shares, cost=cost,
        order_id=order_id,
        claude_probability=trade["probability"],
        market_probability=price,
        edge=edge, kelly_frac=config.KELLY_FRACTION,
        dd_mult=1.0, signal_mult=1.0,
        end_date=trade.get("end_date", ""),
    )
    return True


# ──────────────────────────────────────────────────────────
# MAIN CYCLE
# ──────────────────────────────────────────────────────────

def run_cycle():
    global _cycle_count, _daily_pnl, _daily_date
    _cycle_count += 1

    today = date.today()
    if _daily_date != today:
        _daily_pnl = 0.0
        _daily_date = today

    print(f"\n[INFO] === CYCLE {_cycle_count} ===")

    bankroll = trader.get_balance()
    if bankroll <= 0:
        print("[WARN] Zero balance — skipping cycle")
        return

    # Check existing positions
    open_trades = database.get_open_trades()
    recent_trades = database.get_recent_trades(config.WIN_RATE_WINDOW)

    live_positions = trader.get_positions()
    live_pos_value = sum(float(p.get("currentValue", 0)) for p in (live_positions or []) if float(p.get("size", 0)) > 0)
    num_positions = sum(1 for p in (live_positions or []) if float(p.get("size", 0)) > 0)
    total_equity = bankroll + live_pos_value

    print(f"[INFO] Cash: ${bankroll:.2f} | Positions: ${live_pos_value:.2f} ({num_positions}) | Total: ${total_equity:.2f}")

    # Always check exits
    if open_trades:
        _check_existing_positions(open_trades)

    # Daily loss limit — use SESSION START equity, not remaining cash, so the
    # threshold doesn't shrink as we lose money (that made the halt trigger late)
    daily_loss = _session_start_bankroll - total_equity
    if daily_loss > _session_start_bankroll * config.DAILY_LOSS_LIMIT_PCT:
        print(f"[HALT] Daily loss limit hit (${daily_loss:.2f} vs ${_session_start_bankroll * config.DAILY_LOSS_LIMIT_PCT:.2f} cap)")
        _record_equity(bankroll, live_pos_value)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    # Max positions check
    if num_positions >= config.MAX_OPEN_POSITIONS:
        print(f"[INFO] Max positions ({config.MAX_OPEN_POSITIONS}) reached — monitoring only")
        _record_equity(bankroll, live_pos_value)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    # Fetch ALL markets
    print("[INFO] Scanning all markets...")
    markets = market_data.get_active_markets(limit=config.MAX_MARKETS_PER_CYCLE)
    if not markets:
        print("[INFO] No markets found")
        _record_equity(bankroll, live_pos_value)
        return

    # Filter junk
    markets = [m for m in markets if not _is_junk_market(m.get("question", ""))]

    # Skip markets we already hold
    open_token_ids = {t["token_id"] for t in open_trades}
    markets = [m for m in markets if not any(tid in open_token_ids for tid in m.get("token_ids", []))]

    # Filter out non-allowed durations (blocks hourly/15-min crypto markets)
    markets = [m for m in markets if _is_allowed_market_duration(m.get("question", ""))]

    # Hard filters (lottery-ticket prices + coinflip durations) — logged so you see skips
    hard_filtered = []
    for m in markets:
        passes, reason = _hard_filter_market(m)
        if not passes:
            q_short = (m.get("question", "") or "")[:50]
            print(f"  [FILTER] SKIP {q_short} — {reason}")
            continue
        hard_filtered.append(m)
    markets = hard_filtered

    active = _get_active_strategies()
    crypto_updown = [m for m in markets if "up or down" in m.get("question", "").lower()]
    crypto_all = [m for m in markets if any(k in m.get("question", "").lower()
                  for k in ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "bnb", "doge", "crypto"])]
    print(f"[INFO] {len(markets)} markets to scan ({len(crypto_updown)} up/down, {len(crypto_all)} crypto total) | ACTIVE_STRATEGIES = {active}")

    trades_placed = 0

    # === THRESHOLD (disabled by default) ===
    if config.ENABLE_THRESHOLD:
        print("[INFO] Threshold: Scanning for threshold opportunities...")
        threshold_opps = scan_threshold_opportunities(markets)
        if threshold_opps:
            print(f"  Found {len(threshold_opps)} threshold opportunities!")
            for opp in threshold_opps[:2]:
                if bankroll < config.MIN_ORDER_SIZE_USD:
                    break
                if execute_threshold_trade(opp, bankroll):
                    trades_placed += 1
                    bankroll -= opp["ask_price"] * 5
        else:
            print("[INFO] No threshold opportunities (no extreme prices)")

    # === STRATEGY 1: ARBITRAGE (guaranteed profit) ===
    if config.ENABLE_ARB:
        print("[INFO] [ARB] Scanning for arbitrage...")
        arb_opportunities = scan_all_markets(markets)

        if arb_opportunities:
            # Cap to 1 fill per cycle (was 2) to prevent 15-second capital bursts
            for arb in arb_opportunities[:1]:
                if bankroll < config.MIN_ORDER_SIZE_USD * 2:
                    break
                # Duplicate-exposure gate: arb loads BOTH sides, so skip if asset is already held
                arb_question = arb.get("question", "")
                arb_asset = extract_asset_key(arb_question)
                if arb_asset and any(extract_asset_key(t.get("market_question", "")) == arb_asset for t in open_trades):
                    print(f"  [ARB] BLOCK: already hold {arb_asset}")
                    continue
                result = execute_arb(arb, bankroll)
                if result:
                    trades_placed += 1
                    bankroll -= result["total_cost"]
                    database.record_trade(
                        market_id=result["market_id"],
                        market_question=f"[ARB-YES] {result['question'][:80]}",
                        token_id=result["yes_token"],
                        side="BUY", outcome="Yes",
                        entry_price=result["yes_price"], size=result["shares"],
                        cost=result["shares"] * result["yes_price"],
                        order_id=result["yes_order"],
                        claude_probability=0.0, market_probability=result["yes_price"],
                        edge=result["profit_pct"], kelly_frac=0.0,
                        dd_mult=1.0, signal_mult=1.0,
                    )
                    database.record_trade(
                        market_id=result["market_id"],
                        market_question=f"[ARB-NO] {result['question'][:80]}",
                        token_id=result["no_token"],
                        side="BUY", outcome="No",
                        entry_price=result["no_price"], size=result["shares"],
                        cost=result["shares"] * result["no_price"],
                        order_id=result["no_order"],
                        claude_probability=0.0, market_probability=result["no_price"],
                        edge=result["profit_pct"], kelly_frac=0.0,
                        dd_mult=1.0, signal_mult=1.0,
                    )
                    # Track for in-cycle duplicate-exposure checks of later strategies
                    open_trades.append({"market_question": f"[ARB-YES] {result['question'][:80]}", "outcome": "Yes"})
                    open_trades.append({"market_question": f"[ARB-NO] {result['question'][:80]}", "outcome": "No"})


    # === STRATEGY 2: BINANCE-LAG (directional, Claude Sonnet veto) ===
    if config.ENABLE_BINANCE_LAG and bankroll >= config.MIN_ORDER_SIZE_USD and trades_placed == 0:
        # Auto-disable check: if negative EV over N trades
        lag_trades = [t for t in database.get_recent_trades(config.BINANCE_LAG_AUTO_DISABLE_TRADES)
                      if "[LAG]" in (t.get("market_question") or "")]
        if len(lag_trades) >= config.BINANCE_LAG_AUTO_DISABLE_TRADES:
            lag_pnl = sum(t.get("pnl") or 0 for t in lag_trades)
            if lag_pnl < config.BINANCE_LAG_MIN_EV:
                print(f"[INFO] [BINANCE-LAG] Auto-throttled: negative EV (${lag_pnl:.2f}) over {len(lag_trades)} trades")
            else:
                lag_trades = []  # reset so we proceed

        if len(lag_trades) < config.BINANCE_LAG_AUTO_DISABLE_TRADES:
            crypto_lag_markets = [m for m in markets if "up or down" in m.get("question", "").lower()]
            print(f"[INFO] [BINANCE-LAG] Scanning {len(crypto_lag_markets)} up/down markets for lag opportunities...")
            lag_candidates = scan_binance_lag(markets)

            if not lag_candidates:
                print(f"[INFO] [BINANCE-LAG] No candidates found this cycle")

            for cand in lag_candidates[:1]:  # Only best candidate per cycle
                print(f"  [BINANCE-LAG] PROPOSED: {cand['coin']} {cand['side']} | "
                      f"move={cand['move_10m']:+.2f}% | Poly={cand['poly_price']:.2f} | "
                      f"edge={cand['edge']:.1%} | vol={cand['vol_ratio']:.1f}x")

                # Duplicate-exposure gate: don't stack same asset + direction
                if is_duplicate_exposure(cand["question"], cand["outcome"], open_trades):
                    print(f"  [BINANCE-LAG] BLOCK: already hold {extract_asset_key(cand['question'])} {cand['outcome']}")
                    continue

                # Post-loss cooldown: 3-min lockout after a losing LAG trade on the same asset
                asset_key = extract_asset_key(cand["question"])
                if asset_key and _lag_cooldown_until.get(asset_key, 0) > time.time():
                    remain = int(_lag_cooldown_until[asset_key] - time.time())
                    print(f"  [BINANCE-LAG] SKIP {asset_key}: cooldown active ({remain}s left)")
                    continue

                # BTC-leads-alts: require BTC 180s move to agree (BTC itself always passes)
                if cand["coin"] != "BTC":
                    btc_conf = crypto_predictor.get_btc_confirmation(cand["side"])
                    if not btc_conf["agrees"]:
                        print(f"  [BTC-LEAD] BLOCK {cand['coin']} {cand['side']}: "
                              f"BTC moved {btc_conf['btc_move_180s_pct']:+.2f}% in 180s")
                        continue

                # Macro bias soft-gate: only block if bias is HIGH-confidence against direction
                bias = macro.get_macro_bias()
                if bias.get("confidence") == "high":
                    if bias.get("bias") == "bearish" and cand["side"] == "UP":
                        print(f"  [MACRO] BLOCK bearish bias vs UP trade: {bias.get('reasoning','')[:60]}")
                        continue
                    if bias.get("bias") == "bullish" and cand["side"] == "DOWN":
                        print(f"  [MACRO] BLOCK bullish bias vs DOWN trade: {bias.get('reasoning','')[:60]}")
                        continue

                # Polymarket orderbook flow: are real buyers stacked on this token?
                # Block if sellers clearly dominate the depth we're trying to buy into.
                flow = trader.get_orderbook_flow(cand["token_id"])
                print(f"  [FLOW] {cand['coin']} {cand['outcome']} "
                      f"buy_ratio={flow['buy_ratio']:.2f} ({flow['signal']})")
                if flow["signal"] == "selling":
                    print(f"  [FLOW] BLOCK {cand['coin']} {cand['outcome']}: sellers dominate orderbook")
                    continue

                # Market structure filter
                mf = market_filter_check(cand["symbol"], cand["side"])
                print(f"  [FILTER] {cand['coin']} trend={mf['trend']['label']} "
                      f"pressure={mf['pressure']['score']:+.2f} {mf['pressure']['label']} "
                      f"danger={mf['danger']}")
                if not mf["allowed"]:
                    print(f"  [BLOCK] {cand['coin']} {cand['side']} blocked by {mf['block_reason']}")
                    continue

                # Claude Sonnet veto
                gate_result = binance_lag_claude_gate(cand)
                if not gate_result:
                    print(f"  [BINANCE-LAG] BLOCKED: Claude unavailable — skipping")
                    continue

                approved = gate_result.get("ok_to_trade", False) is True
                confidence = gate_result.get("confidence", "low")
                reason = gate_result.get("reason", "no reason")

                if not approved or confidence == "low":
                    print(f"  [BINANCE-LAG] BLOCKED by Claude: {reason} (confidence={confidence})")
                    continue

                print(f"  [BINANCE-LAG] APPROVED by Claude: {reason} (confidence={confidence})")

                # Position sizing: capped at BINANCE_LAG_MAX_POSITION_PCT of total equity
                max_spend = min(total_equity * config.BINANCE_LAG_MAX_POSITION_PCT, bankroll - 2.0)
                if max_spend < config.MIN_ORDER_SIZE_USD:
                    print(f"  [BINANCE-LAG] Insufficient funds (${max_spend:.2f})")
                    continue

                price = cand["poly_price"]
                shares = max(5, int(max_spend / price)) if price > 0 else 5
                cost = shares * price

                print(f"  [BINANCE-LAG] EXECUTING: {cand['coin']} {cand['side']} | "
                      f"{shares} shares @ ${price:.3f} (${cost:.2f}) | edge={cand['edge']:.1%}")

                order_id = trader.place_limit_order(
                    token_id=cand["token_id"],
                    price=price,
                    size=shares,
                    side="BUY",
                )

                if order_id:
                    print(f"  [BINANCE-LAG] Order placed: {order_id}")
                    database.record_trade(
                        market_id=cand["market"]["id"],
                        market_question=f"[LAG] {cand['question'][:80]}",
                        token_id=cand["token_id"],
                        side="BUY", outcome=cand["outcome"],
                        entry_price=price, size=shares, cost=cost,
                        order_id=order_id,
                        claude_probability=cand["true_prob"],
                        market_probability=price,
                        edge=cand["edge"],
                        kelly_frac=config.BINANCE_LAG_MAX_POSITION_PCT,
                        dd_mult=1.0, signal_mult=1.0,
                        end_date=cand["market"].get("end_date", ""),
                    )
                    trades_placed += 1
                    bankroll -= cost
                    open_trades.append({"market_question": f"[LAG] {cand['question'][:80]}", "outcome": cand["outcome"]})
                else:
                    print(f"  [BINANCE-LAG] Order FAILED")

    # === STRATEGY 3: RESOLUTION SNIPER (near-certain at discount, off by default) ===
    if config.ENABLE_SNIPE and bankroll >= config.MIN_ORDER_SIZE_USD:
        print("[INFO] [SNIPE] Scanning for resolution snipes...")
        snipes = scan_resolution_snipes(markets)

        if snipes:
            print(f"  Found {len(snipes)} snipe opportunities!")
            for snipe in snipes[:2]:
                if bankroll < config.MIN_ORDER_SIZE_USD:
                    break
                # Duplicate-exposure gate (only blocks crypto snipes that overlap live positions)
                if is_duplicate_exposure(snipe["question"], snipe["outcome"], open_trades):
                    print(f"  [SNIPE] BLOCK: duplicate {extract_asset_key(snipe['question'])} {snipe['outcome']}")
                    continue
                print(f"  SNIPE: {snipe['question'][:50]} | {snipe['outcome']} @ ${snipe['ask_price']:.3f} | Profit: {snipe['profit_pct']:.1%}")
                if execute_snipe(snipe, bankroll):
                    trades_placed += 1
                    bankroll -= snipe["ask_price"] * 5  # Approximate cost
                    open_trades.append({"market_question": snipe["question"], "outcome": snipe["outcome"]})
        else:
            print("[INFO] No snipe opportunities (nothing near-certain at a discount)")

    # === STRATEGY 2: MOMENTUM + CLAUDE GATE (5-min crypto only) ===
    if config.ENABLE_MOMENTUM_CLAUDE and bankroll >= config.MIN_ORDER_SIZE_USD and trades_placed == 0:
        regime = crypto_predictor.get_regime()
        candle = crypto_predictor.get_candle_position()
        print(f"[INFO] [MOMENTUM] Checking | regime={regime} candle={candle['phase']} ({candle['seconds_elapsed']}s in)")

        if regime in ("trending", "choppy"):
            # Only scan crypto up/down markets for momentum
            crypto_markets = [m for m in markets
                              if crypto_predictor.is_crypto_updown_market(m.get("question", ""))]

            if crypto_markets:
                print(f"[INFO] Evaluating {len(crypto_markets)} crypto markets (regime={regime}, candle={candle['phase']})...")
                best_trade = None
                best_edge = 0.0

                for market in crypto_markets:
                    question = market["question"]
                    token_ids = market.get("token_ids", [])
                    if len(token_ids) < 2:
                        continue

                    yes_token, no_token = token_ids[0], token_ids[1]
                    yes_price = trader.get_midpoint(yes_token)
                    no_price = trader.get_midpoint(no_token)
                    if not yes_price or not no_price:
                        continue

                    # Determine symbol for gap detection
                    q_lower = question.lower()
                    symbol = None
                    for coin, sym in crypto_predictor.BINANCE_SYMBOLS.items():
                        if coin in q_lower:
                            symbol = sym
                            break

                    # Gap detection: Binance implied prob vs Polymarket price
                    binance_prob = crypto_predictor.get_binance_implied_probability(symbol) if symbol else None

                    if binance_prob is not None and yes_price > 0:
                        gap = binance_prob - yes_price
                        print(f"  [GAP SIGNAL] {question[:40]} | Binance={binance_prob:.0%} Polymarket={yes_price:.0%} Gap={gap:+.0%}")

                        # Price-band filter: skip "lottery ticket" markets where Polymarket
                        # is already pricing one side near-certain. Below 0.25 / above 0.75
                        # means the market knows something — the "gap" is fake, not edge.
                        # (Caught the Solana 3.2¢ Down trade that lost 142 shares.)
                        if yes_price < 0.25 or yes_price > 0.75:
                            print(f"  [GAP] SKIP {question[:40]}: price {yes_price:.2f} outside 0.25-0.75 band (market already decided)")
                        elif gap >= 0.20:
                            # Binance says UP but Polymarket still cheap — BUY YES
                            best_edge = gap
                            best_trade = {
                                "market_id": market["id"], "question": question,
                                "token_id": yes_token, "outcome": "Yes",
                                "price": yes_price, "probability": binance_prob,
                                "edge": gap, "signal": {"confidence": "high", "combo": "gap", "kelly_mult": 1.0,
                                                         "reasoning": f"Gap={gap:.0%} Binance={binance_prob:.0%}"},
                                "end_date": market.get("end_date", ""),
                            }
                            continue  # Skip normal momentum for this market
                        elif gap <= -0.20:
                            # Binance says DOWN but Polymarket overpriced UP — BUY NO
                            best_edge = abs(gap)
                            best_trade = {
                                "market_id": market["id"], "question": question,
                                "token_id": no_token, "outcome": "No",
                                "price": no_price, "probability": 1.0 - binance_prob,
                                "edge": abs(gap), "signal": {"confidence": "high", "combo": "gap", "kelly_mult": 1.0,
                                                              "reasoning": f"Gap={gap:.0%} Binance={binance_prob:.0%}"},
                                "end_date": market.get("end_date", ""),
                            }
                            continue

                    # Fallback: normal momentum check
                    for outcome, token_id, price in [("Yes", yes_token, yes_price), ("No", no_token, no_price)]:
                        # Skip lottery-ticket prices — same logic as gap path
                        if price < 0.25 or price > 0.75:
                            print(f"  [MOMENTUM] SKIP {question[:30]} {outcome} @ {price:.2f}: outside 0.25-0.75 band")
                            continue
                        result = crypto_predictor.estimate_crypto_probability(question, price, outcome)
                        if not result:
                            # Visible stdout diagnostic so reject reason is in bot.log
                            print(f"  [MOMENTUM] {question[:30]} {outcome} → rejected by crypto_predictor (see INFO log)")
                            continue
                        # Allow medium + high confidence
                        if result.get("confidence") == "low":
                            print(f"  [MOMENTUM] {question[:30]} {outcome} → low confidence, skip")
                            continue
                        edge = abs(result["probability"] - price)
                        if edge < config.MIN_EDGE_CRYPTO:
                            print(f"  [MOMENTUM] {question[:30]} {outcome} @ {price:.2f} prob={result['probability']:.2f} edge={edge:+.1%} < {config.MIN_EDGE_CRYPTO:.0%}")
                            continue
                        if edge > best_edge:
                            best_edge = edge
                            best_trade = {
                                "market_id": market["id"],
                                "question": question,
                                "token_id": token_id,
                                "outcome": outcome,
                                "price": price,
                                "probability": result["probability"],
                                "edge": edge,
                                "signal": result,
                                "end_date": market.get("end_date", ""),
                            }

                if best_trade:
                    # Duplicate-exposure gate
                    if is_duplicate_exposure(best_trade["question"], best_trade["outcome"], open_trades):
                        print(f"  [MOMENTUM] BLOCK: duplicate {extract_asset_key(best_trade['question'])} {best_trade['outcome']}")
                        best_trade = None

                # BTC-leads-alts + macro bias for momentum
                if best_trade:
                    mom_asset = extract_asset_key(best_trade["question"])
                    mom_side = "UP" if best_trade["outcome"] == "Yes" else "DOWN"
                    if mom_asset and mom_asset != "BTC":
                        btc_conf = crypto_predictor.get_btc_confirmation(mom_side)
                        if not btc_conf["agrees"]:
                            print(f"  [BTC-LEAD] BLOCK {mom_asset} {mom_side}: BTC moved {btc_conf['btc_move_180s_pct']:+.2f}%")
                            best_trade = None

                if best_trade:
                    bias = macro.get_macro_bias()
                    if bias.get("confidence") == "high":
                        mom_side = "UP" if best_trade["outcome"] == "Yes" else "DOWN"
                        if bias.get("bias") == "bearish" and mom_side == "UP":
                            print(f"  [MACRO] BLOCK bearish vs UP: {bias.get('reasoning','')[:60]}")
                            best_trade = None
                        elif bias.get("bias") == "bullish" and mom_side == "DOWN":
                            print(f"  [MACRO] BLOCK bullish vs DOWN: {bias.get('reasoning','')[:60]}")
                            best_trade = None

                if best_trade:
                    flow = trader.get_orderbook_flow(best_trade["token_id"])
                    print(f"  [FLOW] {best_trade['outcome']} buy_ratio={flow['buy_ratio']:.2f} ({flow['signal']})")
                    if flow["signal"] == "selling":
                        print(f"  [FLOW] BLOCK momentum: sellers dominate orderbook")
                        best_trade = None

                if best_trade:
                    combo = best_trade["signal"].get("combo", "?")
                    print(f"  MOMENTUM: {best_trade['question'][:50]} | {best_trade['outcome']} @ {best_trade['price']:.3f} | Edge: {best_trade['edge']:.1%} | combo={combo}")
                    _execute_momentum_trade(best_trade, bankroll)
                    trades_placed += 1
                    open_trades.append({"market_question": best_trade["question"], "outcome": best_trade["outcome"]})
                else:
                    print("[INFO] [MOMENTUM] No signals found | checked markets, no edge >= 3% at medium+ confidence")
            else:
                print("[INFO] [MOMENTUM] No crypto up/down markets found in filtered list")
        else:
            print(f"[INFO] [MOMENTUM] Skipped | regime={regime} (need trending or choppy)")

    if trades_placed:
        print(f"\n[INFO] {trades_placed} trade(s) placed this cycle")

    _record_equity(bankroll, live_pos_value)
    _print_portfolio(bankroll, open_trades, recent_trades)


# ──────────────────────────────────────────────────────────
# P&L WATCHER THREAD
# ──────────────────────────────────────────────────────────

def pnl_watcher_thread():
    """Background thread that monitors open positions every 3 seconds.

    Exit rules for 5-min crypto binary markets (applied in order):
    1. CUT LOSS  — down 15% (was 30%, too loose for snap-to-zero markets)
    2. ABSOLUTE STOP — price dropped 10c from a 40-60c entry (before resolution snap)
    3. PRE-RESOLUTION EXIT — <60s left, not already up 15% (avoid the binary snap)
    4. TIME EXIT — past 60% of trade life + losing anything
    5. LOCK PROFIT — up 15%+ with <90s left
    """
    last_log_cycle = 0
    while True:
        try:
            open_trades = database.get_open_trades()

            # Periodic visibility: log watcher activity every ~30s even when idle
            import time as _t
            now = int(_t.time())
            if open_trades and (now - last_log_cycle) >= 30:
                print(f"[P&L WATCHER] monitoring {len(open_trades)} position(s)")
                last_log_cycle = now

            for trade in open_trades:
                token_id = trade["token_id"]
                entry_price = trade["entry_price"]
                question = trade.get("market_question", "")
                trade_size = trade.get("size", 0)
                cost = trade.get("cost", entry_price * trade_size)

                if token_id in _dead_tokens:
                    continue

                # Get current price (None = dead/settled token)
                current_price = trader.get_midpoint(token_id)
                if current_price is None:
                    # FIX: When token is dead/resolved, check actual Polymarket position value.
                    # If position value = 0, this was a LOSS of full cost, not breakeven.
                    _dead_tokens.add(token_id)
                    live_positions = trader.get_positions()
                    actual_value = 0.0
                    still_held = False
                    for p in (live_positions or []):
                        if p.get("asset") == token_id:
                            still_held = float(p.get("size", 0)) > 0
                            actual_value = float(p.get("currentValue", 0))
                            break
                    if still_held:
                        # Position still open on Polymarket — probably API blip, keep it
                        continue
                    # Position is closed. Calculate real pnl.
                    real_pnl = actual_value - cost  # if resolved YES, value=size*$1; if NO, value=0
                    r_mult = calculate_r_multiple(entry_price, actual_value / max(trade_size, 1), entry_price)
                    status = "won" if real_pnl > 0 else "lost"
                    database.update_trade_result(trade["id"], actual_value / max(trade_size, 1), real_pnl, r_mult, status)
                    print(f"[P&L WATCHER] RESOLVED '{question[:40]}' | {status.upper()} | PnL=${real_pnl:+.2f}")
                    # Post-loss cooldown for Binance-Lag
                    if status == "lost" and "[LAG]" in question:
                        cool_key = extract_asset_key(question)
                        if cool_key:
                            _lag_cooldown_until[cool_key] = time.time() + 180
                            print(f"[COOLDOWN] {cool_key} locked for 3 min after LAG loss")
                    continue
                if current_price <= 0:
                    continue

                # Calculate P&L percentage
                pnl_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0

                # Calculate minutes left from market end date (now stored in DB)
                end_date = trade.get("end_date") or ""
                minutes_left = 999  # Default: unknown
                if end_date:
                    try:
                        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                        minutes_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 60
                    except (ValueError, TypeError):
                        pass

                # Calculate elapsed time since entry (fallback when end_date missing)
                elapsed_min = None
                created_at = trade.get("created_at")
                if created_at:
                    try:
                        cr_dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
                        if cr_dt.tzinfo is None:
                            cr_dt = cr_dt.replace(tzinfo=timezone.utc)
                        elapsed_min = (datetime.now(timezone.utc) - cr_dt).total_seconds() / 60
                    except (ValueError, TypeError):
                        pass

                should_sell = False
                reason = ""

                # Rule 1: HARD STOP — down 15% (tightened from 30%)
                if pnl_pct <= -0.15:
                    should_sell = True
                    reason = f"CUT LOSS ({pnl_pct:+.0%})"

                # Rule 1b: BINANCE REVERSAL (LAG trades only) — exit if Binance
                # moves sharply against the entry direction during the hold
                elif "[LAG]" in question:
                    entry_side = (trade.get("outcome") or "").lower()  # "yes" or "no"
                    symbol = None
                    q_lower = question.lower()
                    for coin, sym in crypto_predictor.BINANCE_SYMBOLS.items():
                        if coin in q_lower:
                            symbol = sym
                            break
                    if symbol:
                        try:
                            m = crypto_predictor.get_realtime_momentum(symbol)
                            move_180s = (m or {}).get("price_change_180s")
                        except Exception:
                            move_180s = None
                        if move_180s is not None:
                            if entry_side == "yes" and move_180s <= -0.15:
                                should_sell = True
                                reason = f"BINANCE REVERSAL ({move_180s:+.2f}% vs YES)"
                            elif entry_side == "no" and move_180s >= 0.15:
                                should_sell = True
                                reason = f"BINANCE REVERSAL ({move_180s:+.2f}% vs NO)"

                # Rule 2: ABSOLUTE STOP — for 40-60c entries, sell if dropped 10c
                # (5-min binary markets snap to 0 — catch the move before it completes)
                if not should_sell and 0.40 <= entry_price <= 0.60 and current_price <= entry_price - 0.08:
                    should_sell = True
                    reason = f"ABS STOP (${entry_price:.2f}→${current_price:.2f})"

                # Rule 3: PRE-RESOLUTION EXIT — <60s left and clearly losing
                # 5-min binaries snap to $0 or $1; only bail late if already underwater >5%.
                # (Old rule exited any trade not up 15% — capped winners' upside at +14%.)
                if not should_sell and minutes_left < 1.0 and pnl_pct < -0.05:
                    should_sell = True
                    reason = f"PRE-RESOLUTION ({pnl_pct:+.0%}, {minutes_left*60:.0f}s left)"

                # Rule 4: TIME EXIT — held >3min AND losing >5%
                # (Old rule fired on any pnl<0; killed brief dips that would have resolved up.)
                if not should_sell and elapsed_min is not None and elapsed_min >= 3.0 and pnl_pct < -0.05:
                    should_sell = True
                    reason = f"TIME EXIT (held {elapsed_min:.1f}min, {pnl_pct:+.0%})"

                # Rule 5: LOCK PROFIT — up 15%+ with <90s left
                if not should_sell and minutes_left < 1.5 and pnl_pct >= 0.15:
                    should_sell = True
                    reason = f"LOCK PROFIT (+{pnl_pct:.0%}, {minutes_left*60:.0f}s left)"

                if should_sell:
                    print(f"[P&L WATCHER] SELL '{question[:40]}' | ${entry_price:.2f}→${current_price:.2f} | pnl={pnl_pct:+.0%} | {reason}")

                    # Get real position size from Polymarket
                    live_positions = trader.get_positions()
                    real_size = trade_size
                    for p in (live_positions or []):
                        if p.get("asset") == token_id and float(p.get("size", 0)) > 0:
                            real_size = float(p["size"])
                            break

                    if real_size > 0 and not config.DRY_RUN:
                        sell_price = max(0.01, min(0.99, round(current_price - 0.01, 2)))
                        try:
                            trader.place_limit_order(token_id, sell_price, real_size, "SELL")
                            print(f"[P&L WATCHER] Sell order placed @ ${sell_price:.2f} x {real_size:.1f}")
                        except Exception as e:
                            if "400" in str(e) or "balance" in str(e).lower():
                                print(f"[P&L WATCHER] CRITICAL — sell failed (balance), position still open: {e}")
                                continue
                            print(f"[P&L WATCHER] Sell error: {e}")
                            continue

                    # Update DB
                    pnl = (current_price - entry_price) * real_size
                    r_mult = calculate_r_multiple(entry_price, current_price, entry_price)
                    status = "won" if pnl > 0 else "lost"
                    database.update_trade_result(trade["id"], current_price, pnl, r_mult, status)
                    print(f"[P&L WATCHER] Closed: PnL=${pnl:+.2f} | status={status}")

                    # Post-loss cooldown for Binance-Lag: lock asset for 3 minutes
                    if status == "lost" and "[LAG]" in question:
                        cool_key = extract_asset_key(question)
                        if cool_key:
                            _lag_cooldown_until[cool_key] = time.time() + 180
                            print(f"[COOLDOWN] {cool_key} locked for 3 min after LAG loss")

        except Exception as e:
            print(f"[P&L WATCHER] Error: {e}")

        time.sleep(3)


def macro_watcher_thread():
    """Background thread that refreshes the macro bias every MACRO_REFRESH_SEC.

    Synthesizes BTC/ETH/SOL trends + crypto news headlines via Claude Sonnet and
    caches the result for the directional strategies to soft-gate against.
    """
    # Initial refresh on startup so the bot has a bias before the first cycle
    try:
        state = macro.refresh_macro_bias()
        print(f"[MACRO] Initial bias: {state.get('bias')} ({state.get('confidence')}) — {state.get('reasoning','')[:80]}")
    except Exception as e:
        print(f"[MACRO] Initial refresh failed: {e}")

    while True:
        try:
            time.sleep(config.MACRO_REFRESH_SEC)
            state = macro.refresh_macro_bias()
            print(f"[MACRO] Refreshed: {state.get('bias')} ({state.get('confidence')}) — {state.get('reasoning','')[:80]}")
        except Exception as e:
            print(f"[MACRO] Refresh failed: {e}")
            time.sleep(600)  # Retry in 10 min on failure


# ──────────────────────────────────────────────────────────
# STARTUP
# ──────────────────────────────────────────────────────────

def main():
    global _session_start_bankroll
    database.init_db()

    # Cancel stale orders
    print("[INFO] Cancelling stale open orders...")
    try:
        client = trader.get_client()
        client.cancel_all()
        print("[INFO] All open orders cancelled")
    except Exception as e:
        print(f"[WARN] Failed to cancel orders: {e}")

    # Clean ghost trades
    db_open = database.get_open_trades()
    live_positions = trader.get_positions()
    live_tokens = {p.get("asset", "") for p in (live_positions or []) if float(p.get("size", 0)) > 0}
    cleaned = 0
    for t in db_open:
        if t["token_id"] not in live_tokens and t.get("order_id") != "imported":
            database.update_trade_result(t["id"], t["entry_price"], 0.0, 0.0, "cancelled")
            cleaned += 1
    if cleaned:
        print(f"[INFO] Cleaned {cleaned} ghost trades from DB")

    # Sync existing positions
    positions = trader.get_positions()
    pos_value = 0.0
    for p in (positions or []):
        size = float(p.get("size", 0))
        if size <= 0:
            continue
        token_id = p.get("asset", "")
        title = p.get("title", p.get("market", "unknown"))
        avg_price = float(p.get("avgPrice", p.get("price", 0.5)))
        cur_value = float(p.get("currentValue", 0))
        pos_value += cur_value

        already_tracked = any(t["token_id"] == token_id for t in database.get_open_trades())
        if not already_tracked and token_id:
            database.record_trade(
                market_id=p.get("conditionId", "imported"),
                market_question=title, token_id=token_id,
                side="BUY", outcome=p.get("outcome", "Yes"),
                entry_price=avg_price, size=size, cost=size * avg_price,
                order_id="imported", claude_probability=0.0,
                market_probability=avg_price, edge=0.0,
                kelly_frac=0.0, dd_mult=1.0, signal_mult=1.0,
            )
            print(f"  [SYNC] {title[:50]} | {size:.1f} shares @ ${avg_price:.3f}")

    bankroll = trader.get_balance()
    total_equity = bankroll + pos_value
    _session_start_bankroll = total_equity

    database.reset_peak_equity(total_equity)

    print()
    print("=" * 55)
    print("          POLYMARKET TRADING BOT")
    print(f"  Mode:        {'LIVE TRADING' if not config.DRY_RUN else 'DRY RUN'}")
    print(f"  Cash:        ${bankroll:.2f}")
    print(f"  Positions:   ${pos_value:.2f}")
    print(f"  Total:       ${total_equity:.2f}")
    active = _get_active_strategies()
    print(f"  Active:      {active}")
    from strategies.arbitrage import ARB_THRESHOLD_NORMAL, ARB_THRESHOLD_NEAR_EXPIRY, NEAR_EXPIRY_MINUTES
    print(f"  Arb:         {'ON' if config.ENABLE_ARB else 'OFF'} (normal<{ARB_THRESHOLD_NORMAL}, near-expiry<{ARB_THRESHOLD_NEAR_EXPIRY} within {NEAR_EXPIRY_MINUTES}min)")
    print(f"  Binance-Lag: {'ON (Sonnet gate)' if config.ENABLE_BINANCE_LAG else 'OFF'}")
    print(f"  Momentum:    {'ON (Sonnet gate)' if config.ENABLE_MOMENTUM_CLAUDE else 'OFF'}")
    print(f"  Sniper:      {'ON' if config.ENABLE_SNIPE else 'OFF'}")
    print(f"  Markets:     {', '.join(config.ALLOWED_MARKET_DURATIONS)} only")
    print(f"  Cycle:       {config.CYCLE_INTERVAL_SEC}s")
    print("=" * 55)
    print()

    # Start P&L watcher daemon thread
    watcher = threading.Thread(target=pnl_watcher_thread, daemon=True)
    watcher.start()
    print("[P&L WATCHER] Started — checking every 3 seconds")

    # Start macro bias watcher daemon thread (refreshes global crypto outlook every 3h)
    macro_thread = threading.Thread(target=macro_watcher_thread, daemon=True)
    macro_thread.start()
    print(f"[MACRO WATCHER] Started — refreshing every {config.MACRO_REFRESH_SEC // 60} min")
    print()

    while True:
        try:
            run_cycle()
        except KeyboardInterrupt:
            print("\n[INFO] Shutting down...")
            break
        except Exception:
            print(f"[ERROR] Cycle failed:\n{traceback.format_exc()}")

        print(f"\n[INFO] Next cycle in {config.CYCLE_INTERVAL_SEC}s...")
        try:
            time.sleep(config.CYCLE_INTERVAL_SEC)
        except KeyboardInterrupt:
            print("\n[INFO] Shutting down...")
            break


if __name__ == "__main__":
    main()
