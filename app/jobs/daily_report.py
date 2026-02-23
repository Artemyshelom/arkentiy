"""
Вечерний и утренний ежедневные отчёты по точкам → Telegram.

Вечерний (23:30 местного пн-чт): итоги текущего дня.
Вечерний (00:30 сб/вс лок): итоги пт/сб (days_ago=1).
Утренний (09:30 местного): финальные итоги вчера.
"""

import html
import logging
from datetime import datetime, timedelta, timezone

from app.clients import telegram
from app.clients.iiko_bo_events import get_branch_rt
from app.clients.iiko_bo_olap import get_all_branches_stats
from app.config import get_settings
from app.database import (
    clear_updates_for_date,
    get_daily_stats,
    get_rt_snapshot,
    get_updates_for_date,
    log_job_finish,
    log_job_start,
    save_rt_snapshot,
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
    branch: dict,
    stats: dict,
    date: datetime,
    report_type: str = "вечер",
    rt_data: dict | None = None,
    snapshot: dict | None = None,
) -> str:
    name = branch["name"]
    date_str = date.strftime("%d.%m.%Y")

    revenue = stats.get("revenue_net")
    check_count = stats.get("check_count")
    avg_check = round(revenue / check_count) if revenue and check_count else None
    cogs_pct = stats.get("cogs_pct")
    discount_sum = stats.get("discount_sum")
    discount_types = stats.get("discount_types") or []
    sailplay = stats.get("sailplay")

    icon = "🌙" if report_type == "вечер" else "☀️"
    lines = [f"📊 <b>{html.escape(name)}</b> | {date_str} | {icon} {report_type}", ""]

    if revenue is not None:
        lines.append(f"💰 Выручка: <b>{_fmt_money(revenue)}</b>")
    else:
        lines.append("💰 Выручка: <b>—</b>")

    checks_str = _fmt_num(check_count)
    avg_str = _fmt_money(avg_check) if avg_check else "—"
    lines.append(f"🧾 Чеков: {checks_str} | Средний чек: {avg_str}")
    lines.append("")

    disc_str = _fmt_money(discount_sum) if discount_sum else "—"
    sail_str = _fmt_money(sailplay) if sailplay else "—"
    lines.append(f"💸 Скидки: {disc_str} | SailPlay: {sail_str}")
    for t in discount_types:
        lines.append(f"   └ {t}")
    lines.append("")

    lines.append(f"📦 Себестоимость: {_fmt_pct(cogs_pct)}")
    lines.append("")

    # Опоздания — из живых RT или снапшота
    delays = None
    if rt_data:
        delays = rt_data.get("delays")
    elif snapshot:
        total = snapshot.get("delays_total", 0)
        if total:
            delays = {
                "late_count": snapshot["delays_late"],
                "total_delivered": total,
                "avg_delay_min": snapshot["delays_avg_min"],
            }

    if delays and delays.get("total_delivered", 0) > 0:
        late = delays["late_count"]
        total_d = delays["total_delivered"]
        pct = late / total_d * 100 if total_d else 0
        avg_min = delays["avg_delay_min"]
        if late > 0:
            lines.append(f"🔴 Опозданий: {late} из {total_d} ({pct:.1f}%) | среднее: {avg_min} мин")
        else:
            lines.append(f"✅ Опозданий: 0 из {total_d}")
    else:
        lines.append("⏱ Опозданий: нет данных")

    # Штат за день
    cooks_today = rt_data.get("total_cooks_today") if rt_data else (snapshot.get("cooks_today") if snapshot else None)
    couriers_today = rt_data.get("total_couriers_today") if rt_data else (snapshot.get("couriers_today") if snapshot else None)

    staff_parts = []
    if cooks_today:
        staff_parts.append(f"{cooks_today} поваров")
    if couriers_today:
        staff_parts.append(f"{couriers_today} курьеров")
    if staff_parts:
        lines.append(f"👥 На смене за день: {', '.join(staff_parts)}")

    return "\n".join(lines)



async def job_save_rt_snapshot(utc_offset: int) -> None:
    """
    Сохраняет RT-снапшот пт/сб в 23:50 лок. — только из памяти, без OLAP.
    Читает get_branch_rt() (загружается в память каждые 30с),
    пишет в SQLite. Вечерний отчёт в 00:30 читает оттуда.
    """
    tz = _branch_tz(utc_offset)
    today = datetime.now(tz)
    date_iso = today.strftime("%Y-%m-%d")

    branches = [b for b in settings.branches if b.get("utc_offset", 7) == utc_offset]
    saved = 0
    for branch in branches:
        name = branch["name"]
        rt_data = get_branch_rt(name)
        if not rt_data:
            logger.warning(f"RT-снапшот пт/сб: нет данных [{name}]")
            continue
        delays = rt_data.get("delays", {})
        try:
            await save_rt_snapshot(
                branch=name,
                date=date_iso,
                delays_late=delays.get("late_count", 0),
                delays_total=delays.get("total_delivered", 0),
                delays_avg_min=delays.get("avg_delay_min", 0),
                cooks_today=rt_data.get("total_cooks_today", 0),
                couriers_today=rt_data.get("total_couriers_today", 0),
            )
            saved += 1
        except Exception as e:
            logger.warning(f"Не удалось сохранить RT-снапшот [{name}]: {e}")

    logger.info(f"RT-снапшот пт/сб UTC+{utc_offset}: сохранено {saved}/{len(branches)}")


