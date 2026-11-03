# TRADING WITH CLOUDI — MASTER AUDIT LOG

Project: `mzahi2026-dev/TRADING-WITH-CLOUDI` (user-specified local project)
Architecture target: V1.0 — Bybit V5, USDT-settled linear perpetual futures, Demo orders, price-action strategy.
Log initialized: 2026-11-03 (Asia/Dhaka)
Source snapshot: `Cloudi-P0-01-Audit.zip` (user-supplied source archive)
Repository/runtime verification: **Not performed**. This log records static source inspection only.

## Operating rules

1. Append an entry for every test, blocker, remediation proposal, approved change, result, and rollback. Never silently revise historical results: add a correction entry referencing the original ID.
2. Every record has a timestamp, ID, scope, expected result, actual evidence (file:line or sanitized command output), status, next action, and change authorization. No evidence means NOT VERIFIED.
3. Status vocabulary: NOT TESTED, PASS, FAIL, BLOCKED, IN PROGRESS, NOT VERIFIED.
4. Do not log API keys, secrets, tokens, personal data, or complete environment-variable dumps.
5. No production orders, strategy rewrites, or changes to the default branch without explicit user approval. Use a separate Git branch for approved edits.
6. Test outcomes are scoped to their evidence: static inspection is not proof of runtime behavior.
7. One test at a time. After each test, update `PROJECT_STATE.md` and append here. The next chat starts by reading both files.

## Log entries

### AUD-20261103-001 — P0-01-A: Demo environment configuration, static source audit
- Date: 2026-11-03 (Asia/Dhaka; exact execution time not recorded)
- Priority: P0 / critical
- Expected: Bybit V5 Demo private endpoints, demo-account credentials, and a fail-closed order-routing guard; no live/testnet order routing.
- Actual evidence:
  - `app/config.py:8-18`: Binance USDⓈ-M Futures config; base URL conditional between Binance demo and Binance mainnet.
  - `app/trade_config.py:1`: `DEMO_BASE = "https://demo-fapi.binance.com"`.
  - `app/trade_engine.py:10,46-47`: imports/uses `BinanceClient` and `DemoClient`.
  - `run_unified.py:147-148`: `TRADER_DRY_MODE` defaults to `false`; in that case UI mode string indicates Binance demo orders.
  - `run_unified.py:243`: startup message says `DRY RUN ONLY`, inconsistent with the default mode string.
- Result: **FAIL (static target-architecture compliance)**. This does **not** establish which endpoint the running process actually uses or whether live orders were submitted.
- Change made: None.
- Follow-up finding: `FIND-001` — mismatched and potentially misleading dry-mode status messaging.
- Next action: **P0-01-A runtime evidence collection** (sanitized environment and resolved exchange host, no order submissions); then design the smallest approved Bybit-demo migration.
- Authorization: Read-only audit only.

## Findings register

| Finding ID | Description | Severity | State |
|---|---|---|---|
| FIND-001 | `run_unified.py` prints `DRY RUN ONLY` even though `TRADER_DRY_MODE` defaults to false | High / misleading safety status | OPEN — not fixed |
| FIND-002 | Current static exchange implementation is Binance-focused, contradicting approved Bybit Demo target | P0 / architecture mismatch | OPEN — not fixed |

## Current project state

- Target architecture: LOCKED at decision level; detailed price-action entry/exit specification pending.
- Current priority: P0-01 (Demo Isolation).
- P0-01-A static audit: FAIL.
- P0-01-A actual runtime environment: NOT VERIFIED.
- P0-01-B to P0-01-J: NOT TESTED.
- Code or exchange changes: NONE.
- Immediate next step: collect read-only local runtime configuration evidence without disclosing credentials or placing orders.

## P0-01-A | 2026-11-03 | FAIL
Expected: Bybit V5 Futures Demo configuration.
Actual: Binance Futures Demo configuration found in app/config.py (8-18), app/trade_config.py (1), app/demo_client.py (45-77).
Root Cause: Architecture-Implementation mismatch.
Runtime: No matching trading Python process found.
Changes: None. Orders: None.
Next: Define controlled Binance-to-Bybit migration requirements before any code change.


