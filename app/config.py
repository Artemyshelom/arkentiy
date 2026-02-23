import json
from datetime import timezone, timedelta
from pathlib import Path
from functools import lru_cache
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
    iiko_bo_login: str = "artemiish"
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

    # Маркетинг: chat_id или user_id через запятую, кому доступна /выгрузка
    telegram_marketing_ids: str = ""

    # Безопасность
    webhook_secret: str = "change_me"

    # Мониторинг конкурентов
    competitor_bot_token: str = ""
    competitor_notify_chat: str = ""
    competitors_config_file: str = "/app/secrets/competitors.json"
    competitor_sheets_config_file: str = "/app/secrets/competitor_sheets.json"

    # Приложение
    log_level: str = "INFO"
    database_url: str = "sqlite+aiosqlite:////app/data/app.db"

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
        Читает конфиг точек из JSON.
        Каждая точка: {name, dept_id, utc_offset, cloud_org_id?}
        """
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
