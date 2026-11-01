import os
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from app import config

BDT = timezone(timedelta(hours=6))
SHIFT = 6 * 3600          # chart e BDT dekhanor jonno
INDEX = Path(__file__).parent / "static" / "index.html"
app = FastAPI()
JOBS = {}

DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()


def check_auth(request: Request):
    if not DASHBOARD_TOKEN:
        return True
    token = request.headers.get("X-API-KEY") or request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if token == DASHBOARD_TOKEN:
        return True
    if request.query_params.get("token") == DASHBOARD_TOKEN:
        return True
    if request.cookies.get("dashboard_token") == DASHBOARD_TOKEN:
        return True
    raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing dashboard token")


def db():
    return sqlite3.connect(str(config.DB_PATH), timeout=30)


def has_table(con, name):
    r = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return r is not None


def clean(df):
    return df.astype(object).where(df.notna(), None).to_dict("records")


def fmt(ms):
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=BDT).strftime("%m-%d %H:%M")


def ser(t, v):
    out = []
    for a, b in zip(t, v):
        if b is not None and np.isfinite(b):
            out.append({"time": a, "value": round(float(b), 8)})
    return out


@app.get("/ping")
def ping():
    return {"status": "alive", "server_time": int(time.time() * 1000)}


@app.get("/")
def index(request: Request, response: Response):
    token = request.query_params.get("token")
    if DASHBOARD_TOKEN:
        if token == DASHBOARD_TOKEN:
            res = FileResponse(str(INDEX))
            res.set_cookie("dashboard_token", token, max_age=86400 * 7, httponly=True)
            return res
        if request.cookies.get("dashboard_token") != DASHBOARD_TOKEN:
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized. Pass ?token=YOUR_TOKEN to authenticate."}
            )
    return FileResponse(str(INDEX))


