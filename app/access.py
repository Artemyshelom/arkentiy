"""
access.py — система проверки прав Аркентия.

Иерархия: admins > chats[chat_id] > users[user_id]
Hot-reload: конфиг перечитывается при изменении файла (mtime-кэш).
Hybrid fallback: если entity не найдена в конфиге → проверяем .env.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path("/app/secrets/access_config.json")

ALL_MODULES: frozenset[str] = frozenset(
    {"late_alerts", "late_queries", "search", "reports", "marketing", "finance", "admin"}
)
CITIES = ["Барнаул", "Абакан", "Томск", "Черногорск"]

MODULE_LABELS: dict[str, str] = {
    "late_alerts":  "🔴 Алерты",
    "late_queries": "📋 Запросы",
    "search":       "🔍 Поиск",
    "reports":      "📊 Отчёты",
    "marketing":    "📈 Маркетинг",
    "finance":      "💰 Финансы",
    "admin":        "🛠 Админ",
}

# (mtime, config_dict)
_cache: tuple[float, dict] | None = None


@dataclass
class Permissions:
    modules: frozenset[str] = field(default_factory=frozenset)
    city: Optional[str] = None  # None = все города
    is_admin: bool = False

    def has(self, module: str) -> bool:
        """Проверяет доступ к модулю (admin проходит всё)."""
        return self.is_admin or module in self.modules


def _load_config() -> dict:
    """Читает конфиг с диска, кэширует по mtime."""
    global _cache
    try:
        mtime = _CONFIG_PATH.stat().st_mtime
        if _cache is not None and _cache[0] == mtime:
            return _cache[1]
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        _cache = (mtime, data)
        return data
    except FileNotFoundError:
        _cache = None
        return {}
    except Exception as e:
        logger.error(f"[access] Ошибка чтения конфига: {e}")
        return _cache[1] if _cache else {}


def get_permissions(chat_id: int, user_id: int) -> Permissions:
    """
    Возвращает Permissions для пары (chat_id, user_id).

    Приоритет: admins > chats[chat_id] > users[user_id]
    Hybrid: если entity не найдена в конфиге → .env fallback.
    """
    cfg = _load_config()

    # Admins — проверяем через конфиг и .env
    if _check_admin(user_id, cfg):
        return Permissions(modules=ALL_MODULES, city=None, is_admin=True)

    # Если конфига нет — полный fallback на .env
    if not cfg:
        return _env_fallback(chat_id, user_id)

    # Проверяем чат в конфиге
    chat_cfg = cfg.get("chats", {}).get(str(chat_id))
    if chat_cfg is not None:
        return Permissions(
            modules=frozenset(chat_cfg.get("modules", [])),
            city=chat_cfg.get("city"),
        )

    # Проверяем пользователя в конфиге
    user_cfg = cfg.get("users", {}).get(str(user_id))
    if user_cfg is not None:
        return Permissions(
            modules=frozenset(user_cfg.get("modules", [])),
            city=user_cfg.get("city"),
        )

    # Не найдено в конфиге → hybrid fallback
    return _env_fallback(chat_id, user_id)


def _check_admin(user_id: int, cfg: dict) -> bool:
    """Проверяет admin-статус через конфиг и .env."""
    if user_id in cfg.get("admins", []):
        return True
    from app.config import get_settings
    return user_id == get_settings().telegram_admin_id


def _env_fallback(chat_id: int, user_id: int) -> Permissions:
    """Backward compat: права из .env переменных (для незарегистрированных entities)."""
    from app.config import get_settings
    settings = get_settings()

    allowed: set[int] = {
        int(x.strip())
        for x in (settings.telegram_allowed_ids or "").split(",")
        if x.strip().lstrip("-").isdigit()
    }

    if user_id not in allowed:
        # Проверяем маркетинг
        mkt: set[int] = {
            int(x.strip())
            for x in (settings.telegram_marketing_ids or "").split(",")
            if x.strip().lstrip("-").isdigit()
        }
        if chat_id in mkt or user_id in mkt:
            return Permissions(modules=frozenset({"marketing"}), city=None)
        return Permissions()

    # Хардкод групп (backward compat)
    _SEARCH_ONLY_GROUP = 5149932144
    _ANALYTICS_GROUP = 5262858990
    abs_str = str(abs(chat_id))
    if abs_str.endswith(str(_SEARCH_ONLY_GROUP)):
        return Permissions(modules=frozenset({"search"}), city=None)
    if abs_str.endswith(str(_ANALYTICS_GROUP)):
        return Permissions(modules=frozenset({"late_queries"}), city=None)

    # Ограничения конкретных пользователей
    _USER_RESTRICTIONS: dict[int, frozenset] = {
        874186536: frozenset({"search"}),  # ОКК Светлана
    }
    if user_id in _USER_RESTRICTIONS:
        return Permissions(modules=_USER_RESTRICTIONS[user_id], city=None)

    # По умолчанию для ALLOWED_IDS — все модули кроме finance и admin
    return Permissions(modules=ALL_MODULES - frozenset({"finance", "admin"}), city=None)


def is_admin(user_id: int) -> bool:
    """Проверяет является ли пользователь администратором."""
    cfg = _load_config()
    return _check_admin(user_id, cfg)


def get_config() -> dict:
    """Возвращает текущий конфиг (для UI)."""
    return _load_config()


def save_config(data: dict) -> None:
    """Сохраняет конфиг, создаёт бэкап, инвалидирует кэш."""
    global _cache
    if _CONFIG_PATH.exists():
        from datetime import datetime
        bak = _CONFIG_PATH.parent / f"access_config.json.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        bak.write_text(_CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        # Оставляем только 3 последних бэкапа
        baks = sorted(_CONFIG_PATH.parent.glob("access_config.json.bak.*"))
        for old in baks[:-3]:
            try:
                old.unlink()
            except Exception:
                pass
    _CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _cache = None
