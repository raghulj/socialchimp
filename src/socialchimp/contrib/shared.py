"""The part of the framework helpers that is the same everywhere.

Nothing here imports Django, FastAPI or Flask, and nothing here knows what a
request object looks like. Each framework gets a file of its own, and every
one of those does the same three things: take the request apart into plain
values, call something here, turn the `Reply` back into that framework's own
response.

Four pieces live here.

`Routes` is signing in and receiving a webhook, written once. Every one of
its methods is a wrapper around a `SocialChimp` method your app could call
itself, so none of this is the only way in - your own URLs, your own login
checks, or a framework nobody has written a file for are not special cases.

`Reply` is what a route decided to answer: a status, some bytes, a content
type and any headers. `status_for` is the table that turns one of our errors
into a status code.

`LoginMemory` is where a half-finished sign-in waits. The two halves of a
sign-in are two separate requests, and the second one needs what the first
was handed - so it has to be written down somewhere your app controls.

`sync_storage` and the three names around it are re-exported from
`socialchimp.storage`, where they now live. Writing your five storage methods
as ordinary blocking code has nothing to do with any framework, so an app
with no framework at all should not have to import from `contrib` to do it.
The names here go on working.
"""

from __future__ import annotations

import json
import math
from collections import OrderedDict
from dataclasses import dataclass, field
from secrets import token_urlsafe
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from urllib.parse import parse_qsl

from socialchimp.errors import (
    AuthError,
    ConfigError,
    InvalidPostError,
    NetworkError,
    NotAllowedError,
    NotFoundError,
    NotSupportedError,
    PlatformError,
    RateLimitError,
    SignatureError,
    SocialChimpError,
)
from socialchimp.events import answer_setup_check
from socialchimp.features import Feature
from socialchimp.platform import (
    AskForDetails,
    CanCheckSignature,
    CanReadPushedUpdates,
    ChooseAccount,
    SendToNetwork,
)
from socialchimp.storage import (
    RunInThread,
    SyncStorage,
    in_a_thread,
    sync_storage,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from socialchimp.client import SocialChimp
    from socialchimp.events import DeliverUpdate, Update
    from socialchimp.models import RawData
    from socialchimp.platform import LoginStep

__all__ = [
    "InMemoryLoginMemory",
    "LoginMemory",
    "Reply",
    "Routes",
    "RunInThread",
    "SyncStorage",
    "in_a_thread",
    "read_form",
    "status_for",
    "sync_storage",
    "updates_in",
]

# How much randomness goes into a state we make up for an app that did not
# choose one. The state is the key a half-finished sign-in is filed under, so
# it has to be unguessable, not merely unique.
_STATE_BYTES = 32

# How many half-finished sign-ins `InMemoryLoginMemory` keeps before it
# starts forgetting the oldest.
_DEFAULT_MEMORY_SIZE = 10_000

# Our errors and the status code each one deserves, most particular first.
# `TokenExpiredError` is an `AuthError` and both answer 401, so the order
# between those two does not matter; the order is written down anyway,
# because the next error added may not be so forgiving.
_STATUSES: tuple[tuple[type[SocialChimpError], int], ...] = (
    (SignatureError, 401),
    (AuthError, 401),
    (NotAllowedError, 403),
    (NotFoundError, 404),
    (RateLimitError, 429),
    (InvalidPostError, 400),
    (NotSupportedError, 400),
    (NetworkError, 502),
    (PlatformError, 502),
    (ConfigError, 500),
)


def status_for(error: SocialChimpError) -> int:
    """Return the status code that fits one of our errors.

    Args:
        error: What went wrong.

    Returns:
        The status to answer with. Anything we have no particular answer for
        is 500, on the basis that an error we did not plan for is our
        problem and not the caller's.
    """
    for kind, status in _STATUSES:
        if isinstance(error, kind):
            return status
    return 500


@dataclass(frozen=True, slots=True)
class Reply:
    """What a route decided to answer, before any framework is involved.

    Plain bytes and a status, so the same decision can become a FastAPI
    `Response`, a Flask one or a Django one without being decided three
    times.

    Attributes:
        status: The HTTP status code.
        body: Exactly what to send, already encoded.
        content_type: What to say the body is.
        headers: Anything else to send, such as where to redirect to.
    """

    status: int
    body: bytes
    content_type: str = "application/json"
    headers: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, data: Mapping[str, object], *, status: int = 200) -> Reply:
        """Answer with a JSON object.

        Args:
            data: What to send.
            status: The status code.

        Returns:
            The reply.
        """
        return cls(status=status, body=json.dumps(data).encode())

    @classmethod
    def text(cls, words: str, *, status: int = 200) -> Reply:
        """Answer with plain text.

        Args:
            words: What to send.
            status: The status code.

        Returns:
            The reply.
        """
        return cls(
            status=status,
            body=words.encode(),
            content_type="text/plain; charset=utf-8",
        )

    @classmethod
    def redirect(cls, url: str) -> Reply:
        """Send the person's browser somewhere else.

        Args:
            url: Where to send them.

        Returns:
            The reply.
        """
        return cls(status=302, body=b"", headers={"Location": url})

    @classmethod
    def for_error(cls, error: SocialChimpError) -> Reply:
        """Turn one of our errors into an answer.

        Args:
            error: What went wrong.

        Returns:
            The reply, with the status `status_for` chose.
        """
        if isinstance(error, SignatureError):
            # Every one of these is answered the same way, on purpose. Saying
            # which check failed - missing header, wrong digest, too old -
            # only helps whoever is guessing. See `errors.SignatureError`.
            return cls.json({"error": "Refused."}, status=401)

        headers: dict[str, str] = {}
        if isinstance(error, RateLimitError) and error.retry_after is not None:
            # Rounded up, because a client that waits the rounded-down number
            # of seconds arrives a moment early and is refused again.
            headers["Retry-After"] = str(math.ceil(error.retry_after))

        return cls(
            status=status_for(error),
            body=json.dumps({"error": str(error), "platform": error.platform}).encode(),
            headers=headers,
        )


