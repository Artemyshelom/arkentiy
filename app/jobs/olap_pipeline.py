"""
Единый ежедневный OLAP-пайплайн — замена olap_enrichment + cancel_sync.

Расписание: 05:00 локального (вычисляется timezone-aware в main.py).
По понедельникам обрабатывает 7 дней назад (как было в olap_enrichment).

Шаги (один job, последовательно):
  A. fetch_order_detail  → upsert orders_raw
       Поля: клиент, тайминги, оплата, скидка, причина отмены, источник.
       Заменяет: olap_enrichment + cancel_sync.
  B. fetch_dish_detail   → update orders_raw.items + courier
       Поля: состав заказа, курьер (WaiterName для исторических данных).
  C. fetch_branch_aggregate → upsert daily_stats
       Поля: выручка, COGS%, чеки, нал/безнал, скидки.
       daily_report.py читает из daily_stats (0 OLAP!) в 09:25.

Устаревшие jobs (оставлены до окончания тестирования, потом удалить):
  - app/jobs/olap_enrichment.py
  - app/jobs/cancel_sync.py
"""

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.clients.olap_queries import (
    fetch_branch_aggregate,
    fetch_dish_detail,
    fetch_order_detail,
)
from app.database_pg import get_pool
from app.db import (
    aggregate_orders_for_daily_stats,
    get_branches,
    log_job_finish,
    log_job_start,
    upsert_daily_stats_batch,
)
from app.utils.job_tracker import track_job

logger = logging.getLogger(__name__)

LOCAL_UTC_OFFSET = 7  # fallback если в branch нет utc_offset


# ---------------------------------------------------------------------------
# Step A: агрегация и upsert orders_raw из Query A
# ---------------------------------------------------------------------------

