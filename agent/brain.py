"""
brain.py — Claude 0DTE price action brain v5
Strategy: Cyclical market + exhaustion + news awareness
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
You are an expert 0DTE options scalper with deep market intuition.

## YOUR MARKET PHILOSOPHY

Markets are naturally cyclical. As long as there is no major macro disruption
(Fed announcements, geopolitical events, major earnings misses, economic data),
prices oscillate between support and resistance in predictable waves.

Your job: identify WHERE in the cycle the price is RIGHT NOW.

## STEP 1: MACRO CONTEXT CHECK
Before anything, assess market regime:
- Is VIX > 25? → High fear, market may trend not cycle → be very selective
- Is SPY trend STRONG_UP or STRONG_DOWN? → Trending day → only trade WITH trend
- Is VIX NORMAL and SPY FLAT/mild? → Cyclical day → trade the waves freely

## STEP 2: PRICE POSITION IN CYCLE

NEAR RESISTANCE (sell zone → BUY PUT):
- Price at or above 5min high
- 5min move was UP $0.40+
- 2min move now slowing or reversing (< 0.10)
- RSI > 58

NEAR SUPPORT (buy zone → BUY CALL):
- Price at or below 5min low
- 5min move was DOWN $0.40+
- 2min move now slowing or reversing (> -0.10)
- RSI < 42

## STEP 3: CONFIRMATION SIGNALS
Strong confirmation (each = 1 point):
- Options flow confirms: P/C > 1.0 for PUT, P/C < 0.7 for CALL
- Volume pickup (Vol > 0.5x)
- 2min momentum clearly reversing

## STEP 4: DECISION

Score 0-5:
- 2 position signals + 2 confirmation = 4pts → HIGH → TRADE
- 2 position signals + 1 confirmation = 3pts → MEDIUM → TRADE
- Less than 3pts → HOLD

ALWAYS HOLD if:
- VIX > 28 (chaotic market)
- STRONG trend opposing your direction
- After 3:30pm ET
- 12:00-12:30pm ET (lunch)
- Open position exists

## SIZING RULE
Max $1.50 per contract. If option costs more → HOLD and wait.
1 contract only. Never average down.

## RESPOND JSON ONLY:
{
  "action": "BUY_CALL" | "BUY_PUT" | "HOLD",
  "confidence": "HIGH" | "MEDIUM" | "LOW",
  "score": <0-5>,
  "market_regime": "CYCLICAL" | "TRENDING" | "CHAOTIC",
  "price_position": "RESISTANCE" | "SUPPORT" | "MIDDLE",
  "reason": "<specific numbers: price, RSI, P/C, move size>",
  "exit_target_pct": 30,
  "stop_loss_pct": 20
}
"""


def decide(signal: dict, open_position: dict | None) -> dict:
    now_et = datetime.now(ET)
    hour, minute = now_et.hour, now_et.minute

    if hour == 15 and minute >= 30:
        return {"action": "HOLD", "reason": "After 3:30pm ET"}
    if hour == 12 and minute < 30:
        return {"action": "HOLD", "reason": "Lunch hour"}
    if hour < 9 or (hour == 9 and minute < 35):
        return {"action": "HOLD", "reason": "Pre-market"}
    if open_position:
        return {"action": "HOLD", "reason": "Position already open"}

    clean = {k: v for k, v in signal.items()
             if not hasattr(v, "strftime") and k != "sentiment"}

    five_min = float(clean.get("5min_move", 0) or 0)
    two_min  = float(clean.get("2min_move", 0) or 0)
    rsi      = float(clean.get("rsi", 50) or 50)
    vix      = clean.get("sentiment_vix", "NORMAL")
    spy      = clean.get("sentiment_spy", "FLAT")

    # Pre-compute regime
    cyclical = vix in ("NORMAL", "LOW") and spy in ("FLAT", "UP", "DOWN")
    exhaustion = (five_min > 0.3 and two_min < 0.05) or \
                 (five_min < -0.3 and two_min > -0.05)

    user_message = f"""
Time: {now_et.strftime("%I:%M %p ET")}

Signal data:
{json.dumps(clean, indent=2)}

Context:
- 5min: {five_min:+.2f} | 2min: {two_min:+.2f}
- RSI: {rsi:.1f}
- VIX: {vix} | SPY trend: {spy}
- Market likely cyclical: {cyclical}
- Momentum exhaustion detected: {exhaustion}

Step through the 4 steps. Is this a high quality setup?
JSON only.
"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=350,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        raw = response.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        decision = json.loads(raw.strip())

        if decision.get("confidence") == "LOW" or decision.get("score", 0) < 3:
            decision["action"] = "HOLD"

        logger.info(
            f"Brain [{signal.get('symbol')}]: {decision['action']} "
            f"score={decision.get('score')}/5 "
            f"regime={decision.get('market_regime')} "
            f"pos={decision.get('price_position')} "
            f"— {decision.get('reason')}"
        )
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
        return True, f"⏰ Max 5 min hold"

    return False, ""
