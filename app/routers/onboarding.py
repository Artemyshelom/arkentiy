"""
Онбординг API — публичные эндпоинты для визарда регистрации.
Все без JWT (создают аккаунт в процессе).
"""

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import get_settings
from app.services.auth import hash_password, JWT_ALGO

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Onboarding"])
limiter = Limiter(key_func=get_remote_address)


# =====================================================================
# Pydantic models
# =====================================================================

class CheckEmailRequest(BaseModel):
    email: EmailStr


class IikoTestRequest(BaseModel):
    bo_url: str
    api_login: str


class TelegramTestRequest(BaseModel):
    chat_id: str


class PromoValidateRequest(BaseModel):
    code: str


class CityBranches(BaseModel):
    name: str
    branches: list[str]


class IikoData(BaseModel):
    bo_url: str | None = None
    api_login: str | None = None


class TenantCreateRequest(BaseModel):
    company_name: str
    contact_name: str
    email: EmailStr
    password: str
    phone: str | None = None
    cities: list[CityBranches]
    modules: list[str]
    iiko: IikoData | None = None
    telegram_chat_id: str | None = None
    period: str = "monthly"
    promo_code: str | None = None
    trial: bool = False


# =====================================================================
# Helpers
# =====================================================================

async def _get_pool():
    try:
        from app.database_pg import _pool
        return _pool
    except Exception:
        return None


def _calculate_pricing(branches_count: int, cities_count: int, modules: list[str], period: str) -> dict:
    """Расчёт стоимости подписки."""
    base = 5000 * branches_count
    finance = 2000 * branches_count if "finance" in modules else 0
    competitors = 1000 * cities_count if "competitors" in modules else 0
    competitors_setup = 3000 * cities_count if "competitors" in modules else 0

    subtotal = base + finance + competitors

    # Скидка за объём
    if branches_count >= 7:
        volume_discount_pct = 15
    elif branches_count >= 4:
        volume_discount_pct = 10
    else:
        volume_discount_pct = 0

    monthly = int(subtotal * (1 - volume_discount_pct / 100))

    # Годовая скидка
    annual_discount_pct = 20 if period == "annual" else 0
    monthly_final = int(monthly * (1 - annual_discount_pct / 100))

    connection_fee = 10000

    return {
        "monthly_price": monthly_final,
        "connection_fee": connection_fee,
        "competitors_setup_fee": competitors_setup,
        "first_payment": monthly_final + connection_fee + competitors_setup,
        "volume_discount_pct": volume_discount_pct,
        "annual_discount_pct": annual_discount_pct,
    }


# =====================================================================
# 1. POST /api/auth/check-email
# =====================================================================

@router.post("/auth/check-email")
@limiter.limit("10/minute")
async def check_email(req: CheckEmailRequest, request: Request):
    """Проверка уникальности email при регистрации."""
    pool = await _get_pool()
    if pool:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM tenants WHERE email = $1",
                req.email,
            )
            if row:
                return {"available": False, "message": "Email уже зарегистрирован"}
    return {"available": True}


# =====================================================================
# 2. POST /api/iiko/test-connection
# =====================================================================

