"""
backfill_orders_generic.py — универсальный 5-фазовый бэкфилл orders_raw для любого tenant'а.

Использование:
    python -m app.onboarding.cli backfill \
        --tenant-id 3 \
        --date-from 2025-02-01 \
        --date-to 2026-03-01 \
        --skip-cities "Ижевск"

Архитектура:
  - Phase 1 (основной): заполняет базовые поля за день → исполняется ПОСЛЕДОВАТЕЛЬНО по дням
  - Phases 2-5 (обогащение): параллельные UPDATE за неделю (только NULL/empty)
  - Возобновляемо: progress.json отслеживает Phase 1, Phases 2-5 всегда безопасны

Статистика в конце:
  - Всего записей по филиалам
  - Обновлено по фазам и филиалам
"""

import asyncio
import json
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
logger = logging.getLogger("backfill_orders_generic")

# OLAP field constants (shared)
OLAP_ORDER_FIELDS = [
    "Delivery.Number",
    "Department",
    "Delivery.CustomerPhone",
    "Delivery.CancelCause",
    "Delivery.ActualTime",
    "Delivery.Address",
    "Delivery.ServiceType",
]

OLAP_DISH_FIELDS = [
    "Delivery.Number",
    "Department",
    "DishName",
]

OLAP_COURIER_FIELDS = [
    "Delivery.Number",
    "Department",
    "WaiterName",
]

OLAP_PLANNED_FIELDS = [
    "Delivery.Number",
    "Department",
    "Delivery.ExpectedTime",
]

OLAP_CLIENT_NAME_FIELDS = [
    "Delivery.Number",
    "Department",
    "Delivery.CustomerName",
]

# Exact time conditions (should match what's in database_pg.py)
_EXACT_TIME_CONDITIONS = """
    (
        opened_at != '' AND opened_at IS NOT NULL
        AND cooked_time != '' AND cooked_time IS NOT NULL
        AND send_time != '' AND send_time IS NOT NULL
        AND actual_time != '' AND actual_time IS NOT NULL
        AND is_self_service = FALSE
        AND EXTRACT(EPOCH FROM (cooked_time::timestamp
             - REPLACE(SUBSTR(opened_at, 1, 19), 'T', ' ')::timestamp)) / 60 BETWEEN 1 AND 120
        AND EXTRACT(EPOCH FROM (send_time::timestamp - cooked_time::timestamp)) / 60 BETWEEN 0 AND 120
        AND EXTRACT(EPOCH FROM (REPLACE(SUBSTR(actual_time, 1, 19), 'T', ' ')::timestamp
             - send_time::timestamp)) / 60 BETWEEN 1 AND 120
    )
"""


