"""
Ежедневный утренний отчёт по точкам → Telegram.

09:25 местного (после OLAP enrichment в 09:00):
OLAP v2 → выручка/COGS/скидки + orders_raw → агрегаты → daily_stats + ТГ.
"""

import html
import json
import logging
from datetime import datetime, timedelta, timezone

from app.clients import telegram
from app.clients.iiko_bo_olap_v2 import get_all_branches_stats
from app.config import get_settings
from app.jobs.humor import get_morning_quip
from app.database import (
    aggregate_orders_for_daily_stats,
    clear_updates_for_date,
    get_updates_for_date,
    log_job_finish,
    log_job_start,
    upsert_daily_stats_batch,
)

logger = logging.getLogger(__name__)
settings = get_settings()


def _branch_tz(utc_offset: int) -> timezone:
    return timezone(timedelta(hours=utc_offset))


def _fmt_money(v) -> str:
    try:
        return f"{int(v):,} ₽".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def _fmt_num(v) -> str:
    try:
        return str(int(v))
    except (TypeError, ValueError):
        return "—"


def _fmt_pct(v) -> str:
    try:
        return f"{float(v):.1f}%"
    except (TypeError, ValueError):
        return "—"


def _format_branch_report(
    name: str,
    stats: dict,
    date_str: str,
    agg: dict,
    *,
    is_period: bool = False,
) -> str:
    """Форматирует отчёт точки для Telegram.

    stats  — OLAP v2 данные (revenue, cogs, discounts) или daily_stats row.
    agg    — агрегаты из orders_raw: задержки, времена, скидки, штат.
    is_period — True для отчётов за неделю/месяц (скрывает штат).
    """
    revenue = stats.get("revenue_net") or stats.get("revenue")
    check_count = stats.get("check_count") or stats.get("orders_count")
    avg_check = round(revenue / check_count) if revenue and check_count else None
    cogs_pct = stats.get("cogs_pct")
    discount_sum = stats.get("discount_sum")
    sailplay = stats.get("sailplay")

    lines = [f"📊 <b>{html.escape(name)}</b> | {date_str}", ""]

    lines.append(f"💰 Выручка: <b>{_fmt_money(revenue)}</b>" if revenue is not None else "💰 Выручка: <b>—</b>")

    checks_str = _fmt_num(check_count)
    avg_str = _fmt_money(avg_check) if avg_check else "—"
    lines.append(f"🧾 Чеков: {checks_str} | Средний чек: {avg_str}")
    lines.append("")

    disc_str = _fmt_money(discount_sum) if discount_sum else "—"
    sail_str = _fmt_money(sailplay) if sailplay else "—"
    lines.append(f"💸 Скидки: {disc_str} | SailPlay: {sail_str}")
    discount_types = agg.get("discount_types_agg") or []
    if isinstance(discount_types, str):
        try:
            discount_types = json.loads(discount_types)
        except (json.JSONDecodeError, TypeError):
            discount_types = []
    for dt in discount_types:
        if isinstance(dt, dict):
            cnt = dt.get("count", "")
            s = dt.get("sum", 0)
            cnt_str = f" x {cnt}" if cnt else ""
            lines.append(f"   └ {dt.get('type', '?')}{cnt_str}: {_fmt_money(s)}")
        else:
            lines.append(f"   └ {dt}")
    lines.append("")

    lines.append(f"📦 Себестоимость: {_fmt_pct(cogs_pct)}")
    lines.append("")

    late = agg.get("late_delivery_count") or 0
    total_d = agg.get("total_delivery_count") or 0
    if total_d > 0:
        pct = late / total_d * 100
        if late > 0:
            avg_late = agg.get("avg_late_min") or 0
            lines.append(f"🔴 Опозданий: {late} из {total_d} доставок ({pct:.1f}%) | среднее: {avg_late} мин")
        else:
            lines.append(f"✅ Опозданий: 0 из {total_d} доставок")
    else:
        lines.append("🚚 Опозданий: нет данных")

    cook = agg.get("avg_cooking_min")
    wait = agg.get("avg_wait_min")
    delivery = agg.get("avg_delivery_min")
    time_parts = []
    if cook is not None:
        time_parts.append(f"Готовка: {cook}")
    if wait is not None:
        time_parts.append(f"Ожидание: {wait}")
    if delivery is not None:
        time_parts.append(f"В пути: {delivery}")
    if time_parts:
        lines.append(f"🕐 {' | '.join(time_parts)} мин")
        total_time = sum(x for x in (cook, wait, delivery) if x is not None)
        if len([x for x in (cook, wait, delivery) if x is not None]) >= 2:
            lines.append(f"   └ Итого: {round(total_time, 1)} мин")

    exact_cnt = agg.get("exact_time_count") or 0
    if exact_cnt > 0:
        lines.append(f"📌 Точных заказов: {exact_cnt} (не в средних временах)")
    lines.append("")

    if not is_period:
        cooks_today = agg.get("cooks_today") or 0
        couriers_today = agg.get("couriers_today") or 0
        staff_parts = []
        if cooks_today:
            staff_parts.append(f"{cooks_today} поваров")
        if couriers_today:
            staff_parts.append(f"{couriers_today} курьеров")
        if staff_parts:
            lines.append(f"👥 На смене за день: {', '.join(staff_parts)}")

    return "\n".join(lines)



