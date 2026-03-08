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

import asyncio
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from app.jobs import bank_statement
from app.jobs import tbank_reconciliation

from app.services import access
from app.clients.iiko_bo_events import (
    get_all_branches_staff,
    is_events_loaded,
    _states,
    _parse_customer_name,
    _parse_customer_phone,
)
from app.config import get_settings
from app.db import BACKEND, aggregate_orders_for_daily_stats, get_client_order_count, get_daily_stats, get_exact_time_orders, get_live_today_stats, get_period_stats, log_silence
from app.database_pg import get_pool
from app.services import access_manager
from app.jobs.daily_report import _format_branch_report
from app.utils.formatting import fmt_money as _fmt_money
from app.jobs.iiko_status_report import (
    format_branch_status,
    get_available_branches,
    get_branch_status,
)
from app.utils.timezone import branch_tz as _branch_tz
from app.jobs.late_alerts import (
    ACTIVE_DELIVERY_STATUSES,
    LATE_MAX_MIN,
    LOCAL_UTC_OFFSET as _LATE_UTC_OFFSET,
    set_silence as _set_silence,
    is_silenced as _is_silenced,
    get_silence_until as _get_silence_until,
)

logger = logging.getLogger(__name__)
settings = get_settings()


def _tz_for_branch(name: str) -> timezone:
    """Timezone по имени точки (utc_offset из конфига текущего тенанта)."""
    for b in get_available_branches():
        if b["name"] == name:
            return _branch_tz(b)
    return settings.default_tz

# #region agent log
def _debug_log(location: str, message: str, data: dict, hypothesis_id: str = "") -> None:
    import json as _json
    try:
        import pathlib as _pl
        from app.ctx import ctx_tenant_id
        log_path = _pl.Path(__file__).resolve().parents[3] / ".cursor" / "debug-3e913f.log"
        payload = {"sessionId": "3e913f", "location": location, "message": message, "data": data, "timestamp": __import__("time").time() * 1000}
        if hypothesis_id:
            payload["hypothesisId"] = hypothesis_id
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
# #endregion

# Context variables для мульти-тенантного режима (определены в app.ctx, импортируем здесь).
from app.ctx import ctx_tenant_id as _ctx_tenant_id, ctx_bot_token as _ctx_bot_token

# Смещение update_id отдельно для каждого токена
_last_update_id: dict[str, int | None] = {}  # bot_token → last update_id

# AI @ mention — in-memory флаг горячего отключения (/ai on|off)
# При старте берётся из settings.openclaw_enabled; меняется без редеплоя
_openclaw_enabled: bool = True

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
    "смены": "late_queries", "payment_changes": "late_queries",
    "конкуренты": "admin", "competitors": "admin",
    "доступ": "admin", "access": "admin",
    "jobs": "admin",
}



# ------------------------------------------------------------------
# Telegram helpers (берут токен из контекста polling loop)
# ------------------------------------------------------------------

def _bot_url() -> str:
    token = _ctx_bot_token.get() or settings.telegram_analytics_bot_token
    return f"https://api.telegram.org/bot{token}"


_TG_MAX_LEN = 4096


async def _send(chat_id: int, text: str, parse_mode: str = "HTML") -> None:
    """Отправляет сообщение, автоматически разбивая на части если > 4096 символов."""
    if len(text) <= _TG_MAX_LEN:
        chunks = [text]
    else:
        # Разбиваем по двойным переносам (между блоками), чтобы не резать mid-слово
        chunks = []
        current = ""
        for part in text.split("\n\n"):
            block = (("\n\n" + part) if current else part)
            if len(current) + len(block) <= _TG_MAX_LEN:
                current += block
            else:
                if current:
                    chunks.append(current)
                # Если один блок > 4096 — режем жёстко
                if len(part) > _TG_MAX_LEN:
                    for i in range(0, len(part), _TG_MAX_LEN):
                        chunks.append(part[i:i + _TG_MAX_LEN])
                    current = ""
                else:
                    current = part
        if current:
            chunks.append(current)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for chunk in chunks:
                r = await client.post(
                    f"{_bot_url()}/sendMessage",
                    json={"chat_id": chat_id, "text": chunk, "parse_mode": parse_mode},
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


async def _send_with_keyboard_return_id(chat_id: int, text: str, keyboard: list) -> int | None:
    """Отправляет сообщение с inline-клавиатурой, возвращает message_id."""
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
            data = r.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            logger.error(f"analytics_bot _send_with_keyboard_return_id: {r.text[:200]}")
    except Exception as e:
        logger.error(f"analytics_bot _send_with_keyboard_return_id: {e}")
    return None


async def _edit_message(chat_id: int, message_id: int, text: str, keyboard: list | None = None) -> None:
    """Редактирует существующее сообщение (текст + клавиатура)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
            if keyboard is not None:
                payload["reply_markup"] = {"inline_keyboard": keyboard}
            r = await client.post(f"{_bot_url()}/editMessageText", json=payload)
            resp = r.json()
            if not resp.get("ok"):
                desc = resp.get("description", "")
                if "message is not modified" in desc:
                    pass  # данные не изменились — норма
                else:
                    logger.error(f"editMessageText error: {r.text[:200]}")
    except Exception as e:
        logger.error(f"_edit_message: {e}")


# Кеш отчётов ТБанк для drill-down (message_id -> original report text)
_tbank_report_cache: dict[int, str] = {}

# Кеш поиска для edit_message навигации (chat_id, msg_id) -> {text, keyboard, rows, back_label}
_search_cache: dict[tuple[int, int], dict] = {}
_SEARCH_CACHE_MAX = 100

# Кеш статуса для edit_message навигации (chat_id, msg_id) -> {summary_text, summary_keyboard, branches_data, branches}
_status_cache: dict[tuple[int, int], dict] = {}
_STATUS_CACHE_MAX = 50


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


async def _react(chat_id: int, message_id: int, emoji: str) -> None:
    """Ставит emoji-реакцию на сообщение (Bot API 7.0+, setMessageReaction)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{_bot_url()}/setMessageReaction",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reaction": [{"type": "emoji", "emoji": emoji}],
                    "is_big": False,
                },
            )
    except Exception as e:
        logger.debug("_react error: %s", e)


async def _reply(chat_id: int, message_id: int, text: str, parse_mode: str = "HTML") -> None:
    """Отправляет reply на конкретное сообщение. Длинный текст разбивает на части."""
    max_len = 4096
    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, max_len)
        if cut <= 0:
            cut = max_len
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")

    first = True
    for chunk in chunks:
        payload: dict = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": parse_mode,
        }
        if first:
            payload["reply_to_message_id"] = message_id
            payload["allow_sending_without_reply"] = True
            first = False
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(f"{_bot_url()}/sendMessage", json=payload)
                if not r.json().get("ok"):
                    # Если Markdown сломан — повторить без форматирования
                    if parse_mode != "HTML":
                        payload["parse_mode"] = "HTML"
                        await client.post(f"{_bot_url()}/sendMessage", json=payload)
        except Exception as e:
            logger.error("_reply error: %s", e)


