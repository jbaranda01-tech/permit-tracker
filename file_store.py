"""Uploaded-file helpers shared by app.py and the issues blueprint.

Uploads are stored as ``FileStorage`` blobs in the database. These helpers
live in their own module because ``issues/routes.py`` cannot import from
``app.py`` (circular — ``app.py`` registers the blueprint); ``app.py``
re-imports them so its call sites are unchanged.
"""
import uuid

from werkzeug.utils import secure_filename

from models import db, FileStorage

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'doc', 'docx'}

# Receipt evidence on an issue's fulfilment form: photos (HEIC/WebP included,
# phones produce them) and PDF invoices. No office documents.
RECEIPT_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'heic', 'pdf'}


def _extension(filename):
    return filename.rsplit('.', 1)[1].lower() if '.' in filename else ''


def allowed_file(filename):
    return _extension(filename) in ALLOWED_EXTENSIONS


def allowed_receipt(filename):
    return _extension(filename) in RECEIPT_EXTENSIONS


def save_uploaded_file(file):
    """Save an uploaded file to the database and return the storage filename."""
    filename = f"{uuid.uuid4().hex}_{secure_filename(file.filename)}"
    file_data = file.read()
    stored = FileStorage(
        filename=filename,
        original_filename=file.filename,
        mime_type=file.content_type or 'application/octet-stream',
        data=file_data,
        size=len(file_data),
    )
    db.session.add(stored)
    return filename


def delete_stored_file(filename):
    """Delete a file from the database by filename."""
    stored = FileStorage.query.filter_by(filename=filename).first()
    if stored:
        db.session.delete(stored)
