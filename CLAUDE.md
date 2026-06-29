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
flask dedup                      # Remove duplicate employees/permits/equipment (vehicles)/issue reports
flask migrate-shared-insurance --dry-run    # Preview moving per-vehicle SEGURO into shared CompanyPermit
flask migrate-shared-insurance              # Pre-fill LB/PLI shared insurance, hide per-vehicle copies
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

- `app.py` (~2230 lines): All permit routes, CLI commands, Excel import logic, PDF generation, file upload handling, email notification helpers. Contains role decorators (`@admin_required`, `@manager_required`) and context processors: one injects global alert counts into every template, another provides `versioned_static()` for cache-busted asset URLs. Jinja filters: `format_phone`, and `time_ago` (Spanish relative time — "hace 5m / hace 3h / ayer / hace 3d", falling back to `dd/mm/yyyy` beyond ~7 days; computed against `datetime.utcnow()` since issue timestamps are naive UTC; used in the issue queue rows). Registers the `issues` Blueprint. Uses `flask-compress` for gzip/brotli compression. Serves `/manifest.json` for PWA support. Static assets get `Cache-Control: public, max-age=604800` headers.
- `issues/` (Blueprint): Truck issue reporting module. `__init__.py` defines `issues_bp` with `/issues` prefix. `routes.py` has driver report flow, truck-profile shop queue, issue detail, and status update routes. `SEVERITY_RANK` dict and `build_truck_profiles()` helper power the queue's truck-centric grouping. `decorators.py` has `@shop_required` (admin auto-grants access) and `@shop_manager_required` (admin OR base-role `manager` + shop role — gates manual report creation).
- `models.py`: SQLAlchemy models — `User`, `Employee`, `EmployeePermit`, `Equipment`, `EquipmentPermit`, `CompanyPermit`, `FileStorage`, `NotificationLog`, `Issue`, `IssueStatusHistory`, `IssuePhoto`, `UserIssueRole`. Permit types and issue constants (categories, severities, statuses) are defined as module-level lists. Status logic (expired/expiring_soon/valid/missing/na) lives in model `@property` methods. `NotificationLog` tracks sent email notifications with 7-day dedup. `FileStorage` stores uploaded files as binary blobs in the DB (used alongside the filesystem `uploads/` directory).
- `config.py`: Single `Config` class. Prefers `DATABASE_PRIVATE_URL` (Railway internal network) over `DATABASE_URL`, falling back to SQLite for local dev. Auto-converts Railway's `postgres://` to `postgresql://`. Sets PostgreSQL connection timeout only when using PostgreSQL. Email notification config: `SENDGRID_API_KEY`, `SENDGRID_FROM_EMAIL`, `ENABLE_SCHEDULER`, `NOTIFICATION_DAY`, `NOTIFICATION_HOUR`.

**Two-company model**: Every employee and equipment record has a `company` field (`LB` or `PLI`). The dashboard shows both side by side.

**Permit status system**: 30-day window for "expiring soon" alerts (configured via `Config.ALERT_DAYS_BEFORE`). Status is computed at query time in model properties, not stored. N/A permits are filtered out of profile views — detail routes split permits into `active_permits` and `hidden_permits`. Managers can toggle applicability via dedicated `/toggle` endpoints. Toggling to N/A clears the expiration date.

**Company-level permits**: `CompanyPermit` model tracks permits scoped to companies (LB/PLI) rather than individual employees or equipment. `COMPANY_PERMIT_TYPES` defines five types: Seguro / Insurance, Certificado HazMat, Certificado NMFTA, USDOT Biennial Report, Franquicia NTSP — each with a list of applicable companies. Same status property pattern (expired/expiring_soon/valid/missing/na) as employee and equipment permits. Unique constraint enforces one permit per type per company. Edited via the "Empresa" dashboard view (`company_permit_edit` / `company_permit_upload_form` routes).

