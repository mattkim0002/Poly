"""Bot runtime — owns the trading cycle, strategy dispatch, and watcher threads.

main.py keeps startup + thread wiring; this module owns everything that happens
after init. All BUY-side entries route through risk_manager → execution.py.

Sections:
  1. Runtime state
  2. Asset-key helpers (duplicate-exposure gate)
  3. Threshold scanner / executor
  4. Universe filters (junk, duration, hard filters, active strategies)
  5. Resolution sniper scanner / executor
  6. Position management (resolution, exits, equity, portfolio print)
  7. Claude news gate + momentum executor
  8. Cycle orchestrator (run_cycle)
  9. Watcher threads (pnl_watcher, macro_watcher)
  10. Entry point (start)
"""

import sys
import time
import threading
import traceback
from datetime import date, datetime, timezone

import config
from core import database, market_data, trader, crypto_predictor, news, macro, moondev
from core.candidate import Candidate
from core.market_filter import check_trade as market_filter_check
from strategies.sizing import calculate_r_multiple, expectancy, drawdown_multiplier, drawdown
from strategies.arbitrage import scan_all_markets, size_arb
from strategies.binance_lag import scan_binance_lag, claude_gate as binance_lag_claude_gate
from strategies.ev import get_fee_rate, is_fee_free_market
import risk_manager
import execution
from utils.logger import log


# ──────────────────────────────────────────────────────────
# 1. RUNTIME STATE (mutated across cycle + watcher threads)
# ──────────────────────────────────────────────────────────

_cycle_count = 0
_daily_pnl = 0.0
_daily_date = None
_session_start_bankroll = 0.0
_dead_tokens = set()

# Post-loss cooldown for Binance-Lag: asset_key -> unix_ts when cooldown expires
_lag_cooldown_until: dict = {}


# ──────────────────────────────────────────────────────────
# 2. ASSET-KEY HELPERS
# ──────────────────────────────────────────────────────────

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
    """True if any open trade already holds the same crypto asset (any direction).

    Blocks stacking multiple BTC bets AND holding both Yes+No on the same asset
    (outside of arb, which uses its own gate). Non-crypto markets (asset key = None)
    are never blocked.
    """
    key = extract_asset_key(question)
    if not key:
        return False
    for t in open_trades:
        if extract_asset_key(t.get("market_question", "")) == key:
            return True
    return False


# ──────────────────────────────────────────────────────────
# 3. STRATEGY 0: THRESHOLD (mean-reversion on extreme prices)
# ──────────────────────────────────────────────────────────

THRESHOLD_YES_MAX = 0.28
THRESHOLD_NO_MIN = 0.72
THRESHOLD_TP_YES = 0.95
THRESHOLD_TP_NO = 0.05
THRESHOLD_SL_YES = 0.45
THRESHOLD_SL_NO = 0.55


def scan_threshold_opportunities(markets: list[Candidate]) -> list[dict]:
    """Scan crypto markets for extreme-price mean-reversion entries."""
    now = datetime.now(timezone.utc)
    seconds_into_candle = (now.minute % 5) * 60 + now.second

    if seconds_into_candle < 150:
        print(f"[THRESHOLD] Waiting for candle maturity ({seconds_into_candle}s < 150s)")
        return []

    opportunities = []

    for cand in markets:
        q_lower = cand.question.lower()

        if "up or down" not in q_lower or not cand.yes_token_id or not cand.no_token_id:
            continue

        if cand.end_date:
            try:
                end_dt = datetime.fromisoformat(cand.end_date.replace("Z", "+00:00"))
                minutes_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 60
                if minutes_left < 2 or minutes_left > 30:
                    continue
            except (ValueError, TypeError):
                continue

        book = trader.get_orderbook(cand.yes_token_id)
        if not book or not book.get("asks"):
            continue

        yes_ask = book["asks"][0]["price"]
        yes_ask_size = book["asks"][0]["size"]

        symbol = None
        for coin, sym in crypto_predictor.BINANCE_SYMBOLS.items():
            if coin in q_lower:
                symbol = sym
                break

        if yes_ask <= THRESHOLD_YES_MAX:
            if symbol:
                binance_prob = crypto_predictor.get_binance_implied_probability(symbol)
                if binance_prob is not None and binance_prob < 0.35:
                    print(f"[THRESHOLD] SKIP YES {cand.question[:50]} — Binance confirms DOWN ({binance_prob:.0%})")
                    continue

            print(f"[THRESHOLD] BUY YES {cand.question[:50]} at {yes_ask*100:.0f}c — TP: 95c SL: 45c")
            opportunities.append({
                "question": cand.question, "market_id": cand.market_id,
                "token_id": cand.yes_token_id, "outcome": "Yes",
                "ask_price": yes_ask, "ask_size": yes_ask_size,
                "tp": THRESHOLD_TP_YES, "sl": THRESHOLD_SL_YES,
            })

        elif yes_ask >= THRESHOLD_NO_MIN:
            if symbol:
                binance_prob = crypto_predictor.get_binance_implied_probability(symbol)
                if binance_prob is not None and binance_prob > 0.65:
                    print(f"[THRESHOLD] SKIP NO {cand.question[:50]} — Binance confirms UP ({binance_prob:.0%})")
                    continue

            no_book = trader.get_orderbook(cand.no_token_id)
            if not no_book or not no_book.get("asks"):
                continue
            no_ask = no_book["asks"][0]["price"]
            no_ask_size = no_book["asks"][0]["size"]

            print(f"[THRESHOLD] BUY NO {cand.question[:50]} at {no_ask*100:.0f}c — TP: 5c SL: 55c")
            opportunities.append({
                "question": cand.question, "market_id": cand.market_id,
                "token_id": cand.no_token_id, "outcome": "No",
                "ask_price": no_ask, "ask_size": no_ask_size,
                "tp": THRESHOLD_TP_NO, "sl": THRESHOLD_SL_NO,
            })

    return opportunities


def execute_threshold_trade(opp: dict, bankroll: float) -> bool:
    """Execute a threshold mean-reversion trade with risk gate + execution chokepoint."""
    max_spend = min(bankroll * 0.25, bankroll - 2.0)
    if max_spend < config.MIN_ORDER_SIZE_USD:
        return False

    price = opp["ask_price"]
    max_shares_by_bank = int(max_spend / price) if price > 0 else 0
    max_shares_by_book = int(opp["ask_size"])
    shares = min(max_shares_by_bank, max_shares_by_book)
    shares = max(shares, 5)

    cost = shares * price
    edge = (1.0 - price) if opp["outcome"] == "Yes" else price

    print(f"\n  === THRESHOLD TRADE ===")
    print(f"  Market:  {opp['question'][:55]}")
    print(f"  Side:    {opp['outcome']} @ ${price:.3f} (LIMIT)")
    print(f"  Shares:  {shares} (${cost:.2f})")
    print(f"  TP:      ${opp['tp']:.2f}  SL: ${opp['sl']:.2f}")
    print(f"  =========================")

    proposal = risk_manager.build_proposal(
        strategy="threshold", market_id=opp["market_id"],
        side=opp["outcome"], proposed_size_usdc=cost,
        expected_profit_usdc=cost * edge, net_edge_pct=edge,
    )
    decision = risk_manager.evaluate_trade(proposal)
    risk_manager.log_decision(proposal, decision)

    if not decision["approved"]:
        return False

    order_id = execution.execute_buy(
        risk_decision=decision, token_id=opp["token_id"],
        price=price, shares=shares, market_id=opp["market_id"],
        question=f"[THRESH] {opp['question'][:80]}",
        outcome=opp["outcome"], strategy="threshold",
        edge=edge, market_probability=price,
        end_date=opp.get("end_date", ""),
    )
    return order_id is not None


