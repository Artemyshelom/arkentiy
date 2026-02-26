"""
Задача: Мониторинг просроченных задач Битрикс24 → Telegram.
Расписание: ежедневно в 09:00.

Отправляет в #отчёты список задач, у которых просрочен дедлайн.
Помогает Артемию контролировать выполнение договорённостей с встреч.
"""

import logging
from datetime import datetime, timezone

from app.clients import telegram
from app.clients.bitrix24 import get_overdue_tasks
from app.db import log_job_start, log_job_finish

logger = logging.getLogger(__name__)


async def job_check_overdue_tasks() -> None:
    """Получает просроченные задачи и отправляет сводку."""
    log_id = await log_job_start("task_tracker")

    try:
        tasks = await get_overdue_tasks()
    except Exception as e:
        logger.error(f"Ошибка получения задач Битрикс24: {e}")
        await telegram.error_alert("task_tracker", str(e))
        await log_job_finish(log_id, "error", str(e))
        return

    if not tasks:
        logger.info("Просроченных задач нет")
        await log_job_finish(log_id, "ok", "Просроченных задач нет")
        return

    today = datetime.now().strftime("%d.%m.%Y")
    lines = [f"🔴 <b>Просроченные задачи на {today}</b> ({len(tasks)} шт.)\n"]

    for task in tasks:
        deadline = task.get("deadline", "")
        deadline_str = f" (дедлайн: {deadline[:10]})" if deadline else ""
        tags = ", ".join(task.get("tags", []))
        tag_str = f" [{tags}]" if tags else ""
        lines.append(f"• {task['title']}{deadline_str}{tag_str}")

    lines.append("\n<i>Откройте Битрикс24 для работы с задачами</i>")

    await telegram.report("\n".join(lines))
    await log_job_finish(log_id, "ok", f"Просроченных задач: {len(tasks)}")
