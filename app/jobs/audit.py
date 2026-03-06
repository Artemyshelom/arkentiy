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

import httpx

from app.config import get_settings
from app.db import (
    BACKEND,
    clear_audit_events,
    get_all_branches,
    get_audit_events,
    get_module_chats_for_city,
    get_pool,
    save_audit_events_batch,
)
from app.clients import telegram as tg
from app.clients.iiko_auth import get_bo_token
from app.utils.job_tracker import track_job

logger = logging.getLogger(__name__)
settings = get_settings()


def _all_branches_map() -> dict[str, str]:
    """branch_name → city для всех тенантов."""
    try:
        return {b["name"]: b.get("city", "") for b in get_all_branches()}
    except Exception:
        return {b["name"]: b.get("city", "") for b in (settings.branches or [])}

# ---------------------------------------------------------------------------
# Настройки порогов
# ---------------------------------------------------------------------------

FAST_DELIVERY_MIN = 15             # доставка за N мин от создания = подозрительно
CANCEL_HIGH_SUM = 500              # отмена ≥ N₽ без причины = warning
CANCEL_WITH_REASON_SUM = 200       # отмена ≥ N₽ с указанной причиной = warning
EARLY_CLOSURE_MIN = 60             # закрыт на N+ мин раньше плана = подозрительно
MANUAL_DISCOUNT_MIN = 500          # ручная скидка ≥ N₽ без сторно = warning
COURIER_CANCEL_THRESHOLD = 3       # курьер с N+ отменами за день = подозрительно


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
    pool = get_pool()

    # 1. Аномально быстрые доставки (opened_at → actual_time < FAST_DELIVERY_MIN мин)
    rows1 = await pool.fetch(
        """SELECT branch_name, delivery_num, sum, opened_at, actual_time,
                  courier, client_name, client_phone
           FROM orders_raw
           WHERE date::text = $1
             AND is_self_service = false
             AND status IN ('Доставлена', 'Закрыта')
             AND opened_at IS NOT NULL AND opened_at != ''
             AND actual_time IS NOT NULL AND actual_time != ''""",
        date_str,
    )
    for r in rows1:
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
    rows2 = await pool.fetch(
        """SELECT branch_name, delivery_num, sum, cancel_reason,
                  client_name, client_phone, payment_type,
                  cooked_time, comment
           FROM orders_raw
           WHERE date::text = $1
             AND status = 'Отменена'
             AND sum IS NOT NULL
           ORDER BY sum DESC""",
        date_str,
    )
    for r in rows2:
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
    rows3 = await pool.fetch(
        """SELECT branch_name, delivery_num, sum, planned_time, actual_time,
                  courier, client_name, client_phone
           FROM orders_raw
           WHERE date::text = $1
             AND is_self_service = false
             AND status IN ('Доставлена', 'Закрыта')
             AND planned_time IS NOT NULL AND planned_time != ''
             AND actual_time IS NOT NULL AND actual_time != ''""",
        date_str,
    )
    for r in rows3:
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
    now_iso = datetime.now(timezone.utc).isoformat()
    branch_to_city = _all_branches_map()
    pool = get_pool()

    rows = await pool.fetch(
        """SELECT branch_name, delivery_num, sum, planned_time, courier,
                  client_name, client_phone, date::text AS order_date
           FROM orders_raw
           WHERE date::text < $1
             AND status = 'В пути к клиенту'
             AND is_self_service = false
           ORDER BY date ASC, sum DESC""",
        date_str,
    )
    for r in rows:
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


