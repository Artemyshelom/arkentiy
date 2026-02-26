"""
audit.py — Аудитор опасных операций.

Запускается ежедневно в 05:30 МСК (09:30 по UTC+7).
Анализирует данные вчерашнего дня:

orders_raw:
  1. fast_delivery    — доставка закрыта < FAST_DELIVERY_MIN мин после создания
  2. cancellation     — отменённый заказ ≥ CANCEL_HIGH_SUM₽
  3. early_closure    — заказ закрыт на EARLY_CLOSURE_MIN+ мин раньше плана
  4. unclosed_in_transit — заказ «В пути» из прошлых дней

OLAP v2:
  5. storno_discount  — сторно чека + ручная скидка (схема кражи)

Команда /аудит [город|точка] [дата] — читает из audit_events в БД.
Подключается к чату через модуль "audit" в /доступ.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import aiosqlite
import httpx

from app.config import get_settings
from app.db import (
    DB_PATH,
    BACKEND,
    clear_audit_events,
    get_audit_events,
    get_module_chats_for_city,
    save_audit_events_batch,
)
from app.clients import telegram as tg
from app.clients.iiko_auth import get_bo_token

logger = logging.getLogger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# Настройки порогов
# ---------------------------------------------------------------------------

FAST_DELIVERY_MIN = 15             # доставка за N мин от создания = подозрительно
CANCEL_HIGH_SUM = 500              # отмена ≥ N₽ без причины = warning
CANCEL_WITH_REASON_SUM = 200       # отмена ≥ N₽ с указанной причиной = warning
EARLY_CLOSURE_MIN = 60             # закрыт на N+ мин раньше плана = подозрительно


# ---------------------------------------------------------------------------
# Phase A: Детекция из orders_raw
# ---------------------------------------------------------------------------

async def _detect_from_orders_raw(date_str: str) -> list[dict]:
    """
    Ищет подозрительные заказы в orders_raw за указанную дату.

    Детекторы:
    - fast_delivery:       доставка закрыта < FAST_DELIVERY_MIN мин после создания
    - cancellation:        отменённый заказ с суммой ≥ порога
    - early_closure:       заказ закрыт на EARLY_CLOSURE_MIN+ мин раньше плана

    unclosed_in_transit вынесен в _detect_unclosed_in_transit() — генерируется живо.
    Возвращает список событий (без полей date/city — добавляются снаружи).
    """
    findings: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    if BACKEND != "sqlite":
        logger.warning("audit._detect_from_orders_raw: PG backend не реализован, пропуск")
        return findings

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # 1. Аномально быстрые доставки (opened_at → actual_time < FAST_DELIVERY_MIN мин)
        #    Работает только для заказов, созданных после деплоя фикса opened_at
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
                        sum_str = f"{sum_val:,}".replace(",", "\u00a0")
                        o_time = r["opened_at"][11:16]
                        a_time = r["actual_time"][11:16]
                        findings.append({
                            "branch_name": r["branch_name"],
                            "event_type": "fast_delivery",
                            "severity": "critical" if delta_min < 3 else "warning",
                            "description": (
                                f"#{r['delivery_num']} \u2014 доставка за {delta_min:.0f} мин "
                                f"({o_time}\u2192{a_time})"
                                f"{courier_str}, {sum_str}\u20bd"
                            ),
                            "meta_json": json.dumps({
                                "delivery_num": r["delivery_num"],
                                "sum": r["sum"],
                                "delta_min": round(delta_min, 1),
                                "opened_at": r["opened_at"],
                                "actual_time": r["actual_time"],
                                "courier": r["courier"],
                                "client_name": r["client_name"],
                                "client_phone": r["client_phone"],
                            }, ensure_ascii=False),
                            "created_at": now_iso,
                        })
                except Exception as e:
                    logger.debug(f"[audit] Ошибка парсинга fast_delivery {r['delivery_num']}: {e}")

        # 2. Отменённые заказы с суммой
        async with db.execute(
            """
            SELECT branch_name, delivery_num, sum, cancel_reason,
                   client_name, client_phone, payment_type,
                   cooked_time, comment
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
                    sum_int = int(s)
                    sum_str = f"{sum_int:,}".replace(",", "\u00a0")
                    pay_type = (r["payment_type"] or "").strip()
                    pay_label = pay_type if pay_type else "без оплаты"
                    cooked = bool(r["cooked_time"])
                    cooked_label = "готовился" if cooked else "не готовился"
                    reason_label = reason if reason else "без причины"
                    comment = (r["comment"] or "").strip()
                    findings.append({
                        "branch_name": r["branch_name"],
                        "event_type": "cancellation_with_reason" if reason else "cancellation",
                        "severity": "critical" if s >= 1000 else "warning",
                        "description": (
                            f"#{r['delivery_num']} \u2014 {sum_str}\u20bd отменён"
                            f" | {pay_label} | {cooked_label} | {reason_label}"
                        ),
                        "meta_json": json.dumps({
                            "delivery_num": r["delivery_num"],
                            "sum": s,
                            "cancel_reason": reason,
                            "payment_type": pay_type,
                            "cooked": cooked,
                            "comment": comment,
                            "client_name": r["client_name"],
                            "client_phone": r["client_phone"],
                        }, ensure_ascii=False),
                        "created_at": now_iso,
                    })

        # 3. Ранние закрытия: actual_time < planned_time - EARLY_CLOSURE_MIN мин
        async with db.execute(
            """
            SELECT branch_name, delivery_num, sum, planned_time, actual_time,
                   courier, client_name, client_phone
            FROM orders_raw
            WHERE date = ?
              AND is_self_service = 0
              AND status IN ('Доставлена', 'Закрыта')
              AND planned_time IS NOT NULL AND planned_time != ''
              AND actual_time IS NOT NULL AND actual_time != ''
            """,
            (date_str,),
        ) as cur:
            for r in await cur.fetchall():
                try:
                    planned = datetime.fromisoformat(r["planned_time"].replace("Z", "+00:00"))
                    actual = datetime.fromisoformat(r["actual_time"].replace("Z", "+00:00"))
                    early_min = (planned - actual).total_seconds() / 60
                    if early_min >= EARLY_CLOSURE_MIN:
                        courier = r["courier"] or ""
                        courier_str = f", курьер: {courier}" if courier else ""
                        sum_val = int(r["sum"] or 0)
                        sum_str = f"{sum_val:,}".replace(",", "\u00a0")
                        p_time = r["planned_time"][11:16]
                        a_time = r["actual_time"][11:16]
                        findings.append({
                            "branch_name": r["branch_name"],
                            "event_type": "early_closure",
                            "severity": "critical" if early_min >= 90 else "warning",
                            "description": (
                                f"#{r['delivery_num']} \u2014 закрыт на {early_min:.0f} мин раньше плана "
                                f"(план {p_time}, факт {a_time})"
                                f"{courier_str}, {sum_str}\u20bd"
                            ),
                            "meta_json": json.dumps({
                                "delivery_num": r["delivery_num"],
                                "sum": r["sum"],
                                "early_min": round(early_min, 1),
                                "planned_time": r["planned_time"],
                                "actual_time": r["actual_time"],
                                "courier": r["courier"],
                                "client_name": r["client_name"],
                                "client_phone": r["client_phone"],
                            }, ensure_ascii=False),
                            "created_at": now_iso,
                        })
                except Exception as e:
                    logger.debug(f"[audit] Ошибка парсинга early_closure {r['delivery_num']}: {e}")

    return findings


