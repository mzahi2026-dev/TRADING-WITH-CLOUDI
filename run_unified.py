import asyncio
import os
import signal
import sys
import threading
import time
from pathlib import Path

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# Set up project root
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import config, trade_config as tc
from app.binance_client import BinanceClient
from app.collector import run as run_collector
from app.logger import log as clog
from app.notifier import send_alert
from app.ranker import run_rank
from app.scanner import run_scan
from app.storage import Storage
from app.symbols import get_symbols
from app.trade_engine import Engine
from app.ai_engine import AIEngine
from app.trade_log import log as tlog
import app_ui

# --- Single Instance Lock (Duplicate Protection) ---
LOCK_FILE = ROOT / "data_store" / "app.lock"
PID_FILE = ROOT / "data_store" / "app.pid"


def acquire_instance_lock():
    """Ensure only one instance runs at a time."""
    ROOT.joinpath("data_store").mkdir(parents=True, exist_ok=True)
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            alive = False
            if sys.platform == "win32":
                import ctypes
                kernel32 = ctypes.windll.kernel32
                SYNCHRONIZE = 0x00100000
                process = kernel32.OpenProcess(SYNCHRONIZE, False, old_pid)
                if process != 0:
                    kernel32.CloseHandle(process)
                    alive = True
            else:
                try:
                    os.kill(old_pid, 0)
                    alive = True
                except (OSError, ProcessLookupError):
                    alive = False

            if alive:
                print(f"\n[DUPLICATE BLOCKED] Another instance is already running (PID: {old_pid})!")
                print("Only one engine instance is allowed to prevent duplicate orders & conflicts.")
                send_alert(f"⚠️ *Duplicate Instance Blocked*\nAn attempt was made to launch a second instance while PID `{old_pid}` is active.")
                sys.exit(1)
        except Exception:
            pass

    PID_FILE.write_text(str(os.getpid()))


def release_instance_lock():
    try:
        if PID_FILE.exists():
            PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# --- Background Workers with Auto-Recovery & Alerts ---

def run_scanner_worker():
    """Background scheduler for Scanner (06:02) and Ranker (:12) with auto-recovery."""
    while True:
        try:
            sched = BackgroundScheduler(timezone="Asia/Dhaka")
            sched.add_job(
                run_scan,
                CronTrigger(hour=6, minute=2, timezone="Asia/Dhaka"),
                max_instances=1,
                coalesce=True,
            )
            sched.add_job(
                run_rank,
                CronTrigger(minute=12, timezone="Asia/Dhaka"),
                max_instances=1,
                coalesce=True,
            )
            from app.notifier import send_hourly_report
            sched.add_job(
                send_hourly_report,
                CronTrigger(minute=16, timezone="Asia/Dhaka"),
                max_instances=1,
                coalesce=True,
            )
            sched.start()
            clog.info("Scanner & Ranker Scheduler started (06:02 & :12 BDT)")

            # Auto-bootstrap on fresh start or empty database
            def _initial_bootstrap():
                for attempt in range(90):  # wait up to 15 minutes for collector to populate data
                    try:
                        c = Storage().conn
                        has_u_tbl = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='universe'").fetchone()
                        u_count = c.execute("SELECT COUNT(*) FROM universe").fetchone()[0] if has_u_tbl else 0
                        has_k_tbl = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='klines'").fetchone()
                        k_count = c.execute("SELECT COUNT(*) FROM klines WHERE interval='1d'").fetchone()[0] if has_k_tbl else 0
                        has_wl_tbl = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='watchlist'").fetchone()
                        wl_count = c.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] if has_wl_tbl else 0
                        c.close()

                        if wl_count > 0:
                            break

                        if u_count > 0 and k_count >= 20:
                            clog.info(f"Bootstrap data ready ({u_count} symbols, {k_count} daily klines). Running scan & rank...")
                            run_scan(wait=False)
                            time.sleep(2)
                            run_rank(wait=False)
                            clog.info("Initial scan & rank bootstrap complete!")
                            break
                    except Exception as be:
                        clog.warning(f"Bootstrap check retry: {be}")
                    time.sleep(10)

            threading.Thread(target=_initial_bootstrap, daemon=True, name="BootstrapThread").start()

            # Keep scheduler thread alive
            while sched.running:
                time.sleep(30)
        except Exception as e:
            clog.error(f"Scanner scheduler encountered error: {e}")
            send_alert(f"⚠️ *Scanner Scheduler Warning*\nError: `{e}`. Auto-recovering in 10s...")
            time.sleep(10)


