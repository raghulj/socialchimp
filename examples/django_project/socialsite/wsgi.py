"""WSGI entry point.

socialchimp is async underneath and this project is ordinary sync Django.
Nothing about that needs ASGI: `asgiref.sync.async_to_sync` bridges the two
inside each view, which is what `social/client.py` explains.
"""

from __future__ import annotations

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "socialsite.settings")

application = get_wsgi_application()
