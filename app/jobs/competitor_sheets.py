"""
Экспорт меню конкурентов из SQLite в Google Sheets.

Одна таблица на город (маппинг в secrets/competitor_sheets.json).
Структура каждой таблицы:
  ⚙️ Конкуренты  — технический лист (редактируется сотрудниками)
  Сводка         — агрегированный обзор всех конкурентов
  [Имя]          — пивот-таблица: блюда × даты слепков + Δ-столбец

Запуск: из job_monitor_competitors() после скрапинга ИЛИ по команде /конкуренты.
"""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone

import httpx
from google.oauth2 import service_account
from googleapiclient.discovery import build

from app.config import get_settings
from app.db import (
    get_all_competitor_items_by_snapshot,
    get_competitor_last_snapshot,
    get_competitor_names,
)

logger = logging.getLogger(__name__)
settings = get_settings()

SA_EMAIL = "cursoraccountgooglesheets@cursor-487608.iam.gserviceaccount.com"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Цвета (RGB 0-1)
_COLOR_GREEN = {"red": 0.714, "green": 0.843, "blue": 0.659}   # подешевело
_COLOR_RED   = {"red": 0.918, "green": 0.600, "blue": 0.600}   # подорожало
_COLOR_ORANGE= {"red": 1.0,   "green": 0.800, "blue": 0.400}   # не найден в БД
_COLOR_HEADER= {"red": 0.263, "green": 0.263, "blue": 0.263}   # тёмно-серый
_COLOR_SUBHDR= {"red": 0.851, "green": 0.851, "blue": 0.851}   # светло-серый
_COLOR_WHITE = {"red": 1.0,   "green": 1.0,   "blue": 1.0}


# ---------------------------------------------------------------------------
# Google API helpers
# ---------------------------------------------------------------------------

def _get_service():
    creds = service_account.Credentials.from_service_account_file(
        settings.google_service_account_file, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _run_sync(fn):
    """Оборачивает синхронный Google API вызов в executor."""
    return asyncio.get_event_loop().run_in_executor(None, fn)


async def _get_sheet_id(service, spreadsheet_id: str, title: str) -> int | None:
    """Возвращает sheetId листа по имени или None."""
    def _get():
        meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        for s in meta.get("sheets", []):
            if s["properties"]["title"] == title:
                return s["properties"]["sheetId"]
        return None
    return await _run_sync(_get)


async def _ensure_sheet(service, spreadsheet_id: str, title: str) -> int:
    """Создаёт лист если нет, возвращает sheetId."""
    sheet_id = await _get_sheet_id(service, spreadsheet_id, title)
    if sheet_id is not None:
        return sheet_id

    def _create():
        body = {"requests": [{"addSheet": {"properties": {"title": title}}}]}
        resp = service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id, body=body
        ).execute()
        return resp["replies"][0]["addSheet"]["properties"]["sheetId"]

    sheet_id = await _run_sync(_create)
    logger.info(f"[Sheets] Создан лист: {title}")
    return sheet_id


async def _batch_update(service, spreadsheet_id: str, requests: list) -> None:
    """Выполняет batchUpdate с переданными requests."""
    if not requests:
        return
    def _do():
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()
    await _run_sync(_do)


async def _write_values(service, spreadsheet_id: str, range_name: str, values: list) -> None:
    def _do():
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute()
    await _run_sync(_do)


async def _read_values(service, spreadsheet_id: str, range_name: str) -> list[list]:
    def _do():
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=range_name
        ).execute()
        return result.get("values", [])
    return await _run_sync(_do)


async def _clear_range(service, spreadsheet_id: str, range_name: str) -> None:
    def _do():
        service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id, range=range_name
        ).execute()
    await _run_sync(_do)


# ---------------------------------------------------------------------------
# Pivot builder
# ---------------------------------------------------------------------------

_NO_CATEGORY = "—"  # заглушка для блюд без категории


