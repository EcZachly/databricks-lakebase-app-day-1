"""
Support Ticket Management App
- Serves a Flask API for managing support tickets
- Stores tickets and messages in Lakebase (Databricks-managed Postgres)
- Provides a simple web UI for creating and viewing tickets

Run locally:
    python app.py
Deploy as a Databricks App using app.yaml.
"""

import logging
import os

from databricks.sdk import WorkspaceClient
from flask import Flask, jsonify, render_template, request

import lakebase
from db_schema import initialize_schema

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ticket-app")

app = Flask(__name__)
_w = WorkspaceClient()

# Initialize database schema on startup
initialize_schema()


def _current_user_email() -> str:
    """
    Resolve the current user's email for ticket attribution.

    Databricks Apps inject the logged-in user's identity via the
    X-Forwarded-Email header on every request. Fall back to the Databricks
    SDK's current_user API for local development where that header isn't set.
    """
    header_email = request.headers.get("X-Forwarded-Email")
    if header_email:
        return header_email
    return _w.current_user.me().user_name


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.errorhandler(Exception)
def handle_exception(err):
    """Ensure all unhandled errors return JSON (not an HTML error page),
    so the frontend's resp.json() call never chokes on HTML."""
    logger.exception("Unhandled exception while processing request")
    status_code = getattr(err, "code", 500)
    if not isinstance(status_code, int):
        status_code = 500
    return jsonify({"error": str(err)}), status_code


@app.route("/")
def index():
    """Render the ticket support UI."""
    return render_template("index.html")


@app.route("/tickets", methods=["GET"])
def get_tickets():
    """Get all tickets, optionally filtered by status or priority."""
    status = request.args.get("status")
    priority = request.args.get("priority")
    
    query = "SELECT * FROM tickets WHERE 1=1"
    params = []
    
    if status:
        query += " AND status = %s"
        params.append(status)
    if priority:
        query += " AND priority = %s"
        params.append(priority)
    
    query += " ORDER BY created_at DESC"
    
    rows = lakebase.run_query(query, tuple(params) if params else None)
    return jsonify(rows)


@app.route("/tickets", methods=["POST"])
def create_ticket():
    """Create a new support ticket."""
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 400
    
    data = request.json
    title = data.get("title", "").strip()
    description = data.get("description", "").strip()
    priority = data.get("priority", "medium").lower()
    category = data.get("category", "").strip() or None
    
    if not title:
        return jsonify({"error": "Title is required"}), 400
    
    if priority not in ["low", "medium", "high"]:
        return jsonify({"error": "Priority must be low, medium, or high"}), 400
    
    created_by = _current_user_email()
    
    rows = lakebase.run_query(
        """
        INSERT INTO tickets (title, description, priority, category, created_by)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING ticket_id, title, description, status, priority, category, created_by, created_at, updated_at
        """,
        (title, description, priority, category, created_by)
    )
    
    return jsonify(rows[0]), 201


@app.route("/tickets/<int:ticket_id>", methods=["GET"])
def get_ticket(ticket_id):
    """Get a single ticket by ID."""
    rows = lakebase.run_query(
        "SELECT * FROM tickets WHERE ticket_id = %s",
        (ticket_id,)
    )
    
    if not rows:
        return jsonify({"error": "Ticket not found"}), 404
    
    return jsonify(rows[0])


@app.route("/tickets/<int:ticket_id>/messages", methods=["GET"])
def get_ticket_messages(ticket_id):
    """Get all messages for a specific ticket."""
    rows = lakebase.run_query(
        "SELECT * FROM ticket_messages WHERE ticket_id = %s ORDER BY created_at ASC",
        (ticket_id,)
    )
    return jsonify(rows)


@app.route("/tickets/<int:ticket_id>/messages", methods=["POST"])
def add_ticket_message(ticket_id):
    """Add a message to a ticket."""
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 400
    
    # Verify ticket exists
    ticket_rows = lakebase.run_query(
        "SELECT ticket_id FROM tickets WHERE ticket_id = %s",
        (ticket_id,)
    )
    if not ticket_rows:
        return jsonify({"error": "Ticket not found"}), 404
    
    data = request.json
    message_text = data.get("message_text", "").strip()
    
    if not message_text:
        return jsonify({"error": "Message text is required and cannot be blank"}), 400
    
    author = _current_user_email()
    
    rows = lakebase.run_query(
        """
        INSERT INTO ticket_messages (ticket_id, message_text, author)
        VALUES (%s, %s, %s)
        RETURNING message_id, ticket_id, message_text, author, created_at
        """,
        (ticket_id, message_text, author)
    )
    
    # Update ticket's updated_at timestamp
    lakebase.run_write(
        "UPDATE tickets SET updated_at = CURRENT_TIMESTAMP WHERE ticket_id = %s",
        (ticket_id,)
    )
    
    return jsonify(rows[0]), 201


@app.route("/tickets/<int:ticket_id>", methods=["PATCH"])
def update_ticket(ticket_id):
    """Update ticket status or priority."""
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 400
    
    data = request.json
    updates = []
    params = []
    
    if "status" in data:
        status = data["status"].lower()
        if status not in ["open", "in_progress", "resolved"]:
            return jsonify({"error": "Status must be open, in_progress, or resolved"}), 400
        updates.append("status = %s")
        params.append(status)
    
    if "priority" in data:
        priority = data["priority"].lower()
        if priority not in ["low", "medium", "high"]:
            return jsonify({"error": "Priority must be low, medium, or high"}), 400
        updates.append("priority = %s")
        params.append(priority)
    
    if not updates:
        return jsonify({"error": "No valid fields to update"}), 400
    
    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(ticket_id)
    
    query = f"UPDATE tickets SET {', '.join(updates)} WHERE ticket_id = %s"
    affected = lakebase.run_write(query, tuple(params))
    
    if affected == 0:
        return jsonify({"error": "Ticket not found"}), 404
    
    # Return updated ticket
    rows = lakebase.run_query(
        "SELECT * FROM tickets WHERE ticket_id = %s",
        (ticket_id,)
    )
    return jsonify(rows[0])


@app.route("/tickets/<int:ticket_id>", methods=["DELETE"])
def delete_ticket(ticket_id):
    """Delete a ticket and all its associated messages."""
    # Verify ticket exists
    ticket_rows = lakebase.run_query(
        "SELECT ticket_id, title FROM tickets WHERE ticket_id = %s",
        (ticket_id,)
    )
    if not ticket_rows:
        return jsonify({"error": "Ticket not found"}), 404
    
    # Delete associated messages first (due to foreign key constraints)
    lakebase.run_write(
        "DELETE FROM ticket_messages WHERE ticket_id = %s",
        (ticket_id,)
    )
    
    # Delete the ticket
    affected = lakebase.run_write(
        "DELETE FROM tickets WHERE ticket_id = %s",
        (ticket_id,)
    )
    
    if affected == 0:
        return jsonify({"error": "Failed to delete ticket"}), 500
    
    return jsonify({
        "message": f"Ticket #{ticket_id} deleted successfully",
        "ticket_id": ticket_id
    }), 200


if __name__ == '__main__':
    host = os.getenv('FLASK_RUN_HOST', '0.0.0.0')
    port = int(os.getenv('FLASK_RUN_PORT', 8000))
    app.run(debug=True, host=host, port=port)
    print(f"Flask app running on http://{host}:{port}")