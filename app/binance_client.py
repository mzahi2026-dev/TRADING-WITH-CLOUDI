import time
import requests
from . import config
from .logger import log


class BinanceClient:
    def __init__(self):
        self.session = requests.Session()
        if config.API_KEY:
            self.session.headers["X-MBX-APIKEY"] = config.API_KEY
        self.used_weight = 0

    def get(self, path, params=None):
        url = config.FAPI_BASE + path
        for attempt in range(5):
            try:
                r = self.session.get(url, params=params, timeout=config.REQUEST_TIMEOUT)
            except requests.RequestException as e:
                log.warning(f"Network error {path}: {e} (retry {attempt + 1})")
                time.sleep(2 * (attempt + 1))
                continue

            w = r.headers.get("X-MBX-USED-WEIGHT-1M")
            if w:
                self.used_weight = int(w)

            if r.status_code in (429, 418):
                try:
                    retry_hdr = int(r.headers.get("Retry-After", "30"))
                except (ValueError, TypeError):
                    retry_hdr = 30
                wait = min(retry_hdr, 60)
                log.warning(f"Rate limited ({r.status_code}). Sleeping {wait}s")
                time.sleep(wait)
                continue

            if r.status_code in (403, 451):
                raise RuntimeError(
                    f"Binance access blocked (HTTP {r.status_code}). "
                    "VPN on kore abar chalan."
                )

            r.raise_for_status()

            if self.used_weight > config.WEIGHT_SOFT_LIMIT:
                log.info(f"Weight {self.used_weight} high, cooling down 15s")
                time.sleep(15)

            try:
                return r.json()
            except Exception:
                return []

        raise RuntimeError(f"Request failed after retries: {path}")