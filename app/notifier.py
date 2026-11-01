import os
import requests
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
BDT = timezone(timedelta(hours=6))


def format_dt(dt=None):
    """Returns (day_date_str, time_str, full_str) in BDT."""
    if dt is None:
        dt = datetime.now(BDT)
    day_date = dt.strftime("%A, %d %b %Y")
    time_str = dt.strftime("%I:%M %p")
    full_str = f"{day_date} | {time_str} BDT"
    return day_date, time_str, full_str


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


def send_bot_online_alert(mode: str = "DEMO"):
    """Alert on bot system startup matching card format."""
    now_dt = datetime.now(BDT)
    day_date, time_str, full_str = format_dt(now_dt)
    ref_no = f"BOOT-{now_dt.strftime('%m%d-%H%M')}"

    msg = (
        f"🤖 <b>BOT ONLINE</b>\n"
        f"🔖 <b>Ref:</b> <code>#{ref_no}</code>\n"
        f"📅 <i>{full_str}</i>\n\n"
        f"🟢 <b>Scanner:</b> Active\n"
        f"🛡️ <b>Watchdog:</b> Active\n"
        f"🧪 <b>Mode:</b> {mode}\n"
        f"📊 <b>Strategy:</b> EMA Pullback & Breakout Retest"
    )
    return send_alert(msg, parse_mode="HTML")


def send_trade_opened_alert(pid: int, sym: str, side: str, strat: str, entry, sl, tp, dry: bool = False):
    """Alert on trade open matching card format with Day, Date, Time and Ref No."""
    now_dt = datetime.now(BDT)
    day_date, time_str, full_str = format_dt(now_dt)
    ref_no = f"TRD-{pid:04d}" if pid else f"TRD-{int(time.time()) % 10000:04d}"

    strat_clean = "EMA Pullback" if "pullback" in str(strat).lower() else ("Breakout Retest" if "breakout" in str(strat).lower() else strat)
    side_icon = "🟢" if str(side).upper() == "LONG" else "🔴"
    mode_str = "DRY RUN | 15M" if dry else "DEMO | 15M"

    msg = (
        f"{side_icon} <b>{side.upper()} OPENED | {sym}</b>\n"
        f"🔖 <b>Ref:</b> <code>#{ref_no}</code>\n"
        f"📅 <i>{full_str}</i>\n\n"
        f"💰 <b>Entry:</b> {entry}\n"
        f"🔴 <b>SL:</b> {sl} | 🎯 <b>TP:</b> {tp}\n"
        f"📊 <b>Strategy:</b> {strat_clean}\n"
        f"🧪 <b>{mode_str}</b>"
    )
    return send_alert(msg, parse_mode="HTML")


def send_ai_trade_opened_alert(pid: int, sym: str, side: str, thesis: str, confidence: int, entry, sl, tp, dry: bool = False):
    """Alert on autonomous Gemini AI trade open."""
    now_dt = datetime.now(BDT)
    day_date, time_str, full_str = format_dt(now_dt)
    ref_no = f"TRD-{pid:04d}" if pid else f"TRD-{int(time.time()) % 10000:04d}"

    side_icon = "🟢" if str(side).upper() == "LONG" else "🔴"
    mode_str = "DRY RUN | 15M" if dry else "DEMO | 15M"

    msg = (
        f"🧠 <b>GEMINI AI TRADE | {side_icon} {side.upper()}</b>\n"
        f"🔖 <b>Ref:</b> <code>#{ref_no}</code> (AI Autonomous)\n"
        f"📅 <i>{full_str}</i>\n\n"
        f"🪙 <b>Symbol:</b> {sym}\n"
        f"💰 <b>Entry:</b> {entry}\n"
        f"🛑 <b>SL:</b> {sl} (-$15.00 USDT)\n"
        f"🎯 <b>TP:</b> {tp} (+$30.00 USDT)\n"
        f"⚖️ <b>Risk:Reward:</b> 1 : 2.0\n"
        f"🎯 <b>AI Confidence:</b> {confidence}%\n\n"
        f"📝 <b>AI Thesis:</b>\n"
        f"<i>\"{thesis}\"</i>\n"
        f"🧪 <b>{mode_str}</b>"
    )
    return send_alert(msg, parse_mode="HTML")


def send_ai_trade_closed_alert(pid: int, sym: str, side: str, res: str, entry, ex, net: float, fee: float, dry: bool = False):
    """Alert on autonomous Gemini AI trade close."""
    now_dt = datetime.now(BDT)
    day_date, time_str, full_str = format_dt(now_dt)
    ref_no = f"TRD-{pid:04d}" if pid else f"TRD-{int(time.time()) % 10000:04d}"

    pnl_icon = "✅" if net >= 0 else "🛑"
    pnl_heart = "💚" if net >= 0 else "💔"
    pnl_sign = "+" if net > 0 else ""
    mode_str = "DRY RUN" if dry else "DEMO"

    msg = (
        f"🧠 <b>GEMINI AI TRADE CLOSED | {sym}</b>\n"
        f"🔖 <b>Ref:</b> <code>#{ref_no}</code>\n"
        f"📅 <i>{full_str}</i>\n\n"
        f"💰 <b>Entry:</b> {entry} → <b>Exit:</b> {ex:.6g}\n"
        f"{pnl_icon} <b>Result:</b> {res}\n"
        f"{pnl_heart} <b>Net PnL:</b> {pnl_sign}{net:+.4f} USDT\n"
        f"🧪 <b>{mode_str}</b>"
    )
    return send_alert(msg, parse_mode="HTML")


