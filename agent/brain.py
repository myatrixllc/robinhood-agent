"""
brain.py — Claude AI with 7-layer signal analysis
Combines price momentum + options flow for best decisions
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
You are an expert 0DTE options day trader with deep knowledge of options flow.

YOUR PROVEN STRATEGY (replicate exactly):
- AAPL dropped → bought Put → sold for +70% profit
- AAPL bounced → bought Call → sold for +35% profit

SIGNAL INTERPRETATION:
- Price drops $1+ in 5 min + high put/call ratio = smart money already in puts = BOUNCE COMING = BUY CALL
- Price pops $1+ in 5 min + low put/call ratio = smart money already in calls = PULLBACK COMING = BUY PUT
- P/C ratio > 1.5 = heavy put buying = bearish sentiment
- P/C ratio < 0.6 = heavy call buying = bullish sentiment
- High ATM options volume = strong conviction move

OPTIONS FLOW RULES:
- Follow the OPPOSITE of what smart money already did (they front-ran the move)
- If big put volume already hit = price already dropped = buy CALL for bounce
- If big call volume already hit = price already popped = buy PUT for pullback

0DTE RULES:
- Same day expiry only
- ATM or 1 strike OTM
- Max 1 contract
- Exit at +30% profit
- Stop at -20% loss  
- Max 5 min hold
- Never trade after 3:30pm ET

SKIP if:
- Already have open position
- Lunch hour (11:30am-1pm)
- VIX extreme fear (>30)
- Bad news on the stock
- Last 30 min of market

Respond ONLY with valid JSON:
{
  "action": "BUY_CALL" | "BUY_PUT" | "HOLD",
  "symbol": "AAPL" | "SPY" | "QQQ" | "NVDA" | "MCD",
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

    if now_et.hour == 15 and now_et.minute >= 30:
        return {"action": "HOLD", "reason": "After 3:30pm — no new 0DTE positions"}

    position_context = (
        f"OPEN POSITION — DO NOT trade: {json.dumps(open_position)}"
        if open_position
        else "No open positions — free to trade."
    )

    user_message = f"""
Full market snapshot:
{json.dumps(signal, indent=2)}

{position_context}

Time: {now_et.strftime("%H:%M:%S ET")}
Session: {signal.get('session', 'NORMAL')}

Key signals:
- Price move 5min: ${signal.get('move_5min', 0):+.2f}
- Price move 2min: ${signal.get('move_2min', 0):+.2f}  
- RSI: {signal.get('rsi', 50)}
- Put/Call Ratio: {signal.get('pc_ratio', 1.0)} ({signal.get('flow_signal', 'NEUTRAL')} flow)
- Call volume: {signal.get('call_vol', 0):,}
- Put volume: {signal.get('put_vol', 0):,}
- VWAP deviation: ${signal.get('vwap_dev', 0):+.2f}
- Volume ratio: {signal.get('volume_ratio', 1.0)}x

Scanner signal: {signal.get('signal')}
Scanner reason: {signal.get('reason')}

Should I place this 0DTE scalp trade? JSON only.
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
