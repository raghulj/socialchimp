"""Keeping a connection's token usable, even when several workers run at once.

Most networks hand out an access token that stops working after an hour or
two, plus a refresh token that buys a new one. socialchimp renews the access
token for you, a little before it runs out, so a post never fails just
because a token aged out mid-request.

Two things here are easy to get wrong, and both cost real accounts:

**Renewing twice at the same time.** Bluesky, Pinterest and TikTok give you a
new refresh token every time you use the old one, and the old one stops
working immediately. If two workers renew the same connection at once, one of
them ends up holding a refresh token the network has already thrown away, and
that account is disconnected until the person signs in again. So a renewal
takes a lock first, then looks the connection up again - by then the other
worker has usually finished, and there is nothing left to do.

**Not saving the new token.** A rotated refresh token that never reaches your
database is the same disaster one step later. The new token is written
through `Storage.save_connection` before it is handed back, always.

## When the refresh token itself runs out

On most networks it does not, and there is nothing to think about. Pinterest
is the exception: its refresh token lasts sixty days, nothing renews it, and
an account nobody has posted from since the summer needs the person to sign
in again. Where a network says so, the date is on
`Token.refresh_token_expires_at`, and a renewal past it is turned away here
with a message that says which of the two tokens ran out - rather than being
sent to the network to come back as an `invalid_grant` that could mean
either. To warn somebody in advance, ask
`connection.token.refresh_token_expires_within(seconds)` while the account
still works.

## Running more than one process

The lock this uses by default is an `asyncio.Lock`, which only holds inside
one Python process. Run two web workers, or a web worker and a queue worker,
and each has its own locks - so both can still renew the same connection at
the same moment.

If that is you, pass `make_lock` and hand back a lock that every process
shares, such as one built on Redis. It needs to work with `async with` and
nothing else; see the `Lock` class below for the whole of what is expected.
socialchimp asks for one lock per connection id and keeps hold of it, so your
factory is called once per connection, not once per renewal.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Protocol, TypeAlias

from socialchimp.errors import AuthError, ConfigError, TokenExpiredError
from socialchimp.models import Connection, Token

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import TracebackType

    from socialchimp.storage import Storage

__all__ = ["GetNewToken", "Lock", "MakeLock", "TokenManager", "TokenRenewed"]

logger = logging.getLogger(__name__)

# Renew this many seconds before the token actually runs out. Long enough to
# cover a slow request and a clock that is a little off, short enough that we
# are not throwing away most of a token's life.
DEFAULT_REFRESH_BEFORE_SECONDS = 60.0


class Lock(Protocol):
    """Something that lets one renewal through at a time.

    An `asyncio.Lock` already fits, and is what you get if you say nothing.
    Anything else that works with `async with` fits too, which is how you
    plug in a lock shared between processes.
    """

    async def __aenter__(self) -> object:
        """Wait until nobody else holds this lock, then take it."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> object:
        """Let go of the lock, whether the work succeeded or not."""
        ...


# Asking a network for a new token. One connection in, one token out.
#
# `Platform.refresh` takes your app's credentials as well, because Google,
# Meta and X all sign a renewal with them - so the credentials are bound in
# before the platform's method gets here. `SocialChimp` does that for you,
# looking them up per renewal; do the same if you build one of these
# yourself. Nothing here reads storage, because a renewal for one network
# must not need to know how another network's credentials are kept.
GetNewToken: TypeAlias = "Callable[[Connection], Awaitable[Token]]"

# Making the lock for one connection id. Called once per connection.
MakeLock: TypeAlias = "Callable[[str], Lock]"

# Being told a connection now holds a new token. Keep it quick: it runs
# while the renewal is still in progress.
TokenRenewed: TypeAlias = "Callable[[Connection], None]"


def _lock_within_this_process(connection_id: str) -> Lock:
    """Make an ordinary lock that only holds inside this process.

    Args:
        connection_id: Which connection the lock is for. Not needed here,
            because `TokenManager` already keeps one lock per connection.

    Returns:
        A fresh `asyncio.Lock`.
    """
    return asyncio.Lock()


