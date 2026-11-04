"""Shop queue "Camiones / Otros equipos" toggle + reporting on non-truck units.

Run from the repo root:  python3 -m pytest -q tests/test_issue_kinds.py
Boots the real app against a throwaway SQLite DB and logs in as admin.
"""
import os
import pathlib
import sys
import tempfile
import uuid

REPO = pathlib.Path(__file__).resolve().parents[1]
DB = pathlib.Path(tempfile.mkdtemp(prefix='permit-tracker-test-')) / 'test_kinds.db'
os.environ['SECRET_KEY'] = 'test'
os.environ['DATABASE_URL'] = 'sqlite:///' + str(DB)
os.environ.pop('DATABASE_PRIVATE_URL', None)
os.environ['ENABLE_SCHEDULER'] = 'false'
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402
import app as appmod  # noqa: E402
from models import db, Employee, Equipment, Issue, User  # noqa: E402
from issues.routes import (  # noqa: E402
    reportable_equipment_list, reportable_unit_groups, _queue_context, _kind_of,
)

app = appmod.app
app.config['TESTING'] = True

_counter = {'n': 0}


# Suffix with a random token: the three test files share one app process (and
# so one DB) under a full `pytest tests/` run, so a per-file counter alone
# collides with the other files' fixtures on the employees name+company unique.
_RUN = uuid.uuid4().hex[:6]


def _uniq(prefix):
    _counter['n'] += 1
    return f'{prefix}{_RUN}-{_counter["n"]}'


def _ensure_admin():
    from werkzeug.security import generate_password_hash
    with app.app_context():
        if not User.query.filter_by(username='admin').first():
            db.session.add(User(username='admin', email='admin@example.com', role='admin',
                                password_hash=generate_password_hash('admin123', method='pbkdf2:sha256')))
            db.session.commit()


@pytest.fixture(scope='module')
def client():
    _ensure_admin()
    c = app.test_client()
    r = c.post('/login', data={'username': 'admin', 'password': 'admin123'}, follow_redirects=True)
    assert r.status_code == 200
    return c


@pytest.fixture(scope='module')
def fleet():
    """One truck, one chassis, one tank, one generator (LB), each with an open
    issue, plus a driver with a report link."""
    with app.app_context():
        units = {}
        for klass, model in (('truck', 'Camion'), ('chassis', 'Chasis 40'),
                             ('tank', 'Tanque combustible'), ('generator', 'Generador')):
            eq = Equipment(company='LB', unit_number=_uniq('K'), model=model,
                           equipment_class=klass, status='activo')
            db.session.add(eq)
            db.session.flush()
            units[klass] = eq.id
        driver = Employee(name=_uniq('Driver '), company='LB', status='activo',
                          access_token=str(uuid.uuid4()))
        db.session.add(driver)
        db.session.flush()
        issues = {}
        for klass, eq_id in units.items():
            issue = Issue(equipment_id=eq_id, reported_by_employee_id=driver.id,
                          category='otro', severity='alta',
                          description=f'falla en {klass} {eq_id}', current_status='reportado')
            db.session.add(issue)
            db.session.flush()
            issues[klass] = issue.id
        db.session.commit()
        return {'units': units, 'issues': issues, 'token': driver.access_token}


def test_reportable_lists_split_by_kind(fleet):
    with app.app_context():
        trucks = {e.id for e in reportable_equipment_list(kind='trucks')}
        other = {e.id for e in reportable_equipment_list(kind='other')}
        both = {e.id for e in reportable_equipment_list(kind='all')}
        u = fleet['units']
        assert u['truck'] in trucks and u['chassis'] not in trucks
        assert {u['chassis'], u['tank'], u['generator']} <= other
        assert u['truck'] not in other
        assert both >= trucks | other
        # The form groups: Camiones first, then Otros equipos
        labels = [label for label, _ in reportable_unit_groups(company='LB')]
        assert labels == ['Camiones', 'Otros equipos']


def test_queue_defaults_to_trucks_and_toggles(client, fleet):
    i = fleet['issues']
    html = client.get('/issues/').get_data(as_text=True)
    assert f'falla en truck {fleet["units"]["truck"]}' in html
    assert f'falla en chassis {fleet["units"]["chassis"]}' not in html
    assert 'Agregar Camión' in html
    assert 'kind-switch' in html

    html = client.get('/issues/?kind=other').get_data(as_text=True)
    assert f'falla en chassis {fleet["units"]["chassis"]}' in html
    assert f'falla en tank {fleet["units"]["tank"]}' in html
    assert f'falla en truck {fleet["units"]["truck"]}' not in html
    assert 'Agregar Equipo' in html and 'Agregar Camión' not in html
    # the links the toggle view emits keep the user on that side
    assert f'/issues/{i["chassis"]}?kind=other' in html
    assert 'name="kind" value="other"' in html

    # unknown kind silently resets to trucks
    html = client.get('/issues/?kind=bogus').get_data(as_text=True)
    assert f'falla en truck {fleet["units"]["truck"]}' in html


