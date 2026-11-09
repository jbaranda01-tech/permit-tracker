"""The equipment title is the unit number (no separate "Nombre" field).

Run from the repo root:  python3 -m pytest -q tests/test_equipment_title.py
Boots the real app against a throwaway SQLite DB.

Regression: the edit form used to print NULL columns as the literal word
"None", and saving stored it — so a unit's title became "None" and stopped
following the Unidad field.
"""
import os
import pathlib
import sys
import tempfile
import uuid

REPO = pathlib.Path(__file__).resolve().parents[1]
DB = pathlib.Path(tempfile.mkdtemp(prefix='permit-tracker-test-')) / 'test_title.db'
os.environ['SECRET_KEY'] = 'test'
os.environ['DATABASE_URL'] = 'sqlite:///' + str(DB)
os.environ.pop('DATABASE_PRIVATE_URL', None)
os.environ['ENABLE_SCHEDULER'] = 'false'
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402
import app as appmod  # noqa: E402
from models import db, Equipment, User  # noqa: E402

app = appmod.app
app.config['TESTING'] = True

_counter = {'n': 0}
# The test files share one app process (and DB) under a full `pytest tests/`
# run, so names carry a per-run token on top of the counter.
_RUN = uuid.uuid4().hex[:6]


def _uniq(prefix):
    _counter['n'] += 1
    return f'{prefix}{_RUN}-{_counter["n"]}'


@pytest.fixture(scope='module')
def client():
    """Logged-in admin (same admin/admin123 the sibling test files create)."""
    from werkzeug.security import generate_password_hash
    with app.app_context():
        if not User.query.filter_by(username='admin').first():
            db.session.add(User(
                username='admin', email='admin@example.com', role='admin',
                password_hash=generate_password_hash('admin123', method='pbkdf2:sha256')))
            db.session.commit()
    c = app.test_client()
    r = c.post('/login', data={'username': 'admin', 'password': 'admin123'},
               follow_redirects=True)
    assert r.status_code == 200
    return c


_made = []


@pytest.fixture(scope='module', autouse=True)
def _cleanup():
    yield
    with app.app_context():
        for eq_id in _made:
            eq = db.session.get(Equipment, eq_id)
            if eq is not None:
                db.session.delete(eq)
        db.session.commit()


def _equipment(**kw):
    """An equipment row whose optional text columns are NULL unless given."""
    fields = dict(company='LB', equipment_class='truck', status='activo')
    fields.update(kw)
    with app.app_context():
        eq = Equipment(**fields)
        db.session.add(eq)
        db.session.commit()
        _made.append(eq.id)
        return eq.id


def _get(eq_id):
    return db.session.get(Equipment, eq_id)


def _edit_form(**kw):
    """What the browser posts for the edit form (no `name` — the field is gone)."""
    form = dict(company='LB', equipment_class='truck', status='activo',
                titular='', unit_number='', plate_number='', make='', model='',
                year='', vin_serial='', insurance_company='',
                insurance_policy_type='', cost='', notes='')
    form.update(kw)
    return form


# ── display_name ────────────────────────────────────────────────────────────

def test_unit_number_is_the_title_even_with_a_legacy_name():
    unit = _uniq('U')
    eq_id = _equipment(unit_number=unit, name='Nombre viejo', plate_number='ABC-123')
    with app.app_context():
        assert _get(eq_id).display_name == unit


def test_unitless_row_falls_back_to_legacy_name_then_plate_then_id():
    named = _equipment(name='Carretón Azul', plate_number=_uniq('P'))
    plate = _uniq('P')
    plated = _equipment(unit_number='   ', plate_number=plate)
    bare = _equipment()
    with app.app_context():
        assert _get(named).display_name == 'Carretón Azul'
        assert _get(plated).display_name == plate
        assert _get(bare).display_name == f'Equipo #{bare}'


# ── edit form ───────────────────────────────────────────────────────────────

def test_edit_form_has_no_nombre_field_and_never_prints_none(client):
    eq_id = _equipment(unit_number=_uniq('U'))   # every other text column NULL
    html = client.get(f'/equipment/{eq_id}/edit').get_data(as_text=True)
    assert 'name="name"' not in html
    assert 'value="None"' not in html
    assert '>None</textarea>' not in html


