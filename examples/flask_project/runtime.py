"""The one `SocialChimp` this process has, and how a view reaches it.

**One client for the whole process, not one per request.** The locks that
stop two workers renewing the same token at the same moment live on the
client, so a new one per request protects nothing. It also keeps the pool of
HTTP connections, which is the other half of the reason.

It hangs off `app.extensions`, which is where Flask extensions put their
state, so the application factory can build a second app - a test one, say,
against fake networks - without either app seeing the other's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from flask import current_app

if TYPE_CHECKING:
    from socialchimp import Dispatcher, SocialChimp

    from .config import Settings
    from .login_notes import LoginNotes
    from .storage import SqliteStorage

EXTENSION_KEY = "socialchimp"
"""What this app's state is filed under in `app.extensions`."""


@dataclass(frozen=True, slots=True)
class Chimp:
    """Everything a view needs, built once when the app is built.

    Attributes:
        sc: The client. One per process - see the note at the top.
        settings: What the environment said.
        store: The sqlite storage, for the two things socialchimp does not
            do: listing connections, and reading one without renewing its
            token.
        notes: Where a half-finished sign-in waits.
        dispatcher: Where a webhook's updates are handed on to.
    """

    sc: SocialChimp
    settings: Settings
    store: SqliteStorage
    notes: LoginNotes
    dispatcher: Dispatcher


def chimp() -> Chimp:
    """Return this app's state, from inside a request.

    Returns:
        What `create_app` built.

    Raises:
        RuntimeError: If the app was not built by `create_app`. The
            isinstance check is not defensive programming for its own sake -
            `app.extensions` is a plain dict of anything, so this is the
            line that lets the rest of the app be typed without a cast.
    """
    found = current_app.extensions.get(EXTENSION_KEY)
    if not isinstance(found, Chimp):
        message = (
            "This app was not built by flask_project.create_app, so there "
            "is no socialchimp client on it."
        )
        raise RuntimeError(message)
    return found
