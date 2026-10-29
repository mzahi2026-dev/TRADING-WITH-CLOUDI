import sqlite3

import pandas as pd

from app import config

con = sqlite3.connect(str(config.DB_PATH))
try:
    df = pd.read_sql_query("SELECT * FROM positions", con)
    sg = pd.read_sql_query("SELECT action, COUNT(*) AS n FROM signals GROUP BY action", con)
except Exception:
    print("positions/signals table nai. Age trade_main.py chalan.")
    raise SystemExit

print("== SIGNALS ==")
print(sg.to_string(index=False) if not sg.empty else "kono signal nai")

closed = df[(df["status"] == "CLOSED") & df["pnl"].notna()].copy()
openp = df[df["status"] == "OPEN"]
print(f"\nOpen position: {len(openp)}")
if not openp.empty:
    print(openp[["time", "symbol", "strategy", "side", "entry", "sl", "tp"]].to_string(index=False))

if closed.empty:
    print("\nEkhono kono closed trade nai.")
    raise SystemExit

closed["risk"] = closed["qty"] * (closed["entry"] - closed["sl"]).abs()
closed["R"] = closed["pnl"] / closed["risk"].replace(0, float("nan"))
closed["win"] = closed["pnl"] > 0


def summ(g):
    return pd.Series({
        "trades": len(g), "wins": int(g["win"].sum()),
        "win%": round(g["win"].mean() * 100, 1),
        "TP": int((g["result"] == "TP").sum()), "SL": int((g["result"] == "SL").sum()),
        "net_pnl": round(g["pnl"].sum(), 3), "fees": round(g["fee"].sum(), 3),
        "avg_pnl": round(g["pnl"].mean(), 3), "avg_R": round(g["R"].mean(), 2),
    })


print("\n== STRATEGY WISE (closed, net of fee) ==")
print(closed.groupby("strategy").apply(summ).to_string())
print("\n== SIDE WISE ==")
print(closed.groupby("side").apply(summ).to_string())
print("\n== TOTAL ==")
print(summ(closed).to_string())