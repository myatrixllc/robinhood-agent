"""
streamer.py — Real-time Alpaca WebSocket
Uses a cooldown to prevent duplicate scan triggers
"""

import os
import logging
import threading
import time
from datetime import datetime
import pytz
from alpaca.data.live import StockDataStream

logger = logging.getLogger(__name__)
ET     = pytz.timezone("America/New_York")

PRICE_MOVE_PCT = 0.15
_last_prices   = {}
_scan_callback = None
_last_scan_time = 0
_scan_cooldown  = 30  # minimum 30 seconds between WebSocket triggered scans
_scan_lock      = threading.Lock()


def set_scan_callback(fn):
    global _scan_callback
    _scan_callback = fn


async def _on_bar(bar):
    global _last_prices, _last_scan_time
    try:
        symbol = bar.symbol
        price  = float(bar.close)

        from scanner import is_market_open
        if not is_market_open():
            return

        logger.info(f"📊 {symbol} bar: ${price}")

        # Only trigger scan if cooldown has passed
        now = time.time()
        with _scan_lock:
            if now - _last_scan_time < _scan_cooldown:
                logger.debug(f"Scan cooldown active — skipping trigger")
                _last_prices[symbol] = price
                return
            _last_scan_time = now

        if symbol in _last_prices:
            last = _last_prices[symbol]
            move = abs(price - last) / last * 100
            if move >= PRICE_MOVE_PCT:
                logger.info(f"⚡ {symbol} moved {move:.2f}% → ${price}")

        _last_prices[symbol] = price

        if _scan_callback:
            threading.Thread(target=_scan_callback, daemon=True).start()

    except Exception as e:
        logger.error(f"Streamer error: {e}")


def start_stream(symbols: list):
    def run():
        try:
            paper = os.environ.get("PAPER_TRADING", "false").lower() == "true"
            api_key    = os.environ.get("ALPACA_PAPER_API_KEY") if paper else os.environ.get("ALPACA_API_KEY")
            secret_key = os.environ.get("ALPACA_PAPER_SECRET_KEY") if paper else os.environ.get("ALPACA_SECRET_KEY")
            stream = StockDataStream(
                api_key=api_key,
                secret_key=secret_key,
            )
            stream.subscribe_bars(_on_bar, *symbols)
            logger.info(f"⚡ WebSocket stream started for {symbols}")
            stream.run()
        except Exception as e:
            logger.error(f"Stream error: {e}")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    logger.info("WebSocket streamer thread started")
    return thread
