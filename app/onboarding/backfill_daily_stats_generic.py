"""
backfill_daily_stats_generic.py — универсальный бэкфилл daily_stats из OLAP v2 для любого tenant'а.

Заполняет таблицу daily_stats: выручка, COGS, скидки, кол-во чеков, самовывоз.
НЕ заполняет времена (avg_cooking_min и т.д.) — те вычисляются из orders_raw отдельно.

Использование:
    python -m app.onboarding.cli backfill_daily_stats \
        --tenant-id 3 \
        --date-from 2025-02-01 \
        --date-to 2026-03-01

Архитектура:
  - Для каждого дня: 2 OLAP запроса
    1. Core (выручка, COGS, скидки, чеки)
    2. Pickup count (самовывоз)
  - UPSERT в daily_stats (по tenant_id, branch_name, date)
  - По дням последовательно (можно параллелить по точкам)
"""

import asyncio
import hashlib
import logging
import os
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import asyncpg
import httpx

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
    ):
        self.tenant_id = tenant_id
        self.date_from = date_from
        self.date_to = date_to
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
        """Получает iiko credentials для tenant, группирует по серверам."""
        rows = await self.pool.fetch(
            """SELECT DISTINCT 
               bo_url, bo_login, bo_password, city, dept_id
               FROM iiko_credentials
               WHERE tenant_id = $1 AND is_active = true
               ORDER BY city""",
            self.tenant_id,
        )
        return [dict(r) for r in rows]

    async def get_branch_names(self) -> list[str]:
        """Получает unique branch names для tenant из iiko_credentials."""
        rows = await self.pool.fetch(
            """SELECT DISTINCT city as branch_name 
               FROM iiko_credentials
               WHERE tenant_id = $1 AND is_active = true
               ORDER BY city""",
            self.tenant_id,
        )
        return [r["branch_name"] for r in rows]

    async def run(self):
        """Запускает backfill по всем дням."""
        await self.init_db()
        try:
            credentials = await self.get_iiko_credentials()
            if not credentials:
                logger.error(f"Нет iiko_credentials для tenant_id={self.tenant_id}")
                return

            # Группируем по серверам
            by_server = defaultdict(list)
            for cred in credentials:
                key = (cred["bo_url"], cred["bo_login"], cred["bo_password"])
                by_server[key].append(cred["city"])

            # По дням
            current = self.date_from
            today = date.today()
            yesterday = today - timedelta(days=1)

            while current <= min(self.date_to, yesterday):
                date_str = current.isoformat()
                next_str = (current + timedelta(days=1)).isoformat()

                logger.info(f"Обработка {date_str}...")

                async with httpx.AsyncClient(verify=False, timeout=60) as client:
                    tasks = [
                        self._fetch_and_upsert_day(
                            client, bo_url, bo_login, bo_password, 
                            branch_names, date_str, next_str
                        )
                        for (bo_url, bo_login, bo_password), branch_names in by_server.items()
                    ]
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                for result in results:
                    if isinstance(result, Exception):
                        logger.error(f"Ошибка: {result}")
                        self.stats["error"] += 1
                    else:
                        self.stats["ok"] += result
                        
                current += timedelta(days=1)

            self._print_summary()

        finally:
            await self.close_db()

    async def _fetch_and_upsert_day(
        self,
        client: httpx.AsyncClient,
        bo_url: str,
        bo_login: str,
        bo_password: str,
        branch_names: list[str],
        date_str: str,
        next_str: str,
    ) -> int:
        """Загружает OLAP за день и UPSERT в daily_stats."""
        try:
            # Auth
            token = await self._get_token(bo_url, bo_login, bo_password, client)
            
            # Fetch stats
            stats = await self._fetch_olap(bo_url, token, date_str, next_str, client)

            # UPSERT для каждой ветки
            count = 0
            for branch_name in branch_names:
                s = stats.get(branch_name, {})
                if not s.get("revenue_net"):
                    continue

                await self._upsert_daily_stat(branch_name, date_str, s)
                count += 1
                self.stats["branches"].add(branch_name)

            return count

        except Exception as e:
            logger.error(f"Ошибка для серверов {bo_url}: {e}")
            raise

    async def _get_token(self, bo_url: str, bo_login: str, bo_password: str, client: httpx.AsyncClient) -> str:
        """Получает iiko API token."""
        pw_hash = hashlib.sha1(bo_password.encode()).hexdigest()
        r = await client.get(
            f"{bo_url}/api/auth?login={bo_login}&pass={pw_hash}",
            timeout=30,
        )
        r.raise_for_status()
        return r.text.strip()

    async def _fetch_olap(
        self, bo_url: str, token: str, date_from: str, date_to: str, client: httpx.AsyncClient
    ) -> dict[str, dict]:
        """Два OLAP запроса: core и pickup count."""
        stats: dict[str, dict] = defaultdict(lambda: {
            "revenue_net": None,
            "cogs_pct": None,
            "check_count": 0,
            "discount_sum": 0.0,
            "pickup_count": 0,
        })

        # Query 1: core (выручка, COGS, скидки, чеки)
        try:
            body1 = {
                "reportType": "SALES",
                "buildSummary": "false",
                "groupByRowFields": ["Department"],
                "aggregateFields": [
                    "DishDiscountSumInt.withoutVAT",
                    "ProductCostBase.Percent",
                    "UniqOrderId.OrdersCount",
                    "DiscountSum",
                ],
                "filters": {
                    "OpenDate.Typed": {
                        "filterType": "DateRange",
                        "periodType": "CUSTOM",
                        "from": date_from,
                        "to": date_to,
                        "includeLow": "true",
                        "includeHigh": "false",
                    }
                },
            }

            r1 = await client.post(
                f"{bo_url}/api/v2/reports/olap?key={token}",
                json=body1,
                headers={"Content-Type": "application/json"},
                timeout=60,
            )
            r1.raise_for_status()

            for row in r1.json().get("data", []):
                dept = row.get("Department", "").strip()
                if not dept:
                    continue

                rev = row.get("DishDiscountSumInt.withoutVAT") or 0
                cogs_raw = row.get("ProductCostBase.Percent")
                disc = row.get("DiscountSum") or 0
                chk = row.get("UniqOrderId.OrdersCount") or 0

                stats[dept]["revenue_net"] = float(rev)
                stats[dept]["cogs_pct"] = round(float(cogs_raw) * 100, 2) if cogs_raw is not None else None
                stats[dept]["check_count"] = int(chk)
                stats[dept]["discount_sum"] = float(disc)

        except Exception as e:
            logger.warning(f"OLAP core error {bo_url}: {e}")

        # Query 2: pickup count
        try:
            body2 = {
                "reportType": "SALES",
                "buildSummary": "false",
                "groupByRowFields": ["Department", "Delivery.ServiceType"],
                "aggregateFields": ["UniqOrderId"],
                "filters": {
                    "OpenDate.Typed": {
                        "filterType": "DateRange",
                        "periodType": "CUSTOM",
                        "from": date_from,
                        "to": date_to,
                        "includeLow": "true",
                        "includeHigh": "false",
                    }
                },
            }

            r2 = await client.post(
                f"{bo_url}/api/v2/reports/olap?key={token}",
                json=body2,
                headers={"Content-Type": "application/json"},
                timeout=60,
            )
            r2.raise_for_status()

            for row in r2.json().get("data", []):
                dept = row.get("Department", "").strip()
                if not dept:
                    continue

                svc = (row.get("Delivery.ServiceType") or "").upper()
                if svc in ("САМОВЫВОЗ", "PICKUP"):
                    stats[dept]["pickup_count"] += int(row.get("UniqOrderId", 0) or 0)

        except Exception as e:
            logger.warning(f"OLAP pickup error {bo_url}: {e}")

        return dict(stats)

    async def _upsert_daily_stat(self, branch_name: str, date_iso: str, s: dict):
        """UPSERT в daily_stats."""
        avg_check = round((s["revenue_net"] or 0) / s["check_count"]) if s.get("check_count") else 0

        await self.pool.execute(
            """INSERT INTO daily_stats
               (tenant_id, branch_name, date, orders_count, revenue, avg_check,
                cogs_pct, discount_sum, pickup_count, updated_at)
              VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
              ON CONFLICT (tenant_id, branch_name, date)
              DO UPDATE SET
                orders_count = EXCLUDED.orders_count,
                revenue      = EXCLUDED.revenue,
                avg_check    = EXCLUDED.avg_check,
                cogs_pct     = EXCLUDED.cogs_pct,
                discount_sum = EXCLUDED.discount_sum,
                pickup_count = EXCLUDED.pickup_count,
                updated_at   = now()
            """,
            self.tenant_id,
            branch_name,
            date_iso,
            s.get("check_count") or 0,
            s.get("revenue_net") or 0.0,
            avg_check,
            s.get("cogs_pct"),
            s.get("discount_sum") or 0.0,
            s.get("pickup_count") or 0,
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

    args = parser.parse_args()

    date_from = date.fromisoformat(args.date_from)
    date_to = date.fromisoformat(args.date_to)

    backfiller = DailyStatsBackfiller(
        tenant_id=args.tenant_id,
        date_from=date_from,
        date_to=date_to,
    )

    await backfiller.run()


if __name__ == "__main__":
    asyncio.run(main())
