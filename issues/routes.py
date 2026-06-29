import os
import uuid
import secrets
import tempfile
import unicodedata
from collections import defaultdict
from datetime import datetime, date
from flask import render_template, request, redirect, url_for, flash, abort, session
from flask_login import login_required, current_user
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from issues import issues_bp
from issues.decorators import shop_required, admin_required, shop_manager_required
from models import (
    db, Employee, Equipment, EquipmentPermit, Issue, IssueStatusHistory,
    ISSUE_CATEGORIES, ISSUE_SEVERITIES, ISSUE_STATUSES,
    EXCLUDED_EQUIPMENT_MODELS, EQUIPMENT_PERMIT_TYPES,
)

SEVERITY_RANK = {'critica': 4, 'alta': 3, 'media': 2, 'baja': 1}
RESOLVED_STATUSES = ['resuelto', 'cerrado']


def _normalize_unit(raw):
    """Normalize a unit identifier for matching: handle numeric Excel cells,
    a leading '#', and case/whitespace."""
    if raw is None:
        return ''
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)            # 204.0 -> 204, avoids "204.0"
    s = str(raw).strip()
    s = s.lstrip('#').strip()     # "#204" -> "204"
    return s.lower()


def _unreportable_reason(eq):
    """If equipment exists but won't appear in the driver report form, explain why.
    Returns a Spanish reason string, or None if the equipment is reportable."""
    if eq.company not in ('LB', 'PLI'):
        return f'empresa "{eq.company}"'
    if eq.equipment_type != 'vehicle':
        return f'tipo "{eq.equipment_type}"'
    if eq.status != 'activo':
        return f'estado "{eq.status}"'
    if eq.model and eq.model.strip().lower() in EXCLUDED_EQUIPMENT_MODELS:
        return f'modelo "{eq.model}" excluido'
    return None


def reportable_equipment_query(company=None):
    query = Equipment.query.filter(
        Equipment.status == 'activo',
        Equipment.equipment_type == 'vehicle',
        Equipment.company.in_(['LB', 'PLI']),
    )
    for model_name in EXCLUDED_EQUIPMENT_MODELS:
        query = query.filter(db.or_(
            Equipment.model == None,
            func.lower(Equipment.model) != model_name,
        ))
    if company:
        query = query.filter_by(company=company)
    return query.order_by(Equipment.unit_number)


def update_issue_status(issue, new_status, changed_by_user_id=None,
                        changed_by_employee_id=None, notes=None):
    old_status = issue.current_status

    history = IssueStatusHistory(
        issue_id=issue.id,
        from_status=old_status,
        to_status=new_status,
        changed_by_user_id=changed_by_user_id,
        changed_by_employee_id=changed_by_employee_id,
        notes=notes,
    )
    db.session.add(history)

    issue.current_status = new_status

    if new_status in ('resuelto', 'cerrado') and issue.resolved_at is None:
        issue.resolved_at = datetime.utcnow()
    elif new_status not in ('resuelto', 'cerrado'):
        issue.resolved_at = None


BACKLOG_NOTE = 'Importado del backlog'


def synthesize_status_history(issue, final_status, reported_at, resolved_at=None,
                              initial_note=None):
    """Backfill an issue's timeline for manual/bulk entry.

    Unlike ``update_issue_status`` (which stamps ``changed_at`` with the current
    time), this writes ``IssueStatusHistory`` rows with explicit timestamps so an
    imported issue reflects its real history. ``issue`` must already be flushed so
    it has an id. Sets ``current_status`` and, for resolved/closed issues,
    ``resolved_at``. Also aligns ``reported_at``/``created_at`` so the issue sorts
    correctly in the queue.
    """
    issue.reported_at = reported_at
    issue.created_at = reported_at

    db.session.add(IssueStatusHistory(
        issue_id=issue.id,
        from_status=None,
        to_status='reportado',
        changed_at=reported_at,
        notes=initial_note or BACKLOG_NOTE,
    ))

    if final_status != 'reportado':
        final_at = resolved_at or reported_at
        db.session.add(IssueStatusHistory(
            issue_id=issue.id,
            from_status='reportado',
            to_status=final_status,
            changed_at=final_at,
            notes=BACKLOG_NOTE,
        ))

    issue.current_status = final_status
    if final_status in ('resuelto', 'cerrado'):
        issue.resolved_at = resolved_at or reported_at
    else:
        issue.resolved_at = None


