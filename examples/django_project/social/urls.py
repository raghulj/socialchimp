"""The addresses this app answers.

Four of these are the four shapes of a sign-in, and one is the webhook.
`socialchimp.contrib.django.urls()` would mount `connect`, `callback`,
`choose` and `webhooks` for you in four lines, around exactly these calls -
it is worth reading once you have read these.
"""

from __future__ import annotations

from django.urls import path

from . import views, webhooks

app_name = "social"

urlpatterns = [
    path("", views.connections, name="connections"),
    path("compose/", views.compose, name="compose"),
    # Signing in. The address here has to match `SOCIAL_REDIRECT_URI` in the
    # settings and what is typed into each network's developer portal.
    path("social/connect/<str:platform>", views.connect, name="connect"),
    path("social/callback/<str:platform>", views.callback, name="callback"),
    path("social/choose/<str:platform>", views.choose, name="choose"),
    path("social/details/<str:platform>", views.details, name="details"),
    path(
        "social/register-app/<str:platform>",
        views.register_app,
        name="register-app",
    ),
    path(
        "social/disconnect/<path:connection_id>",
        views.disconnect,
        name="disconnect",
    ),
    # The four networks that push. `csrf_exempt` is on the view rather than
    # here, next to the reason for it.
    path("social/webhooks/<str:platform>", webhooks.webhook, name="webhook"),
]
