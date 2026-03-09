"""
backfill_daily_stats_generic.py — универсальный бэкфилл daily_stats из OLAP v2 для любого tenant'а.

Заполняет таблицу daily_stats: выручка, COGS, скидки, чеки, самовывоз, нал/безнал.

Использование:
    python -m app.onboarding.cli backfill_daily_stats \
        --tenant-id 3 \
        --date-from 2025-02-01 \
        --date-to 2026-03-01

Архитектура:
  - Для каждого дня: fetch_branch_aggregate (Query C: 3 параллельных sub-запроса)
  - UPSERT в daily_stats (tenant_id, branch_name, date)
  - По дням последовательно
"""

import asyncio
import logging
import os
from datetime import date, timedelta

import asyncpg

from app.clients.olap_queries import fetch_branch_aggregate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill_daily_stats_generic")


class DailyStatsBackfiller:
    def __init__(
        self,
        tenant_id: int,
        date_from: date,
        date_to: date,
        skip_cities: set[str] = None,
    ):
        self.tenant_id = tenant_id
        self.date_from = date_from
        self.date_to = date_to
        self.skip_cities = skip_cities or set()
        self.pool: asyncpg.Pool = None
        self.stats = {"ok": 0, "error": 0, "branches": set()}

    async def init_db(self):
        """Инициализирует БД соединение."""
        self.pool = await asyncpg.create_pool(
            os.getenv("DATABASE_URL", "postgresql://localhost/arkentiy"),
            min_size=2,
            max_size=5,
        )

    async def close_db(self):
        """Закрывает БД соединение."""
        if self.pool:
            await self.pool.close()

    async def get_iiko_credentials(self) -> list[dict]:
        """Получает iiko credentials для tenant.
        
        ВАЖНО: в iiko_credentials есть и city (человеческое название) 
        и branch_name (техническое, совпадает с Department в OLAP).
        Используем branch_name для матчинга с OLAP ответами.
        """
        rows = await self.pool.fetch(
            """SELECT DISTINCT 
               bo_url, bo_login, bo_password, city, branch_name, dept_id
               FROM iiko_credentials
               WHERE tenant_id = $1 AND is_active = true
               ORDER BY city""",
            self.tenant_id,
        )
        # Фильтруем skip_cities (по city — читаемое название)
        return [dict(r) for r in rows if r["city"] not in self.skip_cities]

    async def run(self):
        """Запускает backfill по всем дням."""
        await self.init_db()
        try:
            credentials = await self.get_iiko_credentials()
            if not credentials:
                logger.error(f"Нет iiko_credentials для tenant_id={self.tenant_id}")
                return

            # Формируем список точек для fetch_branch_aggregate
            branches = [
                {
                    "bo_url": cred["bo_url"],
                    "bo_login": cred["bo_login"],
                    "bo_password": cred["bo_password"],
                    "name": cred["branch_name"],
                }
                for cred in credentials
            ]
            branch_names_set = {cred["branch_name"] for cred in credentials}

            current = self.date_from
            yesterday = date.today() - timedelta(days=1)

            while current <= min(self.date_to, yesterday):
                date_str = current.isoformat()
                next_str = (current + timedelta(days=1)).isoformat()

                logger.info(f"Обработка {date_str}...")
                try:
                    stats = await fetch_branch_aggregate(date_str, next_str, branches)

                    upserted = 0
                    for branch_name in branch_names_set:
                        s = stats.get(branch_name, {})
                        if not s.get("revenue_net"):
                            continue
                        await self._upsert_daily_stat(branch_name, date_str, s)
                        upserted += 1
                        self.stats["branches"].add(branch_name)
                        self.stats["ok"] += 1

                    if upserted == 0:
                        logger.warning(f"  {date_str}: нет данных ни по одной точке")

                except Exception as e:
                    logger.error(f"  {date_str}: {e}")
                    self.stats["error"] += 1

                current += timedelta(days=1)

            self._print_summary()

        finally:
            await self.close_db()

    async def _upsert_daily_stat(self, branch_name: str, date_iso: str, s: dict):
        """UPSERT в daily_stats, включая cash/noncash из Query C."""
        date_obj = date.fromisoformat(date_iso)
        avg_check = round((s["revenue_net"] or 0) / s["check_count"]) if s.get("check_count") else 0

        await self.pool.execute(
            """INSERT INTO daily_stats
               (tenant_id, branch_name, date, orders_count, revenue, avg_check,
                cogs_pct, discount_sum, pickup_count, cash, noncash, updated_at)
              VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now())
              ON CONFLICT (tenant_id, branch_name, date)
              DO UPDATE SET
                orders_count = EXCLUDED.orders_count,
                revenue      = EXCLUDED.revenue,
                avg_check    = EXCLUDED.avg_check,
                cogs_pct     = EXCLUDED.cogs_pct,
                discount_sum = EXCLUDED.discount_sum,
                pickup_count = EXCLUDED.pickup_count,
                cash         = EXCLUDED.cash,
                noncash      = EXCLUDED.noncash,
                updated_at   = now()
            """,
            self.tenant_id,
            branch_name,
            date_obj,
            s.get("check_count") or 0,
            s.get("revenue_net") or 0.0,
            avg_check,
            s.get("cogs_pct"),
            s.get("discount_sum") or 0.0,
            s.get("pickup_count") or 0,
            s.get("cash") or 0.0,
            s.get("noncash") or 0.0,
        )

    def _print_summary(self):
        """Выводит итоговую статистику."""
        logger.info("\n" + "=" * 60)
        logger.info(f"ИТОГИ БЭКФИЛЛА daily_stats (tenant_id={self.tenant_id})")
        logger.info("=" * 60)
        logger.info(f"Успешно: {self.stats['ok']} записей")
        logger.info(f"Ошибок: {self.stats['error']}")
        logger.info(f"Филиалы: {', '.join(sorted(self.stats['branches']))}")


async def main():
    """Entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Generic backfill для daily_stats")
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--date-from", type=str, required=True, help="YYYY-MM-DD")
    parser.add_argument("--date-to", type=str, required=True, help="YYYY-MM-DD")
    parser.add_argument("--skip-cities", type=str, help="Города через запятую")

    args = parser.parse_args()

    date_from = date.fromisoformat(args.date_from)
    date_to = date.fromisoformat(args.date_to)
    skip_cities = set(c.strip() for c in (args.skip_cities or "").split(",") if c.strip())

    backfiller = DailyStatsBackfiller(
        tenant_id=args.tenant_id,
        date_from=date_from,
        date_to=date_to,
        skip_cities=skip_cities,
    )

    await backfiller.run()


if __name__ == "__main__":
    asyncio.run(main())
