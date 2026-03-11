"""
Индексация кодовой базы для RAG-поиска.

Сканирует app/ (*.py), docs/ (*.md), rules/ (*.md).
Разбивает на чанки (AST-aware для .py, по заголовкам для .md).
Генерирует embeddings через Jina AI jina-embeddings-v2-base-code (768-dim).
Сохраняет в таблицу code_chunks (pgvector) с инкрементальным обновлением.

Запуск:
  python3 -m app.tools.reindex_code

Переменные окружения (или .env):
  DATABASE_URL   — postgresql://user:pass@host:5432/db
  JINA_API_KEY   — ключ Jina AI
  JINA_PROXY_URL — socks5://user:pass@host:1080 (опционально, для обхода блокировок)
"""

import ast
import asyncio
import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

import asyncpg
import httpx
import tiktoken

logger = logging.getLogger(__name__)

# --- Конфиг ---

# Внутри Docker: /app, на хосте VPS: /opt/ebidoebi
# Переопределяется через REINDEX_BASE_DIR
BASE_DIR = Path(os.getenv("REINDEX_BASE_DIR", "/app" if Path("/app/app").exists() else "/opt/ebidoebi"))

SCAN_TARGETS = [
    (BASE_DIR / "app",   "**/*.py"),
    (BASE_DIR / "docs",  "**/*.md"),
    (BASE_DIR / "rules", "**/*.md"),
]

# Паттерны для пропуска (если хоть один встретится в пути — файл не индексируется)
EXCLUDE_PATTERNS = {
    "__pycache__",
    ".pyc",
    "archive/",
    "migrations/",
}

CHUNK_SIZE = 800      # токенов
CHUNK_OVERLAP = 60    # токенов overlap
BATCH_SIZE = 128      # максимум чанков за один запрос к Jina AI
EMBEDDING_MODEL = "jina-embeddings-v2-base-code"  # 768-dim, code-specific
JINA_API_URL = "https://api.jina.ai/v1/embeddings"


# --- Утилиты ---

def _get_encoding() -> tiktoken.Encoding:
    return tiktoken.encoding_for_model("gpt-3.5-turbo")


def _format_vector(v: list[float]) -> str:
    """Форматирует вектор в строку для pgvector: '[0.1,0.2,...]'."""
    return "[" + ",".join(str(x) for x in v) + "]"


def should_index(file_path: Path) -> bool:
    """Проверяет, нужно ли индексировать файл."""
    path_str = str(file_path)
    for pattern in EXCLUDE_PATTERNS:
        if pattern in path_str:
            return False
    # Пустые __init__.py не несут смысла
    if file_path.name == "__init__.py":
        try:
            return file_path.stat().st_size > 100
        except OSError:
            return False
    return True


def file_md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def extract_module_category(rel_path: str, file_type: str) -> tuple[Optional[str], Optional[str]]:
    """
    Извлекает module и category из относительного пути:
      app/jobs/daily_report.py   → module="jobs",     category=None
      docs/specs/tg/search.md    → module=None,        category="specs"
      rules/integrator/lessons.md→ module=None,        category="rules"
    """
    parts = Path(rel_path).parts
    module = None
    category = None

    if not parts:
        return None, None

    if file_type == "py" and parts[0] == "app" and len(parts) >= 3:
        module = parts[1]   # jobs, routers, services, clients, ...
    elif file_type == "md":
        if parts[0] == "docs" and len(parts) >= 2:
            category = parts[1]     # specs, reference, onboarding, ...
        elif parts[0] == "rules":
            category = "rules"

    return module, category