**Shared company insurance (SEGURO)**: Every LB/PLI vehicle shares one insurance policy, so insurance is a company-level permit, not per-vehicle. On LB/PLI vehicle profiles the SEGURO `EquipmentPermit` is set to N/A and a read-only "Seguro (compartido)" card is shown instead (pulled from the company `CompanyPermit`, with a link to edit it on the Empresa page) — see `shared_insurance` in `equipment_detail` and the card in `equipment.html`. `equipment_new` creates SEGURO as N/A for LB/PLI. **Personal** vehicles keep their own editable per-vehicle SEGURO. The one-time `flask migrate-shared-insurance` command pre-fills each company's shared record from the best existing per-vehicle insurance (prefers one with an attached PDF, then latest expiration) and sets the per-vehicle copies to N/A. Alert counts in `inject_globals` are not double-counted: LB/PLI per-vehicle SEGURO is N/A (excluded) and the shared policy is counted once as a `CompanyPermit`.

**Mobile optimization**: CSS has three responsive breakpoints (1024px, 768px, 480px). At 768px: sidebar becomes off-canvas drawer, main content gets full viewport width, grids collapse to 1 column, touch targets enforce 44px minimum, form inputs are 16px+ font-size to prevent iOS auto-zoom. At 480px: modals go full-screen, report form submit button is sticky, tighter spacing. Touch feedback via `@media (hover: none) and (pointer: coarse)` targets touch devices with `:active` states without affecting desktop hover.

**No test suite currently exists.**

## Key Patterns

