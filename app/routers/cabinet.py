"""
Cabinet API — личный кабинет клиента.

Все эндпоинты требуют JWT (кроме login).
tenant_id берётся из JWT payload, данные изолированы по тенанту.
"""

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Depends, Header, Request
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import get_settings
from app.services.auth import JWT_ALGO, _jwt_secret, hash_password, verify_password, get_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cabinet", tags=["Cabinet"])
limiter = Limiter(key_func=get_remote_address)


# =====================================================================
# Pydantic models
# =====================================================================

class LoginRequest(BaseModel):
    email: str
    password: str


class IikoUpdate(BaseModel):
    url: str
    login: str


class ChatCreate(BaseModel):
    chat_id: str
    name: str


class ChatUpdate(BaseModel):
    name: str
    cities: list[str] = []
    modules: list[str] = []


class ChatVerify(BaseModel):
    chat_id: str


class SettingsUpdate(BaseModel):
    name: Optional[str] = None
    contact: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None


class PasswordUpdate(BaseModel):
    old_password: str
    new_password: str


class LegalUpdate(BaseModel):
    inn: str = ""
    legal_name: str = ""


class SubscriptionUpdate(BaseModel):
    addons: list[str] = []
    cities: list[dict] = []
    period: str = "monthly"


class AccountDeleteRequest(BaseModel):
    password: str


# =====================================================================
# DB pool helper
# =====================================================================

async def _pool():
    try:
        from app.database_pg import _pool as p
        return p
    except Exception:
        return None


# =====================================================================
# 1. POST /api/cabinet/auth/login
# =====================================================================

@router.post("/auth/login")
@limiter.limit("5/15minutes")
async def login(req: LoginRequest, request: Request):
    pool = await _pool()
    if pool:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, name, password_hash, status, email_verified, token_version FROM tenants WHERE email = $1",
                req.email,
            )
            if row and row["password_hash"]:
                if row["status"] not in ("active", "trial", "pending_payment"):
                    raise HTTPException(403, "Аккаунт неактивен")
                # email_verified может быть NULL для старых записей — пропускаем
                if row["email_verified"] is False:
                    raise HTTPException(403, detail={
                        "error": "email_not_verified",
                        "message": "Подтвердите email для входа. Проверьте почту.",
                    })
                if verify_password(req.password, row["password_hash"]):
                    token = jwt.encode(
                        {
                            "tenant_id": row["id"],
                            "email": req.email,
                            "token_version": row["token_version"] or 1,
                            "exp": datetime.utcnow() + timedelta(days=30),
                        },
                        _jwt_secret(), algorithm=JWT_ALGO,
                    )
                    return {"token": token, "tenant": row["name"]}
            # Логируем неудачную попытку
            logger.warning(f"Failed login attempt for email={req.email} ip={request.client.host}")

    raise HTTPException(401, "Неверный email или пароль")


# =====================================================================
# 2. GET /api/cabinet/overview
# =====================================================================

@router.get("/overview")
async def get_overview(tenant_id: int = Depends(get_tenant_id)):
    pool = await _pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        tenant = await conn.fetchrow(
            "SELECT name, contact, email FROM tenants WHERE id = $1", tenant_id,
        )
        if not tenant:
            raise HTTPException(404, "Тенант не найден")

        sub = await conn.fetchrow(
            """SELECT status, plan, modules_json, branches_count,
                      amount_monthly, next_billing_at, period
               FROM subscriptions WHERE tenant_id = $1""",
            tenant_id,
        )

        trial_row = await conn.fetchval(
            "SELECT trial_ends_at FROM tenants WHERE id = $1", tenant_id,
        )

        cities_rows = await conn.fetch(
            "SELECT DISTINCT city FROM iiko_credentials WHERE tenant_id = $1 AND is_active = true",
            tenant_id,
        )

        iiko_cred = await conn.fetchrow(
            "SELECT bo_url FROM iiko_credentials WHERE tenant_id = $1 AND is_active = true LIMIT 1",
            tenant_id,
        )

        tg_count = await conn.fetchval(
            "SELECT COUNT(*) FROM tenant_chats WHERE tenant_id = $1 AND is_active = true",
            tenant_id,
        )

        events = await conn.fetch(
            """SELECT event_type, text, icon, created_at
               FROM tenant_events WHERE tenant_id = $1
               ORDER BY created_at DESC LIMIT 10""",
            tenant_id,
        )

    modules = []
    if sub and sub["modules_json"]:
        try:
            modules = json.loads(sub["modules_json"]) if isinstance(sub["modules_json"], str) else sub["modules_json"]
        except (json.JSONDecodeError, TypeError):
            pass

    addons = [m for m in modules if m != "base"]

    return {
        "tenant": dict(tenant),
        "subscription": {
            "status": sub["status"] if sub else "none",
            "plan": sub["plan"] if sub else "base",
            "addons": addons,
            "branches_count": sub["branches_count"] if sub else 0,
            "cities_count": len(cities_rows),
            "trial_ends_at": trial_row.isoformat() if trial_row else None,
            "next_payment_date": sub["next_billing_at"].strftime("%Y-%m-%d") if sub and sub["next_billing_at"] else None,
            "next_payment_amount": sub["amount_monthly"] if sub else None,
        },
        "connections": {
            "iiko": {"status": "ok" if iiko_cred else "not_configured"},
            "telegram": {"status": "ok" if tg_count > 0 else "not_configured"},
        },
        "recent_events": [
            {
                "date": e["created_at"].isoformat(),
                "type": e["event_type"],
                "text": e["text"],
                "icon": e["icon"],
            }
            for e in events
        ],
    }


