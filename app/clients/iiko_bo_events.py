"""
iiko BO Events API — real-time данные о заказах и сменах.

Полная загрузка:   GET /api/events?key=TOKEN
Инкрементальная:   GET /api/events?from_rev=N&key=TOKEN
Polling каждые 30с → инкрементальный поток.
Раз в 6 часов → full reload для предотвращения дрейфа.

Экспортирует:
  poll_all_branches()          — главный APScheduler job (polling всех точек)
  job_poll_iiko_events()       — обёртка для APScheduler
  get_branch_rt(branch_name)   → dict | None
  get_branch_staff(branch_name, role) → list[dict]
  get_all_branches_staff(role)  → dict[str, list[dict]]
  _states                      — dict[str, BranchState]
  CLOSED_DELIVERY_STATUSES     — frozenset
  _parse_customer_name(raw)    → str | None
  _parse_customer_phone(raw)   → str | None
"""

import asyncio
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import httpx

from app.clients.iiko_auth import get_bo_token
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

CLOSED_DELIVERY_STATUSES = frozenset({
    "Доставлена", "Закрыта", "Отменена",
})

WAITING_STATUSES = frozenset({
    "Новая", "Не подтверждена", "Ждет отправки",
})

_COOK_ROLE_PREFIXES = ("повар", "cook", "пс", "пбт", "пов", "пз", "кп")
_COOK_ROLE_SUBSTRINGS = ("сушист", "kitchen", "помпов")  # кух убран: кухработники ≠ повара

_COURIER_ROLE_PREFIXES = ("курьер", "courier", "delivery", "кур", "крс")
_COURIER_ROLE_SUBSTRINGS = ("доставка", "k_rs")

FULL_RELOAD_INTERVAL = 6  # часов

# ---------------------------------------------------------------------------
# Модульные переменные
# ---------------------------------------------------------------------------

# Глобальный реестр состояний точек: {branch_name: BranchState}
_states: dict[str, "BranchState"] = {}
_first_poll_done: bool = False

# Кеш сотрудников: {bo_url: {user_id: {name, role, role_class}}}
_employees_global: dict[str, dict] = {}

# Время последнего full reload: {branch_name: float}
_last_full_reload: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Классификация ролей
# ---------------------------------------------------------------------------

def _classify_role(role_code: str) -> str | None:
    """Определяет role_class ('cook' | 'courier' | None) по коду роли из iiko."""
    if not role_code:
        return None
    low = role_code.lower()
    for prefix in _COOK_ROLE_PREFIXES:
        if low.startswith(prefix):
            return "cook"
    for sub in _COOK_ROLE_SUBSTRINGS:
        if sub in low:
            return "cook"
    for prefix in _COURIER_ROLE_PREFIXES:
        if low.startswith(prefix):
            return "courier"
    for sub in _COURIER_ROLE_SUBSTRINGS:
        if sub in low:
            return "courier"
    return None


# ---------------------------------------------------------------------------
# BranchState
# ---------------------------------------------------------------------------

