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
            # 1. Candidate Symbols: Direct Watchlist (Top 20 high-volume coins) + Hourly Ranking
            sd = con.execute("SELECT MAX(scan_date) FROM watchlist").fetchone()[0]
            candidates = []
            if sd:
                wl_df = pd.read_sql_query(
                    "SELECT w.symbol, w.rank, COALESCE(u.quote_volume_24h, 0.0) as quote_vol_24h "
                    "FROM watchlist w "
                    "LEFT JOIN universe u ON w.symbol = u.symbol "
                    "WHERE w.scan_date=? ORDER BY w.score DESC LIMIT 25", con, params=(sd,))
                candidates = wl_df.to_dict("records")

            rh = con.execute("SELECT MAX(run_hour) FROM ranking").fetchone()[0]
            if rh:
                rk_df = pd.read_sql_query(
                    "SELECT symbol, rank, quote_vol_24h FROM ranking WHERE run_hour=? ORDER BY rank",
                    con, params=(rh,))
                seen = {c["symbol"] for c in candidates}
                for r in rk_df.to_dict("records"):
                    if r["symbol"] not in seen:
                        candidates.append(r)

            # Filter candidates with min volume >= $15M
            candidates = [c for c in candidates if float(c.get("quote_vol_24h", 0)) >= tc.MIN_VOL_24H][:20]
            if not candidates:
                log.warning("Kono active candidate symbol nai. Scanner chalu ache?")
                return

            # Risk Limit: Daily drawdown / loss circuit breaker
            day_cutoff = int(time.time() * 1000) - 24 * 3600 * 1000
            daily_loss = con.execute(
                "SELECT COALESCE(SUM(pnl), 0) FROM positions WHERE status='CLOSED' AND closed IS NOT NULL AND entry_ms>=?",
                (day_cutoff,)).fetchone()[0]
            max_daily_loss = getattr(tc, "MAX_DAILY_LOSS_USDT", 150.0)
            if daily_loss <= -max_daily_loss:
                log.error(f"RISK LIMIT HIT: 24h net PnL is {daily_loss:.2f} USDT (limit: -{max_daily_loss} USDT). Trading cycle suspended!")
                return

            # Risk Limit: Available balance check
            if not self.dry and self.dc:
                try:
                    av_bal, _ = self.dc.balance()
                    min_bal = getattr(tc, "MIN_AVAILABLE_BALANCE", 50.0)
                    if av_bal < min_bal:
                        log.warning(f"RISK LIMIT: Available balance {av_bal:.2f} USDT < min required {min_bal:.2f} USDT. Skipping cycle.")
                        return
                except Exception as e:
                    log.warning(f"Failed to check balance risk limit: {e}")

            syms = [c["symbol"] for c in candidates]
            ph = ",".join("?" * len(syms))
            n = 0
            for _ in range(12):
                n = con.execute(f"SELECT COUNT(DISTINCT symbol) FROM klines WHERE interval='15m' "
                                f"AND open_time=? AND symbol IN ({ph})", [exp_open] + syms).fetchone()[0]
                if n >= 0.5 * len(syms):
                    break
                time.sleep(10)

            if n < 0.4 * len(syms):
                log.warning(f"Data not ready: only {n}/{len(syms)} symbols have 15m candle. Skipping trading cycle.")
                return

            open_syms = {r[0] for r in con.execute("SELECT symbol FROM positions WHERE status IN ('OPEN', 'EMERGENCY_UNPROTECTED')")}
            cutoff = int(time.time() * 1000) - tc.COOLDOWN_HOURS * 3600 * 1000
            cool = {r[0] for r in con.execute("SELECT symbol FROM positions WHERE entry_ms>=?", (cutoff,))}
            n_open = len(open_syms)
            checked = nsig = taken = 0

            for r in candidates:
                sym = r["symbol"]
                rnk = r.get("rank", 99)
                vol_24h = float(r.get("quote_vol_24h", 0))
                if sym in open_syms or sym in cool or vol_24h < tc.MIN_VOL_24H:
                    continue
                df = pd.read_sql_query(
                    "SELECT open_time, open, high, low, close, volume, taker_buy_base FROM klines "
                    "WHERE symbol=? AND interval='15m' AND open_time<=? "
                    "ORDER BY open_time DESC LIMIT 120", con, params=(sym, exp_open))
                df = df.sort_values("open_time").reset_index(drop=True)
                if df.empty or int(df["open_time"].iloc[-1]) != exp_open or len(df) < 50:
                    continue
                # Missing historical candle gap detection
                if len(df) > 1 and (df["open_time"].diff().iloc[1:] > MS15).any():
                    log.warning(f"{sym}: 15m candle gap detected, skipping entry")
                    continue
                checked += 1

                # INDEPENDENT DIRECTION: Evaluate BOTH LONG and SHORT setups on 15m price action!
                ev_long = evaluate(df, "LONG")
                ev_short = evaluate(df, "SHORT")

                ev = None
                side = None
                if ev_long and ev_long.get("hits") and ev_short and ev_short.get("hits"):
                    ev, side = (ev_long, "LONG") if abs(ev_long["close"] - df["close"].iloc[-2]) >= 0 else (ev_short, "SHORT")
                elif ev_long and ev_long.get("hits"):
                    ev, side = ev_long, "LONG"
                elif ev_short and ev_short.get("hits"):
                    ev, side = ev_short, "SHORT"

                if not ev or not side or not ev.get("hits"):
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
                    self.add_sig(con, sym, name, side, rnk, close, sl0, tp0, atr,
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
                    self.add_sig(con, sym, name, side, rnk, close, sl0, tp0, atr,
                                 "DRY", f"spread {spread:.3f}% | {note}")
                    log.info(f"DRY SIGNAL {sym} {side} {name} | {note}")
                    n_open += 1
                    continue

                ok, msg = self.execute(sym, side, atr, name, mid)
                self.add_sig(con, sym, name, side, rnk, close, sl0, tp0, atr,
                             "TAKEN" if ok else "FAILED", msg if not ok else note)
                if ok:
                    taken += 1
                    n_open += 1
                    open_syms.add(sym)
                else:
                    log.error(f"FAILED {sym} {side} {name}: {msg}")
            log.info(f"Cycle done | candle {bdt(exp_open)} | checked={checked} "
                     f"signals={nsig} taken={taken} open={n_open}")
        finally:
            con.close()

    # ------------------------------------------------------------ execute
    def try_cond(self, sym, side, otype, trigger):
        for attempt in range(3):
            try:
                self.dc.conditional(sym, side, otype, trigger)
                return True
            except Exception as e:
                log.warning(f"{otype} {sym} @ {trigger} attempt {attempt + 1}/3 failed: {e}")
                time.sleep(1.0)
        return False

    def emergency_close(self, sym, side, amt):
        cs = "SELL" if side == "LONG" else "BUY"
        for att in range(3):
            try:
                log.warning(f"Emergency close attempt {att + 1}/3 for {sym} {cs} qty {amt}")
                self.dc.market(sym, cs, Decimal(str(amt)), reduce=True)
                time.sleep(1.0)
                pos = [p for p in self.dc.positions() if p["symbol"] == sym]
                rem = abs(float(pos[0]["positionAmt"])) if pos else 0.0
                if rem == 0:
                    log.info(f"Emergency close successful for {sym}")
                    return True
                amt = rem
            except Exception as e:
                log.error(f"Emergency close error {sym} attempt {att + 1}: {e}")
                time.sleep(1.0)
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
        cid = f"ent_{sym}_{int(time.time() * 1000)}"
        order = None
        try:
            order = self.dc.market(sym, bs, q, client_order_id=cid)
        except Exception as e:
            log.warning(f"{sym}: Market order exception ({e}). Checking exchange position...")
            time.sleep(1.0)
            try:
                pos = [p for p in self.dc.positions() if p["symbol"] == sym]
                amt_check = abs(float(pos[0]["positionAmt"])) if pos else 0.0
                if amt_check > 0:
                    log.info(f"{sym}: Recovered position ({amt_check}) despite market order error")
                    order = {"avgPrice": float(pos[0].get("entryPrice") or mid), "executedQty": amt_check}
                else:
                    return False, f"market order: {e}"
            except Exception as pos_e:
                return False, f"market order: {e} (pos check error: {pos_e})"

        time.sleep(0.5)
        entry = float(order.get("avgPrice") or 0)
        pos = [p for p in self.dc.positions() if p["symbol"] == sym]
        amt = abs(float(pos[0]["positionAmt"])) if pos else 0.0
        if entry <= 0 and pos:
            entry = float(pos[0]["entryPrice"])
        if amt == 0:
            return False, "order er por position paini"
        if entry <= 0:
            entry = mid

        k = 1 if side == "LONG" else -1
        sl = rt(entry - k * tc.SL_ATR * atr, tick)
        tp = rt(entry + k * tc.TP_ATR * atr, tick)
        if not self.try_cond(sym, cs, "STOP_MARKET", sl):
            log.error(f"{sym}: SL boshate parlam na! Attempting emergency close...")
            if self.emergency_close(sym, side, amt):
                log.info(f"{sym}: Position successfully closed due to SL failure")
                return False, "SL order fail, position emergency close kora holo"
            else:
                log.critical(f"{sym}: EMERGENCY CLOSE FAILED! Position is OPEN without SL! Tracking as EMERGENCY_UNPROTECTED")
                con = self.db()
                con.execute("INSERT INTO positions (time,symbol,strategy,side,entry,sl,tp,qty,status,entry_ms) "
                            "VALUES (?,?,?,?,?,?,?,?,'EMERGENCY_UNPROTECTED',?)",
                            (bdt(), sym, name, side, entry, sl, tp, amt, t_before))
                con.commit()
                con.close()
                return False, "CRITICAL: SL fail and emergency close fail! Tracked as EMERGENCY_UNPROTECTED"

        tp_ok = self.try_cond(sym, cs, "TAKE_PROFIT_MARKET", tp)
        if not tp_ok:
            log.warning(f"{sym}: TP boshe ni, shudhu SL ache")

        con = self.db()
        cur = con.cursor()
        cur.execute("INSERT INTO positions (time,symbol,strategy,side,entry,sl,tp,qty,status,entry_ms) "
                    "VALUES (?,?,?,?,?,?,?,?,'OPEN',?)",
                    (bdt(), sym, name, side, entry, sl, tp, amt, t_before))
        pos_id = cur.lastrowid
        con.commit()
        con.close()
        log.info(f"OPENED {sym} {side} {name} | entry {entry} SL {sl} TP {tp} qty {amt}"
                 f"{'' if tp_ok else ' (TP missing)'}")
        try:
            from .notifier import send_trade_opened_alert
            send_trade_opened_alert(
                pid=pos_id,
                sym=sym,
                side=side,
                strat=name,
                entry=entry,
                sl=sl,
                tp=tp,
                dry=self.dry
            )
        except Exception as te:
            log.warning(f"Telegram alert error on open: {te}")
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
            rows = con.execute("SELECT id, symbol, side, entry, sl, tp, entry_ms, status FROM positions "
                               "WHERE status IN ('OPEN', 'EMERGENCY_UNPROTECTED')").fetchall()
            try:
                pos = self.dc.positions()
            except Exception as e:
                log.warning(f"positions fetch failed: {e}")
                return
            amt = {p["symbol"]: float(p["positionAmt"]) for p in pos}
            tracked = {r[1] for r in rows}
            for s, a in amt.items():
                if a != 0 and s not in tracked:
                    log.warning(f"RECONCILE: Untracked position found on Binance: {s} amt={a}")
                    has_sl = False
                    try:
                        open_ords = (self.dc.open_orders(s) or []) + (self.dc.algo_open_orders(s) or [])
                        has_sl = any(o.get("type") in ("STOP_MARKET", "STOP") for o in open_ords)
                    except Exception as e:
                        log.warning(f"Could not inspect orders for {s}: {e}")

                    side = "LONG" if a > 0 else "SHORT"
                    pos_info = [p for p in pos if p["symbol"] == s]
                    ent_px = float(pos_info[0].get("entryPrice") or 0.0) if pos_info else 0.0
                    st = "OPEN" if has_sl else "EMERGENCY_UNPROTECTED"
                    con.execute(
                        "INSERT INTO positions (time,symbol,strategy,side,entry,sl,tp,qty,status,entry_ms) "
                        "VALUES (?,?,'UNTRACKED_RECOVERED',?,?,0,0,?,?,?)",
                        (bdt(), s, side, ent_px, abs(a), st, int(time.time() * 1000))
                    )
                    con.commit()
                    log.info(f"Reconciled and tracked {s} into positions DB as {st}")
            for row in rows:
                pid, sym, side, entry, sl, tp, ems, status = row
                cur_amt = abs(amt.get(sym, 0.0))
                if cur_amt == 0:
                    self.close_row(con, pid, sym, side, entry, sl, tp, ems)
                    continue

                if status == "EMERGENCY_UNPROTECTED":
                    cs = "SELL" if side == "LONG" else "BUY"
                    log.warning(f"Reconcile handling EMERGENCY_UNPROTECTED position for {sym}...")
                    if sl > 0 and self.try_cond(sym, cs, "STOP_MARKET", sl):
                        con.execute("UPDATE positions SET status='OPEN' WHERE id=?", (pid,))
                        con.commit()
                        log.info(f"{sym}: Successfully protected emergency position with SL {sl}")
                    else:
                        log.critical(f"{sym}: sl <= 0 or cond order failed, trying emergency close...")
                        if self.emergency_close(sym, side, cur_amt):
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
        try:
            from .notifier import send_trade_closed_alert
            send_trade_closed_alert(
                pid=pid,
                sym=sym,
                side=side,
                res=res,
                entry=entry,
                ex=ex,
                net=net,
                fee=fee,
                dry=self.dry
            )
        except Exception as te:
            log.warning(f"Telegram alert error on close: {te}")