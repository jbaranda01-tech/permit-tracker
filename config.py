import os

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///permits.db')
    # Railway uses postgres:// but SQLAlchemy needs postgresql://
    if SQLALCHEMY_DATABASE_URI and SQLALCHEMY_DATABASE_URI.startswith('postgres://'):
        SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI.replace('postgres://', 'postgresql://', 1)
    # Catch placeholder URLs that haven't been configured
    if SQLALCHEMY_DATABASE_URI and '://user:pass@host:port/' in SQLALCHEMY_DATABASE_URI:
        raise ValueError(
            "DATABASE_URL contains placeholder values from .env.example. "
            "Please set a valid DATABASE_URL environment variable."
        )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'connect_args': {'connect_timeout': 5},
        'pool_pre_ping': True,
    }
    UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads'))
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file upload
    ALERT_DAYS_BEFORE = 30  # Days before expiration to trigger alert
