"""
iiko Cloud API клиент.
Документация: https://api-ru.iiko.services/docs

Особенности:
- Токен живёт ~15 минут, кэшируем в SQLite
- У каждого города свой organizationId
- Rate limit: 10 запросов/сек
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import httpx

from app.config import get_settings
from app.database import get_iiko_token, set_iiko_token

logger = logging.getLogger(__name__)
settings = get_settings()

BASE_URL = "https://api-ru.iiko.services/api/1"
REQUEST_TIMEOUT = 30.0


async def _get_fresh_token() -> str:
    """Получает новый токен от iiko API."""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.post(
            f"{BASE_URL}/access_token",
            json={"apiLogin": settings.iiko_api_key},
        )
        response.raise_for_status()
        token = response.json()["token"]
        logger.debug("Получен новый токен iiko")
        return token


async def get_token(city: str) -> str:
    """
    Возвращает актуальный токен для города.
    Использует кэш из SQLite, обновляет если истёк.
    Один токен работает для всех городов (одна учётная запись iiko).
    """
    cached = await get_iiko_token(city)
    if cached:
        return cached

    token = await _get_fresh_token()
    # Сохраняем с запасом -2 мин (токен живёт 15 мин)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=13)
    await set_iiko_token(city, token, expires_at)
    return token


async def _post(endpoint: str, city: str, body: dict, retry: int = 2) -> dict:
    """
    Выполняет POST-запрос к iiko API с автообновлением токена.
    retry=2: при 401 обновляет токен и повторяет.
    """
    for attempt in range(retry + 1):
        token = await get_token(city)
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            try:
                response = await client.post(
                    f"{BASE_URL}/{endpoint}",
                    json=body,
                    headers=headers,
                )
                if response.status_code == 401 and attempt < retry:
                    # Инвалидируем кэш и повторяем
                    logger.warning(f"iiko 401 для {city}, обновляю токен (попытка {attempt+1})")
                    await set_iiko_token(city, "", datetime.now(timezone.utc))
                    await asyncio.sleep(1)
                    continue
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"iiko HTTP ошибка {e.response.status_code} [{city}]: {e.response.text[:200]}")
                raise
    return {}


# --- Публичные методы ---

async def get_organizations() -> list[dict]:
    """Список организаций (получить organizationId для каждого города)."""
    first_city = settings.iiko_cities[0] if settings.iiko_cities else "default"
    data = await _post("organizations", first_city, {})
    return data.get("organizations", [])


async def get_nomenclature(city: str) -> dict[str, str]:
    """
    Возвращает словарь {productId: название} для города.
    Нужен для расшифровки стоп-листа — iiko не возвращает имена в стоп-листе.
    """
    org_id = settings.org_ids.get(city)
    if not org_id:
        return {}
    data = await _post("nomenclature", city, {"organizationId": org_id})
    result = {}
    for product in data.get("products", []):
        pid = product.get("id")
        name = product.get("name", "")
        if pid and name:
            result[pid] = name
    return result


async def get_stop_list(city: str) -> list[dict]:
    """
    Возвращает активный стоп-лист для города.
    Структура ответа iiko:
      terminalGroupStopLists -> items (терм.группы) -> items (позиции)
    Обогащает имена через номенклатуру.
    """
    org_id = settings.org_ids.get(city)
    if not org_id:
        logger.warning(f"Нет organizationId для города {city}")
        return []

    data = await _post("stop_lists", city, {"organizationIds": [org_id]})

    # Собираем все продуктовые ID из стоп-листа
    raw_items = []
    for outer in data.get("terminalGroupStopLists", []):
        for terminal_group in outer.get("items", []):
            for item in terminal_group.get("items", []):
                raw_items.append(item)

    if not raw_items:
        return []

    # Получаем имена из номенклатуры
    try:
        names = await get_nomenclature(city)
    except Exception as e:
        logger.warning(f"Не удалось загрузить номенклатуру [{city}]: {e}")
        names = {}

    result = []
    for item in raw_items:
        pid = item.get("productId", "")
        result.append({
            "productId": pid,
            "name": names.get(pid, f"SKU {item.get('sku', '?')}"),
            "balance": item.get("balance"),
            "sku": item.get("sku", ""),
        })
    return result


async def get_deliveries_today(city: str) -> list[dict]:
    """Заказы доставки за сегодня для города."""
    org_id = settings.org_ids.get(city)
    if not org_id:
        return []

    today = datetime.now().strftime("%Y-%m-%d")
    data = await _post(
        "deliveries/by_delivery_date_and_status",
        city,
        {
            "organizationIds": [org_id],
            "deliveryDateFrom": f"{today} 00:00:00",
            "deliveryDateTo": f"{today} 23:59:59",
            "statuses": ["Delivered", "Closed"],
        },
    )
    return data.get("deliveryOrders", [])


async def get_revenue_today(city: str) -> dict:
    """
    Выручка за сегодня для города (сумма закрытых заказов доставки).
    Возвращает: {total: float, orders_count: int, avg_check: float}
    """
    orders = await get_deliveries_today(city)
    total = sum(o.get("sum", 0) for o in orders)
    count = len(orders)
    return {
        "city": city,
        "total": round(total, 2),
        "orders_count": count,
        "avg_check": round(total / count, 2) if count > 0 else 0,
    }
