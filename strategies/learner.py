"""Adaptive learner — analyzes past trades and adjusts strategy.

Tracks performance by:
- Market category (crypto, politics, climate, geopolitical, finance, other)
- Price range (cheap <0.30, mid 0.30-0.70, expensive >0.70)
- Time of day
- Edge range

Uses this to:
- Boost/penalize edge for categories that win/lose
- Skip categories with sustained losses
- Adjust position sizing based on historical accuracy
"""

from core import database
from utils.logger import log

# Category keywords for classification
CATEGORIES = {
    "crypto": {"bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto",
               "xrp", "dogecoin", "doge", "cardano", "polygon", "matic", "defi",
               "binance", "coinbase", "token", "coin", "altcoin", "up or down",
               "5 minutes", "megaeth", "airdrop"},
    "politics": {"trump", "biden", "congress", "senate", "election", "democrat",
                 "republican", "vance", "president", "governor", "vote", "poll",
                 "party", "cabinet", "impeach"},
    "geopolitical": {"iran", "russia", "china", "ukraine", "nato", "ceasefire",
                     "war", "strike", "missile", "sanctions", "invasion", "lebanon",
                     "israel", "gaza", "north korea", "taiwan"},
    "climate": {"climate", "hurricane", "earthquake", "wildfire", "temperature",
                "flood", "drought", "storm", "weather", "emissions"},
    "finance": {"fed", "interest rate", "inflation", "gdp", "tariff", "trade war",
                "stock", "s&p", "nasdaq", "dow", "oil", "gold", "wti", "crude",
                "treasury", "bond", "recession", "unemployment", "cpi", "eggs"},
}

# Minimum trades before we trust the stats
MIN_TRADES_FOR_LEARNING = 3


def classify_market(question: str) -> str:
    """Classify a market question into a category."""
    q_lower = question.lower()
    scores = {}
    for category, keywords in CATEGORIES.items():
        score = sum(1 for kw in keywords if kw in q_lower)
        if score > 0:
            scores[category] = score
    if scores:
        return max(scores, key=scores.get)
    return "other"


def classify_price_range(price: float) -> str:
    """Classify market price into a bucket."""
    if price < 0.30:
        return "cheap"
    elif price <= 0.70:
        return "mid"
    else:
        return "expensive"


def get_performance_stats() -> dict:
    """Analyze all closed trades and return performance by category and price range.

    Returns dict with:
        categories: {category: {wins, losses, total_pnl, win_rate, avg_pnl}}
        price_ranges: {range: {wins, losses, total_pnl, win_rate, avg_pnl}}
        overall: {wins, losses, total_pnl, win_rate}
    """
    all_trades = database.get_recent_trades(100)  # Last 100 closed trades

    stats = {
        "categories": {},
        "price_ranges": {},
        "overall": {"wins": 0, "losses": 0, "total_pnl": 0.0},
    }

    for trade in all_trades:
        question = trade.get("market_question", "")
        entry_price = trade.get("market_probability", trade.get("entry_price", 0.5))
        pnl = trade.get("pnl", 0) or 0
        status = trade.get("status", "")

        category = classify_market(question)
        price_range = classify_price_range(entry_price)

        is_win = status == "won" or pnl > 0

        # Overall
        if is_win:
            stats["overall"]["wins"] += 1
        else:
            stats["overall"]["losses"] += 1
        stats["overall"]["total_pnl"] += pnl

        # By category
        if category not in stats["categories"]:
            stats["categories"][category] = {"wins": 0, "losses": 0, "total_pnl": 0.0}
        cat = stats["categories"][category]
        if is_win:
            cat["wins"] += 1
        else:
            cat["losses"] += 1
        cat["total_pnl"] += pnl

        # By price range
        if price_range not in stats["price_ranges"]:
            stats["price_ranges"][price_range] = {"wins": 0, "losses": 0, "total_pnl": 0.0}
        pr = stats["price_ranges"][price_range]
        if is_win:
            pr["wins"] += 1
        else:
            pr["losses"] += 1
        pr["total_pnl"] += pnl

    # Calculate derived stats
    for bucket_type in ["categories", "price_ranges"]:
        for key, data in stats[bucket_type].items():
            total = data["wins"] + data["losses"]
            data["total_trades"] = total
            data["win_rate"] = data["wins"] / total if total > 0 else 0.0
            data["avg_pnl"] = data["total_pnl"] / total if total > 0 else 0.0

    overall = stats["overall"]
    total = overall["wins"] + overall["losses"]
    overall["total_trades"] = total
    overall["win_rate"] = overall["wins"] / total if total > 0 else 0.0

    return stats


def get_edge_adjustment(question: str, market_price: float) -> float:
    """Return an edge adjustment based on past performance.

    Positive = boost (category has been profitable)
    Negative = penalty (category has been losing)
    0.0 = not enough data yet
    """
    stats = get_performance_stats()

    category = classify_market(question)
    cat_stats = stats["categories"].get(category)

    if not cat_stats or cat_stats["total_trades"] < MIN_TRADES_FOR_LEARNING:
        return 0.0

    # Scale adjustment based on win rate vs 50% baseline
    # Win rate 70% -> +0.03 boost
    # Win rate 30% -> -0.03 penalty
    win_rate = cat_stats["win_rate"]
    adjustment = (win_rate - 0.50) * 0.15

    # Also factor in average P&L
    # Losing money on average -> additional penalty
    if cat_stats["avg_pnl"] < -0.50:
        adjustment -= 0.02
    elif cat_stats["avg_pnl"] > 0.50:
        adjustment += 0.01

    return round(adjustment, 4)