# =====================================================================
# 3. GET /api/cabinet/subscription
# =====================================================================

@router.get("/subscription")
async def get_subscription(tenant_id: int = Depends(get_tenant_id)):
    pool = await _pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        sub = await conn.fetchrow(
            """SELECT status, plan, modules_json, branches_count, amount_monthly,
                      period, next_billing_at, started_at,
                      cancel_scheduled, cancel_at
               FROM subscriptions WHERE tenant_id = $1""",
            tenant_id,
        )
        if not sub:
            raise HTTPException(404, "Подписка не найдена")

        trial_ends = await conn.fetchval(
            "SELECT trial_ends_at FROM tenants WHERE id = $1", tenant_id,
        )

        branches = await conn.fetch(
            """SELECT branch_name, city FROM iiko_credentials
               WHERE tenant_id = $1 AND is_active = true
               ORDER BY city, branch_name""",
            tenant_id,
        )

    modules = []
    if sub["modules_json"]:
        try:
            modules = json.loads(sub["modules_json"]) if isinstance(sub["modules_json"], str) else sub["modules_json"]
        except (json.JSONDecodeError, TypeError):
            pass

    addons = [m for m in modules if m != "base"]

    cities_map: dict[str, list[dict]] = {}
    for b in branches:
        city = b["city"] or "Другое"
        cities_map.setdefault(city, []).append({"name": b["branch_name"]})
    cities = [{"name": city, "branches": brs} for city, brs in cities_map.items()]

    branches_count = sub["branches_count"] or sum(len(c["branches"]) for c in cities)
    cities_count = len(cities)

    base_per_branch = 5000
    fin_per_branch = 2000
    comp_per_city = 1000

    base_cost = base_per_branch * branches_count
    fin_cost = fin_per_branch * branches_count if "finance" in modules else 0
    comp_cost = comp_per_city * cities_count if "competitors" in modules else 0
    subtotal = base_cost + fin_cost + comp_cost

    vol_pct = 15 if branches_count >= 7 else (10 if branches_count >= 4 else 0)
    annual_pct = 20 if sub["period"] == "annual" else 0

    after_vol = int(subtotal * (1 - vol_pct / 100))
    monthly_total = int(after_vol * (1 - annual_pct / 100))

    return {
        "status": sub["status"],
        "plan": sub["plan"],
        "addons": addons,
        "period": sub["period"] or "monthly",
        "cities": cities,
        "branches_count": branches_count,
        "cities_count": cities_count,
        "pricing": {
            "base_per_branch": base_per_branch,
            "finance_per_branch": fin_per_branch,
            "competitors_per_city": comp_per_city,
            "volume_discount_pct": vol_pct,
            "annual_discount_pct": annual_pct,
            "monthly_total": monthly_total,
            "next_payment": sub["amount_monthly"] or monthly_total,
            "next_payment_date": sub["next_billing_at"].strftime("%Y-%m-%d") if sub["next_billing_at"] else None,
        },
        "trial_ends_at": trial_ends.isoformat() if trial_ends else None,
        "created_at": sub["started_at"].isoformat() if sub["started_at"] else None,
        "cancel_scheduled": bool(sub["cancel_scheduled"]),
        "cancel_at": sub["cancel_at"].isoformat() if sub["cancel_at"] else None,
    }


# =====================================================================
# 4. PUT /api/cabinet/subscription
# =====================================================================

