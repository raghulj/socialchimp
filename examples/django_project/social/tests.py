"""Proof that the wiring works, with no credentials and no network.

Run it:

    python manage.py test social

Nothing here talks to a social network. The four sign-in shapes and the
posting loop run against `socialchimp.testing.FakePlatform`, registered
under the real networks' names, so **every line of the app runs unchanged** -
the same views, the same storage, the same templates. That is the point of
handing socialchimp a fake: your own code never finds out.

The webhook is the exception and runs against the **real** Facebook
platform, because answering Meta's setup check and checking Meta's signature
are pure functions over bytes. Nothing is sent anywhere, and the check being
the real one is worth more here than a fake would be - a signature checked
against reassembled JSON is the single most common way a webhook fails.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

from django.test import Client, TestCase, override_settings

from socialchimp.contrib.django import get_client
from socialchimp.errors import RateLimitError
from socialchimp.features import Feature
from socialchimp.platform import AccountChoice, LoginField
from socialchimp.registry import clear_platform_cache, register_platform
from socialchimp.registry import unregister_platform as forget_platform
from socialchimp.testing import FakePlatform

from .models import PostAttempt, SocialApp, SocialConnection

APP_SECRET = "pretend-app-secret"  # noqa: S105
VERIFY_TOKEN = "pretend-verify-token"  # noqa: S105
PAGE_ID = "101234567890"

# The same shape `settings.SOCIAL_APPS` has, with values that go nowhere.
# Mastodon is missing on purpose: its credentials come from the SocialApp
# table, written by `create_app`, and the first test below does exactly that
# before it signs anybody in.
PRETEND_APPS = {
    "facebook": {"client_id": "pretend-id", "client_secret": APP_SECRET},
    "x": {"client_id": "pretend-id", "client_secret": "pretend-secret"},
}


class Named(FakePlatform):
    """A fake that names its connections the way the real networks do.

    Every real platform names a connection after the account it is for -
    `facebook:<page id>`, `tiktok:<open id>` - and this app relies on it: the
    webhook finds a row by the id the network gave it. The plain fake calls
    every connection `fake-connection`, which would let two networks share
    one row and hide exactly the mistake the naming prevents.
    """


class PretendMastodon(Named):
    """Shape one: the ordinary redirect, and the app can be registered."""

    def __init__(self) -> None:
        """Behave the way Mastodon does."""
        super().__init__(
            name="mastodon",
            features=(
                Feature.CREATE_APP
                | Feature.POST_TEXT
                | Feature.POST_IMAGE
                | Feature.SCHEDULE
            ),
            # Mastodon's tokens do not expire, so there is nothing to renew.
            token_lifetime=None,
        )


class PretendBluesky(Named):
    """Shape two: no sign-in page, so it asks for a handle and a password."""

    def __init__(self) -> None:
        """Behave the way Bluesky does."""
        super().__init__(
            name="bluesky",
            # No SCHEDULE. Bluesky cannot, and asking is refused by name
            # rather than posted straight away.
            features=Feature.POST_TEXT | Feature.POST_IMAGE,
            ask_for=(
                LoginField(name="handle", label="Your Bluesky handle"),
                LoginField(name="app_password", label="An app password", secret=True),
            ),
        )


class PretendFacebook(Named):
    """Shape three: it stops half way to ask which Page."""

    def __init__(self) -> None:
        """Behave the way Facebook does."""
        super().__init__(
            name="facebook",
            features=(
                Feature.POST_TEXT
                | Feature.POST_IMAGE
                | Feature.SCHEDULE
                | Feature.PUSH_UPDATES
            ),
            # Giving it accounts is what makes signing in pause - and it
            # pauses even though there is only the one Page here, which is
            # what the real Facebook does too.
            accounts=(AccountChoice(id=PAGE_ID, name="Bench & Bloom", kind="page"),),
            secret=APP_SECRET,
        )


class PretendBusyNetwork(Named):
    """A network refusing everything, to prove the loop carries on past it."""

    def __init__(self) -> None:
        """Refuse every post."""
        super().__init__(
            name="x",
            features=Feature.POST_TEXT | Feature.POST_IMAGE,
            publish_fails_with=RateLimitError(
                "Too many posts for now.", retry_after=60.0
            ),
        )


@override_settings(SOCIAL_APPS=PRETEND_APPS)
class WiringTest(TestCase):
    """Everything below runs the app's own views, end to end."""

    fakes = (PretendMastodon, PretendBluesky, PretendFacebook, PretendBusyNetwork)

    def setUp(self) -> None:
        """Put the fakes in place of the real networks."""
        for fake in self.fakes:
            register_platform(fake().name, fake)
        # A `SocialChimp` remembers the platform it built the first time one
        # was asked for, and `get_client` is cached for the life of the
        # process - so the client has to be thrown away as well as the
        # registry cleared, or the second test gets the first one's network.
        clear_platform_cache()
        get_client.cache_clear()
        self.client = Client()

    def tearDown(self) -> None:
        """Put the real networks back."""
        for fake in self.fakes:
            forget_platform(fake().name)
        clear_platform_cache()
        get_client.cache_clear()

    def use_the_real_networks(self) -> None:
        """Take the fakes away again, part way through one test."""
        for fake in self.fakes:
            forget_platform(fake().name)
        clear_platform_cache()
        get_client.cache_clear()

    # -- shape one: the ordinary redirect ---------------------------------

    def test_the_redirect_shape_connects_an_account(self) -> None:
        """Mastodon: register the app, send them off, take them back."""
        # Mastodon needs an app on that server first, and is the one network
        # socialchimp can register one on. Nothing about it is in the
        # settings; the row this writes is where its credentials come from.
        self.client.post("/social/register-app/mastodon", {"host": "example.social"})

        started = self.client.post(
            "/social/connect/mastodon", {"host": "example.social"}
        )

        self.assertEqual(started.status_code, 302)
        self.assertTrue(
            started["Location"].startswith("https://mastodon.example/authorize")
        )

        # Both halves of a sign-in are separate requests, so whatever the
        # first was handed has to be somewhere the second can find it.
        kept = self.client.session["socialchimp"]["mastodon"]
        self.assertEqual(kept["remember"], {"verifier": "fake-verifier"})
        self.assertEqual(kept["host"], "example.social")

        came_back = self.client.get(
            "/social/callback/mastodon", {"code": "abc", "state": kept["state"]}
        )

        self.assertEqual(came_back.status_code, 302)
        # Written through `ConnectionStorage.save_connection` by socialchimp
        # itself, before the view ever saw the step. Nothing in the view
        # saves a connection.
        row = SocialConnection.objects.get(pk="mastodon:42")
        self.assertEqual(row.platform, "mastodon")
        # A token that never expires is NULL here, not a sentinel date.
        self.assertIsNone(row.expires_at)
        # And the secrets do not stay in the session once they are of no use.
        self.assertEqual(self.client.session["socialchimp"].get("mastodon"), None)

    def test_registering_an_app_writes_the_row_mastodon_needs(self) -> None:
        """`create_app` goes through `Storage.save_app`, once per server."""
        answered = self.client.post(
            "/social/register-app/mastodon",
            {"host": "example.social", "name": "Sample"},
        )

        self.assertEqual(answered.status_code, 302)
        self.assertTrue(
            SocialApp.objects.filter(
                platform="mastodon", host="example.social"
            ).exists()
        )

    def test_a_network_that_cannot_register_an_app_says_so(self) -> None:
        """Seven of the nine were typed into a portal by a human."""
        self.use_the_real_networks()

        answered = self.client.post("/social/register-app/facebook", follow=True)

        shown = [str(message) for message in answered.context["messages"]]
        self.assertIn("does not support registering an app", " ".join(shown))
        self.assertFalse(SocialApp.objects.exists())

    # -- shape two: it asks for details -----------------------------------

    def test_the_details_shape_shows_boxes_and_connects(self) -> None:
        """Bluesky: no redirect anywhere, and no state to match up."""
        asked = self.client.post("/social/connect/bluesky")

        self.assertEqual(asked.status_code, 200)
        page = asked.content.decode()
        # In the order the platform gave them, with the secret one hidden.
        self.assertIn('name="handle"', page)
        self.assertIn('type="password"', page)

        typed = self.client.post(
            "/social/details/bluesky",
            {"handle": "someone.bsky.social", "app_password": "abcd-efgh"},
        )

        self.assertEqual(typed.status_code, 302)
        self.assertTrue(SocialConnection.objects.filter(pk="bluesky:42").exists())

    # -- shape three: which account? --------------------------------------

    def test_the_choose_shape_pauses_then_finishes(self) -> None:
        """Facebook: three requests, not two, and it always asks."""
        self.client.post("/social/connect/facebook")
        kept = self.client.session["socialchimp"]["facebook"]

        paused = self.client.get(
            "/social/callback/facebook", {"code": "abc", "state": kept["state"]}
        )

        self.assertEqual(paused.status_code, 200)
        page = paused.content.decode()
        self.assertIn("Bench &amp; Bloom", page)
        # Nothing is connected yet: the pause is a real pause.
        self.assertFalse(SocialConnection.objects.exists())
        # The resume token is in the session and not in the page. On the
        # real Facebook it carries the person's own access token.
        self.assertNotIn("fake-resume", page)
        self.assertTrue(self.client.session["socialchimp"]["facebook"]["resume_token"])

        picked = self.client.post("/social/choose/facebook", {"account_id": PAGE_ID})

        self.assertEqual(picked.status_code, 302)
        # Named after the Page, which is what lets the webhook find the row.
        self.assertTrue(
            SocialConnection.objects.filter(pk=f"facebook:{PAGE_ID}").exists()
        )

    # -- posting ----------------------------------------------------------

    def connect_one(self, fake: type[Named]) -> SocialConnection:
        """Put one connected account in the database.

        Args:
            fake: Which pretend network.

        Returns:
            The row.
        """
        connection = fake().connection()
        SocialConnection.objects.create(
            id=connection.id,
            platform=connection.platform,
            account_id=connection.account_id,
            account_name=connection.account_name,
            access_token=connection.token.access_token,
            refresh_token=connection.token.refresh_token or "",
            expires_at=connection.token.expires_at,
        )
        row: SocialConnection = SocialConnection.objects.get(pk=connection.id)
        return row

    def test_one_network_refusing_does_not_cost_the_other(self) -> None:
        """The loop is the app's, and this app chooses to carry on."""
        good = self.connect_one(PretendMastodon)
        busy = self.connect_one(PretendBusyNetwork)

        self.client.post(
            "/compose/",
            {
                "connections": [good.pk, busy.pk],
                "text": "We are open until six today.",
            },
        )

        # Both were tried and both were written down. Deleting the `try` in
        # `_publish_to_each` would stop at the first failure instead, and
        # both are choices only an app can make.
        self.assertEqual(PostAttempt.objects.count(), 2)
        self.assertEqual(PostAttempt.objects.get(platform="mastodon").state, "DONE")
        self.assertIn("Too many posts", PostAttempt.objects.get(platform="x").error)

    def test_a_network_that_cannot_schedule_is_refused_by_name(self) -> None:
        """Refused, visibly. Not posted now, and not quietly skipped."""
        bluesky = self.connect_one(PretendBluesky)
        later = datetime.now(UTC) + timedelta(days=1)

        self.client.post(
            "/compose/",
            {
                "connections": [bluesky.pk],
                "text": "Later.",
                "publish_at": later.strftime("%Y-%m-%dT%H:%M"),
            },
        )

        attempt = PostAttempt.objects.get()
        self.assertIn("bluesky", attempt.error)
        self.assertIn("scheduling", attempt.error)

    def test_a_web_address_is_refused_for_a_network_that_uploads(self) -> None:
        """The one refusal this app makes itself, and why."""
        mastodon = self.connect_one(PretendMastodon)

        self.client.post(
            "/compose/",
            {
                "connections": [mastodon.pk],
                "text": "A picture.",
                # A link and no file, to a network that sends the bytes.
                "media_url": "https://example.com/shop.jpg",
                "media_path": "",
            },
        )

        # socialchimp answers this one with a plain `ValueError` from deep
        # inside the upload rather than a `SocialChimpError`, and a loop
        # catching `SocialChimpError` would miss it - so `posting.py` says
        # no first, in a sentence of its own. Written down the same way.
        self.assertIn("uploads the file itself", PostAttempt.objects.get().error)

    def test_nothing_ticked_is_told_rather_than_ignored(self) -> None:
        """A form mistake gets the same treatment as a network's refusal."""
        self.connect_one(PretendMastodon)

        answered = self.client.post(
            "/compose/", {"text": "Nowhere to go."}, follow=True
        )

        shown = [str(message) for message in answered.context["messages"]]
        self.assertIn("Tick at least one account.", shown)
        self.assertFalse(PostAttempt.objects.exists())

    # -- the webhook, against the real Facebook platform ------------------

    @override_settings(
        SOCIAL_APPS=PRETEND_APPS,
        SOCIAL_WEBHOOK_TOKENS={"facebook": VERIFY_TOKEN},
    )
    def test_the_setup_check_is_answered_and_the_signature_is_checked(self) -> None:
        """Meta's handshake and Meta's signature, both for real."""
        self.use_the_real_networks()

        answered = self.client.get(
            "/social/webhooks/facebook",
            {
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "1158201444",
            },
        )

        self.assertEqual(answered.status_code, 200)
        self.assertEqual(answered.content, b"1158201444")

        # The wrong verify token is 403 with an empty body, which is what
        # Meta's own flow expects.
        refused = self.client.get(
            "/social/webhooks/facebook",
            {
                "hub.mode": "subscribe",
                "hub.verify_token": "not-the-token",
                "hub.challenge": "1158201444",
            },
        )
        self.assertEqual(refused.status_code, 403)

        body = json.dumps(
            {
                "object": "page",
                "entry": [
                    {
                        "id": PAGE_ID,
                        "time": 1_790_000_000,
                        "changes": [
                            {
                                "field": "feed",
                                "value": {
                                    "item": "comment",
                                    "verb": "add",
                                    "comment_id": "c-1",
                                    "message": "Lovely, thanks",
                                },
                            }
                        ],
                    }
                ],
            }
        ).encode()
        signed = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()

        taken = self.client.post(
            "/social/webhooks/facebook",
            data=body,
            content_type="application/json",
            headers={"x-hub-signature-256": f"sha256={signed}"},
        )

        self.assertEqual(taken.status_code, 200)
        self.assertEqual(taken.json(), {"ok": True, "updates": 1})

        # The same bytes signed with somebody else's secret get a 401 and
        # nothing else - no hint about which check failed.
        wrong = hmac.new(b"someone-elses-secret", body, hashlib.sha256).hexdigest()
        turned_away = self.client.post(
            "/social/webhooks/facebook",
            data=body,
            content_type="application/json",
            headers={"x-hub-signature-256": f"sha256={wrong}"},
        )

        self.assertEqual(turned_away.status_code, 401)

    def test_a_network_that_never_pushes_has_no_webhook_to_answer(self) -> None:
        """Five of the nine are asked on a timer instead."""
        self.use_the_real_networks()

        answered = self.client.post(
            "/social/webhooks/pinterest",
            data=b"{}",
            content_type="application/json",
        )

        self.assertEqual(answered.status_code, 405)

    # -- the question socialchimp cannot answer for you -------------------

    def test_finding_the_accounts_that_need_signing_in_again(self) -> None:
        """A query over this app's own columns, because there is no other."""
        soon = self.connect_one(PretendBusyNetwork)
        SocialConnection.objects.filter(pk=soon.pk).update(
            refresh_token_expires_at=datetime.now(UTC) + timedelta(days=3)
        )
        self.connect_one(PretendMastodon)

        found = list(SocialConnection.objects.refresh_running_out())

        # `Storage` has five methods and not one of them lists anything, so
        # this is ours to ask - and it only works because the date is a real
        # column rather than a key inside the `extra` blob.
        self.assertEqual([row.pk for row in found], [soon.pk])
