"""
executor_alpaca.py — Alpaca paper/live options executor
Hard price limits, proper P&L, clean error handling
"""

import os
import logging
import requests
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)
ET     = pytz.timezone("America/New_York")

# ── Per-symbol max option price ───────────────────────────────────────────────
MAX_PRICES = {
    "SPY":  1.50,
    "QQQ":  1.50,
    "AAPL": 1.50,
    "NVDA": 1.50,
    "MCD":  1.50,
}
DEFAULT_MAX_PRICE = 1.50

# ── API Config ────────────────────────────────────────────────────────────────
PAPER_TRADING = os.environ.get("PAPER_TRADING", "true").lower() == "true"
BASE_URL      = os.environ.get("ALPACA_PAPER_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")

API_KEY    = os.environ.get("ALPACA_PAPER_API_KEY") if PAPER_TRADING else os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_PAPER_SECRET_KEY") if PAPER_TRADING else os.environ.get("ALPACA_SECRET_KEY")

HEADERS = {
    "APCA-API-KEY-ID":     API_KEY or "",
    "APCA-API-SECRET-KEY": SECRET_KEY or "",
    "Content-Type":        "application/json",
}

DATA_HEADERS = {
    "APCA-API-KEY-ID":     API_KEY or "",
    "APCA-API-SECRET-KEY": SECRET_KEY or "",
}


def _url(endpoint: str) -> str:
    base = BASE_URL.rstrip("/")
    if not base.endswith("/v2"):
        base = base + "/v2"
    return f"{base}/{endpoint.lstrip('/')}"