def _aggregate_order_rows(rows: list[dict], target_branches: set[str]) -> dict[tuple, dict]:
    """
    Группирует строки OLAP Query A по (branch_name, delivery_num).
    Несколько строк на заказ возникают при разных типах оплаты (split-оплата).

    Возвращает {(branch, num): enriched_dict}:
      payment_type     — тип оплаты с макс. суммой
      pay_breakdown    — JSON {pay_type: amount}
      discount_type    — типы скидок через "; "
      cancel_reason    — из Delivery.CancelCause
      source           — из Delivery.SourceKey
      service_print_time — из Delivery.PrintTime
      cooked_time      — из Delivery.CookingFinishTime
      send_time        — из Delivery.SendTime
      opened_at        — из OpenTime
      client_phone     — из Delivery.CustomerPhone
      client_name      — из Delivery.CustomerName
      actual_time      — из Delivery.ActualTime
      planned_time     — из Delivery.ExpectedTime
      delivery_address — из Delivery.Address
      is_self_service  — True если Delivery.ServiceType == "PICKUP"
      sum              — из DishDiscountSumInt
      discount_sum     — из DiscountSum (пишется в orders_raw.discount_sum)
      status           — "Отменена" если CancelCause непустой
    """
    by_order: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        dept = (row.get("Department") or "").strip()
        num = row.get("Delivery.Number")
        if not dept or dept not in target_branches or num is None:
            continue
        by_order[(dept, str(int(num)))].append(row)

    result: dict[tuple, dict] = {}
    for key, order_rows in by_order.items():
        pay_parts: dict[str, float] = {}
        discount_types: list[str] = []
        cancel_reason = ""
        source = ""
        send_time = ""
        print_time = ""
        cooked_time = ""
        opened_at = ""
        client_phone = ""
        client_name = ""
        actual_time = ""
        planned_time = ""
        delivery_address = ""
        is_self_service = False
        total_amount = 0.0
        discount_sum = 0.0

        for r in order_rows:
            pay_type = r.get("PayTypes", "")
            amount = float(r.get("DishDiscountSumInt", 0) or 0)
            if pay_type and amount:
                pay_parts[pay_type] = pay_parts.get(pay_type, 0) + amount
            total_amount = max(total_amount, amount)

            disc_sum = float(r.get("DiscountSum", 0) or 0)
            # DiscountSum в DELIVERIES — per-order (корректное значение), берём макс.
            if disc_sum > discount_sum:
                discount_sum = disc_sum

            dt = (r.get("OrderDiscount.Type") or "").strip()
            if dt and dt not in discount_types:
                discount_types.append(dt)

            cr = r.get("Delivery.CancelCause")
            if cr and not cancel_reason:
                cancel_reason = str(cr).strip()

            sk = r.get("Delivery.SourceKey")
            if sk and not source:
                source = str(sk).strip()

            if not send_time:
                st = r.get("Delivery.SendTime")
                if st:
                    send_time = str(st)

            if not print_time:
                pt = r.get("Delivery.PrintTime")
                if pt:
                    print_time = str(pt)

            if not cooked_time:
                ct = r.get("Delivery.CookingFinishTime")
                if ct:
                    cooked_time = str(ct)

            if not opened_at:
                ot = r.get("OpenTime")
                if ot:
                    opened_at = str(ot)

            if not client_phone:
                cp = r.get("Delivery.CustomerPhone")
                if cp:
                    client_phone = str(cp).strip()

            if not client_name:
                cn = r.get("Delivery.CustomerName") or ""
                if cn and not cn.startswith("GUEST"):
                    client_name = cn.strip()

            if not actual_time:
                at = r.get("Delivery.ActualTime")
                if at:
                    actual_time = str(at)

            if not planned_time:
                pet = r.get("Delivery.ExpectedTime")
                if pet:
                    planned_time = str(pet)

            if not delivery_address:
                da = r.get("Delivery.Address")
                if da:
                    delivery_address = str(da).strip()

            stype = (r.get("Delivery.ServiceType") or "").upper()
            if stype == "PICKUP":
                is_self_service = True

        main_pay = max(pay_parts, key=pay_parts.get) if pay_parts else ""

        result[key] = {
            "payment_type": main_pay,
            "pay_breakdown": json.dumps(pay_parts, ensure_ascii=False) if pay_parts else "",
            "discount_type": "; ".join(discount_types),
            "cancel_reason": cancel_reason,
            "source": source,
            "send_time": send_time,
            "service_print_time": print_time,
            "cooked_time": cooked_time,
            "opened_at": opened_at,
            "client_phone": client_phone,
            "client_name": client_name,
            "actual_time": actual_time,
            "planned_time": planned_time,
            "delivery_address": delivery_address,
            "is_self_service": is_self_service,
            "sum": total_amount,
            "discount_sum": discount_sum,
            "status": "Отменена" if cancel_reason else None,
        }
    return result


