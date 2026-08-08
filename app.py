"""
Databricks App - Ticket Management System:
- Serves a Flask API for ticket management
- Reads/writes to Lakebase (Databricks-managed Postgres) via lakebase.py
- Manages tickets and ticket_messages tables in tickets_mgmt database

Run locally:
    python app.py
Deploy as a Databricks App using app.yaml.
"""

import logging
import os
from datetime import datetime
from decimal import Decimal

from databricks.sdk import WorkspaceClient
from flask import Flask, jsonify, render_template, request
from flask.json.provider import DefaultJSONProvider

import lakebase

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ticket-app")


class CustomJSONProvider(DefaultJSONProvider):
    """Custom JSON provider to handle datetime and Decimal objects."""
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


app = Flask(__name__)
app.json = CustomJSONProvider(app)
_w = WorkspaceClient()


def ensure_tables():
    """Ensure the tickets and ticket_messages tables exist.
    
    Note: Tables already exist with schema:
    - tickets: ticket_id, title, status, priority, created_by, created_at, category
    - ticket_messages: message_id, ticket_id, message_text, author, created_at
    
    We use SQL aliases in queries to map to frontend expectations (id, message, created_by).
    """
    # Tables already exist - just verify they're there
    lakebase.run_query("SELECT 1 FROM tickets LIMIT 1")
    lakebase.run_query("SELECT 1 FROM ticket_messages LIMIT 1")


def _current_user_email() -> str:
    """
    Resolve the current user's email so the watchlist can be personalized.

    Databricks Apps inject the logged-in user's identity via the
    X-Forwarded-Email header on every request. Fall back to the Databricks
    SDK's current_user API for local development where that header isn't set.
    """
    header_email = request.headers.get("X-Forwarded-Email")
    if header_email:
        return header_email
    return _w.current_user.me().user_name


def _to_db_format(value: str, field_type: str) -> str:
    """Convert frontend format to database format.
    
    Frontend uses lowercase-with-hyphens: 'in-progress', 'open', 'medium'
    Database uses UPPERCASE_WITH_UNDERSCORES: 'IN_PROGRESS', 'OPEN', 'MEDIUM'
    """
    if not value:
        return value
    # Replace hyphens with underscores and convert to uppercase
    return value.replace('-', '_').upper()


def _from_db_format(value: str) -> str:
    """Convert database format to frontend format.
    
    Database uses UPPERCASE_WITH_UNDERSCORES: 'IN_PROGRESS', 'OPEN', 'MEDIUM'
    Frontend uses lowercase-with-hyphens: 'in-progress', 'open', 'medium'
    """
    if not value:
        return value
    # Replace underscores with hyphens and convert to lowercase
    return value.replace('_', '-').lower()


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
    """Ticket management UI."""
    return render_template("index.html")


@app.route("/api/tickets", methods=["GET"])
def get_tickets():
    """Get all tickets with optional filtering and sorting."""
    ensure_tables()
    
    status = request.args.get("status")
    priority = request.args.get("priority")
    category = request.args.get("category")
    search = request.args.get("search")
    sort_by = request.args.get("sort_by", "created_at")  # Default sort by created_at
    sort_order = request.args.get("sort_order", "desc").lower()  # Default descending
    
    query = """
        SELECT 
            t.ticket_id as id, 
            t.title, 
            '' as description, 
            t.status, 
            t.priority, 
            t.category, 
            t.created_by, 
            t.created_at, 
            COALESCE(MAX(m.created_at), t.created_at) as updated_at
        FROM tickets t
        LEFT JOIN ticket_messages m ON t.ticket_id = m.ticket_id
        WHERE 1=1
    """
    params = []
    
    if status:
        query += " AND t.status = %s"
        params.append(_to_db_format(status, 'status'))  # Convert to DB format
    if priority:
        query += " AND t.priority = %s"
        params.append(_to_db_format(priority, 'priority'))  # Convert to DB format
    if category:
        query += " AND t.category = %s"
        params.append(category)
    if search:
        query += " AND t.title ILIKE %s"
        search_param = f"%{search}%"
        params.append(search_param)
    
    # Group by ticket columns to support the MAX aggregate
    query += " GROUP BY t.ticket_id, t.title, t.status, t.priority, t.category, t.created_by, t.created_at"
    
    # Validate and apply sorting
    valid_sort_columns = {
        "id": "t.ticket_id",
        "created_at": "t.created_at",
        "updated_at": "updated_at",  # Use the calculated updated_at from MAX
        "title": "t.title",
        "status": "t.status",
        "priority": "t.priority"
    }
    
    sort_column = valid_sort_columns.get(sort_by, "t.created_at")
    sort_direction = "ASC" if sort_order == "asc" else "DESC"
    
    query += f" ORDER BY {sort_column} {sort_direction}"
    
    rows = lakebase.run_query(query, tuple(params) if params else None)
    # Convert DB format back to frontend format
    result = []
    for row in rows:
        ticket = dict(row)
        ticket['status'] = _from_db_format(ticket['status'])
        ticket['priority'] = _from_db_format(ticket['priority'])
        result.append(ticket)
    return jsonify(result)