# ------------------------------------------------------------------ candles
@app.get("/api/candles")
def candles(symbol: str, interval: str = "15m", limit: int = 600):
    if interval not in config.INTERVALS:
        raise HTTPException(400, "bad interval")
    symbol = symbol.upper()
    con = db()
    df = pd.read_sql_query(
        "SELECT open_time, open, high, low, close, volume FROM klines "
        "WHERE symbol=? AND interval=? ORDER BY open_time DESC LIMIT ?",
        con, params=(symbol, interval, limit + 300))
    if df.empty:
        con.close()
        return {"candles": []}

    # Ensure chronological order (oldest -> newest) BEFORE calculating indicators/levels
    df = df.sort_values("open_time").reset_index(drop=True)

    lv = {}
    if len(df) >= 20:
        cur_c = float(df["close"].iloc[-1])
        # Calculate ATR for dynamic gap reference
        highs, lows, closes = df["high"].values, df["low"].values, df["close"].values
        tr = [highs[0] - lows[0]]
        for i in range(1, len(df)):
            tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
        atr = pd.Series(tr).rolling(14).mean().iloc[-1]
        if pd.isna(atr) or atr <= 0:
            atr = cur_c * 0.015

        # Look at last 150 candles on current chart for recent swing structure
        window = min(len(df), 150)
        sub = df.tail(window).reset_index(drop=True)
        hi, lo = sub["high"].values, sub["low"].values
        sw_highs, sw_lows = [], []
        k = 2
        for i in range(k, len(sub) - k):
            if hi[i] == hi[i - k:i + k + 1].max():
                sw_highs.append(float(hi[i]))
            if lo[i] == lo[i - k:i + k + 1].min():
                sw_lows.append(float(lo[i]))

        buf = atr * 0.2
        above = [h for h in sw_highs if h > cur_c + buf]
        below = [l for l in sw_lows if l < cur_c - buf]

        # If not enough swings in recent window, search wider dataset
        if not above or not below:
            hi_all, lo_all = df["high"].values, df["low"].values
            for i in range(k, len(df) - k):
                if not above and hi_all[i] == hi_all[i - k:i + k + 1].max() and hi_all[i] > cur_c + buf:
                    above.append(float(hi_all[i]))
                if not below and lo_all[i] == lo_all[i - k:i + k + 1].min() and lo_all[i] < cur_c - buf:
                    below.append(float(lo_all[i]))

        dyn_res = min(above) if above else (cur_c + 2.0 * atr)
        dyn_sup = max(below) if below else (cur_c - 2.0 * atr)

        # Invariant safety clamps: support must be < cur_c, resistance must be > cur_c
        if dyn_sup >= cur_c:
            dyn_sup = cur_c - 1.5 * atr
        if dyn_res <= cur_c:
            dyn_res = cur_c + 1.5 * atr

        prec = 6 if cur_c < 0.01 else (5 if cur_c < 1 else (4 if cur_c < 100 else 2))
        lv = {"side": "DYNAMIC", "support": float(round(dyn_sup, prec)), "resistance": float(round(dyn_res, prec))}
    elif has_table(con, "watchlist"):
        r = con.execute("SELECT side, support, resistance FROM watchlist "
                        "WHERE symbol=? ORDER BY scan_date DESC LIMIT 1", (symbol,)).fetchone()
        if r:
            lv = {"side": r[0], "support": r[1], "resistance": r[2]}
    con.close()
    c = df["close"]
    ema20 = c.ewm(span=20, adjust=False).mean()
    ema50 = c.ewm(span=50, adjust=False).mean()
    vma = df["volume"].rolling(20).mean()
    d = c.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    rs = up.ewm(alpha=1 / 14, adjust=False).mean() / dn.ewm(alpha=1 / 14, adjust=False).mean().replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    sig = macd.ewm(span=9, adjust=False).mean()
    hist = macd - sig

    s = max(0, len(df) - limit)
    df = df.iloc[s:]
    t = (df["open_time"] // 1000 + SHIFT).tolist()
    ix = df.index

    cds = [{"time": a, "open": o, "high": h, "low": l, "close": cl}
           for a, o, h, l, cl in zip(t, df["open"], df["high"], df["low"], df["close"])]
    vol = [{"time": a, "value": v, "color": "#26a69a88" if cl >= o else "#ef535088"}
           for a, v, o, cl in zip(t, df["volume"], df["open"], df["close"])]
    hs = [{"time": a, "value": float(h), "color": "#26a69a" if h >= 0 else "#ef5350"}
          for a, h in zip(t, hist.loc[ix]) if np.isfinite(h)]
    return {
        "candles": cds, "volume": vol, "hist": hs,
        "ema20": ser(t, ema20.loc[ix].tolist()), "ema50": ser(t, ema50.loc[ix].tolist()),
        "vma": ser(t, vma.loc[ix].tolist()), "rsi": ser(t, rsi.loc[ix].tolist()),
        "macd": ser(t, macd.loc[ix].tolist()), "signal": ser(t, sig.loc[ix].tolist()),
        "levels": lv,
    }


# ------------------------------------------------------------------ analysis
@app.get("/api/analysis")
def get_analysis(symbol: str, interval: str = "15m"):
    from app.strategies import prep, evaluate
    from app import trade_config as tc
    symbol = symbol.upper()
    con = db()
    df = pd.read_sql_query(
        "SELECT open_time, open, high, low, close, volume, taker_buy_base FROM klines "
        "WHERE symbol=? AND interval=? ORDER BY open_time DESC LIMIT 150",
        con, params=(symbol, interval)
    )
    # Check current bias from ranking
    rh = con.execute("SELECT MAX(run_hour) FROM ranking").fetchone()
    side = "LONG"
    rank_score = 0
    if rh and rh[0]:
        rk = con.execute("SELECT side, rank_score FROM ranking WHERE run_hour=? AND symbol=?", (rh[0], symbol)).fetchone()
        if rk:
            side = rk[0]
            rank_score = rk[1]
        else:
            wl = con.execute("SELECT side, score FROM watchlist WHERE symbol=? ORDER BY scan_date DESC LIMIT 1", (symbol,)).fetchone()
            if wl:
                side = wl[0]
                rank_score = wl[1]
    con.close()

    if df.empty or len(df) < 50:
        return {"ok": False, "msg": "Not enough candle history for analysis"}

    df = df.sort_values("open_time").reset_index(drop=True)
    d = prep(df)
    last = d.iloc[-1]
    close = float(last["close"])
    atr = float(last["atr"]) if not pd.isna(last["atr"]) else close * 0.01
    ema20 = float(last["ema20"])
    ema50 = float(last["ema50"])
    rsi = float(last["rsi"]) if not pd.isna(last["rsi"]) else 50.0

    # Auto detect strategy hits for both LONG and SHORT
    ev_long = evaluate(df, "LONG")
    ev_short = evaluate(df, "SHORT")

    active_ev = ev_long if side == "LONG" else ev_short
    setup_name = active_ev["hits"][0][0] if (active_ev and active_ev.get("hits")) else None
    setup_reason = active_ev["hits"][0][1] if (active_ev and active_ev.get("hits")) else "Monitoring setup (Trend alignment & pullback)"

    # If no hit on primary side, check alternative
    if not setup_name:
        if ev_short and ev_short.get("hits"):
            side = "SHORT"
            setup_name = ev_short["hits"][0][0]
            setup_reason = ev_short["hits"][0][1]
        elif ev_long and ev_long.get("hits"):
            side = "LONG"
            setup_name = ev_long["hits"][0][0]
            setup_reason = ev_long["hits"][0][1]
        else:
            # Fallback to trend bias
            setup_name = f"{side} Trend Follow"

    k = 1 if side == "LONG" else -1
    entry = close
    sl = round(entry - k * tc.SL_ATR * atr, 6 if close < 1 else 4 if close < 100 else 2)
    tp1 = round(entry + k * tc.TP_ATR * atr, 6 if close < 1 else 4 if close < 100 else 2)
    tp2 = round(entry + k * (tc.TP_ATR * 1.5) * atr, 6 if close < 1 else 4 if close < 100 else 2)
    risk_reward = f"1 : {(tc.TP_ATR / tc.SL_ATR):.1f}"

    return {
        "ok": True,
        "symbol": symbol,
        "side": side,
        "strategy": setup_name,
        "reason": setup_reason,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "risk_reward": risk_reward,
        "atr": round(atr, 4),
        "ema20": round(ema20, 4),
        "ema50": round(ema50, 4),
        "rsi": round(rsi, 1),
        "rank_score": rank_score
    }


# ------------------------------------------------------------------ watch / signals
@app.get("/api/watch")
def watch():
    con = db()
    out = {"watch": [], "rank": [], "universe": [], "scan_date": None, "run_hour": None}
    if has_table(con, "universe"):
        out["universe"] = [r[0] for r in con.execute("SELECT symbol FROM universe ORDER BY symbol")]
    if has_table(con, "watchlist"):
        sd = con.execute("SELECT MAX(scan_date) FROM watchlist").fetchone()[0]
        out["scan_date"] = sd
        if sd:
            df = pd.read_sql_query(
                "SELECT w.symbol, w.rank, w.side, w.score, w.trend, w.volume, w.oi, w.close, "
                "COALESCE(u.quote_volume_24h, 0.0) AS quote_vol_24h "
                "FROM watchlist w LEFT JOIN universe u ON w.symbol = u.symbol "
                "WHERE w.scan_date=? ORDER BY w.rank",
                con, params=(sd,)
            )
            out["watch"] = clean(df)
    if has_table(con, "ranking"):
        rh = con.execute("SELECT MAX(run_hour) FROM ranking").fetchone()[0]
        out["run_hour"] = rh
        if rh:
            df = pd.read_sql_query("SELECT symbol, rank, side, rank_score, trend, volume, oi, aligned, daily_score, "
                                   "close, quote_vol_24h FROM ranking WHERE run_hour=? ORDER BY rank",
                                   con, params=(rh,))
            out["rank"] = clean(df)
    con.close()
    return out


@app.get("/api/signals")
def signals():
    con = db()
    if not has_table(con, "signals"):
        con.close()
        return {"rows": [], "note": "Step 3 (entry engine) ekhono install hoyni."}
    df = pd.read_sql_query("SELECT * FROM signals ORDER BY rowid DESC LIMIT 100", con)
    con.close()
    return {"rows": clean(df), "note": ""}


@app.get("/api/positions")
def positions():
    con = db()
    if not has_table(con, "positions"):
        con.close()
        return {"rows": [], "note": "Step 3 (demo executor) ekhono install hoyni."}
    df = pd.read_sql_query("SELECT * FROM positions ORDER BY rowid DESC LIMIT 100", con)
    con.close()
    return {"rows": clean(df), "note": ""}


@app.get("/api/journal")
def journal_summary():
    con = db()
    if not has_table(con, "positions"):
        con.close()
        return {
            "summary": {
                "balance": 0.0, "available": 0.0, "today_pnl": 0.0, "today_trades": 0,
                "active_positions": 0, "tp_count": 0, "tp_usdt": 0.0, "sl_count": 0, "sl_usdt": 0.0,
            },
            "days": [],
            "trades_by_day": {}
        }

    av_bal, tot_bal = 0.0, 0.0
    try:
        from app.demo_client import DemoClient
        dc = DemoClient()
        av_bal, tot_bal = dc.balance()
    except Exception as e:
        log.warning(f"Journal balance check: {e}")

    # Crypto Trading Day: 00:00 UTC = 06:00 AM BDT (Binance 1D candle close)
    now_utc = datetime.now(timezone.utc)
    today_start_utc = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc)
    today_start_ms = int(today_start_utc.timestamp() * 1000)
    seven_days_ago_ms = today_start_ms - 6 * 86400 * 1000

    df = pd.read_sql_query(
        "SELECT * FROM positions WHERE entry_ms>=? ORDER BY rowid DESC",
        con, params=(seven_days_ago_ms,)
    )
    if df.empty:
        df = pd.read_sql_query("SELECT * FROM positions ORDER BY rowid DESC LIMIT 100", con)
    con.close()

    BDT = timezone(timedelta(hours=6))

    def get_date_str(row):
        ems = row.get("entry_ms")
        if ems and not pd.isna(ems) and ems > 0:
            return datetime.fromtimestamp(ems / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        t = str(row.get("time") or "")
        if len(t) >= 5 and "-" in t:
            try:
                mm, dd = int(t[:2]), int(t[3:5])
                hh = int(t[6:8]) if len(t) >= 8 else 0
                mi = int(t[9:11]) if len(t) >= 11 else 0
                dt_bdt = datetime(now_utc.year, mm, dd, hh, mi, tzinfo=BDT)
                return dt_bdt.astimezone(timezone.utc).strftime("%Y-%m-%d")
            except Exception:
                pass
        return now_utc.strftime("%Y-%m-%d")

    if not df.empty:
        df["date_str"] = df.apply(get_date_str, axis=1)
    else:
        df["date_str"] = []

    today_str = now_utc.strftime("%Y-%m-%d")
    active_mask = df["status"].str.upper().isin(["OPEN", "EMERGENCY_UNPROTECTED"]) if not df.empty else pd.Series([], dtype=bool)
    active_count = int(active_mask.sum()) if not df.empty else 0

    today_mask = (df["date_str"] == today_str) if not df.empty else pd.Series([], dtype=bool)
    today_trades = int(today_mask.sum()) if not df.empty else 0

    today_closed = df[today_mask & (df["status"].str.upper() == "CLOSED")] if not df.empty else pd.DataFrame()
    today_pnl = float(today_closed["pnl"].sum()) if not today_closed.empty else 0.0

    tp_closed = today_closed[today_closed["result"].str.upper() == "TP"] if not today_closed.empty else pd.DataFrame()
    tp_count = len(tp_closed)
    tp_usdt = float(tp_closed["pnl"].sum()) if not tp_closed.empty else 0.0

    sl_closed = today_closed[today_closed["result"].str.upper() == "SL"] if not today_closed.empty else pd.DataFrame()
    sl_count = len(sl_closed)
    sl_usdt = float(sl_closed["pnl"].sum()) if not sl_closed.empty else 0.0

    days_list = []
    trades_by_day = {}
    for i in range(7):
        d = now_utc - timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        lbl = "Today" if i == 0 else ("Yesterday" if i == 1 else d.strftime("%a, %d %b"))
        day_trades_df = df[df["date_str"] == ds] if not df.empty else pd.DataFrame()
        day_pnl = float(day_trades_df["pnl"].sum()) if not day_trades_df.empty else 0.0
        days_list.append({
            "date": ds,
            "label": f"{lbl} ({d.strftime('%d %b')})",
            "trades_count": len(day_trades_df),
            "pnl": round(day_pnl, 2)
        })
        trades_by_day[ds] = clean(day_trades_df) if not day_trades_df.empty else []

    return {
        "summary": {
            "balance": round(tot_bal, 2),
            "available": round(av_bal, 2),
            "today_pnl": round(today_pnl, 2),
            "today_trades": today_trades,
            "active_positions": active_count,
            "tp_count": tp_count,
            "tp_usdt": round(tp_usdt, 2),
            "sl_count": sl_count,
            "sl_usdt": round(sl_usdt, 2),
        },
        "days": days_list,
        "trades_by_day": trades_by_day
    }


# ------------------------------------------------------------------ health
def tail_issues(name, keep=6):
    p = config.LOG_DIR / name
    if not p.exists():
        return []
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 80000))
            lines = f.read().decode("utf-8", errors="ignore").splitlines()
    except Exception:
        return []
    bad = [l for l in lines if "| ERROR |" in l or "| WARNING |" in l or "| CRITICAL |" in l]
    return [f"[{name}] {l}" for l in bad[-keep:]]