def _get(endpoint: str, params: dict = None) -> dict | list:
    resp = requests.get(_url(endpoint), headers=HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _post(endpoint: str, payload: dict) -> dict:
    resp = requests.post(_url(endpoint), headers=HEADERS, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _delete(endpoint: str) -> dict:
    resp = requests.delete(_url(endpoint), headers=HEADERS, timeout=15)
    if resp.status_code == 204:
        return {"status": "closed"}
    resp.raise_for_status()
    return resp.json()


def get_account() -> dict:
    return _get("account")


def get_buying_power() -> float:
    return float(get_account().get("buying_power", 0))


def get_option_positions() -> list:
    try:
        positions = _get("positions")
        if isinstance(positions, list):
            return [p for p in positions if p.get("asset_class") == "us_option"]
        return []
    except Exception as e:
        logger.error(f"get_option_positions error: {e}")
        return []


def has_open_position() -> bool:
    return len(get_option_positions()) > 0


def get_options_chain(symbol: str, expiry: str, option_type: str) -> list:
    try:
        result = _get("options/contracts", params={
            "underlying_symbols": symbol,
            "expiration_date":    expiry,
            "type":               option_type,
            "limit":              100,
        })
        return result.get("option_contracts", []) if isinstance(result, dict) else []
    except Exception as e:
        logger.error(f"Options chain error [{symbol}]: {e}")
        return []


def _get_quote_price(symbol: str) -> float:
    try:
        url  = f"https://data.alpaca.markets/v2/stocks/{symbol}/quotes/latest"
        resp = requests.get(url, headers=DATA_HEADERS, timeout=10)
        data = resp.json()
        q    = data.get("quote", {})
        bid  = float(q.get("bp", 0) or 0)
        ask  = float(q.get("ap", 0) or 0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
        return bid or ask
    except Exception as e:
        logger.warning(f"Quote error [{symbol}]: {e}")
        return 0


def _get_option_mid_price(contract_symbol: str) -> float:
    try:
        url  = f"https://data.alpaca.markets/v1beta1/options/quotes/latest"
        resp = requests.get(url, headers=DATA_HEADERS,
                           params={"symbols": contract_symbol}, timeout=10)
        data = resp.json()
        q    = data.get("quotes", {}).get(contract_symbol, {})
        bid  = float(q.get("bp", 0) or 0)
        ask  = float(q.get("ap", 0) or 0)
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2, 2)
        return 0
    except Exception:
        return 0


def find_otm_strike(symbol: str, option_type: str, expiry: str) -> float | None:
    try:
        max_price = MAX_PRICES.get(symbol, DEFAULT_MAX_PRICE)
        price     = _get_quote_price(symbol)

        if not price:
            logger.warning(f"Could not get price for {symbol}")
            return None

        contracts = get_options_chain(symbol, expiry, option_type)
        if not contracts:
            logger.warning(f"No contracts found for {symbol} {expiry} {option_type}")
            return None

        strikes = sorted(set(float(c["strike_price"]) for c in contracts))

        if option_type == "call":
            candidates = [s for s in strikes if s >= price][:3]
        else:
            candidates = [s for s in strikes if s <= price][-3:]
            candidates = list(reversed(candidates))

        logger.info(f"Strike candidates [{symbol} {option_type}]: {candidates}")

        for strike in candidates:
            matching = [c for c in contracts
                       if abs(float(c["strike_price"]) - strike) < 0.01]
            if not matching:
                continue

            contract_sym = matching[0].get("symbol", "")
            mid_price    = _get_option_mid_price(contract_sym)

            if mid_price <= 0:
                logger.info(f"No mid price for {contract_sym} — trying anyway")
                return strike

            if mid_price <= max_price:
                logger.info(f"✅ Selected: {symbol} {option_type} ${strike} @ ${mid_price}")
                return strike
            else:
                logger.info(f"⛔ ${strike} too expensive: ${mid_price} > ${max_price}")

        logger.warning(f"No affordable strikes for {symbol} under ${max_price}")
        return None

    except Exception as e:
        logger.error(f"find_otm_strike error [{symbol}]: {e}")
        return None


def place_option_order(
    symbol:      str,
    option_type: str,
    strike:      float,
    expiry:      str,
    contracts:   int = 1,
) -> dict:
    try:
        max_price = MAX_PRICES.get(symbol, DEFAULT_MAX_PRICE)
        chain     = get_options_chain(symbol, expiry, option_type)
        matching  = [c for c in chain if abs(float(c["strike_price"]) - strike) < 0.01]

        if not matching:
            return {"error": f"No contract: {symbol} {option_type} ${strike} {expiry}"}

        contract_sym = matching[0]["symbol"]

        mid_price = _get_option_mid_price(contract_sym)
        if mid_price > max_price:
            return {"error": f"Too expensive: ${mid_price} > ${max_price}"}

        payload = {
            "symbol":        contract_sym,
            "qty":           str(contracts),
            "side":          "buy",
            "type":          "market",
            "time_in_force": "day",
        }

        result = _post("orders", payload)
        logger.info(f"{'📝 PAPER' if PAPER_TRADING else '🔴 LIVE'} order: {contract_sym} @ ~${mid_price}")
        return result

    except Exception as e:
        logger.error(f"place_option_order error: {e}")
        return {"error": str(e)}


def close_option_position(position: dict) -> dict:
    try:
        symbol = position.get("symbol")
        if not symbol:
            return {"error": "No symbol"}
        result = _delete(f"positions/{symbol}")
        logger.info(f"Position closed: {symbol}")
        return result
    except Exception as e:
        logger.error(f"close_option_position error: {e}")
        return {"error": str(e)}


def enrich_position_pnl(position: dict) -> dict:
    try:
        plpc = position.get("unrealized_plpc")
        if plpc is not None:
            pnl_pct = round(float(plpc) * 100, 2)
        else:
            unrealized = float(position.get("unrealized_pl", 0) or 0)
            cost       = float(position.get("cost_basis", 0) or 0)
            pnl_pct    = round((unrealized / cost) * 100, 2) if cost > 0 else 0

        position["pnl_pct"]       = pnl_pct
        position["current_price"] = float(position.get("current_price", 0) or 0)
        position["entry_price"]   = float(position.get("avg_entry_price", 0) or 0)

        logger.info(
            f"P&L [{position.get('symbol', '?')}]: "
            f"${position['entry_price']}→${position['current_price']} "
            f"= {pnl_pct:+.1f}%"
        )

    except Exception as e:
        logger.warning(f"enrich_position_pnl error: {e}")
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
