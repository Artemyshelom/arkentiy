"""
Рекуррентный биллинг — автоматическое продление подписок через ЮKassa.

Запускается ежедневно в 03:00 МСК.
Ищет подписки с next_billing_at <= now() и сохранённым payment_method_id.
"""

import json
import logging
import uuid
from datetime import datetime

from app.clients.yukassa import create_payment, YukassaError
from app.config import get_settings
from app.utils.job_tracker import track_job

logger = logging.getLogger(__name__)


@track_job("recurring_billing")
async def job_recurring_billing() -> None:
    """Автоматическое продление подписок."""
    try:
        from app.database_pg import _pool
    except Exception:
        return

    if not _pool:
        return

    settings = get_settings()
    if not settings.yukassa_shop_id:
        return

    async with _pool.acquire() as conn:
        # 1. Обрабатываем истекшие cancel_scheduled
        cancelled = await conn.fetch(
            """SELECT tenant_id, plan FROM subscriptions
               WHERE cancel_scheduled = true AND cancel_at <= now()"""
        )
        for row in cancelled:
            await conn.execute(
                """UPDATE subscriptions SET status = 'canceled',
                   cancel_scheduled = false, updated_at = now()
                   WHERE tenant_id = $1""",
                row["tenant_id"],
            )
            await conn.execute(
                "UPDATE tenants SET status = 'canceled', updated_at = now() WHERE id = $1",
                row["tenant_id"],
            )
            await conn.execute(
                """INSERT INTO tenant_events (tenant_id, event_type, text, icon)
                   VALUES ($1, 'subscription', 'Подписка отменена', 'error')""",
                row["tenant_id"],
            )
            logger.info(f"[billing] Тенант {row['tenant_id']}: подписка отменена по расписанию")
            try:
                from app.jobs.subscription_lifecycle import _notify_tenant
                await _notify_tenant(
                    row["tenant_id"],
                    "🔴 <b>Подписка отменена.</b>\n\nДоступ к данным закрыт.\n"
                    "Для возобновления — оплатите подписку по ссылке: https://arkenty.ru/cabinet/",
                )
            except Exception as e:
                logger.warning(f"[billing] Не удалось уведомить тенанта {row['tenant_id']}: {e}")

        # 2. Применяем pending_plan (downgrade со следующего периода)
        pending = await conn.fetch(
            """SELECT tenant_id, plan, pending_plan FROM subscriptions
               WHERE pending_plan IS NOT NULL AND pending_plan_from <= now()"""
        )
        for row in pending:
            from app.routers.cabinet import PLAN_PRICES
            new_price = PLAN_PRICES.get(row["pending_plan"], 0)
            await conn.execute(
                """UPDATE subscriptions SET plan = $1, amount_monthly = $2,
                   pending_plan = NULL, pending_plan_from = NULL, updated_at = now()
                   WHERE tenant_id = $3""",
                row["pending_plan"], new_price, row["tenant_id"],
            )
            await conn.execute(
                """INSERT INTO subscription_changes (tenant_id, from_plan, to_plan, action)
                   VALUES ($1, $2, $3, 'downgrade')""",
                row["tenant_id"], row["plan"], row["pending_plan"],
            )
            logger.info(f"[billing] Тенант {row['tenant_id']}: план изменён {row['plan']} → {row['pending_plan']}")

        # 3. Находим подписки, которые пора продлить
        rows = await conn.fetch(
            """SELECT s.tenant_id, s.amount_monthly, s.yukassa_payment_method_id,
                      s.period, t.name as tenant_name, t.email
               FROM subscriptions s
               JOIN tenants t ON t.id = s.tenant_id
               WHERE s.status = 'active'
                 AND s.cancel_scheduled IS NOT TRUE
                 AND s.next_billing_at <= now()
                 AND s.yukassa_payment_method_id IS NOT NULL"""
        )

    if not rows:
        logger.info("[billing] Нет подписок для продления")
        return

    logger.info(f"[billing] Найдено {len(rows)} подписок для продления")

    for row in rows:
        tenant_id = row["tenant_id"]
        amount = row["amount_monthly"]
        pm_id = row["yukassa_payment_method_id"]

        if amount <= 0:
            continue

        payment_id = str(uuid.uuid4())

        try:
            # Создаём запись платежа
            async with _pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO payments (id, tenant_id, amount, status, payment_method, description)
                       VALUES ($1, $2, $3, 'pending', 'card', $4)""",
                    payment_id, tenant_id, amount,
                    f"Продление подписки — {row['tenant_name']}",
                )

            # Автосписание через ЮKassa
            yk_payment = await create_payment(
                amount=amount,
                description=f"Продление подписки Аркентий — {row['tenant_name']}",
                return_url=settings.yukassa_return_url,
                metadata={"payment_id": payment_id, "tenant_id": str(tenant_id), "type": "recurring"},
                payment_method_id=pm_id,
            )

            # Автосписание может быть succeeded сразу
            if yk_payment.get("status") == "succeeded":
                async with _pool.acquire() as conn:
                    pm = yk_payment.get("payment_method", {})
                    card = pm.get("card", {})
                    await conn.execute(
                        """UPDATE payments SET status = 'succeeded', yukassa_id = $1,
                           card_last4 = $2, card_brand = $3, updated_at = now()
                           WHERE id = $4""",
                        yk_payment["id"], card.get("last4"), card.get("card_type"), payment_id,
                    )
                    # Продлеваем подписку на месяц
                    interval = "1 month" if row["period"] != "annual" else "1 year"
                    await conn.execute(
                        f"""UPDATE subscriptions SET next_billing_at = next_billing_at + interval '{interval}',
                            updated_at = now()
                            WHERE tenant_id = $1 AND status = 'active'""",
                        tenant_id,
                    )
                    await conn.execute(
                        """INSERT INTO tenant_events (tenant_id, event_type, text, icon)
                           VALUES ($1, 'payment', $2, 'success')""",
                        tenant_id,
                        f"Подписка продлена, списано {amount:,} ₽",
                    )

                logger.info(f"[billing] Тенант {tenant_id} ({row['tenant_name']}): продлён, {amount} ₽")

            else:
                # Платёж в обработке — webhook обработает
                async with _pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE payments SET yukassa_id = $1, updated_at = now() WHERE id = $2",
                        yk_payment["id"], payment_id,
                    )
                logger.info(f"[billing] Тенант {tenant_id}: платёж создан, ждём webhook")

        except YukassaError as e:
            logger.error(f"[billing] Ошибка автосписания для тенанта {tenant_id}: {e}")
            # Уведомляем о проблеме
            try:
                from app.clients.telegram import monitor
                await monitor(
                    f"⚠️ <b>Ошибка автосписания</b>\n"
                    f"Компания: {row['tenant_name']}\n"
                    f"Сумма: {amount:,} ₽\n"
                    f"Ошибка: {e}"
                )
            except Exception:
                pass

        except Exception as e:
            logger.error(f"[billing] Неожиданная ошибка для тенанта {tenant_id}: {e}")
