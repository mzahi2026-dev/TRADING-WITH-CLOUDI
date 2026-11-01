import os
import requests
from dotenv import load_dotenv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def send_alert(message: str, force: bool = False, parse_mode: str = "HTML"):
    """Send alert to Telegram if configured."""
    if not BOT_TOKEN or not CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        r = requests.post(url, json=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[Telegram Alert Error]: {e}")
        return False


def send_hourly_report():
    """Compiles and sends a full workflow report to Telegram."""
    if not BOT_TOKEN or not CHAT_ID:
        return False

    import sqlite3, time
    from datetime import datetime, timezone, timedelta
    from . import config, trade_config as tc

    BDT = timezone(timedelta(hours=6))
    now_dt = datetime.now(BDT)
    now_str = now_dt.strftime("%Y-%m-%d %H:%M BDT")

    con = sqlite3.connect(str(config.DB_PATH), timeout=20)
    con.row_factory = sqlite3.Row

    # 1. Universe & Daily Watchlist
    has_u = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='universe'").fetchone()
    u_count = con.execute("SELECT COUNT(*) FROM universe").fetchone()[0] if has_u else 0

    has_wl = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='watchlist'").fetchone()
    wl_count = con.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] if has_wl else 0
    wl_top = con.execute("SELECT symbol, side, score FROM watchlist ORDER BY score DESC LIMIT 3").fetchall() if has_wl else []

    # 2. Hourly Ranking
    has_rk = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ranking'").fetchone()
    latest_rh = con.execute("SELECT MAX(run_hour) FROM ranking").fetchone()[0] if has_rk else None
    rk_top = con.execute("SELECT symbol, rank, side, rank_score FROM ranking WHERE run_hour=? ORDER BY rank ASC LIMIT 5", (latest_rh,)).fetchall() if latest_rh else []
    rk_total = con.execute("SELECT COUNT(*) FROM ranking WHERE run_hour=?", (latest_rh,)).fetchone()[0] if latest_rh else 0

    # 3. Signals in last 1 hour
    has_sig = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='signals'").fetchone()
    recent_signals = con.execute("SELECT symbol, strategy, side, action, note, time FROM signals ORDER BY id DESC LIMIT 5").fetchall() if has_sig else []

    # 4. Open Positions
    has_pos = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='positions'").fetchone()
    open_pos = con.execute("SELECT symbol, side, entry, sl, tp, qty, status, time FROM positions WHERE status IN ('OPEN', 'EMERGENCY_UNPROTECTED')").fetchall() if has_pos else []

    # 5. Closed PnL in last 24h
    day_ago = int(time.time() * 1000) - 24 * 3600 * 1000
    pnl_24h = con.execute("SELECT COALESCE(SUM(pnl), 0) FROM positions WHERE status='CLOSED' AND entry_ms>=?", (day_ago,)).fetchone()[0] if has_pos else 0.0
    closed_count = con.execute("SELECT COUNT(*) FROM positions WHERE status='CLOSED' AND entry_ms>=?", (day_ago,)).fetchone()[0] if has_pos else 0

    con.close()

    # 6. Live Binance Account balance
    bal_text = "Live Demo Connected"
    dry_mode = os.getenv("TRADER_DRY_MODE", "false").lower() == "true"
    if not dry_mode:
        try:
            from .demo_client import DemoClient
            dc = DemoClient()
            av_bal, tot_bal = dc.balance()
            bal_text = f"${av_bal:,.2f} USDT (Total: ${tot_bal:,.2f})"
        except Exception as e:
            bal_text = f"Demo API: {e}"

    msg = [
        "📊 <b>Trading Cloudi — Hourly Workflow Report</b>",
        f"⏰ <b>Time:</b> {now_str}",
        f"🤖 <b>Mode:</b> {'SAFE DRY RUN' if dry_mode else 'BINANCE DEMO FUTURES (Live Orders)'}\n",
        "🌐 <b>1. Universe & Daily Scan:</b>",
        f"• Tracked Universe: <b>{u_count}</b> coins",
        f"• Watchlist Selected: <b>{wl_count}</b> coins",
    ]
    if wl_top:
        wl_str = ", ".join(f"{r['symbol']} ({r['side']})" for r in wl_top)
        msg.append(f"• Top Watchlist: <i>{wl_str}</i>")

    msg.append("\n🏆 <b>2. Hourly Ranker:</b>")
    if rk_top:
        msg.append(f"• Run Hour: <code>{latest_rh}</code> (Top {rk_total} ranked)")
        for r in rk_top:
            msg.append(f"  {r['rank']}. <b>{r['symbol']}</b> — {r['side']} (Score: {r['rank_score']:.1f})")
    else:
        msg.append("• <i>No ranking generated yet (waiting for next :12 hourly candle).</i>")

    msg.append("\n⚡ <b>3. 15m Signal Engine & Execution:</b>")
    if recent_signals:
        msg.append("• Recent 15m Checks & Signals:")
        for s in recent_signals[:3]:
            act_icon = "✅" if s["action"] == "TAKEN" else ("⚠️" if s["action"] == "SKIP" else "ℹ️")
            msg.append(f"  {act_icon} <b>{s['symbol']}</b> {s['side']} ({s['strategy']}) → <code>{s['action']}</code> [{s['time']}]")
            if s["action"] == "SKIP":
                msg.append(f"     <i>Reason: {s['note'][:60]}</i>")
    else:
        msg.append("• <b>Status:</b> <i>No entry trigger yet</i>")
        msg.append("• <b>Reason:</b> <i>Ranked কয়েনগুলোতে 15m ক্যান্ডেলে কোনো স্ট্র্যাটেজি (EMA Pullback / Breakout Retest / Engulfing) শর্ত এখনও পূর্ণ হয়নি। বট হাই-প্রোবাবিলিটি সেটআপের অপেক্ষায় রয়েছে।</i>")

    msg.append("\n💼 <b>4. Positions & Account:</b>")
    if open_pos:
        msg.append(f"• Open Positions: <b>{len(open_pos)}</b>")
        for p in open_pos:
            msg.append(f"  • <b>{p['symbol']}</b> {p['side']} | Entry: {p['entry']} | Qty: {p['qty']} | SL: {p['sl']} | TP: {p['tp']}")
    else:
        msg.append("• Open Positions: <b>0</b> (Waiting for entry signals)")

    msg.append(f"• Available Balance: <b>{bal_text}</b>")
    pnl_sign = "+" if pnl_24h > 0 else ""
    msg.append(f"• 24h Realized PnL: <b>{pnl_sign}{pnl_24h:.2f} USDT</b> ({closed_count} closed trades)")

    return send_alert("\n".join(msg), parse_mode="HTML")
