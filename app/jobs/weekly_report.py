"""
Еженедельный отчёт по сети и городам → Telegram.

Расписание: каждый понедельник в 09:30 местного (после утреннего daily_report в 09:25).
Отчитывается за прошедшую неделю (пн–вс).

Структура отчёта:
  1. Сводка по сети тенанта:  суммарная выручка / чеки / опоздания за неделю
  2. Разбивка по точкам:       каждая точка в одном блоке (переиспользует _format_branch_report)
  3. Сравнение WoW:            если есть данные за предыдущую неделю — дельты выручки/чеков
"""

import html
import logging
from datetime import datetime, timedelta

from app.clients import telegram
from app.config import get_settings
from app.db import (
    get_branches,
    get_fot_period,
    get_module_chats_for_city,
    get_period_stats,
    get_repeat_conversion,
    log_job_finish,
    log_job_start,
)
from app.jobs.daily_report import _format_branch_report
from app.utils.formatting import fmt_money as _fmt, fmt_pct as _fmt_pct, fmt_num as _fmt_num
from app.utils.job_tracker import track_job
from app.utils.timezone import tz_from_offset as _branch_tz

logger = logging.getLogger(__name__)
settings = get_settings()


def _week_range(today: "date") -> tuple[str, str, str]:
    """Возвращает (date_from, date_to, label) для прошлой недели (пн–вс)."""
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)
    label = f"{last_monday.strftime('%d.%m')} – {last_sunday.strftime('%d.%m.%Y')}"
    return last_monday.isoformat(), last_sunday.isoformat(), label


def _format_network_summary(
    label: str,
    branches_stats: list[tuple[str, dict, dict | None]],
    conversion: dict | None = None,
    fot: dict | None = None,
) -> str:
    """Сводка по всей сети тенанта за неделю.

    branches_stats — список (branch_name, cur_stats, prev_stats).
    cur_stats / prev_stats — из get_period_stats (None если нет данных).
    """
    lines = [f"📅 <b>Итоги недели {html.escape(label)}</b>", ""]

    total_rev = 0
    total_chk = 0
    total_late = 0
    total_delivered = 0
    prev_rev = 0
    has_prev = False

    total_new_c = 0
    total_new_r = 0.0
    total_rep_c = 0
    total_rep_r = 0.0

    branch_rows: list[str] = []
    for name, cur, prev in branches_stats:
        if not cur:
            continue
        rev = cur.get("revenue") or 0
        chk = cur.get("orders_count") or 0
        late = cur.get("late_delivery_count") or cur.get("late_count") or 0
        deliv = cur.get("total_delivered") or 0
        total_rev += rev
        total_chk += chk
        total_late += late
        total_delivered += deliv
        total_new_c += cur.get("new_customers") or 0
        total_new_r += cur.get("new_customers_revenue") or 0.0
        total_rep_c += cur.get("repeat_customers") or 0
        total_rep_r += cur.get("repeat_customers_revenue") or 0.0

        p_rev = (prev.get("revenue") or 0) if prev else 0
        if p_rev:
            has_prev = True
            prev_rev += p_rev
            delta = rev - p_rev
            arrow = "▲" if delta >= 0 else "▼"
            pct = abs(delta / p_rev * 100)
            wow = f" {arrow}{pct:.0f}%"
        else:
            wow = ""

        short = html.escape(name[:22])
        branch_rows.append(
            f"  {short}: <b>{_fmt(rev)}</b> · {_fmt_num(chk)} чек{wow}"
        )

    lines.append(f"💰 <b>Выручка сети: {_fmt(total_rev)}</b>")
    if has_prev and prev_rev:
        delta_net = total_rev - prev_rev
        arrow = "▲" if delta_net >= 0 else "▼"
        pct_net = abs(delta_net / prev_rev * 100)
        lines.append(f"   {arrow} {pct_net:.1f}% к прошлой неделе ({_fmt(prev_rev)})")

    lines.append(f"🧾 Чеков: {_fmt_num(total_chk)}")
    if total_delivered > 0:
        late_pct = total_late / total_delivered * 100
        emoji = "✅" if total_late == 0 else ("🟡" if late_pct < 10 else "🔴")
        lines.append(f"{emoji} Опозданий: {total_late} из {total_delivered} ({late_pct:.1f}%)")
    lines.append("")

    if branch_rows:
        lines.append("По точкам:")
        lines.extend(branch_rows)

    if total_new_c + total_rep_c > 0:
        total_cr = total_new_r + total_rep_r
        new_pct = round(total_new_r / total_cr * 100) if total_cr else 0
        rep_pct = 100 - new_pct
        lines.append("")
        lines.append("👥 Клиенты за неделю:")
        lines.append(f"   Новых: {_fmt_num(total_new_c)} · {_fmt(total_new_r)} ({new_pct}%)")
        lines.append(f"   Повторных: {_fmt_num(total_rep_c)} · {_fmt(total_rep_r)} ({rep_pct}%)")

    if conversion and conversion.get("new_count"):
        lines.append("")
        pct = conversion["conversion_pct"]
        new_cnt = conversion["new_count"]
        conv_cnt = conversion["converted"]
        month = conversion.get("month_label", "прошлый месяц")
        lines.append(f"📈 Конверсия за {month}: {pct}%")
        lines.append(f"   (из {new_cnt} новых {conv_cnt} заказали повторно)")

    if fot and total_rev > 0:
        total_fot = sum(v for v in fot.values() if isinstance(v, (int, float)))
        if total_fot > 0:
            total_pct = round(total_fot / total_rev * 100, 1)
            lines.append("")
            lines.append(f"💼 ФОТ за неделю: {total_pct}% от выручки ({_fmt(total_fot)})")
            parts = []
            if (fot.get("cook") or 0) > 0:
                parts.append(f"Повара: {round(fot['cook'] / total_rev * 100, 1)}%")
            if (fot.get("courier") or 0) > 0:
                parts.append(f"Курьеры: {round(fot['courier'] / total_rev * 100, 1)}%")
            if parts:
                lines.append("   " + " · ".join(parts))

    return "\n".join(lines)


