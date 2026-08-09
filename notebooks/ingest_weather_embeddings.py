"""Generate and persist weather chunk embeddings into Lakebase/pgvector."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from psycopg2.extras import execute_values
from sentence_transformers import SentenceTransformer

import lakebase

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
BATCH_SIZE = 64


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    clean_text = (text or "").strip()
    if not clean_text:
        return []
    if len(clean_text) <= chunk_size:
        return [clean_text]

    chunks: list[str] = []
    step = max(1, chunk_size - overlap)
    start = 0
    while start < len(clean_text):
        chunk = clean_text[start : start + chunk_size].strip()
        if chunk:
            chunks.append(chunk)
        if start + chunk_size >= len(clean_text):
            break
        start += step
    return chunks


def ensure_weather_schema() -> None:
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS weather_documents (
                    id TEXT PRIMARY KEY,
                    location TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    headline TEXT,
                    narrative_text TEXT NOT NULL,
                    issued_at TIMESTAMPTZ,
                    effective_at TIMESTAMPTZ,
                    payload JSONB NOT NULL,
                    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS weather_embeddings (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES weather_documents(id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL,
                    chunk_text TEXT NOT NULL,
                    embedding vector(384) NOT NULL,
                    model_name TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_weather_embeddings_document_chunk ON weather_embeddings (document_id, chunk_index)"
            )
            conn.commit()


def fetch_weather_documents() -> list[dict]:
    sql = """
        SELECT d.id, d.narrative_text
        FROM weather_documents d
        WHERE COALESCE(d.narrative_text, '') <> ''
    """
    return lakebase.run_query(sql)


def to_vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{float(value):.8f}" for value in values) + "]"


def make_embedding_id(document_id: str, chunk_index: int) -> str:
    return hashlib.sha256(f"{document_id}:{chunk_index}".encode("utf-8")).hexdigest()


def insert_embeddings(rows: list[tuple]) -> int:
    if not rows:
        return 0

    sql = """
        INSERT INTO weather_embeddings (
            id, document_id, chunk_index, chunk_text, embedding, model_name, created_at
        ) VALUES %s
        ON CONFLICT (id) DO NOTHING
    """

    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                sql,
                rows,
                template="(%s,%s,%s,%s,%s::vector,%s,%s)",
            )
            conn.commit()
            return cur.rowcount


def main() -> None:
    ensure_weather_schema()

    docs = fetch_weather_documents()
    if not docs:
        print("No weather_documents found. Run /weather/sync first.")
        return

    model = SentenceTransformer(MODEL_NAME)

    total_inserted = 0
    pending_rows: list[tuple] = []

    for doc in docs:
        document_id = doc["id"]
        chunks = chunk_text(doc["narrative_text"])
        if not chunks:
            continue

        vectors = model.encode(chunks, normalize_embeddings=True)
        now = datetime.now(UTC)

        for idx, chunk in enumerate(chunks):
            vector_literal = to_vector_literal(vectors[idx].tolist())
            pending_rows.append(
                (
                    make_embedding_id(document_id, idx),
                    document_id,
                    idx,
                    chunk,
                    vector_literal,
                    MODEL_NAME,
                    now,
                )
            )

            if len(pending_rows) >= BATCH_SIZE:
                total_inserted += insert_embeddings(pending_rows)
                pending_rows = []

    if pending_rows:
        total_inserted += insert_embeddings(pending_rows)

    print(f"Embedding ingestion complete. Rows inserted: {total_inserted}")


if __name__ == "__main__":
    main()
