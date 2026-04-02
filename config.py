"""Configuration constants for the Polymarket trading bot."""

import os
from dotenv import load_dotenv

load_dotenv()

# === Secrets (from .env) ===
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# === Polymarket ===
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
CHAIN_ID = 137  # Polygon

# === Trading Cycle ===
CYCLE_INTERVAL_SEC = 10            # Faster scanning — arb opportunities disappear quickly
MAX_MARKETS_PER_CYCLE = 50         # Keep scanning lots

# === Market Filters ===
MIN_VOLUME = 5                     # Keep low for new markets
MIN_LIQUIDITY = 50                 # Lower — we check orderbook depth ourselves

# === Arbitrage Settings ===
MIN_ARB_PROFIT = 0.02              # Minimum 2 cents profit per share (2%)
ARB_MAX_POSITION_PCT = 0.40        # Up to 40% of bankroll per arb (low risk since hedged)
MAX_DAYS_TO_RESOLUTION = 1         # Only markets resolving within 1 day (5-min, 15-min, 1-hour)

# === Sports Filter — skip these categories ===
SPORTS_KEYWORDS = {
    # Traditional sports
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
    # Esports
    "counter-strike", "csgo", "cs2", "dota", "dota2", "dota 2",
    "league of legends", "lol", "valorant", "overwatch",
    "esports", "e-sports", "bo1", "bo3", "bo5",
    "fnatic", "navi", "g2", "faze", "cloud9", "team liquid",
    "major tournament", "esl", "blast", "iem",
    # Soccer/Football
    "marseille", "psg", "barcelona", "real madrid", "manchester",
    "liverpool", "arsenal", "chelsea", "tottenham", "juventus",
    "bayern", "inter milan", "ac milan", "atletico",
    # Entertainment / Pop Culture / Music / TV — NO GUESSING
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
    # Random guessing markets — no data edge
    "coin flip", "dice roll", "random", "lottery", "powerball",
    "mega millions", "roulette", "casino",
    "weather record", "hottest day", "coldest day",
    "first tweet", "first post", "most liked",
    "guinness", "world record", "eating contest",
    "eurovision",
}

# === Preferred categories — prioritize these ===
PREFERRED_KEYWORDS = {
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto",
    "xrp", "dogecoin", "doge", "cardano", "ada", "polygon", "matic",
    "defi", "nft", "blockchain", "token", "coin", "altcoin",
    "binance", "coinbase", "sec crypto", "etf",
    "up or down", "5 minutes", "15 minutes", "1 day",
    "fed", "interest rate", "inflation", "gdp", "tariff", "trade war",
    "trump", "biden", "congress", "senate", "election", "geopolitical",
    "iran", "russia", "china", "ukraine", "nato", "ceasefire",
    "climate", "hurricane", "earthquake", "wildfire", "temperature",
}

# === Edge / EV Thresholds ===
MIN_EDGE = 0.08                    # 8% minimum edge — only trade when Polymarket is clearly mispriced
MIN_EDGE_CRYPTO = 0.08             # Same — need real edge, not coin flips
MIN_EV_PER_DOLLAR = 0.02           # $0.02 minimum EV per dollar

# === Position Sizing ===
KELLY_FRACTION = 0.25              # Quarter-Kelly
MAX_POSITION_PCT = 0.20            # 20% max per trade — we're high confidence
MAX_OPEN_POSITIONS = 10            # More positions OK since they're hedged
MIN_ORDER_SIZE_USD = 1.0

# === Risk Management (Chan Drawdown) ===
DD_THRESHOLD_HALF = 0.20           # Halve size at 20% drawdown
DD_THRESHOLD_STOP = 0.30           # Stop trading at 30% drawdown

# === Stop Loss / Take Profit ===
STOP_LOSS_PCT = 0.0615             # 6.15% stop loss (user requirement)
TAKE_PROFIT_PCT = 0.10             # 10% take profit for all crypto
TAKE_PROFIT_CRYPTO_PCT = 0.10      # Same
TAKE_PROFIT_EDGE_MIN = 0.01        # Exit if edge drops below 1%

# === Crypto Position Limits ===
CRYPTO_MAX_POSITION_PCT = 0.15     # 15% of bankroll per crypto bet — conservative until proven
CRYPTO_CHECK_INTERVAL_SEC = 5      # Check positions every 5 seconds — speed matters

# === Real-Time Edge Detection Thresholds ===
MOMENTUM_THRESHOLD_STRONG = 0.15   # 0.15% move in 60 sec = strong signal
MOMENTUM_THRESHOLD_MEDIUM = 0.10   # 0.10% move in 60 sec = medium signal
MOMENTUM_WINDOW_SECONDS = 60       # Look at last 60 seconds of price action
VOLUME_SPIKE_THRESHOLD = 2.0       # Volume must be 2x average to confirm move

# === Order Book / Whale Detection Thresholds ===
ORDERBOOK_IMBALANCE_THRESHOLD = 0.60  # 60% bid ratio = bullish imbalance
LARGE_TRADE_MULTIPLIER = 5            # Trade > 5x median = "large"
WHALE_NET_THRESHOLD = 2               # Net 2+ large buys = whale signal
FUNDING_EXTREME_THRESHOLD = 0.0005    # 0.05% funding rate = extreme

# === Daily Loss Limit ===
DAILY_LOSS_PER_10 = 2.0            # Max $2 loss per $10 bankroll

# === Rolling Win Rate (Simons) ===
WIN_RATE_WINDOW = 30               # Last 30 trades (more data since we trade often)
WIN_RATE_THRESHOLD = 0.55          # Halve size below 55%
MIN_TRADES_FOR_SIGNAL = 10         # Need 10 trades before adjusting

# === Correlation Filter (Simons) ===
CORRELATION_THRESHOLD = 0.60       # Skip if keyword overlap > 60%

# === Long-Shot Bias (Taleb) ===
LONGSHOT_LOW = 0.05                # Apply correction above this price
LONGSHOT_HIGH = 0.20               # Apply correction below this price
LONGSHOT_CORRECTION = 0.0          # DISABLED — not relevant for crypto

# === Claude AI ===
MAX_CLAUDE_CALLS = 20              # Claude confirms each trade — fast reasoning on momentum data
CLAUDE_MODEL = "claude-sonnet-4-6" # Fast + smart
CLAUDE_MAX_TOKENS = 300            # Short responses only — yes/no + reasoning

# === Order Execution ===
PRICE_IMPROVEMENT = 0.01           # 1 cent — speed matters but preserve edge

# === Database ===
DB_PATH = "polybot.db"