def read_form(body: bytes) -> dict[str, str]:
    """Read the values out of a form's body.

    Used instead of each framework's own form parsing, so that all three
    behave identically and none of them needs an extra package installed to
    read an ordinary HTML form.

    Args:
        body: The raw body of a form post.

    Returns:
        The values, by name.
    """
    return dict(parse_qsl(body.decode()))


@runtime_checkable
class LoginMemory(Protocol):
    """Where a half-finished sign-in waits for the person to come back.

    Signing in is two requests. The first one is handed something the second
    one needs - the secret half of a PKCE pair, which server the person
    named, and later the resume token from `ChooseAccount`. socialchimp
    cannot keep any of that for you: the person can be sent away by one web
    worker and come back to another, so anything held in one process works
    on your laptop and fails in production.

    Everything is filed under the sign-in's `state`, which is the one value
    that makes the round trip through the network.

    Back this with whatever your app already has - a session, a Redis key
    with a short life, a small table. `InMemoryLoginMemory` is here to try
    things out with.
    """

    async def keep(self, state: str, data: RawData) -> None:
        """Write down what the rest of this sign-in will need.

        Args:
            state: The sign-in's state, which is the key.
            data: What to keep. Plain JSON-shaped data.
        """
        ...

    async def look_up(self, state: str) -> RawData | None:
        """Read back what was kept for one sign-in.

        Args:
            state: The sign-in's state.

        Returns:
            What was kept, or `None` if there is nothing under that state.
        """
        ...

    async def forget(self, state: str) -> None:
        """Throw away one sign-in's notes. Quiet if there are none.

        Args:
            state: The sign-in's state.
        """
        ...