def run_trader_worker():
    """Background trader scheduler with auto-recovery."""
    dry_mode = os.getenv("TRADER_DRY_MODE", "false").lower() == "true"
    mode_str = "SAFE DRY RUN MODE (Paper)" if dry_mode else "BINANCE DEMO FUTURES (Live Demo Orders)"
    tlog.info(f"Trader Engine starting in {mode_str}")

    while True:
        try:
            eng = Engine(dry=dry_mode)
            try:
                eng.startup()
            except Exception as se:
                tlog.warning(f"Trader startup check: {se}")

            sched = BackgroundScheduler(timezone="Asia/Dhaka")
            sched.add_job(
                eng.cycle,
                CronTrigger(minute="0,15,30,45", second=tc.CYCLE_SECOND, timezone="Asia/Dhaka"),
                max_instances=1,
                coalesce=True,
                misfire_grace_time=120,
            )
            # Workflow 2: Gemini AI Autonomous Engine Cycle
            ai_eng = AIEngine(dry=dry_mode)
            sched.add_job(
                ai_eng.cycle,
                CronTrigger(minute="0,15,30,45", second=tc.CYCLE_SECOND + 10, timezone="Asia/Dhaka"),
                max_instances=1,
                coalesce=True,
                misfire_grace_time=120,
            )
            sched.add_job(
                eng.reconcile_job,
                IntervalTrigger(seconds=60),
                max_instances=1,
                coalesce=True,
            )
            sched.start()
            tlog.info(f"Trader Engine & Gemini AI Engine running | 15m Cycles active (+{tc.CYCLE_SECOND}s / +{tc.CYCLE_SECOND + 10}s)")

            while sched.running:
                time.sleep(30)
        except Exception as e:
            tlog.critical(f"Trader Engine exception: {e}")
            send_alert(f"🚨 *Trader Engine Alert*\nError: `{e}`. Auto-recovering trader in 15s...")
            time.sleep(15)


# Track background workers for Watchdog supervision
worker_threads = {
    "collector": None,
    "scanner": None,
    "trader": None,
}


def start_collector_thread():
    """Run async collector in background thread with auto-supervision and Telegram alert."""
    def _run():
        crash_count = 0
        while True:
            clog.info("Starting WebSocket Data Collector in background thread...")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(run_collector())
            except Exception as e:
                crash_count += 1
                clog.critical(f"Collector thread exception: {e!r}")
                try:
                    from app.notifier import send_watchdog_alert
                    send_watchdog_alert(
                        title="WebSocket Collector Disconnected",
                        component="Data Pipeline (WebSocket Collector)",
                        issue=f"WebSocket collector stream dropped: {e!r}",
                        action=f"Auto-restarting stream attempt #{crash_count}...",
                        severity="WARNING"
                    )
                except Exception:
                    send_alert(f"⚠️ *Collector Alert*\nWebSocket Collector dropped: `{e!r}`\nAuto-restarting attempt #{crash_count}...")
            finally:
                loop.close()
            time.sleep(5)

    t = threading.Thread(target=_run, daemon=True, name="CollectorSupervisionThread")
    t.start()
    worker_threads["collector"] = t
    return t


def start_scanner_thread():
    """Start scanner and ranker worker thread supervised by Watchdog."""
    t = threading.Thread(target=run_scanner_worker, daemon=True, name="ScannerThread")
    t.start()
    worker_threads["scanner"] = t
    return t


def start_trader_thread():
    """Start trader engine worker thread supervised by Watchdog."""
    t = threading.Thread(target=run_trader_worker, daemon=True, name="TraderThread")
    t.start()
    worker_threads["trader"] = t
    return t


