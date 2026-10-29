import sqlite3
import time
from datetime import datetime

import numpy as np
import pandas as pd

from . import config
from .scanner import log, clip, ema, calc_atr, swings, BDT, HOUR_MS

# score weights (total 100)
RW = {"trend": 25, "sr": 20, "volume": 20, "oi": 20, "ls": 15}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ranking (
    run_hour TEXT NOT NULL,
    symbol TEXT NOT NULL,
    rank INTEGER,
    side TEXT,
    rank_score REAL,
    aligned REAL,
    daily_score REAL,
    trend REAL, sr REAL, volume REAL, oi REAL, ls REAL,
    close REAL, atr REAL, vol_ratio REAL, quote_vol_24h REAL,
    created_at INTEGER,
    PRIMARY KEY (run_hour, symbol)
);
"""


# ------------------------------------------------------------ components
# Shob score -1 (bearish) theke +1 (bullish)

def r_trend(d, atr_v):
    c = d["close"]
    e20, e50 = ema(c, 20), ema(c, 50)
    last = c.iloc[-1]
    s1 = 1.0 if e20.iloc[-1] > e50.iloc[-1] else -1.0
    s2 = 1.0 if last > e20.iloc[-1] else -1.0
    s3 = clip((e20.iloc[-1] - e20.iloc[-6]) / atr_v / 1.5)
    w = d.tail(24)
    h = len(w) // 2
    hh = w["high"].iloc[h:].max() - w["high"].iloc[:h].max()
    ll = w["low"].iloc[h:].min() - w["low"].iloc[:h].min()
    s4 = (np.sign(hh) + np.sign(ll)) / 2
    return clip(0.25 * s1 + 0.25 * s2 + 0.25 * s3 + 0.25 * s4)


def r_sr(d, close, atr_v, wl_sup, wl_res):
    highs, lows = swings(d.tail(200), 3)
    if wl_res is not None and not pd.isna(wl_res):
        highs.append(float(wl_res))      # daily level o dhorbo
    if wl_sup is not None and not pd.isna(wl_sup):
        lows.append(float(wl_sup))
    above = [h for h in highs if h > close]
    below = [l for l in lows if l < close]
    res = min(above) if above else None
    sup = max(below) if below else None
    if res is None and sup is None:
        return 0.0
    if res is None:
        return 1.0       # breakout
    if sup is None:
        return -1.0      # breakdown
    up = (res - close) / atr_v
    dn = (close - sup) / atr_v
    return clip((up - dn) / (up + dn + 1e-9))


def r_volume(d, tk):
    w = d.tail(24)
    base = w["volume"].mean()
    recent = w["volume"].tail(3).mean()
    ratio = float(recent / base) if base > 0 else 1.0
    r3 = w.tail(3)
    buy = r3["taker_buy_base"].sum() / max(r3["volume"].sum(), 1e-9)
    dir1 = clip((buy - 0.5) * 20)
    if tk is not None and len(tk) >= 3:
        dir2 = clip((tk["buy_sell_ratio"].tail(3).mean() - 1.0) * 5)
        direction = (dir1 + dir2) / 2
    else:
        direction = dir1
    mag = clip((ratio - 0.8) / 1.2, 0, 1)
    return direction * mag, ratio


def r_oi(o, price_chg):
    if o is None or len(o) < 5:
        return 0.0
    v = o["sum_oi_value"].tail(5)         # last 4 ghonta
    if v.iloc[0] <= 0:
        return 0.0
    chg = float(v.iloc[-1] / v.iloc[0] - 1)
    pdir = clip(price_chg / 0.015)
    if chg > 0:
        return clip(pdir * clip(chg / 0.04))
    return clip(-0.3 * pdir * clip(-chg / 0.04))


def r_ls(top_pos, glob):
    s = 0.0
    if top_pos and top_pos > 0:
        s += 0.5 * clip(np.log(top_pos) / 0.4)      # smart money
    if glob and glob > 0:
        s -= 0.5 * clip(np.log(glob) / 0.4)         # crowd er ulta
    return clip(s)


# ------------------------------------------------------------ main
def run_rank(wait=True):
    con = sqlite3.connect(str(config.DB_PATH), timeout=30)
    con.executescript(SCHEMA)

    scan_date = con.execute("SELECT MAX(scan_date) FROM watchlist").fetchone()[0]
    if not scan_date:
        log.error("Watchlist nai. Age 'python scanner_main.py --now' chalan.")
        return
    wl = pd.read_sql_query("SELECT * FROM watchlist WHERE scan_date=?", con, params=(scan_date,))
    symbols = wl["symbol"].tolist()
    wl = wl.set_index("symbol")

    now_ms = int(time.time() * 1000)
    exp_open = (now_ms // HOUR_MS - 1) * HOUR_MS      # last closed 1h candle
    cutoff = exp_open + HOUR_MS
    run_hour = datetime.fromtimestamp(cutoff / 1000, tz=BDT).strftime("%Y-%m-%d %H:00")

    if wait:
        for _ in range(20):
            n = con.execute(
                "SELECT COUNT(DISTINCT symbol) FROM klines WHERE interval='1h' AND open_time=?",
                (exp_open,)).fetchone()[0]
            if n >= 0.9 * len(symbols):
                break
            log.info(f"Notun 1h candle er jonno wait ({n}/{len(symbols)})...")
            time.sleep(15)

    log.info(f"Hourly rank shuru | run_hour={run_hour} (BDT) | watchlist={len(symbols)} "
             f"(scan_date={scan_date})")

    k = pd.read_sql_query(
        "SELECT symbol, open_time, open, high, low, close, volume, taker_buy_base "
        "FROM klines WHERE interval='1h' AND open_time>=? AND open_time<?",
        con, params=(cutoff - 250 * HOUR_MS, cutoff))
    oi = pd.read_sql_query(
        "SELECT symbol, ts, sum_oi_value FROM oi_hist WHERE period='1h' AND ts>=? AND ts<=?",
        con, params=(cutoff - 12 * HOUR_MS, cutoff))
    tk = pd.read_sql_query(
        "SELECT symbol, ts, buy_sell_ratio FROM taker_ratio WHERE period='1h' AND ts>=? AND ts<=?",
        con, params=(cutoff - 12 * HOUR_MS, cutoff))
    ls = pd.read_sql_query(
        "SELECT kind, symbol, ts, ratio FROM ls_ratio "
        "WHERE period='1h' AND kind IN ('top_position','global_account') AND ts>=? AND ts<=?",
        con, params=(cutoff - 12 * HOUR_MS, cutoff))
    uni = dict(con.execute("SELECT symbol, quote_volume_24h FROM universe").fetchall())

    sset = set(symbols)
    gk = {s: g.sort_values("open_time").reset_index(drop=True)
          for s, g in k.groupby("symbol") if s in sset}
    go = {s: g.sort_values("ts").reset_index(drop=True) for s, g in oi.groupby("symbol")}
    gt = {s: g.sort_values("ts").reset_index(drop=True) for s, g in tk.groupby("symbol")}
    gl = {}
    for (kind, s), g in ls.groupby(["kind", "symbol"]):
        gl[(kind, s)] = float(g.sort_values("ts")["ratio"].iloc[-1])

    results, skipped = [], 0
    for sym in symbols:
        try:
            d = gk.get(sym)
            if (d is None or len(d) < config.MIN_1H_CANDLES
                    or int(d["open_time"].iloc[-1]) != exp_open):
                skipped += 1
                continue
            atr_v = calc_atr(d)
            if not atr_v > 0:
                skipped += 1
                continue

            close = float(d["close"].iloc[-1])
            price_chg = close / float(d["close"].iloc[-5]) - 1     # last 4h

            c_vol, vol_ratio = r_volume(d, gt.get(sym))
            comp = {
                "trend": r_trend(d, atr_v),
                "sr": r_sr(d, close, atr_v, wl.loc[sym, "support"], wl.loc[sym, "resistance"]),
                "volume": c_vol,
                "oi": r_oi(go.get(sym), price_chg),
                "ls": r_ls(gl.get(("top_position", sym)), gl.get(("global_account", sym))),
            }
            pts = {x: RW[x] * comp[x] for x in comp}
            total = sum(pts.values())                 # + = 1h bullish, - = 1h bearish
            if np.isnan(total):
                skipped += 1
                continue

            side = wl.loc[sym, "side"]
            daily_score = float(wl.loc[sym, "score"])
            sign = 1.0 if side == "LONG" else -1.0
            aligned = total * sign                    # + = 1h daily side er sathe match
            rank_score = aligned + 0.3 * daily_score  # daily bias bonus (max +30)

            results.append({
                "symbol": sym, "side": side, "aligned": aligned, "rank_score": rank_score,
                "daily": daily_score, "pts": {x: pts[x] * sign for x in pts},
                "close": close, "atr": atr_v, "vol_ratio": vol_ratio,
            })
        except Exception as e:
            skipped += 1
            log.warning(f"Rank {sym}: {e}")

    mism = [r for r in results if r["aligned"] <= 0]     # 1h daily er biporite = bad
    results = [r for r in results if r["aligned"] > 0]
    results.sort(key=lambda r: r["rank_score"], reverse=True)
    results = results[:config.RANK_TOP_N]

    now = int(time.time() * 1000)
    rows = []
    for i, r in enumerate(results, 1):
        p = r["pts"]
        rows.append((
            run_hour, r["symbol"], i, r["side"],
            round(r["rank_score"], 2), round(r["aligned"], 2), round(r["daily"], 2),
            round(p["trend"], 2), round(p["sr"], 2), round(p["volume"], 2),
            round(p["oi"], 2), round(p["ls"], 2),
            r["close"], r["atr"], round(r["vol_ratio"], 2),
            float(uni.get(r["symbol"], 0.0)), now,
        ))

    con.execute("DELETE FROM ranking WHERE run_hour=?", (run_hour,))
    con.executemany("INSERT INTO ranking VALUES (" + ",".join("?" * 17) + ")", rows)
    con.commit()

    longs = sum(1 for r in rows if r[3] == "LONG")
    log.info(f"Ranking saved: top {len(rows)} (LONG {longs} / SHORT {len(rows) - longs}) | "
             f"1h daily er biporite={len(mism)} | skipped={skipped}")

    cols = ["run_hour", "symbol", "rank", "side", "rank_score", "aligned", "daily_score",
            "trend", "sr", "volume", "oi", "ls", "close", "atr", "vol_ratio",
            "quote_vol_24h", "created_at"]
    pd.DataFrame(rows, columns=cols).to_csv(
        config.DB_PATH.parent / "ranking_latest.csv", index=False)

    for r in rows:
        log.info(f"  #{r[2]:>2} {r[1]:<14} {r[3]:<5} rank={r[4]:>5.1f} "
                 f"[1h {r[5]:>5.1f} | daily {r[6]:>4.1f}] "
                 f"[trend {r[7]:>5.1f} sr {r[8]:>5.1f} vol {r[9]:>5.1f} "
                 f"oi {r[10]:>5.1f} ls {r[11]:>5.1f}] vol24h={r[15] / 1e6:>6.0f}M")
    con.close()