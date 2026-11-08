import pandas as pd
from app.db import get_db, market_db, has_table


if __name__ == '__main__':
    con = get_db()
    try:
        df = pd.read_sql_query("SELECT * FROM positions", con)
        m_con = market_db()
        sg = pd.read_sql_query("SELECT action, COUNT(*) AS n FROM signals GROUP BY action", con if has_table(con, "signals") else m_con)
        m_con.close()
    except Exception as e:
        print(f"positions/signals table error: {e}")
        raise SystemExit
    finally:
        con.close()

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
