import logging
import os
from datetime import datetime

from flask import Flask, jsonify, render_template, request
from sqlalchemy import DateTime, ForeignKey, String, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, selectinload, sessionmaker

import lakebase

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("support-ticket-app")

app = Flask(__name__)

VALID_STATUSES = {"Open", "In Progress", "Resolved"}


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


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.errorhandler(Exception)
def handle_exception(err):
    logger.exception("Unhandled exception while processing request")
    status_code = getattr(err, "code", 500)
    if not isinstance(status_code, int):
        status_code = 500
    return jsonify({"error": str(err)}), status_code


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


if __name__ == "__main__":
    host = os.getenv("FLASK_RUN_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_RUN_PORT", 8000))
    app.run(debug=True, host=host, port=port)
