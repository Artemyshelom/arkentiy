"""
Webhook от Битрикс24 — уведомления об изменении задач.

Битрикс24 шлёт POST на /webhook/bitrix при изменении задач.
Мы: уведомляем Артемия о завершённых и просроченных задачах.

Настройка в Битрикс24:
  Приложения → Вебхуки → Исходящий вебхук
  Событие: OnTaskUpdate, OnTaskAdd
  URL: https://api.твой-домен.ru/webhook/bitrix
"""

import logging

from fastapi import APIRouter, Request

from app.clients import telegram
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter(tags=["Webhooks"])

# Статусы задач, о которых уведомляем
NOTIFY_ON_STATUS = {
    "3": "✅ Выполнена",
    "5": "🔴 Просрочена",
}


@router.post("/bitrix")
async def handle_bitrix_webhook(request: Request):
    """Принимает webhook от Битрикс24 об изменениях задач."""
    # Проверяем webhook secret
    webhook_secret = settings.webhook_secret
    if webhook_secret:
        token = request.query_params.get("token") or request.headers.get("x-webhook-secret", "")
        if token != webhook_secret:
            logger.warning(f"Битрикс24 webhook: неверный token/secret")
            return {"status": "forbidden"}

    try:
        # Битрикс24 может слать form-data или JSON
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            payload = await request.json()
        else:
            form = await request.form()
            payload = dict(form)
    except Exception as e:
        logger.error(f"Битрикс24 webhook: ошибка парсинга: {e}")
        return {"status": "error"}

    event = payload.get("event", "")
    task_data = payload.get("data", {})

    logger.info(f"Битрикс24 webhook: event={event}, data={str(task_data)[:200]}")

    if event in ("ONTASKUPDATE", "ONTASKADD"):
        task_id = task_data.get("FIELDS_AFTER", {}).get("ID") or task_data.get("ID", "?")
        title = task_data.get("FIELDS_AFTER", {}).get("TITLE") or "Задача"
        status_code = str(task_data.get("FIELDS_AFTER", {}).get("STATUS", ""))
        responsible = task_data.get("FIELDS_AFTER", {}).get("RESPONSIBLE_ID", "")

        status_label = NOTIFY_ON_STATUS.get(status_code)
        if status_label:
            msg = (
                f"{status_label} — <b>{title}</b>\n"
                f"<i>Задача #{task_id}, ответственный ID: {responsible}</i>"
            )
            await telegram.report(msg)
            logger.info(f"Отправлено уведомление о задаче #{task_id}: {status_label}")

    return {"status": "ok"}