class TokenManager:
    """Hands out connections whose token is usable right now.

    Ask it for a connection and it either gives you the one you have, or
    renews the token first, saves it, and gives you that.

        tokens = TokenManager(storage, renew)
        connection = await tokens.valid_token("conn-1")

    One of these can be shared by everything in your process, and should be:
    the locks that stop two renewals colliding live on the instance, so a new
    `TokenManager` per request protects nothing.
    """

    def __init__(
        self,
        storage: Storage,
        get_new_token: GetNewToken,
        *,
        refresh_before_seconds: float = DEFAULT_REFRESH_BEFORE_SECONDS,
        make_lock: MakeLock = _lock_within_this_process,
    ) -> None:
        """Set up token renewal for one app.

        Args:
            storage: Where connections are read from and written back to.
            get_new_token: Asks a network for a new token. Wrap
                `Platform.refresh` in something that looks your app's
                credentials up first - most networks will not renew without
                them, and `SocialChimp` does exactly that when it makes one
                of these itself.
            refresh_before_seconds: How long before a token runs out to renew
                it. The default of 60 seconds leaves room for a slow request.
            make_lock: Makes the lock used while renewing one connection. The
                default only holds inside this process; pass your own, backed
                by something like Redis, if you run more than one.
        """
        self._storage = storage
        self._get_new_token = get_new_token
        self._refresh_before_seconds = refresh_before_seconds
        self._make_lock = make_lock
        self._locks: dict[str, Lock] = {}
        self._listeners: list[TokenRenewed] = []

    def on_token_renewed(self, listener: TokenRenewed) -> None:
        """Ask to be told whenever a token was renewed.

        Handy for logging, or for warming a cache of your own. socialchimp
        has already saved the connection by the time you hear about it, so
        there is nothing you must do.

        Anything your listener raises is logged and dropped. A listener
        watches; it never gets to fail a renewal.

        Args:
            listener: Called with the connection carrying its new token.
        """
        self._listeners.append(listener)

    async def valid_token(self, connection_id: str) -> Connection:
        """Return a connection whose token works right now.

        Renews the token first if it is close to running out. Safe to call
        from anywhere, as often as you like - a connection that is fine costs
        one read.

        Args:
            connection_id: The id your app gave this connection.

        Returns:
            The connection, with a token that is good for a while yet.

        Raises:
            ConfigError: If no connection is stored under that id.
            TokenExpiredError: If the token needed renewing and could not be,
                because there is no refresh token, because the refresh token
                has itself run out, or because the network refused the one
                we have. The person has to sign in again.
        """
        connection = await self._load(connection_id)
        if not self._running_out(connection):
            return connection

        async with self._lock_for(connection_id):
            # Whoever else was renewing this connection has finished by now,
            # so read it again rather than trusting what we saw outside the
            # lock. Their new token is already saved, and renewing a second
            # time would throw it away.
            connection = await self._load(connection_id)
            if not self._running_out(connection):
                return connection
            return await self._renew(connection)

    async def _load(self, connection_id: str) -> Connection:
        """Read one connection, insisting it exists.

        Args:
            connection_id: The id your app gave this connection.

        Returns:
            The stored connection.

        Raises:
            ConfigError: If storage has nothing under that id. This is not
                the network saying no - it is an id your app passed that it
                never saved, or deleted earlier. Retrying will not help.
        """
        connection = await self._storage.get_connection(connection_id)
        if connection is None:
            message = (
                f"No connection is stored with the id {connection_id!r}. "
                f"Check the id, or connect the account again."
            )
            raise ConfigError(message)
        return connection

    def _running_out(self, connection: Connection) -> bool:
        """Say whether this token should be renewed now.

        Args:
            connection: The connection to look at.

        Returns:
            True if the token runs out inside the safety window. Always False
            for a token with no expiry, such as Mastodon's - those are only
            replaced when the person revokes them, and asking for a new one
            would be a wasted request.
        """
        return connection.token.expires_within(self._refresh_before_seconds)

    def _lock_for(self, connection_id: str) -> Lock:
        """Return the one lock belonging to this connection.

        Args:
            connection_id: The id your app gave this connection.

        Returns:
            The same lock every time, so two renewals of one connection
            really do queue up behind each other.
        """
        # Nothing is awaited between the lookup and the write, so two callers
        # cannot both make a lock and each take a different one.
        lock = self._locks.get(connection_id)
        if lock is None:
            lock = self._make_lock(connection_id)
            self._locks[connection_id] = lock
        return lock

    async def _renew(self, connection: Connection) -> Connection:
        """Get a new token, save it, and tell anyone listening.

        Args:
            connection: The connection whose token is running out.

        Returns:
            The same connection carrying its new token.

        Raises:
            TokenExpiredError: If there is no refresh token, if the refresh
                token has itself run out, or if the network refused the one
                we have.
        """
        if connection.token.refresh_token is None:
            message = (
                f"The token for {connection.platform} connection "
                f"{connection.id!r} has run out and there is no refresh "
                f"token to renew it with. The person has to sign in again."
            )
            raise TokenExpiredError(message)

        # Asking anyway would come back as invalid_grant, and the message
        # for that has to guess between "expired" and "revoked". Here we
        # already know which, so we say which, and spend no request on it.
        #
        # The date is asked for separately because a network that never told
        # us one cannot have run out as far as we know, so the two always
        # answer together.
        ran_out = connection.token.refresh_token_expires_at
        if ran_out is not None and connection.token.refresh_token_is_expired:
            message = (
                f"The refresh token for {connection.platform} connection "
                f"{connection.id!r} ran out on {ran_out.isoformat()}, so "
                f"there is nothing left to renew the access token with. A "
                f"refresh token cannot itself be renewed, so the person has "
                f"to sign in again. To warn somebody before this happens, "
                f"ask token.refresh_token_expires_within(seconds) while the "
                f"account is still working."
            )
            raise TokenExpiredError(message, platform=connection.platform)

        try:
            token = await self._get_new_token(connection)
        except AuthError as refused:
            # Only a refusal means the token is gone for good. A timeout or a
            # 500 is left alone on purpose, so a retry can pick it up instead
            # of us disconnecting an account that was never really broken.
            message = (
                f"{connection.platform} would not renew the token for "
                f"connection {connection.id!r}. The refresh token has "
                f"expired or been revoked, so the person has to sign in "
                f"again."
            )
            raise TokenExpiredError(message) from refused

        renewed = connection.with_token(token)
        # Saved before anyone hears about it, and before it is handed back.
        # Where the network rotates refresh tokens, the one we just replaced
        # has already stopped working - losing the new one loses the account.
        await self._storage.save_connection(renewed)
        self._tell_listeners(renewed)
        return renewed

    def _tell_listeners(self, connection: Connection) -> None:
        """Let every listener know about a renewed token.

        Args:
            connection: The connection carrying its new token.
        """
        for listener in self._listeners:
            try:
                listener(connection)
            except Exception:
                # A listener is watching, not taking part. The token is saved
                # either way, so log the problem and carry on to the next one.
                logger.exception(
                    "A token renewal listener raised for connection %r.",
                    connection.id,
                )
