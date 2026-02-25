"""
Аркентий — Диспетчер (@arkentybot, токен: TELEGRAM_ANALYTICS_BOT_TOKEN).
Единственный активный бот проекта. Арсений (telegram_commands.py) отключён.

Команды реального времени (из in-memory BranchState):
  /статус [фильтр]   — текущий статус точек
  /повара [фильтр]   — повара на смене
  /курьеры [фильтр]  — курьеры со статистикой
  /помощь            — справка

Команды из БД (SQLite orders_raw / shifts_raw / daily_stats):
  /поиск <номер>     — найти заказ по номеру доставки
  /отчёт [филиал] [период] — отчёт за день/неделю/месяц/диапазон
  /точные [филиал] [дата]  — заказы на точное время
  /опоздания [фильтр] [дата] — активные или исторические опоздания

Примечание: файл называется arkentiy.py.
Будущий analytics_bot.py будет отдельным модулем для глубокой аналитики.
"""

import html
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite
import httpx

from app.jobs import bank_statement
from app.jobs import tbank_reconciliation

from app import access
from app.clients.iiko_bo_events import (
    get_all_branches_staff,
    _states,
    _parse_customer_name,
    _parse_customer_phone,
)
from app.config import get_settings
from app.database import DB_PATH, aggregate_orders_for_daily_stats, get_client_order_count, get_daily_stats, get_exact_time_orders, get_period_stats, log_silence
from app.jobs import access_manager
from app.jobs.daily_report import _format_branch_report, _fmt_money
from app.jobs.iiko_status_report import (
    format_branch_status,
    get_available_branches,
    get_branch_status,
)
from app.jobs.late_alerts import (
    ACTIVE_DELIVERY_STATUSES,
    LOCAL_UTC_OFFSET as _LATE_UTC_OFFSET,
    set_silence as _set_silence,
    is_silenced as _is_silenced,
    get_silence_until as _get_silence_until,
)

logger = logging.getLogger(__name__)
settings = get_settings()

_last_update_id: Optional[int] = None

# Маппинг команды → модуль для проверки прав
_CMD_MODULE: dict[str, str] = {
    "статус": "reports", "status": "reports",
    "повара": "reports", "cooks": "reports",
    "курьеры": "reports", "couriers": "reports",
    "поиск": "search", "search": "search",
    "отчёт": "reports", "отчет": "reports", "report": "reports",
    "точные": "reports", "exact": "reports",
    "опоздания": "late_queries", "late": "late_queries",
    "самовывоз": "late_queries", "pickup": "late_queries",
    "тишина": "late_alerts", "mute": "late_alerts",
    "выгрузка": "marketing", "export": "marketing",
    "аудит": "audit", "audit": "audit",
    "конкуренты": "admin", "competitors": "admin",
    "доступ": "admin", "access": "admin",
}


# Kyrgyz режим для Ильи
_ILYA_ID = 822559806

_KYRGYZ_GREETINGS = [
    "Салам алейкум, Илья! 🌟",
    "Саламатсызбы, Илья мырза! 🙏",
    "Салам, кандайсың? 😄",
    "Жакшысызбы, Илья мырза! Кабарлар жакшыбы? ✨",
    "Кош келдиңиз, Илья мырза! 🎉",
    "О, патрон келди! Бардыгы даяр, мырза! 😄",
    "Ассалому алайкум, Илья баатыр! Эмне буюрасыз? ⚔️",
    "Бүгүн кандай? Ишке даярмын, Илья мырза! 💪",
    "Куттуу болсун бүгүнкү күн, Илья мырза! 🔥",
]

_KYRGYZ_ACKS = [
    "Макул, мырза, дароо эле кылам! ⚡",
    "Ооба, Илья мырза, азыр эле жасап жатам. 🔥",
    "Буйруңуз, мырза! Дароо орундатам! 🫡",
    "Дароо эле, Илья мырза! Бир секунд... ⏱",
    "Сиздин буйрук — менин иш, мырза! 💪",
    "Эмир кылдыңыз — аткарам, Илья мырза! 🎯",
    "Жарайт, баш мырза, азыр аткарам! 🚀",
]

_greeted_today: dict[int, str] = {}


def _check_and_mark_ilya_greeting() -> bool:
    today = datetime.now().date().isoformat()
    if _greeted_today.get(_ILYA_ID) != today:
        _greeted_today[_ILYA_ID] = today
        return True
    return False


# ------------------------------------------------------------------
# Telegram helpers (используют ANALYTICS токен)
# ------------------------------------------------------------------

def _bot_url() -> str:
    return f"https://api.telegram.org/bot{settings.telegram_analytics_bot_token}"


async def _send(chat_id: int, text: str, parse_mode: str = "HTML") -> None:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{_bot_url()}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            )
            if not r.json().get("ok"):
                logger.error(f"analytics_bot send error: {r.text[:200]}")
    except Exception as e:
        logger.error(f"analytics_bot _send: {e}")


async def _send_with_keyboard(chat_id: int, text: str, keyboard: list) -> None:
    """Отправляет сообщение с inline-клавиатурой."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{_bot_url()}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": keyboard},
                },
            )
            if not r.json().get("ok"):
                logger.error(f"analytics_bot keyboard send error: {r.text[:200]}")
    except Exception as e:
        logger.error(f"analytics_bot _send_with_keyboard: {e}")


async def _download_tg_file(file_id: str) -> bytes | None:
    """Скачивает файл из Telegram по file_id."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{_bot_url()}/getFile", params={"file_id": file_id})
            data = r.json()
            if not data.get("ok"):
                logger.error(f"getFile error: {r.text[:200]}")
                return None
            file_path = data["result"]["file_path"]
            token = settings.telegram_analytics_bot_token
            dl = await client.get(f"https://api.telegram.org/file/bot{token}/{file_path}")
            return dl.content
    except Exception as e:
        logger.error(f"_download_tg_file: {e}")
        return None


async def _send_document(chat_id: int, filename: str, data: bytes, caption: str = "") -> bool:
    """Отправляет файл в Telegram-чат."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            payload = {"chat_id": str(chat_id)}
            if caption:
                payload["caption"] = caption[:1024]
                payload["parse_mode"] = "HTML"
            r = await client.post(
                f"{_bot_url()}/sendDocument",
                data=payload,
                files={"document": (filename, data, "application/octet-stream")},
            )
            if not r.json().get("ok"):
                logger.error(f"sendDocument error: {r.text[:200]}")
                return False
            return True
    except Exception as e:
        logger.error(f"_send_document: {e}")
        return False


async def _handle_bank_statement(chat_id: int, tg_doc: dict, user_id: int = 0) -> None:
    """Обработка банковской выписки: скачать, распарсить, разбить, отправить."""
    file_id = tg_doc.get("file_id", "")
    file_name = tg_doc.get("file_name", "")
    await _send(chat_id, f"📥 Обрабатываю выписку <b>{html.escape(file_name)}</b>...")

    raw = await _download_tg_file(file_id)
    if not raw:
        await _send(chat_id, "❌ Не удалось скачать файл")
        return

    for enc in ("windows-1251", "utf-8", "cp866"):
        try:
            content = raw.decode(enc)
            break
        except (UnicodeDecodeError, ValueError):
            continue
    else:
        await _send(chat_id, "❌ Не удалось определить кодировку файла")
        return

    if not bank_statement.is_1c_statement(content):
        return

    try:
        result = bank_statement.process_statement(content)
    except Exception as e:
        logger.error(f"bank_statement processing: {e}", exc_info=True)
        await _send(chat_id, f"❌ Ошибка обработки: {html.escape(str(e))}")
        return

    await _send(chat_id, result["summary"])

    for filename, file_bytes in result["files"].items():
        ok = await _send_document(chat_id, filename, file_bytes)
        if not ok:
            await _send(chat_id, f"❌ Не удалось отправить {filename}")

    count = len(result["files"])
    await _send(chat_id, f"✅ Готово: {count} файлов отправлено")

    # Сверка эквайринга с iiko (отдельно — если iiko недоступен, файлы уже отправлены)
    if result["acquiring"]:
        try:
            accounts_map = bank_statement.load_accounts_map()
            reconcile_text = await bank_statement.reconcile_acquiring(
                result["acquiring"], accounts_map,
                result["parsed"].date_from, result["parsed"].date_to,
            )
            if reconcile_text:
                await _send(chat_id, reconcile_text)
        except Exception as e:
            logger.error(f"[bank_statement] reconcile: {e}", exc_info=True)
            await _send(chat_id, f"⚠️ Сверка с iiko не удалась: {html.escape(str(e))}")

    # Логирование в БД
    try:
        from app.database import save_bank_statement_log
        await save_bank_statement_log(
            user_id=user_id,
            chat_id=chat_id,
            filename=file_name,
            date_from=result["parsed"].date_from,
            date_to=result["parsed"].date_to,
            total_docs=len(result["parsed"].documents),
            total_files=count,
        )
    except Exception as e:
        logger.warning(f"[bank_statement] db log: {e}")


async def _handle_tbank_registry(chat_id: int, tg_doc: dict, user_id: int = 0) -> None:
    """Обработка реестра ТБанк: скачать, проверить формат, сверить с iiko."""
    file_id = tg_doc.get("file_id", "")
    file_name = tg_doc.get("file_name", "")

    raw = await _download_tg_file(file_id)
    if not raw:
        await _send(chat_id, "❌ Не удалось скачать файл")
        return

    if not tbank_reconciliation.is_tbank_registry(raw):
        return

    await _send(chat_id, f"📥 Обрабатываю реестр ТБанк <b>{html.escape(file_name)}</b>...")

    try:
        result = await tbank_reconciliation.process_registry(
            data=raw,
            user_id=user_id,
            chat_id=chat_id,
            filename=file_name,
        )
    except Exception as e:
        logger.error(f"tbank_reconciliation: {e}", exc_info=True)
        await _send(chat_id, f"❌ Ошибка обработки: {html.escape(str(e))}")
        return

    if "error" in result:
        await _send(chat_id, f"❌ {html.escape(result['error'])}")
        return

    report = result.get("report", "")
    if report:
        await _send(chat_id, report)

    await _send(chat_id, "✅ Сверка онлайн-оплат завершена")


async def _answer_callback(callback_id: str, text: str = "") -> None:
    """Подтверждаем нажатие кнопки (убирает loader в Telegram)."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"{_bot_url()}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text},
            )
    except Exception:
        pass


