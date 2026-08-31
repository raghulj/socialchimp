"""The five methods socialchimp calls, written as ordinary sqlite code.

This is the whole of what the library asks of your app. Five methods, no
base class to inherit from: `Storage` and `SyncStorage` are Protocols, so a
class with the right methods fits, and mypy checks that it does at the
point where you hand it over.

**These five are blocking, and that is the normal case.** Most apps already
have a synchronous database layer - a sqlite cursor, the Django ORM, a
SQLAlchemy session - and there is no reason to rewrite it as async code to
keep socialchimp happy. Write the methods the ordinary way, hand the class
to `sync_storage`, and each call runs on a spare thread so a slow query does
not stop everything else the event loop is in the middle of.

Three rules matter more than they look:

- **Return `None` when there is nothing.** "Not connected yet" is a normal
  state, not a fault, and raising here turns it into one.
- **`delete_connection` is quiet** when the row has already gone. Retries
  happen.
- **`save_connection` replaces, it does not insert.** It is called once when
  an account is connected and again after *every* token renewal, which is
  far more often. On TikTok, Bluesky and Pinterest the old refresh token
  stops working the moment a new one is handed out, so a renewal that never
  reaches this table disconnects the account for good.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from socialchimp import AppCredentials, Connection, RawData, Token

from .db import opened

if TYPE_CHECKING:
    import sqlite3

READ_ONE = """
SELECT id, platform, host, account_id, account_name, access_token,
       refresh_token, expires_at, refresh_token_expires_at, scopes, extra
FROM social_connection WHERE id = ?
"""

READ_ALL = """
SELECT id, platform, host, account_id, account_name, access_token,
       refresh_token, expires_at, refresh_token_expires_at, scopes, extra
FROM social_connection ORDER BY platform, account_name
"""

WRITE_ONE = """
INSERT OR REPLACE INTO social_connection
    (id, platform, host, account_id, account_name, access_token,
     refresh_token, expires_at, refresh_token_expires_at, scopes, extra)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _as_time(written: object) -> datetime | None:
    """Read a stored timestamp back.

    Args:
        written: What came out of the column.

    Returns:
        The moment, or `None` for an empty column. It keeps the timezone it
        was written with, which socialchimp insists on - a naive datetime
        compares wrongly against every other time it holds and does it
        silently.
    """
    if written is None:
        return None
    return datetime.fromisoformat(str(written))


def _as_data(written: object) -> RawData:
    """Read a JSON column back as a mapping.

    Args:
        written: What came out of the column.

    Returns:
        The mapping, empty if the column held anything else.
    """
    loaded: Any = json.loads(str(written))
    return loaded if isinstance(loaded, dict) else {}


def to_connection(row: sqlite3.Row) -> Connection:
    """Turn one row into the object socialchimp works with.

    Args:
        row: A row of `social_connection`.

    Returns:
        The connection. `Connection` and `Token` are frozen, so nothing
        handed back here can be changed underneath the caller and a
        half-applied update is impossible.
    """
    scopes: Any = json.loads(str(row["scopes"]))
    return Connection(
        id=str(row["id"]),
        platform=str(row["platform"]),
        host=None if row["host"] is None else str(row["host"]),
        account_id=str(row["account_id"]),
        account_name=str(row["account_name"]),
        token=Token(
            access_token=str(row["access_token"]),
            refresh_token=(
                None if row["refresh_token"] is None else str(row["refresh_token"])
            ),
            expires_at=_as_time(row["expires_at"]),
            refresh_token_expires_at=_as_time(row["refresh_token_expires_at"]),
        ),
        scopes=tuple(str(one) for one in scopes) if isinstance(scopes, list) else (),
        extra=_as_data(row["extra"]),
    )


def fields_of(connection: Connection) -> tuple[object, ...]:
    """Flatten a connection into the values one row holds.

    Args:
        connection: What to write.

    Returns:
        The values, in the order `CONNECTION_COLUMNS` names them.
    """
    token = connection.token
    return (
        connection.id,
        connection.platform,
        connection.host,
        connection.account_id,
        connection.account_name,
        token.access_token,
        token.refresh_token,
        None if token.expires_at is None else token.expires_at.isoformat(),
        (
            None
            if token.refresh_token_expires_at is None
            else token.refresh_token_expires_at.isoformat()
        ),
        json.dumps(list(connection.scopes)),
        json.dumps(connection.extra),
    )


