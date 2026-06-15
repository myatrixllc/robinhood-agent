"""
expiry.py — Smart expiry picker
"""

import robin_stocks.robinhood as rh
from datetime import datetime
import pytz

ET = pytz.timezone("America/New_York")


def get_best_expiry(symbol: str, days_out: int = 5) -> str | None:
    try:
        chain = rh.options.get_chains(symbol)
        dates = chain.get("expiration_dates", [])
        if not dates:
            return None
        today = datetime.now(ET).date()
        valid = [
            d for d in dates
            if (datetime.strptime(d, "%Y-%m-%d").date() - today).days >= 2
        ]
        if not valid:
            return None
        best = min(valid, key=lambda d: abs(
            (datetime.strptime(d, "%Y-%m-%d").date() - today).days - days_out
        ))
        return best
    except Exception:
        return None