@router.put("/subscription")
async def update_subscription(req: SubscriptionUpdate, tenant_id: int = Depends(get_tenant_id)):
    pool = await _pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    modules = ["base"] + req.addons
    branches_count = sum(len(c.get("branches", [])) for c in req.cities)
    cities_count = len(req.cities)

    base = 5000 * branches_count
    fin = 2000 * branches_count if "finance" in modules else 0
    comp = 1000 * cities_count if "competitors" in modules else 0
    subtotal = base + fin + comp

    vol_pct = 15 if branches_count >= 7 else (10 if branches_count >= 4 else 0)
    annual_pct = 20 if req.period == "annual" else 0
    after_vol = int(subtotal * (1 - vol_pct / 100))
    new_monthly = int(after_vol * (1 - annual_pct / 100))

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """UPDATE subscriptions SET modules_json = $1, branches_count = $2,
                   amount_monthly = $3, period = $4, updated_at = now()
                   WHERE tenant_id = $5""",
                json.dumps(modules), branches_count, new_monthly, req.period, tenant_id,
            )

            await conn.execute(
                "UPDATE tenant_modules SET enabled = false, updated_at = now() WHERE tenant_id = $1",
                tenant_id,
            )
            for m in modules:
                await conn.execute(
                    """INSERT INTO tenant_modules (tenant_id, module, enabled, updated_at)
                       VALUES ($1, $2, true, now())
                       ON CONFLICT (tenant_id, module) DO UPDATE SET enabled = true, updated_at = now()""",
                    tenant_id, m,
                )

            await conn.execute(
                "UPDATE iiko_credentials SET is_active = false WHERE tenant_id = $1", tenant_id,
            )
            cred = await conn.fetchrow(
                "SELECT bo_url, bo_login FROM iiko_credentials WHERE tenant_id = $1 LIMIT 1",
                tenant_id,
            )
            bo_url = cred["bo_url"] if cred else ""
            bo_login = cred["bo_login"] if cred else ""

            for city_data in req.cities:
                city_name = city_data.get("name", "")
                for branch in city_data.get("branches", []):
                    branch_name = branch.get("name", "") if isinstance(branch, dict) else branch
                    if not branch_name:
                        continue
                    await conn.execute(
                        """INSERT INTO iiko_credentials (tenant_id, branch_name, city, bo_url, bo_login, is_active)
                           VALUES ($1, $2, $3, $4, $5, true)
                           ON CONFLICT (tenant_id, branch_name) DO UPDATE
                           SET city = $3, is_active = true, bo_url = COALESCE(NULLIF($4, ''), iiko_credentials.bo_url)""",
                        tenant_id, branch_name, city_name, bo_url, bo_login,
                    )

            await conn.execute(
                """INSERT INTO tenant_events (tenant_id, event_type, text, icon)
                   VALUES ($1, 'subscription', $2, 'info')""",
                tenant_id,
                f"Подписка обновлена: {branches_count} точек, {new_monthly:,} ₽/мес",
            )

    return {
        "new_monthly_total": new_monthly,
        "branches_count": branches_count,
        "cities_count": cities_count,
    }


# =====================================================================
# 5. GET /api/cabinet/connections
# =====================================================================

@router.get("/connections")
async def get_connections(tenant_id: int = Depends(get_tenant_id)):
    pool = await _pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        iiko_rows = await conn.fetch(
            """SELECT bo_url, bo_login, city, branch_name
               FROM iiko_credentials WHERE tenant_id = $1 AND is_active = true
               ORDER BY city, branch_name""",
            tenant_id,
        )

        chats = await conn.fetch(
            """SELECT chat_id, name, modules_json, city, is_active
               FROM tenant_chats WHERE tenant_id = $1 AND is_active = true
               ORDER BY name""",
            tenant_id,
        )

        cities_rows = await conn.fetch(
            "SELECT DISTINCT city FROM iiko_credentials WHERE tenant_id = $1 AND is_active = true",
            tenant_id,
        )

    iiko_data = {
        "status": "not_configured", "url": None, "login": None,
        "last_check": None, "response_time_ms": None, "checks_log": [],
    }
    if iiko_rows:
        first = iiko_rows[0]
        iiko_data["status"] = "ok"
        iiko_data["url"] = first["bo_url"]
        iiko_data["login"] = first["bo_login"]

    tg_chats = []
    for ch in chats:
        mods = ch["modules_json"]
        if isinstance(mods, str):
            try:
                mods = json.loads(mods)
            except (json.JSONDecodeError, TypeError):
                mods = []
        elif not isinstance(mods, list):
            mods = []
        tg_chats.append({
            "chat_id": str(ch["chat_id"]),
            "name": ch["name"],
            "cities": [ch["city"]] if ch["city"] else [],
            "modules": mods,
            "status": "ok",
        })

    cities = [r["city"] for r in cities_rows if r["city"]]

    return {
        "iiko": iiko_data,
        "telegram": {"bot_username": "arkentiy_bot", "chats": tg_chats},
        "cities": cities,
    }


# =====================================================================
# 6. PUT /api/cabinet/connections/iiko
# =====================================================================

