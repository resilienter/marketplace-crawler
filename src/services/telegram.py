import html
import re

import requests

from src.config import settings
from src.core.logger import setup_logger
from src.models.schemas import PriceAnomalySignal

log = setup_logger("telegram", settings.logs_dir / "crawler.log")


def redact_telegram_token(text: str) -> str:
    """Masks bot token strings in URLs, headers, and exception traces to prevent secret leakage."""
    return re.sub(r"bot\d+:[a-zA-Z0-9_-]+", "bot[REDACTED]", text)


class TelegramNotifier:
    """
    Lightweight telemetry and alerting agent over Telegram Bot API.
    Enforces strict log token redaction and HTML payload escaping.
    """

    def __init__(self, bot_token: str | None = None, chat_id: str | None = None):
        self.bot_token = bot_token or settings.telegram_bot_token
        self.chat_id = chat_id or settings.telegram_chat_id
        self.enabled = bool(settings.telegram_enabled and self.bot_token and self.chat_id)

    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        if not self.enabled:
            clean_preview = (
                text.replace("🚨", "[ALERT]").replace("🔥", "[FIRE]").splitlines()[0]
                if text
                else "Empty"
            )
            log.info("[TELEGRAM DRY-RUN] Dispatch skipped (no credentials): >> %s", clean_preview)
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        try:
            resp = requests.post(url, json=payload, timeout=8.0)
            resp.raise_for_status()
            return True
        except requests.RequestException as err:
            safe_error_msg = redact_telegram_token(str(err))
            log.error("Failed to deliver Telegram notification: %s", safe_error_msg)
            return False

    def notify_price_drop(self, signal: PriceAnomalySignal) -> bool:
        safe_url = html.escape(signal.url)
        message = (
            f"🚨 <b>PRICE DROP ANOMALY DETECTED</b>\n\n"
            f"<b>Listing ID:</b> <code>{signal.listing_id}</code>\n"
            f"<b>Old Price:</b> <s>${signal.old_price:,.2f}</s>\n"
            f"<b>New Price:</b> <b>${signal.new_price:,.2f}</b>\n"
            f"<b>Discount:</b> <b>-{signal.drop_pct:.1f}%</b> (${signal.absolute_savings:,.2f})\n"
            f'<b>URL:</b> <a href="{safe_url}">Open Property Card</a>\n'
            f"<i>Timestamp: {signal.detected_at.strftime('%Y-%m-%d %H:%M:%S')} UTC</i>"
        )
        return self.send_message(message)
