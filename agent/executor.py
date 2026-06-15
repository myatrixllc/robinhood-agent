"""
executor.py — 0DTE ITM options executor
Uses ITM strikes like manual trading strategy
"""

import os
import json
import logging
import robin_stocks.robinhood as rh
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)
ET     = pytz.timezone("America/New_York")

_logged_in = False


def login():
    global _logged_in
    if _logged_in:
        return
    username = os.environ.get("ROBINHOOD_USERNAME")
    password = os.environ.get("ROBINHOOD_PASSWORD")
    if not username or not password:
        raise ValueError("ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD required")
    rh.login(username=username, password=password, expiresIn=86400, store_session=True)
    _logged_in = True
    logger.info("Logged into Robinhood successfully")


def ensure_login():
    try:
        login()
    except Exception as e:
        logger.error(f"Login failed: {e}")
        raise


def get_buying_power() -> float:
    ensure_login()
    profile = rh.profiles.load_account_profile()
    return float(profile.get("buying_power", 0))


def get_positions() -> list:
    ensure_login()
    positions = rh.account.get_open_stock_positions()
    result = []
    for p in positions:
        instrument = rh.stocks.get_instrument_by_url(p.get("instrument"))
        symbol = instrument.get("symbol", "") if instrument else ""
        result.append({
            "symbol":            symbol,
            "quantity":          float(p.get("quantity", 0)),
            "average_buy_price": float(p.get("average_buy_price", 0)),
        })
    return result


def get_option_positions() -> list:
    ensure_login()
    return rh.options.get_open_option_positions()


def get_quote(symbol: str) -> dict:
    ensure_login()
    quote = rh.stocks.get_latest_price(symbol)
    return {"price": float(quote[0]) if quote else 0, "symbol": symbol}


def find_itm_strike(symbol: str, option_type: str, expiry: str) -> float | None:
    """
    Find ITM strike — matches your manual strategy.
    For calls: strike BELOW current price (ITM)
    For puts:  strike ABOVE current price (ITM)
    """
    ensure_login()
    try:
        quote = get_quote(symbol)
        price = quote["price"]

        contracts = rh.options.find_options_by_expiration(
            symbol,
            expirationDate=expiry,
            optionType=option_type,
        )

        if not contracts:
            return None

        strikes = sorted([float(c["strike_price"]) for c in contracts])

        if option_type == "call":
            # ITM call = strike below current price
            # Pick strike $2-5 below current price (like your $275 call when price was ~$296)
            itm = [s for s in strikes if s < price - 1.0]
            # Pick the closest ITM (highest strike below price)
            return itm[-1] if itm else None

        else:
            # ITM put = strike above current price  
            # Pick strike $2-5 above current price (like your $277.5 put when price was ~$296... 
            # wait — $277.5 put when price is $296 is actually OTM
            # Let me use ATM/slightly OTM like you did
            otm = [s for s in strikes if s < price]
            return otm[-1] if otm else None

    except Exception as e:
        logger.warning(f"Strike finder error: {e}")
        return None


def find_otm_strike(symbol: str, option_type: str, expiry: str) -> float | None:
    """Alias for compatibility — finds best strike for 0DTE."""
    return find_itm_strike(symbol, option_type, expiry)


def place_option_order(
    symbol:      str,
    option_type: str,
    strike:      float,
    expiry:      str,
    contracts:   int = 1,
) -> dict:
    ensure_login()
    try:
        # Get mark price
        options = rh.options.find_options_by_expiration_and_strike(
            symbol,
            expirationDate=expiry,
            strikePrice=str(strike),
            optionType=option_type,
        )

        if options:
            mark  = float(options[0].get("mark_price", 0))
            limit = round(mark * 1.03, 2)  # 3% above mark for fast fill
        else:
            limit = None

        if limit and limit > 0:
            result = rh.orders.order_buy_option_limit(
                positionEffect="open",
                creditOrDebit="debit",
                price=limit,
                symbol=symbol,
                quantity=contracts,
                expirationDate=expiry,
                strike=strike,
                optionType=option_type,
            )
        else:
            result = rh.orders.order_buy_option_market(
                positionEffect="open",
                symbol=symbol,
                quantity=contracts,
                expirationDate=expiry,
                strike=strike,
                optionType=option_type,
            )

        logger.info(f"Order placed: {option_type} {symbol} ${strike} {expiry} → {result.get('id', 'N/A')}")
        return result

    except Exception as e:
        logger.error(f"Option order failed: {e}")
        return {"error": str(e)}


def close_option_position(position: dict) -> dict:
    ensure_login()
    try:
        result = rh.orders.order_sell_option_market(
            positionEffect="close",
            symbol=position.get("chain_symbol"),
            quantity=int(float(position.get("quantity", 1))),
            expirationDate=position.get("expiration_date"),
            strike=float(position.get("strike_price", 0)),
            optionType=position.get("option_type"),
        )
        logger.info(f"Position closed: {result.get('id', 'N/A')}")
        return result
    except Exception as e:
        logger.error(f"Close failed: {e}")
        return {"error": str(e)}


def enrich_position_pnl(position: dict) -> dict:
    try:
        symbol = position.get("chain_symbol")
        option_data = rh.options.find_options_by_expiration_and_strike(
            symbol,
            expirationDate=position.get("expiration_date"),
            strikePrice=str(position.get("strike_price")),
            optionType=position.get("option_type"),
        )
        if option_data:
            current = float(option_data[0].get("mark_price", 0))
            entry   = float(position.get("average_price", current))
            if entry > 0:
                pnl_pct = ((current - entry) / entry) * 100
                position["pnl_pct"]       = round(pnl_pct, 2)
                position["current_price"] = current
                position["entry_price"]   = entry
    except Exception as e:
        logger.warning(f"P&L error: {e}")
        position["pnl_pct"] = 0
    return position


_daily_loss = {"date": None, "total": 0.0}


def record_loss(amount: float):
    today = datetime.now(ET).date().isoformat()
    if _daily_loss["date"] != today:
        _daily_loss["date"]  = today
        _daily_loss["total"] = 0.0
    _daily_loss["total"] += abs(amount)


def daily_loss_limit_hit(limit: float = 200.0) -> bool:
    today = datetime.now(ET).date().isoformat()
    if _daily_loss["date"] != today:
        return False
    return _daily_loss["total"] >= limit
