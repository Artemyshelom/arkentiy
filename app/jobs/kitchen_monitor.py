"""
Модуль «Директор по производству» — кухонный мониторинг.

Три джоба:
  job_kitchen_morning_report(utc_offset) — сводный отчёт кухни за вчера (:30 каждый час по TZ).
  job_kitchen_clock_out_alert()          — алерт ухода повара (15–21 местного, каждые 10 мин).
  job_kitchen_cooking_alert()            — алерт готовки >20 мин (:10 каждый час).

Дизайн-система:
  - Эмодзи только для severity: ✅ 🟡 🔴 ⚡ 🍳
  - Bold на заголовках и именах филиалов — якорь для глаза
  - Severity только на заголовке блока, не на каждой метрике
  - Одно сводное сообщение вместо пачки
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.clients import telegram
from app.database_pg import get_kitchen_monitor_chats
from app.db import (
    get_all_branches,
    get_daily_stats,
    get_fot_daily,
    get_pool,
    log_job_finish,
    log_job_start,
)
from app.utils.job_tracker import track_job
from app.utils.timezone import tz_from_offset, utc_hour_to_local_bounds

logger = logging.getLogger(__name__)

# Anti-spam state для cooking alert: (tenant_id, branch_name) → {"avg": float, "severity": str}
_cooking_state: dict[tuple[int, str], dict[str, Any]] = {}

_SEVERITY_ICON = {"ok": "✅", "warning": "🟡", "critical": "🔴"}


# ─────────────────────────────── plurals ────────────────────────────────────

def _plural_cooks(n: int) -> str:
    if 11 <= n % 100 <= 19:
        return f"{n} поваров"
    r = n % 10
    if r == 1:
        return f"{n} повар"
    elif 2 <= r <= 4:
        return f"{n} повара"
    return f"{n} поваров"


def _plural_points(n: int) -> str:
    if 11 <= n % 100 <= 19:
        return f"{n} точек"
    r = n % 10
    if r == 1:
        return f"{n} точка"
    elif 2 <= r <= 4:
        return f"{n} точки"
    return f"{n} точек"


# ────────────────────────────── helpers ─────────────────────────────────────

def _fmt_money(val: float | None) -> str:
    if not val:
        return "—"
    return f"{int(val):,} ₽".replace(",", "\u00a0")


def _branch_severity(
    fot_pct: float | None,
    cook_min: float | None,
    late_pct: float | None,
) -> str:
    """Возвращает 'ok', 'warning' или 'critical' — максимальный из трёх метрик."""
    has_critical = (
        (fot_pct is not None and fot_pct > 11)
        or (cook_min is not None and cook_min > 25)
        or (late_pct is not None and late_pct > 15)
    )
    has_warning = (
        (fot_pct is not None and fot_pct > 9)
        or (cook_min is not None and cook_min > 20)
        or (late_pct is not None and late_pct > 10)
    )
    if has_critical:
        return "critical"
    if has_warning:
        return "warning"
    return "ok"


def _parse_local_time(ts: str) -> str:
    """Извлекает HH:MM из ISO-строки timestamp (local naive или с TZ-суффиксом)."""
    try:
        clean = ts.replace("T", " ").split(".")[0].split("+")[0].strip()
        return datetime.strptime(clean[:16], "%Y-%m-%d %H:%M").strftime("%H:%M")
    except Exception:
        return ts[:5] if len(ts) >= 5 else "—"


async def _kitchen_chats_for_tenant(tenant_id: int, branches: list[dict]) -> set[int]:
    """Собирает все kitchen_monitor chat_id тенанта без дублей (city=NULL включается для любого города)."""
    all_chat_ids: set[int] = set()
    for branch in branches:
        city = branch.get("city") or ""
        chats = await get_kitchen_monitor_chats(city, tenant_id)
        all_chat_ids.update(chats)
    return all_chat_ids


# ─────────────────── job 1: Morning report ──────────────────────────────────

@track_job("kitchen_morning_report")
async def job_kitchen_morning_report(utc_offset: int) -> None:
    """Сводный утренний отчёт кухни за вчера. Одно сообщение на тенанта."""
    log_id = await log_job_start(f"kitchen_morning_report_utc{utc_offset}", tenant_id=1)

    tz = tz_from_offset(utc_offset)
    yesterday_dt = datetime.now(tz) - timedelta(days=1)
    yesterday = yesterday_dt.strftime("%Y-%m-%d")
    date_label = yesterday_dt.strftime("%d.%m.%Y")

    all_branches = get_all_branches()
    branches_for_tz = [b for b in all_branches if b.get("utc_offset", 7) == utc_offset]
    if not branches_for_tz:
        await log_job_finish(log_id, "ok", f"Нет точек UTC+{utc_offset}")
        return

    # Группируем по тенанту
    by_tenant: dict[int, list[dict]] = {}
    for b in branches_for_tz:
        by_tenant.setdefault(b.get("tenant_id", 1), []).append(b)

    sent_total = 0
    for tenant_id, t_branches in by_tenant.items():
        branch_data: list[dict] = []
        for branch in sorted(t_branches, key=lambda x: x["name"]):
            name = branch["name"]
            stats = await get_daily_stats(name, yesterday, tenant_id) or {}
            if not stats:
                # Точка не работала вчера — пропускаем
                continue

            fot = await get_fot_daily(name, yesterday, tenant_id) or {}
            revenue = float(stats.get("revenue") or 0)
            cook_fot = float(fot.get("cook") or 0)
            fot_pct = round(cook_fot / revenue * 100, 1) if revenue > 0 and cook_fot > 0 else None

            raw_cook = stats.get("avg_cooking_min")
            cook_min = round(float(raw_cook), 1) if raw_cook is not None else None

            late_count = int(stats.get("late_count") or stats.get("late_delivery_count") or 0)
            total_del = int(stats.get("total_delivered") or stats.get("delivery_count") or 0)
            late_pct = round(late_count / total_del * 100, 1) if total_del > 0 else None

            cooks = int(stats.get("cooks_count") or 0)
            sev = _branch_severity(fot_pct, cook_min, late_pct)
            branch_data.append({
                "name": name,
                "fot_pct": fot_pct,
                "cook_min": cook_min,
                "late_pct": late_pct,
                "late_count": late_count,
                "total_del": total_del,
                "cooks": cooks,
                "revenue": revenue,
                "severity": sev,
            })

        if not branch_data:
            continue

        msg = _build_morning_message(branch_data, date_label)
        chat_ids = await _kitchen_chats_for_tenant(tenant_id, t_branches)
        for chat_id in chat_ids:
            try:
                await telegram.send_message(str(chat_id), msg, parse_mode="HTML")
                sent_total += 1
            except Exception as e:
                logger.error(f"kitchen_morning_report: ошибка отправки в {chat_id}: {e}")

    await log_job_finish(log_id, "ok", f"Отправлено в {sent_total} чатов")


def _build_morning_message(branch_data: list[dict], date_label: str) -> str:
    """Строит единое сводное сообщение кухонного отчёта."""
    total = len(branch_data)
    problem = [b for b in branch_data if b["severity"] != "ok"]
    ok_branches = [b for b in branch_data if b["severity"] == "ok"]

    lines: list[str] = [f"<b>🍳 Кухня · {date_label}</b>", ""]

    if not problem:
        lines.append(f"✅ <b>{_plural_points(total)} в норме</b>")
        lines.append("")
        for b in branch_data:
            lines.append(_compact_line(b))
    else:
        any_critical = any(b["severity"] == "critical" for b in problem)
        summary_icon = "🔴" if any_critical else "🟡"
        lines.append(f"{summary_icon} <b>{len(problem)} из {total} точек требуют внимания</b>")
        lines.append("")
        # Сначала критичные, потом предупреждения
        for b in sorted(problem, key=lambda x: 0 if x["severity"] == "critical" else 1):
            lines.extend(_expanded_block(b))
            lines.append("")
        if ok_branches:
            ok_word = _plural_points(len(ok_branches))
            lines.append(f"✅ <b>Остальные {ok_word} в норме</b>")
            for b in ok_branches:
                lines.append(_compact_line(b))

    return "\n".join(lines)


def _compact_line(b: dict) -> str:
    """Одна строка для точки в норме: Название · ФОТ% · готовка мин · N пов."""
    parts = [html.escape(b["name"])]
    if b["fot_pct"] is not None:
        parts.append(f"{b['fot_pct']}%")
    if b["cook_min"] is not None:
        parts.append(f"{b['cook_min']} мин")
    parts.append(f"{b['cooks']} пов.")
    return " · ".join(parts)


def _expanded_block(b: dict) -> list[str]:
    """Развёрнутый блок для проблемного филиала (severity на заголовке, не на метриках)."""
    icon = _SEVERITY_ICON[b["severity"]]
    lines = [f"<b>{icon} {html.escape(b['name'])}</b>"]
    if b["fot_pct"] is not None:
        lines.append(f"ФОТ: {b['fot_pct']}% (цель &lt; 9%)")
    if b["cook_min"] is not None:
        lines.append(f"Готовка: {b['cook_min']} мин")
    if b["late_pct"] is not None and b["total_del"] > 0:
        lines.append(f"Опоздания: {b['late_pct']}% ({b['late_count']} из {b['total_del']})")
    meta_parts = []
    if b["cooks"]:
        meta_parts.append(_plural_cooks(b["cooks"]))
    if b["revenue"]:
        meta_parts.append(_fmt_money(b["revenue"]))
    if meta_parts:
        lines.append(" · ".join(meta_parts))
    return lines


# ─────────────────── job 2: Clock-out alert ─────────────────────────────────

@track_job("kitchen_clock_out")
async def job_kitchen_clock_out_alert() -> None:
    """Алерт ухода повара со смены (15:00–21:00 местного). Каждые 10 мин."""
    pool = get_pool()

    # Очистка старых записей дедупликации
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM kitchen_alerts_sent WHERE sent_at < NOW() - INTERVAL '2 days'"
        )

    all_branches = get_all_branches()
    # Группируем по (tenant_id, utc_offset)
    groups: dict[tuple[int, int], list[dict]] = {}
    for b in all_branches:
        key = (b.get("tenant_id", 1), b.get("utc_offset", 7))
        groups.setdefault(key, []).append(b)

    for (tenant_id, utc_offset), branches in groups.items():
        tz = tz_from_offset(utc_offset)
        now_local = datetime.now(tz).replace(tzinfo=None)

        # Только рабочее окно 15:00–20:59 местного
        if not (15 <= now_local.hour < 21):
            continue

        window_end = now_local
        window_start = now_local - timedelta(minutes=10)

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT branch_name, employee_id, employee_name, clock_out, clock_in
                   FROM shifts_raw
                   WHERE tenant_id = $1
                     AND role_class = 'cook'
                     AND NULLIF(clock_out, '') IS NOT NULL
                     AND NULLIF(clock_out, '')::timestamp >= $2
                     AND NULLIF(clock_out, '')::timestamp < $3
                   ORDER BY branch_name, clock_out""",
                tenant_id, window_start, window_end,
            )

        if not rows:
            continue

        # Фильтруем уже отправленные (persistent dedup) и группируем по филиалу
        by_branch: dict[str, list[dict]] = {}
        async with pool.acquire() as conn:
            for row in rows:
                already = await conn.fetchval(
                    """SELECT 1 FROM kitchen_alerts_sent
                       WHERE tenant_id=$1 AND branch_name=$2 AND employee_id=$3 AND clock_out=$4""",
                    tenant_id, row["branch_name"], row["employee_id"], row["clock_out"],
                )
                if not already:
                    by_branch.setdefault(row["branch_name"], []).append(dict(row))

        for branch_name, cooks in by_branch.items():
            branch_info = next((b for b in branches if b["name"] == branch_name), None)
            if not branch_info:
                continue
            city = branch_info.get("city") or ""
            chats = await get_kitchen_monitor_chats(city, tenant_id)
            if not chats:
                continue

            msg = _build_clock_out_message(branch_name, cooks)

            # INSERT dedup ПЕРЕД отправкой (идемпотентно, ON CONFLICT DO NOTHING)
            async with pool.acquire() as conn:
                for c in cooks:
                    try:
                        await conn.execute(
                            """INSERT INTO kitchen_alerts_sent
                               (tenant_id, branch_name, employee_id, clock_out)
                               VALUES ($1, $2, $3, $4)
                               ON CONFLICT DO NOTHING""",
                            tenant_id, branch_name, c["employee_id"], c["clock_out"],
                        )
                    except Exception as e:
                        logger.warning(f"kitchen_clock_out: dedup insert error [{branch_name}]: {e}")

            for chat_id in chats:
                try:
                    await telegram.send_message(str(chat_id), msg, parse_mode="HTML")
                except Exception as e:
                    logger.error(f"kitchen_clock_out: ошибка отправки в {chat_id}: {e}")


