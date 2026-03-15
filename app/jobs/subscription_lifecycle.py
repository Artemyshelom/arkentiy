"""
Жизненный цикл подписок — автоматические уведомления и деактивация.

1. job_trial_expiry — предупреждения о конце триала (3 дня, 1 день), деактивация
2. job_payment_grace — предупреждения при просрочке оплаты, деактивация через 7 дней
3. job_welcome_setup — приветствие + запуск бэкфила после первой оплаты

Запускаются ежедневно в 04:00 МСК (после биллинга в 03:00).
"""

import logging
from datetime import datetime, timedelta

from app.database_pg import get_pool_or_none

logger = logging.getLogger(__name__)


async def job_trial_expiry() -> None:
    """
    Обработка истекающих триалов:
    - За 3 дня: предупреждение в Telegram
    - За 1 день: последнее предупреждение
    - Истёк: деактивация (status → expired)
    """
    pool = get_pool_or_none()
    if not pool:
        return

    now = datetime.utcnow()

    async with pool.acquire() as conn:
        # 1. Предупреждение за 3 дня
        warn_3d = await conn.fetch(
            """SELECT t.id, t.name, t.email, t.contact, t.trial_ends_at
               FROM tenants t
               WHERE t.status = 'trial'
                 AND t.trial_ends_at IS NOT NULL
                 AND t.trial_ends_at::date - CURRENT_DATE = 3""",
        )
        for t in warn_3d:
            await conn.execute(
                """INSERT INTO tenant_events (tenant_id, event_type, text, icon)
                   VALUES ($1, 'subscription', 'Триал заканчивается через 3 дня. Оплатите подписку чтобы продолжить', 'warning')""",
                t["id"],
            )
            await _notify_tenant(
                t["id"],
                f"⏳ <b>Триал заканчивается через 3 дня</b>\n\n"
                f"Компания: {t['name']}\n"
                f"Триал до: {t['trial_ends_at'].strftime('%d.%m.%Y')}\n\n"
                f"Оплатите подписку чтобы продолжить работу.\n"
                f"👉 https://arkentiy.ru/cabinet/",
            )
            logger.info(f"[trial] Предупреждение 3 дня: {t['name']}")

        # 2. Предупреждение за 1 день
        warn_1d = await conn.fetch(
            """SELECT t.id, t.name, t.trial_ends_at
               FROM tenants t
               WHERE t.status = 'trial'
                 AND t.trial_ends_at IS NOT NULL
                 AND t.trial_ends_at::date - CURRENT_DATE = 1""",
        )
        for t in warn_1d:
            await conn.execute(
                """INSERT INTO tenant_events (tenant_id, event_type, text, icon)
                   VALUES ($1, 'subscription', 'Триал заканчивается завтра!', 'warning')""",
                t["id"],
            )
            await _notify_tenant(
                t["id"],
                f"🔴 <b>Триал заканчивается завтра!</b>\n\n"
                f"Компания: {t['name']}\n\n"
                f"После окончания триала данные сохраняются 7 дней.\n"
                f"👉 https://arkentiy.ru/cabinet/",
            )
            logger.info(f"[trial] Предупреждение 1 день: {t['name']}")

        # 3. Истёкшие триалы — деактивация
        expired = await conn.fetch(
            """SELECT t.id, t.name, t.email
               FROM tenants t
               WHERE t.status = 'trial'
                 AND t.trial_ends_at IS NOT NULL
                 AND t.trial_ends_at < now()""",
        )
        for t in expired:
            await conn.execute(
                "UPDATE tenants SET status = 'expired', updated_at = now() WHERE id = $1",
                t["id"],
            )
            await conn.execute(
                "UPDATE subscriptions SET status = 'expired', updated_at = now() WHERE tenant_id = $1",
                t["id"],
            )
            await conn.execute(
                """INSERT INTO tenant_events (tenant_id, event_type, text, icon)
                   VALUES ($1, 'subscription', 'Триал истёк. Оплатите подписку для продолжения', 'error')""",
                t["id"],
            )
            logger.info(f"[trial] Истёк: {t['name']} ({t['email']})")

        # Уведомляем Артемия о всех действиях
        if expired:
            try:
                from app.clients.telegram import monitor
                names = ", ".join(t["name"] for t in expired)
                await monitor(f"⏳ <b>Триалы истекли:</b> {names}")
            except Exception:
                pass


