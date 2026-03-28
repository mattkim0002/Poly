"""Configuration constants for the Polymarket trading bot."""

import os
from dotenv import load_dotenv

load_dotenv()

# === Secrets (from .env) ===
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

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

# === Edge / EV Thresholds ===
MIN_EDGE = 0.05                   # 5% minimum edge to consider
MIN_EV_PER_DOLLAR = 0.03          # $0.03 minimum EV per dollar risked

# === Position Sizing ===
KELLY_FRACTION = 0.25             # Quarter-Kelly
MAX_POSITION_PCT = 0.15           # Max 15% of bankroll per trade
MAX_OPEN_POSITIONS = 8            # Portfolio concentration limit
MIN_ORDER_SIZE_USD = 1.0          # Minimum order to place

# === Risk Management (Chan Drawdown) ===
DD_THRESHOLD_HALF = 0.20          # Halve size at 20% drawdown
DD_THRESHOLD_STOP = 0.30          # Stop trading at 30% drawdown

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
PRICE_IMPROVEMENT = 0.01          # 1 cent better than midpoint for limit orders

# === Database ===
DB_PATH = "polybot.db"
