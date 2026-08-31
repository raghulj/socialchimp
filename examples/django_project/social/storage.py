"""The five methods socialchimp calls, written as ordinary Django ORM code.

No `async`, no base class to inherit from, no import of anything you have to
subclass. socialchimp accepts this class because it has the right five
methods, which is what a Protocol means: mypy checks the shape, and nothing
at runtime asks who your parents were.

`settings.SOCIALCHIMP` names this class under `SYNC_STORAGE`, and
`socialchimp.contrib.django.get_client()` wraps it in `orm_storage` before
handing it to `SocialChimp`.

**Why it is written blocking, and why `orm_storage` rather than
`sync_storage`.** socialchimp is async; the Django ORM is not. Both wrappers
take these same five methods and run them somewhere an event loop can wait
for. The difference is *where*. `sync_storage` uses any spare thread, which
is right everywhere except Django: Django keeps one database connection per
thread and a transaction belongs to the thread that opened it, so ORM code
run on a pool thread gets a *second* connection, outside the request's
transaction. It cannot see writes the request has not committed, its own
writes land in a transaction nobody rolls back with the request, and if the
request is holding a row lock the two can sit waiting for each other.
`orm_storage` runs these methods back on the thread the request arrived on,
which is where Django expects them.

**Three rules, and all three are quiet when broken.**

- Return `None` when there is nothing. Not connected yet is a normal state,
  not a fault.
- `delete_connection` says nothing when the row has already gone. Retries
  happen.
- `save_connection` replaces. It is called when an account is connected and
  again after **every token renewal**, and on TikTok, Bluesky and Pinterest
  a renewal that never reaches the database disconnects the account for
  good, because the refresh token it replaced has already stopped working.
"""

from __future__ import annotations

from django.conf import settings

from socialchimp import AppCredentials, Connection, Token

from .models import SocialApp, SocialConnection

# Bluesky is the one network with no app to register anywhere: people sign
# in with an app password, and nothing about your app is ever sent. Yet
# `start_login` looks up app credentials for every network without exception
# and refuses when there are none, so there has to be a row-shaped answer
# here. These two values go nowhere - the Bluesky platform is handed them
# and ignores them.
_BLUESKY_HAS_NO_APP = AppCredentials(
    platform="bluesky",
    host=None,
    client_id="not-used-by-bluesky",
    client_secret="not-used-by-bluesky",  # noqa: S106
)


def _to_connection(row: SocialConnection) -> Connection:
    """Turn one of our rows into the object socialchimp works in.

    Args:
        row: The row.

    Returns:
        The connection.
    """
    return Connection(
        id=row.pk,
        platform=row.platform,
        # "" is how this table spells "there is only one server". socialchimp
        # spells it None, and the two are not interchangeable: `get_app` is
        # keyed on the host, so an empty string would look for a different
        # row from the one that was saved.
        host=row.host or None,
        account_id=row.account_id,
        account_name=row.account_name,
        token=Token(
            access_token=row.access_token,
            # `or None`, not the bare column. A TextField gives back "" for
            # empty, and socialchimp reads an empty string as a refresh
            # token that exists and does not work - so renewal is attempted,
            # fails, and the account looks broken.
            refresh_token=row.refresh_token or None,
            # Both of these are aware, because USE_TZ is on. socialchimp
            # refuses a naive datetime at the door, on purpose: a naive one
            # compares wrongly against an aware one and nothing says so.
            expires_at=row.expires_at,
            refresh_token_expires_at=row.refresh_token_expires_at,
        ),
        scopes=tuple(row.scopes),
        extra=row.extra,
    )


class ConnectionStorage:
    """Connections and app credentials, over the Django ORM."""

    def get_connection(self, connection_id: str) -> Connection | None:
        """Look up one connected account.

        Args:
            connection_id: The id socialchimp gave this connection.

        Returns:
            The connection, or `None` when there is no such row.
        """
        row = SocialConnection.objects.filter(pk=connection_id).first()
        # None rather than an exception. "Nobody has connected this yet" is
        # something that happens all day and is not a fault.
        return _to_connection(row) if row is not None else None

    def save_connection(self, connection: Connection) -> None:
        """Write a connection, replacing any earlier one with the same id.

        Args:
            connection: What to write.
        """
        # update_or_create, never create. This runs on every token renewal
        # as well as on the first connection, and an insert would either
        # collide or leave the old dead token in place.
        SocialConnection.objects.update_or_create(
            pk=connection.id,
            defaults={
                "platform": connection.platform,
                "host": connection.host or "",
                "account_id": connection.account_id,
                "account_name": connection.account_name,
                "access_token": connection.token.access_token,
                "refresh_token": connection.token.refresh_token or "",
                "expires_at": connection.token.expires_at,
                "refresh_token_expires_at": (connection.token.refresh_token_expires_at),
                "scopes": list(connection.scopes),
                "extra": connection.extra,
            },
        )

    def delete_connection(self, connection_id: str) -> None:
        """Remove a connection, quietly if it has already gone.

        Args:
            connection_id: The id socialchimp gave this connection.
        """
        # `.filter(...).delete()` rather than `.get(...)`, so a second
        # attempt is a no-op instead of a DoesNotExist. socialchimp retries.
        SocialConnection.objects.filter(pk=connection_id).delete()

    def get_app(self, platform: str, host: str | None) -> AppCredentials | None:
        """Look up this app's own identity on one network.

        Keyed by network **and** server, because every Mastodon server is a
        separate place: an app registered on mastodon.social means nothing
        on fosstodon.org. `host` is `None` for the seven networks that have
        only one server.

        Args:
            platform: Which network.
            host: Which server, or `None`.

        Returns:
            The credentials, or `None` when this app is not registered with
            that network yet. `None` is the useful answer - socialchimp
            turns it into a message naming the network and saying where to
            register, which beats sending an empty client id and reading
            whatever the network says about that.
        """
        # 1. A row socialchimp wrote itself. Mastodon's `create_app` lands
        #    here, once per server.
        row = SocialApp.objects.filter(platform=platform, host=host or "").first()
        if row is not None:
            return AppCredentials(
                platform=row.platform,
                host=row.host or None,
                client_id=row.client_id,
                client_secret=row.client_secret,
            )

        # 2. Bluesky, which has no app anywhere.
        if platform == "bluesky":
            return _BLUESKY_HAS_NO_APP

        # 3. The seven that were typed into a developer portal by a human,
        #    and read here out of the environment. A network with nothing
        #    configured gets None, which is exactly right.
        configured = settings.SOCIAL_APPS.get(platform)
        if configured is None:
            return None
        return AppCredentials(
            platform=platform,
            host=host,
            client_id=configured["client_id"],
            client_secret=configured["client_secret"],
        )

    def save_app(self, app: AppCredentials) -> None:
        """Write this app's identity on one network and server.

        Called by socialchimp after it registers an app for you, which today
        means Mastodon and nothing else. The other seven never reach this
        method, and the row it writes is what makes registering happen once
        per server rather than once per sign-in - registering again wastes a
        record on somebody else's server and hands back a different id and
        secret from the one people already approved.

        Args:
            app: The credentials to write.
        """
        SocialApp.objects.update_or_create(
            platform=app.platform,
            host=app.host or "",
            defaults={
                "client_id": app.client_id,
                "client_secret": app.client_secret,
            },
        )
