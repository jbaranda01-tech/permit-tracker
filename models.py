import logging
import re
import threading
from contextlib import contextmanager

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from sqlalchemy import event as sa_event, inspect as sa_inspect
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timedelta

db = SQLAlchemy()

# Splits a unit number into digit / non-digit runs for natural ordering
_UNIT_CHUNK_RE = re.compile(r'(\d+)')

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

    @property
    def is_shop_only(self):
        if self.role == 'admin':
            return False
        return any(ir.role == 'shop' for ir in self.issue_roles)

    @property
    def has_shop_role(self):
        if self.role == 'admin':
            return True
        return any(ir.role == 'shop' for ir in self.issue_roles)

    @property
    def is_shop_manager(self):
        # Base-role manager (or admin) who also has the shop/taller role.
        return self.is_manager and self.has_shop_role


# ── EMPLOYEES ──────────────────────────────────────────────────────────

EMPLOYEE_ARCHIVE_REASONS = [
    ('baja', 'Baja'),
    ('renuncia', 'Renuncia'),
    ('otro', 'Otro'),
]


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

    # Archive (soft-remove): archived_at set ⇒ hidden from all active surfaces
    archived_at = db.Column(db.DateTime, nullable=True)
    archive_reason = db.Column(db.String(30), nullable=True)  # code from EMPLOYEE_ARCHIVE_REASONS
    archive_note = db.Column(db.String(300), nullable=True)

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

    # Link-based access for issue reporting (UUID token)
    access_token = db.Column(db.String(36), unique=True, nullable=True, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    permits = db.relationship('EmployeePermit', backref='employee', lazy='dynamic', cascade='all, delete-orphan')

    @property
    def company_full(self):
        return 'LB Caribe Services' if self.company == 'LB' else 'Professional Logistics'

    @property
    def is_archived(self):
        return self.archived_at is not None

    @property
    def archive_reason_label(self):
        for code, label in EMPLOYEE_ARCHIVE_REASONS:
            if code == self.archive_reason:
                return label
        return self.archive_reason or 'Otro'

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

    @property
    def next_expiration(self):
        """Nearest upcoming (>= today) expiration across license + non-N/A permits; None if none."""
        today = date.today()
        dates = [p.expiration_date for p in self.permits
                 if p.applicability != 'N/A' and p.expiration_date and p.expiration_date >= today]
        if self.license_expiration and self.license_expiration >= today:
            dates.append(self.license_expiration)
        return min(dates) if dates else None


EMPLOYEE_PERMIT_TYPES = [
    ('NTSP', 'NTSP'),
    ('TWIC', 'TWIC'),
    ('CERT_MEDICO', 'Certificado Médico Choferil'),
    ('ANTECEDENTES', 'Antecedentes Penales'),
    ('RECORD_CHOFERIL', 'Record Choferil'),
    ('HM126', 'HM-126 / HM-232'),
    ('PRIMEROS_AUXILIOS', 'Primeros Auxilios'),
    ('OTHER', 'Otro'),
]

# Retired codes that may still have rows in the DB. HM-126 and HM-232 were merged
# into a single HM126 permit (one document, one expiration date); employees whose
# two rows carried genuinely distinct dates/documents keep their HM232 row rather
# than losing data. Nothing creates new rows with these codes -- they only need a
# readable label, plus the reachability wiring in app.py.
LEGACY_EMPLOYEE_PERMIT_TYPES = [
    ('HM232', 'HM-232 (legado)'),
]


class EmployeePermit(db.Model):
    __tablename__ = 'employee_permits'
    __table_args__ = (
        db.UniqueConstraint('employee_id', 'permit_type', name='uq_employee_permit_type'),
    )
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employees.id', ondelete='CASCADE'), nullable=False)
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
        for code, name in LEGACY_EMPLOYEE_PERMIT_TYPES:
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

EQUIPMENT_ARCHIVE_REASONS = [
    ('vendido', 'Vendido'),
    ('chatarra', 'Chatarra'),
    ('otro', 'Otro'),
]

EQUIPMENT_CLASSES = [
    ('truck', 'Camión'),
    ('chassis', 'Chasis'),
    ('tank', 'Tanque'),
    ('generator', 'Generador'),
]


def classify_equipment(model, equipment_type=None):
    """Infer the equipment class from the free-text model. Unmatched ⇒ truck
    (matches the historical reportable-vehicle semantics)."""
    m = (model or '').strip().lower()
    if 'chasis' in m or 'carreton' in m or 'carretón' in m:
        return 'chassis'
    if 'tanque' in m:
        return 'tank'
    if 'generador' in m or equipment_type == 'generator':
        return 'generator'
    return 'truck'


class Equipment(db.Model):
    __tablename__ = 'equipment'
    id = db.Column(db.Integer, primary_key=True)
    company = db.Column(db.String(10), nullable=False)  # LB, PLI, or Personal
    equipment_type = db.Column(db.String(20), default='vehicle')  # deprecated in UI; kept for data
    equipment_class = db.Column(db.String(20))  # truck | chassis | tank | generator
    titular = db.Column(db.String(200))
    name = db.Column(db.String(120))  # optional manual display name; see display_name
    unit_number = db.Column(db.String(50))
    plate_number = db.Column(db.String(50))
    make = db.Column(db.String(100))
    model = db.Column(db.String(100))
    year = db.Column(db.Integer)
    vin_serial = db.Column(db.String(100))
    insurance_company = db.Column(db.String(200))
    # Which insurance policy covers this vehicle. NULL = automática (derive from
    # equipment_class), a SEGURO_* code = that shared CompanyPermit, INSURANCE_OWN
    # = the vehicle carries its own editable per-vehicle SEGURO permit.
    insurance_policy_type = db.Column(db.String(30))
    cost = db.Column(db.Numeric(10, 2))
    notes = db.Column(db.Text)
    status = db.Column(db.String(20), default='activo')

    # Archive (soft-remove): archived_at set ⇒ hidden from all active surfaces
    archived_at = db.Column(db.DateTime, nullable=True)
    archive_reason = db.Column(db.String(30), nullable=True)  # code from EQUIPMENT_ARCHIVE_REASONS
    archive_note = db.Column(db.String(300), nullable=True)

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
        """Manual name first, then unit #, then plate; the id is a last resort."""
        return (self.name or self.unit_number or self.plate_number
                or f'Equipo #{self.id}')

    @property
    def unit_sort_key(self):
        """Natural (human) order for unit numbers: 2 < 12 < 100, T-2 < T-12.
        Digit runs compare as ints, text runs as text; each chunk is a
        (is_text, text, number) triple so an int never compares to a str.
        Blank units sort last (deterministic across SQLite/Postgres, which
        disagree on NULL placement), tiebroken by display_name."""
        raw = (self.unit_number or '').strip().lstrip('#').strip()
        if not raw:
            return (1, (), self.display_name.lower())
        parts = tuple(
            (0, '', int(c)) if c.isdigit() else (1, c, 0)
            for c in _UNIT_CHUNK_RE.split(raw.lower()) if c
        )
        return (0, parts, '')

    @property
    def is_archived(self):
        return self.archived_at is not None

    @property
    def archive_reason_label(self):
        for code, label in EQUIPMENT_ARCHIVE_REASONS:
            if code == self.archive_reason:
                return label
        return self.archive_reason or 'Otro'

    @property
    def equipment_class_label(self):
        for code, label in EQUIPMENT_CLASSES:
            if code == self.equipment_class:
                return label
        return 'Camión'

    @property
    def insurance_permit_type(self):
        """CompanyPermit type of the shared policy covering this vehicle.

        None means the vehicle is on its own per-vehicle SEGURO permit (Personal
        equipment, or an explicit SELF_INSURANCE_TYPES choice — INSURANCE_OWN or
        INSURANCE_COMPULSORY). This is the single place the policy is resolved —
        detail view, Excel and PDF all read it.
        """
        if self.company not in ('LB', 'PLI'):
            return None
        choice = self.insurance_policy_type
        if choice in SELF_INSURANCE_TYPES:
            return None
        if choice in set(INSURANCE_TYPE_BY_CLASS.values()):
            return choice
        # NULL (or a stale/unknown code) ⇒ automática: follow the equipment class.
        # Compulsorio is absent from this map on purpose — it is never auto-applied.
        cls = self.equipment_class or classify_equipment(self.model, self.equipment_type)
        return INSURANCE_TYPE_BY_CLASS.get(cls, 'SEGURO_TRUCK')

    @property
    def insurance_card_title(self):
        return INSURANCE_CARD_TITLES.get(self.insurance_permit_type, 'Seguro (compartido)')

    @property
    def own_insurance_label(self):
        """Header for the vehicle's own SEGURO permit card (Compulsorio vs ordinary)."""
        return INSURANCE_OWN_TITLES.get(self.insurance_policy_type, 'Seguro / Insurance')

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

    @property
    def next_expiration(self):
        """Nearest upcoming (>= today) expiration across non-N/A permits; None if none."""
        today = date.today()
        dates = [p.expiration_date for p in self.permits
                 if p.applicability != 'N/A' and p.expiration_date and p.expiration_date >= today]
        return min(dates) if dates else None


EQUIPMENT_PERMIT_TYPES = [
    ('MARBETE', 'Marbete'),
    ('SEGURO', 'Seguro / Insurance'),
    ('INSPECCION', 'Inspección'),
    ('NTSP', 'NTSP'),
    ('OTHER', 'Otro'),
]


class EquipmentPermit(db.Model):
    __tablename__ = 'equipment_permits'
    __table_args__ = (
        db.UniqueConstraint('equipment_id', 'permit_type', name='uq_equipment_permit_type'),
    )
    id = db.Column(db.Integer, primary_key=True)
    equipment_id = db.Column(db.Integer, db.ForeignKey('equipment.id', ondelete='CASCADE'), nullable=False)
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


# ── COMPANY PERMITS ───────────────────────────────────────────────────

COMPANY_PERMIT_TYPES = [
    ('SEGURO_TRUCK', 'Seguro — Camiones', ['LB', 'PLI']),
    ('SEGURO_CHASSIS', 'Seguro — Chasis', ['LB', 'PLI']),
    ('SEGURO_TANK', 'Seguro — Tanques', ['LB', 'PLI']),
    ('SEGURO_GENERATOR', 'Seguro — Generadores', ['LB', 'PLI']),
    ('HAZMAT_CERT', 'Certificado HazMat', ['LB', 'PLI']),
    ('NMFTA_CERT', 'Certificado NMFTA', ['PLI']),
    ('USDOT_BIENNIAL', 'USDOT Biennial Report', ['LB', 'PLI']),
    ('NTSP_FRANCHISE', 'Franquicia NTSP', ['LB', 'PLI']),
]

# Shared insurance is split per equipment class (one policy covers all LB/PLI
# equipment of that class). Per-vehicle EQUIPMENT_PERMIT_TYPES keeps 'SEGURO'.
INSURANCE_TYPE_BY_CLASS = {
    'truck': 'SEGURO_TRUCK',
    'chassis': 'SEGURO_CHASSIS',
    'tank': 'SEGURO_TANK',
    'generator': 'SEGURO_GENERATOR',
}

# Keyed by CompanyPermit type (not class) — Equipment.insurance_permit_type
# resolves to a type, which may differ from the vehicle's class when overridden.
INSURANCE_CARD_TITLES = {
    'SEGURO_TRUCK': 'Seguro de camiones (compartido)',
    'SEGURO_CHASSIS': 'Seguro de chasis (compartido)',
    'SEGURO_TANK': 'Seguro de tanques (compartido)',
    'SEGURO_GENERATOR': 'Seguro de generadores (compartido)',
}

# Sentinel stored in Equipment.insurance_policy_type when the vehicle carries its
# own policy instead of one of the shared company policies.
INSURANCE_OWN = 'OWN'

# Compulsorio is a per-vehicle policy — its expiration date lives on the vehicle's
# own SEGURO EquipmentPermit, exactly like INSURANCE_OWN — NOT a company policy.
# Available to every company (LB, PLI and Personal), and never auto-applied: it is
# deliberately absent from INSURANCE_TYPE_BY_CLASS so only an explicit choice sets it.
INSURANCE_COMPULSORY = 'COMPULSORIO'

# Choices meaning "this vehicle carries its own SEGURO permit" rather than a shared
# company policy. Both keep the per-vehicle SEGURO permit editable ('YES').
SELF_INSURANCE_TYPES = (INSURANCE_OWN, INSURANCE_COMPULSORY)

# Header for the vehicle's own SEGURO permit card, keyed by the stored choice.
# Anything not listed here (INSURANCE_OWN, Personal, NULL) keeps the plain name.
INSURANCE_OWN_TITLES = {
    INSURANCE_COMPULSORY: 'Seguro Compulsorio',
}

# (code, label) options for the equipment form's "Póliza de Seguro" select —
# derived from COMPANY_PERMIT_TYPES so the labels live in exactly one place.
INSURANCE_POLICY_CHOICES = [
    (code, label) for code, label, _companies in COMPANY_PERMIT_TYPES
    if code in set(INSURANCE_TYPE_BY_CLASS.values())
]


def sync_vehicle_insurance_permit(equip):
    """Align the per-vehicle SEGURO permit with the vehicle's insurance choice.

    A vehicle covered by a shared company policy keeps its SEGURO permit at 'N/A'
    (the detail view hides it and shows the read-only policy card instead); a
    vehicle on its own policy gets an editable 'YES' permit. The expiration date
    is deliberately left intact — unlike the /toggle routes — so switching to a
    shared policy and back restores the vehicle's own dates.
    """
    permit = equip.permits.filter_by(permit_type='SEGURO').first()
    applicability = 'N/A' if equip.insurance_permit_type else 'YES'
    if permit is None:
        permit = EquipmentPermit(equipment_id=equip.id, permit_type='SEGURO')
        db.session.add(permit)
    permit.applicability = applicability
    return permit


class CompanyPermit(db.Model):
    __tablename__ = 'company_permits'
    __table_args__ = (
        db.UniqueConstraint('company', 'permit_type', name='uq_company_permit_type'),
    )
    id = db.Column(db.Integer, primary_key=True)
    company = db.Column(db.String(10), nullable=False)
    permit_type = db.Column(db.String(50), nullable=False)
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
        for code, name, _companies in COMPANY_PERMIT_TYPES:
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


# ── FILE STORAGE ──────────────────────────────────────────────────────

class FileStorage(db.Model):
    __tablename__ = 'file_storage'
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(500), unique=True, nullable=False)
    original_filename = db.Column(db.String(500))
    mime_type = db.Column(db.String(100), nullable=False, default='application/pdf')
    data = db.Column(db.LargeBinary, nullable=False)
    size = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# ── NOTIFICATION LOG ─────────────────────────────────────────────────

