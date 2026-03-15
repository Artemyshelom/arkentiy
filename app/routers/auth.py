"""
Auth роутер — публичные endpoints для email-верификации и восстановления пароля.
"""

import logging
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.clients.email import send_verification_email, send_reset_email
from app.services.auth import hash_password, verify_password, _jwt_secret, JWT_ALGO
from app.database_pg import get_pool_or_none
import jwt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["Auth"])
limiter = Limiter(key_func=get_remote_address)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


# =====================================================================
# GET /api/auth/verify-email?token=xxx
# =====================================================================

@router.get("/verify-email")
async def verify_email(token: str):
    """Подтверждение email по токену из письма."""
    pool = get_pool_or_none()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, email_token_expires, email_verified FROM tenants WHERE email_token = $1",
            token,
        )
        if not row:
            raise HTTPException(400, "Ссылка недействительна или уже использована")

        if row["email_verified"]:
            return {"status": "already_verified", "message": "Email уже подтверждён"}

        expires = row["email_token_expires"]
        if expires and datetime.utcnow() > expires.replace(tzinfo=None):
            raise HTTPException(400, "Ссылка истекла. Запросите повторную отправку")

        await conn.execute(
            """UPDATE tenants
               SET email_verified = true, email_token = NULL, email_token_expires = NULL,
                   updated_at = now()
               WHERE id = $1""",
            row["id"],
        )

    return {"status": "verified", "message": "Email подтверждён. Теперь вы можете войти"}


# =====================================================================
# POST /api/auth/resend-verification
# =====================================================================

@router.post("/resend-verification")
@limiter.limit("3/hour")
async def resend_verification(req: ForgotPasswordRequest, request: Request):
    """Повторная отправка письма подтверждения."""
    pool = get_pool_or_none()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, email_verified FROM tenants WHERE email = $1",
            req.email,
        )

    # Всегда отвечаем одинаково — не раскрываем факт существования email
    if not row or row["email_verified"]:
        return {"status": "ok", "message": "Если email зарегистрирован и не подтверждён — письмо отправлено"}

    new_token = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(hours=24)

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE tenants SET email_token = $1, email_token_expires = $2 WHERE id = $3",
            new_token, expires, row["id"],
        )

    await send_verification_email(req.email, new_token)
    return {"status": "ok", "message": "Если email зарегистрирован и не подтверждён — письмо отправлено"}


# =====================================================================
# POST /api/auth/forgot-password
# =====================================================================

@router.post("/forgot-password")
@limiter.limit("3/hour")
async def forgot_password(req: ForgotPasswordRequest, request: Request):
    """Запрос сброса пароля — отправляет письмо со ссылкой."""
    pool = get_pool_or_none()
    if not pool:
        raise HTTPException(500, "Database not available")

    # Всегда отвечаем одинаково — не раскрываем факт существования email
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM tenants WHERE email = $1 AND password_hash IS NOT NULL",
            req.email,
        )

    if row:
        reset_token = secrets.token_urlsafe(32)
        expires = datetime.utcnow() + timedelta(hours=1)

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE tenants SET reset_token = $1, reset_token_expires = $2 WHERE id = $3",
                reset_token, expires, row["id"],
            )

        await send_reset_email(req.email, reset_token)
        logger.info(f"Password reset requested for email={req.email}")

    return {
        "status": "ok",
        "message": "Если email зарегистрирован — письмо с инструкцией отправлено",
    }


# =====================================================================
# POST /api/auth/reset-password
# =====================================================================

@router.post("/reset-password")
@limiter.limit("5/hour")
async def reset_password(req: ResetPasswordRequest, request: Request):
    """Установка нового пароля по токену из письма."""
    if len(req.new_password) < 8:
        raise HTTPException(400, "Пароль должен содержать минимум 8 символов")

    pool = get_pool_or_none()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, reset_token_expires FROM tenants WHERE reset_token = $1",
            req.token,
        )
        if not row:
            raise HTTPException(400, "Ссылка недействительна или уже использована")

        expires = row["reset_token_expires"]
        if not expires or datetime.utcnow() > expires.replace(tzinfo=None):
            raise HTTPException(400, "Ссылка истекла. Запросите новую")

        await conn.execute(
            """UPDATE tenants
               SET password_hash = $1,
                   reset_token = NULL, reset_token_expires = NULL,
                   token_version = token_version + 1,
                   updated_at = now()
               WHERE id = $2""",
            hash_password(req.new_password), row["id"],
        )

        # Выдаём новый JWT сразу после сброса
        tenant = await conn.fetchrow(
            "SELECT id, email, name, token_version FROM tenants WHERE id = $1", row["id"],
        )

    token = jwt.encode(
        {
            "tenant_id": tenant["id"],
            "email": tenant["email"],
            "token_version": tenant["token_version"],
            "exp": datetime.utcnow() + timedelta(days=30),
        },
        _jwt_secret(),
        algorithm=JWT_ALGO,
    )

    logger.info(f"Password reset completed for tenant_id={tenant['id']}")
    return {"status": "ok", "message": "Пароль успешно изменён", "token": token}
