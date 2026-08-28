"""
Convenience alias so Render's default command `gunicorn app:app` imports the Django
WSGI application from the repo root. The real app object lives in `wsgi.py`.
"""
from wsgi import app  # noqa: F401

# `app` is the WSGI callable Render will serve.
application = app