@router.put("/connections/iiko")
async def update_iiko(data: IikoUpdate, tenant_id: int = Depends(get_tenant_id)):
    pool = await _pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE iiko_credentials SET bo_url = $1, bo_login = $2
               WHERE tenant_id = $3 AND is_active = true""",
            data.url.rstrip("/"), data.login, tenant_id,
        )
        await conn.execute(
            """INSERT INTO tenant_events (tenant_id, event_type, text, icon)
               VALUES ($1, 'connection', 'Данные iiko обновлены', 'info')""",
            tenant_id,
        )
    return {"status": "ok"}


# =====================================================================
# 7. POST /api/cabinet/connections/iiko/test
# =====================================================================

@router.post("/connections/iiko/test")
async def test_iiko(tenant_id: int = Depends(get_tenant_id)):
    pool = await _pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        cred = await conn.fetchrow(
            "SELECT bo_url, bo_login, bo_password FROM iiko_credentials WHERE tenant_id = $1 AND is_active = true LIMIT 1",
            tenant_id,
        )

    if not cred or not cred["bo_url"]:
        return {"status": "error", "error": "iiko не настроен"}

    try:
        from app.clients.iiko_auth import get_bo_token
        t0 = time.monotonic()
        token = await get_bo_token(
            cred["bo_url"],
            bo_login=cred.get("bo_login") or None,
            bo_password=cred.get("bo_password") or None,
        )
        elapsed = round((time.monotonic() - t0) * 1000)
        return {"status": "ok" if token else "error", "response_time_ms": elapsed}
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


# =====================================================================
# 8. GET /api/cabinet/chats
# =====================================================================

@router.get("/chats")
async def get_chats(tenant_id: int = Depends(get_tenant_id)):
    pool = await _pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT chat_id, name, modules_json, city, is_active
               FROM tenant_chats WHERE tenant_id = $1 AND is_active = true
               ORDER BY name""",
            tenant_id,
        )

    chats = []
    for r in rows:
        mods = r["modules_json"]
        if isinstance(mods, str):
            try:
                mods = json.loads(mods)
            except (json.JSONDecodeError, TypeError):
                mods = []
        elif not isinstance(mods, list):
            mods = []
        cities_raw = r["cities_json"]
        if isinstance(cities_raw, str):
            try:
                cities_raw = json.loads(cities_raw)
            except (json.JSONDecodeError, TypeError):
                cities_raw = []
        elif not isinstance(cities_raw, list):
            cities_raw = []
        # Fallback: если cities_json ещё пустой (до миграции), берём устаревший city
        if not cities_raw and r["city"]:
            cities_raw = [r["city"]]
        chats.append({
            "chat_id": str(r["chat_id"]),
            "name": r["name"],
            "cities": cities_raw,
            "modules": mods,
        })
    return {"chats": chats}


# =====================================================================
# 9. POST /api/cabinet/chats
# =====================================================================

@router.post("/chats")
async def create_chat(data: ChatCreate, tenant_id: int = Depends(get_tenant_id)):
    pool = await _pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    try:
        chat_id_int = int(data.chat_id)
    except ValueError:
        raise HTTPException(400, "Некорректный chat_id")

    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT tenant_id FROM tenant_chats WHERE chat_id = $1",
            chat_id_int,
        )
        if existing:
            if existing["tenant_id"] != tenant_id:
                raise HTTPException(409, "Этот чат уже привязан к другому аккаунту")
            await conn.execute(
                """UPDATE tenant_chats SET is_active = true, name = $1
                   WHERE tenant_id = $2 AND chat_id = $3""",
                data.name, tenant_id, chat_id_int,
            )
        else:
            default_modules = ["late_alerts", "reports"]
            await conn.execute(
                """INSERT INTO tenant_chats (tenant_id, chat_id, name, modules_json, is_active)
                   VALUES ($1, $2, $3, $4::jsonb, true)""",
                tenant_id, chat_id_int, data.name, json.dumps(default_modules),
            )

        await conn.execute(
            """INSERT INTO tenant_events (tenant_id, event_type, text, icon)
               VALUES ($1, 'connection', $2, 'success')""",
            tenant_id, f"Telegram-чат «{data.name}» добавлен",
        )

    return {"chat_id": data.chat_id, "name": data.name}


# =====================================================================
# 10. PUT /api/cabinet/chats/{chat_id}
# =====================================================================