# ──────────────────────────────────────────────────────────
# 4. UNIVERSE FILTERS
# ──────────────────────────────────────────────────────────

def _is_junk_market(question: str) -> bool:
    q_lower = question.lower()
    for keyword in config.SPORTS_KEYWORDS:
        if keyword in q_lower:
            return True
    return False


def _is_allowed_market_duration(question: str) -> bool:
    """All markets pass — strategy-level filters handle the rest."""
    return True


def _hard_filter_market(market: Candidate) -> tuple[bool, str]:
    """Crypto-only + anti-coinflip + anti-lottery-ticket filters. Returns (passes, reason)."""
    q_lower = market.question.lower()

    if getattr(config, "CRYPTO_ONLY", False):
        import re
        if not re.search(config._CRYPTO_REGEX, q_lower):
            return (False, "not crypto")

    if "up or down" in q_lower:
        if market.end_date:
            try:
                end_dt = datetime.fromisoformat(str(market.end_date).replace("Z", "+00:00"))
                hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
                if hours_left < config.MIN_RESOLUTION_HOURS:
                    mins_left = hours_left * 60.0
                    return (False, f"resolves in {mins_left:.0f} min (under {config.MIN_RESOLUTION_HOURS:.0f}h minimum)")
            except (ValueError, TypeError):
                pass

    for p in (market.yes_price, market.no_price):
        if 0 < p < config.MIN_PRICE_FLOOR:
            return (False, f"price {p*100:.1f}\u00a2 below {config.MIN_PRICE_FLOOR*100:.0f}\u00a2 minimum")

    return (True, "")


def _get_active_strategies() -> list[str]:
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
# 5. RESOLUTION SNIPER (near-certain at discount)
# ──────────────────────────────────────────────────────────

def scan_resolution_snipes(markets: list[Candidate]) -> list[dict]:
    """Find near-certain outcomes priced below face value.

    Consumes list[Candidate] directly. Enriches each Candidate's
    yes_ask/yes_bid/no_ask/no_bid fields from the orderbook fetch.
    """
    snipes = []

    for cand in markets:
        if not cand.yes_token_id or not cand.no_token_id:
            continue

        for token_id, price, outcome, is_yes in (
            (cand.yes_token_id, cand.yes_price, "Yes", True),
            (cand.no_token_id, cand.no_price, "No", False),
        ):
            if price < config.ENDGAME_PRICE_MIN or price > config.ENDGAME_PRICE_MAX:
                continue

            book = trader.get_orderbook(token_id)
            if not book or not book.get("asks"):
                continue

            real_ask = float(book["asks"][0]["price"])
            ask_size = float(book["asks"][0]["size"])
            best_bid = float(book["bids"][0]["price"]) if book.get("bids") else 0.0

            # Enrich Candidate with live orderbook on the relevant side.
            if is_yes:
                cand.yes_ask = real_ask
                cand.yes_bid = best_bid
            else:
                cand.no_ask = real_ask
                cand.no_bid = best_bid

            if real_ask < config.ENDGAME_PRICE_MIN or real_ask > config.ENDGAME_PRICE_MAX:
                continue

            fee_free = is_fee_free_market(cand.question)
            if fee_free:
                fee_per_share = 0.0
            else:
                fee_rate = get_fee_rate(token_id)
                fee_per_share = fee_rate * real_ask * (1.0 - real_ask)

            slip = config.ENDGAME_SLIPPAGE_BUFFER
            profit_per_share = 1.0 - real_ask - fee_per_share - slip
            net_edge_pct = profit_per_share / real_ask if real_ask > 0 else 0.0

            if net_edge_pct < config.ENDGAME_MIN_NET_EDGE:
                continue

            snipes.append({
                "question": cand.question, "market_id": cand.market_id,
                "token_id": token_id, "outcome": outcome,
                "ask_price": real_ask, "ask_size": ask_size,
                "profit_per_share": profit_per_share, "profit_pct": net_edge_pct,
                "fee_per_share": fee_per_share, "slippage_buffer": slip,
                "fee_free": fee_free,
                "end_date": cand.end_date,
            })

    snipes.sort(key=lambda x: x["profit_pct"], reverse=True)
    return snipes


ENDGAME_GATE_SYSTEM = """You are the strategy brain for a Polymarket bot.
Only two strategies are allowed: intra-market ARBITRAGE and ENDGAME / near-resolution.
This request is ENDGAME — a near-certain outcome priced 0.93-0.99 close to resolution.

Approve ONLY if ALL of:
1. The resolution criteria is clear, unambiguous, and has a known source.
2. External data (headlines, official announcements, on-chain facts) makes the outcome
   overwhelmingly likely — not merely "market seems sure".
3. Net edge after fees + spread + slippage is between 1% and 3% of capital committed.
4. No meaningful path risk, no "N/A / 50-50" resolution risk, no disputed outcome.
5. Not a lottery ticket, not a momentum/directional bet.

Reject if resolution is unclear, delayed, disputed, or evidence is weak.

Reply with ONLY this JSON (no prose):
{
  "decision": "approve" | "reject",
  "strategy": "endgame",
  "gross_edge_pct": 0.00,
  "net_edge_pct": 0.00,
  "fees_deducted_pct": 0.00,
  "reason": "1-3 sentence explanation",
  "suggested_changes": {"price_adjustment": "", "size_adjustment": ""}
}"""


