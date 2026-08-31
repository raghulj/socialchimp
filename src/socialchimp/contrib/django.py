"""Ready-made routes for Django, and a way to write storage as ORM code.

In your `urls.py`:

    from django.urls import include, path
    from socialchimp.contrib.django import get_client, urls

    social = urls(
        get_client(),
        redirect_uri="https://app.example/social/callback/{platform}",
        secrets={"facebook": os.environ["FACEBOOK_APP_SECRET"]},
        setup_tokens={"facebook": os.environ["FACEBOOK_VERIFY_TOKEN"]},
        deliver=dispatcher.deliver,
    )

    urlpatterns = [path("social/", include(social))]

That mounts four addresses under whatever prefix you gave `include`:

    GET       connect/<platform>    start a sign-in
    GET POST  callback/<platform>   the person comes back
    POST      choose/<platform>     they picked which page to use
    GET POST  webhooks/<platform>   the network's setup check, then its updates

Nothing here is the only way in. Every one of these is three lines around a
`Routes` method, and `Routes` is a wrapper around a `SocialChimp` method - so
your own addresses, `@login_required` in front of them, or a reply shaped to
fit your own views are all a few lines of your own. See
`socialchimp.contrib.shared`.

**socialchimp adds no models and no migrations.** It never has, on any
framework, and that is the reason the same library works on three of them.
Your tables stay yours: you write five methods, and `orm_storage` below lets
you write them as ordinary synchronous Django ORM code.

**Why there is a bridge in here.** socialchimp is async and most Django apps
are still sync WSGI, and nobody should have to move to ASGI to post a
picture. So the views below are ordinary sync views, and `asgiref.sync.
async_to_sync` runs the async call for them. Under ASGI the same views still
work - Django adapts a sync view either way - so this is one bridge, in one
place, whichever way you serve.

One thing to know about that bridge under WSGI: `async_to_sync` runs each
call on a fresh event loop, so an HTTP client socialchimp kept from an
earlier request belongs to a loop that has gone. Platforms open and close
their own client per call, so signing in and posting are unaffected; the one
that is not is `account.direct`, which reuses a client between calls. Give
`SocialChimp` an `http` client of your own, or make those calls from a
worker, or serve under ASGI, where there is one loop and none of this
applies.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from importlib import import_module
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast

from asgiref.sync import async_to_sync, sync_to_async

from socialchimp.client import SocialChimp
from socialchimp.contrib.shared import Routes, read_form, sync_storage
from socialchimp.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence

    from socialchimp.contrib.shared import LoginMemory, Reply, SyncStorage
    from socialchimp.events import DeliverUpdate
    from socialchimp.storage import Storage

__all__ = ["Request", "get_client", "orm_storage", "urls"]

T = TypeVar("T")

# The setting these routes read, and the two keys it may hold.
_SETTING = "SOCIALCHIMP"
_ASYNC_KEY = "STORAGE"
_SYNC_KEY = "SYNC_STORAGE"

# What `path()` hands back. Django's own `URLPattern` has no published type,
# and nothing here needs to know more about one than "the thing that goes in
# urlpatterns".
UrlPattern = object

# What a view hands back. Same reasoning: we build it with `HttpResponse` and
# give it straight to Django, and never look inside it.
Response = object


class Request(Protocol):
    """The little of Django's request these routes read.

    Written down here because Django ships no type information, and because
    it is a short and useful list: the method, the raw body, the query
    values and the headers. A real `HttpRequest` has all four.

    Attributes:
        method: `"GET"`, `"POST"` and so on.
        body: The request body, exactly as it arrived. This is the one that
            matters for webhooks.
        GET: The query values.
        headers: The request headers.
    """

    method: str
    body: bytes
    GET: Mapping[str, str]
    headers: Mapping[str, str]


class View(Protocol):
    """One of the views below, as Django will call it."""

    def __call__(self, request: Request, *, platform: str) -> Response:
        """Answer one request.

        Args:
            request: The request.
            platform: Which network, from the address.

        Returns:
            The response.
        """
        ...


class MakeResponse(Protocol):
    """Django's `HttpResponse`, as much of it as we use."""

    def __call__(
        self,
        content: bytes,
        *,
        status: int,
        content_type: str,
        headers: Mapping[str, str],
    ) -> Response:
        """Build a response.

        Args:
            content: The body.
            status: The status code.
            content_type: What the body is.
            headers: Anything else to send.

        Returns:
            The response.
        """
        ...


