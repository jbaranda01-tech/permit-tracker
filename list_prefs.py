"""Sticky list preferences — each user's filter/sort choices, remembered per list.

The dashboard and the issue queue drive everything off GET params, but nothing
carries those params off the page: the sidebar links are bare, and every
post-edit redirect drops the query string. A user who works one slice of the
data ("Equipos, clase camión, ordenado por próximo a vencer") lost it the
moment they left the screen.

So the normalized choices are stashed in the Flask session and restored on any
request that arrives with no list state of its own. The `nav_args` dict each
context helper already returns IS the set worth storing — normalized, defaults
elided — so remembering is a filter over it and restoring is a merge back into
`request.args`.

Both `app.py` and `issues/routes.py` use this; it lives in its own module
because `issues/routes.py` cannot import from `app.py` (circular — `app.py`
registers the blueprint).
"""

from flask import session
from flask_login import current_user

SESSION_KEY = 'list_prefs'
SESSION_OWNER_KEY = 'list_prefs_uid'

# `search` is deliberately NOT remembered on either list: a sticky search term
# reads as "records are missing" rather than as an active filter. It stays a
# per-visit lookup. A new list param belongs in the matching tuple below, or it
# silently stops surviving navigation.
DASHBOARD_PREF_PARAMS = ('sort', 'company', 'status', 'permit_type',
                         'permit_status', 'exp_month', 'equipment_class')
QUEUE_PREF_PARAMS = ('sort', 'status', 'severity', 'equipment_id', 'category')


def _owner_id():
    """The current user's id, or None when unauthenticated."""
    try:
        if current_user.is_authenticated:
            return str(current_user.get_id())
    except Exception:
        pass
    return None


def _load_prefs():
    """The stored bucket, dropped wholesale when it belongs to another user.

    Preferences deliberately survive logout (same person, same device, next
    morning), so the bucket outlives the login session — hence the explicit
    owner check rather than relying on session teardown.
    """
    owner = _owner_id()
    if owner is None:
        return {}
    if session.get(SESSION_OWNER_KEY) != owner:
        if SESSION_KEY in session or SESSION_OWNER_KEY in session:
            session.pop(SESSION_KEY, None)
            session[SESSION_OWNER_KEY] = owner
        return {}
    return dict(session.get(SESSION_KEY, {}))


def _store_prefs(prefs):
    owner = _owner_id()
    if owner is None:
        return
    session[SESSION_KEY] = prefs
    session[SESSION_OWNER_KEY] = owner
    # Outlive the browser window; config sets PERMANENT_SESSION_LIFETIME.
    session.permanent = True


def restore_list_args(pref_key, args, params):
    """`args`, or a copy with the remembered values merged in.

    Restoring happens ONLY when the request names no list state at all — a
    sidebar click, a post-edit redirect. The moment a request names any param,
    including naming it empty (which is exactly what the auto-submit filter
    form does when a select goes back to "Todas"), the user is steering and
    `args` wins whole. `view` is not a pref param, so the sidebar's
    `?view=equipment` still restores that view's remembered filters.
    """
    if 'search' in args or any(param in args for param in params):
        return args
    stored = _load_prefs().get(pref_key)
    if not stored:
        return args
    merged = args.copy()  # ImmutableMultiDict -> mutable MultiDict
    for key, value in stored.items():
        merged[key] = value
    return merged


def remember_list_args(pref_key, nav_args, params):
    """Persist the pref-worthy slice of a list's `nav_args`.

    Values are coerced to `str` so a restored MultiDict behaves exactly like a
    real query string (`equipment_id` is read back with `type=int`). An empty
    result pops the key, so browsing a list back to its defaults forgets it.
    """
    prefs = _load_prefs()
    remembered = {key: str(value) for key, value in nav_args.items()
                  if key in params and value not in (None, '')}
    if remembered == prefs.get(pref_key):
        return
    if remembered:
        prefs[pref_key] = remembered
    else:
        prefs.pop(pref_key, None)
    _store_prefs(prefs)


def forget_list_args(pref_key):
    """Drop one list's remembered preferences (the "Limpiar filtros" links)."""
    prefs = _load_prefs()
    if prefs.pop(pref_key, None) is not None:
        _store_prefs(prefs)
