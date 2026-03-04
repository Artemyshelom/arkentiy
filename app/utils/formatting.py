"""Общие функции форматирования для Telegram-отчётов."""


def fmt_money(v) -> str:
    """Форматирует число как сумму в рублях: 12 345 ₽."""
    try:
        return f"{int(v):,} ₽".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def fmt_num(v) -> str:
    """Форматирует число без дробной части."""
    try:
        return str(int(v))
    except (TypeError, ValueError):
        return "—"


def fmt_pct(v) -> str:
    """Форматирует число как процент: 12.3%."""
    try:
        return f"{float(v):.1f}%"
    except (TypeError, ValueError):
        return "—"