async def _upsert_order_data(enriched: dict[tuple, dict], tenant_id: int) -> int:
    """
    UPSERT orders_raw данными из Query A.
    COALESCE: не перезаписывает непустые значения из Events API.
    Исключение: timing-поля (opened_at, cooked_time, send_time) — OLAP надёжнее Events API.
    """
    if not enriched:
        return 0
    pool = get_pool()
    updated = 0

    for (branch, num), data in enriched.items():
        # Timing-поля — принудительно обновляем (OLAP точнее Events API)
        force_update_cols = {
            "opened_at", "cooked_time", "send_time", "service_print_time",
        }
        # Остальные — COALESCE (не трогаем если уже заполнено)
        coalesce_cols = {
            "payment_type", "pay_breakdown", "discount_type", "source",
            "client_phone", "client_name", "actual_time", "planned_time",
            "delivery_address", "cancel_reason",
        }

        sets = []
        vals: list = [tenant_id]  # $1 = tenant_id
        idx = 2

        for col in force_update_cols:
            val = data.get(col)
            if val:
                sets.append(f"{col} = ${idx}")
                vals.append(val)
                idx += 1

        for col in coalesce_cols:
            val = data.get(col)
            if val:
                sets.append(
                    f"{col} = CASE WHEN {col} IS NULL OR {col} = '' "
                    f"THEN ${idx} ELSE {col} END"
                )
                vals.append(val)
                idx += 1

        # is_self_service — обновляем только если True (самовывоз точно известен)
        if data.get("is_self_service"):
            sets.append(f"is_self_service = ${idx}")
            vals.append(True)
            idx += 1

        # sum — обновляем COALESCE
        sum_val = data.get("sum")
        if sum_val:
            sets.append(f"sum = CASE WHEN sum IS NULL OR sum = 0 THEN ${idx} ELSE sum END")
            vals.append(sum_val)
            idx += 1

        # discount_sum — всегда обновляем (DELIVERIES точнее)
        if data.get("discount_sum"):
            sets.append(f"discount_sum = ${idx}")
            vals.append(data["discount_sum"])
            idx += 1

        # status = 'Отменена' — только если OLAP видит отмену
        if data.get("status") == "Отменена":
            sets.append(f"status = ${idx}")
            vals.append("Отменена")
            idx += 1

        if not sets:
            continue

        vals.append(datetime.now(timezone.utc))
        updated_at_idx = idx
        idx += 1
        vals.append(branch)
        branch_idx = idx
        idx += 1
        vals.append(num)
        num_idx = idx

        sql = (
            f"UPDATE orders_raw SET {', '.join(sets)}, "
            f"updated_at = ${updated_at_idx} "
            f"WHERE tenant_id = $1 AND branch_name = ${branch_idx} "
            f"AND delivery_num = ${num_idx}"
        )
        res = await pool.execute(sql, *vals)
        count_str = res.split()[-1]
        if count_str.isdigit():
            updated += int(count_str)

    return updated


# ---------------------------------------------------------------------------
# Step B: агрегация и upsert items + courier из Query B
# ---------------------------------------------------------------------------

def _aggregate_dish_rows(rows: list[dict], target_branches: set[str]) -> dict[tuple, dict]:
    """
    Группирует строки Query B по (branch, delivery_num).
    Собирает items JSON и курьера (WaiterName).
    """
    by_order: dict[tuple, dict] = {}
    for row in rows:
        dept = (row.get("Department") or "").strip()
        num = row.get("Delivery.Number")
        if not dept or dept not in target_branches or num is None:
            continue
        key = (dept, str(int(num)))
        if key not in by_order:
            by_order[key] = {"items": [], "courier": ""}

        dish = (row.get("DishName") or "").strip()
        qty = int(float(row.get("Amount", 1) or 1))
        if dish:
            # Суммируем если блюдо уже есть (OLAP может вернуть дубликаты при разных модиф.)
            existing = next((it for it in by_order[key]["items"] if it["name"] == dish), None)
            if existing:
                existing["qty"] += qty
            else:
                by_order[key]["items"].append({"name": dish, "qty": qty})

        if not by_order[key]["courier"]:
            waiter = (row.get("WaiterName") or "").strip()
            if waiter:
                by_order[key]["courier"] = waiter

    return by_order