class NotificationLog(db.Model):
    __tablename__ = 'notification_logs'
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employees.id', ondelete='CASCADE'), nullable=False)
    permit_key = db.Column(db.String(100), nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)
    email_to = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(20), nullable=False)
    error_message = db.Column(db.Text)


# ── ISSUE REPORTING ──────────────────────────────────────────────────

EXCLUDED_EQUIPMENT_MODELS = [
    'carreton', 'chasis', 'generador alquiler',
    'generador contenedor', 'tanque combustible', 'tanque harina',
]


ISSUE_CATEGORIES = [
    ('motor', 'Motor'),
    ('frenos', 'Frenos'),
    ('electrico', 'Eléctrico'),
    ('llantas', 'Llantas'),
    ('transmision', 'Transmisión'),
    ('carroceria', 'Carrocería'),
    ('ac', 'Aire Acondicionado'),
    ('luces', 'Luces'),
    ('fluidos', 'Fluidos'),
    ('suspension', 'Suspensión'),
    ('otro', 'Otro'),
]

ISSUE_SEVERITIES = [
    ('baja', 'Baja'),
    ('media', 'Media'),
    ('alta', 'Alta'),
    ('critica', 'Crítica'),
]

ISSUE_STATUSES = [
    ('reportado', 'Reportado'),
    ('en_proceso', 'En Proceso'),
    ('cerrado', 'Cerrado'),
]

