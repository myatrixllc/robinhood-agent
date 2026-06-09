"""
brain.py — Claude AI decision layer
Sends market signals to Claude and gets a structured trade decision back.
"""

import os
import json
import logging
import anthropic

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SYSTEM_PROMPT = """
You are a disciplined options trading assistant. You analyze market signals
for AAPL and MCD and decide whether to place a trade.

STRICT RULES you must always follow:
1. Maximum 1 open contract at any time — if one is already open, say HOLD
2. Only trade BUY_CALL or BUY_PUT signals — never HOLD, SKIP, or ERROR
3. For calls: pick strike 1 step OTM, expiry ~14 days out
4. For puts:  pick strike 1 step OTM, expiry ~14 days out
5. Never trade if IV > 80% (options are overpriced)
6. Never trade within 5 days of earnings
7. Exit positions: take profit at +80%, stop loss at -50%

Respond ONLY with a valid JSON object, no extra text:
{
  "action":     "BUY_CALL" | "BUY_PUT" | "HOLD",
  "symbol":     "AAPL" | "MCD",
  "contracts":  1,
  "strike":     <float or null>,
  "expiry":     "<YYYY-MM-DD or null>",
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "reason":     "<one sentence explaining the decision>"
}
"""


def decide(signal: dict, open_position: dict | None, options_chain: list | None = None) -> dict:
    """
    Ask Claude to make a trade decision given the current signal and position state.
    Returns a parsed decision dict.
    """
    position_context = (
        f"Open position: {json.dumps(open_position)}"
        if open_position
        else "No open positions currently."
    )

    options_context = (
        f"Available options near ATM: {json.dumps(options_chain[:6])}"
        if options_chain
        else "Options chain not available — estimate strikes."
    )

    user_message = f"""
Current market signal:
{json.dumps(signal, indent=2)}

{position_context}

{options_context}

Should I place a trade? Respond with JSON only.
"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        decision = json.loads(raw.strip())
        logger.info(f"Claude decision: {decision}")
        return decision

    except json.JSONDecodeError as e:
        logger.error(f"Claude returned invalid JSON: {e}")
        return {"action": "HOLD", "reason": f"JSON parse error: {e}"}
    except Exception as e:
        logger.error(f"Brain error: {e}")
        return {"action": "HOLD", "reason": f"Brain error: {e}"}


def should_exit(position: dict) -> tuple[bool, str]:
    """
    Check if an open position should be exited based on P&L thresholds.
    Returns (should_exit: bool, reason: str)
    """
    if not position:
        return False, ""

    pnl_pct = position.get("pnl_pct", 0)

    if pnl_pct >= 80:
        return True, f"Take profit triggered at +{pnl_pct:.1f}%"
    if pnl_pct <= -50:
        return True, f"Stop loss triggered at {pnl_pct:.1f}%"

    return False, ""