async def _detect_unclosed_in_transit(date_str: str) -> list[dict]:
    """Живая проверка незакрытых заказов 'В пути к клиенту' из прошлых дней."""
    findings: list[dict] = []
    if BACKEND != "sqlite":
        logger.warning("audit._detect_unclosed_in_transit: PG backend не реализован, пропуск")
        return findings
    now_iso = datetime.now(timezone.utc).isoformat()
    branches = settings.branches or []
    branch_to_city = {b["name"]: b.get("city", "") for b in branches}

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT branch_name, delivery_num, sum, planned_time, courier,
                   client_name, client_phone, date as order_date
            FROM orders_raw
            WHERE date < ?
              AND status = 'В пути к клиенту'
              AND is_self_service = 0
            ORDER BY date ASC, sum DESC
            """,
            (date_str,),
        ) as cur:
            for r in await cur.fetchall():
                courier = r["courier"] or ""
                courier_str = f", курьер: {courier}" if courier else ""
                sum_val = int(r["sum"] or 0)
                sum_str = f"{sum_val:,}".replace(",", "\u00a0")
                order_date = r["order_date"]
                findings.append({
                    "branch_name": r["branch_name"],
                    "city": branch_to_city.get(r["branch_name"], ""),
                    "date": date_str,
                    "event_type": "unclosed_in_transit",
                    "severity": "critical",
                    "description": (
                        f"#{r['delivery_num']} ({order_date}) \u2014 заказ не закрыт, "
                        f"статус \u00abВ пути\u00bb{courier_str}, {sum_str}\u20bd"
                    ),
                    "meta_json": json.dumps({
                        "delivery_num": r["delivery_num"],
                        "sum": r["sum"],
                        "order_date": order_date,
                        "planned_time": r["planned_time"],
                        "courier": r["courier"],
                        "client_name": r["client_name"],
                        "client_phone": r["client_phone"],
                    }, ensure_ascii=False),
                    "created_at": now_iso,
                })
    return findings


async def _generate_audit_for_date(date_str: str) -> list[dict]:
    """
    Полная генерация аудит-событий для указанной даты.
    Без unclosed_in_transit (те генерируются живо).
    Включает дедупликацию cancel > storno.
    """
    branches = settings.branches or []
    branch_to_city = {b["name"]: b.get("city", "") for b in branches}

    all_findings = await _detect_from_orders_raw(date_str)

    try:
        storno_findings = await _detect_storno_discount(date_str)
        if storno_findings:
            cancelled_nums = {
                json.loads(f["meta_json"]).get("delivery_num", "")
                for f in all_findings
                if f["event_type"] in ("cancellation", "cancellation_with_reason")
            }
            storno_findings = [
                f for f in storno_findings
                if json.loads(f["meta_json"])["order_num"] not in cancelled_nums
            ]
        all_findings.extend(storno_findings)
    except Exception as e:
        logger.warning(f"[audit] Ошибка детектора сторно: {e}")

    for f in all_findings:
        f["date"] = date_str
        if "city" not in f or not f["city"]:
            f["city"] = branch_to_city.get(f["branch_name"], "")

    return all_findings


async def _detect_storno_discount(date_str: str) -> list[dict]:
    """
    Детектор схемы кражи: сторно чека + ручная скидка.

    Паттерн: администратор сторнирует оплаченный чек, применяет ручную
    скидку, проводит оплату заново — разницу забирает себе.

    Детекция через OLAP v2 per-order: тот же OrderNum имеет строку
    Storned=TRUE и строку Storned=FALSE с пустым OrderDiscount.Type
    и DiscountSum > 0.
    """
    findings: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    d = datetime.strptime(date_str, "%Y-%m-%d")
    date_from = date_str
    date_to = (d + timedelta(days=1)).strftime("%Y-%m-%d")

    branches = settings.branches or []
    branch_to_city = {b["name"]: b.get("city", "") for b in branches}

    by_url: dict[str, set[str]] = defaultdict(set)
    for branch in branches:
        url = branch.get("bo_url", "")
        if url:
            by_url[url].add(branch["name"])

    async def _query_server(bo_url: str, target_names: set[str]) -> list[dict]:
        server_findings: list[dict] = []
        try:
            token = await get_bo_token(bo_url)
        except Exception as e:
            logger.warning(f"[audit] storno_discount: auth error {bo_url}: {e}")
            return server_findings

        body = {
            "reportType": "SALES",
            "buildSummary": "false",
            "groupByRowFields": [
                "Department", "OrderNum", "Storned", "OrderDiscount.Type",
                "OpenTime", "CloseTime", "PayTypes",
            ],
            "aggregateFields": ["DiscountSum", "DishSumInt"],
            "filters": {
                "OpenDate.Typed": {
                    "filterType": "DateRange",
                    "periodType": "CUSTOM",
                    "from": date_from,
                    "to": date_to,
                    "includeLow": "true",
                    "includeHigh": "false",
                }
            },
        }

        try:
            async with httpx.AsyncClient(verify=False, timeout=30) as client:
                resp = await client.post(
                    f"{bo_url}/api/v2/reports/olap?key={token}",
                    json=body,
                    headers={"Content-Type": "application/json"},
                )
            if resp.status_code != 200:
                logger.warning(
                    f"[audit] storno_discount OLAP v2 {resp.status_code} "
                    f"от {bo_url}: {resp.text[:200]}"
                )
                return server_findings
            data = resp.json().get("data", [])
        except Exception as e:
            logger.warning(f"[audit] storno_discount: request error {bo_url}: {e}")
            return server_findings

        storned_orders: dict[tuple[str, str], dict] = {}
        for row in data:
            if row.get("Storned") == "TRUE":
                dept = row.get("Department", "").strip()
                if dept in target_names:
                    key = (dept, str(row.get("OrderNum", "")))
                    storned_orders[key] = {
                        "open_time": row.get("OpenTime", ""),
                        "close_time": row.get("CloseTime", ""),
                        "pay_types": row.get("PayTypes", ""),
                        "dish_sum": float(row.get("DishSumInt", 0)),
                    }

        for row in data:
            dept = row.get("Department", "").strip()
            order_num = str(row.get("OrderNum", ""))
            key = (dept, order_num)
            if (
                key in storned_orders
                and row.get("Storned") == "FALSE"
                and row.get("OrderDiscount.Type", "") == ""
                and float(row.get("DiscountSum", 0)) > 0
            ):
                disc_sum = float(row["DiscountSum"])
                sum_str = f"{int(disc_sum):,}".replace(",", "\u00a0")
                orig = storned_orders[key]

                def _hhmm(ts: str) -> str:
                    if ts and len(ts) >= 16:
                        return ts[11:16]
                    return ""

                open_t = _hhmm(orig.get("open_time", ""))
                storno_t = _hhmm(orig.get("close_time", ""))
                reopen_t = _hhmm(row.get("CloseTime", ""))

                pay_before = orig.get("pay_types", "") or "?"
                pay_after = (row.get("PayTypes", "") or "").strip()
                pay_changed = pay_before != pay_after and pay_after
                disc_type = row.get("OrderDiscount.Type", "") or "ручная"

                order_sum = orig.get("dish_sum", 0)
                order_sum_str = f"{int(order_sum):,}".replace(",", "\u00a0") if order_sum else ""

                lines = [f"#{order_num} {dept}"]
                time_parts = []
                if open_t:
                    time_parts.append(f"откр {open_t}")
                if storno_t:
                    time_parts.append(f"сторно {storno_t}")
                if reopen_t and reopen_t != storno_t:
                    time_parts.append(f"повтор {reopen_t}")
                if time_parts:
                    lines.append(" → ".join(time_parts))

                if pay_changed:
                    lines.append(f"оплата: {pay_before} → {pay_after}")
                else:
                    lines.append(f"оплата: {pay_before}")
                lines.append(f"скидка: {disc_type} {sum_str}\u20bd")
                if order_sum_str:
                    lines.append(f"сумма заказа: {order_sum_str}\u20bd")

                description = " | ".join(lines)

                server_findings.append({
                    "branch_name": dept,
                    "city": branch_to_city.get(dept, ""),
                    "event_type": "storno_discount",
                    "severity": "critical" if disc_sum >= 3000 else "warning",
                    "description": description,
                    "meta_json": json.dumps({
                        "order_num": order_num,
                        "branch_name": dept,
                        "discount_sum": disc_sum,
                        "order_sum": order_sum,
                        "open_time": orig.get("open_time", ""),
                        "storno_time": orig.get("close_time", ""),
                        "pay_before": pay_before,
                        "pay_after": pay_after or pay_before,
                        "discount_type": disc_type,
                    }, ensure_ascii=False),
                    "created_at": now_iso,
                })

        return server_findings

    tasks = [_query_server(url, names) for url, names in by_url.items()]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, list):
            findings.extend(result)
        elif isinstance(result, Exception):
            logger.error(f"[audit] storno_discount server error: {result}")

    findings.sort(key=lambda f: -json.loads(f["meta_json"]).get("discount_sum", 0))
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
    # bo_url уже содержит https:// — используем напрямую
    base = bo_url

    try:
        token = await get_bo_token(bo_url)
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


_SHORT_CITY = {"Барнаул": "Б", "Томск": "Т", "Абакан": "А", "Черногорск": "Ч"}


def _branch_tag(name: str) -> str:
    parts = name.split("_", 1)
    if len(parts) == 2:
        city_part = parts[0]
        num = parts[1].split()[0] if parts[1] else ""
        short = _SHORT_CITY.get(city_part, city_part[:1])
        return f"{short}{num}"
    return name[:3]


def _tag_description(desc: str, branch_name: str) -> str:
    """Добавляет короткий тег филиала перед номером заказа: [Б1] #119861 ..."""
    if not branch_name:
        return desc
    tag = _branch_tag(branch_name)
    if branch_name in desc:
        # Сторно: "#85998 Барнаул_1 Ана | Открыт: ..." → "[Б1] #85998 | Открыт: ..."
        cleaned = desc.replace(branch_name, "").replace("  ", " ").strip()
        return f"[{tag}] {cleaned}"
    # Всё остальное: тег перед описанием (которое уже начинается с # или текста)
    return f"[{tag}] {desc}"


