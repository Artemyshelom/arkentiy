"""
Payment API — ЮKassa интеграция + счета для юрлиц.

Эндпоинты:
  POST /api/payments/create         — создать платёж, вернуть confirmation_url
  POST /api/payments/webhook        — webhook от ЮKassa (payment.succeeded / canceled)
  GET  /api/payments/{id}/status    — статус платежа для success/fail страниц
  GET  /api/invoices/{id}           — детали счёта
  POST /api/invoices/{id}/confirm   — юрлицо подтверждает оплату
"""

import json
import logging
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import get_settings
from app.clients.yukassa import create_payment, get_payment, YukassaError
from app.services.auth import get_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["Payments"])
limiter = Limiter(key_func=get_remote_address)


# =====================================================================
# Pydantic models
# =====================================================================

class PaymentCreateRequest(BaseModel):
    amount: int
    description: str | None = None
    save_payment_method: bool = True


class InvoiceConfirmRequest(BaseModel):
    tenant_id: int


# =====================================================================
# Helpers
# =====================================================================

async def _get_pool():
    try:
        from app.database_pg import _pool
        return _pool
    except Exception:
        return None


async def _activate_tenant(conn, tenant_id: int, payment_id: str | None = None) -> None:
    """Активирует тенанта после успешной оплаты. Обрабатывает все статусы подписки."""
    await conn.execute(
        "UPDATE tenants SET status = 'active', updated_at = now() WHERE id = $1",
        tenant_id,
    )

    sub = await conn.fetchrow(
        "SELECT status, plan FROM subscriptions WHERE tenant_id = $1", tenant_id,
    )
    if not sub:
        return

    prev_status = sub["status"]

    if prev_status in ("pending", "trial"):
        await conn.execute(
            """UPDATE subscriptions SET status = 'active',
               next_billing_at = now() + interval '1 month',
               updated_at = now()
               WHERE tenant_id = $1""",
            tenant_id,
        )
        await conn.execute(
            """INSERT INTO tenant_events (tenant_id, event_type, text, icon)
               VALUES ($1, 'payment', 'Оплата подтверждена, аккаунт активирован', 'success')""",
            tenant_id,
        )
    elif prev_status in ("suspended", "expired", "grace_period"):
        # Восстановление после приостановки — сбрасываем cancel и продлеваем
        await conn.execute(
            """UPDATE subscriptions SET status = 'active',
               next_billing_at = now() + interval '1 month',
               cancel_scheduled = false,
               cancel_at = NULL,
               cancel_reason = NULL,
               updated_at = now()
               WHERE tenant_id = $1""",
            tenant_id,
        )
        await conn.execute(
            """INSERT INTO subscription_changes (tenant_id, from_status, to_status, action, payment_id)
               VALUES ($1, $2, 'active', 'reactivate', $3)""",
            tenant_id, prev_status, payment_id,
        )
        await conn.execute(
            """INSERT INTO tenant_events (tenant_id, event_type, text, icon)
               VALUES ($1, 'payment', 'Подписка восстановлена после оплаты', 'success')""",
            tenant_id,
        )
        # Уведомление тенанту
        try:
            from app.jobs.subscription_lifecycle import _notify_tenant
            await _notify_tenant(tenant_id, "✅ <b>Подписка восстановлена!</b>\n\nСпасибо за оплату. Все функции снова доступны.")
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление о восстановлении: {e}")
    else:
        # active — просто продлеваем
        await conn.execute(
            """UPDATE subscriptions SET next_billing_at = next_billing_at + interval '1 month',
               updated_at = now() WHERE tenant_id = $1""",
            tenant_id,
        )


def _next_invoice_number(year: int, seq: int) -> str:
    """АРК-2026-001"""
    return f"АРК-{year}-{seq:03d}"


# =====================================================================
# 1. POST /api/payments/create
# =====================================================================

