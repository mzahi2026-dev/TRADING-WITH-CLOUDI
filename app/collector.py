import asyncio
import json
import time

import websockets

from . import config
from .binance_client import BinanceClient
from .extra import start_extra
from .logger import log
from .storage import Storage
from .symbols import get_symbols


# ------------------------------------------------------------------ REST
def parse_rest(symbol, interval, r):
    return (
        symbol, interval, int(r[0]),
        float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]),
        int(r[6]), float(r[7]), int(r[8]), float(r[9]), float(r[10]),
    )


def backfill_symbol(client, storage, symbol):
    now_ms = int(time.time() * 1000)
    for interval in config.INTERVALS:
        ms = config.INTERVAL_MS[interval]
        last = storage.last_open_time(symbol, interval)

        if last is None:
            start = None
            limit = config.HISTORY_LIMIT
        else:
            if now_ms < last + 2 * ms:
                continue  # notun closed candle nai
            start = last + 1
            limit = min(config.HISTORY_LIMIT, (now_ms - last) // ms + 1)

        while True:
            params = {"symbol": symbol, "interval": interval, "limit": limit}
            if start is not None:
                params["startTime"] = start
            data = client.get("/fapi/v1/klines", params)
            time.sleep(config.REQUEST_SLEEP)
            if not data:
                break

            # shudhu closed candle save
            rows = [parse_rest(symbol, interval, r) for r in data if int(r[6]) < now_ms]
            storage.upsert_many(rows)

            if start is None or len(data) < limit:
                break
            start = int(data[-1][0]) + 1
            limit = config.HISTORY_LIMIT


def backfill_symbols(client, storage, symbols, quiet=False):
    total = len(symbols)
    for i, sym in enumerate(symbols, 1):
        try:
            backfill_symbol(client, storage, sym)
        except Exception as e:
            log.error(f"Backfill failed {sym}: {e}")
        if not quiet and (i % 10 == 0 or i == total):
            log.info(f"Backfill {i}/{total}")


# ------------------------------------------------------------------ WebSocket
async def ws_worker(name, symbols, queue, client, storage):
    streams = "/".join(
        f"{s.lower()}@kline_{iv}" for s in symbols for iv in config.INTERVALS
    )
    url = f"{config.WS_BASE}/stream?streams={streams}"
    first = True

    while True:
        try:
            async with websockets.connect(
                url, ping_interval=20, ping_timeout=30, max_queue=None
            ) as ws:
                log.info(f"WS-{name} connected ({len(symbols)} symbols)")
                if not first:
                    # reconnect er gap fill
                    await asyncio.to_thread(backfill_symbols, client, storage, symbols)
                first = False

                while True:
                    # 120s kono message na ashle connection dead dhore reconnect
                    raw = await asyncio.wait_for(ws.recv(), timeout=120)
                    msg = json.loads(raw)
                    d = msg.get("data", {})
                    if d.get("e") != "kline":
                        continue
                    k = d["k"]
                    if not k["x"]:  # candle ekhono close hoyni
                        continue
                    await queue.put((
                        d["s"], k["i"], int(k["t"]),
                        float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]),
                        float(k["v"]), int(k["T"]), float(k["q"]), int(k["n"]),
                        float(k["V"]), float(k["Q"]),
                    ))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"WS-{name} dropped: {e!r}. Reconnect in 5s")
            await asyncio.sleep(5)


async def writer(queue, storage):
    while True:
        buf = [await queue.get()]
        while not queue.empty() and len(buf) < 1000:
            buf.append(queue.get_nowait())
        for attempt in range(3):
            try:
                await asyncio.to_thread(storage.upsert_many, buf)
                break
            except Exception as e:
                log.error(f"Writer DB error (try {attempt + 1}/3): {e!r}")
                await asyncio.sleep(2)


def start_ws(symbols, queue, client, storage):
    n = config.WS_SYMBOLS_PER_CONN
    chunks = [symbols[i:i + n] for i in range(0, len(symbols), n)]
    return [
        asyncio.create_task(ws_worker(i + 1, c, queue, client, storage))
        for i, c in enumerate(chunks)
    ]


# ------------------------------------------------------------------ Gap-fill safety net
async def gapfill_loop(client, storage, get_syms):
    """Proti 15 min e REST diye missing closed candle fill (WS atkle o data miss hobe na)."""
    period = 900
    while True:
        now = time.time()
        nxt = (int(now // period) + 1) * period + 20
        await asyncio.sleep(max(1, nxt - now))
        try:
            await asyncio.to_thread(backfill_symbols, client, storage, list(get_syms()), True)
            log.info("Gap-fill done")
        except Exception as e:
            log.error(f"Gap-fill failed: {e!r}")


# ------------------------------------------------------------------ Main loop
def fetch_universe(client, storage):
    items = get_symbols(client)
    storage.save_universe(items)
    return [s for s, _, _ in items]


async def run():
    storage = Storage()
    client = BinanceClient()
    queue = asyncio.Queue()

    writer_task = asyncio.create_task(writer(queue, storage))

    symbols = await asyncio.to_thread(fetch_universe, client, storage)
    ws_tasks = start_ws(symbols, queue, client, storage)

    log.info("Initial history download shuru (kichu minute lagbe)...")
    await asyncio.to_thread(backfill_symbols, client, storage, symbols)
    log.info("Kline history done. Extra data jobs shuru (OI, funding, L/S, taker)...")

    extra_tasks = start_extra(client, storage, lambda: symbols)
    gap_task = asyncio.create_task(gapfill_loop(client, storage, lambda: symbols))

    while True:
        await asyncio.sleep(config.SYMBOL_REFRESH_HOURS * 3600)
        try:
            new_symbols = await asyncio.to_thread(fetch_universe, client, storage)
        except Exception as e:
            log.error(f"Symbol refresh failed: {e}")
            continue

        if set(new_symbols) != set(symbols):
            added = set(new_symbols) - set(symbols)
            removed = set(symbols) - set(new_symbols)
            log.info(f"Universe changed. +{len(added)} -{len(removed)}")
            for t in ws_tasks:
                t.cancel()
            await asyncio.gather(*ws_tasks, return_exceptions=True)
            symbols = new_symbols
            ws_tasks = start_ws(symbols, queue, client, storage)
            await asyncio.to_thread(backfill_symbols, client, storage, symbols)