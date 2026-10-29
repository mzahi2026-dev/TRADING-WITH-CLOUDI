import logging
import sys
from logging.handlers import RotatingFileHandler
from . import config

config.LOG_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("collector")
log.setLevel(logging.INFO)

if not log.handlers:
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    fh = RotatingFileHandler(
        config.LOG_DIR / "collector.log",
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)