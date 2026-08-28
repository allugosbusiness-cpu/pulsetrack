"""
Root-level WSGI entry point for Render / gunicorn.

Render's Python web services default to `gunicorn app:app` at the repo root unless a
custom start command is set. So the Django backend must be importable from the repo
root. This module exposes a WSGI `app` that loads `server.Logistics.settings`, making
`gunicorn app:app` (or `gunicorn wsgi:app`) work with no `cd server` required.
"""

import os
import sys
from pathlib import Path

# Ensure the repo root is importable so `server` (and everything under it) resolves.
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'server.Logistics.settings')

import django  # noqa: E402

django.setup()

# Create/update the database schema automatically at boot. This ensures a fresh
# database (e.g. a newly-attached Render Postgres) gets all tables without needing
# a separate release/migrate step. It's idempotent and safe.
try:
    from django.core.management import call_command
    call_command('migrate', verbosity=0, interactive=False, no_input=True)
except Exception as _exc:  # pragma: no cover - never block boot on migration issues
    import logging
    logging.getLogger(__name__).warning('Startup migrate skipped: %s', _exc)

from django.core.wsgi import get_wsgi_application  # noqa: E402

app = get_wsgi_application()
application = app