async def _detect_courier_multicancellation(date_str: str) -> list[dict]:
    """
    Курьер с 3+ отменами за день — подозрение на намеренные отмены.
    Severity: warning при ≥3, critical при ≥5 отменах.
    """
    findings: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    pool = get_pool()

    rows = await pool.fetch(
        """SELECT branch_name, courier,
                  COUNT(*)::int AS cancel_count,
                  COALESCE(SUM(sum::numeric), 0)::int AS total_sum,
                  array_agg(delivery_num ORDER BY sum::numeric DESC NULLS LAST) AS order_nums
           FROM orders_raw
           WHERE date::text = $1
             AND status = 'Отменена'
             AND courier IS NOT NULL AND courier != ''
           GROUP BY branch_name, courier
           HAVING COUNT(*) >= $2
           ORDER BY cancel_count DESC, total_sum DESC""",
        date_str,
        COURIER_CANCEL_THRESHOLD,
    )
    for r in rows:
        count = r["cancel_count"]
        total = r["total_sum"]
        total_str = f"{total:,}".replace(",", "\u00a0")
        courier = r["courier"]
        nums = list(r["order_nums"])[:3]
        nums_str = ", ".join(f"#{n}" for n in nums)
        if len(r["order_nums"]) > 3:
            nums_str += f" +{len(r['order_nums']) - 3}"
        findings.append({
            "branch_name": r["branch_name"],
            "event_type": "courier_multicancellation",
            "severity": "critical" if count >= 5 else "warning",
            "description": f"{courier} — {count} отмен, {total_str}\u20bd · {nums_str}",
            "meta_json": json.dumps({
                "courier": courier,
                "cancel_count": count,
                "total_sum": total,
                "order_nums": list(r["order_nums"]),
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
    branch_to_city = _all_branches_map()

    all_findings = await _detect_from_orders_raw(date_str)

    try:
        courier_findings = await _detect_courier_multicancellation(date_str)
        all_findings.extend(courier_findings)
    except Exception as e:
        logger.warning(f"[audit] Ошибка детектора courier_multicancellation: {e}")

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

    all_branches = get_all_branches()
    branch_to_city = {b["name"]: b.get("city", "") for b in all_branches}

    by_server: dict[tuple, dict] = {}
    for branch in all_branches:
        url = branch.get("bo_url", "")
        if not url:
            continue
        login = branch.get("bo_login") or ""
        password = branch.get("bo_password") or ""
        key = (url, login, password)
        if key not in by_server:
            by_server[key] = {"names": set(), "login": login or None, "password": password or None}
        by_server[key]["names"].add(branch["name"])

    async def _query_server(bo_url: str, target_names: set[str],
                            bo_login=None, bo_password=None) -> list[dict]:
        server_findings: list[dict] = []
        try:
            token = await get_bo_token(bo_url, bo_login=bo_login, bo_password=bo_password)
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

        # Ручные скидки без сторно (не пересекаются со storno_discount)
        seen_manual: set[tuple[str, str]] = set()
        for row in data:
            dept = row.get("Department", "").strip()
            if dept not in target_names:
                continue
            order_num = str(row.get("OrderNum", ""))
            key = (dept, order_num)
            if key in storned_orders or key in seen_manual:
                continue
            disc_sum = float(row.get("DiscountSum", 0) or 0)
            if (
                row.get("Storned") == "FALSE"
                and row.get("OrderDiscount.Type", "") == ""
                and disc_sum >= MANUAL_DISCOUNT_MIN
            ):
                seen_manual.add(key)
                sum_str = f"{int(disc_sum):,}".replace(",", "\u00a0")
                close_t = ""
                ct = row.get("CloseTime", "")
                if ct and len(ct) >= 16:
                    close_t = ct[11:16]
                pay = (row.get("PayTypes", "") or "").strip()
                pay_str = f" · {pay}" if pay else ""
                server_findings.append({
                    "branch_name": dept,
                    "city": branch_to_city.get(dept, ""),
                    "event_type": "manual_discount",
                    "severity": "critical" if disc_sum >= 2000 else "warning",
                    "description": (
                        f"#{order_num} — ручная скидка {sum_str}\u20bd"
                        + (f" в {close_t}" if close_t else "")
                        + pay_str
                    ),
                    "meta_json": json.dumps({
                        "order_num": order_num,
                        "branch_name": dept,
                        "discount_sum": disc_sum,
                        "pay_types": pay,
                    }, ensure_ascii=False),
                    "created_at": now_iso,
                })

        return server_findings

    tasks = [_query_server(url, srv["names"], srv["login"], srv["password"])
             for (url, _, __), srv in by_server.items()]
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
        token = await get_bo_token(bo_url,
                                   bo_login=branch.get("bo_login") or None,
                                   bo_password=branch.get("bo_password") or None)
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
    courier_multi = [e for e in events if e["event_type"] == "courier_multicancellation"]
    cancelled = [e for e in events if e["event_type"] in ("cancellation", "cancellation_with_reason")]
    discounts = [e for e in events if e["event_type"] in ("storno_discount", "manual_discount")]
    early = [e for e in events if e["event_type"] == "early_closure"]

    total = len(events)
    lines = [f"🔍 <b>Аудит [{html.escape(city)}] — {_date_label(date_str)}</b>"]

    if total == 0:
        lines.append("✅ Чисто")
        return "\n".join(lines)

    total_crit = sum(1 for e in events if e.get("severity") == "critical")
    total_warn = total - total_crit
    summary_parts = []
    if total_crit:
        summary_parts.append(f"{total_crit}🔴")
    if total_warn:
        summary_parts.append(f"{total_warn}🟡")
    lines.append(" · ".join(summary_parts))
    lines.append("")

    def _sort_sev(e: dict) -> int:
        return 0 if e.get("severity") == "critical" else 1

    if unclosed:
        lines.append(f"🚨 <b>Незакрытые «В пути» ({len(unclosed)})</b>")
        for e in unclosed:
            desc = _tag_description(e["description"], e.get("branch_name", ""))
            lines.append(f"🔴 {html.escape(desc)}")
        lines.append("")

    if fast:
        lines.append(f"⚡ <b>Быстрые доставки ({len(fast)})</b>")
        for e in sorted(fast, key=_sort_sev):
            icon = "🔴" if e["severity"] == "critical" else "🟡"
            desc = _tag_description(e["description"], e.get("branch_name", ""))
            lines.append(f"{icon} {html.escape(desc)}")
        lines.append("")

    if courier_multi:
        lines.append(f"👤 <b>Отмены по курьеру ({len(courier_multi)})</b>")
        for e in sorted(courier_multi, key=_sort_sev):
            icon = "🔴" if e["severity"] == "critical" else "🟡"
            desc = _tag_description(e["description"], e.get("branch_name", ""))
            lines.append(f"{icon} {html.escape(desc)}")
        lines.append("")

    if cancelled:
        lines.append(f"❌ <b>Отменённые заказы ({len(cancelled)})</b>")
        for e in sorted(cancelled, key=_sort_sev):
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
        lines.append(f"💸 <b>Скидки / сторно ({len(discounts)})</b>")
        for e in sorted(discounts, key=_sort_sev):
            icon = "🔴" if e["severity"] == "critical" else "🟡"
            desc = _tag_description(e["description"], e.get("branch_name", ""))
            if e["event_type"] == "storno_discount":
                parts_d = desc.split(" | ")
                tail = " · ".join(parts_d[1:]) if len(parts_d) > 1 else ""
                lines.append(
                    f"{icon} <b>{html.escape(parts_d[0])}</b>"
                    + (f"  {html.escape(tail)}" if tail else "")
                )
            else:
                lines.append(f"{icon} {html.escape(desc)}")
        lines.append("")

    if early:
        lines.append(f"🕐 <b>Ранние закрытия ({len(early)})</b>")
        for e in sorted(early, key=_sort_sev):
            icon = "🔴" if e["severity"] == "critical" else "🟡"
            desc = _tag_description(e["description"], e.get("branch_name", ""))
            lines.append(f"{icon} {html.escape(desc)}")
        lines.append("")

    lines.append(f"<i>Итого: {total} событий</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Дайджест (краткий кросс-городской отчёт)
# ---------------------------------------------------------------------------

_TYPE_LABEL: dict[str, str] = {
    "fast_delivery": "быстро",
    "cancellation": "отмена",
    "cancellation_with_reason": "отмена",
    "early_closure": "раньше",
    "storno_discount": "сторно",
    "manual_discount": "скидка",
    "unclosed_in_transit": "незакрыт",
    "courier_multicancellation": "курьер",
}


def _format_digest(date_str: str, all_events: list[dict]) -> str:
    """Краткий дайджест аудита по всем городам одного тенанта."""
    by_city: dict[str, list[dict]] = {}
    for e in all_events:
        city = e.get("city") or "—"
        by_city.setdefault(city, []).append(e)

    total_crit = sum(1 for e in all_events if e.get("severity") == "critical")
    total_warn = sum(1 for e in all_events if e.get("severity") == "warning")

    lines = [f"📋 <b>Аудит-дайджест — {_date_label(date_str)}</b>", ""]

    for city in sorted(by_city):
        events = by_city[city]
        crit = sum(1 for e in events if e.get("severity") == "critical")
        warn = sum(1 for e in events if e.get("severity") == "warning")
        if crit == 0 and warn == 0:
            lines.append(f"✅ {html.escape(city)}")
        else:
            badges: list[str] = []
            if crit:
                badges.append(f"{crit}🔴")
            if warn:
                badges.append(f"{warn}🟡")
            type_counts: dict[str, int] = {}
            for e in events:
                lbl = _TYPE_LABEL.get(e["event_type"], e["event_type"])
                type_counts[lbl] = type_counts.get(lbl, 0) + 1
            type_str = ", ".join(f"{cnt}×{lbl}" for lbl, cnt in type_counts.items())
            lines.append(
                f"⚠️ <b>{html.escape(city)}</b>: {' '.join(badges)} — {html.escape(type_str)}"
            )

    lines.append("")
    if total_crit == 0 and total_warn == 0:
        lines.append("✅ Всё чисто")
    else:
        summary: list[str] = []
        if total_crit:
            summary.append(f"{total_crit} критических🔴")
        if total_warn:
            summary.append(f"{total_warn} предупреждений🟡")
        lines.append(f"<i>{' · '.join(summary)}</i>")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Основной job
# ---------------------------------------------------------------------------

@track_job("audit_report")
async def job_audit_report(utc_offset: int = 7) -> None:
    """Ежедневный аудит-отчёт. Запускается в 05:30 МСК (= 09:30 UTC+7).
    
    Multi-tenant: берёт ветки для всех тенантов из БД (iiko_credentials),
    запускает аудит для каждого тенанта отдельно.
    """
    local_now = datetime.now(timezone.utc) + timedelta(hours=utc_offset)
    yesterday = (local_now - timedelta(days=1)).date()
    date_str = yesterday.isoformat()

    logger.info(f"[audit] Запуск аудита за {date_str}")

    pool = await get_pool()
    
    # Получаем все активные ветки по всем тенантам из БД
    try:
        all_iiko_creds = await pool.fetch(
            "SELECT tenant_id, city, branch_name FROM iiko_credentials WHERE is_active = true"
        )
    except Exception as e:
        logger.error(f"[audit] Ошибка чтения iiko_credentials: {e}")
        all_iiko_creds = []
    
    if not all_iiko_creds:
        logger.warning("[audit] Нет активных веток в БД — пропускаю")
        return
    
    # Группируем по tenant_id → city
    tenant_cities: dict[int, set[str]] = {}
    for row in all_iiko_creds:
        tenant_id = row["tenant_id"]
        city = row.get("city") or ""
        if tenant_id not in tenant_cities:
            tenant_cities[tenant_id] = set()
        if city:
            tenant_cities[tenant_id].add(city)

    # Строим карту branch_name → tenant_id для корректной записи событий
    branch_tenant_map: dict[str, int] = {row["branch_name"]: row["tenant_id"] for row in all_iiko_creds}

    await clear_audit_events(date_str)

    all_findings = await _generate_audit_for_date(date_str)

    if all_findings:
        # Группируем по tenant_id и сохраняем с правильной изоляцией
        by_tenant: dict[int, list] = {}
        for f in all_findings:
            tid = branch_tenant_map.get(f.get("branch_name", ""), 1)
            by_tenant.setdefault(tid, []).append(f)
        for tid, events in by_tenant.items():
            await save_audit_events_batch(events, tenant_id=tid)
        logger.info(f"[audit] Сохранено {len(all_findings)} событий за {date_str}")
    else:
        logger.info(f"[audit] Подозрительных событий не найдено за {date_str}")

    unclosed = await _detect_unclosed_in_transit(date_str)
    report_events = all_findings + unclosed

    # Проверяем cash shifts для всех веток (только для tenant_id=1, legacy)
    for row in all_iiko_creds:
        if row["tenant_id"] != 1:  # Legacy: только для первого тенанта
            continue
        try:
            await _probe_cash_shifts({"name": row["branch_name"]}, date_str)
        except Exception as e:
            logger.debug(f"[audit] probe_cash_shifts exception {row['branch_name']}: {e}")

    # Строим карту tenant → ветки для фильтрации событий
    tenant_branch_set: dict[int, set[str]] = {}
    for row in all_iiko_creds:
        tenant_branch_set.setdefault(row["tenant_id"], set()).add(row["branch_name"])

    # Отправляем отчёты для каждого тенанта по его городам
    for tenant_id, cities in tenant_cities.items():
        branches_of_tenant = tenant_branch_set.get(tenant_id, set())
        tenant_events = [
            e for e in report_events
            if e.get("branch_name", "") in branches_of_tenant
        ]

        # Дайджест: одно сжатое сообщение по всем городам тенанта
        all_digest_chats: set[int] = set()
        for city in sorted(cities):
            cids = await get_module_chats_for_city("audit", city, tenant_id=tenant_id)
            all_digest_chats.update(cids)

        if all_digest_chats:
            digest_text = _format_digest(date_str, tenant_events)
            for chat_id in all_digest_chats:
                try:
                    await tg.send_message(str(chat_id), digest_text)
                except Exception as e:
                    logger.error(f"[audit] Ошибка отправки дайджеста в {chat_id}: {e}")

        # Детальные отчёты только по городам с событиями
        for city in sorted(cities):
            chat_ids = await get_module_chats_for_city("audit", city, tenant_id=tenant_id)
            if not chat_ids:
                continue
            city_events = [e for e in report_events if e.get("city") == city]
            if not city_events:
                continue  # дайджест уже показал «✅ чисто»
            report_text, report_keyboard = _format_report_v2(date_str, city, city_events)
            for chat_id in chat_ids:
                try:
                    await tg.send_message_with_keyboard(str(chat_id), report_text, report_keyboard)
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
    from app.ctx import ctx_tenant_id as _ctx_tid
    current_tenant_id = _ctx_tid.get()

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

    branches = get_all_branches()
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
    events = await get_audit_events(date_str, city=city_query, branch_name=branch_filter, tenant_id=current_tenant_id)

    # Если пусто — генерируем на лету и сохраняем
    if not events:
        generated = await _generate_audit_for_date(date_str)
        if generated:
            await clear_audit_events(date_str, tenant_id=current_tenant_id)
            await save_audit_events_batch(generated, tenant_id=current_tenant_id)
            logger.info(f"[audit] On-demand: сгенерировано {len(generated)} событий за {date_str}")
        events = await get_audit_events(date_str, city=city_query, branch_name=branch_filter)

    # Live unclosed (всегда свежие, не из кэша)
    unclosed = await _detect_unclosed_in_transit(date_str)
    if city_query:
        branch_to_city = _all_branches_map()
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
        text, keyboard = _format_report_v2(date_str, scope_label, events)
        from app.clients.telegram import send_message_with_keyboard
        await send_message_with_keyboard(str(chat_id), text, keyboard)
    else:
        by_city: dict[str, list[dict]] = {}
        for e in events:
            by_city.setdefault(e.get("city", ""), []).append(e)
        from app.clients.telegram import send_message_with_keyboard
        for city_name, city_events in sorted(by_city.items()):
            text, keyboard = _format_report_v2(date_str, city_name, city_events)
            await send_message_with_keyboard(str(chat_id), text, keyboard)


# ---------------------------------------------------------------------------
# Форматировщики v2 — сводка + детали по кнопкам
# ---------------------------------------------------------------------------

def _meta(e: dict) -> dict:
    """Безопасно парсит meta_json события."""
    try:
        return json.loads(e.get("meta_json", "{}"))
    except Exception:
        return {}


def _get_sum(e: dict) -> float:
    m = _meta(e)
    return float(m.get("sum", 0) or 0)


def _get_early_min(e: dict) -> float:
    m = _meta(e)
    return float(m.get("early_min", 0) or 0)


def _fmt_sum(v: float) -> str:
    return f"{int(v):,}".replace(",", "\u00a0") + "₽"


def _attention_items(events: list[dict]) -> list[str]:
    """Формирует список строк блока «Требует внимания»."""
    items: list[str] = []

    high = [e for e in events
            if e["event_type"] in ("cancellation", "cancellation_with_reason")
            and _get_sum(e) >= 5000]
    if high:
        total = sum(_get_sum(e) for e in high)
        items.append(f"{len(high)} отмен >5000₽ · сумма {_fmt_sum(total)}")

    crit_early = [e for e in events
                  if e["event_type"] == "early_closure" and _get_early_min(e) >= 100]
    if crit_early:
        items.append(f"{len(crit_early)} закрытий >100 мин раньше плана")

    storno = [e for e in events if e["event_type"] == "storno_discount"]
    if storno:
        total_st = sum(_meta(e).get("discount_sum", 0) for e in storno)
        items.append(f"{len(storno)} сторно со скидкой · {_fmt_sum(total_st)}")

    courier_mc = [e for e in events if e["event_type"] == "courier_multicancellation"
                  and e.get("severity") == "critical"]
    if courier_mc:
        items.append(f"{len(courier_mc)} курьер(а) с 5+ отменами за день")

    return items


def _group_cancellations(events: list[dict]) -> dict[str, list[dict]]:
    """
    Группирует отмены: сначала крупные (>5000₽), потом по причине.
    Ключ 'high_value' — крупные; остальные ключи — текст причины.
    """
    groups: dict[str, list[dict]] = {}
    for e in events:
        if e["event_type"] not in ("cancellation", "cancellation_with_reason"):
            continue
        m = _meta(e)
        s = float(m.get("sum", 0) or 0)
        reason = (m.get("cancel_reason", "") or "Без причины").strip()
        if s >= 5000:
            groups.setdefault("high_value", []).append(e)
        else:
            groups.setdefault(reason, []).append(e)
    return groups


def _group_attrs(group_events: list[dict]) -> str:
    """Вычисляет общие признаки группы: 'без оплаты · не готовились'."""
    metas = [_meta(e) for e in group_events]
    no_pay = all(not (m.get("payment_type") or "").strip() for m in metas)
    not_cooked = all(not m.get("cooked") for m in metas)
    parts: list[str] = []
    if no_pay:
        parts.append("без оплаты")
    if not_cooked:
        parts.append("не готовились")
    return " · ".join(parts) if parts else ""


def _format_report_v2(date_str: str, city: str, events: list[dict]) -> tuple[str, list[list[dict]]]:
    """
    Сводка аудита v2.
    Возвращает (text, keyboard) — keyboard готов для Telegram InlineKeyboardMarkup.
    """
    cancels = [e for e in events if e["event_type"] in ("cancellation", "cancellation_with_reason")]
    early = [e for e in events if e["event_type"] == "early_closure"]
    discounts = [e for e in events if e["event_type"] in ("storno_discount", "manual_discount")]
    fast = [e for e in events if e["event_type"] == "fast_delivery"]
    unclosed = [e for e in events if e["event_type"] == "unclosed_in_transit"]
    courier_mc = [e for e in events if e["event_type"] == "courier_multicancellation"]

    lines: list[str] = [
        f"🔍 <b>Аудит [{html.escape(city)}] — {_date_label(date_str)}</b>",
    ]

    if not events:
        lines.append("\n✅ Подозрительных операций не выявлено")
        return "\n".join(lines), []

    # Блок «Требует внимания»
    attn = _attention_items(events)
    if attn:
        lines.append("\n⚠️ <b>Требует внимания:</b>")
        for a in attn:
            lines.append(f"• {html.escape(a)}")

    # Сводка по категориям
    lines.append("\n📊 <b>Итого:</b> " + str(len(events)) + " событий")

    if unclosed:
        lines.append(f"🚨 Незакрытые «В пути»: {len(unclosed)}")
    if cancels:
        total_c = sum(_get_sum(e) for e in cancels)
        lines.append(f"❌ Отменённые: {len(cancels)} · {_fmt_sum(total_c)}")
    if discounts:
        total_d = sum(
            _meta(e).get("discount_sum", 0) or _meta(e).get("discount_sum", 0)
            for e in discounts
        )
        lines.append(f"💸 Скидки/сторно: {len(discounts)} · {_fmt_sum(total_d)}")
    if courier_mc:
        lines.append(f"👤 Курьеры с отменами: {len(courier_mc)}")
    if early:
        lines.append(f"🕐 Ранние закрытия: {len(early)}")
    if fast:
        lines.append(f"⚡ Быстрые доставки: {len(fast)}")

    # Кнопки навигации
    cb_prefix = f"audit_detail:{city}:{date_str}"
    buttons: list[dict] = []
    if cancels:
        buttons.append({"text": "❌ Отмены", "callback_data": f"{cb_prefix}:cancellations"})
    if discounts:
        buttons.append({"text": "💸 Скидки", "callback_data": f"{cb_prefix}:discounts"})
    if courier_mc:
        buttons.append({"text": "👤 Курьеры", "callback_data": f"{cb_prefix}:couriers"})
    if early:
        buttons.append({"text": "🕐 Закрытия", "callback_data": f"{cb_prefix}:early"})
    if fast:
        buttons.append({"text": "⚡ Быстрые", "callback_data": f"{cb_prefix}:fast"})
    if unclosed:
        buttons.append({"text": "🚨 В пути", "callback_data": f"{cb_prefix}:unclosed"})

    # Разбиваем кнопки по 3 в ряд
    keyboard: list[list[dict]] = [buttons[i:i+3] for i in range(0, len(buttons), 3)]

    return "\n".join(lines), keyboard


def _format_cancellations_detail(date_str: str, city: str, events: list[dict]) -> tuple[str, list[list[dict]]]:
    cancels = [e for e in events if e["event_type"] in ("cancellation", "cancellation_with_reason")]
    groups = _group_cancellations(events)

    lines = [f"❌ <b>Отменённые [{html.escape(city)}] — {_date_label(date_str)}</b>", ""]

    # 1. Крупные
    high = groups.pop("high_value", [])
    if high:
        total = sum(_get_sum(e) for e in high)
        lines.append(f"🔴 <b>Крупные >5000₽ — {len(high)} шт · {_fmt_sum(total)}</b>")
        for e in sorted(high, key=lambda x: -_get_sum(x)):
            m = _meta(e)
            num = m.get("delivery_num", "?")
            s = _get_sum(e)
            reason = (m.get("cancel_reason", "") or "без причины").strip()
            cooked_lbl = "🍳 готовился" if m.get("cooked") else "без списания"
            branch = e.get("branch_name", "")
            tag = f"[{_branch_tag(branch)}] " if branch else ""
            lines.append(f"  {tag}#{num} {_fmt_sum(s)} · {html.escape(reason)} · {cooked_lbl}")
            comment = (m.get("comment", "") or "").strip()
            if comment:
                lines.append(f"    └ {html.escape(comment[:100])}")
        lines.append("")

    # 2. По причинам
    for reason, group in sorted(groups.items(), key=lambda x: -len(x[1])):
        total = sum(_get_sum(e) for e in group)
        attrs = _group_attrs(group)
        attr_str = f" · {attrs}" if attrs else ""
        lines.append(f"<b>{html.escape(reason)} — {len(group)} шт · {_fmt_sum(total)}</b>{html.escape(attr_str)}")

        # Группируем номера по ветке
        by_branch: dict[str, list[str]] = {}
        for e in sorted(group, key=lambda x: -_get_sum(x)):
            m = _meta(e)
            num = str(m.get("delivery_num", "?"))
            branch = e.get("branch_name", "")
            tag = _branch_tag(branch) if branch else "—"
            by_branch.setdefault(tag, []).append(f"#{num}")
        for tag, nums in sorted(by_branch.items()):
            lines.append(f"  [{tag}] {' · '.join(nums)}")
        lines.append("")

    if not cancels:
        lines.append("Нет данных")

    keyboard = [[{"text": "← Назад", "callback_data": f"audit_summary:{city}:{date_str}"}]]
    return "\n".join(lines), keyboard


def _format_early_detail(date_str: str, city: str, events: list[dict]) -> tuple[str, list[list[dict]]]:
    early = [e for e in events if e["event_type"] == "early_closure"]

    crit = [e for e in early if _get_early_min(e) >= 100]
    moderate = [e for e in early if 60 <= _get_early_min(e) < 100]

    lines = [f"🕐 <b>Ранние закрытия [{html.escape(city)}] — {_date_label(date_str)}</b>", ""]

    if crit:
        lines.append(f"🔴 <b>Критичные >100 мин — {len(crit)} шт</b>")
        for e in sorted(crit, key=lambda x: -_get_early_min(x)):
            m = _meta(e)
            num = m.get("delivery_num", "?")
            em = int(_get_early_min(e))
            courier = (m.get("courier", "") or "").strip()
            s = float(m.get("sum", 0) or 0)
            branch = e.get("branch_name", "")
            tag = f"[{_branch_tag(branch)}] " if branch else ""
            courier_str = f" · {html.escape(courier)}" if courier else ""
            lines.append(f"  {tag}#{num} · −{em} мин{courier_str} · {_fmt_sum(s)}")
        lines.append("")

    if moderate:
        lines.append(f"🟡 <b>Умеренные 60-100 мин — {len(moderate)} шт</b>")
        # Компактно: по 4 в строку
        by_branch: dict[str, list[str]] = {}
        for e in sorted(moderate, key=lambda x: -_get_early_min(x)):
            m = _meta(e)
            num = m.get("delivery_num", "?")
            em = int(_get_early_min(e))
            branch = e.get("branch_name", "")
            tag = _branch_tag(branch) if branch else "—"
            by_branch.setdefault(tag, []).append(f"#{num} −{em}м")
        for tag, items_list in sorted(by_branch.items()):
            chunk = " · ".join(items_list)
            lines.append(f"  [{tag}] {chunk}")
        lines.append("")

    if not early:
        lines.append("Нет данных")

    keyboard = [[{"text": "← Назад", "callback_data": f"audit_summary:{city}:{date_str}"}]]
    return "\n".join(lines), keyboard


def _format_discounts_detail(date_str: str, city: str, events: list[dict]) -> tuple[str, list[list[dict]]]:
    discounts = [e for e in events if e["event_type"] in ("storno_discount", "manual_discount")]

    lines = [f"💸 <b>Скидки / сторно [{html.escape(city)}] — {_date_label(date_str)}</b>", ""]

    storno = [e for e in discounts if e["event_type"] == "storno_discount"]
    manual = [e for e in discounts if e["event_type"] == "manual_discount"]

    if storno:
        lines.append(f"🔴 <b>Сторно + скидка — {len(storno)} шт</b>")
        for e in sorted(storno, key=lambda x: -_meta(x).get("discount_sum", 0)):
            m = _meta(e)
            num = m.get("order_num", "?")
            disc = float(m.get("discount_sum", 0))
            branch = e.get("branch_name", "")
            tag = f"[{_branch_tag(branch)}] " if branch else ""
            open_t = m.get("open_time", "")
            storno_t = m.get("storno_time", "")
            pay_b = m.get("pay_before", "")
            pay_a = m.get("pay_after", pay_b)
            disc_type = m.get("discount_type", "ручная")
            lines.append(f"  {tag}#{num}")
            timeline: list[str] = []
            if open_t and len(open_t) >= 16:
                timeline.append(f"откр {open_t[11:16]}")
            if storno_t and len(storno_t) >= 16:
                timeline.append(f"сторно {storno_t[11:16]}")
            if timeline:
                lines.append(f"    📅 {' → '.join(timeline)}")
            if pay_b != pay_a and pay_a:
                lines.append(f"    💳 оплата: {html.escape(pay_b)} → {html.escape(pay_a)}")
            else:
                lines.append(f"    💳 {html.escape(pay_b or '—')}")
            lines.append(f"    💰 скидка: {html.escape(disc_type)} {_fmt_sum(disc)}")
        lines.append("")

    if manual:
        total_m = sum(_meta(e).get("discount_sum", 0) for e in manual)
        lines.append(f"🟡 <b>Ручные скидки — {len(manual)} шт · {_fmt_sum(total_m)}</b>")
        for e in sorted(manual, key=lambda x: -_meta(x).get("discount_sum", 0)):
            m = _meta(e)
            num = m.get("order_num", "?")
            disc = float(m.get("discount_sum", 0))
            branch = e.get("branch_name", "")
            tag = f"[{_branch_tag(branch)}] " if branch else ""
            pay = (m.get("pay_types", "") or "").strip()
            pay_str = f" · {html.escape(pay)}" if pay else ""
            desc = e.get("description", "")
            # Извлекаем время из description если оно там есть
            time_part = ""
            if " в " in desc:
                time_part = " · " + desc.split(" в ")[-1].split(" ")[0]
            lines.append(f"  {tag}#{num} {_fmt_sum(disc)}{time_part}{pay_str}")
        lines.append("")

    if not discounts:
        lines.append("Нет данных")

    keyboard = [[{"text": "← Назад", "callback_data": f"audit_summary:{city}:{date_str}"}]]
    return "\n".join(lines), keyboard


def _format_couriers_detail(date_str: str, city: str, events: list[dict]) -> tuple[str, list[list[dict]]]:
    couriers = [e for e in events if e["event_type"] == "courier_multicancellation"]

    lines = [f"👤 <b>Курьеры с отменами [{html.escape(city)}] — {_date_label(date_str)}</b>", ""]

    for e in sorted(couriers, key=lambda x: -_meta(x).get("cancel_count", 0)):
        m = _meta(e)
        courier = m.get("courier", "?")
        count = m.get("cancel_count", 0)
        total = float(m.get("total_sum", 0))
        nums = m.get("order_nums", [])
        branch = e.get("branch_name", "")
        tag = f"[{_branch_tag(branch)}] " if branch else ""
        icon = "🔴" if e.get("severity") == "critical" else "🟡"
        nums_str = " · ".join(f"#{n}" for n in nums[:5])
        if len(nums) > 5:
            nums_str += f" +{len(nums)-5}"
        lines.append(f"{icon} {tag}<b>{html.escape(courier)}</b> — {count} отмен · {_fmt_sum(total)}")
        lines.append(f"   {nums_str}")
        lines.append("")

    if not couriers:
        lines.append("Нет данных")

    keyboard = [[{"text": "← Назад", "callback_data": f"audit_summary:{city}:{date_str}"}]]
    return "\n".join(lines), keyboard


def _format_fast_detail(date_str: str, city: str, events: list[dict]) -> tuple[str, list[list[dict]]]:
    fast = [e for e in events if e["event_type"] == "fast_delivery"]

    lines = [f"⚡ <b>Быстрые доставки [{html.escape(city)}] — {_date_label(date_str)}</b>", ""]

    for e in sorted(fast, key=lambda x: _meta(x).get("delta_min", 99)):
        m = _meta(e)
        num = m.get("delivery_num", "?")
        delta = m.get("delta_min", 0)
        courier = (m.get("courier", "") or "").strip()
        s = float(m.get("sum", 0) or 0)
        branch = e.get("branch_name", "")
        tag = f"[{_branch_tag(branch)}] " if branch else ""
        icon = "🔴" if e.get("severity") == "critical" else "🟡"
        courier_str = f" · {html.escape(courier)}" if courier else ""
        lines.append(f"{icon} {tag}#{num} — {delta:.0f} мин{courier_str} · {_fmt_sum(s)}")

    if not fast:
        lines.append("Нет данных")

    keyboard = [[{"text": "← Назад", "callback_data": f"audit_summary:{city}:{date_str}"}]]
    return "\n".join(lines), keyboard


def _format_unclosed_detail(date_str: str, city: str, events: list[dict]) -> tuple[str, list[list[dict]]]:
    unclosed = [e for e in events if e["event_type"] == "unclosed_in_transit"]

    lines = [f"🚨 <b>Незакрытые «В пути» [{html.escape(city)}] — {_date_label(date_str)}</b>", ""]

    for e in unclosed:
        m = _meta(e)
        num = m.get("delivery_num", "?")
        order_date = m.get("order_date", "")
        courier = (m.get("courier", "") or "").strip()
        s = float(m.get("sum", 0) or 0)
        branch = e.get("branch_name", "")
        tag = f"[{_branch_tag(branch)}] " if branch else ""
        courier_str = f" · {html.escape(courier)}" if courier else ""
        date_str_e = f" (от {order_date})" if order_date else ""
        lines.append(f"🔴 {tag}#{num}{date_str_e}{courier_str} · {_fmt_sum(s)}")

    if not unclosed:
        lines.append("Нет данных")

    keyboard = [[{"text": "← Назад", "callback_data": f"audit_summary:{city}:{date_str}"}]]
    return "\n".join(lines), keyboard


async def handle_audit_callback(
    cb_id: str,
    cb_chat_id: int,
    cb_message_id: int,
    cb_data: str,
    current_tenant_id: int,
) -> None:
    """
    Обрабатывает нажатия inline-кнопок аудита.
    cb_data форматы:
      audit_detail:{city}:{date}:{type}
      audit_summary:{city}:{date}
    """
    from app.clients.telegram import edit_message_with_keyboard

    if cb_data.startswith("audit_summary:"):
        parts = cb_data.split(":", 2)
        if len(parts) < 3:
            return
        city, date_str = parts[1], parts[2]
        events = await get_audit_events(date_str, city=city, tenant_id=current_tenant_id)
        unclosed = await _detect_unclosed_in_transit(date_str)
        branch_to_city = _all_branches_map()
        unclosed = [u for u in unclosed if branch_to_city.get(u["branch_name"], "") == city]
        events = [e for e in events if e.get("event_type") != "unclosed_in_transit"]
        events.extend(unclosed)
        text, keyboard = _format_report_v2(date_str, city, events)
        await edit_message_with_keyboard(cb_chat_id, cb_message_id, text, keyboard)

    elif cb_data.startswith("audit_detail:"):
        parts = cb_data.split(":", 3)
        if len(parts) < 4:
            return
        city, date_str, detail_type = parts[1], parts[2], parts[3]
        events = await get_audit_events(date_str, city=city, tenant_id=current_tenant_id)
        unclosed = await _detect_unclosed_in_transit(date_str)
        branch_to_city = _all_branches_map()
        unclosed = [u for u in unclosed if branch_to_city.get(u["branch_name"], "") == city]
        events_with_unclosed = [e for e in events if e.get("event_type") != "unclosed_in_transit"]
        events_with_unclosed.extend(unclosed)

        if detail_type == "cancellations":
            text, keyboard = _format_cancellations_detail(date_str, city, events_with_unclosed)
        elif detail_type == "early":
            text, keyboard = _format_early_detail(date_str, city, events_with_unclosed)
        elif detail_type == "discounts":
            text, keyboard = _format_discounts_detail(date_str, city, events_with_unclosed)
        elif detail_type == "couriers":
            text, keyboard = _format_couriers_detail(date_str, city, events_with_unclosed)
        elif detail_type == "fast":
            text, keyboard = _format_fast_detail(date_str, city, events_with_unclosed)
        elif detail_type == "unclosed":
            text, keyboard = _format_unclosed_detail(date_str, city, events_with_unclosed)
        else:
            return

        await edit_message_with_keyboard(cb_chat_id, cb_message_id, text, keyboard)
