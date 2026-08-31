"""Ready-made routes for Flask.

    from socialchimp.contrib.flask import blueprint

    app.register_blueprint(
        blueprint(
            sc,
            redirect_uri="https://app.example/social/callback/{platform}",
            secrets={"facebook": os.environ["FACEBOOK_APP_SECRET"]},
            setup_tokens={"facebook": os.environ["FACEBOOK_VERIFY_TOKEN"]},
            deliver=dispatcher.deliver,
        ),
        url_prefix="/social",
    )

That mounts four addresses under whatever `url_prefix` you give:

    GET       connect/<platform>    start a sign-in
    GET POST  callback/<platform>   the person comes back
    POST      choose/<platform>     they picked which page to use
    GET POST  webhooks/<platform>   the network's setup check, then its updates

Nothing here is the only way in. Every one of these is three lines around a
`Routes` method, and `Routes` is a wrapper around a `SocialChimp` method - so
your own addresses, a login check in front of them, or a reply shaped to fit
your own API are all a few lines of your own. See
`socialchimp.contrib.shared`.

**Why there is a thread in here.** Flask serves each request on a thread with
no event loop, and socialchimp is async. The obvious fix - `asyncio.run` per
request - is the wrong one: it builds a loop, runs the work and throws the
loop away, and the HTTP connections socialchimp pooled belong to that loop.
The next request finds a pool full of sockets from a loop that no longer
exists. So one loop is started once, on one background thread, and every
request hands its work to that loop and waits for the answer. Connections
stay usable, and the whole bridge is the two functions below. `run` is public,
so your own views can use the same loop rather than starting a second one.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any, TypeVar

from flask import Blueprint, Response, request

from socialchimp.contrib.shared import Routes, read_form

if TYPE_CHECKING:
    from collections.abc import Coroutine, Mapping, Sequence

    from socialchimp.client import SocialChimp
    from socialchimp.contrib.shared import LoginMemory, Reply
    from socialchimp.events import DeliverUpdate

__all__ = ["blueprint", "run"]

T = TypeVar("T")

# The one event loop every Flask request hands its work to. Made on first
# use, because importing this module should not start a thread.
_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None


def _the_loop() -> asyncio.AbstractEventLoop:
    """Return the background loop, starting it the first time.

    Returns:
        The loop. The same one every time, so the HTTP connections
        socialchimp pools stay usable from one request to the next.
    """
    global _loop
    with _lock:
        if _loop is None:
            made = asyncio.new_event_loop()
            # A daemon thread, so this never keeps a process alive that is
            # otherwise finished.
            threading.Thread(
                target=made.run_forever,
                name="socialchimp-loop",
                daemon=True,
            ).start()
            _loop = made
        return _loop


def run(work: Coroutine[Any, Any, T]) -> T:
    """Run one async call from Flask's thread and wait for the answer.

    The routes below use this, and so should your own views - it is the same
    bridge, using the same loop, so the connections socialchimp pools are
    shared with the routes rather than thrown away after every call.

        @app.post("/posts")
        def write():
            account = sc.account(request.form["connection_id"])
            result = run(account.post(Post(text=request.form["text"])))
            return {"id": result.id, "url": result.url}

    Args:
        work: The call to run.

    Returns:
        What it answered.

    Raises:
        Exception: Whatever the call raised, raised again here.
    """
    return asyncio.run_coroutine_threadsafe(work, _the_loop()).result()


def _answer(reply: Reply) -> Response:
    """Turn what a route decided into a Flask response.

    Args:
        reply: What the route decided.

    Returns:
        The response to send.
    """
    return Response(
        reply.body,
        status=reply.status,
        content_type=reply.content_type,
        headers=dict(reply.headers),
    )


def _values() -> dict[str, str]:
    """Read the query values, and a posted form when there is one.

    The form is read out of the raw body rather than through `request.form`,
    so that all three framework files behave identically.

    Returns:
        Everything the person sent, by name. A form value wins over a query
        value of the same name.
    """
    values = request.args.to_dict()
    if request.method == "POST":
        values.update(read_form(request.get_data()))
    return values


def blueprint(
    sc: SocialChimp,
    *,
    redirect_uri: str,
    memory: LoginMemory | None = None,
    scopes: Mapping[str, Sequence[str]] | None = None,
    secrets: Mapping[str, str] | None = None,
    setup_tokens: Mapping[str, str] | None = None,
    deliver: DeliverUpdate | None = None,
    name: str = "socialchimp",
) -> Blueprint:
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
            Giving `secrets` without it is refused, because it would mean
            checking a real update and then dropping it.
        name: What to call the blueprint. Change it if you register two.

    Returns:
        A blueprint to give `app.register_blueprint`.
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
    pages = Blueprint(name, __name__)

    @pages.get("/connect/<platform>")
    def connect(platform: str) -> Response:
        """Begin signing someone in to one network."""
        return _answer(run(routes.start(platform, request.args.to_dict())))

    @pages.route("/callback/<platform>", methods=["GET", "POST"])
    def callback(platform: str) -> Response:
        """Carry on after the person comes back from the network."""
        return _answer(run(routes.finish(platform, _values())))

    @pages.post("/choose/<platform>")
    def choose(platform: str) -> Response:
        """Carry on after the person picked which account to use."""
        return _answer(run(routes.choose(platform, _values())))

    @pages.get("/webhooks/<platform>")
    def setup_check(platform: str) -> Response:
        """Answer the check a network makes before it will send anything."""
        return _answer(run(routes.setup_check(platform, request.args.to_dict())))

    @pages.post("/webhooks/<platform>")
    def webhook(platform: str) -> Response:
        """Receive one update a network pushed to us."""
        # `request.get_data()` is the bytes exactly as they arrived. Never
        # `request.get_json()` here: a signature is over those exact bytes,
        # and parsing the JSON and building it again changes the spacing and
        # the key order, so the signature no longer matches. That is the
        # single most common reason a correct signature appears to fail.
        body = request.get_data()
        headers = dict(request.headers.items())
        return _answer(run(routes.webhook(platform, body, headers)))

    return pages
