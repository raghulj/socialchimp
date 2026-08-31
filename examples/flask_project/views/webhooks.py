"""The address the four networks that push updates send them to.

Facebook, Instagram, Threads and TikTok push. The other five have no way to
tell you anything, so socialchimp asks them on a timer instead - see
`Account.fetch_updates` and `socialchimp.events.Poller` - and your handlers
cannot tell the difference.

There are two different requests at this one address:

- A `GET`, once, when you first point Meta at it. It carries a token you
  invented and typed into Meta's own form, and a challenge to echo back.
  Get it wrong and Meta says the URL could not be verified without saying
  why. TikTok does not do this - it starts sending straight away - so
  asking it here is refused by name, which is worth seeing.
- A `POST`, every time something happens, signed with your app secret.

Answer quickly. Meta gives you a few seconds and then treats the delivery as
failed; TikTok retries for 72 hours and delivers at least once, so the same
message arriving twice is normal rather than a bug. Give `Dispatcher` a
`SeenUpdates` and the second copy is dropped for you - `create_app` does.
"""

from __future__ import annotations

from flask import Blueprint, Response, request
from flask.typing import ResponseReturnValue

from socialchimp import NotSupportedError, SignatureError, SocialChimpError
from socialchimp.contrib.flask import run

from ..runtime import chimp

bp = Blueprint("webhooks", __name__, url_prefix="/webhooks")


@bp.get("/<platform>")
def setup_check(platform: str) -> ResponseReturnValue:
    """Answer the one-off question Meta asks before it will send anything."""
    here = chimp()
    expected = here.settings.setup_tokens.get(platform)

    try:
        challenge = here.sc.answer_setup_check(
            platform,
            request.args.to_dict(),
            # The verify token, not the app secret. Two different values
            # doing two different jobs, and mixing them up is the usual
            # reason a webhook cannot be set up.
            #
            # An empty one is passed deliberately when nothing is set, so
            # that a network which asks nothing first still refuses by name
            # below - socialchimp checks that before it looks at the token.
            verify_token=expected or "",
        )
    except NotSupportedError as refused:
        # TikTok lands here. It pushes, but it starts sending as soon as you
        # point it at a URL rather than asking anything first.
        return Response(str(refused), status=400, content_type="text/plain")
    except SignatureError:
        if expected is None:
            # Nothing was checked, and that is this app's fault rather than
            # the network's - so not a 403.
            return Response(
                f"No verify token is set for {platform}. Put the value you "
                f"typed into that network's dashboard in "
                f"{platform.upper()}_VERIFY_TOKEN.",
                status=500,
                content_type="text/plain",
            )
        # 403 rather than the 401 a bad signature gets: it is what Meta's
        # own flow expects here. Say nothing about which check failed -
        # that only helps whoever is guessing.
        return Response("Refused.", status=403, content_type="text/plain")

    # The challenge on its own, as plain text. Anything else - a JSON
    # wrapper, a trailing newline from a template - and Meta refuses it.
    return Response(challenge, content_type="text/plain")


@bp.post("/<platform>")
def pushed(platform: str) -> ResponseReturnValue:
    """Receive one signed message and hand on every update in it."""
    here = chimp()

    # `request.get_data()` is the bytes exactly as they arrived, and this is
    # the line the whole route depends on. The signature is over those exact
    # bytes. `request.get_json()` would parse them and any check afterwards
    # would be against JSON built again from the parsed object - different
    # spacing, different key order, a signature that no longer matches. That
    # is the single most common reason a correct signature appears to fail.
    body = request.get_data()
    headers = dict(request.headers.items())

    secret = here.settings.webhook_secret(platform)
    if secret is None:
        return Response(
            f"No app credentials are stored for {platform}, so nothing it "
            f"sends can be checked.",
            status=500,
            content_type="text/plain",
        )

    try:
        here.sc.check_signature(platform, body, headers, secret=secret)
        # Only now is it safe to parse. All of them, not the first: Meta
        # batches changes into one message when it is busy, which is exactly
        # when you least want to drop the rest.
        updates = here.sc.read_updates(platform, body)
    except SignatureError:
        return Response("Refused.", status=401, content_type="text/plain")
    except SocialChimpError as refused:
        return Response(str(refused), status=400, content_type="text/plain")

    for update in updates:
        # The handlers are async, and this is the same background loop the
        # rest of the app uses.
        run(here.dispatcher.deliver(update))

    return {"ok": True, "updates": len(updates)}
