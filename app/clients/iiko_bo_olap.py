"""
Общий клиент для iiko Web BO OLAP-пресетов.

Поддержка нескольких серверов: каждая точка в branches.json может иметь
своё поле bo_url — локальный сервер iiko Office с более свежими данными.
Если bo_url не задан — используется дефолтный chain-сервер.

Аутентификация: cookie-сессия (JSESSIONID) через POST /j_spring_security_check.
Запросы: GET /service/reports/report.jspx?presetId=...&dateFrom=...&dateTo=...

Используется в:
  - app/jobs/iiko_to_sheets.py
  - app/jobs/daily_report.py
  - app/jobs/iiko_status_report.py
"""

import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Дефолтный chain-сервер (fallback если у точки нет bo_url)
IIKO_BO_BASE = "https://tomat-i-chedder-ebidoebi-co.iiko.it/resto"

# OLAP пресеты iiko BO
PRESET_API_STATS = "2c0c11d7-48fa-48e6-91b3-26f169587b09"        # Выручка + COGS% по точке
PRESET_ORDER_SUMMARY = "1f56b9d3-13ca-6044-0148-5e7f38cd001f"    # Кол-во чеков по точке
PRESET_PAYMENT_TYPES = "5a8842c5-4681-40ce-ba2b-133d39efbb93"    # Типы оплаты по точке
PRESET_DISCOUNTS = "6a714099-1252-4c8c-a474-9151b79e375a"        # Скидки по точке
PRESET_DELIVERY_TYPES = "81dfa241-55a9-4b0a-b6e7-d4bb48dad9d5"  # Самовывоз/доставка

CASH_PAY_TYPES = {"Наличные"}
EXCLUDED_PAY_TYPES = {"SailPlay Бонус", "(без оплаты)"}

# Кеш cookie-сессий: {base_url: (cookie, timestamp)}
_session_cookies: dict[str, tuple[str, float]] = {}
SESSION_TTL = 1800  # 30 минут


async def get_session_cookie(base_url: str = IIKO_BO_BASE) -> str:
    """Возвращает (или обновляет) JSESSIONID для указанного сервера."""
    cached = _session_cookies.get(base_url)
    if cached and (time.time() - cached[1]) < SESSION_TTL:
        return cached[0]

    login = settings.iiko_bo_login
    password = settings.iiko_bo_password

    async with httpx.AsyncClient(verify=False, timeout=20, follow_redirects=True) as client:
        resp = await client.post(
            f"{base_url}/j_spring_security_check",
            data={"j_username": login, "j_password": password},
        )
        resp.raise_for_status()

        cookie = client.cookies.get("JSESSIONID")
        if not cookie:
            for c in resp.cookies:
                if c.name == "JSESSIONID":
                    cookie = c.value
                    break

        if not cookie:
            raise RuntimeError(f"JSESSIONID не получен для {base_url}")

        _session_cookies[base_url] = (cookie, time.time())
        logger.info(f"iiko BO cookie обновлена: {base_url}")
        return cookie


async def fetch_preset(
    preset_id: str, date_from: str, date_to: str, base_url: str = IIKO_BO_BASE
) -> str:
    """
    Запрашивает отчёт по пресету iiko BO с указанного сервера.
    date_from / date_to — формат dd.MM.yyyy.
    Возвращает XML-строку.
    """
    cookie = await get_session_cookie(base_url)
    url = (
        f"{base_url}/service/reports/report.jspx"
        f"?presetId={preset_id}&dateFrom={date_from}&dateTo={date_to}"
    )
    headers = {"Cookie": f"JSESSIONID={cookie}"}
    async with httpx.AsyncClient(verify=False, timeout=30) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.text


def clean_xml(xml_str: str) -> str:
    return xml_str.replace('<?xml-stylesheet type="text/xsl" href="report-view.xslt"?>', "")


def child_text(elem: ET.Element, tag: str) -> str | None:
    child = elem.find(tag)
    return child.text if child is not None else None


def to_float(text: str | None, default: float | None = None) -> float | None:
    if not text:
        return default
    try:
        return float(text.strip().replace(",", ".").replace("%", ""))
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Парсеры пресетов
# ---------------------------------------------------------------------------