# ── DRIVER REPORT FLOW ───────────────────────────────────────────────

@issues_bp.route('/report/<token>', methods=['GET'])
def report(token):
    employee = Employee.query.filter_by(access_token=token).first_or_404()
    trucks = reportable_equipment_query(company=employee.company).all()
    return render_template('issues/report.html',
                           employee=employee,
                           trucks=trucks,
                           categories=ISSUE_CATEGORIES,
                           severities=ISSUE_SEVERITIES)


@issues_bp.route('/report/<token>', methods=['POST'])
def report_submit(token):
    employee = Employee.query.filter_by(access_token=token).first_or_404()

    equipment_id = request.form.get('equipment_id', type=int)
    category = request.form.get('category', '')
    severity = request.form.get('severity', 'media')
    description = request.form.get('description', '').strip()

    errors = []
    if not equipment_id:
        errors.append('Debe seleccionar un camión.')
    elif not reportable_equipment_query(company=employee.company).filter_by(id=equipment_id).first():
        errors.append('Camión no válido.')

    valid_categories = [c[0] for c in ISSUE_CATEGORIES]
    if category not in valid_categories:
        errors.append('Categoría no válida.')

    valid_severities = [s[0] for s in ISSUE_SEVERITIES]
    if severity not in valid_severities:
        errors.append('Severidad no válida.')

    if not description:
        errors.append('Debe incluir una descripción del problema.')

    if errors:
        trucks = reportable_equipment_query(company=employee.company).all()
        for error in errors:
            flash(error, 'danger')
        return render_template('issues/report.html',
                               employee=employee,
                               trucks=trucks,
                               categories=ISSUE_CATEGORIES,
                               severities=ISSUE_SEVERITIES), 422

    issue = Issue(
        equipment_id=equipment_id,
        reported_by_employee_id=employee.id,
        category=category,
        severity=severity,
        description=description,
    )
    db.session.add(issue)
    db.session.flush()

    update_issue_status(issue, 'reportado', changed_by_employee_id=employee.id,
                        notes='Reporte inicial del chofer')
    db.session.commit()

    return redirect(url_for('issues.report_success', token=token, issue_id=issue.id))


@issues_bp.route('/report/<token>/success', methods=['GET'])
def report_success(token):
    Employee.query.filter_by(access_token=token).first_or_404()
    issue_id = request.args.get('issue_id', type=int)
    return render_template('issues/report_success.html', token=token, issue_id=issue_id)


# ── SHOP QUEUE ────────────────────────────────────────────────────────

