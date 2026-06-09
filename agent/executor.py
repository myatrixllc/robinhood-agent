"""
executor.py — Robinhood MCP trading executor
Uses OAuth2 tokens (saved by auth.py). Auto-refreshes tokens silently.
Currently supports stock orders (options coming soon from Robinhood).
"""

import os
import json
import time
import logging
import requests
from pathlib import Path
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)
ET     = pytz.timezone("America/New_York")

# ── Token management ──────────────────────────────────────────────────────────
TOKEN_FILE    = Path(__file__).parent.parent / ".robinhood_token.json"
TOKEN_URL     = "https://api.robinhood.com/oauth2/token/"
CLIENT_ID     = "robinhood-trading-mcp"
MCP_BASE_URL  = "https://agent.robinhood.com/mcp/trading"


def _load_token() -> dict:
    if not TOKEN_FILE.exists():
        raise FileNotFoundError(
            "No Robinhood token found. Run: python agent/auth.py"
        )
    return json.loads(TOKEN_FILE.read_text())


def _save_token(token_data: dict):
    token_data["saved_at"] = time.time()
    TOKEN_FILE.write_text(json.dumps(token_data, indent=2))
    TOKEN_FILE.chmod(0o600)


def _is_expired(token_data: dict, buffer_secs: int = 60) -> bool:
    saved_at   = token_data.get("saved_at", 0)
    expires_in = token_data.get("expires_in", 0)
    return (time.time() - saved_at) >= (expires_in - buffer_secs)


def _refresh_token(token_data: dict) -> dict:
    logger.info("Refreshing Robinhood access token...")
    resp = requests.post(TOKEN_URL, data={
        "grant_type":    "refresh_token",
        "refresh_token": token_data["refresh_token"],
        "client_id":     CLIENT_ID,
    }, timeout=15)
    resp.raise_for_status()
    new_token = resp.json()
    if "refresh_token" not in new_token:
        new_token["refresh_token"] = token_data["refresh_token"]
    _save_token(new_token)
    logger.info("Token refreshed successfully")
    return new_token


def _get_valid_token() -> str:
    token_data = _load_token()
    if _is_expired(token_data):
        token_data = _refresh_token(token_data)
    return token_data["access_token"]


# ── MCP request helper ────────────────────────────────────────────────────────
def _call(tool: str, params: dict) -> dict:
    access_token = _get_valid_token()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    payload = {"tool": tool, "parameters": params}
    try:
        resp = requests.post(
            MCP_BASE_URL,
            headers=headers,
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        logger.error(f"MCP HTTP error [{tool}]: {e.response.status_code} — {e.response.text}")
        return {"error": str(e), "status_code": e.response.status_code}
    except Exception as e:
        logger.error(f"MCP error [{tool}]: {e}")
        return {"error": str(e)}


# ── Account ───────────────────────────────────────────────────────────────────
def get_account() -> dict:
    return _call("get_account", {})


def get_buying_power() -> float:
    account = get_account()
    return float(account.get("buying_power", 0))


# ── Positions ─────────────────────────────────────────────────────────────────
def get_positions() -> list:
    result = _call("get_positions", {})
    return result.get("positions", [])


def get_open_position(symbol: str) -> dict | None:
    positions = get_positions()
    for p in positions:
        if p.get("symbol") == symbol and float(p.get("quantity", 0)) > 0:
            return p
    return None


# ── Stock quotes ──────────────────────────────────────────────────────────────
def get_quote(symbol: str) -> dict:
    return _call("get_quote", {"symbol": symbol})


# ── Stock orders (live now) ───────────────────────────────────────────────────
def place_stock_order(
    symbol:     str,
    side:       str,
    quantity:   float,
    order_type: str = "market",
) -> dict:
    payload = {
        "symbol":        symbol,
        "side":          side,
        "quantity":      quantity,
        "order_type":    order_type,
        "time_in_force": "gfd",
    }
    result = _call("place_order", payload)
    logger.info(f"Stock order placed: {side} {quantity} {symbol} → {result}")
    return result


def sell_position(symbol: str) -> dict:
    pos = get_open_position(symbol)
    if not pos:
        logger.warning(f"No open position in {symbol} to sell")
        return {"error": "No position found"}
    qty = float(pos.get("quantity", 0))
    return place_stock_order(symbol, "sell", qty)


# ── Options orders (coming soon from Robinhood) ───────────────────────────────
def place_option_order(
    symbol:      str,
    option_type: str,
    strike:      float,
    expiry:      str,
    contracts:   int = 1,
) -> dict:
    payload = {
        "symbol":          symbol,
        "option_type":     option_type,
        "strike":          strike,
        "expiration":      expiry,
        "quantity":        contracts,
        "side":            "buy",
        "position_effect": "open",
        "order_type":      "market",
    }
    result = _call("place_option_order", payload)
    if result.get("error"):
        logger.warning(f"Options not yet supported by Robinhood MCP: {result}")
    return result


# ── P&L tracking ─────────────────────────────────────────────────────────────
def enrich_position_pnl(position: dict) -> dict:
    try:
        symbol        = position.get("symbol")
        quote         = get_quote(symbol)
        current_price = float(quote.get("last_trade_price") or quote.get("price", 0))
        avg_cost      = float(position.get("average_buy_price", 0))
        if avg_cost > 0:
            pnl_pct = ((current_price - avg_cost) / avg_cost) * 100
            position["pnl_pct"]       = round(pnl_pct, 2)
            position["current_price"] = current_price
            position["entry_price"]   = avg_cost
    except Exception as e:
        logger.warning(f"P&L compute error: {e}")
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
