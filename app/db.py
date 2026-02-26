"""
Прокси-модуль для database backend.

Если DATABASE_URL начинается с postgresql:// — использует database_pg (asyncpg).
Иначе — использует database (aiosqlite, SQLite).

Все потребители импортируют из app.db вместо app.database напрямую.
"""

import os

_url = os.getenv("DATABASE_URL", "")

if _url.startswith("postgresql://") or _url.startswith("postgres://"):
    from app.database_pg import *  # noqa: F401,F403
    from app.database_pg import init_db as _pg_init, get_pool  # noqa: F401

    async def init_db() -> None:
        await _pg_init(_url)

    BACKEND = "postgresql"
else:
    from app.database import *  # noqa: F401,F403
    BACKEND = "sqlite"