@issues_bp.route('/', methods=['GET'])
@shop_required
def queue():
    query = (Issue.query
             .join(Equipment, Issue.equipment_id == Equipment.id, isouter=True)
             .options(joinedload(Issue.reporter)))

    status_filter = request.args.get('status', '')
    if status_filter:
        query = query.filter(Issue.current_status == status_filter)
    else:
        query = query.filter(Issue.current_status.notin_(RESOLVED_STATUSES))

    severity_filter = request.args.get('severity', '')
    if severity_filter:
        query = query.filter(Issue.severity == severity_filter)

    equipment_filter = request.args.get('equipment_id', type=int)
    if equipment_filter:
        query = query.filter(Issue.equipment_id == equipment_filter)

    all_issues = query.order_by(Issue.reported_at.desc()).all()

    issues_by_equipment = defaultdict(list)
    for issue in all_issues:
        if issue.equipment_id:
            issues_by_equipment[issue.equipment_id].append(issue)

    def build_truck_profiles(company):
        all_trucks = reportable_equipment_query(company=company).all()
        profiles = []
        for eq in all_trucks:
            issues = issues_by_equipment.get(eq.id, [])
            worst_rank = max((SEVERITY_RANK.get(i.severity, 0) for i in issues), default=0)
            worst_sev = next((s for s, r in SEVERITY_RANK.items() if r == worst_rank), None)
            profiles.append({
                'equipment': eq,
                'issues': issues,
                'issue_count': len(issues),
                'worst_severity': worst_sev,
                'worst_severity_rank': worst_rank,
                'auto_expand': len(issues) > 0,
            })
        profiles.sort(key=lambda p: (-p['worst_severity_rank'], -p['issue_count'],
                                     p['equipment'].display_name))
        return profiles

    lb_trucks = build_truck_profiles('LB')
    pli_trucks = build_truck_profiles('PLI')

    status_counts = {}
    for code, label in ISSUE_STATUSES:
        status_counts[code] = Issue.query.filter_by(current_status=code).count()

    trucks = reportable_equipment_query().all()
    recent_issues = all_issues[:20]

    resolved_issues = []
    resolved_count = 0
    if not status_filter:
        resolved_query = (Issue.query
                          .options(joinedload(Issue.reporter))
                          .filter(Issue.current_status.in_(RESOLVED_STATUSES)))
        if severity_filter:
            resolved_query = resolved_query.filter(Issue.severity == severity_filter)
        if equipment_filter:
            resolved_query = resolved_query.filter(Issue.equipment_id == equipment_filter)
        resolved_count = resolved_query.count()
        resolved_issues = (resolved_query
                           .order_by(Issue.resolved_at.desc().nullslast(),
                                     Issue.reported_at.desc())
                           .limit(20).all())

    return render_template('issues/queue.html',
                           lb_trucks=lb_trucks,
                           pli_trucks=pli_trucks,
                           lb_issue_count=sum(t['issue_count'] for t in lb_trucks),
                           pli_issue_count=sum(t['issue_count'] for t in pli_trucks),
                           recent_issues=recent_issues,
                           resolved_issues=resolved_issues,
                           resolved_count=resolved_count,
                           statuses=ISSUE_STATUSES,
                           severities=ISSUE_SEVERITIES,
                           categories=ISSUE_CATEGORIES,
                           trucks=trucks,
                           status_counts=status_counts,
                           current_status=status_filter,
                           current_severity=severity_filter,
                           current_equipment=equipment_filter)


# ── ISSUE DETAIL ──────────────────────────────────────────────────────

@issues_bp.route('/<int:issue_id>', methods=['GET'])
@shop_required
def detail(issue_id):
    issue = Issue.query.get_or_404(issue_id)
    history = issue.status_history.order_by(IssueStatusHistory.changed_at.asc()).all()
    return render_template('issues/detail.html',
                           issue=issue,
                           history=history,
                           statuses=ISSUE_STATUSES)


@issues_bp.route('/<int:issue_id>/status', methods=['POST'])
@shop_required
def update_status(issue_id):
    issue = Issue.query.get_or_404(issue_id)

    new_status = request.form.get('new_status', '')
    notes = request.form.get('notes', '').strip() or None

    valid_statuses = [s[0] for s in ISSUE_STATUSES]
    if new_status not in valid_statuses:
        flash('Estado no válido.', 'danger')
        return redirect(url_for('issues.detail', issue_id=issue.id))

    if new_status == issue.current_status:
        flash('El estado no ha cambiado.', 'warning')
        return redirect(url_for('issues.detail', issue_id=issue.id))

    update_issue_status(issue, new_status, changed_by_user_id=current_user.id, notes=notes)
    db.session.commit()

    status_label = dict(ISSUE_STATUSES).get(new_status, new_status)
    flash(f'Estado actualizado a "{status_label}".', 'success')
    return redirect(url_for('issues.detail', issue_id=issue.id))


# ── ADMIN: GENERATE DRIVER LINK ──────────────────────────────────────

@issues_bp.route('/generate-link/<int:employee_id>', methods=['POST'])
@login_required
def generate_link(employee_id):
    if not current_user.is_admin:
        abort(403)
    emp = Employee.query.get_or_404(employee_id)
    emp.access_token = str(uuid.uuid4())
    db.session.commit()
    flash(f'Link de reporte generado para {emp.name}.', 'success')
    return redirect(url_for('employee_detail', id=employee_id))