@router.post("/payments/create")
@limiter.limit("10/minute")
async def api_create_payment(
    req: PaymentCreateRequest,
    request: Request,
    tenant_id: int = Depends(get_tenant_id),
):
    """Создаёт платёж в ЮKassa и возвращает URL для редиректа."""
    settings = get_settings()
    if not settings.yukassa_shop_id or not settings.yukassa_secret_key:
        raise HTTPException(503, "Платёжная система временно недоступна")

    pool = await _get_pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    # Проверяем тенанта
    async with pool.acquire() as conn:
        tenant = await conn.fetchrow(
            "SELECT id, name, email, status FROM tenants WHERE id = $1",
            tenant_id,
        )
        if not tenant:
            raise HTTPException(404, "Тенант не найден")

        # Создаём запись в payments
        payment_id = str(uuid.uuid4())
        description = req.description or f"Подписка Аркентий — {tenant['name']}"

        await conn.execute(
            """INSERT INTO payments (id, tenant_id, amount, status, payment_method, description)
               VALUES ($1, $2, $3, 'pending', 'card', $4)""",
            payment_id, tenant_id, req.amount, description,
        )

    # Создаём платёж в ЮKassa
    return_url = f"{settings.yukassa_return_url}/payment/success?payment_id={payment_id}"
    try:
        yk_payment = await create_payment(
            amount=req.amount,
            description=description,
            return_url=return_url,
            metadata={"payment_id": payment_id, "tenant_id": str(tenant_id)},
            save_payment_method=req.save_payment_method,
        )
    except YukassaError as e:
        logger.error(f"ЮKassa create error for tenant {req.tenant_id}: {e}")
        raise HTTPException(502, "Ошибка платёжной системы. Попробуйте позже")

    # Сохраняем yukassa_id
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE payments SET yukassa_id = $1, updated_at = now() WHERE id = $2",
            yk_payment["id"], payment_id,
        )

    confirmation_url = yk_payment.get("confirmation", {}).get("confirmation_url")
    if not confirmation_url:
        raise HTTPException(502, "Не удалось получить ссылку на оплату")

    return {
        "payment_id": payment_id,
        "confirmation_url": confirmation_url,
    }


# =====================================================================
# 2. POST /api/payments/webhook
# =====================================================================

# IP-адреса ЮKassa для верификации webhook
YUKASSA_IPS = {
    "185.71.76.0/27", "185.71.77.0/27", "77.75.153.0/25",
    "77.75.156.11", "77.75.156.35",
}

def _is_yukassa_ip(ip: str) -> bool:
    """Проверяет, что IP принадлежит ЮKassa."""
    import ipaddress
    try:
        addr = ipaddress.ip_address(ip)
        for net in YUKASSA_IPS:
            if "/" in net:
                if addr in ipaddress.ip_network(net, strict=False):
                    return True
            elif ip == net:
                return True
    except ValueError:
        pass
    return False