def parse_api_stats(xml_str: str) -> dict[str, dict]:
    """Пресет .API-Статистика. → {dept_name: {revenue_net, cogs_pct}}"""
    result: dict[str, dict] = {}
    try:
        root = ET.fromstring(clean_xml(xml_str))
        for data_elem in root.findall("data"):
            dept = (child_text(data_elem, "Department") or "").strip()
            if not dept:
                continue
            revenue = None
            cogs_pct = None
            for child in data_elem:
                if child.tag == "DishDiscountSumInt.withoutVAT":
                    revenue = to_float(child.text)
                elif child.tag == "ProductCostBase.Percent":
                    cogs_pct = to_float((child.text or "").replace("%", "").strip())
            result[dept] = {"revenue_net": revenue, "cogs_pct": cogs_pct}
    except ET.ParseError as e:
        logger.error(f"Ошибка парсинга .API-Статистика.: {e}")
    return result


def parse_order_summary(xml_str: str) -> dict[str, int]:
    """Пресет Общий отчет по доставкам → {dept_name: check_count}"""
    result: dict[str, int] = {}
    try:
        root = ET.fromstring(clean_xml(xml_str))
        for data_elem in root.findall("data"):
            dept = (child_text(data_elem, "Department") or "").strip()
            if not dept:
                continue
            for child in data_elem:
                if child.tag == "UniqOrderId.OrdersCount":
                    val = to_float(child.text)
                    if val is not None:
                        result[dept] = result.get(dept, 0) + int(val)
    except ET.ParseError as e:
        logger.error(f"Ошибка парсинга Общий отчет по доставкам: {e}")
    return result


def parse_payment_types(xml_str: str) -> dict[str, dict]:
    """Пресет Типы оплат → {dept_name: {cash, noncash, sailplay}}"""
    result: dict[str, dict] = defaultdict(lambda: {"cash": 0.0, "noncash": 0.0, "sailplay": 0.0})
    try:
        root = ET.fromstring(clean_xml(xml_str))
        for data_elem in root.findall("data"):
            dept = (child_text(data_elem, "Department") or "").strip()
            pay_type = (child_text(data_elem, "PayTypes") or "").strip()
            if not dept:
                continue
            for child in data_elem:
                if child.tag == "DishDiscountSumInt":
                    amount = to_float(child.text) or 0.0
                    if pay_type == "SailPlay Бонус":
                        result[dept]["sailplay"] += amount
                    elif pay_type in CASH_PAY_TYPES:
                        result[dept]["cash"] += amount
                    elif pay_type not in EXCLUDED_PAY_TYPES:
                        result[dept]["noncash"] += amount
    except ET.ParseError as e:
        logger.error(f"Ошибка парсинга Типы оплат: {e}")
    return dict(result)


def parse_discounts(xml_str: str) -> dict[str, dict]:
    """Пресет Отчет по скидкам → {dept_name: {sum, types}}
    types — список ненулевых типов скидок (одна строка = один тип).
    """
    result: dict[str, dict] = {}
    try:
        root = ET.fromstring(clean_xml(xml_str))
        for data_elem in root.findall("data"):
            dept = (child_text(data_elem, "Department") or "").strip()
            if not dept:
                continue
            val = to_float(child_text(data_elem, "DiscountSum")) or 0.0
            disc_type = (child_text(data_elem, "OrderDiscount.Type") or "").strip()
            if dept not in result:
                result[dept] = {"sum": 0.0, "types": []}
            result[dept]["sum"] += val
            if disc_type and val > 0:
                result[dept]["types"].append(disc_type)
    except ET.ParseError as e:
        logger.error(f"Ошибка парсинга Отчет по скидкам: {e}")
    return result


def parse_delivery_types(xml_str: str) -> dict[str, int]:
    """Пресет Доставка/самовывоз → {dept_name: pickup_count}"""
    result: dict[str, int] = {}
    try:
        root = ET.fromstring(clean_xml(xml_str))
        for data_elem in root.findall("data"):
            dept = (child_text(data_elem, "Department") or "").strip()
            if not dept:
                continue
            service_type = ""
            for child in data_elem:
                if child.tag == "Delivery.ServiceType":
                    service_type = (child.text or "").strip()
                elif child.tag == "UniqOrderId" and service_type == "PICKUP":
                    val = to_float(child.text)
                    if val is not None:
                        result[dept] = result.get(dept, 0) + int(val)
    except ET.ParseError as e:
        logger.error(f"Ошибка парсинга Доставка/самовывоз: {e}")
    return result


# ---------------------------------------------------------------------------
# Запрос к конкретному серверу: все 5 пресетов, только нужные точки
# ---------------------------------------------------------------------------

