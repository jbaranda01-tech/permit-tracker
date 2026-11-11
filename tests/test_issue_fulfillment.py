"""The optional fulfilment form ("Trabajo Realizado") on an issue report.

Run from the repo root:  python3 -m pytest -q tests/test_issue_fulfillment.py
Boots the real app against a throwaway SQLite DB. Filling the form is gated on
is_shop_manager; every shop user can read it; it never gates a status change.
"""
import io
import os
import pathlib
import sys
import tempfile
import uuid
from decimal import Decimal

REPO = pathlib.Path(__file__).resolve().parents[1]
DB = pathlib.Path(tempfile.mkdtemp(prefix='permit-tracker-test-')) / 'test_fulfillment.db'
os.environ['SECRET_KEY'] = 'test'
os.environ['DATABASE_URL'] = 'sqlite:///' + str(DB)
os.environ.pop('DATABASE_PRIVATE_URL', None)
os.environ['ENABLE_SCHEDULER'] = 'false'
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402
import app as appmod  # noqa: E402
from models import (  # noqa: E402
    db, Equipment, FileStorage, Issue, IssueFulfillment, IssueReceipt,
    IssueStatusHistory, User, UserIssueRole, PROTECTED_FILE_FIELDS,
)

app = appmod.app
app.config['TESTING'] = True

_counter = {'n': 0}
_RUN = uuid.uuid4().hex[:6]
JPEG = b'\xff\xd8\xff\xe0' + b'0' * 64


def _uniq(prefix):
    _counter['n'] += 1
    return f'{prefix}{_RUN}-{_counter["n"]}'


def _ensure_user(username, role, shop=True, password='pw123'):
    from werkzeug.security import generate_password_hash
    with app.app_context():
        if not User.query.filter_by(username=username).first():
            u = User(username=username, email=f'{username}@example.com', role=role,
                     password_hash=generate_password_hash(password, method='pbkdf2:sha256'))
            db.session.add(u)
            db.session.flush()
            if shop:
                db.session.add(UserIssueRole(user_id=u.id, role='shop'))
            db.session.commit()


def _login(username, password):
    c = app.test_client()
    r = c.post('/login', data={'username': username, 'password': password},
               follow_redirects=True)
    assert r.status_code == 200
    return c


@pytest.fixture(scope='module')
def admin():
    _ensure_user('admin', 'admin', shop=False, password='admin123')
    return _login('admin', 'admin123')


@pytest.fixture(scope='module')
def manager():
    name = f'fmgr{_RUN}'
    _ensure_user(name, 'manager')
    return _login(name, 'pw123')


@pytest.fixture(scope='module')
def viewer():
    name = f'fview{_RUN}'
    _ensure_user(name, 'viewer')
    return _login(name, 'pw123')


# Sibling test files assert absolute counts against the shared DB.
_made = {'issues': [], 'equipment': []}


@pytest.fixture(scope='module', autouse=True)
def _cleanup():
    yield
    with app.app_context():
        for iid in _made['issues']:
            issue = db.session.get(Issue, iid)
            if issue is not None:
                db.session.delete(issue)
        db.session.flush()
        for eq_id in _made['equipment']:
            eq = db.session.get(Equipment, eq_id)
            if eq is not None:
                db.session.delete(eq)
        db.session.commit()


def _new_issue(**kw):
    """A truck with one reported issue; returns (issue_id, equipment_id)."""
    with app.app_context():
        eq = Equipment(company='LB', unit_number=_uniq('F'), model='Camion',
                       equipment_class='truck', status='activo')
        db.session.add(eq)
        db.session.flush()
        fields = dict(category='motor', severity='media', description='humo blanco',
                      current_status='reportado')
        fields.update(kw)
        issue = Issue(equipment_id=eq.id, **fields)
        db.session.add(issue)
        db.session.flush()
        db.session.add(IssueStatusHistory(issue_id=issue.id, from_status=None,
                                          to_status=issue.current_status))
        db.session.commit()
        _made['equipment'].append(eq.id)
        _made['issues'].append(issue.id)
        return issue.id, eq.id


def _save(client, issue_id, files=(), **fields):
    data = dict(fields)
    if files:
        data['receipts'] = [(io.BytesIO(body), name) for name, body in files]
    return client.post(f'/issues/{issue_id}/fulfillment', data=data,
                       content_type='multipart/form-data')


def _work(issue_id):
    return IssueFulfillment.query.filter_by(issue_id=issue_id).first()