@router.post("/payments/webhook")
async def payment_webhook(request: Request):
    """
    Webhook от ЮKassa.
    Обрабатывает события: payment.succeeded, payment.canceled.
    """
    # Проверяем IP отправителя
    client_ip = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip() or request.client.host
    if not _is_yukassa_ip(client_ip):
        logger.warning(f"ЮKassa webhook: отклонён IP {client_ip}")
        raise HTTPException(403, "Forbidden")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    event_type = body.get("event")
    payment_obj = body.get("object", {})
    yukassa_id = payment_obj.get("id")

    if not yukassa_id:
        raise HTTPException(400, "Missing payment id")

    logger.info(f"ЮKassa webhook: event={event_type} payment={yukassa_id}")

    pool = await _get_pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        # Находим наш платёж по yukassa_id
        row = await conn.fetchrow(
            "SELECT id, tenant_id, amount, status FROM payments WHERE yukassa_id = $1",
            yukassa_id,
        )
        if not row:
            logger.warning(f"ЮKassa webhook: платёж {yukassa_id} не найден в БД")
            return {"status": "ignored"}

        payment_id = row["id"]
        tenant_id = row["tenant_id"]

        if event_type == "payment.succeeded":
            if row["status"] == "succeeded":
                return {"status": "already_processed"}

            # Обновляем платёж
            pm = payment_obj.get("payment_method", {})
            card = pm.get("card", {})
            payment_method_id = pm.get("id") if pm.get("saved") else None

            await conn.execute(
                """UPDATE payments SET status = 'succeeded',
                   card_last4 = $1, card_brand = $2, updated_at = now()
                   WHERE id = $3""",
                card.get("last4"), card.get("card_type"), payment_id,
            )

            # Сохраняем payment_method_id для рекуррентных платежей
            if payment_method_id:
                await conn.execute(
                    """UPDATE subscriptions SET yukassa_payment_method_id = $1, updated_at = now()
                       WHERE tenant_id = $2""",
                    payment_method_id, tenant_id,
                )

            # Активируем тенанта
            await _activate_tenant(conn, tenant_id, payment_id)

            logger.info(f"Платёж {payment_id} succeeded, тенант {tenant_id} активирован")

            # Welcome-уведомление тенанту в чат
            try:
                from app.jobs.subscription_lifecycle import _notify_tenant
                tenant_info = await conn.fetchrow("SELECT name FROM tenants WHERE id = $1", tenant_id)
                await _notify_tenant(
                    tenant_id,
                    f"🎉 <b>Добро пожаловать в Аркентий!</b>\n\n"
                    f"Оплата получена. Ваш аккаунт активирован.\n\n"
                    f"📊 Данные начнут подгружаться в ближайшие минуты.\n"
                    f"Настроить модули и чаты → https://arkentiy.ru/cabinet/",
                )
            except Exception as e:
                logger.warning(f"Не удалось отправить welcome-уведомление: {e}")

            # Уведомляем Артемия
            try:
                from app.clients.telegram import monitor
                tenant = await conn.fetchrow("SELECT name, email FROM tenants WHERE id = $1", tenant_id)
                await monitor(
                    f"💳 <b>Оплата получена!</b>\n"
                    f"Компания: {tenant['name']}\n"
                    f"Email: {tenant['email']}\n"
                    f"Сумма: {row['amount']:,} ₽\n"
                    f"Карта: {'*' + card.get('last4', '????')}"
                )
            except Exception as e:
                logger.warning(f"Не удалось отправить уведомление об оплате: {e}")

        elif event_type == "payment.canceled":
            await conn.execute(
                "UPDATE payments SET status = 'canceled', updated_at = now() WHERE id = $1",
                payment_id,
            )
            await conn.execute(
                """INSERT INTO tenant_events (tenant_id, event_type, text, icon)
                   VALUES ($1, 'payment', 'Оплата отменена', 'warning')""",
                tenant_id,
            )
            logger.info(f"Платёж {payment_id} canceled")

    return {"status": "ok"}


# =====================================================================
# 3. GET /api/payments/{payment_id}/status
# =====================================================================

