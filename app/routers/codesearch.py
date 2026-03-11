"""
RAG-поиск по кодовой базе Аркентия через pgvector.

GET /api/codesearch
  q       — поисковый запрос (обязательный)
  limit   — кол-во результатов (1-10, default=5)
  type    — фильтр по типу: "py" или "md"
  module  — фильтр по модулю: "jobs", "routers", "services", ...

Auth: Authorization: Bearer <admin_api_key или ключ с модулем codesearch>
Embeddings: Jina AI jina-embeddings-v2-base-code (768-dim, via SOCKS5 если задан)
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings
from app.database_pg import get_pool

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api", tags=["Code Search"])
_bearer = HTTPBearer(auto_error=False)

EMBEDDING_MODEL = "jina-embeddings-v2-base-code"
JINA_API_URL = "https://api.jina.ai/v1/embeddings"


async def _get_embedding(text: str) -> list[float]:
    """Генерирует embedding запроса через Jina AI (прокси из JINA_PROXY_URL если задан)."""
    api_key = os.getenv("JINA_API_KEY", settings.jina_api_key)
    if not api_key:
        raise HTTPException(status_code=503, detail="JINA_API_KEY не настроен")

    proxy = os.getenv("JINA_PROXY_URL", "")
    client_kwargs: dict = {"timeout": 30.0}
    if proxy:
        client_kwargs["proxy"] = proxy

    async with httpx.AsyncClient(**client_kwargs) as client:
        resp = await client.post(
            JINA_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"model": EMBEDDING_MODEL, "input": [text]},
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError:
            logger.error(f"[codesearch] Jina API ошибка {resp.status_code}: {resp.text[:200]}")
            raise HTTPException(status_code=502, detail=f"Jina AI: {resp.status_code}")

    return resp.json()["data"][0]["embedding"]


def _verify_codesearch_key(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> None:
    """
    Проверяет Bearer-токен:
    - admin_api_key из settings — полный доступ
    - ключ агента из api_keys.json с "codesearch" в modules
    """
    if not creds:
        raise HTTPException(status_code=401, detail="Требуется авторизация")

    token = creds.credentials

    # Admin ключ
    if settings.admin_api_key and token == settings.admin_api_key:
        return

    # Ключи агентов из api_keys.json
    keys_file = Path(settings.stats_api_keys_file)
    if keys_file.exists():
        try:
            keys_data = json.loads(keys_file.read_text())
            for _agent_name, agent_data in keys_data.items():
                if (
                    isinstance(agent_data, dict)
                    and agent_data.get("key") == token
                    and "codesearch" in agent_data.get("modules", [])
                ):
                    return
        except Exception as e:
            logger.warning(f"[codesearch] Ошибка чтения api_keys.json: {e}")

    raise HTTPException(status_code=401, detail="Неверный ключ")


@router.get("/codesearch")
async def codesearch(
    q: str = Query(..., min_length=1, max_length=500, description="Поисковый запрос"),
    limit: int = Query(default=5, ge=1, le=10, description="Количество результатов"),
    type: Optional[str] = Query(
        default=None, pattern="^(py|md)$", description="Фильтр типа: py или md"
    ),
    module: Optional[str] = Query(
        default=None, max_length=50, description="Фильтр модуля: jobs, routers, etc."
    ),
    _: None = Depends(_verify_codesearch_key),
) -> dict:
    """Семантический поиск по кодовой базе и документации (pgvector + Jina AI)."""
    embedding = await _get_embedding(q)
    embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"

    # Строим параметризованный запрос без f-string для пользовательских данных
    # $1 — вектор запроса, $2,$3 — опциональные фильтры, $N — limit
    filter_clauses: list[str] = []
    extra_params: list = []
    param_idx = 2  # $1 уже занят вектором

    if type:
        filter_clauses.append(f"AND file_type = ${param_idx}")
        extra_params.append(type)
        param_idx += 1

    if module:
        filter_clauses.append(f"AND module = ${param_idx}")
        extra_params.append(module)
        param_idx += 1

    limit_param_idx = param_idx
    all_params = [embedding_str] + extra_params + [limit]

    sql = f"""
        SELECT file_path, chunk_index, content, file_type, module, category,
               1 - (embedding <=> $1::vector) AS score
        FROM code_chunks
        WHERE embedding IS NOT NULL
        {"  ".join(filter_clauses)}
        ORDER BY embedding <=> $1::vector
        LIMIT ${limit_param_idx}
    """

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *all_params)

    results = [
        {
            "file": row["file_path"],
            "type": row["file_type"],
            "module": row["module"],
            "category": row["category"],
            "score": round(row["score"], 3),
            "content": row["content"],
        }
        for row in rows
    ]

    logger.info(
        f"[codesearch] q={q!r} type={type} module={module} limit={limit} "
        f"→ {len(results)} результатов"
    )

    return {
        "query": q,
        "count": len(results),
        "results": results,
    }
