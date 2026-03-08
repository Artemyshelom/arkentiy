"""
Утилиты аутентификации: хэширование паролей, JWT, FastAPI dependency.

Вынесены из routers/cabinet.py для переиспользования в onboarding и других модулях.
"""

import hashlib

import bcrypt
import jwt
from fastapi import Header, HTTPException

from app.config import get_settings

JWT_ALGO = "HS256"


def _jwt_secret() -> str:
    return get_settings().jwt_secret


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    if len(hashed) == 64 and all(c in "0123456789abcdef" for c in hashed):
        return hashlib.sha256(plain.encode()).hexdigest() == hashed
    return bcrypt.checkpw(plain.encode(), hashed.encode())


async def get_tenant_id(authorization: str = Header(None)) -> int:
    """
    FastAPI dependency — извлекает tenant_id из JWT Bearer токена.
    Проверяет token_version против БД для инвалидации при смене пароля.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid token")
    token = authorization[7:]
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGO])
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")

    tenant_id = payload.get("tenant_id")
    if tenant_id is None:
        raise HTTPException(401, "Invalid token: tenant_id missing")

    # Проверяем token_version если он есть в токене
    token_version = payload.get("token_version")
    if token_version is not None:
        try:
            from app.database_pg import _pool
            if _pool:
                async with _pool.acquire() as conn:
                    db_version = await conn.fetchval(
                        "SELECT token_version FROM tenants WHERE id = $1", int(tenant_id)
                    )
                    if db_version is not None and token_version != db_version:
                        raise HTTPException(401, "Token revoked")
        except HTTPException:
            raise
        except Exception:
            pass  # Если БД недоступна — пропускаем проверку версии

    return int(tenant_id)
