"""
Единый менеджер аутентификации для iiko BO API.

Token (UUID) — для /api/* endpoints (events, olap v2, reports).
GET /api/auth?login=LOGIN&pass=SHA1HASH → UUID (36 символов), живёт ~1 час.

Используется в: iiko_bo_events, iiko_bo_olap_v2, cancel_sync, audit.
"""

import hashlib
import logging
import time

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_settings = get_settings()
_pwd_hash: str = hashlib.sha1(_settings.iiko_bo_password.encode()).hexdigest()

TOKEN_TTL = 3000  # ~50 мин (iiko токен живёт ~1 час, с запасом)

_token_cache: dict[str, tuple[str, float]] = {}


async def get_bo_token(bo_url: str, client: httpx.AsyncClient | None = None) -> str:
    """
    Возвращает кешированный API-токен iiko BO для указанного сервера.
    GET /api/auth?login=LOGIN&pass=SHA1HASH → UUID (36 символов).

    Если передан client — использует его, иначе создаёт одноразовый.
    """
    cached = _token_cache.get(bo_url)
    if cached and (time.time() - cached[1]) < TOKEN_TTL:
        return cached[0]

    url = f"{bo_url}/api/auth?login={_settings.iiko_bo_login}&pass={_pwd_hash}"

    if client:
        resp = await client.get(url, timeout=15)
        resp.raise_for_status()
        token = resp.text.strip()
    else:
        async with httpx.AsyncClient(verify=False, timeout=15) as c:
            resp = await c.get(url)
            resp.raise_for_status()
            token = resp.text.strip()

    _token_cache[bo_url] = (token, time.time())
    logger.debug(f"iiko BO token обновлён: {bo_url}")
    return token


def invalidate_token(bo_url: str) -> None:
    """Сбросить кеш токена (например, при 401)."""
    _token_cache.pop(bo_url, None)
