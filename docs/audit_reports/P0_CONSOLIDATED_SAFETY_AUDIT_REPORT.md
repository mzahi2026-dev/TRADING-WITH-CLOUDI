# Consolidated P0 Safety Audit Report: TRADING WITH CLOUDI

**Document ID:** AUD-REP-20261103-CONSOLIDATED  
**Audit Date:** 2026-11-03 (Asia/Dhaka)  
**Project Path:** `E:\TRADING WITH CLOUDI`  
**Locked Target Architecture:**
- Bybit V5 (`category=linear`)
- USDT Linear Perpetual Futures
- Bybit Demo Trading Only (`https://api-demo.bybit.com`)
- Price Action Strategy (EMA Pullback, Breakout Retest, Liquidity Sweep)
- No real-money orders (Strict Fail-Closed Enforcement)

**Audit Execution Mode:** Complete Read-Only Static and Database Inspection. No source files modified. No trading orders placed. No bot processes started.

---

## 1. Executive Summary & Component Health Matrix

A comprehensive safety audit was conducted across all twelve critical subsystems of the `TRADING WITH CLOUDI` crypto bot. The overall audit verdict is **P0 FAIL**. 

While the system contains robust analytical models and protective mechanisms in certain modules (e.g., SQLite WAL mode, database candle gap detection, and emergency close routines in `trade_engine.py`), the codebase suffers from severe architectural divergence, critical unhandled failure paths in the AI execution pipeline, misleading runtime status messaging, and complete absence of Bybit V5 integration.

### Component Health Summary

| # | Subsystem / Component | Compliance Status | Highest Severity | Key Defect Summary |
|---|---|---|---|---|
| 1 | **Exchange Configuration** | **FAIL** | P0 Critical | 100% hardcoded to Binance USDⓈ-M Futures; Bybit V5 endpoints completely absent. |
| 2 | **Environment Isolation** | **FAIL** | P0 Critical | Production URL bypass in `binance_client.py:17` (`/futures/data/`); misleading "DRY RUN ONLY" console logging while live orders are placed. |
| 3 | **API Clients** | **FAIL** | P0 Critical | `DemoClient` suppresses cancellation errors; ambiguous order timeout recovery; Binance-only authentication. |
| 4 | **Scanner & Ranker** | **FAIL (Arch)** / **PASS (Logic)** | P0 Arch / P2 Logic | Solid price action math and gap checks, but coupled to Binance data contracts. |
| 5 | **Signal Engines** | **FAIL** | P0 Critical | `ai_engine.py` does not validate SL placement; `trade_engine.py` has a degenerate condition (`abs(x) >= 0`) preventing SHORT selection during dual signals. |
| 6 | **Risk Management** | **FAIL** | P0 Critical | Daily drawdown check queries `entry_ms` instead of `closed_ms`, missing recent losses; AI engine completely lacks daily loss & balance limits. |
| 7 | **Order Execution** | **FAIL** | P0 Critical | `trade_main.py` and `run_all.ps1` bypass dry mode and single-instance locks; potential multi-process order submission. |
| 8 | **Duplicate Order Prevention** | **FAIL** | P0 Critical | AI engine and Rule engine can simultaneously open trades on the same symbol; PID lock not enforced in all entry points. |
| 9 | **SL/TP Management** | **FAIL** | P0 Critical | Cancellation errors swallowed silently in `cancel_conditionals`; AI engine writes `OPEN` even if SL order fails. |
| 10| **Restart Recovery** | **FAIL** | P0 Critical | AI engine has no reconciliation; `close_row` cancels conditionals before verifying trade fills; dictionary key collision in hedge positions. |
| 11| **Database Consistency** | **FAIL** | P1 High | Concurrent threads open SQLite connections without uniform `timeout=30`, risking `database is locked` during scanner/writer bursts. |
| 12| **Trade Journal** | **FAIL** | P1 High | UI journal filters `today_pnl` by `entry_ms >= day_start`, dropping trades opened before 06:00 BDT that closed today. |

---

## 2. Confirmed P0/P1 Defects (Evidence-Backed)

Every finding in this section is proven by exact source code lines and reproducible static/database evidence.