def _build_pivot(
    items: list[dict],
) -> tuple[list[str], dict[str, dict[str, dict[str, float | None]]]]:
    """
    Строит пивот из списка {name, price, snapshot_date, category}.
    Возвращает (sorted_dates, {category: {dish: {date: price}}}).
    Блюда без категории попадают в группу _NO_CATEGORY.
    """
    dates_set: set[str] = set()
    # category → dish → date → price
    pivot: dict[str, dict[str, dict[str, float | None]]] = defaultdict(lambda: defaultdict(dict))
    # Для сохранения порядка категорий по первому появлению
    cat_order: list[str] = []
    seen_cats: set[str] = set()

    for row in items:
        cat = row.get("category") or _NO_CATEGORY
        name = row["name"]
        date = row["snapshot_date"]
        price = row["price"]
        pivot[cat][name][date] = price
        dates_set.add(date)
        if cat not in seen_cats:
            cat_order.append(cat)
            seen_cats.add(cat)

    sorted_dates = sorted(dates_set)
    # Конвертируем вложенные defaultdict в обычные dict
    result = {cat: dict(dishes) for cat, dishes in pivot.items()}
    return sorted_dates, result, cat_order


# ---------------------------------------------------------------------------
# Sheet: конкурент (пивот)
# ---------------------------------------------------------------------------

