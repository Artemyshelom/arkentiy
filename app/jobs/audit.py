"""
audit.py — Аудитор опасных операций.

Запускается ежедневно в 05:30 МСК (09:30 по UTC+7).
Анализирует данные вчерашнего дня из orders_raw:
  1. Аномально быстрые доставки (< 10 мин от создания до доставки)
  2. Отменённые заказы с суммой > 500₽
  3. Отменённые заказы с указанной причиной и суммой > 200₽

Phase A1: тест BO API /api/v2/cashShifts (сторно и изъятия).
Если эндпоинт отвечает — данные логируются для дальнейшего парсинга.

Команда /аудит [город|точка] [дата] — читает из audit_events в БД.
Подключается к чату через модуль "audit" в /доступ.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import aiosqlite
import httpx

from app.config import get_settings
from app.database import (
    DB_PATH,
    clear_audit_events,
    get_audit_events,
    get_module_chats_for_city,
    save_audit_events_batch,
)
from app.clients import telegram as tg

logger = logging.getLogger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# Настройки порогов
# ---------------------------------------------------------------------------

FAST_DELIVERY_MIN = 10       # доставка менее N минут = подозрительно
CANCEL_HIGH_SUM = 500        # отмена ≥ N₽ без причины = warning
CANCEL_WITH_REASON_SUM = 200 # отмена ≥ N₽ с указанной причиной = warning

# ---------------------------------------------------------------------------
# Авторизация BO API (token-based, как в iiko_bo_events.py)
# ---------------------------------------------------------------------------

_bo_tokens: dict[str, tuple[str, float]] = {}
TOKEN_TTL = 900  # 15 минут


async def _get_bo_token(bo_url: str) -> str:
    """Получает (или возвращает кешированный) токен iiko BO."""
    cached = _bo_tokens.get(bo_url)
    if cached and (time.time() - cached[1]) < TOKEN_TTL:
        return cached[0]

    login = settings.iiko_bo_login
    pwd_hash = hashlib.sha1(settings.iiko_bo_password.encode()).hexdigest()
    url = f"https://{bo_url}/api/auth?login={login}&pass={pwd_hash}"

    async with httpx.AsyncClient(verify=False, timeout=15) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        token = resp.text.strip()

    _bo_tokens[bo_url] = (token, time.time())
    return token


# ---------------------------------------------------------------------------
# Phase A: Детекция из orders_raw
# ---------------------------------------------------------------------------

async def _detect_from_orders_raw(date_str: str) -> list[dict]:
    """
    Ищет подозрительные заказы в orders_raw за указанную дату.
    Возвращает список событий (без поля date/city — добавляется снаружи).
    """
    findings: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # 1. Аномально быстрые доставки
        async with db.execute(
            """
            SELECT branch_name, delivery_num, sum, opened_at, actual_time,
                   courier, client_name, client_phone
            FROM orders_raw
            WHERE date = ?
              AND is_self_service = 0
              AND status IN ('Доставлена', 'Закрыта')
              AND opened_at IS NOT NULL AND opened_at != ''
              AND actual_time IS NOT NULL AND actual_time != ''
            """,
            (date_str,),
        ) as cur:
            for r in await cur.fetchall():
                try:
                    opened = datetime.fromisoformat(r["opened_at"].replace("Z", "+00:00"))
                    actual = datetime.fromisoformat(r["actual_time"].replace("Z", "+00:00"))
                    delta_min = (actual - opened).total_seconds() / 60
                    if 0 < delta_min < FAST_DELIVERY_MIN:
                        courier = r["courier"] or ""
                        courier_str = f", курьер: {courier}" if courier else ""
                        sum_val = int(r["sum"] or 0)
                        findings.append({
                            "branch_name": r["branch_name"],
                            "event_type": "fast_delivery",
                            "severity": "critical" if delta_min < 3 else "warning",
                            "description": (
                                f"Д-{r['delivery_num']} — доставка за {delta_min:.0f} мин "
                                f"({r['opened_at'][11:16]}→{r['actual_time'][11:16]})"
                                f"{courier_str}, {sum_val:,}₽".replace(",", "\u00a0")
                            ),
                            "meta_json": json.dumps({
                                "delivery_num": r["delivery_num"],
                                "sum": r["sum"],
                                "delta_min": round(delta_min, 1),
                                "courier": r["courier"],
                                "client_name": r["client_name"],
                                "opened_at": r["opened_at"],
                                "actual_time": r["actual_time"],
                            }, ensure_ascii=False),
                            "created_at": now_iso,
                        })
                except Exception as e:
                    logger.debug(f"[audit] Ошибка парсинга fast_delivery {r['delivery_num']}: {e}")

        # 2. Отменённые заказы с суммой
        async with db.execute(
            """
            SELECT branch_name, delivery_num, sum, cancel_reason,
                   opened_at, client_name, client_phone
            FROM orders_raw
            WHERE date = ?
              AND status = 'Отменена'
              AND sum IS NOT NULL
            ORDER BY sum DESC
            """,
            (date_str,),
        ) as cur:
            for r in await cur.fetchall():
                s = float(r["sum"] or 0)
                reason = (r["cancel_reason"] or "").strip()

                if s >= CANCEL_HIGH_SUM or (reason and s >= CANCEL_WITH_REASON_SUM):
                    reason_str = f", причина: «{reason}»" if reason else ""
                    client_name = r["client_name"] or ""
                    client_str = f", клиент: {client_name}" if client_name else ""
                    sum_int = int(s)
                    findings.append({
                        "branch_name": r["branch_name"],
                        "event_type": "cancellation_with_reason" if reason else "cancellation",
                        "severity": "critical" if s >= 1000 else "warning",
                        "description": (
                            f"Д-{r['delivery_num']} — {sum_int:,}₽ отменён"
                            f"{reason_str}{client_str}"
                        ).replace(",", "\u00a0"),
                        "meta_json": json.dumps({
                            "delivery_num": r["delivery_num"],
                            "sum": s,
                            "cancel_reason": reason,
                            "opened_at": r["opened_at"],
                            "client_name": r["client_name"],
                            "client_phone": r["client_phone"],
                        }, ensure_ascii=False),
                        "created_at": now_iso,
                    })

    return findings


# ---------------------------------------------------------------------------
# Phase A1: Разведка BO API /api/v2/cashShifts
# ---------------------------------------------------------------------------

async def _probe_cash_shifts(branch: dict, date_str: str) -> None:
    """
    Пробует получить кассовые смены через BO API.
    На этапе A1 только логирует ответ — парсинг добавим после изучения структуры.
    """
    bo_url = branch.get("bo_url", "")
    if not bo_url:
        return

    d = datetime.strptime(date_str, "%Y-%m-%d")
    date_bo = d.strftime("%d.%m.%Y")
    base = f"https://{bo_url}"

    try:
        token = await _get_bo_token(bo_url)
        async with httpx.AsyncClient(verify=False, timeout=20) as client:
            resp = await client.get(
                f"{base}/api/v2/cashShifts",
                params={"key": token, "dateFrom": date_bo, "dateTo": date_bo},
            )

        if resp.status_code == 200:
            logger.info(
                f"[audit] cashShifts OK для {branch['name']} ({len(resp.text)} байт). "
                f"Начало ответа: {resp.text[:300]}"
            )
        elif resp.status_code == 404:
            logger.debug(f"[audit] cashShifts 404 для {branch['name']} — эндпоинт недоступен")
        else:
            logger.warning(
                f"[audit] cashShifts {resp.status_code} для {branch['name']}: {resp.text[:200]}"
            )
    except Exception as e:
        logger.debug(f"[audit] cashShifts ошибка для {branch['name']}: {e}")


# ---------------------------------------------------------------------------
# Форматирование отчёта
# ---------------------------------------------------------------------------

_MONTH_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def _date_label(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{d.day} {_MONTH_RU[d.month]} {d.year}"


def _format_report(date_str: str, city: str, events: list[dict]) -> str:
    """Форматирует аудит-отчёт в HTML для Telegram."""
    fast = [e for e in events if e["event_type"] == "fast_delivery"]
    cancelled = [e for e in events if e["event_type"] in ("cancellation", "cancellation_with_reason")]

    lines = [
        f"🔍 <b>Аудит [{html.escape(city)}] — {_date_label(date_str)}</b>",
        "",
    ]

    if fast:
        lines.append(f"⚡ <b>Аномально быстрые доставки ({len(fast)})</b>")
        for e in fast:
            icon = "🔴" if e["severity"] == "critical" else "🟡"
            lines.append(f"{icon} {html.escape(e['description'])}")
        lines.append("")

    if cancelled:
        lines.append(f"❌ <b>Отмены с суммой ({len(cancelled)})</b>")
        for e in cancelled:
            icon = "🔴" if e["severity"] == "critical" else "🟡"
            lines.append(f"{icon} {html.escape(e['description'])}")
        lines.append("")

    if not fast and not cancelled:
        lines.append("✅ Подозрительных операций не выявлено")
    else:
        lines.append(f"<i>Итого: {len(fast) + len(cancelled)} событий</i>")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Основной job
# ---------------------------------------------------------------------------

async def job_audit_report(utc_offset: int = 7) -> None:
    """Ежедневный аудит-отчёт. Запускается в 05:30 МСК (= 09:30 UTC+7)."""
    local_now = datetime.now(timezone.utc) + timedelta(hours=utc_offset)
    yesterday = (local_now - timedelta(days=1)).date()
    date_str = yesterday.isoformat()

    logger.info(f"[audit] Запуск аудита за {date_str}")

    branches = settings.branches or []
    if not branches:
        logger.warning("[audit] Нет точек в branches.json — пропускаю")
        return

    # Очищаем старые записи за эту дату (на случай повторного запуска)
    await clear_audit_events(date_str)

    # Детектируем подозрительные операции из orders_raw
    all_findings = await _detect_from_orders_raw(date_str)

    # Phase A1: разведка cash shifts API (только логирование)
    for branch in branches:
        try:
            await _probe_cash_shifts(branch, date_str)
        except Exception as e:
            logger.debug(f"[audit] probe_cash_shifts exception {branch.get('name')}: {e}")

    # Обогащаем events полями date и city из branches
    branch_to_city = {b["name"]: b.get("city", "") for b in branches}
    for f in all_findings:
        f["date"] = date_str
        f["city"] = branch_to_city.get(f["branch_name"], "")

    # Сохраняем в БД
    if all_findings:
        await save_audit_events_batch(all_findings)
        logger.info(f"[audit] Сохранено {len(all_findings)} событий за {date_str}")
    else:
        logger.info(f"[audit] Подозрительных событий не найдено за {date_str}")

    # Отправляем отчёты в подписанные чаты по городам
    cities = sorted({b.get("city", "") for b in branches if b.get("city")})
    for city in cities:
        chat_ids = await get_module_chats_for_city("audit", city)
        if not chat_ids:
            continue
        city_events = [e for e in all_findings if e.get("city") == city]
        report_text = _format_report(date_str, city, city_events)
        for chat_id in chat_ids:
            try:
                await tg.send_message(str(chat_id), report_text)
            except Exception as e:
                logger.error(f"[audit] Ошибка отправки в {chat_id}: {e}")

    logger.info(f"[audit] Аудит завершён за {date_str}")


# ---------------------------------------------------------------------------
# Обработчик команды /аудит
# ---------------------------------------------------------------------------

async def handle_audit_command(chat_id: int, arg: str, city_filter=None) -> None:
    """
    /аудит [фильтр] [дата]
    Примеры:
      /аудит                → вчера, все города по city_filter чата
      /аудит Томск          → вчера, Томск
      /аудит Томск 22.02    → конкретная дата
      /аудит Томск_1 Яко    → конкретная точка
    """
    from app.clients.telegram import send_message

    parts = arg.split() if arg else []

    # Парсим дату (последний аргумент в формате DD.MM или DD.MM.YYYY)
    date_str: Optional[str] = None
    filter_parts: list[str] = []
    for part in parts:
        if "." in part and any(c.isdigit() for c in part):
            try:
                chunks = part.split(".")
                if len(chunks) == 2:
                    day, mon = int(chunks[0]), int(chunks[1])
                    date_str = date(datetime.now().year, mon, day).isoformat()
                elif len(chunks) == 3:
                    day, mon, yr = int(chunks[0]), int(chunks[1]), int(chunks[2])
                    date_str = date(yr, mon, day).isoformat()
            except Exception:
                filter_parts.append(part)
        else:
            filter_parts.append(part)

    if date_str is None:
        # По умолчанию — вчера по UTC+7
        utc_offset = 7
        local_now = datetime.now(timezone.utc) + timedelta(hours=utc_offset)
        date_str = (local_now - timedelta(days=1)).date().isoformat()

    # Парсим фильтр по городу/точке
    filter_text = " ".join(filter_parts).strip()
    branch_filter: Optional[str] = None
    city_query: Optional[str] = None

    branches = settings.branches or []
    branch_names = [b["name"] for b in branches]
    cities = list({b.get("city", "") for b in branches if b.get("city")})

    if filter_text:
        # Ищем точное совпадение с точкой
        matched_branch = next(
            (b for b in branch_names if filter_text.lower() in b.lower()), None
        )
        if matched_branch:
            branch_filter = matched_branch
        else:
            # Ищем совпадение с городом
            matched_city = next(
                (c for c in cities if filter_text.lower() in c.lower()), None
            )
            city_query = matched_city or filter_text

    # Если фильтр чата задан (frozenset или строка), используем его
    if city_query is None and city_filter is not None:
        if isinstance(city_filter, frozenset):
            # Берём первый город из фильтра (если один — точно, если несколько — первый)
            city_query = next(iter(city_filter), None)
        elif isinstance(city_filter, str):
            city_query = city_filter

    # Запрашиваем из БД
    events = await get_audit_events(date_str, city=city_query, branch_name=branch_filter)

    scope_label = branch_filter or city_query or "все города"
    if not events:
        await send_message(
            str(chat_id),
            f"🔍 <b>Аудит [{html.escape(scope_label)}] — {_date_label(date_str)}</b>\n\n"
            "✅ Подозрительных операций не найдено\n"
            "<i>(или аудит ещё не запускался за эту дату)</i>",
        )
        return

    # Группируем по городам для вывода
    if branch_filter or city_query:
        text = _format_report(date_str, scope_label, events)
        await send_message(str(chat_id), text)
    else:
        # Несколько городов — отправляем по одному блоку на город
        by_city: dict[str, list[dict]] = {}
        for e in events:
            by_city.setdefault(e["city"], []).append(e)
        for city_name, city_events in sorted(by_city.items()):
            text = _format_report(date_str, city_name, city_events)
            await send_message(str(chat_id), text)
