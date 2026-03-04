"""
Phase 6: Обогащение orders_raw временными полями из OLAP v2.

Запускается после Phase 1-5 бэкфилла. Заполняет:
- cooked_time (время готовки, Cooking.FinishTime из OLAP)
- ready_time (время готовности, Delivery.ReadyTime или схожее)
- service_print_time (время печати чека, Service.PrintTime?)

Идея: Events API не передаёт эти поля, но OLAP их имеет.
Нужно обогатить orders_raw за прошлые дни из OLAP v2.
"""

import asyncio
import logging
import os
from datetime import date, timedelta

import asyncpg
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("backfill_phase6_times")

TENANT_ID = 3
DATE_FROM = date(2026, 2, 1)
SKIP_CITIES = {"Ижевск"}  # медленные серверы


async def _get_token(bo_url: str, bo_login: str, bo_password: str, client: httpx.AsyncClient) -> str:
    import hashlib
    pw_hash = hashlib.sha1(bo_password.encode()).hexdigest()
    r = await client.get(f"{bo_url}/api/auth?login={bo_login}&pass={pw_hash}", timeout=30)
    r.raise_for_status()
    return r.text.strip()


async def _fetch_times_from_olap(
    bo_url: str, bo_login: str, bo_password: str, date_from: str, date_to: str
) -> dict:
    """
    Запрашивает OLAP v2 для извлечения временных полей.
    Возвращает {(dept_name, delivery_num): {"cooked_time": ..., "ready_time": ..., "send_time": ...}}
    
    Пробуем разные имена полей:
    - Cooking.FinishTime, CookingFinishTime, OrderItemsCookingTime
    - Delivery.ReadyTime, ReadyTime
    - Delivery.SendTime, CourierSendTime, DispatchTime (для send_time)
    - Service.PrintTime, TerminalPrintTime, ServicePrintTime
    """
    
    result = {}
    async with httpx.AsyncClient(verify=False, timeout=60) as client:
        token = await _get_token(bo_url, bo_login, bo_password, client)
        
        # Пробуем несколько вариантов полей
        for attempt in range(2):
            if attempt == 0:
                group_fields = [
                    "Delivery.Number", "Department",
                    "Delivery.CookingFinishTime",
                    "Delivery.ReadyTime",
                    "Delivery.SendTime",
                ]
            else:
                group_fields = [
                    "Delivery.Number", "Department",
                    "OrderItemsCookingTime",
                    "Delivery.TerminalPrintTime",
                    "Delivery.CourierAssignmentTime",
                ]
            
            try:
                body = {
                    "reportType": "SALES",
                    "buildSummary": "false",
                    "groupByRowFields": group_fields,
                    "aggregateFields": ["DishDiscountSumInt"],
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
                
                r = await client.post(
                    f"{bo_url}/api/v2/reports/olap?key={token}",
                    json=body,
                    headers={"Content-Type": "application/json"},
                    timeout=60,
                )
                
                if r.status_code == 200:
                    for row in r.json().get("data", []):
                        dept = row.get("Department", "").strip()
                        dnum = str(row.get("Delivery.Number", "")).strip()
                        if not dept or not dnum:
                            continue
                        
                        key = (dept, dnum)
                        result[key] = {
                            "cooked_time": row.get("Delivery.CookingFinishTime") or row.get("OrderItemsCookingTime"),
                            "ready_time": row.get("Delivery.ReadyTime"),
                            "send_time": row.get("Delivery.SendTime") or row.get("Delivery.CourierAssignmentTime"),
                            "service_print_time": row.get("Delivery.TerminalPrintTime"),
                        }
                    
                    if result:
                        logger.info(f"  Attempt {attempt+1}: получено {len(result)} заказов с временами")
                        return result
            except Exception as e:
                logger.debug(f"  Attempt {attempt+1} failed: {e}")
                continue
        
        logger.warning(f"  Не удалось получить временные поля из OLAP для {bo_url}")
        return {}


async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    
    try:
        # Получаем точки тенанта
        branches = await conn.fetch(
            "SELECT bo_url, bo_login, bo_password, branch_name FROM iiko_credentials WHERE tenant_id = $1",
            TENANT_ID,
        )
        
        if not branches:
            logger.error(f"Нет веток для tenant_id={TENANT_ID}")
            return
        
        logger.info(f"Phase 6: Обогащение временных полей для {len(branches)} веток")
        
        # Для каждого дня с DATE_FROM до вчера
        current_date = DATE_FROM
        yesterday = date.today() - timedelta(days=1)
        
        total_updated = 0
        
        while current_date <= yesterday:
            date_str = current_date.isoformat()
            date_next = (current_date + timedelta(days=1)).isoformat()
            
            logger.info(f"\n📅 {date_str}:")
            
            for branch in branches:
                branch_name = branch["branch_name"]
                city_name = branch_name.split("_")[0]
                
                if city_name in SKIP_CITIES:
                    logger.debug(f"  {branch_name}: пропущен (в SKIP_CITIES)")
                    continue
                
                logger.info(f"  {branch_name}...")
                
                try:
                    times_map = await _fetch_times_from_olap(
                        branch["bo_url"],
                        branch["bo_login"],
                        branch["bo_password"],
                        date_str,
                        date_next,
                    )
                    
                    # Обновляем БД
                    updated = 0
                    for (dept, dnum), times in times_map.items():
                        result = await conn.execute(
                            """UPDATE orders_raw
                               SET cooked_time = $1, ready_time = $2, send_time = $3, service_print_time = $4, updated_at = now()
                               WHERE tenant_id = $5 AND branch_name = $6 AND delivery_num = $7
                                 AND (cooked_time IS NULL OR ready_time IS NULL OR send_time IS NULL)""",
                            times.get("cooked_time"),
                            times.get("ready_time"),
                            times.get("send_time"),
                            times.get("service_print_time"),
                            TENANT_ID,
                            dept,
                            dnum,
                        )
                        updated += int(result.split()[-1])
                    
                    logger.info(f"    ✅ Обновлено {updated} заказов")
                    total_updated += updated
                    
                except Exception as e:
                    logger.error(f"    ❌ Ошибка: {e}")
            
            current_date += timedelta(days=1)
        
        logger.info(f"\n✅ Phase 6 завершена. Всего обновлено: {total_updated} заказов")
        
        # Финальная статистика
        stats = await conn.fetchrow("""
            SELECT 
              COUNT(*) as total,
              COUNT(*) FILTER (WHERE cooked_time IS NOT NULL) as has_cooked,
              COUNT(*) FILTER (WHERE ready_time IS NOT NULL) as has_ready,
              COUNT(*) FILTER (WHERE service_print_time IS NOT NULL) as has_print
            FROM orders_raw WHERE tenant_id = $1 AND date >= $2
        """, TENANT_ID, DATE_FROM)
        
        print(f"\n📊 Итоговая статистика (tenant_id={TENANT_ID}):")
        print(f"  Всего заказов: {stats['total']}")
        print(f"  cooked_time: {stats['has_cooked']} ({stats['has_cooked']/stats['total']*100:.1f}%)")
        print(f"  ready_time: {stats['has_ready']} ({stats['has_ready']/stats['total']*100:.1f}%)")
        print(f"  service_print_time: {stats['has_print']} ({stats['has_print']/stats['total']*100:.1f}%)")
    
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
