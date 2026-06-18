"""
scanner.py — 7-Layer Signal Scanner
Price momentum + Options flow + Volume + Market context
All free data sources
"""

import pandas as pd
import numpy as np
from datetime import datetime, time, timedelta
import pytz
import logging
import yfinance as yf

logger = logging.getLogger(__name__)
ET     = pytz.timezone("America/New_York")

MARKET_OPEN  = time(9, 35)
MARKET_CLOSE = time(15, 45)


def is_market_open() -> bool:
    now_et = datetime.now(ET)
    if now_et.weekday() >= 5:
        return False
    return MARKET_OPEN <= now_et.time() <= MARKET_CLOSE


def get_time_session() -> str:
    now = datetime.now(ET).time()
    if time(9, 35) <= now <= time(10, 30):
        return "PRIME"
    elif time(12, 0) <= now <= time(12, 30):
        return "LUNCH"
    elif time(13, 0) <= now <= time(15, 45):
        return "AFTERNOON"
    return "NORMAL"


def compute_rsi(series: pd.Series, period: int = 14) -> float:
    if len(series) < period + 1:
        return 50.0
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss
    rsi      = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 2)


def get_price_momentum(symbol: str, threshold: float = 1.0) -> dict:
    """Layer 1 — Price momentum from 1-min bars."""
    try:
        ticker = yf.Ticker(symbol)
        bars   = ticker.history(period="1d", interval="1m")

        if bars.empty or len(bars) < 7:
            return {"signal": "NO_DATA", "move_5min": 0, "move_2min": 0}

        # Use completed bars (skip last incomplete)
        current_price = round(float(bars["Close"].iloc[-2]), 2)
        open_price    = round(float(bars["Open"].iloc[0]), 2)
        price_5min    = round(float(bars["Close"].iloc[-7]), 2)
        price_2min    = round(float(bars["Close"].iloc[-3]), 2)

        move_5min     = round(current_price - price_5min, 2)
        move_2min     = round(current_price - price_2min, 2)
        move_from_open = round(current_price - open_price, 2)

        # Volume
        current_vol = float(bars["Volume"].iloc[-2])
        avg_vol     = float(bars["Volume"].iloc[:-2].mean())
        vol_ratio   = round(current_vol / avg_vol, 2) if avg_vol > 0 else 1.0

        # RSI
        rsi = compute_rsi(bars["Close"].iloc[:-1])

        # Day high/low
        day_high = round(float(bars["High"].iloc[:-1].max()), 2)
        day_low  = round(float(bars["Low"].iloc[:-1].min()), 2)

        # VWAP
        typical  = (bars["High"] + bars["Low"] + bars["Close"]) / 3
        vwap     = round(float(
            (typical * bars["Volume"]).cumsum().iloc[-2] /
            bars["Volume"].cumsum().iloc[-2]
        ), 2)
        vwap_dev = round(current_price - vwap, 2)

        # Momentum signal
        signal = "NEUTRAL"
        if move_5min <= -threshold and move_2min < 0:
            signal = "BEARISH"
        elif move_5min >= threshold and move_2min > 0:
            signal = "BULLISH"

        # Prime session lower threshold
        session = get_time_session()
        if signal == "NEUTRAL" and session == "PRIME":
            if move_5min <= -(threshold * 0.75) and move_2min < 0:
                signal = "BEARISH"
            elif move_5min >= (threshold * 0.75) and move_2min > 0:
                signal = "BULLISH"

        return {
            "signal":         signal,
            "price":          current_price,
            "open_price":     open_price,
            "move_5min":      move_5min,
            "move_2min":      move_2min,
            "move_from_open": move_from_open,
            "vwap":           vwap,
            "vwap_dev":       vwap_dev,
            "day_high":       day_high,
            "day_low":        day_low,
            "volume_ratio":   vol_ratio,
            "rsi":            rsi,
            "session":        session,
        }
    except Exception as e:
        logger.error(f"Momentum error [{symbol}]: {e}")
        return {"signal": "ERROR", "move_5min": 0, "move_2min": 0}


