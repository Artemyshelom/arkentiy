"""Общие функции для работы с таймзонами точек."""

from datetime import datetime, timedelta, timezone

from app.config import get_settings

# Таймзона по умолчанию для всех точек (UTC+7, Krasnoyarsk, без DST с 2014).
# Используется как дефолт до появления per-branch tz в multi-tenant.
DEFAULT_TZ = timezone(timedelta(hours=7))


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


def utc_hour_to_local_bounds(
    hour_utc: datetime,
    tz: timezone = DEFAULT_TZ,
) -> tuple[datetime, datetime]:
    """Конвертирует UTC-час в naive local границы для сравнения с TEXT-timestamps.

    Нужно для WHERE-clauses, где opened_at / clock_in хранятся как local naive text.
    Возвращает (hs_naive_local, he_naive_local).
    """
    local = hour_utc.astimezone(tz)
    hs = local.replace(tzinfo=None)
    he = (local + timedelta(hours=1)).replace(tzinfo=None)
    return hs, he
