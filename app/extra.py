import asyncio
import time

from .logger import log

SLEEP = 0.5  # futures/data endpoint limit: 1000 req / 5 min


def _sleep():
    time.sleep(SLEEP)


# ------------------------------------------------------------ fast (15m)
def job_fast(client, storage, symbols, full=False):
    sset = set(symbols)

    # mark / index / funding (ek call e shob symbol)
    data = client.get("/fapi/v1/premiumIndex")
    rows = []
    for d in data:
        if d["symbol"] not in sset:
            continue
        rows.append((
            d["symbol"], int(d["time"]),
            float(d["markPrice"]), float(d["indexPrice"] or 0),
            float(d["lastFundingRate"] or 0), int(d["nextFundingTime"]),
        ))
    storage.insert("INSERT OR REPLACE INTO premium VALUES (?,?,?,?,?,?)", rows)

    # current open interest
    rows = []
    for sym in symbols:
        try:
            d = client.get("/fapi/v1/openInterest", {"symbol": sym})
            rows.append((sym, int(d["time"]), float(d["openInterest"])))
        except Exception as e:
            log.warning(f"OI now {sym}: {e}")
        time.sleep(0.2)
    storage.insert("INSERT OR REPLACE INTO open_interest VALUES (?,?,?)", rows)


# ------------------------------------------------------------ hourly
def _oi_hist(client, storage, sym, period, limit):
    data = client.get("/futures/data/openInterestHist",
                      {"symbol": sym, "period": period, "limit": limit})
    rows = [(sym, period, int(d["timestamp"]),
             float(d["sumOpenInterest"]), float(d["sumOpenInterestValue"]))
            for d in data]
    storage.insert("INSERT OR REPLACE INTO oi_hist VALUES (?,?,?,?,?)", rows)


LS_KINDS = {
    "top_account": "/futures/data/topLongShortAccountRatio",
    "top_position": "/futures/data/topLongShortPositionRatio",
    "global_account": "/futures/data/globalLongShortAccountRatio",
}


def _ls(client, storage, sym, kind, path, period, limit):
    data = client.get(path, {"symbol": sym, "period": period, "limit": limit})
    rows = [(kind, sym, period, int(d["timestamp"]),
             float(d["longShortRatio"]), float(d["longAccount"]),
             float(d["shortAccount"]))
            for d in data]
    storage.insert("INSERT OR REPLACE INTO ls_ratio VALUES (?,?,?,?,?,?,?)", rows)


def _taker(client, storage, sym, period, limit):
    data = client.get("/futures/data/takerlongshortRatio",
                      {"symbol": sym, "period": period, "limit": limit})
    rows = [(sym, period, int(d["timestamp"]),
             float(d["buySellRatio"]), float(d["buyVol"]), float(d["sellVol"]))
            for d in data]
    storage.insert("INSERT OR REPLACE INTO taker_ratio VALUES (?,?,?,?,?,?)", rows)


def _funding(client, storage, sym):
    last = storage.scalar(
        "SELECT MAX(funding_time) FROM funding_hist WHERE symbol=?", (sym,))
    params = {"symbol": sym, "limit": 1000}
    if last:
        params["startTime"] = int(last) + 1
    data = client.get("/fapi/v1/fundingRate", params)
    rows = [(sym, int(d["fundingTime"]), float(d["fundingRate"]),
             float(d.get("markPrice") or 0)) for d in data]
    storage.insert("INSERT OR REPLACE INTO funding_hist VALUES (?,?,?,?)", rows)


def job_hourly(client, storage, symbols, full=False):
    limit = 500 if full else 30
    total = len(symbols)
    for i, sym in enumerate(symbols, 1):
        steps = []
        for p in ("1h", "4h", "1d"):
            steps.append(lambda p=p: _oi_hist(client, storage, sym, p, limit))
        for kind, path in LS_KINDS.items():
            steps.append(lambda k=kind, pa=path:
                         _ls(client, storage, sym, k, pa, "1h", limit))
        steps.append(lambda: _taker(client, storage, sym, "1h", limit))
        steps.append(lambda: _funding(client, storage, sym))

        for step in steps:
            try:
                step()
            except Exception as e:
                log.warning(f"hourly {sym}: {e}")
            _sleep()
        if i % 20 == 0 or i == total:
            log.info(f"Hourly data {i}/{total}")


# ------------------------------------------------------------ exchange info
def job_info(client, storage, symbols, full=False):
    info = client.get("/fapi/v1/exchangeInfo")
    sset = set(symbols)
    now = int(time.time() * 1000)
    rows = []
    for s in info["symbols"]:
        if s["symbol"] not in sset:
            continue
        tick = step = minq = notional = 0.0
        for f in s.get("filters", []):
            t = f.get("filterType")
            if t == "PRICE_FILTER":
                tick = float(f["tickSize"])
            elif t == "LOT_SIZE":
                step = float(f["stepSize"])
                minq = float(f["minQty"])
            elif t == "MIN_NOTIONAL":
                notional = float(f.get("notional", 0))
        rows.append((s["symbol"], tick, step, minq, notional,
                     int(s.get("pricePrecision", 0)),
                     int(s.get("quantityPrecision", 0)), now))
    storage.insert("INSERT OR REPLACE INTO exchange_info VALUES (?,?,?,?,?,?,?,?)", rows)
    log.info(f"Exchange info saved: {len(rows)} symbols")


# ------------------------------------------------------------ scheduler
def _next(period, offset):
    now = time.time()
    return (int(now // period) + 1) * period + offset


async def _run_job(name, period, offset, fn, client, storage, get_symbols):
    full = True
    while True:
        try:
            await asyncio.to_thread(fn, client, storage, list(get_symbols()), full)
            log.info(f"Job '{name}' done")
        except Exception as e:
            log.error(f"Job '{name}' failed: {e}")
        full = False
        await asyncio.sleep(max(1, _next(period, offset) - time.time()))


def start_extra(client, storage, get_symbols):
    return [
        asyncio.create_task(_run_job("fast", 900, 10, job_fast, client, storage, get_symbols)),
        asyncio.create_task(_run_job("hourly", 3600, 30, job_hourly, client, storage, get_symbols)),
        asyncio.create_task(_run_job("info", 6 * 3600, 60, job_info, client, storage, get_symbols)),
    ]