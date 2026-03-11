"""
API для консультанта Станислава.

POST /api/consultant/activate  — подключить чат к тенанту
GET  /api/consultant/chats     — список активированных чатов

Auth: Authorization: Bearer <admin_api_key>
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.config import get_settings
from app.database_pg import get_pool

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api/consultant", tags=["Consultant API"])
_bearer = HTTPBearer(auto_error=False)


def _verify_admin(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> None:
    if not creds or creds.credentials != settings.admin_api_key:
        raise HTTPException(status_code=401, detail="Неверный ключ")


class ActivateRequest(BaseModel):
    chat_id: int
    tenant_id: str
    note: Optional[str] = None


@router.post("/activate")
async def activate_chat(
    req: ActivateRequest,
    _: None = Depends(_verify_admin),
) -> dict:
    """Зарегистрировать Telegram-чат для консультанта (или обновить binding)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO consultant_chats (chat_id, tenant_id, note)
            VALUES ($1, $2, $3)
            ON CONFLICT (chat_id) DO UPDATE
                SET tenant_id   = EXCLUDED.tenant_id,
                    note        = EXCLUDED.note,
                    updated_at  = NOW()
            """,
            req.chat_id,
            req.tenant_id,
            req.note,
        )
    logger.info(f"[consultant] chat {req.chat_id} → tenant '{req.tenant_id}'")
    return {"status": "ok", "chat_id": req.chat_id, "tenant_id": req.tenant_id}


@router.get("/chats")
async def list_chats(_: None = Depends(_verify_admin)) -> list[dict]:
    """Список всех активированных чатов Станислава."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT chat_id, tenant_id, note, activated_at, updated_at "
            "FROM consultant_chats ORDER BY activated_at DESC"
        )
    return [dict(r) for r in rows]
