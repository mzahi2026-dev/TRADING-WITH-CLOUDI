import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

import pandas as pd
from dotenv import load_dotenv

from . import config, trade_config as tc
from .binance_client import BinanceClient
from .demo_client import DemoClient, DemoError
from .trade_log import log

load_dotenv(config.BASE_DIR / ".env")

BDT = timezone(timedelta(hours=6))
MS15 = 900_000


def bdt(ms=None):
    t = ms / 1000 if ms else time.time()
    return datetime.fromtimestamp(t, tz=BDT).strftime("%m-%d %H:%M")


def rt(x, tick):
    return float((Decimal(str(x)) / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick)


class AIEngine:
    def __init__(self, dry=False):
        self.dry = dry
        self.pub = BinanceClient()
        self.dc = None if dry else DemoClient()
        self.api_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.model_name = getattr(tc, "AI_MODEL", "gemini-2.5-flash")
        self.ensure_schema()

    def db(self):
        return sqlite3.connect(str(config.DB_PATH), timeout=30)

    def ensure_schema(self):
        try:
            con = self.db()
            cols = [r[1] for r in con.execute("PRAGMA table_info(positions)").fetchall()]
            if "engine" not in cols:
                con.execute("ALTER TABLE positions ADD COLUMN engine TEXT DEFAULT 'RULE'")
            if "ai_thesis" not in cols:
                con.execute("ALTER TABLE positions ADD COLUMN ai_thesis TEXT")
            if "ai_confidence" not in cols:
                con.execute("ALTER TABLE positions ADD COLUMN ai_confidence INTEGER")
            con.commit()
            con.close()
        except Exception as e:
            log.warning(f"AI schema check: {e}")

    def get_active_ai_count(self, con):
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM positions WHERE UPPER(COALESCE(engine, 'RULE'))='AI' "
                "AND status IN ('OPEN', 'EMERGENCY_UNPROTECTED')"
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def get_recent_ai_symbols(self, con):
        """Symbols traded by AI within cooldown window (1 hour)."""
        cutoff_ms = int(time.time() * 1000) - int(tc.AI_REENTRY_COOLDOWN_HOURS * 3600 * 1000)
        try:
            rows = con.execute(
                "SELECT DISTINCT symbol FROM positions WHERE UPPER(COALESCE(engine, 'RULE'))='AI' "
                "AND entry_ms >= ?", (cutoff_ms,)
            ).fetchall()
            return {r[0] for r in rows if r[0]}
        except Exception:
            return set()

    def book(self, sym):
        d = self.pub.get("/fapi/v1/depth", {"symbol": sym, "limit": 5})
        return float(d["bids"][0][0]), float(d["asks"][0][0])

    def info(self, sym):
        try:
            d = self.pub.get("/fapi/v1/exchangeInfo", {})
            for s in d["symbols"]:
                if s["symbol"] == sym:
                    tick, step, minq, minn = None, None, None, None
                    for f in s["filters"]:
                        if f["filterType"] == "PRICE_FILTER":
                            tick = float(f["tickSize"])
                        elif f["filterType"] == "LOT_SIZE":
                            step = float(f["stepSize"])
                            minq = float(f["minQty"])
                        elif f["filterType"] == "MIN_NOTIONAL":
                            minn = float(f.get("notional") or 5)
                    return tick, step, minq, minn
        except Exception as e:
            log.warning(f"AI info failed for {sym}: {e}")
        return None

    def call_gemini(self, prompt: str) -> dict:
        """Call Gemini API via google-genai SDK with structured output or fallback."""
        if not self.api_key:
            log.warning("GEMINI_API_KEY missing in .env. Skipping AI market scan.")
            return {}

        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=self.api_key)
            # Request JSON structured output
            response = client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )
            raw_text = response.text or ""
            return json.loads(raw_text)
        except Exception as e:
            # Try fallback to gemini-2.5-flash or gemini-3.8-flash
            try:
                from google import genai
                client = genai.Client(api_key=self.api_key)
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                )
                txt = response.text or ""
                # Strip markdown code blocks if any
                if "```json" in txt:
                    txt = txt.split("```json")[1].split("```")[0].strip()
                elif "```" in txt:
                    txt = txt.split("```")[1].split("```")[0].strip()
                return json.loads(txt)
            except Exception as e2:
                log.error(f"Gemini API call failed: {e} | fallback: {e2}")
                return {}

    def cycle(self):
        """15m AI cycle: evaluate candidates, generate autonomous setup, execute order."""
        if not getattr(tc, "AI_ENABLED", True):
            return

        con = self.db()
        try:
            # 1. Check max open AI positions
            active_ai = self.get_active_ai_count(con)
            if active_ai >= tc.MAX_AI_OPEN:
                log.info(f"AI Engine: Max open positions reached ({active_ai}/{tc.MAX_AI_OPEN}). Waiting for exits.")
                return

            # 2. Check cooldown symbols
            recent_syms = self.get_recent_ai_symbols(con)

            # 3. Get candidate coins from Watchlist / Universe
            sd = con.execute("SELECT MAX(scan_date) FROM watchlist").fetchone()[0]
            candidates = []
            if sd:
                wl_df = pd.read_sql_query(
                    "SELECT w.symbol, w.rank, COALESCE(u.quote_volume_24h, 0.0) as quote_vol_24h "
                    "FROM watchlist w "
                    "LEFT JOIN universe u ON w.symbol = u.symbol "
                    "WHERE w.scan_date=? ORDER BY w.score DESC LIMIT 25", con, params=(sd,))
                candidates = wl_df.to_dict("records")

            # Filter min volume and exclude recent cooldown symbols
            candidates = [
                c for c in candidates
                if float(c.get("quote_vol_24h", 0)) >= tc.MIN_VOL_24H and c["symbol"] not in recent_syms
            ][:15]

            if not candidates:
                log.info("AI Engine: No eligible candidates after cooldown & volume filter.")
                return

            # 4. Build Market Context for Gemini
            market_data = []
            for c in candidates[:8]:  # analyze top 8 high-conviction candidates to keep prompt fast
                sym = c["symbol"]
                try:
                    bid, ask = self.book(sym)
                    mid = (bid + ask) / 2
                    # Fetch recent 15m klines from DB
                    k_df = pd.read_sql_query(
                        "SELECT open_time, open, high, low, close, volume FROM klines "
                        "WHERE symbol=? AND interval='15m' ORDER BY open_time DESC LIMIT 20",
                        con, params=(sym,)
                    )
                    if len(k_df) >= 10:
                        k_df = k_df.iloc[::-1]  # chronological order
                        closes = k_df["close"].tolist()
                        ema20 = pd.Series(closes).ewm(span=20, adjust=False).mean().iloc[-1]
                        ema50 = pd.Series(closes).ewm(span=50, adjust=False).mean().iloc[-1]
                        market_data.append({
                            "symbol": sym,
                            "current_price": mid,
                            "24h_volume_usd": float(c.get("quote_vol_24h", 0)),
                            "ema20": round(float(ema20), 6),
                            "ema50": round(float(ema50), 6),
                            "last_5_candles": [
                                {"c": round(row["close"], 6), "v": round(row["volume"], 2)}
                                for _, row in k_df.tail(5).iterrows()
                            ]
                        })
                except Exception as ex:
                    log.warning(f"AI context gather failed for {sym}: {ex}")

            if not market_data:
                log.warning("AI Engine: Could not assemble market data for candidates.")
                return

            # 5. Formulate Prompt
            prompt = f"""
You are the autonomous Senior Crypto Quant Analyst for Trading Cloudi.
Evaluate the following candidate coins on 15m timeframe and find at most ONE high-probability trade setup.

Capital & Risk Parameters:
- Max position size: ${tc.AI_NOTIONAL_USDT:.2f} USDT notional
- Stop Loss: Fixed ${tc.AI_SL_USDT:.2f} USDT loss risk
- Take Profit: Fixed ${tc.AI_TP_USDT:.2f} USDT reward target (1:2 Risk to Reward)
- Minimum confidence required: {tc.AI_MIN_CONFIDENCE}%

Candidates Data:
{json.dumps(market_data, indent=2)}

Rules:
1. Only choose a setup if there is a clear trend continuation, EMA pullback bounce, breakout retest, or liquidity trap.
2. If the market is choppy or unconvincing, set "has_trade": false.
3. Return ONLY a valid JSON object with these exact keys:
{{
  "has_trade": true,
  "symbol": "SYMBOLUSDT",
  "side": "LONG" or "SHORT",
  "strategy_name": "Clear strategy name",
  "confidence_score": 85,
  "thesis_bangla": "বাংলায় এন্ট্রির মূল যুক্তি ও বিশ্লেষণ (১-২ বাক্য)",
  "thesis_english": "English entry thesis (1-2 sentences)"
}}
"""
            log.info(f"AI Engine: Querying Gemini for {len(market_data)} candidate symbols...")
            ai_res = self.call_gemini(prompt)

            if not ai_res or not ai_res.get("has_trade"):
                log.info("AI Engine: Gemini analyzed candidates — no setup met conviction threshold.")
                return

            sym = ai_res.get("symbol")
            side = str(ai_res.get("side", "")).upper()
            conf = int(ai_res.get("confidence_score") or 0)
            strat = str(ai_res.get("strategy_name") or "Gemini Autonomous Price Action")
            thesis = str(ai_res.get("thesis_bangla") or ai_res.get("thesis_english") or "AI High-Conviction Setup")

            if conf < tc.AI_MIN_CONFIDENCE:
                log.info(f"AI Engine: Setup for {sym} {side} confidence {conf}% < {tc.AI_MIN_CONFIDENCE}%. Skipped.")
                return

            if side not in ("LONG", "SHORT") or not sym:
                log.warning(f"AI Engine: Invalid setup response from Gemini: {ai_res}")
                return

            log.info(f"AI Engine: 🌟 GEMINI SETUP FOUND! {sym} {side} (Conf: {conf}%) | {strat}")

            # 6. Execute Order
            self.execute_ai_trade(con, sym, side, strat, conf, thesis)

        except Exception as e:
            log.exception(f"AI Engine cycle failed: {e}")
        finally:
            con.close()

    def execute_ai_trade(self, con, sym: str, side: str, strat: str, conf: int, thesis: str):
        """Execute autonomous AI trade with fixed $15 SL and $30 TP."""
        info = self.info(sym)
        if not info:
            log.error(f"AI Execute: exchange_info not found for {sym}")
            return False
        tick, step, minq, minn = [Decimal(str(x)) for x in info]

        bid, ask = self.book(sym)
        mid = (bid + ask) / 2

        # Quantity calculation for $200 USDT notional
        q = Decimal(str(tc.AI_NOTIONAL_USDT)) / Decimal(str(mid))
        q = (q / step).to_integral_value(rounding=ROUND_DOWN) * step
        if q < minq or q * Decimal(str(mid)) < max(minn, Decimal("5")):
            log.error(f"AI Execute: Qty {q} too small for {sym}")
            return False

        if not self.dry and self.dc:
            try:
                self.dc.set_margin(sym)
                self.dc.set_leverage(sym, tc.LEVERAGE)
            except Exception as e:
                log.warning(f"AI Execute: margin/leverage error {sym}: {e}")

        # Exact SL and TP calculation for $15 loss and $30 profit
        # Loss = |Entry - SL| * Qty = 15.0 => delta_sl = 15.0 / Qty
        # Profit = |Entry - TP| * Qty = 30.0 => delta_tp = 30.0 / Qty
        delta_sl = float(Decimal(str(tc.AI_SL_USDT)) / q)
        delta_tp = float(Decimal(str(tc.AI_TP_USDT)) / q)

        if side == "LONG":
            sl_price = rt(mid - delta_sl, tick)
            tp_price = rt(mid + delta_tp, tick)
        else:
            sl_price = rt(mid + delta_sl, tick)
            tp_price = rt(mid - delta_tp, tick)

        if self.dry:
            log.info(f"DRY AI ORDER: {sym} {side} qty={q} entry={mid} SL={sl_price} TP={tp_price} (Conf: {conf}%)")
            return True

        bs = "BUY" if side == "LONG" else "SELL"
        cs = "SELL" if side == "LONG" else "BUY"
        cid = f"ai_ent_{sym}_{int(time.time() * 1000)}"

        try:
            order = self.dc.market(sym, bs, q, client_order_id=cid)
        except Exception as e:
            log.error(f"AI Execute: Market order failed for {sym}: {e}")
            return False

        time.sleep(0.5)
        entry = float(order.get("avgPrice") or mid)
        if entry <= 0:
            pos = [p for p in self.dc.positions() if p["symbol"] == sym]
            if pos:
                entry = float(pos[0].get("entryPrice") or mid)
            else:
                entry = mid

        # Place conditional SL & TP orders on Binance
        self.try_cond(sym, cs, "STOP_MARKET", sl_price)
        self.try_cond(sym, cs, "TAKE_PROFIT_MARKET", tp_price)

        entry_ms = int(time.time() * 1000)
        time_str = bdt(entry_ms)

        cur = con.cursor()
        cur.execute(
            "INSERT INTO positions (time, symbol, strategy, side, entry, sl, tp, qty, status, entry_ms, engine, ai_thesis, ai_confidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, 'AI', ?, ?)",
            (time_str, sym, strat, side, entry, sl_price, tp_price, float(q), entry_ms, thesis, conf)
        )
        pid = cur.lastrowid
        con.commit()

        log.info(f"AI Engine: ORDER PLACED! #{pid} {sym} {side} @ {entry} (SL={sl_price}, TP={tp_price})")

        # Send Telegram notification
        try:
            from .notifier import send_ai_trade_opened_alert
            send_ai_trade_opened_alert(
                pid=pid,
                sym=sym,
                side=side,
                thesis=thesis,
                confidence=conf,
                entry=entry,
                sl=sl_price,
                tp=tp_price,
                dry=self.dry
            )
        except Exception as te:
            log.warning(f"Telegram AI alert failed: {te}")

        return True

    def try_cond(self, sym, side, otype, trigger):
        for attempt in range(3):
            try:
                self.dc.conditional(sym, side, otype, trigger)
                return True
            except Exception as e:
                log.warning(f"AI conditional {otype} {sym} @ {trigger} attempt {attempt + 1}/3 failed: {e}")
                time.sleep(1.0)
        return False