- Templates use Jinja2 with a shared `base.html` layout (sidebar + navbar). Use `versioned_static('path')` instead of `url_for('static', filename='path')` for CSS/JS to enable cache busting.
- Dark/light theme uses `data-theme` attribute on `<html>`, persisted in `localStorage`. An inline `<script>` in `<head>` sets the theme before first paint to prevent FOUC; `app.js` handles toggle buttons, syncing, and updating `<meta name="theme-color">` dynamically. The sun/moon toggle (`.theme-toggle` + `.theme-btn`) lives in **two** places: the desktop **sidebar footer** (`.theme-toggle-sidebar`, above the user-info/logout) and the **mobile top bar** (`.theme-toggle-mobile`, hidden on desktop). `syncThemeButtons()` queries *all* `.theme-btn` nodes, so both groups stay in sync automatically — adding a third group anywhere Just Works. (There is no floating/fixed toggle; an earlier `.theme-toggle-floating` was removed because its `position:fixed; top/right; z-index:1000` overlapped each page's top-right action buttons.)
- Mobile navigation: On screens <= 768px, the sidebar becomes a hidden off-canvas drawer (280px, slides from left). A fixed 56px mobile top bar with hamburger menu, app title, and theme toggle replaces the desktop sidebar. `openDrawer()`/`closeDrawer()` in `app.js` manage state. Swipe gestures (edge-swipe to open, swipe-left to close) are supported via passive touch listeners. `toggleSidebar()` delegates to drawer functions on mobile.
- PWA: `static/manifest.json` (served also at `/manifest.json`, `scope: "/"`), `static/sw.js` (service worker with cache-first for static assets, network-first for HTML), and app icons in `static/img/icon-*.png`. PWA meta tags in `base.html` are wrapped in `{% block pwa_meta %}` and service worker registration in `{% block service_worker %}` so child templates can opt out. Driver report pages (`report.html`, `report_success.html`) override both blocks to strip PWA signals — "Add to Home Screen" creates a plain browser bookmark, not a standalone app install. Report pages also unregister any previously-cached service worker on the driver's device.
- File uploads go to `uploads/` directory with UUID-prefixed filenames
- Excel import (openpyxl) includes a 2-digit year fix (1930→2030) for date handling. Both employee and equipment imports recognize N/A-like cell values ("N/A", "NA", "NO", empty string) and set `applicability='N/A'` on the corresponding permit. Equipment import also hardcodes VOUCHER as N/A for Personal company equipment.
- PDF reports generated with WeasyPrint via `report_pdf.html` template
- Auth uses Flask-Login with strict module separation enforced by a `before_request` hook. Base roles: admin, manager, viewer. Issue module uses `user_issue_roles` table (shop role only; office role is deprecated). Any non-admin user with a shop issue role (viewer + shop OR manager + shop) can ONLY access the issue module — they are redirected away from all permit routes. Permit users (viewer/manager without shop role) can ONLY access the permit module. Admin has full access to both modules. `User.is_shop_only` and `User.has_shop_role` model properties centralize this logic. `User.is_shop_manager` (`is_manager and has_shop_role` — i.e. admin, or base-role `manager` + shop role) gates the extra ability to manually create reports, distinguishing a shop manager from a shop viewer (the only place base role + shop role are combined).
- Drivers access issue reporting via link-based auth: each employee has an optional `access_token` (UUID) column; URL `/issues/report/<token>` identifies the driver without login. Report pages extend `base.html` but override `{% block pwa_meta %}` and `{% block service_worker %}` to disable PWA installation — drivers who save the link to their homescreen get a browser bookmark, not the full app. The report form collects truck, category, severity, and description only (odometer/location columns exist in the DB but are not exposed in the UI). Truck dropdown is scoped to vehicles from the driver's company, excluding Personal company and specific equipment models defined in `EXCLUDED_EQUIPMENT_MODELS` (carreton, chasis, generador alquiler, generador contenedor, tanque combustible, tanque harina). The `reportable_equipment_query()` helper in `issues/routes.py` centralizes this filtering.
- Issue status updates are transactional: `Issue.current_status` (denormalized) and `IssueStatusHistory` are always updated in the same commit. `resolved_at` auto-sets on transition to resuelto/cerrado.
- Unique constraints enforce one permit per type per employee/equipment
- `dedup` CLI command uses advisory locking and idempotency tokens for safe bulk operations. It merges duplicate employees (by name+company), employee permits, equipment permits, and **duplicate equipment/vehicle rows**. Equipment vehicles are deduped by **union-find over shared signatures** — two rows are the same vehicle if they share ANY non-blank key: `vin_serial` (global), `plate_number` (global), or `unit_number`+`company` (per-company), all normalized lowercase/trimmed. (Union-find, not a single cascade key, so a VIN-only row and a plate-only row for the same truck still merge.) Keyless rows (no VIN/plate/unit — e.g. carretones, generadores, tanques) fall back to a `(company, titular, model, year)` signature so exact-duplicate copies still merge; a keyless row with titular AND model both blank is never auto-merged. It keeps the lowest-id row, fills its empty fields from dups, merges/re-points child `EquipmentPermit` rows, and reassigns linked `Issue` reports to the keeper before deleting (the `Issue.equipment_id` FK is `SET NULL`, so issues would otherwise be orphaned). It also removes **duplicate issue reports** left by repeated bulk imports, grouping by `(equipment_id, category, normalized description, canonical report day)` (keeps lowest id; re-points any `parent_issue_id` to the keeper; child `IssueStatusHistory` rows cascade-delete). The "canonical report day" comes from `_canonical_report_day()` in `issues/routes.py` (reused here via a local import so the cleanup and importer can never drift): it returns `reported_at.date()` ONLY when the timestamp is exactly midnight — real spreadsheet dates are stored at midnight via `_as_datetime`, whereas a missing date fell back to `datetime.utcnow()` (a non-midnight wall-clock time), so dateless rows resolve to `None` and collapse together regardless of which day each import stamped them. This is what lets `flask dedup` finally merge the dateless duplicates that the old `reported_at.date()` key kept splitting across different days.
- **Duplicate-vehicle defenses (3 layers, all reuse `find_duplicate_equipment(vin, plate, unit, company, titular=, model=, year=)` in `app.py` — a case-insensitive VIN→plate→unit#+company cascade lookup, with a keyless fallback to `(company, titular, model, year)` when VIN/plate/unit are all blank so keyless vehicles aren't duplicated on re-import):** (1) `equipment_new` and `vehicle_create` (`issues/routes.py`) block the save on a match — Spanish warning, re-render with submitted values, HTTP 422; `equipment_form.html` repopulates via `form_data`. (2) The Excel equipment import (`/import/equipment`, app.py) uses the **same helper** for its upsert lookup, so re-importing the master spreadsheet updates vehicles in place (matching by unit# even when VIN/plate are blank) instead of creating duplicates — this was the recurring duplicate source. (3) `dedup` applies **partial unique indexes** (Postgres) as a permanent backstop: `uq_equipment_vin` on `lower(vin_serial)`, `uq_equipment_plate` on `lower(plate_number)`, `uq_equipment_company_unit` on `(company, lower(unit_number))`, each `WHERE <col> IS NOT NULL AND <col> <> ''` so blank rows coexist. A would-be duplicate insert now fails loudly with `IntegrityError`. **Keyless vehicles (blank VIN/plate/unit) are NOT covered by these indexes** (their key columns are excluded by the `<> ''` predicate), so keyless duplicate prevention relies solely on the app-level `(company, titular, model, year)` fallback in `find_duplicate_equipment` and the matching keyless signature in `dedup`.

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

**Issue categories:** motor, frenos, eléctrico, llantas, transmisión, carrocería, aire acondicionado, luces, fluidos, suspensión, otro

**Issue severities:** baja, media, alta, crítica

**Role access (strict module separation):**
- Driver: link-based access via `Employee.access_token` — no login, reports issues only
- Shop: `user_issue_roles` row with `role='shop'` — triage, change status, view queue. Any non-admin user with shop role (viewer + shop OR manager + shop) is blocked from all permit routes via `before_request` hook
- Shop manager (`User.is_shop_manager`): a shop user whose **base** role is `manager` (not `viewer`) — gets everything a shop user does PLUS the ability to manually create reports (`/issues/new`, gated by `@shop_manager_required`). Created via the admin Users page (base role `manager` + Taller checkbox) or `flask assign-issue-role <user> shop` on a manager. No new issue-role value — `manager` stays a base role only
- Admin: existing `User.role == 'admin'` — automatic full access to both modules, no `user_issue_roles` entry needed
- Office role: deprecated, no longer assigned via UI or CLI. Existing DB rows are inert

**Equipment filtering:** Only LB and PLI vehicles are reportable. Personal company equipment is excluded. Specific equipment models are excluded via `EXCLUDED_EQUIPMENT_MODELS` in `models.py` (matched case-insensitively against `Equipment.model` column). The `reportable_equipment_query()` helper in `issues/routes.py` centralizes this logic for both the driver form and the queue filter dropdown.

**Queue layout:** The shop queue (`/issues/`) uses a truck-profile-centric layout. Company panels (LB/PLI) contain expandable truck profile cards — each card shows the truck's display name, make/model/year, plate number, issue count badge, and worst severity indicator. Click a truck header to expand/collapse its inline issue list. Each card also shows an admin-only "Ver ficha / editar camión" link to the permits-side `equipment_detail` page. Trucks are sorted by worst severity (critical first), then issue count, then alphabetically. All reportable trucks appear (including those with no matching issues). A "Reportes Recientes" section below the panels shows the latest 20 issues across all trucks. Status tabs and severity/truck filters sit above the panels and apply to both sections. The `SEVERITY_RANK` dict in `issues/routes.py` controls sort order. The `build_truck_profiles()` helper groups filtered issues by equipment and attaches them to truck profile dicts passed to the template.

**Issue-first row layout:** Inside the truck cards (and in the "Reportes Recientes" / "Resueltos" sections) each issue renders via the shared `issue_row(issue, show_truck=false)` Jinja `{% macro %}` defined at the top of `queue.html` — a single source for all four row sites so they can't drift. Each row is a card with a **severity-colored left border** (`.issue-row--{baja,media,alta,critica}`), a top line (severity badge + category + `#id`), a 2-line clamped description, and a meta line (status badge · reporter name · **relative time** via the `time_ago` filter). `show_truck=true` adds the truck tag (used in the cross-truck Recientes/Resueltos lists where the truck isn't implied by a parent card). CSS lives in the `.issue-row*` block in `style.css`. The older `.issue-card-compact` row styles are now only used by the equipment detail page's "Reportes / Averías" list.

**Routes (all under `/issues` Blueprint):**
- `/issues/report/<token>` — GET/POST driver report form (public, mobile-first)
- `/issues/report/<token>/success` — confirmation page
- `/issues/` — shop/office queue with company panels, status tabs, severity/truck filters
- `/issues/<id>` — issue detail with status history timeline
- `/issues/<id>/status` — POST status change (shop/admin only)
- `/issues/new` — GET/POST manual report creation (`@shop_manager_required`: admin or manager + shop). Shared `manual_new.html` form serves both, but for non-admins the "Estado e Historial" section (status/backdate/notes) is hidden — they always create a fresh `reportado` issue dated now, recorded via `update_issue_status(..., changed_by_user_id=current_user.id, notes='Reporte agregado manualmente por taller')`. Admins keep the full backlog form: status + backdated dates + note via `synthesize_status_history` tagged `BACKLOG_NOTE`. The queue "Agregar Manualmente" button shows for `is_shop_manager`; "Agregar Camión"/"Importar Lote" stay admin-only
- `/issues/generate-link/<employee_id>` — POST generate access token for driver (admin only)
- `/issues/import` — GET/POST admin bulk Excel import of backlog issue reports (Unidad, Categoría, Severidad, Descripción, Estado, Fecha Reporte, Fecha Resuelto)

**Idempotent bulk import (no duplicates):** `import_issues()` (`issues/routes.py`) is safe to re-run on the same file. Each row's identity is `(equipment_id, category, normalized description, canonical report day)` — severity/status are excluded so corrections re-apply instead of duplicating. The day component is the row's **parsed** date (`_coerce_date(reported_raw)`, a `date` or `None`) — NOT the stored `reported_at`, which falls back to `datetime.utcnow()` for display when the date is blank. Keying on the parsed date is critical: rows with a blank/unparseable "Fecha Reporte" key on `None` and collapse on re-import, instead of minting a fresh duplicate each run (the previous bug — the `utcnow()` fallback gave the same dateless row a different date every import, defeating dedup at both the import and `flask dedup` layers). `find_duplicate_issue(equipment_id, category, description, report_day)` derives the existing issue's day via `_canonical_report_day()` (midnight ⇒ real date; non-midnight ⇒ `None`), so a dateless new row matches a prior dateless issue regardless of its stored timestamp. Before inserting, it checks both an in-batch `seen` map (duplicate rows in the same file) and the DB via `find_duplicate_issue()` (rows from a prior import). On a match it does NOT create a new `Issue`; it updates the existing one's severity and (only if the status changed) re-runs `synthesize_status_history`. The results page reports imported / duplicates-skipped / errored counts. Driver reports are never a duplicate source (one at a time, live timestamps). `flask dedup` cleans up pre-existing import duplicates by the same key (keeps lowest id; child `IssueStatusHistory` cascade-deletes). No hard DB unique constraint exists for issues — a legitimate same-day re-report of the same fault (with a real date) is valid data, so enforcement is deliberately at the import/dedup layer only.

**Issue detail layout (issue-first):** `detail.html` leads with a full-width **hero block** (`.issue-hero` + `.issue-hero--<severity>` left-border accent) that makes the reported problem the focal point — large category heading, severity + status badges inline, the full description as prominent body text, and a meta line (truck · reporter · report date). Supporting truck/info metadata (make/model/year, plate, admin "Ver ficha / editar camión" link, reporter, report/resolution dates, `time_to_resolution`, assignee) drops below the hero as a lighter two-up `.profile-grid`. The status-change form and status-history timeline are unchanged.

**Templates:** `templates/issues/` — `report.html`, `report_success.html`, `queue.html`, `detail.html`

**Admin UI integration:**
- Admin user management page (`admin_users.html`) has "Roles Taller" checkboxes (Taller/Oficina) in both create and edit forms. Issue roles are synced via `UserIssueRole` on save.
- Employee detail page has a "Link de Reporte" button (admin-only) that expands to show the driver's reporting URL (copyable) or a button to generate one.

**Unified vehicle history (permits ↔ issues)**: The permits-side equipment detail page (`equipment.html` / `equipment_detail` in `app.py`) is the single source of truth for a vehicle — both modules read the same `Equipment` row, so edits made via `/equipment/<id>/edit` propagate everywhere automatically. The equipment detail page shows an admin-only "Reportes / Averías" section listing that vehicle's issue reports (newest first, via the `equipment.issues` backref, passed as `issues` to the template); each row links to `issues.detail` (admins are auto-granted `@shop_required`). To make editing discoverable from the reports side, both the issue detail "Unidad" card and the queue truck cards show an admin-only "Ver ficha / editar camión" link to `equipment_detail` (the queue link uses `event.stopPropagation()` so it doesn't toggle the card). A unit quick-added via `/issues/vehicle/new` is a normal `Equipment` row and is fully editable on this page.

