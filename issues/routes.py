import uuid
from collections import defaultdict
from datetime import datetime
from flask import render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from issues import issues_bp
from issues.decorators import shop_required
from models import (
    db, Employee, Equipment, Issue, IssueStatusHistory,
    ISSUE_CATEGORIES, ISSUE_SEVERITIES, ISSUE_STATUSES,
    EXCLUDED_EQUIPMENT_MODELS,
)

SEVERITY_RANK = {'critica': 4, 'alta': 3, 'media': 2, 'baja': 1}


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
        query = query.filter(Issue.current_status.notin_(['cerrado']))

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

    return render_template('issues/queue.html',
                           lb_trucks=lb_trucks,
                           pli_trucks=pli_trucks,
                           lb_issue_count=sum(t['issue_count'] for t in lb_trucks),
                           pli_issue_count=sum(t['issue_count'] for t in pli_trucks),
                           recent_issues=recent_issues,
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
