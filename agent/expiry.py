"""
expiry.py — Smart expiry date picker
Picks the best expiry ~14 days out from Robinhood's available dates
"""
import robin_stocks.robinhood as rh
from datetime import datetime
import pytz

ET = pytz.timezone("America/New_York")

def get_best_expiry(symbol: str) -> str:
    """Pick expiry closest to 14 days from today."""
    try:
        chain = rh.options.get_chains(symbol)
        dates = chain.get("expiration_dates", [])
        if not dates:
            return None
        today = datetime.now(ET).date()
        # Find date closest to 14 days out
        best = min(dates, key=lambda d: abs(
            (datetime.strptime(d, "%Y-%m-%d").date() - today).days - 14
        ))
        return best
    except Exception as e:
        return None
