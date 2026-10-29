import sqlite3
from datetime import datetime, timezone, timedelta
from app import config

BDT = timezone(timedelta(hours=6))


def fmt(ms):
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=BDT).strftime("%Y-%m-%d %H:%M")


con = sqlite3.connect(str(config.DB_PATH))
n = con.execute("SELECT COUNT(*) FROM universe").fetchone()[0]
print(f"Universe symbols: {n}\n")

print("== KLINES ==")
for iv, cnt, syms, last in con.execute(
    "SELECT interval, COUNT(*), COUNT(DISTINCT symbol), MAX(open_time) "
    "FROM klines GROUP BY interval"
):
    print(f"{iv:>4} | rows={cnt:>8} | symbols={syms:>4} | latest(BDT)={fmt(last)}")

print("\n== EXTRA DATA ==")
tables = [
    ("premium", "ts"), ("open_interest", "ts"), ("oi_hist", "ts"),
    ("funding_hist", "funding_time"), ("ls_ratio", "ts"),
    ("taker_ratio", "ts"), ("exchange_info", "updated_at"),
]
for t, col in tables:
    cnt, syms, last = con.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT symbol), MAX({col}) FROM {t}"
    ).fetchone()
    print(f"{t:>14} | rows={cnt:>8} | symbols={syms:>4} | latest(BDT)={fmt(last)}")