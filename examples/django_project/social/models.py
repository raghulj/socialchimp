"""Three tables. socialchimp owns none of them.

The library has no models and runs no migrations, on any framework. It calls
five methods (`social/storage.py`) and where the rows go is entirely this
app's business. That is why the same library serves Django, FastAPI and
Flask, and it is why the columns below are named the way this project would
name them rather than the way a library would.

- `SocialConnection` - one social account somebody has connected.
- `SocialApp` - your app's own identity on one network, for the one network
  that hands it to you at runtime.
- `PostAttempt` - what this app did and what came back, including the
  failures. socialchimp raises; writing it down is our job.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.db import models
from django.utils import timezone

if TYPE_CHECKING:
    from django.db.models import QuerySet


class SocialConnectionManager(models.Manager):
    """Questions this app asks about its own connections.

    Worth knowing where the line is: **socialchimp's `Storage` has exactly
    five methods and not one of them lists anything.** It can fetch a
    connection by id, save one, delete one, and read and write your app
    credentials - and that is the whole of it, on purpose, because a library
    that starts asking your database questions starts owning your schema.

    So anything that reads across connections is a query of yours, over your
    own columns. Which is why `refresh_token_expires_at` below is a real
    column and not a key inside the `extra` blob: a value buried in JSON is a
    value nothing can ever find.
    """

    def refresh_running_out(self, within: timedelta = timedelta(days=7)) -> QuerySet:
        """Find accounts that will need signing in again soon.

        This is the one expiry a person has to act on. An **access** token
        running out is not a problem - socialchimp renews it before a post,
        under a lock, and writes the new one back through `save_connection`.
        A **refresh** token running out is the end of the line: there is
        nothing left to renew with, and the only fix is the person signing
        in again.

        Pinterest is the network that makes this real. Its refresh token
        lasts sixty days and renewing an access token does not extend it, so
        an account nobody has posted from since the summer is already gone.
        Asking this question on a schedule is how you put "reconnect
        Pinterest" in front of somebody a week early rather than the morning
        a post fails.

        Args:
            within: How far ahead to look.

        Returns:
            The connections whose refresh token runs out inside that window.
            Rows where the network never told us are left out - not knowing
            is not the same as running out.
        """
        return self.filter(
            refresh_token_expires_at__isnull=False,
            refresh_token_expires_at__lte=timezone.now() + within,
        )


class SocialConnection(models.Model):
    """One social account somebody has linked to this app."""

    # socialchimp chooses this value, and it is worth keeping exactly as
    # given. Each platform names a connection after the account it is for -
    # "facebook:<page id>", "tiktok:<open id>", "mastodon:<server>:<id>" -
    # and the webhook relies on it: Meta tells you which Page something
    # happened on and nothing at all about which of your rows, so naming the
    # row after the Page is what makes the two line up with no lookup table.
    id = models.CharField(primary_key=True, max_length=255)

    platform = models.CharField(max_length=32)

    # Which server, for the networks that have more than one. Empty rather
    # than NULL, because "" and NULL both mean "there is only one server"
    # and having two ways to say it means every query needs to handle both.
    # `storage.py` turns "" back into the None socialchimp expects.
    host = models.CharField(max_length=255, blank=True, default="")

    account_id = models.CharField(max_length=255)
    account_name = models.CharField(max_length=255)

    # Worth encrypting at rest before this goes anywhere real. A Facebook
    # Page token has no expiry at all, so it works forever for whoever gets
    # to read this column.
    access_token = models.TextField()
    refresh_token = models.TextField(blank=True, default="")

    expires_at = models.DateTimeField(null=True, blank=True)

    # When the *refresh* token itself runs out, which is a different thing
    # and only Pinterest tells us. Its refresh token lasts sixty days and
    # renewing an access token does not extend it, so an account nobody has
    # posted from since the summer needs signing in again. Keeping the date
    # is what lets you put "reconnect Pinterest" in front of somebody before
    # a post fails instead of after.
    refresh_token_expires_at = models.DateTimeField(null=True, blank=True)

    scopes = models.JSONField(default=list)

    # Whatever one network needs remembering that no other network has: a
    # page id, a channel id, a Pinterest board to pin to by default.
    extra = models.JSONField(default=dict)

    connected_at = models.DateTimeField(auto_now_add=True)

    # Touched on every token renewal as well as on connection, because
    # `save_connection` is called both times.
    updated_at = models.DateTimeField(auto_now=True)

    objects = SocialConnectionManager()

    class Meta:
        ordering = ("platform", "account_name")

    def __str__(self) -> str:
        """Name this connection the way a person would."""
        return f"{self.account_name} ({self.platform})"


class SocialApp(models.Model):
    """Your app's own identity on one network and one server.

    Only Mastodon fills this in: it is the single network socialchimp can
    register an app on for you, and it has to be registered again on every
    server, so the credentials arrive through `Storage.save_app` at runtime
    and have to go somewhere.

    The other seven networks that need an app were typed into a developer
    portal by a human, and their id and secret live in the environment
    instead - see `settings.SOCIAL_APPS`. Both routes come out of the same
    `get_app` method, so socialchimp never knows the difference.
    """

    platform = models.CharField(max_length=32)

    # Empty rather than NULL for the same reason as above, and here it also
    # matters for the constraint below: in SQL, NULL is not equal to NULL,
    # so a unique index over a nullable column happily stores the same row
    # twice.
    host = models.CharField(max_length=255, blank=True, default="")

    client_id = models.CharField(max_length=255)
    client_secret = models.TextField()

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=("platform", "host"), name="one_app_per_platform_and_host"
            ),
        )

    def __str__(self) -> str:
        """Name this app by its network and server."""
        return f"{self.platform} on {self.host or 'its only server'}"


class PostAttempt(models.Model):
    """What this app tried to publish, and what came back.

    Every post is written down here, whether it worked or not. That is the
    whole of socialchimp's error policy from this side: the library raises
    at the call that went wrong and stops, and deciding what a failure means
    is the app's job. This table is one of the two things this app decides
    to do about it; the other is a message on the page.
    """

    # Plain text rather than a foreign key. The record of what happened
    # should outlive the connection it happened on - somebody disconnecting
    # a Page next week should not quietly delete the history of what was
    # posted to it.
    connection_id = models.CharField(max_length=255)
    platform = models.CharField(max_length=32)
    account_name = models.CharField(max_length=255)

    text = models.TextField(blank=True)
    attempted_at = models.DateTimeField(auto_now_add=True)

    # What socialchimp handed back, when it handed anything back.
    post_id = models.CharField(max_length=255, blank=True, default="")
    url = models.URLField(blank=True, default="")

    # The name of a `PostState`: DONE, SCHEDULED, PROCESSING,
    # WAITING_FOR_PERSON or FAILED. Kept as the name rather than a number so
    # that reading a row tells you what happened without a lookup - and
    # WAITING_FOR_PERSON in particular is worth being able to see, because
    # it is the one state nothing will ever move on from on its own.
    state = models.CharField(max_length=32, blank=True, default="")

    # The refusal, in the words socialchimp used. They are already written
    # for a person to read, so there is nothing to translate.
    error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ("-attempted_at",)

    def __str__(self) -> str:
        """Say which network this went to and how it went."""
        return f"{self.platform}: {self.state or 'failed'}"

    @property
    def worked(self) -> bool:
        """Whether the network took it."""
        return not self.error
