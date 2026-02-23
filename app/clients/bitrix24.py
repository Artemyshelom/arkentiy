"""
Битрикс24 REST API клиент.
Документация: https://dev.1c-bitrix.ru/rest_help/

Используем входящий вебхук (не OAuth) — проще для небольшого числа операций.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

REQUEST_TIMEOUT = 20.0

# Статусы задач в Битрикс24
TASK_STATUS = {
    "1": "Не начата",
    "2": "В работе",
    "3": "Выполнена",
    "4": "Отложена",
    "5": "Просрочена",
    "6": "Завершена",
}


def _url(method: str) -> str:
    base = settings.bitrix24_incoming_webhook.rstrip("/")
    return f"{base}/{method}.json"


async def _call(method: str, params: dict = None, retry: int = 3) -> dict:
    """Вызов метода Битрикс24 REST API."""
    for attempt in range(retry):
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                response = await client.post(_url(method), json=params or {})
                response.raise_for_status()
                data = response.json()
                if "error" in data:
                    logger.error(f"Битрикс24 ошибка [{method}]: {data}")
                    return {}
                return data.get("result", {})
        except httpx.TimeoutException:
            wait = 2 ** attempt
            logger.warning(f"Битрикс24 timeout [{method}], попытка {attempt+1}, жду {wait}с")
            await asyncio.sleep(wait)
        except Exception as e:
            logger.error(f"Битрикс24 ошибка [{method}]: {e}")
            return {}
    return {}


# --- Задачи ---

async def create_task(
    title: str,
    responsible_id: int,
    deadline: str | None = None,
    description: str = "",
    tags: list[str] | None = None,
    group_id: int | None = None,
) -> int | None:
    """
    Создаёт задачу в Битрикс24.
    responsible_id: ID пользователя (узнать через users.get)
    deadline: строка "2026-02-25T18:00:00+07:00"
    Возвращает ID задачи.
    """
    fields = {
        "TITLE": title,
        "RESPONSIBLE_ID": responsible_id,
        "DESCRIPTION": description,
    }
    if deadline:
        fields["DEADLINE"] = deadline
    if tags:
        fields["TAGS"] = tags
    if group_id:
        fields["GROUP_ID"] = group_id

    result = await _call("tasks.task.add", {"fields": fields})
    task_id = result.get("task", {}).get("id")
    if task_id:
        logger.info(f"Создана задача #{task_id}: {title}")
    return task_id


async def get_tasks(
    responsible_id: int | None = None,
    group_id: int | None = None,
    only_overdue: bool = False,
    limit: int = 50,
) -> list[dict]:
    """Список задач с фильтрацией."""
    filter_params = {"!STATUS": "6"}  # исключаем завершённые

    if responsible_id:
        filter_params["RESPONSIBLE_ID"] = responsible_id
    if group_id:
        filter_params["GROUP_ID"] = group_id
    if only_overdue:
        filter_params["<=DEADLINE"] = datetime.now(timezone.utc).isoformat()
        filter_params["!STATUS"] = ["3", "6"]  # не выполнена и не завершена

    result = await _call("tasks.task.list", {
        "filter": filter_params,
        "select": ["ID", "TITLE", "STATUS", "RESPONSIBLE_ID", "DEADLINE", "DESCRIPTION", "TAGS"],
        "limit": limit,
    })
    tasks = result if isinstance(result, list) else result.get("tasks", [])

    parsed = []
    for t in tasks:
        status_code = str(t.get("status", "1"))
        parsed.append({
            "id": t.get("id"),
            "title": t.get("title"),
            "status": TASK_STATUS.get(status_code, status_code),
            "status_code": status_code,
            "responsible_id": t.get("responsibleId"),
            "deadline": t.get("deadline"),
            "tags": t.get("tags", []),
        })
    return parsed


async def get_overdue_tasks(responsible_id: int | None = None) -> list[dict]:
    """Просроченные задачи."""
    return await get_tasks(responsible_id=responsible_id, only_overdue=True)


async def update_task_status(task_id: int, status: str) -> bool:
    """Обновить статус задачи. status: 'complete', 'renew' и т.д."""
    result = await _call(f"tasks.task.{status}", {"taskId": task_id})
    return bool(result)


async def get_users() -> list[dict]:
    """Список пользователей Битрикс24 (для маппинга имён на ID)."""
    result = await _call("user.get", {"filter": {"ACTIVE": True}})
    users = result if isinstance(result, list) else []
    return [{"id": u.get("ID"), "name": f"{u.get('NAME', '')} {u.get('LAST_NAME', '')}".strip()} for u in users]


async def get_calendar_events(
    calendar_id: str = "user",
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
) -> list[dict]:
    """События из командного календаря Битрикс24."""
    now = datetime.now(timezone.utc)
    params = {
        "type": calendar_id,
        "from": (from_dt or now).isoformat(),
        "to": (to_dt or (now + timedelta(hours=2))).isoformat(),
    }
    result = await _call("calendar.event.get", params)
    events = result if isinstance(result, list) else []
    return [
        {
            "id": e.get("ID"),
            "name": e.get("NAME"),
            "date_from": e.get("DATE_FROM"),
            "attendees": e.get("ATTENDEES", []),
        }
        for e in events
    ]