async def _get_updates(offset: Optional[int] = None) -> list[dict]:
    params = {"timeout": 2, "limit": 10}
    if offset is not None:
        params["offset"] = offset
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{_bot_url()}/getUpdates", params=params)
            r.raise_for_status()
            data = r.json()
            return data.get("result", []) if data.get("ok") else []
    except Exception as e:
        logger.debug(f"analytics_bot getUpdates: {e}")
        return []




# ------------------------------------------------------------------
# Formatting helpers
# ------------------------------------------------------------------

def _fmt_time(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        return iso[11:16]
    except Exception:
        return "?"


def _fmt_dt(iso: str | None) -> str:
    """Форматирует ISO в 'ДД.ММ ЧЧ:ММ'."""
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso[:19])
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return iso[:16]


def _parse_date_arg(arg: str) -> str:
    """
    Парсит аргумент даты → 'YYYY-MM-DD'.
    Поддерживает: '' (сегодня), 'вчера', 'YYYY-MM-DD', 'DD.MM.YYYY', 'DD.MM'
    """
    arg = arg.strip().lower()
    today = datetime.now(timezone(timedelta(hours=7))).date()  # UTC+7 (Барнаул)

    if not arg or arg in ("сегодня", "today"):
        return today.isoformat()
    if arg in ("вчера", "yesterday"):
        return (today - timedelta(days=1)).isoformat()

    # YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", arg):
        return arg

    # DD.MM.YYYY
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", arg)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"

    # DD.MM  (текущий год)
    m = re.match(r"^(\d{2})\.(\d{2})$", arg)
    if m:
        return f"{today.year}-{m.group(2)}-{m.group(1)}"

    return today.isoformat()


def _format_staff_block(branch_name: str, staff: list[dict], role: str) -> str:
    active = [s for s in staff if s["is_active"]]
    left = [s for s in staff if not s["is_active"]]
    icon = "👨‍🍳" if role == "cook" else "🛵"
    label = "поваров" if role == "cook" else "курьеров"

    lines = [f"📍 <b>{html.escape(branch_name)}</b> — {label}: {len(active)}"]
    for s in active:
        line = f"  🟢 {html.escape(s['name'])} (с {_fmt_time(s['opened_at'])})"
        if role == "courier":
            line += f" — доставил: {s.get('delivered', 0)}, сейчас: {s.get('active_orders', 0)}"
        lines.append(line)
    for s in left:
        line = (
            f"  ⚫ {html.escape(s['name'])} "
            f"({_fmt_time(s['opened_at'])}–{_fmt_time(s['closed_at'])})"
        )
        if role == "courier":
            line += f" — доставил: {s.get('delivered', 0)}"
        lines.append(line)
    return "\n".join(lines)


# ------------------------------------------------------------------
# RT handlers (статус, повара, курьеры) — читают из BranchState
# ------------------------------------------------------------------

async def _handle_status(chat_id: int, arg: str, city_filter: str | None = None) -> None:
    all_branches = get_available_branches()
    if not all_branches:
        await _send(chat_id, "⚠️ Нет настроенных точек.")
        return

    effective_arg = arg or city_filter or ""
    filtered = get_available_branches(effective_arg) if effective_arg else all_branches
    if effective_arg and not filtered:
        display = "/".join(sorted(effective_arg)) if isinstance(effective_arg, frozenset) else effective_arg
        names = "\n".join(f"• {b['name']}" for b in all_branches)
        await _send(chat_id, f"❌ «{display}» не найдено.\n\nДоступные точки:\n{names}")
        return

    if len(filtered) > 1:
        await _send(chat_id, f"🔍 Собираю данные по {len(filtered)} точкам...")

    for branch in filtered:
        try:
            data = await get_branch_status(branch)
            await _send(chat_id, format_branch_status(data))
        except Exception as e:
            logger.error(f"[analytics_bot] статус [{branch['name']}]: {e}")
            await _send(chat_id, f"⚠️ Ошибка по точке {branch['name']}")


async def _handle_staff(chat_id: int, arg: str, role: str, city_filter: str | None = None) -> None:
    all_branches = get_available_branches()
    effective_arg = arg or city_filter or ""
    filtered = get_available_branches(effective_arg) if effective_arg else all_branches
    if effective_arg and not filtered:
        display = "/".join(sorted(effective_arg)) if isinstance(effective_arg, frozenset) else effective_arg
        await _send(chat_id, f"❌ Точка «{display}» не найдена.")
        return

    all_staff = get_all_branches_staff(role)
    if not all_staff:
        await _send(chat_id, "⏳ Данные ещё загружаются, попробуй через 30 секунд.")
        return

    for branch in filtered:
        name = branch["name"]
        staff = all_staff.get(name)
        if staff is None:
            await _send(chat_id, f"📍 <b>{html.escape(name)}</b> — данные недоступны")
        else:
            await _send(chat_id, _format_staff_block(name, staff, role))


# ------------------------------------------------------------------
# DB handlers — читают из SQLite
# ------------------------------------------------------------------

async def _format_order_card(r: dict, client_count: int | None = None) -> str:
    """Полная карточка заказа."""
    is_cancelled = (r.get("status") or "").lower() in ("отменена", "отменён")

    if is_cancelled:
        late_str = "—"
    elif r["is_late"]:
        late_str = f"🔴 опоздал {r['late_minutes']:.0f} мин"
    elif r["actual_time"]:
        late_str = "✅ вовремя"
    else:
        late_str = "⏳ ещё не доставлен"
        if r.get("planned_time"):
            try:
                from datetime import timezone, timedelta
                LOCAL_TZ = timezone(timedelta(hours=7))
                planned_dt = datetime.strptime(r["planned_time"].replace("T", " ").split(".")[0], "%Y-%m-%d %H:%M:%S")
                now_local = datetime.now(tz=timezone.utc).astimezone(LOCAL_TZ).replace(tzinfo=None)
                if now_local > planned_dt:
                    overdue_min = int((now_local - planned_dt).total_seconds() / 60)
                    late_str = f"⏳ ещё не доставлен | ⚠️ опаздывает {overdue_min} м"
            except Exception:
                pass

    s = r["sum"]
    sum_str = f"{int(float(s)):,} ₽".replace(",", " ") if s else "—"
    client = html.escape(r.get("client_name") or "—")
    phone = html.escape(r.get("client_phone") or "—")

    addr_raw = r.get("delivery_address")
    if addr_raw:
        addr = html.escape(addr_raw)
    elif r.get("is_self_service"):
        addr = "🏪 Самовывоз"
    else:
        addr = "адрес не указан"

    items_raw = (r.get("items") or "").replace("\xa0", " ").strip()
    if items_raw:
        try:
            import json as _json
            items_data = _json.loads(items_raw)
            parts = []
            for item in items_data:
                name = item.get("name", "").strip()
                qty = item.get("qty", 1)
                if name:
                    parts.append(f"{name} × {qty}" if qty and qty != 1 else name)
            items_str = "\n".join(f"   └ {html.escape(p)}" for p in parts) or "   └ —"
        except Exception:
            items_parts = [p.strip() for p in items_raw.split(";") if p.strip()]
            items_str = "\n".join(f"   └ {html.escape(p)}" for p in items_parts)
    else:
        items_str = "   └ —"

    if client_count == 1:
        client_tag = " 🆕 Новый"
    elif client_count is not None and client_count > 1:
        client_tag = f" 🔄 Повторный ({client_count} зак.)"
    else:
        client_tag = ""

    if is_cancelled:
        cancel_reason = r.get("cancel_reason") or ""
        reason_part = f" ({html.escape(cancel_reason)})" if cancel_reason else ""
        status_line = f"   ❌ <b>Статус: ОТМЕНЕНА</b>{reason_part}"
    else:
        stale_note = ""
        if r.get("status") in ("Новая", "Не подтверждена", "Ждет отправки") and r.get("planned_time"):
            try:
                from datetime import timezone, timedelta as _td
                clean_pt = r["planned_time"].replace("T", " ").split(".")[0]
                planned_dt = datetime.strptime(clean_pt, "%Y-%m-%d %H:%M:%S")
                now_local = (datetime.now(tz=timezone.utc) + _td(hours=7)).replace(tzinfo=None)
                if (now_local - planned_dt).total_seconds() > 3600:
                    stale_note = "\n   ⚠️ <i>статус может быть устарел — iiko не прислала обновление</i>"
            except Exception:
                pass
        status_line = f"   Статус: {html.escape(r['status'] or '?')}{stale_note}"

    return (
        f"📍 <b>{html.escape(r['branch_name'])}</b> | #{r['delivery_num']}\n"
        f"{status_line}\n"
        f"   👤 {client}{client_tag} | 📞 <code>{phone}</code>\n"
        f"   🗺 {addr}\n"
        f"   🛵 Курьер: {html.escape(r['courier'] or '—')}\n"
        f"   💰 {sum_str}\n"
        f"   ⏱ {_fmt_dt(r['planned_time'])} → {_fmt_dt(r['actual_time'])} | {late_str}\n"
        f"🍱 Состав:\n{items_str}"
    )


