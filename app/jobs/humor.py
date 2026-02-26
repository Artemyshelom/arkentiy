"""
humor.py — LLM-генерация одной короткой реплики для утреннего отчёта.

Модель: anthropic/claude-3-5-haiku через OpenRouter (тот же ключ что у /выгрузка).
Таймаут: 5 секунд. При ошибке → None (отчёт не ломается).
"""

import logging
import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"

_SYSTEM_PROMPT = """\
Ты — Аркентий, бот-аналитик сети доставки суши. Ироничный, лаконичный, иногда дерзкий.
Получаешь данные за вчерашний день по одной точке и пишешь одну короткую реплику — \
максимум 1-2 предложения.

Правила:
- Без восклицательных знаков
- Не начинать с «Отлично», «Неплохо», «Хорошо», «Молодцы»
- Не объяснять шутку после неё
- Иногда (редко, не каждый раз) можно вставить «фа», «втфа», «шнейне» или «пепе» \
как финальный аккорд — без объяснений, просто вставить. Не чаще 1 раза из 6.
- Цепляйся за конкретную цифру и разворачивай неожиданно

Примеры:
Данные: Барнаул_1, выручка 210к, 91 чек, 0% опозданий
→ Ноль опозданий. Фа.

Данные: Томск_1, выручка 140к, 54 чека, 34% опозданий
→ 34% опозданий. Могли доехать вовремя. Мог бы быть хороший день.

Данные: Черногорск, выручка 67к, 28 чеков, 8% опозданий
→ 28 чеков. Всё в пешей доступности от рекорда.

Данные: Абакан, выручка 310к, 118 чеков, 5% опозданий
→ 310 тысяч. Очень шнейне, Абакан.

Данные: Барнаул_2, выручка 155к, 63 чека, 12% опозданий
→ Вторник. 12% опозданий. Всё в норме, если считать это нормой.

Данные: Томск_2, выручка 98к, 39 чеков, 0% опозданий
→ Тихий день. Суши добрались. Пепе доволен.
"""


async def get_morning_quip(
    branch: str,
    rev: float,
    chk: int,
    late_pct: float,
    avg_late_min: float,
) -> str | None:
    """
    Генерирует короткую реплику для утреннего отчёта через OpenRouter.
    Возвращает None если API недоступен или ключ не настроен.
    """
    api_key = settings.openrouter_api_key
    if not api_key:
        return None

    model = settings.humor_model or "anthropic/claude-3-5-haiku"

    rev_k = round(rev / 1000, 1)
    user_msg = (
        f"Данные: {branch}, выручка {rev_k}к, {chk} чеков, "
        f"{late_pct:.0f}% опозданий"
        + (f", среднее опоздание {avg_late_min:.0f} мин" if avg_late_min > 0 else "")
    )

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(
                f"{_OPENROUTER_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://ebidoebi.ru",
                    "X-Title": "Arkentiy Morning Report",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": user_msg},
                    ],
                    "max_tokens": 80,
                    "temperature": 0.9,
                },
            )
            r.raise_for_status()
        quip = r.json()["choices"][0]["message"]["content"].strip()
        logger.info(f"[humor] {branch}: {quip!r}")
        return quip if quip else None
    except Exception as e:
        logger.warning(f"[humor] quip failed for {branch}: {e}")
        return None
