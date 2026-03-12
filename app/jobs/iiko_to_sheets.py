"""
Задача: Ежедневная выгрузка метрик iiko → Google Sheets.
Расписание: ежедневно в 23:31 местного (19:31 МСК).

Источник данных: daily_stats (заполнен olap_pipeline в 05:00, 0 OLAP запросов).
Логика:
  1. Инкрементальная запись — не дублировать уже существующие строки.
  2. Перепроверка вчерашнего дня — обновлять если daily_stats изменился.
  3. Еженедельная перепроверка (понедельник) — прогон за 7 дней.
  4. Флаги изменений пишутся в БД → утренний отчёт видит что изменилось.

13 колонок: Дата | Точка | Город | Выручка | Чеков | Средний чек |
            Наличные | Безналичные | Скидки | SailPlay ₽ | Самовывоз | С/с ₽ | С/с %
"""

import logging
from datetime import datetime, timedelta

from app.clients import telegram
from app.clients.google_sheets import (
    append_rows,
    clear_range,
    ensure_sheet_exists,
    read_range,
    write_range,
)
from app.clients.iiko_bo_olap_v2 import get_all_branches_stats
from app.config import get_settings
from app.db import (
    get_branches,
    get_daily_stats,
    log_job_finish,
    log_job_start,
    record_data_update,
)
from app.utils.job_tracker import track_job
from app.utils.timezone import branch_tz

logger = logging.getLogger(__name__)
settings = get_settings()

SHEET_NAME = "Выгрузка iiko"
SHEET_RANGE = f"{SHEET_NAME}!A:M"
HEADER_RANGE = f"{SHEET_NAME}!A1:M1"

HEADERS = [
    "Дата",
    "Точка",
    "Город",
    "Выручка (со скидкой), ₽",
    "Кол-во чеков",
    "Средний чек, ₽",
    "Наличные, ₽",
    "Безналичные, ₽",
    "Сумма скидок, ₽",
    "SailPlay, ₽",
    "Самовывоз (чеков)",
    "Себестоимость, ₽",
    "Себестоимость, %",
]

# Индексы колонок (0-based)
COL_DATE = 0
COL_BRANCH = 1
COL_CITY = 2
COL_REVENUE = 3
COL_CHECKS = 4
COL_AVG_CHECK = 5
COL_CASH = 6
COL_NONCASH = 7
COL_DISCOUNT = 8
COL_SAILPLAY = 9
COL_PICKUP = 10
COL_COGS_RUB = 11
COL_COGS_PCT = 12


def _fmt(v):
    """None → пустая строка для записи в Sheets."""
    return v if v is not None else ""


def _build_row(date_iso: str, branch: dict, s: dict) -> list:
    """Строит строку для Sheets из данных точки (daily_stats или OLAP-структура)."""
    revenue = s.get("revenue_net") or s.get("revenue")
    cogs_pct = s.get("cogs_pct")
    check_count = s.get("check_count") or s.get("orders_count")
    cash = s.get("cash")
    noncash = s.get("noncash")
    discount_sum = s.get("discount_sum")
    sailplay = s.get("sailplay")
    pickup_count = s.get("pickup_count")

    avg_check = round(revenue / check_count) if revenue and check_count else None
    cogs_rub = round(revenue * cogs_pct / 100) if revenue and cogs_pct is not None else None
    cogs_pct_display = round(cogs_pct, 2) if cogs_pct is not None else None

    return [
        date_iso,
        branch["name"],
        branch.get("city", ""),
        _fmt(round(revenue) if revenue else None),
        _fmt(check_count),
        _fmt(avg_check),
        _fmt(round(cash) if cash else None),
        _fmt(round(noncash) if noncash else None),
        _fmt(round(discount_sum) if discount_sum else None),
        _fmt(round(sailplay) if sailplay else None),
        _fmt(pickup_count),
        _fmt(cogs_rub),
        _fmt(cogs_pct_display),
    ]


async def _read_sheet_index() -> dict[str, dict[str, int]]:
    """
    Читает все строки из Sheets.
    Возвращает {date_iso: {branch_name: sheet_row_number}} (1-based, учитывая заголовок).
    Строка данных i (0-based от data_rows) → sheet row = i + 2.
    """
    try:
        all_rows = await read_range(settings.google_sheets_iiko_id, SHEET_RANGE)
    except Exception as e:
        logger.error(f"Ошибка чтения Sheets: {e}")
        return {}

    if not all_rows:
        return {}

    index: dict[str, dict[str, int]] = {}
    # all_rows[0] = заголовок, all_rows[1:] = данные
    for row_idx, row in enumerate(all_rows[1:], start=2):
        if len(row) < 2:
            continue
        date = str(row[COL_DATE]).strip()
        branch = str(row[COL_BRANCH]).strip()
        if date and branch:
            if date not in index:
                index[date] = {}
            index[date][branch] = row_idx
    return index