@router.get("/payments/{payment_id}/status")
async def payment_status(payment_id: str):
    """Статус платежа для success/fail страниц."""
    pool = await _get_pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT p.id, p.tenant_id, p.status, p.amount, p.card_last4, p.card_brand,
                      p.created_at, t.name as tenant_name,
                      s.modules_json, s.branches_count, s.next_billing_at
               FROM payments p
               JOIN tenants t ON t.id = p.tenant_id
               LEFT JOIN subscriptions s ON s.tenant_id = p.tenant_id
               WHERE p.id = $1""",
            payment_id,
        )

    if not row:
        raise HTTPException(404, "Платёж не найден")

    modules = []
    if row["modules_json"]:
        try:
            modules = json.loads(row["modules_json"]) if isinstance(row["modules_json"], str) else row["modules_json"]
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "payment_id": row["id"],
        "status": row["status"],
        "amount": row["amount"],
        "tenant_id": row["tenant_id"],
        "tenant_name": row["tenant_name"],
        "modules": modules,
        "branches_count": row["branches_count"],
        "next_payment_date": row["next_billing_at"].strftime("%Y-%m-%d") if row["next_billing_at"] else None,
        "card_last4": row["card_last4"],
        "card_brand": row["card_brand"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


# =====================================================================
# 4. GET /api/invoices/{invoice_id}
# =====================================================================

@router.get("/invoices/{invoice_id}")
async def get_invoice(invoice_id: str):
    """Детали счёта для страницы /payment/invoice."""
    pool = await _get_pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT i.*, t.name as tenant_name
               FROM invoices i
               JOIN tenants t ON t.id = i.tenant_id
               WHERE i.id = $1""",
            invoice_id,
        )

    if not row:
        raise HTTPException(404, "Счёт не найден")

    items = row["items_json"] if isinstance(row["items_json"], list) else json.loads(row["items_json"] or "[]")

    return {
        "invoice_id": row["id"],
        "invoice_number": row["invoice_number"],
        "status": row["status"],
        "amount": row["amount"],
        "tenant_name": row["tenant_name"],
        "inn": row["inn"],
        "legal_name": row["legal_name"],
        "items": items,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }


# =====================================================================
# 5. POST /api/invoices/{invoice_id}/confirm
# =====================================================================

