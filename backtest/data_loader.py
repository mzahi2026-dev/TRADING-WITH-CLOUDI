"""
Data loader and indicator calculator for Velotrade Multi-Timeframe Strategy.
Strictly loads 365-day CSVs from backtest/data/ and synchronizes 1H and 15m timeframes
without lookahead bias.
"""

import os
import pandas as pd
import numpy as np


DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def compute_ema(series: pd.Series, period: int) -> pd.Series:
    """Calculate Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Calculate Relative Strength Index."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-12)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Average True Range."""
    high = df["high"]
    low = df["low"]
    close_prev = df["close"].shift(1)
    tr1 = high - low
    tr2 = (high - close_prev).abs()
    tr3 = (low - close_prev).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    return atr


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Calculate Welles Wilder Average Directional Index (ADX).
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]
    close_prev = close.shift(1)

    tr1 = high - low
    tr2 = (high - close_prev).abs()
    tr3 = (low - close_prev).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr_smooth = pd.Series(tr, index=df.index).ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    plus_dm_smooth = pd.Series(plus_dm, index=df.index).ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    minus_dm_smooth = pd.Series(minus_dm, index=df.index).ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    plus_di = 100.0 * (plus_dm_smooth / (tr_smooth + 1e-12))
    minus_di = 100.0 * (minus_dm_smooth / (tr_smooth + 1e-12))

    dx = 100.0 * ((plus_di - minus_di).abs() / (plus_di + minus_di + 1e-12))
    adx = dx.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    return adx


def load_raw_csv(symbol: str, timeframe: str) -> pd.DataFrame:
    """
    Load raw CSV data for a symbol and timeframe from backtest/data/.
    """
    csv_path = os.path.join(DATA_DIR, symbol, timeframe, "ohlcv_365d.csv")
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Historical data file not found: {csv_path}")

    df = pd.read_csv(csv_path)
    required_cols = ["timestamp", "open", "high", "low", "close", "volume", "turnover"]
    for col in required_cols:
        if col not in df.columns:
            raise ValueError(f"Missing column '{col}' in {csv_path}")

    df = df.sort_values("timestamp").drop_duplicates(subset=["timestamp"]).reset_index(drop=True)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


def prepare_1h_features(df_1h: pd.DataFrame, prefix: str = "") -> pd.DataFrame:
    """
    Compute 1H macro trend indicators and swing levels.
    """
    df = df_1h.copy()
    c = df["close"]

    df[f"{prefix}ema20"] = compute_ema(c, 20)
    df[f"{prefix}ema50"] = compute_ema(c, 50)
    df[f"{prefix}rsi14"] = compute_rsi(c, 14)
    df[f"{prefix}atr14"] = compute_atr(df, 14)
    df[f"{prefix}adx14"] = compute_adx(df, 14)

    # 1H Swing Highs & Lows (rolling 24-hour window)
    df[f"{prefix}swing_high_24"] = df["high"].rolling(24).max()
    df[f"{prefix}swing_low_24"] = df["low"].rolling(24).min()

    # 1H Trend regime: 1 = Uptrend (EMA20 > EMA50), -1 = Downtrend (EMA20 < EMA50)
    df[f"{prefix}trend_dir"] = np.where(
        df[f"{prefix}ema20"] > df[f"{prefix}ema50"], 1,
        np.where(df[f"{prefix}ema20"] < df[f"{prefix}ema50"], -1, 0)
    )

    # 1H bar close time: A 1H bar starting at timestamp T completes at T + 3600*1000
    df["available_at"] = df["timestamp"] + 3600 * 1000

    return df


def prepare_15m_features(df_15m: pd.DataFrame) -> pd.DataFrame:
    """
    Compute 15m lower timeframe micro entry indicators and structure levels.
    """
    df = df_15m.copy()
    c = df["close"]
    v = df["volume"]

    df["ema20"] = compute_ema(c, 20)
    df["ema50"] = compute_ema(c, 50)
    df["atr14"] = compute_atr(df, 14)
    df["vol_sma20"] = v.rolling(20).mean()

    # Swing high/low for breakout patterns (prior to current candle to avoid lookahead)
    df["swing_high_20"] = df["high"].shift(1).rolling(20).max()
    df["swing_low_20"] = df["low"].shift(1).rolling(20).min()

    # 40-candle extreme for liquidity sweep (prior to current candle)
    df["extreme_high_40"] = df["high"].shift(1).rolling(40).max()
    df["extreme_low_40"] = df["low"].shift(1).rolling(40).min()

    # Recent 3-bar swing pivot levels for structural stop loss placement
    df["recent_swing_low_3"] = df["low"].shift(1).rolling(3).min()
    df["recent_swing_high_3"] = df["high"].shift(1).rolling(3).max()

    return df


def load_and_sync(symbol: str) -> pd.DataFrame:
    """
    Synchronize 15m data with 1H macro features and BTCUSDT 1H macro filter.
    Guarantees strict zero lookahead bias by matching 15m bar timestamp with
    completed 1H bar via available_at (open time + 1 hour).
    """
    df_15m_raw = load_raw_csv(symbol, "15m")
    df_1h_raw = load_raw_csv(symbol, "1h")
    btc_1h_raw = load_raw_csv("BTCUSDT", "1h")

    # Prepare features
    df_15m = prepare_15m_features(df_15m_raw)
    df_1h = prepare_1h_features(df_1h_raw, prefix="h1_")
    btc_1h = prepare_1h_features(btc_1h_raw, prefix="btc_h1_")

    # Bitcoin Macro Filter Columns
    # Confirmed downtrend: BTC Price < 1H EMA50 OR BTC 1H RSI < 45
    btc_1h["btc_downtrend"] = (
        (btc_1h["close"] < btc_1h["btc_h1_ema50"]) |
        (btc_1h["btc_h1_rsi14"] < 45.0)
    ).astype(int)

    # Keep only relevant 1H columns for merging
    h1_cols = [
        "available_at",
        "h1_ema20",
        "h1_ema50",
        "h1_rsi14",
        "h1_atr14",
        "h1_adx14",
        "h1_swing_high_24",
        "h1_swing_low_24",
        "h1_trend_dir",
        "close",
    ]
    df_1h_sub = df_1h[h1_cols].rename(columns={"close": "h1_close"}).sort_values("available_at")

    btc_cols = [
        "available_at",
        "btc_h1_ema20",
        "btc_h1_ema50",
        "btc_h1_rsi14",
        "btc_h1_trend_dir",
        "btc_downtrend",
    ]
    btc_1h_sub = btc_1h[btc_cols].sort_values("available_at")

    # Merge 1H symbol features onto 15m without lookahead:
    # 15m bar at timestamp T can only see 1H bars completed at or before T (available_at <= T)
    merged = pd.merge_asof(
        df_15m.sort_values("timestamp"),
        df_1h_sub,
        left_on="timestamp",
        right_on="available_at",
        direction="backward",
    )

    # Merge BTC 1H macro features onto 15m
    merged = pd.merge_asof(
        merged,
        btc_1h_sub,
        left_on="timestamp",
        right_on="available_at",
        direction="backward",
        suffixes=("", "_btc"),
    )

    # Clean up auxiliary columns
    cols_to_drop = [c for c in merged.columns if "available_at" in c]
    merged = merged.drop(columns=cols_to_drop)

    # Drop early rows before all rolling windows & indicators initialize
    merged = merged.dropna().reset_index(drop=True)
    merged["symbol"] = symbol

    return merged