# ── ADMIN: MANUAL ENTRY & BULK BACKLOG IMPORT ────────────────────────

def _coerce_date(cell):
    """Coerce an Excel cell (or date-string) to a date or None.

    Mirrors the equipment import's ``to_date`` including the 2-digit-year fix
    (Excel sometimes reads e.g. 2030 as 1930).
    """
    if cell is None or cell == '':
        return None
    if isinstance(cell, datetime):
        d = cell.date()
    elif isinstance(cell, date):
        d = cell
    elif isinstance(cell, str):
        s = cell.strip()
        if not s:
            return None
        d = None
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y'):
            try:
                d = datetime.strptime(s, fmt).date()
                break
            except ValueError:
                continue
        if d is None:
            return None
    else:
        return None
    if d.year < 2000:
        try:
            d = d.replace(year=d.year + 100)
        except ValueError:
            pass
    return d


def _as_datetime(d):
    """A date (or None) → a datetime at midnight (or None)."""
    if d is None:
        return None
    return datetime(d.year, d.month, d.day)


def _normalize_desc(text):
    """Lower-case and collapse whitespace for duplicate comparison."""
    return ' '.join((text or '').split()).lower()


def _canonical_report_day(reported_at):
    """The report DATE used for dedup, or None when the timestamp is volatile.

    Real spreadsheet dates are stored at midnight via _as_datetime(); a
    non-midnight time means "Fecha Reporte" was blank at import and a utcnow()
    fallback was used, so it must NOT anchor the dedup key (otherwise every
    re-import mints a new duplicate).
    """
    if not reported_at:
        return None
    if reported_at.hour or reported_at.minute or reported_at.second or reported_at.microsecond:
        return None
    return reported_at.date()


def find_duplicate_issue(equipment_id, category, description, report_day):
    """Return an existing Issue matching the import natural key, else None.

    Identity = (equipment_id, category, normalized description, canonical report
    day). ``report_day`` is the row's parsed date or None; a dateless row
    (None) matches a prior dateless issue regardless of which day each was
    imported (see _canonical_report_day). Compared in Python over the small
    per-truck+category candidate set so the normalization behaves identically
    on SQLite and Postgres.
    """
    norm = _normalize_desc(description)
    candidates = Issue.query.filter_by(equipment_id=equipment_id, category=category).all()
    for existing in candidates:
        if _normalize_desc(existing.description) != norm:
            continue
        if _canonical_report_day(existing.reported_at) == report_day:
            return existing
    return None


@issues_bp.route('/new', methods=['GET'])
@shop_manager_required
def new():
    trucks = reportable_equipment_query().all()
    return render_template('issues/manual_new.html',
                           trucks=trucks,
                           categories=ISSUE_CATEGORIES,
                           severities=ISSUE_SEVERITIES,
                           statuses=ISSUE_STATUSES)