@router.put("/chats/{chat_id}")
async def update_chat(chat_id: str, data: ChatUpdate, tenant_id: int = Depends(get_tenant_id)):
    pool = await _pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    try:
        chat_id_int = int(chat_id)
    except ValueError:
        raise HTTPException(400, "Некорректный chat_id")

    cities_json = json.dumps(data.cities)
    # Для обратной совместимости также пишем первый город в устаревшую колонку city
    city_legacy = data.cities[0] if data.cities else None

    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE tenant_chats SET name = $1, modules_json = $2::jsonb,
               city = $3, cities_json = $4::jsonb
               WHERE tenant_id = $5 AND chat_id = $6 AND is_active = true""",
            data.name, json.dumps(data.modules), city_legacy, cities_json,
            tenant_id, chat_id_int,
        )
        if result == "UPDATE 0":
            raise HTTPException(404, "Чат не найден")

    return {"status": "ok"}


# =====================================================================
# 11. DELETE /api/cabinet/chats/{chat_id}
# =====================================================================

@router.delete("/chats/{chat_id}")
async def delete_chat(chat_id: str, tenant_id: int = Depends(get_tenant_id)):
    pool = await _pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    try:
        chat_id_int = int(chat_id)
    except ValueError:
        raise HTTPException(400, "Некорректный chat_id")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name FROM tenant_chats WHERE tenant_id = $1 AND chat_id = $2",
            tenant_id, chat_id_int,
        )
        if not row:
            raise HTTPException(404, "Чат не найден")

        await conn.execute(
            "UPDATE tenant_chats SET is_active = false WHERE tenant_id = $1 AND chat_id = $2",
            tenant_id, chat_id_int,
        )
        await conn.execute(
            """INSERT INTO tenant_events (tenant_id, event_type, text, icon)
               VALUES ($1, 'connection', $2, 'warning')""",
            tenant_id, f"Telegram-чат «{row['name']}» отключён",
        )

    return {"status": "ok"}


# =====================================================================
# 12. POST /api/cabinet/chats/{chat_id}/test
# =====================================================================

@router.post("/chats/{chat_id}/test")
async def test_chat(chat_id: str, tenant_id: int = Depends(get_tenant_id)):
    pool = await _pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    try:
        chat_id_int = int(chat_id)
    except ValueError:
        raise HTTPException(400, "Некорректный chat_id")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name FROM tenant_chats WHERE tenant_id = $1 AND chat_id = $2 AND is_active = true",
            tenant_id, chat_id_int,
        )
    if not row:
        raise HTTPException(404, "Чат не найден")

    settings = get_settings()
    bot_token = settings.telegram_analytics_bot_token or settings.telegram_bot_token
    if not bot_token:
        return {"status": "error", "error": "Бот не настроен"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id_int,
                    "text": "✅ <b>Тестовое сообщение</b>\nАркентий работает в этом чате.",
                    "parse_mode": "HTML",
                },
            )
            data = resp.json()
            if data.get("ok"):
                return {"status": "ok"}
            return {"status": "error", "error": "Бот не может писать в этот чат"}
    except Exception as e:
        return {"status": "error", "error": str(e)[:200]}


# =====================================================================
# 13. POST /api/cabinet/chats/verify
# =====================================================================

@router.post("/chats/verify")
async def verify_chat(data: ChatVerify, tenant_id: int = Depends(get_tenant_id)):
    settings = get_settings()
    bot_token = settings.telegram_analytics_bot_token or settings.telegram_bot_token
    if not bot_token:
        return {"ok": False, "error": "Бот не настроен"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{bot_token}/getChat",
                json={"chat_id": data.chat_id},
            )
            result = resp.json()
            if result.get("ok"):
                chat_title = result["result"].get("title", result["result"].get("first_name", ""))
                return {"ok": True, "chat_title": chat_title}
            return {"ok": False, "error": "Бот не найден в этом чате"}
    except Exception:
        return {"ok": False, "error": "Ошибка проверки. Попробуйте позже"}


# =====================================================================
# 14. GET /api/cabinet/billing
# =====================================================================

@router.get("/billing")
async def get_billing(tenant_id: int = Depends(get_tenant_id)):
    pool = await _pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        card_info = await conn.fetchrow(
            """SELECT card_last4, card_brand FROM payments
               WHERE tenant_id = $1 AND status = 'succeeded' AND card_last4 IS NOT NULL
               ORDER BY created_at DESC LIMIT 1""",
            tenant_id,
        )

        payments = await conn.fetch(
            """SELECT id, amount, status, description, card_last4, created_at
               FROM payments WHERE tenant_id = $1
               ORDER BY created_at DESC LIMIT 50""",
            tenant_id,
        )

    payment_method = None
    if card_info and card_info["card_last4"]:
        payment_method = {
            "type": "card",
            "last4": card_info["card_last4"],
            "brand": card_info["card_brand"],
        }

    return {
        "payment_method": payment_method,
        "payments": [
            {
                "id": p["id"],
                "date": p["created_at"].strftime("%Y-%m-%d") if p["created_at"] else None,
                "amount": p["amount"],
                "status": p["status"],
                "description": p["description"],
            }
            for p in payments
        ],
    }


# =====================================================================
# 15. GET /api/cabinet/settings
# =====================================================================

@router.get("/settings")
async def get_settings_endpoint(tenant_id: int = Depends(get_tenant_id)):
    pool = await _pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name, contact, email, phone, inn, legal_name FROM tenants WHERE id = $1",
            tenant_id,
        )
    if not row:
        raise HTTPException(404, "Тенант не найден")

    return {
        "name": row["name"],
        "contact": row["contact"],
        "email": row["email"],
        "phone": row["phone"],
        "inn": row["inn"],
        "legal_name": row["legal_name"],
    }


# =====================================================================
# 16. PUT /api/cabinet/settings
# =====================================================================

@router.put("/settings")
async def update_settings(data: SettingsUpdate, tenant_id: int = Depends(get_tenant_id)):
    pool = await _pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        updates = {}
        if data.name is not None:
            updates["name"] = data.name
        if data.contact is not None:
            updates["contact"] = data.contact
        if data.email is not None:
            existing = await conn.fetchrow(
                "SELECT id FROM tenants WHERE email = $1 AND id != $2",
                data.email, tenant_id,
            )
            if existing:
                raise HTTPException(409, "Email уже используется")
            updates["email"] = data.email
        if data.phone is not None:
            updates["phone"] = data.phone

        if updates:
            set_parts = [f"{k} = ${i+2}" for i, k in enumerate(updates.keys())]
            set_parts.append("updated_at = now()")
            query = f"UPDATE tenants SET {', '.join(set_parts)} WHERE id = $1"
            await conn.execute(query, tenant_id, *updates.values())

    return {"status": "ok"}


# =====================================================================
# 17. PUT /api/cabinet/settings/password
# =====================================================================

@router.put("/settings/password")
async def update_password(data: PasswordUpdate, tenant_id: int = Depends(get_tenant_id)):
    if len(data.new_password) < 8:
        raise HTTPException(400, "Пароль должен быть минимум 8 символов")

    pool = await _pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT password_hash FROM tenants WHERE id = $1", tenant_id,
        )
        if not row or not row["password_hash"]:
            raise HTTPException(400, "Пароль не установлен")

        if not verify_password(data.old_password, row["password_hash"]):
            raise HTTPException(403, "Неверный текущий пароль")

        new_hash = hash_password(data.new_password)
        await conn.execute(
            """UPDATE tenants
               SET password_hash = $1, token_version = token_version + 1, updated_at = now()
               WHERE id = $2""",
            new_hash, tenant_id,
        )
        await conn.execute(
            """INSERT INTO tenant_events (tenant_id, event_type, text, icon)
               VALUES ($1, 'system', 'Пароль изменён', 'info')""",
            tenant_id,
        )

    return {"status": "ok", "message": "Пароль изменён. Войдите заново на других устройствах"}


# =====================================================================
# 18. PUT /api/cabinet/settings/legal
# =====================================================================

@router.put("/settings/legal")
async def update_legal(data: LegalUpdate, tenant_id: int = Depends(get_tenant_id)):
    pool = await _pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE tenants SET inn = $1, legal_name = $2, updated_at = now() WHERE id = $3",
            data.inn, data.legal_name, tenant_id,
        )

    return {"status": "ok"}


# =====================================================================
# 19. DELETE /api/cabinet/account
# =====================================================================

@router.delete("/account")
async def delete_account(data: AccountDeleteRequest, tenant_id: int = Depends(get_tenant_id)):
    pool = await _pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name, email, password_hash FROM tenants WHERE id = $1", tenant_id,
        )
        if not row or not row["password_hash"]:
            raise HTTPException(400, "Аккаунт не найден")

        if not verify_password(data.password, row["password_hash"]):
            raise HTTPException(403, "Неверный пароль")

        await conn.execute(
            "UPDATE tenants SET status = 'cancelled', updated_at = now() WHERE id = $1",
            tenant_id,
        )
        await conn.execute(
            "UPDATE subscriptions SET status = 'cancelled', updated_at = now() WHERE tenant_id = $1",
            tenant_id,
        )
        await conn.execute(
            """INSERT INTO tenant_events (tenant_id, event_type, text, icon)
               VALUES ($1, 'system', 'Аккаунт удалён по запросу владельца', 'error')""",
            tenant_id,
        )

    try:
        from app.clients.telegram import monitor
        await monitor(
            f"🔴 <b>Аккаунт удалён</b>\n"
            f"Компания: {row['name']}\n"
            f"Email: {row['email']}"
        )
    except Exception:
        pass

    return {"status": "ok", "message": "Аккаунт деактивирован"}


# =====================================================================
# 20. POST /api/cabinet/subscription/cancel
# =====================================================================

class SubscriptionCancelRequest(BaseModel):
    reason: str = ""
    feedback: str = ""


@router.post("/subscription/cancel")
async def cancel_subscription(req: SubscriptionCancelRequest, tenant_id: int = Depends(get_tenant_id)):
    """Отмена подписки — активна до конца оплаченного периода."""
    pool = await _pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        sub = await conn.fetchrow(
            """SELECT status, plan, next_billing_at, cancel_scheduled
               FROM subscriptions WHERE tenant_id = $1""",
            tenant_id,
        )
        if not sub:
            raise HTTPException(404, "Подписка не найдена")
        if sub["status"] not in ("active", "trial", "grace_period"):
            raise HTTPException(400, "Подписка не активна")
        if sub["cancel_scheduled"]:
            raise HTTPException(400, "Отмена уже запланирована")

        active_until = sub["next_billing_at"]

        await conn.execute(
            """UPDATE subscriptions SET
               cancel_scheduled = true,
               cancel_at = $1,
               cancel_reason = $2,
               cancel_feedback = $3,
               updated_at = now()
               WHERE tenant_id = $4""",
            active_until, req.reason, req.feedback, tenant_id,
        )
        await conn.execute(
            """INSERT INTO subscription_changes (tenant_id, from_status, to_status, action)
               VALUES ($1, $2, 'canceled', 'cancel')""",
            tenant_id, sub["status"],
        )
        await conn.execute(
            """INSERT INTO tenant_events (tenant_id, event_type, text, icon)
               VALUES ($1, 'subscription', $2, 'warning')""",
            tenant_id,
            f"Подписка будет отменена {active_until.strftime('%d.%m.%Y') if active_until else 'в конце периода'}",
        )

        tenant = await conn.fetchrow("SELECT name, email FROM tenants WHERE id = $1", tenant_id)

    try:
        from app.clients.telegram import monitor
        reasons_map = {
            "too_expensive": "Слишком дорого",
            "not_using": "Не пользуюсь",
            "switching": "Перехожу на другой сервис",
            "other": "Другое",
        }
        reason_text = reasons_map.get(req.reason, req.reason or "не указана")
        await monitor(
            f"🔴 <b>Клиент отменяет подписку</b>\n"
            f"Компания: {tenant['name']}\n"
            f"Email: {tenant['email']}\n"
            f"Причина: {reason_text}\n"
            f"Активна до: {active_until.strftime('%d.%m.%Y') if active_until else 'неизвестно'}"
            + (f"\nКомментарий: {req.feedback}" if req.feedback else "")
        )
    except Exception:
        pass

    return {
        "status": "cancellation_scheduled",
        "active_until": active_until.isoformat() if active_until else None,
        "message": f"Подписка будет отменена {active_until.strftime('%d %B %Y') if active_until else 'в конце периода'}",
    }


# =====================================================================
# 21. POST /api/cabinet/subscription/change-plan
# =====================================================================

class ChangePlanRequest(BaseModel):
    new_plan: str


PLAN_PRICES = {
    "basic": 1490,
    "pro": 2990,
    "enterprise": 5990,
}


@router.post("/subscription/change-plan")
async def change_plan(req: ChangePlanRequest, tenant_id: int = Depends(get_tenant_id)):
    """Смена плана: upgrade (с проратой) или downgrade (с следующего периода)."""
    if req.new_plan not in PLAN_PRICES:
        raise HTTPException(400, f"Неизвестный план. Доступны: {', '.join(PLAN_PRICES)}")

    pool = await _pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        sub = await conn.fetchrow(
            """SELECT status, plan, amount_monthly, next_billing_at, period
               FROM subscriptions WHERE tenant_id = $1""",
            tenant_id,
        )
        if not sub:
            raise HTTPException(404, "Подписка не найдена")
        if sub["status"] not in ("active", "trial"):
            raise HTTPException(400, "Смена плана доступна только для активной подписки")
        if sub["plan"] == req.new_plan:
            raise HTTPException(400, "Вы уже на этом плане")

        current_price = PLAN_PRICES.get(sub["plan"], sub["amount_monthly"] or 0)
        new_price = PLAN_PRICES[req.new_plan]

        # Upgrade
        if new_price > current_price:
            days_left = 0
            if sub["next_billing_at"]:
                from datetime import timezone
                now = datetime.utcnow().replace(tzinfo=timezone.utc)
                delta = sub["next_billing_at"] - now
                days_left = max(0, delta.days)

            prorata = int((new_price - current_price) * days_left / 30)

            await conn.execute(
                """INSERT INTO subscription_changes (tenant_id, from_plan, to_plan, action, prorata_amount)
                   VALUES ($1, $2, $3, 'upgrade', $4)""",
                tenant_id, sub["plan"], req.new_plan, prorata,
            )

            if prorata > 0:
                # Создаём платёж на доплату
                import uuid as _uuid
                payment_id = str(_uuid.uuid4())
                description = f"Апгрейд плана {sub['plan']} → {req.new_plan}"
                await conn.execute(
                    """INSERT INTO payments (id, tenant_id, amount, status, payment_method, description)
                       VALUES ($1, $2, $3, 'pending', 'card', $4)""",
                    payment_id, tenant_id, prorata, description,
                )

                settings = get_settings()
                try:
                    from app.clients.yukassa import create_payment as yk_create
                    yk = await yk_create(
                        amount=prorata,
                        description=description,
                        return_url=f"{settings.base_url}/cabinet/subscription?upgraded=1",
                        metadata={"payment_id": payment_id, "tenant_id": str(tenant_id), "new_plan": req.new_plan, "type": "upgrade"},
                    )
                    await conn.execute(
                        "UPDATE payments SET yukassa_id = $1, updated_at = now() WHERE id = $2",
                        yk["id"], payment_id,
                    )
                    payment_url = yk.get("confirmation", {}).get("confirmation_url")
                except Exception as e:
                    logger.error(f"ЮKassa upgrade error: {e}")
                    raise HTTPException(502, "Ошибка платёжной системы")

                return {
                    "action": "upgrade",
                    "prorata_amount": prorata,
                    "payment_url": payment_url,
                    "message": f"Доплата за апгрейд: {prorata:,} ₽",
                }
            else:
                # Мгновенный апгрейд без доплаты
                await conn.execute(
                    """UPDATE subscriptions SET plan = $1, amount_monthly = $2, updated_at = now()
                       WHERE tenant_id = $3""",
                    req.new_plan, new_price, tenant_id,
                )
                return {"action": "upgrade", "prorata_amount": 0, "message": "План изменён"}

        # Downgrade — со следующего периода
        else:
            await conn.execute(
                """UPDATE subscriptions SET pending_plan = $1, pending_plan_from = next_billing_at,
                   updated_at = now() WHERE tenant_id = $2""",
                req.new_plan, tenant_id,
            )
            await conn.execute(
                """INSERT INTO subscription_changes (tenant_id, from_plan, to_plan, action)
                   VALUES ($1, $2, $3, 'downgrade')""",
                tenant_id, sub["plan"], req.new_plan,
            )
            await conn.execute(
                """INSERT INTO tenant_events (tenant_id, event_type, text, icon)
                   VALUES ($1, 'subscription', $2, 'info')""",
                tenant_id,
                f"Смена плана {sub['plan']} → {req.new_plan} запланирована",
            )

            return {
                "action": "downgrade",
                "effective_from": sub["next_billing_at"].isoformat() if sub["next_billing_at"] else None,
                "message": f"План изменится на {req.new_plan} с следующего периода",
            }


# =====================================================================
# 22. POST /api/cabinet/subscription/change-payment-method
# =====================================================================

@router.post("/subscription/change-payment-method")
async def change_payment_method(tenant_id: int = Depends(get_tenant_id)):
    """Создаёт привязку новой карты через ЮKassa."""
    settings = get_settings()
    if not settings.yukassa_shop_id or not settings.yukassa_secret_key:
        raise HTTPException(503, "Платёжная система временно недоступна")

    pool = await _pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        tenant = await conn.fetchrow("SELECT name FROM tenants WHERE id = $1", tenant_id)
        if not tenant:
            raise HTTPException(404, "Тенант не найден")

    try:
        from app.clients.yukassa import create_payment as yk_create
        import uuid as _uuid
        payment_id = str(_uuid.uuid4())

        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO payments (id, tenant_id, amount, status, payment_method, description)
                   VALUES ($1, $2, 1, 'pending', 'card', 'Привязка карты')""",
                payment_id, tenant_id,
            )

        yk = await yk_create(
            amount=1,
            description="Привязка карты Аркентий",
            return_url=f"{settings.base_url}/cabinet/subscription?card_linked=1",
            metadata={"payment_id": payment_id, "tenant_id": str(tenant_id), "type": "card_link"},
            save_payment_method=True,
        )
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE payments SET yukassa_id = $1, updated_at = now() WHERE id = $2",
                yk["id"], payment_id,
            )
        confirmation_url = yk.get("confirmation", {}).get("confirmation_url")
        return {
            "confirmation_url": confirmation_url,
            "message": "Перейдите для привязки карты",
        }
    except Exception as e:
        logger.error(f"change-payment-method error: {e}")
        raise HTTPException(502, "Ошибка платёжной системы")