## P0-01-A-DEPENDENCY-20261103 | DISCOVERY COMPLETE
Evidence: Read-only PowerShell dependency scan.
Affected: config.py, trade_config.py, binance_client.py, demo_client.py, trade_engine.py, ai_engine.py, collector.py, symbols.py, extra.py, app_ui.py, run_unified.py, app/__init__.py.
Finding: Binance dependencies span configuration, market data, trading, startup and UI.
Decision: No direct URL replacement. Full API contract mapping required.
P0-01-A: FAIL remains OPEN. P0-01-B: NOT TESTED.
Changes: None. Orders: None.
Next: Read-only API contract mapping for Binance-to-Bybit migration.


## P0-01-METHOD-RISK-20261103 | OPEN FINDINGS
Source: demo_client.py method inventory and source review.
M03: check_order_exists lines 124-131 returns None on lookup errors; possible duplicate-order ambiguity. Caller verification pending.
M04: cancel_conditionals lines 212-217 logs cancellation errors; post-cancel verification pending.
Status: NOT RUNTIME TESTED. No code changes or orders.
Next: Inspect trade_engine.py and ai_engine.py caller contracts.


## M04-CONFIRMED-20261103 | CONFIRMED CODE DEFECT
Evidence: trade_engine.py lines 487-503; demo_client.py lines 212-217.
Defect: cancel_conditionals executes before closure/remaining-position verification; cancellation errors are logged without a success/failure result.
Risk: Existing protective SL/TP may be cancelled while a position remains open; runtime incident not verified.
Required: Verify exchange position zero before cancelling residual protective orders; verify cancellation outcome and journal consistency.
Status: OPEN / CRITICAL. Automated demo order execution should remain disabled pending fix and retest.
Changes: NONE. Orders: NONE.
Next: Inspect all close_row callers and position reconciliation before proposing minimal fix.


## M04-CALLER-CORRECTION-20261103 | FINDING REVISED
Evidence: trade_engine.py lines 466-483 and 487-503.
Caller 470: close_row only when recorded exchange position quantity is zero.
Caller 483: close_row after emergency_close returns success.
Correction: Earlier claim of unconditional cancellation without caller-side position verification was overstated.
Confirmed deficiency: cancel_conditionals suppresses DemoError and returns no verified cancellation result.
Remaining concern: stale position snapshot or race condition; runtime incident not proven.
Status: OPEN - requires cancellation verification design and negative tests.
Code changes: NONE. Orders: NONE.



## P0-AUDIT-CONSOLIDATED-20261103 | COMPLETE P0 READ-ONLY AUDIT
- Date: 2026-11-03 (Asia/Dhaka)
- Priority: P0 / Critical Safety Audit
- Scope: Complete repository audit covering exchange configuration, environment isolation, API clients, scanner, signal engines, risk management, order execution, duplicate order prevention, SL/TP placement and cancellation, restart recovery, database consistency, and trade journal.
- Reports Created:
  - `docs/audit_reports/P0_CONSOLIDATED_SAFETY_AUDIT_REPORT.md`
  - `docs/audit_reports/BINANCE_TO_BYBIT_V5_MIGRATION_ASSESSMENT.md`
