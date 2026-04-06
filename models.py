from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timedelta

db = SQLAlchemy()

# ── USERS ──────────────────────────────────────────────────────────────

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='viewer')  # admin, manager, viewer
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self):
        return self.role == 'admin'

    @property
    def is_manager(self):
        return self.role in ('admin', 'manager')


# ── EMPLOYEES ──────────────────────────────────────────────────────────

class Employee(db.Model):
    __tablename__ = 'employees'
    __table_args__ = (
        db.UniqueConstraint('name', 'company', name='uq_employee_name_company'),
    )
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    company = db.Column(db.String(10), nullable=False)  # LB or PLI
    area = db.Column(db.String(100))  # chofer, administrador, vendedor, etc.
    status = db.Column(db.String(20), default='activo')

    # Profile fields
    fecha_nacimiento = db.Column(db.Date)
    license_number = db.Column(db.String(50))
    license_expiration = db.Column(db.Date)
    license_file = db.Column(db.String(500))  # file path for license copy
    puesto = db.Column(db.String(100))
    telefono = db.Column(db.String(30))
    email = db.Column(db.String(120))
    fecha_contratacion = db.Column(db.Date)
    contacto_emergencia = db.Column(db.String(200))
    shirt_size = db.Column(db.String(10))

    # HAZMAT is a boolean flag, not date-based
    endoso_hazmat = db.Column(db.String(10), default='N/A')  # SI, NO, N/A

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    permits = db.relationship('EmployeePermit', backref='employee', lazy='dynamic', cascade='all, delete-orphan')

    @property
    def company_full(self):
        return 'LB Caribe Services' if self.company == 'LB' else 'Professional Logistics'

    @property
    def completion_status(self):
        """Returns (complete_count, total_applicable, is_complete)"""
        total = 0
        complete = 0
        # Check license
        if self.license_number is not None or self.license_expiration is not None:
            total += 1
            if self.license_number and self.license_expiration:
                complete += 1
        # Check each permit
        for permit in self.permits:
            if permit.applicability != 'N/A':
                total += 1
                if permit.expiration_date:
                    complete += 1
        return (complete, total, complete == total and total > 0)

    @property
    def permit_status_summary(self):
        """Returns dict with expired, expiring_soon, valid, na counts"""
        today = date.today()
        alert_date = today + timedelta(days=30)
        summary = {'expired': 0, 'expiring_soon': 0, 'valid': 0, 'na': 0, 'missing': 0}

        # Check license expiration
        if self.license_expiration:
            if self.license_expiration < today:
                summary['expired'] += 1
            elif self.license_expiration <= alert_date:
                summary['expiring_soon'] += 1
            else:
                summary['valid'] += 1

        for permit in self.permits:
            if permit.applicability == 'N/A':
                summary['na'] += 1
            elif permit.expiration_date is None:
                summary['missing'] += 1
            elif permit.expiration_date < today:
                summary['expired'] += 1
            elif permit.expiration_date <= alert_date:
                summary['expiring_soon'] += 1
            else:
                summary['valid'] += 1

        return summary


EMPLOYEE_PERMIT_TYPES = [
    ('NTSP', 'NTSP'),
    ('TWIC', 'TWIC'),
    ('CERT_MEDICO', 'Certificado Médico Choferil'),
    ('ANTECEDENTES', 'Antecedentes Penales'),
    ('RECORD_CHOFERIL', 'Record Choferil'),
    ('HM126', 'HM-126'),
    ('HM232', 'HM-232'),
    ('OTHER', 'Otro'),
]


class EmployeePermit(db.Model):
    __tablename__ = 'employee_permits'
    __table_args__ = (
        db.UniqueConstraint('employee_id', 'permit_type', name='uq_employee_permit_type'),
    )
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employees.id'), nullable=False)
    permit_type = db.Column(db.String(50), nullable=False)
    permit_name = db.Column(db.String(200))  # For OTHER type
    applicability = db.Column(db.String(10), default='YES')  # YES, N/A
    expiration_date = db.Column(db.Date)
    issuing_authority = db.Column(db.String(200))
    permit_number = db.Column(db.String(100))
    file_path = db.Column(db.String(500))
    renewal_cost = db.Column(db.Float)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def display_name(self):
        if self.permit_type == 'OTHER' and self.permit_name:
            return self.permit_name
        for code, name in EMPLOYEE_PERMIT_TYPES:
            if code == self.permit_type:
                return name
        return self.permit_type

    @property
    def status(self):
        if self.applicability == 'N/A':
            return 'na'
        if self.expiration_date is None:
            return 'missing'
        today = date.today()
        if self.expiration_date < today:
            return 'expired'
        if self.expiration_date <= today + timedelta(days=30):
            return 'expiring_soon'
        return 'valid'

    @property
    def status_label(self):
        labels = {
            'na': 'N/A',
            'missing': 'Incompleto',
            'expired': 'Vencido',
            'expiring_soon': 'Por Vencer',
            'valid': 'Vigente'
        }
        return labels.get(self.status, 'Desconocido')