async def job_send_evening_report(
    utc_offset: int,
    days_ago: int = 0,
    day_label: str = "",
) -> None:
    job_id = f"evening_report_utc{utc_offset}" + (f"_d{days_ago}" if days_ago else "")
    log_id = await log_job_start(job_id)

    tz = _branch_tz(utc_offset)
    target_date = datetime.now(tz) - timedelta(days=days_ago)
    date_iso = target_date.strftime("%Y-%m-%d")

    branches = [b for b in settings.branches if b.get("utc_offset", 7) == utc_offset]
    if not branches:
        await log_job_finish(log_id, "ok", f"Нет точек для UTC+{utc_offset}")
        return

    try:
        all_stats = await get_all_branches_stats(target_date)
    except Exception as e:
        logger.error(f"Ошибка iiko BO в вечернем отчёте: {e}")
        await telegram.error_alert(job_id, str(e))
        await log_job_finish(log_id, "error", str(e))
        return

    sent = 0
    for branch in branches:
        name = branch["name"]
        stats = all_stats.get(name, {})
        rt_data = get_branch_rt(name)
        snapshot = None

        if days_ago == 0 and rt_data:
            # Текущий день: сохраняем снапшот для утреннего отчёта
            delays = rt_data.get("delays", {})
            try:
                await save_rt_snapshot(
                    branch=name,
                    date=date_iso,
                    delays_late=delays.get("late_count", 0),
                    delays_total=delays.get("total_delivered", 0),
                    delays_avg_min=delays.get("avg_delay_min", 0),
                    cooks_today=rt_data.get("total_cooks_today", 0),
                    couriers_today=rt_data.get("total_couriers_today", 0),
                )
            except Exception as e:
                logger.warning(f"Не удалось сохранить RT-снапшот [{name}]: {e}")
        elif days_ago > 0:
            # Прошлый день: берём снапшот из БД
            try:
                snapshot = await get_rt_snapshot(name, date_iso)
            except Exception as e:
                logger.warning(f"Не удалось загрузить RT-снапшот [{name}]: {e}")
            rt_data = None

        # Сохраняем OLAP-данные в daily_stats (чтобы утренний отчёт читал из БД)
        try:
            rev = stats.get("revenue_net") or 0
            chk = stats.get("check_count") or 0
            d_delays = (rt_data.get("delays") or {}) if rt_data else {}
            late_c = d_delays.get("late_count", 0)
            total_d = d_delays.get("total_delivered", 0)
            avg_min = d_delays.get("avg_delay_min", 0)
            cooks = (rt_data.get("total_cooks_today") or 0) if rt_data else 0
            couriers = (rt_data.get("total_couriers_today") or 0) if rt_data else 0
            await upsert_daily_stats_batch([{
                "branch_name":    name,
                "date":           date_iso,
                "orders_count":   chk,
                "revenue":        rev,
                "avg_check":      round(rev / chk) if chk else 0,
                "cogs_pct":       stats.get("cogs_pct"),
                "sailplay":       stats.get("sailplay"),
                "discount_sum":   stats.get("discount_sum"),
                "discount_types": json.dumps(stats.get("discount_types") or [], ensure_ascii=False),
                "delivery_count": chk - (stats.get("pickup_count") or 0),
                "pickup_count":   stats.get("pickup_count") or 0,
                "late_count":     late_c,
                "total_delivered":total_d,
                "late_percent":   round(late_c / total_d * 100, 1) if total_d else 0,
                "avg_late_min":   avg_min,
                "cooks_count":    cooks,
                "couriers_count": couriers,
            }])
        except Exception as e:
            logger.warning(f"Не удалось сохранить daily_stats [{name}]: {e}")

        # Сохраняем OLAP-данные в daily_stats (чтобы утренний отчёт читал из БД)
        try:
            rev = stats.get("revenue_net") or 0
            chk = stats.get("check_count") or 0
            d_delays = (rt_data.get("delays") or {}) if rt_data else {}
            late_c = d_delays.get("late_count", 0)
            total_d = d_delays.get("total_delivered", 0)
            avg_min = d_delays.get("avg_delay_min", 0)
            cooks = (rt_data.get("total_cooks_today") or 0) if rt_data else 0
            couriers = (rt_data.get("total_couriers_today") or 0) if rt_data else 0
            await upsert_daily_stats_batch([{
                "branch_name":    name,
                "date":           date_iso,
                "orders_count":   chk,
                "revenue":        rev,
                "avg_check":      round(rev / chk) if chk else 0,
                "cogs_pct":       stats.get("cogs_pct"),
                "sailplay":       stats.get("sailplay"),
                "discount_sum":   stats.get("discount_sum"),
                "discount_types": json.dumps(stats.get("discount_types") or [], ensure_ascii=False),
                "delivery_count": chk - (stats.get("pickup_count") or 0),
                "pickup_count":   stats.get("pickup_count") or 0,
                "late_count":     late_c,
                "total_delivered":total_d,
                "late_percent":   round(late_c / total_d * 100, 1) if total_d else 0,
                "avg_late_min":   avg_min,
                "cooks_count":    cooks,
                "couriers_count": couriers,
            }])
        except Exception as e:
            logger.warning(f"Не удалось сохранить daily_stats [{name}]: {e}")

        try:
            msg = _format_branch_report(
                branch, stats, target_date,
                report_type="вечер",
                rt_data=rt_data,
                snapshot=snapshot,
            )
            await telegram.report(msg)
            sent += 1
        except Exception as e:
            logger.error(f"Ошибка отправки вечернего отчёта [{name}]: {e}")

    await log_job_finish(log_id, "ok", f"Отправлено: {sent}/{len(branches)}")