def _format_order_compact(r: dict) -> str:
    """Компактная строка для больших выборок."""
    s = r["sum"]
    sum_str = f"{int(float(s)):,} ₽".replace(",", " ") if s else "—"
    client = html.escape(r.get("client_name") or "?")
    if r["is_late"]:
        late_icon = "🔴"
    elif r["actual_time"]:
        late_icon = "✅"
    else:
        late_icon = "⏳"
    return (
        f"  #{r['delivery_num']} {html.escape(r['branch_name'])}\n"
        f"  👤 {client} | 💰 {sum_str} | {late_icon} {_fmt_dt(r['planned_time'])}"
    )


async def _handle_search(chat_id: int, query: str, city_filter: str | None = None) -> None:
    """
    /поиск <запрос> — универсальный поиск по orders_raw.
    Если запрос — только цифры: сначала точное совпадение по номеру заказа (полная карточка),
    остальные совпадения (телефон, адрес, состав) — компактный список отдельно.
    Иначе — обычный поиск по всем полям.
    """
    if not query:
        await _send(
            chat_id,
            "❓ Укажи что искать:\n\n"
            "<code>/поиск 119458</code> — по номеру заказа\n"
            "<code>/поиск +79831...</code> — по телефону клиента\n"
            "<code>/поиск Пролетарская</code> — по адресу\n"
            "<code>/поиск Филадельфия</code> — все заказы с этим блюдом\n"
            "<code>/поиск 119458 томск</code> — поиск с фильтром по городу"
        )
        return

    # Парсинг ручного фильтра города/филиала (последний токен)
    tokens = query.strip().split()
    manual_filter: str | None = None
    if len(tokens) >= 2:
        last = tokens[-1]
        maybe_branches = get_available_branches(last)
        if maybe_branches:
            manual_filter = last
            tokens = tokens[:-1]
            query = " ".join(tokens)

    is_numeric = query.isdigit()
    q = f"%{query}%"
    COLS = """branch_name, delivery_num, status, courier, sum,
              planned_time, actual_time, is_late, late_minutes,
              client_name, client_phone, delivery_address, items, is_self_service, comment"""

    # Права чата
    allowed_set: set[str] = set()
    if city_filter:
        allowed_set = {b["name"] for b in get_available_branches(city_filter)}

    # Ручной фильтр пересекается с правами чата
    city_branch_names: list[str] = []
    if manual_filter:
        manual_branches = get_available_branches(manual_filter)
        manual_names = [b["name"] for b in manual_branches]
        if allowed_set:
            city_branch_names = [n for n in manual_names if n in allowed_set]
            if not city_branch_names:
                await _send(chat_id, f"❌ Нет доступа к «{manual_filter}»")
                return
        else:
            city_branch_names = manual_names
    elif city_filter:
        city_branch_names = list(allowed_set)

    def _city_clause(alias: str = "") -> tuple[str, list]:
        """Возвращает (sql_fragment, params) для фильтра по городу."""
        prefix = f"{alias}." if alias else ""
        if city_branch_names:
            placeholders = ",".join("?" * len(city_branch_names))
            return f"AND {prefix}branch_name IN ({placeholders})", list(city_branch_names)
        return "", []

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        city_sql, city_params = _city_clause()

        if is_numeric:
            async with db.execute(
                f"SELECT {COLS} FROM orders_raw WHERE delivery_num = ? {city_sql} ORDER BY planned_time DESC",
                (query, *city_params),
            ) as cur:
                exact_rows = [dict(r) for r in await cur.fetchall()]

            async with db.execute(
                f"""SELECT COUNT(*) FROM orders_raw
                    WHERE delivery_num != ?
                      AND (client_phone LIKE ? OR delivery_address LIKE ? OR items LIKE ?)
                      {city_sql}""",
                (query, q, q, q, *city_params),
            ) as cur:
                other_total = (await cur.fetchone())[0]

            async with db.execute(
                f"""SELECT {COLS} FROM orders_raw
                    WHERE delivery_num != ?
                      AND (client_phone LIKE ? OR delivery_address LIKE ? OR items LIKE ?)
                      {city_sql}
                    ORDER BY planned_time DESC LIMIT 10""",
                (query, q, q, q, *city_params),
            ) as cur:
                other_rows = [dict(r) for r in await cur.fetchall()]
        else:
            exact_rows = []
            other_total = 0
            other_rows = []
            async with db.execute(
                f"""SELECT COUNT(*) FROM orders_raw
                    WHERE (delivery_num LIKE ? OR client_phone LIKE ?
                       OR delivery_address LIKE ? OR items LIKE ?) {city_sql}""",
                (q, q, q, q, *city_params),
            ) as cur:
                other_total = (await cur.fetchone())[0]
            async with db.execute(
                f"""SELECT {COLS} FROM orders_raw
                    WHERE (delivery_num LIKE ? OR client_phone LIKE ?
                       OR delivery_address LIKE ? OR items LIKE ?) {city_sql}
                    ORDER BY planned_time DESC LIMIT 20""",
                (q, q, q, q, *city_params),
            ) as cur:
                other_rows = [dict(r) for r in await cur.fetchall()]

    if not exact_rows and not other_rows:
        await _send(
            chat_id,
            f"🔍 <code>{html.escape(query)}</code> — ничего не найдено."
        )
        return

    # Показываем точные совпадения как полные карточки
    if exact_rows:
        header = f"🔍 <b>Заказ #{html.escape(query)}</b> — найдено: {len(exact_rows)}"
        await _send(chat_id, header)
        for r in exact_rows:
            phone_for_count = (r.get("client_phone") or "").strip()
            cnt = await get_client_order_count(phone_for_count) if phone_for_count else None
            await _send(chat_id, await _format_order_card(r, client_count=cnt))

    # Показываем остальные совпадения компактно
    if other_rows:
        if exact_rows:
            more_str = f" (показаны 10 из {other_total})" if other_total > 10 else ""
            other_header = f"\n🔎 <i>Ещё найдено {other_total} совпадений в других полях{more_str}:</i>"
        else:
            more_str = f" (показаны 20 из {other_total})" if other_total > 20 else ""
            other_header = f"🔍 <b>{html.escape(query)}</b> — найдено: {other_total}{more_str}"

        out_lines = [other_header, ""]
        keyboard = []
        for r in other_rows:
            out_lines.append(_format_order_compact(r))
            num = r["delivery_num"]
            branch_short = r["branch_name"].split("_")[0]
            keyboard.append([{"text": f"📋 Открыть #{num} ({branch_short})", "callback_data": f"order:{num}"}])
        if other_total > (10 if exact_rows else 20):
            out_lines.append("\n<i>Уточни запрос чтобы сузить выборку.</i>")
        await _send_with_keyboard(chat_id, "\n".join(out_lines), keyboard)