async def _write_competitor_sheet(
    service,
    spreadsheet_id: str,
    competitor_name: str,
    city: str,
) -> None:
    items = await get_all_competitor_items_by_snapshot(city, competitor_name)
    if not items:
        logger.warning(f"[Sheets] {competitor_name}: нет данных в БД")
        return

    dates, pivot_by_cat, cat_order = _build_pivot(items)
    sheet_id = await _ensure_sheet(service, spreadsheet_id, competitor_name)
    n_cols = 1 + len(dates) + 1  # Блюдо + даты + Δ
    delta_col_idx = n_cols - 1

    # Строка 1: заголовок
    delta_label = f"Δ (vs {_fmt_date(dates[-2])})" if len(dates) >= 2 else "Δ"
    header = ["Блюдо"] + [_fmt_date(d) for d in dates] + [delta_label]

    rows: list[list] = [header]
    # Мета-информация о строках для последующего форматирования
    cat_separator_rows: list[int] = []   # номера строк-разделителей (0-based)
    dish_row_ranges: list[tuple[int, int]] = []  # (start, end) строк блюд (0-based, exclusive)
    all_dish_rows: list[int] = []        # все строки с блюдами

    all_dishes_flat: list[tuple[str, dict[str, float | None]]] = []

    for cat in cat_order:
        dishes = pivot_by_cat[cat]
        dish_names = sorted(dishes.keys())

        # Строка-разделитель категории
        cat_row_idx = len(rows)
        rows.append([cat] + [""] * (n_cols - 1))
        cat_separator_rows.append(cat_row_idx)

        dish_start = len(rows)
        for dish in dish_names:
            row = [dish]
            for d in dates:
                p = dishes[dish].get(d)
                row.append(int(p) if p is not None else "")
            row.append(_calc_delta(dishes[dish], dates))
            rows.append(row)
            all_dish_rows.append(len(rows) - 1)
            all_dishes_flat.append((dish, dishes[dish]))
        dish_end = len(rows)
        dish_row_ranges.append((dish_start, dish_end))

    # Итоговые строки (по всем блюдам вместе)
    all_pivot_flat = {name: dates_prices for name, dates_prices in all_dishes_flat}
    avg_row = ["📊 Средняя цена"]
    cnt_row = ["📦 Позиций"]
    for d in dates:
        day_prices = [p for _, dp in all_dishes_flat for dd, p in dp.items() if dd == d and p is not None]
        avg_row.append(round(sum(day_prices) / len(day_prices)) if day_prices else "")
        cnt_row.append(len(day_prices))

    if len(dates) >= 2:
        prev_d, last_d = dates[-2], dates[-1]
        prev_prices = [dp.get(prev_d) for _, dp in all_dishes_flat if dp.get(prev_d)]
        last_prices = [dp.get(last_d) for _, dp in all_dishes_flat if dp.get(last_d)]
        avg_delta = (
            round(sum(last_prices) / len(last_prices) - sum(prev_prices) / len(prev_prices))
            if prev_prices and last_prices else ""
        )
        cnt_delta = len(last_prices) - len(prev_prices)
    else:
        avg_delta = ""
        cnt_delta = ""

    avg_row.append(avg_delta)
    cnt_row.append(cnt_delta)

    summary_start_row = len(rows) + 1  # +1 за пустую строку-разделитель
    rows.append([])
    rows.append(avg_row)
    rows.append(cnt_row)

    n_rows = len(rows)

    # Пишем данные
    await _clear_range(service, spreadsheet_id, f"'{competitor_name}'!A:ZZ")
    await _write_values(service, spreadsheet_id, f"'{competitor_name}'!A1", rows)

    # Форматирование
    requests = []

    # Заморозка строки 1 + столбца A
    requests.append({"updateSheetProperties": {
        "properties": {
            "sheetId": sheet_id,
            "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 1},
        },
        "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
    }})

    # Заголовок — тёмный фон, белый текст, жирный
    requests.append(_fmt_range(sheet_id, 0, 1, 0, n_cols, {
        "backgroundColor": _COLOR_HEADER,
        "textFormat": {"foregroundColor": _COLOR_WHITE, "bold": True},
        "horizontalAlignment": "CENTER",
    }))

    # Строки-разделители категорий — серый фон, жирный курсив
    _COLOR_CAT = {"red": 0.427, "green": 0.427, "blue": 0.427}
    for cat_row in cat_separator_rows:
        requests.append(_fmt_range(sheet_id, cat_row, cat_row + 1, 0, n_cols, {
            "backgroundColor": _COLOR_CAT,
            "textFormat": {"foregroundColor": _COLOR_WHITE, "bold": True, "italic": True},
            "horizontalAlignment": "LEFT",
        }))

    # Строки блюд — обычный стиль, столбец A жирный
    if all_dish_rows:
        requests.append(_fmt_range(sheet_id, all_dish_rows[0], all_dish_rows[-1] + 1, 0, 1, {
            "textFormat": {"bold": False},
        }))

    # Итоговые строки — светло-серый фон, жирный
    requests.append(_fmt_range(sheet_id, summary_start_row, summary_start_row + 2, 0, n_cols, {
        "backgroundColor": _COLOR_SUBHDR,
        "textFormat": {"bold": True},
    }))

    # Условное форматирование Δ-столбца (только строки с блюдами)
    if all_dish_rows:
        delta_ranges = []
        for start, end in dish_row_ranges:
            if start < end:
                delta_ranges.append(_grid_range(sheet_id, start, end, delta_col_idx, delta_col_idx + 1))
        if delta_ranges:
            requests.append({"addConditionalFormatRule": {"rule": {
                "ranges": delta_ranges,
                "booleanRule": {
                    "condition": {"type": "NUMBER_LESS", "values": [{"userEnteredValue": "0"}]},
                    "format": {"backgroundColor": _COLOR_GREEN},
                },
            }, "index": 0}})
            requests.append({"addConditionalFormatRule": {"rule": {
                "ranges": delta_ranges,
                "booleanRule": {
                    "condition": {"type": "NUMBER_GREATER", "values": [{"userEnteredValue": "0"}]},
                    "format": {"backgroundColor": _COLOR_RED},
                },
            }, "index": 1}})

    # Авто-ширина столбцов
    requests.append({"autoResizeDimensions": {
        "dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": n_cols}
    }})

    # Защита всего листа (editors=[SA])
    requests.append({"addProtectedRange": {"protectedRange": {
        "range": _grid_range(sheet_id, 0, None, 0, None),
        "description": "Авто-данные — только скрипт",
        "warningOnly": False,
        "editors": {"users": [SA_EMAIL]},
    }}})

    await _batch_update(service, spreadsheet_id, requests)
    total_dishes = sum(len(d) for d in pivot_by_cat.values())
    logger.info(f"[Sheets] {competitor_name}: {total_dishes} блюд, {len(cat_order)} категорий, {len(dates)} слепков")


