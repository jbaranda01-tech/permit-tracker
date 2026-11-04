"""Driver link "Mis Permisos" view + token-scoped document route.

Run from the repo root:  python3 -m pytest -q tests/test_driver_permits.py
Boots the real app against a throwaway SQLite DB; the client never logs in.
"""
import os
import pathlib
import sys
import tempfile
import uuid
from datetime import date, datetime, timedelta

REPO = pathlib.Path(__file__).resolve().parents[1]
DB = pathlib.Path(tempfile.mkdtemp(prefix='permit-tracker-test-')) / 'test_driver.db'
os.environ['SECRET_KEY'] = 'test'
os.environ['DATABASE_URL'] = 'sqlite:///' + str(DB)      # absolute path => 4 slashes
os.environ.pop('DATABASE_PRIVATE_URL', None)
os.environ['ENABLE_SCHEDULER'] = 'false'
sys.path.insert(0, str(REPO))

import pytest  # noqa: E402
import app as appmod  # noqa: E402  (boot: create_all + migrations)
from models import db, Employee, EmployeePermit, FileStorage  # noqa: E402

app = appmod.app
app.config['TESTING'] = True

_counter = {'n': 0}


def _uniq(prefix):
    _counter['n'] += 1
    return f'{prefix} {_counter["n"]}'


def _stored_file(name, data=b'%PDF-1.4 test', mime='application/pdf'):
    fs = FileStorage(filename=name, original_filename=name, mime_type=mime, data=data, size=len(data))
    db.session.add(fs)
    return fs


def _driver(company='LB', **kwargs):
    emp = Employee(name=_uniq('Driver'), company=company, status='activo',
                   access_token=str(uuid.uuid4()), **kwargs)
    db.session.add(emp)
    db.session.flush()
    return emp


def _permit(emp, ptype, applicability='YES', **kwargs):
    p = EmployeePermit(employee_id=emp.id, permit_type=ptype, applicability=applicability, **kwargs)
    db.session.add(p)
    db.session.flush()
    return p


@pytest.fixture
def ctx():
    with app.app_context():
        yield
        db.session.rollback()


@pytest.fixture
def client():
    return app.test_client()


def _report_url(emp, **args):
    qs = '&'.join(f'{k}={v}' for k, v in args.items())
    return f'/issues/report/{emp.access_token}' + (f'?{qs}' if qs else '')


def _doc_url(emp, filename):
    return f'/issues/report/{emp.access_token}/document/{filename}'


def test_permits_panel_renders_for_valid_token(ctx, client):
    emp = _driver(license_number='LIC-123', license_expiration=date.today() + timedelta(days=200))
    tomorrow = date.today() + timedelta(days=1)
    own = _stored_file('drv_own_ntsp.pdf')
    na_file = _stored_file('drv_na_twic.pdf')
    _permit(emp, 'NTSP', expiration_date=tomorrow, permit_number='N-1', file_path=own.filename)
    _permit(emp, 'TWIC', applicability='N/A', expiration_date=tomorrow, file_path=na_file.filename)
    db.session.commit()

    resp = client.get(_report_url(emp, view='permits'))
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'Mis Permisos' in html
    assert 'id="tab-permits"' in html
    # permits tab selected, permits panel visible, report panel hidden
    permits_tab = html.split('id="tab-permits"', 1)[1].split('</a>', 1)[0]
    assert 'aria-selected="true"' in permits_tab
    report_panel = html.split('id="panel-report"', 1)[1].split('>', 1)[0]
    assert 'hidden' in report_panel
    permits_panel = html.split('id="panel-permits"', 1)[1].split('>', 1)[0]
    assert 'hidden' not in permits_panel
    # the active permit renders, the N/A one does not
    assert 'NTSP' in html
    assert 'N-1' in html
    assert 'TWIC' not in html
    # only the owned, active file is linked, through the token route
    assert _doc_url(emp, own.filename) in html
    assert na_file.filename not in html
    assert '/uploads/' not in html
    # license card
    assert 'LIC-123' in html
    assert 'Vigente' in html


def test_default_and_invalid_view_show_report_form(ctx, client):
    emp = _driver()
    db.session.commit()
    for url in (_report_url(emp), _report_url(emp, view='bogus')):
        resp = client.get(url)
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        report_panel = html.split('id="panel-report"', 1)[1].split('>', 1)[0]
        assert 'hidden' not in report_panel
        permits_panel = html.split('id="panel-permits"', 1)[1].split('>', 1)[0]
        assert 'hidden' in permits_panel
        report_tab = html.split('id="tab-report"', 1)[1].split('</a>', 1)[0]
        assert 'aria-selected="true"' in report_tab


def test_empty_permits_view_renders_empty_state(ctx, client):
    emp = _driver()
    db.session.commit()
    before = EmployeePermit.query.filter_by(employee_id=emp.id).count()
    resp = client.get(_report_url(emp, view='permits'))
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'No hay permisos registrados' in html
    # an anonymous GET must never backfill permit slots
    assert EmployeePermit.query.filter_by(employee_id=emp.id).count() == before == 0


def test_document_route_serves_own_file(ctx, client):
    emp = _driver()
    own = _stored_file('drv_serve_me.pdf', data=b'%PDF-1.4 own')
    _permit(emp, 'NTSP', expiration_date=date.today(), file_path=own.filename)
    lic = _stored_file('drv_license.jpg', data=b'JPEGDATA', mime='image/jpeg')
    emp.license_file = lic.filename
    db.session.commit()

    resp = client.get(_doc_url(emp, own.filename))
    assert resp.status_code == 200
    assert resp.data == b'%PDF-1.4 own'
    assert resp.mimetype == 'application/pdf'

    resp = client.get(_doc_url(emp, lic.filename))
    assert resp.status_code == 200
    assert resp.data == b'JPEGDATA'
    assert resp.mimetype == 'image/jpeg'

    # the login-only route still redirects an anonymous client
    resp = client.get(f'/uploads/{own.filename}')
    assert resp.status_code == 302
    assert '/login' in resp.headers['Location']