def start_health_monitor():
    """Unified Watchdog Guardian: Continuous supervision of threads, kline freshness, and trade safety."""
    def _watchdog_loop():
        alert_cooldowns = {}
        stale_alerted = False

        while True:
            time.sleep(30)  # Check every 30 seconds
            now = time.time()

            def should_alert(key: str, cooldown_secs: int = 1800) -> bool:
                last = alert_cooldowns.get(key, 0)
                if now - last >= cooldown_secs:
                    alert_cooldowns[key] = now
                    return True
                return False

            try:
                from app.notifier import send_watchdog_alert
            except Exception:
                send_watchdog_alert = None

            # 1. Thread Liveness Check
            if worker_threads.get("collector") and not worker_threads["collector"].is_alive():
                clog.critical("Watchdog: CollectorSupervisionThread died! Auto-restarting...")
                start_collector_thread()
                if send_watchdog_alert and should_alert("thread_collector", 600):
                    send_watchdog_alert(
                        title="Collector Thread Crash",
                        component="Data Pipeline",
                        issue="WebSocket Collector supervision thread died unexpectedly.",
                        action="Thread auto-restarted by Watchdog.",
                        severity="CRITICAL"
                    )

            if worker_threads.get("scanner") and not worker_threads["scanner"].is_alive():
                clog.critical("Watchdog: ScannerThread died! Auto-restarting...")
                start_scanner_thread()
                if send_watchdog_alert and should_alert("thread_scanner", 600):
                    send_watchdog_alert(
                        title="Scanner Thread Crash",
                        component="Scanner & Hourly Ranker",
                        issue="Scanner scheduler thread died unexpectedly.",
                        action="Thread auto-restarted by Watchdog.",
                        severity="CRITICAL"
                    )

            if worker_threads.get("trader") and not worker_threads["trader"].is_alive():
                clog.critical("Watchdog: TraderThread died! Auto-restarting...")
                start_trader_thread()
                if send_watchdog_alert and should_alert("thread_trader", 600):
                    send_watchdog_alert(
                        title="Trader Engine Thread Crash",
                        component="Trade Engine",
                        issue="Trader execution thread died unexpectedly.",
                        action="Thread auto-restarted by Watchdog.",
                        severity="CRITICAL"
                    )

            # 2. Market Data Freshness Check
            try:
                con = Storage().conn
                r = con.execute("SELECT MAX(open_time) FROM klines WHERE interval='15m'").fetchone()
                if r and r[0]:
                    age_min = (now * 1000 - r[0]) / 60000
                    if age_min > 25:
                        if send_watchdog_alert and should_alert("kline_stale", 1800):
                            send_watchdog_alert(
                                title="Market Data Delay Detected",
                                component="Klines Pipeline",
                                issue=f"Latest 15m candle delay is {int(age_min)} minutes. Data stream may be lagging.",
                                action="Supervising WebSocket streams.",
                                severity="WARNING"
                            )
                            stale_alerted = True
                    elif stale_alerted and age_min <= 18:
                        stale_alerted = False
                        if send_watchdog_alert:
                            send_watchdog_alert(
                                title="Market Data Pipeline Restored",
                                component="Klines Pipeline",
                                issue=f"Fresh 15m candles arriving normally (delay: {int(age_min)}m).",
                                action="Pipeline healthy.",
                                severity="RESOLVED"
                            )

                # 3. Emergency Unprotected Positions Check
                has_pos_tbl = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='positions'").fetchone()
                if has_pos_tbl:
                    unprot = con.execute(
                        "SELECT id, symbol, side, qty FROM positions WHERE status='EMERGENCY_UNPROTECTED'"
                    ).fetchall()
                    if unprot and send_watchdog_alert and should_alert("unprotected_pos", 900):
                        syms = ", ".join(f"{p[1]} ({p[2]})" for p in unprot)
                        send_watchdog_alert(
                            title="CRITICAL: Unprotected Position Detected",
                            component="Position Protection",
                            issue=f"Positions {syms} are OPEN without Stop Loss order protection!",
                            action="Watchdog flagging for emergency reconciliation.",
                            severity="CRITICAL"
                        )
                con.close()
            except Exception as dbe:
                clog.warning(f"Watchdog database check error: {dbe}")

    t = threading.Thread(target=_watchdog_loop, daemon=True, name="UnifiedWatchdogThread")
    t.start()
    return t


def main():
    acquire_instance_lock()

    print("=" * 65)
    print("       TRADING WITH CLOUDI - UNIFIED GUARDIAN SYSTEM")
    print("=" * 65)
    print(" [✓] Duplicate Protection : ACTIVE (Single Instance Enforced)")
    print(" [✓] Safe Trading Mode     : DRY RUN ONLY (No real order placed)")
    print(" [✓] Auto-Recovery        : ACTIVE (Self-Healing Watchdogs)")
    print(" [✓] Telegram Alerts      : CONFIGURED (Notifies on drop/stale)")
    print("=" * 65)

    try:
        from app.notifier import send_bot_online_alert
        send_bot_online_alert(mode="DEMO" if config.USE_DEMO else "LIVE")
    except Exception as se:
        clog.warning(f"Telegram startup alert error: {se}")

    # 1. Start Collector (Auto-Recovering)
    print("[1/4] Starting Live WebSocket Data Collector...")
    start_collector_thread()
    time.sleep(1)

    # 2. Start Scanner & Hourly Ranker (Auto-Recovering)
    print("[2/4] Starting Daily Scanner & Hourly Ranker Scheduler...")
    start_scanner_thread()
    time.sleep(1)

    # 3. Start Trader Engine (Auto-Recovering, Safe DRY Mode)
    print("[3/4] Starting Trader Engine (Safe DRY Mode)...")
    start_trader_thread()
    time.sleep(1)

    # 4. Start Health Monitor Watchdog
    start_health_monitor()

    # 5. Start Dashboard
    print("[4/4] Starting Dashboard on http://localhost:8000 ...")
    print("=" * 65)
    print(" >>> SYSTEM RUNNING IN SINGLE CONSOLE WITH 100% PROTECTION! <<<")
    print(" >>> Open: http://localhost:8000                            <<<")
    print(" >>> Press Ctrl+C anytime to stop all engines cleanly.      <<<")
    print("=" * 65)

    try:
        import webbrowser
        webbrowser.open("http://localhost:8000")
    except Exception:
        pass

    port = int(os.getenv("PORT", 8000))
    host = "0.0.0.0"
    try:
        uvicorn.run(app_ui.app, host=host, port=port, log_level="warning")
    finally:
        release_instance_lock()
        send_alert("🛑 *Trading System Stopped*\nAll background services have been stopped cleanly.")


if __name__ == "__main__":
    main()