async def _upsert_dish_data(dish_data: dict[tuple, dict], tenant_id: int) -> int:
    """
    Обновляет orders_raw.items и courier из Query B.
    items — COALESCE (не перезаписываем, если уже заполнены из Events API).
    courier — COALESCE.
    """
    if not dish_data:
        return 0
    pool = get_pool()
    updated = 0

    for (branch, num), data in dish_data.items():
        items_json = json.dumps(data["items"], ensure_ascii=False) if data["items"] else ""
        courier = data.get("courier", "")

        sets = []
        vals: list = [tenant_id]
        idx = 2

        if items_json:
            sets.append(
                f"items = CASE WHEN items IS NULL OR items = '' OR items = '[]' "
                f"THEN ${idx} ELSE items END"
            )
            vals.append(items_json)
            idx += 1

        if courier:
            sets.append(
                f"courier = CASE WHEN courier IS NULL OR courier = '' "
                f"THEN ${idx} ELSE courier END"
            )
            vals.append(courier)
            idx += 1

        if not sets:
            continue

        vals.extend([datetime.now(timezone.utc), branch, num])
        sql = (
            f"UPDATE orders_raw SET {', '.join(sets)}, "
            f"updated_at = ${idx} "
            f"WHERE tenant_id = $1 AND branch_name = ${idx + 1} "
            f"AND delivery_num = ${idx + 2}"
        )
        res = await pool.execute(sql, *vals)
        count_str = res.split()[-1]
        if count_str.isdigit():
            updated += int(count_str)

    return updated


# ---------------------------------------------------------------------------
# Step C: upsert daily_stats из Query C + orders_raw
# ---------------------------------------------------------------------------

async def _upsert_daily_stats_from_aggregate(
    olap_stats: dict[str, dict],
    branches: list[dict],
    dates: list[str],
    tenant_id: int,
) -> int:
    """
    Комбинирует OLAP-агрегаты (Query C) с агрегатами из orders_raw
    и делает upsert в daily_stats.
    Вызывается для каждой даты из диапазона.
    """
    saved = 0
    for date_iso in dates:
        rows_to_upsert = []
        for branch in branches:
            name = branch["name"]
            stats = olap_stats.get(name, {})
            if not stats and not stats.get("check_count"):
                continue

            agg = await aggregate_orders_for_daily_stats(name, date_iso)

            rev = stats.get("revenue_net") or 0.0
            chk = stats.get("check_count") or 0
            late_d = agg.get("late_delivery_count") or 0
            total_d = agg.get("total_delivery_count") or 0
            late_pct = round(late_d / total_d * 100, 1) if total_d else 0.0

            discount_types_json = json.dumps(
                stats.get("discount_types") or agg.get("discount_types_agg") or [],
                ensure_ascii=False,
            )

            rows_to_upsert.append({
                "branch_name":    name,
                "date":           date_iso,
                "orders_count":   chk,
                "revenue":        rev,
                "avg_check":      round(rev / chk) if chk else 0,
                "cogs_pct":       stats.get("cogs_pct"),
                "sailplay":       stats.get("sailplay"),
                "discount_sum":   stats.get("discount_sum"),
                "discount_types": discount_types_json,
                "delivery_count": chk - (stats.get("pickup_count") or 0),
                "pickup_count":   stats.get("pickup_count") or 0,
                "cash":           stats.get("cash") or 0.0,
                "noncash":        stats.get("noncash") or 0.0,
                "late_count":     late_d,
                "total_delivered": total_d,
                "late_percent":   late_pct,
                "avg_late_min":   agg.get("avg_late_min") or 0,
                "cooks_count":    agg.get("cooks_today") or 0,
                "couriers_count": agg.get("couriers_today") or 0,
                "late_delivery_count": late_d,
                "late_pickup_count":   agg.get("late_pickup_count") or 0,
                "avg_cooking_min":     agg.get("avg_cooking_min"),
                "avg_wait_min":        agg.get("avg_wait_min"),
                "avg_delivery_min":    agg.get("avg_delivery_min"),
                "exact_time_count":    agg.get("exact_time_count") or 0,
                "new_customers":             agg.get("new_customers", 0),
                "new_customers_revenue":     agg.get("new_customers_revenue", 0.0),
                "repeat_customers":          agg.get("repeat_customers", 0),
                "repeat_customers_revenue":  agg.get("repeat_customers_revenue", 0.0),
            })

        if rows_to_upsert:
            try:
                await upsert_daily_stats_batch(rows_to_upsert, tenant_id=tenant_id)
                saved += len(rows_to_upsert)
            except Exception as e:
                logger.error(f"pipeline daily_stats upsert [{date_iso}]: {e}")

    return saved


