# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PermitTracker is a permit/certification tracking system for LB Caribe Services and Professional Logistics (PLI). It tracks employee permits (NTSP, TWIC, medical certs, etc.) and equipment permits (marbete, seguro, inspección) with expiration alerts. It also includes a truck issue reporting module where drivers report problems via link-based access and shop staff triage/resolve them. The UI is in Spanish.

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
flask generate-token <employee_id>          # Generate issue-reporting access link for a driver
flask assign-issue-role <username> shop     # Assign shop role for issue module

# Production (Railway)
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120 --preload
```

## Architecture

**Flask app with one Blueprint** — permit routes in `app.py`, issue reporting in the `issues/` Blueprint:

- `app.py` (~2160 lines): All permit routes, CLI commands, Excel import logic, PDF generation, file upload handling, email notification helpers. Contains role decorators (`@admin_required`, `@manager_required`) and context processors: one injects global alert counts into every template, another provides `versioned_static()` for cache-busted asset URLs. Registers the `issues` Blueprint. Uses `flask-compress` for gzip/brotli compression. Serves `/manifest.json` for PWA support. Static assets get `Cache-Control: public, max-age=604800` headers.
- `issues/` (Blueprint): Truck issue reporting module. `__init__.py` defines `issues_bp` with `/issues` prefix. `routes.py` has driver report flow, truck-profile shop queue, issue detail, and status update routes. `SEVERITY_RANK` dict and `build_truck_profiles()` helper power the queue's truck-centric grouping. `decorators.py` has `@shop_required` (admin auto-grants access).
- `models.py`: SQLAlchemy models — `User`, `Employee`, `EmployeePermit`, `Equipment`, `EquipmentPermit`, `CompanyPermit`, `FileStorage`, `NotificationLog`, `Issue`, `IssueStatusHistory`, `IssuePhoto`, `UserIssueRole`. Permit types and issue constants (categories, severities, statuses) are defined as module-level lists. Status logic (expired/expiring_soon/valid/missing/na) lives in model `@property` methods. `NotificationLog` tracks sent email notifications with 7-day dedup. `FileStorage` stores uploaded files as binary blobs in the DB (used alongside the filesystem `uploads/` directory).
- `config.py`: Single `Config` class. Prefers `DATABASE_PRIVATE_URL` (Railway internal network) over `DATABASE_URL`, falling back to SQLite for local dev. Auto-converts Railway's `postgres://` to `postgresql://`. Sets PostgreSQL connection timeout only when using PostgreSQL. Email notification config: `SENDGRID_API_KEY`, `SENDGRID_FROM_EMAIL`, `ENABLE_SCHEDULER`, `NOTIFICATION_DAY`, `NOTIFICATION_HOUR`.

**Two-company model**: Every employee and equipment record has a `company` field (`LB` or `PLI`). The dashboard shows both side by side.

**Permit status system**: 30-day window for "expiring soon" alerts (configured via `Config.ALERT_DAYS_BEFORE`). Status is computed at query time in model properties, not stored. N/A permits are filtered out of profile views — detail routes split permits into `active_permits` and `hidden_permits`. Managers can toggle applicability via dedicated `/toggle` endpoints. Toggling to N/A clears the expiration date.

**Company-level permits**: `CompanyPermit` model tracks permits scoped to companies (LB/PLI) rather than individual employees or equipment. `COMPANY_PERMIT_TYPES` defines four types: Certificado HazMat, Certificado NMFTA, USDOT Biennial Report, Franquicia NTSP — each with a list of applicable companies. Same status property pattern (expired/expiring_soon/valid/missing/na) as employee and equipment permits. Unique constraint enforces one permit per type per company.

**Mobile optimization**: CSS has three responsive breakpoints (1024px, 768px, 480px). At 768px: sidebar becomes off-canvas drawer, main content gets full viewport width, grids collapse to 1 column, touch targets enforce 44px minimum, form inputs are 16px+ font-size to prevent iOS auto-zoom. At 480px: modals go full-screen, report form submit button is sticky, tighter spacing. Touch feedback via `@media (hover: none) and (pointer: coarse)` targets touch devices with `:active` states without affecting desktop hover.

