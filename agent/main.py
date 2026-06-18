"""
main.py — Stable 0DTE scalping agent v3
Clean rewrite with working exit monitor
"""

from dotenv import load_dotenv
load_dotenv("/home/ubuntu/robinhood-agent/.env")

import time
import logging
import os
import threading
import subprocess
from datetime import datetime, date
import pytz

from scanner   import get_signal, is_market_open, should_exit_position, get_time_session
from brain     import decide, should_exit
from sentiment import get_market_sentiment

import os as _os
if _os.environ.get("PAPER_TRADING", "false").lower() == "true":
    from executor_alpaca import (
        get_option_positions, has_open_position,
        place_option_order, close_option_position,
        enrich_position_pnl, daily_loss_limit_hit,
        record_loss, find_otm_strike
    )
    def login(): pass
else:
    from executor import (
        login, get_option_positions, has_open_position,
        place_option_order, close_option_position,
        enrich_position_pnl, daily_loss_limit_hit,
        record_loss, find_otm_strike
    )

from expiry    import get_best_expiry
from streamer  import start_stream, set_scan_callback
from notifier  import (
    alert_trade_placed, alert_position_closed,
    alert_daily_summary, alert_error, alert_daily_loss_limit
)

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.FileHandler(f"logs/agent_{date.today()}.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("main")

# ── Config ────────────────────────────────────────────────────────────────────
DAILY_LOSS_CAP = float(os.environ.get("DAILY_LOSS_CAP", "200"))
ET             = pytz.timezone("America/New_York")
SCAN_INTERVAL  = 30
EXIT_INTERVAL  = 10

# ── State ─────────────────────────────────────────────────────────────────────
_open_scalp:    dict | None = None
_trade_lock     = threading.Lock()
_placing_order  = False  # Hard flag to prevent duplicates
_last_order_time = 0     # Timestamp of last order placed
daily_trades: list        = []
last_summary_date: str    = ""


# ── Symbol selector ───────────────────────────────────────────────────────────
def get_todays_symbols() -> list:
    day = datetime.now(ET).weekday()
    symbols = {
        0: ["SPY", "QQQ", "AAPL"],
        1: ["SPY", "QQQ"],
        2: ["SPY", "QQQ", "AAPL", "NVDA"],
        3: ["SPY", "QQQ", "AAPL"],
        4: ["SPY", "QQQ", "AAPL", "NVDA"],
    }
    result = symbols.get(day, [])
    logger.info(f"Today's symbols ({['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][day]}): {result}")
    return result


def get_momentum_threshold(symbol: str) -> float:
    return {
        "SPY":  0.30,
        "QQQ":  0.50,
        "AAPL": 0.50,
        "NVDA": 1.50,
        "MCD":  1.00,
    }.get(symbol, 1.00)


# ── Exit monitor ──────────────────────────────────────────────────────────────
def monitor_exit():
    """Check ALL open positions every EXIT_INTERVAL seconds."""
    global _open_scalp

    positions = get_option_positions()

    if not positions:
        if _open_scalp:
            logger.info("No open positions — clearing state")
            _open_scalp = None
        return

    # Work with first position
    pos     = positions[0]
    pos_sym = pos.get("symbol", "")

    # Set _open_scalp if missing
    if not _open_scalp:
        otype = "put" if "P" in pos_sym[6:] else "call"
        _open_scalp = {
            "symbol":          pos_sym,
            "option_type":     otype,
            "entry_time":      datetime.now(ET),
            "entry_price":     0,
            "pnl_pct":         0,
            "elapsed_seconds": 0,
        }
        logger.info(f"🔄 Tracking: {pos_sym}")

    # Enrich P&L
    pos     = enrich_position_pnl(pos)
    pnl     = pos.get("pnl_pct", 0)
    elapsed = (datetime.now(ET) - _open_scalp.get("entry_time", datetime.now(ET))).seconds

    _open_scalp["pnl_pct"]         = pnl
    _open_scalp["elapsed_seconds"] = elapsed

    logger.info(f"👁 Monitor [{pos_sym}]: P&L={pnl:+.1f}% elapsed={elapsed}s")

    # P&L exit
    exit_now, reason = should_exit(_open_scalp)
    if exit_now:
        _close_all(reason)
        return

    # Momentum exit
    underlying = next((s for s in ["SPY","QQQ","AAPL","NVDA","MCD"] if s in pos_sym), pos_sym[:4])
    exit_now, reason = should_exit_position(
        underlying, _open_scalp.get("entry_price", 0),
        _open_scalp.get("entry_time", datetime.now(ET)),
        _open_scalp.get("option_type", "call")
    )
    if exit_now:
        _close_all(reason)


def _close_all(reason: str):
    """Close ALL open positions."""
    global _open_scalp
    try:
        positions = get_option_positions()
        for pos in positions:
            pos = enrich_position_pnl(pos)
            result = close_option_position(pos)
            pnl = pos.get("pnl_pct", 0)
            if "error" not in result:
                if pnl < 0:
                    record_loss(abs(pnl))
                alert_position_closed(pos, reason)
                daily_trades.append({
                    "symbol":      pos.get("symbol"),
                    "option_type": _open_scalp.get("option_type", "") if _open_scalp else "",
                    "pnl_pct":     pnl,
                    "exit_reason": reason,
                })
                logger.info(f"✅ Closed: {reason} | P&L: {pnl:+.1f}%")
            else:
                logger.error(f"Close failed: {result}")
        _open_scalp = None
    except Exception as e:
        logger.error(f"Close all error: {e}")


# ── Entry scanner ─────────────────────────────────────────────────────────────
def run_scan_cycle():
    global _open_scalp

    global _last_order_time, _placing_order, _open_scalp

    if not _trade_lock.acquire(blocking=False):
        logger.debug("Trade lock busy")
        return

    try:
        # Hard cooldown — 60 seconds after any order
        if time.time() - _last_order_time < 60:
            logger.debug("⛔ Order cooldown active — skipping")
            return

        if _placing_order:
            logger.info("⛔ Order being placed — skipping")
            return

        if has_open_position():
            logger.info("⛔ Open position exists — skipping scan")
            return

        if _open_scalp:
            return

        if daily_loss_limit_hit(DAILY_LOSS_CAP):
            logger.warning("Daily loss cap hit")
            return

        session = get_time_session()
        if session == "LUNCH":
            return

        now_et = datetime.now(ET)
        if now_et.hour == 15 and now_et.minute >= 30:
            return

        for symbol in get_todays_symbols():
            sentiment = get_market_sentiment(symbol)
            if not sentiment["safe"]:
                logger.info(f"🚫 [{symbol}]: {sentiment['reason']}")
                continue

            spy_trend = sentiment["spy"]["trend"]
            logger.info(f"✅ Sentiment OK [{symbol}]: SPY={spy_trend} VIX={sentiment['vix']['level']}")

            signal = get_signal(symbol, threshold=get_momentum_threshold(symbol))
            logger.info(f"Signal [{symbol}]: {signal.get('signal')} — {signal.get('reason')}")

            if signal.get("signal") not in ("BUY_CALL", "BUY_PUT"):
                continue

            option_type = "call" if signal["signal"] == "BUY_CALL" else "put"

            if option_type == "call" and spy_trend == "STRONG_DOWN":
                continue
            if option_type == "put" and spy_trend == "STRONG_UP":
                continue

            expiry = get_best_expiry(symbol, days_out=0)
            if not expiry:
                logger.warning(f"No 0DTE expiry for {symbol} — skipping")
                continue

            strike = find_otm_strike(symbol, option_type, expiry)
            if not strike:
                logger.warning(f"No affordable strike for {symbol}")
                continue

            signal_clean = {k: v for k, v in signal.items()
                          if not hasattr(v, "strftime") and k != "sentiment"}
            signal_clean["sentiment_spy"] = sentiment["spy"]["trend"]
            signal_clean["sentiment_vix"] = sentiment["vix"]["level"]

            decision = decide(signal_clean, None)
            if decision.get("action") == "HOLD":
                logger.info(f"Claude HOLD — {decision.get('reason')}")
                continue

            if has_open_position():
                return

            _placing_order = True

            order = place_option_order(
                symbol=symbol,
                option_type=option_type,
                strike=strike,
                expiry=expiry,
                contracts=1,
            )

            if "error" in order:
                logger.warning(f"Order skipped: {order['error']}")
                continue
            else:
                _open_scalp = {
                    "symbol":          symbol,
                    "option_type":     option_type,
                    "entry_time":      datetime.now(ET),
                    "entry_price":     signal.get("price"),
                    "pnl_pct":         0,
                    "elapsed_seconds": 0,
                }
                alert_trade_placed(decision, order)
                _last_order_time = time.time()
                logger.info(f"🎯 0DTE: {symbol} {option_type} ${strike} {expiry}")
                break

    finally:
        _placing_order = False
        _trade_lock.release()


# ── Cleanup ───────────────────────────────────────────────────────────────────
def cleanup_logs():
    while True:
        try:
            for syslog in ["/var/log/syslog", "/var/log/syslog.1"]:
                if os.path.exists(syslog) and os.path.getsize(syslog) > 100 * 1024 * 1024:
                    open(syslog, 'w').close()
                    logger.info(f"🧹 Truncated {syslog}")

            subprocess.run(
                "find /home/ubuntu/robinhood-agent/logs -name '*.log' -mtime +3 -delete",
                shell=True, capture_output=True
            )

            result = subprocess.run(
                "df / --output=pcent | tail -1",
                shell=True, capture_output=True, text=True
            )
            usage = result.stdout.strip().replace('%', '')
            if usage.isdigit() and int(usage) > 80:
                logger.warning(f"⚠️ Disk {usage}% full!")
                subprocess.run("sudo apt-get clean -y", shell=True, capture_output=True)

            logger.info(f"🧹 Cleanup done. Disk: {usage}%")

        except Exception as e:
            logger.warning(f"Cleanup error: {e}")

        time.sleep(1800)


# ── Daily summary ─────────────────────────────────────────────────────────────
def maybe_send_daily_summary():
    global last_summary_date
    now_et = datetime.now(ET)
    today  = now_et.date().isoformat()
    if now_et.hour >= 16 and last_summary_date != today:
        total_pnl = sum(t.get("pnl_pct", 0) for t in daily_trades)
        alert_daily_summary(daily_trades, total_pnl)
        daily_trades.clear()
        last_summary_date = today


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    logger.info("🤖 0DTE Scalping Agent v3 started")
    logger.info(f"   Daily loss cap:   ${DAILY_LOSS_CAP}")
    logger.info(f"   Max option price: $1.50")
    logger.info(f"   Scan interval:    {SCAN_INTERVAL}s")
    logger.info(f"   Exit interval:    {EXIT_INTERVAL}s")
    logger.info(f"   Paper trading:    {_os.environ.get('PAPER_TRADING', 'false')}")

    login()

    threading.Thread(target=cleanup_logs, daemon=True).start()

    symbols = get_todays_symbols()
    set_scan_callback(run_scan_cycle)
    start_stream(symbols)

    last_scan = 0

    while True:
        try:
            now = time.time()
            if is_market_open():
                monitor_exit()
                if now - last_scan >= SCAN_INTERVAL:
                    run_scan_cycle()
                    last_scan = now
            else:
                logger.debug("Market closed — sleeping")
            maybe_send_daily_summary()
        except KeyboardInterrupt:
            logger.info("Agent stopped")
            break
        except Exception as e:
            logger.error(f"Main loop error: {e}", exc_info=True)
            alert_error("Main loop", str(e))
        time.sleep(EXIT_INTERVAL)


if __name__ == "__main__":
    main()
