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
CYCLE_INTERVAL_SEC = 60           # 1 minute — fast for 5-min crypto markets
MAX_MARKETS_PER_CYCLE = 50        # Scan lots of markets per cycle

# === Market Filters ===
MIN_VOLUME = 100                  # Low minimum to catch 5-min crypto markets
MIN_LIQUIDITY = 100               # Low minimum for short-term markets
MAX_DAYS_TO_RESOLUTION = 60       # Markets resolving within 60 days

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
MIN_EDGE = 0.03                   # 3% minimum edge to consider
MIN_EV_PER_DOLLAR = 0.01          # $0.01 minimum EV per dollar risked

# === Position Sizing ===
KELLY_FRACTION = 0.40             # 40% Kelly — more aggressive
MAX_POSITION_PCT = 0.20           # Max 20% of bankroll per trade
MAX_OPEN_POSITIONS = 5            # Max 5 open orders at a time
MIN_ORDER_SIZE_USD = 1.0          # Minimum order to place

# === Risk Management (Chan Drawdown) ===
DD_THRESHOLD_HALF = 0.35          # Halve size at 35% drawdown
DD_THRESHOLD_STOP = 0.50          # Stop trading at 50% drawdown

# === Stop Loss / Take Profit ===
STOP_LOSS_PCT = 0.20              # Exit if position down 20%
TAKE_PROFIT_PCT = 0.15            # Exit if position up 15%
TAKE_PROFIT_CRYPTO_PCT = 0.05     # Exit crypto 5-min markets at 5% profit
TAKE_PROFIT_EDGE_MIN = 0.02       # Exit if edge drops below 2%

# === Crypto Short-Term Market Limits ===
CRYPTO_MAX_POSITION_PCT = 0.20    # Max 20% of bankroll on a single 5-min crypto bet
CRYPTO_CHECK_INTERVAL_SEC = 15    # Check crypto positions every 15 seconds

# === Daily Loss Limit ===
DAILY_LOSS_PER_10 = 5.0           # Max $5 loss per $10 bankroll — loose for small bankroll

# === Rolling Win Rate (Simons) ===
WIN_RATE_WINDOW = 20              # Last N trades
WIN_RATE_THRESHOLD = 0.45         # Halve size below this
MIN_TRADES_FOR_SIGNAL = 5         # Need at least this many trades

# === Correlation Filter (Simons) ===
CORRELATION_THRESHOLD = 0.60      # Skip if keyword overlap > 60%

# === Long-Shot Bias (Taleb) ===
LONGSHOT_LOW = 0.05               # Apply correction above this price
LONGSHOT_HIGH = 0.20              # Apply correction below this price
LONGSHOT_CORRECTION = 0.08        # +8% edge correction

# === Claude AI ===
CLAUDE_MODEL = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS = 500

# === Order Execution ===
PRICE_IMPROVEMENT = 0.03          # 3 cents better than midpoint — fills faster

# === Database ===
DB_PATH = "polybot.db"