**No test suite currently exists.**

## Key Patterns

- Templates use Jinja2 with a shared `base.html` layout (sidebar + navbar). Use `versioned_static('path')` instead of `url_for('static', filename='path')` for CSS/JS to enable cache busting.
- Dark/light theme uses `data-theme` attribute on `<html>`, persisted in `localStorage`. An inline `<script>` in `<head>` sets the theme before first paint to prevent FOUC; `app.js` handles toggle buttons, syncing, and updating `<meta name="theme-color">` dynamically.
- Mobile navigation: On screens <= 768px, the sidebar becomes a hidden off-canvas drawer (280px, slides from left). A fixed 56px mobile top bar with hamburger menu, app title, and theme toggle replaces the desktop sidebar. `openDrawer()`/`closeDrawer()` in `app.js` manage state. Swipe gestures (edge-swipe to open, swipe-left to close) are supported via passive touch listeners. `toggleSidebar()` delegates to drawer functions on mobile.
- PWA: `static/manifest.json` (served also at `/manifest.json`, `scope: "/"`), `static/sw.js` (service worker with cache-first for static assets, network-first for HTML), and app icons in `static/img/icon-*.png`. PWA meta tags in `base.html` are wrapped in `{% block pwa_meta %}` and service worker registration in `{% block service_worker %}` so child templates can opt out. Driver report pages (`report.html`, `report_success.html`) override both blocks to strip PWA signals — "Add to Home Screen" creates a plain browser bookmark, not a standalone app install. Report pages also unregister any previously-cached service worker on the driver's device.
- File uploads go to `uploads/` directory with UUID-prefixed filenames
- Excel import (openpyxl) includes a 2-digit year fix (1930→2030) for date handling. Both employee and equipment imports recognize N/A-like cell values ("N/A", "NA", "NO", empty string) and set `applicability='N/A'` on the corresponding permit. Equipment import also hardcodes VOUCHER as N/A for Personal company equipment.
- PDF reports generated with WeasyPrint via `report_pdf.html` template
- Auth uses Flask-Login with strict module separation enforced by a `before_request` hook. Base roles: admin, manager, viewer. Issue module uses `user_issue_roles` table (shop role only; office role is deprecated). Any non-admin user with a shop issue role (viewer + shop OR manager + shop) can ONLY access the issue module — they are redirected away from all permit routes. Permit users (viewer/manager without shop role) can ONLY access the permit module. Admin has full access to both modules. `User.is_shop_only` and `User.has_shop_role` model properties centralize this logic.
- Drivers access issue reporting via link-based auth: each employee has an optional `access_token` (UUID) column; URL `/issues/report/<token>` identifies the driver without login. Report pages extend `base.html` but override `{% block pwa_meta %}` and `{% block service_worker %}` to disable PWA installation — drivers who save the link to their homescreen get a browser bookmark, not the full app. The report form collects truck, category, severity, and description only (odometer/location columns exist in the DB but are not exposed in the UI). Truck dropdown is scoped to vehicles from the driver's company, excluding Personal company and specific equipment models defined in `EXCLUDED_EQUIPMENT_MODELS` (carreton, chasis, generador alquiler, generador contenedor, tanque combustible, tanque harina). The `reportable_equipment_query()` helper in `issues/routes.py` centralizes this filtering.
- Issue status updates are transactional: `Issue.current_status` (denormalized) and `IssueStatusHistory` are always updated in the same commit. `resolved_at` auto-sets on transition to resuelto/cerrado.
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

## Issue Reporting Module

Truck issue reporting for drivers (report) and shop staff (triage/resolve).

**Models:** `Issue` (linked to equipment + reporting employee), `IssueStatusHistory` (audit log of status transitions), `IssuePhoto` (schema defined, upload routes pending), `UserIssueRole` (junction table for shop/office roles).

**Issue statuses:** reportado → en_revision → en_reparacion → resuelto → cerrado