async def job_send_morning_report(utc_offset: int) -> None:
    """Единственный ежедневный отчёт. Запускается утром за вчера.
    1) OLAP v2 → выручка, COGS, скидки
    2) orders_raw → агрегаты (задержки, времена, типы скидок)
    3) Пишет daily_stats
    4) Отправляет в ТГ
    """
    log_id = await log_job_start(f"morning_report_utc{utc_offset}")

    tz = _branch_tz(utc_offset)
    yesterday = datetime.now(tz) - timedelta(days=1)
    date_iso = yesterday.strftime("%Y-%m-%d")

    branches = [b for b in settings.branches if b.get("utc_offset", 7) == utc_offset]
    if not branches:
        await log_job_finish(log_id, "ok", f"Нет точек для UTC+{utc_offset}")
        return

    try:
        all_stats = await get_all_branches_stats(yesterday)
    except Exception as e:
        logger.error(f"Ошибка iiko BO в утреннем отчёте: {e}")
        await telegram.error_alert(f"morning_report_utc{utc_offset}", str(e))
        await log_job_finish(log_id, "error", str(e))
        return

    updates = await get_updates_for_date(date_iso)
    updates_by_branch: dict[str, list[dict]] = {}
    for u in updates:
        updates_by_branch.setdefault(u["branch"], []).append(u)

    sent = 0
    for branch in branches:
        name = branch["name"]
        stats = all_stats.get(name, {})

        # Агрегаты из orders_raw (задержки, времена, типы скидок)
        agg = await aggregate_orders_for_daily_stats(name, date_iso)

        rev = stats.get("revenue_net") or 0
        chk = stats.get("check_count") or 0

        discount_types_json = json.dumps(
            agg.get("discount_types_agg") or stats.get("discount_types") or [],
            ensure_ascii=False,
        )

        total_d = agg.get("total_delivery_count") or 0
        late_d = agg.get("late_delivery_count") or 0
        late_pct = round(late_d / total_d * 100, 1) if total_d else 0

        try:
            await upsert_daily_stats_batch([{
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
            }])
        except Exception as e:
            logger.warning(f"Не удалось сохранить daily_stats [{name}]: {e}")

        # Отправка в ТГ
        branch_updates = updates_by_branch.get(name, [])
        try:
            lines = []
            if branch_updates:
                for u in branch_updates:
                    field = u["field"]
                    old = u["old_value"]
                    new = u["new_value"]
                    if field == "revenue":
                        try:
                            old_fmt = f"{int(float(old)):,} ₽".replace(",", " ") if old else "—"
                            new_fmt = f"{int(float(new)):,} ₽".replace(",", " ") if new else "—"
                        except (ValueError, TypeError):
                            old_fmt, new_fmt = old or "—", new or "—"
                        lines.append(f"⚠️ Данные обновлены: выручка {old_fmt} → {new_fmt}")
                    elif field == "check_count":
                        lines.append(f"⚠️ Данные обновлены: чеков {old or '—'} → {new or '—'}")
                if lines:
                    lines.append("")

            date_str = yesterday.strftime("%d.%m.%Y")
            msg_body = _format_branch_report(name, stats, date_str, agg)
            lines.append(msg_body)
            quip = await get_morning_quip(name, rev, chk, late_pct, agg.get("avg_late_min") or 0)
            if quip:
                lines.append(f"\n<i>{html.escape(quip)}</i>")
            await telegram.report("\n".join(lines))
            sent += 1
        except Exception as e:
            logger.error(f"Ошибка отправки утреннего отчёта [{name}]: {e}")

    try:
        await clear_updates_for_date(date_iso)
    except Exception as e:
        logger.warning(f"Не удалось очистить report_updates за {date_iso}: {e}")

    await log_job_finish(log_id, "ok", f"Отправлено: {sent}/{len(branches)}")
