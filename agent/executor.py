"""
executor.py — Robinhood options executor
Fixes:
  - Max option price $1.50
  - Proper P&L tracking
  - Single order guarantee
  - Better error handling
"""

import os
import logging
import robin_stocks.robinhood as rh
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)
ET     = pytz.timezone("America/New_York")

MAX_OPTION_PRICE = 1.50
_logged_in       = False


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


def get_option_positions() -> list:
    ensure_login()
    try:
        positions = rh.options.get_open_option_positions()
        return positions if positions else []
    except Exception as e:
        logger.error(f"Error getting positions: {e}")
        return []


def has_open_position() -> bool:
    positions = get_option_positions()
    return len(positions) > 0


def get_quote(symbol: str) -> dict:
    ensure_login()
    quote = rh.stocks.get_latest_price(symbol)
    return {"price": float(quote[0]) if quote else 0, "symbol": symbol}


def find_otm_strike(symbol: str, option_type: str, expiry: str) -> float | None:
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
            candidates = [s for s in strikes if s >= price][:3]
        else:
            candidates = [s for s in strikes if s <= price][-3:]

        for strike in candidates:
            opts = rh.options.find_options_by_expiration_and_strike(
                symbol, expirationDate=expiry,
                strikePrice=str(strike), optionType=option_type,
            )
            if opts:
                mark = float(opts[0].get("mark_price", 0) or 0)
                if 0 < mark <= MAX_OPTION_PRICE:
                    logger.info(f"Selected strike ${strike} @ ${mark}")
                    return strike

        logger.warning(f"No affordable strikes under ${MAX_OPTION_PRICE} for {symbol}")
        return None

    except Exception as e:
        logger.warning(f"Strike finder error: {e}")
        return None


def place_option_order(
    symbol:      str,
    option_type: str,
    strike:      float,
    expiry:      str,
    contracts:   int = 1,
) -> dict:
    ensure_login()
    try:
        opts = rh.options.find_options_by_expiration_and_strike(
            symbol, expirationDate=expiry,
            strikePrice=str(strike), optionType=option_type,
        )

        if not opts:
            return {"error": "No options data found"}

        mark = float(opts[0].get("mark_price", 0) or 0)

        if mark > MAX_OPTION_PRICE:
            logger.warning(f"Option too expensive: ${mark} > ${MAX_OPTION_PRICE}")
            return {"error": f"Option too expensive: ${mark}"}

        if mark <= 0:
            return {"error": "Invalid mark price"}

        limit = round(mark * 1.03, 2)

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

        logger.info(f"Order placed: {option_type} {symbol} ${strike} {expiry} mark=${mark} limit=${limit}")
        return result

    except Exception as e:
        logger.error(f"Option order failed: {e}")
        return {"error": str(e)}


def close_option_position(position: dict) -> dict:
    ensure_login()
    try:
        symbol = position.get("chain_symbol")
        expiry = position.get("expiration_date")
        strike = position.get("strike_price")
        otype  = position.get("option_type")
        qty    = int(float(position.get("quantity", 1)))

        opts = rh.options.find_options_by_expiration_and_strike(
            symbol, expirationDate=expiry,
            strikePrice=str(strike), optionType=otype,
        )

        mark  = float(opts[0].get("mark_price", 0)) if opts else 0
        limit = round(mark * 0.97, 2) if mark > 0 else 0.01

        result = rh.orders.order_sell_option_limit(
            positionEffect="close",
            creditOrDebit="credit",
            price=limit,
            symbol=symbol,
            quantity=qty,
            expirationDate=expiry,
            strike=strike,
            optionType=otype,
        )
        logger.info(f"Position closed: {symbol} {otype} ${strike} @ ${limit}")
        return result

    except Exception as e:
        logger.error(f"Close failed: {e}")
        return {"error": str(e)}


def enrich_position_pnl(position: dict) -> dict:
    try:
        symbol = position.get("chain_symbol")
        expiry = position.get("expiration_date")
        strike = position.get("strike_price")
        otype  = position.get("option_type")

        opts = rh.options.find_options_by_expiration_and_strike(
            symbol, expirationDate=expiry,
            strikePrice=str(strike), optionType=otype,
        )

        if opts:
            current = float(opts[0].get("mark_price", 0) or 0)
            entry   = float(position.get("average_price", 0) or 0)

            if entry == 0:
                entry = float(position.get("intraday_average_open_price", 0) or 0)

            if entry > 0 and current > 0:
                pnl_pct = ((current - entry) / entry) * 100
                position["pnl_pct"]       = round(pnl_pct, 2)
                position["current_price"] = current
                position["entry_price"]   = entry
                logger.info(f"P&L [{symbol}]: ${entry}→${current} = {pnl_pct:+.1f}%")
            else:
                position["pnl_pct"] = 0
        else:
            position["pnl_pct"] = 0

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
    logger.info(f"Daily loss: ${_daily_loss['total']:.2f}")


def daily_loss_limit_hit(limit: float = 200.0) -> bool:
    today = datetime.now(ET).date().isoformat()
    if _daily_loss["date"] != today:
        return False
    return _daily_loss["total"] >= limit