# ---------------------------------------------------------------------------
# Главный job
# ---------------------------------------------------------------------------

@track_job("olap_pipeline")
async def job_olap_pipeline(tenant_id: int = 1) -> None:
    """
    Единый ежедневный OLAP-пайплайн: шаги A → B → C.
    Запускается из main.py по timezone-aware расписанию в 05:00 локального.
    """
    log_id = await log_job_start(f"olap_pipeline_t{tenant_id}")

    branches = get_branches(tenant_id)
    if not branches:
        await log_job_finish(log_id, "ok", f"Нет точек для tenant_id={tenant_id}")
        return

    utc_offset = branches[0].get("utc_offset", LOCAL_UTC_OFFSET)
    now_local = datetime.now(tz=timezone.utc) + timedelta(hours=utc_offset)
    yesterday = now_local - timedelta(days=1)
    yesterday_iso = yesterday.strftime("%Y-%m-%d")
    today_iso = now_local.strftime("%Y-%m-%d")

    # По понедельникам — захватываем 7 дней назад (как в прежнем olap_enrichment)
    is_monday = now_local.weekday() == 0
    if is_monday:
        date_from = (now_local - timedelta(days=7)).strftime("%Y-%m-%d")
    else:
        date_from = yesterday_iso

    branch_names = [b["name"] for b in branches]
    branch_names_set = set(branch_names)

    # Даты для обновления daily_stats (все дни в диапазоне)
    dates_range: list[str] = []
    d = datetime.strptime(date_from, "%Y-%m-%d")
    end = datetime.strptime(yesterday_iso, "%Y-%m-%d")
    while d <= end:
        dates_range.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)

    logger.info(
        f"[olap_pipeline] tenant={tenant_id} период={date_from}..{yesterday_iso} "
        f"точек={len(branches)} понедельник={is_monday}"
    )

    # ─── Шаг A: order detail → orders_raw ────────────────────────────────
    try:
        rows_a = await fetch_order_detail(date_from, today_iso, branches)
        enriched = _aggregate_order_rows(rows_a, branch_names_set)
        updated_a = await _upsert_order_data(enriched, tenant_id)
        logger.info(f"[olap_pipeline] A: получено={len(rows_a)} строк, обновлено={updated_a} заказов")
    except Exception as e:
        logger.error(f"[olap_pipeline] Шаг A (order detail): {e}", exc_info=True)
        updated_a = 0

    # ─── Шаг B: dish detail → orders_raw.items + courier ─────────────────
    try:
        rows_b = await fetch_dish_detail(date_from, today_iso, branches)
        dish_data = _aggregate_dish_rows(rows_b, branch_names_set)
        updated_b = await _upsert_dish_data(dish_data, tenant_id)
        logger.info(f"[olap_pipeline] B: получено={len(rows_b)} строк, обновлено={updated_b} заказов")
    except Exception as e:
        logger.error(f"[olap_pipeline] Шаг B (dish detail): {e}", exc_info=True)
        updated_b = 0

    # ─── Шаг C: branch aggregate → daily_stats ───────────────────────────
    saved_c = 0
    try:
        for date_iso in dates_range:
            next_day = (datetime.strptime(date_iso, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            olap_stats = await fetch_branch_aggregate(date_iso, next_day, branches)
            saved = await _upsert_daily_stats_from_aggregate(
                olap_stats, branches, [date_iso], tenant_id
            )
            saved_c += saved
        logger.info(f"[olap_pipeline] C: сохранено daily_stats={saved_c} строк по {len(dates_range)} дням")
    except Exception as e:
        logger.error(f"[olap_pipeline] Шаг C (daily_stats): {e}", exc_info=True)

    detail = (
        f"период={date_from}..{yesterday_iso} "
        f"A={updated_a} B={updated_b} C={saved_c}"
    )
    await log_job_finish(log_id, "ok", detail)