_MONTH_NAMES = {
    "январь": 1, "янв": 1, "февраль": 2, "фев": 2, "март": 3, "мар": 3,
    "апрель": 4, "апр": 4, "май": 5, "июнь": 6, "июн": 6,
    "июль": 7, "июл": 7, "август": 8, "авг": 8, "сентябрь": 9, "сен": 9,
    "октябрь": 10, "окт": 10, "ноябрь": 11, "ноя": 11, "декабрь": 12, "дек": 12,
}

_PERIOD_PATTERNS = [
    # single dates
    r"^\d{4}-\d{2}-\d{2}$",
    r"^\d{2}\.\d{2}\.\d{4}$",
    r"^\d{2}\.\d{2}$",
    r"^(вчера|сегодня|today|yesterday)$",
    # ranges
    r"^\d{2}\.\d{2}-\d{2}\.\d{2}$",
    r"^\d{2}\.\d{2}\.\d{4}-\d{2}\.\d{2}\.\d{4}$",
    # relative
    r"^\d+д$",
    r"^(неделя|week|эта_неделя|this_week)$",
    r"^(месяц|month)$",
]


def _parse_period(tokens: list[str]) -> tuple[str, str, str, list[str]]:
    """Разбирает токены на (date_from, date_to, display_label, filter_tokens).

    Поддерживает: один день, диапазон DD.MM-DD.MM, Nд, неделя, месяц, название месяца.
    """
    import calendar
    today = datetime.now(timezone(timedelta(hours=7))).date()
    period_token = ""
    filter_tokens = []

    all_period = list(_PERIOD_PATTERNS) + [
        r"^(" + "|".join(_MONTH_NAMES.keys()) + r")$",
    ]

    for tok in tokens:
        low = tok.lower()
        if not period_token and any(re.match(p, low) for p in all_period):
            period_token = low
        else:
            filter_tokens.append(tok)

    if not period_token:
        period_token = "вчера"

    # --- Single date ---
    if period_token in ("вчера", "yesterday"):
        d = today - timedelta(days=1)
        return d.isoformat(), d.isoformat(), d.strftime("%d.%m.%Y"), filter_tokens
    if period_token in ("сегодня", "today"):
        return today.isoformat(), today.isoformat(), today.strftime("%d.%m.%Y"), filter_tokens
    if re.match(r"^\d{4}-\d{2}-\d{2}$", period_token):
        return period_token, period_token, datetime.fromisoformat(period_token).strftime("%d.%m.%Y"), filter_tokens
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", period_token)
    if m:
        iso = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        return iso, iso, period_token, filter_tokens
    m = re.match(r"^(\d{2})\.(\d{2})$", period_token)
    if m:
        iso = f"{today.year}-{m.group(2)}-{m.group(1)}"
        return iso, iso, period_token, filter_tokens

    # --- Range DD.MM-DD.MM ---
    m = re.match(r"^(\d{2})\.(\d{2})-(\d{2})\.(\d{2})$", period_token)
    if m:
        f = f"{today.year}-{m.group(2)}-{m.group(1)}"
        t = f"{today.year}-{m.group(4)}-{m.group(3)}"
        label = f"{m.group(1)}.{m.group(2)} – {m.group(3)}.{m.group(4)}.{today.year}"
        return f, t, label, filter_tokens

    # --- Range DD.MM.YYYY-DD.MM.YYYY ---
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})-(\d{2})\.(\d{2})\.(\d{4})$", period_token)
    if m:
        f = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        t = f"{m.group(6)}-{m.group(5)}-{m.group(4)}"
        label = f"{m.group(1)}.{m.group(2)}.{m.group(3)} – {m.group(4)}.{m.group(5)}.{m.group(6)}"
        return f, t, label, filter_tokens

    # --- Relative: Nд ---
    m = re.match(r"^(\d+)д$", period_token)
    if m:
        n = int(m.group(1))
        d_from = today - timedelta(days=n)
        d_to = today - timedelta(days=1)
        label = f"{d_from.strftime('%d.%m')} – {d_to.strftime('%d.%m.%Y')} ({n}д)"
        return d_from.isoformat(), d_to.isoformat(), label, filter_tokens

    # --- неделя / месяц ---
    if period_token in ("неделя", "week"):
        last_monday = today - timedelta(days=today.weekday() + 7)
        last_sunday = last_monday + timedelta(days=6)
        label = f"{last_monday.strftime('%d.%m')} – {last_sunday.strftime('%d.%m.%Y')} (неделя)"
        return last_monday.isoformat(), last_sunday.isoformat(), label, filter_tokens
    if period_token in ("эта_неделя", "эта неделя", "this_week"):
        this_monday = today - timedelta(days=today.weekday())
        d_to = today - timedelta(days=1)
        if d_to < this_monday:
            d_to = this_monday
        label = f"{this_monday.strftime('%d.%m')} – {d_to.strftime('%d.%m.%Y')} (эта неделя)"
        return this_monday.isoformat(), d_to.isoformat(), label, filter_tokens
    if period_token in ("месяц", "month"):
        d_from = today - timedelta(days=30)
        d_to = today - timedelta(days=1)
        label = f"{d_from.strftime('%d.%m')} – {d_to.strftime('%d.%m.%Y')} (месяц)"
        return d_from.isoformat(), d_to.isoformat(), label, filter_tokens

    # --- Название месяца ---
    month_num = _MONTH_NAMES.get(period_token)
    if month_num:
        year = today.year if month_num <= today.month else today.year - 1
        last_day = calendar.monthrange(year, month_num)[1]
        d_from = f"{year}-{month_num:02d}-01"
        d_to = f"{year}-{month_num:02d}-{last_day:02d}"
        label = f"{period_token.capitalize()} {year}"
        return d_from, d_to, label, filter_tokens

    # fallback — вчера
    d = today - timedelta(days=1)
    return d.isoformat(), d.isoformat(), d.strftime("%d.%m.%Y"), filter_tokens


async def _build_branch_report(
    name: str, date_from: str, date_to: str, label: str, is_single_day: bool,
) -> str | None:
    """Собирает отчёт для одной точки. Возвращает текст или None."""
    import json as _json

    if is_single_day:
        ds = await get_daily_stats(name, date_from)
    else:
        ds = await get_period_stats(name, date_from, date_to)

    if not ds:
        return None

    if is_single_day:
        agg = await aggregate_orders_for_daily_stats(name, date_from)
    else:
        discount_types_raw = ds.get("discount_types") or "[]"
        try:
            dt_parsed = _json.loads(discount_types_raw)
        except (TypeError, _json.JSONDecodeError):
            dt_parsed = []

        agg = {
            "late_delivery_count": ds.get("late_delivery_count") or ds.get("late_count") or 0,
            "total_delivery_count": ds.get("total_delivered") or 0,
            "avg_late_min": ds.get("avg_late_min") or 0,
            "avg_cooking_min": ds.get("avg_cooking_min"),
            "avg_wait_min": ds.get("avg_wait_min"),
            "avg_delivery_min": ds.get("avg_delivery_min"),
            "discount_types_agg": dt_parsed,
            "cooks_today": ds.get("cooks_count") or 0,
            "couriers_today": ds.get("couriers_count") or 0,
            "exact_time_count": ds.get("exact_time_count") or 0,
        }

    return _format_branch_report(name, ds, label, agg, is_period=not is_single_day)


