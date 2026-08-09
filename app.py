import json
import logging
import os
from datetime import datetime

import psycopg2
from flask import Flask, jsonify, render_template, request
from psycopg2.extras import execute_values
from sentence_transformers import SentenceTransformer
from sqlalchemy import DateTime, ForeignKey, String, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, selectinload, sessionmaker
from werkzeug.exceptions import BadRequest

import lakebase
from weather_client import WeatherClient, WeatherClientError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("support-ticket-app")

app = Flask(__name__)

VALID_STATUSES = {"Open", "In Progress", "Resolved"}
WEATHER_EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


class Base(DeclarativeBase):
    pass


class Ticket(Base):
    __tablename__ = "tickets"

    ticket_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    messages: Mapped[list["TicketMessage"]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="TicketMessage.created_at",
    )


class TicketMessage(Base):
    __tablename__ = "ticket_messages"

    message_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.ticket_id", ondelete="CASCADE"), nullable=False
    )
    message_text: Mapped[str] = mapped_column(String(2000), nullable=False)
    author: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    ticket: Mapped[Ticket] = relationship(back_populates="messages")


ENGINE = lakebase.get_engine()
SessionLocal = sessionmaker(bind=ENGINE, expire_on_commit=False)
_initialized = False

_weather_client = WeatherClient()
_weather_model_error: str | None = None
try:
    _weather_embedding_model = SentenceTransformer(WEATHER_EMBEDDING_MODEL_NAME)
except Exception as err:  # pragma: no cover
    _weather_embedding_model = None
    _weather_model_error = str(err)
    logger.exception("Failed to load weather embedding model")


SEED_TICKETS = [
    {
        "title": "Cannot access Databricks dashboard",
        "status": "Open",
        "created_by": "alex@example.com",
        "messages": [
            {
                "message_text": "I receive an access denied error on the analytics dashboard.",
                "author": "alex@example.com",
            },
            {
                "message_text": "We are investigating role permissions for your workspace.",
                "author": "support-agent@example.com",
            },
        ],
    },
    {
        "title": "Pipeline job failing nightly",
        "status": "In Progress",
        "created_by": "sam@example.com",
        "messages": [
            {
                "message_text": "The nightly ETL job has failed for two days.",
                "author": "sam@example.com",
            },
            {
                "message_text": "We identified a schema mismatch and are preparing a fix.",
                "author": "support-agent@example.com",
            },
        ],
    },
    {
        "title": "Question about Lakebase connection string",
        "status": "Resolved",
        "created_by": "jamie@example.com",
        "messages": [
            {
                "message_text": "Where can I find the correct Lakebase connection URL?",
                "author": "jamie@example.com",
            },
            {
                "message_text": "Please use the role URL from the database secret scope. Marking resolved.",
                "author": "support-agent@example.com",
            },
        ],
    },
]


def _serialize_ticket(ticket: Ticket) -> dict:
    return {
        "ticket_id": ticket.ticket_id,
        "title": ticket.title,
        "status": ticket.status,
        "created_by": ticket.created_by,
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
    }


def _serialize_message(message: TicketMessage) -> dict:
    return {
        "message_id": message.message_id,
        "ticket_id": message.ticket_id,
        "message_text": message.message_text,
        "author": message.author,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


def ensure_database_ready() -> None:
    global _initialized
    if _initialized:
        return

    Base.metadata.create_all(bind=ENGINE)
    lakebase.ensure_weather_schema()

    with SessionLocal() as session:
        ticket_count = session.scalar(select(func.count()).select_from(Ticket)) or 0
        if ticket_count == 0:
            for seed in SEED_TICKETS:
                ticket = Ticket(
                    title=seed["title"],
                    status=seed["status"],
                    created_by=seed["created_by"],
                )
                for msg in seed["messages"]:
                    ticket.messages.append(
                        TicketMessage(
                            message_text=msg["message_text"],
                            author=msg["author"],
                        )
                    )
                session.add(ticket)
            session.commit()

    _initialized = True


def _validate_status(status: str | None) -> str | None:
    if status in VALID_STATUSES:
        return status
    return None


def _parse_json_body() -> dict:
    payload = request.get_json(silent=False)
    if payload is None or not isinstance(payload, dict):
        raise BadRequest("Request body must be a JSON object")
    return payload


def _embedding_to_vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{float(value):.8f}" for value in values) + "]"


