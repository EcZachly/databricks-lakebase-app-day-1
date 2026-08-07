#!/usr/bin/env python3
"""
ONE-TIME SEED DATA SCRIPT - DO NOT RUN IN PRODUCTION OR DEPLOYMENT PIPELINE

This script inserts 10 realistic example support tickets into the database.
It is intended to be executed MANUALLY ONCE after schema creation for
demonstration, testing, or development purposes.

DO NOT:
- Include this in app.py startup logic
- Run this automatically during deployment
- Commit this as part of the application runtime

TO USE:
1. Ensure the database schema has been initialized (run the app once)
2. Run this script manually: python seed_data.py
3. The script is idempotent - it checks for existing data before inserting
"""

import logging
import sys

import lakebase

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SAMPLE_TICKETS = [
    {
        "title": "Cannot log in to account",
        "description": "I've been trying to log in for the past hour using my usual credentials, but keep getting an 'Invalid username or password' error. I've reset my password twice but still can't access my account. This is urgent as I have a deadline today.",
        "status": "open",
        "priority": "high",
        "category": "Authentication",
        "created_by": "sarah.miller@example.com"
    },
    {
        "title": "Billing discrepancy on last invoice",
        "description": "Our January invoice shows charges for 5 users, but we only have 3 active users in our account. Can someone review this and issue a corrected invoice? Reference invoice #INV-2024-0123.",
        "status": "in_progress",
        "priority": "medium",
        "category": "Billing",
        "created_by": "finance@acmecorp.com"
    },
    {
        "title": "Dashboard loading very slowly",
        "description": "The analytics dashboard is taking 30+ seconds to load since yesterday. This used to load in under 3 seconds. The issue affects all team members.",
        "status": "open",
        "priority": "high",
        "category": "Performance",
        "created_by": "john.davis@techcorp.io"
    },
    {
        "title": "Request to add new team member",
        "description": "Please add emily.chen@example.com to our workspace with admin privileges. She's starting next week and will need access to all current projects.",
        "status": "resolved",
        "priority": "low",
        "category": "User Management",
        "created_by": "hr@example.com"
    },
    {
        "title": "API returning 500 errors",
        "description": "The /api/v1/reports endpoint has been returning 500 Internal Server Error since approximately 2:30 PM EST. This is blocking our automated reporting pipeline. Error occurs for all report types.",
        "status": "in_progress",
        "priority": "high",
        "category": "API",
        "created_by": "devops@startup.dev"
    },
    {
        "title": "Feature request: Dark mode",
        "description": "Would love to see a dark mode option for the UI. Many team members work late hours and would appreciate this feature for reduced eye strain.",
        "status": "open",
        "priority": "low",
        "category": "Feature Request",
        "created_by": "product@innovate.com"
    },
    {
        "title": "Data export not including all records",
        "description": "When I export our customer data to CSV, it only shows 500 records but our dashboard indicates we have 1,247 customers. The export seems to be truncated. I've tried multiple times with the same result.",
        "status": "open",
        "priority": "medium",
        "category": "Data Export",
        "created_by": "analytics@retailco.com"
    },
    {
        "title": "Mobile app crashes on iOS 17",
        "description": "The mobile app crashes immediately after launch on iOS 17. Tested on iPhone 14 Pro and iPhone 15. Works fine on iOS 16. Crash report attached.",
        "status": "open",
        "priority": "high",
        "category": "Mobile",
        "created_by": "qa@mobileapp.io"
    },
    {
        "title": "Documentation outdated for new API version",
        "description": "The API documentation at /docs still references v1 endpoints, but we've been instructed to use v2. Could someone update the docs or point us to the v2 documentation?",
        "status": "resolved",
        "priority": "low",
        "category": "Documentation",
        "created_by": "dev.team@partner.com"
    },
    {
        "title": "Email notifications not being received",
        "description": "None of our team members are receiving email notifications for ticket updates. We've checked spam folders and email settings but nothing is arriving. Our notification email is support@company.org.",
        "status": "in_progress",
        "priority": "medium",
        "category": "Notifications",
        "created_by": "support@company.org"
    }
]


def check_existing_data():
    """Check if there are already tickets in the database."""
    rows = lakebase.run_query("SELECT COUNT(*) as count FROM tickets")
    return rows[0]["count"] if rows else 0


def insert_sample_tickets():
    """Insert sample tickets into the database."""
    logger.info("Starting seed data insertion...")
    
    existing_count = check_existing_data()
    if existing_count > 0:
        logger.warning(
            f"Database already contains {existing_count} ticket(s). "
            "This script is designed for initial seeding only."
        )
        response = input("Do you want to add sample data anyway? (yes/no): ")
        if response.lower() not in ["yes", "y"]:
            logger.info("Seed operation cancelled.")
            return
    
    inserted = 0
    for ticket in SAMPLE_TICKETS:
        try:
            lakebase.run_write(
                """
                INSERT INTO tickets (title, description, status, priority, category, created_by)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    ticket["title"],
                    ticket["description"],
                    ticket["status"],
                    ticket["priority"],
                    ticket["category"],
                    ticket["created_by"]
                )
            )
            inserted += 1
            logger.info(f"✓ Inserted: {ticket['title']}")
        except Exception as e:
            logger.error(f"✗ Failed to insert '{ticket['title']}': {e}")
    
    logger.info(f"\nSeed complete: {inserted}/{len(SAMPLE_TICKETS)} tickets inserted.")


if __name__ == "__main__":
    try:
        insert_sample_tickets()
    except KeyboardInterrupt:
        logger.info("\nSeed operation interrupted by user.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Seed operation failed: {e}")
        sys.exit(1)