def _calc_delta(dish_dates: dict[str, float | None], dates: list[str]) -> str | int:
    """Считает дельту между последним и предпоследним снапшотом."""
    if len(dates) < 2:
        return ""
    last_d, prev_d = dates[-1], dates[-2]
    last_p = dish_dates.get(last_d)
    prev_p = dish_dates.get(prev_d)
    if last_p is None and prev_p is not None:
        return "REM"
    if last_p is not None and prev_p is None:
        return "NEW"
    if last_p is not None and prev_p is not None:
        diff = int(last_p - prev_p)
        return diff if diff != 0 else 0
    return ""


# ---------------------------------------------------------------------------
# Sheet: Сводка
# ---------------------------------------------------------------------------

async def _write_summary_sheet(
    service,
    spreadsheet_id: str,
    city: str,
    competitors: list[tuple[str, str]],  # [(city, name)]
) -> None:
    SHEET = "Сводка"
    sheet_id = await _ensure_sheet(service, spreadsheet_id, SHEET)

    header = ["Конкурент", "Слепков", "Последний слепок", "Позиций", "Ср. цена", "Δ ср. цены"]
    rows: list[list] = [header]

    for _, name in competitors:
        items = await get_all_competitor_items_by_snapshot(city, name)
        if not items:
            rows.append([name, 0, "—", "—", "—", "—"])
            continue

        dates, pivot_by_cat, _ = _build_pivot(items)
        # Плоский список всех блюд по всем категориям
        all_dishes = {dish: dp for cat_d in pivot_by_cat.values() for dish, dp in cat_d.items()}
        last_d = dates[-1]
        last_prices = [dp.get(last_d) for dp in all_dishes.values() if dp.get(last_d) is not None]
        last_avg = round(sum(last_prices) / len(last_prices)) if last_prices else 0

        if len(dates) >= 2:
            prev_d = dates[-2]
            prev_prices = [dp.get(prev_d) for dp in all_dishes.values() if dp.get(prev_d) is not None]
            prev_avg = round(sum(prev_prices) / len(prev_prices)) if prev_prices else 0
            avg_delta = last_avg - prev_avg
            delta_str = f"+{avg_delta}" if avg_delta > 0 else str(avg_delta)
        else:
            delta_str = "—"

        snap = await get_competitor_last_snapshot(city, name)
        snap_date = snap["date"] if snap else "—"
        snap_count = len(dates)

        rows.append([name, snap_count, snap_date, len(last_prices), last_avg, delta_str])

    await _clear_range(service, spreadsheet_id, f"'{SHEET}'!A:Z")
    await _write_values(service, spreadsheet_id, f"'{SHEET}'!A1", rows)

    n_rows = len(rows)
    n_cols = len(header)
    requests = []

    # Заголовок
    requests.append(_fmt_range(sheet_id, 0, 1, 0, n_cols, {
        "backgroundColor": _COLOR_HEADER,
        "textFormat": {"foregroundColor": _COLOR_WHITE, "bold": True},
        "horizontalAlignment": "CENTER",
    }))
    # Заморозка строки 1
    requests.append({"updateSheetProperties": {
        "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
        "fields": "gridProperties.frozenRowCount",
    }})
    # Авто-ширина
    requests.append({"autoResizeDimensions": {
        "dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": n_cols}
    }})
    # Защита всего листа
    requests.append({"addProtectedRange": {"protectedRange": {
        "range": _grid_range(sheet_id, 0, None, 0, None),
        "description": "Авто-данные — только скрипт",
        "warningOnly": False,
        "editors": {"users": [SA_EMAIL]},
    }}})

    await _batch_update(service, spreadsheet_id, requests)
    logger.info(f"[Sheets] Сводка обновлена: {len(competitors)} конкурентов")


# ---------------------------------------------------------------------------
# Sheet: ⚙️ Конкуренты (технический)
# ---------------------------------------------------------------------------

