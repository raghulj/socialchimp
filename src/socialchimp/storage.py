"""Where connections and app credentials are kept.

socialchimp does not own a database. It does not create tables and it never
runs a migration. Instead your app provides a small class with five methods,
and socialchimp calls them when it needs to read or write something.

That keeps your schema yours, and it means the same library works on Django,
FastAPI, Flask, or no framework at all.

Start from `InMemoryStorage` to try things out, then write your own backed by
your database. See `docs/getting-started.md` for a worked example.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from socialchimp.models import AppCredentials, Connection

__all__ = ["InMemoryStorage", "Storage"]


@runtime_checkable
class Storage(Protocol):
    """What your app must provide so socialchimp can save things.

    Five methods. Every one is `async`; if your database driver is not, wrap
    the call - the Django helper does this for you so you can write ordinary
    Django ORM code.

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