- Confirmed Defects:
  1. `FIND-P0-01`: Architecture mismatch. Binance Futures hardcoded across all trading modules (`config.py:8-18`, `trade_config.py:1`, `demo_client.py:45-77`). Status: FAIL.
  2. `FIND-P0-02`: Production endpoint leak. `binance_client.py:15-18` routes `/futures/data/` directly to `https://fapi.binance.com` in demo mode. Status: FAIL.
  3. `FIND-P0-03`: Misleading console status. `run_unified.py:147, 243, 264` prints "DRY RUN ONLY" while `dry_mode` evaluates to False, placing live demo orders. Status: FAIL.
  4. `FIND-P0-04`: Unchecked Stop-Loss placement. `ai_engine.py:339-354` ignores return value of `try_cond` for SL, marking positions `OPEN` without protection. Status: FAIL.
  5. `FIND-P0-05`: Error suppression on SL/TP cancellation. `demo_client.py:212-217` swallows `DemoError` without verifying cancellation. Status: FAIL.
  6. `FIND-P0-06`: Concurrent dual-engine symbol collision. `run_unified.py:159-175` and `ai_engine.py:71-80, 159-178` allow Rule Engine and AI Engine to enter same symbol simultaneously. Status: FAIL.
  7. `FIND-P0-07`: Flawed daily drawdown circuit breaker. `trade_engine.py:161-168` queries `entry_ms >= day_cutoff` instead of `closed_ms`, missing recent losses; AI engine has no daily loss check. Status: FAIL.
  8. `FIND-P0-08`: Degenerate directional condition. `trade_engine.py:226-227` evaluates `abs(...) >= 0`, making SHORT permanently unreachable on dual signal bars. Status: FAIL.
  9. `FIND-P0-09`: Unprotected entry points. `trade_main.py` and `run_all.ps1` lack single-instance locking and run without dry mode enforcement. Status: FAIL.
  10. `FIND-P1-10`: Journal PnL filter distortion. `app_ui.py:389` queries `entry_ms` instead of `closed_ms`. Status: FAIL.
  11. `FIND-P1-11`: SQLite concurrency lock contention. `scanner.py:169`, `ranker.py:127`, `app_ui.py:92` omit explicit connection timeout. Status: FAIL.
- Runtime State: Zero python trading processes currently running.
- Trading Source Code Changes: NONE.
- Orders Placed: NONE.
- Status: AUDIT COMPLETE. AUTOMATED TRADING REMAINS BLOCKED PENDING IMPLEMENTATION OF MINIMAL FIX PLAN.


## P0-01-FIX-BATCH-01-20261103 | ENVIRONMENT SAFETY FOUNDATION COMPLETE
- Date: 2026-11-03 (Asia/Dhaka)
- Priority: P0 / Critical Fix Batch 01
- Scope: P0-01 Environment Safety Foundation
- Branch: `fix/p0-01-environment-safety` (created from baseline commit `0db85ff08dde89f6552c92af16bd73cb19000f3a`)
- Implemented:
  - `app/safety.py`: Central environment safety guard, Bybit Demo endpoint contract, credential format validation, log masking, and `SafetyViolationError`.
  - `tests/test_p0_01_environment_safety.py`: 10 isolated mock automated safety tests.
  - Fail-closed execution guard: `Engine(dry=True)`, `AIEngine(dry=True)`, `trade_main.py` default to execution disabled.
  - Prohibits Bybit production (`api.bybit.com`), Bybit testnet (`api-testnet.bybit.com`), and Binance trading configurations.
  - Replaces misleading "DRY RUN ONLY" console strings in `run_unified.py` with accurate dynamic safety banner.
- Test Evidence: `python -m pytest tests/test_p0_01_environment_safety.py -v` -> 10 PASSED, 0 FAILED in 0.91s.
- Orders Placed: ZERO.
- Full Exchange Migration: NOT PERFORMED (Scoped exclusively to Fix Batch 01 foundation).
- Status: BATCH 01 VERIFIED PASS. Full P0-01 remains IN PROGRESS pending Bybit V5 client adapter implementation (Fix Batch 02).
- Reports Created:
  - `docs/audit_reports/P0_01_FIX_BATCH_01_REPORT.md`
  - `docs/PROJECT_STATE.md`


