"""Configuration constants for the Polymarket trading bot.

=== MATH-BASED SETTINGS (bankroll: $35) ===

Binary option math:
- Buy YES at $0.50, win → $1.00 (100% return), lose → $0 (100% loss)
- Kelly formula for binary: f* = (q - p) / (1 - p)
  where q = true probability, p = market price
- For q=0.60, p=0.50: f* = 0.10/0.50 = 0.20 (20% of bankroll)

With $35 bankroll:
- Polymarket minimum order = $5 = 14.3% of bankroll
- So every trade is already ~full Kelly for a 5% edge trade
- Can't go smaller → must be VERY selective (Claude gate)
- Max 3 concurrent positions = $15 at risk (43% of bankroll)

Stop loss on 5-min markets:
- Markets resolve to $0 or $1 within 5 minutes
- Normal binary option price swings: 10-20% in minutes
- Tight stop losses (6%) = get stopped out of winners constantly
- Better approach: let 5-min markets RESOLVE naturally, use stop
  loss only for longer-duration positions
"""

import os
from dotenv import load_dotenv

load_dotenv()

# === Secrets (from .env) ===
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# === Paper Trading ===
PAPER_TRADING = os.getenv("PAPER_TRADING", "false").lower() == "true"
PAPER_STARTING_BALANCE = float(os.getenv("PAPER_STARTING_BALANCE", "100.0"))

# === Polymarket ===
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
CHAIN_ID = 137  # Polygon

# === Trading Cycle ===
CYCLE_INTERVAL_SEC = 15            # Fast scanning for momentum
MAX_MARKETS_PER_CYCLE = 50

# === Strategy Toggles ===
ENABLE_THRESHOLD = False           # Strategy 0: OFF — mean-reversion loses on crypto trends
ENABLE_ARB = True                  # Strategy 1: arbitrage (Yes+No < $1.00)
ENABLE_SNIPE = True                # Strategy 2: resolution sniper
ENABLE_MOMENTUM_INTRADAY = False   # Strategy 3: intraday Up/Down momentum (OFF)

# === Market Filters ===
MIN_VOLUME = 5
MIN_LIQUIDITY = 50
MAX_RESOLUTION_MINUTES = 30        # Crypto 5-min markets only
MIN_RESOLUTION_MINUTES = 1.5       # Matches candle filter (entry_window + mid_candle)

# === Arbitrage Settings ===
MIN_ARB_PROFIT = 0.005
ARB_MAX_POSITION_PCT = 0.40

# === Sports Filter ===
SPORTS_KEYWORDS = {
    "nba", "nfl", "mlb", "nhl", "mls", "ufc", "wwe", "wnba",
    "premier league", "la liga", "champions league", "serie a", "bundesliga",
    "ligue 1", "eredivisie", "copa america", "euro 2026",
    "touchdown", "home run", "three-pointer", "goal scorer",
    "points", "rebounds", "assists", "rushing yards", "passing yards",
    "batting average", "era", "strikeouts", "saves",
    "mvp", "rookie of the year", "player of the month",
    "super bowl", "world series", "stanley cup", "march madness",
    "olympics", "wimbledon", "us open tennis", "french open tennis",
    "formula 1", "f1", "nascar", "grand prix",
    "pro football", "draft pick", "1st pick", "first pick",
    "quarterback", "wide receiver", "running back", "tight end",
    "lebron", "curry", "mahomes", "ohtani", "messi", "ronaldo",
    "spurs", "bucks", "lakers", "celtics", "warriors", "nets",
    "bulls", "knicks", "heat", "76ers", "suns", "nuggets",
    "chiefs", "eagles", "cowboys", "49ers", "ravens", "bills",
    "yankees", "dodgers", "braves", "astros", "mets", "red sox",
    "spread", "over/under", "o/u", "moneyline",
    "counter-strike", "csgo", "cs2", "dota", "dota2", "dota 2",
    "league of legends", "lol", "valorant", "overwatch",
    "esports", "e-sports", "bo1", "bo3", "bo5",
    "fnatic", "navi", "g2", "faze", "cloud9", "team liquid",
    "major tournament", "esl", "blast", "iem",
    "marseille", "psg", "barcelona", "real madrid", "manchester",
    "liverpool", "arsenal", "chelsea", "tottenham", "juventus",
    "bayern", "inter milan", "ac milan", "atletico",
    "streamed", "streaming", "spotify", "billboard", "album", "song",
    "box office", "movie", "film", "oscar", "emmy", "grammy", "golden globe",
    "netflix", "disney", "hbo", "youtube", "tiktok", "views",
    "subscriber", "followers", "likes", "viral",
    "celebrity", "kardashian", "taylor swift", "drake", "kanye",
    "bachelor", "bachelorette", "reality tv", "survivor", "big brother",
    "american idol", "the voice", "dancing with the stars",
    "weekend", "weeknd", "top chart", "number one", "hit single",
    "tv ratings", "viewership", "audience", "premiere",
    "book sales", "bestseller", "new york times list",
    "baby name", "gender reveal", "wedding", "divorce",
    "influencer", "podcast", "twitch", "content creator",
    "coin flip", "dice roll", "random", "lottery", "powerball",
    "mega millions", "roulette", "casino",
    "weather record", "hottest day", "coldest day",
    "first tweet", "first post", "most liked",
    "guinness", "world record", "eating contest",
    "eurovision",
}

