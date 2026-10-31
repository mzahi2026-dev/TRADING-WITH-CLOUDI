import sys
import app

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import trade_config as tc
from app.trade_engine import Engine
from app.trade_log import log

if __name__ == "__main__":
    if "--check" in sys.argv:
        Engine(dry=False).check()
        sys.exit(0)

    dry = "--dry" in sys.argv
    eng = Engine(dry=dry)
    eng.startup()

    if "--now" in sys.argv:          # ekbar cycle chalie ber hoy
        eng.cycle()
        sys.exit(0)

    sched = BlockingScheduler(timezone="Asia/Dhaka")
    sched.add_job(eng.cycle,
                  CronTrigger(minute="0,15,30,45", second=tc.CYCLE_SECOND, timezone="Asia/Dhaka"),
                  max_instances=1, coalesce=True, misfire_grace_time=120)
    sched.add_job(eng.reconcile_job, IntervalTrigger(seconds=60),
                  max_instances=1, coalesce=True)
    log.info("Scheduler chalu. Proti 15m candle close er por entry scan. (Ctrl+C = bondho)")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        print("Stopped.")