### FIND-P0-01: Hardcoded Binance Target Architecture (Target Architecture Violation)
- **Files & Lines:** `app/config.py:8-18`, `app/trade_config.py:1`, `app/demo_client.py:45-77`
- **Root Cause:** The system was built solely against Binance USDⓈ-M Futures API (`https://demo-fapi.binance.com`). There is no Bybit V5 client, endpoint configuration, or data mapping in the repository.
- **Impact:** The system cannot trade on Bybit V5 Demo. Launching it attempts connection to Binance endpoints.
- **Acceptance Test:** System configuration must load `BYBIT_API_KEY`, `BYBIT_API_SECRET`, target `https://api-demo.bybit.com`, verify connectivity against Bybit V5 `/v5/market/time`, and refuse execution if any Binance endpoint is configured.

### FIND-P0-02: Production Endpoint Leakage in Demo Mode
- **Files & Lines:** `app/binance_client.py:15-18`
- **Code Extract:**
  ```python
  is_futures_data = path.startswith("/futures/data/")
  if is_futures_data:
      url = "https://fapi.binance.com" + path
  else:
      url = config.FAPI_BASE + path
  ```
- **Root Cause:** A hardcoded routing rule intercepts requests starting with `/futures/data/` (open interest, long/short ratios, taker volume in `app/extra.py`) and routes them unconditionally to Binance production `https://fapi.binance.com`.
- **Impact:** Violates environment isolation. Exposes caller IP to production rate limits and potential geo-blocking (HTTP 451/403).
- **Acceptance Test:** In demo or paper mode, zero HTTP packets must be transmitted to production hosts. Network assertion test confirms 100% of outgoing requests hit designated demo or mock sandbox hosts.

### FIND-P0-03: Misleading "DRY RUN ONLY" Console Status vs Live Network Execution
- **Files & Lines:** `run_unified.py:147-148, 243, 264`
- **Code Extract:**
  ```python
  dry_mode = os.getenv("TRADER_DRY_MODE", "false").lower() == "true"
  ...
  print(" [✓] Safe Trading Mode     : DRY RUN ONLY (No real order placed)")
  ...
  print("[3/4] Starting Trader Engine (Safe DRY Mode)...")
  ```
- **Root Cause:** Hardcoded console print strings in `run_unified.py` claim `DRY RUN ONLY` regardless of `dry_mode`. Because `TRADER_DRY_MODE` is not defined in `.env`, `dry_mode` evaluates to `False`. The trader engine actively connects to the demo exchange and submits live orders.
- **Impact:** Critical operator hazard. An operator running `start.bat` or `python run_unified.py` is informed that no orders are being placed, while real network orders are actively executing on the exchange.
- **Acceptance Test:** The console header must strictly reflect the evaluated boolean value: if `dry_mode` is `False`, display loud warnings: `[DANGER] LIVE DEMO ORDERS ENABLED`. The application must require explicit `--dry` or `.env` configuration to enable dry mode.

### FIND-P0-04: Unchecked Stop-Loss Placement in Gemini AI Engine
- **Files & Lines:** `app/ai_engine.py:339-354`
- **Code Extract:**
  ```python
  # Place conditional SL & TP orders on Binance
  self.try_cond(sym, cs, "STOP_MARKET", sl_price)
  self.try_cond(sym, cs, "TAKE_PROFIT_MARKET", tp_price)

  cur = con.cursor()
  cur.execute(
      "INSERT INTO positions (time, symbol, strategy, side, entry, sl, tp, qty, status, entry_ms, engine, ai_thesis, ai_confidence) "
      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, 'AI', ?, ?)",
      (time_str, sym, strat, side, entry, sl_price, tp_price, float(q), entry_ms, thesis, conf)
  )
  ```
- **Root Cause:** Unlike `trade_engine.py` (which verifies SL placement and triggers `emergency_close` if SL fails), `ai_engine.py` ignores the return boolean of `self.try_cond`.
- **Impact:** If exchange SL placement fails (e.g. rate limit, tick-size filter error, network timeout), the AI Engine still logs the position as `OPEN`. The position remains active in the live market with zero downside protection.
- **Acceptance Test:** Mock `try_cond` failure for SL. Verify that `ai_engine` immediately executes an emergency market close. If emergency close fails, mark status as `EMERGENCY_UNPROTECTED` and trigger urgent Telegram notification.

