"""
OpenClaw AI API клиент.

OpenAI-compatible API (chat/completions).
URL: OPENCLAW_API_URL из .env

Инфраструктура: OpenClaw на отдельном сервере 72.56.107.85:18789
                (не тот же хост что Аркентий — обычный HTTP по внешнему IP)
"""

import logging
import time
from typing import Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class OpenClawError(Exception):
    """Базовая ошибка OpenClaw."""


class OpenClawAuthError(OpenClawError):
    """401 — неверный токен."""


class OpenClawRateLimitError(OpenClawError):
    """429 — rate limit на стороне OpenClaw."""


class OpenClawTimeoutError(OpenClawError):
    """Запрос превысил таймаут."""


class OpenClawServerError(OpenClawError):
    """5xx — сервер упал или недоступен."""


async def call_openclaw(
    user_text: str,
    system_prompt: Optional[str] = None,
) -> tuple[str, int]:
    """
    Отправляет запрос в OpenClaw API.

    Возвращает (response_text, elapsed_ms).
    Raises: OpenClawError и подклассы при ошибках.
    """
    settings = get_settings()
    url = settings.openclaw_api_url
    token = settings.openclaw_api_token
    model = settings.openclaw_model
    timeout = settings.openclaw_timeout

    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_text})

    payload = {"model": model, "messages": messages}
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    t0 = time.monotonic()
    logger.debug("[openclaw] → %d chars, model=%s, url=%s", len(user_text), model, url)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        logger.warning("[openclaw] timeout after %dms", elapsed)
        raise OpenClawTimeoutError(f"Timeout after {elapsed}ms") from e
    except Exception as e:
        elapsed = int((time.monotonic() - t0) * 1000)
        logger.error("[openclaw] request error after %dms: %s", elapsed, e)
        raise OpenClawError(f"Request error: {e}") from e

    elapsed = int((time.monotonic() - t0) * 1000)

    if r.status_code == 401:
        logger.error("[openclaw] 401 invalid token")
        raise OpenClawAuthError("Invalid token")

    if r.status_code == 429:
        logger.warning("[openclaw] 429 rate limited")
        raise OpenClawRateLimitError("Rate limited")

    if r.status_code >= 500:
        logger.error("[openclaw] %d server error: %s", r.status_code, r.text[:200])
        raise OpenClawServerError(f"Server error {r.status_code}")

    if r.status_code != 200:
        logger.error("[openclaw] unexpected %d: %s", r.status_code, r.text[:200])
        raise OpenClawError(f"Unexpected status {r.status_code}")

    try:
        data = r.json()
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as e:
        logger.error("[openclaw] bad response format: %s", r.text[:300])
        raise OpenClawError(f"Bad response format: {e}") from e

    logger.info("[openclaw] ✓ %dms, %d chars in response", elapsed, len(text))
    return text, elapsed
