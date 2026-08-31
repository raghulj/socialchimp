"""Every address in the project.

The social routes are mounted under `/social/` so that
`SOCIAL_REDIRECT_URI` in the settings lines up with them.

`socialchimp.contrib.django.urls()` would give you `connect`, `callback`,
`choose` and `webhooks` ready-made, in four lines. They are written out in
`social/views.py` and `social/webhooks.py` instead, because this project is
here to be read - and because a real app wants its own pages and its own
error messages rather than the JSON those return.
"""

from __future__ import annotations

from django.urls import include, path

urlpatterns = [
    path("", include("social.urls")),
]
