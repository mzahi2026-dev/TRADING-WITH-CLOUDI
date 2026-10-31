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

# Configure Paths
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import config, trade_config as tc
from app.binance_client import BinanceClient
from app.collector import run as run_collector
from app.logger import log as clog
from app.ranker import run_rank
from app.scanner import run_scan
from app.storage import Storage
from app.symbols import get_symbols
from app.trade_engine import Engine
from app.trade_log import log as tlog
import app_ui


def run_scanner_worker():
    """Background scheduler for Scanner (06:02) and Ranker (:12)."""
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
    clog.info("Scheduler started: Daily Scan (06:02 BDT) & Hourly Rank (:12 BDT)")

    # Run quick check if watchlist is empty
    con = Storage().conn
    has_wl = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='watchlist'").fetchone()
    count = con.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] if has_wl else 0
    if count == 0:
        clog.info("Watchlist empty. Running initial scan & rank...")
        threading.Thread(target=run_scan, kwargs={"wait": False}, daemon=True).start()


def run_trader_worker():
    """Background scheduler for Trader Engine."""
    dry_mode = "--live" not in sys.argv
    mode_str = "DEMO LIVE" if not dry_mode else "DRY RUN (Safe)"
    tlog.info(f"Starting Trader Engine in background | Mode: {mode_str}")

    eng = Engine(dry=dry_mode)
    try:
        eng.startup()
    except Exception as e:
        tlog.warning(f"Trader startup notice: {e}")

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
    tlog.info(f"Trader Engine active | Cycles every 15m candle close (+{tc.CYCLE_SECOND}s)")


def start_collector_thread():
    """Run async collector in background daemon thread."""
    def _run():
        clog.info("Starting WebSocket Data Collector in background...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run_collector())
        except Exception as e:
            clog.error(f"Collector encountered error: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def main():
    print("=" * 60)
    print("       TRADING WITH CLOUDI - ALL-IN-ONE ENGINE")
    print("=" * 60)

    # 1. Start WebSocket Collector
    print("[1/4] Starting Live Market Data Collector (WebSocket)...")
    start_collector_thread()
    time.sleep(1)

    # 2. Start Scanner & Hourly Ranker
    print("[2/4] Starting Daily Scanner & Hourly Ranker Scheduler...")
    run_scanner_worker()
    time.sleep(1)

    # 3. Start Trading Engine
    print("[3/4] Starting Trading Engine (Auto-Scan & Reconcile)...")
    run_trader_worker()
    time.sleep(1)

    # 4. Start Dashboard Web Server
    print("[4/4] Starting Dashboard on http://localhost:8080 ...")
    print("=" * 60)
    print(" >>> SYSTEM RUNNING SUCCESSFULLY IN THIS SINGLE WINDOW! <<<")
    print(" >>> Open Browser: http://localhost:8080                <<<")
    print(" >>> Press Ctrl+C anytime to stop all services.        <<<")
    print("=" * 60)

    # Automatically launch default browser
    try:
        import webbrowser
        webbrowser.open("http://localhost:8080")
    except Exception:
        pass

    # Run Uvicorn directly in main thread
    uvicorn.run(app_ui.app, host="127.0.0.1", port=8080, log_level="warning")


if __name__ == "__main__":
    main()
