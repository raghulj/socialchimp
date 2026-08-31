"""Checks that your platform behaves like the others.

Anyone can publish a package that adds a network to socialchimp without a
line of it landing in this repository. That only works if the platform inside
behaves the way the built-in ones do, and the only honest way to know is to
run the same checks against it. Otherwise every third-party platform is a
guess, and the guesses turn up as bugs in other people's apps.

So subclass `PlatformChecks` in a test file of your own, say how to build
your platform, and you inherit the lot:

    from socialchimp.testing import PlatformChecks

    class TestMyPlatform(PlatformChecks):
        def make_platform(self) -> Platform:
            return MyPlatform(transport=self.transport)

The names matter. pytest collects classes called `Test...` and leaves every
other class alone, so your subclass is collected and `PlatformChecks` itself
is not. That is the whole reason the base is named `PlatformChecks`. Call
your own subclass `TestSomething` or pytest will quietly run nothing.

Three doubles come with it, for tests of your own rather than of a platform:

- `FakePlatform` - a platform that works without a network, with enough
  knobs to make it behave badly on purpose.
- `RecordingStorage` - a `Storage` that stores things properly and remembers
  every call your code made.
- `RecordingTransport` - answers httpx requests from a table of replies and
  keeps each request it was given.

This lives behind an extra, because pytest is not something an app should
have to install to post a picture:

    pip install "socialchimp[testing]"
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from functools import cached_property
from typing import TYPE_CHECKING, Final

import httpx
import pytest

from socialchimp.errors import (
    InvalidPostError,
    NotFoundError,
    NotSupportedError,
    SocialChimpError,
)
from socialchimp.events import Update, verify_hmac_sha256
from socialchimp.features import Feature, Limits, check_post
from socialchimp.http import HttpClient
from socialchimp.models import (
    AppCredentials,
    Connection,
    Media,
    Post,
    PostResult,
    Token,
)
from socialchimp.platform import (
    AccountChoice,
    CanCheckSignature,
    CanCreateApp,
    CanDeletePosts,
    CanReadUpdates,
    ChooseAccount,
    Finished,
    LoginRequest,
    LoginStep,
    Platform,
    SendToNetwork,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from socialchimp.models import RawData

__all__ = [
    "FakePlatform",
    "PlatformChecks",
    "RecordingStorage",
    "RecordingTransport",
    "StorageCall",
]

# The methods every platform provides. `registry` keeps the same short list
# for its own early warning, privately - copying five names is cheaper than
# two modules reaching into each other.
_MUST_HAVE: Final = ("limits", "start_login", "finish_login", "refresh", "publish")

# A platform that cannot post anything is not a platform anyone can use.
_WAYS_TO_POST: Final = Feature.POST_TEXT | Feature.POST_IMAGE | Feature.POST_VIDEO

# What an entry point name may look like: lowercase, no spaces, nothing that
# would have to be quoted in a `pyproject.toml` or typed carefully in a shell.
_ENTRY_POINT_NAME: Final = re.compile(r"^[a-z][a-z0-9_-]*$")

# The header `FakePlatform` signs with. Real networks all pick their own.
_FAKE_SIGNATURE_HEADER: Final = "X-Fake-Signature"

# Stand-in values for the fake. None of these reaches a network, and none
# of them is a secret. They are named rather than written where they are
# used so the linter's hunt for real ones stays worth listening to.
_FAKE_SIGNING_KEY: Final = "fake-secret"
_FAKE_CLIENT_KEY: Final = "fake-client-secret"
_FAKE_ACCESS: Final = "fake-access"
_FAKE_REFRESH: Final = "fake-refresh"
_FAKE_NEW_ACCESS: Final = "fake-access-after-refresh"
_FAKE_NEW_REFRESH: Final = "fake-refresh-after-refresh"
_FAKE_RESUME: Final = "fake-resume"
_FAKE_ACCOUNT: Final = "42"
_FAKE_STATE: Final = "fake-state"
_FAKE_VERIFIER: Final = "fake-verifier"


@dataclass(frozen=True, slots=True)
class _Claim:
    """One thing a platform can claim in `features`, and what backs it up."""

    feature: Feature
    protocol: type[object]
    methods: tuple[str, ...]
    wants_async: bool


# Claims that are only worth making if the methods behind them exist.
_CLAIMS: Final[tuple[_Claim, ...]] = (
    _Claim(Feature.CREATE_APP, CanCreateApp, ("create_app",), wants_async=True),
    _Claim(
        Feature.PUSH_UPDATES,
        CanCheckSignature,
        ("check_signature", "read_update"),
        wants_async=False,
    ),
    _Claim(Feature.DELETE_POST, CanDeletePosts, ("delete_post",), wants_async=True),
)


def _method_is_wrong(owner: object, name: str, *, wants_async: bool) -> bool:
    """Say whether a method is missing or the wrong shape.

    Args:
        owner: The object that should have the method.
        name: The method's name.
        wants_async: Whether it should be `async def`.

    Returns:
        True if it is not there, is not callable, or is sync where it should
        be async (or the other way round).
    """
    found = getattr(owner, name, None)
    if not callable(found):
        return True
    return inspect.iscoroutinefunction(found) is not wants_async


def _posts_it_should_refuse(features: Feature, limits: Limits) -> list[Post]:
    """Build posts a platform has already said it cannot take.

    Only builds the ones this platform actually rules out, so a network with
    no declared limits and every feature gets an empty list rather than a
    made-up failure.

    Args:
        features: What the platform says it can do.
        limits: What it says it allows right now.

    Returns:
        Posts it should refuse, which may be none at all.
    """
    posts: list[Post] = []

    if limits.max_text_length is not None:
        posts.append(Post(text="x" * (limits.max_text_length + 1)))

    if Feature.SCHEDULE not in features:
        posts.append(Post(text="later", publish_at=datetime.now(UTC) + timedelta(1)))

    if Feature.REPLY not in features:
        posts.append(Post(text="reply", reply_to="no-such-post"))

    if limits.max_images is not None:
        picture = Media.from_bytes(b"not really a picture", filename="a.png")
        posts.append(Post(media=(picture,) * (limits.max_images + 1)))

    return posts


class _Watcher(httpx.AsyncBaseTransport):
    """Wraps a transport and keeps a copy of everything sent through it.

    `PlatformChecks` puts this in front of whatever `make_transport` handed
    back, so a check can say "and nothing went to the wire" and mean it.
    """

    def __init__(self, wrapped: httpx.AsyncBaseTransport) -> None:
        """Watch one transport.

        Args:
            wrapped: The transport that actually answers.
        """
        self.wrapped = wrapped
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Remember the request, then let the real transport answer it.

        Args:
            request: The request on its way out.

        Returns:
            Whatever the wrapped transport replied.
        """
        self.requests.append(request)
        return await self.wrapped.handle_async_request(request)


