"""Settings for running a management command against the production database.

    python manage.py <command> --settings=backend.prod_settings

`DEBUG = True` is hardcoded in settings.py, and that flag is what chooses SQLite
over Postgres, so there is no way to reach production without either editing
that line or overriding DATABASES here. Overriding here is safer: nothing in the
committed settings changes, so there is no flipped flag to forget to put back.

Credentials come from the same RDS_DB_* environment variables the real
deployment uses, loaded from backend/.env by the load_dotenv() call in
settings.py. They are never written into this file.

This is for deliberate, one-off operations — imports, data fixes, inspection.
Do not use it to run the dev server.
"""
import os

from .settings import *  # noqa: F401,F403

DEBUG = False

_REQUIRED = [
    'RDS_DB_NAME', 'RDS_DB_USER', 'RDS_DB_PASSWORD',
    'RDS_DB_HOST', 'RDS_DB_PORT',
]
_missing = [name for name in _REQUIRED if not os.environ.get(name)]
if _missing:
    raise RuntimeError(
        "Cannot target production: missing environment variables "
        + ", ".join(_missing)
        + ". Set them in backend/.env or in the shell before running."
    )

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ['RDS_DB_NAME'],
        'USER': os.environ['RDS_DB_USER'],
        'PASSWORD': os.environ['RDS_DB_PASSWORD'],
        'HOST': os.environ['RDS_DB_HOST'],
        'PORT': os.environ['RDS_DB_PORT'],
    }
}