def _is_bot_mentioned(message: dict, text: str) -> bool:
    """
    Проверяет что бот упомянут через @username в тексте.
    Использует entities из Telegram — надёжнее regex.
    Если telegram_bot_username не задан — срабатывает на любой @mention.
    """
    entities = message.get("entities", [])
    bot_username = settings.telegram_bot_username.lower().lstrip("@")
    for entity in entities:
        if entity.get("type") != "mention":
            continue
        offset = entity["offset"]
        length = entity["length"]
        mentioned = text[offset : offset + length].lstrip("@").lower()
        if not bot_username or mentioned == bot_username:
            return True
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
                statement_accounts=set(result["parsed"].accounts),
            )
            if reconcile_text:
                await _send(chat_id, reconcile_text)
        except Exception as e:
            logger.error(f"[bank_statement] reconcile: {e}", exc_info=True)
            await _send(chat_id, f"⚠️ Сверка с iiko не удалась: {html.escape(str(e))}")

    # Логирование в БД
    try:
        from app.db import save_bank_statement_log
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


async def _handle_tbank_payout(chat_id: int, file_name: str, raw: bytes, user_id: int = 0) -> None:
    """Обработка отчёта по выплатам ТБанк."""
    await _send(chat_id, f"📥 Обрабатываю выплаты ТБанк <b>{html.escape(file_name)}</b>...")

    try:
        result = await tbank_reconciliation.process_payout(
            data=raw,
            user_id=user_id,
            chat_id=chat_id,
            filename=file_name,
        )
    except Exception as e:
        logger.error(f"tbank_payout: {e}", exc_info=True)
        await _send(chat_id, f"❌ Ошибка обработки: {html.escape(str(e))}")
        return

    if "error" in result:
        await _send(chat_id, f"❌ {html.escape(result['error'])}")
        return

    report = result.get("report", "")
    if report:
        await _send(chat_id, report)

    await _send(chat_id, "✅ Выплаты зафиксированы")


async def _handle_tbank_registry(chat_id: int, file_name: str, raw: bytes, user_id: int = 0) -> None:
    """Обработка реестра ТБанк: сверить с iiko."""
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
        if result.get("has_pending"):
            keyboard = [[{"text": "🔍 Детализация по точке", "callback_data": "tbank:branches"}]]
            msg_id = await _send_with_keyboard_return_id(chat_id, report, keyboard)
            if msg_id:
                _tbank_report_cache[msg_id] = report
        else:
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
    params = {"timeout": 2, "limit": 10, "allowed_updates": ["message", "callback_query", "my_chat_member"]}
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
    today = datetime.now(settings.default_tz).date()

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

def _status_summary_line(data: dict, show_time: bool = False) -> str:
    """Двухстрочный блок точки для сводки /статус (вариант А)."""
    name = html.escape(data["name"])
    rev = f"{data['revenue']:,} ₽".replace(",", " ") if data.get("revenue") is not None else "—"
    checks = f"{data['check_count']} чека" if data.get("check_count") is not None else "—"

    if show_time:
        tz = data.get("tz") or settings.default_tz
        now_tz = datetime.now(tz).strftime("%H:%M")
        line1 = f"<b>{name}</b> · {now_tz} · {rev} · {checks}"
    else:
        line1 = f"<b>{name}</b> · {rev} · {checks}"

    # Строка 2: опоздания + активные заказы
    parts2: list[str] = []
    delays = data.get("delays")
    if delays and delays.get("total_delivered", 0) > 0:
        late = delays["late_count"]
        total = delays["total_delivered"]
        pct = round(late / total * 100) if total else 0
        avg_min = delays.get("avg_delay_min", 0)
        if late >= 2:
            parts2.append(f"🔴 {late}/{total} опозд. ({pct}%) ≈{avg_min} мин")
        elif late == 1:
            parts2.append(f"🟡 1/{total} опозд. ({pct}%) ≈{avg_min} мин")
        else:
            parts2.append(f"✅ 0/{total} опозд.")
    elif delays is not None:
        parts2.append("✅ 0 опозд.")
    elif data.get("revenue") is None and data.get("check_count") is None:
        parts2.append("⚠️ нет данных")
    else:
        parts2.append("⏳ RT загружается")

    active = data.get("active_orders")
    if active is not None:
        n_dispatch = data.get("orders_before_dispatch") or 0
        n_way = data.get("orders_on_way") or 0
        zak = f"🚚 {active} акт."
        if n_dispatch:
            zak += f" · {n_dispatch} до отпр."
        if n_way:
            zak += f" · {n_way} в пути"
        parts2.append(zak)

    line2 = " · ".join(parts2)
    return f"{line1}\n{line2}"


def _build_status_summary(results: list[dict]) -> tuple[str, list]:
    """Строит сводное сообщение и клавиатуру для нескольких точек."""
    # Если все точки в одной tz — время однажды в шапке; если tz разные — в каждой строке.
    offsets = {int(r["tz"].utcoffset(None).total_seconds()) for r in results if r.get("tz")}
    multi_tz = len(offsets) > 1

    if multi_tz:
        header = "📊 <b>Статус</b>"
    else:
        common_tz = next((r["tz"] for r in results if r.get("tz")), settings.default_tz)
        now_str = datetime.now(common_tz).strftime("%H:%M")
        header = f"📊 <b>Статус</b> — {now_str}"

    blocks = [header]
    for data in results:
        blocks.append(_status_summary_line(data, show_time=multi_tz))

    keyboard: list[list[dict]] = []
    row_buf: list[dict] = []
    for data in results:
        name = data["name"]
        row_buf.append({"text": f"📍 {name}", "callback_data": f"stat:branch:{name}"})
        if len(row_buf) == 2:
            keyboard.append(row_buf)
            row_buf = []
    if row_buf:
        keyboard.append(row_buf)
    keyboard.append([{"text": "🔄 Обновить", "callback_data": "stat:refresh"}])

    return "\n\n".join(blocks), keyboard


