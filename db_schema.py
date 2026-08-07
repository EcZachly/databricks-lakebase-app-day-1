"""
Idempotent database schema initialization for the Support Ticket App.

This module creates the required PostgreSQL tables and indexes in Lakebase.
It is safe to run multiple times (idempotent) - existing tables are not dropped
and existing data is preserved.

Schema Design:

1. tickets table:
   - Stores support tickets with status tracking
   - Each ticket has a unique auto-generated ID
   - Status is constrained to: open, in_progress, resolved
   - Priority is constrained to: low, medium, high
   - Indexed on status, priority, and created_at for efficient querying

2. ticket_messages table:
   - Stores messages/comments associated with tickets
   - Each message has an auto-generated ID
   - Foreign key relationship to tickets table
   - CASCADE DELETE: When a ticket is deleted, all its messages are automatically
     deleted. This is appropriate for a support ticket system where messages
     are dependent on their parent ticket and have no value without it.
   - Indexed on ticket_id for efficient message retrieval per ticket

Usage:
    from db_schema import initialize_schema
    initialize_schema()  # Call during app startup
"""

import logging

import lakebase

logger = logging.getLogger(__name__)


def initialize_schema():
    """
    Create all required tables and indexes if they don't exist.
    This function is idempotent and safe to call on every app deployment.
    """
    logger.info("Initializing database schema...")

    # Create tickets table
    lakebase.run_write(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            priority TEXT NOT NULL DEFAULT 'medium',
            category TEXT,
            created_by TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT tickets_status_check CHECK (status IN ('open', 'in_progress', 'resolved')),
            CONSTRAINT tickets_priority_check CHECK (priority IN ('low', 'medium', 'high'))
        )
        """
    )
    logger.info("✓ tickets table created/verified")

    # Create ticket_messages table with CASCADE DELETE
    lakebase.run_write(
        """
        CREATE TABLE IF NOT EXISTS ticket_messages (
            message_id SERIAL PRIMARY KEY,
            ticket_id INTEGER NOT NULL,
            message_text TEXT NOT NULL,
            author TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ticket_messages_message_text_check CHECK (length(trim(message_text)) > 0),
            CONSTRAINT ticket_messages_ticket_id_fkey 
                FOREIGN KEY (ticket_id) 
                REFERENCES tickets(ticket_id) 
                ON DELETE CASCADE
        )
        """
    )
    logger.info("✓ ticket_messages table created/verified")

    # Create indexes for efficient querying
    # These are idempotent - IF NOT EXISTS prevents errors on repeated runs
    lakebase.run_write(
        "CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status)"
    )
    lakebase.run_write(
        "CREATE INDEX IF NOT EXISTS idx_tickets_priority ON tickets(priority)"
    )
    lakebase.run_write(
        "CREATE INDEX IF NOT EXISTS idx_tickets_created_at ON tickets(created_at)"
    )
    lakebase.run_write(
        "CREATE INDEX IF NOT EXISTS idx_ticket_messages_ticket_id ON ticket_messages(ticket_id)"
    )
    logger.info("✓ All indexes created/verified")

    logger.info("Database schema initialization complete")
