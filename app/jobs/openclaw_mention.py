"""
openclaw_mention.py — обработчик @ mention бота через OpenClaw AI.

Вызывается из arkentiy.py когда бот упомянут в групповом чате.
Изолированный модуль: отключается одной строкой в arkentiy.py.

Ответственность:
  - rate limiting (in-memory, per user)
  - очистка текста от @mention
  - сборка system prompt с контекстом пользователя
  - вызов OpenClaw API
  - конвертация ошибок в человекочитаемые сообщения
"""

import logging
import re
import time
from typing import Optional

from app.clients.openclaw import (
    OpenClawAuthError,
    OpenClawError,
    OpenClawRateLimitError,
    OpenClawServerError,
    OpenClawTimeoutError,
    call_openclaw,
)

logger = logging.getLogger(__name__)

# Rate limiter: user_id → список monotonic timestamps за последние WINDOW секунд
_rate_limit: dict[int, list[float]] = {}

RATE_LIMIT_COUNT = 3      # макс запросов
RATE_LIMIT_WINDOW = 60.0  # за N секунд


def check_rate_limit(user_id: int) -> bool:
    """Возвращает True если запрос разрешён, False если лимит превышен."""
    now = time.monotonic()
    window_start = now - RATE_LIMIT_WINDOW
    timestamps = [t for t in _rate_limit.get(user_id, []) if t > window_start]
    if len(timestamps) >= RATE_LIMIT_COUNT:
        _rate_limit[user_id] = timestamps
        return False
    timestamps.append(now)
    _rate_limit[user_id] = timestamps
    return True


def clean_mention(text: str) -> str:
    """Удаляет все @username из текста и возвращает чистый запрос."""
    return re.sub(r"@\w+", "", text).strip()


def build_system_prompt(
    username: str,
    city: Optional[frozenset],
    is_admin: bool,
) -> str:
    """Строит system prompt с контекстом пользователя."""
    city_str = ", ".join(sorted(city)) if city else "все города"
    role_str = "администратор" if is_admin else "менеджер"
    return (
        f"Ты — Аркентий, AI-ассистент команды доставки Ёбидоёби. "
        f"Пользователь: @{username}, роль: {role_str}, город(а): {city_str}. "
        f"Отвечай кратко и по делу, на русском языке."
    )


async def handle_mention(
    text: str,
    user_id: int,
    username: str,
    city: Optional[frozenset],
    is_admin: bool,
) -> str:
    """
    Основная точка входа модуля.

    Принимает сырой текст сообщения (с @mention).
    Возвращает строку ответа для отправки reply-ем в чат.
    Никогда не бросает исключения — все ошибки конвертирует в сообщения.
    """
    if not check_rate_limit(user_id):
        logger.info("[mention] rate limit hit user_id=%d", user_id)
        return "⏳ Подожди немного — слишком много запросов подряд."

    clean = clean_mention(text)
    if not clean:
        return ""

    system_prompt = build_system_prompt(username, city, is_admin)

    try:
        response_text, elapsed_ms = await call_openclaw(clean, system_prompt)
        logger.info(
            "[mention] ok user=%d elapsed=%dms response=%d chars",
            user_id, elapsed_ms, len(response_text),
        )
        return response_text

    except OpenClawTimeoutError:
        logger.warning("[mention] timeout user=%d", user_id)
        return "⚠️ Мозги думают слишком долго, попробуй ещё раз."

    except OpenClawRateLimitError:
        logger.warning("[mention] openclaw rate limit user=%d", user_id)
        return "⚠️ Слишком много запросов к AI, подожди немного."

    except OpenClawAuthError:
        logger.error("[mention] auth error — check OPENCLAW_API_TOKEN")
        return "⚠️ Ошибка авторизации AI. Напиши администратору."

    except OpenClawServerError:
        logger.error("[mention] server error user=%d", user_id)
        return "⚠️ Мозги временно недоступны."

    except OpenClawError as e:
        logger.error("[mention] openclaw error user=%d: %s", user_id, e)
        return "⚠️ Что-то пошло не так. Попробуй позже."

    except Exception as e:
        logger.error("[mention] unexpected error user=%d: %s", user_id, e, exc_info=True)
        return "⚠️ Что-то пошло не так. Попробуй позже."