def record_loss_pattern(question: str, entry_price: float, exit_price: float, loss_pct: float):
    """Classify a stop-loss exit and record it for future avoidance."""
    category = classify_market(question)
    price_range = classify_price_range(entry_price)
    database.record_loss_pattern(question, category, price_range, loss_pct)
    log.info("Loss pattern recorded: %s/%s loss=%.1f%% — '%s'",
             category, price_range, loss_pct * 100, question[:50])


def should_skip_market(question: str) -> tuple[bool, str]:
    """Check if we should skip this market based on learning.

    Returns (should_skip, reason).
    Crypto can be skipped if win rate is really bad (< 35% over 10+ trades).
    Also skips category+price_range combos with 3+ stop-loss patterns.
    """
    stats = get_performance_stats()
    category = classify_market(question)
    cat_stats = stats["categories"].get(category)

    if not cat_stats or cat_stats["total_trades"] < MIN_TRADES_FOR_LEARNING:
        # Even without enough category stats, check loss patterns
        price_range = classify_price_range(0.5)  # default mid
        loss_patterns = database.get_loss_patterns(category, price_range, limit=20)
        if len(loss_patterns) >= 3:
            return True, f"category '{category}/{price_range}' has {len(loss_patterns)} stop-loss exits — avoiding"
        return False, ""

    # Crypto can be skipped too if it's been REALLY bad
    if category == "crypto":
        if cat_stats["win_rate"] < 0.35 and cat_stats["total_trades"] >= 10:
            return True, f"crypto win rate {cat_stats['win_rate']:.0%} over {cat_stats['total_trades']} trades — pausing"
        return False, ""

    # Skip category if: 0% win rate with 3+ trades
    if cat_stats["win_rate"] == 0.0 and cat_stats["total_trades"] >= 3:
        return True, f"category '{category}' has 0% win rate over {cat_stats['total_trades']} trades"

    # Skip category if: losing badly (avg PnL < -$1 with 5+ trades)
    if cat_stats["avg_pnl"] < -1.0 and cat_stats["total_trades"] >= 5:
        return True, f"category '{category}' avg PnL ${cat_stats['avg_pnl']:.2f} over {cat_stats['total_trades']} trades"

    # Skip if win rate under 25% with enough data
    if cat_stats["win_rate"] < 0.25 and cat_stats["total_trades"] >= 5:
        return True, f"category '{category}' win rate {cat_stats['win_rate']:.0%} over {cat_stats['total_trades']} trades"

    # Skip category+price_range combos with 3+ stop-loss exits in recent patterns
    # Use a representative price (mid-range) for classification
    for pr in ["cheap", "mid", "expensive"]:
        loss_patterns = database.get_loss_patterns(category, pr, limit=20)
        if len(loss_patterns) >= 3:
            return True, f"category '{category}/{pr}' has {len(loss_patterns)} stop-loss exits — avoiding"

    return False, ""


def get_size_multiplier(question: str) -> float:
    """Return a position size multiplier based on category performance.

    1.0 = normal
    0.5 = halve (losing category)
    1.25 = boost (winning category)
    """
    stats = get_performance_stats()
    category = classify_market(question)
    cat_stats = stats["categories"].get(category)

    if not cat_stats or cat_stats["total_trades"] < MIN_TRADES_FOR_LEARNING:
        return 1.0

    win_rate = cat_stats["win_rate"]

    if win_rate >= 0.65 and cat_stats["total_pnl"] > 0:
        return 1.25  # Winning category — size up slightly
    elif win_rate < 0.40:
        return 0.5   # Losing category — halve position
    else:
        return 1.0


def print_learning_report():
    """Print a summary of what the bot has learned."""
    stats = get_performance_stats()

    if stats["overall"]["total_trades"] == 0:
        print("[LEARN] No completed trades yet — learning in progress")
        return

    print(f"\n[LEARN] ═══ PERFORMANCE REPORT ({stats['overall']['total_trades']} trades) ═══")
    print(f"[LEARN] Overall: {stats['overall']['win_rate']:.0%} win rate | PnL: ${stats['overall']['total_pnl']:+.2f}")

    for category, data in sorted(stats["categories"].items(), key=lambda x: x[1]["total_pnl"], reverse=True):
        icon = "+" if data["total_pnl"] >= 0 else "-"
        skip_note = ""
        if data["win_rate"] == 0.0 and data["total_trades"] >= 3:
            skip_note = " [SKIPPING]"
        elif data["win_rate"] < 0.25 and data["total_trades"] >= 5:
            skip_note = " [SKIPPING]"
        elif data["avg_pnl"] < -1.0 and data["total_trades"] >= 5:
            skip_note = " [SKIPPING]"
        elif data["win_rate"] >= 0.65 and data["total_pnl"] > 0:
            skip_note = " [BOOSTED]"
        elif data["win_rate"] < 0.40 and data["total_trades"] >= MIN_TRADES_FOR_LEARNING:
            skip_note = " [HALVED]"

        print(f"[LEARN]   {category:<14} {data['wins']}W/{data['losses']}L ({data['win_rate']:.0%}) | PnL: ${data['total_pnl']:+.2f} | Avg: ${data['avg_pnl']:+.2f}{skip_note}")

    print()
