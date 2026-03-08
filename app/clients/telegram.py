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
    """Разбивает длинный текст на части ≤ max_len по границам строк."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    current = ""

    for line in text.split("\n"):
        # Одна строка длиннее лимита — режем по символам
        if len(line) > max_len:
            if current:
                chunks.append(current.rstrip())
                current = ""
            for i in range(0, len(line), max_len - 10):
                chunks.append(line[i:i + max_len - 10])
            continue

        candidate = current + line + "\n"
        if len(candidate) > max_len:
            chunks.append(current.rstrip())
            current = line + "\n"
        else:
            current = candidate

    if current.strip():
        chunks.append(current.rstrip())

    return chunks


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
    """Отправить сообщение через Аркентий (analytics bot). Автоматически разбивает > 4096."""
    token = settings.telegram_analytics_bot_token
    if not token:
        return await send_message(chat_id, text, disable_notification=disable_notification)

    chunks = _split_text(text)
    for i, chunk in enumerate(chunks):
        success = False
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                    response = await client.post(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": chunk,
                            "parse_mode": "HTML",
                            "disable_notification": disable_notification,
                        },
                    )
                    data = response.json()
                    if data.get("ok"):
                        success = True
                        break
                    if response.status_code == 429:
                        retry_after = data.get("parameters", {}).get("retry_after", 5)
                        await asyncio.sleep(retry_after)
                        continue
                    logger.error(f"Аркентий мониторинг ошибка: {data}")
                    return False
            except Exception as e:
                logger.error(f"Аркентий мониторинг неожиданная ошибка: {e}")
                return False
        if not success:
            return False
        if i < len(chunks) - 1:
            await asyncio.sleep(0.3)
    return True


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
    """Отправляет сообщение с InlineKeyboard. Возвращает message_id или None.
    Если текст длиннее 4096 — промежуточные части отправляются без кнопок,
    кнопки прикрепляются только к последнему чанку."""
    token = settings.telegram_analytics_bot_token or settings.telegram_bot_token
    if not token:
        await send_message(chat_id, text, parse_mode=parse_mode)
        return None

    chunks = _split_text(text)
    last_message_id: int | None = None

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            for i, chunk in enumerate(chunks):
                is_last = i == len(chunks) - 1
                payload: dict = {
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": parse_mode,
                }
                if is_last and keyboard:
                    payload["reply_markup"] = {"inline_keyboard": keyboard}
                resp = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json=payload,
                )
                data = resp.json()
                if data.get("ok"):
                    last_message_id = data["result"]["message_id"]
                else:
                    logger.error(f"send_message_with_keyboard error: {data}")
                    return last_message_id
                if not is_last:
                    await asyncio.sleep(0.3)
    except Exception as e:
        logger.error(f"send_message_with_keyboard exception: {e}")

    return last_message_id


async def edit_message_with_keyboard(
    chat_id: int,
    message_id: int,
    text: str,
    keyboard: list[list[dict]],
    parse_mode: str = "HTML",
) -> None:
    """Редактирует сообщение с InlineKeyboard.
    Edit не поддерживает разбиение — текст усекается до 4096 символов с маркером."""
    token = settings.telegram_analytics_bot_token or settings.telegram_bot_token
    if not token:
        return
    if len(text) > TG_MAX_LEN:
        text = text[:TG_MAX_LEN - 5] + "\n…"
    payload: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
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