async def _build_city_aggregate(
    branches: list[dict], date_from: str, date_to: str, label: str, is_single_day: bool,
) -> str | None:
    """Агрегирует данные по всем филиалам города в один отчёт."""
    import json as _json

    totals: dict = {}
    weighted_keys = ("avg_cooking_min", "avg_wait_min", "avg_delivery_min", "avg_late_min")
    sum_keys = (
        "revenue", "orders_count", "discount_sum", "sailplay",
        "late_delivery_count", "total_delivered", "exact_time_count",
    )
    count = 0
    all_dt: dict[str, dict] = {}

    for branch in branches:
        name = branch["name"]
        if is_single_day:
            ds = await get_daily_stats(name, date_from)
            agg = await aggregate_orders_for_daily_stats(name, date_from) if ds else {}
        else:
            ds = await get_period_stats(name, date_from, date_to)
            agg = {}

        if not ds:
            continue
        count += 1

        for k in sum_keys:
            val = ds.get(k) or agg.get(k) or 0
            totals[k] = totals.get(k, 0) + val

        for k in weighted_keys:
            val = agg.get(k) if is_single_day else ds.get(k)
            if val is not None:
                totals.setdefault(f"_{k}_sum", 0)
                totals[f"_{k}_sum"] += val
                totals.setdefault(f"_{k}_cnt", 0)
                totals[f"_{k}_cnt"] += 1

        cogs_pct = ds.get("cogs_pct")
        rev = ds.get("revenue") or ds.get("revenue_net") or 0
        if cogs_pct is not None and rev:
            totals.setdefault("_cogs_w", 0)
            totals["_cogs_w"] += cogs_pct * rev
            totals.setdefault("_cogs_rev", 0)
            totals["_cogs_rev"] += rev

        dt_src = agg.get("discount_types_agg") if is_single_day else ds.get("discount_types")
        if isinstance(dt_src, str):
            try:
                dt_src = _json.loads(dt_src)
            except (TypeError, _json.JSONDecodeError):
                dt_src = []
        if dt_src and isinstance(dt_src, list):
            for dt in dt_src:
                if isinstance(dt, dict):
                    t = dt.get("type", "?")
                    all_dt.setdefault(t, {"type": t, "count": 0, "sum": 0})
                    all_dt[t]["count"] += dt.get("count", 0)
                    all_dt[t]["sum"] += dt.get("sum", 0)

    if count == 0:
        return None

    for k in weighted_keys:
        s = totals.pop(f"_{k}_sum", None)
        c = totals.pop(f"_{k}_cnt", None)
        if s is not None and c:
            totals[k] = round(s / c, 1)

    cogs_w = totals.pop("_cogs_w", None)
    cogs_rev = totals.pop("_cogs_rev", None)
    totals["cogs_pct"] = round(cogs_w / cogs_rev, 2) if cogs_w and cogs_rev else None

    rev = totals.get("revenue") or totals.get("orders_count", 0)
    chk = totals.get("orders_count") or 0
    totals["avg_check"] = round(rev / chk) if chk else 0
    totals["check_count"] = chk

    city_name = branches[0].get("city", "Город")
    agg_out = {
        "late_delivery_count": totals.get("late_delivery_count", 0),
        "total_delivery_count": totals.get("total_delivered", 0),
        "avg_late_min": totals.get("avg_late_min", 0),
        "avg_cooking_min": totals.get("avg_cooking_min"),
        "avg_wait_min": totals.get("avg_wait_min"),
        "avg_delivery_min": totals.get("avg_delivery_min"),
        "discount_types_agg": sorted(all_dt.values(), key=lambda x: x["sum"], reverse=True),
        "cooks_today": 0,
        "couriers_today": 0,
        "exact_time_count": totals.get("exact_time_count", 0),
    }

    return _format_branch_report(
        f"{city_name} (все точки)", totals, label, agg_out, is_period=not is_single_day,
    )


async def _handle_day(chat_id: int, arg: str, city_filter=None) -> None:
    """
    /отчёт [филиал] [период] — отчёт за день/неделю/месяц/диапазон из daily_stats.
    При запросе по городу (>1 филиала) — сначала агрегат, потом inline-кнопки.
    """
    tokens = arg.strip().split() if arg.strip() else []
    date_from, date_to, label, filter_tokens = _parse_period(tokens)
    filter_q = " ".join(filter_tokens).strip()
    is_single_day = (date_from == date_to)

    if not filter_q and city_filter:
        filter_q = city_filter

    branches_cfg = get_available_branches(filter_q) if filter_q else get_available_branches()
    if filter_q and not branches_cfg:
        await _send(chat_id, f"❌ Точка или город «{filter_q}» не найдены.")
        return

    is_city_query = len(branches_cfg) > 1 and filter_q

    if is_city_query:
        agg_msg = await _build_city_aggregate(branches_cfg, date_from, date_to, label, is_single_day)
        if agg_msg:
            buttons = []
            row: list[dict] = []
            for b in branches_cfg:
                short = b["name"].split("_")[-1] if "_" in b["name"] else b["name"]
                cb = f"rpt:{b['name']}:{date_from}:{date_to}"
                row.append({"text": short, "callback_data": cb})
                if len(row) >= 3:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
            await _send_with_keyboard(chat_id, agg_msg, buttons)
        else:
            await _send(chat_id, f"📭 <b>{label}</b> — нет данных за этот период.")
        return

    sent = 0
    for branch in branches_cfg:
        msg = await _build_branch_report(branch["name"], date_from, date_to, label, is_single_day)
        if msg:
            await _send(chat_id, msg)
            sent += 1

    if sent == 0:
        await _send(chat_id, f"📭 <b>{label}</b> — нет данных за этот период.")


async def _handle_exact_orders(chat_id: int, arg: str, city_filter: str | None = None) -> None:
    """/точные [филиал] [дата] — заказы на точное время → CSV-файл."""
    import csv
    import io

    tokens = arg.strip().split() if arg.strip() else []
    date_from, _, _, filter_tokens = _parse_period(tokens)
    filter_q = " ".join(filter_tokens).strip()

    if not filter_q and city_filter:
        filter_q = city_filter

    branch_names: list[str] = []
    branch_name: str | None = None
    if filter_q:
        branches_cfg = get_available_branches(filter_q)
        if not branches_cfg:
            await _send(chat_id, f"❌ Точка или город «{filter_q}» не найдены.")
            return
        branch_names = [b["name"] for b in branches_cfg]
        if len(branches_cfg) == 1:
            branch_name = branches_cfg[0]["name"]

    orders = await get_exact_time_orders(branch_name, date_from, branch_names or None)

    if not orders:
        await _send(chat_id, f"📌 <b>Точных заказов нет</b> за {date_from}")
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Номер", "Филиал", "Сумма", "Тип", "Комментарий",
        "Открыт", "План", "На кухне", "Отправлен", "Доставлен",
    ])
    for o in orders:
        writer.writerow([
            o["delivery_num"],
            o["branch_name"] or "",
            int(o["sum"] or 0),
            "Самовывоз" if o.get("is_self_service") else "Доставка",
            (o.get("comment") or "").replace("\n", " "),
            (o.get("opened_at") or "")[:19].replace("T", " "),
            (o.get("planned_time") or "")[:19].replace("T", " "),
            (o.get("cooked_time") or "")[:19].replace("T", " "),
            (o.get("send_time") or "")[:19].replace("T", " "),
            (o.get("actual_time") or "")[:19].replace("T", " "),
        ])

    csv_bytes = buf.getvalue().encode("utf-8-sig")
    filename = f"exact_orders_{date_from}.csv"
    caption = f"📌 <b>Заказы на точное время</b> | {date_from} | {len(orders)} шт."

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{_bot_url()}/sendDocument",
                data={"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"},
                files={"document": (filename, csv_bytes, "text/csv; charset=utf-8")},
            )
            if not r.json().get("ok"):
                logger.error(f"exact_orders sendDocument: {r.text[:200]}")
                await _send(chat_id, f"❌ Ошибка отправки файла")
    except Exception as e:
        logger.error(f"exact_orders sendDocument: {e}")
        await _send(chat_id, f"❌ Ошибка: {e}")


