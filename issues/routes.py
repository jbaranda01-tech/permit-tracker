from datetime import datetime
from flask import render_template, request, redirect, url_for, flash
from flask_login import current_user

from issues import issues_bp
from issues.decorators import shop_required, shop_or_office_required
from models import (
    db, Employee, Equipment, Issue, IssueStatusHistory,
    ISSUE_CATEGORIES, ISSUE_SEVERITIES, ISSUE_STATUSES,
)


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


# ── DRIVER REPORT FLOW ───────────────────────────────────────────────

@issues_bp.route('/report/<token>', methods=['GET'])
def report(token):
    employee = Employee.query.filter_by(access_token=token).first_or_404()
    trucks = (Equipment.query
              .filter_by(status='activo', equipment_type='vehicle', company=employee.company)
              .order_by(Equipment.unit_number)
              .all())
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
    odometer = request.form.get('odometer_reading', type=int)
    location = request.form.get('location', '').strip() or None

    errors = []
    if not equipment_id:
        errors.append('Debe seleccionar un camión.')
    elif not Equipment.query.filter_by(
        id=equipment_id, status='activo', equipment_type='vehicle', company=employee.company
    ).first():
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
        trucks = (Equipment.query
                  .filter_by(status='activo', equipment_type='vehicle', company=employee.company)
                  .order_by(Equipment.unit_number)
                  .all())
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
        odometer_reading=odometer,
        location=location,
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
@shop_or_office_required
def queue():
    query = Issue.query

    status_filter = request.args.get('status', '')
    if status_filter:
        query = query.filter_by(current_status=status_filter)
    else:
        query = query.filter(Issue.current_status.notin_(['cerrado']))

    severity_filter = request.args.get('severity', '')
    if severity_filter:
        query = query.filter_by(severity=severity_filter)

    equipment_filter = request.args.get('equipment_id', type=int)
    if equipment_filter:
        query = query.filter_by(equipment_id=equipment_filter)

    issues = query.order_by(Issue.reported_at.desc()).all()

    status_counts = {}
    for code, label in ISSUE_STATUSES:
        status_counts[code] = Issue.query.filter_by(current_status=code).count()

    trucks = (Equipment.query
              .filter_by(status='activo', equipment_type='vehicle')
              .order_by(Equipment.unit_number)
              .all())

    return render_template('issues/queue.html',
                           issues=issues,
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
@shop_or_office_required
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