async def _read_sheet_rows_for_date(date_iso: str) -> dict[str, list]:
    """
    Читает строки Sheets для конкретной даты.
    Возвращает {branch_name: row_data}.
    """
    try:
        all_rows = await read_range(settings.google_sheets_iiko_id, SHEET_RANGE)
    except Exception as e:
        logger.error(f"Ошибка чтения Sheets для {date_iso}: {e}")
        return {}

    result = {}
    for row in (all_rows[1:] if all_rows else []):
        if len(row) >= 2 and str(row[COL_DATE]).strip() == date_iso:
            result[str(row[COL_BRANCH]).strip()] = row
    return result


def _safe_float(v) -> float | None:
    """Безопасный парсинг числа из ячейки Sheets."""
    try:
        return float(str(v).replace(",", ".")) if v not in ("", None) else None
    except (ValueError, TypeError):
        return None


async def _compare_and_update_date(
    date: datetime,
    index: dict[str, dict[str, int]],
    branches: list[dict] | None = None,
) -> list[str]:
    """
    Сравнивает данные в Sheets с daily_stats за дату.
    Обновляет строки если данные изменились.
    Возвращает список названий точек где были изменения.
    """
    date_iso = date.strftime("%Y-%m-%d")
    if branches is None:
        branches = settings.branches
    if not branches or date_iso not in index:
        return []

    # Читаем из daily_stats (пайплайн обновил в 05:00 или в понедельник)
    all_stats: dict[str, dict] = {}
    for branch in branches:
        row = await get_daily_stats(branch["name"], date_iso, branch.get("tenant_id", 1))
        if row:
            all_stats[branch["name"]] = row

    # Читаем текущие строки из Sheets для этой даты
    existing_rows = await _read_sheet_rows_for_date(date_iso)
    changed = []

    for branch in branches:
        name = branch["name"]
        row_num = index.get(date_iso, {}).get(name)
        if row_num is None:
            continue

        s = all_stats.get(name, {})
        new_row = _build_row(date_iso, branch, s)
        old_row = existing_rows.get(name, [])

        # Сравниваем ключевые поля: выручка, чеки, себестоимость
        old_revenue = _safe_float(old_row[COL_REVENUE]) if len(old_row) > COL_REVENUE else None
        new_revenue = _safe_float(new_row[COL_REVENUE]) if new_row[COL_REVENUE] != "" else None

        old_checks = _safe_float(old_row[COL_CHECKS]) if len(old_row) > COL_CHECKS else None
        new_checks = _safe_float(new_row[COL_CHECKS]) if new_row[COL_CHECKS] != "" else None

        revenue_changed = old_revenue != new_revenue and new_revenue is not None
        checks_changed = old_checks != new_checks and new_checks is not None

        if not (revenue_changed or checks_changed):
            continue

        # Обновляем строку в Sheets
        cell_range = f"{SHEET_NAME}!A{row_num}:M{row_num}"
        try:
            await write_range(settings.google_sheets_iiko_id, cell_range, [new_row])
            logger.info(f"Обновлена строка {row_num} для {name} за {date_iso}")
            changed.append(name)

            # Фиксируем в SQLite для утреннего отчёта
            _tid = branch.get("tenant_id", 1)
            if revenue_changed:
                await record_data_update(date_iso, name, "revenue", old_revenue, new_revenue, tenant_id=_tid)
            if checks_changed:
                await record_data_update(date_iso, name, "check_count", old_checks, new_checks, tenant_id=_tid)
        except Exception as e:
            logger.error(f"Ошибка обновления строки {name}: {e}")

    return changed


async def export_day(date: datetime, branches: list[dict] | None = None) -> list[list]:
    """
    Выгружает данные за один день из daily_stats. Возвращает список строк для Sheets.
    """
    if branches is None:
        branches = settings.branches
    if not branches:
        logger.error("Нет точек для выгрузки")
        return []

    date_iso = date.strftime("%Y-%m-%d")
    rows = []
    for branch in branches:
        s = await get_daily_stats(branch["name"], date_iso, branch.get("tenant_id", 1)) or {}
        if not s:
            logger.warning(f"Нет данных для {branch['name']} за {date_iso} в daily_stats")
        rows.append(_build_row(date_iso, branch, s))
    return rows


