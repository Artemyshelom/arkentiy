"""
ЮKassa API клиент (async, через httpx).

API docs: https://yookassa.ru/developers/api
Auth: Basic (shop_id:secret_key)
"""

import logging
import uuid

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.yookassa.ru/v3"
REQUEST_TIMEOUT = 30.0


def _auth() -> tuple[str, str]:
    s = get_settings()
    return (s.yukassa_shop_id, s.yukassa_secret_key)


async def create_payment(
    amount: int,
    description: str,
    return_url: str,
    metadata: dict | None = None,
    save_payment_method: bool = False,
    payment_method_id: str | None = None,
) -> dict:
    """
    Создаёт платёж в ЮKassa.

    Args:
        amount: сумма в рублях (целое число)
        description: описание платежа
        return_url: URL для возврата после оплаты
        metadata: произвольные данные (tenant_id, payment_id)
        save_payment_method: сохранить способ оплаты для рекуррентных платежей
        payment_method_id: ID сохранённого способа оплаты (для автосписаний)

    Returns:
        dict с полями: id, status, confirmation.confirmation_url, ...
    """
    payload: dict = {
        "amount": {"value": f"{amount}.00", "currency": "RUB"},
        "description": description,
        "capture": True,
    }

    if payment_method_id:
        # Автосписание по сохранённому способу оплаты
        payload["payment_method_id"] = payment_method_id
    else:
        # Обычный платёж с редиректом
        payload["confirmation"] = {"type": "redirect", "return_url": return_url}
        if save_payment_method:
            payload["save_payment_method"] = True

    if metadata:
        payload["metadata"] = metadata

    idempotence_key = str(uuid.uuid4())

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{BASE_URL}/payments",
            json=payload,
            auth=_auth(),
            headers={"Idempotence-Key": idempotence_key},
        )

    if resp.status_code not in (200, 201):
        logger.error(f"ЮKassa create_payment error: {resp.status_code} {resp.text}")
        raise YukassaError(f"Ошибка создания платежа: {resp.status_code}")

    data = resp.json()
    logger.info(f"ЮKassa платёж создан: {data['id']} status={data['status']}")
    return data


async def get_payment(payment_id: str) -> dict:
    """Получает информацию о платеже."""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.get(
            f"{BASE_URL}/payments/{payment_id}",
            auth=_auth(),
        )

    if resp.status_code != 200:
        logger.error(f"ЮKassa get_payment error: {resp.status_code} {resp.text}")
        raise YukassaError(f"Платёж не найден: {payment_id}")

    return resp.json()


class YukassaError(Exception):
    pass
