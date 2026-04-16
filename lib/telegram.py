import logging
import requests
from config import Settings

log = logging.getLogger(__name__)


def send_message(settings: Settings, text: str) -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.info("Telegram not configured, skipping.")
        return

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": settings.telegram_chat_id, "text": text},
        timeout=10,
    )
    if resp.ok:
        log.info("Telegram ping sent.")
    else:
        log.error("Telegram failed: %s %s", resp.status_code, resp.text)