def _format_report(date_str: str, city: str, events: list[dict]) -> str:
    """Форматирует аудит-отчёт в HTML для Telegram."""
    unclosed = [e for e in events if e["event_type"] == "unclosed_in_transit"]
    fast = [e for e in events if e["event_type"] == "fast_delivery"]
    cancelled = [e for e in events if e["event_type"] in ("cancellation", "cancellation_with_reason")]
    discounts = [e for e in events if e["event_type"] in ("storno_discount", "discount_manual")]
    early = [e for e in events if e["event_type"] == "early_closure"]

    lines = [
        f"🔍 <b>Аудит [{html.escape(city)}] — {_date_label(date_str)}</b>",
        "",
    ]

    if unclosed:
        lines.append(f"🚨 <b>Незакрытые заказы «В пути» ({len(unclosed)})</b>")
        for e in unclosed:
            desc = _tag_description(e["description"], e.get("branch_name", ""))
            lines.append(f"🔴 {html.escape(desc)}")
        lines.append("")

    if fast:
        lines.append(f"⚡ <b>Аномально быстрые доставки ({len(fast)})</b>")
        for e in fast:
            icon = "🔴" if e["severity"] == "critical" else "🟡"
            desc = _tag_description(e["description"], e.get("branch_name", ""))
            lines.append(f"{icon} {html.escape(desc)}")
        lines.append("")

    if cancelled:
        lines.append(f"❌ <b>Отменённые заказы с суммой ({len(cancelled)})</b>")
        for e in cancelled:
            icon = "🔴" if e["severity"] == "critical" else "🟡"
            desc = _tag_description(e["description"], e.get("branch_name", ""))
            lines.append(f"{icon} {html.escape(desc)}")
            try:
                meta = json.loads(e.get("meta_json", "{}"))
                comment = meta.get("comment", "").strip()
                if comment:
                    lines.append(f"   └ {html.escape(comment[:80])}")
            except Exception:
                pass
        lines.append("")

    if discounts:
        lines.append(f"💸 <b>Сторно + скидка ({len(discounts)})</b>")
        for e in discounts:
            icon = "🔴" if e["severity"] == "critical" else "🟡"
            desc = _tag_description(e["description"], e.get("branch_name", ""))
            parts = desc.split(" | ")
            lines.append(f"{icon} <b>{html.escape(parts[0])}</b>")
            for p in parts[1:]:
                lines.append(f"   {html.escape(p)}")
        lines.append("")

    if early:
        lines.append(f"🕐 <b>Ранние закрытия заказов ({len(early)})</b>")
        for e in early:
            icon = "🔴" if e["severity"] == "critical" else "🟡"
            desc = _tag_description(e["description"], e.get("branch_name", ""))
            lines.append(f"{icon} {html.escape(desc)}")
        lines.append("")

    total = len(unclosed) + len(fast) + len(cancelled) + len(discounts) + len(early)
    if total == 0:
        lines.append("✅ Подозрительных операций не выявлено")
    else:
        lines.append(f"<i>Итого: {total} событий</i>")

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

    await clear_audit_events(date_str)

    all_findings = await _generate_audit_for_date(date_str)

    if all_findings:
        await save_audit_events_batch(all_findings)
        logger.info(f"[audit] Сохранено {len(all_findings)} событий за {date_str}")
    else:
        logger.info(f"[audit] Подозрительных событий не найдено за {date_str}")

    unclosed = await _detect_unclosed_in_transit(date_str)
    report_events = all_findings + unclosed

    for branch in branches:
        try:
            await _probe_cash_shifts(branch, date_str)
        except Exception as e:
            logger.debug(f"[audit] probe_cash_shifts exception {branch.get('name')}: {e}")

    cities = sorted({b.get("city", "") for b in branches if b.get("city")})
    for city in cities:
        chat_ids = await get_module_chats_for_city("audit", city)
        if not chat_ids:
            continue
        city_events = [e for e in report_events if e.get("city") == city]
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

    utc_offset = 7
    local_now = datetime.now(timezone.utc) + timedelta(hours=utc_offset)
    today_local = local_now.date()

    _DATE_WORDS = {"вчера": -1, "yesterday": -1, "сегодня": 0, "today": 0}

    date_str: Optional[str] = None
    filter_parts: list[str] = []
    for part in parts:
        low = part.lower()
        if low in _DATE_WORDS:
            date_str = (today_local + timedelta(days=_DATE_WORDS[low])).isoformat()
        elif "." in part and any(c.isdigit() for c in part):
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
        date_str = (today_local - timedelta(days=1)).isoformat()

    # Парсим фильтр по городу/точке
    filter_text = " ".join(filter_parts).strip()
    branch_filter: Optional[str] = None
    city_query: Optional[str] = None

    branches = settings.branches or []
    branch_names = [b["name"] for b in branches]
    cities = list({b.get("city", "") for b in branches if b.get("city")})

    if filter_text:
        low = filter_text.lower()
        matched_city = next(
            (c for c in cities if low in c.lower()), None
        )
        if matched_city:
            city_query = matched_city
        else:
            matched_branch = next(
                (b for b in branch_names if low in b.lower()), None
            )
            if matched_branch:
                branch_filter = matched_branch
            else:
                city_query = filter_text

    # Если фильтр чата задан (frozenset или строка), используем его
    if city_query is None and city_filter is not None:
        if isinstance(city_filter, frozenset):
            # Берём первый город из фильтра (если один — точно, если несколько — первый)
            city_query = next(iter(city_filter), None)
        elif isinstance(city_filter, str):
            city_query = city_filter

    # Запрашиваем из БД
    events = await get_audit_events(date_str, city=city_query, branch_name=branch_filter)

    # Если пусто — генерируем на лету и сохраняем
    if not events:
        generated = await _generate_audit_for_date(date_str)
        if generated:
            await clear_audit_events(date_str)
            await save_audit_events_batch(generated)
            logger.info(f"[audit] On-demand: сгенерировано {len(generated)} событий за {date_str}")
        events = await get_audit_events(date_str, city=city_query, branch_name=branch_filter)

    # Live unclosed (всегда свежие, не из кэша)
    unclosed = await _detect_unclosed_in_transit(date_str)
    if city_query:
        branch_to_city = {b["name"]: b.get("city", "") for b in (settings.branches or [])}
        unclosed = [u for u in unclosed if branch_to_city.get(u["branch_name"], "") == city_query]
    if branch_filter:
        unclosed = [u for u in unclosed if u["branch_name"] == branch_filter]

    events = [e for e in events if e.get("event_type") != "unclosed_in_transit"]
    events.extend(unclosed)

    scope_label = branch_filter or city_query or "все города"
    if not events:
        await send_message(
            str(chat_id),
            f"🔍 <b>Аудит [{html.escape(scope_label)}] — {_date_label(date_str)}</b>\n\n"
            "✅ Подозрительных операций не выявлено",
        )
        return

    if branch_filter or city_query:
        text = _format_report(date_str, scope_label, events)
        await send_message(str(chat_id), text)
    else:
        by_city: dict[str, list[dict]] = {}
        for e in events:
            by_city.setdefault(e.get("city", ""), []).append(e)
        for city_name, city_events in sorted(by_city.items()):
            text = _format_report(date_str, city_name, city_events)
            await send_message(str(chat_id), text)
