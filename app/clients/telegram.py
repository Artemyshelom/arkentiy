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


TG_MAX_LEN = 4096


def _split_text(text: str, max_len: int = TG_MAX_LEN) -> list[str]:
    """Разбивает длинный текст на части по границам строк."""
    if len(text) <= max_len:
        return [text]
    parts: list[str] = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, max_len)
        if cut <= 0:
            cut = max_len
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts


async def send_message(
    chat_id: str,
    text: str,
    parse_mode: str = "HTML",
    disable_notification: bool = False,
    retry: int = 3,
) -> bool:
    """
    Отправляет сообщение в чат. Возвращает True при успехе.
    Автоматически разбивает длинные сообщения на части.
    Retry с exponential backoff при ошибках.
    """
    parts = _split_text(text)
    for part in parts:
        ok = await _send_single(chat_id, part, parse_mode, disable_notification, retry)
        if not ok:
            return False
        if len(parts) > 1:
            await asyncio.sleep(0.3)
    return True


async def _send_single(
    chat_id: str,
    text: str,
    parse_mode: str = "HTML",
    disable_notification: bool = False,
    retry: int = 3,
) -> bool:
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


async def send_message_with_keyboard(
    chat_id: str,
    text: str,
    keyboard: list[list[dict]],
    parse_mode: str = "HTML",
) -> int | None:
    """Отправляет сообщение с InlineKeyboard. Возвращает message_id или None."""
    token = settings.telegram_analytics_bot_token or settings.telegram_bot_token
    if not token:
        await send_message(chat_id, text, parse_mode=parse_mode)
        return None
    payload: dict = {
        "chat_id": chat_id,
        "text": text[:4096],
        "parse_mode": parse_mode,
    }
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload,
            )
            data = resp.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            logger.error(f"send_message_with_keyboard error: {data}")
    except Exception as e:
        logger.error(f"send_message_with_keyboard exception: {e}")
    return None


async def edit_message_with_keyboard(
    chat_id: int,
    message_id: int,
    text: str,
    keyboard: list[list[dict]],
    parse_mode: str = "HTML",
) -> None:
    """Редактирует сообщение с InlineKeyboard."""
    token = settings.telegram_analytics_bot_token or settings.telegram_bot_token
    if not token:
        return
    payload: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:4096],
        "parse_mode": parse_mode,
    }
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/editMessageText",
                json=payload,
            )
            if not resp.json().get("ok"):
                logger.error(f"edit_message_with_keyboard error: {resp.json()}")
    except Exception as e:
        logger.error(f"edit_message_with_keyboard exception: {e}")