@issues_bp.route('/new', methods=['POST'])
@shop_manager_required
def create():
    equipment_id = request.form.get('equipment_id', type=int)
    category = request.form.get('category', '')
    severity = request.form.get('severity', 'media')
    status = request.form.get('status', 'reportado') or 'reportado'
    description = request.form.get('description', '').strip()
    notes = request.form.get('notes', '').strip()

    errors = []
    if not equipment_id:
        errors.append('Debe seleccionar un camión.')
    elif not reportable_equipment_query().filter_by(id=equipment_id).first():
        errors.append('Camión no válido.')

    if category not in [c[0] for c in ISSUE_CATEGORIES]:
        errors.append('Categoría no válida.')
    if severity not in [s[0] for s in ISSUE_SEVERITIES]:
        errors.append('Severidad no válida.')
    if status not in [s[0] for s in ISSUE_STATUSES]:
        errors.append('Estado no válido.')
    if not description:
        errors.append('Debe incluir una descripción del problema.')

    reported_at = _as_datetime(_coerce_date(request.form.get('fecha_reporte', ''))) \
        or datetime.utcnow()
    resolved_at = _as_datetime(_coerce_date(request.form.get('fecha_resuelto', '')))

    if errors:
        for error in errors:
            flash(error, 'danger')
        return render_template('issues/manual_new.html',
                               trucks=reportable_equipment_query().all(),
                               categories=ISSUE_CATEGORIES,
                               severities=ISSUE_SEVERITIES,
                               statuses=ISSUE_STATUSES), 422

    issue = Issue(
        equipment_id=equipment_id,
        category=category,
        severity=severity,
        description=description,
    )
    db.session.add(issue)
    db.session.flush()

    if current_user.is_admin:
        # Admins backfill the backlog: honor status, backdated dates, and note.
        initial_note = f'{BACKLOG_NOTE} — {notes}' if notes else BACKLOG_NOTE
        synthesize_status_history(issue, status, reported_at, resolved_at,
                                  initial_note=initial_note)
    else:
        # Shop managers add a live report: always a fresh 'reportado' dated now,
        # attributed to the manager's user id.
        update_issue_status(issue, 'reportado', changed_by_user_id=current_user.id,
                            notes='Reporte agregado manualmente por taller')
    db.session.commit()

    flash(f'Reporte #{issue.id} agregado.', 'success')
    return redirect(url_for('issues.detail', issue_id=issue.id))


# ── ADMIN: QUICK-ADD REPORTABLE VEHICLE ──────────────────────────────

@issues_bp.route('/vehicle/new', methods=['GET'])
@admin_required
def vehicle_new():
    return render_template('issues/vehicle_new.html', companies=['LB', 'PLI'])


@issues_bp.route('/vehicle/new', methods=['POST'])
@admin_required
def vehicle_create():
    company = request.form.get('company', '')
    unit_number = request.form.get('unit_number', '').strip()
    plate_number = request.form.get('plate_number', '').strip()
    make = request.form.get('make', '').strip()
    model = request.form.get('model', '').strip()
    year_raw = request.form.get('year', '').strip()

    errors = []
    if company not in ('LB', 'PLI'):
        errors.append('Debe seleccionar una empresa (LB o PLI).')
    if not unit_number:
        errors.append('Debe incluir el número de unidad.')
    if model and model.lower() in EXCLUDED_EQUIPMENT_MODELS:
        errors.append(f'El modelo "{model}" está excluido de los reportes; '
                      'el camión no aparecería en la lista.')

    from app import find_duplicate_equipment
    match = find_duplicate_equipment(None, plate_number, unit_number, company)
    if match:
        errors.append(f'Ya existe un camión con esa unidad/placa: {match.display_name} '
                      f'({match.company_full}). Verifique antes de crear un duplicado.')

    year = None
    if year_raw:
        try:
            year = int(year_raw)
        except ValueError:
            errors.append('El año no es válido.')

    if errors:
        for error in errors:
            flash(error, 'danger')
        return render_template('issues/vehicle_new.html',
                               companies=['LB', 'PLI']), 422

    equip = Equipment(
        company=company,
        equipment_type='vehicle',
        status='activo',
        unit_number=unit_number,
        plate_number=plate_number,
        make=make,
        model=model,
        year=year,
    )
    db.session.add(equip)
    db.session.flush()

    # Create default permit slots (mirrors equipment_new in app.py).
    for code, name in EQUIPMENT_PERMIT_TYPES:
        if code == 'OTHER':
            continue
        applicability = 'YES'
        # Insurance is shared at the company level for LB/PLI.
        if code == 'SEGURO' and equip.company in ('LB', 'PLI'):
            applicability = 'N/A'
        db.session.add(EquipmentPermit(
            equipment_id=equip.id,
            permit_type=code,
            applicability=applicability,
        ))

    db.session.commit()
    flash(f'Camión {equip.display_name} agregado.', 'success')
    return redirect(url_for('issues.queue'))


@issues_bp.route('/import', methods=['GET'])
@admin_required
def import_form():
    session['issue_import_token'] = secrets.token_hex(16)
    return render_template('issues/import.html',
                           import_token=session['issue_import_token'],
                           categories=ISSUE_CATEGORIES,
                           severities=ISSUE_SEVERITIES,
                           statuses=ISSUE_STATUSES)