@dataclass
class BranchState:
    bo_url: str
    branch_name: str
    bo_login: str = ""
    bo_password: str = ""
    tenant_id: int = 1
    revision: int = 0
    deliveries: dict = field(default_factory=dict)      # num → {status, courier, sum, planned_time, actual_time, ...}
    sessions: dict = field(default_factory=dict)        # user_id → {role_class, name, opened_at, closed_at}
    employees: dict = field(default_factory=dict)       # user_id → {name, role, role_class}
    cooking_statuses: dict = field(default_factory=dict) # order_num_int → "Приготовлено" | "Собран"

    @staticmethod
    def _parse_ts(s: str | None) -> "datetime | None":
        """Парсит строку timestamp из iiko событий в naive datetime."""
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("T", " ").split(".")[0]).replace(tzinfo=None)
        except Exception:
            return None

    def _cooking_status(self, delivery_num: str) -> str | None:
        """Возвращает cooking status для номера доставки (сопоставляет через int)."""
        try:
            num_int = int(re.sub(r"\D", "", delivery_num))
            return self.cooking_statuses.get(num_int)
        except (ValueError, TypeError):
            return None

    @property
    def avg_cooking_current_min(self) -> int | None:
        """Среднее время (мин) с момента создания для заказов, сейчас готовящихся на кухне."""
        now = datetime.now()
        times = [
            (now - t).total_seconds() / 60
            for num, d in self.deliveries.items()
            if d.get("status") in WAITING_STATUSES
            and self._cooking_status(str(num)) == "Приготовлено"
            for t in (self._parse_ts(d.get("opened_at")),)
            if t is not None and 0 < (now - t).total_seconds() / 60 < 120
        ]
        return round(sum(times) / len(times)) if times else None

    @property
    def avg_wait_current_min(self) -> int | None:
        """Среднее время ожидания курьера для текущих готовых (собранных) заказов."""
        now = datetime.now()
        times = [
            (now - t).total_seconds() / 60
            for num, d in self.deliveries.items()
            if d.get("status") in WAITING_STATUSES
            and self._cooking_status(str(num)) == "Собран"
            for t in (self._parse_ts(d.get("ready_time_actual")),)
            if t is not None and 0 < (now - t).total_seconds() / 60 < 120
        ]
        return round(sum(times) / len(times)) if times else None

    @property
    def avg_delivery_current_min(self) -> int | None:
        """Среднее время в пути для заказов, сейчас едущих к клиентам."""
        now = datetime.now()
        times = [
            (now - t).total_seconds() / 60
            for d in self.deliveries.values()
            if d.get("status") == "В пути к клиенту"
            for t in (self._parse_ts(d.get("sent_at")),)
            if t is not None and 0 < (now - t).total_seconds() / 60 < 120
        ]
        return round(sum(times) / len(times)) if times else None

    @property
    def active_orders(self) -> int:
        return sum(
            1 for d in self.deliveries.values()
            if d.get("status") not in CLOSED_DELIVERY_STATUSES
        )

    @property
    def orders_new(self) -> int:
        return sum(
            1 for num, d in self.deliveries.items()
            if d.get("status") in WAITING_STATUSES
            and self._cooking_status(str(num)) is None
        )

    @property
    def orders_cooking(self) -> int:
        return sum(
            1 for num, d in self.deliveries.items()
            if d.get("status") in WAITING_STATUSES
            and self._cooking_status(str(num)) == "Приготовлено"
        )

    @property
    def orders_ready(self) -> int:
        return sum(
            1 for num, d in self.deliveries.items()
            if d.get("status") in WAITING_STATUSES
            and self._cooking_status(str(num)) == "Собран"
        )

    @property
    def orders_before_dispatch(self) -> int:
        return sum(
            1 for d in self.deliveries.values()
            if d.get("status") in WAITING_STATUSES
        )

    @property
    def orders_on_way(self) -> int:
        return sum(
            1 for d in self.deliveries.values()
            if d.get("status") == "В пути к клиенту"
        )

    @property
    def delivered_today(self) -> int:
        return sum(
            1 for d in self.deliveries.values()
            if d.get("status") in ("Доставлена", "Закрыта")
        )

    @property
    def cooks_on_shift(self) -> int:
        return sum(
            1 for s in self.sessions.values()
            if s.get("role_class") == "cook" and s.get("closed_at") is None
        )

    @property
    def couriers_on_shift(self) -> int:
        return sum(
            1 for s in self.sessions.values()
            if s.get("role_class") == "courier" and s.get("closed_at") is None
        )

    @property
    def total_cooks_today(self) -> int:
        return sum(
            1 for s in self.sessions.values()
            if s.get("role_class") == "cook"
        )

    @property
    def total_couriers_today(self) -> int:
        return sum(
            1 for s in self.sessions.values()
            if s.get("role_class") == "courier"
        )

    @property
    def delay_stats(self) -> dict:
        late_count = 0
        total_delivered = 0
        delay_minutes = []

        today_local = (datetime.now(timezone.utc) + timedelta(hours=7)).date()

        for d in self.deliveries.values():
            if d.get("status") not in ("Доставлена", "Закрыта"):
                continue
            if d.get("is_self_service"):
                continue
            if "смен" in (d.get("comment") or "").lower():
                continue  # смена оплаты — iiko пересоздаёт заказ, время доставки = 0
            planned = d.get("planned_time")
            if not planned:
                continue
            # Фильтр по дате: только заказы сегодняшнего дня (UTC+7)
            try:
                planned_date = datetime.fromisoformat(planned.replace("T", " ").split(".")[0]).date()
                if planned_date != today_local:
                    continue
            except Exception:
                continue
            total_delivered += 1
            actual = d.get("actual_time")
            if actual:
                try:
                    def _parse_dt(s: str) -> datetime:
                        return datetime.fromisoformat(s.replace("T", " ").split(".")[0])
                    p = _parse_dt(planned)
                    a = _parse_dt(actual)
                    diff_min = (a - p).total_seconds() / 60
                    if diff_min > 0:
                        late_count += 1
                        delay_minutes.append(diff_min)
                except Exception:
                    pass

        avg = round(sum(delay_minutes) / len(delay_minutes)) if delay_minutes else 0
        return {
            "late_count": late_count,
            "total_delivered": total_delivered,
            "avg_delay_min": avg,
        }

    def staff_list(self, role_class: str) -> list[dict]:
        """Список персонала по роли с признаком is_active.
        Фильтрует только сессии, открытые сегодня (по локальному дню точки).
        """
        from datetime import date, datetime

        today_str = date.today().isoformat()
        result = []
        for uid, s in self.sessions.items():
            if s.get("role_class") != role_class:
                continue
            opened = s.get("opened_at") or ""
            if opened and opened[:10] < today_str:
                continue
            result.append({
                "name": s.get("name", uid),
                "opened_at": opened,
                "closed_at": s.get("closed_at"),
                "is_active": s.get("closed_at") is None,
            })
        return result

    def courier_order_stats(self) -> dict[str, dict]:
        """
        Статистика заказов по курьерам.
        Возвращает: {courier_name: {delivered, active_orders}}
        """
        stats: dict[str, dict] = {}

        # Нечёткое сопоставление: имена из deliveries → сессии
        session_tokens: dict[frozenset, str] = {}
        for s in self.sessions.values():
            if s.get("role_class") == "courier":
                name = s.get("name", "")
                if name:
                    session_tokens[frozenset(name.lower().split())] = name

        def _best_session_name(delivery_name: str) -> str:
            dtokens = frozenset(delivery_name.lower().split())
            best_name, best_score = delivery_name, 0
            for stokens, sname in session_tokens.items():
                score = len(dtokens & stokens)
                if score > best_score and score >= max(1, len(dtokens) - 1):
                    best_score, best_name = score, sname
            return best_name

        for d in self.deliveries.values():
            courier_raw = (d.get("courier") or "").strip()
            if not courier_raw:
                continue
            courier_name = _best_session_name(courier_raw)
            if courier_name not in stats:
                stats[courier_name] = {"delivered": 0, "active_orders": 0}
            if d.get("status") in ("Доставлена", "Закрыта"):
                stats[courier_name]["delivered"] += 1
            elif d.get("status") not in CLOSED_DELIVERY_STATUSES:
                stats[courier_name]["active_orders"] += 1

        return stats


