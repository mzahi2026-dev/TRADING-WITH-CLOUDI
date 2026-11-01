DEMO_BASE = "https://demo-fapi.binance.com"   # shudhu demo, real account e order jabe na

NOTIONAL_USDT = 100.0       # proti trade er size
LEVERAGE = 5
MAX_OPEN = 5
SL_ATR = 0.8
TP_ATR = 1.6

# Risk Limits
MAX_DAILY_LOSS_USDT = 150.0    # 24h net loss exceeded -> stop new trades (circuit breaker)
MIN_AVAILABLE_BALANCE = 50.0   # minimum available balance required to open new trade

MIN_VOL_24H = 15_000_000        # Unified $15M 24h volume threshold across scanner and trader
MAX_SPREAD_PCT = 0.05       # order book spread %
MAX_FUNDING = 0.001         # 0.1% er beshi funding hole skip
MAX_CHASE_ATR = 0.5         # signal close theke price 0.5 ATR er beshi soreche hole skip
COOLDOWN_HOURS = 2
RANK_MAX_AGE_MIN = 100      # ranking er boyos er limit
CYCLE_SECOND = 25           # proti 15m candle close er 25 sec por