"""
main.py — 0DTE Scalping Agent with day-of-week symbol selector
"""

from dotenv import load_dotenv
load_dotenv("/home/ubuntu/robinhood-agent/.env")

import time
import logging
import os
from datetime import datetime, date
import pytz

from scanner   import get_signal, is_market_open, should_exit_position, get_time_session
from brain     import decide, should_exit
from sentiment import get_market_sentiment
from executor  import (
    login, get_option_positions, place_option_order,
    close_option_position, enrich_position_pnl,
    daily_loss_limit_hit, record_loss, find_otm_strike
)
from expiry    import get_best_expiry
from streamer  import start_stream, set_scan_callback
from notifier  import (
    alert_trade_placed, alert_position_closed,
    alert_daily_summary, alert_error, alert_daily_loss_limit
)

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

DAILY_LOSS_CAP = float(os.environ.get("DAILY_LOSS_CAP", "200"))
ET             = pytz.timezone("America/New_York")
SCAN_INTERVAL  = 30
EXIT_INTERVAL  = 5

_open_scalp: dict | None = None
daily_trades: list       = []
last_summary_date: str   = ""


def get_todays_symbols() -> list:
    """
    Return symbols based on day of week.
    Only trade symbols that have 0DTE options today.
    """
    day = datetime.now(ET).weekday()  # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri

    if day == 0:    # Monday
        symbols = ["SPY", "QQQ", "AAPL"]
    elif day == 1:  # Tuesday
        symbols = ["SPY", "QQQ"]
    elif day == 2:  # Wednesday
        symbols = ["SPY", "QQQ", "AAPL", "NVDA"]
    elif day == 3:  # Thursday
        symbols = ["SPY", "QQQ"]
    elif day == 4:  # Friday
        symbols = ["SPY", "QQQ", "AAPL", "NVDA", "MCD"]
    else:
        symbols = []

    logger.info(f"Today's symbols ({['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][day]}): {symbols}")
    return symbols


def get_momentum_threshold(symbol: str) -> float:
    """Different thresholds per symbol based on typical move size."""
    thresholds = {
        "SPY":  0.50,   # SPY moves $0.50 = significant
        "QQQ":  0.75,   # QQQ slightly bigger moves
        "AAPL": 1.00,   # Your proven threshold
        "NVDA": 2.00,   # NVDA is more volatile
        "MCD":  1.00,   # Similar to AAPL
    }
    return thresholds.get(symbol, 1.00)


def monitor_exit():
    global _open_scalp
    if not _open_scalp:
        return

    symbol      = _open_scalp.get("symbol")
    option_type = _open_scalp.get("option_type")
    entry_time  = _open_scalp.get("entry_time")
    elapsed     = (datetime.now(ET) - entry_time).seconds
    _open_scalp["elapsed_seconds"] = elapsed

    positions = get_option_positions()
    for pos in positions:
        if pos.get("chain_symbol") == symbol:
            pos = enrich_position_pnl(pos)
            _open_scalp["pnl_pct"] = pos.get("pnl_pct", 0)
            exit_now, reason = should_exit(_open_scalp)
            if exit_now:
                _close_position(pos, reason)
                return

    exit_now, reason = should_exit_position(
        symbol, _open_scalp.get("entry_price", 0),
        entry_time, option_type
    )
    if exit_now:
        positions = get_option_positions()
        for pos in positions:
            if pos.get("chain_symbol") == symbol:
                _close_position(pos, reason)
                return


def _close_position(pos: dict, reason: str):
    global _open_scalp
    result = close_option_position(pos)
    if "error" not in result:
        pnl = pos.get("pnl_pct", 0)
        if pnl < 0:
            record_loss(abs(pnl))
        alert_position_closed(pos, reason)
        daily_trades.append({**pos, "exit_reason": reason, "pnl_pct": pnl})
        logger.info(f"Position closed: {reason} | P&L: {pnl:+.1f}%")
        _open_scalp = None
    else:
        logger.error(f"Close failed: {result}")