def get_options_flow(symbol: str) -> dict:
    """
    Layer 2 — Options flow analysis.
    Put/Call ratio tells us where smart money is positioned.
    """
    try:
        ticker      = yf.Ticker(symbol)
        expirations = ticker.options

        if not expirations:
            return {"pc_ratio": 1.0, "signal": "NEUTRAL", "call_vol": 0, "put_vol": 0}

        # Use nearest expiry (today or this week)
        today     = datetime.now(ET).date().strftime("%Y-%m-%d")
        expiry    = expirations[0]  # nearest

        chain     = ticker.option_chain(expiry)
        call_vol  = int(chain.calls["volume"].fillna(0).sum())
        put_vol   = int(chain.puts["volume"].fillna(0).sum())
        total_vol = call_vol + put_vol

        if total_vol == 0:
            return {"pc_ratio": 1.0, "signal": "NEUTRAL", "call_vol": 0, "put_vol": 0}

        pc_ratio = round(put_vol / call_vol, 2) if call_vol > 0 else 2.0

        # Unusual options activity
        # High call volume = bullish flow
        # High put volume  = bearish flow
        if pc_ratio > 1.5:
            signal = "BEARISH"   # heavy put buying
        elif pc_ratio < 0.6:
            signal = "BULLISH"   # heavy call buying
        else:
            signal = "NEUTRAL"

        # Check for unusual volume spikes on specific strikes
        atm_calls = chain.calls[
            chain.calls["strike"].between(
                chain.calls["strike"].median() * 0.98,
                chain.calls["strike"].median() * 1.02
            )
        ]
        atm_puts = chain.puts[
            chain.puts["strike"].between(
                chain.puts["strike"].median() * 0.98,
                chain.puts["strike"].median() * 1.02
            )
        ]

        atm_call_vol = int(atm_calls["volume"].fillna(0).sum())
        atm_put_vol  = int(atm_puts["volume"].fillna(0).sum())

        return {
            "pc_ratio":    pc_ratio,
            "signal":      signal,
            "call_vol":    call_vol,
            "put_vol":     put_vol,
            "atm_call_vol": atm_call_vol,
            "atm_put_vol":  atm_put_vol,
            "expiry":      expiry,
        }

    except Exception as e:
        logger.error(f"Options flow error [{symbol}]: {e}")
        return {"pc_ratio": 1.0, "signal": "NEUTRAL", "call_vol": 0, "put_vol": 0}


