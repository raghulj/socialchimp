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
from collections.abc import Mapping, Sequence
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
from socialchimp.features import Feature, Limits, TextCount, check_post, measure_text
from socialchimp.http import HttpClient
from socialchimp.models import (
    AppCredentials,
    Connection,
    Media,
    Post,
    PostResult,
    PostState,
    Token,
)
from socialchimp.platform import (
    AccountChoice,
    AskForDetails,
    CanCheckSignature,
    CanCheckState,
    CanCreateApp,
    CanDeletePosts,
    CanReadUpdates,
    CanResumeLogin,
    ChooseAccount,
    Finished,
    LoginField,
    LoginRequest,
    LoginStep,
    Platform,
    SendToNetwork,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from socialchimp.models import RawData

__all__ = [
    "FakePlatform",
    "PlatformChecks",
    "RecordingStorage",
    "RecordingTransport",
    "StorageCall",
]

# The methods every platform provides as `async def`. `registry` keeps the
# same five for its own early warning, privately - copying five names is
# cheaper than two modules reaching into each other.
_MUST_HAVE: Final = ("limits", "start_login", "finish_login", "refresh", "publish")

# A redirect address for the check that has to start a login. Nobody is ever
# sent there - only its shape matters, because a platform reads it and hands
# back what to do next without asking anybody anything.
_A_REDIRECT: Final = "https://app.example/callback"

# A platform that cannot post anything is not a platform anyone can use.
_WAYS_TO_POST: Final = Feature.POST_TEXT | Feature.POST_IMAGE | Feature.POST_VIDEO

# What `SocialChimp.choose` passes to `resume_login`, all of them by name.
_RESUME_ARGUMENTS: Final = ("resume_token", "account_id", "remember")

# How many things `Account.check_state` hands a `check_state`: the connection
# and the post id, both by position.
_CHECK_STATE_ARGUMENTS: Final = 2

# Fields on `Limits` that this check leaves alone.
#
# `text_counted_in` is not a number at all - it says how the length is
# counted, not how much is allowed.
#
# `posts_left_today` is a number, but zero is a real answer for it, not a
# stand-in for "we do not know". Instagram and Threads both count down and
# both reach zero, and `check_post` reads that zero to refuse the post. Every
# other number here means a limit, and a limit of zero would mean the network
# allows none of something, which is what the check is looking for.
_NOT_CHECKED: Final = frozenset({"text_counted_in", "posts_left_today"})

# One thumbs-up with a skin tone on it. One letter to a person, two
# characters to Python, four units to a network counting the way Java does,
# eight bytes written out - so a post made of these is a different length
# under every way of counting, which is exactly what we want to test with.
_A_BIG_LETTER: Final = "\U0001f44d\U0001f3fd"


def _copies_that_fit(limits: Limits, allowed: int) -> int:
    """Work out how many big letters a post can hold and still be allowed.

    Args:
        limits: What the network allows.
        allowed: Its `max_text_length`, already known not to be `None`.

    Returns:
        How many copies of `_A_BIG_LETTER` fit inside every limit declared -
        and never fewer than one, because an empty post is not a post.
    """
    copies = allowed // measure_text(_A_BIG_LETTER, limits.text_counted_in)
    if limits.max_text_bytes is not None:
        copies = min(copies, limits.max_text_bytes // len(_A_BIG_LETTER.encode()))
    return max(copies, 1)


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


def _must_be_a_plain_function(platform: object, name: str) -> None:
    """Stop a check unless a method is there and is a plain function.

    Args:
        platform: The platform being checked.
        name: The method it should have.
    """
    if not _method_is_wrong(platform, name, wants_async=False):
        return

    pytest.fail(
        f"{type(platform).__name__} is missing {name}, or has it as an "
        f"`async def`. It runs before every single request, and the token "
        f"has already been renewed by the time it does, so it is a plain "
        f"function that reads what it needs off the connection. Anything "
        f"you would wait for here belongs in refresh()."
    )


def _something_to_attach(features: Feature) -> tuple[Media, ...]:
    """Build the smallest attachment a platform will take.

    For a network with no text-only post, this is what makes a probe post
    into something it will look at. Nothing here is a real picture or a real
    video - no check sends one to anything but a fake transport.

    Args:
        features: What the platform says it can do.

    Returns:
        One attachment, or nothing at all when the platform takes neither
        pictures nor video.
    """
    if Feature.POST_IMAGE in features:
        return (Media.from_bytes(b"not really a picture", filename="a.png"),)
    if Feature.POST_VIDEO in features:
        return (Media.from_bytes(b"not really a video", filename="a.mp4"),)
    return ()


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
    ask which one, and carries on once one is picked. Give it
    `publish_fails_with` and every post raises that; `login_fails_with` and
    signing in raises that instead of finishing. Give it
    `token_lifetime=None` and its tokens never expire, the way Mastodon's do
    not.

    Give it a transport and `publish` really sends a request through
    `HttpClient`, so retries, rate limits and error handling all run. Leave
    the transport out and it answers from memory.

    Give it `ask_for` and signing in asks for those instead of sending
    anybody anywhere, the way Bluesky's app password and the bot-token
    networks work.

    Example:
        transport = RecordingTransport({"POST /posts": {"id": "1"}})
        platform = FakePlatform(transport=transport)
        result = await platform.publish(platform.connection(), Post(text="hi"))

    Attributes:
        name: How this platform is named in code.
        features: What it says it can do.
        accounts: Accounts sign-in offers to choose between. Empty means it
            never asks - and then there is no `resume_login` either, so a
            fake with no accounts is not a `CanResumeLogin`, the same as a
            network that signs somebody in in one step.
        ask_for: What signing in should ask a person for. Empty sends them
            to a sign-in page instead, which is what most networks do.
        secret: The secret `check_signature` expects, unless told another.
        updates: What `fetch_updates` hands back.
        states: What `check_state` says about a post, one call after
            another, with the last one repeating. Empty means this fake has
            no `check_state` at all, the same as a network that has finished
            by the time it answers - and then `Account.check_state` refuses,
            which is what most networks do.
        token_lifetime: How long a fresh token lasts. `None` for a token
            that never expires.
        publish_fails_with: An error every `publish` raises instead of
            working.
        login_fails_with: An error raised instead of finishing a sign-in, by
            `finish_login` and by `resume_login`. Those are the two halves
            where a real network refuses - a code already used, a person who
            changed their mind. Starting a sign-in is left alone, so a test
            can send somebody away and have only the return go wrong.
        published: Every post published, as (connection id, post).
        deleted: The id of every post deleted.
        created_apps: Every app registered.
        resumed: Every account picked part way through a sign-in, as
            (resume token, account id).
        state_asked: Every post asked about, as (connection id, post id).
        refreshed: The id of every connection whose token was renewed.
        refreshed_with: Your app's credentials as each renewal was handed
            them, in the same order as `refreshed`. `None` where a renewal
            was given none, which is how a test says the client failed to
            look them up.
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
        ask_for: tuple[LoginField, ...] = (),
        secret: str = _FAKE_SIGNING_KEY,
        updates: Sequence[Update] = (),
        states: Sequence[PostState] = (),
        token_lifetime: timedelta | None = timedelta(hours=1),
        publish_fails_with: SocialChimpError | None = None,
        login_fails_with: SocialChimpError | None = None,
    ) -> None:
        """Set up a fake network that behaves however you need it to.

        Args:
            name: How this platform is named in code.
            features: What it says it can do.
            limits: What it allows. Left out, a sensible small set.
            transport: Where `publish` sends its request. Left out, it does
                not send one.
            accounts: Accounts to offer during sign-in. Empty never asks,
                and leaves this fake without a `resume_login`.
            ask_for: What signing in should ask a person for. Empty sends
                them to a sign-in page instead.
            secret: The secret `check_signature` expects.
            updates: What `fetch_updates` hands back.
            states: What `check_state` says, one call after another. Empty
                leaves this fake without a `check_state`.
            token_lifetime: How long a fresh token lasts, or `None` for one
                that never expires.
            publish_fails_with: An error every `publish` raises.
            login_fails_with: An error `finish_login` and `resume_login`
                raise instead of finishing.
        """
        self.name = name
        self.features = features
        self.accounts = accounts
        self.ask_for = ask_for
        self.secret = secret
        self.updates = tuple(updates)
        self.states = tuple(states)
        self.token_lifetime = token_lifetime
        self.publish_fails_with = publish_fails_with
        self.login_fails_with = login_fails_with
        self.published: list[tuple[str, Post]] = []
        self.deleted: list[str] = []
        self.created_apps: list[AppCredentials] = []
        self.resumed: list[tuple[str, str]] = []
        self.state_asked: list[tuple[str, str]] = []
        self.refreshed: list[str] = []
        self.refreshed_with: list[AppCredentials | None] = []
        self.last_remember: RawData | None = None
        self._limits = (
            limits
            if limits is not None
            else Limits(max_text_length=300, max_images=4, max_videos=1)
        )
        self._transport = transport
        self._live: set[str] = set()
        self._counter = 0
        self._state_asks = 0
        if accounts and not hasattr(self, "resume_login"):
            # Put on the instance rather than written as a method, so that a
            # fake with nothing to choose between has no `resume_login` at
            # all and is not a `CanResumeLogin`. socialchimp reads that
            # before it will carry a login on, and a fake that claimed it
            # while never pausing would let a wrong app pass its own tests.
            # A subclass that wrote its own keeps it - hence the hasattr.
            self.resume_login = self._resume_login
        if states and not hasattr(self, "check_state"):
            # Same trick, same reason. A fake with nothing left to happen
            # has no `check_state`, so `Account.check_state` refuses against
            # it the way it does against every network that finishes while
            # we wait.
            self.check_state = self._check_state

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

    def api_base(self, connection: Connection) -> str:
        """Return where this fake's API lives.

        One address for every account, the way most real networks work.

        Args:
            connection: The account we are about to act as. Ignored here.

        Returns:
            The address every path is joined onto.
        """
        return f"https://{self.name}.example"

    def auth_headers(self, connection: Connection) -> Mapping[str, str]:
        """Return the header that proves we may act as this account.

        Args:
            connection: The account we are acting as.

        Returns:
            An ordinary bearer token header, built from the connection and
            nothing else.
        """
        return {"Authorization": f"Bearer {connection.token.access_token}"}

    async def limits(self, connection: Connection) -> Limits:
        """Return what this fake currently allows.

        Args:
            connection: The account to ask about. Ignored here.

        Returns:
            The limits it was built with.
        """
        return self._limits

    async def start_login(self, request: LoginRequest) -> LoginStep:
        """Begin signing someone in.

        Asks for details when this fake was given `ask_for`, the way a
        network with no sign-in page does, and sends the person to one
        otherwise.

        Args:
            request: Where to send them back to, and what to ask for.

        Returns:
            Where to send the person next, or what to ask them for.
        """
        if self.ask_for:
            return AskForDetails(
                fields=self.ask_for,
                help_url=f"https://{self.name}.example/help/signing-in",
            )

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

        Raises:
            SocialChimpError: Whatever `login_fails_with` holds.
        """
        # Written down before the refusal, so a test can still say what your
        # app carried between the two halves of a sign-in that went wrong.
        self.last_remember = remember
        if self.login_fails_with is not None:
            raise self.login_fails_with
        if self.accounts and "account" not in callback:
            return ChooseAccount(options=self.accounts, resume_token=_FAKE_RESUME)
        account_id = callback.get("account", _FAKE_ACCOUNT)
        return Finished(connection=self.connection(account_id=account_id))

    async def _resume_login(
        self,
        request: LoginRequest,
        *,
        resume_token: str,
        account_id: str,
        remember: RawData | None = None,
    ) -> LoginStep:
        """Carry on with a sign-in now that an account has been picked.

        Reached as `resume_login`, and only on a fake that was given
        `accounts` - see `__init__`.

        Args:
            request: The same request the login was started with.
            resume_token: The value `ChooseAccount` handed out.
            account_id: Which of the offered accounts was picked.
            remember: Whatever `start_login` asked to be kept. Left on
                `last_remember`, the same as `finish_login` leaves it.

        Returns:
            The finished connection, for the account that was picked.

        Raises:
            SocialChimpError: Whatever `login_fails_with` holds.
        """
        self.resumed.append((resume_token, account_id))
        self.last_remember = remember
        if self.login_fails_with is not None:
            raise self.login_fails_with
        return Finished(connection=self.connection(account_id=account_id))

    async def refresh(
        self,
        connection: Connection,
        app: AppCredentials | None = None,
    ) -> Token:
        """Hand out a fresh token.

        Args:
            connection: The account whose token is running out.
            app: Your app's credentials. This fake does not need them - it
                asks nobody for anything - but it writes down what arrived
                on `refreshed_with`, so a test can say the credentials
                really did reach the platform.

        Returns:
            A new token, with a new refresh token, the way the networks that
            rotate them do it.
        """
        self.refreshed.append(connection.id)
        self.refreshed_with.append(app)
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
                self.api_base(connection),
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

    async def _check_state(
        self,
        connection: Connection,
        post_id: str,
    ) -> PostResult:
        """Say how far along a post is, working through `states` in turn.

        Put on the instance by `__init__` when there are states to give, so
        that a fake with none has no `check_state` and is not a
        `CanCheckState` - the same as a network that has finished by the
        time it answers.

        Args:
            connection: The account the post belongs to.
            post_id: The id this fake handed back.

        Returns:
            The next state in the list. The last one repeats, so a post that
            has finished stays finished however many times you ask.
        """
        self.state_asked.append((connection.id, post_id))
        step = min(self._state_asks, len(self.states) - 1)
        self._state_asks += 1
        return PostResult(id=post_id, state=self.states[step])

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

    def read_updates(self, body: bytes) -> list[Update]:
        """Turn a checked request into every update it carries.

        This fake never batches, so it is always a list of one. It is here
        so that `SocialChimp.read_updates` reaches a fake the same way it
        reaches Facebook, and an app's own tests do not have to know the
        difference.

        Args:
            body: The request body, untouched.

        Returns:
            What happened, as a list of one.
        """
        return [self.read_update(body, {})]

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

    def make_post(self, text: str) -> Post:
        """Build a post your platform would take, carrying this text. Optional.

        The checks that measure length need a post that is right in every
        other way, so that its length is the only thing being judged. Left
        alone, this is `Post(text=...)`, with a small picture or video
        attached for a network that has no text-only post.

        Write your own when your network wants more than that. YouTube
        refuses any video without a title, so there is no post it will look
        at twice without one:

            def make_post(self, text: str) -> Post:
                return Post(
                    text=text,
                    media=(Media.from_bytes(b"video", filename="a.mp4"),),
                    options={"title": "A video", "made_for_kids": False},
                )

        Args:
            text: The words the post has to carry, exactly as they are
                given. The checks count them, so a post that changes them
                is a post that measures the wrong thing.

        Returns:
            A post your platform would take.
        """
        features = self.platform.features
        if Feature.POST_TEXT in features:
            return Post(text=text)

        attached = _something_to_attach(features)
        if not attached:
            pytest.skip(
                f"{self.platform.name} says it can post neither text, "
                f"pictures nor video, so there is no post to build. Say what "
                f"it can post in features, or write make_post."
            )
        return Post(text=text, media=attached)

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
                f"{type(platform).__name__} has those methods but is still "
                f"not a platform. It also needs `name`, the word people ask "
                f"for it by, `features`, what it can do, and `api_base` and "
                f"`auth_headers`, which say where to send a request and what "
                f"to send with it."
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

    async def test_it_says_where_its_api_lives(self) -> None:
        """`api_base` gives a whole address that a path can be joined onto."""
        connection = self.connection_or_skip()
        platform = self.platform
        _must_be_a_plain_function(platform, "api_base")

        # Read as `object` on purpose: what turns up is the point, and an
        # annotation is only as good as the type checker its author ran.
        address: object = platform.api_base(connection)

        if not isinstance(address, str) or not address.startswith("https://"):
            pytest.fail(
                f"api_base() returned {address!r}. Every path is joined onto "
                f"it, so it has to be a whole address starting with "
                f'"https://" - "https://graph.facebook.com/v21.0", not '
                f'"graph.facebook.com".'
            )

        if address.endswith("/"):
            pytest.fail(
                f"api_base() returned {address!r}, which ends in a slash. "
                f"The paths joined onto it start with one already, so this "
                f"sends every request to an address with two."
            )

    async def test_it_says_what_headers_prove_who_we_are(self) -> None:
        """`auth_headers` gives headers that can go on a request as they are."""
        connection = self.connection_or_skip()
        platform = self.platform
        _must_be_a_plain_function(platform, "auth_headers")

        headers: object = platform.auth_headers(connection)

        if not isinstance(headers, Mapping) or not all(
            isinstance(name, str) and isinstance(value, str)
            for name, value in headers.items()
        ):
            pytest.fail(
                f"auth_headers() returned {headers!r}. It goes straight onto "
                f"the request, so it has to be a mapping of header names to "
                f'header values, both text: {{"Authorization": "Bearer ..."}}.'
            )

    async def test_the_details_it_asks_for_can_be_shown_in_a_form(self) -> None:
        """A network with no sign-in page asks for things a person can type.

        Bluesky takes an app password, Discord and Telegram a bot token.
        There is nowhere to send anybody, so the platform says what to ask
        for and the app draws the form. A box with no label on it is a box
        nobody knows what to put in.
        """
        platform = self.platform

        try:
            step = await platform.start_login(LoginRequest(redirect_uri=_A_REDIRECT))
        except SocialChimpError as refused:
            pytest.skip(
                f"{platform.name} will not start a login without your app's "
                f"credentials, so there is no first step to look at: {refused}"
            )

        if not isinstance(step, AskForDetails):
            # Every other kind of first step sends the person to the
            # network's own page, and there is no form of ours to look at.
            return

        if not step.fields:
            pytest.fail(
                f"{platform.name} asks for details but names no fields, so "
                f"there is nothing for an app to draw. Say what to ask for, "
                f"or send the person to a sign-in page instead."
            )

        for asked in step.fields:
            if not asked.name or not asked.label:
                pytest.fail(
                    f"{platform.name} asks for {asked!r}. Every field needs "
                    f"a name, which is where the answer comes back under, "
                    f"and a label, which is what the person reads next to "
                    f"the box."
                )

    async def test_a_platform_that_keeps_working_can_be_asked_how_it_is_going(
        self,
    ) -> None:
        """A `check_state` is `async def check_state(self, connection, post_id)`.

        YouTube encodes for minutes and TikTok can put a video in somebody's
        drafts, so both answer `publish` while they are still busy.
        `Account.check_state` hands this the connection and the post id, in
        that order and by position. A plain `def`, or one that takes some
        other number of things, is a TypeError in somebody else's app rather
        than a failure here.
        """
        platform = self.platform
        if not hasattr(platform, "check_state"):
            # Most networks have finished by the time they answer. Nothing
            # to check.
            return

        if _method_is_wrong(
            platform, "check_state", wants_async=True
        ) or not isinstance(platform, CanCheckState):
            pytest.fail(
                f"{platform.name} has a check_state, but not one socialchimp "
                f"can use. It is `async def check_state(self, connection, "
                f"post_id)`. Anything else - a plain def, something that is "
                f"not callable - and Account.check_state raises instead of "
                f"telling an app how its post is getting on."
            )

        taken = list(inspect.signature(platform.check_state).parameters.values())
        if any(given.kind is inspect.Parameter.VAR_POSITIONAL for given in taken):
            # Something that takes whatever it is handed cannot be missing
            # an argument, so there is nothing left to look at.
            return

        positional = [
            given
            for given in taken
            if given.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        if len(positional) != _CHECK_STATE_ARGUMENTS:
            pytest.fail(
                f"{platform.name}.check_state takes {len(positional)} things "
                f"where it should take two: the connection and the post id, "
                f"in that order. socialchimp passes both by position, so any "
                f"other shape is a TypeError the moment an app asks how a "
                f"post is getting on."
            )

    async def test_a_platform_that_pauses_to_ask_can_carry_on(self) -> None:
        """A `resume_login` is `async def` and takes what it will be given.

        Facebook asks which page, Instagram which business account, YouTube
        which channel. All three answer `finish_login` with `ChooseAccount`
        and finish the job in `resume_login`, and socialchimp calls that one
        by name with `resume_token`, `account_id` and `remember`. A plain
        `def`, or arguments under other names, leaves the person stuck on
        the page where they picked - and it fails there, in someone else's
        app, rather than here.
        """
        platform = self.platform
        if not hasattr(platform, "resume_login"):
            # Most networks sign somebody in in one step. Nothing to check.
            return

        if _method_is_wrong(
            platform, "resume_login", wants_async=True
        ) or not isinstance(platform, CanResumeLogin):
            pytest.fail(
                f"{platform.name} has a resume_login, but not one socialchimp "
                f"can use. It is `async def resume_login(self, request, *, "
                f"resume_token, account_id, remember=None)`. Anything else - "
                f"a plain def, something that is not callable - and choose() "
                f"refuses the login instead of carrying it on."
            )

        taken = inspect.signature(platform.resume_login).parameters
        if any(given.kind is inspect.Parameter.VAR_KEYWORD for given in taken.values()):
            # Something that takes whatever it is handed cannot be missing an
            # argument, so there is nothing left to look at.
            return

        missing = [name for name in _RESUME_ARGUMENTS if name not in taken]
        if missing:
            pytest.fail(
                f"{platform.name}.resume_login does not take "
                f"{', '.join(missing)}. socialchimp passes all of "
                f"{', '.join(_RESUME_ARGUMENTS)} by name, so an argument "
                f"spelled some other way is a TypeError the moment somebody "
                f"picks an account."
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
            if shape.name in _NOT_CHECKED:
                continue
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
                connection, self.make_post("x" * (limits.max_text_length + 1))
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

    async def test_it_counts_text_the_way_it_says_it_does(self) -> None:
        """Text is counted the way this platform's `Limits` says it is.

        Hardly any network's "300" means characters. Bluesky counts letters
        as a person would, Threads counts bytes, TikTok counts an emoji as
        two. A platform that says which and then counts characters anyway
        refuses posts the network would have taken, or sends posts it will
        not - both quietly, and both only once somebody uses an emoji.
        """
        connection = self.connection_or_skip()
        sent = self.requests_or_skip()
        platform = self.platform
        limits = await platform.limits(connection)

        counted: object = limits.text_counted_in
        if not isinstance(counted, TextCount):
            pytest.fail(
                f"limits().text_counted_in is {counted!r}, which is not a "
                f"TextCount. Say how this network counts the length of a "
                f"post - TextCount.CHARACTERS, GRAPHEMES, UTF8_BYTES or "
                f"UTF16_UNITS - or leave it out, which means characters."
            )

        allowed = limits.max_text_length
        if allowed is None:
            pytest.skip(
                f"{platform.name} declares no max_text_length, so there is "
                f"nothing here to count."
            )

        # A post as long as this network allows, written in a letter that
        # takes more than one of everything else. It is built through
        # `make_post` so that a network with no text-only post gets one it
        # will actually look at, rather than being refused for the wrong
        # reason and passing this check by accident.
        fits = _A_BIG_LETTER * _copies_that_fit(limits, allowed)
        before = len(sent)
        try:
            await platform.publish(connection, self.make_post(fits))
        except InvalidPostError:
            if len(sent) == before:
                pytest.fail(
                    f"{platform.name} says its limit of {allowed} is counted "
                    f"in {counted.in_words}, then refused a post of "
                    f"{measure_text(fits, counted)} {counted.in_words} "
                    f"without asking the network at all. Either it is "
                    f"counting something else - "
                    f"`socialchimp.features.measure_text` counts whichever "
                    f"way your Limits says - or the post was missing "
                    f"something else your network insists on, which is what "
                    f"make_post is for."
                )
        except SocialChimpError:
            # The network itself said no. Not our business here: the post
            # got past the length check, which is all this half asked.
            pass

        # And one more than it allows. For every way of counting but
        # characters this is *fewer* characters than the limit, so a
        # platform using Python's own len sends it and is refused.
        each = measure_text(_A_BIG_LETTER, counted)
        too_long = _A_BIG_LETTER * (allowed // each + 1)
        before = len(sent)
        refused: Exception | None = None
        try:
            await platform.publish(connection, self.make_post(too_long))
        except SocialChimpError as problem:
            refused = problem

        if len(sent) != before:
            pytest.fail(
                f"{platform.name} sent a post of "
                f"{measure_text(too_long, counted)} {counted.in_words} to "
                f"the network, having said it allows {allowed}. Check the "
                f"post first - `socialchimp.features.check_post` counts "
                f"whichever way your Limits says - so a mistake costs "
                f"nothing against the rate limit and the message says what "
                f"is wrong."
            )

        if not isinstance(refused, InvalidPostError):
            answered = type(refused).__name__ if refused is not None else "nothing"
            pytest.fail(
                f"{platform.name} answered a post of "
                f"{measure_text(too_long, counted)} {counted.in_words}, over "
                f"its own limit of {allowed}, with {answered}. A post that "
                f"breaks a declared limit is an InvalidPostError, which is "
                f"the one an app can catch and explain to a person."
            )

    async def test_a_text_only_post_is_refused_when_it_cannot_post_text(
        self,
    ) -> None:
        """A platform without POST_TEXT turns words away, and says why.

        YouTube has no text-only post at all: everything on it is a video,
        and its community posts are not in the API. A platform in that
        position has to say so plainly, because `Post(text="hello")` is the
        first thing anybody tries.
        """
        platform = self.platform
        if Feature.POST_TEXT in platform.features:
            pytest.skip(
                f"{platform.name} lists Feature.POST_TEXT, so a post of "
                f"words is one it should take."
            )

        connection = self.connection_or_skip()

        try:
            await platform.publish(connection, Post(text="just some words"))
        except NotSupportedError:
            return
        except Exception as error:
            pytest.fail(
                f"{platform.name} does not list Feature.POST_TEXT, so a post "
                f"of words alone should raise NotSupportedError, naming what "
                f"to attach instead. It raised {type(error).__name__}: "
                f"{error}. NotSupportedError is the one an app can catch and "
                f"explain to a person."
            )

        pytest.fail(
            f"{platform.name} does not list Feature.POST_TEXT but took a "
            f"post of words anyway. Whatever it did with them, it was not "
            f"what the caller asked for - raise NotSupportedError instead."
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
                    f"than learning what nine networks each throw."
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
