import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

import pandas as pd

from . import config, trade_config as tc
from .binance_client import BinanceClient
from .demo_client import DemoClient, DemoError
from .strategies import evaluate
from .trade_log import log

BDT = timezone(timedelta(hours=6))
MS15 = 900_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT, symbol TEXT, strategy TEXT, side TEXT, rank INTEGER,
    close REAL, sl REAL, tp REAL, atr REAL, action TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT, symbol TEXT, strategy TEXT, side TEXT,
    entry REAL, sl REAL, tp REAL, qty REAL, status TEXT,
    exit REAL, pnl REAL, fee REAL, result TEXT, closed TEXT, entry_ms INTEGER
);
"""


def bdt(ms=None):
    t = ms / 1000 if ms else time.time()
    return datetime.fromtimestamp(t, tz=BDT).strftime("%m-%d %H:%M")


def rt(x, tick):
    return float((Decimal(str(x)) / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick)


class Engine:
    def __init__(self, dry=False):
        self.dry = dry
        self.pub = BinanceClient()
        self.dc = None if dry else DemoClient()
        self.lock = threading.Lock()
        self.lev_set = set()
        con = self.db()
        con.executescript(SCHEMA)
        con.commit()
        con.close()

    def db(self):
        return sqlite3.connect(str(config.DB_PATH), timeout=30)

    # ------------------------------------------------------------ helpers
    def book(self, sym):
        d = self.pub.get("/fapi/v1/depth", {"symbol": sym, "limit": 5})
        return float(d["bids"][0][0]), float(d["asks"][0][0])

    def funding(self, con, sym):
        r = con.execute("SELECT funding_rate FROM premium WHERE symbol=? "
                        "ORDER BY ts DESC LIMIT 1", (sym,)).fetchone()
        return float(r[0]) if r else None

    def info(self, sym):
        con = self.db()
        r = con.execute("SELECT tick_size, step_size, min_qty, min_notional "
                        "FROM exchange_info WHERE symbol=?", (sym,)).fetchone()
        con.close()
        return r

    def add_sig(self, con, sym, name, side, rank, close, sl, tp, atr, action, note):
        con.execute("INSERT INTO signals (time,symbol,strategy,side,rank,close,sl,tp,atr,action,note) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (bdt(), sym, name, side, int(rank), close, sl, tp, atr, action, note))
        con.commit()

    # ------------------------------------------------------------ check / startup
    def check(self):
        print("Demo base      :", tc.DEMO_BASE)
        print("Time offset ms :", self.dc.offset)
        av, bal = self.dc.balance()
        print(f"USDT balance   : {bal:.2f} (available {av:.2f})")
        dual = self.dc.dual_mode()
        print("Hedge mode     :", dual, "(One-way hote hobe)" if dual else "(ok)")
        pos = [p for p in self.dc.positions() if float(p["positionAmt"]) != 0]
        print("Open positions :", [(p["symbol"], p["positionAmt"]) for p in pos])
        bid, ask = self.book("BTCUSDT")
        print(f"Public depth   : BTCUSDT bid {bid} ask {ask}")
        print("OK. Key + demo connection thik ache.")

    def startup(self):
        mode = "DRY (order hobe na)" if self.dry else "DEMO ORDER"
        log.info(f"Trader start | mode={mode} | {tc.NOTIONAL_USDT} USDT x{tc.LEVERAGE} | "
                 f"max {tc.MAX_OPEN} | SL {tc.SL_ATR} ATR / TP {tc.TP_ATR} ATR")
        if not self.dry:
            if self.dc.dual_mode():
                try:
                    self.dc.set_one_way()
                    log.info("Position mode One-way kora holo")
                except DemoError as e:
                    raise RuntimeError(f"Hedge mode off korte parlam na: {e}. "
                                       "Demo account e Position Mode = One-way korun.")
            self.reconcile(startup=True)

    # ------------------------------------------------------------ cycle
    def cycle(self):
        with self.lock:
            try:
                self._cycle()
            except Exception:
                log.exception("Cycle failed")

    def _cycle(self):
        exp_open = (int(time.time() * 1000) // MS15 - 1) * MS15
        if not self.dry:
            self.reconcile()
        con = self.db()
        try:
            rh = con.execute("SELECT MAX(run_hour) FROM ranking").fetchone()[0]
            if not rh:
                log.warning("Ranking nai. Scheduler (scanner_main.py) chalu ache?")
                return
            rk = pd.read_sql_query(
                "SELECT symbol, rank, side, quote_vol_24h, created_at FROM ranking "
                "WHERE run_hour=? ORDER BY rank", con, params=(rh,))
            age = (time.time() * 1000 - rk["created_at"].max()) / 60000
            if age > tc.RANK_MAX_AGE_MIN:
                log.warning(f"Ranking purono ({age:.0f} min, {rh}). Cycle skip.")
                return
            syms = rk["symbol"].tolist()
            ph = ",".join("?" * len(syms))
            for _ in range(12):
                n = con.execute(f"SELECT COUNT(DISTINCT symbol) FROM klines WHERE interval='15m' "
                                f"AND open_time=? AND symbol IN ({ph})", [exp_open] + syms).fetchone()[0]
                if n >= 0.8 * len(syms):
                    break
                time.sleep(10)

            open_syms = {r[0] for r in con.execute("SELECT symbol FROM positions WHERE status='OPEN'")}
            cutoff = int(time.time() * 1000) - tc.COOLDOWN_HOURS * 3600 * 1000
            cool = {r[0] for r in con.execute("SELECT symbol FROM positions WHERE entry_ms>=?", (cutoff,))}
            n_open = len(open_syms)
            checked = nsig = taken = 0

            for r in rk.itertuples():
                sym, side = r.symbol, r.side
                if sym in open_syms or sym in cool or r.quote_vol_24h < tc.MIN_VOL_24H:
                    continue
                df = pd.read_sql_query(
                    "SELECT open_time, open, high, low, close, volume, taker_buy_base FROM klines "
                    "WHERE symbol=? AND interval='15m' AND open_time<=? "
                    "ORDER BY open_time DESC LIMIT 120", con, params=(sym, exp_open))
                df = df.sort_values("open_time").reset_index(drop=True)
                if df.empty or int(df["open_time"].iloc[-1]) != exp_open:
                    continue
                checked += 1
                ev = evaluate(df, side)
                if not ev or not ev["hits"]:
                    continue
                nsig += 1
                name, detail = ev["hits"][0]
                close, atr = ev["close"], ev["atr"]
                k = 1 if side == "LONG" else -1
                sl0 = close - k * tc.SL_ATR * atr
                tp0 = close + k * tc.TP_ATR * atr
                note = detail + (" | also: " + ", ".join(h[0] for h in ev["hits"][1:])
                                 if len(ev["hits"]) > 1 else "")

                def skip(why):
                    self.add_sig(con, sym, name, side, r.rank, close, sl0, tp0, atr,
                                 "SKIP", f"{why} | {note}")
                    log.info(f"SKIP {sym} {side} {name}: {why}")

                if n_open >= tc.MAX_OPEN:
                    skip("max position")
                    continue
                fr = self.funding(con, sym)
                if fr is not None and abs(fr) > tc.MAX_FUNDING:
                    skip(f"funding {fr * 100:.3f}%")
                    continue
                try:
                    bid, ask = self.book(sym)
                except Exception as e:
                    skip(f"depth error: {e}")
                    continue
                mid = (bid + ask) / 2
                spread = (ask - bid) / mid * 100
                if spread > tc.MAX_SPREAD_PCT:
                    skip(f"spread {spread:.3f}%")
                    continue
                if abs(mid - close) > tc.MAX_CHASE_ATR * atr:
                    skip("price signal candle theke onek soreche")
                    continue

                if self.dry:
                    self.add_sig(con, sym, name, side, r.rank, close, sl0, tp0, atr,
                                 "DRY", f"spread {spread:.3f}% | {note}")
                    log.info(f"DRY SIGNAL {sym} {side} {name} | {note}")
                    n_open += 1
                    continue

                ok, msg = self.execute(sym, side, atr, name, mid)
                self.add_sig(con, sym, name, side, r.rank, close, sl0, tp0, atr,
                             "TAKEN" if ok else "FAILED", msg if not ok else note)
                if ok:
                    taken += 1
                    n_open += 1
                    open_syms.add(sym)
                else:
                    log.error(f"FAILED {sym} {side} {name}: {msg}")
            log.info(f"Cycle done | candle {bdt(exp_open)} | rank {rh} | checked={checked} "
                     f"signals={nsig} taken={taken} open={n_open}")
        finally:
            con.close()

    # ------------------------------------------------------------ execute
    def try_cond(self, sym, side, otype, trigger):
        for _ in range(2):
            try:
                self.dc.conditional(sym, side, otype, trigger)
                return True
            except Exception as e:
                log.error(f"{otype} {sym} @ {trigger} failed: {e}")
                time.sleep(1)
        return False

    def execute(self, sym, side, atr, name, mid):
        info = self.info(sym)
        if not info:
            return False, "exchange_info nai"
        tick, step, minq, minn = [Decimal(str(x)) for x in info]
        if tick <= 0 or step <= 0:
            return False, "bad tick/step"
        q = Decimal(str(tc.NOTIONAL_USDT)) / Decimal(str(mid))
        q = (q / step).to_integral_value(rounding=ROUND_DOWN) * step
        if q < minq or q * Decimal(str(mid)) < max(minn, Decimal("5")):
            return False, f"qty {q} choto (min qty {minq}, min notional {minn})"
        try:
            if sym not in self.lev_set:
                self.dc.set_margin(sym)
                self.dc.set_leverage(sym, tc.LEVERAGE)
                self.lev_set.add(sym)
        except Exception as e:
            return False, f"margin/leverage: {e}"

        t_before = int(time.time() * 1000)
        bs = "BUY" if side == "LONG" else "SELL"
        cs = "SELL" if side == "LONG" else "BUY"
        try:
            order = self.dc.market(sym, bs, q)
            time.sleep(1.0)
            entry = float(order.get("avgPrice") or 0)
            pos = [p for p in self.dc.positions() if p["symbol"] == sym]
            amt = abs(float(pos[0]["positionAmt"])) if pos else 0.0
            if entry <= 0 and pos:
                entry = float(pos[0]["entryPrice"])
        except Exception as e:
            return False, f"market order: {e}"
        if amt == 0:
            return False, "order er por position paini"
        if entry <= 0:
            entry = mid

        k = 1 if side == "LONG" else -1
        sl = rt(entry - k * tc.SL_ATR * atr, tick)
        tp = rt(entry + k * tc.TP_ATR * atr, tick)
        if not self.try_cond(sym, cs, "STOP_MARKET", sl):
            log.error(f"{sym}: SL boshate parlam na, position close kortechi")
            try:
                self.dc.market(sym, cs, Decimal(str(amt)), reduce=True)
            except Exception as e:
                log.critical(f"{sym}: EMERGENCY CLOSE FAILED: {e}. Manually dekhun!")
            return False, "SL order fail, position close kora holo"
        tp_ok = self.try_cond(sym, cs, "TAKE_PROFIT_MARKET", tp)
        if not tp_ok:
            log.warning(f"{sym}: TP boshe ni, shudhu SL ache")

        con = self.db()
        con.execute("INSERT INTO positions (time,symbol,strategy,side,entry,sl,tp,qty,status,entry_ms) "
                    "VALUES (?,?,?,?,?,?,?,?,'OPEN',?)",
                    (bdt(), sym, name, side, entry, sl, tp, amt, t_before))
        con.commit()
        con.close()
        log.info(f"OPENED {sym} {side} {name} | entry {entry} SL {sl} TP {tp} qty {amt}"
                 f"{'' if tp_ok else ' (TP missing)'}")
        return True, ""

    # ------------------------------------------------------------ reconcile
    def reconcile_job(self):
        if not self.lock.acquire(blocking=False):
            return
        try:
            self.reconcile()
        except Exception:
            log.exception("Reconcile failed")
        finally:
            self.lock.release()

    def reconcile(self, startup=False):
        if self.dry or not self.dc:
            return
        con = self.db()
        try:
            rows = con.execute("SELECT id, symbol, side, entry, sl, tp, entry_ms FROM positions "
                               "WHERE status='OPEN'").fetchall()
            try:
                pos = self.dc.positions()
            except Exception as e:
                log.warning(f"positions fetch failed: {e}")
                return
            amt = {p["symbol"]: float(p["positionAmt"]) for p in pos}
            if startup:
                tracked = {r[1] for r in rows}
                for s, a in amt.items():
                    if a != 0 and s not in tracked:
                        log.warning(f"Untracked demo position: {s} amt={a}")
            for (pid, sym, side, entry, sl, tp, ems) in rows:
                if sym not in amt or abs(amt[sym]) > 0:
                    continue
                self.close_row(con, pid, sym, side, entry, sl, tp, ems)
        finally:
            con.close()

    def close_row(self, con, pid, sym, side, entry, sl, tp, ems):
        try:
            tr = self.dc.user_trades(sym, ems - 2000)
        except Exception as e:
            log.warning(f"userTrades {sym}: {e}")
            return
        tr = [t for t in tr if int(t["time"]) >= ems - 2000]
        cside = "SELL" if side == "LONG" else "BUY"
        cl = [t for t in tr if t["side"] == cside]
        self.dc.cancel_conditionals(sym)       # baki SL/TP leg muche dei
        if not cl:
            if time.time() * 1000 - ems > 600000:
                con.execute("UPDATE positions SET status='CLOSED', result='UNKNOWN', closed=? WHERE id=?",
                            (bdt(), pid))
                con.commit()
                log.warning(f"{sym}: close trade pailam na, UNKNOWN mark kora holo")
            return
        q = sum(float(t["qty"]) for t in cl)
        ex = sum(float(t["price"]) * float(t["qty"]) for t in cl) / q
        realized = sum(float(t["realizedPnl"]) for t in tr)
        fee = sum(float(t["commission"]) for t in tr)
        net = realized - fee
        d_tp, d_sl = abs(ex - tp), abs(ex - sl)
        if d_tp < d_sl and d_tp <= 0.4 * abs(tp - entry):
            res = "TP"
        elif d_sl < d_tp and d_sl <= 0.6 * abs(entry - sl):
            res = "SL"
        else:
            res = "OTHER"
        con.execute("UPDATE positions SET status='CLOSED', exit=?, pnl=?, fee=?, result=?, closed=? "
                    "WHERE id=?", (ex, round(net, 4), round(fee, 4), res, bdt(), pid))
        con.commit()
        log.info(f"CLOSED {sym} {side} {res} | exit {ex:.6g} | net {net:+.4f} USDT (fee {fee:.4f})")