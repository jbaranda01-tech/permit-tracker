# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PermitTracker is a permit/certification tracking system for LB Caribe Services and Professional Logistics (PLI). It tracks employee permits (NTSP, TWIC, medical certs, etc.) and equipment permits (marbete, seguro, inspección) with expiration alerts. The UI is in Spanish.

## Commands

```bash
# Local development
python app.py                    # Runs on http://localhost:5000

# Environment variables needed
export SECRET_KEY=dev-secret
export DATABASE_URL=sqlite:///permits.db   # SQLite for local dev; production uses DATABASE_PRIVATE_URL (Railway internal network)

# Database
flask init-db                    # Creates tables + default admin user (admin/admin123)
flask db migrate -m "message"    # Generate migration (Flask-Migrate, but no migrations dir yet)
flask db upgrade                 # Apply migrations
flask dedup --dry-run            # Preview duplicate cleanup
flask dedup                      # Remove duplicate employees/permits
flask send-notifications --dry-run          # Preview without sending emails
flask send-notifications                    # Send all pending notifications
flask send-notifications --employee-id 42   # Single employee (testing)

# Production (Railway)
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120 --preload
```

## Architecture

**Monolith Flask app** — all routes, models, and config in three files:

- `app.py` (~2000 lines): All routes, CLI commands, Excel import logic, PDF generation, file upload handling, email notification helpers. Contains role decorators (`@admin_required`, `@manager_required`) and a context processor that injects global alert counts into every template.
- `models.py`: SQLAlchemy models — `User`, `Employee`, `EmployeePermit`, `Equipment`, `EquipmentPermit`, `NotificationLog`. Permit types are defined as module-level lists (`EMPLOYEE_PERMIT_TYPES`, `EQUIPMENT_PERMIT_TYPES`). Status logic (expired/expiring_soon/valid/missing/na) lives in model `@property` methods. `NotificationLog` tracks sent email notifications with 7-day dedup.
- `config.py`: Single `Config` class. Prefers `DATABASE_PRIVATE_URL` (Railway internal network) over `DATABASE_URL`, falling back to SQLite for local dev. Auto-converts Railway's `postgres://` to `postgresql://`. Sets PostgreSQL connection timeout only when using PostgreSQL. Email notification config: `SENDGRID_API_KEY`, `SENDGRID_FROM_EMAIL`, `ENABLE_SCHEDULER`, `NOTIFICATION_DAY`, `NOTIFICATION_HOUR`.

**Two-company model**: Every employee and equipment record has a `company` field (`LB` or `PLI`). The dashboard shows both side by side.

**Permit status system**: 30-day window for "expiring soon" alerts (configured via `Config.ALERT_DAYS_BEFORE`). Status is computed at query time in model properties, not stored.

**No test suite currently exists.**

## Key Patterns

- Templates use Jinja2 with a shared `base.html` layout (sidebar + navbar)
- Dark/light theme uses `data-theme` attribute on `<html>`, persisted in `localStorage`. An inline `<script>` in `<head>` sets the theme before first paint to prevent FOUC; `app.js` handles toggle buttons and syncing.
- File uploads go to `uploads/` directory with UUID-prefixed filenames
- Excel import (openpyxl) includes a 2-digit year fix (1930→2030) for date handling. Both employee and equipment imports recognize N/A-like cell values ("N/A", "NA", "NO", empty string) and set `applicability='N/A'` on the corresponding permit. Equipment import also hardcodes VOUCHER as N/A for Personal company equipment.
- PDF reports generated with WeasyPrint via `report_pdf.html` template
- Auth uses Flask-Login with three roles: admin, manager, viewer
- Unique constraints enforce one permit per type per employee/equipment
- `dedup` CLI command uses advisory locking and idempotency tokens for safe bulk operations

## Email Notifications

Weekly automated emails to employees with expiring/expired permits via SendGrid. Runs every Monday at 8 AM AST (12:00 UTC) using APScheduler as an in-process background scheduler.

- `NotificationLog` model provides 7-day dedup — same employee+permit won't be re-notified within a week
- `_gather_expiring_items()` collects expired and expiring-soon permits for active employees with email addresses
- `send_notification_email()` lazy-imports sendgrid so local dev works without the package
- `run_notification_cycle()` orchestrates gather → filter → send → log
- Gunicorn `--preload` ensures the scheduler starts once in the master process, not per-worker
- Email template: `templates/email/permit_expiry_notice.html` (standalone HTML, does not extend `base.html`)

**Production env vars (Railway):**
- `SENDGRID_API_KEY` — API key with "Mail Send" permissions
- `SENDGRID_FROM_EMAIL` — `notificaciones@lb-pli-permits.com`
- `ENABLE_SCHEDULER` — `true` to start APScheduler on boot
- `NOTIFICATION_DAY` — day of week (default: `monday`)
- `NOTIFICATION_HOUR` — hour in UTC (default: `12`, which is 8 AM Puerto Rico)

## Deployment

Deployed on Railway using Dockerfile. WeasyPrint requires system-level libraries (pango, cairo, gdk-pixbuf) installed in the Docker image via `apt-get`. Gunicorn runs with `--preload` for single-instance APScheduler.
