"""
executor.py — Robinhood trading executor using robin_stocks
Supports both stocks AND options trading.
Works fully unattended on VM 24/7.
"""

import os
import json
import logging
import robin_stocks.robinhood as rh
from datetime import datetime, timedelta
import pytz

logger = logging.getLogger(__name__)
ET     = pytz.timezone("America/New_York")

# ── Login ─────────────────────────────────────────────────────────────────────
_logged_in = False

def login():
    global _logged_in
    if _logged_in:
        return
    username = os.environ.get("ROBINHOOD_USERNAME")
    password = os.environ.get("ROBINHOOD_PASSWORD")
    if not username or not password:
        raise ValueError("ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD required in .env")
    rh.login(
        username=username,
        password=password,
        expiresIn=86400,      # 24 hours
        store_session=True,   # saves session to disk, auto-renews
        by_sms=True,          # MFA via SMS
    )
    _logged_in = True
    logger.info("Logged into Robinhood successfully")


def ensure_login():
    """Call before every trade operation."""
    try:
        login()
    except Exception as e:
        logger.error(f"Login failed: {e}")
        raise


# ── Account ───────────────────────────────────────────────────────────────────
def get_buying_power() -> float:
    ensure_login()
    profile = rh.profiles.load_account_profile()
    return float(profile.get("buying_power", 0))


def get_account() -> dict:
    ensure_login()
    return rh.profiles.load_account_profile()


# ── Quotes ────────────────────────────────────────────────────────────────────
def get_quote(symbol: str) -> dict:
    ensure_login()
    quote = rh.stocks.get_latest_price(symbol)
    return {"price": float(quote[0]) if quote else 0, "symbol": symbol}


# ── Stock positions ───────────────────────────────────────────────────────────
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
            "id":                p.get("url"),
        })
    return result


def get_option_positions() -> list:
    ensure_login()
    return rh.options.get_open_option_positions()


def get_all_positions() -> list:
    """Return both stock and option positions."""
    stocks  = get_positions()
    options = get_option_positions()
    return stocks + options


def get_open_position(symbol: str) -> dict | None:
    for p in get_positions():
        if p.get("symbol") == symbol and p.get("quantity", 0) > 0:
            return p
    return None


# ── Stock orders ──────────────────────────────────────────────────────────────
def place_stock_order(
    symbol:     str,
    side:       str,      # 'buy' or 'sell'
    quantity:   float,
    order_type: str = "market",
) -> dict:
    ensure_login()
    if side == "buy":
        result = rh.orders.order_buy_market(symbol, quantity)
    else:
        result = rh.orders.order_sell_market(symbol, quantity)
    logger.info(f"Stock order: {side} {quantity} {symbol} → {result.get('id', 'N/A')}")
    return result


def sell_stock_position(symbol: str) -> dict:
    pos = get_open_position(symbol)
    if not pos:
        return {"error": f"No open position in {symbol}"}
    return place_stock_order(symbol, "sell", pos["quantity"])


# ── Options orders ✅ ─────────────────────────────────────────────────────────
def get_options_chain(symbol: str, expiry: str, option_type: str) -> list:
    """
    Get options chain for a symbol.
    option_type: 'call' or 'put'
    expiry: 'YYYY-MM-DD'
    """
    ensure_login()
    try:
        chain = rh.options.get_options_chain(symbol)
        contracts = rh.options.find_options_by_expiration(
            symbol,
            expirationDate=expiry,
            optionType=option_type,
        )
        return contracts or []
    except Exception as e:
        logger.warning(f"Options chain error: {e}")
        return []


def find_otm_strike(symbol: str, option_type: str, expiry: str) -> float | None:
    """Find the first OTM strike for a given symbol and option type."""
    ensure_login()
    try:
        quote       = get_quote(symbol)
        price       = quote["price"]
        contracts   = rh.options.find_options_by_expiration(
            symbol,
            expirationDate=expiry,
            optionType=option_type,
        )
        if not contracts:
            return None
        strikes = sorted([float(c["strike_price"]) for c in contracts])
        if option_type == "call":
            # First strike above current price
            otm = [s for s in strikes if s > price]
            return otm[0] if otm else None
        else:
            # First strike below current price
            otm = [s for s in strikes if s < price]
            return otm[-1] if otm else None
    except Exception as e:
        logger.warning(f"Strike finder error: {e}")
        return None


def place_option_order(
    symbol:      str,
    option_type: str,    # 'call' or 'put'
    strike:      float,
    expiry:      str,    # 'YYYY-MM-DD'
    contracts:   int = 1,
) -> dict:
    """
    Buy to open an options contract.
    Uses limit order at mark price for better fills.
    """
    ensure_login()
    try:
        # Get mark price for limit order
        options = rh.options.find_options_by_expiration_and_strike(
            symbol,
            expirationDate=expiry,
            strikePrice=str(strike),
            optionType=option_type,
        )
        if options:
            mark = float(options[0].get("mark_price", 0))
            limit_price = round(mark * 1.02, 2)  # 2% above mark for quick fill
        else:
            limit_price = None

        if limit_price:
            result = rh.orders.order_buy_option_limit(
                positionEffect="open",
                creditOrDebit="debit",
                price=limit_price,
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

        logger.info(f"Option order placed: {option_type} {symbol} ${strike} {expiry} → {result.get('id', 'N/A')}")
        return result

    except Exception as e:
        logger.error(f"Option order failed: {e}")
        return {"error": str(e)}


def close_option_position(position: dict) -> dict:
    """Sell to close an open option position."""
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
        logger.info(f"Option closed: {result.get('id', 'N/A')}")
        return result
    except Exception as e:
        logger.error(f"Close option failed: {e}")
        return {"error": str(e)}


# ── P&L tracking ─────────────────────────────────────────────────────────────
def enrich_position_pnl(position: dict) -> dict:
    """Add pnl_pct to position."""
    try:
        symbol = position.get("symbol") or position.get("chain_symbol")
        if not symbol:
            return position

        # Options position
        if position.get("option_type"):
            option_data = rh.options.find_options_by_expiration_and_strike(
                symbol,
                expirationDate=position.get("expiration_date"),
                strikePrice=str(position.get("strike_price")),
                optionType=position.get("option_type"),
            )
            if option_data:
                current = float(option_data[0].get("mark_price", 0))
                entry   = float(position.get("average_price", current))
        else:
            # Stock position
            quote   = get_quote(symbol)
            current = quote["price"]
            entry   = float(position.get("average_buy_price", current))

        if entry > 0:
            pnl_pct = ((current - entry) / entry) * 100
            position["pnl_pct"]       = round(pnl_pct, 2)
            position["current_price"] = current
            position["entry_price"]   = entry

    except Exception as e:
        logger.warning(f"P&L error: {e}")
        position["pnl_pct"] = 0

    return position


# ── Daily loss guard ──────────────────────────────────────────────────────────
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