async def job_payment_grace() -> None:
    """
    Grace period при неудачной оплате:
    - Подписка active, next_billing_at < now(), нет succeeded payment за последний месяц
    - Ставим status = past_due, grace_until = +7 дней
    - За 3 дня до grace_until: предупреждение
    - grace_until < now(): деактивация
    """
    pool = get_pool_or_none()
    if not pool:
        return

    async with pool.acquire() as conn:
        # 1. Новые просрочки: active подписки где billing просрочен и нет grace
        overdue = await conn.fetch(
            """SELECT s.tenant_id, t.name, t.email, s.next_billing_at
               FROM subscriptions s
               JOIN tenants t ON t.id = s.tenant_id
               WHERE s.status = 'active'
                 AND s.next_billing_at < now() - interval '1 day'
                 AND s.grace_until IS NULL
                 AND s.yukassa_payment_method_id IS NOT NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM payments p
                     WHERE p.tenant_id = s.tenant_id
                       AND p.status = 'succeeded'
                       AND p.created_at > s.next_billing_at - interval '3 days'
                 )""",
        )
        for s in overdue:
            grace = datetime.utcnow() + timedelta(days=7)
            await conn.execute(
                """UPDATE subscriptions SET status = 'past_due', grace_until = $1, updated_at = now()
                   WHERE tenant_id = $2""",
                grace, s["tenant_id"],
            )
            await conn.execute(
                "UPDATE tenants SET status = 'past_due', updated_at = now() WHERE id = $1",
                s["tenant_id"],
            )
            await conn.execute(
                """INSERT INTO tenant_events (tenant_id, event_type, text, icon)
                   VALUES ($1, 'payment', 'Оплата не прошла. Сервис работает ещё 7 дней', 'warning')""",
                s["tenant_id"],
            )
            await _notify_tenant(
                s["tenant_id"],
                f"⚠️ <b>Оплата не прошла</b>\n\n"
                f"Компания: {s['name']}\n"
                f"Сервис работает до: {grace.strftime('%d.%m.%Y')}\n\n"
                f"Обновите карту в личном кабинете.\n"
                f"👉 https://arkentiy.ru/cabinet/",
            )
            logger.info(f"[grace] Новая просрочка: {s['name']}")

        # 2. Предупреждение за 3 дня до конца grace
        warn_grace = await conn.fetch(
            """SELECT s.tenant_id, t.name, s.grace_until
               FROM subscriptions s
               JOIN tenants t ON t.id = s.tenant_id
               WHERE s.status = 'past_due'
                 AND s.grace_until IS NOT NULL
                 AND s.grace_until::date - CURRENT_DATE = 3""",
        )
        for s in warn_grace:
            await _notify_tenant(
                s["tenant_id"],
                f"🔴 <b>Сервис будет отключён через 3 дня</b>\n\n"
                f"Компания: {s['name']}\n"
                f"Дедлайн: {s['grace_until'].strftime('%d.%m.%Y')}\n\n"
                f"👉 https://arkentiy.ru/cabinet/",
            )

        # 3. Grace period истёк — деактивация
        grace_expired = await conn.fetch(
            """SELECT s.tenant_id, t.name, t.email
               FROM subscriptions s
               JOIN tenants t ON t.id = s.tenant_id
               WHERE s.status = 'past_due'
                 AND s.grace_until IS NOT NULL
                 AND s.grace_until < now()""",
        )
        for s in grace_expired:
            await conn.execute(
                "UPDATE tenants SET status = 'suspended', updated_at = now() WHERE id = $1",
                s["tenant_id"],
            )
            await conn.execute(
                "UPDATE subscriptions SET status = 'suspended', updated_at = now() WHERE tenant_id = $1",
                s["tenant_id"],
            )
            await conn.execute(
                """INSERT INTO tenant_events (tenant_id, event_type, text, icon)
                   VALUES ($1, 'subscription', 'Подписка приостановлена из-за неоплаты', 'error')""",
                s["tenant_id"],
            )
            logger.info(f"[grace] Приостановлен: {s['name']} ({s['email']})")

        if grace_expired:
            try:
                from app.clients.telegram import monitor
                names = ", ".join(s["name"] for s in grace_expired)
                await monitor(f"🔴 <b>Подписки приостановлены (неоплата):</b> {names}")
            except Exception:
                pass


async def _notify_tenant(tenant_id: int, text: str) -> None:
    """Отправить уведомление тенанту в его первый активный чат."""
    pool = get_pool_or_none()
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
            chat = await conn.fetchrow(
                """SELECT chat_id FROM tenant_chats
                   WHERE tenant_id = $1 AND is_active = true
                   ORDER BY chat_id LIMIT 1""",
                tenant_id,
            )
        if not chat:
            # Fallback: уведомляем Артемия
            from app.clients.telegram import monitor
            await monitor(text)
            return

        from app.config import get_settings
        settings = get_settings()
        bot_token = settings.telegram_analytics_bot_token or settings.telegram_bot_token
        if not bot_token:
            return

        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat["chat_id"], "text": text, "parse_mode": "HTML"},
            )
    except Exception as e:
        logger.warning(f"[lifecycle] Не удалось отправить уведомление тенанту {tenant_id}: {e}")
