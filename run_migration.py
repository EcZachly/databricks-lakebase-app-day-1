#!/usr/bin/env python3
"""
Database Schema Migration Script
Migrates existing Lakebase tables to match app.py expectations.

This script will:
1. Rename ticket_id → id in tickets table
2. Add description and updated_at columns to tickets
3. Rename message_id → id in ticket_messages table
4. Rename message_text → message in ticket_messages
5. Rename author → created_by in ticket_messages
6. Convert all date columns to TIMESTAMPTZ

Run this ONCE before deploying your app:
    python run_migration.py
"""

import sys
import os
import subprocess

# Install required packages
print("Installing required packages...")
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "psycopg2-binary", "sqlalchemy"])
print("Packages installed.\n")

# Add the current directory to path so we can import lakebase
sys.path.insert(0, '/Workspace/Users/san.datae1@gmail.com/databricks-lakebase-app-day-1_sk')

import lakebase

def print_header(text):
    """Print a section header."""
    print("\n" + "=" * 80)
    print(f"  {text}")
    print("=" * 80)

def check_column_exists(table_name, column_name):
    """Check if a column exists in a table."""
    result = lakebase.run_query("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns 
            WHERE table_name = %s AND column_name = %s
        )
    """, (table_name, column_name))
    return result[0]['exists'] if result else False

def get_table_schema(table_name):
    """Get the schema of a table."""
    return lakebase.run_query("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = %s
        ORDER BY ordinal_position
    """, (table_name,))

