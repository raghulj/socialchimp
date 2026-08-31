"""Being told when something happens, in one shape for every network.

Networks announce a new comment or a new like in wildly different ways. Meta
posts to a URL of yours and signs the body. Telegram posts to a URL and echoes
a secret you agreed on. Discord signs with a different algorithm again.
Mastodon holds a socket open. LinkedIn, Pinterest, Reddit and Tumblr do not
tell you anything at all, so we have to check on a timer.

All of that ends up here as the same `Update`, delivered to the same handlers.
Your code should never need to know which of those actually happened.

The pieces:

- `Update` and `UpdateKind` - what happened, in one shape.
- `verify_hmac_sha256`, `verify_shared_secret`, `check_not_too_old` and
  `answer_setup_check` - proving a request really came from the network.
- `SeenUpdates` and `InMemorySeenUpdates` - not handling the same update
  twice.
- `Poller` and `poll` - checking on a timer, for networks with no push.
- `Dispatcher` - handing an update to the code that cares about it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from socialchimp.errors import SignatureError
from socialchimp.models import RawData, require_timezone

__all__ = [
    "DeliverUpdate",
    "Dispatcher",
    "FetchUpdates",
    "Handler",
    "InMemorySeenUpdates",
    "Poller",
    "SaveMarker",
    "SeenUpdates",
    "SignatureError",
    "Update",
    "UpdateKind",
    "answer_setup_check",
    "check_not_too_old",
    "poll",
    "verify_hmac_sha256",
    "verify_shared_secret",
]

logger = logging.getLogger(__name__)

# How old a signed request may be before we refuse it. Five minutes is what
# most networks themselves suggest.
DEFAULT_ALLOWED_AGE_SECONDS = 300.0


class UpdateKind(Enum):
    """What happened.

    The values are the words socialchimp uses on the wire. A network's own
    word for the same thing is translated by its platform file, so your
    handlers only ever see these.

    Anything we do not recognise becomes `UNKNOWN`, with the network's
    original word kept on `Update.kind_name`. Networks add new kinds without
    warning, and an app that only cares about comments should keep working
    the day one appears.
    """

    COMMENT_CREATED = "comment_created"
    """Someone commented on a post."""

    COMMENT_DELETED = "comment_deleted"
    """A comment was removed, by its author or by a moderator."""

    POST_PUBLISHED = "post_published"
    """A post the network was still working on is now live.

    YouTube and TikTok keep working after they accept an upload, so this can
    arrive long after `publish()` returned.
    """

    POST_FAILED = "post_failed"
    """A post the network was still working on will never go live."""

    POST_DELETED = "post_deleted"
    """A post was removed, by its author or by a moderator."""

    POST_DRAFTED = "post_drafted"
    """The network put the post in somebody's drafts for them to finish.

    TikTok can do this instead of posting straight away. Nothing is wrong,
    and nothing more will happen until a person taps a button.
    """

    REACTION_ADDED = "reaction_added"
    """Someone liked, favourited or reacted to a post."""

    MENTION = "mention"
    """Someone named this account in a post of their own."""

    CONNECTION_REVOKED = "connection_revoked"
    """The person took your app's access away.

    Delete the connection when you see this. Its token has already stopped
    working, and Meta will not tell you twice.
    """

    UNKNOWN = "unknown"
    """Something we have no name for yet. Look at `raw` to see what it was."""

    @classmethod
    def from_name(cls, name: str) -> UpdateKind:
        """Turn a word from the wire into a kind, without ever failing.

        Args:
            name: The word socialchimp uses for this kind of update.

        Returns:
            The matching kind, or `UNKNOWN` if there is no match.
        """
        try:
            return cls(name)
        except ValueError:
            return cls.UNKNOWN


@dataclass(frozen=True, slots=True)
class Update:
    """Something that happened on a social network.

    The same object whether the network pushed it to us or we found it by
    checking on a timer.

    Attributes:
        id: The network's identifier for this update. Used to spot the same
            update arriving twice.
        kind: What happened.
        platform: Which network it happened on, for example `"facebook"`.
        connection_id: Which of your connections it concerns, so you know
            whose account this is without another lookup.
        created_at: When it happened, according to the network. Always has a
            timezone.
        raw: The one thing that happened, in the network's own untouched
            words, for anything we did not model. A handler reads this
            straight - it is this update's own change and nothing else.
        kind_name: The word the network used. The same as `kind`'s own value
            for anything we recognise; for `UNKNOWN` this is where the
            network's original word is kept.
        envelope: The message this arrived in, where the network wraps
            things up. Meta puts several changes in one message and names
            the page and the time out there rather than on each change, so
            that is what this holds. Empty for a network that sends one
            thing on its own, and for an update found by asking.
    """

    id: str
    kind: UpdateKind
    platform: str
    connection_id: str
    created_at: datetime
    raw: RawData = field(default_factory=dict, repr=False)
    kind_name: str = ""
    envelope: RawData = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Check the time has a timezone and fill in the missing word.

        Raises:
            ValueError: If `created_at` has no timezone. Without one it
                compares wrongly against every other time we hold, and the
                failure is silent.
        """
        require_timezone(self.created_at, "created_at")
        if not self.kind_name:
            object.__setattr__(self, "kind_name", self.kind.value)

    @classmethod
    def from_network(
        cls,
        *,
        update_id: str,
        kind_name: str,
        platform: str,
        connection_id: str,
        created_at: datetime,
        raw: RawData | None = None,
        envelope: RawData | None = None,
    ) -> Update:
        """Build an update from a word a network gave us.

        This is what platform files use. It never fails on a word we do not
        know: the update comes back as `UNKNOWN` with the word kept.

        Args:
            update_id: The network's identifier for this update.
            kind_name: The network's word for what happened, already
                translated into socialchimp's vocabulary by the platform file.
            platform: Which network it happened on.
            connection_id: Which of your connections it concerns.
            created_at: When it happened. Must have a timezone.
            raw: The one thing that happened, untouched. Pass the change
                itself, not the message it came in - a handler should not
                have to hunt through a list for its own change.
            envelope: The message it arrived in, where the network wraps
                things up and puts the account and the time out there.

        Returns:
            The update, ready to deliver.
        """
        return cls(
            id=update_id,
            kind=UpdateKind.from_name(kind_name),
            platform=platform,
            connection_id=connection_id,
            created_at=created_at,
            raw=raw if raw is not None else {},
            kind_name=kind_name,
            envelope=envelope if envelope is not None else {},
        )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Find a header whatever case it was sent in.

    HTTP header names do not care about case, but a plain dict does. Every
    framework hands them over differently, so we look for ourselves.

    Args:
        headers: The request's headers.
        name: The header to look for.

    Returns:
        Its value, or `None` if it is not there.
    """
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def verify_hmac_sha256(
    body: bytes,
    headers: Mapping[str, str],
    *,
    secret: str,
    header_name: str = "X-Hub-Signature-256",
    prefix: str = "sha256=",
) -> None:
    """Check a signed body against the secret only you and the network know.

    This is how Meta signs what it sends to Facebook, Instagram, Threads and
    WhatsApp apps, and several other networks copy it.

    It takes the raw bytes and a plain mapping of headers on purpose. It must
    never be handed a framework's request object, because by the time one of
    those has parsed the JSON the original bytes are gone. Read the body,
    check it here, and only then parse it - re-encoding a parsed body changes
    the spacing and the key order, and the signature is over the exact bytes
    that were sent. Frameworks that parse the body for you are the single most
    common reason a correct signature appears to fail.

    Args:
        body: The request body, exactly as it arrived. Not a parsed and
            re-encoded copy of it.
        headers: The request's headers. Case does not matter.
        secret: The secret agreed with the network. Meta calls this the app
            secret.
        header_name: Which header carries the signature.
        prefix: What the network puts in front of the hex digits. Pass `""`
            for a network that sends the digits on their own.

    Raises:
        SignatureError: If the header is missing, malformed, or does not
            match. Answer 401 and stop.
    """
    sent = _header(headers, header_name)
    if sent is None:
        message = (
            f"This request has no {header_name} header, so there is nothing "
            f"to check it against. Refusing it."
        )
        raise SignatureError(message)

    if not sent.startswith(prefix):
        message = (
            f"The {header_name} header does not start with {prefix!r}, so it "
            f"is not a signature we know how to check. Refusing it."
        )
        raise SignatureError(message)

    expected = prefix + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    # Compared this way so that how long the comparison takes says nothing
    # about how much of the signature was right.
    if not hmac.compare_digest(sent, expected):
        message = (
            "The signature does not match the body. Either the body was "
            "changed on the way here, or it was signed with a different "
            "secret. If you are sure the secret is right, check that nothing "
            "parsed and rebuilt the body before it reached this function."
        )
        raise SignatureError(message)


def verify_shared_secret(
    headers: Mapping[str, str],
    *,
    secret: str,
    header_name: str = "X-Telegram-Bot-Api-Secret-Token",
) -> None:
    """Check a header that simply repeats a secret back to us.

    Telegram works this way: you give it a secret when you set up the URL, and
    it sends that same secret with everything it posts to you.

    This proves less than a signature does. It says the sender knows the
    secret; it says nothing about the body being unchanged. Only serve such a
    URL over HTTPS, and keep the secret long and random - `secrets.token_hex`
    makes a good one.

    Args:
        headers: The request's headers. Case does not matter.
        secret: The secret you gave the network.
        header_name: Which header carries it.

    Raises:
        SignatureError: If the header is missing or holds something else.
    """
    sent = _header(headers, header_name)
    if sent is None:
        message = (
            f"This request has no {header_name} header, so there is nothing "
            f"to check it against. Refusing it."
        )
        raise SignatureError(message)

    if not hmac.compare_digest(sent, secret):
        message = (
            f"The secret in {header_name} does not match the one we agreed "
            f"with the network. Refusing it."
        )
        raise SignatureError(message)


def check_not_too_old(
    sent_at: datetime | float,
    *,
    allowed_age_seconds: float = DEFAULT_ALLOWED_AGE_SECONDS,
    now: datetime | None = None,
) -> None:
    """Refuse a request that was signed too long ago.

    A signature stays correct forever. Anyone who gets hold of one request -
    from a log file, a proxy, a screenshot of a debug page - can send that
    exact request again next year and the signature will still check out.
    Refusing anything old closes that off, so run this alongside the
    signature check, not instead of it.

    Only works if the time itself is covered by the signature. Networks that
    sign a timestamp header, such as Discord, cover it; where a network puts
    the time inside the body, the body is what was signed, so it counts.

    Args:
        sent_at: When the network says it sent this. Either a datetime with a
            timezone or plain seconds since 1970, which is what most networks
            send.
        allowed_age_seconds: How old a request may be. Five minutes by
            default, which leaves room for clocks that disagree a little.
        now: The current time. Only useful in tests.

    Raises:
        ValueError: If `sent_at` is a datetime with no timezone.
        SignatureError: If the request is older than allowed.
    """
    if isinstance(sent_at, datetime):
        moment = sent_at
    else:
        moment = datetime.fromtimestamp(sent_at, UTC)

    require_timezone(moment, "sent_at")

    against = now if now is not None else datetime.now(UTC)
    age = (against - moment).total_seconds()
    if age > allowed_age_seconds:
        message = (
            f"This request was signed {age:.0f} seconds ago, and we only "
            f"accept requests up to {allowed_age_seconds:.0f} seconds old. "
            f"An old request with a correct signature can be sent again by "
            f"anyone who copied it, so we refuse it."
        )
        raise SignatureError(message)


def answer_setup_check(
    params: Mapping[str, str],
    *,
    expected_token: str,
) -> str:
    """Answer the one-off GET that Meta sends when you point it at a URL.

    Before Meta will send you anything it asks your URL a question: it does a
    GET with a token you chose and a challenge. Echo the challenge back as
    plain text and the URL starts working. Get it wrong and Meta says the URL
    could not be verified, without saying why.

    Args:
        params: The query values from the GET, such as Django's `request.GET`
            or FastAPI's `request.query_params`.
        expected_token: The token you typed into Meta's dashboard. Its own
            forms call this the verify token.

    Returns:
        The challenge. Send it back as the whole body, with a 200 and a
        content type of `text/plain`.

    Raises:
        SignatureError: If this is not a setup check, or the token is wrong.
            Answer 403 and send nothing back.
    """
    challenge = params.get("hub.challenge")
    if params.get("hub.mode") != "subscribe" or challenge is None:
        message = (
            "This is not a setup check: it has no hub.mode of 'subscribe' "
            "and a hub.challenge to answer with."
        )
        raise SignatureError(message)

    token = params.get("hub.verify_token")
    if token is None or not hmac.compare_digest(token, expected_token):
        message = (
            "The token in this setup check is not the one we expected, so it "
            "did not come from the network. Refusing it."
        )
        raise SignatureError(message)

    return challenge


@runtime_checkable
class SeenUpdates(Protocol):
    """A memory of which updates have already been handled.

    Every network promises to deliver at least once, which is a promise to
    deliver twice sometimes: a slow reply, a timeout, a retry after your
    server restarts. Without this, one comment can send two notifications or
    post two replies.
    """

    async def seen(self, update_id: str) -> bool:
        """Say whether this update has already been handled.

        Args:
            update_id: The network's identifier for the update.

        Returns:
            True if it has been handled before.
        """
        ...

    async def remember(self, update_id: str) -> None:
        """Note that this update has now been handled.

        Args:
            update_id: The network's identifier for the update.
        """
        ...


class InMemorySeenUpdates:
    """A memory that lives in one process and is lost on restart.

    Good for tests, examples and a single small server. Not good for
    production: two workers do not share it, so the same update can be
    handled once by each, and a restart forgets everything just when a
    network is most likely to retry.

    Back it with your database instead - a table of update ids with a unique
    index, and `remember` doing an insert that ignores duplicates. That gets
    you both workers agreeing and a memory that survives a restart.

    The memory here is capped so that a busy account cannot fill up the
    process. Once it is full the oldest ids are forgotten first, on the basis
    that a network that is going to retry does so within minutes.
    """

    def __init__(self, max_size: int = 10_000) -> None:
        """Start with an empty memory.

        Args:
            max_size: How many update ids to keep before forgetting the
                oldest.
        """
        self._max_size = max_size
        self._ids: OrderedDict[str, None] = OrderedDict()

    async def seen(self, update_id: str) -> bool:
        """Say whether this update has already been handled.

        Args:
            update_id: The network's identifier for the update.

        Returns:
            True if it has been handled before.
        """
        return update_id in self._ids

    async def remember(self, update_id: str) -> None:
        """Note that this update has now been handled.

        Args:
            update_id: The network's identifier for the update.
        """
        self._ids[update_id] = None
        while len(self._ids) > self._max_size:
            self._ids.popitem(last=False)


# What a handler looks like: it takes one update and does something about it.
Handler = Callable[[Update], Awaitable[None]]

# Asks a network for the items it has right now. The platform file behind it
# turns the network's own reply into `Update` objects.
FetchUpdates = Callable[[], Awaitable[Sequence[Update]]]

# Hands one update on. `Dispatcher.deliver` fits this exactly.
DeliverUpdate = Callable[[Update], Awaitable[None]]

# Writes down how far we got, so a restart carries on rather than starting
# again.
SaveMarker = Callable[[datetime], Awaitable[None]]


class Dispatcher:
    """Sends each update to the code that cares about it.

    Register handlers by kind, or one that hears about everything, then hand
    updates to `deliver`. Where they came from - a signed request from Meta,
    a socket held open to Mastodon, or `Poller` checking LinkedIn on a timer
    - makes no difference here.

    Example:
        dispatcher = Dispatcher(seen=InMemorySeenUpdates())
        dispatcher.on(UpdateKind.COMMENT_CREATED, reply_to_comment)
        await dispatcher.deliver(update)
    """

    def __init__(self, *, seen: SeenUpdates | None = None) -> None:
        """Start with no handlers registered.

        Args:
            seen: A memory of updates already handled. Given one, an update
                that arrives twice is only handled once. Leave it out and
                every update is handled every time it arrives.
        """
        self._by_kind: dict[UpdateKind, list[Handler]] = {}
        self._catch_all: list[Handler] = []
        self._seen = seen

    def on(self, kind: UpdateKind, handler: Handler) -> None:
        """Call this handler for updates of one kind.

        Args:
            kind: Which updates it wants.
            handler: What to call. Registering several for the same kind is
                fine; they run in the order they were registered.
        """
        self._by_kind.setdefault(kind, []).append(handler)

    def on_any(self, handler: Handler) -> None:
        """Call this handler for every update, whatever kind it is.

        Useful for writing everything to a log or a queue. It is also the
        only way to see updates of a kind we have no name for yet.

        Args:
            handler: What to call.
        """
        self._catch_all.append(handler)

    async def deliver(self, update: Update) -> None:
        """Hand one update to every handler that wants it.

        Handlers run one after another rather than all at once, so their
        order is the order you registered them in. A handler that raises is
        logged and the rest still run: one broken handler must not cost you
        the others.

        The update is remembered as handled only once every handler has had
        it. Remembering first would mean a crash halfway through lost the
        update for good, and a network's retry is the second chance.

        Args:
            update: What happened.
        """
        if self._seen is not None and await self._seen.seen(update.id):
            logger.debug(
                "Update %s has been handled already, so skipping it.", update.id
            )
            return

        for handler in [*self._by_kind.get(update.kind, []), *self._catch_all]:
            try:
                await handler(update)
            except Exception:
                logger.exception(
                    "A handler for update %s failed. The other handlers for "
                    "it still ran.",
                    update.id,
                )

        if self._seen is not None:
            await self._seen.remember(update.id)


class Poller:
    """Checks a network on a timer, for networks that never tell us anything.

    LinkedIn, Pinterest, Reddit and Tumblr have no way to push anything to
    you, so the only way to know about a new comment is to ask. This asks,
    works out which items are new since last time, and hands them on as the
    same `Update` objects a network that does push would have sent. Your
    handlers cannot tell the difference.

    New means "happened after the marker", where the marker is the time of
    the newest update handed on so far. Give `save_marker` somewhere to write
    it and a restart carries on from where it left off instead of going
    quiet or repeating a day's worth of comments.

    Example:
        poller = Poller(fetch=recent_comments, deliver=dispatcher.deliver)
        await poller.run_forever()
    """

    def __init__(
        self,
        *,
        fetch: FetchUpdates,
        deliver: DeliverUpdate,
        every_seconds: float = 60.0,
        since: datetime | None = None,
        save_marker: SaveMarker | None = None,
    ) -> None:
        """Set up the checking, without starting it.

        Args:
            fetch: Asks the network what it has right now, as updates. It is
                fine for this to return the same items every time; working
                out which are new is this class's job.
            deliver: Where a new update goes. `Dispatcher.deliver` fits.
            every_seconds: How long to wait between rounds. Watch the
                network's rate limit: asking every second uses up an hourly
                allowance in minutes.
            since: The marker you saved last time. Leave it out and
                everything the first round finds counts as new; pass
                `datetime.now(UTC)` to start from this moment instead.
            save_marker: Called with the new marker whenever it moves, so you
                can store it. Leave it out and the marker is lost on restart.
        """
        self._fetch = fetch
        self._deliver = deliver
        self._every_seconds = every_seconds
        self._since = since
        self._save_marker = save_marker

    def _is_new(self, update: Update) -> bool:
        """Say whether this update happened after the marker.

        Args:
            update: The update to judge.

        Returns:
            True if it is new to us.
        """
        return self._since is None or update.created_at > self._since

    async def check_once(self) -> list[Update]:
        """Do one round: ask, work out what is new, hand it on.

        Updates are handed on oldest first, so handlers see things in the
        order they happened rather than the order the network listed them.

        Returns:
            The updates that were new this round, oldest first.

        Raises:
            Exception: Whatever `fetch` or `deliver` raised. `run_forever`
                catches these; call this yourself and you handle them.
        """
        found = await self._fetch()
        new = sorted(
            (update for update in found if self._is_new(update)),
            key=lambda update: update.created_at,
        )

        for update in new:
            await self._deliver(update)

        # Moved only after everything has been handed on, so a failure part
        # way through means the next round tries those items again.
        if new:
            self._since = new[-1].created_at
            if self._save_marker is not None:
                await self._save_marker(self._since)

        return new

    async def run_forever(self) -> None:
        """Keep checking until this task is cancelled.

        A round that fails is logged and the next one still happens. Networks
        go down, tokens hiccup and rate limits bite, and none of those are a
        reason to stop checking for good. The marker does not move on a
        failed round, so nothing is skipped over.

        Raises:
            asyncio.CancelledError: When the task is cancelled. Passed on
                rather than swallowed, so shutting down actually shuts down.
        """
        try:
            while True:
                try:
                    await self.check_once()
                except Exception:
                    logger.exception(
                        "A round of checking for updates failed. Trying "
                        "again in %s seconds.",
                        self._every_seconds,
                    )
                await asyncio.sleep(self._every_seconds)
        except asyncio.CancelledError:
            logger.info("Stopped checking for updates.")
            raise


async def poll(
    *,
    fetch: FetchUpdates,
    deliver: DeliverUpdate,
    every_seconds: float = 60.0,
    since: datetime | None = None,
    save_marker: SaveMarker | None = None,
) -> None:
    """Check a network on a timer until this task is cancelled.

    The short way to write `Poller(...).run_forever()`. Use `Poller` itself
    when you want to run a single round by hand, such as from a cron job.

    Args:
        fetch: Asks the network what it has right now, as updates.
        deliver: Where a new update goes.
        every_seconds: How long to wait between rounds.
        since: The marker you saved last time.
        save_marker: Called with the new marker whenever it moves.

    Raises:
        asyncio.CancelledError: When the task is cancelled.
    """
    await Poller(
        fetch=fetch,
        deliver=deliver,
        every_seconds=every_seconds,
        since=since,
        save_marker=save_marker,
    ).run_forever()
