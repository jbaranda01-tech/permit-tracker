import os
import uuid
import secrets
import click
import logging
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from io import BytesIO
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_file, jsonify, abort, session
)
from flask_login import (
    LoginManager, login_user, logout_user,
    login_required, current_user
)
from flask_migrate import Migrate
from werkzeug.utils import secure_filename

from config import Config
from models import (
    db, User, Employee, EmployeePermit, Equipment, EquipmentPermit,
    CompanyPermit, EMPLOYEE_PERMIT_TYPES, EQUIPMENT_PERMIT_TYPES,
    COMPANY_PERMIT_TYPES, FileStorage, NotificationLog
)

# ── APP INIT ───────────────────────────────────────────────────────────

app = Flask(__name__)
app.config.from_object(Config)

# Ensure logs go to stdout for Railway
if not app.debug:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level or logging.INFO)

db.init_app(app)
migrate = Migrate(app, db)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Por favor inicie sesión para acceder.'
login_manager.login_message_category = 'warning'


ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'doc', 'docx'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def save_uploaded_file(file):
    """Save an uploaded file to the database and return the storage filename."""
    filename = f"{uuid.uuid4().hex}_{secure_filename(file.filename)}"
    file_data = file.read()
    stored = FileStorage(
        filename=filename,
        original_filename=file.filename,
        mime_type=file.content_type or 'application/octet-stream',
        data=file_data,
        size=len(file_data),
    )
    db.session.add(stored)
    return filename


def delete_stored_file(filename):
    """Delete a file from the database by filename."""
    stored = FileStorage.query.filter_by(filename=filename).first()
    if stored:
        db.session.delete(stored)


# ── NOTIFICATION HELPERS ──────────────────────────────────────────────

def _gather_expiring_items(employee_id=None):
    today = date.today()
    alert_date = today + timedelta(days=app.config['ALERT_DAYS_BEFORE'])

    query = Employee.query.filter(
        Employee.status == 'activo',
        Employee.email.isnot(None),
        Employee.email != '',
    )
    if employee_id:
        query = query.filter(Employee.id == employee_id)

    results = {}
    for emp in query.all():
        items = []
        if emp.license_expiration:
            if emp.license_expiration < today:
                items.append({'key': f'license:{emp.id}', 'name': 'Licencia de Conducir',
                              'date': emp.license_expiration, 'status': 'expired'})
            elif emp.license_expiration <= alert_date:
                items.append({'key': f'license:{emp.id}', 'name': 'Licencia de Conducir',
                              'date': emp.license_expiration, 'status': 'expiring_soon'})

        for permit in emp.permits:
            if permit.applicability == 'N/A' or permit.expiration_date is None:
                continue
            if permit.expiration_date < today:
                items.append({'key': f'employee_permit:{permit.id}', 'name': permit.display_name,
                              'date': permit.expiration_date, 'status': 'expired'})
            elif permit.expiration_date <= alert_date:
                items.append({'key': f'employee_permit:{permit.id}', 'name': permit.display_name,
                              'date': permit.expiration_date, 'status': 'expiring_soon'})

        if items:
            results[emp.id] = {'employee': emp, 'items': items}
    return results


def _filter_already_notified(employee_id, items):
    cutoff = datetime.utcnow() - timedelta(days=7)
    recent = NotificationLog.query.filter(
        NotificationLog.employee_id == employee_id,
        NotificationLog.sent_at > cutoff,
        NotificationLog.status == 'sent',
    ).all()
    notified_keys = {log.permit_key for log in recent}
    return [item for item in items if item['key'] not in notified_keys]


def send_notification_email(employee, items, dry_run=False):
    if dry_run:
        return (True, None)
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, HtmlContent

        html = render_template('email/permit_expiry_notice.html',
                               employee=employee, items=items)
        message = Mail(
            from_email=app.config['SENDGRID_FROM_EMAIL'],
            to_emails=employee.email,
            subject='Aviso: Documentos por vencer o vencidos',
            html_content=HtmlContent(html),
        )
        sg = SendGridAPIClient(app.config['SENDGRID_API_KEY'])
        response = sg.send(message)
        if response.status_code in (200, 201, 202):
            return (True, None)
        return (False, f'SendGrid status {response.status_code}')
    except Exception as e:
        return (False, str(e))


def run_notification_cycle(dry_run=False, employee_id=None):
    data = _gather_expiring_items(employee_id)
    sent = 0
    skipped = 0
    failed = 0
    details = []

    for emp_id, entry in data.items():
        emp = entry['employee']
        remaining = _filter_already_notified(emp_id, entry['items'])
        if not remaining:
            skipped += 1
            details.append(f'  SKIP {emp.name} — already notified')
            continue

        success, error = send_notification_email(emp, remaining, dry_run=dry_run)

        for item in remaining:
            log = NotificationLog(
                employee_id=emp_id,
                permit_key=item['key'],
                email_to=emp.email,
                status='sent' if success else 'failed',
                error_message=error,
            )
            if not dry_run:
                db.session.add(log)

        if success:
            sent += 1
            details.append(f'  {"[DRY RUN] " if dry_run else ""}SENT {emp.name} ({emp.email}) — {len(remaining)} items')
        else:
            failed += 1
            details.append(f'  FAIL {emp.name} ({emp.email}) — {error}')

    if not dry_run:
        db.session.commit()

    return {'sent': sent, 'skipped': skipped, 'failed': failed, 'details': details}


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ── ROLE DECORATORS ────────────────────────────────────────────────────