# Open vs terminal. Imported by issues/routes.py and app.py so the queue, the
# reports and the resolved section can never disagree about what "open" means.
OPEN_STATUSES = ['reportado', 'en_proceso']
RESOLVED_STATUSES = ['cerrado']

# Retired status codes -> their replacement. 'en_revision'/'en_reparacion' were
# the same working state and 'resuelto'/'cerrado' the same terminal one, so each
# pair collapsed into one. Consumed by the boot-time migration, the
# migrate-issue-statuses CLI command, the Excel importer (old spreadsheets still
# say "Resuelto") and the queue's ?status= filter (old bookmarks).
LEGACY_ISSUE_STATUSES = {
    'en_revision': 'en_proceso',
    'en_reparacion': 'en_proceso',
    'resuelto': 'cerrado',
}


class Issue(db.Model):
    __tablename__ = 'issues'
    id = db.Column(db.Integer, primary_key=True)
    equipment_id = db.Column(db.Integer, db.ForeignKey('equipment.id', ondelete='SET NULL'), nullable=True)
    reported_by_employee_id = db.Column(db.Integer, db.ForeignKey('employees.id', ondelete='SET NULL'), nullable=True)
    assigned_to_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    parent_issue_id = db.Column(db.Integer, db.ForeignKey('issues.id', ondelete='SET NULL'), nullable=True)
    category = db.Column(db.String(50), nullable=False)
    severity = db.Column(db.String(20), nullable=False, default='media')
    current_status = db.Column(db.String(30), nullable=False, default='reportado')
    description = db.Column(db.Text, nullable=False)
    odometer_reading = db.Column(db.Integer, nullable=True)
    location = db.Column(db.String(200), nullable=True)
    reported_at = db.Column(db.DateTime, default=datetime.utcnow)
    resolved_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    equipment = db.relationship('Equipment', backref=db.backref('issues', lazy='dynamic'))
    reporter = db.relationship('Employee', backref=db.backref('reported_issues', lazy='dynamic'))
    assignee = db.relationship('User', backref=db.backref('assigned_issues', lazy='dynamic'))
    status_history = db.relationship('IssueStatusHistory', backref='issue', lazy='dynamic', cascade='all, delete-orphan')
    children = db.relationship('Issue', backref=db.backref('parent', remote_side='Issue.id'), lazy='dynamic')

    @property
    def time_to_resolution(self):
        if self.resolved_at and self.reported_at:
            return self.resolved_at - self.reported_at
        return None

    @property
    def category_label(self):
        for code, name in ISSUE_CATEGORIES:
            if code == self.category:
                return name
        return self.category

    @property
    def severity_label(self):
        for code, name in ISSUE_SEVERITIES:
            if code == self.severity:
                return name
        return self.severity

    @property
    def status_label(self):
        for code, name in ISSUE_STATUSES:
            if code == self.current_status:
                return name
        return self.current_status


