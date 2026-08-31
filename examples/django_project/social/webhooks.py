"""One address for every network that pushes: Facebook, Instagram, Threads,
TikTok.

The other five have no way to tell you anything happened, so socialchimp
asks them on a timer instead - `Account.fetch_updates` and
`socialchimp.events.Poller`, which belong in a management command rather
than in a view. Either way a handler is called with the same `Update` and
cannot tell which route it came by.

Two different requests arrive here.

**A `GET`, once.** Meta's three do this the moment you save the address in
their form: a challenge to echo back and a token you invented, to prove the
address is yours. TikTok does not - it starts sending straight away, and
`answer_setup_check` says so by name.

**A signed `POST`, every time something happens.** The signature is over the
**raw bytes** of the body. `request.body` is those bytes; `request.POST` and
`json.loads` are not. Parsing the JSON and building it again changes the
spacing and the key order, and the signature then fails on a message that
was perfectly good. This is the single most common reason a correct
signature appears to be wrong, and it looks exactly like a wrong secret.

The three secrets are three different values and mixing them up is the usual
reason Meta says the URL could not be verified without saying why:

- the **verify token**, which you invented and typed into the network's
  form. `answer_setup_check` takes this one.
- the **app secret** (TikTok calls it the client secret), from the developer
  portal. `check_signature` takes this one.
- the **app id**, which is not a secret at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from asgiref.sync import async_to_sync
from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from socialchimp import Dispatcher, InMemorySeenUpdates, UpdateKind
from socialchimp.errors import NotSupportedError, PlatformError, SignatureError

from .client import client

if TYPE_CHECKING:
    from django.http import HttpRequest

    from socialchimp import Update

# One dispatcher for the process. `InMemorySeenUpdates` drops a message that
# arrives twice, which happens all the time: Meta and TikTok both promise to
# deliver at least once, and TikTok retries for 72 hours. In production put
# something the workers share here - Redis, or a table - because this one
# only remembers inside one process.
dispatcher = Dispatcher(seen=InMemorySeenUpdates())


async def someone_commented(update: Update) -> None:
    """Somebody left a comment on one of our posts.

    Args:
        update: What happened.
    """
    # `update.connection_id` is the primary key of a SocialConnection row,
    # because socialchimp names a connection after the account it is for and
    # `storage.py` saved it under exactly that. Meta's message names a Page,
    # not one of our rows, and this is what makes the two line up with no
    # lookup table of our own.
    #
    # `update.raw` is this one comment in the network's own words.
    # `update.envelope` is the message it arrived in, where Meta puts
    # several changes together.
    print(f"comment on {update.connection_id}: {update.raw.get('message')!r}")


async def post_finished(update: Update) -> None:
    """A post the network was still encoding is now live, or has failed.

    Args:
        update: What happened.
    """
    print(f"{update.kind.name} on {update.connection_id}")


async def app_was_removed(update: Update) -> None:
    """The person took this app's access away.

    Args:
        update: What happened.
    """
    # Delete the row. Its token has already stopped working and the network
    # will not tell us twice. Written through socialchimp's storage rather
    # than the model, so this app has one way of doing it.
    #
    # This handler is async and the ORM is not, so the delete goes through
    # `Storage.delete_connection`, which `orm_storage` already runs on the
    # right thread. Reaching for `SocialConnection.objects` here instead
    # would be calling the ORM from async code, which Django refuses.
    await client().storage.delete_connection(update.connection_id)


dispatcher.on(UpdateKind.COMMENT_CREATED, someone_commented)
dispatcher.on(UpdateKind.POST_PUBLISHED, post_finished)
dispatcher.on(UpdateKind.POST_FAILED, post_finished)
dispatcher.on(UpdateKind.POST_DRAFTED, post_finished)
dispatcher.on(UpdateKind.CONNECTION_REVOKED, app_was_removed)


def _setup_check(request: HttpRequest, platform: str) -> HttpResponse:
    """Answer the one-off question a network asks before it will push.

    Args:
        request: The request.
        platform: Which network.

    Returns:
        The challenge as plain text, or a refusal.
    """
    try:
        challenge = client().answer_setup_check(
            platform,
            request.GET.dict(),
            # The value you invented and typed into the network's form. Not
            # the app secret.
            verify_token=settings.SOCIAL_WEBHOOK_TOKENS.get(platform, ""),
        )
    except NotSupportedError:
        # TikTok asks nothing before it starts sending, so a GET here is not
        # part of any flow it has.
        return HttpResponse(status=405)
    except SignatureError:
        # 403 with nothing in the body is what Meta's own flow expects.
        return HttpResponse(status=403)

    return HttpResponse(challenge, content_type="text/plain")


def _receive(request: HttpRequest, platform: str) -> HttpResponse:
    """Check a pushed message and hand every update in it on.

    Args:
        request: The request.
        platform: Which network.

    Returns:
        200 once the message has been taken.
    """
    try:
        client().check_signature(
            platform,
            # The bytes exactly as they arrived. Never parsed first.
            request.body,
            dict(request.headers.items()),
            # Meta calls this the app secret and TikTok the client secret;
            # it is the same value the sign-in uses, and it is not the
            # verify token.
            secret=_signing_secret(platform),
        )
    except NotSupportedError:
        return HttpResponse(status=405)
    except SignatureError:
        # Say nothing about which check failed. That only helps whoever is
        # guessing.
        return HttpResponse(status=401)

    try:
        # `read_updates`, not one update. Meta batches several changes into
        # one message when it is busy, which is exactly when you least want
        # to drop the rest of them.
        found = client().read_updates(platform, request.body)
    except PlatformError:
        # A message shaped like nothing this network sends. Taking it and
        # saying nothing is right: answering with an error would have the
        # network retry it for three days.
        return JsonResponse({"ok": True})

    for update in found:
        # `deliver` is what drops the second copy of a message we have
        # already handled, through the `SeenUpdates` on the dispatcher.
        async_to_sync(dispatcher.deliver)(update)

    # 200 quickly. A network that does not get one soon enough treats the
    # message as undelivered and sends it again - so anything slow belongs
    # on a queue, not here.
    return JsonResponse({"ok": True, "updates": len(found)})


def _signing_secret(platform: str) -> str:
    """Find the secret one network signs its messages with.

    Args:
        platform: Which network.

    Returns:
        The secret, or an empty string when this app has none configured -
        which fails the signature check, which is the right outcome.
    """
    configured = settings.SOCIAL_APPS.get(platform)
    return str(configured["client_secret"]) if configured else ""


# Only the webhook is exempt from Django's CSRF protection. A social network
# has no way to send one of Django's tokens, so a protected webhook answers
# 403 to everything and the network eventually gives up on the address. The
# sign-in views are posted to by this app's own pages and keep it, which is
# why every form in the templates has {% csrf_token %} in it.
@csrf_exempt
@require_http_methods(["GET", "POST"])
def webhook(request: HttpRequest, platform: str) -> HttpResponse:
    """Answer a network's setup check, then receive its updates.

    Args:
        request: The request.
        platform: Which network.

    Returns:
        The reply.
    """
    if request.method == "GET":
        return _setup_check(request, platform)
    return _receive(request, platform)
