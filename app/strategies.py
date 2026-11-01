import numpy as np
import pandas as pd


def prep(df):
    d = df.reset_index(drop=True).copy()
    c = d["close"]
    d["ema20"] = c.ewm(span=20, adjust=False).mean()
    d["ema50"] = c.ewm(span=50, adjust=False).mean()
    pc = c.shift(1)
    tr = pd.concat([d["high"] - d["low"], (d["high"] - pc).abs(),
                    (d["low"] - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.rolling(14).mean()
    d["vma"] = d["volume"].shift(1).rolling(20).mean()
    delta = c.diff()
    up = delta.clip(lower=0)
    dn = -delta.clip(upper=0)
    rs = up.ewm(alpha=1 / 14, adjust=False).mean() / \
        dn.ewm(alpha=1 / 14, adjust=False).mean().replace(0, np.nan)
    d["rsi"] = 100 - 100 / (1 + rs)
    return d


def _view(d, side):
    """SHORT ke mirror kore LONG logic e chalai."""
    if side == "LONG":
        return {"o": d["open"].values, "h": d["high"].values, "l": d["low"].values,
                "c": d["close"].values, "e20": d["ema20"].values, "e50": d["ema50"].values,
                "rsi": d["rsi"].values}
    return {"o": -d["open"].values, "h": -d["low"].values, "l": -d["high"].values,
            "c": -d["close"].values, "e20": -d["ema20"].values, "e50": -d["ema50"].values,
            "rsi": 100 - d["rsi"].values}


def s_breakout(v, atr, volr, tb):
    """Pure Breakout & Retest Strategy:
    1. Retest of previously broken horizontal resistance/support level.
    2. Direct strong high-volume breakout of 20-candle high.
    """
    i = len(v["c"]) - 1
    c, o, h, l = v["c"][i], v["o"][i], v["h"][i], v["l"][i]
    if not (v["e20"][i] > v["e50"][i]):
        return None

    # Setup A: Breakout Retest (Previous resistance broken and tested as support)
    lvl = v["h"][i - 30:i - 3].max()
    prior_break = (v["c"][i - 6:i] > lvl).any()
    retest_touch = l <= lvl + 0.3 * atr
    retest_hold = c >= lvl - 0.15 * atr
    if prior_break and retest_touch and retest_hold and (c >= o or (c - l) >= 0.4 * max(h - l, 1e-9)):
        return "breakout_retest", f"Retest of {abs(lvl):.6g}, vol x{volr:.1f}"

    # Setup B: Direct Strong Breakout above 20-candle high with volume expansion
    level = v["h"][i - 20:i].max()
    if c > level and c > o and volr >= 1.2 and (c - level) <= 2.0 * atr:
        return "breakout_retest", f"Breakout above {abs(level):.6g}, vol x{volr:.1f}"

    return None


def s_pullback(v, atr, volr, tb):
    """Pure EMA Pullback Strategy:
    1. Trend confirmation: EMA20 > EMA50.
    2. Pullback: Price dips into/near EMA20 within last 3 candles.
    3. Bounce: Current candle holds EMA20 and confirms rejection.
    4. Healthy RSI trend zone (35-70).
    """
    i = len(v["c"]) - 1
    c, o, h, l = v["c"], v["o"], v["h"], v["l"]
    e20, e50 = v["e20"], v["e50"]

    # 1. Trend alignment
    if not (e20[i] > e50[i]):
        return None

    # 2. Pullback touch: in last 3 candles, price touched/tested EMA20
    touched_ema = (l[i - 2:i + 1] <= e20[i - 2:i + 1] + 0.25 * atr).any()
    if not touched_ema:
        return None

    # 3. Bounce: current candle holds above EMA20 and shows bullish rejection
    bounced = c[i] >= e20[i] - 0.1 * atr
    bullish_rejection = (c[i] > o[i]) or ((c[i] - l[i]) >= 0.45 * max(h[i] - l[i], 1e-9))
    if not (bounced and bullish_rejection):
        return None

    # 4. Healthy RSI (not overextended)
    if not (35 <= v["rsi"][i] <= 70):
        return None

    return "ema_pullback", f"EMA20 pullback bounce, RSI {v['rsi'][i]:.0f}"


def s_sweep(v, atr, volr, tb):
    """Liquidity Sweep & Reclaim."""
    i = len(v["c"]) - 1
    c, o, h, l = v["c"][i], v["o"][i], v["h"][i], v["l"][i]
    lvl = v["l"][i - 40:i - 1].min()
    rng = max(h - l, 1e-12)
    lower = min(o, c) - l
    if (l < lvl - 0.05 * atr and (lvl - l) <= 2.5 * atr and c > lvl
            and lower >= 0.4 * rng and volr >= 1.3):
        return "liq_sweep", f"sweep of {abs(lvl):.6g} + reclaim, vol x{volr:.1f}"
    return None


def evaluate(df, side):
    if len(df) < 80:
        return None
    d = prep(df)
    i = len(d) - 1
    atr = float(d["atr"].iloc[i])
    vma = float(d["vma"].iloc[i])
    if not (atr > 0 and vma > 0):
        return None
    volr = float(d["volume"].iloc[i] / vma)
    tb = float(d["taker_buy_base"].iloc[i] / max(d["volume"].iloc[i], 1e-12))
    if side == "SHORT":
        tb = 1 - tb
    v = _view(d, side)
    hits = []
    for fn in (s_breakout, s_pullback, s_sweep):
        r = fn(v, atr, volr, tb)
        if r:
            hits.append(r)
    return {"close": float(d["close"].iloc[i]), "atr": atr, "hits": hits}