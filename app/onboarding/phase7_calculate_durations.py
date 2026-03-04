"""
Phase 7: Расчёт производных временных метрик.

Вычисляет длительности на основе заполненных временных полей:
- cooking_duration = cooked_time - opened_at (сколько варили)
- idle_time = ready_time - cooked_time (сколько стояло после готовки)
- delivery_duration = actual_time - send_time (сколько везли)
- total_duration = actual_time - opened_at (сквозное время)

Работает для обоих тенантов (tenant_id=1 и tenant_id=3).
Запускается один раз при необходимости или периодически.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

import asyncpg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("phase7_durations")


async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    
    print("\n" + "="*70)
    print("🔢 Phase 7: Расчёт производных временных метрик")
    print("="*70)
    
    try:
        # Добавляем колонки если их нет
        print("\n📋 Проверяем структуру БД...")
        
        for col_name, col_type in [
            ("cooking_duration", "INTERVAL"),
            ("idle_time", "INTERVAL"),
            ("delivery_duration", "INTERVAL"),
            ("total_duration", "INTERVAL"),
        ]:
            try:
                await conn.execute(
                    f"ALTER TABLE orders_raw ADD COLUMN {col_name} {col_type};"
                )
                logger.info(f"  ✓ Добавлена колонка {col_name}")
            except asyncpg.exceptions.DuplicateColumnError:
                logger.info(f"  ✓ {col_name} уже существует")
            except Exception as e:
                logger.warning(f"  ⚠️  {col_name}: {e}")
        
        # Расчёт для каждого тенанта
        for tenant_id in [1, 3]:
            print(f"\n👤 Tenant {tenant_id}:")
            
            # 1. cooking_duration = cooked_time - opened_at
            try:
                result = await conn.execute("""
                    UPDATE orders_raw
                    SET cooking_duration = TO_TIMESTAMP(cooked_time, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'::TEXT) - 
                                          TO_TIMESTAMP(opened_at, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'::TEXT)::TIMESTAMP WITH TIME ZONE,
                        updated_at = now()
                    WHERE tenant_id = $1
                      AND cooked_time IS NOT NULL
                      AND opened_at IS NOT NULL
                      AND cooking_duration IS NULL
                """, tenant_id)
                cnt = int(result.split()[-1])
                print(f"  ✓ cooking_duration: обновлено {cnt} заказов")
            except Exception as e:
                logger.error(f"  ❌ cooking_duration: {e}")
            
            # 2. idle_time = ready_time - cooked_time
            try:
                result = await conn.execute("""
                    UPDATE orders_raw
                    SET idle_time = TO_TIMESTAMP(ready_time, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'::TEXT) -
                                    TO_TIMESTAMP(cooked_time, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'::TEXT),
                        updated_at = now()
                    WHERE tenant_id = $1
                      AND ready_time IS NOT NULL
                      AND cooked_time IS NOT NULL
                      AND idle_time IS NULL
                """, tenant_id)
                cnt = int(result.split()[-1])
                print(f"  ✓ idle_time: обновлено {cnt} заказов")
            except Exception as e:
                logger.error(f"  ❌ idle_time: {e}")
            
            # 3. delivery_duration = actual_time - send_time
            try:
                result = await conn.execute("""
                    UPDATE orders_raw
                    SET delivery_duration = TO_TIMESTAMP(actual_time, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'::TEXT) -
                                           TO_TIMESTAMP(send_time, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'::TEXT),
                        updated_at = now()
                    WHERE tenant_id = $1
                      AND actual_time IS NOT NULL
                      AND send_time IS NOT NULL
                      AND delivery_duration IS NULL
                """, tenant_id)
                cnt = int(result.split()[-1])
                print(f"  ✓ delivery_duration: обновлено {cnt} заказов")
            except Exception as e:
                logger.error(f"  ❌ delivery_duration: {e}")
            
            # 4. total_duration = actual_time - opened_at
            try:
                result = await conn.execute("""
                    UPDATE orders_raw
                    SET total_duration = TO_TIMESTAMP(actual_time, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'::TEXT) - 
                                        TO_TIMESTAMP(opened_at, 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'::TEXT)::TIMESTAMP WITH TIME ZONE,
                        updated_at = now()
                    WHERE tenant_id = $1
                      AND actual_time IS NOT NULL
                      AND opened_at IS NOT NULL
                      AND total_duration IS NULL
                """, tenant_id)
                cnt = int(result.split()[-1])
                print(f"  ✓ total_duration: обновлено {cnt} заказов")
            except Exception as e:
                logger.error(f"  ❌ total_duration: {e}")
            
            # Статистика
            stats = await conn.fetchrow("""
                SELECT COUNT(*) as total,
                       COUNT(*) FILTER (WHERE cooking_duration IS NOT NULL) as cooking,
                       COUNT(*) FILTER (WHERE idle_time IS NOT NULL) as idle,
                       COUNT(*) FILTER (WHERE delivery_duration IS NOT NULL) as delivery,
                       COUNT(*) FILTER (WHERE total_duration IS NOT NULL) as total_dur
                FROM orders_raw WHERE tenant_id = $1
            """, tenant_id)
            
            t = stats['total']
            print(f"\n  📊 Итого (tenant_id={tenant_id}, {t} заказов):")
            print(f"    cooking_duration: {stats['cooking']:6d} ({stats['cooking']/t*100:.1f}%)")
            print(f"    idle_time:        {stats['idle']:6d} ({stats['idle']/t*100:.1f}%)")
            print(f"    delivery_duration:{stats['delivery']:6d} ({stats['delivery']/t*100:.1f}%)")
            print(f"    total_duration:   {stats['total_dur']:6d} ({stats['total_dur']/t*100:.1f}%)")
        
        print("\n" + "="*70)
        print("✅ Phase 7 завершена!")
        print("="*70)
        print("""
📊 Доступные метрики:
  cooking_duration    — время готовки (мин-секунды варили)
  idle_time           — время ожидания отправки (когда готов, но не отправлен)
  delivery_duration   — время доставки (как долго везли)
  total_duration      — сквозное время (от открытия до доставки)

💡 Использование:
  SELECT 
    order_num,
    EXTRACT(EPOCH FROM cooking_duration) / 60 as cook_minutes,
    EXTRACT(EPOCH FROM idle_time) / 60 as idle_minutes,
    EXTRACT(EPOCH FROM delivery_duration) / 60 as delivery_minutes
  FROM orders_raw
  WHERE tenant_id = 1 AND cooking_duration IS NOT NULL
  LIMIT 10;
""")
    
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
