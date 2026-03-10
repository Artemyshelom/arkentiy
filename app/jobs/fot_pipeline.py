"""
ФОТ-пайплайн — ежедневный расчёт фонда оплаты труда по категориям персонала.

Источник смен:  shifts_raw (заполняется Events API в реальном времени)
Источник ставок: GET {bo_url}/api/v2/employees/salary (запрос раз в сутки)

Расписание: ежедневно в 04:00 МСК.
  - Все активные точки UTC+7 → смены закрыты к 02:00 local = 21:00 МСК накануне.
  - Запуск в 04:00 МСК даёт 7-часовой буфер.
  - До ранних daily_report (UTC+7 → 05:25 МСК) и weekly_report (пн 06:00 МСК).

Экспортирует:
  run_fot_pipeline(target_date, tenant_id) → dict  — основная логика
  job_fot_pipeline()                               — враппер для APScheduler
"""

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx

from app.clients.iiko_auth import get_bo_token
from app.clients.iiko_schedule import fetch_salary_map
from app.clients import telegram
from app.database_pg import (
    get_fot_shifts_by_date,
    get_alert_chats_for_city,
    upsert_fot_daily_batch,
    get_all_branches,
)
from app.utils.job_tracker import track_job

logger = logging.getLogger(__name__)

# Категории, которые пишем в fot_daily (other пропускаем)
_FOT_CATEGORIES = frozenset({"cook", "courier", "admin"})


def _calc_hours(clock_in: str | None, clock_out: str | None) -> float:
    """Вычисляет отработанные часы по ISO-строкам clock_in/clock_out."""
    if not clock_in or not clock_out:
        return 0.0
    try:
        # iiko хранит строки вида "2026-03-01T09:45:00+07:00"
        dt_in = datetime.fromisoformat(clock_in)
        dt_out = datetime.fromisoformat(clock_out)
        hours = (dt_out - dt_in).total_seconds() / 3600
        return max(0.0, round(hours, 4))
    except (ValueError, TypeError):
        return 0.0


