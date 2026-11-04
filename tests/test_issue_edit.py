"""Editing and deleting issue reports (fix mistakes after the fact).

Run from the repo root:  python3 -m pytest -q tests/test_issue_edit.py
Boots the real app against a throwaway SQLite DB. Edits are gated on
is_shop_manager (admin, or manager + shop role); delete is admin-only.
"""
import os
import pathlib
import sys
import tempfile
import uuid
from datetime import datetime

REPO = pathlib.Path(__file__).resolve().parents[1]
DB = pathlib.Path(tempfile.mkdtemp(prefix='permit-tracker-test-')) / 'test_edit.db'
os.environ['SECRET_KEY'] = 'test'
os.environ['DATABASE_URL'] = 'sqlite:///' + str(DB)
os.environ.pop('DATABASE_PRIVATE_URL', None)
os.environ['ENABLE_SCHEDULER'] = 'false'
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402
import app as appmod  # noqa: E402
from models import (  # noqa: E402
    db, Equipment, Issue, IssueStatusHistory, User, UserIssueRole, ISSUE_EDIT_NOTE,
)

app = appmod.app
app.config['TESTING'] = True

_counter = {'n': 0}
# The test files share one app process (and DB) under a full `pytest tests/`
# run, so names carry a per-run token on top of the counter.
_RUN = uuid.uuid4().hex[:6]


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
def client():
    """Logged-in admin (same admin/admin123 the sibling test files create)."""
    _ensure_user('admin', 'admin', shop=False, password='admin123')
    return _login('admin', 'admin123')


@pytest.fixture(scope='module')
def shop_manager():
    name = _uniq('mgr')
    _ensure_user(name, 'manager')
    return _login(name, 'pw123')


@pytest.fixture(scope='module')
def shop_viewer():
    name = _uniq('viewer')
    _ensure_user(name, 'viewer')
    return _login(name, 'pw123')


# Rows this file creates, removed at module teardown: the sibling
# test_issue_kinds.py asserts absolute open-issue counts against the shared DB.
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


def _truck(company='LB', **kw):
    eq = Equipment(company=company, unit_number=_uniq('E'), model='Camion',
                   equipment_class='truck', status='activo', **kw)
    db.session.add(eq)
    db.session.flush()
    _made['equipment'].append(eq.id)
    return eq


def _issue(equipment_id, **kw):
    """A reported issue with its creation history row; returns the id."""
    fields = dict(category='motor', severity='media', description='humo blanco',
                  current_status='reportado')
    fields.update(kw)
    issue = Issue(equipment_id=equipment_id, **fields)
    db.session.add(issue)
    db.session.flush()
    db.session.add(IssueStatusHistory(issue_id=issue.id, from_status=None,
                                      to_status=issue.current_status))
    db.session.commit()
    _made['issues'].append(issue.id)
    return issue.id


def _history(issue_id):
    with app.app_context():
        return (IssueStatusHistory.query.filter_by(issue_id=issue_id)
                .order_by(IssueStatusHistory.id).all())


def _edit_rows(issue_id):
    return [h for h in _history(issue_id)
            if h.from_status is not None and h.from_status == h.to_status]


# ── Edit ──────────────────────────────────────────────────────────────

def test_edit_form_prefills_and_hides_create_only_chrome(client):
    with app.app_context():
        iid = _issue(_truck().id, description='pierde aceite por el carter')
    html = client.get(f'/issues/{iid}/edit').get_data(as_text=True)
    assert f'Editar Reporte #{iid}' in html
    assert 'pierde aceite por el carter' in html
    assert 'Guardar Cambios' in html
    assert 'id="save-and-new-btn"' not in html
    assert 'name="status"' not in html        # status is not edited here
    assert 'name="fecha_reporte"' not in html


