"""
Задача: Мониторинг стоп-листа iiko → алерт в Telegram.
Расписание: каждые 5 минут.

Логика:
1. Для каждого города получаем текущий стоп-лист из iiko
2. Сравниваем с предыдущим (хэш в SQLite)
3. Если изменился — отправляем алерт в Telegram
4. Дедупликация: один и тот же стоп-лист не отправляем дважды
"""

import asyncio
import logging
from datetime import datetime

from app.clients import iiko, telegram
from app.config import get_settings
from app.database import (
    hash_stoplist,
    get_stoplist_hash,
    set_stoplist_hash,
    log_job_start,
    log_job_finish,
)

logger = logging.getLogger(__name__)
settings = get_settings()

# Задержка между запросами к разным городам (rate limit iiko)
CITY_DELAY_SECONDS = 1.0


def _make_id_set(items: list[dict]) -> set[str]:
    return {item["productId"] for item in items}


async def _check_city(city: str) -> None:
    """Проверяет стоп-лист одного города и отправляет алерт при изменении."""
    try:
        items = await iiko.get_stop_list(city)
    except Exception as e:
        logger.error(f"Ошибка получения стоп-листа [{city}]: {e}")
        await telegram.error_alert(f"iiko стоп-лист [{city}]", str(e))
        return

    current_hash = hash_stoplist(items)
    previous_hash = await get_stoplist_hash(city)

    if current_hash == previous_hash:
        logger.debug(f"Стоп-лист [{city}] не изменился")
        return

    now_str = datetime.now().strftime("%H:%M")

    # Первый запуск — сохраняем и сообщаем только счётчик
    if previous_hash is None:
        await set_stoplist_hash(city, current_hash)
        logger.info(f"Первая проверка стоп-листа [{city}], позиций: {len(items)}")
        count = len(items)
        icon = "🛑" if count > 0 else "✅"
        msg = (
            f"{icon} <b>{city}</b> — мониторинг запущен\n"
            f"В стоп-листе сейчас: <b>{count} позиций</b>\n"
            f"<i>Следующие алерты — только при изменениях</i>"
        )
        await telegram.alert(msg)
        return

    # Загружаем старое состояние для сравнения
    # (используем текущие items как "после", пересчитываем "до" не можем —
    #  поэтому шлём diff по факту: сообщаем новый размер и изменение)
    await set_stoplist_hash(city, current_hash)
    count = len(items)
    logger.info(f"Стоп-лист [{city}] изменился, позиций теперь: {count}")

    if count == 0:
        msg = f"✅ <b>{city}</b> — стоп-лист очищен ({now_str})"
    else:
        lines = [f"🛑 <b>Стоп-лист {city}</b> изменился — {count} поз. ({now_str})\n"]
        # Показываем максимум 15 позиций чтобы не спамить
        for item in items[:15]:
            balance = item.get("balance")
            bal_str = f" [{balance:.0f} шт.]" if balance and balance != 0 else ""
            lines.append(f"• {item['name']}{bal_str}")
        if count > 15:
            lines.append(f"<i>...и ещё {count - 15} позиций</i>")
        msg = "\n".join(lines)

    await telegram.alert(msg)


async def job_check_stoplist() -> None:
    """Главная задача: проверяет все города последовательно."""
    log_id = await log_job_start("iiko_stoplist")
    cities = settings.iiko_cities

    if not cities:
        logger.warning("Нет городов в IIKO_ORG_IDS — стоп-лист не проверяется")
        await log_job_finish(log_id, "error", "Нет городов в конфигурации")
        return

    errors = []
    for i, city in enumerate(cities):
        try:
            await _check_city(city)
        except Exception as e:
            logger.error(f"Критическая ошибка стоп-листа [{city}]: {e}")
            errors.append(f"{city}: {e}")
        # Пауза между городами чтобы не нарваться на rate limit
        if i < len(cities) - 1:
            await asyncio.sleep(CITY_DELAY_SECONDS)

    if errors:
        await log_job_finish(log_id, "error", "; ".join(errors))
    else:
        await log_job_finish(log_id, "ok")