async def _sync_tech_sheet(
    service,
    spreadsheet_id: str,
    city: str,
    db_competitors: list[tuple[str, str]],  # [(city, name)] с данными в БД
) -> list[str]:
    """
    Читает лист '⚙️ Конкуренты', сверяет с БД.
    Обновляет статус/дату/позиций для известных конкурентов.
    Подсвечивает оранжевым тех, кого нет в БД.
    Возвращает список имён без данных в БД (для TG-алерта).
    """
    SHEET = "⚙️ Конкуренты"
    sheet_id = await _ensure_sheet(service, spreadsheet_id, SHEET)

    # Читаем текущее содержимое
    values = await _read_values(service, spreadsheet_id, f"'{SHEET}'!A:E")

    # Если лист пустой или только заголовок — инициализируем заголовок
    header = ["Название", "Сайт", "Статус", "Последний слепок", "Позиций"]
    if not values or values[0] != header:
        await _write_values(service, spreadsheet_id, f"'{SHEET}'!A1", [header])
        # Защита заголовка и авто-столбцов + разрешение редактировать A:B
        await _apply_tech_sheet_protection(service, spreadsheet_id, sheet_id)
        logger.info(f"[Sheets] '{SHEET}': создан заголовок")
        return []

    db_names = {name for _, name in db_competitors}
    not_found: list[str] = []
    update_requests = []

    for row_idx, row in enumerate(values[1:], start=1):  # пропускаем заголовок
        if not row or not row[0].strip():
            continue
        comp_name = row[0].strip()

        snap = await get_competitor_last_snapshot(city, comp_name)

        if snap is None:
            # Нет данных в БД — подсвечиваем оранжевым, статус ⚠️
            status = "⚠️ Не найден в БД"
            snap_date = ""
            positions = ""
            not_found.append(comp_name)
            bg = _COLOR_ORANGE
        else:
            status = "✅ Активен"
            snap_date = snap["date"]
            positions = snap["items_count"] or ""
            bg = _COLOR_WHITE

        # Обновляем C, D, E
        col_c = _grid_range(sheet_id, row_idx, row_idx + 1, 2, 5)
        update_requests.append({"repeatCell": {
            "range": col_c,
            "cell": {"userEnteredFormat": {"backgroundColor": bg}},
            "fields": "userEnteredFormat.backgroundColor",
        }})

        # Пишем значения C:E через values update
        await _write_values(
            service, spreadsheet_id,
            f"'{SHEET}'!C{row_idx + 1}:E{row_idx + 1}",
            [[status, snap_date, positions]],
        )

    # Форматирование
    requests = []
    # Заголовок
    requests.append(_fmt_range(sheet_id, 0, 1, 0, 5, {
        "backgroundColor": _COLOR_HEADER,
        "textFormat": {"foregroundColor": _COLOR_WHITE, "bold": True},
        "horizontalAlignment": "CENTER",
    }))
    requests.append({"updateSheetProperties": {
        "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
        "fields": "gridProperties.frozenRowCount",
    }})
    requests.append({"autoResizeDimensions": {
        "dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 5}
    }})
    requests += update_requests
    await _batch_update(service, spreadsheet_id, requests)

    # Применяем защиту (идемпотентно — пересоздаём)
    await _apply_tech_sheet_protection(service, spreadsheet_id, sheet_id)

    return not_found


async def _apply_tech_sheet_protection(service, spreadsheet_id: str, sheet_id: int) -> None:
    """
    Защита тех. листа:
    - Строка 1 (заголовок) — защита, editors=[SA]
    - Столбцы C:E (авто-поля) — защита, editors=[SA]
    - Столбцы A:B — свободны для сотрудников
    """
    # Сначала удаляем старые защиты этого листа чтобы не дублировать
    def _get_protections():
        meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        for s in meta.get("sheets", []):
            if s["properties"]["sheetId"] == sheet_id:
                return s.get("protectedRanges", [])
        return []

    old_protections = await _run_sync(_get_protections)
    delete_requests = [
        {"deleteProtectedRange": {"protectedRangeId": p["protectedRangeId"]}}
        for p in old_protections
    ]

    add_requests = [
        # Заголовок (строка 1)
        {"addProtectedRange": {"protectedRange": {
            "range": _grid_range(sheet_id, 0, 1, 0, None),
            "description": "Заголовок — не редактировать",
            "warningOnly": False,
            "editors": {"users": [SA_EMAIL]},
        }}},
        # Столбцы C:E (индексы 2-4)
        {"addProtectedRange": {"protectedRange": {
            "range": _grid_range(sheet_id, 1, None, 2, 5),
            "description": "Авто-поля (Статус/Слепок/Позиций) — не редактировать",
            "warningOnly": False,
            "editors": {"users": [SA_EMAIL]},
        }}},
    ]

    await _batch_update(service, spreadsheet_id, delete_requests + add_requests)