class MakePath(Protocol):
    """Django's `path`, as much of it as we use."""

    def __call__(self, route: str, view: View, *, name: str) -> UrlPattern:
        """Point one address at one view.

        Args:
            route: The address, relative to wherever it is included.
            view: What answers it.
            name: What to call it, for `reverse`.

        Returns:
            The pattern to put in urlpatterns.
        """
        ...


class Exempt(Protocol):
    """Django's `csrf_exempt`."""

    def __call__(self, view: View) -> View:
        """Let a view be posted to without one of Django's CSRF tokens.

        Args:
            view: The view to exempt.

        Returns:
            The same view, exempted.
        """
        ...


class Settings(Protocol):
    """Django's settings, which we only ever read one name out of."""

    def __getattr__(self, name: str) -> object:
        """Read one setting.

        Args:
            name: Which setting.

        Returns:
            Its value, whatever the app put there.
        """
        ...


@dataclass(frozen=True, slots=True)
class _Django:
    """The four pieces of Django these routes use."""

    response: MakeResponse
    path: MakePath
    csrf_exempt: Exempt
    settings: Settings


def _django() -> _Django:
    """Fetch the pieces of Django we use, typed.

    Django ships no type information - no `py.typed`, no bundled stubs - so
    `from django.http import HttpResponse` is an import mypy cannot follow,
    and silencing that with an ignore would leave everything downstream of it
    unchecked as well. So the four things we use are written down above as
    the shapes we expect and fetched by name here. Everything past this
    function is then checked properly, and the shapes themselves are checked
    by the tests, which run against real Django.

    Fetched here rather than imported at the top of the file, so that
    importing socialchimp never imports Django.

    Returns:
        The pieces.
    """
    return _Django(
        response=cast("MakeResponse", import_module("django.http").HttpResponse),
        path=cast("MakePath", import_module("django.urls").path),
        csrf_exempt=cast(
            "Exempt",
            import_module("django.views.decorators.csrf").csrf_exempt,
        ),
        settings=cast("Settings", import_module("django.conf").settings),
    )


async def _await(work: Coroutine[Any, Any, Reply]) -> Reply:
    """Wait for one async call and hand back what it answered.

    Args:
        work: The call to run.

    Returns:
        What it answered.
    """
    return await work


# The whole sync/async bridge, in one place: a sync view calls this, and the
# async work runs. Nothing else in this file knows the difference.
_run = async_to_sync(_await)


async def _on_the_request_thread(work: Callable[[], T]) -> T:
    """Run one blocking storage call where the Django ORM expects to be.

    Args:
        work: The storage method, already given its arguments.

    Returns:
        Whatever it returned.
    """
    # thread_sensitive=True is the whole point. Django keeps one database
    # connection per thread, and a transaction belongs to the thread that
    # opened it - so ORM code run on some spare pool thread gets a second
    # connection, outside the request's own transaction. It sees writes the
    # request has not committed as missing, its own writes land in a
    # transaction nobody rolls back with the request, and if the request
    # holds a row lock the two can sit and wait for each other. With this on,
    # asgiref hands the work back to the thread the request arrived on.
    return await sync_to_async(work, thread_sensitive=True)()


