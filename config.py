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
CYCLE_INTERVAL_SEC = 300          # 5 minutes
MAX_MARKETS_PER_CYCLE = 20        # Claude API cost control

# === Market Filters ===
MIN_VOLUME = 1000                 # $1000 minimum market volume
MIN_LIQUIDITY = 500               # $500 minimum liquidity

# === Sports Filter — skip these categories ===
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
    # Esports
    "counter-strike", "csgo", "cs2", "dota", "dota2", "dota 2",
    "league of legends", "lol", "valorant", "overwatch",
    "esports", "e-sports", "bo1", "bo3", "bo5",
    "fnatic", "navi", "g2", "faze", "cloud9", "team liquid",
    "major tournament", "esl", "blast", "iem",
}

# === Edge / EV Thresholds ===
MIN_EDGE = 0.04                   # 4% minimum edge to consider
MIN_EV_PER_DOLLAR = 0.02          # $0.02 minimum EV per dollar risked

# === Position Sizing ===
KELLY_FRACTION = 0.40             # 40% Kelly — more aggressive
MAX_POSITION_PCT = 0.20           # Max 20% of bankroll per trade
MAX_OPEN_POSITIONS = 12           # More concurrent positions
MIN_ORDER_SIZE_USD = 1.0          # Minimum order to place

# === Risk Management (Chan Drawdown) ===
DD_THRESHOLD_HALF = 0.20          # Halve size at 20% drawdown
DD_THRESHOLD_STOP = 0.30          # Stop trading at 30% drawdown

# === Stop Loss / Take Profit ===
STOP_LOSS_PCT = 0.20              # Exit if position down 20%
TAKE_PROFIT_EDGE_MIN = 0.02       # Exit if edge drops below 2%

# === Daily Loss Limit ===
DAILY_LOSS_PER_10 = 2.0           # Max $2 loss per $10 bankroll

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
