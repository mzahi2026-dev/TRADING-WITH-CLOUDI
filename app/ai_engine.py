import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

import numpy as np
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


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Welles Wilder Average Directional Index (ADX)."""
    if len(df) < period + 5:
        return pd.Series(dtype=float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
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


def rt(x, tick):
    return float((Decimal(str(x)) / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick)


class AIEngine:
    def __init__(self, dry=False):
        self.dry = dry
        self.pub = BinanceClient()
        self.dc = None if dry else DemoClient()
        self.api_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.model_name = getattr(tc, "AI_MODEL", "gemini-3.5-flash")
        self._genai_client = None
        if self.api_key:
            try:
                from google import genai
                self._genai_client = genai.Client(api_key=self.api_key)
            except Exception as ge:
                log.warning(f"AI Engine: Failed to pre-init GenAI client: {ge}")
        self.ensure_schema()

    def db(self):
        from .db import get_db
        return get_db()

    def market_db(self):
        con = sqlite3.connect(str(config.DB_PATH), timeout=20)
        con.row_factory = sqlite3.Row
        return con

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
            if "closed_ms" not in cols:
                con.execute("ALTER TABLE positions ADD COLUMN closed_ms INTEGER")
            if "tp1_hit" not in cols:
                con.execute("ALTER TABLE positions ADD COLUMN tp1_hit INTEGER DEFAULT 0")
            if "tp1_price" not in cols:
                con.execute("ALTER TABLE positions ADD COLUMN tp1_price REAL DEFAULT 0")
            if "original_qty" not in cols:
                con.execute("ALTER TABLE positions ADD COLUMN original_qty REAL DEFAULT 0")
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

    def get_active_and_recent_symbols(self, con):
        """Symbols currently open, traded within standard cooldown, or blacklisted due to SL hit within 12h."""
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - int(getattr(tc, "AI_REENTRY_COOLDOWN_HOURS", 2) * 3600 * 1000)
        loss_cutoff_ms = now_ms - int(getattr(tc, "AI_LOSS_COOLDOWN_HOURS", 12) * 3600 * 1000)
        blocked = set()
        try:
            # 1. Block any symbol currently OPEN or EMERGENCY_UNPROTECTED (AI or RULE)
            open_rows = con.execute(
                "SELECT DISTINCT symbol FROM positions WHERE status IN ('OPEN', 'EMERGENCY_UNPROTECTED')"
            ).fetchall()
            for r in open_rows:
                if r[0]:
                    blocked.add(r[0])

            # 2. Block any symbol traded recently within normal re-entry window
            recent_rows = con.execute(
                "SELECT DISTINCT symbol FROM positions WHERE entry_ms >= ?", (cutoff_ms,)
            ).fetchall()
            for r in recent_rows:
                if r[0]:
                    blocked.add(r[0])

            # 3. 12-Hour Loss Cooldown: Blacklist any symbol that closed with SL or negative PnL in last 12h
            cols = [col[1] for col in con.execute("PRAGMA table_info(positions)").fetchall()]
            closed_ts_expr = "COALESCE(closed_ms, entry_ms)" if "closed_ms" in cols else "entry_ms"
            loss_rows = con.execute(
                f"SELECT DISTINCT symbol FROM positions WHERE (result='SL' OR pnl < 0) AND {closed_ts_expr} >= ?",
                (loss_cutoff_ms,)
            ).fetchall()
            for r in loss_rows:
                if r[0]:
                    blocked.add(r[0])
                    log.info(f"AI Engine: Symbol {r[0]} is under 12h loss cooldown guard (recent SL hit).")

            return blocked
        except Exception as e:
            log.warning(f"AI Engine: active/recent symbols query error: {e}")
            return blocked

    def get_btc_regime(self, m_con):
        """Analyze BTCUSDT 1h trend & ADX to determine market regime (BULLISH, BEARISH, NEUTRAL, CHOP)."""
        try:
            btc_df = pd.read_sql_query(
                "SELECT open_time, open, high, low, close, volume FROM klines "
                "WHERE symbol='BTCUSDT' AND interval='1h' ORDER BY open_time DESC LIMIT 100",
                m_con
            )
            if len(btc_df) < 40:
                return "NEUTRAL", 0.0, 0.0, 50.0, 20.0
            btc_df = btc_df.iloc[::-1].reset_index(drop=True)
            c = btc_df["close"].astype(float)
            e20 = c.ewm(span=20, adjust=False).mean().iloc[-1]
            e50 = c.ewm(span=50, adjust=False).mean().iloc[-1]
            curr = c.iloc[-1]
            # RSI 14
            delta = c.diff()
            up = delta.clip(lower=0)
            dn = -delta.clip(upper=0)
            rs = up.ewm(alpha=1/14, adjust=False).mean() / dn.ewm(alpha=1/14, adjust=False).mean().replace(0, np.nan)
            rsi = float((100 - 100 / (1 + rs)).iloc[-1])
            adx_series = compute_adx(btc_df, 14)
            adx = float(adx_series.iloc[-1]) if not adx_series.empty and not pd.isna(adx_series.iloc[-1]) else 20.0

            if adx < getattr(tc, "ADX_MIN_THRESHOLD", 25.0):
                regime = "CHOP_RANGING"
            elif curr > e20 and e20 > e50 and rsi >= 48:
                regime = "BULLISH"
            elif curr < e20 and e20 < e50:
                regime = "BEARISH"
            elif curr < e50 or rsi < 42:
                regime = "BEARISH"
            else:
                regime = "NEUTRAL"
            return regime, curr, e50, rsi, adx
        except Exception as e:
            log.warning(f"AI Engine: BTC regime check error: {e}")
            return "NEUTRAL", 0.0, 0.0, 50.0, 20.0

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
        """Call Gemini API via google-genai SDK with multi-model fallback and quota guard."""
        if not self.api_key:
            log.warning("GEMINI_API_KEY missing in .env. Skipping AI market scan.")
            return {}

        now = time.time()
        if hasattr(self, "_quota_pause_until") and now < self._quota_pause_until:
            rem = int(self._quota_pause_until - now) // 60
            log.info(f"AI Engine: Gemini API paused due to daily quota limit ({rem}m remaining). Skipping call.")
            return {}

        primary_model = getattr(tc, "AI_MODEL", "gemini-3.5-flash")
        candidates = [primary_model]
        for fb in ("gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-3.8-flash"):
            if fb not in candidates:
                candidates.append(fb)

        for model_candidate in candidates:
            try:
                from google.genai import types

                client = self._genai_client
                if not client:
                    from google import genai
                    client = genai.Client(api_key=self.api_key)
                    self._genai_client = client

                response = client.models.generate_content(
                    model=model_candidate,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.2,
                    ),
                )
                raw_text = response.text or ""
                if "```json" in raw_text:
                    raw_text = raw_text.split("```json")[1].split("```")[0].strip()
                elif "```" in raw_text:
                    raw_text = raw_text.split("```")[1].split("```")[0].strip()
                return json.loads(raw_text)
            except Exception as e:
                err_str = str(e)
                is_quota = "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower()
                log.warning(f"AI Engine: Model {model_candidate} call failed: {e}")
                if not is_quota:
                    break
                continue

        # All model candidates exhausted due to quota limit:
        self._quota_pause_until = now + 3600
        log.error("AI Engine: All Gemini models exceeded quota (429 RESOURCE_EXHAUSTED). Pausing AI calls for 60m.")
        try:
            from .notifier import send_watchdog_alert
            send_watchdog_alert(
                title="Gemini AI Quota Limit Reached",
                component="AI Autonomous Engine",
                issue="Gemini API returned 429 RESOURCE_EXHAUSTED (Free Tier daily request limit reached).",
                action="AI Engine temporarily paused for 60m to prevent log spam. Rule Engine continues trading normally.",
                severity="WARNING"
            )
        except Exception as ne:
            log.warning(f"Failed to send watchdog alert: {ne}")
        return {}

    def cycle(self):
        """15m AI cycle: evaluate candidates, generate autonomous setup, execute order."""
        if not getattr(tc, "AI_ENABLED", True):
            return

        con = self.db()
        m_con = self.market_db()
        try:
            # 1. Check max open AI positions
            active_ai = self.get_active_ai_count(con)
            if active_ai >= tc.MAX_AI_OPEN:
                log.info(f"AI Engine: Max open positions reached ({active_ai}/{tc.MAX_AI_OPEN}). Waiting for exits.")
                return

            # 2. Check cooldown symbols (including 12h loss cooldown)
            recent_syms = self.get_active_and_recent_symbols(con)

            # 3. Get candidate coins from Watchlist / Universe (market_db SQLite)
            has_wl = m_con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='watchlist'").fetchone()
            if not has_wl:
                log.info("AI Engine: Watchlist table not populated yet.")
                return

            sd = m_con.execute("SELECT MAX(scan_date) FROM watchlist").fetchone()[0]
            candidates = []
            if sd:
                wl_df = pd.read_sql_query(
                    "SELECT w.symbol, w.rank, COALESCE(u.quote_volume_24h, 0.0) as quote_vol_24h "
                    "FROM watchlist w "
                    "LEFT JOIN universe u ON w.symbol = u.symbol "
                    "WHERE w.scan_date=? ORDER BY w.score DESC LIMIT 25", m_con, params=(sd,))
                candidates = wl_df.to_dict("records")

            # Filter min volume ($30M+) and exclude recent cooldown symbols
            candidates = [
                c for c in candidates
                if float(c.get("quote_vol_24h", 0)) >= tc.MIN_VOL_24H and c["symbol"] not in recent_syms
            ][:15]

            if not candidates:
                log.info("AI Engine: No eligible candidates after cooldown & volume filter ($30M+).")
                return

            # 4. Build Market Context for Gemini (with BTC Regime & accurate indicators)
            btc_regime, btc_curr, btc_e50, btc_rsi, btc_adx = self.get_btc_regime(m_con)
            log.info(f"AI Engine: BTC 1h Regime = {btc_regime} (Price: ${btc_curr:.1f}, EMA50: ${btc_e50:.1f}, RSI: {btc_rsi:.1f}, ADX: {btc_adx:.1f})")

            market_data = []
            atr_map = {}
            for c in candidates[:8]:  # analyze top 8 high-conviction candidates
                sym = c["symbol"]
                try:
                    bid, ask = self.book(sym)
                    mid = (bid + ask) / 2

                    # Fetch 1H klines for macro trend and ADX
                    h1_df = pd.read_sql_query(
                        "SELECT open_time, open, high, low, close, volume FROM klines "
                        "WHERE symbol=? AND interval='1h' ORDER BY open_time DESC LIMIT 60",
                        m_con, params=(sym,)
                    )
                    h1_adx = 0.0
                    h1_trend = "UNKNOWN"
                    if len(h1_df) >= 30:
                        h1_df = h1_df.iloc[::-1].reset_index(drop=True)
                        h1_c = h1_df["close"].astype(float)
                        h1_e20 = float(h1_c.ewm(span=20, adjust=False).mean().iloc[-1])
                        h1_e50 = float(h1_c.ewm(span=50, adjust=False).mean().iloc[-1])
                        h1_adx_s = compute_adx(h1_df, 14)
                        h1_adx = float(h1_adx_s.iloc[-1]) if not h1_adx_s.empty and not pd.isna(h1_adx_s.iloc[-1]) else 0.0
                        h1_trend = "BULLISH" if h1_e20 > h1_e50 else "BEARISH"

                    # Skip candidates with no macro momentum
                    if h1_adx < 20.0:
                        continue

                    # Fetch 150 klines for statistically accurate EMA20, EMA50, and ATR
                    k_df = pd.read_sql_query(
                        "SELECT open_time, open, high, low, close, volume FROM klines "
                        "WHERE symbol=? AND interval='15m' ORDER BY open_time DESC LIMIT 150",
                        m_con, params=(sym,)
                    )
                    if len(k_df) >= 50:
                        k_df = k_df.iloc[::-1].reset_index(drop=True)
                        closes = k_df["close"].astype(float)
                        highs = k_df["high"].astype(float)
                        lows = k_df["low"].astype(float)
                        ema20 = closes.ewm(span=20, adjust=False).mean().iloc[-1]
                        ema50 = closes.ewm(span=50, adjust=False).mean().iloc[-1]
                        
                        # ATR 14
                        prev_c = closes.shift(1)
                        tr = pd.concat([highs - lows, (highs - prev_c).abs(), (lows - prev_c).abs()], axis=1).max(axis=1)
                        atr14 = float(tr.rolling(14).mean().iloc[-1])
                        atr_map[sym] = atr14

                        market_data.append({
                            "symbol": sym,
                            "current_price": mid,
                            "24h_volume_usd": float(c.get("quote_vol_24h", 0)),
                            "1h_trend": h1_trend,
                            "1h_adx": round(h1_adx, 1),
                            "ema20": round(float(ema20), 6),
                            "ema50": round(float(ema50), 6),
                            "atr14": round(atr14, 6),
                            "last_5_candles": [
                                {"c": round(row["close"], 6), "v": round(row["volume"], 2)}
                                for _, row in k_df.tail(5).iterrows()
                            ]
                        })
                except Exception as ex:
                    log.warning(f"AI context gather failed for {sym}: {ex}")

            if not market_data:
                log.warning("AI Engine: Could not assemble qualified market data (all candidates pruned by ADX or volume).")
                return

            # 5. Formulate Prompt with Market Regime & Institutional ADX
            prompt = f"""
