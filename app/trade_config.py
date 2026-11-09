DEMO_BASE = "https://demo-fapi.binance.com"   # shudhu demo, real account e order jabe na

# Risk & Position Sizing
TARGET_RISK_USDT = 15.0       # proti trade e target risk $15 USDT (0.30% of $5,000 capital)
MAX_NOTIONAL_USDT = 800.0     # max position notional size cap (5x leverage = max $160 margin)
NOTIONAL_USDT = 100.0         # fallback notional size
LEVERAGE = 5
MAX_OPEN = 5
SL_ATR = 1.5
TP1_ATR = 2.0                 # Phase 4 Scaled TP1: 50% partial exit at 2.0 * ATR
TP_ATR = 3.5                  # Secondary / Runner TP target

# Risk Limits
MAX_DAILY_LOSS_USDT = 150.0   # 24h net loss exceeded -> stop new trades (circuit breaker)
MIN_AVAILABLE_BALANCE = 50.0  # minimum available balance required to open new trade

# Liquidity & Volume Gating (Raised to $30M to prune noisy, illiquid micro-caps like AIOT, NXPC)
MIN_VOL_24H = 30_000_000      # Unified $30M 24h volume threshold across scanner and trader
MAX_SPREAD_PCT = 0.05         # order book spread %
MAX_FUNDING = 0.001           # 0.1% er beshi funding hole skip
MAX_CHASE_ATR = 0.5           # signal close theke price 0.5 ATR er beshi soreche hole skip

# Cooldown Controls
COOLDOWN_HOURS = 2            # Standard re-entry cooldown
LOSS_COOLDOWN_HOURS = 12      # 12-hour blackout on any symbol that closed with SL or loss
RANK_MAX_AGE_MIN = 100        # ranking er boyos er limit
CYCLE_SECOND = 25             # proti 15m candle close er 25 sec por

# Institutional Trend Filter
ADX_MIN_THRESHOLD = 25.0      # Minimum 1H ADX for confirmed trend strength

# ---- Workflow 2: Gemini AI Autonomous Engine ----
AI_ENABLED = True
MAX_AI_OPEN = 3                   # at a time max 3 open trades
AI_REENTRY_COOLDOWN_HOURS = 2     # standard re-entry minimum 2h gap
AI_LOSS_COOLDOWN_HOURS = 12       # 12-hour blackout for AI after SL hit
AI_NOTIONAL_USDT = 200.0          # max invest 200 USDT per trade
AI_SL_USDT = 15.0                 # fixed SL $15
AI_TP1_ATR = 2.0                  # Scaled TP1 at 2.0 * ATR
AI_TP2_ATR = 3.5                  # Secondary TP at 3.5 * ATR
AI_TP_USDT = 30.0                 # fixed TP $30 (1:2 R:R)
AI_MIN_CONFIDENCE = 70            # minimum confidence to take trade (1-100)
AI_MODEL = "gemini-3.5-flash"     # Gemini active model (High quota, stable JSON)
