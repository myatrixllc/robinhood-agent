"""
notifier.py — SMS + email alerts for every trade action
Uses Twilio for SMS (free tier works fine for low volume)
"""

import os
import logging
import smtplib
from email.mime.text import MIMEText
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)
ET = pytz.timezone("America/New_York")

# ── Config from environment ───────────────────────────────────────────────────
TWILIO_SID    = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM   = os.environ.get("TWILIO_FROM_NUMBER", "")   # e.g. +12345678900
ALERT_TO      = os.environ.get("ALERT_PHONE_NUMBER", "")   # your cell

EMAIL_FROM    = os.environ.get("EMAIL_FROM", "")
EMAIL_TO      = os.environ.get("EMAIL_TO", "")
EMAIL_PASS    = os.environ.get("EMAIL_APP_PASSWORD", "")    # Gmail app password


def _sms(message: str):
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, ALERT_TO]):
        logger.warning("Twilio not configured — SMS skipped")
        return
    try:
        from twilio.rest import Client
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(body=message, from_=TWILIO_FROM, to=ALERT_TO)
        logger.info(f"SMS sent: {message[:60]}...")
    except Exception as e:
        logger.error(f"SMS failed: {e}")


def _email(subject: str, body: str):
    if not all([EMAIL_FROM, EMAIL_TO, EMAIL_PASS]):
        logger.warning("Email not configured — email skipped")
        return
    try:
        msg            = MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_FROM, EMAIL_PASS)
            server.send_message(msg)
        logger.info(f"Email sent: {subject}")
    except Exception as e:
        logger.error(f"Email failed: {e}")


# ── Public alert functions ────────────────────────────────────────────────────

def alert_trade_placed(decision: dict, order_result: dict):
    now   = datetime.now(ET).strftime("%I:%M %p ET")
    emoji = "📈" if decision.get("action") == "BUY_CALL" else "📉"
    msg = (
        f"{emoji} TRADE PLACED @ {now}\n"
        f"{decision.get('symbol')} {decision.get('action')}\n"
        f"Strike: ${decision.get('strike')}  Expiry: {decision.get('expiry')}\n"
        f"Reason: {decision.get('reason')}\n"
        f"Order ID: {order_result.get('id', 'N/A')}"
    )
    _sms(msg)
    _email(f"{emoji} Trade Placed — {decision.get('symbol')} {decision.get('action')}", msg)


def alert_position_closed(position: dict, reason: str):
    now    = datetime.now(ET).strftime("%I:%M %p ET")
    pnl    = position.get("pnl_pct", 0)
    emoji  = "✅" if pnl > 0 else "🛑"
    msg = (
        f"{emoji} POSITION CLOSED @ {now}\n"
        f"{position.get('symbol', 'N/A')} — P&L: {pnl:+.1f}%\n"
        f"Reason: {reason}"
    )
    _sms(msg)
    _email(f"{emoji} Position Closed — P&L {pnl:+.1f}%", msg)


def alert_daily_summary(trades: list, total_pnl: float):
    now   = datetime.now(ET).strftime("%Y-%m-%d")
    emoji = "📊"
    lines = [f"{emoji} Daily Summary — {now}", f"Total P&L: {total_pnl:+.2f}%", ""]
    for t in trades:
        lines.append(
            f"  {t.get('symbol')} {t.get('action')} → {t.get('pnl_pct', 0):+.1f}%"
        )
    if not trades:
        lines.append("  No trades today.")
    msg = "\n".join(lines)
    _sms(msg[:160])  # SMS 160 char limit
    _email(f"{emoji} Daily Trading Summary — {now}", msg)


def alert_error(context: str, error: str):
    now = datetime.now(ET).strftime("%I:%M %p ET")
    msg = f"⚠️ AGENT ERROR @ {now}\n{context}\n{error}"
    _sms(msg[:160])
    logger.error(msg)


def alert_daily_loss_limit(loss: float):
    msg = f"🛑 DAILY LOSS LIMIT HIT — ${loss:.2f} lost today. Trading stopped."
    _sms(msg)
    _email("🛑 Daily Loss Limit Hit — Trading Stopped", msg)
