"""
Прокси-модуль для database backend (PostgreSQL через asyncpg).

Все потребители импортируют из app.db вместо app.database_pg напрямую.
"""

import os

from app.database_pg import *  # noqa: F401,F403
from app.database_pg import init_db as _pg_init, get_pool  # noqa: F401

_url = os.getenv("DATABASE_URL", "")

if not (_url.startswith("postgresql://") or _url.startswith("postgres://")):
    raise RuntimeError(
        "DATABASE_URL не задан или не PostgreSQL. "
        "SQLite backend удалён — используйте PostgreSQL."
    )


async def init_db() -> None:
    await _pg_init(_url)


BACKEND = "postgresql"
