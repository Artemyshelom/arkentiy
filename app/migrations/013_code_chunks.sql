-- Миграция 013: RAG-поиск по кодовой базе (pgvector + code_chunks)
-- Использует: Jina AI jina-embeddings-v2-base-code (768-dim)
-- Требует: PostgreSQL 14+ с pgvector extension

-- Расширение pgvector
CREATE EXTENSION IF NOT EXISTS vector;

-- Таблица чанков кодовой базы
CREATE TABLE IF NOT EXISTS code_chunks (
    id          SERIAL PRIMARY KEY,
    file_path   TEXT NOT NULL,           -- "app/jobs/daily_report.py"
    chunk_index INTEGER NOT NULL,        -- порядковый номер чанка в файле
    content     TEXT NOT NULL,           -- текст чанка (400-800 токенов)
    embedding   vector(768),             -- jina-embeddings-v2-base-code (768-dim)
    file_hash   TEXT NOT NULL,           -- MD5 файла (для инкрементального обновления)
    file_type   TEXT NOT NULL,           -- "py" или "md"
    module      TEXT,                    -- "jobs", "routers", "services", "clients" и т.д.
    category    TEXT,                    -- "specs", "reference", "rules", "onboarding" и т.д.
    updated_at  TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT code_chunks_unique UNIQUE (file_path, chunk_index)
);

-- HNSW-индекс (лучше IVFFlat для малых объёмов, не требует lists)
CREATE INDEX IF NOT EXISTS idx_code_chunks_embedding
    ON code_chunks USING hnsw (embedding vector_cosine_ops);

-- Вспомогательные индексы для фильтрации
CREATE INDEX IF NOT EXISTS idx_code_chunks_file_type ON code_chunks (file_type);
CREATE INDEX IF NOT EXISTS idx_code_chunks_module    ON code_chunks (module);
CREATE INDEX IF NOT EXISTS idx_code_chunks_category  ON code_chunks (category);
CREATE INDEX IF NOT EXISTS idx_code_chunks_file_path ON code_chunks (file_path);