class InMemoryLoginMemory:
    """A memory that lives in one process and is lost on restart.

    Fine for trying things out and for tests. Not fine in production: two
    web workers do not share it, so a person sent away by one and returning
    to another is told their sign-in has expired, and every restart loses
    every sign-in in flight.

    Use your session, or a Redis key, or a small table instead.

    What is kept is capped, so that abandoned sign-ins cannot fill up the
    process. Once it is full the oldest are forgotten first.
    """

    def __init__(self, max_size: int = _DEFAULT_MEMORY_SIZE) -> None:
        """Start with nothing remembered.

        Args:
            max_size: How many half-finished sign-ins to hold before
                forgetting the oldest.
        """
        self._max_size = max_size
        self._kept: OrderedDict[str, RawData] = OrderedDict()

    async def keep(self, state: str, data: RawData) -> None:
        """Write down what the rest of this sign-in will need.

        Args:
            state: The sign-in's state, which is the key.
            data: What to keep.
        """
        self._kept[state] = data
        while len(self._kept) > self._max_size:
            self._kept.popitem(last=False)

    async def look_up(self, state: str) -> RawData | None:
        """Read back what was kept for one sign-in.

        Args:
            state: The sign-in's state.

        Returns:
            What was kept, or `None`.
        """
        return self._kept.get(state)

    async def forget(self, state: str) -> None:
        """Throw away one sign-in's notes.

        Args:
            state: The sign-in's state.
        """
        self._kept.pop(state, None)


def updates_in(
    pusher: CanCheckSignature,
    body: bytes,
    headers: Mapping[str, str],
) -> list[Update]:
    """Return everything one pushed message carries.

    Meta puts several changes in a single message - a comment and a like on
    the same post arrive together - so reading only the first would quietly
    lose the rest, and nothing anywhere would say so.

    A network that sends one thing per message says so by having no
    `read_updates`, which is also what a platform written before that existed
    looks like. Those still work.

    Args:
        pusher: The platform the message came from, already checked.
        body: The request body, untouched.
        headers: The request headers.

    Returns:
        Every update in the message, in the order the network sent them.
    """
    if isinstance(pusher, CanReadPushedUpdates):
        return list(pusher.read_updates(body))
    return [pusher.read_update(body, headers)]


def _no_webhook_secret(platform: str) -> str:
    """Say that we cannot check anything this network sends.

    Args:
        platform: Which network sent it.

    Returns:
        The message, saying what to do about it.
    """
    return (
        f"No webhook secret is stored for {platform}, so nothing it sends "
        f"can be checked. Add it to the secrets given to Routes."
    )


@dataclass(frozen=True, slots=True)
class _Webhooks:
    """What receiving a webhook takes: how to check one, and where it goes.

    The two are kept together because neither is any use alone. Secrets with
    no `deliver` would check a real update and then throw it away, and
    `Routes` refuses to be built that way - see `_webhooks_from`.

    Attributes:
        secrets: The secret each network signs its webhooks with, by network
            name.
        deliver: Where a checked update goes.
    """

    secrets: Mapping[str, str]
    deliver: DeliverUpdate


def _webhooks_from(
    secrets: Mapping[str, str] | None,
    deliver: DeliverUpdate | None,
) -> _Webhooks | None:
    """Pair up what receiving a webhook takes, or refuse the pairing.

    Args:
        secrets: The secret each network signs its webhooks with.
        deliver: Where a checked update goes.

    Returns:
        The pair, or `None` when these routes receive no webhooks at all,
        which is fine - plenty of apps only sign people in.

    Raises:
        ConfigError: If there are secrets but nowhere to hand updates on to.
    """
    if deliver is None:
        if secrets:
            message = (
                "These routes were given webhook secrets but no deliver, so "
                "an update that arrived and passed its signature check would "
                "be thrown away, and the network would be told it was "
                "handled. Pass deliver=dispatcher.deliver. If you only want "
                "to see them while you get the URL working, pass a function "
                "of your own that writes them to your log - then dropping "
                "them is your decision rather than a surprise."
            )
            raise ConfigError(message)
        return None
    return _Webhooks(secrets=secrets if secrets is not None else {}, deliver=deliver)


