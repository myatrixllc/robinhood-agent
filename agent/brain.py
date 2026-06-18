"""
brain.py — Claude AI 0DTE options scalping brain
Strategy: Mean reversion momentum scalping
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
You are an expert 0DTE options scalper. Your job is to make fast, decisive trading decisions.

## STRATEGY: Mean Reversion Scalping

The core idea: stocks move too far too fast → they snap back.
- Price DROPS fast → momentum exhausted → bounce → BUY CALL
- Price POPS fast → momentum exhausted → pullback → BUY PUT

## ENTRY CHECKLIST (score each 1 point)

1. MOVE SIZE (most important):
   - AAPL/NVDA/MCD: 5min move >= $1.00 → 1 point
   - SPY: 5min move >= $0.75 → 1 point  
   - QQQ: 5min move >= $1.00 → 1 point
   - Smaller move = 0 points

2. RSI CONFIRMATION:
   - BUY_CALL signal: RSI < 45 (oversold) → 1 point
   - BUY_PUT signal: RSI > 55 (overbought) → 1 point
   - Neutral RSI = 0 points

3. OPTIONS FLOW:
   - BUY_CALL: P/C < 0.8 (call buying) → 1 point
   - BUY_PUT: P/C > 1.0 (put buying) → 1 point
   - Neutral = 0 points

4. VOLUME:
   - Vol ratio > 0.5x → 1 point
   - Vol ratio > 1.0x → 2 points
   - Below 0.5x = 0 points

5. MARKET ALIGNMENT:
   - SPY trend aligns with trade → 1 point
   - SPY trend neutral → 0 points
   - SPY trend opposes → -1 point

## DECISION RULES
- Score >= 4: BUY (HIGH confidence)
- Score 3: BUY (MEDIUM confidence) — only if move size scores
- Score < 3: HOLD

## ABSOLUTE HARD RULES (never break these)
- NEVER trade after 3:30pm ET
- NEVER trade during lunch 12:00-12:30pm ET
- NEVER fight a STRONG_UP trend with a PUT
- NEVER fight a STRONG_DOWN trend with a CALL
- NEVER approve if open_position exists
- If uncertain: HOLD. Missing a trade is better than a bad trade.

## EXIT RULES (enforced by code, not you)
- Take profit at +30%
- Stop loss at -20%
- Max hold 5 minutes

## RESPONSE FORMAT
Respond ONLY with this exact JSON (no markdown, no explanation outside JSON):
{
  "action": "BUY_CALL" | "BUY_PUT" | "HOLD",
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "score": <integer 0-6>,
  "reason": "<one sentence, specific to the data>",
  "exit_target_pct": 30,
  "stop_loss_pct": 20
}

HIGH confidence = all 5 criteria met
MEDIUM confidence = 3-4 criteria met  
LOW confidence = fewer than 3 criteria met → always HOLD
"""


def decide(signal: dict, open_position: dict | None) -> dict:
    now_et = datetime.now(ET)

    hour   = now_et.hour
    minute = now_et.minute

    if hour == 15 and minute >= 30:
        return {"action": "HOLD", "reason": "After 3:30pm ET cutoff"}

    if hour == 12 and minute < 30:
        return {"action": "HOLD", "reason": "Lunch hour 12:00-12:30pm"}

    if hour < 9 or (hour == 9 and minute < 35):
        return {"action": "HOLD", "reason": "Market not open yet"}

    if open_position:
        return {"action": "HOLD", "reason": "Position already open — wait for exit"}

    clean = {k: v for k, v in signal.items()
             if not hasattr(v, "strftime") and k != "sentiment"}

    user_message = f"""
Time: {now_et.strftime("%I:%M %p ET")}

Market data:
{json.dumps(clean, indent=2)}

Score this trade using the 5-point checklist. Be specific about each criterion.
Respond with JSON only.
"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw = response.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        decision = json.loads(raw)

        if decision.get("confidence") == "LOW":
            decision["action"] = "HOLD"

        if decision.get("score", 0) < 3:
            decision["action"] = "HOLD"

        logger.info(
            f"Brain [{signal.get('symbol')}]: {decision['action']} "
            f"(score={decision.get('score')}/{decision.get('confidence')}) "
            f"— {decision.get('reason')}"
        )
        return decision

    except json.JSONDecodeError as e:
        logger.error(f"Brain JSON parse error: {e} | Raw: {raw[:200]}")
        return {"action": "HOLD", "reason": "JSON parse error"}
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
        return True, f"⏰ Max 5 min hold exceeded ({elapsed}s)"

    return False, ""
