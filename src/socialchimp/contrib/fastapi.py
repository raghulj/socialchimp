"""Ready-made routes for FastAPI.

    from socialchimp.contrib.fastapi import router

    app.include_router(
        router(
            sc,
            redirect_uri="https://app.example/social/callback/{platform}",
            secrets={"facebook": settings.facebook_app_secret},
            setup_tokens={"facebook": settings.facebook_verify_token},
            deliver=dispatcher.deliver,
        ),
        prefix="/social",
    )

That mounts four addresses, all of them under whatever prefix you give
`include_router`:

    GET       connect/{platform}    start a sign-in
    GET POST  callback/{platform}   the person comes back
    POST      choose/{platform}     they picked which page to use
    GET POST  webhooks/{platform}   the network's setup check, then its updates

Nothing here is the only way in. Every one of these is three lines around a
`Routes` method, and `Routes` is a wrapper around a `SocialChimp` method - so
if you want your own addresses, a login check in front of them, or a reply
that fits your own API, write the route yourself and call the same thing.
See `socialchimp.contrib.shared`.

FastAPI is async all the way down and so is socialchimp, so there is no
bridge here and nothing runs on a thread. The Django and Flask files have
more to say on that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, Response

from socialchimp.contrib.shared import Routes, read_form

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from socialchimp.client import SocialChimp
    from socialchimp.contrib.shared import LoginMemory, Reply
    from socialchimp.events import DeliverUpdate

__all__ = ["router"]


def _answer(reply: Reply) -> Response:
    """Turn what a route decided into a FastAPI response.

    Args:
        reply: What the route decided.

    Returns:
        The response to send.
    """
    return Response(
        content=reply.body,
        status_code=reply.status,
        media_type=reply.content_type,
        headers=dict(reply.headers),
    )


async def _values(request: Request) -> dict[str, str]:
    """Read the query values, and a posted form when there is one.

    The form is read from the raw body rather than through FastAPI's own
    `Form(...)`, which needs another package installed. All three framework
    files read it the same way, so all three behave the same.

    Args:
        request: The request.

    Returns:
        Everything the person sent, by name. A form value wins over a query
        value of the same name.
    """
    values = dict(request.query_params)
    if request.method == "POST":
        values.update(read_form(await request.body()))
    return values


def router(
    sc: SocialChimp,
    *,
    redirect_uri: str,
    memory: LoginMemory | None = None,
    scopes: Mapping[str, Sequence[str]] | None = None,
    secrets: Mapping[str, str] | None = None,
    setup_tokens: Mapping[str, str] | None = None,
    deliver: DeliverUpdate | None = None,
) -> APIRouter:
    """Build the routes for signing in and receiving updates.

    Args:
        sc: The client to work through. Keep one for the life of your
            process, and hand the same one to your own code.
        redirect_uri: Where networks send people back to. `{platform}` in it
            is replaced by the network's name.
        memory: Where a half-finished sign-in waits. Left out, one that
            lives in this process is used - fine to try things out with,
            wrong in production. See `shared.LoginMemory`.
        scopes: Permissions to ask each network for, by network name.
        secrets: The secret each network signs its webhooks with, by network
            name.
        setup_tokens: The token each network's setup check quotes back, by
            network name.
        deliver: Where a webhook's update goes. `Dispatcher.deliver` fits.

    Returns:
        A router to give `app.include_router`.
    """
    routes = Routes(
        sc,
        redirect_uri=redirect_uri,
        memory=memory,
        scopes=scopes,
        secrets=secrets,
        setup_tokens=setup_tokens,
        deliver=deliver,
    )
    api = APIRouter()

    @api.get("/connect/{platform}")
    async def connect(platform: str, request: Request) -> Response:
        """Begin signing someone in to one network."""
        return _answer(await routes.start(platform, dict(request.query_params)))

    @api.api_route("/callback/{platform}", methods=["GET", "POST"])
    async def callback(platform: str, request: Request) -> Response:
        """Carry on after the person comes back from the network."""
        return _answer(await routes.finish(platform, await _values(request)))

    @api.post("/choose/{platform}")
    async def choose(platform: str, request: Request) -> Response:
        """Carry on after the person picked which account to use."""
        return _answer(await routes.choose(platform, await _values(request)))

    @api.get("/webhooks/{platform}")
    async def setup_check(platform: str, request: Request) -> Response:
        """Answer the check a network makes before it will send anything."""
        return _answer(await routes.setup_check(platform, dict(request.query_params)))

    @api.post("/webhooks/{platform}")
    async def webhook(platform: str, request: Request) -> Response:
        """Receive one update a network pushed to us."""
        # `request.body()` is the bytes exactly as they arrived. Never
        # `request.json()` here: a signature is over those exact bytes, and
        # parsing the JSON and building it again changes the spacing and the
        # key order, so the signature no longer matches. That is the single
        # most common reason a correct signature appears to fail.
        body = await request.body()
        return _answer(await routes.webhook(platform, body, dict(request.headers)))

    return api