# ---------------------------------------------------------------------------
# Auth (делегируем в iiko_auth — единый кеш токенов)
# ---------------------------------------------------------------------------

async def _get_token(bo_url: str, client: httpx.AsyncClient, bo_login: str = "", bo_password: str = "") -> str:
    """Обёртка: передаёт httpx client и логин точки в iiko_auth."""
    return await get_bo_token(bo_url, client=client, bo_login=bo_login or None, bo_password=bo_password or None)


# ---------------------------------------------------------------------------
# Парсинг клиентских данных
# ---------------------------------------------------------------------------

def _parse_customer_name(customer_raw: str | None) -> str | None:
    """Извлекает имя клиента из сырой строки customer_raw."""
    if not customer_raw:
        return None
    # JSON-формат: "name":"Иван"
    m = re.search(r'"name"\s*:\s*"([^"]+)"', customer_raw)
    if m:
        return m.group(1).strip()
    # Формат iiko Events: "Дарья тел. +79009211476" или "GUEST123 тел. ..."
    if " тел." in customer_raw:
        return customer_raw.split(" тел.")[0].strip()
    # Fallback: первая часть до запятой/|
    parts = re.split(r"[,|;]", customer_raw)
    name = parts[0].strip()
    return name if name else None


def _parse_customer_phone(customer_raw: str | None) -> str | None:
    """Извлекает телефон клиента из сырой строки customer_raw."""
    if not customer_raw:
        return None
    # Ищем телефонный паттерн
    m = re.search(r"(\+?7[\d\-\s]{9,13}|8[\d\-\s]{9,13}|\d{10,11})", customer_raw)
    if m:
        return m.group(1).strip()
    # Пробуем JSON-подобный формат
    m = re.search(r'"phone"\s*:\s*"([^"]+)"', customer_raw)
    if m:
        return m.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# Загрузка сотрудников (один раз при старте)
# ---------------------------------------------------------------------------

