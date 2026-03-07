"""
Бэкфилл OLAP-обогащения orders_raw за произвольный диапазон дат.

Использование:
    python3 app/onboarding/backfill_olap_enrichment.py \
        --tenant 3 \
        --from 2025-01-01 \
        --to 2026-02-01

Параметры:
    --tenant    tenant_id (обязательно)
    --from      дата начала включительно (YYYY-MM-DD)
    --to        дата конца НЕвключительно (YYYY-MM-DD)
    --chunk     размер батча в днях (default: 7, уменьши до 1 при таймаутах)
    --dry-run   только показать что будет сделано, без записи в БД

Запуск внутри контейнера:
    docker compose exec app python3 app/onboarding/backfill_olap_enrichment.py \
        --tenant 3 --from 2025-01-01 --to 2026-02-01
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import date, timedelta

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill_olap")


async def run(tenant_id: int, date_from: date, date_to: date, chunk: int, dry_run: bool) -> None:
    from app.database_pg import init_db
    await init_db(os.environ["DATABASE_URL"])

    from app.db import get_branches
    from app.jobs.olap_enrichment import _fetch_enrichment, _aggregate_by_order, _update_orders_raw

    branches = get_branches(tenant_id)
    if not branches:
        print(f"❌ Нет точек для tenant_id={tenant_id}")
        return
    print(f"Tenant {tenant_id}: {[b['name'] for b in branches]}")
    print(f"Период: {date_from} .. {date_to} (chunk={chunk}d, dry_run={dry_run})")

    # Группируем точки по серверу (одна точка = один сервер, но бывают совмещённые)
    by_server: dict[tuple, dict] = {}
    for branch in branches:
        url = branch.get("bo_url", "")
        if not url:
            continue
        login = branch.get("bo_login") or ""
        password = branch.get("bo_password") or ""
        key = (url, login, password)
        if key not in by_server:
            by_server[key] = {"names": set(), "login": login or None, "password": password or None, "url": url}
        by_server[key]["names"].add(branch["name"])

    grand_total = 0
    d = date_from
    while d < date_to:
        chunk_end = min(d + timedelta(days=chunk), date_to)
        d_str = d.isoformat()
        e_str = chunk_end.isoformat()

        all_enriched: dict = {}
        for srv in by_server.values():
            rows = await _fetch_enrichment(srv["url"], d_str, e_str, srv["login"], srv["password"])
            enriched = _aggregate_by_order(rows, srv["names"])
            all_enriched.update(enriched)

        if not all_enriched:
            print(f"  {d_str}..{e_str}: нет данных")
            d = chunk_end
            continue

        if dry_run:
            print(f"  {d_str}..{e_str}: [DRY] найдено {len(all_enriched)} заказов")
        else:
            updated = await _update_orders_raw(all_enriched, tenant_id)
            grand_total += updated
            print(f"  {d_str}..{e_str}: найдено {len(all_enriched)}, обновлено {updated}")

        d = chunk_end

    print(f"\n✅ Итого обновлено: {grand_total}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill OLAP enrichment for orders_raw")
    parser.add_argument("--tenant", type=int, required=True, help="tenant_id")
    parser.add_argument("--from", dest="date_from", required=True, help="Start date YYYY-MM-DD (inclusive)")
    parser.add_argument("--to", dest="date_to", required=True, help="End date YYYY-MM-DD (exclusive)")
    parser.add_argument("--chunk", type=int, default=7, help="Days per OLAP request (default: 7)")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to DB")
    args = parser.parse_args()

    asyncio.run(run(
        tenant_id=args.tenant,
        date_from=date.fromisoformat(args.date_from),
        date_to=date.fromisoformat(args.date_to),
        chunk=args.chunk,
        dry_run=args.dry_run,
    ))


if __name__ == "__main__":
    main()