@app.get("/api/debug_status")
def debug_status():
    p = config.LOG_DIR / "collector.log"
    txt = ""
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read()[-3000:]
        except Exception as e:
            txt = f"read error: {e}"

    b_test = ""
    try:
        import requests
        hdrs = {}
        if config.API_KEY:
            hdrs["X-MBX-APIKEY"] = config.API_KEY
        r = requests.get(config.FAPI_BASE + "/fapi/v1/time", headers=hdrs, timeout=5)
        b_test = f"HTTP {r.status_code}: {r.text[:50]}"
    except Exception as be:
        b_test = f"Binance time error: {be}"

    return {"collector_log": txt, "binance_test": b_test}


def pct_status(p):
    return "ok" if p >= 0.95 else ("warn" if p >= 0.6 else "bad")


@app.get("/api/health")
def health():
    con = db()
    now = time.time()
    items = []
    universe = [r[0] for r in con.execute("SELECT symbol FROM universe")] if has_table(con, "universe") else []
    n = len(universe)

    for iv in config.INTERVALS:
        ms = config.INTERVAL_MS[iv]
        exp = int(((now - 120) * 1000) // ms) * ms - ms
        rows = con.execute("SELECT symbol, MAX(open_time) FROM klines WHERE interval=? GROUP BY symbol", (iv,)).fetchall()
        last = {s: t for s, t in rows}
        ok = sum(1 for s in universe if last.get(s, 0) >= exp)
        latest = max(last.values()) if last else None
        st = pct_status(ok / n if n else 0)
        items.append({
            "key": f"kline_{iv}", "name": f"Candle {iv}", "status": st,
            "detail": f"{ok}/{n} symbol fresh | latest open {fmt(latest)}",
            "hint": "" if st == "ok" else "Collector bondho/atke. python main.py restart korun, tarpor Fix: Gap fill.",
            "fix": None if st == "ok" else "backfill"})

    extra = [("premium", "Mark/Funding (15m)", "premium", "ts", 1500),
             ("open_interest", "Open Interest (15m)", "open_interest", "ts", 1500),
             ("oi_hist", "OI History (1h)", "oi_hist", "ts", 7800),
             ("funding_hist", "Funding History", "funding_hist", "funding_time", 30000),
             ("ls_ratio", "Long/Short Ratio", "ls_ratio", "ts", 7800),
             ("taker_ratio", "Taker Ratio", "taker_ratio", "ts", 11000),
             ("exchange_info", "Exchange Info", "exchange_info", "updated_at", 25000)]
    for key, name, tbl, col, maxage in extra:
        try:
            last = con.execute(f"SELECT MAX({col}) FROM {tbl}").fetchone()[0]
        except Exception:
            last = None
        age = (now - last / 1000) if last else 1e9
        st = "ok" if age <= maxage else ("warn" if age <= 3 * maxage else "bad")
        items.append({
            "key": key, "name": name, "status": st,
            "detail": f"latest {fmt(last)}",
            "hint": "" if st == "ok" else "Collector er extra job atke ba collector bondho. python main.py restart korun.",
            "fix": None})

    # daily scanner
    exp_date = datetime.fromtimestamp(int(now // 86400) * 86400, tz=BDT).strftime("%Y-%m-%d")
    sd, cnt, ca = None, 0, None
    if has_table(con, "watchlist"):
        sd = con.execute("SELECT MAX(scan_date) FROM watchlist").fetchone()[0]
        if sd:
            cnt, ca = con.execute("SELECT COUNT(*), MAX(created_at) FROM watchlist WHERE scan_date=?", (sd,)).fetchone()
    st = "bad" if not sd else ("ok" if sd == exp_date and cnt >= 10 else "warn")
    items.append({
        "key": "scanner", "name": "Daily Scanner (06:02)", "status": st,
        "detail": f"scan_date {sd} | {cnt} symbol | run {fmt(ca)}",
        "hint": "" if st == "ok" else "Aajker scan hoyni ba symbol kom. Collector data thik thakle Fix: Daily scan.",
        "fix": None if st == "ok" else "scan"})

    # hourly ranker
    rh, rc, rca = None, 0, None
    if has_table(con, "ranking"):
        rh = con.execute("SELECT MAX(run_hour) FROM ranking").fetchone()[0]
        if rh:
            rc, rca = con.execute("SELECT COUNT(*), MAX(created_at) FROM ranking WHERE run_hour=?", (rh,)).fetchone()
    age = (now - rca / 1000) if rca else 1e9
    st = "ok" if age <= 4200 and rc > 0 else ("warn" if age <= 8000 else "bad")
    items.append({
        "key": "ranker", "name": "Hourly Ranker (:12)", "status": st,
        "detail": f"run_hour {rh} | top {rc} | run {fmt(rca)}",
        "hint": "" if st == "ok" else "Scheduler (python scanner_main.py) bondho ba 1h candle nai. Fix: Hourly rank.",
        "fix": None if st == "ok" else "rank"})

    # Signal engine health
    sig_last = None
    if has_table(con, "signals"):
        sig_r = con.execute("SELECT MAX(time) FROM signals").fetchone()
        sig_last = sig_r[0] if sig_r else None
    items.append({
        "key": "signal", "name": "Signal Engine (15m)",
        "status": "idle" if not sig_last else "ok",
        "detail": f"latest signal: {sig_last}" if sig_last else "waiting for 15m signals",
        "hint": "", "fix": None
    })

    # Trader / Executor process health
    trader_log_path = config.LOG_DIR / "trader.log"
    trader_mtime = trader_log_path.stat().st_mtime if trader_log_path.exists() else None
    trader_age = (now - trader_mtime) if trader_mtime else 1e9
    if not trader_mtime:
        ex_st = "idle"
        ex_detail = "trader.log not found (trade_main.py chalu hoyni)"
        ex_hint = "python trade_main.py chalan"
    elif trader_age <= 1200:
        ex_st = "ok"
        ex_detail = f"active | last cycle {int(trader_age // 60)}m ago"
        ex_hint = ""
    elif trader_age <= 3600:
        ex_st = "warn"
        ex_detail = f"delayed | last cycle {int(trader_age // 60)}m ago"
        ex_hint = "Trader loop slow ba delayed"
    else:
        ex_st = "bad"
        ex_detail = f"stale | last cycle {int(trader_age // 60)}m ago"
        ex_hint = "Trader bondho ba crash koreche. python trade_main.py restart korun."

    items.append({
        "key": "executor", "name": "Trader Engine",
        "status": ex_st,
        "detail": ex_detail,
        "hint": ex_hint, "fix": None
    })

    # Alert if any emergency unprotected position exists
    if has_table(con, "positions"):
        unprot = con.execute("SELECT COUNT(*) FROM positions WHERE status='EMERGENCY_UNPROTECTED'").fetchone()[0]
        if unprot > 0:
            items.insert(0, {
                "key": "emergency_pos",
                "name": "🚨 UNPROTECTED POSITIONS",
                "status": "bad",
                "detail": f"{unprot} position(s) OPEN without SL!",
                "hint": "trade_engine reconcile dekhun ba manually SL set korun!",
                "fix": None
            })

    con.close()

    errs = tail_issues("collector.log") + tail_issues("scanner.log") + tail_issues("trader.log")
    return {"items": items, "errors": errs, "jobs": JOBS, "now": int(now * 1000)}


# ------------------------------------------------------------------ safe fix
@app.post("/api/fix/{action}")
def fix(action: str, request: Request):
    check_auth(request)
    if action not in ("scan", "rank", "backfill", "extra", "report"):
        raise HTTPException(400, "unknown action")
    if JOBS.get(action, {}).get("running"):
        return {"ok": False, "msg": "already running"}

    def work():
        JOBS[action] = {"running": True, "msg": "running...", "started": int(time.time() * 1000)}
        try:
            if action == "report":
                from app.notifier import send_hourly_report
                send_hourly_report()
            elif action == "scan":
                from app.scanner import run_scan
                run_scan(wait=False)
            elif action == "rank":
                from app.ranker import run_rank
                run_rank(wait=False)
            elif action == "extra":
                from app.binance_client import BinanceClient
                from app.storage import Storage
                from app.extra import job_hourly, job_fast
                c = BinanceClient()
                s = Storage()
                con = db()
                syms = [r[0] for r in con.execute("SELECT symbol FROM universe")] if has_table(con, "universe") else []
                con.close()
                if syms:
                    job_fast(c, s, syms, full=True)
                    job_hourly(c, s, syms, full=False)
            else:
                from app.binance_client import BinanceClient
                from app.storage import Storage
                from app.collector import backfill_symbols, fetch_universe
                c = BinanceClient()
                s = Storage()
                con = db()
                syms = [r[0] for r in con.execute("SELECT symbol FROM universe")] if has_table(con, "universe") else []
                con.close()
                if not syms:
                    syms = fetch_universe(c, s)
                backfill_symbols(c, s, syms, True)
            JOBS[action] = {"running": False, "msg": "done", "finished": int(time.time() * 1000)}
        except Exception as e:
            JOBS[action] = {"running": False, "msg": f"error: {e}", "finished": int(time.time() * 1000)}

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "msg": "started"}


if __name__ == "__main__":
    print("Open: http://localhost:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")