class RecordingTransport(httpx.AsyncBaseTransport):
    """Answers httpx requests from a table, and keeps every request.

    Hand it to `HttpClient(transport=...)` and a platform runs end to end
    with no network at all. Replies are keyed by method and path together:

        transport = RecordingTransport({"POST /api/v1/statuses": {"id": "1"}})

    Anything it has no reply for comes back as a 404 whose body names what
    was asked for and what it does know, because "your platform asked for a
    path you did not set up" is otherwise a very quiet failure.

    Attributes:
        replies: What to answer, keyed by `"METHOD /path"`.
        answer: Your own function, used instead of the table when you need
            to look at the body or count the calls.
        status_code: The status used for a reply found in the table. Set it
            to 429 or 500 to see what your platform does then.
        requests: Every request sent, in order.
    """

    def __init__(
        self,
        replies: Mapping[str, RawData] | None = None,
        *,
        answer: Callable[[httpx.Request], httpx.Response] | None = None,
        status_code: int = 200,
    ) -> None:
        """Set up the replies this transport will give.

        Args:
            replies: What to answer, keyed by `"METHOD /path"`.
            answer: Your own function, used for every request instead of the
                table.
            status_code: The status used for replies found in the table.
        """
        self.replies: dict[str, RawData] = dict(replies) if replies else {}
        self.answer = answer
        self.status_code = status_code
        self.requests: list[httpx.Request] = []

    @property
    def paths(self) -> list[str]:
        """Every request as `"METHOD /path"`, in order, for asserting on."""
        return [f"{sent.method} {sent.url.path}" for sent in self.requests]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Record the request and answer it.

        Args:
            request: The request on its way out.

        Returns:
            The reply from your `answer` function, or from the table, or a
            404 saying which key was missing.
        """
        self.requests.append(request)

        if self.answer is not None:
            return self.answer(request)

        key = f"{request.method} {request.url.path}"
        if key in self.replies:
            return httpx.Response(self.status_code, json=self.replies[key])

        known = ", ".join(sorted(self.replies)) or "nothing"
        return httpx.Response(
            404,
            json={
                "error": (
                    f"RecordingTransport has no reply for {key!r}. "
                    f"It was set up to answer: {known}."
                )
            },
        )


@dataclass(frozen=True, slots=True)
class StorageCall:
    """One call your code made to storage.

    Attributes:
        name: Which method, such as `"save_connection"`.
        args: What it was called with, in the order they were passed.
    """

    name: str
    args: tuple[object, ...] = ()


class RecordingStorage:
    """Storage that works properly and remembers every call.

    Use it wherever a test needs a `Storage` and then wants to say what
    should have reached it - that a rotated refresh token was written, that
    a revoked connection was deleted, that nothing was read twice.

    It really stores things, so reads see earlier writes. That matters:
    a double that forgets is a double that hides bugs.

    Example:
        storage = RecordingStorage(connections=[connection])
        await sc.account(connection.id).post(Post(text="hi"))
        assert storage.names() == ["get_connection"]

    Attributes:
        calls: Every call made, in order.
    """

    def __init__(
        self,
        *,
        connections: Iterable[Connection] = (),
        apps: Iterable[AppCredentials] = (),
    ) -> None:
        """Start with whatever should already be stored.

        Args:
            connections: Connections to start with.
            apps: App credentials to start with.
        """
        self.calls: list[StorageCall] = []
        self._connections = {found.id: found for found in connections}
        self._apps = {found.key: found for found in apps}
        self._errors: dict[str, Exception] = {}

    def fails(self, method: str, error: Exception) -> None:
        """Make one method raise from now on.

        For the half of your code that only runs when the database is down.

        Args:
            method: Which method, such as `"save_connection"`.
            error: What it should raise.
        """
        self._errors[method] = error

    def reset(self) -> None:
        """Forget the calls recorded so far, keeping what is stored."""
        self.calls.clear()

    def names(self) -> list[str]:
        """Return the names of the methods called, in order."""
        return [call.name for call in self.calls]

    def calls_to(self, method: str) -> list[StorageCall]:
        """Return every recorded call to one method.

        Args:
            method: Which method to look for.

        Returns:
            The calls, in the order they were made.
        """
        return [call for call in self.calls if call.name == method]

    def _record(self, method: str, *args: object) -> None:
        """Note a call down, then raise if this method was told to fail.

        The call is recorded before the failure, because it did happen.

        Args:
            method: Which method was called.
            *args: What it was called with.

        Raises:
            Exception: Whatever `fails` was given for this method.
        """
        self.calls.append(StorageCall(name=method, args=args))
        error = self._errors.get(method)
        if error is not None:
            raise error

    async def get_connection(self, connection_id: str) -> Connection | None:
        """Look up one connected account.

        Args:
            connection_id: The id your app gave this connection.

        Returns:
            The connection, or `None` if there is no such connection.
        """
        self._record("get_connection", connection_id)
        return self._connections.get(connection_id)

    async def save_connection(self, connection: Connection) -> None:
        """Write a connection, replacing any earlier one with the same id.

        Args:
            connection: The connection to write.
        """
        self._record("save_connection", connection)
        self._connections[connection.id] = connection

    async def delete_connection(self, connection_id: str) -> None:
        """Remove a connection. Quiet if it is already gone.

        Args:
            connection_id: The id your app gave this connection.
        """
        self._record("delete_connection", connection_id)
        self._connections.pop(connection_id, None)

    async def get_app(self, platform: str, host: str | None) -> AppCredentials | None:
        """Look up your app's credentials for one network.

        Args:
            platform: Which network.
            host: Which server, or `None`.

        Returns:
            The credentials, or `None` if none are stored yet.
        """
        self._record("get_app", platform, host)
        return self._apps.get((platform, host))

    async def save_app(self, app: AppCredentials) -> None:
        """Write your app's credentials for one network.

        Args:
            app: The credentials to write.
        """
        self._record("save_app", app)
        self._apps[app.key] = app


# What `FakePlatform` can do unless you say otherwise. Deliberately not
# SCHEDULE: most networks cannot schedule, and a fake that can hides the
# code that has to cope when a network cannot.
_FAKE_FEATURES: Final = (
    Feature.POST_TEXT
    | Feature.POST_IMAGE
    | Feature.POST_VIDEO
    | Feature.REPLY
    | Feature.READ_POSTS
    | Feature.DELETE_POST
    | Feature.CREATE_APP
    | Feature.PUSH_UPDATES
)


class FakePlatform:
    """A platform that works without a network, for tests of your own.

    It passes every check in `PlatformChecks`, which is the point: it is
    what a well-behaved platform looks like, and it is the thing the checks
    themselves are tested against.

    Every knob is a way of making it misbehave on purpose, so you can see
    what your app does about it. Give it `accounts` and signing in stops to
    ask which one. Give it `publish_fails_with` and every post raises that.
    Give it `token_lifetime=None` and its tokens never expire, the way
    Mastodon's do not.

    Give it a transport and `publish` really sends a request through
    `HttpClient`, so retries, rate limits and error handling all run. Leave
    the transport out and it answers from memory.

    Example:
        transport = RecordingTransport({"POST /posts": {"id": "1"}})
        platform = FakePlatform(transport=transport)
        result = await platform.publish(platform.connection(), Post(text="hi"))

    Attributes:
        name: How this platform is named in code.
        features: What it says it can do.
        accounts: Accounts sign-in offers to choose between. Empty means it
            never asks.
        secret: The secret `check_signature` expects, unless told another.
        updates: What `fetch_updates` hands back.
        token_lifetime: How long a fresh token lasts. `None` for a token
            that never expires.
        publish_fails_with: An error every `publish` raises instead of
            working.
        published: Every post published, as (connection id, post).
        deleted: The id of every post deleted.
        created_apps: Every app registered.
        refreshed: The id of every connection whose token was renewed.
        last_remember: What the last `finish_login` was handed back from
            `start_login`. `None` until one has happened.
    """

    def __init__(
        self,
        *,
        name: str = "fake",
        features: Feature = _FAKE_FEATURES,
        limits: Limits | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        accounts: tuple[AccountChoice, ...] = (),
        secret: str = _FAKE_SIGNING_KEY,
        updates: Sequence[Update] = (),
        token_lifetime: timedelta | None = timedelta(hours=1),
        publish_fails_with: SocialChimpError | None = None,
    ) -> None:
        """Set up a fake network that behaves however you need it to.

        Args:
            name: How this platform is named in code.
            features: What it says it can do.
            limits: What it allows. Left out, a sensible small set.
            transport: Where `publish` sends its request. Left out, it does
                not send one.
            accounts: Accounts to offer during sign-in. Empty never asks.
            secret: The secret `check_signature` expects.
            updates: What `fetch_updates` hands back.
            token_lifetime: How long a fresh token lasts, or `None` for one
                that never expires.
            publish_fails_with: An error every `publish` raises.
        """
        self.name = name
        self.features = features
        self.accounts = accounts
        self.secret = secret
        self.updates = tuple(updates)
        self.token_lifetime = token_lifetime
        self.publish_fails_with = publish_fails_with
        self.published: list[tuple[str, Post]] = []
        self.deleted: list[str] = []
        self.created_apps: list[AppCredentials] = []
        self.refreshed: list[str] = []
        self.last_remember: RawData | None = None
        self._limits = (
            limits
            if limits is not None
            else Limits(max_text_length=300, max_images=4, max_videos=1)
        )
        self._transport = transport
        self._live: set[str] = set()
        self._counter = 0

    def _expiry(self) -> datetime | None:
        """Work out when a token handed out now would stop working."""
        if self.token_lifetime is None:
            return None
        return datetime.now(UTC) + self.token_lifetime

    def connection(
        self,
        *,
        connection_id: str = "fake-connection",
        account_id: str = _FAKE_ACCOUNT,
    ) -> Connection:
        """Build a connection to this fake, ready to use.

        Args:
            connection_id: The id your app would have given it.
            account_id: The id the network would use.

        Returns:
            A connection with a working token.
        """
        return Connection(
            id=connection_id,
            platform=self.name,
            host=None,
            account_id=account_id,
            account_name=f"someone@{self.name}.example",
            token=Token(
                access_token=_FAKE_ACCESS,
                refresh_token=_FAKE_REFRESH,
                expires_at=self._expiry(),
            ),
        )

    def sign(self, body: bytes, *, secret: str | None = None) -> dict[str, str]:
        """Return the headers this fake wants alongside a pushed body.

        Args:
            body: The exact bytes that will be sent.
            secret: Sign with this instead of the fake's own secret, to see
                what happens when a signature does not match.

        Returns:
            Headers to send with the body.
        """
        used = secret if secret is not None else self.secret
        digest = hmac.new(used.encode(), body, hashlib.sha256).hexdigest()
        return {_FAKE_SIGNATURE_HEADER: f"sha256={digest}"}

    async def limits(self, connection: Connection) -> Limits:
        """Return what this fake currently allows.

        Args:
            connection: The account to ask about. Ignored here.

        Returns:
            The limits it was built with.
        """
        return self._limits

    async def start_login(self, request: LoginRequest) -> SendToNetwork:
        """Begin signing someone in.

        Args:
            request: Where to send them back to, and what to ask for.

        Returns:
            Where to send the person next.
        """
        state = request.state if request.state is not None else _FAKE_STATE
        return SendToNetwork(
            url=f"https://{self.name}.example/authorize?state={state}",
            state=state,
            remember={"verifier": _FAKE_VERIFIER},
        )

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> LoginStep:
        """Carry on after the person comes back.

        Asks which account to use when this fake was given accounts and the
        callback does not name one yet, the way Facebook asks which page.

        Args:
            request: The same request used to start the login.
            callback: The query values the network sent back.
            remember: Whatever `start_login` asked to be kept. Left on
                `last_remember` so a test can check your app carried it
                between the two halves of the sign-in.

        Returns:
            The finished connection, or the question to ask first.
        """
        self.last_remember = remember
        if self.accounts and "account" not in callback:
            return ChooseAccount(options=self.accounts, resume_token=_FAKE_RESUME)
        account_id = callback.get("account", _FAKE_ACCOUNT)
        return Finished(connection=self.connection(account_id=account_id))

    async def refresh(self, connection: Connection) -> Token:
        """Hand out a fresh token.

        Args:
            connection: The account whose token is running out.

        Returns:
            A new token, with a new refresh token, the way the networks that
            rotate them do it.
        """
        self.refreshed.append(connection.id)
        return Token(
            access_token=_FAKE_NEW_ACCESS,
            refresh_token=_FAKE_NEW_REFRESH,
            expires_at=self._expiry(),
        )

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        """Publish a post, checking it first.

        Args:
            connection: The account to publish as.
            post: What to publish.

        Returns:
            What this fake says about the new post.

        Raises:
            SocialChimpError: Whatever `publish_fails_with` holds, and
                whatever `check_post` raises for a post this fake cannot
                take.
        """
        limits = await self.limits(connection)
        check_post(post, platform=self.name, features=self.features, limits=limits)

        if self.publish_fails_with is not None:
            raise self.publish_fails_with

        self.published.append((connection.id, post))
        self._counter += 1
        post_id = str(self._counter)
        raw: RawData = {}

        if self._transport is not None:
            async with HttpClient(
                f"https://{self.name}.example",
                platform=self.name,
                transport=self._transport,
            ) as http:
                raw = await http.json("POST", "/posts", json={"text": post.text})
                post_id = str(raw.get("id", post_id))

        self._live.add(post_id)
        return PostResult(
            id=post_id,
            url=f"https://{self.name}.example/p/{post_id}",
            raw=raw,
        )

    async def create_app(
        self,
        *,
        name: str,
        redirect_uri: str,
        host: str | None = None,
        scopes: tuple[str, ...] = (),
    ) -> AppCredentials:
        """Register an app with this fake.

        Args:
            name: The app name people would see.
            redirect_uri: Where the network sends people back to.
            host: Which server to register on.
            scopes: Permissions the app will ask for.

        Returns:
            Credentials that work with this fake.
        """
        app = AppCredentials(
            platform=self.name,
            host=host,
            client_id=f"fake-client-for-{name}",
            client_secret=_FAKE_CLIENT_KEY,
        )
        self.created_apps.append(app)
        return app

    async def delete_post(self, connection: Connection, post_id: str) -> None:
        """Remove a post this fake published.

        Args:
            connection: The account that published it.
            post_id: The id this fake handed back.

        Raises:
            NotFoundError: If this fake never published that post, or it has
                already been deleted. Real networks say the same.
        """
        if post_id not in self._live:
            message = (
                f"{self.name} has no post {post_id!r}. This fake only knows "
                f"about posts it published itself."
            )
            raise NotFoundError(message, platform=self.name)
        self._live.discard(post_id)
        self.deleted.append(post_id)

    async def fetch_updates(
        self,
        connection: Connection,
        since: datetime | None,
    ) -> Sequence[Update]:
        """Return what has happened since a moment in time.

        Args:
            connection: The account to ask about. Ignored here.
            since: Only return things newer than this.

        Returns:
            The updates this fake was built with, oldest first.
        """
        if since is None:
            return self.updates
        return tuple(found for found in self.updates if found.created_at > since)

    def check_signature(
        self,
        body: bytes,
        headers: Mapping[str, str],
        *,
        secret: str,
    ) -> None:
        """Check a pushed request really came from this fake.

        Args:
            body: The request body, untouched.
            headers: The request headers.
            secret: The secret agreed with the network.

        Raises:
            SignatureError: If the header is missing or does not match.
        """
        verify_hmac_sha256(
            body,
            headers,
            secret=secret,
            header_name=_FAKE_SIGNATURE_HEADER,
        )

    def read_update(self, body: bytes, headers: Mapping[str, str]) -> Update:
        """Turn a checked request into an update.

        Args:
            body: The request body, untouched.
            headers: The request headers. Not needed by this fake.

        Returns:
            What happened, in socialchimp's own words.
        """
        sent = json.loads(body)
        return Update.from_network(
            update_id=str(sent["id"]),
            kind_name=str(sent["kind"]),
            platform=self.name,
            connection_id=str(sent["connection_id"]),
            created_at=datetime.fromisoformat(str(sent["at"])),
            raw=sent,
        )


class PlatformChecks:
    """Checks that your platform behaves like the others. Subclass it.

    Write one method saying how to build your platform and you inherit every
    check below:

        class TestMyPlatform(PlatformChecks):
            def make_platform(self) -> Platform:
                return MyPlatform(transport=self.transport)

    Your subclass must be called `Test...`, because that is what pytest
    collects. This base is not, on purpose - renaming it would make pytest
    run these checks on their own, with no platform to check.

    Some checks need an account and something to answer requests. Add
    `make_connection` and `make_transport` and those run too; leave them out
    and they skip with a line saying what to add. A check never fails
    because you did not write an optional method.

    None of this replaces mypy. These checks run your code; only a type
    checker looks at what your methods take and return.
    """

    def make_platform(self) -> Platform:
        """Build the platform to check. Every subclass writes this one.

        Returns:
            Your platform, built and ready to use.

        Raises:
            NotImplementedError: Always, until you write it.
        """
        message = (
            f"{type(self).__name__} has no make_platform, so there is "
            f"nothing to check. Write one:\n\n"
            f"    def make_platform(self) -> Platform:\n"
            f"        return MyPlatform(transport=self.transport)\n"
        )
        raise NotImplementedError(message)

    def make_connection(self) -> Connection | None:
        """Build an account for the checks that need one. Optional.

        Return a connection your fake transport will answer for. Without one
        the checks that publish, ask for limits or read updates skip.

        Returns:
            The account to use, or `None` to skip those checks.
        """
        return None

    def make_transport(self) -> httpx.AsyncBaseTransport | None:
        """Build something to answer requests. Optional.

        `RecordingTransport` is a reasonable starting point. Whatever you
        return is wrapped so the checks can see what went to the wire, so
        hand `self.transport` - not this - to your platform.

        Returns:
            The transport to answer with, or `None` to skip the checks that
            watch the wire.
        """
        return None

    @cached_property
    def platform(self) -> Platform:
        """The platform being checked, built once per check."""
        return self.make_platform()

    @cached_property
    def _watcher(self) -> _Watcher | None:
        """Your transport with a note-taker in front of it, or `None`."""
        given = self.make_transport()
        if given is None:
            return None
        return _Watcher(given)

    @cached_property
    def _connection(self) -> Connection | None:
        """The account to use, built once per check."""
        return self.make_connection()

    @property
    def transport(self) -> httpx.AsyncBaseTransport | None:
        """Hand this to your platform inside `make_platform`.

        It is what `make_transport` returned, wrapped so these checks can
        see the requests. Give your platform something else and the checks
        that watch the wire will pass without ever having looked.
        """
        return self._watcher

    def connection_or_skip(self) -> Connection:
        """Return the account to use, or skip the check that asked for it.

        Returns:
            The connection from `make_connection`.
        """
        found = self._connection
        if found is None:
            pytest.skip(
                f"{type(self).__name__} has no make_connection, so the "
                f"checks that need an account cannot run. Add one returning "
                f"a Connection your transport will answer for."
            )
        return found

    def requests_or_skip(self) -> list[httpx.Request]:
        """Return the live list of requests sent, or skip the check.

        Returns:
            The requests that have gone through `self.transport` so far. It
            keeps filling up as more are sent.
        """
        watcher = self._watcher
        if watcher is None:
            pytest.skip(
                f"{type(self).__name__} has no make_transport, so there is "
                f"no way to see what went to the wire. Add one returning a "
                f"RecordingTransport, and pass self.transport to your "
                f"platform in make_platform."
            )
        return watcher.requests

    async def test_it_provides_everything_a_platform_must(self) -> None:
        """Every method a platform has to have is there, and is async."""
        platform = self.platform

        wrong = [
            method
            for method in _MUST_HAVE
            if _method_is_wrong(platform, method, wants_async=True)
        ]
        if wrong:
            pytest.fail(
                f"{type(platform).__name__} is missing, or has as a plain "
                f"function, {', '.join(wrong)}. A platform provides "
                f"{', '.join(_MUST_HAVE)}, and every one is `async def`."
            )

        if not isinstance(platform, Platform):
            pytest.fail(
                f"{type(platform).__name__} has the methods but not the two "
                f"attributes. A platform also has `name`, the word people "
                f"ask for it by, and `features`, what it can do."
            )

    async def test_its_name_can_be_an_entry_point_name(self) -> None:
        """The name is something a package can register and a person can type."""
        # Read as `object` on purpose. An annotation is only as good as the
        # type checker its author ran, and we are checking what turns up.
        name: object = self.platform.name

        if not isinstance(name, str) or not _ENTRY_POINT_NAME.match(name):
            pytest.fail(
                f"name is {name!r}. It is how a package registers this "
                f"platform, how an app asks for it, and how the extra that "
                f"installs it is spelled, so it has to be lowercase letters, "
                f"digits, `-` or `_`, starting with a letter, with no "
                f'spaces - "mastodon", not "Mastodon" or "my platform".'
            )

    async def test_it_declares_at_least_one_way_to_post(self) -> None:
        """`features` is a Feature and says it can post something."""
        features: object = self.platform.features

        if not isinstance(features, Feature):
            pytest.fail(
                f"features is {features!r}, which is not a Feature. Build it "
                f"from the flags: Feature.POST_TEXT | Feature.POST_IMAGE."
            )

        if not features & _WAYS_TO_POST:
            pytest.fail(
                f"{self.platform.name} says it cannot post text, pictures or "
                f"video. Publishing is the one thing every platform is for, "
                f"so at least one of POST_TEXT, POST_IMAGE and POST_VIDEO "
                f"belongs in features."
            )

    async def test_everything_it_claims_in_features_it_can_actually_do(self) -> None:
        """Anything listed in `features` has the method that backs it up.

        This is the one that matters most. `features` is what socialchimp
        and your users read before deciding whether to call something, so a
        claim with nothing behind it fails at the worst moment, in someone
        else's app.
        """
        platform = self.platform

        for claim in _CLAIMS:
            if claim.feature not in platform.features:
                continue

            wrong = [
                method
                for method in claim.methods
                if _method_is_wrong(platform, method, wants_async=claim.wants_async)
            ]

            # `Platform` and its extras are runtime_checkable, but isinstance
            # against a protocol only checks that the method names exist -
            # not what they take, what they return, or that they are async.
            # So this is an early warning that catches the everyday mistakes.
            # It is never a promise that the platform is correct; mypy is
            # what checks the rest, and running it is on you.
            if wrong or not isinstance(platform, claim.protocol):
                shape = "async def" if claim.wants_async else "a plain def"
                pytest.fail(
                    f"{platform.name} lists {claim.feature.name} in features "
                    f"but {', '.join(wrong) or 'nothing'} backs it up. Add "
                    f"{' and '.join(claim.methods)} as {shape}, or take "
                    f"{claim.feature.name} out of features. socialchimp "
                    f"reads features before it calls anything, so a claim "
                    f"nothing answers breaks apps that believed it."
                )

    async def test_its_limits_are_never_zero_for_unknown(self) -> None:
        """`limits()` gives a Limits, and every number is None or positive."""
        connection = self.connection_or_skip()
        limits: object = await self.platform.limits(connection)

        if not isinstance(limits, Limits):
            pytest.fail(
                f"limits() returned {limits!r}, which is not a Limits. "
                f"Return Limits(...), leaving out anything the network does "
                f"not tell you."
            )

        for shape in fields(Limits):
            value: object = getattr(limits, shape.name)
            if value is None:
                continue
            if not isinstance(value, int) or value <= 0:
                pytest.fail(
                    f"limits().{shape.name} is {value!r}. A number here is "
                    f"a real limit and has to be positive; `None` is how you "
                    f"say you do not know. Zero means the opposite of "
                    f"unknown - it means nothing is allowed - and a post "
                    f"would be refused for a limit the network never set."
                )

    async def test_a_post_over_a_limit_is_refused_before_any_request(self) -> None:
        """A post that breaks a declared limit never reaches the network."""
        connection = self.connection_or_skip()
        sent = self.requests_or_skip()
        platform = self.platform

        limits = await platform.limits(connection)
        if limits.max_text_length is None:
            pytest.skip(
                f"{platform.name} declares no max_text_length, so there is "
                f"no limit here to break."
            )

        before = len(sent)
        with pytest.raises(InvalidPostError):
            await platform.publish(
                connection, Post(text="x" * (limits.max_text_length + 1))
            )

        if len(sent) != before:
            pytest.fail(
                f"{platform.name} sent {len(sent) - before} request(s) while "
                f"refusing a post that breaks its own limit. Check the post "
                f"first - `socialchimp.features.check_post` does exactly "
                f"this - so a mistake costs nothing against the rate limit "
                f"and the message says what is wrong instead of the network "
                f"answering with a number."
            )

    async def test_scheduling_is_refused_when_it_cannot_schedule(self) -> None:
        """A platform without SCHEDULE says so, rather than posting now."""
        platform = self.platform
        if Feature.SCHEDULE in platform.features:
            pytest.skip(
                f"{platform.name} lists Feature.SCHEDULE, so there is "
                f"nothing here to refuse."
            )

        connection = self.connection_or_skip()
        later = Post(text="later", publish_at=datetime.now(UTC) + timedelta(hours=1))

        try:
            await platform.publish(connection, later)
        except NotSupportedError:
            return
        except Exception as error:
            pytest.fail(
                f"{platform.name} does not list Feature.SCHEDULE, so a post "
                f"with publish_at should raise NotSupportedError. It raised "
                f"{type(error).__name__}: {error}. NotSupportedError is the "
                f"one an app can catch and explain to a person."
            )

        pytest.fail(
            f"{platform.name} does not list Feature.SCHEDULE but took a post "
            f"with publish_at anyway. Publishing now instead of later is the "
            f"kind of quiet wrong answer this library exists to avoid - "
            f"raise NotSupportedError instead."
        )

    async def test_the_errors_it_raises_are_all_socialchimp_errors(self) -> None:
        """Anything it refuses, it refuses with a socialchimp error."""
        connection = self.connection_or_skip()
        platform = self.platform

        limits = await platform.limits(connection)
        refusable = _posts_it_should_refuse(platform.features, limits)
        if not refusable:
            pytest.skip(
                f"{platform.name} declares no limits and rules nothing out, "
                f"so there is nothing it can refuse without a network."
            )

        for post in refusable:
            try:
                await platform.publish(connection, post)
            except SocialChimpError:
                continue
            except Exception as error:
                pytest.fail(
                    f"{platform.name} raised {type(error).__name__}: "
                    f"{error}. Every error a platform raises is a "
                    f"SocialChimpError, so an app catches one thing rather "
                    f"than learning what fifteen networks each throw."
                )

    async def test_the_updates_it_reads_come_back_as_updates(self) -> None:
        """`fetch_updates` gives back Updates, each with a real timestamp."""
        platform = self.platform
        if not isinstance(platform, CanReadUpdates):
            pytest.skip(
                f"{platform.name} has no fetch_updates, so socialchimp will "
                f"not check it on a timer. Nothing to check here."
            )

        connection = self.connection_or_skip()
        updates: object = await platform.fetch_updates(connection, None)

        if isinstance(updates, str | bytes) or not isinstance(updates, Sequence):
            pytest.fail(
                f"fetch_updates returned {updates!r}. It has to be a "
                f"sequence - a list or a tuple - so the poller can walk it "
                f"and work out what is new."
            )

        for item in updates:
            if not isinstance(item, Update) or item.created_at.tzinfo is None:
                pytest.fail(
                    f"fetch_updates returned {item!r}. Every item is an "
                    f"Update with a timezone on created_at. Build them with "
                    f"`Update.from_network(...)`, which does both for you - "
                    f"a time with no timezone compares wrongly against every "
                    f"other time socialchimp holds, and does it silently."
                )