async def job_send_morning_report(utc_offset: int) -> None:
    log_id = await log_job_start(f"morning_report_utc{utc_offset}")

    tz = _branch_tz(utc_offset)
    yesterday = datetime.now(tz) - timedelta(days=1)
    date_iso = yesterday.strftime("%Y-%m-%d")

    branches = [b for b in settings.branches if b.get("utc_offset", 7) == utc_offset]
    if not branches:
        await log_job_finish(log_id, "ok", f"Нет точек для UTC+{utc_offset}")
        return

    updates = await get_updates_for_date(date_iso)
    updates_by_branch: dict[str, list[dict]] = {}
    for u in updates:
        updates_by_branch.setdefault(u["branch"], []).append(u)

    # Пробуем загрузить финансовые данные из нашей БД (сохранены вечерним отчётом)
    db_stats: dict[str, dict] = {}
    for branch in branches:
        try:
            row = await get_daily_stats(branch["name"], date_iso)
            if row and row.get("revenue"):
                import json as _json
                disc_types = []
                try:
                    disc_types = _json.loads(row.get("discount_types") or "[]")
                except Exception:
                    pass
                db_stats[branch["name"]] = {
                    "revenue_net":    row["revenue"],
                    "check_count":    row["orders_count"],
                    "cogs_pct":       row.get("cogs_pct"),
                    "sailplay":       row.get("sailplay"),
                    "discount_sum":   row.get("discount_sum"),
                    "discount_types": disc_types,
                    "pickup_count":   row.get("pickup_count"),
                }
        except Exception as e:
            logger.warning(f"Не удалось прочитать daily_stats [{branch['name']}]: {e}")

    # Если БД покрыла все точки — iiko BO не запрашиваем
    if len(db_stats) == len(branches):
        all_stats = db_stats
        logger.info(f"Утренний отчёт UTC+{utc_offset}: данные из local DB (iiko BO не запрашивался)")
    else:
        logger.info(f"Утренний отчёт UTC+{utc_offset}: БД покрыла {len(db_stats)}/{len(branches)}, дозапрашиваем iiko BO")
        try:
            iiko_stats = await get_all_branches_stats(yesterday)
        except Exception as e:
            logger.error(f"Ошибка iiko BO в утреннем отчёте: {e}")
            await telegram.error_alert(f"morning_report_utc{utc_offset}", str(e))
            await log_job_finish(log_id, "error", str(e))
            return
        all_stats = {**iiko_stats, **db_stats}  # db_stats приоритетнее

    sent = 0
    for branch in branches:
        name = branch["name"]
        stats = all_stats.get(name, {})
        branch_updates = updates_by_branch.get(name, [])

        snapshot = None
        try:
            snapshot = await get_rt_snapshot(name, date_iso)
        except Exception as e:
            logger.warning(f"Не удалось загрузить RT-снапшот для утреннего [{name}]: {e}")

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

            msg_body = _format_branch_report(
                branch, stats, yesterday,
                report_type="утро",
                rt_data=None,
                snapshot=snapshot,
            )
            lines.append(msg_body)
            await telegram.report("\n".join(lines))
            sent += 1
        except Exception as e:
            logger.error(f"Ошибка отправки утреннего отчёта [{name}]: {e}")

    try:
        await clear_updates_for_date(date_iso)
    except Exception as e:
        logger.warning(f"Не удалось очистить report_updates за {date_iso}: {e}")

    await log_job_finish(log_id, "ok", f"Отправлено: {sent}/{len(branches)}")