async def _fetch_all_presets_from_server(
    base_url: str, date_str: str, target_names: set[str], include_delivery: bool = True
) -> dict[str, dict]:
    """
    Делает 5 (или 4) параллельных запроса к одному серверу iiko BO.
    Возвращает метрики только для точек из target_names.
    """
    stats: dict[str, dict] = defaultdict(dict)

    preset_ids = [
        PRESET_API_STATS,
        PRESET_ORDER_SUMMARY,
        PRESET_PAYMENT_TYPES,
        PRESET_DISCOUNTS,
    ]
    if include_delivery:
        preset_ids.append(PRESET_DELIVERY_TYPES)

    async def _fetch_one(preset_id: str) -> tuple[str, str]:
        try:
            xml = await fetch_preset(preset_id, date_str, date_str, base_url)
            return preset_id, xml
        except Exception as e:
            logger.error(f"Ошибка пресета {preset_id} от {base_url}: {e}")
            return preset_id, ""

    results = await asyncio.gather(*[_fetch_one(pid) for pid in preset_ids])

    for preset_id, xml in results:
        if not xml:
            continue
        try:
            if preset_id == PRESET_API_STATS:
                parsed = parse_api_stats(xml)
                for dept, data in parsed.items():
                    if dept in target_names:
                        stats[dept].update(data)

            elif preset_id == PRESET_ORDER_SUMMARY:
                parsed = parse_order_summary(xml)
                for dept, count in parsed.items():
                    if dept in target_names:
                        stats[dept]["check_count"] = count

            elif preset_id == PRESET_PAYMENT_TYPES:
                parsed = parse_payment_types(xml)
                for dept, data in parsed.items():
                    if dept in target_names:
                        stats[dept].update(data)

            elif preset_id == PRESET_DISCOUNTS:
                parsed = parse_discounts(xml)
                for dept, data in parsed.items():
                    if dept in target_names:
                        stats[dept]["discount_sum"] = data["sum"]
                        stats[dept]["discount_types"] = data["types"]

            elif preset_id == PRESET_DELIVERY_TYPES:
                parsed = parse_delivery_types(xml)
                for dept, count in parsed.items():
                    if dept in target_names:
                        stats[dept]["pickup_count"] = count

        except Exception as e:
            logger.error(f"Ошибка парсинга пресета {preset_id}: {e}")

    return dict(stats)


# ---------------------------------------------------------------------------
# Агрегированный запрос: все точки, по их локальным серверам
# ---------------------------------------------------------------------------

async def get_all_branches_stats(date: datetime) -> dict[str, dict]:
    """
    Запрашивает 5 OLAP-пресетов для каждой точки через её локальный сервер.
    Группирует точки по bo_url → один вызов на сервер → параллельно.

    Возвращает {dept_name: {revenue_net, cogs_pct, check_count, cash, noncash,
                             sailplay, discount_sum, pickup_count}}.
    """
    date_str = date.strftime("%d.%m.%Y")
    logger.info(f"Запрашиваю OLAP-пресеты iiko BO за {date_str}")

    # Группируем точки по серверу
    by_url: dict[str, set[str]] = defaultdict(set)
    for branch in settings.branches:
        url = branch.get("bo_url") or IIKO_BO_BASE
        by_url[url].add(branch["name"])

    # Запускаем параллельно по одному вызову на каждый сервер
    tasks = [
        _fetch_all_presets_from_server(url, date_str, names, include_delivery=True)
        for url, names in by_url.items()
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    merged: dict[str, dict] = {}
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"Ошибка запроса к серверу: {result}")
        elif isinstance(result, dict):
            merged.update(result)

    return merged


async def get_branch_olap_stats(date: datetime) -> dict[str, dict]:
    """
    Версия для /статус — 4 пресета (без доставки), через локальные серверы точек.
    """
    date_str = date.strftime("%d.%m.%Y")

    by_url: dict[str, set[str]] = defaultdict(set)
    for branch in settings.branches:
        url = branch.get("bo_url") or IIKO_BO_BASE
        by_url[url].add(branch["name"])

    tasks = [
        _fetch_all_presets_from_server(url, date_str, names, include_delivery=False)
        for url, names in by_url.items()
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    merged: dict[str, dict] = {}
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"Ошибка запроса к серверу: {result}")
        elif isinstance(result, dict):
            merged.update(result)

    return merged