@router.post("/iiko/test-connection")
@limiter.limit("5/minute")
async def test_iiko_connection(req: IikoTestRequest, request: Request):
    """Тест подключения к iiko BO (публичный, без JWT)."""
    bo_url = req.bo_url.rstrip("/")

    # SHA1 хеш пароля для API — iiko использует логин как пароль при первом подключении
    # Но для теста нам нужен только логин — iiko BO auth: GET /api/auth?login=LOGIN&pass=SHA1(password)
    # В визарде клиент указывает только логин. Пароль iiko = пустая строка для теста.
    pwd_hash = hashlib.sha1(b"").hexdigest()

    try:
        async with httpx.AsyncClient(verify=False, timeout=15) as client:
            resp = await client.get(
                f"{bo_url}/api/auth?login={req.api_login}&pass={pwd_hash}",
                timeout=15,
            )

            if resp.status_code == 200 and len(resp.text.strip()) == 36:
                # Токен получен — пробуем получить список точек
                token = resp.text.strip()
                try:
                    orgs_resp = await client.get(
                        f"{bo_url}/api/organization/list?access_token={token}",
                        timeout=10,
                    )
                    if orgs_resp.status_code == 200:
                        orgs = orgs_resp.json()
                        branches = [o.get("name", "") for o in orgs if isinstance(o, dict)]
                        return {
                            "success": True,
                            "branches_found": len(branches),
                            "branches": branches[:20],
                        }
                except Exception:
                    pass

                return {"success": True, "branches_found": 0, "branches": []}

            if resp.status_code == 401 or resp.status_code == 403:
                return {
                    "success": False,
                    "error_code": "auth_failed",
                    "message": "Неверный логин. Создайте отдельного API-пользователя в iiko",
                }

            return {
                "success": False,
                "error_code": "connection_refused",
                "message": "Не удаётся подключиться. Проверьте URL и доступность сервера",
            }

    except httpx.TimeoutException:
        return {
            "success": False,
            "error_code": "timeout",
            "message": "Сервер не отвечает. Попробуйте позже",
        }
    except httpx.ConnectError:
        return {
            "success": False,
            "error_code": "connection_refused",
            "message": "Не удаётся подключиться. Проверьте URL и доступность сервера",
        }
    except Exception as e:
        logger.error(f"iiko test-connection error: {e}")
        return {
            "success": False,
            "error_code": "unknown",
            "message": "Произошла ошибка при проверке подключения",
        }


# =====================================================================
# 3. POST /api/telegram/test-chat
# =====================================================================

@router.post("/telegram/test-chat")
@limiter.limit("5/minute")
async def test_telegram_chat(req: TelegramTestRequest, request: Request):
    """Проверить что бот добавлен в чат и отправить тестовое сообщение."""
    settings = get_settings()
    bot_token = settings.telegram_analytics_bot_token or settings.telegram_bot_token

    if not bot_token:
        raise HTTPException(500, "Telegram бот не настроен")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Проверяем чат через getChat
            resp = await client.post(
                f"https://api.telegram.org/bot{bot_token}/getChat",
                json={"chat_id": req.chat_id},
            )
            data = resp.json()

            if not data.get("ok"):
                return {
                    "success": False,
                    "message": "Бот не найден в этом чате. Добавьте @arkentiy_bot в чат",
                }

            chat_title = data["result"].get("title", data["result"].get("first_name", "Чат"))

            # Отправляем тестовое сообщение
            await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": req.chat_id,
                    "text": "✅ <b>Аркентий подключён!</b>\nТестовое сообщение — чат работает.",
                    "parse_mode": "HTML",
                },
            )

            return {
                "success": True,
                "chat_title": chat_title,
                "message": "Чат подключён! Отправили тестовое сообщение",
            }

    except Exception as e:
        logger.error(f"telegram test-chat error: {e}")
        return {
            "success": False,
            "message": "Ошибка при проверке чата. Попробуйте позже",
        }


# =====================================================================
# 4. POST /api/promo/validate
# =====================================================================