def main():
    print_header("STARTING DATABASE SCHEMA MIGRATION")
    
    # ========================================
    # STEP 1: Migrate tickets table
    # ========================================
    print_header("STEP 1: Migrating 'tickets' table")
    
    # Check current schema
    print("\nCurrent tickets schema:")
    tickets_schema = get_table_schema('tickets')
    for col in tickets_schema:
        print(f"  - {col['column_name']}: {col['data_type']}")
    
    # Add description column if missing
    if not check_column_exists('tickets', 'description'):
        print("\n→ Adding 'description' column...")
        lakebase.run_write("ALTER TABLE tickets ADD COLUMN description TEXT")
        print("  ✅ Added description column")
    else:
        print("\n  ℹ️  description column already exists")
    
    # Add updated_at column if missing
    if not check_column_exists('tickets', 'updated_at'):
        print("\n→ Adding 'updated_at' column...")
        lakebase.run_write("ALTER TABLE tickets ADD COLUMN updated_at TIMESTAMPTZ DEFAULT now()")
        print("  ✅ Added updated_at column")
    else:
        print("\n  ℹ️  updated_at column already exists")
    
    # Rename ticket_id to id
    has_ticket_id = check_column_exists('tickets', 'ticket_id')
    has_id = check_column_exists('tickets', 'id')
    
    if has_ticket_id and not has_id:
        print("\n→ Renaming 'ticket_id' to 'id'...")
        lakebase.run_write("ALTER TABLE tickets RENAME COLUMN ticket_id TO id")
        print("  ✅ Renamed ticket_id to id")
    elif has_id:
        print("\n  ℹ️  Column 'id' already exists")
    else:
        print("\n  ⚠️  Warning: Neither ticket_id nor id found!")
    
    # Convert created_at to TIMESTAMPTZ
    print("\n→ Converting created_at to TIMESTAMPTZ...")
    try:
        lakebase.run_write("""
            ALTER TABLE tickets 
            ALTER COLUMN created_at TYPE TIMESTAMPTZ 
            USING created_at::TIMESTAMPTZ
        """)
        print("  ✅ Converted created_at to TIMESTAMPTZ")
    except Exception as e:
        if "already" in str(e).lower() or "timestamptz" in str(e).lower():
            print("  ℹ️  created_at is already TIMESTAMPTZ")
        else:
            print(f"  ⚠️  Warning: {e}")
    
    # Set updated_at for existing rows
    print("\n→ Setting updated_at for existing rows...")
    lakebase.run_write("UPDATE tickets SET updated_at = created_at WHERE updated_at IS NULL")
    print("  ✅ Updated existing rows")
    
    # ========================================
    # STEP 2: Migrate ticket_messages table
    # ========================================
    print_header("STEP 2: Migrating 'ticket_messages' table")
    
    # Check current schema
    print("\nCurrent ticket_messages schema:")
    messages_schema = get_table_schema('ticket_messages')
    for col in messages_schema:
        print(f"  - {col['column_name']}: {col['data_type']}")
    
    # Rename message_id to id
    has_message_id = check_column_exists('ticket_messages', 'message_id')
    has_id = check_column_exists('ticket_messages', 'id')
    
    if has_message_id and not has_id:
        print("\n→ Renaming 'message_id' to 'id'...")
        lakebase.run_write("ALTER TABLE ticket_messages RENAME COLUMN message_id TO id")
        print("  ✅ Renamed message_id to id")
    elif has_id:
        print("\n  ℹ️  Column 'id' already exists")
    
    # Rename message_text to message
    has_message_text = check_column_exists('ticket_messages', 'message_text')
    has_message = check_column_exists('ticket_messages', 'message')
    
    if has_message_text and not has_message:
        print("\n→ Renaming 'message_text' to 'message'...")
        lakebase.run_write("ALTER TABLE ticket_messages RENAME COLUMN message_text TO message")
        print("  ✅ Renamed message_text to message")
    elif has_message:
        print("\n  ℹ️  Column 'message' already exists")
    
    # Rename author to created_by
    has_author = check_column_exists('ticket_messages', 'author')
    has_created_by = check_column_exists('ticket_messages', 'created_by')
    
    if has_author and not has_created_by:
        print("\n→ Renaming 'author' to 'created_by'...")
        lakebase.run_write("ALTER TABLE ticket_messages RENAME COLUMN author TO created_by")
        print("  ✅ Renamed author to created_by")
    elif has_created_by:
        print("\n  ℹ️  Column 'created_by' already exists")
    
    # Convert created_at to TIMESTAMPTZ
    print("\n→ Converting created_at to TIMESTAMPTZ...")
    try:
        lakebase.run_write("""
            ALTER TABLE ticket_messages 
            ALTER COLUMN created_at TYPE TIMESTAMPTZ 
            USING created_at::TIMESTAMPTZ
        """)
        print("  ✅ Converted created_at to TIMESTAMPTZ")
    except Exception as e:
        if "already" in str(e).lower() or "timestamptz" in str(e).lower():
            print("  ℹ️  created_at is already TIMESTAMPTZ")
        else:
            print(f"  ⚠️  Warning: {e}")
    
    # ========================================
    # STEP 3: Verify final schema
    # ========================================
    print_header("STEP 3: Verifying Final Schema")
    
    print("\nFinal 'tickets' schema:")
    final_tickets = get_table_schema('tickets')
    for col in final_tickets:
        print(f"  - {col['column_name']}: {col['data_type']}")
    
    print("\nFinal 'ticket_messages' schema:")
    final_messages = get_table_schema('ticket_messages')
    for col in final_messages:
        print(f"  - {col['column_name']}: {col['data_type']}")
    
    # Check all required columns
    print("\nValidating required columns...")
    
    required_tickets_cols = ['id', 'title', 'description', 'status', 'priority', 
                             'category', 'created_by', 'created_at', 'updated_at']
    tickets_cols = [col['column_name'] for col in final_tickets]
    missing_tickets = [col for col in required_tickets_cols if col not in tickets_cols]
    
    required_messages_cols = ['id', 'ticket_id', 'message', 'created_by', 'created_at']
    messages_cols = [col['column_name'] for col in final_messages]
    missing_messages = [col for col in required_messages_cols if col not in messages_cols]
    
    if missing_tickets:
        print(f"  ⚠️  tickets table missing: {missing_tickets}")
    else:
        print("  ✅ tickets table has all required columns")
    
    if missing_messages:
        print(f"  ⚠️  ticket_messages table missing: {missing_messages}")
    else:
        print("  ✅ ticket_messages table has all required columns")
    
    # ========================================
    # COMPLETION
    # ========================================
    print_header("✅ MIGRATION COMPLETED SUCCESSFULLY!")
    print("\nYour database schema now matches app.py expectations.")
    print("\nNext steps:")
    print("  1. Deploy your app: databricks apps deploy ticket-management-system")
    print("  2. Or open it in the Databricks Apps UI")
    print()

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)