@track_job("iiko_to_sheets")
async def job_export_iiko_to_sheets(tenant_id: int | None = None) -> None:
    """
    Ежедневная выгрузка данных за сегодня в Google Sheets (23:31 местного).
    
    MULTI-TENANT: вызывается отдельно для каждого tenant_id.
    Если tenant_id не передан, то используется конфиг из settings.
    """
    # Если tenant_id не передан явно, используем tenant_id=1
    if tenant_id is None:
        tenant_id = 1
    
    # Для других тенантов можно дохечь из БД если понадобится, пока используем settings
    
    log_id = await log_job_start("iiko_to_sheets", tenant_id=tenant_id)

    if not settings.google_sheets_iiko_id:
        await log_job_finish(log_id, "error", "GOOGLE_SHEETS_IIKO_ID не задан")
        return

    branches = get_branches(tenant_id)
    if not branches:
        await log_job_finish(log_id, "error", f"Нет точек для tenant_id={tenant_id}")
        return

    tz = branch_tz(branches[0])
    # Джоб запускается утром (~09:25 местного) — экспортируем вчерашний закрытый день,
    # аналогично job_send_morning_report.
    today = datetime.now(tz)
    export_date = today - timedelta(days=1)
    date_iso = export_date.strftime("%Y-%m-%d")
    date_display = export_date.strftime("%d.%m.%Y")

    # Создаём лист + заголовок если нужно
    try:
        await ensure_sheet_exists(settings.google_sheets_iiko_id, SHEET_NAME)
        existing_header = await _get_header()
        if existing_header != HEADERS:
            await write_range(settings.google_sheets_iiko_id, HEADER_RANGE, [HEADERS])
    except Exception as e:
        logger.warning(f"Не удалось обновить заголовок: {e}")

    # Читаем индекс существующих строк
    index = await _read_sheet_index()
    date_in_sheets = date_iso in index and len(index[date_iso]) > 0

    written = 0
    if not date_in_sheets:
        try:
            rows = await export_day(export_date, branches=branches)
        except Exception as e:
            logger.error(f"Ошибка сбора данных: {e}")
            await telegram.error_alert("iiko_to_sheets", str(e))
            await log_job_finish(log_id, "error", str(e))
            return

        try:
            await append_rows(settings.google_sheets_iiko_id, SHEET_RANGE, rows)
            written = len(rows)
            logger.info(f"Записано {written} строк за {date_display}")
            # После добавления нужно обновить индекс для перепроверки
            index = await _read_sheet_index()
        except Exception as e:
            logger.error(f"Ошибка записи в Sheets: {e}")
            await telegram.error_alert("iiko_to_sheets", str(e))
            await log_job_finish(log_id, "error", str(e))
            return
    else:
        logger.info(f"Строки за {date_display} уже есть в Sheets — пропускаем append")

    # --- 2.2: Перепроверка вчерашнего дня (iiko может скорректировать числа) ---
    changed = await _compare_and_update_date(export_date, index, branches=branches)
    if changed:
        logger.info(f"Перепроверка {date_display}: обновлено точек: {len(changed)}")
    else:
        logger.info(f"Перепроверка {date_display}: изменений нет")

    # --- 2.3: Еженедельная перепроверка (понедельник) ---
    is_monday = datetime.now(tz).weekday() == 0
    if is_monday:
        logger.info("Понедельник: еженедельная перепроверка за 7 дней")
        for days_back in range(1, 7):
            past_date = export_date - timedelta(days=days_back)
            weekly_changed = await _compare_and_update_date(past_date, index, branches=branches)
            if weekly_changed:
                logger.info(f"Обновлено за {past_date.strftime('%d.%m.%Y')}: {len(weekly_changed)} точек")

    await log_job_finish(log_id, "ok", f"Записано: {written}, Обновлено: {len(changed)}")


async def _get_header() -> list[str]:
    """Читает первую строку заголовка из Sheets."""
    try:
        data = await read_range(settings.google_sheets_iiko_id, HEADER_RANGE)
        return data[0] if data else []
    except Exception:
        return []


async def reset_sheet_and_backfill(date_from: datetime, date_to: datetime) -> None:
    """
    Очищает лист, ставит заголовок и заполняет данными за диапазон дат.
    Запускается вручную через POST /backfill.
    """
    if not settings.google_sheets_iiko_id:
        logger.error("GOOGLE_SHEETS_IIKO_ID не задан")
        return

    logger.info(f"Сброс и бэкфилл: {date_from.date()} → {date_to.date()}")

    await ensure_sheet_exists(settings.google_sheets_iiko_id, SHEET_NAME)
    await clear_range(settings.google_sheets_iiko_id, SHEET_RANGE)
    await write_range(settings.google_sheets_iiko_id, HEADER_RANGE, [HEADERS])

    all_rows = []
    current = date_from
    while current <= date_to:
        logger.info(f"Бэкфилл: {current.strftime('%d.%m.%Y')}")
        try:
            rows = await export_day(current)
            all_rows.extend(rows)
        except Exception as e:
            logger.error(f"Ошибка за {current.date()}: {e}")
        current += timedelta(days=1)

    if all_rows:
        await append_rows(settings.google_sheets_iiko_id, SHEET_RANGE, all_rows)
        logger.info(f"Бэкфилл завершён: {len(all_rows)} строк")
    else:
        logger.warning("Бэкфилл: нет данных")
