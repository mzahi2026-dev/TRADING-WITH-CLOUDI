import sqlite3
import threading
import time
from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS klines (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    open_time INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL,
    close_time INTEGER,
    quote_volume REAL,
    trades INTEGER,
    taker_buy_base REAL,
    taker_buy_quote REAL,
    PRIMARY KEY (symbol, interval, open_time)
);
CREATE TABLE IF NOT EXISTS universe (
    symbol TEXT PRIMARY KEY,
    quote_volume_24h REAL,
    listing_age_days REAL,
    updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS premium (
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    mark_price REAL,
    index_price REAL,
    funding_rate REAL,
    next_funding_time INTEGER,
    PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS open_interest (
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    open_interest REAL,
    PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS oi_hist (
    symbol TEXT NOT NULL,
    period TEXT NOT NULL,
    ts INTEGER NOT NULL,
    sum_oi REAL,
    sum_oi_value REAL,
    PRIMARY KEY (symbol, period, ts)
);
CREATE TABLE IF NOT EXISTS funding_hist (
    symbol TEXT NOT NULL,
    funding_time INTEGER NOT NULL,
    funding_rate REAL,
    mark_price REAL,
    PRIMARY KEY (symbol, funding_time)
);
CREATE TABLE IF NOT EXISTS ls_ratio (
    kind TEXT NOT NULL,
    symbol TEXT NOT NULL,
    period TEXT NOT NULL,
    ts INTEGER NOT NULL,
    ratio REAL,
    long_account REAL,
    short_account REAL,
    PRIMARY KEY (kind, symbol, period, ts)
);
CREATE TABLE IF NOT EXISTS taker_ratio (
    symbol TEXT NOT NULL,
    period TEXT NOT NULL,
    ts INTEGER NOT NULL,
    buy_sell_ratio REAL,
    buy_vol REAL,
    sell_vol REAL,
    PRIMARY KEY (symbol, period, ts)
);
CREATE TABLE IF NOT EXISTS exchange_info (
    symbol TEXT PRIMARY KEY,
    tick_size REAL,
    step_size REAL,
    min_qty REAL,
    min_notional REAL,
    price_precision INTEGER,
    quantity_precision INTEGER,
    updated_at INTEGER
);
"""

UPSERT = """
INSERT OR REPLACE INTO klines
(symbol, interval, open_time, open, high, low, close, volume,
 close_time, quote_volume, trades, taker_buy_base, taker_buy_quote)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


class Storage:
    def __init__(self, path=config.DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def upsert_many(self, rows):
        self.insert(UPSERT, rows)

    def insert(self, sql, rows):
        if not rows:
            return
        with self.lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    def scalar(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchone()[0]

    def last_open_time(self, symbol, interval):
        return self.scalar(
            "SELECT MAX(open_time) FROM klines WHERE symbol=? AND interval=?",
            (symbol, interval),
        )

    def find_candle_gaps(self, symbol, interval, limit=300):
        """Returns list of (gap_start, gap_end) where intermediate candles are missing."""
        ms = config.INTERVAL_MS.get(interval)
        if not ms:
            return []
        with self.lock:
            rows = self.conn.execute(
                "SELECT open_time FROM klines WHERE symbol=? AND interval=? ORDER BY open_time ASC LIMIT ?",
                (symbol, interval, limit),
            ).fetchall()
        if len(rows) < 2:
            return []
        gaps = []
        for i in range(1, len(rows)):
            prev_t = rows[i - 1][0]
            curr_t = rows[i][0]
            if curr_t - prev_t > ms:
                gaps.append((prev_t + ms, curr_t - ms))
        return gaps

    def save_universe(self, items):
        now = int(time.time() * 1000)
        with self.lock:
            self.conn.execute("DELETE FROM universe")
            self.conn.executemany(
                "INSERT INTO universe VALUES (?,?,?,?)",
                [(s, v, a, now) for s, v, a in items],
            )
            self.conn.commit()