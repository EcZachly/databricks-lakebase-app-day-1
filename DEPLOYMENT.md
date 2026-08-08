# Ticket Management System - Deployment Guide

## ⚠️ IMPORTANT: This is NOT a Notebook!

This Flask application **cannot be run in a Databricks notebook**. You'll get `ModuleNotFoundError: No module named 'flask'` if you try to execute `app.py` as a notebook cell.

**This must be deployed as a Databricks App.**

---

## 🚀 How to Deploy as a Databricks App

### Option 1: Using Databricks Apps UI (Recommended)

1. **Open Databricks Apps**
   - Go to your Databricks workspace
   - Click on **"Apps"** in the left sidebar (or **"Compute" → "Apps"**)

2. **Create New App**
   - Click **"Create App"**
   - Give it a name: `ticket-management-system`

3. **Select Source**
   - Choose **"Workspace"**
   - Navigate to: `/Users/san.datae1@gmail.com/databricks-lakebase-app-day-1_sk`
   - The system will automatically detect `app.yaml`

4. **Deploy**
   - Click **"Create"** or **"Deploy"**
   - Wait for deployment to complete (usually 2-5 minutes)

5. **Access Your App**
   - Once deployed, you'll get a URL like: `https://<workspace>.cloud.databricks.com/apps/<app-id>`
   - Click the URL to open your ticket management system

---

### Option 2: Using Databricks CLI

```bash
# Install Databricks CLI if not already installed
pip install databricks-cli

# Configure authentication
databricks configure --token

# Create and deploy the app
databricks apps create ticket-management-system \
  --source-code-path /Users/san.datae1@gmail.com/databricks-lakebase-app-day-1_sk

# Check deployment status
databricks apps list

# Get app URL
databricks apps get ticket-management-system
```

---

## 📋 Prerequisites

Before deploying, ensure you have:

### 1. ⚠️ CRITICAL: Run Schema Migration First!

Your existing Lakebase database schema doesn't match what app.py expects. You **MUST** run the migration before deploying:

**Option A: Run the Python migration script (Recommended)**
```bash
cd /Workspace/Users/san.datae1@gmail.com/databricks-lakebase-app-day-1_sk
python run_migration.py
```

**Option B: Run SQL manually in your Lakebase database**
```sql
-- Migrate tickets table
ALTER TABLE tickets ADD COLUMN description TEXT;
ALTER TABLE tickets ADD COLUMN updated_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE tickets RENAME COLUMN ticket_id TO id;
ALTER TABLE tickets ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at::TIMESTAMPTZ;
UPDATE tickets SET updated_at = created_at WHERE updated_at IS NULL;

-- Migrate ticket_messages table
ALTER TABLE ticket_messages RENAME COLUMN message_id TO id;
ALTER TABLE ticket_messages RENAME COLUMN message_text TO message;
ALTER TABLE ticket_messages RENAME COLUMN author TO created_by;
ALTER TABLE ticket_messages ALTER COLUMN created_at TYPE TIMESTAMPTZ USING created_at::TIMESTAMPTZ;
```

**What the migration does:**
- Renames `ticket_id` → `id` in tickets table
- Adds `description` and `updated_at` columns to tickets
- Renames `message_id` → `id` in ticket_messages
- Renames `message_text` → `message`
- Renames `author` → `created_by`
- Converts all `date` columns to `TIMESTAMPTZ`

### 2. Lakebase Database Setup

The app requires a Lakebase Postgres database. Make sure:

- **Lakebase endpoint URL** is stored in Databricks Secrets:
  ```python
  # The app reads from:
  # Scope: "database"
  # Key: "lakebase-url"
  ```

- **Format**: `postgresql://username:password@host:port/database_name`

If you haven't set this up yet:

```bash
# Create secret scope (if doesn't exist)
databricks secrets create-scope database

# Add your Lakebase connection URL
databricks secrets put database lakebase-url
# Then paste your Lakebase URL when prompted
```