### FIND-P0-05: Silent Error Suppression in Protective Order Cancellation
- **Files & Lines:** `app/demo_client.py:212-217`
- **Code Extract:**
  ```python
  def cancel_conditionals(self, symbol):
      for path in ("/fapi/v1/algoOpenOrders", "/fapi/v1/allOpenOrders"):
          try:
              self._req("DELETE", path, {"symbol": symbol})
          except DemoError as e:
              log.info(f"cancel {path} {symbol}: {e}")
  ```
- **Root Cause:** `cancel_conditionals` swallows all `DemoError` exceptions, logs them with `log.info`, and returns `None`. Caller `close_row` has no verification that residual SL or TP conditional orders were cancelled.
- **Impact:** If the cancellation request fails, an orphaned conditional SL/TP order remains active on the exchange. If market price later crosses that trigger, an unintended opposite position is opened without tracking.
- **Acceptance Test:** Cancellation routine must return a verified result. A post-cancellation check (`open_orders(symbol)`) must confirm 0 remaining conditional orders. If orders remain, raise an alert.

### FIND-P0-06: Dual-Engine Race Condition & Duplicate Position Conflict
- **Files & Lines:** `run_unified.py:159-175`, `app/ai_engine.py:71-80, 159-178`, `app/trade_engine.py:195`
- **Root Cause:** `run_unified.py` schedules Rule Engine at `:25s` and AI Engine at `:35s`. In `ai_engine.py`, `get_recent_ai_symbols()` only inspects positions where `engine='AI'`. It does not check if the symbol currently has an active `OPEN` position initiated by the Rule Engine.
- **Impact:** Both engines can simultaneously enter positions in the same symbol on the same 15m candle. Observed in actual trade history on Nov 3 (`nov3_trades.json` IDs 9 & 10: `CATIUSDT` entered by both Rule Engine and AI Engine).
- **Acceptance Test:** Before opening a position, both engines must check a unified lock: `SELECT COUNT(*) FROM positions WHERE symbol=? AND status IN ('OPEN', 'EMERGENCY_UNPROTECTED')`. If $>0$, entry must be aborted.

### FIND-P0-07: Flawed Daily Loss Calculation (Bypasses Recent Closed Losses)
- **Files & Lines:** `app/trade_engine.py:161-168`, `app/ai_engine.py:150-185`
- **Code Extract:**
  ```python
  day_cutoff = int(time.time() * 1000) - 24 * 3600 * 1000
  daily_loss = con.execute(
      "SELECT COALESCE(SUM(pnl), 0) FROM positions WHERE status='CLOSED' AND closed IS NOT NULL AND entry_ms>=?",
      (day_cutoff,)).fetchone()[0]
  ```
- **Root Cause:**
  1. In `trade_engine.py`, `daily_loss` filters by `entry_ms >= day_cutoff`. If a trade was opened 25 hours ago and closed 30 minutes ago with a -$200 loss, it is completely excluded from the daily loss calculation.
  2. Unrealized PnL of open positions is ignored.
  3. `ai_engine.py` has NO daily loss check or balance check whatsoever.
- **Impact:** The -$150 circuit breaker can be breached, but trading continues unabated, leading to uncontrolled account drawdown.
- **Acceptance Test:** Add unit test with a trade opened 25 hours ago and closed 1 hour ago with -$160 PnL. The risk engine must immediately detect that 24h loss limit was breached and suspend new entries.

### FIND-P0-08: Degenerate Directional Condition in Rule Engine
- **Files & Lines:** `app/trade_engine.py:226-227`
- **Code Extract:**
  ```python
  if ev_long and ev_long.get("hits") and ev_short and ev_short.get("hits"):
      ev, side = (ev_long, "LONG") if abs(ev_long["close"] - df["close"].iloc[-2]) >= 0 else (ev_short, "SHORT")
  ```
- **Root Cause:** The expression `abs(...) >= 0` is mathematically guaranteed to be true for all real values.
- **Impact:** When both LONG and SHORT signals trigger on the same candle, SHORT is permanently unreachable; LONG is unconditionally selected.
- **Acceptance Test:** Unit test feeding simultaneous LONG and SHORT signals must verify unambiguous, non-trivial arbitration (or reject entering dual-signal bars).

### FIND-P0-09: Unlocked Secondary Entry Points (`trade_main.py`, `run_all.ps1`)
- **Files & Lines:** `trade_main.py:12-35`, `run_all.ps1:7-12`
- **Root Cause:** `run_unified.py` contains single-instance PID locking (`app.pid`), but `trade_main.py` has no instance lock. `run_all.ps1` starts `trade_main.py` without `--dry`.
- **Impact:** If an operator runs `run_all.ps1` while `run_unified.py` is running, two independent trading processes run concurrently, placing duplicate orders.
- **Acceptance Test:** Moving the lock acquisition into `Engine.__init__` ensures no two engine instances can exist simultaneously regardless of launch vector.

