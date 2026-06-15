"""
brain.py — Claude AI scalping decision engine
"""

import os
import json
import logging
import anthropic
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)
ET     = pytz.timezone("America/New_York")

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SYSTEM_PROMPT = """
You are an expert options scalp trader. You make fast, disciplined decisions
based on intraday price action for AAPL and MCD.

YOUR TRADING STYLE:
- Scalp trades: in and out within 1-10 minutes
- Ride momentum: catch the bounce after a fast drop, or pullback after a fast pop
- Small consistent wins: target +15-25% on options, cut at -10%
- React to price velocity: $2-10 moves in 1-2 minutes = opportunity

ENTRY RULES:
- BUY_CALL: Price dropped fast ($2+), below VWAP, volume confirming, RSI not overbought
- BUY_PUT: Price popped fast ($2+), above VWAP, volume confirming, RSI not oversold
- SKIP if: lunch hour, no volume, earnings coming, IV too high
- SKIP if: already have open position

OPTION SELECTION:
- Strike: 1 step OTM
- Expiry: nearest weekly (3-7 days out for scalps)
- Quantity: always 1 contract

EXIT RULES:
- EXIT: momentum reverses direction
- EXIT: +15% profit minimum, take it fast
- EXIT: -10% loss, no hesitation
- EXIT: 10 min max hold, no exceptions

Respond ONLY with valid JSON:
{
  "action": "BUY_CALL" | "BUY_PUT" | "HOLD",
  "symbol": "AAPL" | "MCD",
  "contracts": 1,
  "strike": <float or null>,
  "expiry": "<YYYY-MM-DD or null>",
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "reason": "<one sentence max>",
  "exit_target_pct": <float>,
  "stop_loss_pct": <float>
}
"""


def decide(signal: dict, open_position: dict | None, options_chain: list | None = None) -> dict:
    position_context = (
        f"OPEN POSITION EXISTS: {json.dumps(open_position)} — DO NOT open new position!"
        if open_position
        else "No open positions — free to trade."
    )

    user_message = f"""
Current market snapshot:
{json.dumps(signal, indent=2)}

{position_context}

Current time ET: {datetime.now(ET).strftime("%H:%M:%S")}

Should I scalp this? Respond with JSON only.
"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        decision = json.loads(raw.strip())
        logger.info(f"Brain [{signal.get('symbol')}]: {decision['action']} — {decision.get('reason')}")
        return decision

    except json.JSONDecodeError as e:
        logger.error(f"Brain JSON error: {e}")
        return {"action": "HOLD", "reason": f"Parse error: {e}"}
    except Exception as e:
        logger.error(f"Brain error: {e}")
        return {"action": "HOLD", "reason": f"Brain error: {e}"}


def should_exit(position: dict) -> tuple[bool, str]:
    pnl_pct = position.get("pnl_pct", 0)
    elapsed  = position.get("elapsed_seconds", 0)

    if pnl_pct >= 15:
        return True, f"✅ Take profit at +{pnl_pct:.1f}%"
    if pnl_pct <= -10:
        return True, f"🛑 Stop loss at {pnl_pct:.1f}%"
    if elapsed >= 600:
        return True, f"⏰ Max 10 min hold reached"

    return False, ""