### 2. Verify Files

Ensure these files exist in your folder:
- ✅ `app.py` - Main Flask application
- ✅ `lakebase.py` - Database connection helper
- ✅ `app.yaml` - App configuration
- ✅ `requirements.txt` - Python dependencies
- ✅ `templates/index.html` - Frontend UI

---

## 🔧 What Was Fixed

### Bug Fixes Applied:

1. **✅ Ticket ID Not Showing**
   - **Issue**: SQL query used `ticket_id` instead of `id`
   - **Fix**: Changed `SELECT ticket_id, ...` to `SELECT id, ...`
   - **File**: `app.py` line 118

2. **✅ Category Column Missing**
   - **Issue**: Table header didn't include Category column
   - **Fix**: Added `<th>Category</th>` to table header
   - **File**: `templates/index.html`

3. **✅ Filters Not Working**
   - **Issue**: Backend query filters work, but frontend wasn't getting full data
   - **Fix**: Added `description` to SELECT query for search functionality
   - **File**: `app.py` line 118

4. **✅ Message Viewing & Adding**
   - **Issue**: RealDictRow objects not properly serialized to JSON
   - **Fix**: Added `dict()` conversions and `CustomJSONProvider`
   - **Files**: `app.py` multiple endpoints

---

## 🎯 Features

Once deployed, your Ticket Management System will have:

- ✅ **Create/Read/Update/Delete Tickets**
- ✅ **Filter by Status** (open, in-progress, resolved, closed)
- ✅ **Filter by Priority** (low, medium, high, critical)
- ✅ **Filter by Category** (custom categories)
- ✅ **Search** tickets by title or description
- ✅ **Message Threads** - Add messages/comments to tickets
- ✅ **Statistics Dashboard** - View ticket counts by status and priority
- ✅ **User Authentication** - Automatic user email detection

---

## 🐛 Troubleshooting

### Error: "ModuleNotFoundError: No module named 'flask'"
**Solution**: Don't run `app.py` in a notebook! Deploy it as a Databricks App (see above).

### Error: 'column "id" does not exist' or 'column "description" does not exist'
**Solution**: You forgot to run the schema migration! Run `python run_migration.py` first (see Prerequisites section).

### Error: "Failed to connect to database"
**Solution**: Check that your Lakebase URL is correctly configured in Databricks Secrets:
```bash
databricks secrets get database lakebase-url
```

### Error: App won't start
**Solution**: Check app logs in the Databricks Apps UI:
1. Go to Apps
2. Click your app
3. Click "Logs" tab

### Filters/Messages Not Working
**Solution**: Clear your browser cache and refresh the page. The JavaScript functions should work after the bug fixes.

---

## 📱 Accessing Your App

After deployment:

1. **Get App URL**:
   - UI: Apps → Your App → Click "Open"
   - CLI: `databricks apps get ticket-management-system`

2. **Share with Team**:
   - The URL is shareable with anyone in your Databricks workspace
   - Users will be automatically authenticated via Databricks

3. **Stop/Restart**:
   - UI: Apps → Your App → "Stop" / "Start"
   - CLI: `databricks apps stop ticket-management-system`

---

## 🔄 Making Updates

After modifying code:

```bash
# Redeploy the app
databricks apps deploy ticket-management-system \
  --source-code-path /Users/san.datae1@gmail.com/databricks-lakebase-app-day-1_sk

# Or use the UI: Apps → Your App → "Redeploy"
```

---

## ✅ All Fixed Issues

1. ✅ **Priority & Status filters** - Now working with proper backend query
2. ✅ **Ticket ID display** - Fixed column name mismatch
3. ✅ **Message viewing** - Proper JSON serialization
4. ✅ **Adding messages** - Full CRUD functionality for messages
5. ✅ **Category column** - Added to table header

**Ready to deploy!** 🚀