def _upsert_weather_documents(documents: list[dict]) -> int:
    if not documents:
        return 0

    values = [
        (
            doc["id"],
            doc["location"],
            doc["source_type"],
            doc.get("headline") or "",
            doc["narrative_text"],
            doc.get("issued_at"),
            doc.get("effective_at"),
            doc.get("payload") if isinstance(doc.get("payload"), str) else json.dumps(doc.get("payload") or {}),
            doc.get("synced_at") or datetime.utcnow(),
        )
        for doc in documents
    ]

    sql = """
        INSERT INTO weather_documents (
            id, location, source_type, headline, narrative_text,
            issued_at, effective_at, payload, synced_at
        ) VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            location = EXCLUDED.location,
            source_type = EXCLUDED.source_type,
            headline = EXCLUDED.headline,
            narrative_text = EXCLUDED.narrative_text,
            issued_at = EXCLUDED.issued_at,
            effective_at = EXCLUDED.effective_at,
            payload = EXCLUDED.payload,
            synced_at = EXCLUDED.synced_at
    """

    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                sql,
                values,
                template="(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
            )
            conn.commit()

    return len(values)


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.errorhandler(BadRequest)
def handle_bad_request(err):
    message = getattr(err, "description", None) or "Invalid JSON request"
    return jsonify({"error": message}), 400


@app.errorhandler(Exception)
def handle_exception(err):
    logger.exception("Unhandled exception while processing request")
    status_code = getattr(err, "code", 500)
    if not isinstance(status_code, int):
        status_code = 500
    if status_code >= 500:
        return jsonify({"error": "Internal server error"}), status_code
    return jsonify({"error": "Request failed"}), status_code


@app.route("/")
def index():
    ensure_database_ready()
    return render_template("index.html", statuses=sorted(VALID_STATUSES))


@app.route("/tickets", methods=["GET"])
def list_tickets():
    ensure_database_ready()
    with SessionLocal() as session:
        tickets = session.scalars(
            select(Ticket).order_by(Ticket.created_at.desc(), Ticket.ticket_id.desc())
        ).all()
        return jsonify([_serialize_ticket(ticket) for ticket in tickets])


@app.route("/tickets", methods=["POST"])
def create_ticket():
    ensure_database_ready()
    payload = request.get_json(silent=True) or request.form

    title = (payload.get("title") or "").strip()
    created_by = (payload.get("created_by") or "").strip()
    status = _validate_status((payload.get("status") or "Open").strip())

    if not title:
        return jsonify({"error": "title is required"}), 400
    if not created_by:
        return jsonify({"error": "created_by is required"}), 400
    if not status:
        return jsonify({"error": "status must be Open, In Progress, or Resolved"}), 400

    with SessionLocal() as session:
        ticket = Ticket(title=title, created_by=created_by, status=status)
        session.add(ticket)
        session.commit()
        session.refresh(ticket)
        return jsonify(_serialize_ticket(ticket)), 201


@app.route("/tickets/<int:ticket_id>", methods=["GET"])
def get_ticket(ticket_id: int):
    ensure_database_ready()
    with SessionLocal() as session:
        ticket = session.scalar(
            select(Ticket)
            .options(selectinload(Ticket.messages))
            .where(Ticket.ticket_id == ticket_id)
        )
        if not ticket:
            return jsonify({"error": "ticket not found"}), 404

        return jsonify(
            {
                **_serialize_ticket(ticket),
                "messages": [_serialize_message(message) for message in ticket.messages],
            }
        )


@app.route("/tickets/<int:ticket_id>/messages", methods=["POST"])
def add_ticket_message(ticket_id: int):
    ensure_database_ready()
    payload = request.get_json(silent=True) or request.form

    message_text = (payload.get("message_text") or "").strip()
    author = (payload.get("author") or "").strip()

    if not message_text:
        return jsonify({"error": "message_text is required"}), 400
    if not author:
        return jsonify({"error": "author is required"}), 400

    with SessionLocal() as session:
        ticket = session.get(Ticket, ticket_id)
        if not ticket:
            return jsonify({"error": "ticket not found"}), 404

        message = TicketMessage(
            ticket_id=ticket_id,
            message_text=message_text,
            author=author,
        )
        session.add(message)
        session.commit()
        session.refresh(message)
        return jsonify(_serialize_message(message)), 201


@app.route("/tickets/<int:ticket_id>/status", methods=["PATCH"])
def update_ticket_status(ticket_id: int):
    ensure_database_ready()
    payload = request.get_json(silent=True) or request.form

    status = _validate_status((payload.get("status") or "").strip())
    if not status:
        return jsonify({"error": "status must be Open, In Progress, or Resolved"}), 400

    with SessionLocal() as session:
        ticket = session.get(Ticket, ticket_id)
        if not ticket:
            return jsonify({"error": "ticket not found"}), 404

        ticket.status = status
        session.commit()
        return jsonify(_serialize_ticket(ticket))


