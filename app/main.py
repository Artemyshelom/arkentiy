"""
Точка входа: FastAPI + APScheduler.
Все задачи по расписанию регистрируются здесь.
"""

import asyncio
import logging
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, Request, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from datetime import datetime
from app.clients import telegram
from app.config import get_settings
from app.db import (
    init_db, get_access_config_from_db,
    get_all_tenant_users, get_all_tenant_chats,
    upsert_tenant_user, upsert_tenant_chat,
    get_active_tenants_with_tokens,
)
from app.services import access as _access
from app.monitoring.healthcheck import router as health_router
from app.webhooks.bitrix import router as bitrix_router
from app.routers.cabinet import router as cabinet_router
from app.routers.onboarding import router as onboarding_router
from app.routers.payments import router as payments_router
from app.routers.auth import router as auth_router
from app.routers.stats import router as stats_router

# Импорт задач
from app.jobs.iiko_to_sheets import job_export_iiko_to_sheets
from app.jobs.olap_enrichment import job_olap_enrichment
from app.clients.iiko_bo_events import job_poll_iiko_events
from app.jobs.competitor_monitor import job_monitor_competitors
from app.jobs.arkentiy import poll_analytics_bot, run_polling_loop
from app.jobs.late_alerts import job_late_alerts
from app.jobs.daily_report import job_send_morning_report
from app.jobs.weekly_report import job_weekly_report
from app.jobs.audit import job_audit_report
from app.jobs.cancel_sync import job_cancel_sync
from app.jobs.billing import job_recurring_billing
from app.jobs.subscription_lifecycle import job_trial_expiry, job_payment_grace
from app.utils.tenant import run_for_all_tenants

settings = get_settings()

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="Europe/Moscow")


