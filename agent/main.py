"""
main.py — Main trading agent loop
Runs continuously during market hours, scanning and trading AAPL + MCD
"""

import time
import logging
import json
import os
from datetime import datetime, date
import pytz

from scanner  import get_signal, is_market_open
from brain    import decide, should_exit
from executor import (
    get_positions, place_option_order, close_position,
    enrich_position_pnl, daily_loss_limit_hit, record_loss, get_options_chain
)
from notifier import (
    alert_trade_placed, alert_position_closed,
    alert_daily_summary, alert_error, alert_daily_loss_limit
)

# ── Logging setup ─────────────────────────────────────────────────────────────
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
SYMBOLS         = ["AAPL", "MCD"]
SCAN_INTERVAL   = 60 * 2      # check every 2 minutes
DAILY_LOSS_CAP  = float(os.environ.get("DAILY_LOSS_CAP", "200"))
ET              = pytz.timezone("America/New_York")

# In-memory trade log for daily summary
daily_trades: list[dict] = []
last_summary_date: str   = ""


def run_scan_cycle():
    """One full scan cycle — check signals for all symbols and act."""

    # Daily loss guard
    if daily_loss_limit_hit(DAILY_LOSS_CAP):
        logger.warning(f"Daily loss cap ${DAILY_LOSS_CAP} hit — skipping cycle")
        return

    # Get current open positions (max 1 rule enforced by Claude + here)
    open_positions = get_positions()
    open_symbols   = {p.get("symbol") for p in open_positions}

    # ── Check exit conditions on open positions ───────────────────────────────
    for pos in open_positions:
        pos = enrich_position_pnl(pos)
        exit_now, reason = should_exit(pos)
        if exit_now:
            logger.info(f"Exiting position: {pos.get('symbol')} — {reason}")
            result = close_position(pos.get("id") or pos.get("position_id"))
            if "error" not in result:
                pnl = pos.get("pnl_pct", 0)
                if pnl < 0:
                    record_loss(abs(pnl))
                alert_position_closed(pos, reason)
                daily_trades.append({**pos, "exit_reason": reason})
                open_symbols.discard(pos.get("symbol"))
            else:
                alert_error("Close position failed", str(result))

    # ── Scan for new entries ──────────────────────────────────────────────────
    if len(open_symbols) >= 1:
        logger.info(f"Already have open position(s): {open_symbols} — no new entries")
        return

    for symbol in SYMBOLS:
        signal = get_signal(symbol)
        logger.info(f"Signal [{symbol}]: {signal.get('signal')} — {signal.get('reason')}")

        if signal.get("signal") not in ("BUY_CALL", "BUY_PUT"):
            continue

        # Fetch options chain to give Claude real strikes
        option_type = "call" if signal["signal"] == "BUY_CALL" else "put"
        try:
            from datetime import timedelta
            expiry = (datetime.now(ET) + timedelta(days=14)).strftime("%Y-%m-%d")
            chain = get_options_chain(symbol, option_type, expiry)
        except Exception:
            chain = None
            expiry = None

        # Ask Claude
        open_pos = open_positions[0] if open_positions else None
        decision = decide(signal, open_pos, chain)
        logger.info(f"Decision [{symbol}]: {decision}")

        if decision.get("action") == "HOLD":
            logger.info(f"Claude says HOLD — {decision.get('reason')}")
            continue

        # Place the order
        order = place_option_order(
            symbol     = decision["symbol"],
            option_type= "call" if decision["action"] == "BUY_CALL" else "put",
            strike     = decision["strike"],
            expiry     = decision.get("expiry", expiry),
            contracts  = 1,
        )

        if "error" in order:
            alert_error(f"Order failed for {symbol}", str(order))
        else:
            alert_trade_placed(decision, order)
            daily_trades.append({**decision, "order_id": order.get("id")})
            break  # max 1 position — stop scanning after placing


def maybe_send_daily_summary():
    """Send a daily P&L summary at market close."""
    global last_summary_date
    now_et = datetime.now(ET)
    today  = now_et.date().isoformat()

    # Send once after 4pm ET
    if now_et.hour >= 16 and last_summary_date != today:
        total_pnl = sum(t.get("pnl_pct", 0) for t in daily_trades)
        alert_daily_summary(daily_trades, total_pnl)
        daily_trades.clear()
        last_summary_date = today
        logger.info("Daily summary sent")


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    logger.info("🤖 Trading agent started")
    logger.info(f"   Symbols: {SYMBOLS}")
    logger.info(f"   Daily loss cap: ${DAILY_LOSS_CAP}")
    logger.info(f"   Scan interval: {SCAN_INTERVAL}s")

    while True:
        try:
            if is_market_open():
                run_scan_cycle()
            else:
                logger.debug("Market closed — sleeping")

            maybe_send_daily_summary()

        except KeyboardInterrupt:
            logger.info("Agent stopped by user")
            break
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
            alert_error("Main loop error", str(e))

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