def test_document_route_404s_foreign_na_and_missing_files(ctx, client):
    emp = _driver()
    other = _driver()
    foreign = _stored_file('drv_foreign.pdf')
    _permit(other, 'NTSP', expiration_date=date.today(), file_path=foreign.filename)
    na_file = _stored_file('drv_na_hidden.pdf')
    _permit(emp, 'TWIC', applicability='N/A', file_path=na_file.filename)
    orphan_permit = _permit(emp, 'CERT_MEDICO', expiration_date=date.today(), file_path='drv_no_blob.pdf')
    db.session.commit()

    assert client.get(_doc_url(emp, foreign.filename)).status_code == 404
    assert client.get(_doc_url(emp, na_file.filename)).status_code == 404
    assert client.get(_doc_url(emp, 'does_not_exist.pdf')).status_code == 404
    assert client.get(_doc_url(emp, orphan_permit.file_path)).status_code == 404
    # sanity: the owner of the foreign file can still open it
    assert client.get(_doc_url(other, foreign.filename)).status_code == 200


def test_archived_employee_gets_410_everywhere(ctx, client):
    emp = _driver(archived_at=datetime.utcnow(), archive_reason='baja')
    own = _stored_file('drv_archived.pdf')
    _permit(emp, 'NTSP', expiration_date=date.today(), file_path=own.filename)
    db.session.commit()

    assert client.get(_report_url(emp)).status_code == 410
    assert client.get(_report_url(emp, view='permits')).status_code == 410
    assert client.get(_doc_url(emp, own.filename)).status_code == 410


def test_unknown_token_404s(ctx, client):
    assert client.get('/issues/report/not-a-token?view=permits').status_code == 404
    assert client.get('/issues/report/not-a-token/document/x.pdf').status_code == 404


def test_invalid_submit_rerenders_on_report_panel(ctx, client):
    emp = _driver()
    db.session.commit()
    resp = client.post(_report_url(emp, view='permits'), data={'equipment_id': ''})
    assert resp.status_code == 422
    html = resp.get_data(as_text=True)
    report_panel = html.split('id="panel-report"', 1)[1].split('>', 1)[0]
    assert 'hidden' not in report_panel
    assert 'Debe seleccionar una unidad' in html


def test_success_page_links_to_permits_view(ctx, client):
    emp = _driver()
    db.session.commit()
    resp = client.get(f'/issues/report/{emp.access_token}/success?issue_ids=1')
    assert resp.status_code == 200
    assert _report_url(emp, view='permits') in resp.get_data(as_text=True)


# ── Per-company head branding (home-screen icon, favicon, link preview) ──────

def _head(html):
    return html.split('</head>', 1)[0]


def _assert_company_head(html, company):
    """The driver pages' <head> follows the employee's company, not LB always."""
    head = _head(html)
    prefix, brand = ('img/icon-pli-', 'Professional Logistics') if company == 'PLI' else ('img/icon-', 'LB Caribe Services')
    assert f'rel="apple-touch-icon" sizes="180x180" href="/static/{prefix}180.png"' in head
    assert f'rel="apple-touch-icon" sizes="152x152" href="/static/{prefix}152.png"' in head
    assert f'rel="icon" type="image/png" sizes="192x192" href="/static/{prefix}192.png"' in head
    assert f'property="og:site_name" content="{brand}"' in head
    assert f'property="og:image" content="https://localhost/static/{prefix}512.png"' in head
    assert f' — {brand}</title>' in head
    if company == 'PLI':
        assert 'img/icon-180.png' not in head and 'LB Caribe' not in head
    else:
        assert 'icon-pli-' not in head


def test_pli_driver_pages_carry_pli_icon_and_preview_tags(ctx, client):
    emp = _driver(company='PLI')
    db.session.commit()
    for url in (_report_url(emp), _report_url(emp, view='permits'), _report_url(emp) + '/success?issue_ids=1'):
        html = client.get(url).get_data(as_text=True)
        _assert_company_head(html, 'PLI')
        assert f'property="og:description" content="Reporte de problemas y permisos de {emp.name}"' in _head(html)
    report = _head(client.get(_report_url(emp)).get_data(as_text=True))
    assert 'property="og:title" content="Reportar Problema — Professional Logistics"' in report
    permits = _head(client.get(_report_url(emp, view='permits')).get_data(as_text=True))
    assert 'property="og:title" content="Mis Permisos — Professional Logistics"' in permits


def test_lb_driver_pages_keep_lb_icon(ctx, client):
    emp = _driver(company='LB')
    db.session.commit()
    for url in (_report_url(emp), _report_url(emp) + '/success?issue_ids=1'):
        _assert_company_head(client.get(url).get_data(as_text=True), 'LB')


def test_inactive_link_page_follows_company(ctx, client):
    emp = _driver(company='PLI', archived_at=datetime.utcnow(), archive_reason='baja')
    db.session.commit()
    resp = client.get(_report_url(emp))
    assert resp.status_code == 410
    html = resp.get_data(as_text=True)
    _assert_company_head(html, 'PLI')
    assert 'property="og:title" content="Enlace Inactivo — Professional Logistics"' in _head(html)


def test_staff_pages_untouched_by_driver_head_branding(ctx, client):
    head = _head(client.get('/login').get_data(as_text=True))
    assert 'href="/static/img/icon-180.png"' in head
    assert 'icon-pli-' not in head and 'property="og:' not in head
    assert ' — LB Caribe</title>' in head
