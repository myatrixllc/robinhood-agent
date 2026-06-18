"""
main.py — 0DTE Scalping Agent v4
Bulletproof single-trade-at-a-time logic
"""

from dotenv import load_dotenv
load_dotenv("/home/ubuntu/robinhood-agent/.env")

import os
import time
import logging
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
DAILY_LOSS_CAP   = float(os.environ.get("DAILY_LOSS_CAP", "200"))
ET               = pytz.timezone("America/New_York")
SCAN_INTERVAL    = 30
EXIT_INTERVAL    = 5
ORDER_COOLDOWN   = 60

# ── State ─────────────────────────────────────────────────────────────────────
_open_scalp:     dict | None = None
_trade_lock      = threading.Lock()
_placing_order   = False
_last_order_time = 0.0
daily_trades:    list = []
last_summary_date: str = ""


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
        "SPY":  0.75,
        "QQQ":  1.00,
        "AAPL": 1.00,
        "NVDA": 1.50,
        "MCD":  1.00,
    }.get(symbol, 1.00)


def monitor_exit():
    global _open_scalp

    positions = get_option_positions()

    if not positions:
        if _open_scalp:
            logger.info("No open positions found — clearing state")
            _open_scalp = None
        return

    pos     = positions[0]
    pos_sym = pos.get("symbol", "")

    if not _open_scalp:
        otype = "put" if "P" in pos_sym[6:] else "call"
        _open_scalp = {
            "symbol":          pos_sym,
            "option_type":     otype,
            "entry_time":      datetime.now(ET),
            "entry_price":     float(pos.get("avg_entry_price", 0) or 0),
            "pnl_pct":         0,
            "elapsed_seconds": 0,
        }
        logger.info(f"🔄 Tracking restored: {pos_sym}")

    pos     = enrich_position_pnl(pos)
    pnl     = pos.get("pnl_pct", 0)
    elapsed = int((datetime.now(ET) - _open_scalp.get("entry_time", datetime.now(ET))).total_seconds())

    _open_scalp["pnl_pct"]         = pnl
    _open_scalp["elapsed_seconds"] = elapsed

    logger.info(f"👁 [{pos_sym}]: P&L={pnl:+.1f}% elapsed={elapsed}s")

    exit_now, reason = should_exit(_open_scalp)
    if exit_now:
        _close_all(reason)
        return

    underlying = next(
        (s for s in ["SPY", "QQQ", "AAPL", "NVDA", "MCD"] if s in pos_sym),
        pos_sym[:4]
    )
    exit_now, reason = should_exit_position(
        underlying,
        _open_scalp.get("entry_price", 0),
        _open_scalp.get("entry_time", datetime.now(ET)),
        _open_scalp.get("option_type", "call")
    )
    if exit_now:
        _close_all(reason)


def _close_all(reason: str):
    global _open_scalp
    try:
        positions = get_option_positions()
        if not positions:
            _open_scalp = None
            return

        for pos in positions:
            pos    = enrich_position_pnl(pos)
            result = close_option_position(pos)
            pnl    = pos.get("pnl_pct", 0)

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
                logger.info(f"✅ CLOSED: {pos.get('symbol')} | {reason} | P&L: {pnl:+.1f}%")
            else:
                logger.error(f"Close failed: {result}")

        _open_scalp = None

    except Exception as e:
        logger.error(f"_close_all error: {e}", exc_info=True)