class GenericBackfiller:
    def __init__(
        self,
        tenant_id: int,
        date_from: date,
        date_to: date,
        skip_cities: set[str] = None,
        progress_file: str = None,
    ):
        self.tenant_id = tenant_id
        self.date_from = date_from
        self.date_to = date_to
        self.skip_cities = skip_cities or set()
        
        # Progress tracking
        if progress_file is None:
            progress_file = f"/app/data/backfill_orders_{tenant_id}_progress.json"
        self.progress_file = progress_file
        Path(self.progress_file).parent.mkdir(parents=True, exist_ok=True)
        
        self.pool: asyncpg.Pool = None
        self.stats = {
            "phase1": {},
            "phase2": {},
            "phase3": {},
            "phase4": {},
            "phase5": {},
        }

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
        """Получает iiko credentials для tenant."""
        rows = await self.pool.fetch(
            """SELECT city, iiko_url, api_login, api_pass, bo_url, dept_id
               FROM iiko_credentials
               WHERE tenant_id = $1 AND is_active = true
               ORDER BY city""",
            self.tenant_id,
        )
        return [dict(r) for r in rows]

    def _load_progress(self) -> dict:
        """Загружает прогресс Phase 1."""
        if os.path.exists(self.progress_file):
            try:
                with open(self.progress_file) as f:
                    return json.load(f)
            except:
                pass
        return {"phase1_dates": set()}

    def _save_progress(self, progress: dict):
        """Сохраняет прогресс Phase 1."""
        # Конвертируем set в list для JSON
        data = {
            "phase1_dates": list(progress.get("phase1_dates", [])),
        }
        with open(self.progress_file, "w") as f:
            json.dump(data, f, indent=2)

    async def run(self):
        """Запускает полный backfill: Phase 1 → Phases 2-5 параллельно."""
        await self.init_db()
        try:
            # Phase 1: последовательно по дням
            logger.info(f"[tenant_id={self.tenant_id}] Запуск Phase 1 (базовые заказы)...")
            await self._run_phase1()

            # Phases 2-5: параллельно
            logger.info(f"[tenant_id={self.tenant_id}] Запуск Phases 2-5 параллельно...")
            tasks = [
                self._run_phase2(),
                self._run_phase3(),
                self._run_phase4(),
                self._run_phase5(),
            ]
            await asyncio.gather(*tasks)

            # Итоговая статистика
            self._print_summary()

        finally:
            await self.close_db()

    async def _run_phase1(self):
        """Phase 1: заполняет базовые поля за каждый день."""
        credentials = await self.get_iiko_credentials()
        if not credentials:
            logger.error(f"Нет iiko_credentials для tenant_id={self.tenant_id}")
            return

        progress = self._load_progress()
        processed_dates = set(progress.get("phase1_dates", []))

        current = self.date_from
        while current <= self.date_to:
            current_str = current.isoformat()
            
            if current_str in processed_dates:
                logger.debug(f"Phase 1: {current_str} уже обработана, пропуск")
                current += timedelta(days=1)
                continue

            logger.info(f"Phase 1: обработка {current_str}...")
            
            next_day = (current + timedelta(days=1)).isoformat()
            
            # Параллельная загрузка всех филиалов для этого дня
            tasks = [
                self._phase1_fetch_and_upsert(
                    cred, current_str, next_day
                )
                for cred in credentials
                if cred["city"] not in self.skip_cities
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for city, cred, count in results:
                if isinstance(count, Exception):
                    logger.error(f"Phase 1 ошибка {city}: {count}")
                else:
                    self.stats["phase1"][city] = self.stats["phase1"].get(city, 0) + count
                    logger.info(f"Phase 1: {city} за {current_str}: +{count} заказов")

            processed_dates.add(current_str)
            progress["phase1_dates"] = processed_dates
            self._save_progress(progress)

            current += timedelta(days=1)

    async def _phase1_fetch_and_upsert(self, cred: dict, date_from: str, date_to: str) -> tuple:
        """Загружает заказы одного филиала за день и вставляет в orders_raw."""
        city = cred["city"]
        try:
            # OLAP запрос
            headers = {
                "Authorization": f"Bearer {await self._get_iiko_token(cred)}"
            }
            
            url = cred["iiko_url"].rstrip("/")
            params = {
                "key": await self._get_iiko_token(cred),
                "reportType": "SALES",
                "groupByRowFields": ",".join(OLAP_ORDER_FIELDS),
                "filters": json.dumps({
                    "dateRange": {
                        "startDate": date_from,
                        "endDate": date_to,
                    },
                    "departments": [cred["dept_id"]] if cred.get("dept_id") else [],
                }),
            }

            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.get(f"{url}/api/v2/reports/olap", params=params, headers=headers)
                r.raise_for_status()
                data = r.json()

            rows = data.get("rows", [])
            if not rows:
                return city, cred, 0

            # Парсинг и UPSERT
            count = await self._upsert_phase1_rows(city, rows)
            return city, cred, count

        except Exception as e:
            logger.error(f"Phase 1 ошибка загрузки {city}: {e}")
            return city, cred, e

    async def _upsert_phase1_rows(self, city: str, rows: list) -> int:
        """Парсит OLAP rows и вставляет в orders_raw."""
        upsert_count = 0
        
        for row in rows:
            values = row.get("values", {})
            
            delivery_num = values.get("Delivery.Number", "").strip()
            if not delivery_num:
                continue

            # Парсинг полей
            branch_name = city  # Можно расширить если нужны точки
            client_phone = values.get("Delivery.CustomerPhone", "").strip() or None
            cancel_reason = values.get("Delivery.CancelCause", "").strip() or None
            actual_time = values.get("Delivery.ActualTime", "").strip() or None
            delivery_address = values.get("Delivery.Address", "").strip() or None
            service_type = values.get("Delivery.ServiceType", "").strip() or None
            
            is_self_service = service_type == "Самовывоз" if service_type else False
            status = "Отменена" if cancel_reason else "Доставлена" if actual_time else "В пути"

            # UPSERT
            try:
                await self.pool.execute(
                    """INSERT INTO orders_raw 
                       (tenant_id, delivery_num, branch_name, client_phone, status, 
                        cancel_reason, actual_time, delivery_address, is_self_service)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                       ON CONFLICT (tenant_id, delivery_num, branch_name)
                       DO UPDATE SET
                           client_phone = COALESCE($4, orders_raw.client_phone),
                           status = $5,
                           cancel_reason = COALESCE($6, orders_raw.cancel_reason),
                           actual_time = COALESCE($7, orders_raw.actual_time),
                           delivery_address = COALESCE($8, orders_raw.delivery_address),
                           is_self_service = $9
                    """,
                    self.tenant_id,
                    delivery_num,
                    branch_name,
                    client_phone,
                    status,
                    cancel_reason,
                    actual_time,
                    delivery_address,
                    is_self_service,
                )
                upsert_count += 1
            except Exception as e:
                logger.warning(f"Ошибка UPSERT {delivery_num}: {e}")

        return upsert_count

    async def _get_iiko_token(self, cred: dict) -> str:
        """Получает iiko API token."""
        # Упрощённо — можно расширить с кешированием
        return cred.get("api_pass", "")

    async def _run_phase2(self):
        """Phase 2: обогащает items (DishName)."""
        logger.info(f"[Phase 2] Запуск обогащения блюд для tenant_id={self.tenant_id}...")
        
        credentials = await self.get_iiko_credentials()
        if not credentials:
            return

        current = self.date_from
        while current <= self.date_to:
            current_str = current.isoformat()
            next_day = (current + timedelta(days=1)).isoformat()
            
            tasks = [
                self._phase2_fetch_and_update(cred, current_str, next_day)
                for cred in credentials
                if cred["city"] not in self.skip_cities
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for city, count in results:
                if isinstance(count, Exception):
                    logger.error(f"Phase 2 ошибка {city}: {count}")
                else:
                    self.stats["phase2"][city] = self.stats["phase2"].get(city, 0) + count
                    if count > 0:
                        logger.info(f"Phase 2: {city} за {current_str}: +{count} блюд")

            current += timedelta(days=1)

    async def _phase2_fetch_and_update(self, cred: dict, date_from: str, date_to: str) -> tuple:
        """Загружает блюда и обновляет items в orders_raw."""
        city = cred["city"]
        try:
            token = await self._get_iiko_token(cred)
            url = cred["iiko_url"].rstrip("/")
            
            params = {
                "key": token,
                "reportType": "SALES",
                "groupByRowFields": ",".join(OLAP_DISH_FIELDS),
                "aggregateFields": "DishDiscountSumInt",
                "filters": json.dumps({
                    "dateRange": {"startDate": date_from, "endDate": date_to},
                    "departments": [cred["dept_id"]] if cred.get("dept_id") else [],
                }),
            }

            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.get(f"{url}/api/v2/reports/olap", params=params)
                r.raise_for_status()
                rows = r.json().get("rows", [])

            # Парсим в items map
            items_map = defaultdict(list)
            for row in rows:
                values = row.get("values", {})
                num = values.get("Delivery.Number", "").strip()
                dept = values.get("Department", "").strip()
                dish = values.get("DishName", "").strip()
                
                if num and dept and dish:
                    items_map[(dept, num)].append({"name": dish, "qty": 1})

            # UPDATE в orders_raw
            count = 0
            for (branch, num), dishes in items_map.items():
                items_json = json.dumps(dishes, ensure_ascii=False)
                result = await self.pool.execute(
                    """UPDATE orders_raw
                       SET items = $1, updated_at = now()
                       WHERE tenant_id = $2 AND branch_name = $3 AND delivery_num = $4
                         AND (items IS NULL OR items = '')""",
                    items_json, self.tenant_id, branch, num,
                )
                if result and "UPDATE" in result and int(result.split()[-1]) > 0:
                    count += 1

            return city, count
        except Exception as e:
            logger.error(f"Phase 2 ошибка {city}: {e}")
            return city, e

    async def _run_phase3(self):
        """Phase 3: обогащает courier (WaiterName)."""
        logger.info(f"[Phase 3] Запуск обогащения курьеров для tenant_id={self.tenant_id}...")
        
        credentials = await self.get_iiko_credentials()
        if not credentials:
            return

        current = self.date_from
        while current <= self.date_to:
            current_str = current.isoformat()
            next_day = (current + timedelta(days=1)).isoformat()
            
            tasks = [
                self._phase3_fetch_and_update(cred, current_str, next_day)
                for cred in credentials
                if cred["city"] not in self.skip_cities
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for city, count in results:
                if isinstance(count, Exception):
                    logger.error(f"Phase 3 ошибка {city}: {count}")
                else:
                    self.stats["phase3"][city] = self.stats["phase3"].get(city, 0) + count
                    if count > 0:
                        logger.info(f"Phase 3: {city} за {current_str}: +{count} курьеров")

            current += timedelta(days=1)

    async def _phase3_fetch_and_update(self, cred: dict, date_from: str, date_to: str) -> tuple:
        """Загружает курьеров и обновляет courier в orders_raw."""
        city = cred["city"]
        try:
            token = await self._get_iiko_token(cred)
            url = cred["iiko_url"].rstrip("/")
            
            params = {
                "key": token,
                "reportType": "SALES",
                "groupByRowFields": ",".join(OLAP_COURIER_FIELDS),
                "aggregateFields": "DishDiscountSumInt",
                "filters": json.dumps({
                    "dateRange": {"startDate": date_from, "endDate": date_to},
                    "departments": [cred["dept_id"]] if cred.get("dept_id") else [],
                }),
            }

            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.get(f"{url}/api/v2/reports/olap", params=params)
                r.raise_for_status()
                rows = r.json().get("rows", [])

            # Парсим в courier map
            courier_map = {}
            for row in rows:
                values = row.get("values", {})
                num = values.get("Delivery.Number", "").strip()
                dept = values.get("Department", "").strip()
                waiter = values.get("WaiterName", "").strip()
                
                if num and dept and waiter:
                    courier_map[(dept, num)] = waiter

            # UPDATE в orders_raw
            count = 0
            for (branch, num), courier in courier_map.items():
                result = await self.pool.execute(
                    """UPDATE orders_raw
                       SET courier = $1, updated_at = now()
                       WHERE tenant_id = $2 AND branch_name = $3 AND delivery_num = $4
                         AND (courier IS NULL OR courier = '')""",
                    courier, self.tenant_id, branch, num,
                )
                if result and "UPDATE" in result and int(result.split()[-1]) > 0:
                    count += 1

            return city, count
        except Exception as e:
            logger.error(f"Phase 3 ошибка {city}: {e}")
            return city, e

    async def _run_phase4(self):
        """Phase 4: обогащает planned_time (Delivery.ExpectedTime)."""
        logger.info(f"[Phase 4] Запуск обогащения planned_time для tenant_id={self.tenant_id}...")
        
        credentials = await self.get_iiko_credentials()
        if not credentials:
            return

        current = self.date_from
        while current <= self.date_to:
            current_str = current.isoformat()
            next_day = (current + timedelta(days=1)).isoformat()
            
            tasks = [
                self._phase4_fetch_and_update(cred, current_str, next_day)
                for cred in credentials
                if cred["city"] not in self.skip_cities
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for city, count in results:
                if isinstance(count, Exception):
                    logger.error(f"Phase 4 ошибка {city}: {count}")
                else:
                    self.stats["phase4"][city] = self.stats["phase4"].get(city, 0) + count
                    if count > 0:
                        logger.info(f"Phase 4: {city} за {current_str}: +{count} времён доставки")

            current += timedelta(days=1)

    async def _phase4_fetch_and_update(self, cred: dict, date_from: str, date_to: str) -> tuple:
        """Загружает planned_time и обновляет planned_time в orders_raw."""
        city = cred["city"]
        try:
            token = await self._get_iiko_token(cred)
            url = cred["iiko_url"].rstrip("/")
            
            params = {
                "key": token,
                "reportType": "DELIVERIES",  # IMPORTANT: DELIVERIES для ExpectedTime
                "groupByRowFields": ",".join(OLAP_PLANNED_FIELDS),
                "aggregateFields": "DishDiscountSumInt",
                "filters": json.dumps({
                    "dateRange": {"startDate": date_from, "endDate": date_to},
                    "departments": [cred["dept_id"]] if cred.get("dept_id") else [],
                }),
            }

            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.get(f"{url}/api/v2/reports/olap", params=params)
                r.raise_for_status()
                rows = r.json().get("rows", [])

            # Парсим в planned map
            planned_map = {}
            for row in rows:
                values = row.get("values", {})
                num = values.get("Delivery.Number", "").strip()
                dept = values.get("Department", "").strip()
                expected_time = values.get("Delivery.ExpectedTime", "").strip()
                
                if num and dept and expected_time:
                    planned_map[(dept, num)] = expected_time

            # UPDATE в orders_raw
            count = 0
            for (branch, num), planned_time in planned_map.items():
                result = await self.pool.execute(
                    """UPDATE orders_raw
                       SET planned_time = $1, updated_at = now()
                       WHERE tenant_id = $2 AND branch_name = $3 AND delivery_num = $4
                         AND (planned_time IS NULL OR planned_time = '')""",
                    planned_time, self.tenant_id, branch, num,
                )
                if result and "UPDATE" in result and int(result.split()[-1]) > 0:
                    count += 1

            return city, count
        except Exception as e:
            logger.error(f"Phase 4 ошибка {city}: {e}")
            return city, e

    async def _run_phase5(self):
        """Phase 5: обогащает client_name (Delivery.CustomerName)."""
        logger.info(f"[Phase 5] Запуск обогащения client_name для tenant_id={self.tenant_id}...")
        
        credentials = await self.get_iiko_credentials()
        if not credentials:
            return

        current = self.date_from
        while current <= self.date_to:
            current_str = current.isoformat()
            next_day = (current + timedelta(days=1)).isoformat()
            
            tasks = [
                self._phase5_fetch_and_update(cred, current_str, next_day)
                for cred in credentials
                if cred["city"] not in self.skip_cities
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for city, count in results:
                if isinstance(count, Exception):
                    logger.error(f"Phase 5 ошибка {city}: {count}")
                else:
                    self.stats["phase5"][city] = self.stats["phase5"].get(city, 0) + count
                    if count > 0:
                        logger.info(f"Phase 5: {city} за {current_str}: +{count} имён клиентов")

            current += timedelta(days=1)

    async def _phase5_fetch_and_update(self, cred: dict, date_from: str, date_to: str) -> tuple:
        """Загружает client_name и обновляет client_name в orders_raw."""
        city = cred["city"]
        try:
            token = await self._get_iiko_token(cred)
            url = cred["iiko_url"].rstrip("/")
            
            params = {
                "key": token,
                "reportType": "DELIVERIES",  # IMPORTANT: DELIVERIES для CustomerName
                "groupByRowFields": ",".join(OLAP_CLIENT_NAME_FIELDS),
                "aggregateFields": "DishDiscountSumInt",
                "filters": json.dumps({
                    "dateRange": {"startDate": date_from, "endDate": date_to},
                    "departments": [cred["dept_id"]] if cred.get("dept_id") else [],
                }),
            }

            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.get(f"{url}/api/v2/reports/olap", params=params)
                r.raise_for_status()
                rows = r.json().get("rows", [])

            # Парсим в client_name map (фильтруем GUEST*)
            client_map = {}
            for row in rows:
                values = row.get("values", {})
                num = values.get("Delivery.Number", "").strip()
                dept = values.get("Department", "").strip()
                client_name = values.get("Delivery.CustomerName", "").strip()
                
                # Фильтруем GUEST* (анонимные заказы)
                if num and dept and client_name and not client_name.startswith("GUEST"):
                    client_map[(dept, num)] = client_name

            # UPDATE в orders_raw
            count = 0
            for (branch, num), client_name in client_map.items():
                result = await self.pool.execute(
                    """UPDATE orders_raw
                       SET client_name = $1, updated_at = now()
                       WHERE tenant_id = $2 AND branch_name = $3 AND delivery_num = $4
                         AND (client_name IS NULL OR client_name = '')""",
                    client_name, self.tenant_id, branch, num,
                )
                if result and "UPDATE" in result and int(result.split()[-1]) > 0:
                    count += 1

            return city, count
        except Exception as e:
            logger.error(f"Phase 5 ошибка {city}: {e}")
            return city, e

    def _print_summary(self):
        """Выводит итоговую статистику."""
        logger.info("\n" + "=" * 60)
        logger.info(f"ИТОГИ БЭКФИЛЛА (tenant_id={self.tenant_id})")
        logger.info("=" * 60)
        
        for phase in ["phase1", "phase2", "phase3", "phase4", "phase5"]:
            stats = self.stats[phase]
            if not stats:
                continue
            total = sum(stats.values())
            logger.info(f"\n{phase.upper()}:")
            for city, count in sorted(stats.items()):
                logger.info(f"  {city}: {count:,}")
            logger.info(f"  ИТОГО: {total:,}")


async def main():
    """Entry point для CLI."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Generic backfill для orders_raw")
    parser.add_argument("--tenant-id", type=int, required=True, help="Tenant ID")
    parser.add_argument("--date-from", type=str, required=True, help="YYYY-MM-DD")
    parser.add_argument("--date-to", type=str, required=True, help="YYYY-MM-DD")
    parser.add_argument("--skip-cities", type=str, help="Города через запятую")
    
    args = parser.parse_args()
    
    date_from = date.fromisoformat(args.date_from)
    date_to = date.fromisoformat(args.date_to)
    skip_cities = set(c.strip() for c in (args.skip_cities or "").split(",") if c.strip())
    
    backfiller = GenericBackfiller(
        tenant_id=args.tenant_id,
        date_from=date_from,
        date_to=date_to,
        skip_cities=skip_cities,
    )
    
    await backfiller.run()


if __name__ == "__main__":
    asyncio.run(main())