async def _handle_day_delays(chat_id: int, arg: str, city_filter=None) -> None:
    """
    Исторические опоздания за день из БД (группировка по точкам).
    Вызывается из /опоздания [дата].
    """
    import re
    from collections import defaultdict

    DATE_PATTERNS = [
        r"^\d{4}-\d{2}-\d{2}$",
        r"^\d{2}\.\d{2}\.\d{4}$",
        r"^\d{2}\.\d{2}$",
        r"^(вчера|сегодня|today|yesterday)$",
    ]
    tokens = arg.strip().split() if arg.strip() else []
    date_token = ""
    filter_tokens = []
    for tok in tokens:
        if not date_token and any(re.match(p, tok.lower()) for p in DATE_PATTERNS):
            date_token = tok
        else:
            filter_tokens.append(tok)

    date = _parse_date_arg(date_token)
    filter_q = " ".join(filter_tokens).strip()

    # Если пользователь не указал фильтр точки — подставляем city_filter из прав чата
    if not filter_q and city_filter:
        filter_q = city_filter

    date_display = datetime.fromisoformat(date).strftime("%d.%m.%Y")

    branches_cfg = get_available_branches(filter_q) if filter_q else get_available_branches()
    branch_names = [b["name"] for b in branches_cfg]

    if filter_q and not branch_names:
        await _send(chat_id, f"❌ Точка или город «{filter_q}» не найдены.")
        return

    placeholders = ",".join("?" * len(branch_names))
    params_filter = tuple(branch_names)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Статистика: доставки и самовывозы по каждой точке
        async with db.execute(
            f"""SELECT branch_name,
                       SUM(CASE WHEN is_self_service=0 THEN 1 ELSE 0 END) AS delivery_cnt,
                       SUM(CASE WHEN is_self_service=1 THEN 1 ELSE 0 END) AS pickup_cnt
               FROM orders_raw
               WHERE date = ?
                 AND status IN ('Доставлена','Закрыта')
                 AND branch_name IN ({placeholders})
               GROUP BY branch_name""",
            (date,) + params_filter,
        ) as cur:
            stats_rows = {r["branch_name"]: dict(r) for r in await cur.fetchall()}

        # Опоздавшие доставки
        async with db.execute(
            f"""SELECT branch_name, delivery_num, courier, client_name,
                       planned_time, late_minutes, sum
               FROM orders_raw
               WHERE date = ? AND is_late = 1 AND is_self_service = 0
                 AND branch_name IN ({placeholders})
               ORDER BY branch_name, late_minutes DESC""",
            (date,) + params_filter,
        ) as cur:
            delivery_late = [dict(r) for r in await cur.fetchall()]

        # Опоздавшие самовывозы
        async with db.execute(
            f"""SELECT branch_name, delivery_num, client_name,
                       planned_time, actual_time, late_minutes, sum, ready_time
               FROM orders_raw
               WHERE date = ? AND is_late = 1 AND is_self_service = 1
                 AND branch_name IN ({placeholders})
               ORDER BY branch_name, late_minutes DESC""",
            (date,) + params_filter,
        ) as cur:
            pickup_late = [dict(r) for r in await cur.fetchall()]

    total_late_d = len(delivery_late)
    total_late_p = len(pickup_late)
    total_late = total_late_d + total_late_p
    filter_label = f" · {html.escape(filter_q)}" if filter_q else ""

    total_del = sum((s.get("delivery_cnt") or 0) for s in stats_rows.values())
    total_pickup = sum((s.get("pickup_cnt") or 0) for s in stats_rows.values())

    if total_late == 0:
        if total_del + total_pickup == 0:
            await _send(
                chat_id,
                f"📭 <b>{date_display}{filter_label}</b> — нет данных за эту дату."
            )
        else:
            await _send(
                chat_id,
                f"✅ <b>{date_display}{filter_label}</b> — опозданий нет!\n"
                f"Доставлено: {total_del} | Самовывоз: {total_pickup}"
            )
        return

    # Заголовок — одно сообщение с итогом
    header_parts = [f"🔴 <b>{date_display}{filter_label} — опоздания</b>"]
    d_pct = total_late_d / total_del * 100 if total_del else 0
    p_pct = total_late_p / total_pickup * 100 if total_pickup else 0
    header_parts.append(f"🚚 Доставка: {total_late_d}/{total_del} ({d_pct:.0f}%)")
    if total_pickup > 0:
        header_parts.append(f"🏪 Самовывоз: {total_late_p}/{total_pickup} ({p_pct:.0f}%)")
    await _send(chat_id, "\n".join(header_parts))

    # Группируем по точкам
    by_branch_delivery: dict = defaultdict(list)
    for r in delivery_late:
        by_branch_delivery[r["branch_name"]].append(r)

    by_branch_pickup: dict = defaultdict(list)
    for r in pickup_late:
        by_branch_pickup[r["branch_name"]].append(r)

    def _fmt_sum(s) -> str:
        return f"{int(float(s)):,} ₽".replace(",", "\u00a0") if s else "—"

    for branch_name in branch_names:
        stat = stats_rows.get(branch_name, {})
        d_total = stat.get("delivery_cnt") or 0
        p_total = stat.get("pickup_cnt") or 0
        d_orders = by_branch_delivery.get(branch_name, [])
        p_orders = by_branch_pickup.get(branch_name, [])

        # Пропускаем точку если нет ни опозданий ни вообще доставок
        if d_total == 0 and p_total == 0 and not d_orders and not p_orders:
            continue

        lines_out = []

        # Блок доставок
        if d_total > 0 or d_orders:
            d_late_cnt = len(d_orders)
            d_pct_b = d_late_cnt / d_total * 100 if d_total else 0
            d_avg = sum(r["late_minutes"] for r in d_orders) / d_late_cnt if d_orders else 0
            d_icon = "🔴" if d_pct_b > 20 else "🟡" if d_pct_b > 10 else "🟠" if d_pct_b > 0 else "✅"
            lines_out.append(
                f"{d_icon} <b>{html.escape(branch_name)}</b> 🚚 {d_late_cnt}/{d_total} ({d_pct_b:.0f}%)"
                + (f" | ср. +{d_avg:.0f} мин" if d_orders else "")
            )
            for r in d_orders:
                courier = html.escape(r.get("courier") or "—")
                lines_out.append(
                    f"   +{r['late_minutes']:.0f} м | #{r['delivery_num']} | 🛵 {courier} | план: {_fmt_dt(r['planned_time'])} | {_fmt_sum(r['sum'])}"
                )

        # Блок самовывозов — ТОЛЬКО если есть опоздания
        if p_orders:
            p_late_cnt = len(p_orders)
            p_pct_b = p_late_cnt / p_total * 100 if p_total else 0
            p_avg = sum(r["late_minutes"] for r in p_orders) / p_late_cnt
            lines_out.append(
                f"\n🏪 Самовывоз: {p_late_cnt}/{p_total} ({p_pct_b:.0f}%) | ср. +{p_avg:.0f} мин"
            )
            for r in p_orders:
                ready_str = ""
                for time_field in ("ready_time", "actual_time"):
                    raw = r.get(time_field)
                    if raw:
                        try:
                            dt = datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S") if "T" in raw else datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
                            if time_field == "ready_time":
                                dt = dt + timedelta(minutes=5)
                            ready_str = f" → готов: {dt.strftime('%H:%M')}"
                            break
                        except Exception:
                            pass
                lines_out.append(
                    f"   +{r['late_minutes']:.0f} м | #{r['delivery_num']} | план: {_fmt_dt(r['planned_time'])}{ready_str} | {_fmt_sum(r['sum'])}"
                )

        if lines_out:
            await _send(chat_id, "\n".join(lines_out))


# ------------------------------------------------------------------
# Real-time опоздания из in-memory _states
# ------------------------------------------------------------------

def _human_status_rt(status: str, cooking_status: str | None) -> str:
    """Человекочитаемый статус для реалтайм-запросов."""
    if status == "В пути к клиенту":
        return "в пути"
    if status in ("Новая", "Не подтверждена", "Ждет отправки"):
        if cooking_status == "Собран":
            return "приготовлен, ждёт"
        if cooking_status == "Приготовлено":
            return "готовится"
        return "ожидает кухни"
    if status == "В процессе приготовления":
        return "готовится"
    return status or "—"


async def _handle_late(chat_id: int, arg: str, city_filter=None) -> None:
    """/опоздания [фильтр] [дата] — без даты: активные прямо сейчас, с датой: из БД."""
    DATE_PATTERNS = [
        r"^\d{4}-\d{2}-\d{2}$",
        r"^\d{2}\.\d{2}\.\d{4}$",
        r"^\d{2}\.\d{2}$",
        r"^(вчера|yesterday)$",
    ]
    tokens = arg.strip().split() if arg.strip() else []
    has_date = any(
        any(re.match(p, tok.lower()) for p in DATE_PATTERNS)
        for tok in tokens
    )
    if has_date:
        await _handle_day_delays(chat_id, arg, city_filter=city_filter)
        return

    now_local = (
        datetime.now(tz=timezone.utc) + timedelta(hours=_LATE_UTC_OFFSET)
    ).replace(tzinfo=None)

    filter_q = arg.strip() or None
    if filter_q is None and city_filter is not None:
        filter_q = city_filter

    branch_names_set = {b["name"] for b in (
        get_available_branches(filter_q) if filter_q else get_available_branches()
    )}

    results = []
    for branch_name, state in _states.items():
        if branch_name not in branch_names_set:
            continue
        for num, d in list(state.deliveries.items()):
            if d.get("is_self_service"):
                continue
            if d.get("status") not in ACTIVE_DELIVERY_STATUSES:
                continue
            planned_raw = d.get("planned_time")
            if not planned_raw:
                continue
            try:
                clean = planned_raw.replace("T", " ").split(".")[0]
                planned_dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            overdue_min = (now_local - planned_dt).total_seconds() / 60
            if overdue_min <= 0:
                continue
            results.append({
                "branch": branch_name,
                "num": num,
                "overdue_min": overdue_min,
                "planned_dt": planned_dt,
                "status": d.get("status", ""),
                "cooking": state._cooking_status(str(num)),
                "courier": (d.get("courier") or "").strip(),
                "customer_raw": d.get("customer_raw"),
            })

    results.sort(key=lambda x: -x["overdue_min"])

    filter_label = ""
    if isinstance(filter_q, str):
        filter_label = f" · {html.escape(filter_q)}"

    if not results:
        await _send(chat_id, f"✅ Активных опозданий нет{filter_label}")
        return

    lines = [f"🚨 <b>Активные опоздания{filter_label}</b> — {len(results)} зак.\n"]
    for r in results:
        name = html.escape(_parse_customer_name(r["customer_raw"]) or "—")
        phone = html.escape(_parse_customer_phone(r["customer_raw"]) or "—")
        status_str = _human_status_rt(r["status"], r["cooking"])
        courier_part = ""
        if r["status"] == "В пути к клиенту" and r["courier"]:
            courier_part = f"\n  🛵 {html.escape(r['courier'])}"
        lines.append(
            f"<b>+{int(r['overdue_min'])} мин</b> | #{r['num']}"
            f" | {html.escape(r['branch'])}\n"
            f"  👤 {name} | 📞 <code>{phone}</code>\n"
            f"  🕐 план: {r['planned_dt'].strftime('%H:%M')} | {status_str}"
            + courier_part
        )

    await _send(chat_id, "\n\n".join(lines))