@router.post("/invoices/{invoice_id}/confirm")
@limiter.limit("5/minute")
async def confirm_invoice(invoice_id: str, request: Request):
    """Юрлицо подтверждает оплату счёта (ручная верификация Артемием)."""
    pool = await _get_pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, tenant_id, status, amount, invoice_number FROM invoices WHERE id = $1",
            invoice_id,
        )
        if not row:
            raise HTTPException(404, "Счёт не найден")
        if row["status"] not in ("pending",):
            return {"status": row["status"], "message": "Счёт уже обработан"}

        await conn.execute(
            "UPDATE invoices SET status = 'pending_verification', updated_at = now() WHERE id = $1",
            invoice_id,
        )

        await conn.execute(
            """INSERT INTO tenant_events (tenant_id, event_type, text, icon)
               VALUES ($1, 'payment', $2, 'info')""",
            row["tenant_id"],
            f"Подтверждение оплаты счёта {row['invoice_number']} отправлено на проверку",
        )

    # Уведомляем Артемия
    try:
        from app.clients.telegram import monitor
        await monitor(
            f"📄 <b>Подтверждение оплаты счёта</b>\n"
            f"Счёт: {row['invoice_number']}\n"
            f"Сумма: {row['amount']:,} ₽\n"
            f"<i>Требуется ручная проверка</i>"
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить уведомление о подтверждении счёта: {e}")

    return {
        "status": "pending_verification",
        "message": "Заявка на подтверждение отправлена. Активируем в течение 1 рабочего дня",
    }


# =====================================================================
# 6. POST /api/invoices/create (вызывается из визарда для юрлиц)
# =====================================================================

@router.post("/invoices/create")
@limiter.limit("5/minute")
async def create_invoice(
    request: Request,
    tenant_id: int = Depends(get_tenant_id),
):
    """Создаёт счёт для юрлица."""
    body = await request.json()
    amount = body.get("amount")
    inn = body.get("inn", "")
    legal_name = body.get("legal_name", "")
    items = body.get("items", [])

    if not amount:
        raise HTTPException(400, "amount обязателен")

    pool = await _get_pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        tenant = await conn.fetchrow(
            "SELECT id, name FROM tenants WHERE id = $1", tenant_id,
        )
        if not tenant:
            raise HTTPException(404, "Тенант не найден")

        # Генерируем номер счёта
        year = datetime.utcnow().year
        seq = await conn.fetchval(
            "SELECT COALESCE(MAX(CAST(SPLIT_PART(invoice_number, '-', 3) AS INTEGER)), 0) + 1 FROM invoices WHERE invoice_number LIKE $1",
            f"АРК-{year}-%",
        )
        invoice_number = _next_invoice_number(year, seq)
        invoice_id = str(uuid.uuid4())

        await conn.execute(
            """INSERT INTO invoices (id, tenant_id, invoice_number, amount, inn, legal_name, items_json)
               VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)""",
            invoice_id, tenant_id, invoice_number, amount, inn, legal_name,
            json.dumps(items, ensure_ascii=False),
        )

        await conn.execute(
            """INSERT INTO tenant_events (tenant_id, event_type, text, icon)
               VALUES ($1, 'payment', $2, 'info')""",
            tenant_id, f"Создан счёт {invoice_number} на {amount:,} ₽",
        )

    # Уведомляем Артемия
    try:
        from app.clients.telegram import monitor
        await monitor(
            f"📄 <b>Новый счёт</b>\n"
            f"Компания: {tenant['name']}\n"
            f"Счёт: {invoice_number}\n"
            f"Сумма: {amount:,} ₽\n"
            f"ИНН: {inn or 'не указан'}"
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить уведомление о новом счёте: {e}")

    return {
        "invoice_id": invoice_id,
        "invoice_number": invoice_number,
        "invoice_url": f"/payment/invoice?invoice_id={invoice_id}",
    }


# =====================================================================
# 7. POST /api/payments/{payment_id}/retry
# =====================================================================

@router.post("/payments/{payment_id}/retry")
@limiter.limit("10/minute")
async def retry_payment(payment_id: str, request: Request):
    """
    Повторный платёж без JWT — по payment_id ищет исходный платёж в БД,
    создаёт новый. Используется со страницы /payment/fail.
    """
    settings = get_settings()
    if not settings.yukassa_shop_id or not settings.yukassa_secret_key:
        raise HTTPException(503, "Платёжная система временно недоступна")

    pool = await _get_pool()
    if not pool:
        raise HTTPException(500, "Database not available")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT p.tenant_id, p.amount, p.description, p.status, t.name as tenant_name
               FROM payments p
               JOIN tenants t ON t.id = p.tenant_id
               WHERE p.id = $1""",
            payment_id,
        )

    if not row:
        raise HTTPException(404, "Платёж не найден")
    if row["status"] == "succeeded":
        raise HTTPException(409, "Этот платёж уже был успешно оплачен")

    tenant_id = row["tenant_id"]
    amount = row["amount"]
    description = row["description"] or f"Подписка Аркентий — {row['tenant_name']}"

    new_payment_id = str(uuid.uuid4())

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO payments (id, tenant_id, amount, status, payment_method, description)
               VALUES ($1, $2, $3, 'pending', 'card', $4)""",
            new_payment_id, tenant_id, amount, description,
        )

    return_url = f"{settings.yukassa_return_url}/payment/success?payment_id={new_payment_id}"
    try:
        yk_payment = await create_payment(
            amount=amount,
            description=description,
            return_url=return_url,
            metadata={"payment_id": new_payment_id, "tenant_id": str(tenant_id)},
            save_payment_method=True,
        )
    except YukassaError as e:
        logger.error(f"ЮKassa retry error for tenant {tenant_id}: {e}")
        raise HTTPException(502, "Ошибка платёжной системы. Попробуйте позже")

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE payments SET yukassa_id = $1, updated_at = now() WHERE id = $2",
            yk_payment["id"], new_payment_id,
        )

    confirmation_url = yk_payment.get("confirmation", {}).get("confirmation_url")
    if not confirmation_url:
        raise HTTPException(502, "Не удалось получить ссылку на оплату")

    return {
        "payment_id": new_payment_id,
        "confirmation_url": confirmation_url,
    }
