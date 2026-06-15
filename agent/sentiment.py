"""
sentiment.py — Market sentiment analysis
Checks SPY trend, VIX, and news before allowing trades
"""

import os
import logging
import requests
from datetime import datetime, timedelta
import pytz
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

logger = logging.getLogger(__name__)
ET     = pytz.timezone("America/New_York")

_client = None

def get_client():
    global _client
    if _client is None:
        _client = StockHistoricalDataClient(
            api_key=os.environ.get("ALPACA_API_KEY"),
            secret_key=os.environ.get("ALPACA_SECRET_KEY"),
        )
    return _client


def get_spy_trend() -> dict:
    """
    Check SPY (S&P 500) trend for overall market direction.
    Returns trend, strength and whether it's safe to trade.
    """
    try:
        client  = get_client()
        start   = datetime.now(ET).replace(hour=9, minute=30, second=0)
        request = StockBarsRequest(
            symbol_or_symbols="SPY",
            timeframe=TimeFrame.Minute,
            start=start,
        )
        bars = client.get_stock_bars(request).df

        if bars.empty or len(bars) < 5:
            return {"trend": "UNKNOWN", "safe": True, "reason": "Not enough SPY data"}

        if hasattr(bars.index, 'levels'):
            bars = bars.xs("SPY", level="symbol")

        price_now  = float(bars["close"].iloc[-1])
        price_open = float(bars["close"].iloc[0])
        price_5ago = float(bars["close"].iloc[-5])

        day_move   = round(price_now - price_open, 2)
        day_pct    = round((day_move / price_open) * 100, 3)
        recent_move = round(price_now - price_5ago, 2)

        # Determine trend
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

        # Market is safe to trade if not crashing
        safe   = day_pct > -1.5
        reason = f"SPY ${price_now} | Day: {day_pct:+.2f}% | Last 5min: ${recent_move:+.2f}"

        return {
            "trend":       trend,
            "day_pct":     day_pct,
            "recent_move": recent_move,
            "price":       price_now,
            "safe":        safe,
            "reason":      reason,
        }

    except Exception as e:
        logger.error(f"SPY trend error: {e}")
        return {"trend": "UNKNOWN", "safe": True, "reason": str(e)}


def get_vix_level() -> dict:
    """
    Check VIX (fear index) level.
    High VIX = high fear = options are expensive + risky
    """
    try:
        client  = get_client()
        start   = datetime.now(ET) - timedelta(hours=1)
        request = StockBarsRequest(
            symbol_or_symbols="VIXY",  # VIX ETF (tradeable proxy)
            timeframe=TimeFrame.Minute,
            start=start,
        )
        bars = client.get_stock_bars(request).df

        if bars.empty:
            return {"vix": None, "level": "UNKNOWN", "safe": True}

        if hasattr(bars.index, 'levels'):
            bars = bars.xs("VIXY", level="symbol")

        vix_price = float(bars["close"].iloc[-1])

        # VIXY levels (proxy for VIX)
        if vix_price > 25:
            level = "EXTREME_FEAR"
            safe  = False
        elif vix_price > 18:
            level = "HIGH_FEAR"
            safe  = True   # can still trade but be careful
        elif vix_price > 12:
            level = "NORMAL"
            safe  = True
        else:
            level = "COMPLACENT"
            safe  = True

        return {
            "vix":   round(vix_price, 2),
            "level": level,
            "safe":  safe,
        }

    except Exception as e:
        logger.error(f"VIX error: {e}")
        return {"vix": None, "level": "UNKNOWN", "safe": True}


def get_news_sentiment(symbol: str) -> dict:
    """
    Check recent news sentiment for a symbol using Alpaca News API.
    """
    try:
        api_key    = os.environ.get("ALPACA_API_KEY")
        secret_key = os.environ.get("ALPACA_SECRET_KEY")

        # Alpaca news endpoint
        url    = f"https://data.alpaca.markets/v1beta1/news"
        params = {
            "symbols": symbol,
            "limit":   5,
            "sort":    "desc",
        }
        headers = {
            "APCA-API-KEY-ID":     api_key,
            "APCA-API-SECRET-KEY": secret_key,
        }

        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        news = resp.json().get("news", [])

        if not news:
            return {"sentiment": "NEUTRAL", "safe": True, "headlines": []}

        headlines  = [n.get("headline", "") for n in news[:3]]
        # Simple keyword sentiment
        negative   = ["crash", "drop", "fall", "loss", "miss", "warning", "recall", "sued", "fine", "cut"]
        positive   = ["beat", "rise", "gain", "surge", "record", "upgrade", "buy", "growth", "profit"]

        neg_count  = sum(1 for h in headlines for w in negative if w.lower() in h.lower())
        pos_count  = sum(1 for h in headlines for w in positive if w.lower() in h.lower())

        if neg_count > pos_count + 1:
            sentiment = "NEGATIVE"
            safe      = False
        elif pos_count > neg_count:
            sentiment = "POSITIVE"
            safe      = True
        else:
            sentiment = "NEUTRAL"
            safe      = True

        return {
            "sentiment": sentiment,
            "safe":      safe,
            "headlines": headlines[:2],
        }

    except Exception as e:
        logger.error(f"News error: {e}")
        return {"sentiment": "NEUTRAL", "safe": True, "headlines": []}


def get_market_sentiment(symbol: str) -> dict:
    """
    Full market sentiment check — combines SPY, VIX, news.
    Returns overall safe/unsafe to trade + full context for Claude.
    """
    spy  = get_spy_trend()
    vix  = get_vix_level()
    news = get_news_sentiment(symbol)

    # Overall safety check
    safe = spy["safe"] and vix["safe"] and news["safe"]

    # Build reason
    reasons = []
    if not spy["safe"]:
        reasons.append(f"Market crashing: SPY {spy['day_pct']:+.2f}%")
    if not vix["safe"]:
        reasons.append(f"Extreme fear: VIX={vix['vix']}")
    if not news["safe"]:
        reasons.append(f"Negative news: {news['headlines'][0][:50] if news['headlines'] else 'N/A'}")

    return {
        "symbol":    symbol,
        "safe":      safe,
        "spy":       spy,
        "vix":       vix,
        "news":      news,
        "reason":    " | ".join(reasons) if reasons else "Market conditions OK",
        "timestamp": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET"),
    }