class SqliteStorage:
    """Connections and app credentials, kept in a sqlite file.

    The five methods socialchimp calls, plus two of this app's own. Nothing
    marks the difference: socialchimp only ever calls the five, and
    `everything` and `delete_note`-style extras are yours to add because the
    table is yours.

    Note:
        The token columns here are in the clear, which is right for an
        example and wrong for production. Encrypting them is a change to
        this file and to nothing else - which is the point of socialchimp
        never touching your database.
    """

    def __init__(self, database: Path) -> None:
        """Point the storage at a sqlite file.

        Args:
            database: Where the file lives. It is opened per call rather
                than kept, because these methods run on whatever thread is
                free and a sqlite connection belongs to one thread. See
                `db.py`.
        """
        self.database = database

    # ---- the five socialchimp calls -------------------------------------

    def get_connection(self, connection_id: str) -> Connection | None:
        """Look up one connected account.

        Args:
            connection_id: The id this app gave the connection.

        Returns:
            The connection, or `None` if there is no such row. Not an
            error: nobody has connected that account yet.
        """
        with opened(self.database) as db:
            row = db.execute(READ_ONE, (connection_id,)).fetchone()
        return None if row is None else to_connection(row)

    def save_connection(self, connection: Connection) -> None:
        """Write a connection, replacing any earlier one with the same id.

        An upsert, not an insert. This runs after every token renewal as
        well as when the account is first connected.

        Args:
            connection: What to write.
        """
        with opened(self.database) as db:
            db.execute(WRITE_ONE, fields_of(connection))

    def delete_connection(self, connection_id: str) -> None:
        """Remove a connection, quietly if it has already gone.

        Args:
            connection_id: The id this app gave the connection.
        """
        with opened(self.database) as db:
            db.execute("DELETE FROM social_connection WHERE id = ?", (connection_id,))

    def get_app(self, platform: str, host: str | None) -> AppCredentials | None:
        """Look up this app's credentials for one network.

        Keyed by network *and* server. Every Mastodon server is separate, so
        `("mastodon", "mastodon.social")` and `("mastodon", "fosstodon.org")`
        are two different rows holding two different app registrations.
        Everywhere else the host is `None`.

        Args:
            platform: Which network.
            host: Which server, or `None`.

        Returns:
            The credentials, or `None` if none are stored yet.
        """
        with opened(self.database) as db:
            row = db.execute(
                "SELECT platform, host, client_id, client_secret "
                "FROM social_app WHERE platform = ? AND host = ?",
                (platform, host or ""),
            ).fetchone()
        if row is None:
            return None
        return AppCredentials(
            platform=str(row["platform"]),
            host=str(row["host"]) or None,
            client_id=str(row["client_id"]),
            client_secret=str(row["client_secret"]),
        )

    def save_app(self, app: AppCredentials) -> None:
        """Write this app's credentials for one network.

        socialchimp calls this itself after it registers an app on a
        Mastodon server for you, which is why it is one of the five rather
        than something an app would only ever do at startup.

        Args:
            app: What to write.
        """
        with opened(self.database) as db:
            db.execute(
                "INSERT OR REPLACE INTO social_app "
                "(platform, host, client_id, client_secret) VALUES (?, ?, ?, ?)",
                (app.platform, app.host or "", app.client_id, app.client_secret),
            )

    # ---- this app's own, which socialchimp knows nothing about ----------

    def everything(self) -> list[Connection]:
        """Every connection, for the list page.

        socialchimp never asks for this - it always knows which connection
        it wants. Listing them is your app's business, which is exactly why
        the table is yours.

        Returns:
            Every connected account, oldest first.
        """
        with opened(self.database) as db:
            rows = db.execute(READ_ALL).fetchall()
        return [to_connection(row) for row in rows]