def test_edit_changes_fields_and_logs_one_audit_row(client):
    with app.app_context():
        iid = _issue(_truck().id)
    before = len(_history(iid))
    r = client.post(f'/issues/{iid}/edit', data={
        'equipment_id': _current_equipment(iid), 'category': 'frenos',
        'severity': 'alta', 'description': 'humo blanco',
    })
    assert r.status_code == 302
    assert r.headers['Location'].endswith(f'/issues/{iid}')
    with app.app_context():
        issue = Issue.query.get(iid)
        assert (issue.category, issue.severity) == ('frenos', 'alta')
        assert issue.current_status == 'reportado'
    rows = _history(iid)
    assert len(rows) == before + 1
    audit = rows[-1]
    assert audit.from_status == audit.to_status == 'reportado'
    assert audit.notes.startswith(ISSUE_EDIT_NOTE)
    assert 'Categoría: Motor → Frenos' in audit.notes
    assert 'Severidad: Media → Alta' in audit.notes
    assert 'Descripción' not in audit.notes
    assert audit.changed_by_user_id is not None
    # The detail page renders the row as "Editado" with the prefix stripped.
    html = client.get(f'/issues/{iid}').get_data(as_text=True)
    assert 'issue-status-badge--edit' in html
    assert 'Categoría: Motor → Frenos' in html


def _current_equipment(issue_id):
    with app.app_context():
        return Issue.query.get(issue_id).equipment_id


def test_edit_moves_issue_to_another_unit(client):
    with app.app_context():
        a, b = _truck(), _truck()
        iid = _issue(a.id)
        b_id, b_name, a_name = b.id, b.display_name, a.display_name
    r = client.post(f'/issues/{iid}/edit', data={
        'equipment_id': b_id, 'category': 'motor', 'severity': 'media',
        'description': 'humo blanco',
    })
    assert r.status_code == 302
    assert _current_equipment(iid) == b_id
    assert f'Unidad: {a_name} → {b_name}' in _edit_rows(iid)[-1].notes


def test_no_change_adds_no_row(client):
    with app.app_context():
        iid = _issue(_truck().id, description='humo blanco  ')   # trailing space
    before = len(_history(iid))
    r = client.post(f'/issues/{iid}/edit', data={
        'equipment_id': _current_equipment(iid), 'category': 'motor',
        'severity': 'media', 'description': 'humo blanco',
    })
    assert r.status_code == 302
    assert len(_history(iid)) == before


def test_invalid_category_is_422_and_saves_nothing(client):
    with app.app_context():
        iid = _issue(_truck().id)
    r = client.post(f'/issues/{iid}/edit', data={
        'equipment_id': _current_equipment(iid), 'category': 'nope',
        'severity': 'alta', 'description': 'otra cosa',
    })
    assert r.status_code == 422
    with app.app_context():
        issue = Issue.query.get(iid)
        assert (issue.category, issue.severity, issue.description) == ('motor', 'media', 'humo blanco')
    assert _edit_rows(iid) == []


def test_archived_unit_stays_editable_but_cannot_be_chosen(client):
    with app.app_context():
        archived = _truck(archived_at=datetime.utcnow(), archive_reason='vendido')
        archived.status = 'inactivo'
        other_archived = _truck(archived_at=datetime.utcnow(), archive_reason='vendido')
        iid = _issue(archived.id)
        archived_id, other_id = archived.id, other_archived.id
    html = client.get(f'/issues/{iid}/edit').get_data(as_text=True)
    assert 'archivado' in html
    # Keeping the current (archived) unit is fine …
    r = client.post(f'/issues/{iid}/edit', data={
        'equipment_id': archived_id, 'category': 'motor', 'severity': 'media',
        'description': 'descripción corregida',
    })
    assert r.status_code == 302
    assert _current_equipment(iid) == archived_id
    assert 'Descripción actualizada' in _edit_rows(iid)[-1].notes
    # … but moving the report onto a *different* unreportable unit is not.
    r = client.post(f'/issues/{iid}/edit', data={
        'equipment_id': other_id, 'category': 'motor', 'severity': 'media',
        'description': 'descripción corregida',
    })
    assert r.status_code == 422
    assert _current_equipment(iid) == archived_id


