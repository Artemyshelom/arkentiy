"""
Еженедельный мониторинг цен конкурентов.

Расписание: каждое воскресенье в 10:00 МСК.
Логика: scrape → diff с предыдущим снапшотом → уведомление в Telegram.

Уведомляет только при наличии изменений.
Отправляет через отдельный бот (COMPETITOR_BOT_TOKEN) в личку Артемию.
"""

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from app.clients.competitor_scraper import MenuItem, scrape_competitor
from app.config import get_settings
from app.db import (
    create_competitor_snapshot,
    get_second_last_competitor_items,
    log_job_finish,
    log_job_start,
    save_competitor_items,
)
from app.utils.job_tracker import track_job

logger = logging.getLogger(__name__)
settings = get_settings()


# ---------------------------------------------------------------------------
# Telegram helpers (отдельный бот для конкурентов)
# ---------------------------------------------------------------------------

async def _send_tg(text: str) -> bool:
    """Отправляет сообщение через бота конкурент-монитора."""
    token = settings.competitor_bot_token
    chat_id = settings.competitor_notify_chat
    if not token or not chat_id:
        logger.warning("[Конкуренты] COMPETITOR_BOT_TOKEN или COMPETITOR_NOTIFY_CHAT не задан — уведомление пропущено")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                })
                data = resp.json()
                if data.get("ok"):
                    return True
                if resp.status_code == 429:
                    wait = data.get("parameters", {}).get("retry_after", 5)
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"[Конкуренты TG] Ошибка: {data}")
                return False
        except Exception as e:
            wait = 2 ** attempt
            logger.warning(f"[Конкуренты TG] Попытка {attempt + 1} не удалась: {e}, жду {wait}с")
            await asyncio.sleep(wait)
    return False


# ---------------------------------------------------------------------------
# Diff engine
# ---------------------------------------------------------------------------

def _diff_items(
    old: list[dict],
    new: list[MenuItem],
) -> dict:
    """
    Сравнивает два снапшота. Возвращает словарь с изменениями:
      - price_up:   подорожавшие  [{name, old_price, new_price, pct}]
      - price_down: подешевевшие  [{name, old_price, new_price, pct}]
      - added:      новые позиции [{name, price}]
      - removed:    убранные      [{name, price}]
    """
    old_map = {item["name"]: item["price"] for item in old}
    new_map = {item.name: item.price for item in new}

    price_up = []
    price_down = []
    added = []
    removed = []

    for name, new_price in new_map.items():
        if name in old_map:
            old_price = old_map[name]
            if abs(new_price - old_price) > 0.5:  # Порог: 50 копеек
                pct = (new_price - old_price) / old_price * 100
                entry = {"name": name, "old_price": old_price, "new_price": new_price, "pct": pct}
                (price_up if pct > 0 else price_down).append(entry)
        else:
            added.append({"name": name, "price": new_price})

    for name, old_price in old_map.items():
        if name not in new_map:
            removed.append({"name": name, "price": old_price})

    return {
        "price_up": sorted(price_up, key=lambda x: -x["pct"]),
        "price_down": sorted(price_down, key=lambda x: x["pct"]),
        "added": added,
        "removed": removed,
    }


def _has_changes(diff: dict) -> bool:
    return any(diff[k] for k in ("price_up", "price_down", "added", "removed"))


def _avg_price(items: list) -> float | None:
    """Средняя цена по списку."""
    if not items:
        return None
    prices = [item["price"] if isinstance(item, dict) else item.price for item in items]
    return sum(prices) / len(prices)


# ---------------------------------------------------------------------------
# Message formatter
# ---------------------------------------------------------------------------

def _format_report(city: str, competitor_name: str, diff: dict, old_items: list[dict], new_items: list[MenuItem]) -> str:
    date_str = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    lines = [f"📊 <b>{competitor_name}</b> ({city}) — изменения {date_str}\n"]

    if diff["price_up"]:
        lines.append(f"📈 <b>Подорожало ({len(diff['price_up'])}):</b>")
        for item in diff["price_up"][:15]:
            lines.append(
                f"  • {item['name']}: {item['old_price']:.0f} → {item['new_price']:.0f} р "
                f"(<b>+{item['pct']:.1f}%</b>)"
            )
        if len(diff["price_up"]) > 15:
            lines.append(f"  <i>...и ещё {len(diff['price_up']) - 15} позиций</i>")

    if diff["price_down"]:
        lines.append(f"\n📉 <b>Подешевело ({len(diff['price_down'])}):</b>")
        for item in diff["price_down"][:15]:
            lines.append(
                f"  • {item['name']}: {item['old_price']:.0f} → {item['new_price']:.0f} р "
                f"({item['pct']:.1f}%)"
            )
        if len(diff["price_down"]) > 15:
            lines.append(f"  <i>...и ещё {len(diff['price_down']) - 15} позиций</i>")

    if diff["added"]:
        names = ", ".join(f"{i['name']} ({i['price']:.0f} р)" for i in diff["added"][:10])
        suffix = f" и ещё {len(diff['added']) - 10}" if len(diff["added"]) > 10 else ""
        lines.append(f"\n🆕 <b>Новые позиции ({len(diff['added'])}):</b> {names}{suffix}")

    if diff["removed"]:
        names = ", ".join(f"{i['name']}" for i in diff["removed"][:10])
        suffix = f" и ещё {len(diff['removed']) - 10}" if len(diff["removed"]) > 10 else ""
        lines.append(f"\n❌ <b>Убрали ({len(diff['removed'])}):</b> {names}{suffix}")

    # Средняя цена
    old_avg = _avg_price(old_items)
    new_avg = _avg_price(new_items)
    if old_avg and new_avg:
        avg_pct = (new_avg - old_avg) / old_avg * 100
        sign = "+" if avg_pct >= 0 else ""
        lines.append(
            f"\n📊 <b>Средняя цена:</b> {old_avg:.0f} р → {new_avg:.0f} р "
            f"({sign}{avg_pct:.1f}%)"
        )
    elif new_avg:
        lines.append(f"\n📊 <b>Средняя цена:</b> {new_avg:.0f} р")

    lines.append(f"\n<i>Позиций в меню: {len(new_items)}</i>")
    return "\n".join(lines)


