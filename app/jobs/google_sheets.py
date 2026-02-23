"""
Google Sheets API клиент через Service Account.
Документация: https://developers.google.com/sheets/api

Важно: сервисный аккаунт должен быть добавлен в таблицу через "Поделиться".
Rate limit: 60 запросов/мин.
"""

import asyncio
import logging
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _get_service():
    """Возвращает авторизованный Sheets API client."""
    credentials = service_account.Credentials.from_service_account_file(
        settings.google_service_account_file,
        scopes=SCOPES,
    )
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def _get_drive_service():
    """Возвращает авторизованный Drive API client."""
    credentials = service_account.Credentials.from_service_account_file(
        settings.google_service_account_file,
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


async def read_range(spreadsheet_id: str, range_name: str) -> list[list]:
    """
    Читает диапазон из таблицы.
    range_name: например 'Sheet1!A1:E10' или 'Данные!A:Z'
    """
    def _read():
        service = _get_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_name,
        ).execute()
        return result.get("values", [])

    return await asyncio.get_event_loop().run_in_executor(None, _read)


async def write_range(
    spreadsheet_id: str,
    range_name: str,
    values: list[list[Any]],
    value_input_option: str = "USER_ENTERED",
) -> dict:
    """
    Записывает данные в диапазон таблицы.
    Перезаписывает существующие значения.
    """
    def _write():
        service = _get_service()
        body = {"values": values}
        return service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueInputOption=value_input_option,
            body=body,
        ).execute()

    return await asyncio.get_event_loop().run_in_executor(None, _write)


async def append_rows(
    spreadsheet_id: str,
    range_name: str,
    values: list[list[Any]],
) -> dict:
    """
    Добавляет строки в конец диапазона (не перезаписывает).
    Удобно для логов и ежедневных отчётов.
    """
    def _append():
        service = _get_service()
        body = {"values": values}
        return service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body=body,
        ).execute()

    return await asyncio.get_event_loop().run_in_executor(None, _append)


async def ensure_header(
    spreadsheet_id: str,
    range_name: str,
    headers: list[str],
) -> None:
    """
    Проверяет что первая строка содержит заголовки.
    Если лист пуст — записывает заголовки.
    """
    existing = await read_range(spreadsheet_id, range_name)
    if not existing or existing[0] != headers:
        await write_range(spreadsheet_id, range_name, [headers])


async def clear_range(spreadsheet_id: str, range_name: str) -> dict:
    """Очищает диапазон (удаляет значения, не форматирование)."""
    def _clear():
        service = _get_service()
        return service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=range_name,
        ).execute()

    return await asyncio.get_event_loop().run_in_executor(None, _clear)


async def backup_file_to_drive(local_path: str, filename: str, folder_id: str) -> str | None:
    """
    Загружает файл в Google Drive (для бэкапов SQLite).
    Возвращает ID загруженного файла.
    """
    from googleapiclient.http import MediaFileUpload

    def _upload():
        service = _get_drive_service()
        media = MediaFileUpload(local_path, mimetype="application/octet-stream")
        file_metadata = {
            "name": filename,
            "parents": [folder_id],
        }
        result = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id",
        ).execute()
        return result.get("id")

    try:
        file_id = await asyncio.get_event_loop().run_in_executor(None, _upload)
        logger.info(f"Файл {filename} загружен в Drive, id={file_id}")
        return file_id
    except HttpError as e:
        logger.error(f"Ошибка загрузки в Drive: {e}")
        return None
