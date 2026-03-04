"""
Утилиты аутентификации: хэширование паролей, JWT.

Вынесены из routers/cabinet.py для переиспользования в onboarding и других модулях.
"""

import hashlib

import bcrypt
import jwt

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
