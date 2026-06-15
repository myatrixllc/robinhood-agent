"""
brain.py — 0DTE scalping brain
Matches manual strategy: cheap options, quick in/out
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
You are an expert 0DTE options day trader. You replicate a proven manual strategy:

PROVEN MANUAL TRADES (replicate this exactly):
- AAPL dropped → bought $277.5 Put for $1.00 → sold for $1.70 (+70%)
- AAPL bounced → bought $275 Call for $1.00 → sold for $1.35 (+35%)

YOUR STRATEGY:
- 0DTE options only (same day expiry)
- Cheap options $0.50-$2.00 premium
- ATM or slightly OTM strikes
- React to $1+ moves in 1-5 minutes
- Hold 1-5 minutes maximum
- Exit immediately when momentum dies

ENTRY RULES:
- BUY_CALL: Stock dropped $1+ in 5 min → bounce expected
- BUY_PUT: Stock popped $1+ in 5 min → pullback expected
- SKIP if: lunch hour, VIX extreme, bad news
- SKIP if: already have open position
- SKIP if: less than 30 min to market close

EXIT RULES:
- Take profit at +30% (quick win)
- Stop loss at -20% (tight stop)
- Max hold 5 minutes (0DTE moves fast)
- Exit immediately on momentum reversal

IMPORTANT:
- These are CHEAP options ($0.50-2.00)
- Big % moves on small $ moves
- Speed is everything — in and out fast
- Never hold 0DTE options overnight

Respond ONLY with valid JSON:
{
  "action": "BUY_CALL" | "BUY_PUT" | "HOLD",
  "symbol": "AAPL" | "MCD",
  "contracts": 1,
  "strike": <float or null>,
  "expiry": "<YYYY-MM-DD or null>",
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "reason": "<one sentence max>",
  "exit_target_pct": 30,
  "stop_loss_pct": 20
}
"""


def decide(signal: dict, open_position: dict | None, options_chain: list | None = None) -> dict:
    now_et = datetime.now(ET)

    # Don't trade last 30 min — 0DTE liquidity dies
    if now_et.hour == 15 and now_et.minute >= 30:
        return {"action": "HOLD", "reason": "Last 30 min — 0DTE liquidity too low"}

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

This is 0DTE trading. React fast. Should I place this scalp trade?
JSON only.
"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
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
