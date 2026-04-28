import os

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    SQLALCHEMY_DATABASE_URI = (
        os.environ.get('DATABASE_PRIVATE_URL')
        or os.environ.get('DATABASE_URL')
        or 'sqlite:///permits.db'
    )
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
    # Add connection timeout for PostgreSQL to prevent workers hanging on unreachable DB
    if SQLALCHEMY_DATABASE_URI and SQLALCHEMY_DATABASE_URI.startswith('postgresql://'):
        SQLALCHEMY_ENGINE_OPTIONS = {
            'connect_args': {'connect_timeout': 5},
            'pool_pre_ping': True,
            'pool_recycle': 300,
            'pool_size': 5,
            'max_overflow': 2,
        }
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file upload
    ALERT_DAYS_BEFORE = 30  # Days before expiration to trigger alert

    SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
    SENDGRID_FROM_EMAIL = os.environ.get('SENDGRID_FROM_EMAIL', 'notificaciones@lb-pli-permits.com')
    ENABLE_SCHEDULER = os.environ.get('ENABLE_SCHEDULER', 'false').lower() == 'true'
    NOTIFICATION_DAY = os.environ.get('NOTIFICATION_DAY', 'monday')
    NOTIFICATION_HOUR = int(os.environ.get('NOTIFICATION_HOUR', '12'))