@issues_bp.route('/import', methods=['POST'])
@admin_required
def import_issues():
    from openpyxl import load_workbook
    from sqlalchemy.exc import IntegrityError

    # Idempotency: block double-submit / re-POST on refresh.
    token = request.form.get('import_token', '')
    if token != session.pop('issue_import_token', None):
        flash('Formulario ya fue procesado. Por favor intente nuevamente.', 'warning')
        return redirect(url_for('issues.import_form'))

    if 'file' not in request.files:
        flash('No se seleccionó archivo.', 'danger')
        return redirect(url_for('issues.import_form'))

    file = request.files['file']
    if not file.filename.endswith(('.xlsx', '.xls')):
        flash('Solo se aceptan archivos Excel (.xlsx, .xls).', 'danger')
        return redirect(url_for('issues.import_form'))

    # Lookup maps: accept either the internal code or the Spanish label.
    cat_map = {c[0]: c[0] for c in ISSUE_CATEGORIES}
    cat_map.update({c[1].lower(): c[0] for c in ISSUE_CATEGORIES})
    sev_map = {s[0]: s[0] for s in ISSUE_SEVERITIES}
    sev_map.update({s[1].lower(): s[0] for s in ISSUE_SEVERITIES})
    status_map = {s[0]: s[0] for s in ISSUE_STATUSES}
    status_map.update({s[1].lower(): s[0] for s in ISSUE_STATUSES})

    # Truck lookup by unit_number, falling back to plate_number (both normalized).
    # Matches against ALL equipment so a historical backlog issue can attach to any
    # truck, including currently inactive/non-reportable ones. unit_number wins over
    # plate_number when both resolve to the same key.
    truck_by_unit = {}
    for eq in Equipment.query.all():
        if eq.unit_number:
            truck_by_unit[_normalize_unit(eq.unit_number)] = eq
    for eq in Equipment.query.all():
        if eq.plate_number:
            truck_by_unit.setdefault(_normalize_unit(eq.plate_number), eq)

    imported = 0
    duplicates = 0
    errors = []
    notices = []
    dup_notices = []
    seen = {}
    filepath = None
    try:
        fd, filepath = tempfile.mkstemp(suffix='.xlsx')
        os.close(fd)
        file.save(filepath)
        wb = load_workbook(filepath, read_only=True)

        if db.engine.dialect.name == 'postgresql':
            db.session.execute(db.text("SELECT pg_advisory_xact_lock(44)"))

        ws = None
        for name in wb.sheetnames:
            if 'reporte' in name.lower() or 'issue' in name.lower() or 'problema' in name.lower():
                ws = wb[name]
                break
        if ws is None:
            ws = wb[wb.sheetnames[0]]

        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            wb.close()
            flash('El archivo está vacío.', 'warning')
            return redirect(url_for('issues.import_form'))

        # Reject the wrong template.
        headers = [str(h).strip().upper() if h else '' for h in rows[0]]
        normalized = [unicodedata.normalize('NFKD', h).encode('ascii', 'ignore').decode()
                      for h in headers]
        joined = ' '.join(normalized)
        if 'UNIDAD' not in joined or ('DESCRIPCION' not in joined and 'DESCRIPCIN' not in joined):
            wb.close()
            flash('El archivo no parece ser de reportes. Verifique que está usando la '
                  'plantilla correcta (Unidad, Categoría, Severidad, Descripción, '
                  'Estado, Fecha Reporte, Fecha Resuelto).', 'danger')
            return redirect(url_for('issues.import_form'))

        # Columns: Unidad | Categoría | Severidad | Descripción | Estado | Fecha Reporte | Fecha Resuelto
        for idx, row in enumerate(rows[1:], start=2):
            if not row or not any(row):
                continue

            def cell(i):
                return row[i] if i < len(row) else None

            unit_raw = cell(0)
            cat_raw = cell(1)
            sev_raw = cell(2)
            desc_raw = cell(3)
            status_raw = cell(4)
            reported_raw = cell(5)
            resolved_raw = cell(6)

            unit = _normalize_unit(unit_raw)
            truck = truck_by_unit.get(unit)
            if not truck:
                display = str(unit_raw).strip() if unit_raw is not None else ''
                errors.append(
                    f'Fila {idx}: no existe ninguna unidad con número/placa '
                    f'"{display or "(vacía)"}".')
                continue
            reason = _unreportable_reason(truck)

            description = str(desc_raw).strip() if desc_raw is not None else ''
            if not description:
                errors.append(f'Fila {idx}: falta la descripción.')
                continue

            category = cat_map.get(str(cat_raw).strip().lower()) if cat_raw is not None else None
            if not category:
                errors.append(f'Fila {idx}: categoría "{cat_raw}" no válida.')
                continue

            sev_key = str(sev_raw).strip().lower() if sev_raw is not None else ''
            severity = sev_map.get(sev_key, 'media') if sev_key else 'media'

            status_key = str(status_raw).strip().lower() if status_raw is not None else ''
            status = status_map.get(status_key, 'reportado') if status_key else 'reportado'
            if status_key and status_key not in status_map:
                errors.append(f'Fila {idx}: estado "{status_raw}" no válido.')
                continue

            report_date = _coerce_date(reported_raw)                      # date or None
            reported_at = _as_datetime(report_date) or datetime.utcnow()  # stored value
            resolved_at = _as_datetime(_coerce_date(resolved_raw))

            # Skip duplicates so re-importing the same file is idempotent. Match
            # against rows already created in this batch (seen) and rows already
            # in the DB from a previous import (find_duplicate_issue). On a hit we
            # update the existing issue rather than create a new one. Key on the
            # PARSED date (None for dateless rows) — never the volatile utcnow
            # fallback — so dateless rows collapse instead of duplicating.
            dup_key = (truck.id, category, _normalize_desc(description),
                       report_date)
            existing = seen.get(dup_key) or find_duplicate_issue(
                truck.id, category, description, report_date)
            if existing:
                existing.severity = severity
                if status != existing.current_status:
                    synthesize_status_history(existing, status, reported_at, resolved_at)
                duplicates += 1
                dup_notices.append(
                    f'Fila {idx}: duplicado de "{truck.display_name}" — '
                    f'actualizado (no se creó uno nuevo).')
                continue

            issue = Issue(
                equipment_id=truck.id,
                category=category,
                severity=severity,
                description=description,
            )
            db.session.add(issue)
            db.session.flush()
            synthesize_status_history(issue, status, reported_at, resolved_at)
            seen[dup_key] = issue
            imported += 1

            if reason:
                notices.append(
                    f'Fila {idx}: unidad "{truck.display_name}" importada, pero está '
                    f'marcada con {reason} y no aparece en el formulario del chofer.')

        wb.close()
        db.session.commit()

    except IntegrityError:
        db.session.rollback()
        flash('Error: datos duplicados detectados. Posible importación simultánea.', 'danger')
        return redirect(url_for('issues.import_form'))
    except Exception as e:
        db.session.rollback()
        flash(f'Error en la importación: {str(e)}', 'danger')
        return redirect(url_for('issues.import_form'))
    finally:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)

    if imported or duplicates:
        flash(f'Importación completa: {imported} reportes importados, '
              f'{duplicates} duplicados omitidos, {len(errors)} con errores.', 'success')
    else:
        flash(f'No se importó ningún reporte. {len(errors)} fila(s) con errores.', 'warning')

    # Re-issue a token so the results page can be re-submitted from if needed.
    session['issue_import_token'] = secrets.token_hex(16)
    return render_template('issues/import.html',
                           import_token=session['issue_import_token'],
                           categories=ISSUE_CATEGORIES,
                           severities=ISSUE_SEVERITIES,
                           statuses=ISSUE_STATUSES,
                           import_errors=errors,
                           import_notices=notices,
                           dup_notices=dup_notices,
                           imported_count=imported,
                           duplicate_count=duplicates)