async def _handle_status(chat_id: int, arg: str, city_filter: str | None = None) -> None:
    """
    /статус [фильтр] — статус точек.
    Одна точка → сразу карточка + кнопка обновить.
    Несколько → сводка + кнопки per-точка + обновить (edit_message навигация).
    """
    if not is_events_loaded():
        await _send(chat_id, "⏳ Данные загружаются после перезапуска, подождите 1\u20132 минуты.")
        return
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

    # Параллельный сбор данных
    async def _safe_get(branch: dict) -> dict:
        try:
            return await get_branch_status(branch)
        except Exception as e:
            logger.error(f"[статус] [{branch['name']}]: {e}")
            return {"name": branch["name"], "revenue": None, "check_count": None,
                    "avg_check": None, "cogs_pct": None, "discount_sum": None,
                    "discount_types_agg": [], "sailplay": None, "tz": None,
                    "active_orders": None, "delivered_today": None,
                    "orders_before_dispatch": None, "orders_cooking": None,
                    "orders_ready": None, "orders_on_way": None,
                    "couriers_on_shift": None, "cooks_on_shift": None,
                    "delays": None, "avg_cooking_min": None, "avg_wait_min": None,
                    "avg_delivery_min": None}

    results = await asyncio.gather(*[_safe_get(b) for b in filtered])

    # Одна точка — карточка напрямую
    if len(results) == 1:
        data = results[0]
        card_text = format_branch_status(data)
        refresh_kb = [[{"text": "🔄 Обновить", "callback_data": f"stat:refresh:{data['name']}"}]]
        msg_id = await _send_with_keyboard_return_id(chat_id, card_text, refresh_kb)
        if msg_id:
            if len(_status_cache) >= _STATUS_CACHE_MAX:
                del _status_cache[next(iter(_status_cache))]
            _status_cache[(chat_id, msg_id)] = {
                "summary_text": None,
                "summary_keyboard": None,
                "branches_data": {data["name"]: data},
                "branches": filtered,
            }
        return

    # Несколько точек — сводка
    branches_data = {d["name"]: d for d in results}
    summary_text, summary_kb = _build_status_summary(list(results))
    msg_id = await _send_with_keyboard_return_id(chat_id, summary_text, summary_kb)
    if msg_id:
        if len(_status_cache) >= _STATUS_CACHE_MAX:
            del _status_cache[next(iter(_status_cache))]
        _status_cache[(chat_id, msg_id)] = {
            "summary_text": summary_text,
            "summary_keyboard": summary_kb,
            "branches_data": branches_data,
            "branches": filtered,
        }


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
                _local_tz = _tz_for_branch(r.get("branch_name", ""))
                planned_dt = datetime.strptime(r["planned_time"].replace("T", " ").split(".")[0], "%Y-%m-%d %H:%M:%S")
                now_local = datetime.now(_local_tz).replace(tzinfo=None)
                if now_local > planned_dt:
                    overdue_min = int((now_local - planned_dt).total_seconds() / 60)
                    late_str = f"⏳ ещё не доставлен | ⚠️ опаздывает {overdue_min} м"
            except Exception:
                pass

    s = r["sum"]
    sum_str = f"{int(float(s)):,} ₽".replace(",", " ") if s else "—"
    pay_raw = (r.get("payment_type") or "").strip()
    _pay_map = {
        "наличные": "Наличные",
        "безналичный расчет": "Безнал", "безналичный расчёт": "Безнал",
        "онлайн": "Онлайн", "тинькофф": "Онлайн", "т-банк": "Онлайн",
        "системы лояльности": "Бонусы",
    }
    pay_str = _pay_map.get(pay_raw.lower(), pay_raw) if pay_raw else ""
    sum_line = f"{sum_str} · {html.escape(pay_str)}" if pay_str else sum_str
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
                clean_pt = r["planned_time"].replace("T", " ").split(".")[0]
                planned_dt = datetime.strptime(clean_pt, "%Y-%m-%d %H:%M:%S")
                now_local = datetime.now(_tz_for_branch(r.get("branch_name", ""))).replace(tzinfo=None)
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
        f"   💰 {sum_line}\n"
        f"   ⏱ {_fmt_dt(r['planned_time'])} → {_fmt_dt(r['actual_time'])} | {late_str}\n"
        f"🍱 Состав:\n{items_str}"
    )


def _format_order_compact(r: dict) -> str:
    """Компактная строка для больших выборок — двухстрочный формат."""
    type_icon = "🚶" if r.get("is_self_service") else "🛵"
    city = r["branch_name"].split("_")[0] if r.get("branch_name") else "?"
    s = r["sum"]
    sum_str = f"{int(float(s))}₽" if s else "—"
    if r["is_late"]:
        mins = int(r.get("late_minutes") or 0)
        late_icon = f"🔴 +{mins}м" if mins else "🔴"
    elif r["actual_time"]:
        late_icon = "✅"
    else:
        late_icon = "⏳"
    return (
        f"{type_icon} #{r['delivery_num']} {html.escape(city)}\n"
        f"   {_fmt_dt(r['planned_time'])} · {sum_str} · {late_icon}"
    )


