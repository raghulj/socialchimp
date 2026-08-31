"""ASGI entry point.

Here so that `uvicorn socialsite.asgi:application` works if you would rather run
it that way. The views do not change: Django adapts a sync view under ASGI,
and `async_to_sync` behaves under both. Under ASGI there is one event loop
for the life of the process, so socialchimp's HTTP clients are shared rather
than made per call, which is the better of the two - but neither is
something the code below has to know about.
"""

from __future__ import annotations

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "socialsite.settings")

application = get_asgi_application()