def _format_first_snapshot(city: str, competitor_name: str, items: list[MenuItem]) -> str:
    """Отчёт при первом запуске (нет предыдущего снапшота для сравнения)."""
    date_str = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    avg = _avg_price(items)
    avg_str = f"{avg:.0f} р" if avg else "—"
    return (
        f"📋 <b>{competitor_name}</b> ({city}) — первый снапшот {date_str}\n"
        f"Позиций собрано: <b>{len(items)}</b>\n"
        f"Средняя цена: <b>{avg_str}</b>\n"
        f"<i>Со следующей недели — дифф и уведомления при изменениях.</i>"
    )


def _format_error(city: str, competitor_name: str, url: str) -> str:
    date_str = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    return (
        f"⚠️ <b>{competitor_name}</b> ({city}) — не удалось спарсить {date_str}\n"
        f"URL: <code>{url}</code>\n"
        f"<i>Проверь лог контейнера.</i>"
    )


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

@track_job("competitor_monitor")
async def job_monitor_competitors() -> None:
    """Еженедельный обход сайтов конкурентов."""
    log_id = await log_job_start("competitor_monitor", tenant_id=1)
    logger.info("[Конкуренты] Запуск еженедельного мониторинга")

    competitors_by_city = settings.competitors
    if not competitors_by_city:
        logger.warning("[Конкуренты] competitors.json пуст или не найден")
        await log_job_finish(log_id, "warning", error="competitors.json пуст")
        return

    total_processed = 0
    total_changes = 0
    errors = []

    for city, competitors in competitors_by_city.items():
        for comp in competitors:
            if not comp.get("active", True):
                continue
            name = comp["name"]
            url = comp["url"]

            logger.info(f"[Конкуренты] {city} / {name}")
            try:
                new_items = await scrape_competitor(comp)

                if not new_items:
                    logger.warning(f"[Конкуренты] {name}: 0 позиций — возможно, сайт изменился")
                    snapshot_id = await create_competitor_snapshot(
                        city, name, url, 1, status="error",
                        error_msg="0 позиций после парсинга",
                    )
                    await _send_tg(_format_error(city, name, url))
                    errors.append(name)
                    continue

                # Сохраняем снапшот
                snapshot_id = await create_competitor_snapshot(
                    city, name, url, 1, status="ok", items_count=len(new_items),
                )
                await save_competitor_items(
                    snapshot_id, city, name,
                    [item.to_dict() for item in new_items],
                    tenant_id=1,
                )

                # Дифф с предыдущим снапшотом
                old_items = await get_second_last_competitor_items(city, name, tenant_id=1)

                if not old_items:
                    # Первый запуск — просто уведомляем
                    await _send_tg(_format_first_snapshot(city, name, new_items))
                    logger.info(f"[Конкуренты] {name}: первый снапшот ({len(new_items)} позиций)")
                else:
                    diff = _diff_items(old_items, new_items)
                    if _has_changes(diff):
                        msg = _format_report(city, name, diff, old_items, new_items)
                        await _send_tg(msg)
                        total_changes += 1
                        logger.info(
                            f"[Конкуренты] {name}: изменения найдены — "
                            f"+{len(diff['price_up'])} ↑, {len(diff['price_down'])} ↓, "
                            f"+{len(diff['added'])} новых, -{len(diff['removed'])} убрано"
                        )
                    else:
                        logger.info(f"[Конкуренты] {name}: изменений нет")

                total_processed += 1

            except Exception as e:
                logger.error(f"[Конкуренты] Ошибка для {name}: {e}", exc_info=True)
                errors.append(name)
                try:
                    await create_competitor_snapshot(
                        city, name, url, 1, status="error", error_msg=str(e)[:500],
                    )
                except Exception:
                    pass

            # Пауза между сайтами, чтобы не перегружать VPS
            await asyncio.sleep(5)

    status = "ok" if not errors else ("warning" if total_processed > 0 else "error")
    details = f"Обработано: {total_processed}, с изменениями: {total_changes}, ошибок: {len(errors)}"
    if errors:
        details += f" ({', '.join(errors)})"

    await log_job_finish(log_id, status, details=details)
    logger.info(f"[Конкуренты] Завершено. {details}")

    # Экспорт в Google Sheets после скрапинга
    try:
        from app.jobs.competitor_sheets import export_all_competitors_to_sheets
        await export_all_competitors_to_sheets()
    except Exception as e:
        logger.error(f"[Конкуренты] Ошибка Sheets-экспорта: {e}", exc_info=True)