@router.post("/promo/validate")
@limiter.limit("10/minute")
async def validate_promo(req: PromoValidateRequest, request: Request):
    """Проверка промокода."""
    pool = await _get_pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, code, bonuses_json, usage_limit, used_count, valid_until
               FROM promo_codes
               WHERE code = $1 AND is_active = true""",
            req.code.upper().strip(),
        )

    if not row:
        return {"valid": False, "message": "Промокод недействителен или истёк"}

    # Проверка срока действия
    if row["valid_until"] and datetime.utcnow() > row["valid_until"].replace(tzinfo=None):
        return {"valid": False, "message": "Промокод недействителен или истёк"}

    # Проверка лимита использования
    if row["usage_limit"] is not None and row["used_count"] >= row["usage_limit"]:
        return {"valid": False, "message": "Промокод недействителен или истёк"}

    bonuses = row["bonuses_json"] if isinstance(row["bonuses_json"], list) else json.loads(row["bonuses_json"] or "[]")

    return {
        "valid": True,
        "code": row["code"],
        "bonuses": bonuses,
    }


# =====================================================================
# 5. POST /api/tenants/create
# =====================================================================

@router.post("/tenants/create")
@limiter.limit("3/minute")
async def create_tenant(req: TenantCreateRequest, request: Request):
    """Создание тенанта + аккаунта при регистрации."""
    pool = await _get_pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    # Валидация
    if len(req.password) < 8:
        raise HTTPException(400, "Пароль должен быть не менее 8 символов")
    if not req.cities or not any(c.branches for c in req.cities):
        raise HTTPException(400, "Выберите хотя бы одну точку")

    async with pool.acquire() as conn:
        # Проверка уникальности email
        existing = await conn.fetchrow("SELECT id FROM tenants WHERE email = $1", req.email)
        if existing:
            raise HTTPException(409, "Email уже зарегистрирован")

        # Транзакция: создаём всё атомарно
        async with conn.transaction():
            slug = req.company_name.lower().replace(" ", "-")[:50]
            # Убеждаемся в уникальности slug
            slug_check = await conn.fetchval("SELECT id FROM tenants WHERE slug = $1", slug)
            if slug_check:
                slug = f"{slug}-{uuid.uuid4().hex[:6]}"

            branches_count = sum(len(c.branches) for c in req.cities)
            cities_count = len(req.cities)

            # Расчёт стоимости
            pricing = _calculate_pricing(branches_count, cities_count, req.modules, req.period)

            # Применение промокода
            promo_applied = False
            promo_id = None
            if req.promo_code:
                promo_row = await conn.fetchrow(
                    """SELECT id, bonuses_json, usage_limit, used_count, valid_until
                       FROM promo_codes
                       WHERE code = $1 AND is_active = true""",
                    req.promo_code.upper().strip(),
                )
                if promo_row:
                    valid = True
                    if promo_row["valid_until"] and datetime.utcnow() > promo_row["valid_until"].replace(tzinfo=None):
                        valid = False
                    if promo_row["usage_limit"] is not None and promo_row["used_count"] >= promo_row["usage_limit"]:
                        valid = False

                    if valid:
                        promo_id = promo_row["id"]
                        promo_applied = True
                        bonuses = promo_row["bonuses_json"] if isinstance(promo_row["bonuses_json"], list) else json.loads(promo_row["bonuses_json"] or "[]")
                        for bonus in bonuses:
                            if bonus.get("type") == "free_connection":
                                pricing["connection_fee"] = 0
                            elif bonus.get("type") == "fixed_discount":
                                pricing["monthly_price"] = max(0, pricing["monthly_price"] - bonus.get("amount", 0))

                        pricing["first_payment"] = pricing["monthly_price"] + pricing["connection_fee"] + pricing["competitors_setup_fee"]

            # Статус тенанта
            now = datetime.utcnow()
            if req.trial:
                status = "trial"
                trial_ends = now + timedelta(days=7)
            else:
                status = "pending_payment"
                trial_ends = None

            # 1. Создаём tenant
            tenant_id = await conn.fetchval(
                """INSERT INTO tenants (name, slug, email, contact, phone, password_hash, plan, status, trial_ends_at, created_at, updated_at)
                   VALUES ($1, $2, $3, $4, $5, $6, 'base', $7, $8, now(), now())
                   RETURNING id""",
                req.company_name, slug, req.email, req.contact_name, req.phone,
                hash_password(req.password), status, trial_ends,
            )

            # 2. Создаём subscription
            await conn.execute(
                """INSERT INTO subscriptions
                   (tenant_id, status, plan, modules_json, branches_count, amount_monthly, period,
                    connection_fee_paid, started_at, next_billing_at, created_at, updated_at)
                   VALUES ($1, $2, 'base', $3, $4, $5, $6, $7, now(),
                           CASE WHEN $2 = 'trial' THEN now() + interval '7 days' ELSE NULL END,
                           now(), now())""",
                tenant_id, "trial" if req.trial else "pending",
                json.dumps(req.modules), branches_count, pricing["monthly_price"],
                req.period, pricing["connection_fee"] == 0,
            )

            # 3. Включаем модули
            for module in req.modules:
                await conn.execute(
                    """INSERT INTO tenant_modules (tenant_id, module, enabled, updated_at)
                       VALUES ($1, $2, true, now())
                       ON CONFLICT (tenant_id, module) DO UPDATE SET enabled = true, updated_at = now()""",
                    tenant_id, module,
                )

            # 4. Сохраняем iiko credentials (если заполнены)
            if req.iiko and req.iiko.bo_url:
                for city_data in req.cities:
                    for branch_name in city_data.branches:
                        await conn.execute(
                            """INSERT INTO iiko_credentials
                               (tenant_id, branch_name, city, bo_url, bo_login, is_active, created_at)
                               VALUES ($1, $2, $3, $4, $5, true, now())
                               ON CONFLICT (tenant_id, branch_name) DO NOTHING""",
                            tenant_id, branch_name, city_data.name,
                            req.iiko.bo_url, req.iiko.api_login,
                        )

            # 5. Сохраняем Telegram чат (если заполнен)
            if req.telegram_chat_id:
                default_modules = ["late_alerts", "reports"]
                await conn.execute(
                    """INSERT INTO tenant_chats (tenant_id, chat_id, name, modules_json, is_active)
                       VALUES ($1, $2, $3, $4::jsonb, true)
                       ON CONFLICT (tenant_id, chat_id) DO NOTHING""",
                    tenant_id, int(req.telegram_chat_id),
                    f"Рабочий чат {req.company_name}",
                    json.dumps(default_modules),
                )

            # 6. Фиксируем промокод
            if promo_id:
                await conn.execute(
                    """INSERT INTO promo_usage (promo_id, tenant_id) VALUES ($1, $2)
                       ON CONFLICT (promo_id, tenant_id) DO NOTHING""",
                    promo_id, tenant_id,
                )
                await conn.execute(
                    "UPDATE promo_codes SET used_count = used_count + 1 WHERE id = $1",
                    promo_id,
                )

            # 7. Записываем событие
            await conn.execute(
                """INSERT INTO tenant_events (tenant_id, event_type, text, icon)
                   VALUES ($1, 'system', $2, 'success')""",
                tenant_id,
                f"Аккаунт создан {'(триал 7 дней)' if req.trial else '(ожидает оплаты)'}",
            )

    # Выдаём JWT
    settings = get_settings()
    token = jwt.encode(
        {
            "tenant_id": tenant_id,
            "email": req.email,
            "exp": datetime.utcnow() + timedelta(days=30),
        },
        settings.jwt_secret,
        algorithm=JWT_ALGO,
    )

    # Уведомляем Артемия
    first_payment_fmt = f"{pricing['first_payment']:,} ₽"
    try:
        from app.clients.telegram import monitor
        await monitor(
            f"🆕 <b>Новый клиент!</b>\n"
            f"Компания: {req.company_name}\n"
            f"Email: {req.email}\n"
            f"Точки: {branches_count}, городов: {cities_count}\n"
            f"Модули: {', '.join(req.modules)}\n"
            f"{'🎁 Триал 7 дней' if req.trial else f'💳 К оплате: {first_payment_fmt}'}\n"
            f"{'🏷 Промокод: ' + req.promo_code if promo_applied else ''}"
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить уведомление о новом клиенте: {e}")

    return {
        "tenant_id": tenant_id,
        "token": token,
        "subscription": {
            "monthly_price": pricing["monthly_price"],
            "connection_fee": pricing["connection_fee"],
            "first_payment": pricing["first_payment"],
            "promo_applied": promo_applied,
            "trial": req.trial,
        },
    }
