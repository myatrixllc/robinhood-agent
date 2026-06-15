"""
scanner.py — Professional scalping scanner
Uses 1-min bars for price velocity, VWAP, RSI, volume confirmation
"""

import os
import pandas as pd
import numpy as np
from datetime import datetime, time, timedelta
import pytz
import logging
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")

MARKET_OPEN  = time(9, 35)
MARKET_CLOSE = time(15, 45)
LUNCH_START  = time(11, 30)
LUNCH_END    = time(13, 0)

_client = None

def get_client():
    global _client
    if _client is None:
        _client = StockHistoricalDataClient(
            api_key=os.environ.get("ALPACA_API_KEY"),
            secret_key=os.environ.get("ALPACA_SECRET_KEY"),
        )
    return _client


def is_market_open() -> bool:
    now_et = datetime.now(ET)
    if now_et.weekday() >= 5:
        return False
    return MARKET_OPEN <= now_et.time() <= MARKET_CLOSE


def get_time_session() -> str:
    now = datetime.now(ET).time()
    if time(9, 35) <= now <= time(10, 30):
        return "PRIME"
    elif LUNCH_START <= now <= LUNCH_END:
        return "LUNCH"
    elif time(13, 0) <= now <= time(15, 45):
        return "AFTERNOON"
    return "NORMAL"


def compute_rsi(series: pd.Series, period: int = 14) -> float:
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss
    rsi      = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 2)


def compute_vwap(bars: pd.DataFrame) -> float:
    typical_price = (bars["high"] + bars["low"] + bars["close"]) / 3
    vwap = (typical_price * bars["volume"]).cumsum() / bars["volume"].cumsum()
    return round(float(vwap.iloc[-1]), 2)


def compute_price_velocity(bars: pd.DataFrame, lookback: int = 2) -> dict:
    if len(bars) < lookback + 1:
        return {"move": 0, "direction": "FLAT", "magnitude": 0}
    price_now = float(bars["close"].iloc[-1])
    price_ago = float(bars["close"].iloc[-lookback - 1])
    move      = price_now - price_ago
    move_pct  = (move / price_ago) * 100
    direction = "UP" if move > 0 else "DOWN" if move < 0 else "FLAT"
    return {
        "move":      round(move, 2),
        "move_pct":  round(move_pct, 3),
        "direction": direction,
        "magnitude": abs(round(move, 2)),
    }


def get_signal(symbol: str) -> dict:
    try:
        client  = get_client()
        session = get_time_session()

        if session == "LUNCH":
            return {"symbol": symbol, "signal": "SKIP", "reason": "Lunch hour — avoid", "session": session}

        start   = datetime.now(ET).replace(hour=9, minute=30, second=0)
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start,
        )
        bars = client.get_stock_bars(request).df

        if bars.empty or len(bars) < 5:
            return {"symbol": symbol, "signal": "NO_DATA", "reason": "Not enough bars"}

        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(symbol, level="symbol")

        current_price  = round(float(bars["close"].iloc[-1]), 2)
        current_volume = float(bars["volume"].iloc[-1])
        avg_volume     = float(bars["volume"].mean())
        volume_ratio   = round(current_volume / avg_volume, 2) if avg_volume > 0 else 1.0
        rsi            = compute_rsi(bars["close"])
        vwap           = compute_vwap(bars)
        velocity       = compute_price_velocity(bars, lookback=2)
        vwap_dev       = round(current_price - vwap, 2)

        signal = "HOLD"
        reason = f"Price=${current_price} VWAP=${vwap} RSI={rsi} Vol={volume_ratio}x Move=${velocity['move']}"

        if (
            velocity["direction"] == "DOWN" and
            velocity["magnitude"] >= 2.0 and
            vwap_dev < 0 and
            volume_ratio >= 0.8 and
            rsi < 55
        ):
            signal = "BUY_CALL"
            reason = f"⚡ DROP ${velocity['magnitude']} in 2min | Below VWAP ${vwap_dev} | RSI={rsi} | Vol={volume_ratio}x"

        elif (
            velocity["direction"] == "UP" and
            velocity["magnitude"] >= 2.0 and
            vwap_dev > 0 and
            volume_ratio >= 0.8 and
            rsi > 45
        ):
            signal = "BUY_PUT"
            reason = f"⚡ POP ${velocity['magnitude']} in 2min | Above VWAP ${vwap_dev} | RSI={rsi} | Vol={volume_ratio}x"

        if signal != "HOLD" and session == "PRIME":
            reason += " | 🔥 PRIME SESSION"

        return {
            "symbol":       symbol,
            "price":        current_price,
            "vwap":         vwap,
            "vwap_dev":     vwap_dev,
            "rsi":          rsi,
            "volume_ratio": volume_ratio,
            "velocity":     velocity,
            "session":      session,
            "signal":       signal,
            "reason":       reason,
            "timestamp":    datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET"),
        }

    except Exception as e:
        logger.error(f"Scanner error for {symbol}: {e}")
        return {"symbol": symbol, "signal": "ERROR", "reason": str(e)}


def should_exit_position(symbol: str, entry_price: float, entry_time: datetime, option_type: str) -> tuple[bool, str]:
    try:
        client  = get_client()
        start   = datetime.now(ET) - timedelta(minutes=5)
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=start,
        )
        bars = client.get_stock_bars(request).df

        if bars.empty:
            return False, ""

        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(symbol, level="symbol")

        velocity = compute_price_velocity(bars, lookback=1)
        elapsed  = (datetime.now(ET) - entry_time).seconds

        if elapsed >= 600:
            return True, f"⏰ Max 10 min hold reached"

        if option_type == "call" and velocity["direction"] == "UP" and elapsed > 60:
            return True, f"✅ Momentum reversed UP — take profit"

        if option_type == "put" and velocity["direction"] == "DOWN" and elapsed > 60:
            return True, f"✅ Momentum reversed DOWN — take profit"

        if velocity["magnitude"] < 0.3 and elapsed > 120:
            return True, f"⚠️ Price stalled — exiting"

        return False, ""

    except Exception as e:
        logger.error(f"Exit check error: {e}")
        return False, ""