def send_trade_closed_alert(pid: int, sym: str, side: str, res: str, entry, ex, net: float, fee: float, dry: bool = False):
    """Alert on trade close matching card format with Day, Date, Time and Ref No."""
    now_dt = datetime.now(BDT)
    day_date, time_str, full_str = format_dt(now_dt)
    ref_no = f"TRD-{pid:04d}" if pid else f"TRD-{int(time.time()) % 10000:04d}"

    pnl_icon = "✅" if net >= 0 else "🛑"
    pnl_heart = "💚" if net >= 0 else "💔"
    pnl_sign = "+" if net > 0 else ""
    mode_str = "DRY RUN" if dry else "DEMO"

    msg = (
        f"{pnl_icon} <b>TRADE CLOSED | {sym}</b>\n"
        f"🔖 <b>Ref:</b> <code>#{ref_no}</code>\n"
        f"📅 <i>{full_str}</i>\n\n"
        f"💰 <b>Entry:</b> {entry} → <b>Exit:</b> {ex:.6g}\n"
        f"{pnl_heart} <b>Net PnL:</b> {pnl_sign}{net:+.4f} USDT\n"
        f"🏁 <b>Reason:</b> {res}\n"
        f"🧪 <b>{mode_str}</b>"
    )
    return send_alert(msg, parse_mode="HTML")


def send_hourly_report():
    """Compiles and sends clean Hourly Trading Report to Telegram matching user card format."""
    if not BOT_TOKEN or not CHAT_ID:
        return False

    from . import config, trade_config as tc

    now_dt = datetime.now(BDT)
    prev_dt = now_dt - timedelta(hours=1)
    time_window = f"{prev_dt.strftime('%I:00 %p')} – {now_dt.strftime('%I:00 %p')}"
    next_report = (now_dt + timedelta(hours=1)).strftime("%I:00 %p")
    day_date, time_str, full_str = format_dt(now_dt)
    ref_no = f"REP-{now_dt.strftime('%m%d')}-{now_dt.strftime('%H%M')}"

    dry_mode = os.getenv("TRADER_DRY_MODE", "false").lower() == "true"
    mode_str = "DRY" if dry_mode else "DEMO"

    con = sqlite3.connect(str(config.DB_PATH), timeout=20)
    con.row_factory = sqlite3.Row

    # 1. Closed Trades in last 24h
    has_pos = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='positions'").fetchone()
    day_ago = int(time.time() * 1000) - 24 * 3600 * 1000

    closed_rows = con.execute(
        "SELECT pnl, fee, result FROM positions WHERE status='CLOSED' AND entry_ms>=?", (day_ago,)
    ).fetchall() if has_pos else []

    total_trades = len(closed_rows)
    wins = sum(1 for r in closed_rows if float(r["pnl"] or 0) > 0)
    losses = sum(1 for r in closed_rows if float(r["pnl"] or 0) <= 0)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0
    net_pnl = sum(float(r["pnl"] or 0) for r in closed_rows)
    total_fees = sum(float(r["fee"] or 0) for r in closed_rows)

    # 2. Open Positions
    open_pos = con.execute(
        "SELECT symbol, side, entry, sl, tp, qty, status FROM positions WHERE status IN ('OPEN', 'EMERGENCY_UNPROTECTED')"
    ).fetchall() if has_pos else []

    # 3. Top Hourly Ranking
    has_rk = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ranking'").fetchone()
    latest_rh = con.execute("SELECT MAX(run_hour) FROM ranking").fetchone()[0] if has_rk else None
    rk_top = con.execute(
        "SELECT symbol, rank, side, rank_score FROM ranking WHERE run_hour=? ORDER BY rank ASC LIMIT 3", (latest_rh,)
    ).fetchall() if latest_rh else []

    con.close()

    pnl_sign = "+" if net_pnl > 0 else ""
    pnl_heart = "💚" if net_pnl >= 0 else "💔"

    msg = [
        "📊 <b>HOURLY TRADING REPORT</b>",
        "─────────────",
        f"⏱️ <b>{time_window}</b>",
        f"📅 <b>{day_date}</b> | <b>Ref:</b> <code>#{ref_no}</code>",
        f"🟢 <b>Bot:</b> ACTIVE | 🧪 <b>{mode_str}</b>",
        "",
        f"📈 <b>Trades:</b> {total_trades:02d}",
        f"✅ <b>Wins:</b> {wins:02d} | ❌ <b>Losses:</b> {losses:02d}",
        f"🎯 <b>Win Rate:</b> {win_rate:.1f}%",
        "",
        f"{pnl_heart} <b>Net PnL:</b> {pnl_sign}{net_pnl:.2f} USDT",
        f"💸 <b>Fees:</b> {total_fees:.2f} USDT",
        f"📁 <b>Open Positions:</b> {len(open_pos):02d}",
    ]

    if open_pos:
        msg.append("\n<b>Active Trades:</b>")
        for p in open_pos[:3]:
            msg.append(f"• <b>{p['symbol']}</b> {p['side']} | Entry: {p['entry']} | SL: {p['sl']} | TP: {p['tp']}")

    if rk_top:
        rk_symbols = ", ".join(f"{r['symbol']} ({r['side']})" for r in rk_top)
        msg.append(f"\n🏆 <b>Hourly Top:</b> <i>{rk_symbols}</i>")

    msg.append("─────────────")
    msg.append(f"🤖 <b>Next Report:</b> {next_report}")

    return send_alert("\n".join(msg), parse_mode="HTML")