@app.route("/api/tickets/<int:ticket_id>", methods=["GET"])
def get_ticket(ticket_id):
    """Get a single ticket with its messages."""
    ensure_tables()
    
    ticket = lakebase.run_query_one(
        "SELECT ticket_id as id, title, '' as description, status, priority, category, created_by, created_at, created_at as updated_at FROM tickets WHERE ticket_id = %s",
        (ticket_id,)
    )
    
    if not ticket:
        return jsonify({"error": "Ticket not found"}), 404
    
    messages = lakebase.run_query(
        "SELECT message_id as id, ticket_id, message_text as message, author as created_by, created_at FROM ticket_messages WHERE ticket_id = %s ORDER BY created_at ASC",
        (ticket_id,)
    )
    
    # Convert DB format to frontend format
    ticket_dict = dict(ticket)
    ticket_dict['status'] = _from_db_format(ticket_dict['status'])
    ticket_dict['priority'] = _from_db_format(ticket_dict['priority'])
    
    return jsonify({"ticket": ticket_dict, "messages": [dict(msg) for msg in messages]})


@app.route("/api/tickets", methods=["POST"])
def create_ticket():
    """Create a new ticket."""
    ensure_tables()
    
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400
    
    data = request.json
    title = data.get("title", "").strip()
    # Note: description field doesn't exist in DB, ignored
    status = data.get("status", "open").strip()
    priority = data.get("priority", "medium").strip()
    category = data.get("category", "").strip()
    
    if not title:
        return jsonify({"error": "Title is required"}), 400
    
    if status not in ["open", "in-progress", "resolved", "closed"]:
        return jsonify({"error": "Invalid status"}), 400
    
    if priority not in ["low", "medium", "high", "critical"]:
        return jsonify({"error": "Invalid priority"}), 400
    
    email = _current_user_email()
    
    # Convert to DB format before inserting
    db_status = _to_db_format(status, 'status')
    db_priority = _to_db_format(priority, 'priority')
    
    ticket = lakebase.run_write_returning(
        """
        INSERT INTO tickets (title, status, priority, category, created_by, created_at)
        VALUES (%s, %s, %s, %s, %s, CURRENT_DATE)
        RETURNING ticket_id as id, title, '' as description, status, priority, category, created_by, created_at, created_at as updated_at
        """,
        (title, db_status, db_priority, category or None, email)
    )
    
    # Convert back to frontend format
    ticket_dict = dict(ticket)
    ticket_dict['status'] = _from_db_format(ticket_dict['status'])
    ticket_dict['priority'] = _from_db_format(ticket_dict['priority'])
    
    return jsonify(ticket_dict), 201


