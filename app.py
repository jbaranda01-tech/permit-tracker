import os
import uuid
from datetime import datetime, date, timedelta
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_file, jsonify, abort, send_from_directory
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
    EMPLOYEE_PERMIT_TYPES, EQUIPMENT_PERMIT_TYPES
)

# ── APP INIT ───────────────────────────────────────────────────────────

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)
migrate = Migrate(app, db)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Por favor inicie sesión para acceder.'
login_manager.login_message_category = 'warning'

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'doc', 'docx'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


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

@app.context_processor
def inject_globals():
    today = date.today()
    alert_date = today + timedelta(days=30)

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

    return {
        'permisos_por_vencer': expiring_emp + expiring_lic + expiring_eq,
        'permisos_vencidos': expired_emp + expired_lic + expired_eq,
        'today': today,
        'alert_date': alert_date,
    }


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

    if view == 'equipment':
        query = Equipment.query
        if search:
            query = query.filter(
                db.or_(
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
        search=search,
        company_filter=company_filter,
        status_filter=status_filter,
    )


# ── EMPLOYEE ROUTES ────────────────────────────────────────────────────

@app.route('/employee/<int:id>')
@login_required
def employee_detail(id):
    emp = Employee.query.get_or_404(id)
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
                filename = f"{uuid.uuid4().hex}_{secure_filename(file.filename)}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                emp.license_file = filename

        db.session.add(emp)
        db.session.flush()  # Get ID

        # Create default permit slots
        for code, name in EMPLOYEE_PERMIT_TYPES:
            if code != 'OTHER':
                permit = EmployeePermit(
                    employee_id=emp.id,
                    permit_type=code,
                    applicability='N/A'
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
                filename = f"{uuid.uuid4().hex}_{secure_filename(file.filename)}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
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
            filename = f"{uuid.uuid4().hex}_{secure_filename(file.filename)}"
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            permit.file_path = filename

    db.session.commit()
    flash(f'Permiso {permit.display_name} actualizado.', 'success')
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
            plate_number=request.form.get('plate_number', ''),
            make=request.form.get('make', ''),
            model=request.form.get('model', ''),
            vin_serial=request.form.get('vin_serial', ''),
            notes=request.form.get('notes', ''),
            status=request.form.get('status', 'activo'),
        )
        yr = request.form.get('year', '')
        if yr:
            try:
                equip.year = int(yr)
            except ValueError:
                pass

        db.session.add(equip)
        db.session.flush()

        # Create default permit slots
        for code, name in EQUIPMENT_PERMIT_TYPES:
            if code != 'OTHER':
                permit = EquipmentPermit(
                    equipment_id=equip.id,
                    permit_type=code,
                    applicability='YES'
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
        equip.plate_number = request.form.get('plate_number', '')
        equip.make = request.form.get('make', '')
        equip.model = request.form.get('model', '')
        equip.vin_serial = request.form.get('vin_serial', '')
        equip.notes = request.form.get('notes', '')
        equip.status = request.form.get('status', 'activo')

        yr = request.form.get('year', '')
        if yr:
            try:
                equip.year = int(yr)
            except ValueError:
                pass

        db.session.commit()
        flash(f'Equipo {equip.display_name} actualizado.', 'success')
        return redirect(url_for('equipment_detail', id=equip.id))

    return render_template('equipment_form.html', equipment=equip)


@app.route('/equipment/<int:id>/delete', methods=['POST'])
@admin_required
def equipment_delete(id):
    equip = Equipment.query.get_or_404(id)
    name = equip.display_name
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
            filename = f"{uuid.uuid4().hex}_{secure_filename(file.filename)}"
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            permit.file_path = filename

    db.session.commit()
    flash(f'Permiso {permit.display_name} actualizado.', 'success')
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


# ── FILE SERVING ───────────────────────────────────────────────────────

@app.route('/uploads/<filename>')
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


# ── EXCEL IMPORT ───────────────────────────────────────────────────────

@app.route('/import', methods=['GET', 'POST'])
@admin_required
def import_data():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No se seleccionó archivo.', 'danger')
            return redirect(url_for('import_data'))

        file = request.files['file']
        if not file.filename.endswith(('.xlsx', '.xls')):
            flash('Solo se aceptan archivos Excel (.xlsx, .xls).', 'danger')
            return redirect(url_for('import_data'))

        try:
            from openpyxl import load_workbook
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], f'import_{uuid.uuid4().hex}.xlsx')
            file.save(filepath)
            wb = load_workbook(filepath, read_only=True)

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
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                flash('El archivo está vacío.', 'warning')
                return redirect(url_for('import_data'))

            headers = [str(h).strip().upper() if h else '' for h in rows[0]]

            for row in rows[1:]:
                if not row[0]:
                    continue

                name = str(row[0]).strip()

                # Check if employee already exists
                existing = Employee.query.filter_by(name=name).first()
                if existing:
                    skipped += 1
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

            db.session.commit()
            os.remove(filepath)
            flash(f'Importación completa: {imported} empleados importados, {skipped} duplicados omitidos.', 'success')

        except Exception as e:
            db.session.rollback()
            flash(f'Error en la importación: {str(e)}', 'danger')

        return redirect(url_for('import_data'))

    return render_template('import.html')


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

    alerts.sort(key=lambda a: a['date'])
    return jsonify(alerts)


# ── HEALTH CHECK ──────────────────────────────────────────────────────

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


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


# For Railway — auto-init on startup
with app.app_context():
    try:
        db.create_all()
        if not User.query.filter_by(role='admin').first():
            admin = User(username='admin', email='admin@lbcaribe.com', role='admin')
            admin.set_password('admin123')
            db.session.add(admin)
            db.session.commit()
    except Exception as e:
        print(f"[WARN] Database init failed: {e}")


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
