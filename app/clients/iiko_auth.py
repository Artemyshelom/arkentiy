"""
Менеджер аутентификации для iiko BO API — multi-tenant.

Token (UUID) — для /api/* endpoints (events, olap v2, reports).
GET /api/auth?login=LOGIN&pass=SHA1HASH → UUID (36 символов), живёт ~1 час.

Логин/пароль берётся из iiko_credentials каждой точки (bo_login, bo_password).
Кеш по (bo_url, bo_login) — разные тенанты на одном сервере не конфликтуют.

Используется в: iiko_bo_events, iiko_bo_olap_v2, cancel_sync, audit.
"""

import hashlib
import logging
import time

import httpx

logger = logging.getLogger(__name__)

TOKEN_TTL = 3000  # ~50 мин (iiko токен живёт ~1 час, с запасом)

# Ключ: (bo_url, bo_login) → (token, timestamp)
_token_cache: dict[tuple[str, str], tuple[str, float]] = {}


def _cache_key(bo_url: str, bo_login: str) -> tuple[str, str]:
    return (bo_url.rstrip("/"), bo_login)


async def get_bo_token(
    bo_url: str,
    client: httpx.AsyncClient | None = None,
    bo_login: str | None = None,
    bo_password: str | None = None,
) -> str:
    """
    Возвращает кешированный API-токен iiko BO.

    Приоритет логина/пароля:
      1. bo_login/bo_password из аргументов (из iiko_credentials конкретной точки)
      2. Глобальный логин из env (IIKO_BO_LOGIN / IIKO_BO_PASSWORD) — fallback

    Кеш по (bo_url, bo_login) — несколько тенантов с разными логинами не конфликтуют.
    """
    # Fallback на глобальный логин из env
    if not bo_login or not bo_password:
        from app.config import get_settings
        _settings = get_settings()
        bo_login = bo_login or _settings.iiko_bo_login
        bo_password = bo_password or _settings.iiko_bo_password

    key = _cache_key(bo_url, bo_login)
    cached = _token_cache.get(key)
    if cached and (time.time() - cached[1]) < TOKEN_TTL:
        return cached[0]

    pwd_hash = hashlib.sha1(bo_password.encode()).hexdigest()
    url = f"{bo_url.rstrip('/')}/api/auth?login={bo_login}&pass={pwd_hash}"

    if client:
        resp = await client.get(url, timeout=15)
        resp.raise_for_status()
        token = resp.text.strip()
    else:
        async with httpx.AsyncClient(verify=False, timeout=15) as c:
            resp = await c.get(url, timeout=15)
            resp.raise_for_status()
            token = resp.text.strip()

    _token_cache[key] = (token, time.time())
    logger.debug(f"iiko BO token обновлён: {bo_url} (login={bo_login})")
    return token


def invalidate_token(bo_url: str, bo_login: str | None = None) -> None:
    """Сбросить кеш токена (например, при 401)."""
    if bo_login:
        _token_cache.pop(_cache_key(bo_url, bo_login), None)
    else:
        # Удалить все записи для этого bo_url (обратная совместимость)
        keys_to_del = [k for k in _token_cache if k[0] == bo_url.rstrip("/")]
        for k in keys_to_del:
            _token_cache.pop(k, None)
