"""
WSGI config for the Logistics (server) project.

It exposes the WSGI callable as a module-level variable named ``application``.
Any bootstrapping/table-seeding that used to live here was removed and moved to
dedicated management commands so the WSGI entry point stays clean.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'server.Logistics.settings')

application = get_wsgi_application()