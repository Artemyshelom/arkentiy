"""
Точка входа: FastAPI + APScheduler.
Все задачи по расписанию регистрируются здесь.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from datetime import datetime
from app.clients import telegram
from app.config import get_settings
from app.database import init_db, get_access_config_from_db
from app import access as _access
from app.monitoring.healthcheck import router as health_router
from app.webhooks.bitrix import router as bitrix_router

# Импорт задач
from app.jobs.iiko_to_sheets import job_export_iiko_to_sheets
# from app.jobs.telegram_commands import poll_telegram_commands  # Арсений отключён, заменён Аркентием
from app.clients.iiko_bo_events import job_poll_iiko_events
from app.monitoring.healthcheck import job_backup_sqlite
from app.jobs.competitor_monitor import job_monitor_competitors
from app.jobs.arkentiy import poll_analytics_bot, run_polling_loop
from app.jobs.late_alerts import job_late_alerts
from app.jobs.daily_report import (
    job_send_evening_report,
    job_send_morning_report,
    job_save_rt_snapshot,
)

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

    # Арсений (telegram_commands.py) — отключён, заменён Аркентием (analytics_bot.py)
    # scheduler.add_job(poll_telegram_commands, trigger=IntervalTrigger(seconds=3), ...)

    # iiko Events API (real-time заказы): каждые 30 секунд
    scheduler.add_job(
        job_poll_iiko_events,
        trigger=IntervalTrigger(seconds=30),
        id="iiko_events",
        name="iiko события (Events API)",
        replace_existing=True,
        misfire_grace_time=15,
    )


    # ОТКЛЮЧЕНО: Стоп-лист iiko → Telegram (временно, коряво работает)
    # scheduler.add_job(
    #     job_check_stoplist,
    #     trigger=IntervalTrigger(minutes=5),
    #     id="iiko_stoplist",
    #     name="Стоп-лист iiko → Telegram",
    #     replace_existing=True,
    #     misfire_grace_time=60,
    # )

    # Выгрузка заказов iiko → Google Sheets: ежедневно в 23:00
    scheduler.add_job(
        job_export_iiko_to_sheets,
        trigger=CronTrigger(hour=23, minute=0),
        id="iiko_to_sheets",
        name="iiko заказы → Google Sheets",
        replace_existing=True,
        misfire_grace_time=300,
    )



    # ОТКЛЮЧЕНО: Пре-встречные саммари (временно, коряво работает)
    # scheduler.add_job(
    #     job_check_upcoming_meetings,
    #     trigger=IntervalTrigger(minutes=15),
    #     id="pre_meeting",
    #     name="Пре-встречные саммари → Telegram",
    #     replace_existing=True,
    #     misfire_grace_time=60,
    # )

    # Просроченные здачи Битрикс24: отключено
    # scheduler.add_job(
    #     job_check_overdue_tasks,
    #     trigger=CronTrigger(hour=9, minute=0),
    #     id="task_tracker",
    #     name="Просроченные задачи Битрикс24 → Telegram",
    #     replace_existing=True,
    #     misfire_grace_time=300,
    # )

    # Heartbeat отключён — уведомления только при старте/падении через Аркентий
    # scheduler.add_job(job_send_heartbeat, trigger=IntervalTrigger(minutes=30), ...)

    # Бэкап SQLite в Google Drive: каждую ночь в 02:00
    scheduler.add_job(
        job_backup_sqlite,
        trigger=CronTrigger(hour=2, minute=0),
        id="backup_sqlite",
        name="Бэкап SQLite → Google Drive",
        replace_existing=True,
        misfire_grace_time=3600,
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

    # Вечерний отчёт UTC+7 (пн-чт): 23:30 лок = 19:30 МСК
    scheduler.add_job(
        job_send_evening_report,
        trigger=CronTrigger(day_of_week="sun,mon,tue,wed,thu", hour=19, minute=30),
        kwargs={"utc_offset": 7},
        id="evening_report_utc7",
        name="Вечерний отчёт (вс-чт 23:30) UTC+7 → Telegram",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # Вечерний отчёт UTC+7 пт/сб: 00:30 следующего дня лок = 20:30 МСК пт/сб
    scheduler.add_job(
        job_send_evening_report,
        trigger=CronTrigger(day_of_week="fri,sat", hour=20, minute=30),
        kwargs={"utc_offset": 7, "days_ago": 1},
        id="evening_report_utc7_fri",
        name="Вечерний отчёт пт/сб (00:30 след. дня лок) UTC+7 → Telegram",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # Утренний отчёт UTC+7: 09:30 лок = 05:30 МСК
    scheduler.add_job(
        job_send_morning_report,
        trigger=CronTrigger(hour=5, minute=30),
        kwargs={"utc_offset": 7},
        id="morning_report_utc7",
        name="Утренний отчёт UTC+7 → Telegram",
        replace_existing=True,
        misfire_grace_time=600,
    )

    # RT-снапшот UTC+7 пт/сб: 23:50 лок = 19:50 МСК
    scheduler.add_job(
        job_save_rt_snapshot,
        trigger=CronTrigger(day_of_week="fri,sat", hour=19, minute=50),
        kwargs={"utc_offset": 7},
        id="rt_snapshot_utc7",
        name="RT-снапшот пт/сб UTC+7",
        replace_existing=True,
        misfire_grace_time=300,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Запуск приложения...")
    await init_db()
    logger.info("SQLite инициализирован")

    _db_access = await get_access_config_from_db()
    _access.update_db_cache(_db_access)
    logger.info(
        f"Access DB cache: {len(_db_access.get('chats', {}))} чатов, "
        f"{len(_db_access.get('users', {}))} пользователей"
    )

    register_jobs()
    scheduler.start()
    logger.info(f"APScheduler запущен, задач: {len(scheduler.get_jobs())}")
    for job in scheduler.get_jobs():
        logger.info(f"  ✓ {job.name} | следующий запуск: {job.next_run_time}")

    _polling_task = asyncio.create_task(run_polling_loop())
    logger.info("Аркентий polling task started")

    await telegram.monitor(
        f"🟢 <b>Аркентий запущен</b>\n"
        f"Задач: {len(scheduler.get_jobs())}\n"
        f"<i>{datetime.now().strftime('%d.%m.%Y %H:%M')}</i>"
    )

    yield

    logger.info("Остановка приложения...")
    _polling_task.cancel()
    scheduler.shutdown(wait=False)


app = FastAPI(
    title="Аркентий (Интеграции Ёбидоёби)",
    version="1.0.0",
    description="Автоматические интеграции: iiko, Telegram, Google Sheets, Битрикс24, MyMeet",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(bitrix_router, prefix="/webhook")


# --- Ручные триггеры (для отладки и тестирования) ---

@app.post("/run/{job_id}", tags=["Manual triggers"])
async def run_job_manually(job_id: str):
    """Запустить задачу вручную по ID."""
    job = scheduler.get_job(job_id)
    if not job:
        jobs = [j.id for j in scheduler.get_jobs()]
        return {"error": f"Job '{job_id}' не найден", "available": jobs}
    job.modify(next_run_time=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    return {"status": "triggered", "job": job_id, "name": job.name}


@app.get("/jobs", tags=["Manual triggers"])
async def list_jobs():
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
async def run_backfill(date_from: str = "2026-02-01", date_to: str | None = None):
    """
    Сброс листа 'Выгрузка iiko' и заполнение данными за диапазон дат.
    Формат дат: YYYY-MM-DD. По умолчанию date_to = вчера по UTC+7.
    """
    import datetime as _dt
    from app.jobs.iiko_to_sheets import reset_sheet_and_backfill
    from app.config import get_settings as _gs
    from app.jobs.iiko_status_report import branch_tz

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