# ---------------------------------------------------------------------------
# TG alert
# ---------------------------------------------------------------------------

async def _send_tg_alert(text: str) -> None:
    """Отправляет алерт о новых конкурентах в личку Артемию."""
    token = settings.telegram_analytics_bot_token
    chat_id = settings.telegram_admin_id
    if not token or not chat_id:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
    except Exception as e:
        logger.error(f"[Sheets] TG alert error: {e}")


# ---------------------------------------------------------------------------
# Главная точка входа
# ---------------------------------------------------------------------------

async def export_all_competitors_to_sheets() -> None:
    """
    Экспортирует данные всех конкурентов из БД в Google Sheets.
    Итерирует по городам из competitor_sheets.json.
    Не делает re-scrape — только читает БД.
    """
    sheets_config = settings.competitor_sheets
    if not sheets_config:
        logger.warning("[Sheets] competitor_sheets.json пуст — экспорт пропущен")
        return

    # Строим множество неактивных конкурентов по cities из competitors.json
    inactive: set[tuple[str, str]] = set()
    for city_name, comps in settings.competitors.items():
        for c in comps:
            if not c.get("active", True):
                inactive.add((city_name, c["name"]))

    # Все конкуренты с данными в БД — исключаем inactive
    all_db_competitors = await get_competitor_names()
    db_by_city: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for city, name in all_db_competitors:
        if (city, name) in inactive:
            logger.info(f"[Sheets] Пропускаем {name} ({city}) — active: false")
            continue
        db_by_city[city].append((city, name))

    try:
        service = _get_service()
    except Exception as e:
        logger.error(f"[Sheets] Не удалось инициализировать Google API: {e}")
        return

    for city, spreadsheet_id in sheets_config.items():
        logger.info(f"[Sheets] Экспорт города: {city} → {spreadsheet_id}")
        city_competitors = db_by_city.get(city, [])

        try:
            # 1. Технический лист
            not_found = await _sync_tech_sheet(
                service, spreadsheet_id, city, city_competitors
            )

            # 2. TG алерт если есть новые конкуренты в листе без данных
            if not_found:
                names_str = "\n".join(f"  • {n}" for n in not_found)
                await _send_tg_alert(
                    f"⚠️ <b>Конкуренты [{city}] без данных в БД</b>\n\n"
                    f"{names_str}\n\n"
                    f"Добавь их в <code>secrets/competitors.json</code> и запусти скрапинг."
                )

            # 3. Листы конкурентов
            for _, name in city_competitors:
                try:
                    await _write_competitor_sheet(service, spreadsheet_id, name, city)
                except Exception as e:
                    logger.error(f"[Sheets] Ошибка листа {name}: {e}", exc_info=True)

            # 4. Сводка
            if city_competitors:
                await _write_summary_sheet(service, spreadsheet_id, city, city_competitors)

        except Exception as e:
            logger.error(f"[Sheets] Ошибка экспорта города {city}: {e}", exc_info=True)

    logger.info("[Sheets] Экспорт завершён")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_date(iso_date: str) -> str:
    """'2026-02-11' → '11.02.26'"""
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d.%m.%y")
    except ValueError:
        return iso_date


def _grid_range(sheet_id: int, start_row=None, end_row=None, start_col=None, end_col=None) -> dict:
    r = {"sheetId": sheet_id}
    if start_row is not None:
        r["startRowIndex"] = start_row
    if end_row is not None:
        r["endRowIndex"] = end_row
    if start_col is not None:
        r["startColumnIndex"] = start_col
    if end_col is not None:
        r["endColumnIndex"] = end_col
    return r


def _fmt_range(sheet_id: int, r1: int, r2: int, c1: int, c2: int, fmt: dict) -> dict:
    return {"repeatCell": {
        "range": _grid_range(sheet_id, r1, r2, c1, c2),
        "cell": {"userEnteredFormat": fmt},
        "fields": "userEnteredFormat(" + ",".join(fmt.keys()) + ")",
    }}