def orm_storage(inner: SyncStorage) -> Storage:
    """Let socialchimp use storage you wrote as ordinary Django ORM code.

    Write the five methods with `Model.objects.get(...)` and `.save()`, the
    way you write everything else, and hand the class here.

    Example:
        class MyStorage:
            def get_connection(self, connection_id):
                row = SocialAccount.objects.filter(pk=connection_id).first()
                return row.to_connection() if row else None
            ...

        sc = SocialChimp(storage=orm_storage(MyStorage()))

    Args:
        inner: Your storage class. Five methods, none of them async.

    Returns:
        A `Storage` to hand to `SocialChimp`.
    """
    return sync_storage(inner, run=_on_the_request_thread)


def _class_at(path: str) -> type[object]:
    """Find a class by its dotted path.

    Args:
        path: Something like `"myapp.social.MyStorage"`.

    Returns:
        The class.

    Raises:
        ConfigError: If the path has no dot in it, or nothing is there.
    """
    module_name, _, class_name = path.rpartition(".")
    if not module_name:
        message = (
            f"{path!r} is not a dotted path to a class. It should look like "
            f"'myapp.social.MyStorage'."
        )
        raise ConfigError(message)

    try:
        found = getattr(import_module(module_name), class_name)
    except (ImportError, AttributeError) as problem:
        message = (
            f"There is no class at {path!r}, so settings.{_SETTING} names "
            f"something that does not exist. {problem}"
        )
        raise ConfigError(message) from problem

    return cast("type[object]", found)


def _storage_from_settings(settings: Settings) -> Storage:
    """Build the storage your settings name.

    Args:
        settings: Django's settings.

    Returns:
        The storage, wrapped for the ORM when it was written as blocking
        code.

    Raises:
        ConfigError: If the setting is missing, the wrong shape, or names
            neither or both kinds of storage.
    """
    named = getattr(settings, _SETTING, None)
    if not isinstance(named, Mapping):
        message = (
            f"settings.{_SETTING} should be a dict saying where your storage "
            f"class is, such as {{'{_SYNC_KEY}': 'myapp.social.MyStorage'}}. "
            f"Add it to your settings file."
        )
        raise ConfigError(message)

    written_async = named.get(_ASYNC_KEY)
    written_sync = named.get(_SYNC_KEY)
    if (written_async is None) == (written_sync is None):
        message = (
            f"settings.{_SETTING} should name exactly one of {_ASYNC_KEY} - "
            f"a storage class whose five methods are async - or {_SYNC_KEY}, "
            f"one written as ordinary Django ORM code. It names "
            f"{'both' if written_async is not None else 'neither'}."
        )
        raise ConfigError(message)

    if written_async is not None:
        return cast("Storage", _class_at(str(written_async))())
    return orm_storage(cast("SyncStorage", _class_at(str(written_sync))()))


@cache
def get_client() -> SocialChimp:
    """Return the one `SocialChimp` for this process, built from settings.

    Reads `settings.SOCIALCHIMP`, which names your storage class and says
    which sort it is:

        SOCIALCHIMP = {"SYNC_STORAGE": "myapp.social.MyStorage"}

    Use `SYNC_STORAGE` for a class written as ordinary Django ORM code -
    which is what you want unless you have gone out of your way - and
    `STORAGE` for one whose five methods are already async. Exactly one of
    them, because guessing which you meant is the sort of thing that works
    until it does not.

    The client is built once and kept, because the locks that stop two
    workers renewing the same token at once live on it. Call
    `get_client.cache_clear()` if you really need a new one.

    Returns:
        The client.

    Raises:
        ConfigError: If the setting is missing, the wrong shape, or names a
            class that is not there.
    """
    return SocialChimp(storage=_storage_from_settings(_django().settings))


def _answer(make: MakeResponse, reply: Reply) -> Response:
    """Turn what a route decided into a Django response.

    Args:
        make: Django's `HttpResponse`.
        reply: What the route decided.

    Returns:
        The response to send.
    """
    return make(
        reply.body,
        status=reply.status,
        content_type=reply.content_type,
        headers=dict(reply.headers),
    )