def run_scan_cycle():
    global _open_scalp, _placing_order, _last_order_time

    if not _trade_lock.acquire(blocking=False):
        logger.debug("Trade lock busy — skipping")
        return

    try:
        if time.time() - _last_order_time < ORDER_COOLDOWN:
            remaining = int(ORDER_COOLDOWN - (time.time() - _last_order_time))
            logger.debug(f"⛔ Order cooldown: {remaining}s remaining")
            return

        if _placing_order:
            logger.info("⛔ Order already being placed")
            return

        if has_open_position():
            logger.info("⛔ Open position exists — waiting for exit")
            return

        if _open_scalp:
            return

        if daily_loss_limit_hit(DAILY_LOSS_CAP):
            logger.warning(f"⛔ Daily loss cap ${DAILY_LOSS_CAP} hit")
            return

        now_et = datetime.now(ET)
        hour, minute = now_et.hour, now_et.minute

        if hour < 9 or (hour == 9 and minute < 35):
            return

        if hour == 12 and minute < 30:
            return

        if hour == 15 and minute >= 30:
            return

        for symbol in get_todays_symbols():
            sentiment = get_market_sentiment(symbol)
            if not sentiment["safe"]:
                logger.info(f"🚫 Sentiment block [{symbol}]: {sentiment['reason']}")
                continue

            spy_trend = sentiment["spy"]["trend"]
            vix_level = sentiment["vix"]["level"]
            logger.info(f"✅ Sentiment [{symbol}]: SPY={spy_trend} VIX={vix_level}")

            signal = get_signal(symbol, threshold=get_momentum_threshold(symbol))
            sig    = signal.get("signal")
            logger.info(f"Signal [{symbol}]: {sig} — {signal.get('reason')}")

            if sig not in ("BUY_CALL", "BUY_PUT"):
                continue

            option_type = "call" if sig == "BUY_CALL" else "put"

            if option_type == "call" and spy_trend == "STRONG_DOWN":
                logger.info(f"⚠️ [{symbol}] Skipping CALL — market STRONG_DOWN")
                continue
            if option_type == "put" and spy_trend == "STRONG_UP":
                logger.info(f"⚠️ [{symbol}] Skipping PUT — market STRONG_UP")
                continue

            expiry = get_best_expiry(symbol, days_out=0)
            if not expiry:
                logger.warning(f"⚠️ No 0DTE expiry for {symbol}")
                continue

            strike = find_otm_strike(symbol, option_type, expiry)
            if not strike:
                logger.warning(f"⚠️ No affordable strike for {symbol}")
                continue

            signal_clean = {k: v for k, v in signal.items()
                           if not hasattr(v, "strftime") and k != "sentiment"}
            signal_clean["sentiment_spy"]  = spy_trend
            signal_clean["sentiment_vix"]  = vix_level
            signal_clean["sentiment_news"] = sentiment.get("news", {}).get("sentiment", "NEUTRAL")

            decision = decide(signal_clean, None)
            action   = decision.get("action")
            score    = decision.get("score", 0)

            if action == "HOLD":
                logger.info(f"Claude HOLD [{symbol}] (score={score}) — {decision.get('reason')}")
                continue

            if has_open_position():
                logger.info("⛔ Position opened by another thread — aborting")
                return

            _placing_order = True
            order = place_option_order(
                symbol=symbol,
                option_type=option_type,
                strike=strike,
                expiry=expiry,
                contracts=1,
            )
            _last_order_time = time.time()

            if "error" in order:
                logger.warning(f"Order rejected: {order['error']}")
                _placing_order = False
                continue

            _open_scalp = {
                "symbol":          symbol,
                "option_type":     option_type,
                "strike":          strike,
                "expiry":          expiry,
                "entry_time":      datetime.now(ET),
                "entry_price":     signal.get("price", 0),
                "pnl_pct":         0,
                "elapsed_seconds": 0,
            }

            alert_trade_placed(decision, order)
            logger.info(
                f"🎯 ENTERED: {symbol} {option_type.upper()} ${strike} "
                f"exp={expiry} score={score} confidence={decision.get('confidence')}"
            )
            break

    finally:
        _placing_order = False
        _trade_lock.release()


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
            if usage.isdigit():
                if int(usage) > 80:
                    logger.warning(f"⚠️ Disk {usage}% full!")
                    subprocess.run("sudo apt-get clean -y", shell=True, capture_output=True)
                logger.info(f"🧹 Cleanup done. Disk: {usage}%")

        except Exception as e:
            logger.warning(f"Cleanup error: {e}")

        time.sleep(1800)


def maybe_send_daily_summary():
    global last_summary_date
    now_et = datetime.now(ET)
    today  = now_et.date().isoformat()
    if now_et.hour >= 16 and last_summary_date != today:
        total_pnl = sum(t.get("pnl_pct", 0) for t in daily_trades)
        wins  = sum(1 for t in daily_trades if t.get("pnl_pct", 0) > 0)
        total = len(daily_trades)
        logger.info(f"📊 Daily: {wins}/{total} wins | P&L: {total_pnl:+.1f}%")
        alert_daily_summary(daily_trades, total_pnl)
        daily_trades.clear()
        last_summary_date = today


def main():
    paper = _os.environ.get("PAPER_TRADING", "false").lower() == "true"

    logger.info("🤖 0DTE Scalping Agent v4 started")
    logger.info(f"   Mode:           {'📝 PAPER' if paper else '🔴 LIVE'}")
    logger.info(f"   Daily loss cap: ${DAILY_LOSS_CAP}")
    logger.info(f"   Scan interval:  {SCAN_INTERVAL}s")
    logger.info(f"   Exit interval:  {EXIT_INTERVAL}s")
    logger.info(f"   Order cooldown: {ORDER_COOLDOWN}s")

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
            alert_error("Main loop crash", str(e))
        time.sleep(EXIT_INTERVAL)


if __name__ == "__main__":
    main()
