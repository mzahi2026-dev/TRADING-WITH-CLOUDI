"""
Velotrade Multi-Timeframe (1H Macro Trend + 15m Micro Entry) Price Action Strategy.

Higher Timeframe Regime Filter (1H):
- Trend Alignment: 1H EMA20 > EMA50 for LONG; 1H EMA20 < EMA50 for SHORT.
- Trend Strength Filter: 1H ADX(14) >= 25.0 (Phase 3 Institutional Trend Filter).
- Bitcoin Macro Filter: BTCUSDT 1H in confirmed downtrend (Price < 1H EMA50 or RSI < 45) blocks Altcoin LONG setups.
- Market Structure: Key 1H swing highs/lows for support and resistance proximity guards.

Lower Timeframe Entry Triggers (15m):
- Pattern 1 (EMA Pullback Bounce): Pulls back into 15m EMA20/EMA50 zone with strong rejection wick or engulfing close on >= 1.5x volume.
- Pattern 2 (Breakout & Retest): Clean break of 20-candle horizontal swing high/low with >= 1.5x volume, followed by successful retest holding level.
- Pattern 3 (Liquidity Sweep & Reclaim): 15m wick sweeps 40-candle extreme and aggressively closes back inside range on >= 1.5x volume.

Risk & Position Sizing:
- Structural Stop Loss: Recent 3-candle swing pivot high/low with 0.1 * ATR buffer (guardrails: 1.5% min, 7.5% max).
- Scaled Take Profit: TP1 = 1.5 * ATR (50% position), runner managed dynamically with 15m EMA20 trailing stop.
"""

from dataclasses import dataclass
from typing import Optional, Dict, Any
import numpy as np
import pandas as pd


@dataclass
class StrategyConfig:
    use_btc_filter: bool = True
    use_adx_filter: bool = True
    adx_threshold: float = 25.0               # Phase 3: Raised to 25.0 for confirmed institutional trends
    min_volume_expansion: float = 1.5         # Phase 3: Raised to 1.5x volume expansion across all triggers
    use_structural_sl: bool = True
    structural_buffer_atr_mult: float = 0.1
    min_wick_ratio: float = 0.35
    sr_proximity_buffer: float = 0.0025       # 0.25% buffer against 1H S/R
    atr_period: int = 14
    sl_atr_mult: float = 1.5
    tp1_atr_mult: float = 2.0                 # Phase 4: Upgraded to 2.0 * ATR (50% close)
    tp2_atr_mult: float = 3.0                 # Phase 3: Secondary target or uncapped EMA20 trailing
    min_sl_pct: float = 0.015                 # 1.5% min guardrail
    max_sl_pct: float = 0.075                 # 7.5% max guardrail
    use_1h_rsi_filter: bool = True
    rsi_long_min: float = 45.0
    rsi_short_max: float = 55.0


