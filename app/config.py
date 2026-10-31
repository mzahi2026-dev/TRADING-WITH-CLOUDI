from pathlib import Path
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# ---- Binance USDS-M Futures ----
USE_DEMO = os.getenv("USE_DEMO", "true").lower() in ("true", "1", "yes")

# In DEMO mode (default for demo trading), use demo endpoints to prevent shared cloud IP 418 rate-limits
FAPI_BASE = os.getenv(
    "BINANCE_FAPI_BASE",
    "https://demo-fapi.binance.com" if USE_DEMO else "https://fapi.binance.com",
)
WS_BASE = os.getenv(
    "BINANCE_WS_BASE",
    "wss://demo-fstream.binance.com" if USE_DEMO else "wss://fstream.binance.com",
)
API_KEY = os.getenv("BINANCE_API_KEY", "") or os.getenv("DEMO_API_KEY", "")  # authenticate requests

# ---- Timeframes ----
INTERVALS = ["15m", "1h", "4h", "1d"]
INTERVAL_MS = {
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}
HISTORY_LIMIT = 1500

# ---- Symbol filter (mandatory) ----
MIN_QUOTE_VOLUME_24H = 10_000_000
MIN_LISTING_AGE_DAYS = 30
TOP_N = 150
BLACKLIST = []
WHITELIST = []
SYMBOL_REFRESH_HOURS = 6

# ---- Runtime ----
WS_SYMBOLS_PER_CONN = 25
REQUEST_TIMEOUT = 15
REQUEST_SLEEP = 0.3
WEIGHT_SOFT_LIMIT = 1800

# ---- Step 1: Daily scan ----
SCAN_HOUR_BDT = 6          # 06:00 BDT = 00:00 UTC (1d candle close)
SCAN_MINUTE_BDT = 2
WATCHLIST_MAX = 100
WATCHLIST_MIN_SCORE = 8
MIN_1D_CANDLES = 25

# ---- Step 2: Hourly ranking ----
RANK_MINUTE_BDT = 12       # proti ghonta :12 e (hourly data job sesh hoyar por)
RANK_TOP_N = 20
MIN_1H_CANDLES = 60

# ---- Paths ----
DB_PATH = BASE_DIR / "data_store" / "market.db"
LOG_DIR = BASE_DIR / "logs"