@track_job("weekly_report")
async def job_weekly_report(utc_offset: int) -> None:
    """Еженедельный отчёт для всех тенантов с точками на данном utc_offset.

    Запускается раз в понедельник. Внутри фильтрует точки по offset,
    чтобы корректно определить «сегодня» для каждого тенанта.
    """
    from app.database_pg import get_all_branches, get_module_chats_for_city as _get_chats

    log_id = await log_job_start(f"weekly_report_utc{utc_offset}")

    tz = _branch_tz(utc_offset)
    today = datetime.now(tz).date()
    date_from, date_to, label = _week_range(today)

    # Предыдущая неделя — для WoW
    prev_from_d = datetime.fromisoformat(date_from) - timedelta(days=7)
    prev_to_d = datetime.fromisoformat(date_to) - timedelta(days=7)
    prev_from = prev_from_d.strftime("%Y-%m-%d")
    prev_to = prev_to_d.strftime("%Y-%m-%d")

    all_branches = get_all_branches()
    branches = [b for b in all_branches if b.get("utc_offset", 7) == utc_offset]
    if not branches:
        await log_job_finish(log_id, "ok", f"Нет точек UTC+{utc_offset}")
        return

    # Группируем по тенанту
    by_tenant: dict[int, list[dict]] = {}
    for b in branches:
        tid = b.get("tenant_id", 1)
        by_tenant.setdefault(tid, []).append(b)

    sent = 0
    for tenant_id, tbranches in by_tenant.items():
        try:
            # Собираем статистику по каждой точке
            branches_stats: list[tuple[str, dict, dict | None]] = []
            for b in tbranches:
                name = b["name"]
                cur = await get_period_stats(name, date_from, date_to, tenant_id)
                prev = await get_period_stats(name, prev_from, prev_to, tenant_id)
                branches_stats.append((name, cur or {}, prev))

            # Конверсия клиентов за прошлый полный месяц
            branch_names = [b["name"] for b in tbranches]
            conversion = await get_repeat_conversion(branch_names, tenant_id)

            # ФОТ за неделю
            fot_week = await get_fot_period(branch_names, date_from, date_to, tenant_id)

            # 1. Сводка по сети
            summary_msg = _format_network_summary(label, branches_stats, conversion=conversion, fot=fot_week)

            # 2. Детальные отчёты по точкам (переиспользуем daily_report форматтер)
            detail_msgs: list[str] = []
            for name, cur, prev in branches_stats:
                if not cur:
                    continue
                agg = {
                    "late_delivery_count": cur.get("late_delivery_count") or cur.get("late_count") or 0,
                    "total_delivery_count": cur.get("total_delivered") or 0,
                    "avg_late_min": cur.get("avg_late_min"),
                    "avg_cooking_min": cur.get("avg_cooking_min"),
                    "avg_wait_min": cur.get("avg_wait_min"),
                    "avg_delivery_min": cur.get("avg_delivery_min"),
                    "exact_time_count": cur.get("exact_time_count") or 0,
                    "payment_changed_count": cur.get("payment_changed_count") or 0,
                    "discount_types_agg": cur.get("discount_types"),
                    "new_customers": cur.get("new_customers") or 0,
                    "new_customers_revenue": cur.get("new_customers_revenue") or 0.0,
                    "repeat_customers": cur.get("repeat_customers") or 0,
                    "repeat_customers_revenue": cur.get("repeat_customers_revenue") or 0.0,
                    # штат не показываем в недельном (is_period=True в форматтере)
                }
                # ФОТ за период по этой точке
                if fot_week:
                    # Для одной точки нет детализации по точке в fot_week (это суммарный),
                    # но передаём общие данные — форматтер покажет одну строку % от revenue
                    branch_fot = await get_fot_period([name], date_from, date_to, tenant_id)
                    if branch_fot:
                        agg["_fot"] = branch_fot
                # WoW-строка
                wow_line = ""
                if prev and (prev.get("revenue") or 0) > 0:
                    delta = (cur.get("revenue") or 0) - (prev.get("revenue") or 0)
                    arrow = "▲" if delta >= 0 else "▼"
                    pct = abs(delta / prev["revenue"] * 100)
                    p_chk = prev.get("orders_count") or 0
                    c_chk = cur.get("orders_count") or 0
                    chk_d = c_chk - p_chk
                    chk_arrow = "▲" if chk_d >= 0 else "▼"
                    wow_line = (
                        f"\n📊 WoW: {arrow}{pct:.1f}% выручки | "
                        f"{chk_arrow}{abs(chk_d)} чеков"
                    )

                text = _format_branch_report(
                    name, cur, label, agg, is_period=True
                )
                if wow_line:
                    text += wow_line
                detail_msgs.append(text)

            # Отправляем
            if tenant_id == 1:
                # Ёбидоёби — один общий канал
                await telegram.report(summary_msg)
                for dm in detail_msgs:
                    await telegram.report(dm)
                sent += 1
            else:
                # SaaS-тенант: отправляем в чаты по городам
                # Сводка — во все "reports" чаты тенанта (city=None покрывает всех)
                # Детали — только по точкам соответствующего города
                all_chats_sent: set[int] = set()
                for b in tbranches:
                    city = b.get("city", "")
                    report_chats = await _get_chats("reports", city, tenant_id)
                    for chat_id in report_chats:
                        if chat_id in all_chats_sent:
                            continue
                        all_chats_sent.add(chat_id)
                        await telegram.send_message(str(chat_id), summary_msg)

                        # Детали только по точкам этого города
                        city_branches = {
                            bb["name"] for bb in tbranches if bb.get("city") == city
                        }
                        for n, n_cur, n_prev in branches_stats:
                            if n not in city_branches or not n_cur:
                                continue
                            agg2 = {
                                "late_delivery_count": n_cur.get("late_delivery_count") or n_cur.get("late_count") or 0,
                                "total_delivery_count": n_cur.get("total_delivered") or 0,
                                "avg_late_min": n_cur.get("avg_late_min"),
                                "avg_cooking_min": n_cur.get("avg_cooking_min"),
                                "avg_wait_min": n_cur.get("avg_wait_min"),
                                "avg_delivery_min": n_cur.get("avg_delivery_min"),
                                "exact_time_count": n_cur.get("exact_time_count") or 0,
                                "payment_changed_count": n_cur.get("payment_changed_count") or 0,
                                "discount_types_agg": n_cur.get("discount_types"),
                            }
                            # ФОТ по этой точке за период
                            n_fot = await get_fot_period([n], date_from, date_to, tenant_id)
                            if n_fot:
                                agg2["_fot"] = n_fot
                            wow_line = ""
                            if n_prev and (n_prev.get("revenue") or 0) > 0:
                                delta = (n_cur.get("revenue") or 0) - n_prev["revenue"]
                                arrow = "▲" if delta >= 0 else "▼"
                                pct = abs(delta / n_prev["revenue"] * 100)
                                c_chk = n_cur.get("orders_count") or 0
                                p_chk = n_prev.get("orders_count") or 0
                                ca = "▲" if c_chk >= p_chk else "▼"
                                wow_line = f"\n📊 WoW: {arrow}{pct:.1f}% выручки | {ca}{abs(c_chk - p_chk)} чеков"
                            dm = _format_branch_report(n, n_cur, label, agg2, is_period=True)
                            if wow_line:
                                dm += wow_line
                            await telegram.send_message(str(chat_id), dm)
                        sent += 1

        except Exception as e:
            logger.error(f"weekly_report tenant={tenant_id}: {e}", exc_info=True)

    await log_job_finish(log_id, "ok", f"Отправлено тенантов: {sent}")
