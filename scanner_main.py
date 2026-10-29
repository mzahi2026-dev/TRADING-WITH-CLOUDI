import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app import config
from app.scanner import run_scan, log
from app.ranker import run_rank


def scan_job():
    try:
        run_scan(wait=True)
    except Exception:
        log.exception("Daily scan failed")


def rank_job():
    try:
        run_rank(wait=True)
    except Exception:
        log.exception("Hourly rank failed")


if __name__ == "__main__":
    if "--now" in sys.argv:            # test: daily scan ekhoni
        run_scan(wait=False)
        sys.exit(0)
    if "--rank-now" in sys.argv:       # test: hourly rank ekhoni
        run_rank(wait=False)
        sys.exit(0)

    sched = BlockingScheduler(timezone="Asia/Dhaka")
    sched.add_job(
        scan_job,
        CronTrigger(hour=config.SCAN_HOUR_BDT, minute=config.SCAN_MINUTE_BDT,
                    timezone="Asia/Dhaka"),
        misfire_grace_time=3600, coalesce=True,
    )
    sched.add_job(
        rank_job,
        CronTrigger(minute=config.RANK_MINUTE_BDT, timezone="Asia/Dhaka"),
        misfire_grace_time=900, coalesce=True,
    )
    log.info(f"Scheduler chalu. Daily scan {config.SCAN_HOUR_BDT:02d}:"
             f"{config.SCAN_MINUTE_BDT:02d} BDT, hourly rank proti ghonta "
             f":{config.RANK_MINUTE_BDT:02d} e. (Ctrl+C = bondho)")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        print("Stopped.")