def test_closing_never_requires_the_form(manager):
    issue_id, _ = _new_issue()
    r = manager.post(f'/issues/{issue_id}/status', data={'new_status': 'cerrado'})
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(Issue, issue_id).current_status == 'cerrado'
        assert _work(issue_id) is None
    page = manager.get(f'/issues/{issue_id}').get_data(as_text=True)
    assert 'Trabajo Realizado' in page and 'Este formulario es opcional' in page


def test_manager_creates_edits_and_uploads(manager):
    issue_id, _ = _new_issue()
    r = _save(manager, issue_id, technician='  Juan Pérez ', work_description='Cambio de bomba',
              man_hours='3.5', parts_cost='1,234.50',
              files=[('a.jpg', JPEG), ('factura.pdf', b'%PDF-1.4 x')])
    assert r.status_code == 302 and r.headers['Location'].endswith('#trabajo-realizado')
    with app.app_context():
        work = _work(issue_id)
        assert work.technician == 'Juan Pérez'
        assert work.man_hours == Decimal('3.50') and work.parts_cost == Decimal('1234.50')
        assert [x.mime_type for x in work.receipts] == ['image/jpeg', 'application/pdf']
        for receipt in work.receipts:
            assert FileStorage.query.filter_by(filename=receipt.storage_filename).count() == 1
        # The form does not touch the issue's status or its timeline.
        assert db.session.get(Issue, issue_id).current_status == 'reportado'
        assert IssueStatusHistory.query.filter_by(issue_id=issue_id).count() == 1

    # Edit: fields replaced, a new receipt is appended to the existing ones.
    r = _save(manager, issue_id, technician='Taller Externo', work_description='',
              man_hours='', parts_cost='20', files=[('b.png', b'\x89PNG' + b'0' * 32)])
    assert r.status_code == 302
    with app.app_context():
        work = _work(issue_id)
        assert work.technician == 'Taller Externo' and work.work_description is None
        assert work.man_hours is None and work.parts_cost == Decimal('20.00')
        assert len(work.receipts) == 3
        assert IssueFulfillment.query.filter_by(issue_id=issue_id).count() == 1

    page = manager.get(f'/issues/{issue_id}').get_data(as_text=True)
    assert 'Taller Externo' in page and '$20.00' in page and 'receipt-grid' in page


def test_decimal_comma_and_nav_next(manager):
    issue_id, _ = _new_issue()
    r = _save(manager, issue_id, man_hours='2,5', next=f'/issues/{issue_id}?equipment_id=1')
    assert r.headers['Location'].endswith(f'/issues/{issue_id}?equipment_id=1#trabajo-realizado')
    with app.app_context():
        assert _work(issue_id).man_hours == Decimal('2.50')
    r = _save(manager, issue_id, man_hours='1', next='//evil.example')
    assert r.headers['Location'].endswith(f'/issues/{issue_id}#trabajo-realizado')


def test_validation_is_all_or_nothing(manager):
    issue_id, _ = _new_issue()
    with app.app_context():
        blobs = FileStorage.query.count()
    for bad in (dict(man_hours='abc'), dict(parts_cost='-5'), dict(man_hours='NaN'),
                dict(), dict(technician='x' * 121)):
        r = _save(manager, issue_id, **bad)
        assert r.status_code == 422, bad
    r = _save(manager, issue_id, technician='Ana', files=[('virus.exe', b'MZ'), ('ok.jpg', JPEG)])
    assert r.status_code == 422
    page = r.get_data(as_text=True)
    assert 'tipo de archivo no permitido' in page and 'value="Ana"' in page
    with app.app_context():
        assert _work(issue_id) is None
        assert FileStorage.query.count() == blobs


def test_viewer_reads_but_cannot_write(manager, viewer):
    issue_id, _ = _new_issue()
    _save(manager, issue_id, technician='Luis', files=[('a.jpg', JPEG)])
    with app.app_context():
        receipt_id = _work(issue_id).receipts[0].id

    page = viewer.get(f'/issues/{issue_id}').get_data(as_text=True)
    assert 'Luis' in page and 'work-panel' not in page and 'Eliminar formulario' not in page
    got = viewer.get(f'/issues/{issue_id}/receipts/{receipt_id}')
    assert got.status_code == 200 and got.data == JPEG and got.mimetype == 'image/jpeg'

    assert _save(viewer, issue_id, technician='Hack').status_code == 302
    assert viewer.post(f'/issues/{issue_id}/fulfillment/delete').status_code == 302
    assert viewer.post(
        f'/issues/{issue_id}/fulfillment/receipts/{receipt_id}/delete').status_code == 302
    with app.app_context():
        work = _work(issue_id)
        assert work.technician == 'Luis' and len(work.receipts) == 1

    anonymous = app.test_client().get(f'/issues/{issue_id}/receipts/{receipt_id}')
    assert anonymous.status_code == 302


