"""
Ежедневный утренний отчёт по точкам → Telegram.

09:25 местного (после olap_pipeline в 05:00):
daily_stats (iiko OLAP, заполнен пайплайном) + orders_raw → ТГ.
"""

import html
import json
import logging
from datetime import datetime, timedelta, timezone

from app.clients import telegram
from app.config import get_settings
from app.jobs.humor import get_morning_quip
from app.db import (
    aggregate_orders_for_daily_stats,
    clear_updates_for_date,
    get_daily_stats,
    get_fot_daily,
    get_updates_for_date,
    log_job_finish,
    log_job_start,
)
from app.utils.formatting import fmt_money as _fmt_money, fmt_num as _fmt_num, fmt_pct as _fmt_pct
from app.utils.timezone import tz_from_offset as _branch_tz
from app.utils.job_tracker import track_job

try:
    from app.db import get_all_branches as _get_all_branches, get_module_chats_for_city as _get_module_chats
except ImportError:
    _get_all_branches = None
    _get_module_chats = None

logger = logging.getLogger(__name__)
settings = get_settings()


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
    lines.append(f"💸 Скидки: {disc_str} | Оплата бонусами: {sail_str}")
    discount_types = stats.get("discount_types") or agg.get("discount_types_agg") or []
    if isinstance(discount_types, str):
        try:
            discount_types = json.loads(discount_types)
        except (json.JSONDecodeError, TypeError):
            discount_types = []
    for dt in discount_types:
        if isinstance(dt, dict):
            cnt = dt.get("count", "")
            s = dt.get("sum", 0)
            dtype = dt.get("type", "?")
            if dtype == "SailPlay":
                dtype = "Промокоды SailPlay"
            cnt_str = f" ({cnt} шт.)" if cnt else ""
            lines.append(f"   └ {dtype}: {_fmt_money(s)}{cnt_str}")
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
    pc_cnt = agg.get("payment_changed_count") or stats.get("payment_changed_count") or 0
    if pc_cnt > 0:
        lines.append(f"⚠️ Исключено из расчёта: {pc_cnt} (смена оплаты)")
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

    # Блок ФОТ (только повара — курьеры на мотивационной программе)
    fot = agg.get("_fot")  # передаётся из job_send_morning_report
    if fot and revenue:
        cook_fot = fot.get("cook") or 0
        if cook_fot > 0:
            lines.append(f"💼 ФОТ поваров: {round(cook_fot / revenue * 100, 1)}% от выручки ({_fmt_money(cook_fot)})")
    elif is_period:
        period_fot = agg.get("_fot")
        if period_fot and revenue:
            cook_fot = period_fot.get("cook") or 0
            if cook_fot > 0:
                lines.append(f"💼 ФОТ поваров: {round(cook_fot / revenue * 100, 1)}% от выручки")

    new_c = agg.get("new_customers") or 0
    new_r = agg.get("new_customers_revenue") or 0.0
    rep_c = agg.get("repeat_customers") or 0
    rep_r = agg.get("repeat_customers_revenue") or 0.0
    if new_c + rep_c > 0:
        total_r = new_r + rep_r
        new_pct = round(new_r / total_r * 100) if total_r else 0
        rep_pct = 100 - new_pct
        lines.append("")
        lines.append("👥 Клиенты:")
        lines.append(f"   Новых: {new_c} · {_fmt_money(new_r)} ({new_pct}%)")
        lines.append(f"   Повторных: {rep_c} · {_fmt_money(rep_r)} ({rep_pct}%)")

    return "\n".join(lines)


def _format_daily_summary(date_str: str, branches: list[tuple[str, float, int]]) -> str:
    """Сводная таблица выручки по всем точкам тенанта за день."""
    sorted_b = sorted(branches, key=lambda x: x[1] or 0, reverse=True)
    total_rev = sum(r for _, r, _ in sorted_b if r)
    total_chk = sum(c for _, _, c in sorted_b if c)

    rows = []
    for name, rev, chk in sorted_b:
        short = html.escape(name[:20])
        rev_str = _fmt_money(rev) if rev else "—"
        rows.append(f"{short:<20}  {rev_str:>12}  {chk} чек")
    rows.append("─" * 40)
    rows.append(f"{'Итого:':<20}  {_fmt_money(total_rev):>12}  {total_chk} чек")

    return f"📈 <b>Итоги {date_str}</b>\n\n<code>" + "\n".join(rows) + "</code>"