### FIND-P1-10: Distorted Daily Journal PnL Window
- **Files & Lines:** `app_ui.py:386-389`
- **Code Extract:**
  ```python
  day_start = now_bdt.replace(hour=6, minute=0, second=0, microsecond=0)
  ...
  day_cutoff_ms = int(day_start.timestamp() * 1000)
  df = pd.read_sql_query("SELECT * FROM positions WHERE entry_ms>=? ORDER BY rowid DESC", con, params=(day_cutoff_ms,))
  ```
- **Root Cause:** The dashboard calculates `today_pnl` by querying `entry_ms >= day_cutoff_ms`.
- **Impact:** Any trade opened prior to 06:00 BDT that closes during the day is missing from today's journal PnL metrics.
- **Acceptance Test:** Query `WHERE closed_ms >= day_cutoff_ms OR (status='OPEN' AND entry_ms >= day_cutoff_ms)` to ensure accurate realized PnL accounting.

### FIND-P1-11: SQLite Concurrency Timeout Inconsistency
- **Files & Lines:** `app/scanner.py:169`, `app/ranker.py:127`, `app_ui.py:92`
- **Root Cause:** Multiple modules open `sqlite3.connect(str(config.DB_PATH))` without specifying `timeout=30` (defaulting to 5.0s).
- **Impact:** During high-volume backfill or kline write operations, background scanner or UI threads encounter `sqlite3.OperationalError: database is locked`.
- **Acceptance Test:** Verify all SQLite connections in the codebase use uniform `timeout=30` and WAL mode via a shared DB factory function.

---

## 3. Suspected Risks (Requiring Runtime Verification)

| Risk ID | Component | Suspected Condition | Verification Requirement |
|---|---|---|---|
| **RISK-01** | `DemoClient._req` | In `check_order_exists` (lines 121-131), network timeout returns `None`. If exchange accepted order, retry may send duplicate order or get blocked by exchange. | Mock network drop during order POST response in isolated sandbox and observe retry behavior. |
| **RISK-02** | `app/collector.py` | WebSocket worker catches general exception and reconnects after `delay` seconds. Reconnect backfill runs synchronously in `asyncio.to_thread`. If backfill is slow, queue can stall. | Stress test WebSocket drop with 100 symbols under network disconnect simulation. |
| **RISK-03** | `AIEngine.call_gemini` | Gemini API response parsing in `ai_engine.py:124-144`. If model outputs unstructured text or markdown wrapping fails, setup is dropped. | Run unit test with varied LLM markdown JSON formatting responses. |
| **RISK-04** | `trade_engine.reconcile` | Line 443 creates dict `amt = {p["symbol"]: float(p["positionAmt"]) for p in pos}`. In dual-position or multi-asset responses, dictionary overwrites earlier entries. | Test payload with multiple entries per symbol under Hedge Mode. |

---

## 4. Migration Impact Assessment: Binance to Bybit V5

*(Refer to companion document `docs/audit_reports/BINANCE_TO_BYBIT_V5_MIGRATION_ASSESSMENT.md` for full contract mapping).*

### Key Architectural Deltas
1. **Endpoint Target:** Target moves from `demo-fapi.binance.com` to Bybit V5 Linear Demo `https://api-demo.bybit.com`.
2. **Signature Protocol:** Bybit V5 replaces Binance's URL query string signature with custom HTTP headers (`X-BAPI-API-KEY`, `X-BAPI-TIMESTAMP`, `X-BAPI-SIGN`, `X-BAPI-RECV-WINDOW`, `X-BAPI-SIGN-TYPE: 2`).
3. **Integrated Protective SL/TP:** In Bybit V5, SL and TP are set natively on the position via `POST /v5/position/trading-stop` or on order entry (`stopLoss`, `takeProfit`, `tpslMode="Full"`). When the position closes, Bybit cancels the conditional triggers automatically, permanently solving **FIND-P0-05**.
4. **Idempotency via `orderLinkId`:** Bybit V5 guarantees order idempotency using the `orderLinkId` field (max 36 alphanumeric characters).

