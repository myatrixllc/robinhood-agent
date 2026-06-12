"""
scanner.py — Real-time price & signal scanner for AAPL/MCD
Uses Alpaca for real-time data, computes RSI + volume signals
"""

import os
import pandas as pd
import numpy as np
from datetime import datetime, time, timedelta
import pytz
import logging
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")
MARKET_OPEN  = time(9, 35)
MARKET_CLOSE = time(15, 45)

# Alpaca client
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
    current_time = now_et.time()
    return MARKET_OPEN <= current_time <= MARKET_CLOSE


def compute_rsi(series: pd.Series, period: int = 14) -> float:
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss
    rsi      = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 2)


def get_signal(symbol: str) -> dict:
    try:
        client = get_client()

        # Get 30 days of hourly bars
        start = datetime.now(ET) - timedelta(days=30)
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Hour,
            start=start,
        )
        bars = client.get_stock_bars(request).df

        if bars.empty or len(bars) < 20:
            return {"symbol": symbol, "signal": "NO_DATA", "reason": "Insufficient history"}

        # Flatten multi-index if needed
        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(symbol, level="symbol")

        current_price  = round(float(bars["close"].iloc[-1]), 2)
        current_volume = float(bars["volume"].iloc[-1])
        avg_volume     = float(bars["volume"].iloc[-20:].mean())
        volume_ratio   = round(current_volume / avg_volume, 2) if avg_volume > 0 else 1.0
        rsi            = compute_rsi(bars["close"])

        # IV check via yfinance (Alpaca doesn't provide IV)
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            expirations = ticker.options
            if expirations:
                chain = ticker.option_chain(expirations[0])
                atm_calls = chain.calls[
                    chain.calls["strike"].between(current_price * 0.97, current_price * 1.03)
                ]
                iv = round(float(atm_calls["impliedVolatility"].mean()) * 100, 1) if not atm_calls.empty else None
            else:
                iv = None
        except Exception:
            iv = None

        # Earnings check
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            cal = ticker.calendar
            if cal is not None and not cal.empty:
                earnings_date  = pd.to_datetime(cal.iloc[0]["Earnings Date"]).tz_localize(None)
                days_to_earnings = (earnings_date - pd.Timestamp.now()).days
                near_earnings  = 0 <= days_to_earnings <= 5
            else:
                near_earnings = False
        except Exception:
            near_earnings = False

        # Signal logic
        signal = "HOLD"
        reason = f"RSI={rsi}, VolRatio={volume_ratio}"

        if near_earnings:
            signal = "SKIP"
            reason += " | Earnings within 5 days"
        elif iv and iv > 80:
            signal = "SKIP"
            reason += f" | IV={iv}% too high"
        elif rsi < 35 and volume_ratio >= 1.5:
            signal = "BUY_CALL"
            reason += " | Oversold + volume spike"
        elif rsi > 68 and volume_ratio >= 1.5:
            signal = "BUY_PUT"
            reason += " | Overbought + volume spike"

        return {
            "symbol":        symbol,
            "price":         current_price,
            "rsi":           rsi,
            "volume_ratio":  volume_ratio,
            "iv":            iv,
            "near_earnings": near_earnings,
            "signal":        signal,
            "reason":        reason,
            "timestamp":     datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET"),
        }

    except Exception as e:
        logger.error(f"Scanner error for {symbol}: {e}")
        return {"symbol": symbol, "signal": "ERROR", "reason": str(e)}
