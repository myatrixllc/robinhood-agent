"""
streamer.py — Real-time Alpaca WebSocket
Triggers scan on EVERY 1-minute bar — true real-time
"""

import os
import logging
import threading
from datetime import datetime
import pytz
from alpaca.data.live import StockDataStream

logger = logging.getLogger(__name__)
ET     = pytz.timezone("America/New_York")

SYMBOLS        = ["AAPL", "MCD"]
PRICE_MOVE_PCT = 0.15

_last_prices   = {}
_scan_callback = None


def set_scan_callback(fn):
    global _scan_callback
    _scan_callback = fn


async def _on_bar(bar):
    global _last_prices
    try:
        symbol = bar.symbol
        price  = float(bar.close)

        from scanner import is_market_open
        if not is_market_open():
            return

        logger.info(f"📊 {symbol} bar: ${price}")
        if _scan_callback:
            threading.Thread(target=_scan_callback, daemon=True).start()

        if symbol in _last_prices:
            last = _last_prices[symbol]
            move = abs(price - last) / last * 100
            if move >= PRICE_MOVE_PCT:
                logger.info(f"⚡ {symbol} moved {move:.2f}% → ${price}")

        _last_prices[symbol] = price

    except Exception as e:
        logger.error(f"Streamer error: {e}")


def start_stream():
    def run():
        try:
            stream = StockDataStream(
                api_key=os.environ.get("ALPACA_API_KEY"),
                secret_key=os.environ.get("ALPACA_SECRET_KEY"),
            )
            stream.subscribe_bars(_on_bar, *SYMBOLS)
            logger.info(f"⚡ WebSocket stream started for {SYMBOLS}")
            stream.run()
        except Exception as e:
            logger.error(f"Stream error: {e}")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    logger.info("WebSocket streamer thread started")
    return thread
