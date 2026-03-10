"""
backfill_fot.py — бэкфилл fot_daily из shifts_raw + salary API за указанный период.

Требует заполненных shifts_raw за период (запускать после backfill_new_client шаг 4).

Использование:
    docker compose exec app python -m app.onboarding.backfill_fot \
        --tenant-id N \
        --date-from 2026-02-01 \
        --date-to 2026-03-09

Прогресс сохраняется в /app/data/backfill_fot_{tenant_id}_progress.json.
"""

import argparse
import asyncio
import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path

import asyncpg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill_fot")


class FotBackfiller:
    def __init__(self, tenant_id: int, date_from: date, date_to: date):
        self.tenant_id = tenant_id
        self.date_from = date_from
        self.date_to = date_to
        self.pool: asyncpg.Pool = None
        self.progress_file = f"/app/data/backfill_fot_{tenant_id}_progress.json"
        self.stats = {"ok": 0, "skipped": 0, "error": 0, "no_rate_total": 0}

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

    async def run(self) -> None:
        # Инициализируем пул БД до импорта app-модулей, которые его используют
        await self.init_db()

        # Инжектируем пул в database_pg (аналогично другим backfill-скриптам)
        from app import database_pg as _db
        _db._pool = self.pool

        # Загружаем кеш точек тенанта
        branches = await _db.get_branches_from_db(self.tenant_id)
        if not branches:
            logger.error(f"Нет активных точек для tenant_id={self.tenant_id}")
            return
        # Заполняем in-memory cache, нужный для get_all_branches()
        _db._branches_cache[self.tenant_id] = branches
        logger.info(f"Загружено {len(branches)} точек для tenant_id={self.tenant_id}")

        from app.jobs.fot_pipeline import run_fot_pipeline

        done = self._load_progress()

        current = self.date_from
        while current <= self.date_to:
            date_iso = current.isoformat()

            if date_iso in done:
                logger.info(f"  {date_iso} — пропущено (уже в прогрессе)")
                self.stats["skipped"] += 1
                current += timedelta(days=1)
                continue

            try:
                result = await run_fot_pipeline(current, self.tenant_id)
                if result["branches"] > 0:
                    logger.info(
                        f"  {date_iso} ✓ — точек: {result['branches']}, "
                        f"строк: {result['rows_saved']}, "
                        f"без ставки: {result['no_rate_total']}"
                    )
                    self.stats["no_rate_total"] += result["no_rate_total"]
                else:
                    logger.info(f"  {date_iso} — нет смен")
                self.stats["ok"] += 1
                done.add(date_iso)
                self._save_progress(done)
            except Exception as e:
                logger.error(f"  {date_iso} ОШИБКА: {e}", exc_info=True)
                self.stats["error"] += 1

            current += timedelta(days=1)

        await self.close_db()

        logger.info(
            f"\n=== Бэкфилл ФОТ завершён (tenant_id={self.tenant_id}) ===\n"
            f"  Обработано: {self.stats['ok']} дней\n"
            f"  Пропущено:  {self.stats['skipped']} дней\n"
            f"  Ошибок:     {self.stats['error']} дней\n"
            f"  Сотр. без ставки (всего): {self.stats['no_rate_total']}"
        )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Бэкфилл ФОТ из shifts_raw + salary API")
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--date-from", required=True, help="YYYY-MM-DD")
    parser.add_argument("--date-to", required=True, help="YYYY-MM-DD")
    args = parser.parse_args()

    backfiller = FotBackfiller(
        tenant_id=args.tenant_id,
        date_from=date.fromisoformat(args.date_from),
        date_to=date.fromisoformat(args.date_to),
    )
    await backfiller.run()


if __name__ == "__main__":
    asyncio.run(main())