You are the autonomous Senior Crypto Quant Analyst for Trading Cloudi.
Evaluate the following candidate coins on 15m timeframe and find at most ONE high-probability trade setup.

Market Overview:
- Bitcoin (BTCUSDT 1h): {btc_regime} (Price: ${btc_curr:.1f}, 1h EMA50: ${btc_e50:.1f}, RSI: {btc_rsi:.1f}, 1h ADX: {btc_adx:.1f})
- REGIME & TREND STRENGTH GATING RULES:
  * When 1H ADX is below 25.0, the market is in choppy consolidation. Breakouts and trend continuation setups have low statistical edge. Set "has_trade": false unless there is an extraordinary breakout with institutional volume.
  * When BTC is BEARISH, market probability heavily punishes Altcoin LONGs. You MUST NOT propose any Altcoin LONG setup. Only propose an exceptionally clean SHORT setup (confidence >= 80%), or set "has_trade": false.
  * When BTC is BULLISH, favor high-probability LONG trend continuation or pullback bounces.
  * Candidate Selection: Require candidate 1H ADX >= 25.0 and candidate 1H trend alignment with the proposed trade side.
  * When market is choppy, unclear, or unconvincing, set "has_trade": false.

Capital & Risk Parameters:
- Max position size: ${tc.AI_NOTIONAL_USDT:.2f} USDT notional
- Target Stop Loss Risk: ${tc.AI_SL_USDT:.2f} USDT (Dynamic ATR-based stop)
- Scaled Take Profit 1 Target: 2.0 * ATR (50% partial exit)
- Secondary Take Profit Target: 3.5 * ATR (or 1:2 Risk to Reward)
- Minimum confidence required: {tc.AI_MIN_CONFIDENCE}%

