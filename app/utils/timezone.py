"""Общие функции для работы с таймзонами точек."""

from datetime import datetime, timedelta, timezone

from app.config import get_settings


def branch_tz(branch: dict) -> timezone:
    """Возвращает timezone для точки по utc_offset (default=7)."""
    offset = branch.get("utc_offset", 7)
    return timezone(timedelta(hours=offset))


def tz_from_offset(utc_offset: int) -> timezone:
    """Возвращает timezone по числовому UTC offset."""
    return timezone(timedelta(hours=utc_offset))


def now_local(tz: timezone | None = None) -> datetime:
    """Текущее время в заданной таймзоне (или default из settings)."""
    if tz is None:
        tz = get_settings().default_tz
    return datetime.now(tz)
