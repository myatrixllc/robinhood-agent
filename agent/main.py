"""
main.py — Stable 0DTE scalping agent v2
Fixes: duplicate orders, exit monitor, log cleanup, max price $1.50
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
from executor  import (
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
_open_scalp:  dict | None = None
_trade_lock   = threading.Lock()
daily_trades: list        = []
last_summary_date: str    = ""


# ── Symbol selector ───────────────────────────────────────────────────────────
def get_todays_symbols() -> list:
    day = datetime.now(ET).weekday()
    symbols = {
        0: ["SPY", "QQQ", "AAPL"],
        1: ["SPY", "QQQ"],
        2: ["SPY", "QQQ", "AAPL", "NVDA"],
        3: ["SPY", "QQQ"],
        4: ["SPY", "QQQ", "AAPL", "NVDA"],
    }
    result = symbols.get(day, [])
    logger.info(f"Today's symbols ({['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][day]}): {result}")
    return result


def get_momentum_threshold(symbol: str) -> float:
    return {
        "SPY":  0.30,
        "QQQ":  0.50,
        "AAPL": 0.75,
        "NVDA": 1.50,
        "MCD":  1.00,
    }.get(symbol, 1.00)


# ── Exit monitor ──────────────────────────────────────────────────────────────
def monitor_exit():
    global _open_scalp

    if not _open_scalp:
        return

    try:
        symbol      = _open_scalp.get("symbol")
        option_type = _open_scalp.get("option_type")
        entry_time  = _open_scalp.get("entry_time")

        if not entry_time:
            return

        elapsed = (datetime.now(ET) - entry_time).seconds
        _open_scalp["elapsed_seconds"] = elapsed

        positions = get_option_positions()
        matching  = [p for p in positions if p.get("chain_symbol") == symbol]

        if not matching:
            logger.info(f"Position {symbol} no longer open — clearing")
            _open_scalp = None
            return

        pos = enrich_position_pnl(matching[0])
        pnl = pos.get("pnl_pct", 0)
        _open_scalp["pnl_pct"] = pnl

        logger.info(f"👁 Monitor [{symbol}]: P&L={pnl:+.1f}% elapsed={elapsed}s")

        # P&L exit
        exit_now, reason = should_exit(_open_scalp)
        if exit_now:
            _close_position(pos, reason)
            return

        # Momentum exit
        exit_now, reason = should_exit_position(
            symbol, _open_scalp.get("entry_price", 0),
            entry_time, option_type
        )
        if exit_now:
            _close_position(pos, reason)

    except Exception as e:
        logger.error(f"Monitor exit error: {e}")


def _close_position(pos: dict, reason: str):
    global _open_scalp
    try:
        result = close_option_position(pos)
        if "error" not in result:
            pnl = pos.get("pnl_pct", 0)
            if pnl < 0:
                record_loss(abs(pnl))
            alert_position_closed(pos, reason)
            daily_trades.append({
                "symbol":      pos.get("chain_symbol"),
                "option_type": pos.get("option_type"),
                "pnl_pct":     pnl,
                "exit_reason": reason,
            })
            logger.info(f"✅ Closed: {reason} | P&L: {pnl:+.1f}%")
            _open_scalp = None
        else:
            logger.error(f"Close failed: {result}")
    except Exception as e:
        logger.error(f"Close position error: {e}")


# ── Entry scanner ─────────────────────────────────────────────────────────────
def run_scan_cycle():
    global _open_scalp

    if not _trade_lock.acquire(blocking=False):
        logger.debug("Trade lock busy — skipping")
        return

    try:
        if has_open_position():
            logger.info("⛔ Open position exists — skipping scan")
            return

        if _open_scalp:
            return

        if daily_loss_limit_hit(DAILY_LOSS_CAP):
            logger.warning("Daily loss cap hit — no new trades")
            return

        session = get_time_session()
        if session == "LUNCH":
            logger.debug("Lunch hour — skipping")
            return

        now_et = datetime.now(ET)
        if now_et.hour == 15 and now_et.minute >= 30:
            logger.debug("After 3:30pm — no new 0DTE")
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
                logger.info(f"⚠️ Skipping CALL — market STRONG_DOWN")
                continue
            if option_type == "put" and spy_trend == "STRONG_UP":
                logger.info(f"⚠️ Skipping PUT — market STRONG_UP")
                continue

            expiry = get_best_expiry(symbol, days_out=0)
            if not expiry:
                logger.warning(f"No 0DTE expiry for {symbol}")
                continue

            strike = find_otm_strike(symbol, option_type, expiry)
            if not strike:
                logger.warning(f"No affordable strike for {symbol} — skipping")
                continue

            # Clean signal for Claude
            signal_clean = {k: v for k, v in signal.items()
                          if not hasattr(v, "strftime") and k != "sentiment"}
            signal_clean["sentiment_spy"]  = sentiment["spy"]["trend"]
            signal_clean["sentiment_vix"]  = sentiment["vix"]["level"]
            signal_clean["sentiment_news"] = sentiment["news"]["sentiment"]

            decision = decide(signal_clean, None)
            if decision.get("action") == "HOLD":
                logger.info(f"Claude HOLD — {decision.get('reason')}")
                continue

            # Final live check
            if has_open_position():
                logger.info("⛔ Position opened by another thread — aborting")
                return

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
                    "strike":          strike,
                    "expiry":          expiry,
                    "entry_time":      datetime.now(ET),
                    "entry_price":     signal.get("price"),
                    "pnl_pct":         0,
                    "elapsed_seconds": 0,
                }
                alert_trade_placed(decision, order)
                logger.info(f"🎯 0DTE entered: {symbol} {option_type} ${strike} exp {expiry}")
                break

    finally:
        _trade_lock.release()


# ── Background cleanup ────────────────────────────────────────────────────────
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
    logger.info("🤖 0DTE Scalping Agent v2 started")
    logger.info(f"   Daily loss cap:   ${DAILY_LOSS_CAP}")
    logger.info(f"   Max option price: $1.50")
    logger.info(f"   Scan interval:    {SCAN_INTERVAL}s")
    logger.info(f"   Exit interval:    {EXIT_INTERVAL}s")

    login()

    threading.Thread(target=cleanup_logs, daemon=True).start()
    logger.info("🧹 Background cleanup started")

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