def test_new_form_has_no_nombre_field(client):
    html = client.get('/equipment/new').get_data(as_text=True)
    assert 'name="name"' not in html
    assert 'name="unit_number"' in html


def test_changing_the_unit_changes_the_title(client):
    eq_id = _equipment(unit_number=_uniq('U'))
    new_unit = _uniq('N')
    r = client.post(f'/equipment/{eq_id}/edit',
                    data=_edit_form(unit_number=f'  {new_unit} '),
                    follow_redirects=True)
    assert r.status_code == 200
    assert f'<h1>{new_unit}</h1>' in r.get_data(as_text=True)
    with app.app_context():
        eq = _get(eq_id)
        assert eq.unit_number == new_unit          # trimmed
        assert eq.display_name == new_unit
        for col in ('name', 'titular', 'plate_number', 'make', 'model',
                    'vin_serial', 'insurance_company', 'notes'):
            assert getattr(eq, col) != 'None', col


def test_round_tripping_the_rendered_form_stores_no_none(client):
    """Post back exactly what the edit page pre-filled — the original bug path."""
    import re
    eq_id = _equipment(unit_number=_uniq('U'))
    html = client.get(f'/equipment/{eq_id}/edit').get_data(as_text=True)
    form = _edit_form()
    for field in ('titular', 'unit_number', 'plate_number', 'make', 'model',
                  'vin_serial', 'insurance_company'):
        m = re.search(r'<input[^>]*name="%s"[^>]*value="([^"]*)"' % field, html)
        assert m, field
        form[field] = m.group(1)
    client.post(f'/equipment/{eq_id}/edit', data=form)
    with app.app_context():
        eq = _get(eq_id)
        assert 'None' not in (eq.titular, eq.plate_number, eq.make, eq.model,
                              eq.vin_serial, eq.insurance_company)
        assert eq.display_name == eq.unit_number


def test_edit_keeps_a_legacy_name(client):
    eq_id = _equipment(name='Generador Patio', plate_number=_uniq('P'))
    client.post(f'/equipment/{eq_id}/edit', data=_edit_form(model='Generador'))
    with app.app_context():
        eq = _get(eq_id)
        assert eq.name == 'Generador Patio'
        assert eq.display_name == 'Generador Patio'


def test_create_uses_the_unit_as_title_and_ignores_a_posted_name(client):
    unit = _uniq('C')
    r = client.post('/equipment/new',
                    data=_edit_form(unit_number=unit, name='No debe guardarse'),
                    follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        eq = Equipment.query.filter_by(unit_number=unit).one()
        _made.append(eq.id)
        assert not eq.name
        assert eq.display_name == unit


# ── boot repair ─────────────────────────────────────────────────────────────

def test_clear_none_strings_repairs_only_the_literal():
    unit = _uniq('U')
    broken = _equipment(unit_number=unit, name='None', titular='None',
                        vin_serial='None', notes='None')
    real = _equipment(unit_number=_uniq('U'), name='Nonexistente', titular='Juan',
                      notes='None of the above')
    with app.app_context():
        assert _get(broken).display_name == unit   # unit wins even before the repair
        repaired = appmod._clear_none_strings()
        assert repaired.get('equipment.name', 0) >= 1
        db.session.expire_all()
        eq = _get(broken)
        assert (eq.name, eq.titular, eq.vin_serial, eq.notes) == (None, None, None, None)
        assert eq.unit_number == unit
        ok = _get(real)
        assert (ok.name, ok.titular, ok.notes) == ('Nonexistente', 'Juan', 'None of the above')
        assert appmod._clear_none_strings() == {}   # idempotent


def test_unitless_row_stamped_none_recovers_its_fallback_title():
    plate = _uniq('P')
    eq_id = _equipment(unit_number='None', name='None', plate_number=plate)
    with app.app_context():
        appmod._clear_none_strings()
        db.session.expire_all()
        assert _get(eq_id).display_name == plate
