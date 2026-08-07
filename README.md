# Databricks Support Ticket Management App

A Flask + Lakebase/PostgreSQL Databricks App for managing support tickets and their messages.

## Features

- Ticket list view
- Ticket details with full message history
- Create ticket
- Add message to ticket
- Update ticket status (`Open`, `In Progress`, `Resolved`)
- Delete ticket with cascading message delete
- Automatic table creation for `tickets` and `ticket_messages`
- Automatic seed data (3 tickets, 2 messages each) when database is empty

## Files

- `app.py` - Flask routes + SQLAlchemy models (`tickets`, `ticket_messages`)
- `lakebase.py` - Lakebase connection helper (`LAKEBASE_URL` env first, secret fallback)
- `templates/index.html` - Support ticket management UI
- `setup_secrets.py` - Optional script to store Lakebase URL in Databricks secrets
- `app.yaml` - Databricks App command and env configuration

## Environment variables

- `LAKEBASE_URL` (recommended for local development)
- `LAKEBASE_SECRET_SCOPE` (default: `database`)
- `LAKEBASE_SECRET_KEY` (default: `lakebase-url`)

## Run locally

```bash
pip install -r requirements.txt
python app.py
```

## API endpoints

- `GET /healthz`
- `GET /tickets`
- `POST /tickets`
- `GET /tickets/<ticket_id>`
- `POST /tickets/<ticket_id>/messages`
- `PATCH /tickets/<ticket_id>/status`
- `DELETE /tickets/<ticket_id>`