@app.route("/api/tickets/<int:ticket_id>", methods=["PUT"])
def update_ticket(ticket_id):
    """Update an existing ticket."""
    ensure_tables()
    
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400
    
    data = request.json
    
    # Build dynamic update query
    updates = []
    params = []
    
    if "title" in data:
        title = data["title"].strip()
        if not title:
            return jsonify({"error": "Title cannot be empty"}), 400
        updates.append("title = %s")
        params.append(title)
    
    # Note: description field doesn't exist in DB, ignored
    
    if "status" in data:
        status = data["status"].strip()
        if status not in ["open", "in-progress", "resolved", "closed"]:
            return jsonify({"error": "Invalid status"}), 400
        updates.append("status = %s")
        params.append(_to_db_format(status, 'status'))  # Convert to DB format
    
    if "priority" in data:
        priority = data["priority"].strip()
        if priority not in ["low", "medium", "high", "critical"]:
            return jsonify({"error": "Invalid priority"}), 400
        updates.append("priority = %s")
        params.append(_to_db_format(priority, 'priority'))  # Convert to DB format
    
    if "category" in data:
        updates.append("category = %s")
        params.append(data["category"].strip() or None)
    
    if not updates:
        return jsonify({"error": "No fields to update"}), 400
    
    params.append(ticket_id)
    
    query = f"UPDATE tickets SET {', '.join(updates)} WHERE ticket_id = %s RETURNING ticket_id as id, title, '' as description, status, priority, category, created_by, created_at, created_at as updated_at"
    
    ticket = lakebase.run_write_returning(query, tuple(params))
    
    if not ticket:
        return jsonify({"error": "Ticket not found"}), 404
    
    # Convert back to frontend format
    ticket_dict = dict(ticket)
    ticket_dict['status'] = _from_db_format(ticket_dict['status'])
    ticket_dict['priority'] = _from_db_format(ticket_dict['priority'])
    
    return jsonify(ticket_dict)


@app.route("/api/tickets/<int:ticket_id>", methods=["DELETE"])
def delete_ticket(ticket_id):
    """Delete a ticket and its messages."""
    ensure_tables()
    
    # First verify the ticket exists
    ticket = lakebase.run_query_one(
        "SELECT ticket_id FROM tickets WHERE ticket_id = %s",
        (ticket_id,)
    )
    
    if not ticket:
        return jsonify({"error": "Ticket not found"}), 404
    
    # Delete all messages first (to satisfy foreign key constraint)
    lakebase.run_write(
        "DELETE FROM ticket_messages WHERE ticket_id = %s",
        (ticket_id,)
    )
    
    # Now delete the ticket
    lakebase.run_write(
        "DELETE FROM tickets WHERE ticket_id = %s",
        (ticket_id,)
    )
    
    return jsonify({"success": True})


@app.route("/api/tickets/<int:ticket_id>/messages", methods=["POST"])
def add_message(ticket_id):
    """Add a message to a ticket."""
    ensure_tables()
    
    if not request.is_json:
        return jsonify({"error": "Request must be JSON"}), 400
    
    data = request.json
    message = data.get("message", "").strip()
    
    if not message:
        return jsonify({"error": "Message is required"}), 400
    
    # Verify ticket exists
    ticket = lakebase.run_query_one(
        "SELECT ticket_id FROM tickets WHERE ticket_id = %s",
        (ticket_id,)
    )
    
    if not ticket:
        return jsonify({"error": "Ticket not found"}), 404
    
    email = _current_user_email()
    
    msg = lakebase.run_write_returning(
        """
        INSERT INTO ticket_messages (ticket_id, message_text, author, created_at)
        VALUES (%s, %s, %s, CURRENT_DATE)
        RETURNING message_id as id, ticket_id, message_text as message, author as created_by, created_at
        """,
        (ticket_id, message, email)
    )
    
    return jsonify(dict(msg)), 201


@app.route("/api/statistics", methods=["GET"])
def get_statistics():
    """Get ticket statistics."""
    ensure_tables()
    
    stats = lakebase.run_query_one(
        """
        SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE status = 'OPEN') as open,
            COUNT(*) FILTER (WHERE status = 'IN_PROGRESS') as in_progress,
            COUNT(*) FILTER (WHERE status = 'RESOLVED') as resolved,
            COUNT(*) FILTER (WHERE status = 'CLOSED') as closed,
            COUNT(*) FILTER (WHERE priority = 'CRITICAL') as critical
        FROM tickets
        """
    )
    
    return jsonify(dict(stats) if stats else {})


if __name__ == '__main__':
    host = os.getenv('FLASK_RUN_HOST', '0.0.0.0')
    port = int(os.getenv('FLASK_RUN_PORT', 8000))
    app.run(debug=True, host=host, port=port)
    print(f"Flask app running on http://{host}:{port}")