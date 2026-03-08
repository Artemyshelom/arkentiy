import json
import sys
from datetime import timezone, timedelta
from pathlib import Path
from functools import lru_cache
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    # iiko Cloud API (стоп-листы, номенклатура) — ключи по городам
    iiko_api_key: str = ""           # устаревший единый ключ (backward compat)
    iiko_barnaul_api_key: str = ""
    iiko_abakan_api_key: str = ""
    iiko_tomsk_api_key: str = ""
    iiko_chernogorsk_api_key: str = ""
    iiko_org_ids: str = ""           # JSON-строка: {название точки: org_id}
    iiko_org_ids_file: str = "/app/secrets/org_ids.json"

    # iiko Web BO API (выручка, отчёты)
    iiko_bo_login: str = ""
    iiko_bo_password: str = ""

    # Конфиг точек (города, dept IDs, timezone)
    branches_config_file: str = "/app/secrets/branches.json"

    # Telegram
    telegram_bot_token: str = ""
    telegram_analytics_bot_token: str = ""
    telegram_admin_id: int = 0
    telegram_allowed_ids: str = ""
    telegram_chat_alerts: str = ""
    telegram_chat_reports: str = ""
    telegram_chat_meetings: str = ""
    telegram_chat_monitoring: str = ""

    # Google
    google_service_account_file: str = "/app/secrets/google-service-account.json"
    google_sheets_iiko_id: str = ""
    google_drive_backup_folder_id: str = ""

    # MyMeet
    mymeet_api_key: str = ""

    # Битрикс24
    bitrix24_incoming_webhook: str = ""

    # OpenRouter (LLM-парсинг маркетинговых запросов)
    openrouter_api_key: str = ""
    openrouter_model: str = "google/gemini-2.5-flash"
    humor_model: str = "anthropic/claude-3-5-haiku"

    # Маркетинг: chat_id или user_id через запятую, кому доступна /выгрузка
    telegram_marketing_ids: str = ""

    # ЮKassa
    yukassa_shop_id: str = ""
    yukassa_secret_key: str = ""
    yukassa_return_url: str = "https://arkenty.ru"

    # Безопасность
    webhook_secret: str = ""
    jwt_secret: str = ""
    admin_api_key: str = ""  # ключ для /run, /jobs, /backfill

    # Stats API (внешние AI-агенты: Борис и др.)
    stats_api_keys_file: str = "/app/secrets/api_keys.json"
    debug: bool = False

    # Email (Resend)
    resend_api_key: str = ""
    email_from: str = "Аркентий <noreply@arkenty.ru>"
    base_url: str = "https://arkenty.ru"

    # OpenClaw AI (@ mention обработчик)
    openclaw_enabled: bool = False
    openclaw_api_url: str = ""
    openclaw_api_token: str = ""
    openclaw_model: str = "openclaw:arkentiy-brain"
    openclaw_timeout: float = 60.0
    telegram_bot_username: str = ""

    # Мониторинг конкурентов
    competitor_bot_token: str = ""
    competitor_notify_chat: str = ""
    competitors_config_file: str = "/app/secrets/competitors.json"
    competitor_sheets_config_file: str = "/app/secrets/competitor_sheets.json"

    # Приложение
    log_level: str = "INFO"
    database_url: str = ""  # DATABASE_URL env var (postgresql://...)

    @model_validator(mode="after")
    def validate_critical_secrets(self) -> "Settings":
        """Блокируем старт если JWT_SECRET не задан или слишком слабый."""
        _weak = {"changeme", "secret", "password", "12345678901234567890123456789012", "test"}
        if not self.jwt_secret or len(self.jwt_secret) < 32:
            print(
                "FATAL: JWT_SECRET не задан или слишком короткий (минимум 32 символа).\n"
                "Сгенерируйте: openssl rand -hex 32",
                file=sys.stderr,
            )
            sys.exit(1)
        if self.jwt_secret.lower() in _weak:
            print("FATAL: JWT_SECRET — известное слабое значение. Смените на случайную строку.", file=sys.stderr)
            sys.exit(1)
        return self

    @property
    def competitor_sheets(self) -> dict[str, str]:
        """Маппинг {город: spreadsheet_id} из competitor_sheets.json."""
        path = Path(self.competitor_sheets_config_file)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    @property
    def competitors(self) -> dict[str, list[dict]]:
        """Читает конфиг конкурентов: {город: [{name, url, parser, active, ...}]}."""
        path = Path(self.competitors_config_file)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    @property
    def org_ids(self) -> dict[str, str]:
        """Читает org IDs из JSON-файла: {название точки: org_id}."""
        path = Path(self.iiko_org_ids_file)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    @property
    def iiko_cities(self) -> list[str]:
        return list(self.org_ids.keys())

    @property
    def branches(self) -> list[dict]:
        """
        Читает конфиг точек.
        PG режим: из in-memory cache (заполняется при init_db из iiko_credentials).
        SQLite режим: из JSON-файла (обратная совместимость).
        """
        import os
        _url = os.getenv("DATABASE_URL", "")
        if _url.startswith("postgresql://") or _url.startswith("postgres://"):
            try:
                from app.db import get_branches as _get_branches
                cached = _get_branches(1)
                if cached:
                    return cached
            except Exception:
                pass
        path = Path(self.branches_config_file)
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    @property
    def branches_by_name(self) -> dict[str, dict]:
        """Словарь name → branch config для быстрого поиска."""
        return {b["name"]: b for b in self.branches}

    @property
    def default_tz(self) -> timezone:
        """Timezone по умолчанию из первой точки (или UTC+7)."""
        if self.branches:
            offset = self.branches[0].get("utc_offset", 7)
            return timezone(timedelta(hours=offset))
        return timezone(timedelta(hours=7))


@lru_cache
def get_settings() -> Settings:
    return Settings()
