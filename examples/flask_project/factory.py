"""The application factory.

One function that builds an app and returns it, so the app is not a module
global. That matters here for a reason beyond tidiness: the tests in
`check_it_runs.py` build a second app against pretend networks, in the same
process, and the two must not see each other's client or each other's
database.

    flask --app examples.flask_project run

Flask finds `create_app` by name and calls it with no arguments.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from flask import Flask

from socialchimp import (
    Dispatcher,
    InMemorySeenUpdates,
    SocialChimp,
    Update,
    UpdateKind,
    in_a_thread,
    sync_storage,
)

from . import views
from .config import Settings
from .db import set_up, write_activity
from .login_notes import LoginNotes
from .runtime import EXTENSION_KEY, Chimp
from .storage import SqliteStorage

if TYPE_CHECKING:
    from collections.abc import Mapping

    from socialchimp.platform import Platform


def create_app(
    settings: Settings | None = None,
    platforms: Mapping[str, Platform] | None = None,
) -> Flask:
    """Build the app.

    Args:
        settings: What to run with. Left out, read from the environment.
        platforms: Ready-made platforms, by network name. Anything not named
            here is found among the installed ones and built with no
            arguments, which is what happens in real life. This is where
            `check_it_runs.py` puts its fakes, and it is also where a
            platform that needs settings of its own would go.

    Returns:
        The app, ready to run.
    """
    settings = settings if settings is not None else Settings.from_environment()
    set_up(settings.database)

    store = SqliteStorage(settings.database)
    # The credentials for every network you registered by hand. Written in
    # at startup because they come from the environment and never change
    # while the process runs. Mastodon's are not here: socialchimp writes
    # those itself, through this same storage class, when it registers the
    # app on a server - see views/signin.py.
    for app_credentials in settings.apps.values():
        store.save_app(app_credentials)

    sc = SocialChimp(
        # Five blocking methods, wrapped so the core can await them. Each
        # call runs on a spare thread, so a slow query does not stop
        # everything else the event loop is in the middle of. Django is the
        # one framework where this is the wrong wrapper - it has
        # `contrib.django.orm_storage`, which runs your ORM code back on the
        # request's own thread.
        storage=sync_storage(store),
        platforms=dict(platforms) if platforms is not None else None,
    )

    # TikTok retries for 72 hours and delivers at least once, so the same
    # message arriving twice is normal. Given a memory of what it has
    # already handled, the second copy is dropped rather than acted on.
    dispatcher = Dispatcher(seen=InMemorySeenUpdates())

    async def write_it_down(update: Update) -> None:
        """Keep a line about everything that happens, whatever it is.

        Args:
            update: What happened.
        """
        # This runs on socialchimp's own event loop, so the blocking write
        # goes to a spare thread rather than stopping it. `in_a_thread` is
        # the same helper `sync_storage` uses.
        await in_a_thread(
            lambda: write_activity(
                settings.database,
                what="update",
                platform=update.platform,
                connection_id=update.connection_id,
                detail=f"{update.kind.name} ({update.kind_name})",
            )
        )

    async def they_took_it_back(update: Update) -> None:
        """Forget an account whose owner removed this app.

        Args:
            update: What happened.
        """
        # Its token has already stopped working and Meta will not tell us
        # twice, so there is nothing to keep. Through socialchimp's copy of
        # the storage rather than the blocking one, because we are on the
        # event loop here.
        await sc.storage.delete_connection(update.connection_id)

    dispatcher.on_any(write_it_down)
    dispatcher.on(UpdateKind.CONNECTION_REVOKED, they_took_it_back)

    app = Flask(__name__)
    app.config["SECRET_KEY"] = settings.cookie_phrase
    app.extensions[EXTENSION_KEY] = Chimp(
        sc=sc,
        settings=settings,
        store=store,
        notes=LoginNotes(settings.database),
        dispatcher=dispatcher,
    )

    app.register_blueprint(views.connections)
    app.register_blueprint(views.signin)
    app.register_blueprint(views.posting)
    app.register_blueprint(views.webhooks)

    # There is deliberately no catch-all for SocialChimpError here. Every
    # route that can raise one catches it and does something a person can
    # see - a message on the page, a row in `activity`. socialchimp raises
    # and the app decides; a handler that turned every refusal into the same
    # anonymous 500 would be the app declining to decide.
    return app