def _endgame_claude_gate(snipe: dict) -> dict | None:
    """Send endgame candidate to Claude Sonnet for structured approval."""
    try:
        import json as _json
        import anthropic
        client = anthropic.Anthropic()

        try:
            headlines = news.fetch_headlines(snipe["question"][:80], max_results=4) or []
        except Exception:
            headlines = []

        hours_to_resolution = None
        if snipe.get("end_date"):
            try:
                end_dt = datetime.fromisoformat(str(snipe["end_date"]).replace("Z", "+00:00"))
                hours_to_resolution = round(
                    (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0, 2
                )
            except (ValueError, TypeError):
                pass

        gross = 1.0 - snipe["ask_price"]
        fees = snipe.get("fee_per_share", 0.0) + snipe.get("slippage_buffer", 0.0)
        smart_money = moondev.get_profitable_wallet_signal(
            snipe.get("market_id") or snipe.get("question", "")[:40]
        )

        payload = _json.dumps({
            "market_question": snipe["question"][:160],
            "outcome_buying": snipe["outcome"],
            "ask_price": round(snipe["ask_price"], 4),
            "gross_edge_pct": round(gross / snipe["ask_price"], 4),
            "net_edge_pct": round(snipe["profit_pct"], 4),
            "fees_deducted_pct": round(fees / snipe["ask_price"], 4),
            "hours_to_resolution": hours_to_resolution,
            "recent_headlines": headlines[:4],
            "smart_money": smart_money,
        })

        resp = client.messages.create(
            model=config.CLAUDE_MODEL, max_tokens=250,
            system=ENDGAME_GATE_SYSTEM,
            messages=[{"role": "user", "content": payload}],
        )
        text = resp.content[0].text.strip()
        print(f"  [ENDGAME GATE] {text[:140]}")

        try:
            return _json.loads(text)
        except _json.JSONDecodeError:
            print(f"  [ENDGAME GATE] Invalid JSON — rejecting")
            return None
    except Exception as e:
        print(f"  [ENDGAME GATE] Failed ({e}) — rejecting")
        return None


def execute_snipe(snipe: dict, bankroll: float) -> bool:
    """Execute an endgame trade — Claude gate + risk_manager + execution chokepoint."""
    gate = _endgame_claude_gate(snipe)
    if not gate:
        return False

    gate_decision = gate.get("decision", "reject")
    if gate_decision != "approve":
        print(f"  [ENDGAME] REJECT: {gate.get('reason', 'no reason')[:120]}")
        return False

    claude_net = float(gate.get("net_edge_pct") or 0.0)
    if claude_net < config.ENDGAME_MIN_NET_EDGE:
        print(f"  [ENDGAME] REJECT: Claude net_edge {claude_net:.2%} < {config.ENDGAME_MIN_NET_EDGE:.0%}")
        return False

    max_spend = min(bankroll * config.ENDGAME_MAX_POSITION_PCT, bankroll - 2.0)
    if max_spend < config.MIN_ORDER_SIZE_USD:
        print(f"  [ENDGAME] REJECT: max_spend ${max_spend:.2f} too small")
        return False

    price = snipe["ask_price"]
    max_shares_by_bank = int(max_spend / price) if price > 0 else 0
    max_shares_by_book = int(snipe["ask_size"])
    shares = min(max_shares_by_bank, max_shares_by_book)
    shares = max(shares, 5)

    cost = shares * price
    expected_profit = shares * snipe["profit_per_share"]

    if expected_profit < config.ENDGAME_MIN_PROFIT_USD:
        print(f"  [ENDGAME] REJECT: profit ${expected_profit:.3f} < min ${config.ENDGAME_MIN_PROFIT_USD}")
        return False

    proposal = risk_manager.build_proposal(
        strategy="endgame", market_id=snipe["market_id"],
        side=snipe["outcome"], proposed_size_usdc=cost,
        expected_profit_usdc=expected_profit, net_edge_pct=snipe["profit_pct"],
    )
    decision = risk_manager.evaluate_trade(proposal)
    risk_manager.log_decision(proposal, decision)

    if not decision["approved"]:
        return False

    resized_cost = decision["final_size_usdc"]
    if resized_cost < cost:
        shares = max(5, int(resized_cost / price)) if price > 0 else 5
        cost = shares * price
        expected_profit = shares * snipe["profit_per_share"]

    print(f"\n  === ENDGAME TRADE (Claude + Risk approved) ===")
    print(f"  Market:  {snipe['question'][:60]}")
    print(f"  Side:    {snipe['outcome']} @ ${price:.3f}")
    print(f"  Shares:  {shares}")
    print(f"  Cost:    ${cost:.2f}")
    print(f"  Profit:  ${expected_profit:.2f} ({snipe['profit_pct']:.1%})")
    print(f"  Claude:  net={claude_net:.2%} | {gate.get('reason', '')[:80]}")
    fee_str = "FREE" if snipe['fee_free'] else f"${snipe['fee_per_share']:.4f}/share"
    print(f"  Fees:    {fee_str}")
    print(f"  ========================================")

    order_id = execution.execute_buy(
        risk_decision=decision, token_id=snipe["token_id"],
        price=price, shares=shares, market_id=snipe["market_id"],
        question=f"[ENDGAME] {snipe['question'][:80]}",
        outcome=snipe["outcome"], strategy="endgame",
        edge=snipe["profit_pct"], market_probability=price,
        end_date=snipe.get("end_date", ""),
    )
    return order_id is not None


# ──────────────────────────────────────────────────────────
# 6. POSITION MANAGEMENT
# ──────────────────────────────────────────────────────────

def _check_existing_positions(open_trades: list[dict]):
    """Check open positions for resolution or exit conditions."""
    global _daily_pnl

    live_positions = execution.get_positions()
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

        if current_price >= 0.95:
            pnl = (1.0 - avg_price) * real_size
            r_mult = calculate_r_multiple(avg_price, 1.0, avg_price)
            database.update_trade_result(trade["id"], 1.0, pnl, r_mult, "won")
            _daily_pnl += pnl
            print(f"  WIN: '{question[:40]}' | PnL=${pnl:+.2f}")

        elif current_price <= 0.05:
            pnl = -avg_price * real_size
            r_mult = calculate_r_multiple(avg_price, 0.0, avg_price)
            database.update_trade_result(trade["id"], 0.0, pnl, r_mult, "lost")
            _daily_pnl += pnl
            print(f"  LOSS: '{question[:40]}' | PnL=${pnl:.2f}")

        elif "[THRESH]" in question:
            is_yes = trade.get("outcome") == "Yes"
            tp_level = THRESHOLD_TP_YES if is_yes else THRESHOLD_TP_NO
            sl_level = THRESHOLD_SL_YES if is_yes else THRESHOLD_SL_NO

            if (is_yes and current_price >= tp_level) or (not is_yes and current_price <= tp_level):
                pnl = (current_price - avg_price) * real_size if is_yes else (avg_price - current_price) * real_size
                pnl = (1.0 - avg_price) * real_size
                r_mult = calculate_r_multiple(avg_price, current_price, avg_price)
                database.update_trade_result(trade["id"], current_price, pnl, r_mult, "won")
                _daily_pnl += pnl
                print(f"  [THRESHOLD TP] '{question[:40]}' | PnL=${pnl:+.2f}")
                if not config.DRY_RUN:
                    sell_price = max(0.01, min(0.99, round(current_price - 0.01, 2)))
                    execution.execute_sell(token_id, sell_price, real_size)

            elif (is_yes and current_price >= sl_level) or (not is_yes and current_price <= sl_level):
                pnl = (current_price - avg_price) * real_size
                r_mult = calculate_r_multiple(avg_price, current_price, avg_price)
                database.update_trade_result(trade["id"], current_price, pnl, r_mult, "lost")
                _daily_pnl += pnl
                print(f"  [THRESHOLD SL] '{question[:40]}' | PnL=${pnl:.2f}")
                if not config.DRY_RUN:
                    execution.execute_sell(token_id, current_price, real_size)

        elif "[ARB" not in question and "[SNIPE]" not in question:
            loss_pct = (avg_price - current_price) / avg_price if avg_price > 0 else 0
            if loss_pct >= config.STOP_LOSS_PCT:
                pnl = (current_price - avg_price) * real_size
                r_mult = calculate_r_multiple(avg_price, current_price, avg_price)
                database.update_trade_result(trade["id"], current_price, pnl, r_mult, "lost")
                _daily_pnl += pnl
                print(f"  STOP LOSS: '{question[:40]}' | PnL=${pnl:.2f}")
                if not config.DRY_RUN:
                    execution.execute_sell(token_id, current_price, real_size)


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
    live_positions = execution.get_positions()
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


# ──────────────────────────────────────────────────────────
# 7. CLAUDE NEWS GATE + MOMENTUM EXECUTOR
# ──────────────────────────────────────────────────────────

NEWS_GATE_SYSTEM = """You are a trade filter for a Polymarket 5-minute crypto Up/Down trading bot.

You receive raw data: coin, side (UP/DOWN), Polymarket price, Binance momentum,
orderbook imbalance, recent_headlines, and macro_bias.

Your ONLY job: APPROVE or REJECT. Default is APPROVE. Only reject when clearly bad.

Reply with ONLY this JSON:
{"ok_to_trade": true or false, "reason": "single short sentence", "confidence": "low|medium|high"}

REJECT only when:
1. Binance 1h AND 4h trends BOTH clearly oppose the trade direction.
2. A major headline describes a clear opposing catalyst (hack, SEC action, exchange outage).
3. Macro_bias is bearish with HIGH confidence and side = UP (or vice versa).

APPROVE when:
- Binance short-term momentum supports the direction (even weakly).
- Signals are mixed but not clearly opposing → APPROVE with low confidence.
- Edge >= 3% with no clear opposition → APPROVE.
- When in doubt → APPROVE with low confidence.

The user wants the bot TRADING. Be permissive, not conservative.
Never write anything outside the JSON."""


def _claude_news_check(question: str, direction: str, edge: float,
                       symbol: str = None, poly_price: float = 0.0) -> bool:
    """Claude risk gate: sends all raw data to Sonnet, requires explicit APPROVE."""
    try:
        import json as _json
        import anthropic
        client = anthropic.Anthropic()

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

        try:
            coin_label = symbol.replace("USDT", "") if symbol else "Bitcoin"
            recent_headlines = news.fetch_headlines(f"{coin_label} crypto price", max_results=3)
        except Exception:
            recent_headlines = []

        try:
            bias_snapshot = macro.get_macro_bias()
            macro_payload = {
                "bias": bias_snapshot.get("bias", "neutral"),
                "confidence": bias_snapshot.get("confidence", "low"),
                "reasoning": bias_snapshot.get("reasoning", ""),
            }
        except Exception:
            macro_payload = {"bias": "neutral", "confidence": "low", "reasoning": ""}

        try:
            btc_confirm = crypto_predictor.get_btc_confirmation(direction)
        except Exception:
            btc_confirm = {"agrees": True, "btc_move_180s_pct": 0.0, "strength": "none"}

        smart_money = moondev.get_profitable_wallet_signal(question[:40])

        payload = _json.dumps({
            "coin": symbol or "BTC", "side": direction,
            "polymarket_price": poly_price, "edge": f"{edge:.1%}",
            "market": question[:80], "binance": binance_summary,
            "recent_headlines": recent_headlines,
            "macro_bias": macro_payload,
            "btc_confirmation": btc_confirm,
            "smart_money": smart_money,
        })

        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=120,
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

    tier = "A" if edge >= config.TIER_A_EDGE else "B"
    if tier == "B":
        size_dollars *= 0.5

    if size_dollars < config.MIN_ORDER_SIZE_USD:
        return False

    shares = max(5, int(size_dollars / price)) if price > 0 else 5
    cost = shares * price

    print(f"\n  === MOMENTUM TRADE ===")
    print(f"  Market:  {trade['question'][:50]}")
    print(f"  Side:    {trade['outcome']} @ ${price:.3f}")
    print(f"  Edge:    {edge:.1%} (Tier {tier})")
    print(f"  Shares:  {shares} (${cost:.2f})")

    direction = "UP" if trade["outcome"] == "Yes" else "DOWN"
    q_lower = trade["question"].lower()
    symbol = None
    coin_name = ""
    for coin, sym in crypto_predictor.BINANCE_SYMBOLS.items():
        if coin in q_lower:
            symbol = sym
            coin_name = coin.upper()
            break

    if symbol:
        mf = market_filter_check(symbol, direction)
        print(f"  [FILTER] {coin_name} trend={mf['trend']['label']} "
              f"pressure={mf['pressure']['score']:+.2f} {mf['pressure']['label']} "
              f"danger={mf['danger']}")
        if not mf["allowed"]:
            print(f"  [BLOCK] {coin_name} {direction} blocked by {mf['block_reason']}")
            return False
        if mf["needs_claude"]:
            print(f"  [FILTER] Pressure/danger conflict — requiring Claude approval")

    if not _claude_news_check(trade["question"], direction, edge,
                              symbol=symbol, poly_price=price):
        print(f"  [BLOCKED] Claude rejected — news/data contradicts signal")
        return False

    print(f"  ======================")

    proposal = risk_manager.build_proposal(
        strategy="momentum", market_id=trade["market_id"],
        side=trade["outcome"], proposed_size_usdc=cost,
        expected_profit_usdc=cost * edge, net_edge_pct=edge,
    )
    risk_decision = risk_manager.evaluate_trade(proposal)
    risk_manager.log_decision(proposal, risk_decision)

    if not risk_decision["approved"]:
        return False

    resized_cost = risk_decision["final_size_usdc"]
    if resized_cost < cost:
        shares = max(5, int(resized_cost / price)) if price > 0 else 5
        cost = shares * price

    order_id = execution.execute_buy(
        risk_decision=risk_decision, token_id=trade["token_id"],
        price=price, shares=shares, market_id=trade["market_id"],
        question=trade["question"], outcome=trade["outcome"],
        strategy="momentum", edge=edge,
        claude_probability=trade["probability"],
        market_probability=price, kelly_frac=config.KELLY_FRACTION,
        end_date=trade.get("end_date", ""),
    )
    return order_id is not None


# ──────────────────────────────────────────────────────────
# 8. CYCLE ORCHESTRATOR
# ──────────────────────────────────────────────────────────

def run_cycle():
    global _cycle_count, _daily_pnl, _daily_date
    _cycle_count += 1

    today = date.today()
    if _daily_date != today:
        _daily_pnl = 0.0
        _daily_date = today

    print(f"\n[INFO] === CYCLE {_cycle_count} ===")

    bankroll = execution.get_balance()
    if bankroll <= 0:
        print("[WARN] Zero balance — skipping cycle")
        return

    open_trades = database.get_open_trades()
    recent_trades = database.get_recent_trades(config.WIN_RATE_WINDOW)

    live_positions = execution.get_positions()
    live_pos_value = sum(float(p.get("currentValue", 0)) for p in (live_positions or []) if float(p.get("size", 0)) > 0)
    num_positions = sum(1 for p in (live_positions or []) if float(p.get("size", 0)) > 0)
    total_equity = bankroll + live_pos_value

    print(f"[INFO] Cash: ${bankroll:.2f} | Positions: ${live_pos_value:.2f} ({num_positions}) | Total: ${total_equity:.2f}")

    if open_trades:
        _check_existing_positions(open_trades)

    # Daily loss limit — uses SESSION START equity so the threshold doesn't shrink as we lose
    daily_loss = _session_start_bankroll - total_equity
    if daily_loss > _session_start_bankroll * config.DAILY_LOSS_LIMIT_PCT:
        print(f"[HALT] Daily loss limit hit (${daily_loss:.2f} vs ${_session_start_bankroll * config.DAILY_LOSS_LIMIT_PCT:.2f} cap)")
        _record_equity(bankroll, live_pos_value)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    if num_positions >= config.MAX_OPEN_POSITIONS:
        print(f"[INFO] Max positions ({config.MAX_OPEN_POSITIONS}) reached — monitoring only")
        _record_equity(bankroll, live_pos_value)
        _print_portfolio(bankroll, open_trades, recent_trades)
        return

    print("[INFO] Scanning all markets...")
    markets = market_data.get_active_markets(limit=config.MAX_MARKETS_PER_CYCLE)
    if not markets:
        print("[INFO] No markets found")
        _record_equity(bankroll, live_pos_value)
        return

    markets = [m for m in markets if not _is_junk_market(m.question)]
    open_token_ids = {t["token_id"] for t in open_trades}
    markets = [m for m in markets if m.yes_token_id not in open_token_ids and m.no_token_id not in open_token_ids]
    markets = [m for m in markets if _is_allowed_market_duration(m.question)]

    hard_filtered = []
    for m in markets:
        passes, reason = _hard_filter_market(m)
        if not passes:
            print(f"  [FILTER] SKIP {m.question[:50]} — {reason}")
            continue
        hard_filtered.append(m)
    markets = hard_filtered

    active = _get_active_strategies()
    crypto_updown = [m for m in markets if "up or down" in m.question.lower()]
    crypto_all = [m for m in markets if any(k in m.question.lower()
                  for k in ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "bnb", "doge", "crypto"])]
    print(f"[INFO] {len(markets)} markets to scan ({len(crypto_updown)} up/down, {len(crypto_all)} crypto total) | ACTIVE_STRATEGIES = {active}")

    trades_placed = 0

    # === THRESHOLD ===
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

    # === ARBITRAGE ===
    if config.ENABLE_ARB:
        print("[INFO] [ARB] Scanning for arbitrage...")
        arb_opportunities = scan_all_markets(markets)

        if arb_opportunities:
            for arb in arb_opportunities[:1]:
                if bankroll < config.MIN_ORDER_SIZE_USD * 2:
                    break
                arb_question = arb.get("question", "")
                arb_asset = extract_asset_key(arb_question)
                if arb_asset and any(extract_asset_key(t.get("market_question", "")) == arb_asset for t in open_trades):
                    print(f"  [ARB] BLOCK: already hold {arb_asset}")
                    continue

                sized = size_arb(arb, bankroll)
                if not sized:
                    continue

                proposal = risk_manager.build_proposal(
                    strategy="arb", market_id=sized["market_id"],
                    side="BOTH", proposed_size_usdc=sized["total_cost"],
                    expected_profit_usdc=sized["expected_profit"],
                    net_edge_pct=sized["profit_pct"],
                )
                decision = risk_manager.evaluate_trade(proposal)
                risk_manager.log_decision(proposal, decision)

                if not decision["approved"]:
                    continue

                result = execution.execute_arb_pair(
                    risk_decision=decision,
                    yes_token=sized["yes_token"], no_token=sized["no_token"],
                    yes_price=sized["yes_price"], no_price=sized["no_price"],
                    shares=sized["shares"], market_id=sized["market_id"],
                    question=sized["question"], profit_pct=sized["profit_pct"],
                )
                if result:
                    trades_placed += 1
                    bankroll -= result["total_cost"]
                    open_trades.append({"market_question": f"[ARB-YES] {result['question'][:80]}", "outcome": "Yes"})
                    open_trades.append({"market_question": f"[ARB-NO] {result['question'][:80]}", "outcome": "No"})

    # === BINANCE-LAG ===
    if config.ENABLE_BINANCE_LAG and bankroll >= config.MIN_ORDER_SIZE_USD and trades_placed == 0:
        lag_trades = [t for t in database.get_recent_trades(config.BINANCE_LAG_AUTO_DISABLE_TRADES)
                      if "[LAG]" in (t.get("market_question") or "")]
        if len(lag_trades) >= config.BINANCE_LAG_AUTO_DISABLE_TRADES:
            lag_pnl = sum(t.get("pnl") or 0 for t in lag_trades)
            if lag_pnl < config.BINANCE_LAG_MIN_EV:
                print(f"[INFO] [BINANCE-LAG] Auto-throttled: negative EV (${lag_pnl:.2f}) over {len(lag_trades)} trades")
            else:
                lag_trades = []

        if len(lag_trades) < config.BINANCE_LAG_AUTO_DISABLE_TRADES:
            crypto_lag_markets = [m for m in markets if "up or down" in m.question.lower()]
            print(f"[INFO] [BINANCE-LAG] Scanning {len(crypto_lag_markets)} up/down markets for lag opportunities...")
            lag_candidates = scan_binance_lag(markets)

            if not lag_candidates:
                print(f"[INFO] [BINANCE-LAG] No candidates found this cycle")

            for cand in lag_candidates[:1]:
                print(f"  [BINANCE-LAG] PROPOSED: {cand['coin']} {cand['side']} | "
                      f"move={cand['move_10m']:+.2f}% | Poly={cand['poly_price']:.2f} | "
                      f"edge={cand['edge']:.1%} | vol={cand['vol_ratio']:.1f}x")

                if is_duplicate_exposure(cand["question"], cand["outcome"], open_trades):
                    print(f"  [BINANCE-LAG] BLOCK: already hold {extract_asset_key(cand['question'])} {cand['outcome']}")
                    continue

                asset_key = extract_asset_key(cand["question"])
                if asset_key and _lag_cooldown_until.get(asset_key, 0) > time.time():
                    remain = int(_lag_cooldown_until[asset_key] - time.time())
                    print(f"  [BINANCE-LAG] SKIP {asset_key}: cooldown active ({remain}s left)")
                    continue

                if cand["coin"] != "BTC":
                    btc_conf = crypto_predictor.get_btc_confirmation(cand["side"])
                    if not btc_conf["agrees"]:
                        print(f"  [BTC-LEAD] BLOCK {cand['coin']} {cand['side']}: "
                              f"BTC moved {btc_conf['btc_move_180s_pct']:+.2f}% in 180s")
                        continue

                bias = macro.get_macro_bias()
                if bias.get("confidence") == "high":
                    if bias.get("bias") == "bearish" and cand["side"] == "UP":
                        print(f"  [MACRO] BLOCK bearish bias vs UP trade: {bias.get('reasoning','')[:60]}")
                        continue
                    if bias.get("bias") == "bullish" and cand["side"] == "DOWN":
                        print(f"  [MACRO] BLOCK bullish bias vs DOWN trade: {bias.get('reasoning','')[:60]}")
                        continue

                try:
                    from core import yfinance_data
                    risk_signal = yfinance_data.get_risk_off_signal()
                    if risk_signal.get("risk_off") and cand["side"] == "UP":
                        sigs = ", ".join(risk_signal.get("signals", []))
                        print(f"  [RISK-OFF] BLOCK UP trade: {sigs}")
                        continue
                except Exception:
                    pass

                flow = trader.get_orderbook_flow(cand["token_id"])
                print(f"  [FLOW] {cand['coin']} {cand['outcome']} "
                      f"buy_ratio={flow['buy_ratio']:.2f} ({flow['signal']})")
                if flow["buy_ratio"] < 0.15 and cand["edge"] < config.TIER_A_EDGE:
                    print(f"  [FLOW] BLOCK {cand['coin']} {cand['outcome']}: extreme sell pressure ({flow['buy_ratio']:.2f})")
                    continue

                mf = market_filter_check(cand["symbol"], cand["side"])
                print(f"  [FILTER] {cand['coin']} trend={mf['trend']['label']} "
                      f"pressure={mf['pressure']['score']:+.2f} {mf['pressure']['label']} "
                      f"danger={mf['danger']}")
                if not mf["allowed"]:
                    print(f"  [BLOCK] {cand['coin']} {cand['side']} blocked by {mf['block_reason']}")
                    continue

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

                max_spend = min(total_equity * config.BINANCE_LAG_MAX_POSITION_PCT, bankroll - 2.0)
                if max_spend < config.MIN_ORDER_SIZE_USD:
                    print(f"  [BINANCE-LAG] Insufficient funds (${max_spend:.2f})")
                    continue

                price = cand["poly_price"]
                shares = max(5, int(max_spend / price)) if price > 0 else 5
                cost = shares * price

                proposal = risk_manager.build_proposal(
                    strategy="binance-lag", market_id=cand["market_id"],
                    side=cand["side"], proposed_size_usdc=cost,
                    expected_profit_usdc=cost * cand["edge"],
                    net_edge_pct=cand["edge"], confidence=confidence,
                )
                decision = risk_manager.evaluate_trade(proposal)
                risk_manager.log_decision(proposal, decision)

                if not decision["approved"]:
                    continue

                resized_cost = decision["final_size_usdc"]
                if resized_cost < cost:
                    shares = max(5, int(resized_cost / price)) if price > 0 else 5
                    cost = shares * price

                print(f"  [BINANCE-LAG] EXECUTING: {cand['coin']} {cand['side']} | "
                      f"{shares} shares @ ${price:.3f} (${cost:.2f}) | edge={cand['edge']:.1%}")

                order_id = execution.execute_buy(
                    risk_decision=decision, token_id=cand["token_id"],
                    price=price, shares=shares, market_id=cand["market_id"],
                    question=f"[LAG] {cand['question'][:80]}",
                    outcome=cand["outcome"], strategy="binance-lag",
                    edge=cand["edge"], claude_probability=cand["true_prob"],
                    market_probability=price,
                    kelly_frac=config.BINANCE_LAG_MAX_POSITION_PCT,
                    end_date=cand.get("end_date", ""),
                )

                if order_id:
                    trades_placed += 1
                    bankroll -= cost
                    open_trades.append({"market_question": f"[LAG] {cand['question'][:80]}", "outcome": cand["outcome"]})
                else:
                    print(f"  [BINANCE-LAG] Order FAILED")

    # === RESOLUTION SNIPER ===
    if config.ENABLE_SNIPE and bankroll >= config.MIN_ORDER_SIZE_USD:
        print("[INFO] [SNIPE] Scanning for resolution snipes...")
        snipes = scan_resolution_snipes(markets)

        if snipes:
            print(f"  Found {len(snipes)} snipe opportunities!")
            for snipe in snipes[:2]:
                if bankroll < config.MIN_ORDER_SIZE_USD:
                    break
                if is_duplicate_exposure(snipe["question"], snipe["outcome"], open_trades):
                    print(f"  [SNIPE] BLOCK: duplicate {extract_asset_key(snipe['question'])} {snipe['outcome']}")
                    continue
                snipe_asset = extract_asset_key(snipe["question"])
                if snipe_asset and _lag_cooldown_until.get(snipe_asset, 0) > time.time():
                    remain = int(_lag_cooldown_until[snipe_asset] - time.time())
                    print(f"  [SNIPE] SKIP {snipe_asset}: cooldown active ({remain}s left)")
                    continue
                print(f"  SNIPE: {snipe['question'][:50]} | {snipe['outcome']} @ ${snipe['ask_price']:.3f} | Profit: {snipe['profit_pct']:.1%}")
                if execute_snipe(snipe, bankroll):
                    trades_placed += 1
                    bankroll -= snipe["ask_price"] * 5
                    open_trades.append({"market_question": snipe["question"], "outcome": snipe["outcome"]})
        else:
            print("[INFO] No snipe opportunities (nothing near-certain at a discount)")

    # === MOMENTUM + CLAUDE GATE ===
    if config.ENABLE_MOMENTUM_CLAUDE and bankroll >= config.MIN_ORDER_SIZE_USD and trades_placed == 0:
        regime = crypto_predictor.get_regime()
        candle = crypto_predictor.get_candle_position()
        print(f"[INFO] [MOMENTUM] Checking | regime={regime} candle={candle['phase']} ({candle['seconds_elapsed']}s in)")

        if regime in ("trending", "choppy"):
            crypto_markets = [m for m in markets
                              if crypto_predictor.is_crypto_updown_market(m.question)]

            if crypto_markets:
                print(f"[INFO] Evaluating {len(crypto_markets)} crypto markets (regime={regime}, candle={candle['phase']})...")
                best_trade = None
                best_edge = 0.0

                for cand in crypto_markets:
                    if not cand.yes_token_id or not cand.no_token_id:
                        continue

                    yes_price = trader.get_midpoint(cand.yes_token_id)
                    no_price = trader.get_midpoint(cand.no_token_id)
                    if not yes_price or not no_price:
                        continue

                    q_lower = cand.question.lower()
                    symbol = None
                    for coin, sym in crypto_predictor.BINANCE_SYMBOLS.items():
                        if coin in q_lower:
                            symbol = sym
                            break

                    binance_prob = crypto_predictor.get_binance_implied_probability(symbol) if symbol else None

                    if binance_prob is not None and yes_price > 0:
                        gap = binance_prob - yes_price
                        print(f"  [GAP SIGNAL] {cand.question[:40]} | Binance={binance_prob:.0%} Polymarket={yes_price:.0%} Gap={gap:+.0%}")

                        if yes_price < 0.25 or yes_price > 0.75:
                            print(f"  [GAP] SKIP {cand.question[:40]}: price {yes_price:.2f} outside 0.25-0.75 band (market already decided)")
                        elif gap >= config.MIN_EDGE_CRYPTO:
                            conf_label = "high" if gap >= config.TIER_A_EDGE else "medium"
                            best_edge = gap
                            best_trade = {
                                "market_id": cand.market_id, "question": cand.question,
                                "token_id": cand.yes_token_id, "outcome": "Yes",
                                "price": yes_price, "probability": binance_prob,
                                "edge": gap, "signal": {"confidence": conf_label, "combo": "gap", "kelly_mult": 1.0,
                                                         "reasoning": f"Gap={gap:.0%} Binance={binance_prob:.0%}"},
                                "end_date": cand.end_date,
                            }
                            continue
                        elif gap <= -config.MIN_EDGE_CRYPTO:
                            conf_label = "high" if abs(gap) >= config.TIER_A_EDGE else "medium"
                            best_edge = abs(gap)
                            best_trade = {
                                "market_id": cand.market_id, "question": cand.question,
                                "token_id": cand.no_token_id, "outcome": "No",
                                "price": no_price, "probability": 1.0 - binance_prob,
                                "edge": abs(gap), "signal": {"confidence": conf_label, "combo": "gap", "kelly_mult": 1.0,
                                                              "reasoning": f"Gap={gap:.0%} Binance={binance_prob:.0%}"},
                                "end_date": cand.end_date,
                            }
                            continue

                    for outcome, token_id, price in [("Yes", cand.yes_token_id, yes_price), ("No", cand.no_token_id, no_price)]:
                        if price < 0.25 or price > 0.75:
                            print(f"  [MOMENTUM] SKIP {cand.question[:30]} {outcome} @ {price:.2f}: outside 0.25-0.75 band")
                            continue
                        result = crypto_predictor.estimate_crypto_probability(cand.question, price, outcome)
                        if not result:
                            print(f"  [MOMENTUM] {cand.question[:30]} {outcome} → rejected by crypto_predictor (see INFO log)")
                            continue
                        edge = abs(result["probability"] - price)
                        if edge < config.MIN_EDGE_CRYPTO:
                            print(f"  [MOMENTUM] {cand.question[:30]} {outcome} @ {price:.2f} prob={result['probability']:.2f} edge={edge:+.1%} < {config.MIN_EDGE_CRYPTO:.0%}")
                            continue
                        tier_here = "A" if edge >= config.TIER_A_EDGE else "B"
                        if tier_here == "A" and result.get("confidence") == "low":
                            print(f"  [MOMENTUM] {cand.question[:30]} {outcome} edge={edge:.1%} Tier A but low confidence → skip")
                            continue
                        if edge > best_edge:
                            best_edge = edge
                            best_trade = {
                                "market_id": cand.market_id, "question": cand.question,
                                "token_id": token_id, "outcome": outcome,
                                "price": price, "probability": result["probability"],
                                "edge": edge, "signal": result,
                                "end_date": cand.end_date,
                            }

                if best_trade:
                    if is_duplicate_exposure(best_trade["question"], best_trade["outcome"], open_trades):
                        print(f"  [MOMENTUM] BLOCK: duplicate {extract_asset_key(best_trade['question'])} {best_trade['outcome']}")
                        best_trade = None

                if best_trade:
                    mom_asset = extract_asset_key(best_trade["question"])
                    if mom_asset and _lag_cooldown_until.get(mom_asset, 0) > time.time():
                        remain = int(_lag_cooldown_until[mom_asset] - time.time())
                        print(f"  [MOMENTUM] SKIP {mom_asset}: cooldown active ({remain}s left)")
                        best_trade = None

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
                    try:
                        from core import yfinance_data
                        risk_signal = yfinance_data.get_risk_off_signal()
                        mom_side = "UP" if best_trade["outcome"] == "Yes" else "DOWN"
                        if risk_signal.get("risk_off") and mom_side == "UP":
                            sigs = ", ".join(risk_signal.get("signals", []))
                            print(f"  [RISK-OFF] BLOCK UP momentum: {sigs}")
                            best_trade = None
                    except Exception:
                        pass

                if best_trade:
                    flow = trader.get_orderbook_flow(best_trade["token_id"])
                    print(f"  [FLOW] {best_trade['outcome']} buy_ratio={flow['buy_ratio']:.2f} ({flow['signal']})")
                    if flow["buy_ratio"] < 0.15 and best_trade["edge"] < config.TIER_A_EDGE:
                        print(f"  [FLOW] BLOCK momentum: extreme sell pressure ({flow['buy_ratio']:.2f})")
                        best_trade = None

                if best_trade:
                    combo = best_trade["signal"].get("combo", "?")
                    print(f"  MOMENTUM: {best_trade['question'][:50]} | {best_trade['outcome']} @ {best_trade['price']:.3f} | Edge: {best_trade['edge']:.1%} | combo={combo}")
                    _execute_momentum_trade(best_trade, bankroll)
                    trades_placed += 1
                    open_trades.append({"market_question": best_trade["question"], "outcome": best_trade["outcome"]})
                else:
                    print("[INFO] [MOMENTUM] No signals found | no edge >= 1% after fees (Tier A/B thresholds)")
            else:
                print("[INFO] [MOMENTUM] No crypto up/down markets found in filtered list")
        else:
            print(f"[INFO] [MOMENTUM] Skipped | regime={regime} (need trending or choppy)")

    if trades_placed:
        print(f"\n[INFO] {trades_placed} trade(s) placed this cycle")

    _record_equity(bankroll, live_pos_value)
    _print_portfolio(bankroll, open_trades, recent_trades)