# =====================================================================
# 23. GET /api/cabinet/subscription/history
# =====================================================================

@router.get("/subscription/history")
async def subscription_history(tenant_id: int = Depends(get_tenant_id)):
    """История изменений подписки."""
    pool = await _pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        events = await conn.fetch(
            """SELECT action, from_plan, to_plan, from_status, to_status,
                      prorata_amount, created_at
               FROM subscription_changes WHERE tenant_id = $1
               ORDER BY created_at DESC LIMIT 50""",
            tenant_id,
        )
        payments = await conn.fetch(
            """SELECT amount, status, description, created_at, card_last4
               FROM payments WHERE tenant_id = $1 AND status = 'succeeded'
               ORDER BY created_at DESC LIMIT 20""",
            tenant_id,
        )

    action_labels = {
        "cancel": "Отмена подписки",
        "reactivate": "Восстановление подписки",
        "upgrade": "Апгрейд плана",
        "downgrade": "Даунгрейд плана",
        "created": "Создание подписки",
    }

    return {
        "changes": [
            {
                "date": e["created_at"].isoformat(),
                "action": e["action"],
                "label": action_labels.get(e["action"], e["action"]),
                "from_plan": e["from_plan"],
                "to_plan": e["to_plan"],
                "from_status": e["from_status"],
                "to_status": e["to_status"],
                "prorata_amount": float(e["prorata_amount"]) if e["prorata_amount"] else None,
            }
            for e in events
        ],
        "payments": [
            {
                "date": p["created_at"].isoformat(),
                "amount": p["amount"],
                "description": p["description"],
                "card_last4": p["card_last4"],
            }
            for p in payments
        ],
    }