@track_job("daily_report")
async def job_send_morning_report(utc_offset: int) -> None:
    """Утренний отчёт. Читает из daily_stats (заполнен пайплайном в 05:00), 0 OLAP-запросов."""
    log_id = await log_job_start(f"morning_report_utc{utc_offset}")

    tz = _branch_tz(utc_offset)
    yesterday = datetime.now(tz) - timedelta(days=1)
    date_iso = yesterday.strftime("%Y-%m-%d")

    # Берём все точки всех тенантов (multi-tenant)
    if _get_all_branches:
        all_branches = _get_all_branches()
    else:
        all_branches = [{**b, "tenant_id": 1} for b in settings.branches]
    branches = [b for b in all_branches if b.get("utc_offset", 7) == utc_offset]
    if not branches:
        await log_job_finish(log_id, "ok", f"Нет точек для UTC+{utc_offset}")
        return

    updates = await get_updates_for_date(date_iso)
    updates_by_branch: dict[str, list[dict]] = {}
    for u in updates:
        updates_by_branch.setdefault(u["branch"], []).append(u)

    # Для итоговой сводки: (name, rev, chk) по тенанту + чаты для рассылки
    branch_summaries: dict[int, list[tuple[str, float, int]]] = {}
    tenant_report_chats: dict[int, set[int]] = {}

    sent = 0
    for branch in branches:
        name = branch["name"]
        tenant_id = branch.get("tenant_id", 1)

        # Читаем из daily_stats (заполнен пайплайном в 05:00)
        stats = await get_daily_stats(name, date_iso, tenant_id) or {}

        # Агрегаты из orders_raw (задержки, времена, взаимедействия со сменой оплаты)
        agg = await aggregate_orders_for_daily_stats(name, date_iso, tenant_id)

        # ФОТ за вчера — если пайплайн отработал, подставляем в блок отчёта
        try:
            fot_data = await get_fot_daily(name, date_iso, tenant_id)
            if fot_data:
                agg["_fot"] = fot_data
        except Exception as _e:
            logger.debug(f"get_fot_daily [{name}]: {_e}")

        rev = stats.get("revenue") or 0
        chk = stats.get("orders_count") or 0
        branch_summaries.setdefault(tenant_id, []).append((name, rev, chk))

        late_d = agg.get("late_delivery_count") or stats.get("late_count") or 0
        total_d = agg.get("total_delivery_count") or stats.get("total_delivered") or 0
        late_pct = round(late_d / total_d * 100, 1) if total_d else 0

        # Отправка в ТГ — per-tenant роутинг
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

            full_msg = "\n".join(lines)
            city = branch.get("city", "")

            if _get_module_chats and tenant_id != 1:
                # Внешний тенант — шлём в его "reports" чаты
                report_chats = await _get_module_chats("reports", city, tenant_id)
                for chat_id in report_chats:
                    await telegram.send_message(str(chat_id), full_msg)
                    tenant_report_chats.setdefault(tenant_id, set()).add(chat_id)
                if report_chats:
                    sent += 1
            else:
                # Ёбидоёби (tenant_id=1) — глобальный канал
                await telegram.report(full_msg)
                tenant_report_chats.setdefault(1, set()).add(0)  # маркер «слали в основной канал»
                sent += 1
        except Exception as e:
            logger.error(f"Ошибка отправки утреннего отчёта [{name}]: {e}")

    try:
        await clear_updates_for_date(date_iso)
    except Exception as e:
        logger.warning(f"Не удалось очистить report_updates за {date_iso}: {e}")

    # Итоговая сводка по тенанту (только если точек ≥ 2)
    date_str_fmt = yesterday.strftime("%d.%m.%Y")
    for tid, br_list in branch_summaries.items():
        if len(br_list) < 2:
            continue
        try:
            summary_msg = _format_daily_summary(date_str_fmt, br_list)
            if tid == 1:
                await telegram.report(summary_msg)
            else:
                chat_ids = tenant_report_chats.get(tid, set())
                for chat_id in chat_ids:
                    await telegram.send_message(str(chat_id), summary_msg)
        except Exception as e:
            logger.error(f"Ошибка отправки итоговой сводки [tenant_id={tid}]: {e}")

    await log_job_finish(log_id, "ok", f"Отправлено: {sent}/{len(branches)}")