async def _handle_pickup(chat_id: int, arg: str, city_filter=None) -> None:
    """/самовывоз [фильтр] — активные опоздавшие самовывозы прямо сейчас."""
    now_local = (
        datetime.now(tz=timezone.utc) + timedelta(hours=_LATE_UTC_OFFSET)
    ).replace(tzinfo=None)

    filter_q = arg.strip() or None
    if filter_q is None and city_filter is not None:
        filter_q = city_filter

    branch_names_set = {b["name"] for b in (
        get_available_branches(filter_q) if filter_q else get_available_branches()
    )}

    # Самовывоз считается опоздавшим если сейчас > planned_time (заказ ещё не выдан)
    ACTIVE_PICKUP_STATUSES = frozenset({
        "Новая", "Не подтверждена", "Ждет отправки",
        "В процессе приготовления",
    })

    results = []
    for branch_name, state in _states.items():
        if branch_name not in branch_names_set:
            continue
        for num, d in list(state.deliveries.items()):
            if not d.get("is_self_service"):
                continue
            if d.get("status") not in ACTIVE_PICKUP_STATUSES:
                continue
            planned_raw = d.get("planned_time")
            if not planned_raw:
                continue
            try:
                clean = planned_raw.replace("T", " ").split(".")[0]
                planned_dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            overdue_min = (now_local - planned_dt).total_seconds() / 60
            if overdue_min <= 0:
                continue
            results.append({
                "branch": branch_name,
                "num": num,
                "overdue_min": overdue_min,
                "planned_dt": planned_dt,
                "status": d.get("status", ""),
                "cooking": state._cooking_status(str(num)),
                "customer_raw": d.get("customer_raw"),
            })

    results.sort(key=lambda x: -x["overdue_min"])

    filter_label = ""
    if isinstance(filter_q, str):
        filter_label = f" · {html.escape(filter_q)}"

    if not results:
        await _send(chat_id, f"✅ Активных опозданий самовывоза нет{filter_label}")
        return

    lines = [f"🏪 <b>Самовывоз{filter_label}</b> — {len(results)} опоздавших\n"]
    for r in results:
        name = html.escape(_parse_customer_name(r["customer_raw"]) or "—")
        phone = html.escape(_parse_customer_phone(r["customer_raw"]) or "—")
        status_str = _human_status_rt(r["status"], r["cooking"])
        lines.append(
            f"<b>+{int(r['overdue_min'])} мин</b> | #{r['num']}"
            f" | {html.escape(r['branch'])}\n"
            f"  👤 {name} | 📞 <code>{phone}</code>\n"
            f"  🕐 план: {r['planned_dt'].strftime('%H:%M')} | {status_str}"
        )

    await _send(chat_id, "\n\n".join(lines))


def _parse_mute_duration(s: str) -> int | None:
    """Парсим строку длительности в минуты (макс 120). None если не распознано."""
    s = s.strip().lower()
    # "2ч", "2 часа", "1 час", "2h", "2hour"
    m = re.match(r"^(\d+\.?\d*)\s*(?:ч|час(?:а|ов)?|h(?:our)?s?)$", s)
    if m:
        return min(120, int(float(m.group(1)) * 60))
    # "30м", "30 мин", "45 минут", "30min"
    m = re.match(r"^(\d+)\s*(?:м|мин(?:ут(?:а|ы)?)?|min(?:ute)?s?)$", s)
    if m:
        return min(120, int(m.group(1)))
    # Просто число = минуты
    m = re.match(r"^(\d+)$", s)
    if m:
        return min(120, int(m.group(1)))
    return None


async def _handle_mute(chat_id: int, arg: str, user_id: int) -> None:
    """/тишина [длительность] — выключить алерты в этом чате (макс 2 ч)."""
    now_local = (
        datetime.now(tz=timezone.utc) + timedelta(hours=_LATE_UTC_OFFSET)
    ).replace(tzinfo=None)

    if not arg.strip():
        # Показать текущий статус
        until = _get_silence_until(chat_id)
        if until:
            remaining = int((until - now_local).total_seconds() / 60)
            await _send(chat_id, f"🔕 Тишина активна ещё ~{remaining} мин (до {until.strftime('%H:%M')})")
        else:
            await _send(chat_id, "🔔 Алерты включены. Напиши /тишина 30 чтобы выключить на 30 мин.")
        return

    minutes = _parse_mute_duration(arg.strip())
    if minutes is None:
        await _send(
            chat_id,
            "❌ Не понял формат. Примеры: <code>/тишина 30</code>, "
            "<code>/тишина 1ч</code>, <code>/тишина 2 часа</code>"
        )
        return

    until_dt = now_local + timedelta(minutes=minutes)
    _set_silence(chat_id, until_dt)
    try:
        await log_silence(chat_id, minutes, user_id)
    except Exception as e:
        logger.warning(f"[/тишина] log_silence error: {e}")

    h = minutes // 60
    m = minutes % 60
    dur_str = (f"{h} ч " if h else "") + (f"{m} мин" if m else "")
    await _send(
        chat_id,
        f"🔕 Режим тишины включён на {dur_str.strip()} (до {until_dt.strftime('%H:%M')}).\n"
        f"Алерты об опозданиях в этом чате не будут отправляться."
    )


# ------------------------------------------------------------------
# Help text
# ------------------------------------------------------------------

def _build_help(perms) -> str:
    """Собирает справку только из модулей, доступных этому чату."""
    lines = ["<b>Аналитический бот — команды:</b>", ""]

    if perms.has("reports"):
        lines += [
            "<b>📊 Отчёты и статус:</b>",
            "/статус [фильтр] — состояние всех точек прямо сейчас",
            "/повара [фильтр] — повара на сегодняшней смене",
            "/курьеры [фильтр] — курьеры со статистикой",
            "/отчёт [филиал] [период] — отчёт за день/неделю/месяц/диапазон",
            "/точные [филиал] [дата] — заказы на точное время (предзаказы)",
            "",
        ]

    if perms.has("late_queries"):
        lines += [
            "<b>🚚 Опоздания:</b>",
            "/опоздания [фильтр] — активные опоздавшие доставки прямо сейчас",
            "/опоздания [фильтр] [дата] — опоздания из БД за дату",
            "/самовывоз [фильтр] — активные опоздавшие самовывозы",
            "",
        ]

    if perms.has("late_alerts"):
        lines += [
            "<b>🔕 Алерты:</b>",
            "/тишина [длительность] — выключить алерты в этом чате (макс 2 ч)",
            "/тишина — показать статус тишины",
            "",
        ]

    if perms.has("search"):
        lines += [
            "<b>🔍 Поиск:</b>",
            "/поиск <i>запрос</i> — по номеру, телефону, адресу или блюду",
            "/поиск <i>запрос</i> <i>город</i> — с фильтром по городу",
            "",
        ]

    if perms.has("audit"):
        lines += [
            "<b>🔎 Аудит:</b>",
            "/аудит [город] [дата] — подозрительные операции за день",
            "",
        ]

    if perms.has("marketing"):
        lines += [
            "<b>📈 Выгрузка:</b>",
            "/выгрузка <i>запрос</i> — выгрузка базы клиентов в CSV",
            "",
        ]

    if perms.has("admin"):
        lines += [
            "<b>🛠 Администратор:</b>",
            "/конкуренты — обновить таблицы конкурентов",
            "",
        ]

    has_dates = perms.has("reports") or perms.has("late_queries")
    if has_dates:
        lines += [
            "<i>Форматы даты: 21.02 / 2026-02-21 / вчера</i>",
            "<i>Периоды: вчера / 14.02 / неделя / эта неделя / месяц / 7д / 01.02-15.02 / февраль</i>",
            "<i>Фильтр: город (Барнаул) или точка (Барнаул_1)</i>",
        ]

    if perms.has("reports"):
        lines += [
            "",
            "<i>Примеры:</i>",
            "<code>/отчёт Барнаул 14.02</code>",
            "<code>/отчёт Барнаул неделя</code>",
            "<code>/отчёт 01.02-15.02</code>",
            "<code>/точные Барнаул</code>",
        ]

    if perms.has("search"):
        lines += [
            "<code>/поиск 119458</code>",
            "<code>/поиск 12345 томск</code>",
        ]

    if perms.has("late_queries") and not perms.has("reports"):
        lines += [
            "",
            "<i>Примеры:</i>",
            "<code>/опоздания Томск</code>",
            "<code>/опоздания вчера</code>",
        ]

    if perms.has("late_alerts"):
        lines += [
            "<code>/тишина 30</code>",
            "<code>/тишина 1ч</code>",
        ]

    return "\n".join(lines)


