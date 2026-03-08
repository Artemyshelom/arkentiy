"""
set_chat_avatars.py — установить аватарки бота во всех чатах тенанта.

Использование:
    python -m app.onboarding.set_chat_avatars --tenant-id 2

Бот должен быть администратором в каждом чате с правом can_change_info.
Если прав нет — предупреждение в лог, продолжаем дальше.
"""

import argparse
import asyncio
import logging

from app.database_pg import init_pool, get_all_tenant_chats
from app.services.access_manager import set_chat_avatar, AVATAR_MAP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("set_chat_avatars")


async def run(tenant_id: int) -> None:
    await init_pool()
    chats = await get_all_tenant_chats(tenant_id)

    if not chats:
        logger.warning(f"Тенант {tenant_id}: чатов не найдено.")
        return

    logger.info(f"Тенант {tenant_id}: найдено {len(chats)} чатов")
    logger.info(f"Паттерны аватарок: {[p for p, _ in AVATAR_MAP]}")

    ok = 0
    skipped = 0
    for chat in chats:
        chat_id = chat["chat_id"]
        name = chat["name"]
        result = await set_chat_avatar(chat_id, name)
        if result:
            ok += 1
            logger.info(f"  ✅ {name} ({chat_id})")
        else:
            skipped += 1
            logger.info(f"  — {name} ({chat_id}) — паттерн не совпал или нет прав")
        await asyncio.sleep(0.5)  # rate limit Telegram

    logger.info(f"\nГотово. Установлено: {ok}, пропущено: {skipped}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Установить аватарки в чатах тенанта")
    parser.add_argument("--tenant-id", type=int, required=True, help="ID тенанта")
    args = parser.parse_args()
    asyncio.run(run(args.tenant_id))
