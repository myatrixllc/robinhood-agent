"""
executor_alpaca.py — Alpaca paper trading executor
Uses Alpaca API for options paper trading
Switch to live by changing PAPER_TRADING=false
"""

import os
import logging
import requests
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)
ET     = pytz.timezone("America/New_York")

# Per-symbol max price
MAX_PRICES = {
    "SPY":  3.00,
    "QQQ":  3.00,
    "AAPL": 2.00,
    "NVDA": 2.00,
    "MCD":  1.50,
}
MAX_OPTION_PRICE = 3.00  # global fallback
PAPER_TRADING    = os.environ.get("PAPER_TRADING", "true").lower() == "true"
# Remove /v2 from URL if present — added in _get/_post
_raw_url = os.environ.get("ALPACA_PAPER_BASE_URL", "https://paper-api.alpaca.markets/v2")
BASE_URL = _raw_url.replace("/v2", "").rstrip("/")
API_KEY          = os.environ.get("ALPACA_PAPER_API_KEY")
SECRET_KEY       = os.environ.get("ALPACA_PAPER_SECRET_KEY")

HEADERS = {
    "APCA-API-KEY-ID":     API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
    "Content-Type":        "application/json",
}

DATA_HEADERS = {
    "APCA-API-KEY-ID":     API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
}


def _get(endpoint: str, params: dict = None) -> dict:
    url  = f"{BASE_URL}/v2/{endpoint}"
    resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _post(endpoint: str, payload: dict) -> dict:
    url  = f"{BASE_URL}/v2/{endpoint}"
    resp = requests.post(url, headers=HEADERS, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _delete(endpoint: str) -> dict:
    url  = f"{BASE_URL}/v2/{endpoint}"
    resp = requests.delete(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return {}


def get_account() -> dict:
    return _get("account")


def get_buying_power() -> float:
    account = get_account()
    return float(account.get("buying_power", 0))


def get_option_positions() -> list:
    try:
        positions = _get("positions")
        return [p for p in positions if p.get("asset_class") == "us_option"]
    except Exception as e:
        logger.error(f"Positions error: {e}")
        return []


def has_open_position() -> bool:
    return len(get_option_positions()) > 0


def get_options_chain(symbol: str, expiry: str, option_type: str) -> list:
    try:
        params = {
            "underlying_symbols": symbol,
            "expiration_date":    expiry,
            "type":               option_type,
            "limit":              100,
        }
        result = _get("options/contracts", params=params)
        return result.get("option_contracts", [])
    except Exception as e:
        logger.error(f"Options chain error: {e}")
        return []


def find_otm_strike(symbol: str, option_type: str, expiry: str) -> float | None:
    try:
        contracts = get_options_chain(symbol, expiry, option_type)
        if not contracts:
            return None

        # Get current price
        url   = f"https://data.alpaca.markets/v2/stocks/{symbol}/quotes/latest"
        resp  = requests.get(url, headers=DATA_HEADERS, timeout=10)
        quote = resp.json()
        price = float(
            quote.get("quote", {}).get("ap", 0) or
            quote.get("quote", {}).get("bp", 0) or 0
        )

        if not price:
            return None

        strikes = sorted([float(c["strike_price"]) for c in contracts])

        if option_type == "call":
            candidates = [s for s in strikes if s >= price][:3]
        else:
            candidates = [s for s in strikes if s <= price][-3:]

        return candidates[0] if candidates else None

    except Exception as e:
        logger.error(f"Strike finder error: {e}")
        return None


def place_option_order(
    symbol:      str,
    option_type: str,
    strike:      float,
    expiry:      str,
    contracts:   int = 1,
) -> dict:
    try:
        chain    = get_options_chain(symbol, expiry, option_type)
        matching = [c for c in chain if float(c["strike_price"]) == strike]

        if not matching:
            return {"error": f"No contract: {symbol} {option_type} ${strike} {expiry}"}

        contract_symbol = matching[0]["symbol"]

        payload = {
            "symbol":        contract_symbol,
            "qty":           str(contracts),
            "side":          "buy",
            "type":          "market",
            "time_in_force": "day",
        }

        result = _post("orders", payload)
        logger.info(f"📝 PAPER order placed: {contract_symbol} → {result.get('id')}")
        return result

    except Exception as e:
        logger.error(f"Order failed: {e}")
        return {"error": str(e)}


def close_option_position(position: dict) -> dict:
    try:
        symbol = position.get("symbol")
        result = _delete(f"positions/{symbol}")
        logger.info(f"Position closed: {symbol}")
        return result
    except Exception as e:
        logger.error(f"Close failed: {e}")
        return {"error": str(e)}


def enrich_position_pnl(position: dict) -> dict:
    try:
        # Use unrealized_plpc (percentage) directly from Alpaca
        plpc = float(position.get("unrealized_plpc", 0) or 0)
        pnl_pct = round(plpc * 100, 2)

        # Fallback to manual calc
        if pnl_pct == 0:
            unrealized = float(position.get("unrealized_pl", 0) or 0)
            cost       = float(position.get("cost_basis", 0) or 0)
            if cost > 0:
                pnl_pct = round((unrealized / cost) * 100, 2)

        position["pnl_pct"]       = pnl_pct
        position["current_price"] = float(position.get("current_price", 0) or 0)
        position["entry_price"]   = float(position.get("avg_entry_price", 0) or 0)
        logger.info(f"P&L: {pnl_pct:+.1f}% (current=${position['current_price']})")
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
