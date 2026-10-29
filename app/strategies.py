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
    i = len(v["c"]) - 1
    c, o = v["c"][i], v["o"][i]
    if not v["e20"][i] > v["e50"][i]:
        return None
    level = v["h"][i - 20:i].max()
    if (c > level and c > o and c > v["e20"][i] and volr >= 1.5 and tb >= 0.52
            and (c - level) <= 1.5 * atr):
        return "breakout_retest", f"20-candle breakout, vol x{volr:.1f}, buy {tb:.2f}"
    lvl = v["h"][i - 30:i - 8].max()
    if ((v["c"][i - 8:i] > lvl).any() and v["l"][i] <= lvl + 0.3 * atr
            and c > lvl and c > o and c > v["e20"][i] and tb >= 0.5):
        return "breakout_retest", f"retest of {abs(lvl):.6g}, buy {tb:.2f}"
    return None


def s_pullback(v, atr, volr, tb):
    i = len(v["c"]) - 1
    c, o, h, l = v["c"], v["o"], v["h"], v["l"]
    e20, e50 = v["e20"], v["e50"]
    if not (e20[i] > e50[i] and e20[i] > e20[i - 5] and c[i] > e20[i] and c[i] > o[i]):
        return None
    if not (l[i - 2:i + 1] <= e20[i - 2:i + 1] + 0.2 * atr).any():
        return None
    if not (40 <= v["rsi"][i] <= 65):
        return None
    body = c[i] - o[i]
    lower = min(o[i], c[i]) - l[i]
    rng = max(h[i] - l[i], 1e-12)
    hammer = lower >= 1.2 * body and lower >= 0.4 * rng
    engulf = c[i - 1] < o[i - 1] and c[i] >= o[i - 1] and o[i] <= c[i - 1]
    if hammer:
        return "ema_pullback", f"EMA20 pullback + hammer, RSI {v['rsi'][i]:.0f}"
    if engulf:
        return "ema_pullback", f"EMA20 pullback + engulfing, RSI {v['rsi'][i]:.0f}"
    return None


def s_sweep(v, atr, volr, tb):
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