**Issue categories:** motor, frenos, eléctrico, llantas, transmisión, carrocería, aire acondicionado, luces, fluidos, otro

**Issue severities:** baja, media, alta, crítica

**Role access (strict module separation):**
- Driver: link-based access via `Employee.access_token` — no login, reports issues only
- Shop: `user_issue_roles` row with `role='shop'` — triage, change status, view queue. Any non-admin user with shop role (viewer + shop OR manager + shop) is blocked from all permit routes via `before_request` hook
- Admin: existing `User.role == 'admin'` — automatic full access to both modules, no `user_issue_roles` entry needed
- Office role: deprecated, no longer assigned via UI or CLI. Existing DB rows are inert

**Equipment filtering:** Only LB and PLI vehicles are reportable. Personal company equipment is excluded. Specific equipment models are excluded via `EXCLUDED_EQUIPMENT_MODELS` in `models.py` (matched case-insensitively against `Equipment.model` column). The `reportable_equipment_query()` helper in `issues/routes.py` centralizes this logic for both the driver form and the queue filter dropdown.

**Queue layout:** The shop queue (`/issues/`) uses a truck-profile-centric layout. Company panels (LB/PLI) contain expandable truck profile cards — each card shows the truck's display name, make/model/year, plate number, issue count badge, and worst severity indicator. Click a truck header to expand/collapse its inline issue list. Trucks are sorted by worst severity (critical first), then issue count, then alphabetically. All reportable trucks appear (including those with no matching issues). A "Reportes Recientes" section below the panels shows the latest 20 issues across all trucks. Status tabs and severity/truck filters sit above the panels and apply to both sections. The `SEVERITY_RANK` dict in `issues/routes.py` controls sort order. The `build_truck_profiles()` helper groups filtered issues by equipment and attaches them to truck profile dicts passed to the template.

**Routes (all under `/issues` Blueprint):**
- `/issues/report/<token>` — GET/POST driver report form (public, mobile-first)
- `/issues/report/<token>/success` — confirmation page
- `/issues/` — shop/office queue with company panels, status tabs, severity/truck filters
- `/issues/<id>` — issue detail with status history timeline
- `/issues/<id>/status` — POST status change (shop/admin only)
- `/issues/generate-link/<employee_id>` — POST generate access token for driver (admin only)

**Templates:** `templates/issues/` — `report.html`, `report_success.html`, `queue.html`, `detail.html`

**Admin UI integration:**
- Admin user management page (`admin_users.html`) has "Roles Taller" checkboxes (Taller/Oficina) in both create and edit forms. Issue roles are synced via `UserIssueRole` on save.
- Employee detail page has a "Link de Reporte" button (admin-only) that expands to show the driver's reporting URL (copyable) or a button to generate one.

## Static Assets

- `static/css/style.css` — All CSS in one file (~1760 lines). Custom CSS with CSS variables, no framework. Three responsive breakpoints (1024px, 768px, 480px). Dual theme (light/dark) via `data-theme` attribute.
- `static/js/app.js` — All JS in one file (~200 lines). Vanilla JS, no framework. Theme toggle, sidebar/drawer, alerts modal, flash messages, swipe gestures.
- `static/manifest.json` — PWA manifest with explicit `scope: "/"`. Also served at `/manifest.json` via Flask route.
- `static/sw.js` — Service worker. Caches CSS, JS, and Font Awesome on install. Cache-first for `/static/` and CDN assets, network-first with cache fallback for HTML.
- `static/img/` — Company logos (`logo-lb.png`, `logo-pli.png`) and PWA icons (`icon-152.png`, `icon-180.png`, `icon-192.png`, `icon-512.png`, `icon-maskable-512.png`).
- No build pipeline — assets served directly by Flask. `flask-compress` handles gzip/brotli. `versioned_static()` appends `?v=<hash>` for cache busting.

## Deployment

Deployed on Railway using Dockerfile. WeasyPrint requires system-level libraries (pango, cairo, gdk-pixbuf) installed in the Docker image via `apt-get`. Gunicorn runs with `--preload` for single-instance APScheduler.