def test_queue_counts_are_scoped_to_the_kind(client, fleet):
    with app.app_context(), app.test_request_context('/issues/?kind=other'):
        from flask import request
        ctx = _queue_context(request.args)
        assert ctx['kind'] == 'other'
        assert ctx['severity_counts']['alta'] == 3        # chassis + tank + generator
        assert ctx['kind_counts'] == {'trucks': 1, 'other': 3}
        assert ctx['nav_args'] == {'kind': 'other'}
    with app.app_context(), app.test_request_context('/issues/'):
        from flask import request
        ctx = _queue_context(request.args)
        assert ctx['severity_counts']['alta'] == 1
        assert ctx['nav_args'] == {}


def test_sticky_prefs_are_per_kind(client, fleet):
    # Filter the "other" queue by severity; the trucks queue must not inherit it.
    client.get('/issues/?kind=other&severity=baja')
    html = client.get('/issues/?kind=other').get_data(as_text=True)
    assert 'stat-tile stat-tile--baja stat-tile--active' in html
    html = client.get('/issues/').get_data(as_text=True)
    assert 'stat-tile stat-tile--baja stat-tile--active' not in html
    # reset forgets only that kind
    r = client.get('/issues/?kind=other&reset=1')
    assert r.status_code == 302 and r.headers['Location'].endswith('/issues/?kind=other')
    html = client.get('/issues/?kind=other').get_data(as_text=True)
    assert 'stat-tile stat-tile--baja stat-tile--active' not in html


def test_detail_nav_walks_the_other_queue(client, fleet):
    i = fleet['issues']
    html = client.get(f'/issues/{i["chassis"]}?kind=other').get_data(as_text=True)
    assert 'detail-nav' in html
    assert 'href="/issues/?kind=other"' in html           # back-link keeps the toggle
    # same issue reached without kind is not in the trucks queue: no arrows
    html = client.get(f'/issues/{i["chassis"]}').get_data(as_text=True)
    assert 'data-prev-url' not in html


def test_truck_profile_back_link_follows_kind(client, fleet):
    u = fleet['units']
    html = client.get(f'/issues/truck/{u["tank"]}').get_data(as_text=True)
    assert 'href="/issues/?kind=other"' in html
    assert 'editar equipo' in html
    html = client.get(f'/issues/truck/{u["truck"]}').get_data(as_text=True)
    assert 'href="/issues/"' in html
    assert 'editar camión' in html
    with app.app_context():
        assert _kind_of(db.session.get(Equipment, u['tank'])) == 'other'


def _assert_unit_search(html, fleet):
    """The type-to-search box renders and each option carries a data-search
    haystack (unit #, class label, …) for initSelectFilters() to match on."""
    assert 'id="equipment_search"' in html
    assert 'data-filter-input="equipment_search"' in html
    with app.app_context():
        chassis = db.session.get(Equipment, fleet['units']['chassis'])
        unit_number = chassis.unit_number
    option = html.split(f'<option value="{fleet["units"]["chassis"]}"', 1)[1].split('</option>', 1)[0]
    assert 'data-search="' in option
    search_attr = option.split('data-search="', 1)[1].split('"', 1)[0]
    assert unit_number in search_attr
    assert 'Chasis' in search_attr


def test_driver_form_groups_units_and_accepts_a_chassis(fleet):
    anon = app.test_client()
    url = f'/issues/report/{fleet["token"]}'
    html = anon.get(url).get_data(as_text=True)
    assert '<optgroup label="Camiones">' in html
    assert '<optgroup label="Otros equipos">' in html
    assert html.index('label="Camiones"') < html.index('label="Otros equipos"')
    _assert_unit_search(html, fleet)

    r = anon.post(url, data={'equipment_id': fleet['units']['chassis'],
                             'category[]': 'llantas', 'severity[]': 'media',
                             'description[]': 'goma del chasis ponchada'})
    assert r.status_code == 302
    with app.app_context():
        issue = Issue.query.filter_by(description='goma del chasis ponchada').one()
        assert issue.equipment_id == fleet['units']['chassis']


def test_manual_form_accepts_other_equipment(client, fleet):
    html = client.get('/issues/new').get_data(as_text=True)
    assert '<optgroup label="Otros equipos">' in html
    _assert_unit_search(html, fleet)
    r = client.post('/issues/new', data={'equipment_id': fleet['units']['generator'],
                                         'category': 'electrico', 'severity': 'alta',
                                         'description': 'generador no arranca'})
    assert r.status_code == 302
    with app.app_context():
        issue = Issue.query.filter_by(description='generador no arranca').one()
        assert issue.equipment_id == fleet['units']['generator']
    html = client.get('/issues/?kind=other').get_data(as_text=True)
    assert 'generador no arranca' in html
