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
    lv = {}
    if has_table(con, "watchlist"):
        r = con.execute("SELECT side, support, resistance FROM watchlist "
                        "WHERE symbol=? ORDER BY scan_date DESC LIMIT 1", (symbol,)).fetchone()
        if r:
            lv = {"side": r[0], "support": r[1], "resistance": r[2]}
    con.close()
    if df.empty:
        return {"candles": []}

    df = df.sort_values("open_time").reset_index(drop=True)
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
            df = pd.read_sql_query("SELECT symbol, rank, side, score, trend, volume, oi, close FROM watchlist "
                                   "WHERE scan_date=? ORDER BY rank", con, params=(sd,))
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
    bad = [l for l in lines if "| ERROR |" in l or "| WARNING |" in l]
    return [f"[{name}] {l}" for l in bad[-keep:]]


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
    if action not in ("scan", "rank", "backfill"):
        raise HTTPException(400, "unknown action")
    if JOBS.get(action, {}).get("running"):
        return {"ok": False, "msg": "already running"}

    def work():
        JOBS[action] = {"running": True, "msg": "running...", "started": int(time.time() * 1000)}
        try:
            if action == "scan":
                from app.scanner import run_scan
                run_scan(wait=False)
            elif action == "rank":
                from app.ranker import run_rank
                run_rank(wait=False)
            else:
                from app.binance_client import BinanceClient
                from app.storage import Storage
                from app.collector import backfill_symbols
                con = db()
                syms = [r[0] for r in con.execute("SELECT symbol FROM universe")]
                con.close()
                backfill_symbols(BinanceClient(), Storage(), syms, True)
            JOBS[action] = {"running": False, "msg": "done", "finished": int(time.time() * 1000)}
        except Exception as e:
            JOBS[action] = {"running": False, "msg": f"error: {e}", "finished": int(time.time() * 1000)}

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "msg": "started"}


if __name__ == "__main__":
    print("Open: http://localhost:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")