class IssueStatusHistory(db.Model):
    __tablename__ = 'issue_status_history'
    id = db.Column(db.Integer, primary_key=True)
    issue_id = db.Column(db.Integer, db.ForeignKey('issues.id', ondelete='CASCADE'), nullable=False)
    from_status = db.Column(db.String(30), nullable=True)
    to_status = db.Column(db.String(30), nullable=False)
    changed_by_user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    changed_by_employee_id = db.Column(db.Integer, db.ForeignKey('employees.id', ondelete='SET NULL'), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    changed_at = db.Column(db.DateTime, default=datetime.utcnow)

    changed_by_user = db.relationship('User', foreign_keys=[changed_by_user_id])
    changed_by_employee = db.relationship('Employee', foreign_keys=[changed_by_employee_id])


class IssuePhoto(db.Model):
    __tablename__ = 'issue_photos'
    id = db.Column(db.Integer, primary_key=True)
    issue_id = db.Column(db.Integer, db.ForeignKey('issues.id', ondelete='CASCADE'), nullable=False)
    uploaded_by_employee_id = db.Column(db.Integer, db.ForeignKey('employees.id', ondelete='SET NULL'), nullable=True)
    storage_filename = db.Column(db.String(500), nullable=False)
    caption = db.Column(db.String(300), nullable=True)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    issue = db.relationship('Issue', backref=db.backref('photos', lazy='dynamic', cascade='all, delete-orphan'))


class UserIssueRole(db.Model):
    __tablename__ = 'user_issue_roles'
    __table_args__ = (
        db.PrimaryKeyConstraint('user_id', 'role'),
    )
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    role = db.Column(db.String(20), nullable=False)

    user = db.relationship('User', backref=db.backref('issue_roles', lazy='select', cascade='all, delete-orphan'))


# ── UPLOADED-FILE PROTECTION ─────────────────────────────────────────
# Uploaded documents (permit PDFs, licence scans, certificates) may only be
# attached, replaced or removed through the manual upload/edit routes. Excel
# imports are data-only: they refresh dates and applicability, never documents.
#
# Today's importers already leave the document columns alone, but that is a
# convention a future edit could quietly break — and a lost permit PDF is not
# recoverable from the spreadsheet. `protect_uploaded_files()` turns the
# convention into an enforced invariant: while it is active, any write to a
# document column is reverted before it reaches the database, and any attempt
# to delete a stored file aborts the whole import.

# Model → the columns on it that point at a stored document.
PROTECTED_FILE_FIELDS = {
    'Employee': ('license_file',),
    'EmployeePermit': ('file_path',),
    'EquipmentPermit': ('file_path',),
    'CompanyPermit': ('file_path',),
    'IssuePhoto': ('storage_filename',),
}

_file_guard = threading.local()


def uploaded_files_protected():
    """True while an import (or other bulk job) is running under the guard."""
    return getattr(_file_guard, 'active', False)


@contextmanager
def protect_uploaded_files():
    """Block all writes to uploaded documents for the duration of the block.

    Wraps the Excel importers. Re-entrant, and restores the previous state on
    exit so nesting (or an exception) can't leave the guard stuck on.
    """
    previous = getattr(_file_guard, 'active', False)
    _file_guard.active = True
    try:
        yield
    finally:
        _file_guard.active = previous


class ProtectedFileError(RuntimeError):
    """Raised when a guarded operation tries to destroy a stored document."""


def _protected_fields(obj):
    return PROTECTED_FILE_FIELDS.get(type(obj).__name__)


@sa_event.listens_for(db.session, 'before_flush')
def _block_document_writes(session, flush_context, instances):
    if not uploaded_files_protected():
        return

    reverted = []

    # 1. Existing rows: put back the committed filename. The rest of the
    #    row's changes (expiration_date, applicability, …) still go through.
    for obj in session.dirty:
        fields = _protected_fields(obj)
        if not fields:
            continue
        state = sa_inspect(obj)
        for field in fields:
            history = state.attrs[field].history
            if not history.has_changes():
                continue
            original = history.deleted[0] if history.deleted else None
            setattr(obj, field, original)
            reverted.append(f'{type(obj).__name__}(id={obj.id}).{field}')

    # 2. New rows: an import has no document to attach, so a filename here is
    #    either a mistake or a second reference to someone else's file.
    for obj in session.new:
        fields = _protected_fields(obj)
        if not fields:
            continue
        for field in fields:
            if getattr(obj, field, None) is not None:
                setattr(obj, field, None)
                reverted.append(f'new {type(obj).__name__}.{field}')

    # 3. Deletions are not recoverable, so they abort the transaction instead
    #    of being silently patched up.
    for obj in session.deleted:
        if isinstance(obj, FileStorage):
            raise ProtectedFileError(
                f'Una importación intentó eliminar el archivo almacenado '
                f'"{obj.filename}". Los documentos solo se pueden reemplazar manualmente.'
            )
        fields = _protected_fields(obj)
        if fields and any(getattr(obj, f, None) for f in fields):
            raise ProtectedFileError(
                f'Una importación intentó eliminar {type(obj).__name__} '
                f'id={obj.id}, que tiene un documento adjunto.'
            )

    if reverted:
        logging.getLogger(__name__).warning(
            'Importación bloqueada de modificar documentos adjuntos: %s',
            ', '.join(reverted),
        )