def _query(request: Request) -> dict[str, str]:
    """Read the query values off a request.

    Args:
        request: The request.

    Returns:
        The values, by name.
    """
    # Through `.items()` rather than `dict(request.GET)`. A QueryDict is a
    # dict underneath, holding a list per name, so copying it directly hands
    # you lists where you expected strings.
    return dict(request.GET.items())


def _values(request: Request) -> dict[str, str]:
    """Read the query values, and a posted form when there is one.

    The form is read out of the raw body rather than through `request.POST`,
    so that all three framework files behave identically.

    Args:
        request: The request.

    Returns:
        Everything the person sent, by name. A form value wins over a query
        value of the same name.
    """
    values = _query(request)
    if request.method == "POST":
        values.update(read_form(request.body))
    return values


def urls(
    sc: SocialChimp,
    *,
    redirect_uri: str,
    memory: LoginMemory | None = None,
    scopes: Mapping[str, Sequence[str]] | None = None,
    secrets: Mapping[str, str] | None = None,
    setup_tokens: Mapping[str, str] | None = None,
    deliver: DeliverUpdate | None = None,
) -> list[UrlPattern]:
    """Build the routes for signing in and receiving updates.

    Args:
        sc: The client to work through. `get_client()` builds one from your
            settings and keeps it.
        redirect_uri: Where networks send people back to. `{platform}` in it
            is replaced by the network's name.
        memory: Where a half-finished sign-in waits. Left out, one that
            lives in this process is used - fine to try things out with,
            wrong in production, because two workers do not share it. See
            `shared.LoginMemory`.
        scopes: Permissions to ask each network for, by network name.
        secrets: The secret each network signs its webhooks with, by network
            name.
        setup_tokens: The token each network's setup check quotes back, by
            network name.
        deliver: Where a webhook's update goes. `Dispatcher.deliver` fits.

    Returns:
        Patterns to give `include()`, each with a name beginning
        `socialchimp-`.
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
    pieces = _django()

    def connect(request: Request, *, platform: str) -> Response:
        """Begin signing someone in to one network."""
        return _answer(pieces.response, _run(routes.start(platform, _query(request))))

    def callback(request: Request, *, platform: str) -> Response:
        """Carry on after the person comes back from the network."""
        return _answer(pieces.response, _run(routes.finish(platform, _values(request))))

    def choose(request: Request, *, platform: str) -> Response:
        """Carry on after the person picked which account to use."""
        return _answer(pieces.response, _run(routes.choose(platform, _values(request))))

    def webhook(request: Request, *, platform: str) -> Response:
        """Answer a network's setup check, or receive an update from it."""
        if request.method == "GET":
            decided = _run(routes.setup_check(platform, _query(request)))
        else:
            # `request.body` is the bytes exactly as they arrived. Never
            # `json.loads` first: a signature is over those exact bytes, and
            # parsing the JSON and building it again changes the spacing and
            # the key order, so the signature no longer matches. That is the
            # single most common reason a correct signature appears to fail.
            decided = _run(
                routes.webhook(platform, request.body, dict(request.headers.items()))
            )
        return _answer(pieces.response, decided)

    return [
        pieces.path("connect/<str:platform>", connect, name="socialchimp-connect"),
        pieces.path("callback/<str:platform>", callback, name="socialchimp-callback"),
        pieces.path("choose/<str:platform>", choose, name="socialchimp-choose"),
        # Only the webhook is exempted. A social network has no way to send
        # one of Django's CSRF tokens, so a protected webhook answers 403 to
        # everything and the network eventually stops trying. The other three
        # are posted to by your own pages, so they keep Django's protection -
        # put {% csrf_token %} in those forms as usual.
        pieces.path(
            "webhooks/<str:platform>",
            pieces.csrf_exempt(webhook),
            name="socialchimp-webhook",
        ),
    ]