# ── EQUIPMENT ──────────────────────────────────────────────────────────

class Equipment(db.Model):
    __tablename__ = 'equipment'
    id = db.Column(db.Integer, primary_key=True)
    company = db.Column(db.String(10), nullable=False)  # LB, PLI, or Personal
    equipment_type = db.Column(db.String(20), default='vehicle')  # vehicle, generator
    titular = db.Column(db.String(200))
    unit_number = db.Column(db.String(50))
    plate_number = db.Column(db.String(50))
    make = db.Column(db.String(100))
    model = db.Column(db.String(100))
    year = db.Column(db.Integer)
    vin_serial = db.Column(db.String(100))
    insurance_company = db.Column(db.String(200))
    notes = db.Column(db.Text)
    status = db.Column(db.String(20), default='activo')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    permits = db.relationship('EquipmentPermit', backref='equipment', lazy='dynamic', cascade='all, delete-orphan')

    @property
    def company_full(self):
        names = {'LB': 'LB Caribe Services', 'PLI': 'Professional Logistics', 'Personal': 'Personal'}
        return names.get(self.company, self.company)

    @property
    def display_name(self):
        parts = []
        if self.year:
            parts.append(str(self.year))
        if self.make:
            parts.append(self.make)
        if self.model:
            parts.append(self.model)
        if parts:
            return ' '.join(parts)
        if self.plate_number:
            return f'Tablilla: {self.plate_number}'
        return f'Equipo #{self.id}'

    @property
    def permit_status_summary(self):
        today = date.today()
        alert_date = today + timedelta(days=30)
        summary = {'expired': 0, 'expiring_soon': 0, 'valid': 0, 'na': 0, 'missing': 0}
        for permit in self.permits:
            if permit.applicability == 'N/A':
                summary['na'] += 1
            elif permit.expiration_date is None:
                summary['missing'] += 1
            elif permit.expiration_date < today:
                summary['expired'] += 1
            elif permit.expiration_date <= alert_date:
                summary['expiring_soon'] += 1
            else:
                summary['valid'] += 1
        return summary


EQUIPMENT_PERMIT_TYPES = [
    ('MARBETE', 'Marbete'),
    ('SEGURO', 'Seguro / Insurance'),
    ('VOUCHER', 'Voucher'),
    ('INSPECCION', 'Inspección'),
    ('OTHER', 'Otro'),
]


class EquipmentPermit(db.Model):
    __tablename__ = 'equipment_permits'
    __table_args__ = (
        db.UniqueConstraint('equipment_id', 'permit_type', name='uq_equipment_permit_type'),
    )
    id = db.Column(db.Integer, primary_key=True)
    equipment_id = db.Column(db.Integer, db.ForeignKey('equipment.id'), nullable=False)
    permit_type = db.Column(db.String(50), nullable=False)
    permit_name = db.Column(db.String(200))
    applicability = db.Column(db.String(10), default='YES')
    expiration_date = db.Column(db.Date)
    issuing_authority = db.Column(db.String(200))
    permit_number = db.Column(db.String(100))
    file_path = db.Column(db.String(500))
    renewal_cost = db.Column(db.Float)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def display_name(self):
        if self.permit_type == 'OTHER' and self.permit_name:
            return self.permit_name
        for code, name in EQUIPMENT_PERMIT_TYPES:
            if code == self.permit_type:
                return name
        return self.permit_type

    @property
    def status(self):
        if self.applicability == 'N/A':
            return 'na'
        if self.expiration_date is None:
            return 'missing'
        today = date.today()
        if self.expiration_date < today:
            return 'expired'
        if self.expiration_date <= today + timedelta(days=30):
            return 'expiring_soon'
        return 'valid'

    @property
    def status_label(self):
        labels = {
            'na': 'N/A',
            'missing': 'Incompleto',
            'expired': 'Vencido',
            'expiring_soon': 'Por Vencer',
            'valid': 'Vigente'
        }
        return labels.get(self.status, 'Desconocido')