class Routes:
    """Signing in and receiving a webhook, with no framework in sight.

    Each method takes plain values - a network's name, a mapping of query
    values, the raw bytes of a body - and hands back a `Reply`. A framework
    file does the taking apart and the putting back together, and nothing
    else.

    Every method is a wrapper around a `SocialChimp` method you could call
    yourself. Anything the caller did wrong, and anything a network said no
    to, comes back as a `Reply` with a sensible status, so a route never has
    to catch those. Two things are raised instead, because both are yours to
    deal with and neither is the caller's fault:

    - `ConfigError`. Something is set up wrong - a secret that was never
      stored, an app that was never registered. It would be the same mistake
      on every request, so answering a tidy 500 only buries it in a log.
      Raised, it stops you in development and shows up as an error in
      production, which is what a mistake in your own set-up deserves.
    - Whatever `deliver` raised - an `ExceptionGroup` of the handlers that
      failed, if it is `Dispatcher.deliver`. See `webhook`.

    Example:
        routes = Routes(sc, redirect_uri="https://app.example/cb/{platform}")
        reply = await routes.start("mastodon", {"host": "mastodon.social"})
    """

    def __init__(
        self,
        sc: SocialChimp,
        *,
        redirect_uri: str,
        memory: LoginMemory | None = None,
        scopes: Mapping[str, Sequence[str]] | None = None,
        secrets: Mapping[str, str] | None = None,
        setup_tokens: Mapping[str, str] | None = None,
        deliver: DeliverUpdate | None = None,
    ) -> None:
        """Say how these routes should behave.

        Args:
            sc: The client to work through. Keep one for the life of your
                process - see `SocialChimp`.
            redirect_uri: Where networks send people back to. `{platform}`
                in it is replaced by the network's name, so one address
                covers all of them. It has to match what each network's
                developer portal has on file.
            memory: Where a half-finished sign-in waits. Left out, one that
                lives in this process is used, which is fine to try things
                out with and wrong in production - see `LoginMemory`.
            scopes: Permissions to ask each network for, by network name.
                Anything not named here uses that platform's own defaults.
            secrets: The secret each network signs its webhooks with, by
                network name. Meta calls this the app secret.
            setup_tokens: The token each network's setup check quotes back,
                by network name. Meta's forms call this the verify token.
            deliver: Where a webhook's update goes. `Dispatcher.deliver`
                fits exactly. Leave it out only if these routes sign people
                in and nothing else: giving `secrets` without it is refused
                here, because it would mean checking a real update and then
                dropping it.

        Raises:
            ConfigError: If there are webhook secrets but no `deliver`.
        """
        self._sc = sc
        self._redirect_uri = redirect_uri
        self._memory = memory if memory is not None else InMemoryLoginMemory()
        self._scopes = scopes if scopes is not None else {}
        self._setup_tokens = setup_tokens if setup_tokens is not None else {}
        self._webhooks = _webhooks_from(secrets, deliver)

    def _redirect_for(self, platform: str) -> str:
        """Work out where this network should send people back to.

        Args:
            platform: Which network.

        Returns:
            The address, with the network's name filled in.
        """
        return self._redirect_uri.replace("{platform}", platform)

    def _scopes_for(self, platform: str) -> tuple[str, ...]:
        """Work out what to ask this network's permission for.

        Args:
            platform: Which network.

        Returns:
            The scopes, or an empty tuple to use the platform's defaults.
        """
        return tuple(self._scopes.get(platform, ()))

    async def start(self, platform: str, params: Mapping[str, str]) -> Reply:
        """Begin signing someone in.

        Args:
            platform: Which network, for example `"mastodon"`.
            params: The query values. `state` is yours to choose and comes
                back to you at the end; one is made up if you leave it out.
                `host` names the server, for networks that have more than
                one.

        Returns:
            A redirect to the network for most networks. For a network
            signed in to with an app password or a bot token, the fields to
            show a person, as JSON.
        """
        try:
            state = params.get("state") or token_urlsafe(_STATE_BYTES)
            host = params.get("host")
            step = await self._sc.start_login(
                platform,
                redirect_uri=self._redirect_for(platform),
                scopes=self._scopes_for(platform),
                host=host,
                state=state,
            )
            return await self._next(state, {"host": host}, step)
        except ConfigError:
            # Your set-up, not this request. See the class docstring.
            raise
        except SocialChimpError as error:
            return Reply.for_error(error)

    async def finish(self, platform: str, params: Mapping[str, str]) -> Reply:
        """Carry on after the person comes back from the network.

        Args:
            platform: Which network.
            params: The query values the network sent back, or - for a
                network that asked for details instead - what the person
                typed. Either way it has to carry the same `state` the
                sign-in started with.

        Returns:
            The connected account as JSON, or the accounts to choose
            between when the network needs to know which page or channel to
            use.
        """
        state = params.get("state")
        if not state:
            return _needs("state", "The network should have sent it back.")

        kept = await self._memory.look_up(state)
        if kept is None:
            return _unknown_state()

        try:
            step = await self._sc.finish_login(
                platform,
                callback=params,
                redirect_uri=self._redirect_for(platform),
                scopes=self._scopes_for(platform),
                host=kept.get("host"),
                state=state,
                remember=kept.get("remember"),
            )
            return await self._next(state, kept, step)
        except ConfigError:
            # Your set-up, not this request. See the class docstring.
            raise
        except SocialChimpError as error:
            return Reply.for_error(error)

    async def choose(self, platform: str, params: Mapping[str, str]) -> Reply:
        """Carry on a sign-in after the person picked which account to use.

        Args:
            platform: Which network.
            params: `state` from the sign-in, and `account_id` naming which
                of the offered accounts they picked.

        Returns:
            The connected account as JSON.
        """
        state = params.get("state")
        if not state:
            return _needs("state", "It is the one from the sign-in.")

        account_id = params.get("account_id")
        if not account_id:
            return _needs("account_id", "It is the id of the account they picked.")

        kept = await self._memory.look_up(state)
        if kept is None:
            return _unknown_state()

        resume_token = kept.get("resume_token")
        if not isinstance(resume_token, str):
            message = (
                "This sign-in did not stop to ask which account to use, so "
                "there is nothing to carry on from. Only call this after a "
                "callback answered with choose_account."
            )
            return Reply.json({"error": message}, status=400)

        try:
            step = await self._sc.choose(
                platform,
                account_id=account_id,
                resume_token=resume_token,
                redirect_uri=self._redirect_for(platform),
                scopes=self._scopes_for(platform),
                host=kept.get("host"),
                state=state,
                remember=kept.get("remember"),
            )
            return await self._next(state, kept, step)
        except ConfigError:
            # Your set-up, not this request. See the class docstring.
            raise
        except SocialChimpError as error:
            return Reply.for_error(error)

    async def _next(self, state: str, kept: RawData, step: LoginStep) -> Reply:
        """Write down whatever the next request needs, and say what to do.

        Args:
            state: The state this sign-in is filed under.
            kept: What is already written down for it.
            step: Where the sign-in got to.

        Returns:
            The reply for this step.
        """
        if isinstance(step, SendToNetwork):
            # Filed under the state the platform is actually sending, which
            # is the one that will come back in the callback. It is normally
            # the state we passed in, but a platform is free to make its own
            # when we did not choose one.
            await self._memory.keep(
                step.state,
                {**kept, "remember": dict(step.remember)},
            )
            return Reply.redirect(step.url)

        if isinstance(step, AskForDetails):
            await self._memory.keep(state, {**kept, "remember": {}})
            return Reply.json(
                {
                    "step": "ask_for_details",
                    "state": state,
                    "help_url": step.help_url,
                    "fields": [
                        {
                            "name": one.name,
                            "label": one.label,
                            "secret": one.secret,
                            "help_text": one.help_text,
                        }
                        for one in step.fields
                    ],
                }
            )

        if isinstance(step, ChooseAccount):
            # The resume token stays here and is never sent to the browser.
            # On some networks it carries the tokens themselves, so a hidden
            # form field would be handing them out - see `ChooseAccount`.
            await self._memory.keep(
                state,
                {**kept, "resume_token": step.resume_token},
            )
            return Reply.json(
                {
                    "step": "choose_account",
                    "state": state,
                    "options": [
                        {"id": one.id, "name": one.name, "kind": one.kind}
                        for one in step.options
                    ],
                }
            )

        await self._memory.forget(state)
        return Reply.json(
            {
                "step": "connected",
                "connection_id": step.connection.id,
                "platform": step.connection.platform,
                "account_name": step.connection.account_name,
            }
        )

    async def webhook(
        self,
        platform: str,
        body: bytes,
        headers: Mapping[str, str],
    ) -> Reply:
        """Receive one request a network pushed to us.

        The body must be the **raw bytes** of the request, exactly as they
        arrived. A signature is over those exact bytes, so a framework that
        parses the JSON and builds it again has already broken it - the
        spacing and the key order will not match. Read the body, pass it
        here, and let `read_update` do the parsing afterwards. This is the
        single most common reason a correct signature appears to fail.

        Args:
            platform: Which network.
            body: The request body, untouched.
            headers: The request headers.

        Returns:
            200 when the request was signed properly and every update in it
            was handed on. 401 when it was not signed properly, with nothing
            said about which check failed.

        Raises:
            ConfigError: If these routes are not set up to receive this
                network's webhooks, or the platform file is wrong about
                itself. Both are mistakes to fix rather than answers to send.
            Exception: Whatever `deliver` raised, which for
                `Dispatcher.deliver` is an `ExceptionGroup` of the handlers
                that failed. Nothing is answered, so the framework's own 500
                goes back - and a 500 is how a network is told to send the
                update again. Answering 200 for an update nothing handled
                would tell it never to bother.
        """
        try:
            pusher = self._sc.platform_for(platform)

            if Feature.PUSH_UPDATES not in pusher.features:
                raise NotSupportedError(
                    platform=pusher.name,
                    what="pushing updates to a URL of yours",
                    suggestion=(
                        "Ask it on a timer instead, with socialchimp.events.Poller."
                    ),
                )

            if not isinstance(pusher, CanCheckSignature):
                message = (
                    f"The {pusher.name} platform says it pushes updates, but "
                    f"its class has no check_signature method. That is a "
                    f"mistake in the platform file: either add it or take "
                    f"PUSH_UPDATES off its list."
                )
                raise ConfigError(message)

            webhooks = self._webhooks
            if webhooks is None:
                # No deliver was given, so there are no secrets either -
                # `Routes` refuses that pairing when it is built. These
                # routes receive no webhooks at all.
                raise ConfigError(_no_webhook_secret(platform))

            secret = webhooks.secrets.get(platform)
            if secret is None:
                raise ConfigError(_no_webhook_secret(platform))

            pusher.check_signature(body, headers, secret=secret)
            updates = updates_in(pusher, body, headers)
        except ConfigError:
            # Your set-up, not this request. See the class docstring.
            raise
        except SocialChimpError as error:
            return Reply.for_error(error)

        # Outside the try on purpose. A handler that failed is not something
        # to turn into a tidy reply: it goes up, the framework answers 500,
        # and the network sends the update again. Anything handed on before
        # the failure is skipped second time round if you gave the dispatcher
        # a `SeenUpdates`, which is what that is for.
        for update in updates:
            await webhooks.deliver(update)

        return Reply.json({"ok": True})

    async def setup_check(self, platform: str, params: Mapping[str, str]) -> Reply:
        """Answer the one-off check a network makes before it will send us anything.

        Meta does a GET at the same address with a token you chose and a
        challenge to echo back. Get it wrong and it says the URL could not
        be verified, without saying why.

        Args:
            platform: Which network.
            params: The query values from the check.

        Returns:
            The challenge as plain text, or 403 if the token was not ours.

        Raises:
            ConfigError: If no setup token is stored for this network. See
                the class docstring for why that is raised and not answered.
        """
        # Outside the try, because it is the one thing here that is your
        # set-up rather than this request, and it is meant to get out.
        expected = self._setup_tokens.get(platform)
        if expected is None:
            message = (
                f"No setup token is stored for {platform}, so there is "
                f"nothing to check this against. Add it to the "
                f"setup_tokens given to Routes - it is the value you "
                f"typed into that network's dashboard."
            )
            raise ConfigError(message)

        try:
            return Reply.text(answer_setup_check(params, expected_token=expected))
        except SignatureError:
            # 403 rather than the 401 a bad webhook signature gets. Meta's
            # own setup flow expects it, and this is not a signed request -
            # it is a token quoted back at us.
            return Reply.json({"error": "Refused."}, status=403)


def _needs(name: str, why: str) -> Reply:
    """Say that a request left out something it had to carry.

    Args:
        name: The value that is missing.
        why: A sentence saying where it should have come from.

    Returns:
        A 400 reply.
    """
    return Reply.json({"error": f"This request has no {name}. {why}"}, status=400)


def _unknown_state() -> Reply:
    """Say that we have no note of the sign-in this request claims to be.

    Returns:
        A 400 reply.
    """
    message = (
        "There is no sign-in waiting under that state. Either it was never "
        "started here, or it has been finished or forgotten already. Start "
        "the sign-in again."
    )
    return Reply.json({"error": message}, status=400)
