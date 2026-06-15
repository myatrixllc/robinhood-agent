"""
expiry.py — 0DTE expiry picker
Always picks today's expiry for same-day options
"""

import robin_stocks.robinhood as rh
from datetime import datetime
import pytz

ET = pytz.timezone("America/New_York")


def get_best_expiry(symbol: str, days_out: int = 0) -> str | None:
    try:
        chain = rh.options.get_chains(symbol)
        dates = chain.get("expiration_dates", [])
        if not dates:
            return None

        today = datetime.now(ET).date()
        today_str = today.strftime("%Y-%m-%d")

        # Try today first (0DTE)
        if today_str in dates:
            return today_str

        # If today not available (weekend/holiday) use nearest
        future = [
            d for d in dates
            if datetime.strptime(d, "%Y-%m-%d").date() >= today
        ]
        return future[0] if future else None

    except Exception:
        return None
