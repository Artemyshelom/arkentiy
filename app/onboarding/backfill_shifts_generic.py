"""
backfill_shifts_generic.py — бэкфилл shifts_raw из /api/v2/employees/schedule.

Источник данных: GET /api/v2/employees/schedule?key=TOKEN&from=DATE&to=DATE
Возвращает XML с расписанием сотрудников: кто, на какой точке, с/по когда.

Маппинг dept_id → branch_name берётся из iiko_credentials.
Классификация ролей — через _classify_role из iiko_bo_events.

Использование:
    python -m app.onboarding.backfill_shifts_generic \
        --tenant-id 1 \
        --date-from 2025-12-01 \
        --date-to 2026-02-21

Особенности:
  - Чанки по 7 дней (API отдаёт расписание, не реальные входы/выходы)
  - dateFrom = schedule.dateFrom.date(), clock_in = dateFrom, clock_out = dateTo
  - Пропускаются записи с role_class=None (не повар и не курьер)
  - Resumable: прогресс в /app/data/backfill_shifts_{tenant_id}_progress.json
"""

import argparse
import asyncio
import json
import logging
import os
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from typing import Optional

import asyncpg
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill_shifts_generic")


def _classify_role(role_code: str) -> Optional[str]:
    """Определяет role_class ('cook' | 'courier' | None) по коду роли из iiko."""
    if not role_code:
        return None
    low = role_code.lower()
    cook_prefixes = ("повар", "cook", "пс", "пбт", "пов", "пз", "кп")
    cook_substrings = ("сушист", "kitchen", "помпов")
    courier_prefixes = ("курьер", "courier", "delivery", "кур", "крс")
    courier_substrings = ("доставка", "k_rs")
    for p in cook_prefixes:
        if low.startswith(p):
            return "cook"
    for s in cook_substrings:
        if s in low:
            return "cook"
    for p in courier_prefixes:
        if low.startswith(p):
            return "courier"
    for s in courier_substrings:
        if s in low:
            return "courier"
    return None


