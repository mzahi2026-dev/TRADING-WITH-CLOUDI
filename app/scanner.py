import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler

import numpy as np
import pandas as pd

from . import config

BDT = timezone(timedelta(hours=6))
DAY_MS = 86_400_000
HOUR_MS = 3_600_000

# ---- logger (alada file, collector er sathe conflict hobe na) ----
config.LOG_DIR.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("scanner")
log.setLevel(logging.INFO)
if not log.handlers:
    _fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    _fh = RotatingFileHandler(config.LOG_DIR / "scanner.log", maxBytes=5_000_000,
                              backupCount=3, encoding="utf-8")
    _fh.setFormatter(_fmt)
    log.addHandler(_fh)
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(_fmt)
    log.addHandler(_sh)

# ---- score weights (total 100) ----
W = {"trend": 30, "sr": 20, "volume": 20, "candle": 15, "oi": 10, "funding": 5}

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    scan_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    rank INTEGER,
    side TEXT,
    score REAL,
    trend REAL, sr REAL, volume REAL, candle REAL, oi REAL, funding REAL,
    close REAL, support REAL, resistance REAL, atr REAL,
    vol_ratio REAL, oi_chg_pct REAL, price_chg_pct REAL,
    created_at INTEGER,
    PRIMARY KEY (scan_date, symbol)
);
"""


def clip(x, lo=-1.0, hi=1.0):
    return float(max(lo, min(hi, x)))


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def calc_atr(d, n=14):
    pc = d["close"].shift(1)
    tr = pd.concat([d["high"] - d["low"],
                    (d["high"] - pc).abs(),
                    (d["low"] - pc).abs()], axis=1).max(axis=1)
    return float(tr.tail(n).mean())


# ------------------------------------------------------------ components
# Shob score -1 (bearish) theke +1 (bullish)

def score_trend(d, atr_v):
    c = d["close"]
    e20, e50 = ema(c, 20), ema(c, 50)
    last = c.iloc[-1]
    s1 = 1.0 if e20.iloc[-1] > e50.iloc[-1] else -1.0
    s2 = 1.0 if last > e20.iloc[-1] else -1.0
    s3 = clip((e20.iloc[-1] - e20.iloc[-6]) / atr_v / 2) if len(e20) > 6 else 0.0
    w = d.tail(15)
    h = len(w) // 2
    hh = w["high"].iloc[h:].max() - w["high"].iloc[:h].max()
    ll = w["low"].iloc[h:].min() - w["low"].iloc[:h].min()
    s4 = (np.sign(hh) + np.sign(ll)) / 2
    return clip(0.25 * s1 + 0.25 * s2 + 0.25 * s3 + 0.25 * s4)


def swings(df, k):
    hi, lo = df["high"].values, df["low"].values
    highs, lows = [], []
    for i in range(k, len(df) - k):
        if hi[i] == hi[i - k:i + k + 1].max():
            highs.append(float(hi[i]))
        if lo[i] == lo[i - k:i + k + 1].min():
            lows.append(float(lo[i]))
    return highs, lows


def score_sr(d1, d4, close, atr_v):
    h4, l4 = swings(d4.tail(90), 3)    # 15 din er 4h swing
    h1, l1 = swings(d1.tail(15), 2)    # 15 din er 1d swing
    highs, lows = h4 + h1, l4 + l1
    above = [h for h in highs if h > close]
    below = [l for l in lows if l < close]
    res = min(above) if above else None
    sup = max(below) if below else None
    if res is None and sup is None:
        return 0.0, None, None
    if res is None:
        return 1.0, sup, None      # shob resistance er upore = breakout
    if sup is None:
        return -1.0, None, res     # shob support er niche = breakdown
    up = (res - close) / atr_v
    dn = (close - sup) / atr_v
    return clip((up - dn) / (up + dn + 1e-9)), sup, res


def score_volume(d):
    w = d.tail(15)
    base = w["volume"].mean()
    recent = w["volume"].tail(3).mean()
    ratio = float(recent / base) if base > 0 else 1.0
    r3 = w.tail(3)
    buy = r3["taker_buy_base"].sum() / max(r3["volume"].sum(), 1e-9)
    direction = clip((buy - 0.5) * 20)
    mag = clip((ratio - 0.8) / 1.2, 0, 1)
    return direction * mag, ratio


def score_candle(d):
    last, prev = d.iloc[-1], d.iloc[-2]
    rng = max(last["high"] - last["low"], 1e-12)
    pos = (last["close"] - last["low"]) / rng * 2 - 1
    upper = last["high"] - max(last["open"], last["close"])
    lower = min(last["open"], last["close"]) - last["low"]
    wick = clip((lower - upper) / rng * 2)
    body = clip((last["close"] - last["open"]) / rng)
    eng = 0.0
    if (prev["close"] < prev["open"] and last["close"] > last["open"]
            and last["close"] >= prev["open"] and last["open"] <= prev["close"]):
        eng = 1.0
    elif (prev["close"] > prev["open"] and last["close"] < last["open"]
            and last["close"] <= prev["open"] and last["open"] >= prev["close"]):
        eng = -1.0
    return clip(0.3 * pos + 0.3 * wick + 0.2 * eng + 0.2 * body)


def score_oi(o, price_chg):
    if o is None or len(o) < 24:
        return 0.0, 0.0
    v = o["sum_oi_value"].tail(73)       # last 72 ghonta
    if v.iloc[0] <= 0:
        return 0.0, 0.0
    chg = float(v.iloc[-1] / v.iloc[0] - 1)
    pdir = clip(price_chg / 0.03)
    if chg > 0:
        s = pdir * clip(chg / 0.10)           # price + OI ek dike = trend confirm
    else:
        s = -0.3 * pdir * clip(-chg / 0.10)   # OI komche = trend dorbol
    return clip(s), chg


def score_funding(f):
    if f is None or len(f) == 0:
        return 0.0
    avg = f["funding_rate"].tail(9).mean()    # last 3 din
    return clip(-avg / 0.0003)                # beshi positive = crowded long = bearish


# ------------------------------------------------------------ main scan
def run_scan(wait=True):
    con = sqlite3.connect(str(config.DB_PATH), timeout=30)
    con.executescript(SCHEMA)

    now_ms = int(time.time() * 1000)
    exp_open = (now_ms // DAY_MS - 1) * DAY_MS     # last closed 1d candle
    cutoff = exp_open + DAY_MS                     # 00:00 UTC = 06:00 BDT
    scan_date = datetime.fromtimestamp(cutoff / 1000, tz=BDT).strftime("%Y-%m-%d")

    symbols = [r[0] for r in con.execute("SELECT symbol FROM universe")]
    if not symbols:
        log.error("Universe khali. Age collector chalan.")
        return

    if wait:
        for _ in range(20):
            n = con.execute(
                "SELECT COUNT(DISTINCT symbol) FROM klines WHERE interval='1d' AND open_time=?",
                (exp_open,)).fetchone()[0]
            if n >= 0.9 * len(symbols):
                break
            log.info(f"Notun 1d candle er jonno wait ({n}/{len(symbols)})...")
            time.sleep(30)

    log.info(f"Daily scan shuru | scan_date={scan_date} | symbols={len(symbols)}")

    d1 = pd.read_sql_query(
        "SELECT symbol, open_time, open, high, low, close, volume, taker_buy_base "
        "FROM klines WHERE interval='1d' AND open_time>=? AND open_time<?",
        con, params=(cutoff - 130 * DAY_MS, cutoff))
    d4 = pd.read_sql_query(
        "SELECT symbol, open_time, open, high, low, close, volume, taker_buy_base "
        "FROM klines WHERE interval='4h' AND open_time>=? AND open_time<?",
        con, params=(cutoff - 16 * DAY_MS, cutoff))
    oi = pd.read_sql_query(
        "SELECT symbol, ts, sum_oi_value FROM oi_hist "
        "WHERE period='1h' AND ts>=? AND ts<=?",
        con, params=(cutoff - 80 * HOUR_MS, cutoff))
    fu = pd.read_sql_query(
        "SELECT symbol, funding_time, funding_rate FROM funding_hist "
        "WHERE funding_time>=? AND funding_time<=?",
        con, params=(cutoff - 5 * DAY_MS, cutoff))

    g1 = {s: g.sort_values("open_time").reset_index(drop=True) for s, g in d1.groupby("symbol")}
    g4 = {s: g.sort_values("open_time").reset_index(drop=True) for s, g in d4.groupby("symbol")}
    go = {s: g.sort_values("ts").reset_index(drop=True) for s, g in oi.groupby("symbol")}
    gf = {s: g.sort_values("funding_time").reset_index(drop=True) for s, g in fu.groupby("symbol")}

    results, skipped = [], 0
    for sym in symbols:
        try:
            a, b = g1.get(sym), g4.get(sym)
            if (a is None or b is None or len(a) < config.MIN_1D_CANDLES
                    or len(b) < 30 or int(a["open_time"].iloc[-1]) != exp_open):
                skipped += 1
                continue
            atr_v = calc_atr(a)
            if not atr_v > 0:
                skipped += 1
                continue

            close = float(a["close"].iloc[-1])
            price_chg = close / float(a["close"].iloc[-4]) - 1

            c_sr, sup, res = score_sr(a, b, close, atr_v)
            c_vol, vol_ratio = score_volume(a)
            c_oi, oi_chg = score_oi(go.get(sym), price_chg)
            comp = {
                "trend": score_trend(a, atr_v),
                "sr": c_sr,
                "volume": c_vol,
                "candle": score_candle(a),
                "oi": c_oi,
                "funding": score_funding(gf.get(sym)),
            }
            pts = {k: W[k] * comp[k] for k in comp}     # signed points
            total = sum(pts.values())
            if np.isnan(total):
                skipped += 1
                continue
            results.append({
                "symbol": sym, "total": total, "pts": pts, "close": close,
                "sup": sup, "res": res, "atr": atr_v, "vol_ratio": vol_ratio,
                "oi_chg": oi_chg, "price_chg": price_chg,
            })
        except Exception as e:
            skipped += 1
            log.warning(f"Scan {sym}: {e}")

    results.sort(key=lambda r: abs(r["total"]), reverse=True)
    results = [r for r in results if abs(r["total"]) >= config.WATCHLIST_MIN_SCORE]
    results = results[:config.WATCHLIST_MAX]

    now = int(time.time() * 1000)
    rows = []
    for i, r in enumerate(results, 1):
        p = r["pts"]
        rows.append((
            scan_date, r["symbol"], i, "LONG" if r["total"] > 0 else "SHORT",
            round(abs(r["total"]), 2),
            round(p["trend"], 2), round(p["sr"], 2), round(p["volume"], 2),
            round(p["candle"], 2), round(p["oi"], 2), round(p["funding"], 2),
            r["close"], r["sup"], r["res"], r["atr"],
            round(r["vol_ratio"], 2), round(r["oi_chg"] * 100, 2),
            round(r["price_chg"] * 100, 2), now,
        ))

    con.execute("DELETE FROM watchlist WHERE scan_date=?", (scan_date,))
    con.executemany("INSERT INTO watchlist VALUES (" + ",".join("?" * 19) + ")", rows)
    con.commit()

    longs = sum(1 for r in rows if r[3] == "LONG")
    log.info(f"Watchlist saved: {len(rows)} symbols "
             f"(LONG {longs} / SHORT {len(rows) - longs}), skipped={skipped}")

    cols = ["scan_date", "symbol", "rank", "side", "score", "trend", "sr", "volume",
            "candle", "oi", "funding", "close", "support", "resistance", "atr",
            "vol_ratio", "oi_chg_pct", "price_chg_pct", "created_at"]
    out = pd.DataFrame(rows, columns=cols)
    out.to_csv(config.DB_PATH.parent / "watchlist_latest.csv", index=False)

    log.info("Top 15:")
    for r in rows[:15]:
        log.info(f"  #{r[2]:>3} {r[1]:<14} {r[3]:<5} score={r[4]:>5.1f} "
                 f"[trend {r[5]:>6.1f} sr {r[6]:>6.1f} vol {r[7]:>6.1f} "
                 f"candle {r[8]:>6.1f} oi {r[9]:>5.1f} fund {r[10]:>5.1f}]")
    con.close()