from functools import wraps
from flask import flash, redirect, url_for
from flask_login import login_required, current_user
from models import UserIssueRole


def _has_issue_role(user, role):
    if user.role == 'admin':
        return True
    return UserIssueRole.query.filter_by(user_id=user.id, role=role).first() is not None


def _has_any_issue_role(user):
    if user.role == 'admin':
        return True
    return UserIssueRole.query.filter_by(user_id=user.id).first() is not None


def shop_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not _has_issue_role(current_user, 'shop'):
            flash('No tiene permisos para acceder al taller.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


def shop_or_office_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not (_has_issue_role(current_user, 'shop') or _has_issue_role(current_user, 'office')):
            flash('No tiene permisos para esta sección.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated
