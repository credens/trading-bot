"""
Notifications — Telegram + Email
=================================
Módulo central de notificaciones para los trading bots.
Usa requests para Telegram y smtplib para Gmail.
"""

import os
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "")


def send_telegram(message: str, parse_mode: str = "HTML") -> bool:
    """Envía mensaje por Telegram Bot API."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram no configurado (falta token o chat_id)")
        return False
    try:
        import requests
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": parse_mode,
        }, timeout=10)
        if resp.status_code == 200:
            return True
        log.warning(f"Telegram error {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")
        return False


def send_email(subject: str, body_html: str, to: str = "") -> bool:
    """Envía email via Gmail SMTP con App Password."""
    to = to or ALERT_EMAIL
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD or not to:
        log.warning("Email no configurado (falta gmail address, app password, o destinatario)")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"Trading Bot HQ <{GMAIL_ADDRESS}>"
        msg["To"] = to
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, to, msg.as_string())
        return True
    except Exception as e:
        log.warning(f"Email send failed: {e}")
        return False


def send_alert(title: str, message: str, level: str = "WARNING"):
    """Envía alerta por Telegram + Email."""
    emoji = "⚠️" if level == "WARNING" else "🚨" if level == "CRITICAL" else "ℹ️"

    # Telegram
    tg_msg = f"{emoji} <b>{title}</b>\n{message}"
    send_telegram(tg_msg)

    # Email
    color = "#ff8c00" if level == "WARNING" else "#ff0000" if level == "CRITICAL" else "#00cc66"
    html = f"""
    <div style="font-family:monospace; padding:20px; background:#1a1a2e; color:#ccc; border-radius:10px;">
        <h2 style="color:{color}; margin:0 0 10px;">{emoji} {title}</h2>
        <p style="font-size:14px; line-height:1.6;">{message.replace(chr(10), '<br>')}</p>
        <hr style="border-color:#333;">
        <small style="color:#888;">Trading Bot HQ — Alerta automática</small>
    </div>
    """
    send_email(f"{emoji} {title}", html)
