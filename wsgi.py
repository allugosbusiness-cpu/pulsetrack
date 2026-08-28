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

from django.core.wsgi import get_wsgi_application  # noqa: E402

app = get_wsgi_application()
application = app