def _build_clock_out_message(branch_name: str, cooks: list[dict]) -> str:
    """Строит сообщение об уходе поваров. Батчит несколько в одно."""
    count = len(cooks)
    icon = "🔴" if count >= 3 else "🟡"
    lines = [f"{icon} <b>{html.escape(branch_name)}</b>"]

    if count == 1:
        c = cooks[0]
        lines.append(f"{html.escape(c['employee_name'])} ушёл в {_parse_local_time(c['clock_out'])}")
    else:
        n_word = "2 повара" if count == 2 else _plural_cooks(count)
        lines.append(f"За 10 мин ушли {n_word}")
        lines.append("")
        for c in cooks:
            lines.append(f"• {html.escape(c['employee_name'])} — {_parse_local_time(c['clock_out'])}")

    if count >= 3:
        lines.append("")
        lines.append("⚡ Проверь укомплектованность смены")

    return "\n".join(lines)


# ─────────────────── job 3: Cooking alert ───────────────────────────────────

@track_job("kitchen_cooking_alert")
async def job_kitchen_cooking_alert() -> None:
    """Алерт готовки >20 мин за прошлый час. CronTrigger(minute=10)."""
    pool = get_pool()
    now_utc = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    hour_start_utc = now_utc - timedelta(hours=1)

    all_branches = get_all_branches()
    groups: dict[tuple[int, int], list[dict]] = {}
    for b in all_branches:
        key = (b.get("tenant_id", 1), b.get("utc_offset", 7))
        groups.setdefault(key, []).append(b)

    for (tenant_id, utc_offset), branches in groups.items():
        tz = tz_from_offset(utc_offset)
        # Только рабочие часы 10:00–22:59 местного
        if not (10 <= datetime.now(tz).hour < 23):
            continue

        # Naive local bounds для WHERE по TEXT-timestamps
        hs, he = utc_hour_to_local_bounds(hour_start_utc, tz)
        hour_label = hs.strftime("%H:%M")
        hour_label_end = he.strftime("%H:%M")

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT
                     branch_name,
                     ROUND(AVG(
                       EXTRACT(EPOCH FROM (
                         NULLIF(cooked_time, '')::timestamp
                         - NULLIF(service_print_time, '')::timestamp
                       )) / 60
                     )::numeric, 1) AS avg_cook_min,
                     COUNT(*) AS orders_count,
                     (SELECT COUNT(*)
                      FROM shifts_raw s
                      WHERE s.tenant_id = $1
                        AND s.branch_name = orders_raw.branch_name
                        AND s.role_class = 'cook'
                        AND NULLIF(s.clock_in, '') IS NOT NULL
                        AND NULLIF(s.clock_in, '')::timestamp < $3
                        AND (
                          NULLIF(s.clock_out, '') IS NULL
                          OR NULLIF(s.clock_out, '')::timestamp > $2
                        )
                     ) AS cooks_count
                   FROM orders_raw
                   WHERE tenant_id = $1
                     AND NULLIF(service_print_time, '') IS NOT NULL
                     AND NULLIF(cooked_time, '') IS NOT NULL
                     AND status IN ('Доставлена', 'Закрыта')
                     AND EXTRACT(EPOCH FROM (
                           NULLIF(cooked_time, '')::timestamp
                           - NULLIF(service_print_time, '')::timestamp
                         )) / 60 BETWEEN 1 AND 120
                     AND NULLIF(service_print_time, '')::timestamp >= $2
                     AND NULLIF(service_print_time, '')::timestamp < $3
                   GROUP BY branch_name
                   HAVING COUNT(*) >= 5""",
                tenant_id, hs, he,
            )

        results: dict[str, dict] = {r["branch_name"]: dict(r) for r in rows}

        # ── Resolve detection: были в проблемном состоянии, теперь ок ────────
        for bn, prev in list(_cooking_state.items()):
            if bn[0] != tenant_id:
                continue
            branch_name = bn[1]
            if prev.get("severity") in (None, "ok"):
                continue
            r = results.get(branch_name)
            avg = float(r["avg_cook_min"]) if r else None
            # Resolved если есть данные с нормальным avg ИЛИ вообще нет данных в час
            if avg is None or avg <= 20:
                _cooking_state[bn] = {"avg": avg or 0.0, "severity": "ok"}
                if avg is not None:
                    # Есть данные — отправляем resolve
                    branch_info = next((b for b in branches if b["name"] == branch_name), None)
                    if branch_info:
                        city = branch_info.get("city") or ""
                        chats = await get_kitchen_monitor_chats(city, tenant_id)
                        resolve_msg = (
                            f"<b>✅ {html.escape(branch_name)}</b>\n"
                            f"Готовка нормализовалась\n"
                            f"{avg} мин за {hour_label}–{hour_label_end}"
                        )
                        for chat_id in chats:
                            try:
                                await telegram.send_message(
                                    str(chat_id), resolve_msg, parse_mode="HTML"
                                )
                            except Exception as e:
                                logger.error(
                                    f"kitchen_cooking_alert resolve [{branch_name}]: {e}"
                                )

        # ── Сборка новых алертов ─────────────────────────────────────────────
        alert_items: list[dict] = []
        for branch_name, r in sorted(results.items()):
            avg = float(r["avg_cook_min"])
            sev = "critical" if avg > 25 else ("warning" if avg > 20 else "ok")

            if sev == "ok":
                _cooking_state[(tenant_id, branch_name)] = {"avg": avg, "severity": "ok"}
                continue

            prev = _cooking_state.get((tenant_id, branch_name))
            should_send = (
                prev is None
                or prev.get("severity") in (None, "ok")
                or (sev == "critical" and prev.get("severity") == "warning")
                or (avg - prev.get("avg", 0.0)) > 2.0
            )
            _cooking_state[(tenant_id, branch_name)] = {"avg": avg, "severity": sev}

            if should_send:
                alert_items.append({
                    "name": branch_name,
                    "avg": avg,
                    "orders": int(r["orders_count"]),
                    "cooks": int(r["cooks_count"]),
                    "severity": sev,
                })

        if not alert_items:
            continue

        chat_ids = await _kitchen_chats_for_tenant(tenant_id, branches)
        if not chat_ids:
            continue

        msg = _build_cooking_message(alert_items, hour_label, hour_label_end)
        for chat_id in chat_ids:
            try:
                await telegram.send_message(str(chat_id), msg, parse_mode="HTML")
            except Exception as e:
                logger.error(f"kitchen_cooking_alert: ошибка отправки в {chat_id}: {e}")


def _build_cooking_message(
    items: list[dict], hour_label: str, hour_label_end: str
) -> str:
    """Строит сообщение для одного или нескольких проблемных филиалов."""
    if len(items) == 1:
        b = items[0]
        icon = _SEVERITY_ICON[b["severity"]]
        lines = [
            f"<b>{icon} {html.escape(b['name'])}</b> · {hour_label}–{hour_label_end}",
            f"Готовка: {b['avg']} мин (норма &lt; 20)",
            f"{b['orders']} зак. · {_plural_cooks(b['cooks'])}",
        ]
        if b["severity"] == "critical":
            lines.append("⚡ Возможна нехватка поваров")
        return "\n".join(lines)

    # Батч
    any_critical = any(b["severity"] == "critical" for b in items)
    header_icon = "🔴" if any_critical else "🟡"
    lines = [f"<b>{header_icon} Готовка выше нормы · {hour_label}–{hour_label_end}</b>", ""]
    critical_names: list[str] = []
    for b in sorted(items, key=lambda x: 0 if x["severity"] == "critical" else 1):
        icon = _SEVERITY_ICON[b["severity"]]
        lines.append(f"<b>{icon} {html.escape(b['name'])}</b>")
        lines.append(f"{b['avg']} мин · {b['orders']} зак. · {_plural_cooks(b['cooks'])}")
        lines.append("")
        if b["severity"] == "critical":
            critical_names.append(html.escape(b["name"]))
    if critical_names:
        for name in critical_names:
            lines.append(f"⚡ {name} — возможна нехватка поваров")
    return "\n".join(lines).rstrip()