async def poll_analytics_bot() -> None:
    """Polling job для аналитического бота. Запускается каждые 3 секунды."""
    global _last_update_id

    if not settings.telegram_analytics_bot_token:
        return

    updates = await _get_updates(offset=_last_update_id)

    for update in updates:
        _last_update_id = update["update_id"] + 1

        # Автодетект: бот добавлен в новый чат
        my_chat_member = update.get("my_chat_member")
        if my_chat_member:
            new_status = my_chat_member.get("new_chat_member", {}).get("status")
            if new_status in ("member", "administrator"):
                chat_info = my_chat_member.get("chat", {})
                new_chat_id = chat_info.get("id", 0)
                chat_title = chat_info.get("title", str(new_chat_id))
                admin_id = settings.telegram_admin_id
                try:
                    await access_manager.notify_new_chat(admin_id, new_chat_id, chat_title)
                except Exception as e:
                    logger.error(f"[autodетект] notify_new_chat: {e}")
            continue

        # Обработка нажатия inline-кнопки
        callback = update.get("callback_query")
        if callback:
            cb_id = callback["id"]
            cb_data = callback.get("data", "")
            cb_user_id = callback.get("from", {}).get("id", 0)
            cb_chat_id = callback["message"]["chat"]["id"]
            cb_message_id = callback["message"]["message_id"]

            if cb_data.startswith("ac:"):
                await access_manager.handle_callback(
                    cb_id, cb_user_id, cb_chat_id, cb_message_id, cb_data
                )
            elif cb_data.startswith("order:"):
                perms = access.get_permissions(cb_chat_id, cb_user_id)
                if perms.has("search"):
                    order_num = cb_data[6:]
                    await _answer_callback(cb_id)
                    await _handle_search(cb_chat_id, order_num, city_filter=perms.city)
                else:
                    await _answer_callback(cb_id, "🚫 Нет доступа")
            elif cb_data.startswith("rpt:"):
                perms = access.get_permissions(cb_chat_id, cb_user_id)
                if perms.has("reports"):
                    parts = cb_data.split(":", 3)
                    if len(parts) == 4:
                        _, branch_name, d_from, d_to = parts
                        is_single = (d_from == d_to)
                        lbl = d_from if is_single else f"{d_from} – {d_to}"
                        await _answer_callback(cb_id)
                        msg = await _build_branch_report(branch_name, d_from, d_to, lbl, is_single)
                        if msg:
                            await _send(cb_chat_id, msg)
                        else:
                            await _send(cb_chat_id, f"📭 {branch_name} — нет данных за {lbl}")
                    else:
                        await _answer_callback(cb_id, "❌ Ошибка данных")
                else:
                    await _answer_callback(cb_id, "🚫 Нет доступа")
            else:
                await _answer_callback(cb_id, "🚫 Нет доступа")
            continue

        message = update.get("message") or update.get("edited_message")
        if not message:
            continue

        text: str = message.get("text", "").strip()
        chat_id: int = message["chat"]["id"]
        user_id: int = message.get("from", {}).get("id", 0)
        username: str = message.get("from", {}).get("username", "unknown")

        # Личка — только admin. Остальные игнорируются молча.
        if chat_id > 0 and not access.is_admin(user_id):
            continue

        # Автодетект финансовых файлов (без команды)
        tg_doc = message.get("document")
        if tg_doc and not text:
            file_name = tg_doc.get("file_name", "")
            perms = access.get_permissions(chat_id, user_id)
            if perms.has("finance"):
                if file_name.lower().endswith(".txt"):
                    try:
                        await _handle_bank_statement(chat_id, tg_doc, user_id=user_id)
                    except Exception as e:
                        logger.error(f"[bank_statement] {e}", exc_info=True)
                        await _send(chat_id, f"❌ Ошибка: {html.escape(str(e))}")
                elif file_name.lower().endswith(".xlsx"):
                    try:
                        await _handle_tbank_registry(chat_id, tg_doc, user_id=user_id)
                    except Exception as e:
                        logger.error(f"[tbank_registry] {e}", exc_info=True)
                        await _send(chat_id, f"❌ Ошибка: {html.escape(str(e))}")
            continue

        # Не команда — проверяем диалог access_manager (например, ввод ID чата)
        if text and not text.startswith("/"):
            if access.is_admin(user_id) and chat_id > 0:
                try:
                    handled = await access_manager.handle_text(chat_id, user_id, text)
                    if handled:
                        continue
                except Exception as e:
                    logger.error(f"[access_manager] handle_text: {e}")
            continue

        if not text.startswith("/"):
            continue

        logger.info(f"[analytics_bot] @{username} ({user_id}): {text}")

        perms = access.get_permissions(chat_id, user_id)

        if not perms.modules and not perms.is_admin:
            if chat_id > 0:
                await _send(chat_id, "🚫 Нет доступа.")
            continue

        # Киргизское приветствие Ильи — раз в день
        if user_id == _ILYA_ID and _check_and_mark_ilya_greeting():
            await _send(chat_id, random.choice(_KYRGYZ_GREETINGS))

        cmd_raw = text.lstrip("/").split("@")[0]
        parts = cmd_raw.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        # Команды без проверки модуля
        if cmd in ("помощь", "help", "start"):
            await _send(chat_id, _build_help(perms))
            continue

        # Проверяем модуль для команды
        required_module = _CMD_MODULE.get(cmd)
        if required_module and not perms.has(required_module):
            continue

        city = perms.city  # None = все города, иначе фильтруем

        if cmd in ("статус", "status"):
            if user_id == _ILYA_ID:
                await _send(chat_id, random.choice(_KYRGYZ_ACKS))
            await _handle_status(chat_id, arg, city_filter=city)
        elif cmd in ("повара", "cooks"):
            if user_id == _ILYA_ID:
                await _send(chat_id, random.choice(_KYRGYZ_ACKS))
            await _handle_staff(chat_id, arg, "cook", city_filter=city)
        elif cmd in ("курьеры", "couriers"):
            if user_id == _ILYA_ID:
                await _send(chat_id, random.choice(_KYRGYZ_ACKS))
            await _handle_staff(chat_id, arg, "courier", city_filter=city)
        elif cmd in ("поиск", "search"):
            await _handle_search(chat_id, arg, city_filter=city)
        elif cmd in ("отчёт", "отчет", "report"):
            await _handle_day(chat_id, arg, city_filter=city)
        elif cmd in ("точные", "exact"):
            await _handle_exact_orders(chat_id, arg, city_filter=city)
        elif cmd in ("опоздания", "late"):
            await _handle_late(chat_id, arg, city_filter=city)
        elif cmd in ("самовывоз", "pickup"):
            await _handle_pickup(chat_id, arg, city_filter=city)
        elif cmd in ("тишина", "mute"):
            await _handle_mute(chat_id, arg, user_id=user_id)
        elif cmd in ("аудит", "audit"):
            from app.jobs.audit import handle_audit_command
            await handle_audit_command(chat_id, arg, city_filter=city)
        elif cmd in ("выгрузка", "export"):
            from app.jobs.marketing_export import run_export
            await run_export(chat_id, arg, _bot_url())
        elif cmd in ("доступ", "access"):
            await access_manager.handle_command(chat_id, user_id)
        elif cmd in ("конкуренты", "competitors"):
            await _send(chat_id, "⏳ Обновляю таблицы конкурентов...")
            try:
                from app.jobs.competitor_sheets import export_all_competitors_to_sheets
                await export_all_competitors_to_sheets()
                await _send(chat_id, "✅ Таблицы конкурентов обновлены.")
            except Exception as e:
                logger.error(f"[/конкуренты] Ошибка: {e}", exc_info=True)
                await _send(chat_id, f"❌ Ошибка при обновлении: {e}")
        elif required_module is None:
            await _send(chat_id, f"❓ Неизвестная команда: /{cmd}\n\nНапиши /помощь")


async def run_polling_loop() -> None:
    """Continuous polling loop. Runs as asyncio.Task in lifespan (replaces APScheduler job)."""
    import asyncio as _asyncio
    logger.info("Аркентий: polling loop started")
    while True:
        try:
            await poll_analytics_bot()
            await _asyncio.sleep(0.5)
        except _asyncio.CancelledError:
            logger.info("Аркентий: polling loop cancelled")
            break
        except Exception as e:
            logger.error(f"Аркентий polling loop error: {e}")
            await _asyncio.sleep(5)