def test_next_is_honored_only_when_relative(client):
    with app.app_context():
        iid = _issue(_truck().id)
    base = {'equipment_id': _current_equipment(iid), 'category': 'motor', 'severity': 'media'}
    r = client.post(f'/issues/{iid}/edit',
                    data={**base, 'description': 'v2', 'next': f'/issues/{iid}?kind=other'})
    assert r.headers['Location'].endswith(f'/issues/{iid}?kind=other')
    r = client.post(f'/issues/{iid}/edit',
                    data={**base, 'description': 'v3', 'next': 'https://evil.example/'})
    assert r.headers['Location'].endswith(f'/issues/{iid}')


# ── Permissions ───────────────────────────────────────────────────────

def test_shop_manager_can_edit_but_not_delete(shop_manager):
    with app.app_context():
        iid = _issue(_truck().id)
    assert shop_manager.get(f'/issues/{iid}/edit').status_code == 200
    r = shop_manager.post(f'/issues/{iid}/edit', data={
        'equipment_id': _current_equipment(iid), 'category': 'luces',
        'severity': 'baja', 'description': 'humo blanco',
    })
    assert r.status_code == 302
    r = shop_manager.post(f'/issues/{iid}/delete')
    assert r.status_code == 302
    assert r.headers['Location'].rstrip('/').endswith('/issues')
    with app.app_context():
        assert Issue.query.get(iid) is not None


def test_shop_viewer_cannot_edit(shop_viewer):
    with app.app_context():
        iid = _issue(_truck().id)
    r = shop_viewer.get(f'/issues/{iid}/edit')
    assert r.status_code == 302
    r = shop_viewer.post(f'/issues/{iid}/edit', data={
        'equipment_id': _current_equipment(iid), 'category': 'luces',
        'severity': 'baja', 'description': 'hack',
    })
    assert r.status_code == 302
    with app.app_context():
        assert Issue.query.get(iid).description == 'humo blanco'


# ── Delete ────────────────────────────────────────────────────────────

def test_admin_delete_removes_issue_history_and_unlinks_children(client):
    with app.app_context():
        eq = _truck()
        parent = _issue(eq.id)
        child = _issue(eq.id, parent_issue_id=parent, description='seguimiento')
        db.session.add(IssueStatusHistory(issue_id=parent, from_status='reportado',
                                          to_status='en_proceso'))
        db.session.commit()
        eq_id = eq.id
    assert len(_history(parent)) == 2
    r = client.post(f'/issues/{parent}/delete')
    assert r.status_code == 302
    assert r.headers['Location'].endswith(f'/issues/truck/{eq_id}')
    with app.app_context():
        assert Issue.query.get(parent) is None
        assert IssueStatusHistory.query.filter_by(issue_id=parent).count() == 0
        assert Issue.query.get(child).parent_issue_id is None
    assert client.get(f'/issues/{parent}').status_code == 404


# ── Boot migration keeps the audit rows ───────────────────────────────

def test_status_migration_preserves_edit_rows(client):
    with app.app_context():
        iid = _issue(_truck().id)
    client.post(f'/issues/{iid}/edit', data={
        'equipment_id': _current_equipment(iid), 'category': 'motor',
        'severity': 'critica', 'description': 'humo blanco',
    })
    with app.app_context():
        # A legacy redundant transition — exactly what the migration exists to drop.
        db.session.add(IssueStatusHistory(issue_id=iid, from_status='en_proceso',
                                          to_status='en_proceso', notes=None))
        db.session.commit()
        assert len(_edit_rows(iid)) == 2

        counts = appmod._migrate_issue_statuses(dry_run=True)
        assert counts.get('issue_status_history transiciones redundantes eliminadas') == 1
        assert len(_edit_rows(iid)) == 2            # dry run leaves both

        counts = appmod._migrate_issue_statuses()
        assert counts.get('issue_status_history transiciones redundantes eliminadas') == 1
        rows = _edit_rows(iid)
        assert len(rows) == 1
        assert rows[0].notes.startswith(ISSUE_EDIT_NOTE)
        assert rows[0].to_status == 'reportado'