class VelotradeStrategy:
    """
    Modular implementation of Velotrade 1H+15m Price Action Strategy with Phase 3 enhancements.
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or StrategyConfig()

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Generate entry signals, patterns, stop loss, and take profit distances.
        Output columns:
        - signal: 1 (LONG), -1 (SHORT), 0 (HOLD)
        - pattern: 'P1_PULLBACK', 'P2_BREAKOUT_RETEST', 'P3_SWEEP_RECLAIM', or ''
        - sl_dist: Dynamic/structural stop loss distance in quote currency
        - tp1_dist: Scaled take profit 1 distance in quote currency (1.5 * ATR)
        - tp2_dist: Scaled take profit 2 distance in quote currency (3.0 * ATR)
        - tp_dist: Alias for tp2_dist (backwards compatibility)
        """
        df = df.copy()
        n = len(df)

        signals = np.zeros(n, dtype=int)
        patterns = [""] * n
        sl_distances = np.zeros(n, dtype=float)
        tp1_distances = np.zeros(n, dtype=float)
        tp2_distances = np.zeros(n, dtype=float)

        opens = df["open"].values
        highs = df["high"].values
        lows = df["low"].values
        closes = df["close"].values
        volumes = df["volume"].values
        vol_sma20 = df["vol_sma20"].values
        atr14 = df["atr14"].values
        ema20 = df["ema20"].values
        ema50 = df["ema50"].values

        h1_ema20 = df["h1_ema20"].values
        h1_ema50 = df["h1_ema50"].values
        h1_rsi14 = df["h1_rsi14"].values
        h1_adx14 = df["h1_adx14"].values if "h1_adx14" in df.columns else np.full(n, 25.0)
        h1_trend_dir = df["h1_trend_dir"].values
        h1_swing_high_24 = df["h1_swing_high_24"].values
        h1_swing_low_24 = df["h1_swing_low_24"].values
        btc_downtrend = df["btc_downtrend"].values

        swing_high_20 = df["swing_high_20"].values
        swing_low_20 = df["swing_low_20"].values
        extreme_high_40 = df["extreme_high_40"].values
        extreme_low_40 = df["extreme_low_40"].values

        recent_swing_low_3 = df["recent_swing_low_3"].values if "recent_swing_low_3" in df.columns else lows
        recent_swing_high_3 = df["recent_swing_high_3"].values if "recent_swing_high_3" in df.columns else highs

        symbol = df["symbol"].iloc[0] if "symbol" in df.columns else "UNKNOWN"
        is_btc = symbol == "BTCUSDT"

        cfg = self.config

        for i in range(50, n):
            c = closes[i]
            o = opens[i]
            h = highs[i]
            l = lows[i]
            v = volumes[i]
            v_sma = vol_sma20[i]
            rng = h - l

            if rng <= 1e-8 or v_sma <= 0:
                continue

            c_prev = closes[i - 1]
            o_prev = opens[i - 1]

            lower_wick = min(o, c) - l
            upper_wick = h - max(o, c)
            lw_ratio = lower_wick / rng
            uw_ratio = upper_wick / rng
            vol_mult = v / v_sma

            # 1. Higher Timeframe Regime Filter (1H)
            h1_trend = h1_trend_dir[i]
            btc_downtrend_active = btc_downtrend[i] == 1

            # 1H Trend Strength Filter (ADX >= 25.0)
            if cfg.use_adx_filter and h1_adx14[i] < cfg.adx_threshold:
                continue

            # 15m Institutional Volume Filter (Volume >= 1.5x SMA20)
            if vol_mult < cfg.min_volume_expansion:
                continue

            # Bitcoin Macro Filter:
            # If Altcoin and BTC 1H is confirmed downtrend -> STRICTLY DISALLOW Altcoin LONG setups
            btc_blocks_long = False
            if cfg.use_btc_filter:
                if not is_btc and btc_downtrend_active:
                    btc_blocks_long = True
                elif is_btc and btc_downtrend_active:
                    btc_blocks_long = True

            # Market Structure: Key 1H S/R proximity guards
            dist_to_res = (h1_swing_high_24[i] - c) / c if h1_swing_high_24[i] > c else 1.0
            dist_to_sup = (c - h1_swing_low_24[i]) / c if c > h1_swing_low_24[i] else 1.0

            sig = 0
            pat = ""

            # --- LONG SETUP EVALUATION ---
            if h1_trend == 1 and not btc_blocks_long and dist_to_res > cfg.sr_proximity_buffer:
                rsi_ok = True
                if cfg.use_1h_rsi_filter:
                    rsi_ok = h1_rsi14[i] >= cfg.rsi_long_min

                if rsi_ok:
                    # Pattern 1: EMA Pullback Bounce
                    p1_pullback = (min(l, lows[i - 1]) <= max(ema20[i], ema50[i])) and (c >= min(ema20[i], ema50[i]))
                    p1_wick_bounce = (lw_ratio >= cfg.min_wick_ratio) and (c > o)
                    p1_engulfing = (c > o) and (c > o_prev) and (o < c_prev)
                    if p1_pullback and (p1_wick_bounce or p1_engulfing) and (c > ema20[i]) and (ema20[i] >= ema50[i]):
                        sig = 1
                        pat = "P1_PULLBACK"

                    # Pattern 2: Breakout & Retest
                    if sig == 0:
                        for lookback in range(1, 5):
                            bidx = i - lookback
                            if bidx < 20:
                                break
                            sh = swing_high_20[bidx]
                            if closes[bidx] > sh and volumes[bidx] >= cfg.min_volume_expansion * vol_sma20[bidx]:
                                if l <= sh * 1.002 and c >= sh and (c >= o or lw_ratio >= 0.30):
                                    sig = 1
                                    pat = "P2_BREAKOUT_RETEST"
                                    break

                    # Pattern 3: Liquidity Sweep & Reclaim
                    if sig == 0:
                        ext_low = extreme_low_40[i]
                        if l < ext_low and c > ext_low:
                            if lw_ratio >= cfg.min_wick_ratio or c > o:
                                sig = 1
                                pat = "P3_SWEEP_RECLAIM"

            # --- SHORT SETUP EVALUATION ---
            elif h1_trend == -1 and dist_to_sup > cfg.sr_proximity_buffer:
                rsi_ok = True
                if cfg.use_1h_rsi_filter:
                    rsi_ok = h1_rsi14[i] <= cfg.rsi_short_max

                if rsi_ok:
                    # Pattern 1: EMA Pullback Bounce
                    p1_pullback = (max(h, highs[i - 1]) >= min(ema20[i], ema50[i])) and (c <= max(ema20[i], ema50[i]))
                    p1_wick_bounce = (uw_ratio >= cfg.min_wick_ratio) and (c < o)
                    p1_engulfing = (c < o) and (c < o_prev) and (o > c_prev)
                    if p1_pullback and (p1_wick_bounce or p1_engulfing) and (c < ema20[i]) and (ema20[i] <= ema50[i]):
                        sig = -1
                        pat = "P1_PULLBACK"

                    # Pattern 2: Breakout & Retest
                    if sig == 0:
                        for lookback in range(1, 5):
                            bidx = i - lookback
                            if bidx < 20:
                                break
                            sl_lvl = swing_low_20[bidx]
                            if closes[bidx] < sl_lvl and volumes[bidx] >= cfg.min_volume_expansion * vol_sma20[bidx]:
                                if h >= sl_lvl * 0.998 and c <= sl_lvl and (c <= o or uw_ratio >= 0.30):
                                    sig = -1
                                    pat = "P2_BREAKOUT_RETEST"
                                    break

                    # Pattern 3: Liquidity Sweep & Reclaim
                    if sig == 0:
                        ext_high = extreme_high_40[i]
                        if h > ext_high and c < ext_high:
                            if uw_ratio >= cfg.min_wick_ratio or c < o:
                                sig = -1
                                pat = "P3_SWEEP_RECLAIM"

            if sig != 0:
                if cfg.use_structural_sl:
                    if sig == 1:
                        pivot_low = min(l, recent_swing_low_3[i])
                        raw_sl = (c - pivot_low) + cfg.structural_buffer_atr_mult * atr14[i]
                    else:
                        pivot_high = max(h, recent_swing_high_3[i])
                        raw_sl = (pivot_high - c) + cfg.structural_buffer_atr_mult * atr14[i]
                else:
                    raw_sl = cfg.sl_atr_mult * atr14[i]

                min_sl_dist = cfg.min_sl_pct * c
                max_sl_dist = cfg.max_sl_pct * c
                sl_dist = np.clip(raw_sl, min_sl_dist, max_sl_dist)

                tp1_dist = cfg.tp1_atr_mult * atr14[i]
                tp2_dist = cfg.tp2_atr_mult * atr14[i]

                signals[i] = sig
                patterns[i] = pat
                sl_distances[i] = sl_dist
                tp1_distances[i] = tp1_dist
                tp2_distances[i] = tp2_dist

        df["signal"] = signals
        df["pattern"] = patterns
        df["sl_dist"] = sl_distances
        df["tp1_dist"] = tp1_distances
        df["tp2_dist"] = tp2_distances
        df["tp_dist"] = tp2_distances

        return df
