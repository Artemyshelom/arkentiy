"""
Telegram Bot API клиент.
Документация: https://core.telegram.org/bots/api

Rate limit: 30 сообщений/сек, 20 сообщений/мин в одну группу.
"""

import asyncio
import logging

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

BASE_URL = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
REQUEST_TIMEOUT = 15.0


async def send_message(
    chat_id: str,
    text: str,
    parse_mode: str = "HTML",
    disable_notification: bool = False,
    retry: int = 3,
) -> bool:
    """
    Отправляет сообщение в чат. Возвращает True при успехе.
    Retry с exponential backoff при ошибках.
    """
    for attempt in range(retry):
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.post(
                    f"{BASE_URL}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": parse_mode,
                        "disable_notification": disable_notification,
                    },
                )
                data = response.json()
                if data.get("ok"):
                    return True

                # Telegram rate limit (429)
                if response.status_code == 429:
                    retry_after = data.get("parameters", {}).get("retry_after", 5)
                    logger.warning(f"Telegram rate limit, жду {retry_after}с")
                    await asyncio.sleep(retry_after)
                    continue

                logger.error(f"Telegram ошибка: {data}")
                return False

        except httpx.TimeoutException:
            wait = 2 ** attempt
            logger.warning(f"Telegram timeout (попытка {attempt+1}), жду {wait}с")
            await asyncio.sleep(wait)
        except Exception as e:
            logger.error(f"Telegram неожиданная ошибка: {e}")
            return False

    return False


# --- Удобные методы для каждого чата ---

async def alert(text: str) -> bool:
    """Критический алерт в #алерты (управляющие + Артемий)."""
    return await send_message(settings.telegram_chat_alerts, text)


async def report(text: str) -> bool:
    """Ежедневный отчёт в #отчёты (менеджмент)."""
    return await send_message(settings.telegram_chat_reports, text)


async def meeting(text: str) -> bool:
    """Пре-встречное саммари в #встречи (Артемий)."""
    return await send_message(settings.telegram_chat_meetings, text)



async def _send_via_arkentiy(chat_id: str, text: str, disable_notification: bool = False) -> bool:
    """Отправить сообщение через Аркентий (analytics bot)."""
    token = settings.telegram_analytics_bot_token
    if not token:
        return await send_message(chat_id, text, disable_notification=disable_notification)
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_notification": disable_notification,
                    },
                )
                data = response.json()
                if data.get("ok"):
                    return True
                if response.status_code == 429:
                    retry_after = data.get("parameters", {}).get("retry_after", 5)
                    await asyncio.sleep(retry_after)
                    continue
                logger.error(f"Аркентий мониторинг ошибка: {data}")
                return False
        except Exception as e:
            logger.error(f"Аркентий мониторинг неожиданная ошибка: {e}")
            return False
    return False


async def monitor(text: str) -> bool:
    """Технические сообщения в личку Артемию — через Аркентий."""
    return await _send_via_arkentiy(
        str(settings.telegram_chat_monitoring),
        text,
        disable_notification=True,
    )


async def error_alert(job_name: str, error: str) -> bool:
    """Стандартное сообщение об ошибке задачи в мониторинг."""
    text = (
        f"🔴 <b>Ошибка задачи:</b> {job_name}\n\n"
        f"<code>{error[:500]}</code>"
    )
    return await monitor(text)