## P0-01-FIX-BATCH-01-CORRECTIONS-20261103 | REVIEW FINDINGS R01–R04 RESOLVED
- Date: 2026-11-03 (Asia/Dhaka)
- Priority: P0 / Critical Fix Batch 01 Review Corrections
- Scope: Correct review findings R01, R02, R03, R04 and verify with isolated negative tests.
- Corrections Implemented:
  - `R01`: Decoupled credential format validation from exchange account identity verification. `account_verified` permanently set to `False` until authenticated exchange session is implemented.
  - `R02`: Implemented strict `validate_endpoint` checking scheme, host, port, path, query, and fragment for Bybit Demo REST (`https://api-demo.bybit.com`), Demo Private WS (`wss://stream-demo.bybit.com/v5/private`), and Public Linear WS (`wss://stream.bybit.com/v5/public/linear`).
  - `R03`: Synchronized startup Telegram alert (`send_bot_online_alert` in `app/notifier.py` and `run_unified.py`) to report actual execution state (`DISABLED`) and unverified account identity. Never reports enabled/verified solely on `USE_DEMO=true`.
  - `R04`: Added 4 new negative test cases (14 total) covering fake formatted keys, URL malformations, custom ports, unexpected paths, and all order-capable startup paths.
- Test Evidence: `python -m pytest tests/test_p0_01_environment_safety.py -v` -> 14 PASSED, 0 FAILED in 3.95s.
- Orders Placed: ZERO.
- Fix Batch 01 Status: CLOSED.


## P0-01-FIX-BATCH-01-FINAL-CORRECTIONS-20261103 | BATCH 01 FINAL CORRECTIONS COMPLETE
- Date: 2026-11-03 (Asia/Dhaka)
- Priority: P0 / Critical Fix Batch 01 Final Corrections
- Scope:
  1. Safe handling of malformed and out-of-range URL ports without uncaught exceptions; return ENDPOINT_VALIDATION_FAILED.
  2. Complete redaction of sensitive URL query values, passwords, userinfo, and parameter values from validation errors, reports, and logs.
  3. Synchronization of hourly report (`send_hourly_report` in `app/notifier.py`) trading-mode determination with fail-closed safety state; never claims DEMO order execution when execution is disabled.
- Implemented:
  - `app/safety.py`: Port exception handling in `validate_endpoint` (`try...except ValueError`); URL sanitizer `sanitize_url` ensuring userinfo, query values, parameters, and fragments are replaced with `[REDACTED]`.
  - `app/notifier.py`: Updated `send_hourly_report` to query `validate_environment()` and report `DRY RUN (Execution Disabled)`.
  - `tests/test_p0_01_environment_safety.py`: Added 3 regression tests (`test_malformed_and_out_of_range_ports_handled_without_exception`, `test_sensitive_url_values_never_included_in_errors_reports_or_logs`, `test_hourly_report_trading_mode_synchronized_with_fail_closed_guard`).
- Test Evidence: `python -m pytest tests/test_p0_01_environment_safety.py -v` -> 17 PASSED, 0 FAILED in 2.42s.
- Orders Placed: ZERO.
- Trading Bot Started: NO.
- Status: BATCH 01 FINAL CORRECTIONS VERIFIED. ALL 17 TESTS PASS.


## P0-01-HOURLY-REPORT-WINDOW-SYNC-20261104 | HOURLY VS 24H BREAKDOWN RESOLVED
- Date: 2026-11-04 (Asia/Dhaka)
- Priority: P1 / Telemetry UX Clarity
- Scope: Resolved ambiguity in Telegram hourly report between the 1-hour time window performance and 24h overall performance.
- Implemented:
  - `app/notifier.py`: Updated `send_hourly_report` to parse position closing timestamps via `_parse_closed_ts` and generate two distinct sections:
    1. `⚡ Last 1 Hour ({time_window})`: Closed trades, wins/losses, and net PnL within that specific 1-hour period.
    2. `🗓️ 24h Overall Summary`: Cumulative 24-hour total closed trades, overall win rate, total net PnL, and fees.
- Test Evidence: `python -m pytest tests/test_p0_01_environment_safety.py -v` -> 17 PASSED in 0.87s.
- Orders Placed: ZERO.
