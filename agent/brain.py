"""
brain.py — Claude AI 0DTE scalping decisions
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
You are an expert 0DTE options day trader replicating a proven manual strategy:

PROVEN TRADES:
- AAPL dropped → bought Put → sold for +70%
- AAPL bounced → bought Call → sold for +35%

STRATEGY:
- 0DTE options only (same day expiry)
- Max $1.50 premium per contract
- ATM or 1 strike OTM only
- React to $0.30-$1.00 moves in 5 minutes
- Hold 1-5 minutes maximum
- Exit immediately when momentum dies

ENTRY:
- BUY_CALL: Price dropped fast → bounce expected
- BUY_PUT:  Price popped fast → pullback expected
- SKIP if: lunch hour, VIX extreme, bad news, open position

EXIT:
- Take profit at +30%
- Stop loss at -20%
- Max 5 min hold

NEVER trade after 3:30pm ET.

Respond ONLY with valid JSON:
{
  "action": "BUY_CALL" | "BUY_PUT" | "HOLD",
  "symbol": "AAPL" | "SPY" | "QQQ" | "NVDA" | "MCD",
  "contracts": 1,
  "strike": <float or null>,
  "expiry": "<YYYY-MM-DD or null>",
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "reason": "<one sentence>",
  "exit_target_pct": 30,
  "stop_loss_pct": 20
}
"""


def decide(signal: dict, open_position: dict | None, options_chain: list | None = None) -> dict:
    now_et = datetime.now(ET)

    if now_et.hour == 15 and now_et.minute >= 30:
        return {"action": "HOLD", "reason": "After 3:30pm — no new 0DTE"}

    position_context = (
        f"OPEN POSITION — DO NOT trade: {json.dumps(open_position)}"
        if open_position
        else "No open positions — free to trade."
    )

    user_message = f"""
Market snapshot:
{json.dumps(signal, indent=2)}

{position_context}

Time: {now_et.strftime("%H:%M:%S ET")}

Should I scalp this? JSON only.
"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
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

    except Exception as e:
        logger.error(f"Brain error: {e}")
        return {"action": "HOLD", "reason": f"Error: {e}"}


def should_exit(position: dict) -> tuple[bool, str]:
    pnl_pct = position.get("pnl_pct", 0)
    elapsed  = position.get("elapsed_seconds", 0)

    if pnl_pct >= 30:
        return True, f"✅ Take profit +{pnl_pct:.1f}%"
    if pnl_pct <= -20:
        return True, f"🛑 Stop loss {pnl_pct:.1f}%"
    if elapsed >= 300:
        return True, f"⏰ Max 5 min 0DTE exit"

    return False, ""