def test_receipt_is_scoped_to_its_issue_and_mime_is_not_trusted(manager):
    issue_id, _ = _new_issue()
    other_id, _ = _new_issue()
    r = manager.post(f'/issues/{issue_id}/fulfillment', content_type='multipart/form-data',
                     data={'receipts': (io.BytesIO(b'<script>x</script>'), 'x.jpg', 'text/html')})
    assert r.status_code == 302
    with app.app_context():
        receipt_id = _work(issue_id).receipts[0].id
    assert manager.get(f'/issues/{other_id}/receipts/{receipt_id}').status_code == 404
    assert manager.post(
        f'/issues/{other_id}/fulfillment/receipts/{receipt_id}/delete').status_code == 404
    assert manager.get(f'/issues/{issue_id}/receipts/{receipt_id}').mimetype == 'image/jpeg'


def test_deletes_remove_the_blobs(manager, admin):
    issue_id, _ = _new_issue()
    _save(manager, issue_id, technician='Eva', files=[('a.jpg', JPEG), ('b.jpg', JPEG), ('c.jpg', JPEG)])
    with app.app_context():
        names = [x.storage_filename for x in _work(issue_id).receipts]
        first_id = _work(issue_id).receipts[0].id

    def blobs():
        with app.app_context():
            return FileStorage.query.filter(FileStorage.filename.in_(names)).count()

    assert blobs() == 3
    manager.post(f'/issues/{issue_id}/fulfillment/receipts/{first_id}/delete')
    assert blobs() == 2
    with app.app_context():
        assert len(_work(issue_id).receipts) == 2

    # Deleting the form removes the rest; the issue itself survives.
    manager.post(f'/issues/{issue_id}/fulfillment/delete')
    assert blobs() == 0
    with app.app_context():
        assert _work(issue_id) is None and db.session.get(Issue, issue_id) is not None
        assert IssueReceipt.query.filter(IssueReceipt.storage_filename.in_(names)).count() == 0

    # Hard-deleting an issue takes its form and receipts with it.
    _save(manager, issue_id, technician='Eva', files=[('d.jpg', JPEG)])
    with app.app_context():
        names[:] = [x.storage_filename for x in _work(issue_id).receipts]
    assert blobs() == 1
    assert admin.post(f'/issues/{issue_id}/delete').status_code == 302
    assert blobs() == 0
    with app.app_context():
        assert _work(issue_id) is None


def test_truck_profile_totals_and_excel_sheet(manager, admin):
    from openpyxl import load_workbook
    issue_id, eq_id = _new_issue(current_status='cerrado')
    with app.app_context():
        second = Issue(equipment_id=eq_id, category='frenos', severity='alta',
                       description='pastillas', current_status='reportado')
        db.session.add(second)
        db.session.commit()
        second_id = second.id
        _made['issues'].append(second_id)
    _save(manager, issue_id, technician='Rosa', man_hours='2', parts_cost='100')
    _save(manager, second_id, man_hours='1.5', parts_cost='50.25', files=[('a.jpg', JPEG)])

    page = manager.get(f'/issues/truck/{eq_id}').get_data(as_text=True)
    assert '3.50 h' in page and '$150.25' in page and 'Trabajo registrado' in page

    r = admin.get('/export/excel?type=issues&company=LB')
    wb = load_workbook(io.BytesIO(r.data))
    assert 'Trabajos' in wb.sheetnames
    rows = {row[2]: row for row in wb['Trabajos'].iter_rows(min_row=2, values_only=True)}
    assert rows[issue_id][5] == 'Rosa' and rows[issue_id][7] == 2 and rows[issue_id][8] == 100
    assert rows[second_id][9] == 1          # receipt count
    assert rows[issue_id][4] == 'Cerrado'   # closed issues are included here


def test_receipts_are_import_protected():
    assert PROTECTED_FILE_FIELDS['IssueReceipt'] == ('storage_filename',)