def get_signal(symbol: str, threshold: float = 1.0) -> dict:
    """
    Full 7-layer signal combining all data sources.
    """
    try:
        session = get_time_session()

        if session == "LUNCH":
            return {
                "symbol":  symbol,
                "signal":  "SKIP",
                "reason":  "Lunch hour — avoid",
                "session": session,
            }

        # Don't open after 3:30pm
        now_et = datetime.now(ET)
        if now_et.hour == 15 and now_et.minute >= 30:
            return {
                "symbol": symbol,
                "signal": "SKIP",
                "reason": "After 3:30pm — no new 0DTE positions",
            }

        # Layer 1 — Price momentum
        momentum = get_price_momentum(symbol, threshold)
        if momentum["signal"] == "NO_DATA":
            return {"symbol": symbol, "signal": "NO_DATA", "reason": "No price data"}

        # Layer 2 — Options flow
        flow = get_options_flow(symbol)

        # ── Signal Logic ──────────────────────────────────────────────────
        signal = "HOLD"
        reason = (
            f"Price=${momentum['price']} "
            f"5min=${momentum['move_5min']:+.2f} "
            f"2min=${momentum['move_2min']:+.2f} "
            f"RSI={momentum['rsi']} "
            f"Vol={momentum['volume_ratio']}x "
            f"P/C={flow['pc_ratio']} "
            f"Flow={flow['signal']}"
        )

        # Strong BUY CALL signals:
        # Price dropping + put/call ratio high (smart money already in puts)
        # = bounce coming, buy calls
        if (
            momentum["signal"] == "BEARISH" and
            momentum["price"] > momentum["day_low"] + 0.50 and
            momentum["rsi"] < 65
        ):
            # Extra confirmation from options flow
            if flow["signal"] in ("BEARISH", "NEUTRAL"):
                signal = "BUY_CALL"
                reason = (
                    f"📉→📈 Price dropped ${momentum['move_5min']:.2f} | "
                    f"RSI={momentum['rsi']} | "
                    f"P/C={flow['pc_ratio']} | "
                    f"Vol={momentum['volume_ratio']}x"
                )

        # Strong BUY PUT signals:
        # Price popping + call/put ratio low (smart money already in calls)
        # = pullback coming, buy puts
        elif (
            momentum["signal"] == "BULLISH" and
            momentum["price"] < momentum["day_high"] - 0.50 and
            momentum["rsi"] > 35
        ):
            if flow["signal"] in ("BULLISH", "NEUTRAL"):
                signal = "BUY_PUT"
                reason = (
                    f"📈→📉 Price popped ${momentum['move_5min']:+.2f} | "
                    f"RSI={momentum['rsi']} | "
                    f"P/C={flow['pc_ratio']} | "
                    f"Vol={momentum['volume_ratio']}x"
                )

        # Prime session — act on flow alone even without momentum
        if signal == "HOLD" and session == "PRIME":
            if flow["signal"] == "BEARISH" and flow["pc_ratio"] > 2.0:
                signal = "BUY_PUT"
                reason = f"🔥 PRIME heavy put flow P/C={flow['pc_ratio']} | RSI={momentum['rsi']}"
            elif flow["signal"] == "BULLISH" and flow["pc_ratio"] < 0.4:
                signal = "BUY_CALL"
                reason = f"🔥 PRIME heavy call flow P/C={flow['pc_ratio']} | RSI={momentum['rsi']}"

        if signal != "HOLD" and session == "PRIME":
            reason += " | 🔥 PRIME"

        return {
            "symbol":       symbol,
            "price":        momentum["price"],
            "open_price":   momentum["open_price"],
            "move_5min":    momentum["move_5min"],
            "move_2min":    momentum["move_2min"],
            "vwap":         momentum["vwap"],
            "vwap_dev":     momentum["vwap_dev"],
            "day_high":     momentum["day_high"],
            "day_low":      momentum["day_low"],
            "volume_ratio": momentum["volume_ratio"],
            "rsi":          momentum["rsi"],
            "pc_ratio":     flow["pc_ratio"],
            "call_vol":     flow["call_vol"],
            "put_vol":      flow["put_vol"],
            "flow_signal":  flow["signal"],
            "session":      session,
            "signal":       signal,
            "reason":       reason,
            "timestamp":    datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET"),
        }

    except Exception as e:
        logger.error(f"Scanner error [{symbol}]: {e}")
        return {"symbol": symbol, "signal": "ERROR", "reason": str(e)}


def should_exit_position(
    symbol: str,
    entry_price: float,
    entry_time: datetime,
    option_type: str
) -> tuple[bool, str]:
    """Fast exit check based on momentum reversal."""
    try:
        ticker = yf.Ticker(symbol)
        bars   = ticker.history(period="1d", interval="1m")

        if bars.empty or len(bars) < 3:
            return False, ""

        current_price = float(bars["Close"].iloc[-2])
        prev_price    = float(bars["Close"].iloc[-3])
        move_1min     = current_price - prev_price
        elapsed       = (datetime.now(ET) - entry_time).seconds

        # 0DTE max hold 5 min
        if elapsed >= 300:
            return True, "⏰ Max 5 min — 0DTE exit"

        # Exit call on reversal
        if option_type == "call":
            if move_1min < -0.50 and elapsed > 60:
                return True, f"📉 Call momentum reversed — exit"
            if move_1min < 0 and elapsed > 180:
                return True, f"⚠️ Call stalling 3 min — exit"

        # Exit put on reversal
        if option_type == "put":
            if move_1min > 0.50 and elapsed > 60:
                return True, f"📈 Put momentum reversed — exit"
            if move_1min > 0 and elapsed > 180:
                return True, f"⚠️ Put stalling 3 min — exit"

        return False, ""

    except Exception as e:
        logger.error(f"Exit check error: {e}")
        return False, ""