PREFERRED_KEYWORDS = {
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto",
    "xrp", "dogecoin", "doge", "cardano", "ada", "polygon", "matic",
    "defi", "nft", "blockchain", "token", "coin", "altcoin",
    "binance", "coinbase", "sec crypto", "etf",
    "up or down", "5 minutes", "15 minutes", "1 day",
}

# === Market Category Filters ===
WHITELIST_CATEGORIES = ["crypto"]
BLACKLIST_KEYWORDS = ["win the", "election", "president", "minister", "vote", "poll"]

# === Edge Thresholds ===
# Math: with $35 bankroll and $5 min bet, each trade is ~14% of bankroll.
# Need high confidence to justify that concentration.
# Claude gate + 5% edge = only trade when we're 55%+ sure on a 50/50 market.
MIN_EDGE = 0.05                    # 5% edge minimum
MIN_EDGE_CRYPTO = 0.05             # Same for crypto — Claude confirms every trade
MIN_EV_PER_DOLLAR = 0.02           # $0.02 EV per dollar risked

# === Position Sizing ===
# Math: $5 min bet / $35 bankroll = 14.3% per trade forced.
# Kelly says ~20% for a 60/40 edge at even odds.
# So $5 bets are actually near-optimal Kelly for moderate edges.
KELLY_FRACTION = 0.40              # 40% Kelly — but floor is $5 anyway
MAX_POSITION_PCT = 0.20            # Cap at $7 per trade (20% of $35)
MAX_OPEN_POSITIONS = 2             # Reduced — keep buffer cash for sell orders
MIN_ORDER_SIZE_USD = 1.0           # Will get bumped to $5 by Polymarket minimum

# === Risk Management ===
DD_THRESHOLD_HALF = 0.20           # Halve at 20% drawdown ($7 loss)
DD_THRESHOLD_STOP = 0.35           # Stop at 35% drawdown ($12 loss)

# === Stop Loss / Take Profit ===
# Math: 5-min binary markets resolve to $0 or $1. A position bought at
# $0.50 can swing to $0.35 (-30%) and still win at $1.00.
# Tight stops on 5-min markets = selling winners before resolution.
# Let short-term markets resolve. Only stop out longer positions.
STOP_LOSS_PCT = 0.40               # 40% — effectively let 5-min markets resolve
TAKE_PROFIT_PCT = 0.30             # 30% take profit on longer markets
TAKE_PROFIT_CRYPTO_PCT = 0.20      # 20% take profit — lock gains if price jumps
TAKE_PROFIT_EDGE_MIN = 0.01

# === Crypto Limits ===
CRYPTO_MAX_POSITION_PCT = 0.20     # 20% max per crypto bet = ~$7
CRYPTO_CHECK_INTERVAL_SEC = 5      # Check every 5 seconds

# === Momentum Thresholds ===
# These determine when Binance data shows a real move vs noise.
# 0.10% in 60s on BTC = ~$90 move = meaningful for 5-min markets.
MOMENTUM_THRESHOLD_STRONG = 0.08   # 0.08% in 60s = strong
MOMENTUM_THRESHOLD_MEDIUM = 0.04   # 0.04% in 60s = medium (catches off-peak moves)
MOMENTUM_WINDOW_SECONDS = 60
VOLUME_SPIKE_THRESHOLD = 2.0       # 2x avg volume confirms move

# === Order Book / Whale Detection ===
ORDERBOOK_IMBALANCE_THRESHOLD = 0.60
LARGE_TRADE_MULTIPLIER = 5
WHALE_NET_THRESHOLD = 2
FUNDING_EXTREME_THRESHOLD = 0.0005

# === Daily Loss Limit ===
# Math: with $35, max daily loss = $7 (20% of bankroll).
# Survive bad days so you can trade tomorrow.
DAILY_LOSS_PER_10 = 2.0            # $2 per $10 = $7/day max loss
DAILY_LOSS_LIMIT_PCT = 0.20        # 20% daily loss = stop

# === Rolling Win Rate ===
WIN_RATE_WINDOW = 20
WIN_RATE_THRESHOLD = 0.45          # Halve size below 45% win rate
MIN_TRADES_FOR_SIGNAL = 5          # Need 5 trades before adjusting

# === Correlation Filter ===
CORRELATION_THRESHOLD = 0.60

# === Long-Shot Bias ===
LONGSHOT_LOW = 0.05
LONGSHOT_HIGH = 0.20
LONGSHOT_CORRECTION = 0.0          # Disabled for crypto

# === Claude AI ===
# Claude is the GATE — every trade must be approved.
# Using Sonnet for speed + intelligence.
CLAUDE_MODEL = "claude-sonnet-4-6"
MAX_CLAUDE_CALLS = 20
CLAUDE_MAX_TOKENS = 300

# === Order Execution ===
# Math: 1 cent improvement on a $0.50 market = 2% edge cost.
# But with 0 improvement, orders DON'T FILL (sitting at 0/8).
# Need 2-3 cents above midpoint to cross the spread and get filled.
PRICE_IMPROVEMENT = 0.03           # 3 cents — pay the spread to actually get filled

# === Database ===
DB_PATH = "polybot.db"
