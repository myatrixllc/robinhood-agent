"""
sentiment.py — Market sentiment using yfinance
"""

import os
import logging
import requests
import yfinance as yf
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)
ET     = pytz.timezone("America/New_York")


def get_spy_trend() -> dict:
    try:
        bars       = yf.Ticker("SPY").history(period="1d", interval="1m")
        if bars.empty or len(bars) < 5:
            return {"trend": "UNKNOWN", "safe": True, "reason": "No SPY data"}

        price_now  = float(bars["Close"].iloc[-1])
        price_open = float(bars["Close"].iloc[0])
        price_5ago = float(bars["Close"].iloc[-5])
        day_pct    = round((price_now - price_open) / price_open * 100, 3)
        recent     = round(price_now - price_5ago, 2)

        if day_pct > 0.5:
            trend = "STRONG_UP"
        elif day_pct > 0.1:
            trend = "UP"
        elif day_pct < -0.5:
            trend = "STRONG_DOWN"
        elif day_pct < -0.1:
            trend = "DOWN"
        else:
            trend = "FLAT"

        return {
            "trend":       trend,
            "day_pct":     day_pct,
            "recent_move": recent,
            "price":       price_now,
            "safe":        day_pct > -1.5,
            "reason":      f"SPY ${price_now} | Day: {day_pct:+.2f}%",
        }
    except Exception as e:
        logger.error(f"SPY trend error: {e}")
        return {"trend": "UNKNOWN", "safe": True, "reason": str(e)}


def get_vix_level() -> dict:
    try:
        bars = yf.Ticker("^VIX").history(period="1d", interval="1m")
        if bars.empty:
            return {"vix": None, "level": "UNKNOWN", "safe": True}

        vix = round(float(bars["Close"].iloc[-1]), 2)

        if vix > 30:
            level = "EXTREME_FEAR"
            safe  = False
        elif vix > 20:
            level = "HIGH_FEAR"
            safe  = True
        elif vix > 12:
            level = "NORMAL"
            safe  = True
        else:
            level = "COMPLACENT"
            safe  = True

        return {"vix": vix, "level": level, "safe": safe}

    except Exception as e:
        logger.error(f"VIX error: {e}")
        return {"vix": None, "level": "UNKNOWN", "safe": True}


def get_news_sentiment(symbol: str) -> dict:
    try:
        api_key    = os.environ.get("ALPACA_API_KEY")
        secret_key = os.environ.get("ALPACA_SECRET_KEY")
        url        = "https://data.alpaca.markets/v1beta1/news"
        params     = {"symbols": symbol, "limit": 5, "sort": "desc"}
        headers    = {
            "APCA-API-KEY-ID":     api_key,
            "APCA-API-SECRET-KEY": secret_key,
        }
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        news = resp.json().get("news", [])

        if not news:
            return {"sentiment": "NEUTRAL", "safe": True, "headlines": []}

        headlines = [n.get("headline", "") for n in news[:3]]
        negative  = ["crash", "drop", "fall", "loss", "miss", "warning", "recall", "sued", "fine", "cut"]
        positive  = ["beat", "rise", "gain", "surge", "record", "upgrade", "buy", "growth", "profit"]
        neg_count = sum(1 for h in headlines for w in negative if w.lower() in h.lower())
        pos_count = sum(1 for h in headlines for w in positive if w.lower() in h.lower())

        if neg_count > pos_count + 1:
            sentiment, safe = "NEGATIVE", False
        elif pos_count > neg_count:
            sentiment, safe = "POSITIVE", True
        else:
            sentiment, safe = "NEUTRAL", True

        return {"sentiment": sentiment, "safe": safe, "headlines": headlines[:2]}

    except Exception as e:
        logger.error(f"News error: {e}")
        return {"sentiment": "NEUTRAL", "safe": True, "headlines": []}


def get_market_sentiment(symbol: str) -> dict:
    spy  = get_spy_trend()
    vix  = get_vix_level()
    news = get_news_sentiment(symbol)
    safe = spy["safe"] and vix["safe"] and news["safe"]

    reasons = []
    if not spy["safe"]:
        reasons.append(f"Market crashing: SPY {spy['day_pct']:+.2f}%")
    if not vix["safe"]:
        reasons.append(f"Extreme fear: VIX={vix['vix']}")
    if not news["safe"]:
        reasons.append(f"Negative news detected")

    return {
        "symbol":    symbol,
        "safe":      safe,
        "spy":       spy,
        "vix":       vix,
        "news":      news,
        "reason":    " | ".join(reasons) if reasons else "Market conditions OK",
        "timestamp": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET"),
    }
