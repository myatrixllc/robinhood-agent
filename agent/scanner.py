"""
scanner.py — 0DTE Scalping Scanner
Matches manual strategy: ITM options, same-day expiry, $1-2 moves
"""

import pandas as pd
from datetime import datetime, time
import pytz
import logging
import yfinance as yf

logger = logging.getLogger(__name__)
ET     = pytz.timezone("America/New_York")

MARKET_OPEN  = time(9, 35)
MARKET_CLOSE = time(15, 45)

# Tuned to match your manual strategy
MOMENTUM_THRESHOLD = 1.0   # $1 move triggers signal
MIN_VOLUME_RATIO   = 0.5   # relaxed volume requirement


def is_market_open() -> bool:
    now_et = datetime.now(ET)
    if now_et.weekday() >= 5:
        return False
    return MARKET_OPEN <= now_et.time() <= MARKET_CLOSE


def get_time_session() -> str:
    now = datetime.now(ET).time()
    if time(9, 35) <= now <= time(10, 30):
        return "PRIME"
    elif time(11, 30) <= now <= time(13, 0):
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


def get_signal(symbol: str) -> dict:
    try:
        session = get_time_session()

        # Skip lunch — low volume choppy
        if session == "LUNCH":
            return {
                "symbol":  symbol,
                "signal":  "SKIP",
                "reason":  "Lunch hour — avoid",
                "session": session,
            }

        # Get 1-min bars
        ticker = yf.Ticker(symbol)
        bars   = ticker.history(period="1d", interval="1m")

        if bars.empty or len(bars) < 6:
            return {"symbol": symbol, "signal": "NO_DATA", "reason": "Not enough bars"}

        # Use second-to-last bar (last is incomplete)
        current_price  = round(float(bars["Close"].iloc[-2]), 2)
        open_price     = round(float(bars["Open"].iloc[0]), 2)
        price_5min     = round(float(bars["Close"].iloc[-7]), 2) if len(bars) >= 7 else open_price
        price_2min     = round(float(bars["Close"].iloc[-3]), 2)
        price_1min     = round(float(bars["Close"].iloc[-2]), 2)

        # Volume (use completed bars only)
        current_vol    = float(bars["Volume"].iloc[-2])
        avg_vol        = float(bars["Volume"].iloc[:-2].mean())
        vol_ratio      = round(current_vol / avg_vol, 2) if avg_vol > 0 else 1.0

        # Moves
        move_5min      = round(current_price - price_5min, 2)
        move_2min      = round(current_price - price_2min, 2)
        move_from_open = round(current_price - open_price, 2)

        # RSI
        rsi = compute_rsi(bars["Close"].iloc[:-1])

        # Day high/low
        day_high = round(float(bars["High"].iloc[:-1].max()), 2)
        day_low  = round(float(bars["Low"].iloc[:-1].min()), 2)

        # Distance from open — good for context
        open_pct = round((move_from_open / open_price) * 100, 2)

        signal = "HOLD"
        reason = (
            f"Price=${current_price} Open=${open_price}({open_pct:+.1f}%) "
            f"5min=${move_5min:+.2f} 2min=${move_2min:+.2f} "
            f"Vol={vol_ratio}x RSI={rsi}"
        )

        # ── BUY CALL conditions ───────────────────────────────────────────
        # Price dropped $1+ → bounce expected (like your PUT→CALL trade)
        # OR price popping strongly → momentum continuation
        if (
            move_5min <= -MOMENTUM_THRESHOLD and   # dropped $1+ in 5 min
            move_2min <= -0.5 and                  # still dropping
            vol_ratio >= MIN_VOLUME_RATIO and
            rsi < 60 and
            current_price > day_low + 0.5          # not at absolute low
        ):
            signal = "BUY_CALL"
            reason = (
                f"📉→📈 DROP ${move_5min:.2f} in 5min | "
                f"Bounce setup | RSI={rsi} | Vol={vol_ratio}x"
            )

        # ── BUY PUT conditions ────────────────────────────────────────────
        # Price popped $1+ → pullback expected (like your trade)
        elif (
            move_5min >= MOMENTUM_THRESHOLD and    # popped $1+ in 5 min
            move_2min >= 0.5 and                   # still rising
            vol_ratio >= MIN_VOLUME_RATIO and
            rsi > 40 and
            current_price < day_high - 0.5         # not at absolute high
        ):
            signal = "BUY_PUT"
            reason = (
                f"📈→📉 POP ${move_5min:+.2f} in 5min | "
                f"Pullback setup | RSI={rsi} | Vol={vol_ratio}x"
            )

        # ── PRIME session — lower threshold ──────────────────────────────
        if signal == "HOLD" and session == "PRIME":
            if move_5min <= -0.75 and move_2min < 0 and vol_ratio >= 0.5:
                signal = "BUY_CALL"
                reason = f"🔥 PRIME DROP ${move_5min:.2f} | RSI={rsi} | Vol={vol_ratio}x"
            elif move_5min >= 0.75 and move_2min > 0 and vol_ratio >= 0.5:
                signal = "BUY_PUT"
                reason = f"🔥 PRIME POP ${move_5min:+.2f} | RSI={rsi} | Vol={vol_ratio}x"

        return {
            "symbol":         symbol,
            "price":          current_price,
            "open_price":     open_price,
            "move_5min":      move_5min,
            "move_2min":      move_2min,
            "move_from_open": move_from_open,
            "open_pct":       open_pct,
            "day_high":       day_high,
            "day_low":        day_low,
            "volume_ratio":   vol_ratio,
            "rsi":            rsi,
            "session":        session,
            "signal":         signal,
            "reason":         reason,
            "timestamp":      datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET"),
        }

    except Exception as e:
        logger.error(f"Scanner error for {symbol}: {e}")
        return {"symbol": symbol, "signal": "ERROR", "reason": str(e)}


def should_exit_position(symbol: str, entry_price: float, entry_time: datetime, option_type: str) -> tuple[bool, str]:
    try:
        ticker = yf.Ticker(symbol)
        bars   = ticker.history(period="1d", interval="1m")

        if bars.empty or len(bars) < 3:
            return False, ""

        current_price = float(bars["Close"].iloc[-2])
        prev_price    = float(bars["Close"].iloc[-3])
        move_1min     = current_price - prev_price
        elapsed       = (datetime.now(ET) - entry_time).seconds

        # 0DTE — max hold 5 min (not 10)
        if elapsed >= 300:
            return True, "⏰ Max 5 min hold — 0DTE exit"

        # Exit call when momentum reverses
        if option_type == "call":
            if move_1min < -0.5 and elapsed > 60:
                return True, f"📉 Call momentum reversed — exit"
            if move_1min < 0 and elapsed > 180:
                return True, f"⚠️ Call stalling 3 min — exit"

        # Exit put when momentum reverses
        if option_type == "put":
            if move_1min > 0.5 and elapsed > 60:
                return True, f"📈 Put momentum reversed — exit"
            if move_1min > 0 and elapsed > 180:
                return True, f"⚠️ Put stalling 3 min — exit"

        return False, ""

    except Exception as e:
        logger.error(f"Exit check error: {e}")
        return False, ""
