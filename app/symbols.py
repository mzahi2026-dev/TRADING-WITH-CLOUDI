import time
from . import config
from .logger import log


def get_symbols(client):
    """Returns list of (symbol, quote_volume_24h, listing_age_days)."""
    info = client.get("/fapi/v1/exchangeInfo")
    now_ms = time.time() * 1000

    eligible = {}
    for s in info["symbols"]:
        if s.get("status") != "TRADING":
            continue
        if s.get("contractType") != "PERPETUAL":
            continue
        if s.get("quoteAsset") != "USDT":
            continue
        age_days = (now_ms - s.get("onboardDate", 0)) / 86_400_000
        eligible[s["symbol"]] = age_days

    tickers = client.get("/fapi/v1/ticker/24hr")
    volume = {t["symbol"]: float(t["quoteVolume"]) for t in tickers}

    black = {x.upper() for x in config.BLACKLIST}
    white = [x.upper() for x in config.WHITELIST]

    passed = []
    for sym, age in eligible.items():
        if sym in black:
            continue
        v = volume.get(sym, 0.0)
        if v < config.MIN_QUOTE_VOLUME_24H:
            continue
        if age < config.MIN_LISTING_AGE_DAYS:
            continue
        passed.append((sym, v, age))

    passed.sort(key=lambda x: x[1], reverse=True)
    passed = passed[: config.TOP_N]

    have = {p[0] for p in passed}
    for sym in white:
        if sym in eligible and sym not in have and sym not in black:
            passed.append((sym, volume.get(sym, 0.0), eligible[sym]))

    log.info(
        f"Symbols: eligible={len(eligible)} selected={len(passed)} "
        f"(min vol {config.MIN_QUOTE_VOLUME_24H:,.0f}, "
        f"min age {config.MIN_LISTING_AGE_DAYS}d, top {config.TOP_N})"
    )
    return passed