async def _load_employees(bo_url: str, client: httpx.AsyncClient, token: str) -> None:
    """Загружает справочник сотрудников (тяжёлый, ~18MB XML). Кешируется."""
    if bo_url in _employees_global:
        return
    try:
        resp = await client.get(
            f"{bo_url}/api/employees?key={token}",
            timeout=60,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        employees = {}
        for emp in root.findall(".//employee"):
            uid = emp.findtext("id", "")
            if not uid:
                continue
            deleted = emp.findtext("deleted", "false")
            if deleted == "true":
                continue
            name = emp.findtext("name", "")
            # iiko возвращает mainRoleCode, а не role
            role = emp.findtext("mainRoleCode", "") or emp.findtext("roleCodes", "") or ""
            employees[uid] = {
                "name": name,
                "role": role,
                "role_class": _classify_role(role),
            }
        _employees_global[bo_url] = employees
        logger.info(f"Загружено {len(employees)} сотрудников из {bo_url}")
    except Exception as e:
        logger.warning(f"Не удалось загрузить сотрудников из {bo_url}: {e}")
        _employees_global[bo_url] = {}


# ---------------------------------------------------------------------------
# Обработка событий
# ---------------------------------------------------------------------------

def _process_events(state: BranchState, events_xml: list, incremental: bool = False) -> None:
    """
    Обрабатывает список событий, обновляет state.
    iiko Events XML: тип в <type>, данные в <attribute><name>/<value>.
    incremental=True — включает лог замера задержки для новых заказов.
    """
    events_sorted = sorted(events_xml, key=lambda ev: ev.findtext("date", ""))

    for ev in events_sorted:
        # ФИКС: type — дочерний элемент, не атрибут
        ev_type = ev.findtext("type", "")
        # ФИКС: <attribute><name>key</name><value>val</value></attribute>
        attrs = {a.findtext("name"): a.findtext("value") for a in ev.findall("attribute")}
        ev_date = ev.findtext("date", "")

        if ev_type in ("deliveryOrderCreated", "deliveryOrderEdited"):
            num = attrs.get("deliveryNumber", "")
            if not num:
                continue
            existing = state.deliveries.get(num, {})
            # Время создания заказа — фиксируем только при первом событии
            if ev_type == "deliveryOrderCreated" and not existing.get("opened_at"):
                existing["opened_at"] = ev_date
                # Замер задержки: только при инкрементальном поллинге (не full_reload)
                if incremental and ev_date:
                    try:
                        # Парсим с сохранением timezone (fromisoformat понимает +07:00)
                        created_at_tz = datetime.fromisoformat(ev_date)
                        now_utc = datetime.now(timezone.utc)
                        # created_at_tz может быть naive (без tz) — тогда нельзя сравнивать с utc
                        if created_at_tz.tzinfo is not None:
                            lag_sec = (now_utc - created_at_tz).total_seconds()
                            logger.info(
                                f"[latency] [{state.branch_name}] NEW ORDER #{num} "
                                f"opened_at={ev_date} detected_utc={now_utc.strftime('%H:%M:%S')} "
                                f"lag={lag_sec:.0f}s"
                            )
                    except Exception:
                        pass

            if attrs.get("deliveryStatus"):
                new_status = attrs["deliveryStatus"]
                # Фиксируем момент отправки курьера
                if new_status == "В пути к клиенту" and not existing.get("sent_at"):
                    existing["sent_at"] = ev_date
                old_status = existing.get("status")
                existing["status"] = new_status
                # Замер задержки смены статуса — только инкрементально, только при смене
                if incremental and ev_date and new_status != old_status:
                    try:
                        ev_ts = datetime.fromisoformat(ev_date)
                        now_utc = datetime.now(timezone.utc)
                        if ev_ts.tzinfo is not None:
                            lag_sec = (now_utc - ev_ts).total_seconds()
                            logger.info(
                                f"[latency_status] [{state.branch_name}] ORDER #{num} "
                                f"{old_status!r} → {new_status!r} "
                                f"ev={ev_date} detected_utc={now_utc.strftime('%H:%M:%S')} "
                                f"lag={lag_sec:.0f}s"
                            )
                    except Exception:
                        pass
            if attrs.get("deliveryCourier") is not None:
                existing["courier"] = attrs.get("deliveryCourier", "")
            if attrs.get("deliverySum"):
                existing["sum"] = attrs["deliverySum"]
            if attrs.get("deliveryDate"):
                existing["planned_time"] = attrs["deliveryDate"]
            if attrs.get("deliveryActualTime"):
                existing["actual_time"] = attrs["deliveryActualTime"]
            # ФИКС: поле называется deliveryIsSelfService, значение "0E-9" = false
            if "deliveryIsSelfService" in attrs:
                val = attrs["deliveryIsSelfService"] or "0E-9"
                existing["is_self_service"] = val not in ("0E-9", "0", "", "false")
            if attrs.get("deliveryAddress"):
                existing["delivery_address"] = attrs["deliveryAddress"]
            if attrs.get("deliveryComment"):
                existing["comment"] = attrs.get("deliveryComment")
            # ФИКС: состав заказа
            if attrs.get("deliveryItems"):
                existing["items"] = attrs["deliveryItems"]
            # ФИКС: оператор
            if attrs.get("deliveryOperator"):
                existing["operator"] = attrs["deliveryOperator"]
            # ФИКС: клиент — одно поле "Имя тел. +7XXX"
            if attrs.get("deliveryCustomer"):
                existing["customer_raw"] = attrs["deliveryCustomer"]
            state.deliveries[num] = existing

        elif ev_type == "deliveryProblemChanged":
            # Событие изменения проблемы заказа — содержит полный снапшот данных доставки.
            # Обрабатываем как deliveryOrderEdited: обновляем поля если они присутствуют.
            num = attrs.get("deliveryNumber", "")
            if num:
                existing = state.deliveries.get(num, {})
                if attrs.get("deliveryStatus"):
                    existing["status"] = attrs["deliveryStatus"]
                if attrs.get("deliveryCourier") is not None:
                    existing["courier"] = attrs.get("deliveryCourier", "")
                if attrs.get("deliverySum"):
                    existing["sum"] = attrs["deliverySum"]
                if attrs.get("deliveryDate"):
                    existing["planned_time"] = attrs["deliveryDate"]
                if attrs.get("deliveryActualTime"):
                    existing["actual_time"] = attrs["deliveryActualTime"]
                if attrs.get("deliveryAddress"):
                    existing["delivery_address"] = attrs["deliveryAddress"]
                if attrs.get("deliveryItems"):
                    existing["items"] = attrs["deliveryItems"]
                if attrs.get("deliveryCustomer"):
                    existing["customer_raw"] = attrs["deliveryCustomer"]
                state.deliveries[num] = existing

        elif ev_type == "persSessionOpened":
            # ФИКС: ключ "user", а не "userId"
            uid = attrs.get("user", "")
            if not uid:
                continue
            role_name = attrs.get("roleName", "")
            # Если roleName пустой — ищем в глобальном справочнике сотрудников
            emp_data = _employees_global.get(state.bo_url, {}).get(uid, {})
            if not role_name:
                role_name = emp_data.get("role", "")
            role_class = _classify_role(role_name)
            if role_class is None:
                continue
            existing = state.sessions.get(uid, {})
            existing.update({
                "name": emp_data.get("name", uid),
                "role": role_name,
                "role_class": role_class,
                "opened_at": ev_date,  # ФИКС: дата из события
                "closed_at": None,
            })
            state.sessions[uid] = existing

        elif ev_type == "persSessionClosed":
            # ФИКС: ключ "user", а не "userId"; время — из события
            uid = attrs.get("user", "")
            if uid in state.sessions:
                state.sessions[uid]["closed_at"] = ev_date

        else:
            logger.debug(f"[events] unhandled ev_type={ev_type!r} attrs_keys={list(attrs.keys())}")

        if ev_type == "cookingStatusChangedToNext":
            order_num_str = attrs.get("orderNum", "")
            cooking_status = attrs.get("cookingStatus", "")
            if order_num_str and cooking_status:
                try:
                    num_int = int(float(order_num_str))  # приходит как "81317.000000000"
                    state.cooking_statuses[num_int] = cooking_status
                    # Замер задержки кухонного статуса
                    if incremental and ev_date:
                        try:
                            ev_ts = datetime.fromisoformat(ev_date)
                            now_utc = datetime.now(timezone.utc)
                            if ev_ts.tzinfo is not None:
                                lag_sec = (now_utc - ev_ts).total_seconds()
                                logger.info(
                                    f"[latency_cooking] [{state.branch_name}] ORDER #{num_int} "
                                    f"cooking={cooking_status!r} "
                                    f"ev={ev_date} detected_utc={now_utc.strftime('%H:%M:%S')} "
                                    f"lag={lag_sec:.0f}s"
                                )
                        except Exception:
                            pass
                    # Сохраняем timestamps этапов приготовления для расчёта опоздания самовывоза
                    if cooking_status in ("Приготовлено", "Собран"):
                        ts = ev_date or datetime.now(timezone.utc).isoformat()
                        for dnum, dd in state.deliveries.items():
                            try:
                                if int(dnum) == num_int:
                                    if cooking_status == "Приготовлено" and not dd.get("cooked_time"):
                                        dd["cooked_time"] = ts  # блюдо готово, ещё без упаковки
                                    elif cooking_status == "Собран" and not dd.get("ready_time_actual"):
                                        dd["ready_time_actual"] = ts  # упакован, готов к выдаче
                                    break
                            except (ValueError, TypeError):
                                pass
                except ValueError:
                    pass


# ---------------------------------------------------------------------------
# Сохранение в SQLite
# ---------------------------------------------------------------------------

def _delivery_to_row(branch_name: str, num: str, d: dict, now: str, ready_time_override: str | None = None) -> dict:
    """Конвертирует доставку в строку для orders_raw."""
    planned = d.get("planned_time", "")
    actual = d.get("actual_time", "")
    is_late = 0
    late_minutes = 0.0

    def _p(s: str) -> datetime:
        return datetime.fromisoformat(s.replace("T", " ").split(".")[0])

    if d.get("is_self_service") and ready_time_override and planned:
        # Самовывоз: опоздание = (готов + 5 мин) vs план
        try:
            diff = ((_p(ready_time_override) + timedelta(minutes=5)) - _p(planned)).total_seconds() / 60
            if diff > 0:
                is_late = 1
                late_minutes = round(diff, 1)
        except Exception:
            pass
    elif planned and actual:
        # Доставка: опоздание = факт - план
        try:
            diff = (_p(actual) - _p(planned)).total_seconds() / 60
            if diff > 0:
                is_late = 1
                late_minutes = round(diff, 1)
        except Exception:
            pass

    # Определение смены оплаты
    payment_changed = 0
    comment_text = (d.get("comment", "") or "").lower()
    # Признак 1: комментарий содержит "смен"
    if "смен" in comment_text:
        payment_changed = 1
    # Признак 2: курьер ждал заказ >= 120 мин, но доставка заняла < 5 мин
    # Признак искусственно завершённого заказа при смене формы оплаты
    if not payment_changed:
        try:
            ready_ts = BranchState._parse_ts(d.get("ready_time_actual"))
            sent_ts = BranchState._parse_ts(d.get("sent_at"))
            actual_ts = BranchState._parse_ts(actual)
            if ready_ts and sent_ts and actual_ts:
                idle_min = (sent_ts - ready_ts).total_seconds() / 60
                delivery_min = (actual_ts - sent_ts).total_seconds() / 60
                if idle_min >= 120 and delivery_min < 5:
                    payment_changed = 1
        except Exception:
            pass

    customer_raw = d.get("customer_raw", "")
    return {
        "branch_name": branch_name,
        "delivery_num": str(num),
        "status": d.get("status", ""),
        "courier": d.get("courier", ""),
        "sum": d.get("sum"),
        "planned_time": planned,
        "actual_time": actual,
        "is_self_service": 1 if d.get("is_self_service") else 0,
        "date": (planned or now)[:10],
        "is_late": is_late,
        "late_minutes": late_minutes,
        "client_name": _parse_customer_name(customer_raw) or "",
        "client_phone": _parse_customer_phone(customer_raw) or "",
        "delivery_address": d.get("delivery_address", ""),
        "items": d.get("items", ""),
        "cooked_time": d.get("cooked_time", ""),
        "comment": d.get("comment", ""),
        "operator": d.get("operator", ""),
        "opened_at": d.get("opened_at", ""),
        "has_problem": 0,
        "payment_type": d.get("payment_type", ""),
        "source": d.get("source", ""),
        "cancel_reason": d.get("cancel_reason", ""),
        "cancellation_details": "",
        "payment_changed": payment_changed,
        "updated_at": now,
    }


def _session_to_row(branch_name: str, uid: str, s: dict, now: str) -> dict:
    """Конвертирует сессию в строку для shifts_raw."""
    return {
        "branch_name": branch_name,
        "employee_id": uid[:36] if len(uid) > 36 else uid,
        "employee_name": s.get("name", ""),
        "role_class": s.get("role_class", ""),
        "date": (s.get("opened_at") or now)[:10],
        "clock_in": s.get("opened_at", ""),
        "clock_out": s.get("closed_at", ""),
        "updated_at": now,
    }


async def _save_to_db(state: BranchState) -> None:
    """Сохраняет текущее состояние точки в SQLite (orders_raw + shifts_raw)."""
    try:
        from app.db import upsert_orders_batch, upsert_shifts_batch
        now = datetime.now(timezone.utc).isoformat()

        def _get_ready_time(num: str) -> str | None:
            d = state.deliveries.get(str(num), {})
            # "Собран" — заказ упакован и готов к выдаче
            if d.get("ready_time_actual"):
                return d["ready_time_actual"]
            # "Приготовлено" — блюдо готово, добавим 5 мин на упаковку
            if d.get("cooked_time"):
                try:
                    ct = datetime.fromisoformat(d["cooked_time"].replace("T", " ").split(".")[0])
                    return (ct + timedelta(minutes=5)).isoformat()
                except Exception:
                    pass
            # Если текущий статус "Собран" но метка ещё не в dict (гонка)
            cs = state._cooking_status(num)
            if cs == "Собран":
                return now
            return None

        order_rows = [
            _delivery_to_row(state.branch_name, num, d, now, _get_ready_time(str(num)))
            for num, d in state.deliveries.items()
        ]
        if order_rows:
            await upsert_orders_batch(order_rows, tenant_id=state.tenant_id)

        session_rows = [
            _session_to_row(state.branch_name, uid, s, now)
            for uid, s in state.sessions.items()
        ]
        if session_rows:
            await upsert_shifts_batch(session_rows, tenant_id=state.tenant_id)
    except Exception as e:
        logger.error(f"[{state.branch_name}] Ошибка сохранения в БД: {e}")


# ---------------------------------------------------------------------------
# Full load / Incremental poll
# ---------------------------------------------------------------------------


async def _seed_sessions_from_db(state):
    try:
        from app.db import get_today_shifts
        from datetime import datetime, timezone, timedelta
        now_local = datetime.now(timezone.utc) + timedelta(hours=7)
        if now_local.hour < 6:
            date_iso = (now_local - timedelta(days=1)).date().isoformat()
        else:
            date_iso = now_local.date().isoformat()
        today_iso = now_local.date().isoformat()
        shifts = await get_today_shifts(state.branch_name, date_iso)
        is_yesterday_fallback = (date_iso != today_iso)
        if not shifts:
            prev_date = (now_local - timedelta(days=1)).date().isoformat()
            if prev_date != date_iso:
                shifts = await get_today_shifts(state.branch_name, prev_date)
                is_yesterday_fallback = True
        if not shifts:
            return
        # При загрузке вчерашних смен — пропускаем незакрытые (clock_out IS NULL).
        # Если сотрудник реально работает сегодня — он откроет новую смену через Events API.
        if is_yesterday_fallback:
            shifts = [s for s in shifts if s.get("clock_out") is not None]
        if not shifts:
            return
        # uid уже в sessions из Events (ключ без _суффикса или с _)
        existing_uids = {key.split("_")[0] for key in state.sessions}
        # Дедупликация: один uid -> лучшая смена (активная > поздняя)
        best = {}
        for s in shifts:
            uid = s.get("employee_id", "")
            if not uid or uid in existing_uids:
                continue
            prev = best.get(uid)
            if prev is None:
                best[uid] = s
            elif s.get("clock_out") is None and prev.get("clock_out") is not None:
                best[uid] = s
            elif (s.get("clock_in") or "") > (prev.get("clock_in") or ""):
                best[uid] = s
        added = 0
        for uid, s in best.items():
            state.sessions[uid] = {
                "name": s.get("employee_name", ""),
                "role": s.get("role_class", ""),
                "role_class": s.get("role_class"),
                "opened_at": s.get("clock_in", ""),
                "closed_at": s.get("clock_out"),
            }
            added += 1
        if added:
            import logging
            logging.getLogger(__name__).info(
                "[" + state.branch_name + "] Смены из БД: " + str(added) + " чел. (fallback)")
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("seed_sessions error: " + str(e))

async def _full_load(state: BranchState, client: httpx.AsyncClient) -> None:
    """Полная загрузка всех событий с начала дня."""
    token = await _get_token(state.bo_url, client, state.bo_login, state.bo_password)
    await _load_employees(state.bo_url, client, token)

    resp = await client.get(
        f"{state.bo_url}/api/events?key={token}",
        timeout=60,
    )
    resp.raise_for_status()

    root = ET.fromstring(resp.text)
    rev_elem = root.find("revision")
    max_revision = int(rev_elem.text) if rev_elem is not None else 0
    events = list(root.findall("event"))

    # Сброс состояния перед full reload
    state.deliveries.clear()
    state.sessions.clear()
    state.cooking_statuses.clear()

    _process_events(state, events)
    state.revision = max_revision
    _last_full_reload[state.branch_name] = time.time()

    logger.info(
        f"[{state.branch_name}] Full load: {len(events)} событий, revision={max_revision}"
    )
    from app.db import close_stale_shifts
    await close_stale_shifts(__import__("datetime").date.today().isoformat())
    await _seed_sessions_from_db(state)
    await _save_to_db(state)


async def _incremental_poll(state: BranchState, client: httpx.AsyncClient) -> None:
    """Инкрементальный опрос новых событий с ревизии state.revision."""
    token = await _get_token(state.bo_url, client, state.bo_login, state.bo_password)

    resp = await client.get(
        f"{state.bo_url}/api/events?from_rev={state.revision}&key={token}",
        timeout=30,
    )
    resp.raise_for_status()

    root = ET.fromstring(resp.text)
    rev_elem = root.find("revision")
    max_revision = int(rev_elem.text) if rev_elem is not None else state.revision
    events = list(root.findall("event"))

    if not events:
        return

    _process_events(state, events, incremental=True)
    state.revision = max_revision

    logger.debug(
        f"[{state.branch_name}] Incremental: +{len(events)} событий, revision={max_revision}"
    )
    await _save_to_db(state)


# ---------------------------------------------------------------------------
# Safe wrappers
# ---------------------------------------------------------------------------

async def _safe_full_load(state: BranchState, client: httpx.AsyncClient) -> None:
    try:
        await _full_load(state, client)
    except Exception as e:
        logger.error(f"[{state.branch_name}] Ошибка full load: {e}")


async def _safe_incremental(state: BranchState, client: httpx.AsyncClient) -> None:
    try:
        await _incremental_poll(state, client)
    except Exception as e:
        logger.warning(f"[{state.branch_name}] Ошибка incremental poll: {e}")


# ---------------------------------------------------------------------------
# Главный polling job
# ---------------------------------------------------------------------------

async def poll_all_branches() -> None:
    """Опрашивает все точки всех тенантов параллельно."""
    try:
        from app.db import get_all_branches as _get_all
        branches = _get_all()
    except Exception:
        branches = settings.branches
    if not branches:
        logger.warning("iiko_bo_events: branches.json пуст или не найден")
        return

    # Инициализация состояний для новых точек
    for branch in branches:
        name = branch["name"]
        bo_url = branch.get("bo_url", "")
        if not bo_url:
            continue
        if name not in _states:
            _states[name] = BranchState(
                bo_url=bo_url,
                branch_name=name,
                bo_login=branch.get("bo_login", ""),
                bo_password=branch.get("bo_password", ""),
                tenant_id=branch.get("tenant_id", 1),
            )

    async def _poll_branch(branch: dict) -> None:
        name = branch["name"]
        bo_url = branch.get("bo_url", "")
        if not bo_url or name not in _states:
            return
        state = _states[name]
        async with httpx.AsyncClient(verify=False, timeout=60) as client:
            need_full = (
                state.revision == 0
                or (time.time() - _last_full_reload.get(name, 0)) > FULL_RELOAD_INTERVAL * 3600
            )
            if need_full:
                await _safe_full_load(state, client)
            else:
                await _safe_incremental(state, client)

    await asyncio.gather(*[_poll_branch(b) for b in branches], return_exceptions=True)

    global _first_poll_done
    _first_poll_done = True


def is_events_loaded() -> bool:
    """True если хотя бы один полный polling-цикл завершён после старта."""
    return _first_poll_done


async def job_poll_iiko_events() -> None:
    """APScheduler обёртка для poll_all_branches."""
    try:
        await poll_all_branches()
    except Exception as e:
        logger.error(f"Ошибка в job_poll_iiko_events: {e}")


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

def get_branch_rt(branch_name: str) -> dict | None:
    """
    Возвращает словарь RT-данных точки или None если данные ещё не загружены.
    """
    state = _states.get(branch_name)
    if state is None:
        # Вернуть пустой словарь вместо None если статс ещё не загружен
        return {
            "active_orders": 0,
            "delivered_today": 0,
            "orders_new": 0,
            "orders_before_dispatch": 0,
            "orders_cooking": 0,
            "orders_ready": 0,
            "orders_on_way": 0,
            "couriers_on_shift": 0,
            "cooks_on_shift": 0,
            "total_cooks_today": 0,
            "total_couriers_today": 0,
            "delays": None,
            "avg_cooking_min": None,
            "avg_wait_min": None,
            "avg_delivery_min": None,
        }
    if state.revision == 0:
        return None
    ds = state.delay_stats
    return {
        "active_orders": state.active_orders,
        "delivered_today": state.delivered_today,
        "orders_new": state.orders_new,
        "orders_before_dispatch": state.orders_before_dispatch,
        "orders_cooking": state.orders_cooking,
        "orders_ready": state.orders_ready,
        "orders_on_way": state.orders_on_way,
        "couriers_on_shift": state.couriers_on_shift,
        "cooks_on_shift": state.cooks_on_shift,
        "total_cooks_today": state.total_cooks_today,
        "total_couriers_today": state.total_couriers_today,
        "delays": ds,
        "avg_cooking_min": state.avg_cooking_current_min,
        "avg_wait_min": state.avg_wait_current_min,
        "avg_delivery_min": state.avg_delivery_current_min,
    }


def get_branch_staff(branch_name: str, role: str) -> list[dict] | None:
    """
    Возвращает список персонала точки по роли ('cook' | 'courier').
    None если данные ещё не загружены.
    """
    state = _states.get(branch_name)
    if state is None or state.revision == 0:
        return None

    staff = state.staff_list(role)

    if role == "courier":
        order_stats = state.courier_order_stats()
        for s in staff:
            name = s["name"]
            stats = order_stats.get(name, {"delivered": 0, "active_orders": 0})
            s["delivered"] = stats["delivered"]
            s["active_orders"] = stats["active_orders"]

    return staff


def get_all_branches_staff(role: str) -> dict[str, list[dict]]:
    """
    Возвращает {branch_name: [staff_list]} для всех загруженных точек.
    Если точка ещё не загружена — возвращает пустой список для точки.
    """
    result = {}
    for name, state in _states.items():
        if state.revision == 0:
            # Вернуть пустой список вместо пропуска
            result[name] = []
            continue
        staff = get_branch_staff(name, role)
        if staff is not None:
            result[name] = staff
        else:
            result[name] = []
    return result
