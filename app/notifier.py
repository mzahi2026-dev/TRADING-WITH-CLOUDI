import hashlib
import os
import re
import requests
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
BDT = timezone(timedelta(hours=6))

# Anti-Spam & Rate Limiter Configuration
_ALERT_LOCK = threading.Lock()
_LAST_SEND_TIME = 0.0
_RECENT_SEND_TIMES = []
_ALERT_HISTORY = {}           # sig -> {"last_sent": ts, "suppressed": count}
_FLOOD_SILENCE_UNTIL = 0.0

MIN_INTERVAL_SECONDS = 2.0    # At least 2 seconds between outgoing Telegram requests
BURST_WINDOW_SECONDS = 60.0   # Window for burst check
BURST_MAX_COUNT = 6           # Max 6 messages per 60 seconds
DEDUP_COOLDOWN_SECONDS = 900  # 15 minutes deduplication cooldown for identical errors


def format_dt(dt=None):
    """Returns (day_date_str, time_str, full_str) in BDT."""
    if dt is None:
        dt = datetime.now(BDT)
    day_date = dt.strftime("%A, %d %b %Y")
    time_str = dt.strftime("%I:%M %p")
    full_str = f"{day_date} | {time_str} BDT"
    return day_date, time_str, full_str


def _get_message_signature(message: str) -> str:
    """Generate normalized fingerprint of message by stripping dynamic values (timestamps, numbers, ref numbers)."""
    cleaned = re.sub(r'<[^>]+>', ' ', message)
    cleaned = re.sub(r'#[A-Za-z0-9_-]+', '#REF', cleaned)
    cleaned = re.sub(r'\d{4}-\d{2}-\d{2}', 'DATE', cleaned)
    cleaned = re.sub(r'\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM|BDT)?', 'TIME', cleaned)
    cleaned = re.sub(r'\d+\.\d+', 'NUM', cleaned)
    cleaned = re.sub(r'\b\d+\b', 'NUM', cleaned)
    normalized = " ".join(cleaned.split())
    return hashlib.md5(normalized.encode('utf-8', errors='ignore')).hexdigest()


def send_alert(message: str, force: bool = False, parse_mode: str = "HTML", dedup_window: int = DEDUP_COOLDOWN_SECONDS) -> bool:
    """Send alert to Telegram with thread-safe rate-limiting and anti-spam deduplication."""
    global _LAST_SEND_TIME, _RECENT_SEND_TIMES, _FLOOD_SILENCE_UNTIL

    if not BOT_TOKEN or not CHAT_ID:
        return False

    now = time.time()
    sig = _get_message_signature(message)

    with _ALERT_LOCK:
        # 1. Deduplication Check (for non-force alerts)
        if not force and dedup_window > 0:
            hist = _ALERT_HISTORY.get(sig)
            if hist:
                time_since = now - hist["last_sent"]
                if time_since < dedup_window:
                    hist["suppressed"] += 1
                    return False
                else:
                    suppressed = hist["suppressed"]
                    if suppressed > 0:
                        message = f"{message}\n\n<i>(ℹ️ Repeated {suppressed} time{'s' if suppressed > 1 else ''} in the last {int(time_since // 60)}m - throttled)</i>"

        # 2. Flood Guard Check
        if not force:
            if now < _FLOOD_SILENCE_UNTIL:
                return False

            _RECENT_SEND_TIMES = [t for t in _RECENT_SEND_TIMES if now - t <= BURST_WINDOW_SECONDS]
            if len(_RECENT_SEND_TIMES) >= BURST_MAX_COUNT:
                _FLOOD_SILENCE_UNTIL = now + 300.0  # Silence non-critical alerts for 5 minutes
                try:
                    flood_msg = (
                        "🔇 <b>Alert Flood Guard Activated</b>\n"
                        "<i>Too many background alerts in 60 seconds. Notifications are silenced for 5 minutes to prevent spam. Trade executions will still notify.</i>"
                    )
                    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                    requests.post(url, json={"chat_id": CHAT_ID, "text": flood_msg, "parse_mode": "HTML"}, timeout=5)
                except Exception:
                    pass
                return False

        # 3. Minimum Interval Spacing
        elapsed = now - _LAST_SEND_TIME
        if elapsed < MIN_INTERVAL_SECONDS:
            time.sleep(MIN_INTERVAL_SECONDS - elapsed)

        # 4. Perform Request
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": CHAT_ID,
                "text": message,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            r = requests.post(url, json=payload, timeout=10)
            sent_ok = r.status_code == 200

            if sent_ok:
                now_after = time.time()
                _LAST_SEND_TIME = now_after
                _RECENT_SEND_TIMES.append(now_after)
                _ALERT_HISTORY[sig] = {"last_sent": now_after, "suppressed": 0}
            return sent_ok
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
    return send_alert(msg, force=True, parse_mode="HTML")


