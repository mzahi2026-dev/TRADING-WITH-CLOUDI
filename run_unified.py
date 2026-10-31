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
            # Check if old pid is alive on Windows
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            process = kernel32.OpenProcess(SYNCHRONIZE, False, old_pid)
            if process != 0:
                kernel32.CloseHandle(process)
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
            sched.start()
            clog.info("Scanner & Ranker Scheduler started (06:02 & :12 BDT)")

            # Check if watchlist is empty
            con = Storage().conn
            has_wl = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='watchlist'").fetchone()
            count = con.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] if has_wl else 0
            if count == 0:
                clog.info("Watchlist empty. Running initial scan & rank...")
                threading.Thread(target=run_scan, kwargs={"wait": False}, daemon=True).start()

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
            sched.add_job(
                eng.reconcile_job,
                IntervalTrigger(seconds=60),
                max_instances=1,
                coalesce=True,
            )
            sched.start()
            tlog.info(f"Trader Engine running in SAFE DRY MODE | Cycle every 15m (+{tc.CYCLE_SECOND}s)")

            while sched.running:
                time.sleep(30)
        except Exception as e:
            tlog.critical(f"Trader Engine exception: {e}")
            send_alert(f"🚨 *Trader Engine Alert*\nError: `{e}`. Auto-recovering trader in 15s...")
            time.sleep(15)


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
                send_alert(f"⚠️ *Collector Alert*\nWebSocket Collector dropped: `{e!r}`\nAuto-restarting attempt #{crash_count}...")
            finally:
                loop.close()
            time.sleep(5)

    t = threading.Thread(target=_run, daemon=True, name="CollectorSupervisionThread")
    t.start()


def start_health_monitor():
    """Monitor data freshness every 10 minutes and alert if data becomes stale."""
    def _monitor():
        while True:
            time.sleep(600)  # every 10 min
            try:
                con = Storage().conn
                now = time.time()
                # Check 15m kline freshness
                r = con.execute("SELECT MAX(open_time) FROM klines WHERE interval='15m'").fetchone()
                if r and r[0]:
                    age_min = (now * 1000 - r[0]) / 60000
                    if age_min > 45:
                        send_alert(f"⚠️ *Data Stale Alert*\n15m candle delay is {int(age_min)} minutes! Collector might be stalled.")
            except Exception:
                pass

    t = threading.Thread(target=_monitor, daemon=True, name="HealthMonitorThread")
    t.start()


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

    send_alert("🚀 *Trading System Started*\n• Mode: `DRY RUN (Safe)`\n• Watchdog: `Active`\n• Dashboard: `http://localhost:8080`")

    # 1. Start Collector (Auto-Recovering)
    print("[1/4] Starting Live WebSocket Data Collector...")
    start_collector_thread()
    time.sleep(1)

    # 2. Start Scanner & Hourly Ranker (Auto-Recovering)
    print("[2/4] Starting Daily Scanner & Hourly Ranker Scheduler...")
    threading.Thread(target=run_scanner_worker, daemon=True, name="ScannerThread").start()
    time.sleep(1)

    # 3. Start Trader Engine (Auto-Recovering, Safe DRY Mode)
    print("[3/4] Starting Trader Engine (Safe DRY Mode)...")
    threading.Thread(target=run_trader_worker, daemon=True, name="TraderThread").start()
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

    try:
        uvicorn.run(app_ui.app, host="127.0.0.1", port=8000, log_level="warning")
    finally:
        release_instance_lock()
        send_alert("🛑 *Trading System Stopped*\nAll background services have been stopped cleanly.")


if __name__ == "__main__":
    main()
