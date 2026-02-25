"""
access_manager.py — Telegram UI для управления доступом.

Команды:
  /доступ — открыть панель управления (только admins, только в личке)

Callbacks prefix: "ac:"
  ac:main             — главный экран
  ac:c:<chat_id>      — настройки чата
  ac:tm:<cid>:<mod>   — toggle модуля для чата
  ac:ty:<cid>:<city>  — установить город для чата (null = все)
  ac:cd:<cid>         — удалить чат
  ac:users            — список пользователей
  ac:u:<user_id>      — настройки пользователя
  ac:um:<uid>:<mod>   — toggle модуля для пользователя
  ac:uy:<uid>:<city>  — установить город для пользователя
  ac:ud:<uid>         — удалить пользователя
  ac:addchat          — добавить новый чат (диалог)
  ac:adduser          — добавить нового пользователя (диалог)
  ac:rg:<chat_id>     — зарегистрировать чат из автодетекта
  ac:ig:<chat_id>     — игнорировать чат из автодетекта
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

from app import access
from app import database as _db
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

CITIES = access.CITIES

_MODULE_META: list[tuple[str, str]] = [
    ("late_alerts",  "🔴 Алерты"),
    ("late_queries", "📋 Опоздания"),
    ("search",       "🔍 Поиск"),
    ("reports",      "📊 Отчёты"),
    ("marketing",    "📈 Выгрузка данных"),
    ("finance",      "💰 Финансы"),
    ("audit",        "🔍 Аудит"),
    ("admin",        "🛠 Админ"),
]

# Короткие метки для отображения в списке
_MOD_SHORT: dict[str, str] = {
    "late_alerts":  "Алерты",
    "late_queries": "Опоздания",
    "search":       "Поиск",
    "reports":      "Отчёты",
    "marketing":    "Выгрузка",
    "finance":      "Финансы",
    "audit":        "Аудит",
    "admin":        "Админ",
}

# In-memory состояния диалогов: {user_id: {action, step, ...}}
_pending: dict[int, dict] = {}


def _parse_city_raw(city_val: str | None) -> frozenset | None:
    """Парсит city из DB/config: null→None, JSON-массив→frozenset, строка→{строка}."""
    if city_val is None:
        return None
    try:
        parsed = json.loads(city_val)
        if isinstance(parsed, list):
            return frozenset(parsed) if parsed else None
    except (json.JSONDecodeError, ValueError):
        pass
    return frozenset({city_val})


def _serialize_cities(cities: frozenset | None) -> str | None:
    """Сериализует frozenset городов для хранения в DB."""
    if cities is None:
        return None
    return json.dumps(sorted(cities))


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def _bot_url() -> str:
    return f"https://api.telegram.org/bot{settings.telegram_analytics_bot_token}"


async def _send(chat_id: int, text: str, keyboard: list | None = None) -> Optional[int]:
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{_bot_url()}/sendMessage", json=payload)
            data = r.json()
            if data.get("ok"):
                return data["result"]["message_id"]
            logger.debug(f"[access_manager] send failed: {data.get('description')}")
    except Exception as e:
        logger.error(f"[access_manager] _send: {e}")
    return None


async def _edit(chat_id: int, message_id: int, text: str, keyboard: list | None = None) -> None:
    payload: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": keyboard or []},
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{_bot_url()}/editMessageText", json=payload)
            if not r.json().get("ok"):
                logger.debug(f"[access_manager] edit failed: {r.text[:100]}")
    except Exception as e:
        logger.error(f"[access_manager] _edit: {e}")


async def _answer_cb(cb_id: str, text: str = "", alert: bool = False) -> None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"{_bot_url()}/answerCallbackQuery",
                json={"callback_query_id": cb_id, "text": text, "show_alert": alert},
            )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Screen builders
# ---------------------------------------------------------------------------

def _main_screen() -> tuple[str, list]:
    """Главный экран — чаты сгруппированы по городам."""
    cfg = access.get_config()
    chats: dict = cfg.get("chats", {})

    by_city: dict[str, list] = {c: [] for c in CITIES}
    by_city["_all"] = []

    for cid_str, cdata in chats.items():
        cities = _parse_city_raw(cdata.get("city"))
        # Один конкретный город → в секцию этого города
        if cities is not None and len(cities) == 1:
            city = next(iter(cities))
            key = city if city in CITIES else "_all"
        else:
            # None (все) или несколько городов → в "ВСЕ ГОРОДА / НЕСКОЛЬКО"
            key = "_all"
        by_city[key].append((cid_str, cdata))

    def _chat_line(cdata: dict, show_cities: bool = False) -> str:
        mods = cdata.get("modules", [])
        mod_part = ", ".join(_MOD_SHORT.get(m, m) for m in mods) if mods else "—"
        if show_cities:
            cities = _parse_city_raw(cdata.get("city"))
            city_part = "все" if cities is None else ", ".join(sorted(cities))
            return f"[{city_part}] ({mod_part})"
        return f"({mod_part})"

    lines = ["📋 <b>Управление доступом</b>\n"]
    keyboard: list = []

    for city in CITIES:
        items = by_city[city]
        if not items:
            continue
        lines.append(f"<b>{city.upper()}</b>")
        for cid, cdata in items:
            name = cdata.get("name", cid)
            lines.append(f"  📍 {name} {_chat_line(cdata)}")
            keyboard.append([{"text": f"⚙️ {name}", "callback_data": f"ac:c:{cid}"}])

    all_items = by_city["_all"]
    if all_items:
        lines.append("<b>ВСЕ ГОРОДА</b>")
        for cid, cdata in all_items:
            name = cdata.get("name", cid)
            lines.append(f"  📍 {name} {_chat_line(cdata, show_cities=True)}")
            keyboard.append([{"text": f"⚙️ {name}", "callback_data": f"ac:c:{cid}"}])

    users = cfg.get("users", {})
    lines.append(f"\n💬 Чатов: {len(chats)}  👤 Пользователей: {len(users)}")

    keyboard.append([
        {"text": "➕ Добавить чат", "callback_data": "ac:addchat"},
        {"text": "👤 Пользователи", "callback_data": "ac:users"},
    ])

    return "\n".join(lines), keyboard


def _chat_screen(cid_str: str) -> tuple[str, list]:
    """Экран настроек чата."""
    cfg = access.get_config()
    chat = cfg.get("chats", {}).get(cid_str, {})
    modules = set(chat.get("modules", []))
    current_cities = _parse_city_raw(chat.get("city"))
    name = chat.get("name", cid_str)

    city_display = "Все" if current_cities is None else ", ".join(sorted(current_cities))
    text = (
        f"⚙️ <b>{name}</b>\n"
        f"ID: <code>{cid_str}</code>\n"
        f"Города: {city_display}\n\n"
        f"<b>Модули:</b>"
    )

    keyboard: list = []
    row: list = []
    for mod_id, mod_label in _MODULE_META:
        icon = "✅" if mod_id in modules else "❌"
        row.append({"text": f"{icon} {mod_label}", "callback_data": f"ac:tm:{cid_str}:{mod_id}"})
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # Выбор городов — чекбоксы, по 2 в ряд
    city_row: list = []
    for c in CITIES:
        mark = "✅" if current_cities is not None and c in current_cities else "○"
        city_row.append({"text": f"{mark} {c}", "callback_data": f"ac:ty:{cid_str}:{c}"})
        if len(city_row) == 2:
            keyboard.append(city_row)
            city_row = []
    if city_row:
        keyboard.append(city_row)
    all_mark = "●" if current_cities is None else "○"
    keyboard.append([{"text": f"{all_mark} Все города", "callback_data": f"ac:ty:{cid_str}:null"}])

    keyboard.append([
        {"text": "🗑 Удалить", "callback_data": f"ac:cd:{cid_str}"},
        {"text": "← Назад", "callback_data": "ac:main"},
    ])

    return text, keyboard


def _users_screen() -> tuple[str, list]:
    """Список пользователей."""
    cfg = access.get_config()
    users: dict = cfg.get("users", {})

    lines = ["👤 <b>Пользователи</b>\n"]
    keyboard: list = []

    if not users:
        lines.append("Пользователей нет.")
    else:
        for uid_str, udata in users.items():
            name = udata.get("name", uid_str)
            mods = ", ".join(udata.get("modules", [])) or "—"
            city = udata.get("city") or "Все"
            lines.append(f"• <b>{name}</b>\n  <code>{uid_str}</code> | {city} | {mods}")
            keyboard.append([{"text": f"⚙️ {name}", "callback_data": f"ac:u:{uid_str}"}])

    keyboard.append([
        {"text": "➕ Добавить", "callback_data": "ac:adduser"},
        {"text": "← Назад", "callback_data": "ac:main"},
    ])
    return "\n".join(lines), keyboard


def _user_screen(uid_str: str) -> tuple[str, list]:
    """Экран настроек пользователя."""
    cfg = access.get_config()
    user = cfg.get("users", {}).get(uid_str, {})
    modules = set(user.get("modules", []))
    current_cities = _parse_city_raw(user.get("city"))
    name = user.get("name", uid_str)

    city_display = "Все" if current_cities is None else ", ".join(sorted(current_cities))
    text = (
        f"⚙️ <b>{name}</b>\n"
        f"ID: <code>{uid_str}</code>\n"
        f"Города: {city_display}\n\n"
        f"<b>Модули:</b>"
    )

    keyboard: list = []
    row: list = []
    for mod_id, mod_label in _MODULE_META:
        icon = "✅" if mod_id in modules else "❌"
        row.append({"text": f"{icon} {mod_label}", "callback_data": f"ac:um:{uid_str}:{mod_id}"})
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # Чекбоксы городов, по 2 в ряд
    city_row: list = []
    for c in CITIES:
        mark = "✅" if current_cities is not None and c in current_cities else "○"
        city_row.append({"text": f"{mark} {c}", "callback_data": f"ac:uy:{uid_str}:{c}"})
        if len(city_row) == 2:
            keyboard.append(city_row)
            city_row = []
    if city_row:
        keyboard.append(city_row)
    all_mark = "●" if current_cities is None else "○"
    keyboard.append([{"text": f"{all_mark} Все города", "callback_data": f"ac:uy:{uid_str}:null"}])

    keyboard.append([
        {"text": "🗑 Удалить", "callback_data": f"ac:ud:{uid_str}"},
        {"text": "← Назад", "callback_data": "ac:users"},
    ])
    return text, keyboard


# ---------------------------------------------------------------------------
# Config mutations (async — пишут в БД, обновляют in-memory кэш access.py)
# ---------------------------------------------------------------------------

async def _refresh_cache() -> None:
    """Перечитывает конфиг из БД и обновляет in-memory кэш access.py."""
    cfg = await _db.get_access_config_from_db()
    access.update_db_cache(cfg)


async def _toggle_module(section: str, key_str: str, module: str) -> None:
    cfg = access.get_config()
    entry = cfg.get(section, {}).get(key_str, {"name": key_str, "modules": [], "city": None})
    mods = set(entry.get("modules", []))
    mods.discard(module) if module in mods else mods.add(module)
    modules = sorted(mods)
    name = entry.get("name", key_str)
    city = entry.get("city")
    if section == "chats":
        await _db.upsert_tenant_chat(int(key_str), name, modules, city)
    else:
        await _db.upsert_tenant_user(int(key_str), name, modules, city)
    await _refresh_cache()


async def _toggle_city(section: str, key_str: str, city: str | None) -> None:
    """Переключает город для чата/пользователя.
    city=None → сбрасываем в «все города»
    city=строка → добавляем если нет, убираем если есть.
    """
    cfg = access.get_config()
    entry = cfg.get(section, {}).get(key_str, {"name": key_str, "modules": [], "city": None})
    modules = entry.get("modules", [])
    name = entry.get("name", key_str)

    if city is None:
        new_city_db = None
    else:
        current = _parse_city_raw(entry.get("city"))
        if current is None:
            new_cities = frozenset({city})
        elif city in current:
            remaining = current - {city}
            new_cities = remaining if remaining else None
        else:
            new_cities = current | {city}
        new_city_db = _serialize_cities(new_cities) if new_cities is not None else None

    if section == "chats":
        await _db.upsert_tenant_chat(int(key_str), name, modules, new_city_db)
    else:
        await _db.upsert_tenant_user(int(key_str), name, modules, new_city_db)
    await _refresh_cache()


async def _delete_entry(section: str, key_str: str) -> None:
    if section == "chats":
        await _db.delete_tenant_chat(int(key_str))
    else:
        await _db.delete_tenant_user(int(key_str))
    await _refresh_cache()


async def _register_chat(cid_str: str, name: str) -> None:
    cfg = access.get_config()
    if cid_str not in cfg.get("chats", {}):
        await _db.upsert_tenant_chat(int(cid_str), name, [], None)
        await _refresh_cache()


async def _register_user(uid_str: str, name: str) -> None:
    await _db.upsert_tenant_user(int(uid_str), name, [], None)
    await _refresh_cache()


# ---------------------------------------------------------------------------
# Public handlers
# ---------------------------------------------------------------------------

async def handle_command(chat_id: int, user_id: int) -> None:
    """/доступ — открывает панель управления."""
    if not access.is_admin(user_id):
        await _send(chat_id, "🚫 Команда только для администраторов.")
        return
    if chat_id <= 0:
        await _send(chat_id, "⚙️ Управление доступом — только в личных сообщениях.")
        return

    # Если конфига нет — создаём минимальный
    if not access.get_config():
        from app.config import get_settings as _gs
        _s = _gs()
        access.save_config({
            "admins": [_s.telegram_admin_id],
            "chats": {},
            "users": {},
        })

    text, kb = _main_screen()
    await _send(chat_id, text, kb)


async def handle_callback(
    cb_id: str,
    user_id: int,
    chat_id: int,
    message_id: int,
    data: str,
) -> None:
    """Обрабатывает callback_query с префиксом 'ac:'."""
    if not access.is_admin(user_id):
        await _answer_cb(cb_id, "🚫 Нет доступа", alert=True)
        return

    # Формат: "ac:action:arg1:arg2" — split с лимитом 4 части
    parts = data.split(":", 3)
    action = parts[1] if len(parts) > 1 else ""

    # Для действий с переключением — _answer_cb вызывается внутри с осмысленным текстом.
    # Для навигационных действий — вызываем здесь с пустым текстом (снимает "часики").
    _nav_actions = {"main", "c", "users", "u", "addchat", "adduser", "rg", "ig"}
    if action in _nav_actions:
        await _answer_cb(cb_id)

    if action == "main":
        text, kb = _main_screen()
        await _edit(chat_id, message_id, text, kb)

    elif action == "c" and len(parts) >= 3:
        text, kb = _chat_screen(parts[2])
        await _edit(chat_id, message_id, text, kb)

    elif action == "tm" and len(parts) >= 4:
        await _toggle_module("chats", parts[2], parts[3])
        mod_label = dict(_MODULE_META).get(parts[3], parts[3])
        cfg = access.get_config()
        mods = set(cfg.get("chats", {}).get(parts[2], {}).get("modules", []))
        state = "включён ✅" if parts[3] in mods else "выключен ❌"
        await _answer_cb(cb_id, f"{mod_label} {state}", alert=True)
        text, kb = _chat_screen(parts[2])
        await _edit(chat_id, message_id, text, kb)
        return

    elif action == "ty" and len(parts) >= 4:
        city_val = None if parts[3] == "null" else parts[3]
        await _toggle_city("chats", parts[2], city_val)
        cfg = access.get_config()
        raw = cfg.get("chats", {}).get(parts[2], {}).get("city")
        cur = _parse_city_raw(raw)
        city_label = "Все города" if cur is None else ", ".join(sorted(cur))
        await _answer_cb(cb_id, f"Город: {city_label} ✅", alert=True)
        text, kb = _chat_screen(parts[2])
        await _edit(chat_id, message_id, text, kb)
        return

    elif action == "cd" and len(parts) >= 3:
        cfg = access.get_config()
        chat_name = cfg.get("chats", {}).get(parts[2], {}).get("name", parts[2])
        await _delete_entry("chats", parts[2])
        await _answer_cb(cb_id, f"«{chat_name}» удалён", alert=True)
        text, kb = _main_screen()
        await _edit(chat_id, message_id, text, kb)
        return

    elif action == "users":
        text, kb = _users_screen()
        await _edit(chat_id, message_id, text, kb)

    elif action == "u" and len(parts) >= 3:
        text, kb = _user_screen(parts[2])
        await _edit(chat_id, message_id, text, kb)

    elif action == "um" and len(parts) >= 4:
        await _toggle_module("users", parts[2], parts[3])
        mod_label = dict(_MODULE_META).get(parts[3], parts[3])
        cfg = access.get_config()
        mods = set(cfg.get("users", {}).get(parts[2], {}).get("modules", []))
        state = "включён ✅" if parts[3] in mods else "выключен ❌"
        await _answer_cb(cb_id, f"{mod_label} {state}", alert=True)
        text, kb = _user_screen(parts[2])
        await _edit(chat_id, message_id, text, kb)
        return

    elif action == "uy" and len(parts) >= 4:
        city_val = None if parts[3] == "null" else parts[3]
        await _toggle_city("users", parts[2], city_val)
        cfg = access.get_config()
        raw = cfg.get("users", {}).get(parts[2], {}).get("city")
        cur = _parse_city_raw(raw)
        city_label = "Все города" if cur is None else ", ".join(sorted(cur))
        await _answer_cb(cb_id, f"Город: {city_label} ✅", alert=True)
        text, kb = _user_screen(parts[2])
        await _edit(chat_id, message_id, text, kb)
        return

    elif action == "ud" and len(parts) >= 3:
        cfg = access.get_config()
        user_name = cfg.get("users", {}).get(parts[2], {}).get("name", parts[2])
        await _delete_entry("users", parts[2])
        await _answer_cb(cb_id, f"«{user_name}» удалён", alert=True)
        text, kb = _users_screen()
        await _edit(chat_id, message_id, text, kb)
        return

    elif action == "addchat":
        _pending[user_id] = {"action": "add_chat", "step": "await_id"}
        await _edit(
            chat_id, message_id,
            "➕ <b>Добавить чат</b>\n\n"
            "Отправь ID чата (например: <code>-1001234567890</code>)\n\n"
            "<i>Совет: узнать ID можно через @userinfobot или из логов бота</i>",
            [[{"text": "← Отмена", "callback_data": "ac:main"}]],
        )

    elif action == "adduser":
        _pending[user_id] = {"action": "add_user", "step": "await_id"}
        await _edit(
            chat_id, message_id,
            "➕ <b>Добавить пользователя</b>\n\n"
            "Отправь Telegram ID пользователя\n\n"
            "<i>Совет: узнать ID можно через @userinfobot</i>",
            [[{"text": "← Отмена", "callback_data": "ac:main"}]],
        )

    elif action == "rg" and len(parts) >= 3:
        cid_str = parts[2]
        await _register_chat(cid_str, f"Чат {cid_str}")
        text, kb = _chat_screen(cid_str)
        await _edit(chat_id, message_id, text, kb)

    elif action == "ig":
        await _edit(chat_id, message_id, "✅ Чат проигнорирован.", [])


async def handle_text(chat_id: int, user_id: int, text: str) -> bool:
    """
    Обрабатывает текстовые сообщения в рамках диалога (добавление чата/юзера).
    Возвращает True если сообщение обработано (не передавать дальше).
    """
    state = _pending.get(user_id)
    if not state:
        return False

    if state["action"] == "add_chat":
        if state["step"] == "await_id":
            try:
                cid = int(text.strip())
            except ValueError:
                await _send(chat_id, "❌ Некорректный ID. Попробуй снова или /доступ для отмены.")
                return True
            state["step"] = "await_name"
            state["chat_id"] = str(cid)
            await _send(chat_id, f"ID принят: <code>{cid}</code>\n\nТеперь отправь название чата (например: «Опоздания Томск»)")
            return True

        if state["step"] == "await_name":
            name = text.strip()
            await _register_chat(state["chat_id"], name)
            _pending.pop(user_id, None)
            t, kb = _chat_screen(state["chat_id"])
            await _send(chat_id, f"✅ Чат «{name}» добавлен. Настрой модули:\n\n{t}", kb)
            return True

    if state["action"] == "add_user":
        if state["step"] == "await_id":
            try:
                uid = int(text.strip())
            except ValueError:
                await _send(chat_id, "❌ Некорректный ID. Попробуй снова или /доступ для отмены.")
                return True
            state["step"] = "await_name"
            state["user_id"] = str(uid)
            await _send(chat_id, f"ID принят: <code>{uid}</code>\n\nТеперь отправь имя пользователя (например: «Маркетолог Аня»)")
            return True

        if state["step"] == "await_name":
            name = text.strip()
            await _register_user(state["user_id"], name)
            _pending.pop(user_id, None)
            t, kb = _user_screen(state["user_id"])
            await _send(chat_id, f"✅ Пользователь «{name}» добавлен. Настрой модули:\n\n{t}", kb)
            return True

    return False


async def notify_new_chat(admin_id: int, chat_id: int, chat_title: str) -> None:
    """Уведомляет admin о добавлении бота в новый чат."""
    cid_str = str(chat_id)
    text = (
        f"⚠️ <b>Бот добавлен в новый чат</b>\n"
        f"«{chat_title}»\n"
        f"ID: <code>{cid_str}</code>"
    )
    keyboard = [[
        {"text": "Зарегистрировать", "callback_data": f"ac:rg:{cid_str}"},
        {"text": "Игнорировать", "callback_data": f"ac:ig:{cid_str}"},
    ]]
    await _send(admin_id, text, keyboard)