def register_jobs() -> None:
    """Регистрирует все задачи в планировщике."""

    # iiko Events API (real-time заказы): каждые 30 секунд
    scheduler.add_job(
        job_poll_iiko_events,
        trigger=IntervalTrigger(seconds=30),
        id="iiko_events",
        name="iiko события (Events API)",
        replace_existing=True,
        misfire_grace_time=15,
    )


    # OLAP enrichment orders_raw: каждый час в :00, за 25 мин до утреннего отчёта.
    # Обогащает только тенантов, у которых сейчас 09:00 местного.
    async def _olap_enrichment_by_tz():
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from app.db import get_all_branches
        from app.ctx import ctx_tenant_id
        msk_now = _dt.now(_tz(_td(hours=3)))
        target_offset = 12 - msk_now.hour
        all_branches = get_all_branches()
        # Собираем tenant_id, у которых есть точки с нужным offset
        tenant_ids = {b["tenant_id"] for b in all_branches if b.get("utc_offset", 7) == target_offset}
        for tid in sorted(tenant_ids):
            token = ctx_tenant_id.set(tid)
            try:
                await job_olap_enrichment(tenant_id=tid)
            except Exception as e:
                logger.error(f"olap_enrichment UTC+{target_offset} tenant={tid}: {e}", exc_info=True)
            finally:
                ctx_tenant_id.reset(token)

    scheduler.add_job(
        _olap_enrichment_by_tz,
        trigger=CronTrigger(minute=0),  # каждый час в :00
        id="olap_enrichment",
        name="OLAP обогащение orders_raw (по таймзонам)",
        replace_existing=True,
        misfire_grace_time=300,
    )

    # Выгрузка iiko → Google Sheets: 05:26 МСК
    scheduler.add_job(
        run_for_all_tenants(job_export_iiko_to_sheets),
        trigger=CronTrigger(hour=5, minute=26),
        id="iiko_to_sheets",
        name="iiko заказы → Google Sheets (все тенанты)",
        replace_existing=True,
        misfire_grace_time=300,
    )


    # Мониторинг цен конкурентов: каждое воскресенье в 10:00 МСК
    scheduler.add_job(
        job_monitor_competitors,
        trigger=CronTrigger(day_of_week="mon", hour=7, minute=0),
        id="competitor_monitor",
        name="Мониторинг цен конкурентов",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Алерты опоздания заказов: каждые 2 минуты
    scheduler.add_job(
        job_late_alerts,
        trigger=IntervalTrigger(minutes=2),
        id="late_alerts",
        name="Алерты опоздания заказов",
        replace_existing=True,
        misfire_grace_time=60,
    )

    # Утренний отчёт: каждый час в :25, шлём тем offset'ам, у которых сейчас 09:25 местного.
    # Scheduler в Europe/Moscow (UTC+3). Формула: target_offset = current_msk_hour + 3 + (25/60≈0) → round.
    # Проще: если сейчас H:25 МСК, local_hour = H + (offset - 3). Нужно local_hour == 9.
    # Значит offset = 9 - H + 3 = 12 - H.
    async def _morning_report_by_tz():
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from app.db import get_all_branches
        msk_now = _dt.now(_tz(_td(hours=3)))
        target_offset = 12 - msk_now.hour  # offset, для которого сейчас 09:xx местного
        offsets = {b.get("utc_offset", 7) for b in get_all_branches()}
        if target_offset in offsets:
            try:
                await job_send_morning_report(utc_offset=target_offset)
            except Exception as e:
                logger.error(f"morning_report UTC+{target_offset}: {e}", exc_info=True)

    scheduler.add_job(
        _morning_report_by_tz,
        trigger=CronTrigger(minute=25),  # каждый час в :25
        id="morning_report",
        name="Утренний отчёт → Telegram (по таймзонам)",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # Аудит опасных операций: каждый час в :27, аналогичная логика.
    async def _audit_report_by_tz():
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from app.db import get_all_branches
        msk_now = _dt.now(_tz(_td(hours=3)))
        target_offset = 12 - msk_now.hour
        offsets = {b.get("utc_offset", 7) for b in get_all_branches()}
        if target_offset in offsets:
            try:
                await job_audit_report(utc_offset=target_offset)
            except Exception as e:
                logger.error(f"audit_report UTC+{target_offset}: {e}", exc_info=True)

    scheduler.add_job(
        _audit_report_by_tz,
        trigger=CronTrigger(minute=27),  # каждый час в :27
        id="audit_report",
        name="Аудит опасных операций → Telegram (по таймзонам)",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # Еженедельный отчёт: каждый понедельник в :30, аналогичная логика по таймзонам.
    # 09:30 локального = понедельник и target_offset == 12 - msk_hour.
    async def _weekly_report_by_tz():
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from app.db import get_all_branches
        msk_now = _dt.now(_tz(_td(hours=3)))
        target_offset = 12 - msk_now.hour
        offsets = {b.get("utc_offset", 7) for b in get_all_branches()}
        if target_offset in offsets:
            try:
                await job_weekly_report(utc_offset=target_offset)
            except Exception as e:
                logger.error(f"weekly_report UTC+{target_offset}: {e}", exc_info=True)

    scheduler.add_job(
        _weekly_report_by_tz,
        trigger=CronTrigger(day_of_week="mon", minute=30),  # каждый пн в :30
        id="weekly_report",
        name="Еженедельный отчёт → Telegram (по таймзонам)",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # Синхронизация отменённых заказов из OLAP v2: каждые 3 минуты (все тенанты)
    scheduler.add_job(
        run_for_all_tenants(job_cancel_sync),
        trigger=IntervalTrigger(minutes=3),
        id="cancel_sync",
        name="Синхронизация отмен (OLAP v2, все тенанты)",
        replace_existing=True,
        misfire_grace_time=120,
    )

    # Рекуррентный биллинг ЮKassa: ежедневно в 03:00 МСК
    scheduler.add_job(
        job_recurring_billing,
        trigger=CronTrigger(hour=3, minute=0),
        id="recurring_billing",
        name="Рекуррентный биллинг ЮKassa",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Жизненный цикл подписок: ежедневно в 04:00 МСК (после биллинга в 03:00)
    scheduler.add_job(
        job_trial_expiry,
        trigger=CronTrigger(hour=4, minute=0),
        id="trial_expiry",
        name="Истечение триалов",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        job_payment_grace,
        trigger=CronTrigger(hour=4, minute=10),
        id="payment_grace",
        name="Grace period неоплаты",
        replace_existing=True,
        misfire_grace_time=3600,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Запуск приложения...")
    await init_db()
    logger.info("SQLite инициализирован")

    # Seed .env users/chats into DB if tables are empty (Issue 1a: first-run migration)
    _existing_users = await get_all_tenant_users()
    _existing_chats = await get_all_tenant_chats()
    if not _existing_users and not _existing_chats:
        from app.config import get_settings as _gs
        _s = _gs()
        _default_mods = ["late_alerts", "late_queries", "search", "reports"]
        _admin_mods = ["late_alerts", "late_queries", "search", "reports", "marketing", "finance", "admin"]

        # Seed admin
        if _s.telegram_admin_id:
            await upsert_tenant_user(_s.telegram_admin_id, "Артемий (admin)", _admin_mods, None, role="admin")

        # Seed TELEGRAM_ALLOWED_IDS
        for _raw_id in (_s.telegram_allowed_ids or "").split(","):
            _raw_id = _raw_id.strip()
            if not _raw_id.lstrip("-").isdigit():
                continue
            _tid = int(_raw_id)
            if _tid == _s.telegram_admin_id:
                continue  # уже добавлен
            if _tid < 0:
                await upsert_tenant_chat(_tid, f"Чат {abs(_tid)}", _default_mods, None)
            else:
                await upsert_tenant_user(_tid, f"User {_tid}", _default_mods, None)

        logger.info("[main] Seed from .env: users/chats added to DB")

    # Грузим конфиги ВСЕХ активных тенантов в _db_cfg (merge по chat_id/user_id)
    from app.database_pg import load_chat_tenant_map, get_pool as _get_pg_pool
    _merged_access: dict = {"chats": {}, "users": {}}
    try:
        _pg_pool = _get_pg_pool()
        _tid_rows = await _pg_pool.fetch("SELECT id FROM tenants WHERE status = 'active' ORDER BY id")
        for _row in _tid_rows:
            _cfg = await get_access_config_from_db(_row["id"])
            _merged_access["chats"].update(_cfg.get("chats", {}))
            _merged_access["users"].update(_cfg.get("users", {}))
    except Exception as _e:
        logger.warning(f"[main] Не удалось загрузить конфиги всех тенантов: {_e}")
        _cfg1 = await get_access_config_from_db(1)
        _merged_access = _cfg1
    _access.update_db_cache(_merged_access)
    await load_chat_tenant_map()
    logger.info(
        f"Access DB cache: {len(_merged_access.get('chats', {}))} чатов, "
        f"{len(_merged_access.get('users', {}))} пользователей (все тенанты)"
    )

    register_jobs()
    scheduler.start()
    logger.info(f"APScheduler запущен, задач: {len(scheduler.get_jobs())}")
    for job in scheduler.get_jobs():
        logger.info(f"  ✓ {job.name} | следующий запуск: {job.next_run_time}")

    # Запускаем polling loop для каждого активного тенанта с bot_token
    tenants = await get_active_tenants_with_tokens()
    _polling_tasks: list[asyncio.Task] = []
    if tenants:
        for t in tenants:
            task = asyncio.create_task(
                run_polling_loop(bot_token=t["bot_token"], tenant_id=t["id"]),
                name=f"polling:{t['slug']}",
            )
            _polling_tasks.append(task)
        logger.info(f"Аркентий: запущено {len(_polling_tasks)} polling loop(s): {[t['slug'] for t in tenants]}")
    else:
        # Fallback: один loop из .env (SQLite режим или PG без тенантов)
        task = asyncio.create_task(run_polling_loop(), name="polling:default")
        _polling_tasks.append(task)
        logger.info("Аркентий: запущен polling loop (fallback, single tenant)")

    await telegram.monitor(
        f"🟢 <b>Аркентий запущен</b>\n"
        f"Задач: {len(scheduler.get_jobs())}\n"
        f"Ботов: {len(_polling_tasks)}\n"
        f"<i>{datetime.now().strftime('%d.%m.%Y %H:%M')}</i>"
    )

    yield

    logger.info("Остановка приложения...")
    for task in _polling_tasks:
        task.cancel()
    scheduler.shutdown(wait=False)


limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="Аркентий (Интеграции Ёбидоёби)",
    version="1.0.0",
    description="Автоматические интеграции: iiko, Telegram, Google Sheets, Битрикс24, MyMeet",
    lifespan=lifespan,
)

app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Слишком много запросов. Попробуйте позже."},
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://arkenty.ru",
        "https://www.arkenty.ru",
        # Аркентий.рф (браузер шлёт Origin в punycode для IDN доменов)
        "https://аркентий.рф",
        "https://www.аркентий.рф",
        "https://xn--e1afkbrcdl.xn--p1ai",
        "https://www.xn--e1afkbrcdl.xn--p1ai",
        "http://localhost:8000",
    ],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=True,
)

app.include_router(health_router)
app.include_router(bitrix_router, prefix="/webhook")
app.include_router(auth_router)
app.include_router(cabinet_router)
app.include_router(onboarding_router)
app.include_router(payments_router)
app.include_router(stats_router)


# --- Ручные триггеры (для отладки и тестирования) ---

def _verify_admin_key(x_admin_key: str = Header(None)) -> str:
    """Проверяет API-ключ для админ-эндпоинтов."""
    expected = settings.admin_api_key
    if not expected:
        raise HTTPException(503, "ADMIN_API_KEY не настроен")
    if not x_admin_key or x_admin_key != expected:
        raise HTTPException(401, "Неверный X-Admin-Key")
    return x_admin_key


@app.post("/run/{job_id}", tags=["Manual triggers"])
async def run_job_manually(job_id: str, _key: str = Depends(_verify_admin_key)):
    """Запустить задачу вручную по ID."""
    job = scheduler.get_job(job_id)
    if not job:
        jobs = [j.id for j in scheduler.get_jobs()]
        return {"error": f"Job '{job_id}' не найден", "available": jobs}
    job.modify(next_run_time=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    return {"status": "triggered", "job": job_id, "name": job.name}


@app.get("/jobs", tags=["Manual triggers"])
async def list_jobs(_key: str = Depends(_verify_admin_key)):
    """Список всех задач и времени следующего запуска."""
    return [
        {
            "id": j.id,
            "name": j.name,
            "next_run": str(j.next_run_time),
        }
        for j in scheduler.get_jobs()
    ]


@app.post("/backfill", tags=["Manual triggers"])
async def run_backfill(date_from: str = "2026-02-01", date_to: str | None = None, _key: str = Depends(_verify_admin_key)):
    """
    Сброс листа 'Выгрузка iiko' и заполнение данными за диапазон дат.
    Формат дат: YYYY-MM-DD. По умолчанию date_to = вчера по UTC+7.
    """
    import datetime as _dt
    from app.jobs.iiko_to_sheets import reset_sheet_and_backfill
    from app.config import get_settings as _gs
    from app.utils.timezone import branch_tz

    _settings = _gs()
    branches = _settings.branches
    if not branches:
        return {"error": "Нет точек в branches.json"}

    tz = branch_tz(branches[0])
    yesterday = _dt.datetime.now(tz) - _dt.timedelta(days=1)

    try:
        df = _dt.datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=tz)
        dt = (
            _dt.datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=tz)
            if date_to
            else yesterday
        )
    except ValueError as e:
        return {"error": f"Неверный формат даты: {e}"}

    import asyncio
    asyncio.create_task(reset_sheet_and_backfill(df, dt))
    return {
        "status": "started",
        "date_from": df.strftime("%Y-%m-%d"),
        "date_to": dt.strftime("%Y-%m-%d"),
    }


# --- StaticFiles mount (ДОЛЖЕН быть в конце, после всех маршрутов) ---
app.mount("/", StaticFiles(directory="web", html=True), name="static")