def run_scan_cycle():
    global _open_scalp

    if daily_loss_limit_hit(DAILY_LOSS_CAP):
        logger.warning(f"Daily loss cap hit — no new trades")
        return

    if _open_scalp:
        return

    session = get_time_session()
    if session == "LUNCH":
        logger.debug("Lunch hour — skipping")
        return

    # Don't open new 0DTE positions last 30 min
    now_et = datetime.now(ET)
    if now_et.hour == 15 and now_et.minute >= 30:
        logger.debug("Last 30 min — no new 0DTE positions")
        return

    symbols = get_todays_symbols()

    for symbol in symbols:
        # Sentiment check
        sentiment = get_market_sentiment(symbol)
        if not sentiment["safe"]:
            logger.info(f"🚫 Sentiment block [{symbol}]: {sentiment['reason']}")
            continue

        spy_trend = sentiment["spy"]["trend"]
        logger.info(f"✅ Sentiment OK [{symbol}]: SPY={spy_trend} VIX={sentiment['vix']['level']}")

        # Get signal with symbol-specific threshold
        signal = get_signal(symbol, threshold=get_momentum_threshold(symbol))
        logger.info(f"Signal [{symbol}]: {signal.get('signal')} — {signal.get('reason')}")

        if signal.get("signal") not in ("BUY_CALL", "BUY_PUT"):
            continue

        # Align with market direction
        option_type = "call" if signal["signal"] == "BUY_CALL" else "put"
        if option_type == "call" and spy_trend == "STRONG_DOWN":
            logger.info(f"⚠️ Skipping CALL — market STRONG_DOWN")
            continue
        if option_type == "put" and spy_trend == "STRONG_UP":
            logger.info(f"⚠️ Skipping PUT — market STRONG_UP")
            continue

        # Get 0DTE expiry
        expiry = get_best_expiry(symbol, days_out=0)
        if not expiry:
            logger.warning(f"No 0DTE expiry for {symbol}")
            continue

        # Get strike
        strike = find_otm_strike(symbol, option_type, expiry)
        if not strike:
            logger.warning(f"No strike for {symbol}")
            continue

        # Ask Claude
        signal["sentiment"] = sentiment
        decision = decide(signal, _open_scalp)
        if decision.get("action") == "HOLD":
            logger.info(f"Claude HOLD — {decision.get('reason')}")
            continue

        # Place order
        order = place_option_order(
            symbol=symbol,
            option_type=option_type,
            strike=strike,
            expiry=expiry,
            contracts=1,
        )

        if "error" in order:
            alert_error(f"Order failed {symbol}", str(order))
        else:
            _open_scalp = {
                "symbol":      symbol,
                "option_type": option_type,
                "strike":      strike,
                "expiry":      expiry,
                "entry_time":  datetime.now(ET),
                "entry_price": signal.get("price"),
                "pnl_pct":     0,
            }
            alert_trade_placed(decision, order)
            logger.info(f"🎯 0DTE entered: {symbol} {option_type} ${strike} exp {expiry}")
            break


def maybe_send_daily_summary():
    global last_summary_date
    now_et = datetime.now(ET)
    today  = now_et.date().isoformat()
    if now_et.hour >= 16 and last_summary_date != today:
        total_pnl = sum(t.get("pnl_pct", 0) for t in daily_trades)
        alert_daily_summary(daily_trades, total_pnl)
        daily_trades.clear()
        last_summary_date = today


def main():
    logger.info("🤖 0DTE Scalping Agent started")
    logger.info(f"   Daily loss cap: ${DAILY_LOSS_CAP}")
    logger.info(f"   Scan interval:  {SCAN_INTERVAL}s")
    logger.info(f"   Exit interval:  {EXIT_INTERVAL}s")

    login()
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
