"""
Алерты об опоздании активных заказов доставки (>= 15 мин от плановой доставки).

Запускается каждые 2 минуты. Шлёт через аналитический бот в город-специфичный чат.
Каждый заказ оповещается не более одного раза в сутки.
"""
import html
import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.clients.iiko_bo_events import (
    _parse_customer_name,
    _parse_customer_phone,
    _states,
)
from app.config import get_settings
from app.database import get_alert_chats_for_city, get_client_order_count

logger = logging.getLogger(__name__)

LATE_THRESHOLD_MIN = 15
LATE_MAX_MIN = 60  # заказы >60 мин опоздания — стале/отменены, не алертим
LOCAL_UTC_OFFSET = 7  # все 9 точек в UTC+7

# Чаты для алертов теперь берутся из БД (модуль late_alerts + город).
# Управление через /доступ → группа → включить "Алерты" + выбрать города.

# Только эти статусы считаем активной доставкой (whitelist вместо blacklist)
ACTIVE_DELIVERY_STATUSES = frozenset({
    "Новая", "Не подтверждена", "Ждет отправки",
    "В пути к клиенту", "В процессе приготовления",
})

# In-memory деduplication: {(branch_name, delivery_num): datetime оповещения}
_alerted: dict[tuple[str, str], datetime] = {}

# Время запуска — первые 5 минут после старта не шлём алерты по уже опоздавшим заказам
_startup_time: datetime = datetime.now(tz=timezone.utc)


def _human_status(delivery: dict, cooking_status: str | None) -> str:
    """Человекочитаемый статус заказа с учётом cooking_status."""
    status = delivery.get("status", "")
    if status == "В пути к клиенту":
        return "в пути к клиенту"
    if status in ("Новая", "Не подтверждена", "Ждет отправки"):
        if cooking_status == "Собран":
            return "приготовлен, ждёт курьера"
        if cooking_status == "Приготовлено":
            return "готовится"
        return "ожидает кухни"
    return status or "неизвестен"


async def _send_alert(chat_id: int, text: str, token: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
            })
            if r.status_code != 200:
                logger.warning(f"late_alerts: TG {r.status_code} → {r.text[:200]}")
        except Exception as e:
            logger.error(f"late_alerts: Telegram send error: {e}")


async def job_late_alerts() -> None:
    settings = get_settings()
    token = settings.telegram_analytics_bot_token
    if not token:
        logger.warning("late_alerts: TELEGRAM_ANALYTICS_BOT_TOKEN не задан")
        return

    now_local = (datetime.now(tz=timezone.utc) + timedelta(hours=LOCAL_UTC_OFFSET)).replace(tzinfo=None)

    # Очищаем записи старше 24 часов (защита от утечки памяти)
    cutoff = now_local - timedelta(hours=24)
    for k in [k for k, v in _alerted.items() if v < cutoff]:
        del _alerted[k]

    branch_to_city = {b["name"]: b["city"] for b in settings.branches}

    alerts_sent = 0
    for branch_name, state in _states.items():
        city = branch_to_city.get(branch_name)
        if not city:
            continue
        target_chats = await get_alert_chats_for_city(city)
        if not target_chats:
            continue  # нет зарегистрированных чатов с алертами для этого города

        for num, d in list(state.deliveries.items()):
            # Только явно активные статусы (whitelist надёжнее blacklist)
            if d.get("status") not in ACTIVE_DELIVERY_STATUSES:
                continue
            if d.get("is_self_service"):
                continue

            planned_raw = d.get("planned_time")
            if not planned_raw:
                continue

            try:
                # iiko Events хранит время как "2026-02-21T22:00:00.000" или "2026-02-21 22:00:00"
                clean = planned_raw.replace("T", " ").split(".")[0]
                planned_dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                logger.warning("late_alerts: не удалось распарсить planned_time=%r", planned_raw)
                continue

            overdue_min = (now_local - planned_dt).total_seconds() / 60
            if overdue_min < LATE_THRESHOLD_MIN:
                continue
            if overdue_min > LATE_MAX_MIN:
                continue  # стале или отменённый, пропускаем

            key = (branch_name, str(num))
            if key in _alerted:
                continue  # уже оповещали сегодня

            # При рестарте сервера: молча помечаем уже опоздавшие заказы как уведомлённые
            # Это предотвращает шторм алертов по старым/отменённым заказам после рестарта
            fresh_start = (datetime.now(tz=timezone.utc) - _startup_time).total_seconds() < 300
            if fresh_start:
                _alerted[key] = now_local
                continue

            _alerted[key] = now_local

            # Получаем cooking status из state
            cooking_status = state._cooking_status(str(num))

            # Парсим данные клиента
            customer_raw = d.get("customer_raw")
            raw_phone = _parse_customer_phone(customer_raw) or ""
            client_name = html.escape(_parse_customer_name(customer_raw) or "—")
            client_phone = html.escape(raw_phone or "—")
            order_count = await get_client_order_count(raw_phone)
            if order_count == 1:
                client_tag = "🆕 Новый клиент"
            elif order_count > 1:
                client_tag = f"🔄 Повторный ({order_count} зак.)"
            else:
                client_tag = ""
            address = html.escape(d.get("delivery_address") or "адрес не указан")
            courier = (d.get("courier") or "").strip()
            h_status = html.escape(_human_status(d, cooking_status))
            s = d.get("sum")
            sum_str = f"{int(float(s)):,} ₽".replace(",", " ") if s else "—"

            courier_line = f"🛵 Курьер: <b>{html.escape(courier)}</b>\n" if courier else ""

            text = (
                f"🚨 <b>Опоздание +{int(overdue_min)} мин</b> — {html.escape(branch_name)}\n\n"
                f"<b>#{html.escape(str(num))}</b>\n"
                f"👤 {client_name}\n"
                f"📞 <code>{client_phone}</code>\n"
                f"💰 {sum_str}\n"
                f"🗺 {address}\n"
                f"📦 Статус: {h_status}\n"
                f"{courier_line}"
                + (f"\n\n{client_tag}" if client_tag else "")
            ).strip()

            logger.info(
                f"late_alerts: {branch_name} #{num} +{int(overdue_min)} мин → {target_chats}"
            )
            for chat_id in target_chats:
                await _send_alert(chat_id, text, token)
            alerts_sent += 1

    if alerts_sent:
        logger.info(f"late_alerts: отправлено {alerts_sent} алертов")
