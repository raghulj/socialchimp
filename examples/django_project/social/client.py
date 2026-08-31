"""The one `SocialChimp` for this process, and the bridge into it.

`socialchimp.contrib.django.get_client()` reads `settings.SOCIALCHIMP`,
finds `social.storage.ConnectionStorage`, wraps it in `orm_storage` and
builds the client. It caches the result, which is the important part: the
locks that stop two workers renewing the same token at the same moment live
on the client, so a fresh one per request protects nothing.

**Why `async_to_sync` and not `async def` views.** socialchimp is async and
this project is ordinary sync WSGI Django, and nobody should have to move a
whole application to ASGI to post a picture. `asgiref.sync.async_to_sync`
runs one async call from a sync view and hands back what it returned. The
views below do that at exactly the points where socialchimp is called, and
nowhere else, so the rest of the code is Django the way you already write
it.

Under WSGI each call gets its own event loop, and an HTTP client holds
sockets belonging to the loop that made it. socialchimp knows: it keeps one
client per loop as well as per network, and lets go of a client when its
loop finishes. Under ASGI there is one loop for the life of the process and
one client is shared. Neither is anything this app has to do something
about.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from socialchimp.contrib.django import get_client

if TYPE_CHECKING:
    from socialchimp import SocialChimp

__all__ = ["client"]


def client() -> SocialChimp:
    """Return the client every view uses.

    A one-line wrapper so that the rest of this app imports one name from
    one place, and so there is somewhere obvious to hand `SocialChimp` a
    shared lock the day this runs on more than one worker.

    Returns:
        The client.
    """
    return get_client()