async def _handle_search(chat_id: int, query: str, city_filter: str | None = None) -> None:
    """
    /поиск <запрос> — универсальный поиск по orders_raw.
    Все результаты — в одном сообщении с inline-навигацией (edit_message).
    Типы: numeric (по номеру заказа), phone (по телефону), text (по тексту).
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

    # Определяем тип поиска
    is_phone = bool(re.match(r"^\+?\d{10,11}$", query.replace(" ", "").replace("-", "")))
    is_numeric = query.isdigit() and not is_phone
    q = f"%{query}%"

    COLS = """branch_name, delivery_num, status, courier, sum,
              planned_time, actual_time, is_late, late_minutes,
              client_name, client_phone, delivery_address, items, is_self_service, comment,
              payment_type"""

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

    # Если нет явного фильтра по городу — ограничиваем ветками текущего тенанта
    if not city_branch_names:
        city_branch_names = [b["name"] for b in get_available_branches()]

    rows: list[dict] = []
    total = 0
    query_type = "text"

    if BACKEND == "postgresql":
        pool = get_pool()
        has_city = bool(city_branch_names)
        tenant_id = _ctx_tenant_id.get()

        if is_numeric:
            if has_city:
                # branch_name фильтр уже изолирует по тенанту — tenant_id не нужен
                pg_rows = await pool.fetch(
                    f"SELECT {COLS} FROM orders_raw WHERE delivery_num = $1 AND branch_name = ANY($2) ORDER BY planned_time DESC",
                    query, city_branch_names,
                )
            else:
                pg_rows = await pool.fetch(
                    f"SELECT {COLS} FROM orders_raw WHERE tenant_id = $1 AND delivery_num = $2 ORDER BY planned_time DESC",
                    tenant_id, query,
                )
            query_type = "numeric"

        elif is_phone:
            phone_q = query.lstrip("+")
            if has_city:
                # branch_name фильтр уже изолирует по тенанту — tenant_id не нужен
                pg_rows = await pool.fetch(
                    f"SELECT {COLS} FROM orders_raw WHERE (client_phone LIKE $1 OR client_phone LIKE $2) AND branch_name = ANY($3) ORDER BY planned_time DESC LIMIT 30",
                    f"%{phone_q}%", f"%{query}%", city_branch_names,
                )
            else:
                pg_rows = await pool.fetch(
                    f"SELECT {COLS} FROM orders_raw WHERE tenant_id = $1 AND (client_phone LIKE $2 OR client_phone LIKE $3) ORDER BY planned_time DESC LIMIT 30",
                    tenant_id, f"%{phone_q}%", f"%{query}%",
                )
            query_type = "phone"

        else:
            if has_city:
                # branch_name фильтр уже изолирует по тенанту — tenant_id не нужен
                count_row = await pool.fetchrow(
                    "SELECT COUNT(*) FROM orders_raw WHERE (delivery_num LIKE $1 OR client_phone LIKE $1 OR delivery_address LIKE $1 OR items LIKE $1) AND branch_name = ANY($2)",
                    q, city_branch_names,
                )
                pg_rows = await pool.fetch(
                    f"SELECT {COLS} FROM orders_raw WHERE (delivery_num LIKE $1 OR client_phone LIKE $1 OR delivery_address LIKE $1 OR items LIKE $1) AND branch_name = ANY($2) ORDER BY planned_time DESC LIMIT 20",
                    q, city_branch_names,
                )
            else:
                count_row = await pool.fetchrow(
                    "SELECT COUNT(*) FROM orders_raw WHERE tenant_id = $1 AND (delivery_num LIKE $2 OR client_phone LIKE $2 OR delivery_address LIKE $2 OR items LIKE $2)",
                    tenant_id, q,
                )
                pg_rows = await pool.fetch(
                    f"SELECT {COLS} FROM orders_raw WHERE tenant_id = $1 AND (delivery_num LIKE $2 OR client_phone LIKE $2 OR delivery_address LIKE $2 OR items LIKE $2) ORDER BY planned_time DESC LIMIT 20",
                    tenant_id, q,
                )
            total = count_row[0] if count_row else 0
            query_type = "text"

        rows = [dict(r) for r in pg_rows]

    if not rows:
        await _send(chat_id, f"🔍 <code>{html.escape(query)}</code> — ничего не найдено.")
        return

    # Один результат — сразу карточка, без навигации
    if len(rows) == 1:
        r = rows[0]
        phone = (r.get("client_phone") or "").strip()
        cnt = await get_client_order_count(phone) if phone else None
        await _send(chat_id, await _format_order_card(r, client_count=cnt))
        return

    # --- Несколько результатов: одно сообщение + кнопки ---

    def _late_ico(r: dict) -> str:
        if (r.get("status") or "").lower() in ("отменена", "отменён"):
            return "❌"
        if r["is_late"]:
            mins = r.get("late_minutes")
            return f"🔴 +{int(mins)} мин" if mins else "🔴"
        if r["actual_time"]:
            return "✅"
        return "⏳"

    if query_type == "numeric":
        lines = [f"🔍 <b>#{html.escape(query)}</b> — найдено в {len(rows)} филиалах\n"]
        for r in rows:
            s = r["sum"]
            sum_str = f"{int(float(s)):,} ₽".replace(",", " ") if s else "—"
            lines.append(
                f"📍 {html.escape(r['branch_name'])} · "
                f"{html.escape(r.get('status') or '?')} · {_late_ico(r)} · {sum_str}"
            )
        text = "\n".join(lines)
        keyboard: list[list[dict]] = []
        row_buf: list[dict] = []
        for idx, r in enumerate(rows):
            row_buf.append({"text": f"📋 {r['branch_name']}", "callback_data": f"srch:card:{query}:{idx}"})
            if len(row_buf) == 2:
                keyboard.append(row_buf)
                row_buf = []
        if row_buf:
            keyboard.append(row_buf)
        back_label = f"← #{query} (все филиалы)"

    elif query_type == "phone":
        client_name = (rows[0].get("client_name") or "—")
        branches_set = {r["branch_name"] for r in rows}
        header = (
            f"🔍 <code>{html.escape(query)}</code> — {len(rows)} заказов\n"
            f"👤 {html.escape(client_name)}"
        )
        order_lines = [_format_order_compact(r) for r in rows]
        text = header + "\n\n" + "\n\n".join(order_lines)
        keyboard = []
        row_buf = []
        for idx, r in enumerate(rows):
            city_short = r["branch_name"].split("_")[0] if r.get("branch_name") else "?"
            row_buf.append({"text": f"#{r['delivery_num']} ({city_short})", "callback_data": f"srch:card:{r['delivery_num']}:{idx}"})
            if len(row_buf) == 3:
                keyboard.append(row_buf)
                row_buf = []
        if row_buf:
            keyboard.append(row_buf)
        back_label = "← К клиенту"

    else:
        more_str = f" (показаны 20 из {total})" if total > 20 else ""
        lines = [f"🔍 <b>{html.escape(query)}</b> — найдено: {total or len(rows)}{more_str}\n"]
        for r in rows:
            s = r["sum"]
            sum_str = f"{int(float(s)):,} ₽".replace(",", " ") if s else "—"
            client = html.escape(r.get("client_name") or "?")
            lines.append(
                f"#{r['delivery_num']} · {client} · {sum_str} · {_late_ico(r)} · {_fmt_dt(r['planned_time'])}"
            )
        if total > 20:
            lines.append("\n<i>Уточни запрос чтобы сузить выборку.</i>")
        text = "\n".join(lines)
        keyboard = []
        row_buf = []
        for idx, r in enumerate(rows):
            row_buf.append({"text": f"#{r['delivery_num']}", "callback_data": f"srch:card:{r['delivery_num']}:{idx}"})
            if len(row_buf) == 3:
                keyboard.append(row_buf)
                row_buf = []
        if row_buf:
            keyboard.append(row_buf)
        back_label = "← К результатам"

    msg_id = await _send_with_keyboard_return_id(chat_id, text, keyboard)
    if msg_id:
        if len(_search_cache) >= _SEARCH_CACHE_MAX:
            oldest = next(iter(_search_cache))
            del _search_cache[oldest]
        _search_cache[(chat_id, msg_id)] = {
            "text": text,
            "keyboard": keyboard,
            "rows": rows,
            "back_label": back_label,
        }



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
    today = datetime.now(settings.default_tz).date()
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

    # #region agent log
    _tid = _ctx_tenant_id.get()
    _debug_log("arkentiy:_build_branch_report:before", "about to fetch stats", {"branch": name, "date_from": date_from, "date_to": date_to, "is_single_day": is_single_day, "ctx_tenant_id": _tid}, "H1")
    # #endregion
    if is_single_day:
        ds = await get_daily_stats(name, date_from, tenant_id=_tid)
    else:
        ds = await get_period_stats(name, date_from, date_to, tenant_id=_tid)

    # #region agent log
    _debug_log("arkentiy:_build_branch_report:after", "stats result", {"branch": name, "has_data": ds is not None}, "H1")
    # #endregion

    # Если данных в daily_stats нет — пробуем live-отчёт из orders_raw
    is_live = False
    if not ds and is_single_day:
        _br_cfg = next((b for b in get_available_branches() if b["name"] == name), {})
        today_local = datetime.now(_branch_tz(_br_cfg) if _br_cfg else settings.default_tz).date().isoformat()
        if date_from == today_local:
            ds = await get_live_today_stats(name, date_from, tenant_id=_tid)
            is_live = True

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
            "payment_changed_count": ds.get("payment_changed_count") or 0,
        }

    result = _format_branch_report(name, ds, label, agg, is_period=not is_single_day)
    if is_live:
        result = "⚠️ <b>Смена не закрыта — данные неполные</b>\n(COGS, скидки и времена этапов появятся после закрытия смены)\n\n" + result
    return result


async def _build_city_aggregate(
    branches: list[dict], date_from: str, date_to: str, label: str, is_single_day: bool,
) -> str | None:
    """Агрегирует данные по всем филиалам города в один отчёт."""
    import json as _json

    _tid = _ctx_tenant_id.get()
    totals: dict = {}
    weighted_keys = ("avg_cooking_min", "avg_wait_min", "avg_delivery_min", "avg_late_min")
    # weight column for each metric
    _weight_col = {
        "avg_cooking_min": "orders_count",
        "avg_wait_min": "total_delivered",
        "avg_delivery_min": "total_delivered",
        "avg_late_min": "late_delivery_count",
    }
    sum_keys = (
        "revenue", "orders_count", "discount_sum", "sailplay",
        "late_delivery_count", "total_delivered", "exact_time_count",
        "payment_changed_count", "cooks_today", "couriers_today",
    )
    count = 0
    all_dt: dict[str, dict] = {}
    any_live = False
    _first_tz = _branch_tz(branches[0]) if branches else settings.default_tz
    today_local = datetime.now(_first_tz).date().isoformat()

    for branch in branches:
        name = branch["name"]
        if is_single_day:
            ds = await get_daily_stats(name, date_from, tenant_id=_tid)
            if not ds and date_from == today_local:
                ds = await get_live_today_stats(name, date_from, tenant_id=_tid)
                if ds:
                    any_live = True
            agg = await aggregate_orders_for_daily_stats(name, date_from) if ds else {}
        else:
            ds = await get_period_stats(name, date_from, date_to, tenant_id=_tid)
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
                w_col = _weight_col[k]
                # for single day late count lives in agg, others in ds
                if is_single_day and w_col == "late_delivery_count":
                    w = agg.get("late_delivery_count") or 0
                else:
                    w = (ds.get(w_col) or agg.get(w_col) or 0)
                totals.setdefault(f"_{k}_wsum", 0.0)
                totals[f"_{k}_wsum"] += val * w
                totals.setdefault(f"_{k}_wdenom", 0.0)
                totals[f"_{k}_wdenom"] += w

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
        wsum = totals.pop(f"_{k}_wsum", None)
        wdenom = totals.pop(f"_{k}_wdenom", None)
        if wsum is not None and wdenom:
            totals[k] = round(wsum / wdenom, 1)
        else:
            totals.pop(k, None)

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
        "cooks_today": totals.get("cooks_today", 0),
        "couriers_today": totals.get("couriers_today", 0),
        "exact_time_count": totals.get("exact_time_count", 0),
        "payment_changed_count": totals.get("payment_changed_count", 0),
    }

    result = _format_branch_report(
        f"{city_name} (все точки)", totals, label, agg_out, is_period=not is_single_day,
    )
    if any_live:
        result = "⚠️ <b>Смена не закрыта — данные неполные</b>\n(COGS, скидки и времена этапов появятся после закрытия смены)\n\n" + result
    return result


async def _handle_day(chat_id: int, arg: str, city_filter=None) -> None:
    """
    /отчёт [филиал] [период] — отчёт за день/неделю/месяц/диапазон из daily_stats.
    При запросе по городу (>1 филиала) — сначала агрегат, потом inline-кнопки.
    """
    # #region agent log
    _tid = _ctx_tenant_id.get()
    _debug_log("arkentiy:_handle_day:entry", "handle_day started", {"chat_id": chat_id, "arg": arg, "city_filter": city_filter, "ctx_tenant_id": _tid}, "H2")
    # #endregion
    tokens = arg.strip().split() if arg.strip() else []
    date_from, date_to, label, filter_tokens = _parse_period(tokens)
    filter_q = " ".join(filter_tokens).strip()
    is_single_day = (date_from == date_to)

    if not filter_q and city_filter:
        filter_q = city_filter

    branches_cfg = get_available_branches(filter_q) if filter_q else get_available_branches()
    # #region agent log
    _debug_log("arkentiy:_handle_day:branches", "branches resolved", {"count": len(branches_cfg), "names": [b["name"] for b in branches_cfg], "filter_q": filter_q, "tenant_id": _tid}, "H4")
    # #endregion
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

    if BACKEND == "postgresql":
        pool = get_pool()
        tenant_id = _ctx_tenant_id.get()
        
        stats_pg = await pool.fetch(
            """SELECT branch_name,
                      SUM(CASE WHEN is_self_service=false THEN 1 ELSE 0 END) AS delivery_cnt,
                      SUM(CASE WHEN is_self_service=true THEN 1 ELSE 0 END) AS pickup_cnt
               FROM orders_raw
               WHERE tenant_id = $1 AND date::text = $2
                 AND status IN ('Доставлена','Закрыта')
                 AND branch_name = ANY($3)
               GROUP BY branch_name""",
            tenant_id, date, branch_names,
        )
        stats_rows = {r["branch_name"]: dict(r) for r in stats_pg}

        delivery_pg = await pool.fetch(
            """SELECT branch_name, delivery_num, courier, client_name,
                      planned_time, late_minutes, sum
               FROM orders_raw
               WHERE tenant_id = $1 AND date::text = $2 AND is_late = true AND is_self_service = false
                 AND branch_name = ANY($3)
               ORDER BY branch_name, late_minutes DESC""",
            tenant_id, date, branch_names,
        )
        delivery_late = [dict(r) for r in delivery_pg]

        pickup_pg = await pool.fetch(
            """SELECT branch_name, delivery_num, client_name,
                      planned_time, actual_time, late_minutes, sum, ready_time
               FROM orders_raw
               WHERE tenant_id = $1 AND date::text = $2 AND is_late = true AND is_self_service = true
                 AND branch_name = ANY($3)
               ORDER BY branch_name, late_minutes DESC""",
            tenant_id, date, branch_names,
        )
        pickup_late = [dict(r) for r in pickup_pg]

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

    if not is_events_loaded():
        await _send(chat_id, "⏳ Данные загружаются после перезапуска, подождите 1\u20132 минуты.")
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
            if overdue_min <= 0 or overdue_min > LATE_MAX_MIN:
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
                "address": (d.get("delivery_address") or "").strip(),
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
        address_part = ""
        if r.get("address"):
            address_part = f"\n  📍 {html.escape(r['address'])}"
        courier_part = ""
        if r["status"] == "В пути к клиенту" and r["courier"]:
            courier_part = f"\n  🛵 {html.escape(r['courier'])}"
        lines.append(
            f"<b>+{int(r['overdue_min'])} мин</b> | #{r['num']}"
            f" | {html.escape(r['branch'])}\n"
            f"  👤 {name} | 📞 <code>{phone}</code>"
            + address_part
            + f"\n  🕐 план: {r['planned_dt'].strftime('%H:%M')} | {status_str}"
            + courier_part
        )

    await _send(chat_id, "\n\n".join(lines))


async def _handle_pickup(chat_id: int, arg: str, city_filter=None) -> None:
    """/самовывоз [фильтр] — активные опоздавшие самовывозы прямо сейчас."""
    if not is_events_loaded():
        await _send(chat_id, "⏳ Данные загружаются после перезапуска, подождите 1\u20132 минуты.")
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




async def _handle_payment_changes(chat_id: int, arg: str, city_filter=None) -> None:
    """/смены [фильтр] [дата] — заказы со сменой оплаты (исключённые из статистики)."""
    from datetime import datetime, timedelta
    
    parts = arg.strip().split() if arg else []
    date_iso = None
    filter_q = None
    
    for p in parts:
        for fmt in ("%d.%m", "%d.%m.%Y", "%d.%m.%y"):
            try:
                dt = datetime.strptime(p, fmt)
                if dt.year < 2000:
                    dt = dt.replace(year=datetime.now().year)
                date_iso = dt.strftime("%Y-%m-%d")
                break
            except ValueError:
                continue
        else:
            if p.lower() in ("вчера", "yesterday"):
                date_iso = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            elif p.lower() in ("сегодня", "today"):
                date_iso = datetime.now().strftime("%Y-%m-%d")
            else:
                filter_q = p

    if filter_q is None and city_filter:
        filter_q = city_filter
    if date_iso is None:
        date_iso = datetime.now().strftime("%Y-%m-%d")

    branch_names = [b["name"] for b in (
        get_available_branches(filter_q) if filter_q else get_available_branches()
    )]

    try:
        from app.db import get_payment_changed_orders
        rows = await get_payment_changed_orders(branch_names, date_iso)
    except Exception as e:
        await _send(chat_id, f"Ошибка: {e}")
        return

    date_display = datetime.strptime(date_iso, "%Y-%m-%d").strftime("%d.%m.%Y")
    filter_label = f" ({html.escape(filter_q)})" if filter_q else ""

    if not rows:
        await _send(chat_id, f"📋 Смены оплаты: {date_display}{filter_label}\n\nНет заказов со сменой оплаты")
        return

    by_branch: dict[str, list[dict]] = {}
    for r in rows:
        branch = r["branch_name"]
        if branch not in by_branch:
            by_branch[branch] = []
        by_branch[branch].append(r)

    lines = [f"📋 <b>Смены оплаты: {date_display}{filter_label}</b>\n"]
    total = 0
    for branch, orders in sorted(by_branch.items()):
        lines.append(f"<b>{html.escape(branch)}</b> — {len(orders)}")
        for o in orders:
            num = o["delivery_num"]
            planned = o["planned_time"] or ""
            summ = o["sum"]
            time_str = planned[11:16] if len(planned) > 16 else "—"
            sum_str = f"{int(summ):,}".replace(",", " ") + " ₽" if summ else "—"
            lines.append(f"• #{num} — {time_str} — {sum_str}")
        total += len(orders)
        lines.append("")

    lines.append(f"<b>Итого:</b> {total} заказов")
    await _send(chat_id, "\n".join(lines))


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
        from app.ctx import ctx_tenant_id as _ctx_tid
        await log_silence(chat_id, minutes, user_id, tenant_id=_ctx_tid.get())
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


async def poll_analytics_bot(bot_token: str = "", tenant_id: int = 1) -> None:
    """
    Polling job для одного тенанта.
    bot_token и tenant_id устанавливаются в ContextVar — все хелперы используют их автоматически.
    """
    global _openclaw_enabled  # объявляем вверху функции, до первого использования
    token = bot_token or settings.telegram_analytics_bot_token
    if not token:
        return

    _ctx_bot_token.set(token)
    _ctx_tenant_id.set(tenant_id)

    current_offset = _last_update_id.get(token)
    updates = await _get_updates(offset=current_offset)

    for update in updates:
        _last_update_id[token] = update["update_id"] + 1

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

            # Мульти-тенант: определяем tenant по chat_id колбэка
            try:
                from app.database_pg import get_tenant_id_for_chat
                resolved = get_tenant_id_for_chat(cb_chat_id)
                if resolved is not None:
                    _ctx_tenant_id.set(resolved)
                else:
                    _ctx_tenant_id.set(tenant_id)
            except Exception:
                _ctx_tenant_id.set(tenant_id)

            if cb_data.startswith("ac:"):
                await access_manager.handle_callback(
                    cb_id, cb_user_id, cb_chat_id, cb_message_id, cb_data
                )
            elif cb_data.startswith("audit_detail:") or cb_data.startswith("audit_summary:"):
                perms = access.get_permissions(cb_chat_id, cb_user_id)
                if perms.has("audit"):
                    await _answer_callback(cb_id)
                    from app.jobs.audit import handle_audit_callback
                    from app.ctx import ctx_tenant_id as _ctx_tid
                    await handle_audit_callback(
                        cb_id, cb_chat_id, cb_message_id, cb_data, _ctx_tid.get()
                    )
                else:
                    await _answer_callback(cb_id, "🚫 Нет доступа")
            elif cb_data.startswith("tbank:"):
                perms = access.get_permissions(cb_chat_id, cb_user_id)
                if perms.has("finance"):
                    await _answer_callback(cb_id)
                    from app.db import get_overdue_payments, get_pending_payments
                    all_pending = await get_pending_payments(since_date=tbank_reconciliation.TRACKING_START_DATE)
                    overdue = await get_overdue_payments(tbank_reconciliation.OVERDUE_DAYS, since_date=tbank_reconciliation.TRACKING_START_DATE)

                    if cb_data == "tbank:branches":
                        text, keyboard = tbank_reconciliation.build_branch_list(all_pending, overdue)
                        await _edit_message(cb_chat_id, cb_message_id, text, keyboard)

                    elif cb_data.startswith("tbank:branch:"):
                        branch = cb_data[len("tbank:branch:"):]
                        text, keyboard = tbank_reconciliation.build_branch_detail(branch, all_pending, overdue)
                        await _edit_message(cb_chat_id, cb_message_id, text, keyboard)

                    elif cb_data == "tbank:back":
                        cached = _tbank_report_cache.get(cb_message_id)
                        if cached:
                            keyboard = [[{"text": "🔍 Детализация по точке", "callback_data": "tbank:branches"}]]
                            await _edit_message(cb_chat_id, cb_message_id, cached, keyboard)
                        else:
                            await _answer_callback(cb_id, "Отчёт устарел")
                else:
                    await _answer_callback(cb_id, "🚫 Нет доступа")
            elif cb_data.startswith("stat:"):
                perms = access.get_permissions(cb_chat_id, cb_user_id)
                if perms.has("reports"):
                    await _answer_callback(cb_id)
                    cached = _status_cache.get((cb_chat_id, cb_message_id))

                    if cb_data.startswith("stat:branch:"):
                        branch_name = cb_data[len("stat:branch:"):]
                        if cached and branch_name in cached["branches_data"]:
                            data = cached["branches_data"][branch_name]
                            card_text = format_branch_status(data)
                            back_kb = [
                                [{"text": "← Назад", "callback_data": "stat:back"},
                                 {"text": "🔄 Обновить точку", "callback_data": f"stat:refresh:{branch_name}"}],
                            ]
                            await _edit_message(cb_chat_id, cb_message_id, card_text, back_kb)
                        else:
                            await _answer_callback(cb_id, "Данные устарели, повтори /статус")

                    elif cb_data == "stat:back":
                        if cached and cached.get("summary_text"):
                            await _edit_message(cb_chat_id, cb_message_id, cached["summary_text"], cached["summary_keyboard"])
                        else:
                            await _answer_callback(cb_id, "Данные устарели, повтори /статус")

                    elif cb_data == "stat:refresh":
                        if cached:
                            branches = cached["branches"]
                            async def _safe_get_r(branch: dict) -> dict:
                                try:
                                    return await get_branch_status(branch)
                                except Exception as e:
                                    logger.error(f"[stat:refresh] [{branch['name']}]: {e}")
                                    return cached["branches_data"].get(branch["name"], {"name": branch["name"]})
                            results = await asyncio.gather(*[_safe_get_r(b) for b in branches])
                            branches_data = {d["name"]: d for d in results}
                            summary_text, summary_kb = _build_status_summary(list(results))
                            cached["summary_text"] = summary_text
                            cached["summary_keyboard"] = summary_kb
                            cached["branches_data"] = branches_data
                            await _edit_message(cb_chat_id, cb_message_id, summary_text, summary_kb)
                        else:
                            await _answer_callback(cb_id, "Данные устарели, повтори /статус")

                    elif cb_data.startswith("stat:refresh:"):
                        branch_name = cb_data[len("stat:refresh:"):]
                        if cached:
                            branch_obj = next((b for b in cached["branches"] if b["name"] == branch_name), None)
                            if branch_obj:
                                try:
                                    data = await get_branch_status(branch_obj)
                                except Exception as e:
                                    logger.error(f"[stat:refresh:branch] [{branch_name}]: {e}")
                                    data = cached["branches_data"].get(branch_name, {"name": branch_name})
                                cached["branches_data"][branch_name] = data
                                card_text = format_branch_status(data)
                                has_summary = cached.get("summary_text") is not None
                                if has_summary:
                                    back_kb = [
                                        [{"text": "← Назад", "callback_data": "stat:back"},
                                         {"text": "🔄 Обновить точку", "callback_data": f"stat:refresh:{branch_name}"}],
                                    ]
                                else:
                                    back_kb = [[{"text": "🔄 Обновить", "callback_data": f"stat:refresh:{branch_name}"}]]
                                await _edit_message(cb_chat_id, cb_message_id, card_text, back_kb)
                            else:
                                await _answer_callback(cb_id, "Точка не найдена")
                        else:
                            await _answer_callback(cb_id, "Данные устарели, повтори /статус")
                else:
                    await _answer_callback(cb_id, "🚫 Нет доступа")
            elif cb_data.startswith("srch:"):
                perms = access.get_permissions(cb_chat_id, cb_user_id)
                if perms.has("search"):
                    await _answer_callback(cb_id)
                    srch_parts = cb_data.split(":", 3)
                    if len(srch_parts) >= 4 and srch_parts[1] == "card":
                        delivery_num = srch_parts[2]
                        try:
                            row_idx = int(srch_parts[3])
                        except ValueError:
                            row_idx = 0
                        cached = _search_cache.get((cb_chat_id, cb_message_id))
                        if cached and row_idx < len(cached["rows"]):
                            r = cached["rows"][row_idx]
                            phone = (r.get("client_phone") or "").strip()
                            cnt = await get_client_order_count(phone) if phone else None
                            card_text = await _format_order_card(r, client_count=cnt)
                            back_label = cached.get("back_label", "← Назад")
                            back_kb = [[{"text": back_label, "callback_data": "srch:back"}]]
                            await _edit_message(cb_chat_id, cb_message_id, card_text, back_kb)
                        else:
                            await _handle_search(cb_chat_id, delivery_num, city_filter=perms.city)
                    elif len(srch_parts) >= 2 and srch_parts[1] == "back":
                        cached = _search_cache.get((cb_chat_id, cb_message_id))
                        if cached:
                            await _edit_message(cb_chat_id, cb_message_id, cached["text"], cached["keyboard"])
                        else:
                            await _answer_callback(cb_id, "Результаты устарели, повтори поиск")
                else:
                    await _answer_callback(cb_id, "🚫 Нет доступа")
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

        # Мульти-тенант: определяем tenant по chat_id (группа) или user_id (личка)
        try:
            from app.database_pg import get_tenant_id_for_chat
            if chat_id < 0:
                resolved = get_tenant_id_for_chat(chat_id)
            else:
                from app.database_pg import get_tenant_id_by_admin
                resolved = await get_tenant_id_by_admin(user_id)
            _ctx_tenant_id.set(resolved if resolved is not None else tenant_id)
        except Exception:
            _ctx_tenant_id.set(tenant_id)

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
                        raw_xlsx = await _download_tg_file(tg_doc.get("file_id", ""))
                        if not raw_xlsx:
                            await _send(chat_id, "❌ Не удалось скачать файл")
                        elif tbank_reconciliation.is_tbank_payout(raw_xlsx):
                            await _handle_tbank_payout(chat_id, file_name, raw_xlsx, user_id=user_id)
                        elif tbank_reconciliation.is_tbank_registry(raw_xlsx):
                            await _handle_tbank_registry(chat_id, file_name, raw_xlsx, user_id=user_id)
                    except Exception as e:
                        logger.error(f"[tbank_xlsx] {e}", exc_info=True)
                        await _send(chat_id, f"❌ Ошибка: {html.escape(str(e))}")
            continue

        # Не команда — проверяем диалог access_manager и @ mention
        if text and not text.startswith("/"):
            if access.is_admin(user_id) and chat_id > 0:
                try:
                    handled = await access_manager.handle_text(chat_id, user_id, text)
                    if handled:
                        continue
                except Exception as e:
                    logger.error(f"[access_manager] handle_text: {e}")

            # AI: @ mention бота → OpenClaw
            if _openclaw_enabled and _is_bot_mentioned(message, text):
                perms = access.get_permissions(chat_id, user_id)
                if perms.has("ai"):
                    msg_id: int = message.get("message_id", 0)
                    await _react(chat_id, msg_id, "🤔")
                    try:
                        from app.jobs.openclaw_mention import handle_mention
                        response = await handle_mention(
                            text=text,
                            user_id=user_id,
                            username=username,
                            city=perms.city,
                            is_admin=perms.is_admin,
                        )
                        if response:
                            await _reply(chat_id, msg_id, response)
                            await _react(chat_id, msg_id, "✅")
                        else:
                            await _react(chat_id, msg_id, "✅")
                    except Exception as e:
                        logger.error("[mention] unhandled error: %s", e, exc_info=True)
                        await _reply(chat_id, msg_id, "⚠️ Мозги временно недоступны.")
                        await _react(chat_id, msg_id, "❌")
            continue

        if not text.startswith("/"):
            continue

        logger.info(f"[analytics_bot] @{username} ({user_id}): {text}")

        perms = access.get_permissions(chat_id, user_id)

        if not perms.modules and not perms.is_admin:
            if chat_id > 0:
                await _send(chat_id, "🚫 Нет доступа.")
            continue

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
            await _send(chat_id, "🚫 Нет доступа к этой команде.")
            continue

        city = perms.city  # None = все города, иначе фильтруем

        try:
            if cmd in ("статус", "status"):
                await _handle_status(chat_id, arg, city_filter=city)
            elif cmd in ("повара", "cooks"):
                await _handle_staff(chat_id, arg, "cook", city_filter=city)
            elif cmd in ("курьеры", "couriers"):
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
            elif cmd in ("смены", "payment_changes"):
                await _handle_payment_changes(chat_id, arg, city_filter=city)
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
            elif cmd == "ai" and perms.is_admin:
                if arg.lower() == "on":
                    _openclaw_enabled = True
                    await _send(chat_id, "✅ AI включён (@ mention активен).")
                elif arg.lower() == "off":
                    _openclaw_enabled = False
                    await _send(chat_id, "🔕 AI выключен до рестарта или <code>/ai on</code>.")
                else:
                    status = "включён ✅" if _openclaw_enabled else "выключен 🔕"
                    await _send(chat_id, f"🤖 AI сейчас: {status}\n\n/ai on — включить\n/ai off — выключить")
            elif cmd in ("конкуренты", "competitors"):
                await _send(chat_id, "⏳ Обновляю таблицы конкурентов...")
                try:
                    from app.jobs.competitor_sheets import export_all_competitors_to_sheets
                    await export_all_competitors_to_sheets()
                    await _send(chat_id, "✅ Таблицы конкурентов обновлены.")
                except Exception as e:
                    logger.error(f"[/конкуренты] Ошибка: {e}", exc_info=True)
                    await _send(chat_id, f"❌ Ошибка при обновлении: {e}")
            elif cmd == "jobs" and perms.is_admin:
                from app.utils.job_tracker import get_jobs_status
                from datetime import timezone as _tz
                try:
                    jobs = await get_jobs_status()
                    msk = datetime.now(timezone.utc).astimezone(
                        __import__("datetime").timezone(__import__("datetime").timedelta(hours=3))
                    )
                    lines = ["📊 <b>Scheduled Jobs</b>\n"]
                    for j in jobs:
                        status = j["status"]
                        if status == "running":
                            emoji = "⏳"
                        elif status == "ok":
                            emoji = "✅"
                        elif status == "error":
                            emoji = "❌"
                        elif status == "never":
                            emoji = "🔘"
                        else:
                            emoji = "❓"
                        lines.append(f"{emoji} <b>{j['name']}</b>")
                        if status == "never":
                            lines.append("   никогда не запускался")
                        elif j["started_at"]:
                            import datetime as _dt_mod
                            msk_offset = _dt_mod.timezone(_dt_mod.timedelta(hours=3))
                            started_msk = j["started_at"].astimezone(msk_offset)
                            time_str = started_msk.strftime("%d.%m %H:%M")
                            if j["duration_sec"] is not None:
                                dur = j["duration_sec"]
                                dur_str = f"{dur} сек" if dur < 60 else f"{dur // 60} мин {dur % 60} сек"
                                lines.append(f"   {time_str} · {dur_str}")
                            else:
                                lines.append(f"   {time_str} · выполняется…")
                            if status == "error" and j["error"]:
                                lines.append(f"   💥 {j['error'][:80]}")
                        lines.append("")
                    await _send(chat_id, "\n".join(lines))
                except Exception as e:
                    logger.error(f"[/jobs] Ошибка: {e}", exc_info=True)
                    await _send(chat_id, f"❌ Ошибка: {e}")
            elif required_module is None:
                await _send(chat_id, f"❓ Неизвестная команда: /{cmd}\n\nНапиши /помощь")
        except Exception as _cmd_exc:
            logger.error(f"[/{cmd}] unhandled error: {_cmd_exc}", exc_info=True)
            await _send(chat_id, f"❌ Ошибка при выполнении /{cmd}. Подробности в логах.")


async def run_polling_loop(bot_token: str = "", tenant_id: int = 1) -> None:
    """
    Continuous polling loop для одного тенанта.
    Запускается как asyncio.Task в lifespan — по одному на каждый активный тенант.
    """
    import asyncio as _asyncio
    global _openclaw_enabled
    _openclaw_enabled = settings.openclaw_enabled  # берём из .env при старте

    token = bot_token or settings.telegram_analytics_bot_token
    label = f"tenant_id={tenant_id}"
    
    # Загружаем кэш точек для этого тенанта перед началом
    try:
        from app.database_pg import load_branches_cache
        await load_branches_cache(tenant_id)
    except Exception as e:
        logger.warning(f"Не удалось загрузить кэш точек для {label}: {e}")
    
    logger.info(f"Аркентий: polling loop started [{label}], AI={'on' if _openclaw_enabled else 'off'}")
    while True:
        try:
            await poll_analytics_bot(bot_token=token, tenant_id=tenant_id)
            await _asyncio.sleep(0.5)
        except _asyncio.CancelledError:
            logger.info(f"Аркентий: polling loop cancelled [{label}]")
            break
        except Exception as e:
            logger.error(f"Аркентий polling loop error [{label}]: {e}")
            await _asyncio.sleep(5)