def manager_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not current_user.is_manager:
            flash('No tiene permisos para esta acción.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not current_user.is_admin:
            flash('Solo administradores pueden acceder.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


# ── TEMPLATE HELPERS ───────────────────────────────────────────────────

@app.template_filter('format_phone')
def format_phone(value):
    if not value:
        return value
    digits = ''.join(c for c in str(value) if c.isdigit())
    if len(digits) == 10:
        return f'({digits[:3]}) {digits[3:6]}-{digits[6:]}'
    if len(digits) == 7:
        return f'{digits[:3]}-{digits[3:]}'
    return value

@app.context_processor
def inject_globals():
    today = date.today()
    alert_date = today + timedelta(days=30)

    try:
        # Count expiring permits (employees)
        expiring_emp = EmployeePermit.query.filter(
            EmployeePermit.applicability != 'N/A',
            EmployeePermit.expiration_date != None,
            EmployeePermit.expiration_date > today,
            EmployeePermit.expiration_date <= alert_date
        ).count()

        # Count expiring licenses
        expiring_lic = Employee.query.filter(
            Employee.license_expiration != None,
            Employee.license_expiration > today,
            Employee.license_expiration <= alert_date
        ).count()

        # Count expiring equipment permits
        expiring_eq = EquipmentPermit.query.filter(
            EquipmentPermit.applicability != 'N/A',
            EquipmentPermit.expiration_date != None,
            EquipmentPermit.expiration_date > today,
            EquipmentPermit.expiration_date <= alert_date
        ).count()

        # Count expired
        expired_emp = EmployeePermit.query.filter(
            EmployeePermit.applicability != 'N/A',
            EmployeePermit.expiration_date != None,
            EmployeePermit.expiration_date < today
        ).count()

        expired_lic = Employee.query.filter(
            Employee.license_expiration != None,
            Employee.license_expiration < today
        ).count()

        expired_eq = EquipmentPermit.query.filter(
            EquipmentPermit.applicability != 'N/A',
            EquipmentPermit.expiration_date != None,
            EquipmentPermit.expiration_date < today
        ).count()

        # Count expiring company permits
        expiring_co = CompanyPermit.query.filter(
            CompanyPermit.applicability != 'N/A',
            CompanyPermit.expiration_date != None,
            CompanyPermit.expiration_date > today,
            CompanyPermit.expiration_date <= alert_date
        ).count()

        expired_co = CompanyPermit.query.filter(
            CompanyPermit.applicability != 'N/A',
            CompanyPermit.expiration_date != None,
            CompanyPermit.expiration_date < today
        ).count()

        return {
            'permisos_por_vencer': expiring_emp + expiring_lic + expiring_eq + expiring_co,
            'permisos_vencidos': expired_emp + expired_lic + expired_eq + expired_co,
            'today': today,
            'alert_date': alert_date,
        }
    except Exception as e:
        app.logger.error(f"Database error in context processor: {e}")
        return {
            'permisos_por_vencer': 0,
            'permisos_vencidos': 0,
            'today': today,
            'alert_date': alert_date,
        }


# ── REQUEST LOGGING ────────────────────────────────────────────────────

@app.after_request
def log_request(response):
    app.logger.info(f"{request.method} {request.path} → {response.status_code}")
    return response


# ── AUTH ROUTES ────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user, remember=True)
            next_page = request.args.get('next')
            flash(f'Bienvenido, {user.username}!', 'success')
            return redirect(next_page or url_for('dashboard'))
        flash('Usuario o contraseña incorrectos.', 'danger')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Sesión cerrada.', 'info')
    return redirect(url_for('login'))


# ── DASHBOARD ──────────────────────────────────────────────────────────

@app.route('/')
@login_required
def dashboard():
    view = request.args.get('view', 'employees')  # employees or equipment
    search = request.args.get('search', '').strip()
    company_filter = request.args.get('company', '')
    status_filter = request.args.get('status', '')
    sort_by = request.args.get('sort', 'name')

    if view == 'company':
        # Backfill missing company permits
        changed = False
        for code, name, companies in COMPANY_PERMIT_TYPES:
            for company in companies:
                existing = CompanyPermit.query.filter_by(company=company, permit_type=code).first()
                if not existing:
                    db.session.add(CompanyPermit(company=company, permit_type=code, applicability='YES'))
                    changed = True
        if changed:
            db.session.commit()

        lb_permits = CompanyPermit.query.filter_by(company='LB').order_by(CompanyPermit.permit_type).all()
        pli_permits = CompanyPermit.query.filter_by(company='PLI').order_by(CompanyPermit.permit_type).all()

        return render_template('dashboard.html',
            view=view,
            lb_permits=lb_permits,
            pli_permits=pli_permits,
        )

    if view == 'equipment':
        query = Equipment.query
        if search:
            query = query.filter(
                db.or_(
                    Equipment.unit_number.ilike(f'%{search}%'),
                    Equipment.plate_number.ilike(f'%{search}%'),
                    Equipment.make.ilike(f'%{search}%'),
                    Equipment.model.ilike(f'%{search}%'),
                    Equipment.vin_serial.ilike(f'%{search}%'),
                )
            )
        if company_filter:
            query = query.filter(Equipment.company == company_filter)
        if status_filter:
            query = query.filter(Equipment.status == status_filter)

        # Split by company
        lb_items = query.filter(Equipment.company == 'LB').order_by(Equipment.make).all()
        pli_items = query.filter(Equipment.company == 'PLI').order_by(Equipment.make).all()
        personal_items = query.filter(Equipment.company == 'Personal').order_by(Equipment.make).all()
    else:
        query = Employee.query
        if search:
            query = query.filter(
                db.or_(
                    Employee.name.ilike(f'%{search}%'),
                    Employee.license_number.ilike(f'%{search}%'),
                    Employee.area.ilike(f'%{search}%'),
                )
            )
        if company_filter:
            query = query.filter(Employee.company == company_filter)
        if status_filter:
            query = query.filter(Employee.status == status_filter)

        # Split by company
        lb_items = query.filter(Employee.company == 'LB').order_by(Employee.name).all()
        pli_items = query.filter(Employee.company == 'PLI').order_by(Employee.name).all()

    return render_template('dashboard.html',
        view=view,
        lb_items=lb_items,
        pli_items=pli_items,
        personal_items=personal_items if view == 'equipment' else [],
        search=search,
        company_filter=company_filter,
        status_filter=status_filter,
    )


# ── EMPLOYEE ROUTES ────────────────────────────────────────────────────

@app.route('/employee/<int:id>')
@login_required
def employee_detail(id):
    emp = Employee.query.get_or_404(id)

    # Backfill missing permit slots for existing employees
    existing_types = {}
    for p in emp.permits.all():
        existing_types[p.permit_type] = p
    changed = False
    for code, name in EMPLOYEE_PERMIT_TYPES:
        if code == 'OTHER':
            continue
        if code not in existing_types:
            db.session.add(EmployeePermit(
                employee_id=emp.id,
                permit_type=code,
                applicability='YES' if code == 'PRIMEROS_AUXILIOS' else 'N/A'
            ))
            changed = True
        elif code == 'PRIMEROS_AUXILIOS':
            permit = existing_types[code]
            if permit.applicability == 'N/A' and permit.expiration_date is None:
                permit.applicability = 'YES'
                changed = True
    if changed:
        db.session.commit()

    permits = emp.permits.order_by(EmployeePermit.permit_type).all()
    return render_template('employee.html', employee=emp, permits=permits,
                           permit_types=EMPLOYEE_PERMIT_TYPES)


@app.route('/employee/new', methods=['GET', 'POST'])
@manager_required
def employee_new():
    if request.method == 'POST':
        emp = Employee(
            name=request.form['name'],
            company=request.form['company'],
            area=request.form.get('area', ''),
            status=request.form.get('status', 'activo'),
            license_number=request.form.get('license_number', ''),
            puesto=request.form.get('puesto', ''),
            telefono=request.form.get('telefono', ''),
            email=request.form.get('email', ''),
            contacto_emergencia=request.form.get('contacto_emergencia', ''),
            shirt_size=request.form.get('shirt_size', ''),
            endoso_hazmat=request.form.get('endoso_hazmat', 'N/A'),
        )
        # Parse dates
        for field in ['fecha_nacimiento', 'license_expiration', 'fecha_contratacion']:
            val = request.form.get(field, '')
            if val:
                try:
                    setattr(emp, field, datetime.strptime(val, '%Y-%m-%d').date())
                except ValueError:
                    pass

        # Handle license file upload
        if 'license_file' in request.files:
            file = request.files['license_file']
            if file and file.filename and allowed_file(file.filename):
                filename = save_uploaded_file(file)
                emp.license_file = filename

        db.session.add(emp)
        db.session.flush()  # Get ID

        # Create default permit slots
        for code, name in EMPLOYEE_PERMIT_TYPES:
            if code != 'OTHER':
                permit = EmployeePermit(
                    employee_id=emp.id,
                    permit_type=code,
                    applicability='YES' if code == 'PRIMEROS_AUXILIOS' else 'N/A'
                )
                db.session.add(permit)

        db.session.commit()
        flash(f'Empleado {emp.name} creado.', 'success')
        return redirect(url_for('employee_detail', id=emp.id))

    return render_template('employee_form.html', employee=None)


@app.route('/employee/<int:id>/edit', methods=['GET', 'POST'])
@manager_required
def employee_edit(id):
    emp = Employee.query.get_or_404(id)
    if request.method == 'POST':
        emp.name = request.form['name']
        emp.company = request.form['company']
        emp.area = request.form.get('area', '')
        emp.status = request.form.get('status', 'activo')
        emp.license_number = request.form.get('license_number', '')
        emp.puesto = request.form.get('puesto', '')
        emp.telefono = request.form.get('telefono', '')
        emp.email = request.form.get('email', '')
        emp.contacto_emergencia = request.form.get('contacto_emergencia', '')
        emp.shirt_size = request.form.get('shirt_size', '')
        emp.endoso_hazmat = request.form.get('endoso_hazmat', 'N/A')

        for field in ['fecha_nacimiento', 'license_expiration', 'fecha_contratacion']:
            val = request.form.get(field, '')
            if val:
                try:
                    setattr(emp, field, datetime.strptime(val, '%Y-%m-%d').date())
                except ValueError:
                    pass
            else:
                setattr(emp, field, None)

        if 'license_file' in request.files:
            file = request.files['license_file']
            if file and file.filename and allowed_file(file.filename):
                filename = save_uploaded_file(file)
                emp.license_file = filename

        db.session.commit()
        flash(f'Empleado {emp.name} actualizado.', 'success')
        return redirect(url_for('employee_detail', id=emp.id))

    return render_template('employee_form.html', employee=emp)


@app.route('/employee/<int:id>/delete', methods=['POST'])
@admin_required
def employee_delete(id):
    emp = Employee.query.get_or_404(id)
    name = emp.name
    # Explicitly delete child permits first: the relationship is lazy='dynamic'
    # and the FK has no ON DELETE CASCADE, so the ORM-level cascade doesn't
    # always issue child DELETEs before the parent, causing FK violations.
    EmployeePermit.query.filter_by(employee_id=emp.id).delete(synchronize_session=False)
    db.session.delete(emp)
    db.session.commit()
    flash(f'Empleado {name} eliminado.', 'warning')
    return redirect(url_for('dashboard'))


# ── EMPLOYEE PERMIT ROUTES ─────────────────────────────────────────────

@app.route('/employee/<int:emp_id>/permit/<int:permit_id>/edit', methods=['POST'])
@manager_required
def employee_permit_edit(emp_id, permit_id):
    permit = EmployeePermit.query.get_or_404(permit_id)
    if permit.employee_id != emp_id:
        abort(403)

    permit.applicability = request.form.get('applicability', 'YES')
    permit.permit_number = request.form.get('permit_number', '')
    permit.issuing_authority = request.form.get('issuing_authority', '')
    permit.notes = request.form.get('notes', '')

    val = request.form.get('expiration_date', '')
    if val:
        try:
            permit.expiration_date = datetime.strptime(val, '%Y-%m-%d').date()
        except ValueError:
            pass
    else:
        permit.expiration_date = None

    cost = request.form.get('renewal_cost', '')
    if cost:
        try:
            permit.renewal_cost = float(cost)
        except ValueError:
            pass

    if 'permit_file' in request.files:
        file = request.files['permit_file']
        if file and file.filename and allowed_file(file.filename):
            filename = save_uploaded_file(file)
            permit.file_path = filename

    db.session.commit()
    flash(f'Permiso {permit.display_name} actualizado.', 'success')
    return redirect(url_for('employee_detail', id=emp_id))


@app.route('/employee/<int:emp_id>/permit/<int:permit_id>/upload-form', methods=['POST'])
@manager_required
def employee_permit_upload_form(emp_id, permit_id):
    permit = EmployeePermit.query.get_or_404(permit_id)
    if permit.employee_id != emp_id:
        abort(403)

    file = request.files.get('form_file')
    if not file or not file.filename:
        flash('No se seleccionó ningún archivo.', 'error')
        return redirect(url_for('employee_detail', id=emp_id))
    if '.' not in file.filename or file.filename.rsplit('.', 1)[1].lower() != 'pdf':
        flash('Solo se permiten archivos PDF.', 'error')
        return redirect(url_for('employee_detail', id=emp_id))

    if permit.file_path:
        delete_stored_file(permit.file_path)

    filename = save_uploaded_file(file)
    permit.file_path = filename
    db.session.commit()
    flash(f'Formulario adjuntado a {permit.display_name}.', 'success')
    return redirect(url_for('employee_detail', id=emp_id))


@app.route('/employee/<int:emp_id>/permit/<int:permit_id>/delete-form', methods=['POST'])
@manager_required
def employee_permit_delete_form(emp_id, permit_id):
    permit = EmployeePermit.query.get_or_404(permit_id)
    if permit.employee_id != emp_id:
        abort(403)

    if permit.file_path:
        delete_stored_file(permit.file_path)
        permit.file_path = None
        db.session.commit()
        flash(f'Formulario eliminado de {permit.display_name}.', 'success')
    return redirect(url_for('employee_detail', id=emp_id))


@app.route('/employee/<int:emp_id>/permit/new', methods=['POST'])
@manager_required
def employee_permit_new(emp_id):
    emp = Employee.query.get_or_404(emp_id)
    permit = EmployeePermit(
        employee_id=emp.id,
        permit_type=request.form.get('permit_type', 'OTHER'),
        permit_name=request.form.get('permit_name', ''),
        applicability='YES',
    )
    val = request.form.get('expiration_date', '')
    if val:
        try:
            permit.expiration_date = datetime.strptime(val, '%Y-%m-%d').date()
        except ValueError:
            pass

    db.session.add(permit)
    db.session.commit()
    flash(f'Nuevo permiso agregado.', 'success')
    return redirect(url_for('employee_detail', id=emp_id))


# ── EQUIPMENT ROUTES ───────────────────────────────────────────────────

@app.route('/equipment/<int:id>')
@login_required
def equipment_detail(id):
    equip = Equipment.query.get_or_404(id)
    permits = equip.permits.order_by(EquipmentPermit.permit_type).all()
    return render_template('equipment.html', equipment=equip, permits=permits,
                           permit_types=EQUIPMENT_PERMIT_TYPES)


@app.route('/equipment/new', methods=['GET', 'POST'])
@manager_required
def equipment_new():
    if request.method == 'POST':
        equip = Equipment(
            company=request.form['company'],
            equipment_type=request.form.get('equipment_type', 'vehicle'),
            titular=request.form.get('titular', ''),
            unit_number=request.form.get('unit_number', ''),
            plate_number=request.form.get('plate_number', ''),
            make=request.form.get('make', ''),
            model=request.form.get('model', ''),
            vin_serial=request.form.get('vin_serial', ''),
            insurance_company=request.form.get('insurance_company', ''),
            notes=request.form.get('notes', ''),
            status=request.form.get('status', 'activo'),
        )
        yr = request.form.get('year', '')
        if yr:
            try:
                equip.year = int(yr)
            except ValueError:
                pass

        cost_raw = request.form.get('cost', '').strip()
        if cost_raw:
            try:
                equip.cost = Decimal(cost_raw)
            except (InvalidOperation, ValueError):
                pass

        db.session.add(equip)
        db.session.flush()

        # Create default permit slots
        for code, name in EQUIPMENT_PERMIT_TYPES:
            if code != 'OTHER':
                # Voucher does not apply to Personal equipment
                applicability = 'N/A' if code == 'VOUCHER' and equip.company == 'Personal' else 'YES'
                permit = EquipmentPermit(
                    equipment_id=equip.id,
                    permit_type=code,
                    applicability=applicability,
                )
                db.session.add(permit)

        db.session.commit()
        flash(f'Equipo {equip.display_name} creado.', 'success')
        return redirect(url_for('equipment_detail', id=equip.id))

    return render_template('equipment_form.html', equipment=None)


@app.route('/equipment/<int:id>/edit', methods=['GET', 'POST'])
@manager_required
def equipment_edit(id):
    equip = Equipment.query.get_or_404(id)
    if request.method == 'POST':
        equip.company = request.form['company']
        equip.equipment_type = request.form.get('equipment_type', 'vehicle')
        equip.titular = request.form.get('titular', '')
        equip.unit_number = request.form.get('unit_number', '')
        equip.plate_number = request.form.get('plate_number', '')
        equip.make = request.form.get('make', '')
        equip.model = request.form.get('model', '')
        equip.vin_serial = request.form.get('vin_serial', '')
        equip.insurance_company = request.form.get('insurance_company', '')
        equip.notes = request.form.get('notes', '')
        equip.status = request.form.get('status', 'activo')

        yr = request.form.get('year', '')
        if yr:
            try:
                equip.year = int(yr)
            except ValueError:
                pass

        cost_raw = request.form.get('cost', '').strip()
        if cost_raw:
            try:
                equip.cost = Decimal(cost_raw)
            except (InvalidOperation, ValueError):
                pass
        else:
            equip.cost = None

        db.session.commit()
        flash(f'Equipo {equip.display_name} actualizado.', 'success')
        return redirect(url_for('equipment_detail', id=equip.id))

    return render_template('equipment_form.html', equipment=equip)


@app.route('/equipment/<int:id>/delete', methods=['POST'])
@admin_required
def equipment_delete(id):
    equip = Equipment.query.get_or_404(id)
    name = equip.display_name
    # Explicitly delete child permits first (see employee_delete for rationale).
    EquipmentPermit.query.filter_by(equipment_id=equip.id).delete(synchronize_session=False)
    db.session.delete(equip)
    db.session.commit()
    flash(f'Equipo {name} eliminado.', 'warning')
    return redirect(url_for('dashboard', view='equipment'))


@app.route('/equipment/<int:eq_id>/permit/<int:permit_id>/edit', methods=['POST'])
@manager_required
def equipment_permit_edit(eq_id, permit_id):
    permit = EquipmentPermit.query.get_or_404(permit_id)
    if permit.equipment_id != eq_id:
        abort(403)

    permit.applicability = request.form.get('applicability', 'YES')
    permit.permit_number = request.form.get('permit_number', '')
    permit.issuing_authority = request.form.get('issuing_authority', '')
    permit.notes = request.form.get('notes', '')

    val = request.form.get('expiration_date', '')
    if val:
        try:
            permit.expiration_date = datetime.strptime(val, '%Y-%m-%d').date()
        except ValueError:
            pass
    else:
        permit.expiration_date = None

    cost = request.form.get('renewal_cost', '')
    if cost:
        try:
            permit.renewal_cost = float(cost)
        except ValueError:
            pass

    if 'permit_file' in request.files:
        file = request.files['permit_file']
        if file and file.filename and allowed_file(file.filename):
            filename = save_uploaded_file(file)
            permit.file_path = filename

    db.session.commit()
    flash(f'Permiso {permit.display_name} actualizado.', 'success')
    return redirect(url_for('equipment_detail', id=eq_id))


@app.route('/equipment/<int:eq_id>/permit/<int:permit_id>/upload-form', methods=['POST'])
@manager_required
def equipment_permit_upload_form(eq_id, permit_id):
    permit = EquipmentPermit.query.get_or_404(permit_id)
    if permit.equipment_id != eq_id:
        abort(403)

    file = request.files.get('form_file')
    if not file or not file.filename:
        flash('No se seleccionó ningún archivo.', 'error')
        return redirect(url_for('equipment_detail', id=eq_id))
    if '.' not in file.filename or file.filename.rsplit('.', 1)[1].lower() != 'pdf':
        flash('Solo se permiten archivos PDF.', 'error')
        return redirect(url_for('equipment_detail', id=eq_id))

    if permit.file_path:
        delete_stored_file(permit.file_path)

    filename = save_uploaded_file(file)
    permit.file_path = filename
    db.session.commit()
    flash(f'Formulario adjuntado a {permit.display_name}.', 'success')
    return redirect(url_for('equipment_detail', id=eq_id))


@app.route('/equipment/<int:eq_id>/permit/<int:permit_id>/delete-form', methods=['POST'])
@manager_required
def equipment_permit_delete_form(eq_id, permit_id):
    permit = EquipmentPermit.query.get_or_404(permit_id)
    if permit.equipment_id != eq_id:
        abort(403)

    if permit.file_path:
        delete_stored_file(permit.file_path)
        permit.file_path = None
        db.session.commit()
        flash(f'Formulario eliminado de {permit.display_name}.', 'success')
    return redirect(url_for('equipment_detail', id=eq_id))


@app.route('/equipment/<int:eq_id>/permit/new', methods=['POST'])
@manager_required
def equipment_permit_new(eq_id):
    equip = Equipment.query.get_or_404(eq_id)
    permit = EquipmentPermit(
        equipment_id=equip.id,
        permit_type=request.form.get('permit_type', 'OTHER'),
        permit_name=request.form.get('permit_name', ''),
        applicability='YES',
    )
    val = request.form.get('expiration_date', '')
    if val:
        try:
            permit.expiration_date = datetime.strptime(val, '%Y-%m-%d').date()
        except ValueError:
            pass
    db.session.add(permit)
    db.session.commit()
    flash(f'Nuevo permiso agregado.', 'success')
    return redirect(url_for('equipment_detail', id=eq_id))


# ── COMPANY PERMIT ROUTES ─────────────────────────────────────────────

@app.route('/company/<company>/permit/<int:permit_id>/edit', methods=['POST'])
@manager_required
def company_permit_edit(company, permit_id):
    permit = CompanyPermit.query.get_or_404(permit_id)
    if permit.company != company:
        abort(403)

    permit.applicability = request.form.get('applicability', 'YES')
    permit.permit_number = request.form.get('permit_number', '')
    permit.issuing_authority = request.form.get('issuing_authority', '')
    permit.notes = request.form.get('notes', '')

    val = request.form.get('expiration_date', '')
    if val:
        try:
            permit.expiration_date = datetime.strptime(val, '%Y-%m-%d').date()
        except ValueError:
            pass
    else:
        permit.expiration_date = None

    cost_val = request.form.get('renewal_cost', '')
    if cost_val:
        try:
            permit.renewal_cost = float(cost_val)
        except (ValueError, TypeError):
            pass
    else:
        permit.renewal_cost = None

    file = request.files.get('permit_file')
    if file and file.filename:
        if permit.file_path:
            delete_stored_file(permit.file_path)
        permit.file_path = save_uploaded_file(file)

    db.session.commit()
    flash(f'{permit.display_name} actualizado.', 'success')
    return redirect(url_for('dashboard', view='company'))


@app.route('/company/<company>/permit/<int:permit_id>/upload-form', methods=['POST'])
@manager_required
def company_permit_upload_form(company, permit_id):
    permit = CompanyPermit.query.get_or_404(permit_id)
    if permit.company != company:
        abort(403)

    file = request.files.get('form_file')
    if not file or not file.filename:
        flash('No se seleccionó archivo.', 'warning')
        return redirect(url_for('dashboard', view='company'))

    if not file.filename.lower().endswith('.pdf'):
        flash('Solo se permiten archivos PDF.', 'warning')
        return redirect(url_for('dashboard', view='company'))

    if permit.file_path:
        delete_stored_file(permit.file_path)

    filename = save_uploaded_file(file)
    permit.file_path = filename
    db.session.commit()
    flash(f'Formulario adjuntado a {permit.display_name}.', 'success')
    return redirect(url_for('dashboard', view='company'))


@app.route('/company/<company>/permit/<int:permit_id>/delete-form', methods=['POST'])
@manager_required
def company_permit_delete_form(company, permit_id):
    permit = CompanyPermit.query.get_or_404(permit_id)
    if permit.company != company:
        abort(403)

    if permit.file_path:
        delete_stored_file(permit.file_path)
        permit.file_path = None
        db.session.commit()
        flash(f'Formulario eliminado de {permit.display_name}.', 'success')
    return redirect(url_for('dashboard', view='company'))


# ── FILE SERVING ───────────────────────────────────────────────────────

@app.route('/uploads/<filename>')
@login_required
def uploaded_file(filename):
    stored = FileStorage.query.filter_by(filename=filename).first_or_404()
    return send_file(
        BytesIO(stored.data),
        mimetype=stored.mime_type,
        as_attachment=False,
        download_name=stored.original_filename or filename,
    )


# ── DEBUG: Duplicate Check & Fix (temporary) ──────────────────────────

@app.route('/debug/duplicates')
@login_required
def debug_duplicates():
    dupes = db.session.query(
        Employee.name, Employee.company, db.func.count(Employee.id)
    ).group_by(Employee.name, Employee.company).having(db.func.count(Employee.id) > 1).all()
    return {'duplicates': [{'name': d[0], 'company': d[1], 'count': d[2]} for d in dupes],
            'total_duplicate_groups': len(dupes)}


@app.route('/debug/dedup', methods=['POST'])
@admin_required
def run_dedup():
    """Web-accessible dedup for environments without CLI access."""
    emp_deleted = 0
    permit_deleted = 0
    eq_permit_deleted = 0

    # --- Deduplicate employees by (name, company) ---
    dupes = (
        db.session.query(Employee.name, Employee.company, db.func.count(Employee.id))
        .group_by(Employee.name, Employee.company)
        .having(db.func.count(Employee.id) > 1)
        .all()
    )
    for name, company, count in dupes:
        employees = Employee.query.filter_by(name=name, company=company).order_by(Employee.id).all()
        keeper = employees[0]
        for dup in employees[1:]:
            for col in ['area', 'status', 'fecha_nacimiento', 'license_number',
                        'license_expiration', 'license_file', 'puesto', 'telefono',
                        'email', 'fecha_contratacion', 'contacto_emergencia', 'shirt_size']:
                if getattr(keeper, col) is None and getattr(dup, col) is not None:
                    setattr(keeper, col, getattr(dup, col))
            if keeper.endoso_hazmat == 'N/A' and dup.endoso_hazmat != 'N/A':
                keeper.endoso_hazmat = dup.endoso_hazmat
            for dup_permit in dup.permits.all():
                keeper_permit = keeper.permits.filter_by(permit_type=dup_permit.permit_type).first()
                if keeper_permit:
                    if keeper_permit.expiration_date is None and dup_permit.expiration_date is not None:
                        keeper_permit.expiration_date = dup_permit.expiration_date
                    if keeper_permit.applicability == 'N/A' and dup_permit.applicability == 'YES':
                        keeper_permit.applicability = dup_permit.applicability
                    for field in ['permit_number', 'issuing_authority', 'file_path', 'renewal_cost', 'notes']:
                        if getattr(keeper_permit, field) is None and getattr(dup_permit, field) is not None:
                            setattr(keeper_permit, field, getattr(dup_permit, field))
            db.session.delete(dup)
            emp_deleted += 1

    # --- Deduplicate employee permits ---
    permit_dupes = (
        db.session.query(EmployeePermit.employee_id, EmployeePermit.permit_type, db.func.count(EmployeePermit.id))
        .filter(EmployeePermit.permit_type != 'OTHER')
        .group_by(EmployeePermit.employee_id, EmployeePermit.permit_type)
        .having(db.func.count(EmployeePermit.id) > 1)
        .all()
    )
    for emp_id, ptype, count in permit_dupes:
        permits = EmployeePermit.query.filter_by(employee_id=emp_id, permit_type=ptype).order_by(EmployeePermit.id).all()
        keeper = permits[0]
        for dup in permits[1:]:
            if keeper.expiration_date is None and dup.expiration_date is not None:
                keeper.expiration_date = dup.expiration_date
            if keeper.applicability == 'N/A' and dup.applicability == 'YES':
                keeper.applicability = dup.applicability
            for field in ['permit_number', 'issuing_authority', 'file_path', 'renewal_cost', 'notes']:
                if getattr(keeper, field) is None and getattr(dup, field) is not None:
                    setattr(keeper, field, getattr(dup, field))
            db.session.delete(dup)
            permit_deleted += 1

    # --- Deduplicate equipment permits ---
    eq_permit_dupes = (
        db.session.query(EquipmentPermit.equipment_id, EquipmentPermit.permit_type, db.func.count(EquipmentPermit.id))
        .filter(EquipmentPermit.permit_type != 'OTHER')
        .group_by(EquipmentPermit.equipment_id, EquipmentPermit.permit_type)
        .having(db.func.count(EquipmentPermit.id) > 1)
        .all()
    )
    for eq_id, ptype, count in eq_permit_dupes:
        permits = EquipmentPermit.query.filter_by(equipment_id=eq_id, permit_type=ptype).order_by(EquipmentPermit.id).all()
        keeper = permits[0]
        for dup in permits[1:]:
            if keeper.expiration_date is None and dup.expiration_date is not None:
                keeper.expiration_date = dup.expiration_date
            if keeper.applicability == 'N/A' and dup.applicability == 'YES':
                keeper.applicability = dup.applicability
            for field in ['permit_number', 'issuing_authority', 'file_path', 'renewal_cost', 'notes']:
                if getattr(keeper, field) is None and getattr(dup, field) is not None:
                    setattr(keeper, field, getattr(dup, field))
            db.session.delete(dup)
            eq_permit_deleted += 1

    db.session.commit()

    # Apply unique constraints
    if db.engine.dialect.name == 'postgresql':
        for sql in [
            "ALTER TABLE employees ADD CONSTRAINT uq_employee_name_company UNIQUE (name, company)",
            "ALTER TABLE employee_permits ADD CONSTRAINT uq_employee_permit_type UNIQUE (employee_id, permit_type)",
            "ALTER TABLE equipment_permits ADD CONSTRAINT uq_equipment_permit_type UNIQUE (equipment_id, permit_type)",
        ]:
            try:
                db.session.execute(db.text(sql))
                db.session.commit()
            except Exception:
                db.session.rollback()

    return {
        'status': 'ok',
        'employees_deleted': emp_deleted,
        'employee_permits_deleted': permit_deleted,
        'equipment_permits_deleted': eq_permit_deleted,
    }


# ── EXCEL IMPORT ───────────────────────────────────────────────────────

@app.route('/import', methods=['GET', 'POST'])
@admin_required
def import_data():
    if request.method == 'POST':
        # Validate idempotency token to prevent double-submission
        token = request.form.get('import_token', '')
        if token != session.pop('import_token', None):
            flash('Formulario ya fue procesado. Por favor intente nuevamente.', 'warning')
            return redirect(url_for('import_data'))

        if 'file' not in request.files:
            flash('No se seleccionó archivo.', 'danger')
            return redirect(url_for('import_data'))

        file = request.files['file']
        if not file.filename.endswith(('.xlsx', '.xls')):
            flash('Solo se aceptan archivos Excel (.xlsx, .xls).', 'danger')
            return redirect(url_for('import_data'))

        try:
            from openpyxl import load_workbook
            from sqlalchemy.exc import IntegrityError
            import unicodedata
            import tempfile
            fd, filepath = tempfile.mkstemp(suffix='.xlsx')
            os.close(fd)
            file.save(filepath)
            wb = load_workbook(filepath, read_only=True)

            # Acquire advisory lock to prevent concurrent imports (PostgreSQL only)
            if db.engine.dialect.name == 'postgresql':
                db.session.execute(db.text("SELECT pg_advisory_xact_lock(42)"))

            # Find the data sheet
            ws = None
            for name in wb.sheetnames:
                if 'registro' in name.lower() or 'documento' in name.lower():
                    ws = wb[name]
                    break
            if ws is None:
                ws = wb[wb.sheetnames[0]]

            imported = 0
            skipped = 0
            updated = 0
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                flash('El archivo está vacío.', 'warning')
                return redirect(url_for('import_data'))

            headers = [str(h).strip().upper() if h else '' for h in rows[0]]

            # Reject files that don't look like an employee sheet (e.g. an
            # equipment template uploaded by mistake creates ghost PLI rows).
            def _norm(s):
                return unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode()
            normalized_headers = [_norm(h) for h in headers]
            joined_headers = ' '.join(normalized_headers)
            first_header = normalized_headers[0] if normalized_headers else ''
            equipment_markers = ('TITULAR', 'UNIDAD', 'VIN', 'MARBETE', 'TABLILLA')
            looks_like_employee = ('NOMBRE' in first_header) or ('NAME' in first_header)
            looks_like_equipment = any(m in joined_headers for m in equipment_markers)
            if not looks_like_employee or looks_like_equipment:
                wb.close()
                os.remove(filepath)
                flash('El archivo no parece ser de empleados. Verifique que está usando la plantilla correcta.', 'danger')
                return redirect(url_for('import_data'))

            for row in rows[1:]:
                if not row[0]:
                    continue

                name = str(row[0]).strip()

                # Check if employee already exists
                existing = Employee.query.filter_by(name=name).first()
                if existing:
                    # Update permits for existing employee
                    permit_map = {
                        7: 'NTSP',
                        8: 'TWIC',
                        9: 'CERT_MEDICO',
                        10: 'ANTECEDENTES',
                        11: 'RECORD_CHOFERIL',
                        13: 'HM126',
                        14: 'HM232',
                        15: 'PRIMEROS_AUXILIOS',
                    }
                    for col_idx, ptype in permit_map.items():
                        if col_idx >= len(row):
                            continue
                        val = row[col_idx]
                        applicability = 'YES'
                        exp_date = None

                        if val is None:
                            applicability = 'YES'
                            exp_date = None
                        elif isinstance(val, str):
                            val_upper = val.strip().upper()
                            if val_upper in ('N/A', 'NA', ''):
                                applicability = 'N/A'
                            elif val_upper in ('NO',):
                                applicability = 'N/A'
                        elif hasattr(val, 'date'):
                            d = val.date() if hasattr(val, 'date') else val
                            if hasattr(d, 'year') and d.year < 2000:
                                try:
                                    d = d.replace(year=d.year + 100)
                                except ValueError:
                                    pass
                            exp_date = d

                        permit = EmployeePermit.query.filter_by(
                            employee_id=existing.id, permit_type=ptype
                        ).first()
                        if permit:
                            permit.applicability = applicability
                            permit.expiration_date = exp_date
                        else:
                            db.session.add(EmployeePermit(
                                employee_id=existing.id,
                                permit_type=ptype,
                                applicability=applicability,
                                expiration_date=exp_date,
                            ))
                    updated += 1
                    continue

                # Map company
                company_raw = str(row[1]).strip().upper() if row[1] else ''
                company = 'LB' if 'LB' in company_raw else 'PLI'

                area = str(row[2]).strip() if row[2] else ''
                status = str(row[3]).strip() if row[3] else 'activo'

                # License number
                lic_num = str(int(row[4])) if row[4] and isinstance(row[4], (int, float)) else (str(row[4]).strip() if row[4] else '')

                # License expiration - fix 2-digit year issue
                lic_exp = None
                if row[5] and hasattr(row[5], 'date'):
                    d = row[5].date() if hasattr(row[5], 'date') else row[5]
                    # Fix dates with year < 2000 (Excel 2-digit year bug)
                    if hasattr(d, 'year') and d.year < 2000:
                        try:
                            d = d.replace(year=d.year + 100)
                        except ValueError:
                            pass
                    lic_exp = d

                # Shirt size (column 12, index 12)
                shirt = str(row[12]).strip() if len(row) > 12 and row[12] else ''

                # Endoso HAZMAT (column 6)
                hazmat_raw = str(row[6]).strip().upper() if row[6] else 'N/A'
                if hazmat_raw in ('SI', 'SÍ', 'YES'):
                    hazmat = 'SI'
                elif hazmat_raw in ('NO',):
                    hazmat = 'NO'
                else:
                    hazmat = 'N/A'

                emp = Employee(
                    name=name,
                    company=company,
                    area=area,
                    status=status,
                    license_number=lic_num,
                    license_expiration=lic_exp,
                    shirt_size=shirt,
                    endoso_hazmat=hazmat,
                )
                db.session.add(emp)
                db.session.flush()

                # Create permits from columns
                permit_map = {
                    7: 'NTSP',
                    8: 'TWIC',
                    9: 'CERT_MEDICO',
                    10: 'ANTECEDENTES',
                    11: 'RECORD_CHOFERIL',
                    13: 'HM126',
                    14: 'HM232',
                    15: 'PRIMEROS_AUXILIOS',
                }

                for col_idx, ptype in permit_map.items():
                    if col_idx >= len(row):
                        continue
                    val = row[col_idx]
                    applicability = 'YES'
                    exp_date = None

                    if val is None:
                        applicability = 'YES'  # Applicable but missing
                        exp_date = None
                    elif isinstance(val, str):
                        val_upper = val.strip().upper()
                        if val_upper in ('N/A', 'NA', ''):
                            applicability = 'N/A'
                        elif val_upper in ('NO',):
                            applicability = 'N/A'
                    elif hasattr(val, 'date'):
                        d = val.date() if hasattr(val, 'date') else val
                        if hasattr(d, 'year') and d.year < 2000:
                            try:
                                d = d.replace(year=d.year + 100)
                            except ValueError:
                                pass
                        exp_date = d

                    permit = EmployeePermit(
                        employee_id=emp.id,
                        permit_type=ptype,
                        applicability=applicability,
                        expiration_date=exp_date,
                    )
                    db.session.add(permit)

                imported += 1

            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash('Error: datos duplicados detectados. Posible importación simultánea.', 'danger')
                os.remove(filepath)
                return redirect(url_for('import_data'))
            os.remove(filepath)
            flash(f'Importación completa: {imported} nuevos, {updated} actualizados, {skipped} omitidos.', 'success')

        except Exception as e:
            db.session.rollback()
            flash(f'Error en la importación: {str(e)}', 'danger')

        return redirect(url_for('import_data'))

    session['import_token'] = secrets.token_hex(16)
    return render_template('import.html', import_token=session['import_token'])


@app.route('/import/equipment', methods=['POST'])
@admin_required
def import_equipment():
    import unicodedata

    def to_date(cell):
        """Coerce an Excel cell to a date or None."""
        if cell is None or cell == '':
            return None
        if isinstance(cell, datetime):
            return cell.date()
        if isinstance(cell, date):
            return cell
        if isinstance(cell, str):
            s = cell.strip()
            if not s:
                return None
            for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y'):
                try:
                    return datetime.strptime(s, fmt).date()
                except ValueError:
                    continue
        return None

    def to_float(cell):
        if cell is None or cell == '':
            return None
        try:
            return float(cell)
        except (ValueError, TypeError):
            return None

    # Validate idempotency token
    token = request.form.get('import_token', '')
    if token != session.pop('import_token', None):
        flash('Formulario ya fue procesado. Por favor intente nuevamente.', 'warning')
        return redirect(url_for('import_data'))

    if 'file' not in request.files:
        flash('No se seleccionó archivo.', 'danger')
        return redirect(url_for('import_data'))

    file = request.files['file']
    if not file.filename.endswith(('.xlsx', '.xls')):
        flash('Solo se aceptan archivos Excel (.xlsx, .xls).', 'danger')
        return redirect(url_for('import_data'))

    try:
        from openpyxl import load_workbook
        from sqlalchemy.exc import IntegrityError

        import tempfile
        fd, filepath = tempfile.mkstemp(suffix='.xlsx')
        os.close(fd)
        file.save(filepath)
        wb = load_workbook(filepath, read_only=True)

        # Advisory lock for PostgreSQL
        if db.engine.dialect.name == 'postgresql':
            db.session.execute(db.text("SELECT pg_advisory_xact_lock(43)"))

        # Find data sheet
        ws = None
        for name in wb.sheetnames:
            if 'equipo' in name.lower() or 'vehicle' in name.lower():
                ws = wb[name]
                break
        if ws is None:
            ws = wb[wb.sheetnames[0]]

        imported = 0
        skipped = 0
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            flash('El archivo está vacío.', 'warning')
            return redirect(url_for('import_data'))

        # Reject files that don't look like an equipment sheet (mirrors the
        # guard in import_data() so neither endpoint accepts the wrong template).
        headers = [str(h).strip().upper() if h else '' for h in rows[0]]
        normalized_headers = [
            unicodedata.normalize('NFKD', h).encode('ascii', 'ignore').decode()
            for h in headers
        ]
        joined_headers = ' '.join(normalized_headers)
        employee_markers = ('TWIC', 'HAZMAT', 'CERT MEDICO', 'ANTECEDENTES')
        looks_like_equipment = any(
            m in joined_headers for m in ('TITULO', 'UNIDAD', 'VIN', 'MARBETE', 'TABLILLA', 'COMPANIA')
        )
        looks_like_employee = any(m in joined_headers for m in employee_markers)
        if not looks_like_equipment or looks_like_employee:
            wb.close()
            os.remove(filepath)
            flash('El archivo no parece ser de equipos. Verifique que está usando la plantilla correcta.', 'danger')
            return redirect(url_for('import_data'))

        for row in rows[1:]:
            if not row or not any(row):
                continue

            # Column mapping (Registro de Equipos.xlsx):
            # 0 Titulo | 1 Unidad | 2 Compañia | 3 Modelo | 4 Año | 5 VIN |
            # 6 Tablilla | 7 Seguro | 8 Marbete | 9 Inspección | 10 Voucher |
            # 11 NTSP | 12 Costo
            titulo = str(row[0]).strip() if row[0] is not None else ''
            unit_number = str(row[1]).strip() if len(row) > 1 and row[1] is not None else ''
            # Numeric unit numbers come back as int/float — strip the trailing .0
            if unit_number and unit_number.replace('.', '').isdigit():
                unit_number = str(int(float(unit_number)))

            company_raw = str(row[2]).strip().upper() if len(row) > 2 and row[2] is not None else ''
            if 'PERSONAL' in company_raw:
                company = 'Personal'
            elif 'PLI' in company_raw:
                company = 'PLI'
            else:
                company = 'LB'

            model = str(row[3]).strip() if len(row) > 3 and row[3] is not None else ''

            year = None
            if len(row) > 4 and row[4] is not None:
                try:
                    year = int(row[4])
                except (ValueError, TypeError):
                    pass

            vin_serial = str(row[5]).strip() if len(row) > 5 and row[5] is not None else ''
            if vin_serial and vin_serial.replace('.', '').replace(',', '').isdigit():
                vin_serial = str(int(float(vin_serial)))

            plate_number = str(row[6]).strip() if len(row) > 6 and row[6] is not None else ''
            if plate_number and plate_number.replace('.', '').isdigit():
                plate_number = str(int(float(plate_number)))

            # Skip rows with no identifying data (empty/junk rows from Excel)
            if not titulo and not unit_number and not vin_serial and not plate_number:
                continue

            insurance_company = str(row[7]).strip() if len(row) > 7 and row[7] is not None else ''

            marbete_date    = to_date(row[8])  if len(row) > 8  else None
            inspeccion_date = to_date(row[9])  if len(row) > 9  else None
            voucher_date    = to_date(row[10]) if len(row) > 10 else None
            ntsp_date       = to_date(row[11]) if len(row) > 11 else None
            cost            = to_float(row[12]) if len(row) > 12 else None

            permit_dates = {
                'MARBETE':    marbete_date,
                'INSPECCION': inspeccion_date,
                'VOUCHER':    voucher_date,
                'NTSP':       ntsp_date,
            }

            # Look up existing equipment by VIN or plate; upsert in place so
            # corrections to the source spreadsheet propagate on re-import.
            existing = None
            if vin_serial:
                existing = Equipment.query.filter_by(vin_serial=vin_serial).first()
            if not existing and plate_number:
                existing = Equipment.query.filter_by(plate_number=plate_number).first()

            if existing:
                if titulo:            existing.titular = titulo
                if insurance_company: existing.insurance_company = insurance_company
                if cost is not None:  existing.cost = cost
                if year is not None:  existing.year = year
                if model:             existing.model = model

                existing_permits = {p.permit_type: p for p in existing.permits}
                for ptype, pdate in permit_dates.items():
                    applicability = 'N/A' if (ptype == 'VOUCHER' and company == 'Personal') else 'YES'
                    if ptype in existing_permits:
                        permit = existing_permits[ptype]
                        permit.applicability = applicability
                        if pdate is not None:
                            permit.expiration_date = pdate
                    else:
                        db.session.add(EquipmentPermit(
                            equipment_id=existing.id,
                            permit_type=ptype,
                            applicability=applicability,
                            expiration_date=pdate if applicability == 'YES' else None,
                        ))
                skipped += 1
                continue

            equip = Equipment(
                company=company,
                titular=titulo,
                unit_number=unit_number,
                model=model,
                year=year,
                vin_serial=vin_serial,
                plate_number=plate_number,
                insurance_company=insurance_company,
                cost=cost,
                status='activo',
            )
            db.session.add(equip)
            db.session.flush()

            for ptype, pdate in permit_dates.items():
                applicability = 'N/A' if (ptype == 'VOUCHER' and company == 'Personal') else 'YES'
                db.session.add(EquipmentPermit(
                    equipment_id=equip.id,
                    permit_type=ptype,
                    applicability=applicability,
                    expiration_date=pdate if applicability == 'YES' else None,
                ))

            imported += 1

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash('Error: datos duplicados detectados. Posible importación simultánea.', 'danger')
            os.remove(filepath)
            return redirect(url_for('import_data'))

        os.remove(filepath)
        flash(f'Importación completa: {imported} equipos importados, {skipped} actualizados.', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Error en la importación: {str(e)}', 'danger')

    return redirect(url_for('import_data'))


# ── PDF REPORT ─────────────────────────────────────────────────────────

@app.route('/report/pdf')
@login_required
def generate_pdf():
    report_type = request.args.get('type', 'all')  # all, employees, equipment
    company_filter = request.args.get('company', '')

    employees = []
    equipment_list = []

    if report_type in ('all', 'employees'):
        q = Employee.query.order_by(Employee.company, Employee.name)
        if company_filter:
            q = q.filter(Employee.company == company_filter)
        employees = q.all()

    if report_type in ('all', 'equipment'):
        q = Equipment.query.order_by(Equipment.company, Equipment.make)
        if company_filter:
            q = q.filter(Equipment.company == company_filter)
        equipment_list = q.all()

    html = render_template('report_pdf.html',
        employees=employees,
        equipment_list=equipment_list,
        report_type=report_type,
        today=date.today(),
        alert_date=date.today() + timedelta(days=30),
    )

    try:
        from weasyprint import HTML
        pdf = HTML(string=html, base_url=request.url_root).write_pdf()
        from io import BytesIO
        buffer = BytesIO(pdf)
        buffer.seek(0)
        fname = f"reporte_permisos_{date.today().strftime('%Y%m%d')}.pdf"
        return send_file(buffer, mimetype='application/pdf', as_attachment=True, download_name=fname)
    except ImportError:
        # Fallback: return HTML for printing
        return html


# ── USER MANAGEMENT ────────────────────────────────────────────────────

@app.route('/admin/users')
@admin_required
def admin_users():
    users = User.query.order_by(User.role, User.username).all()
    return render_template('admin_users.html', users=users)


@app.route('/admin/users/new', methods=['POST'])
@admin_required
def admin_user_new():
    username = request.form['username'].strip()
    email = request.form['email'].strip()
    password = request.form['password']
    role = request.form.get('role', 'viewer')

    if User.query.filter_by(username=username).first():
        flash(f'El usuario {username} ya existe.', 'danger')
        return redirect(url_for('admin_users'))

    user = User(username=username, email=email, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    flash(f'Usuario {username} creado como {role}.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:id>/edit', methods=['POST'])
@admin_required
def admin_user_edit(id):
    user = User.query.get_or_404(id)
    user.email = request.form.get('email', user.email)
    user.role = request.form.get('role', user.role)
    password = request.form.get('password', '')
    if password:
        user.set_password(password)
    db.session.commit()
    flash(f'Usuario {user.username} actualizado.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/users/<int:id>/delete', methods=['POST'])
@admin_required
def admin_user_delete(id):
    user = User.query.get_or_404(id)
    if user.id == current_user.id:
        flash('No puede eliminarse a sí mismo.', 'danger')
        return redirect(url_for('admin_users'))
    db.session.delete(user)
    db.session.commit()
    flash(f'Usuario eliminado.', 'warning')
    return redirect(url_for('admin_users'))


# ── ALERTS API ─────────────────────────────────────────────────────────

@app.route('/api/alerts')
@login_required
def api_alerts():
    today = date.today()
    alert_date = today + timedelta(days=30)
    alerts = []

    # Employee license alerts
    for emp in Employee.query.filter(Employee.license_expiration != None).all():
        if emp.license_expiration < today:
            alerts.append({
                'type': 'expired', 'entity': 'employee', 'id': emp.id,
                'name': emp.name, 'company': emp.company,
                'permit': 'Licencia de Conducir', 'date': emp.license_expiration.isoformat()
            })
        elif emp.license_expiration <= alert_date:
            alerts.append({
                'type': 'expiring', 'entity': 'employee', 'id': emp.id,
                'name': emp.name, 'company': emp.company,
                'permit': 'Licencia de Conducir', 'date': emp.license_expiration.isoformat()
            })

    # Employee permit alerts
    for permit in EmployeePermit.query.filter(
        EmployeePermit.applicability != 'N/A',
        EmployeePermit.expiration_date != None
    ).all():
        emp = permit.employee
        if permit.expiration_date < today:
            alerts.append({
                'type': 'expired', 'entity': 'employee', 'id': emp.id,
                'name': emp.name, 'company': emp.company,
                'permit': permit.display_name, 'date': permit.expiration_date.isoformat()
            })
        elif permit.expiration_date <= alert_date:
            alerts.append({
                'type': 'expiring', 'entity': 'employee', 'id': emp.id,
                'name': emp.name, 'company': emp.company,
                'permit': permit.display_name, 'date': permit.expiration_date.isoformat()
            })

    # Equipment permit alerts
    for permit in EquipmentPermit.query.filter(
        EquipmentPermit.applicability != 'N/A',
        EquipmentPermit.expiration_date != None
    ).all():
        eq = permit.equipment
        if permit.expiration_date < today:
            alerts.append({
                'type': 'expired', 'entity': 'equipment', 'id': eq.id,
                'name': eq.display_name, 'company': eq.company,
                'permit': permit.display_name, 'date': permit.expiration_date.isoformat()
            })
        elif permit.expiration_date <= alert_date:
            alerts.append({
                'type': 'expiring', 'entity': 'equipment', 'id': eq.id,
                'name': eq.display_name, 'company': eq.company,
                'permit': permit.display_name, 'date': permit.expiration_date.isoformat()
            })

    # Company permit alerts
    company_names = {'LB': 'LB Caribe Services', 'PLI': 'Professional Logistics'}
    for permit in CompanyPermit.query.filter(
        CompanyPermit.applicability != 'N/A',
        CompanyPermit.expiration_date != None
    ).all():
        if permit.expiration_date < today:
            alerts.append({
                'type': 'expired', 'entity': 'company', 'id': 0,
                'name': company_names.get(permit.company, permit.company),
                'company': permit.company,
                'permit': permit.display_name, 'date': permit.expiration_date.isoformat()
            })
        elif permit.expiration_date <= alert_date:
            alerts.append({
                'type': 'expiring', 'entity': 'company', 'id': 0,
                'name': company_names.get(permit.company, permit.company),
                'company': permit.company,
                'permit': permit.display_name, 'date': permit.expiration_date.isoformat()
            })

    alerts.sort(key=lambda a: a['date'])
    return jsonify(alerts)


# ── HEALTH CHECK ──────────────────────────────────────────────────────

@app.route('/health')
def health():
    try:
        db.session.execute(db.text('SELECT 1'))
        return jsonify({'status': 'ok', 'database': 'ok'})
    except Exception as e:
        app.logger.error(f"Health check DB failure: {e}")
        return jsonify({'status': 'degraded', 'database': str(e)}), 503


# ── DB INIT CLI ────────────────────────────────────────────────────────

@app.cli.command('init-db')
def init_db():
    """Create tables and default admin user."""
    db.create_all()
    if not User.query.filter_by(username='admin').first():
        admin = User(username='admin', email='admin@lbcaribe.com', role='admin')
        admin.set_password('admin123')
        db.session.add(admin)
        db.session.commit()
        print('Database initialized. Admin user created (admin / admin123)')
    else:
        print('Database already initialized.')


@app.cli.command('dedup')
@click.option('--dry-run', is_flag=True, help='Preview changes without modifying the database.')
def dedup_data(dry_run):
    """Remove duplicate employees and permits, keeping the record with the most data."""
    # --- Deduplicate employees by (name, company) ---
    dupes = (
        db.session.query(Employee.name, Employee.company, db.func.count(Employee.id))
        .group_by(Employee.name, Employee.company)
        .having(db.func.count(Employee.id) > 1)
        .all()
    )

    emp_deleted = 0
    for name, company, count in dupes:
        employees = Employee.query.filter_by(name=name, company=company).order_by(Employee.id).all()
        keeper = employees[0]
        for dup in employees[1:]:
            # Merge non-null fields from duplicate into keeper
            for col in ['area', 'status', 'fecha_nacimiento', 'license_number',
                        'license_expiration', 'license_file', 'puesto', 'telefono',
                        'email', 'fecha_contratacion', 'contacto_emergencia', 'shirt_size']:
                if getattr(keeper, col) is None and getattr(dup, col) is not None:
                    setattr(keeper, col, getattr(dup, col))
            if keeper.endoso_hazmat == 'N/A' and dup.endoso_hazmat != 'N/A':
                keeper.endoso_hazmat = dup.endoso_hazmat

            # Merge permit data: if keeper's permit is missing data that dup's has, copy it
            for dup_permit in dup.permits.all():
                keeper_permit = keeper.permits.filter_by(permit_type=dup_permit.permit_type).first()
                if keeper_permit:
                    if keeper_permit.expiration_date is None and dup_permit.expiration_date is not None:
                        keeper_permit.expiration_date = dup_permit.expiration_date
                    if keeper_permit.applicability == 'N/A' and dup_permit.applicability == 'YES':
                        keeper_permit.applicability = dup_permit.applicability
                    for field in ['permit_number', 'issuing_authority', 'file_path', 'renewal_cost', 'notes']:
                        if getattr(keeper_permit, field) is None and getattr(dup_permit, field) is not None:
                            setattr(keeper_permit, field, getattr(dup_permit, field))

            print(f'  {"[DRY RUN] " if dry_run else ""}Delete duplicate employee: {dup.name} ({dup.company}) id={dup.id}, keeping id={keeper.id}')
            if not dry_run:
                db.session.delete(dup)
            emp_deleted += 1

    # --- Deduplicate permits within each employee by (employee_id, permit_type) ---
    permit_dupes = (
        db.session.query(EmployeePermit.employee_id, EmployeePermit.permit_type, db.func.count(EmployeePermit.id))
        .filter(EmployeePermit.permit_type != 'OTHER')
        .group_by(EmployeePermit.employee_id, EmployeePermit.permit_type)
        .having(db.func.count(EmployeePermit.id) > 1)
        .all()
    )

    permit_deleted = 0
    for emp_id, ptype, count in permit_dupes:
        permits = EmployeePermit.query.filter_by(employee_id=emp_id, permit_type=ptype).order_by(EmployeePermit.id).all()
        keeper = permits[0]
        for dup in permits[1:]:
            # Merge non-null fields
            if keeper.expiration_date is None and dup.expiration_date is not None:
                keeper.expiration_date = dup.expiration_date
            if keeper.applicability == 'N/A' and dup.applicability == 'YES':
                keeper.applicability = dup.applicability
            for field in ['permit_number', 'issuing_authority', 'file_path', 'renewal_cost', 'notes']:
                if getattr(keeper, field) is None and getattr(dup, field) is not None:
                    setattr(keeper, field, getattr(dup, field))
            print(f'  {"[DRY RUN] " if dry_run else ""}Delete duplicate permit: employee_id={emp_id} type={ptype} id={dup.id}, keeping id={keeper.id}')
            if not dry_run:
                db.session.delete(dup)
            permit_deleted += 1

    # --- Same for equipment permits ---
    eq_permit_dupes = (
        db.session.query(EquipmentPermit.equipment_id, EquipmentPermit.permit_type, db.func.count(EquipmentPermit.id))
        .filter(EquipmentPermit.permit_type != 'OTHER')
        .group_by(EquipmentPermit.equipment_id, EquipmentPermit.permit_type)
        .having(db.func.count(EquipmentPermit.id) > 1)
        .all()
    )

    eq_permit_deleted = 0
    for eq_id, ptype, count in eq_permit_dupes:
        permits = EquipmentPermit.query.filter_by(equipment_id=eq_id, permit_type=ptype).order_by(EquipmentPermit.id).all()
        keeper = permits[0]
        for dup in permits[1:]:
            if keeper.expiration_date is None and dup.expiration_date is not None:
                keeper.expiration_date = dup.expiration_date
            if keeper.applicability == 'N/A' and dup.applicability == 'YES':
                keeper.applicability = dup.applicability
            for field in ['permit_number', 'issuing_authority', 'file_path', 'renewal_cost', 'notes']:
                if getattr(keeper, field) is None and getattr(dup, field) is not None:
                    setattr(keeper, field, getattr(dup, field))
            if not dry_run:
                db.session.delete(dup)
            eq_permit_deleted += 1

    if not dry_run:
        db.session.commit()

        # Apply unique constraints (db.create_all() won't add them to existing tables)
        dialect = db.engine.dialect.name
        if dialect == 'postgresql':
            constraints = [
                "ALTER TABLE employees ADD CONSTRAINT uq_employee_name_company UNIQUE (name, company)",
                "ALTER TABLE employee_permits ADD CONSTRAINT uq_employee_permit_type UNIQUE (employee_id, permit_type)",
                "ALTER TABLE equipment_permits ADD CONSTRAINT uq_equipment_permit_type UNIQUE (equipment_id, permit_type)",
            ]
            for sql in constraints:
                try:
                    db.session.execute(db.text(sql))
                    db.session.commit()
                    print(f'  Applied: {sql}')
                except Exception:
                    db.session.rollback()
                    print(f'  Constraint already exists or skipped: {sql.split("ADD CONSTRAINT ")[1].split(" ")[0]}')

    prefix = '[DRY RUN] ' if dry_run else ''
    print(f'{prefix}Dedup complete: {emp_deleted} duplicate employees, {permit_deleted} duplicate employee permits, {eq_permit_deleted} duplicate equipment permits removed.')


@app.cli.command('send-notifications')
@click.option('--dry-run', is_flag=True, help='Preview without sending emails.')
@click.option('--employee-id', type=int, default=None, help='Send to a single employee (for testing).')
def send_notifications(dry_run, employee_id):
    """Send email notifications for expiring/expired permits."""
    print(f'{"[DRY RUN] " if dry_run else ""}Running notification cycle...')
    result = run_notification_cycle(dry_run=dry_run, employee_id=employee_id)
    for line in result['details']:
        print(line)
    prefix = '[DRY RUN] ' if dry_run else ''
    print(f'{prefix}Done: {result["sent"]} sent, {result["skipped"]} skipped, {result["failed"]} failed.')


# For Railway — auto-init on startup
with app.app_context():
    try:
        db.create_all()

        # Additive schema sync: db.create_all() does not ALTER existing tables,
        # so any column added to a model after the initial deploy is missing in
        # production. Compare each model's columns to the live table and ADD
        # whatever is missing. Idempotent and safe to run on every boot.
        if db.engine.dialect.name == 'postgresql':
            from sqlalchemy import inspect as sa_inspect
            from sqlalchemy.schema import CreateColumn
            inspector = sa_inspect(db.engine)
            for table in db.metadata.sorted_tables:
                if not inspector.has_table(table.name):
                    continue
                existing_cols = {c['name'] for c in inspector.get_columns(table.name)}
                for col in table.columns:
                    if col.name in existing_cols:
                        continue
                    try:
                        col_ddl = str(CreateColumn(col).compile(db.engine))
                        sql = f'ALTER TABLE {table.name} ADD COLUMN IF NOT EXISTS {col_ddl}'
                        db.session.execute(db.text(sql))
                        db.session.commit()
                        print(f"[INFO] Schema backfill: added {table.name}.{col.name}")
                    except Exception as e:
                        db.session.rollback()
                        print(f"[WARN] Schema backfill skipped {table.name}.{col.name}: {e}")

        # Rename CPR → PRIMEROS_AUXILIOS in existing records
        db.session.execute(db.text("UPDATE employee_permits SET permit_type = 'PRIMEROS_AUXILIOS' WHERE permit_type = 'CPR'"))
        db.session.commit()

        if not User.query.filter_by(role='admin').first():
            admin = User(username='admin', email='admin@lbcaribe.com', role='admin')
            admin.set_password('admin123')
            db.session.add(admin)
            db.session.commit()
    except Exception as e:
        print(f"[WARN] Database init failed: {e}")

    if app.config.get('ENABLE_SCHEDULER'):
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(daemon=True)
        day = app.config['NOTIFICATION_DAY'][:3].lower()
        hour = app.config['NOTIFICATION_HOUR']

        def _scheduled_notifications():
            with app.app_context():
                run_notification_cycle()

        scheduler.add_job(_scheduled_notifications, 'cron', day_of_week=day, hour=hour, misfire_grace_time=3600)
        scheduler.start()
        import atexit
        atexit.register(scheduler.shutdown)
        app.logger.info(f"[SCHEDULER] Started — notifications every {app.config['NOTIFICATION_DAY']} at {hour}:00 UTC")


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