---

## 5. Minimal Fix Plan (Strict Ordering)

Implementation of fixes must proceed strictly in the following phased order. **Zero trading code is to be executed until Phase 1 and 2 are fully verified in isolation.**

### Phase 1: Environment Isolation & Fail-Closed Guard (P0)
1. **Add `app/bybit_client.py`:** Implement clean, isolated Bybit V5 REST client adhering strictly to `https://api-demo.bybit.com`.
2. **Enforce Fail-Closed Protection:** Add assertion at client initialization:
   ```python
   if "api-demo.bybit.com" not in self.base_url:
       raise SystemExit("CRITICAL: Bybit Live endpoint detected! Fail-closed abort.")
   ```
3. **Fix Console Logging:** Synchronize `run_unified.py` banner with real effective mode. Remove deceptive "DRY RUN ONLY" messages.
4. **Eliminate Production Leaks:** Remove hardcoded `/futures/data/` production URL bypass in all clients.

### Phase 2: Engine Concurrency & Protective Order Safeguards (P0)
1. **Fix AI Engine SL Validation:** Make `try_cond` return value blocking in `ai_engine.py`. Trigger immediate emergency close if SL placement fails.
2. **Implement Unified Symbol Lock:** Prevent Rule Engine and AI Engine from entering the same symbol simultaneously.
3. **Fix Daily Loss Calculation:** In `trade_engine.py` and `ai_engine.py`, query `closed_ms >= day_cutoff` and incorporate open position unrealized PnL.
4. **Fix Degenerate Directional Condition:** Replace `abs(...) >= 0` with valid signal tie-breaking logic.
5. **Universal Instance Lock:** Move instance locking into `Engine.__init__` so that any script attempting to instantiate a second engine exits immediately.

### Phase 3: Bybit V5 Protocol Migration (P0/P1)
1. **Replace `DemoClient` with `BybitV5DemoClient`:** Map order creation, leverage, position queries, and wallet balance.
2. **Migrate SL/TP to Position Trading Stops:** Use Bybit V5 native `/v5/position/trading-stop`.
3. **Update WebSocket Collector:** Implement Bybit V5 linear topic subscription (`kline.{interval}.{symbol}`) with 20s ping/pong keep-alive.
4. **Standardize SQLite Database Connections:** Centralize connection creation with uniform WAL mode and `timeout=30`.

---

## 6. Automated Retest Plan

To adhere to operating rules ("Do not declare any test PASS without actual evidence"), the following automated test suite must be executed prior to any deployment:

```
tests/
├── test_fail_closed_guard.py       # Verifies client immediately aborts on live Bybit/Binance URLs
├── test_bybit_signature.py         # Validates HMAC-SHA256 headers against Bybit V5 test vectors
├── test_ai_sl_failure_handling.py  # Simulates SL failure and verifies immediate emergency close
├── test_dual_engine_lock.py        # Simulates concurrent Rule & AI signals on same symbol; verifies 1 enters, 1 blocked
├── test_daily_loss_circuit.py      # Seeds DB with -$160 loss 20h ago; verifies new trades are rejected
├── test_directional_arbitration.py# Feeds simultaneous LONG and SHORT signals; verifies non-degenerate selection
└── test_single_instance_lock.py    # Attempts to launch second Engine instance; verifies immediate block
```

---

## 7. Files Created

1. `docs/audit_reports/P0_CONSOLIDATED_SAFETY_AUDIT_REPORT.md` (This document)
2. `docs/audit_reports/BINANCE_TO_BYBIT_V5_MIGRATION_ASSESSMENT.md` (Detailed Bybit V5 contract mapping)
3. Appended audit entries to `docs/AUDIT_LOG.md` (Maintaining audit trail history)

---

## 8. Remaining Blockers

1. **Architecture Mismatch:** Current repository contains zero Bybit V5 integration code.
2. **Missing Bybit V5 Demo Credentials:** `.env` currently holds `DEMO_API_KEY` (Binance format) and has not been provisioned with Bybit V5 Demo keys (`BYBIT_DEMO_API_KEY`, `BYBIT_DEMO_API_SECRET`).
3. **Unprotected SL in AI Engine:** `ai_engine.py` cannot be run in any live or demo order environment until the SL placement guard is implemented.
4. **Approval Gate:** Implementation of Phase 1 and Phase 2 fixes requires user authorization.