class ShiftsBackfiller:
    def __init__(self, tenant_id: int, date_from: date, date_to: date):
        self.tenant_id = tenant_id
        self.date_from = date_from
        self.date_to = date_to
        self.pool: asyncpg.Pool = None
        self.progress_file = f"/app/data/backfill_shifts_{tenant_id}_progress.json"

    async def init_db(self) -> None:
        db_url = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@postgres:5432/ebidoebi")
        self.pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3)

    async def close_db(self) -> None:
        if self.pool:
            await self.pool.close()

    def _load_progress(self) -> set:
        try:
            with open(self.progress_file) as f:
                return set(json.load(f))
        except Exception:
            return set()

    def _save_progress(self, done: set) -> None:
        os.makedirs(os.path.dirname(self.progress_file), exist_ok=True)
        with open(self.progress_file, "w") as f:
            json.dump(sorted(done), f)

    async def _get_credentials(self) -> list[dict]:
        """Возвращает уникальные серверы с маппингом dept_id → branch_name."""
        rows = await self.pool.fetch(
            """SELECT bo_url, bo_login, bo_password, dept_id, branch_name
               FROM iiko_credentials
               WHERE tenant_id = $1 AND is_active = true""",
            self.tenant_id,
        )
        # Группируем по серверу (один сервер → одна точка или несколько)
        by_server: dict[str, dict] = {}
        for r in rows:
            bo_url = r["bo_url"]
            if bo_url not in by_server:
                by_server[bo_url] = {
                    "bo_url": bo_url,
                    "bo_login": r["bo_login"],
                    "bo_password": r["bo_password"],
                    "dept_map": {},  # dept_id → branch_name
                }
            if r["dept_id"]:
                by_server[bo_url]["dept_map"][r["dept_id"]] = r["branch_name"]
        return list(by_server.values())

    async def _get_token(self, bo_url: str, bo_login: Optional[str], bo_password: Optional[str], client: httpx.AsyncClient) -> str:
        from app.clients.iiko_auth import get_bo_token
        return await get_bo_token(bo_url, client=client, bo_login=bo_login, bo_password=bo_password)

    async def _load_employees(self, bo_url: str, token: str, client: httpx.AsyncClient) -> dict:
        """Загружает справочник сотрудников: {employee_id: {name, role_class}}."""
        try:
            r = await client.get(f"{bo_url}/api/employees?key={token}", timeout=60)
            r.raise_for_status()
            root = ET.fromstring(r.text)
            employees = {}
            for emp in root.findall(".//employee"):
                uid = emp.findtext("id", "")
                if not uid:
                    continue
                if emp.findtext("deleted", "false") == "true":
                    continue
                name = emp.findtext("name", "")
                role = emp.findtext("mainRoleCode", "") or emp.findtext("roleCodes", "") or ""
                employees[uid] = {
                    "name": name,
                    "role": role,
                    "role_class": _classify_role(role),
                }
            logger.info(f"  Сотрудников загружено: {len(employees)} ({bo_url.split('//')[1].split('.')[0]})")
            return employees
        except Exception as e:
            logger.error(f"  Ошибка загрузки сотрудников {bo_url}: {e}")
            return {}

    async def _fetch_schedule(self, bo_url: str, token: str, date_from: str, date_to: str, client: httpx.AsyncClient) -> list[dict]:
        """Получает расписание смен за период."""
        try:
            r = await client.get(
                f"{bo_url}/api/v2/employees/schedule",
                params={"key": token, "from": date_from, "to": date_to},
                timeout=30,
            )
            if r.status_code != 200:
                logger.warning(f"  schedule {bo_url} {date_from}: {r.status_code}")
                return []
            root = ET.fromstring(r.text)
            schedules = []
            for s in root.findall(".//schedule"):
                schedules.append({
                    "employee_id": s.findtext("employeeId", ""),
                    "dept_id": s.findtext("departmentId", ""),
                    "date_from": s.findtext("dateFrom", ""),
                    "date_to": s.findtext("dateTo", ""),
                })
            return schedules
        except Exception as e:
            logger.error(f"  schedule error {bo_url} {date_from}: {e}")
            return []

    async def _upsert_shifts(self, rows: list[dict]) -> int:
        """UPSERT смен в shifts_raw."""
        if not rows:
            return 0
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for r in rows:
                    await conn.execute(
                        """INSERT INTO shifts_raw
                           (tenant_id, branch_name, employee_id, employee_name,
                            role_class, date, clock_in, clock_out, updated_at)
                           VALUES ($1,$2,$3,$4,$5,$6::date,$7,$8,now())
                           ON CONFLICT (tenant_id, branch_name, employee_id, clock_in) DO UPDATE SET
                             employee_name = EXCLUDED.employee_name,
                             role_class    = EXCLUDED.role_class,
                             date          = EXCLUDED.date,
                             clock_out     = EXCLUDED.clock_out,
                             updated_at    = now()""",
                        self.tenant_id,
                        r["branch_name"], r["employee_id"], r["employee_name"],
                        r["role_class"], r["shift_date"], r["clock_in"], r["clock_out"],
                    )
        return len(rows)

    async def run(self) -> None:
        await self.init_db()
        try:
            servers = await self._get_credentials()
            if not servers:
                logger.error(f"Нет credentials для tenant_id={self.tenant_id}")
                return
            logger.info(f"Серверов: {len(servers)}, период: {self.date_from} — {self.date_to}")

            done = self._load_progress()
            total_ok = 0
            total_skipped = 0

            async with httpx.AsyncClient(verify=False, timeout=60) as client:
                for srv in servers:
                    bo_url = srv["bo_url"]
                    dept_map = srv["dept_map"]
                    if not dept_map:
                        logger.warning(f"  Нет dept_id для {bo_url}, пропускаем")
                        continue

                    try:
                        token = await self._get_token(bo_url, srv["bo_login"], srv["bo_password"], client)
                    except Exception as e:
                        logger.error(f"  Auth failed {bo_url}: {e}")
                        continue

                    employees = await self._load_employees(bo_url, token, client)

                    week_start = self.date_from
                    today = date.today()
                    yesterday = today - timedelta(days=1)

                    while week_start <= min(self.date_to, yesterday):
                        week_end = min(week_start + timedelta(days=7), today)
                        chunk_key = f"{bo_url}:{week_start}"

                        if chunk_key in done:
                            week_start += timedelta(days=7)
                            continue

                        schedules = await self._fetch_schedule(bo_url, token, str(week_start), str(week_end), client)

                        rows_to_insert = []
                        for s in schedules:
                            dept_id = s["dept_id"]
                            branch_name = dept_map.get(dept_id)
                            if not branch_name:
                                continue

                            emp_id = s["employee_id"]
                            emp_info = employees.get(emp_id, {})
                            role_class = emp_info.get("role_class")
                            if role_class is None:
                                total_skipped += 1
                                continue

                            date_from_str = s["date_from"]
                            date_to_str = s["date_to"]
                            if not date_from_str:
                                continue

                            # dateFrom: "2025-12-01T09:45:00+07:00" → date + timestamp
                            shift_date = date_from_str[:10]

                            rows_to_insert.append({
                                "branch_name": branch_name,
                                "employee_id": emp_id,
                                "employee_name": emp_info.get("name", emp_id),
                                "role_class": role_class,
                                "shift_date": shift_date,
                                "clock_in": date_from_str,
                                "clock_out": date_to_str or date_from_str,
                            })

                        upserted = await self._upsert_shifts(rows_to_insert)
                        total_ok += upserted
                        done.add(chunk_key)
                        self._save_progress(done)
                        logger.info(f"  ✓ {week_start}..{week_end} [{bo_url.split('//')[1].split('.')[0]}]: {len(schedules)} записей → {upserted} смен (пропущено ролей: {total_skipped})")

                        await asyncio.sleep(0.3)
                        week_start += timedelta(days=7)

            logger.info("=" * 60)
            logger.info(f"ИТОГО: {total_ok} смен сохранено, {total_skipped} пропущено (не повар/курьер)")

        finally:
            await self.close_db()


async def main():
    parser = argparse.ArgumentParser(description="Бэкфилл shifts_raw из iiko employees/schedule")
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--date-from", type=str, required=True, help="YYYY-MM-DD")
    parser.add_argument("--date-to", type=str, required=True, help="YYYY-MM-DD")
    args = parser.parse_args()

    backfiller = ShiftsBackfiller(
        tenant_id=args.tenant_id,
        date_from=date.fromisoformat(args.date_from),
        date_to=date.fromisoformat(args.date_to),
    )
    await backfiller.run()


if __name__ == "__main__":
    asyncio.run(main())
