"""
Lakebase (Databricks-managed Postgres) connection helper.

Reads the connection URL from LAKEBASE_URL when provided (local/dev) and
falls back to Databricks secrets for app deployment.
"""

import base64
import os
from contextlib import contextmanager

import psycopg2
from databricks.sdk import WorkspaceClient
from psycopg2.extras import RealDictCursor
from sqlalchemy import create_engine

_w = WorkspaceClient()

_SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "database")
_KEY = os.environ.get("LAKEBASE_SECRET_KEY", "lakebase-url")


def _lakebase_url() -> str:
    """Fetch and decode the Lakebase connection URL."""
    if os.environ.get("LAKEBASE_URL"):
        return os.environ["LAKEBASE_URL"]

    secret = _w.secrets.get_secret(scope=_SCOPE, key=_KEY)
    return base64.b64decode(secret.value).decode("utf-8")


@contextmanager
def get_connection():
    """Yield a raw psycopg2 connection with a RealDictCursor factory."""
    conn = psycopg2.connect(_lakebase_url(), cursor_factory=RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


def get_engine():
    """Return a SQLAlchemy engine for Lakebase."""
    return create_engine(_lakebase_url())


def run_query(sql: str, params: tuple | dict | None = None) -> list[dict]:
    """Run a read query against Lakebase and return rows as list[dict]."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def run_write(sql: str, params: tuple | dict | None = None) -> int:
    """Run an INSERT/UPDATE/DELETE against Lakebase, return affected row count."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            return cur.rowcount


def ensure_weather_schema() -> None:
    """Create weather tables/indexes needed for ingestion and semantic search."""
    with get_connection() as conn:
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

            try:
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_weather_embeddings_embedding_cosine ON weather_embeddings USING hnsw (embedding vector_cosine_ops)"
                )
                conn.commit()
            except psycopg2.Error:
                conn.rollback()
                try:
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS idx_weather_embeddings_embedding_cosine_ivf ON weather_embeddings USING ivfflat (embedding vector_cosine_ops)"
                    )
                    conn.commit()
                except psycopg2.Error:
                    conn.rollback()