# ──────────────────────────────────────────────────────────
# 9. WATCHER THREADS
# ──────────────────────────────────────────────────────────

def pnl_watcher_thread():
    """Monitors open positions every 3 seconds and places SELL exits per rules."""
    last_log_cycle = 0
    while True:
        try:
            open_trades = database.get_open_trades()

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
                end_date_str = trade.get("end_date") or ""

                # Check if this market has expired
                market_expired = False
                if end_date_str:
                    try:
                        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                        market_expired = datetime.now(timezone.utc) > end_dt
                    except (ValueError, TypeError):
                        pass

                # Force-resolve expired positions (paper or live)
                if market_expired and token_id in _dead_tokens:
                    exit_price = 0.0
                    real_pnl = -cost
                    if config.PAPER_TRADING:
                        from core.paper_trader import paper_close_position
                        paper_close_position(token_id, exit_price, trade_size)
                    r_mult = calculate_r_multiple(entry_price, exit_price, entry_price)
                    database.update_trade_result(trade["id"], exit_price, real_pnl, r_mult, "lost")
                    print(f"[P&L WATCHER] EXPIRED '{question[:40]}' | PnL=${real_pnl:+.2f}")
                    continue

                if token_id in _dead_tokens:
                    continue

                current_price = trader.get_midpoint(token_id)
                if current_price is None:
                    _dead_tokens.add(token_id)

                    # If market is expired, resolve immediately
                    if market_expired:
                        exit_price = 0.0
                        real_pnl = -cost
                        if config.PAPER_TRADING:
                            from core.paper_trader import paper_close_position
                            paper_close_position(token_id, exit_price, trade_size)
                        r_mult = calculate_r_multiple(entry_price, exit_price, entry_price)
                        database.update_trade_result(trade["id"], exit_price, real_pnl, r_mult, "lost")
                        print(f"[P&L WATCHER] EXPIRED '{question[:40]}' | PnL=${real_pnl:+.2f}")
                        continue

                    live_positions = execution.get_positions()
                    actual_value = 0.0
                    still_held = False
                    for p in (live_positions or []):
                        if p.get("asset") == token_id:
                            still_held = float(p.get("size", 0)) > 0
                            actual_value = float(p.get("currentValue", 0))
                            break
                    if still_held:
                        continue
                    real_pnl = actual_value - cost
                    r_mult = calculate_r_multiple(entry_price, actual_value / max(trade_size, 1), entry_price)
                    status = "won" if real_pnl > 0 else "lost"
                    database.update_trade_result(trade["id"], actual_value / max(trade_size, 1), real_pnl, r_mult, status)
                    print(f"[P&L WATCHER] RESOLVED '{question[:40]}' | {status.upper()} | PnL=${real_pnl:+.2f}")
                    if status == "lost":
                        cool_key = extract_asset_key(question)
                        if cool_key:
                            existing = _lag_cooldown_until.get(cool_key, 0)
                            if existing > time.time():
                                _lag_cooldown_until[cool_key] = time.time() + 1800
                                print(f"[COOLDOWN] {cool_key} ESCALATED to 30 min (repeated loss)")
                            else:
                                _lag_cooldown_until[cool_key] = time.time() + 600
                                print(f"[COOLDOWN] {cool_key} locked for 10 min after loss")
                    continue
                if current_price <= 0:
                    continue

                pnl_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0

                end_date = trade.get("end_date") or ""
                minutes_left = 999
                if end_date:
                    try:
                        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                        minutes_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 60
                    except (ValueError, TypeError):
                        pass

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

                # Force-close expired markets immediately
                if minutes_left < -5:
                    should_sell = True
                    exit_price = current_price
                    reason = f"EXPIRED (ended {abs(minutes_left):.0f}min ago)"

                # Also detect expired markets by parsing the question title
                # for trades that have no end_date stored
                if not should_sell and not end_date and elapsed_min is not None:
                    q_lower = question.lower()
                    import re
                    date_match = re.search(r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})', q_lower)
                    if date_match:
                        try:
                            month_name = date_match.group(1)
                            day = int(date_match.group(2))
                            now_utc = datetime.now(timezone.utc)
                            month_num = ["january","february","march","april","may","june","july","august","september","october","november","december"].index(month_name) + 1
                            market_date = datetime(now_utc.year, month_num, day, 23, 59, tzinfo=timezone.utc)
                            if now_utc > market_date:
                                should_sell = True
                                reason = f"EXPIRED (market date {month_name.title()} {day} passed)"
                        except (ValueError, IndexError):
                            pass

                if should_sell and reason.startswith("EXPIRED"):
                    print(f"[P&L WATCHER] CLOSE EXPIRED '{question[:40]}' | ${entry_price:.2f}→${current_price:.2f} | {reason}")
                    real_size = trade_size
                    live_positions = execution.get_positions()
                    for p in (live_positions or []):
                        if p.get("asset") == token_id and float(p.get("size", 0)) > 0:
                            real_size = float(p["size"])
                            break
                    if real_size > 0 and not config.DRY_RUN:
                        sell_price = max(0.01, min(0.99, round(current_price - 0.01, 2)))
                        try:
                            execution.execute_sell(token_id, sell_price, real_size)
                        except Exception as e:
                            print(f"[P&L WATCHER] Expired sell error: {e}")
                    pnl = (current_price - entry_price) * real_size
                    r_mult = calculate_r_multiple(entry_price, current_price, entry_price)
                    status = "won" if pnl > 0 else "lost"
                    database.update_trade_result(trade["id"], current_price, pnl, r_mult, status)
                    print(f"[P&L WATCHER] Expired closed: PnL=${pnl:+.2f} | {status}")
                    continue

                # Detect short-duration markets (≤10 min total) — let them resolve
                # naturally. Position sizes are small ($5); binary pays $1 on win.
                # Stop-losses on 5-min binaries consistently sell -15% dips that
                # would have resolved to +100%. Only exit on Binance reversal.
                is_short_binary = False
                total_duration_min = None
                if elapsed_min is not None and minutes_left < 999:
                    total_duration_min = elapsed_min + minutes_left
                    is_short_binary = total_duration_min <= 10

                if is_short_binary:
                    # Only rule for short binaries: Binance reversal exit for LAG trades
                    if "[LAG]" in question:
                        entry_side = (trade.get("outcome") or "").lower()
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
                else:
                    # Scale exit thresholds by time remaining.
                    # Daily markets swing 20-30% routinely; tight stops just
                    # realize noise as losses. Only panic-sell on large moves.
                    if minutes_left > 60:
                        # >1 hour left: very wide stops, let the position breathe
                        cut_loss_threshold = -0.35
                        time_exit_threshold = -0.25
                        time_exit_hold_min = 30.0
                    elif minutes_left > 10:
                        # 10-60 min left: moderate stops
                        cut_loss_threshold = -0.25
                        time_exit_threshold = -0.15
                        time_exit_hold_min = 10.0
                    else:
                        # <10 min left: original-ish thresholds
                        cut_loss_threshold = -0.15
                        time_exit_threshold = -0.05
                        time_exit_hold_min = 3.0

                    if pnl_pct <= cut_loss_threshold:
                        should_sell = True
                        reason = f"CUT LOSS ({pnl_pct:+.0%}, {minutes_left:.0f}min left)"

                    elif "[LAG]" in question:
                        entry_side = (trade.get("outcome") or "").lower()
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

                    if not should_sell and 0.40 <= entry_price <= 0.60 and current_price <= entry_price - 0.08:
                        should_sell = True
                        reason = f"ABS STOP (${entry_price:.2f}→${current_price:.2f})"

                    if not should_sell and minutes_left < 1.0 and pnl_pct < -0.05:
                        should_sell = True
                        reason = f"PRE-RESOLUTION ({pnl_pct:+.0%}, {minutes_left*60:.0f}s left)"

                    if not should_sell and elapsed_min is not None and elapsed_min >= time_exit_hold_min and pnl_pct < time_exit_threshold:
                        should_sell = True
                        reason = f"TIME EXIT (held {elapsed_min:.1f}min, {pnl_pct:+.0%})"

                    if not should_sell and minutes_left < 1.5 and pnl_pct >= 0.15:
                        should_sell = True
                        reason = f"LOCK PROFIT (+{pnl_pct:.0%}, {minutes_left*60:.0f}s left)"

                if should_sell:
                    print(f"[P&L WATCHER] SELL '{question[:40]}' | ${entry_price:.2f}→${current_price:.2f} | pnl={pnl_pct:+.0%} | {reason}")

                    live_positions = execution.get_positions()
                    real_size = trade_size
                    for p in (live_positions or []):
                        if p.get("asset") == token_id and float(p.get("size", 0)) > 0:
                            real_size = float(p["size"])
                            break

                    if real_size > 0 and not config.DRY_RUN:
                        sell_price = max(0.01, min(0.99, round(current_price - 0.01, 2)))
                        try:
                            execution.execute_sell(token_id, sell_price, real_size)
                            print(f"[P&L WATCHER] Sell order placed @ ${sell_price:.2f} x {real_size:.1f}")
                        except Exception as e:
                            if "400" in str(e) or "balance" in str(e).lower():
                                print(f"[P&L WATCHER] CRITICAL — sell failed (balance), position still open: {e}")
                                continue
                            print(f"[P&L WATCHER] Sell error: {e}")
                            continue

                    pnl = (current_price - entry_price) * real_size
                    r_mult = calculate_r_multiple(entry_price, current_price, entry_price)
                    status = "won" if pnl > 0 else "lost"
                    database.update_trade_result(trade["id"], current_price, pnl, r_mult, status)
                    print(f"[P&L WATCHER] Closed: PnL=${pnl:+.2f} | status={status}")

                    if status == "lost":
                        cool_key = extract_asset_key(question)
                        if cool_key:
                            existing = _lag_cooldown_until.get(cool_key, 0)
                            if existing > time.time():
                                # Repeated loss: escalate cooldown to 30 min
                                _lag_cooldown_until[cool_key] = time.time() + 1800
                                print(f"[COOLDOWN] {cool_key} ESCALATED to 30 min (repeated loss)")
                            else:
                                _lag_cooldown_until[cool_key] = time.time() + 600
                                print(f"[COOLDOWN] {cool_key} locked for 10 min after loss")

        except Exception as e:
            print(f"[P&L WATCHER] Error: {e}")

        time.sleep(3)


def macro_watcher_thread():
    """Refreshes the macro bias every MACRO_REFRESH_SEC."""
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
            time.sleep(600)


# ──────────────────────────────────────────────────────────
# 10. ENTRY POINT
# ──────────────────────────────────────────────────────────

def start():
    """Spawn watcher threads and run the main cycle loop.

    Called by main.py after init (DB, moondev smoke, stale-order cancel,
    ghost-trade cleanup, position sync, session bankroll set, banner print).
    """
    watcher = threading.Thread(target=pnl_watcher_thread, daemon=True)
    watcher.start()
    print("[P&L WATCHER] Started — checking every 3 seconds")

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