## Static Assets

- `static/css/style.css` — All CSS in one file (~1920 lines). Custom CSS with CSS variables, no framework. Three responsive breakpoints (1024px, 768px, 480px). Dual theme (light/dark) via `data-theme` attribute. Issue UI: severity-accented `.issue-row*` (queue) and `.issue-hero*` (detail) blocks reuse the severity/status color tokens. Generic card primitives `.card-header` / `.card-body` (the `.card-header` selector shares the rule with `.profile-card-header`: uppercase banded header) style the `.profile-card`s on `issues/detail.html` and the `queue.html` Recientes/Resueltos sections — keep new cards on these classes rather than re-declaring padding. Use `.card-stack` (`margin-top: var(--space-lg)`) to space stacked cards instead of inline styles. All **detail/profile pages share the `.detail-header` header pattern** (block back-link above an `<h1>`, optional `.detail-title-row` with right-aligned `.detail-actions`); `issues/detail.html` follows it too, with the status/severity badges shown once in the `.issue-hero` (not duplicated in the header).
- `static/js/app.js` — All JS in one file (~200 lines). Vanilla JS, no framework. Theme toggle, sidebar/drawer, alerts modal, flash messages, swipe gestures.
- `static/manifest.json` — PWA manifest with explicit `scope: "/"`. Also served at `/manifest.json` via Flask route.
- `static/sw.js` — Service worker. Caches CSS, JS, and Font Awesome on install. Cache-first for `/static/` and CDN assets, network-first with cache fallback for HTML.
- `static/img/` — Company logos (`logo-lb.png`, `logo-pli.png`) and PWA icons (`icon-152.png`, `icon-180.png`, `icon-192.png`, `icon-512.png`, `icon-maskable-512.png`).
- No build pipeline — assets served directly by Flask. `flask-compress` handles gzip/brotli. `versioned_static()` appends `?v=<hash>` for cache busting.

## Deployment

Deployed on Railway using Dockerfile. WeasyPrint requires system-level libraries (pango, cairo, gdk-pixbuf) installed in the Docker image via `apt-get`. Gunicorn runs with `--preload` for single-instance APScheduler.