@app.route("/tickets/<int:ticket_id>", methods=["DELETE"])
def delete_ticket(ticket_id: int):
    ensure_database_ready()
    with SessionLocal() as session:
        ticket = session.get(Ticket, ticket_id)
        if not ticket:
            return jsonify({"error": "ticket not found"}), 404

        session.delete(ticket)
        session.commit()
        return jsonify({"deleted": True, "ticket_id": ticket_id})


@app.route("/weather/sync", methods=["POST"])
def sync_weather_documents():
    ensure_database_ready()

    try:
        payload = _parse_json_body()
    except BadRequest as err:
        return jsonify({"error": getattr(err, "description", "Invalid JSON")}), 400

    locations = payload.get("locations")
    if not isinstance(locations, list) or not locations:
        return jsonify({"error": "locations must be a non-empty array"}), 400

    cleaned_locations = [loc.strip() for loc in locations if isinstance(loc, str) and loc.strip()]
    if not cleaned_locations or len(cleaned_locations) != len(locations):
        return jsonify({"error": "each location must be a non-empty string"}), 400

    limit_raw = payload.get("limit", 50)
    try:
        limit = int(limit_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer"}), 400
    if limit < 1:
        return jsonify({"error": "limit must be at least 1"}), 400

    all_docs: list[dict] = []
    failures: list[dict[str, str]] = []

    for location in cleaned_locations:
        try:
            docs = _weather_client.fetch_documents_for_location(location=location, limit=limit)
            all_docs.extend(docs)
        except WeatherClientError as err:
            logger.warning("Weather sync failed for location '%s': %s", location, err)
            failures.append({"location": location, "error": "Weather data fetch failed for location"})

    if not all_docs and failures:
        return jsonify({"error": "Weather sync failed", "details": failures}), 502

    try:
        synced = _upsert_weather_documents(all_docs)
    except psycopg2.Error:
        logger.exception("Weather document upsert failed")
        return jsonify({"error": "Database error while syncing weather documents"}), 500

    response = {
        "message": "Weather sync completed",
        "documents_synced": synced,
    }
    if failures:
        response["warnings"] = failures
    return jsonify(response)


@app.route("/weather/search", methods=["POST"])
def search_weather_documents():
    ensure_database_ready()

    try:
        payload = _parse_json_body()
    except BadRequest as err:
        return jsonify({"error": getattr(err, "description", "Invalid JSON")}), 400

    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        return jsonify({"error": "query is required and must be a non-empty string"}), 400

    top_k_raw = payload.get("top_k", 5)
    try:
        top_k = int(top_k_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "top_k must be an integer"}), 400

    if _weather_embedding_model is None:
        return jsonify({"error": "Weather embedding model is unavailable"}), 500

    top_k = min(max(top_k, 1), 20)

    try:
        vector = _weather_embedding_model.encode([query.strip()], normalize_embeddings=True)[0]
    except Exception:
        logger.exception("Weather query embedding failed")
        return jsonify({"error": "Failed to embed query"}), 500

    vector_literal = _embedding_to_vector_literal(vector.tolist())

    sql = """
        SELECT
            d.id,
            d.location,
            d.headline,
            d.narrative_text,
            e.chunk_text,
            1 - (e.embedding <=> %s::vector) AS similarity
        FROM weather_embeddings e
        JOIN weather_documents d ON d.id = e.document_id
        ORDER BY e.embedding <=> %s::vector
        LIMIT %s
    """

    try:
        rows = lakebase.run_query(sql, (vector_literal, vector_literal, top_k))
    except psycopg2.Error:
        logger.exception("Weather vector search failed")
        return jsonify({"error": "Database error while searching weather embeddings"}), 500

    if not rows:
        return jsonify({"results": [], "message": "No weather embeddings found. Run /weather/sync and ingest_weather_embeddings first."})

    return jsonify(
        {
            "results": [
                {
                    "id": row["id"],
                    "location": row["location"],
                    "headline": row["headline"],
                    "narrative_text": row["narrative_text"],
                    "chunk_text": row["chunk_text"],
                    "similarity": float(row["similarity"]) if row["similarity"] is not None else None,
                }
                for row in rows
            ]
        }
    )


if __name__ == "__main__":
    host = os.getenv("FLASK_RUN_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_RUN_PORT", 8000))
    app.run(debug=True, host=host, port=port)
