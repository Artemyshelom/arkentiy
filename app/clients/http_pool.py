"""
Глобальные httpx клиенты для переиспользования TCP/TLS соединений.

Инициализируются при старте приложения через lifespan, закрываются при остановке.
Вместо создания нового AsyncClient на каждый запрос используются эти экземпляры —
это устраняет накладные расходы на новое TCP-соединение и TLS handshake.
"""

import httpx

# Для запросов к Telegram Bot API
telegram_client: httpx.AsyncClient | None = None

# Для запросов к iiko BO (verify=False — self-signed cert)
iiko_client: httpx.AsyncClient | None = None

# Для запросов к ЮKassa
yukassa_client: httpx.AsyncClient | None = None


async def init_http_clients() -> None:
    """Создаёт глобальные httpx клиенты. Вызывается в lifespan при старте."""
    global telegram_client, iiko_client, yukassa_client
    telegram_client = httpx.AsyncClient(timeout=15.0)
    iiko_client = httpx.AsyncClient(verify=False, timeout=60.0)
    yukassa_client = httpx.AsyncClient(timeout=30.0)


async def close_http_clients() -> None:
    """Закрывает все глобальные httpx клиенты. Вызывается в lifespan при остановке."""
    for client in (telegram_client, iiko_client, yukassa_client):
        if client is not None:
            await client.aclose()