def send_watchdog_alert(title: str, component: str, issue: str, action: str = "", severity: str = "WARNING"):
    """Sends standardized Watchdog health alert to Telegram with deduplication."""
    if not BOT_TOKEN or not CHAT_ID:
        return False

    icon_map = {
        "CRITICAL": "🚨",
        "WARNING": "⚠️",
        "INFO": "ℹ️",
        "RESOLVED": "✅",
    }
    icon = icon_map.get(severity.upper(), "⚠️")
    now_dt = datetime.now(BDT)
    day_date, time_str, full_str = format_dt(now_dt)

    msg = [
        f"{icon} <b>WATCHDOG GUARDIAN ALERT</b>",
        "─────────────",
        f"🏷️ <b>Event:</b> {title}",
        f"⚙️ <b>Component:</b> <code>{component}</code>",
        f"🔥 <b>Severity:</b> <b>{severity.upper()}</b>",
        f"⏱️ <b>Time:</b> {time_str} BDT",
        "",
        f"📝 <b>Issue:</b> {issue}",
    ]
    if action:
        msg.extend([
            "",
            f"🛠️ <b>Action:</b> {action}",
        ])
    msg.append("─────────────")

    cooldown = 300 if severity.upper() == "RESOLVED" else 1800  # 30m cooldown for recurring warnings
    return send_alert("\n".join(msg), parse_mode="HTML", dedup_window=cooldown)


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
    return send_alert(msg, force=True, parse_mode="HTML")


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
    return send_alert(msg, force=True, parse_mode="HTML")


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
    return send_alert(msg, force=True, parse_mode="HTML")


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
    return send_alert(msg, force=True, parse_mode="HTML")


def _parse_closed_ts(closed_val, fallback_dt: datetime) -> int:
    """Parse positions.closed string/int to epoch ms for accurate 1-hour filtering."""
    if not closed_val:
        return 0
    if isinstance(closed_val, (int, float)):
        return int(closed_val) if closed_val > 1e11 else int(closed_val * 1000)
    s = str(closed_val).strip()
    if s.isdigit():
        val = int(s)
        return val if val > 1e11 else val * 1000
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=BDT)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return 0


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
    mode_str = "DRY RUN" if dry_mode else ("DEMO" if getattr(config, "USE_DEMO", True) else "LIVE")

    from .db import get_db, market_db
    con = get_db()

    # 1. Closed Trades in last 24h & last 1h
    has_pos = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='positions'").fetchone()
    day_ago = int(time.time() * 1000) - 24 * 3600 * 1000
    hour_ago = int(time.time() * 1000) - 3600 * 1000

    closed_rows = con.execute(
        "SELECT pnl, fee, result, closed, entry_ms FROM positions WHERE status='CLOSED' AND entry_ms>=?", (day_ago,)
    ).fetchall() if has_pos else []

    # Last 1-hour subset
    hour_rows = [
        r for r in closed_rows
        if _parse_closed_ts(r["closed"], now_dt) >= hour_ago or (not r["closed"] and (r["entry_ms"] or 0) >= hour_ago)
    ]
    h_trades = len(hour_rows)
    h_wins = sum(1 for r in hour_rows if float(r["pnl"] or 0) > 0)
    h_losses = sum(1 for r in hour_rows if float(r["pnl"] or 0) <= 0)
    h_pnl = sum(float(r["pnl"] or 0) for r in hour_rows)
    h_fees = sum(float(r["fee"] or 0) for r in hour_rows)
    h_sign = "+" if h_pnl > 0 else ""
    h_heart = "💚" if h_pnl >= 0 else "💔"

    # 24h overall totals
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

    # 3. Top Hourly Ranking (from local market SQLite)
    m_con = market_db()
    has_rk = m_con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ranking'").fetchone()
    latest_rh = m_con.execute("SELECT MAX(run_hour) FROM ranking").fetchone()[0] if has_rk else None
    rk_top = m_con.execute(
        "SELECT symbol, rank, side, rank_score FROM ranking WHERE run_hour=? ORDER BY rank ASC LIMIT 3", (latest_rh,)
    ).fetchall() if latest_rh else []
    m_con.close()

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
        f"⚡ <b>Last 1 Hour ({time_window}):</b>",
    ]

    if h_trades > 0:
        msg.extend([
            f"• <b>Trades:</b> {h_trades:02d} (✅ {h_wins:02d} | ❌ {h_losses:02d})",
            f"• {h_heart} <b>Hourly Net PnL:</b> {h_sign}{h_pnl:.2f} USDT (Fees: {h_fees:.2f} USDT)",
        ])
    else:
        msg.append("• <i>No trades closed in this 1-hour window</i>")

    msg.extend([
        "",
        "🗓️ <b>24h Overall Summary:</b>",
        f"• <b>Total Trades:</b> {total_trades:02d} (✅ {wins:02d} | ❌ {losses:02d})",
        f"• <b>Win Rate:</b> {win_rate:.1f}%",
        f"• {pnl_heart} <b>Net PnL:</b> {pnl_sign}{net_pnl:.2f} USDT",
        f"• 💸 <b>Fees:</b> {total_fees:.2f} USDT",
        "",
        f"📁 <b>Open Positions:</b> {len(open_pos):02d}",
    ])

    if open_pos:
        msg.append("\n<b>Active Trades:</b>")
        for p in open_pos[:3]:
            msg.append(f"• <b>{p['symbol']}</b> {p['side']} | Entry: {p['entry']} | SL: {p['sl']} | TP: {p['tp']}")

    if rk_top:
        rk_symbols = ", ".join(f"{r['symbol']} ({r['side']})" for r in rk_top)
        msg.append(f"\n🏆 <b>Hourly Top:</b> <i>{rk_symbols}</i>")

    msg.append("─────────────")
    msg.append(f"🤖 <b>Next Report:</b> {next_report}")

    return send_alert("\n".join(msg), force=True, parse_mode="HTML")