def split_by_tokens(text: str, max_tokens: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Простое разбиение по токенам с перекрытием."""
    enc = _get_encoding()
    tokens = enc.encode(text)

    if not tokens:
        return []
    if len(tokens) <= max_tokens:
        return [text] if text.strip() else []

    chunks = []
    start = 0
    while start < len(tokens):
        end = min(start + max_tokens, len(tokens))
        chunk_text = enc.decode(tokens[start:end])
        if chunk_text.strip():
            chunks.append(chunk_text)
        start += max_tokens - overlap

    return chunks


def chunk_python(content: str) -> list[str]:
    """
    AST-aware chunking для Python:
    - Каждая функция/класс → отдельный чанк (если помещается)
    - Большие блоки режутся по токенам с overlap
    - Глобальный код (imports, constants) → первый чанк
    """
    enc = _get_encoding()

    try:
        tree = ast.parse(content)
    except SyntaxError:
        return split_by_tokens(content)

    lines = content.splitlines(keepends=True)

    # Собираем top-level узлы (функции и классы), без вложенных
    top_level: list[tuple[int, int]] = []
    last_end = 0
    for node in sorted(ast.walk(tree), key=lambda n: getattr(n, "lineno", 0)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not hasattr(node, "lineno") or not hasattr(node, "end_lineno"):
                continue
            start_l = node.lineno
            end_l = node.end_lineno
            if end_l is None:
                continue
            if start_l > last_end:
                top_level.append((start_l, end_l))
                last_end = end_l

    if not top_level:
        return split_by_tokens(content)

    chunks = []

    # Глобальный код (вне функций/классов)
    node_lines: set[int] = set()
    for s, e in top_level:
        node_lines.update(range(s, e + 1))
    global_text = "".join(
        line for i, line in enumerate(lines, 1) if i not in node_lines
    ).strip()
    if global_text and len(enc.encode(global_text)) > 20:
        chunks.append(global_text)

    # Сами функции/классы
    for start_l, end_l in top_level:
        block = "".join(lines[start_l - 1:end_l]).strip()
        if not block:
            continue
        token_count = len(enc.encode(block))
        if token_count <= CHUNK_SIZE:
            chunks.append(block)
        else:
            chunks.extend(split_by_tokens(block))

    return chunks if chunks else split_by_tokens(content)


def chunk_markdown(content: str) -> list[str]:
    """
    Header-based chunking для Markdown:
    - Каждый блок между заголовками (#, ##, ###) → чанк
    - Большие секции режутся по токенам
    """
    lines = content.splitlines(keepends=True)
    sections: list[str] = []
    current: list[str] = []

    for line in lines:
        if line.startswith("#") and current:
            text = "".join(current).strip()
            if text:
                sections.append(text)
            current = [line]
        else:
            current.append(line)

    if current:
        text = "".join(current).strip()
        if text:
            sections.append(text)

    chunks = []
    for section in sections:
        enc = _get_encoding()
        if len(enc.encode(section)) <= CHUNK_SIZE:
            chunks.append(section)
        else:
            chunks.extend(split_by_tokens(section))

    return chunks if chunks else split_by_tokens(content)


def chunk_file(content: str, file_type: str) -> list[str]:
    if file_type == "py":
        return chunk_python(content)
    elif file_type == "md":
        return chunk_markdown(content)
    return split_by_tokens(content)


# --- Основная логика ---

async def _get_embeddings(
    texts: list[str],
    api_key: str,
    proxy: Optional[str],
) -> list[list[float]]:
    """Батчевый запрос к Jina AI (через SOCKS5-прокси если задан JINA_PROXY_URL).
    
    Автоматически повторяет при 429 с экспоненциальной задержкой.
    """
    client_kwargs: dict = {"timeout": 60.0}
    if proxy:
        client_kwargs["proxy"] = proxy

    for attempt in range(5):
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.post(
                JINA_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": EMBEDDING_MODEL, "input": texts},
            )
            if resp.status_code == 429:
                wait = 2 ** attempt * 3  # 3, 6, 12, 24, 48 сек
                logger.warning(f"[reindex] Jina 429, жду {wait}с (попытка {attempt+1}/5)")
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            return [
                item["embedding"]
                for item in sorted(data["data"], key=lambda x: x["index"])
            ]

    raise RuntimeError("Jina AI: превышен лимит попыток (429)")


async def reindex() -> None:
    """Переиндексация кодовой базы: инкрементальная, по MD5-хешу файлов."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        try:
            from dotenv import load_dotenv
            load_dotenv(BASE_DIR / ".env")
            db_url = os.getenv("DATABASE_URL")
        except ImportError:
            pass

    if not db_url:
        raise RuntimeError(
            "DATABASE_URL не задан. Укажи в .env или переменных окружения."
        )

    jina_api_key = os.getenv("JINA_API_KEY", "")
    if not jina_api_key:
        raise RuntimeError(
            "JINA_API_KEY не задан. Укажи в .env или переменных окружения."
        )

    jina_proxy = os.getenv("JINA_PROXY_URL")  # socks5://user:pass@host:1080
    if jina_proxy:
        logger.info(f"[reindex] Прокси: {jina_proxy.split('@')[-1]}")
    else:
        logger.info("[reindex] Прокси не задан, прямое подключение")

    conn: asyncpg.Connection = await asyncpg.connect(db_url)
    try:
        await _run_reindex(conn, jina_api_key, jina_proxy)
    finally:
        await conn.close()


async def _run_reindex(
    conn: asyncpg.Connection,
    jina_api_key: str,
    jina_proxy: Optional[str],
) -> None:
    # Сканируем файлы
    all_files: list[tuple[Path, str]] = []
    for base_path, pattern in SCAN_TARGETS:
        if not base_path.exists():
            logger.warning(f"[reindex] Директория не найдена: {base_path}")
            continue
        for file_path in sorted(base_path.glob(pattern)):
            if file_path.is_file() and should_index(file_path):
                rel_path = str(file_path.relative_to(BASE_DIR))
                all_files.append((file_path, rel_path))

    logger.info(f"[reindex] Найдено файлов: {len(all_files)}")

    # Загружаем известные хеши из БД
    rows = await conn.fetch(
        "SELECT DISTINCT ON (file_path) file_path, file_hash FROM code_chunks ORDER BY file_path"
    )
    existing_hashes: dict[str, str] = {r["file_path"]: r["file_hash"] for r in rows}

    indexed = 0
    skipped = 0
    errors = 0

    for file_path, rel_path in all_files:
        try:
            file_hash = file_md5(file_path)

            if existing_hashes.get(rel_path) == file_hash:
                skipped += 1
                continue

            content = file_path.read_text(encoding="utf-8", errors="replace")
            if not content.strip():
                skipped += 1
                continue

            file_type = file_path.suffix.lstrip(".")
            module, category = extract_module_category(rel_path, file_type)
            chunks = chunk_file(content, file_type)

            if not chunks:
                skipped += 1
                continue

            # Генерация embeddings через Jina AI (батчами)
            embeddings: list[list[float]] = []
            for i in range(0, len(chunks), BATCH_SIZE):
                batch = chunks[i : i + BATCH_SIZE]
                batch_embs = await _get_embeddings(batch, jina_api_key, jina_proxy)
                embeddings.extend(batch_embs)

            async with conn.transaction():
                for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
                    await conn.execute(
                        """
                        INSERT INTO code_chunks
                            (file_path, chunk_index, content, embedding, file_hash, file_type, module, category)
                        VALUES ($1, $2, $3, $4::vector, $5, $6, $7, $8)
                        ON CONFLICT (file_path, chunk_index)
                        DO UPDATE SET
                            content    = EXCLUDED.content,
                            embedding  = EXCLUDED.embedding,
                            file_hash  = EXCLUDED.file_hash,
                            module     = EXCLUDED.module,
                            category   = EXCLUDED.category,
                            updated_at = NOW()
                        """,
                        rel_path, i, chunk,
                        _format_vector(emb),
                        file_hash, file_type, module, category,
                    )

                # Удалить устаревшие чанки если файл стал короче
                await conn.execute(
                    "DELETE FROM code_chunks WHERE file_path = $1 AND chunk_index >= $2",
                    rel_path, len(chunks),
                )

            indexed += 1
            logger.info(f"[reindex]   ✓ {rel_path} → {len(chunks)} чанков")

        except Exception as e:
            errors += 1
            logger.error(f"[reindex]   ✗ {rel_path}: {e}", exc_info=True)

    # Удалить чанки файлов, которых больше нет
    all_rel_paths = [rp for _, rp in all_files]
    if all_rel_paths:
        deleted = await conn.execute(
            "DELETE FROM code_chunks WHERE file_path != ALL($1::text[])",
            all_rel_paths,
        )
        logger.info(f"[reindex] Удалено устаревших записей: {deleted}")

    logger.info(
        f"[reindex] Готово: {indexed} обновлено, {skipped} пропущено, {errors} ошибок"
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    asyncio.run(reindex())
