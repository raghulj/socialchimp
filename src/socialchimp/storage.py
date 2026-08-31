"""Where connections and app credentials are kept.

socialchimp does not own a database. It does not create tables and it never
runs a migration. Instead your app provides a small class with five methods,
and socialchimp calls them when it needs to read or write something.

That keeps your schema yours, and it means the same library works on Django,
FastAPI, Flask, or no framework at all.

Start from `InMemoryStorage` to try things out, then write your own backed by
your database. See `docs/getting-started.md` for a worked example.

If the database layer you already have is a blocking one - the Django ORM, a
psycopg cursor, a SQLAlchemy session - write the five methods the ordinary
way and hand the class to `sync_storage`. Every call then runs on a spare
thread, so a slow read does not stop everything else the event loop is in the
middle of. That has nothing to do with any framework, which is why it lives
here rather than off in `socialchimp.contrib`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol, TypeVar, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable

    from socialchimp.models import AppCredentials, Connection

__all__ = [
    "InMemoryStorage",
    "RunInThread",
    "Storage",
    "SyncStorage",
    "in_a_thread",
    "sync_storage",
]

T = TypeVar("T")


@runtime_checkable
class Storage(Protocol):
    """What your app must provide so socialchimp can save things.

    Five methods. Every one is `async`. If the database layer you have is a
    blocking one, write the five methods the ordinary way and hand the class
    to `sync_storage` below, which does the wrapping for you. Django has its
    own version of that, `contrib.django.orm_storage`, because its ORM cares
    which thread it is run on.

    None of these should raise when something is missing. Return `None`
    instead, and let deleting something that is not there pass quietly.
    """

    async def get_connection(self, connection_id: str) -> Connection | None:
        """Look up one connected account.

        Args:
            connection_id: The id your app gave this connection.

        Returns:
            The connection, or `None` if there is no such connection.
        """
        ...

    async def save_connection(self, connection: Connection) -> None:
        """Write a connection, replacing any earlier one with the same id.

        Called when an account is first connected, and again every time a
        token is renewed.

        Args:
            connection: The connection to write.
        """
        ...

    async def delete_connection(self, connection_id: str) -> None:
        """Remove a connection. Quiet if it is already gone.

        Args:
            connection_id: The id your app gave this connection.
        """
        ...

    async def get_app(self, platform: str, host: str | None) -> AppCredentials | None:
        """Look up your app's credentials for one network.

        Args:
            platform: Which network, for example `"mastodon"`.
            host: Which server, for networks that have more than one.
                `None` for networks with a single server.

        Returns:
            The credentials, or `None` if none are stored yet.
        """
        ...

    async def save_app(self, app: AppCredentials) -> None:
        """Write your app's credentials for one network.

        Mostly used after socialchimp registers an app on a Mastodon server
        for you, since that has to happen once per server.

        Args:
            app: The credentials to write.
        """
        ...


class InMemoryStorage:
    """Storage that keeps everything in memory and forgets it on restart.

    Useful for tests, examples and a first look at the library. Do not use
    it in production - every restart disconnects every account.
    """

    def __init__(self) -> None:
        """Start with nothing stored."""
        self._connections: dict[str, Connection] = {}
        self._apps: dict[tuple[str, str | None], AppCredentials] = {}

    async def get_connection(self, connection_id: str) -> Connection | None:
        """Look up one connected account.

        Args:
            connection_id: The id your app gave this connection.

        Returns:
            The connection, or `None` if there is no such connection.
        """
        return self._connections.get(connection_id)

    async def save_connection(self, connection: Connection) -> None:
        """Write a connection, replacing any earlier one with the same id.

        Args:
            connection: The connection to write.
        """
        self._connections[connection.id] = connection

    async def delete_connection(self, connection_id: str) -> None:
        """Remove a connection. Quiet if it is already gone.

        Args:
            connection_id: The id your app gave this connection.
        """
        self._connections.pop(connection_id, None)

    async def get_app(self, platform: str, host: str | None) -> AppCredentials | None:
        """Look up your app's credentials for one network.

        Args:
            platform: Which network.
            host: Which server, or `None`.

        Returns:
            The credentials, or `None` if none are stored yet.
        """
        return self._apps.get((platform, host))

    async def save_app(self, app: AppCredentials) -> None:
        """Write your app's credentials for one network.

        Args:
            app: The credentials to write.
        """
        self._apps[app.key] = app


@runtime_checkable
class SyncStorage(Protocol):
    """`Storage`, written the ordinary blocking way.

    The same five methods, none of them `async`. Most apps with a database
    already have a layer like this - the Django ORM, a psycopg cursor, a
    SQLAlchemy session - and there is no reason to rewrite it as async code
    just to keep socialchimp happy.

    Hand one to `sync_storage` and it becomes a `Storage` the core can use.
    """

    def get_connection(self, connection_id: str) -> Connection | None:
        """Look up one connected account.

        Args:
            connection_id: The id your app gave this connection.

        Returns:
            The connection, or `None` if there is no such connection.
        """
        ...

    def save_connection(self, connection: Connection) -> None:
        """Write a connection, replacing any earlier one with the same id.

        Args:
            connection: The connection to write.
        """
        ...

    def delete_connection(self, connection_id: str) -> None:
        """Remove a connection. Quiet if it is already gone.

        Args:
            connection_id: The id your app gave this connection.
        """
        ...

    def get_app(self, platform: str, host: str | None) -> AppCredentials | None:
        """Look up your app's credentials for one network.

        Args:
            platform: Which network.
            host: Which server, or `None`.

        Returns:
            The credentials, or `None` if none are stored yet.
        """
        ...

    def save_app(self, app: AppCredentials) -> None:
        """Write your app's credentials for one network.

        Args:
            app: The credentials to write.
        """
        ...


class RunInThread(Protocol):
    """Runs one piece of blocking work without blocking the event loop.

    There is more than one right answer to this, which is why it is a
    setting rather than a decision. `in_a_thread` hands the work to any
    spare thread, which is what a plain app wants. Django wants it run on
    the thread the request arrived on, because that is where its database
    connection lives - see `socialchimp.contrib.django`.
    """

    async def __call__(self, work: Callable[[], T]) -> T:
        """Run the work and hand back what it returned.

        Args:
            work: The blocking call, already given its arguments.

        Returns:
            Whatever the work returned.
        """
        ...


async def in_a_thread(work: Callable[[], T]) -> T:
    """Run blocking work on a spare thread.

    Args:
        work: The blocking call, already given its arguments.

    Returns:
        Whatever the work returned.
    """
    return await asyncio.to_thread(work)


class _StorageInAThread:
    """A `SyncStorage` dressed up as the `Storage` the core asks for."""

    def __init__(self, inner: SyncStorage, run: RunInThread) -> None:
        """Wrap one blocking storage class.

        Args:
            inner: The storage class you wrote.
            run: How to run one of its methods.
        """
        self._inner = inner
        self._run = run

    async def get_connection(self, connection_id: str) -> Connection | None:
        """Look up one connected account.

        Args:
            connection_id: The id your app gave this connection.

        Returns:
            The connection, or `None`.
        """
        return await self._run(lambda: self._inner.get_connection(connection_id))

    async def save_connection(self, connection: Connection) -> None:
        """Write a connection.

        Args:
            connection: The connection to write.
        """
        await self._run(lambda: self._inner.save_connection(connection))

    async def delete_connection(self, connection_id: str) -> None:
        """Remove a connection.

        Args:
            connection_id: The id your app gave this connection.
        """
        await self._run(lambda: self._inner.delete_connection(connection_id))

    async def get_app(self, platform: str, host: str | None) -> AppCredentials | None:
        """Look up your app's credentials for one network.

        Args:
            platform: Which network.
            host: Which server, or `None`.

        Returns:
            The credentials, or `None`.
        """
        return await self._run(lambda: self._inner.get_app(platform, host))

    async def save_app(self, app: AppCredentials) -> None:
        """Write your app's credentials for one network.

        Args:
            app: The credentials to write.
        """
        await self._run(lambda: self._inner.save_app(app))


def sync_storage(inner: SyncStorage, *, run: RunInThread | None = None) -> Storage:
    """Let the core use a storage class you wrote as blocking code.

    Example:
        class MyStorage:
            def get_connection(self, connection_id):
                row = session.get(SocialAccount, connection_id)
                return row.to_connection() if row else None
            ...

        sc = SocialChimp(storage=sync_storage(MyStorage()))

    Args:
        inner: Your storage class, with the five methods written the
            ordinary way.
        run: How to run one of those methods. Left out, each call goes to a
            spare thread, which is right for anything but Django - see
            `socialchimp.contrib.django.orm_storage`.

    Returns:
        A `Storage` to hand to `SocialChimp`.
    """
    return _StorageInAThread(inner, run if run is not None else in_a_thread)
