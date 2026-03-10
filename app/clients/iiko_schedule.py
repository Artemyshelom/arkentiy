"""
iiko BO Schedule API — ставки сотрудников для расчёта ФОТ.

Экспортирует:
  fetch_salary_map(bo_url, client, token, target_date) → dict[str, Decimal]
"""

import logging
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal

import httpx

logger = logging.getLogger(__name__)

# Ставки ниже этого значения считаются аномальными и пропускаются
_MIN_VALID_RATE = Decimal("100")


async def fetch_salary_map(
    bo_url: str,
    client: httpx.AsyncClient,
    token: str,
    target_date: date,
) -> dict[str, Decimal]:
    """Возвращает {employee_id: rate_per_hour} актуальные на target_date.

    Выполняет GET /api/v2/employees/salary для конкретного bo_url тенанта.
    Изоляция по тенанту обеспечивается автоматически — каждый bo_url принадлежит
    одному тенанту. Пропускает ставки < 100 ₽/ч как аномальные.
    """
    url = f"{bo_url.rstrip('/')}/api/v2/employees/salary"
    try:
        resp = await client.get(url, params={"key": token}, timeout=30)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.error(f"fetch_salary_map HTTP {e.response.status_code} for {bo_url}: {e}")
        return {}
    except Exception as e:
        logger.error(f"fetch_salary_map error for {bo_url}: {e}")
        return {}

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        logger.error(f"fetch_salary_map XML parse error for {bo_url}: {e}")
        return {}

    result: dict[str, Decimal] = {}

    for salary in root.findall(".//salary"):
        emp_id = (salary.findtext("employeeId") or "").strip()
        if not emp_id:
            continue

        date_from_str = (salary.findtext("dateFrom") or "")[:10]
        date_to_str = (salary.findtext("dateTo") or "")[:10]
        payment_str = salary.findtext("payment") or "0"

        try:
            rate = Decimal(payment_str)
        except Exception:
            continue

        if rate < _MIN_VALID_RATE:
            continue

        # Проверяем актуальность на target_date
        try:
            date_from = date.fromisoformat(date_from_str)
            date_to = date.fromisoformat(date_to_str)
        except ValueError:
            continue

        if not (date_from <= target_date < date_to):
            continue

        # Если несколько записей — оставляем с наибольшим dateFrom (последнее изменение)
        if emp_id in result:
            existing_entry = result.get(f"__df_{emp_id}")
            if existing_entry and date_from_str <= existing_entry:
                continue
        result[emp_id] = rate
        result[f"__df_{emp_id}"] = date_from_str  # служебный ключ для сравнения дат

    # Убираем служебные ключи
    return {k: v for k, v in result.items() if not k.startswith("__df_")}