Candidates Data:
{json.dumps(market_data, indent=2)}

Rules:
1. Only choose a setup if there is a clear trend continuation, EMA pullback bounce, breakout retest, or liquidity sweep.
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

            # 6. Execute Order with Dynamic ATR & Scaled TP
            chosen_atr = atr_map.get(sym, 0.0)
            self.execute_ai_trade(con, sym, side, strat, conf, thesis, atr=chosen_atr)

        except Exception as e:
            log.exception(f"AI Engine cycle failed: {e}")
        finally:
            con.close()
            m_con.close()

    def execute_ai_trade(self, con, sym: str, side: str, strat: str, conf: int, thesis: str, atr: float = 0.0):
        """Execute autonomous AI trade with Dynamic ATR Stop Loss, Scaled TP, and strict $15 Risk Capping."""
        info = self.info(sym)
        if not info:
            log.error(f"AI Execute: exchange_info not found for {sym}")
            return False
        tick, step, minq, minn = [Decimal(str(x)) for x in info]

        bid, ask = self.book(sym)
        mid = (bid + ask) / 2

        # 1. Dynamic ATR-based Stop Distance calculation
        if atr and atr > 0:
            sl_dist = Decimal(str(round(1.5 * atr, 6)))
        else:
            sl_dist = Decimal(str(mid)) * Decimal("0.03")

        # Volatility Guardrails: minimum 1.5% distance, maximum 7.5% distance
        min_dist = Decimal(str(mid)) * Decimal("0.015")
        max_dist = Decimal(str(mid)) * Decimal("0.075")
        sl_dist = max(min_dist, min(max_dist, sl_dist))

        # 2. Position Sizing: Risk = sl_dist * q == $15.0 USDT
        target_risk = Decimal(str(getattr(tc, "AI_SL_USDT", 15.0)))
        q = target_risk / sl_dist

        # Cap max notional size ($200)
        max_notional = Decimal(str(getattr(tc, "AI_NOTIONAL_USDT", 200.0)))
        max_q = max_notional / Decimal(str(mid))
        q = min(q, max_q)
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

        # Exact delta_sl and delta_tp based on actual rounded quantity
        delta_sl = float(target_risk / q)
        delta_tp = float(Decimal(str(getattr(tc, "AI_TP_USDT", 30.0))) / q)

        # Scaled Take Profit 1 (Phase 4: 2.0 * ATR partial exit target)
        tp1_mult = float(getattr(tc, "AI_TP1_ATR", 2.0))
        tp1_dist = float(tp1_mult * atr) if atr and atr > 0 else float(delta_sl * 1.33)

        if side == "LONG":
            sl_price = rt(mid - delta_sl, tick)
            tp1_price = rt(mid + tp1_dist, tick)
            tp_price = rt(mid + delta_tp, tick)
        else:
            sl_price = rt(mid + delta_sl, tick)
            tp1_price = rt(mid - tp1_dist, tick)
            tp_price = rt(mid - delta_tp, tick)

        if self.dry:
            log.info(f"DRY AI ORDER: {sym} {side} qty={q} entry={mid} SL={sl_price} TP1={tp1_price} TP2={tp_price} (Conf: {conf}%)")
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
            "INSERT INTO positions (time, symbol, strategy, side, entry, sl, tp, qty, status, entry_ms, engine, ai_thesis, ai_confidence, tp1_hit, tp1_price, original_qty) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, 'AI', ?, ?, 0, ?, ?)",
            (time_str, sym, strat, side, entry, sl_price, tp_price, float(q), entry_ms, thesis, conf, tp1_price, float(q))
        )
        pid = cur.lastrowid
        con.commit()

        log.info(f"AI Engine: ORDER PLACED! #{pid} {sym} {side} @ {entry} (SL={sl_price}, TP1={tp1_price}, TP2={tp_price})")

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