async def run_fot_pipeline(target_date: date, tenant_id: int) -> dict:
    """Рассчитывает и сохраняет fot_daily для tenant за target_date.

    Возвращает сводку: {branches, rows_saved, no_rate_total}.
    """
    date_iso = target_date.isoformat()

    # 1. Все точки тенанта (из кеша) — нужен bo_url для salary API
    all_branches = get_all_branches()
    branches_of_tenant = [b for b in all_branches if b.get("tenant_id", 1) == tenant_id]
    if not branches_of_tenant:
        logger.warning(f"fot_pipeline tenant={tenant_id}: нет активных точек")
        return {"branches": 0, "rows_saved": 0, "no_rate_total": 0}

    # name → branch dict (для bo_url lookup)
    branch_by_name: dict[str, dict] = {b["name"]: b for b in branches_of_tenant}

    # 2. Смены из БД за эту дату для тенанта
    shifts = await get_fot_shifts_by_date(date_iso, tenant_id)
    if not shifts:
        logger.info(f"fot_pipeline tenant={tenant_id} {date_iso}: смен нет, пропускаем")
        return {"branches": 0, "rows_saved": 0, "no_rate_total": 0}

    # 3. Группируем смены по уникальным bo_url для батчевой загрузки ставок
    bo_url_to_shifts: dict[str, list[dict]] = defaultdict(list)
    for shift in shifts:
        branch = branch_by_name.get(shift["branch_name"])
        if not branch:
            continue
        bo_url_to_shifts[branch["bo_url"]].append(shift)

    # 4. Загружаем salary map для каждого уникального BO-сервера
    salary_maps: dict[str, dict[str, Decimal]] = {}  # bo_url → {employee_id: rate}
    async with httpx.AsyncClient(verify=False) as client:
        for bo_url, bo_shifts in bo_url_to_shifts.items():
            # Берём логин/пароль с первой точки этого bo_url
            sample_branch = branch_by_name.get(bo_shifts[0]["branch_name"])
            if not sample_branch:
                continue
            try:
                token = await get_bo_token(
                    bo_url,
                    client=client,
                    bo_login=sample_branch.get("bo_login"),
                    bo_password=sample_branch.get("bo_password"),
                )
                salary_maps[bo_url] = await fetch_salary_map(bo_url, client, token, target_date)
                logger.info(
                    f"fot_pipeline: salary {bo_url} → {len(salary_maps[bo_url])} записей"
                )
            except Exception as e:
                logger.error(f"fot_pipeline: salary error {bo_url}: {e}")
                salary_maps[bo_url] = {}

    # 5. Агрегируем ФОТ по (branch_name, category)
    # Структура: branch_name → category → {fot_sum, hours_sum, employees: set, no_rate: int}
    agg: dict[str, dict[str, dict]] = defaultdict(
        lambda: defaultdict(lambda: {"fot_sum": Decimal("0"), "hours_sum": 0.0, "employees": set(), "no_rate": 0})
    )

    no_rate_by_branch: dict[str, int] = defaultdict(int)

    for shift in shifts:
        branch_name = shift["branch_name"]
        branch = branch_by_name.get(branch_name)
        if not branch:
            continue

        role_class = shift.get("role_class") or "other"
        if role_class not in _FOT_CATEGORIES:
            continue

        hours = _calc_hours(shift.get("clock_in"), shift.get("clock_out"))
        if hours <= 0:
            continue

        employee_id = shift.get("employee_id", "")
        bo_url = branch["bo_url"]
        salary_map = salary_maps.get(bo_url, {})
        rate = salary_map.get(employee_id)

        if rate is None:
            agg[branch_name][role_class]["no_rate"] += 1
            no_rate_by_branch[branch_name] += 1
            continue

        agg[branch_name][role_class]["fot_sum"] += rate * Decimal(str(hours))
        agg[branch_name][role_class]["hours_sum"] += hours
        agg[branch_name][role_class]["employees"].add(employee_id)

    # 6. Уведомления о сотрудниках без ставки
    for branch_name, no_rate_cnt in no_rate_by_branch.items():
        if no_rate_cnt == 0:
            continue
        branch = branch_by_name.get(branch_name)
        city = branch.get("city", "") if branch else ""
        msg = (
            f"⚠️ ФОТ {branch_name}: у <b>{no_rate_cnt}</b> сотр. нет ставки в iiko — "
            f"данные занижены. Проверь /api/v2/employees/salary"
        )
        logger.warning(f"fot_pipeline: {msg}")
        try:
            alert_chats = await get_alert_chats_for_city(city, tenant_id)
            for chat_id in alert_chats:
                await telegram.send_message(str(chat_id), msg)
        except Exception as e:
            logger.error(f"fot_pipeline: уведомление не отправлено {branch_name}: {e}")

    # 7. Формируем строки для upsert
    rows: list[dict] = []
    for branch_name, cat_data in agg.items():
        for category, data in cat_data.items():
            rows.append({
                "branch_name": branch_name,
                "date": date_iso,
                "category": category,
                "fot_sum": float(data["fot_sum"]),
                "hours_sum": round(data["hours_sum"], 2),
                "employees_count": len(data["employees"]),
                "employees_no_rate": data["no_rate"],
            })

    if rows:
        await upsert_fot_daily_batch(rows, tenant_id)

    total_no_rate = sum(no_rate_by_branch.values())
    result = {
        "branches": len(agg),
        "rows_saved": len(rows),
        "no_rate_total": total_no_rate,
    }
    logger.info(f"fot_pipeline tenant={tenant_id} {date_iso}: {result}")
    return result


@track_job("fot_daily")
async def job_fot_pipeline() -> None:
    """Ежедневный ФОТ-пайплайн за вчера для всех тенантов.

    Запускается в 04:00 МСК — до ранних daily_report (05:25 МСК для UTC+7).
    """
    from app.database_pg import get_active_tenants_with_tokens

    yesterday = (datetime.now(timezone(timedelta(hours=3))) - timedelta(days=1)).date()

    tenants = await get_active_tenants_with_tokens()
    tenant_ids = {t["tenant_id"] for t in tenants} if tenants else {1}

    for tenant_id in sorted(tenant_ids):
        try:
            result = await run_fot_pipeline(yesterday, tenant_id)
            logger.info(f"job_fot_pipeline tenant={tenant_id} {yesterday}: {result}")
        except Exception as e:
            logger.error(f"job_fot_pipeline tenant={tenant_id}: {e}", exc_info=True)
