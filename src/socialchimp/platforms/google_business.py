"""Google Business Profile: one account, several APIs, and a place rather than a feed.

Everywhere else socialchimp goes, signing in gets you one address to post to.
Here it gets you a **location** - Google's word for one business's listing -
and posting is the smallest part of what there is to do with it: a location
also has a name, a phone number, an address, a category, and a verification
process that has nothing to do with signing in at all.

That is reflected in how many hosts this file talks to. Google split what
used to be one "My Business" API into several, each with its own address:

- `mybusinessaccountmanagement.googleapis.com` - which accounts you can act as
- `mybusinessbusinessinformation.googleapis.com` - a location's name, phone,
  address, category
- `mybusiness.googleapis.com` (the old, still-live v4) - posts and reviews
- `mybusinessqanda.googleapis.com` - questions and answers
- `mybusinessverifications.googleapis.com` - proving the location is real

**The resource names are not consistent between them, and that is Google's
API, not a bug here.** Posts, reviews and business information all want the
full path, `accounts/{accountId}/locations/{locationId}`. Questions and
verifications want the location on its own, `locations/{locationId}`, with no
account in front of it. Both forms are built from the one resource name saved
on `Connection.extra["location_name"]` at sign-in, so nothing about this
leaks into your app - but it is worth knowing before you go looking for it in
Google's own docs and wonder why the same location has two addresses.

## Before any of this works

There is no `create_app` here. A person has to:

1. Make a project at https://console.cloud.google.com
2. Turn on the Business Profile APIs listed above for it
3. Create an **OAuth client** and add your redirect address to it
4. Apply for **Business Profile API access** through Google's own contact
   form - a separate, manual approval on top of the OAuth client, and slower.
   An unapproved project's quota on these APIs is zero, whatever the OAuth
   client says. There is nothing in the API that tells you which side of
   that approval you are on; a 0 QPM quota in the Cloud console is the only
   sign.

Then hand the client id and secret to socialchimp as `AppCredentials`.

## Signing someone in, then asking which location

Two steps to a token, exactly as with YouTube: `start_login` sends the
person to Google with a PKCE challenge, `finish_login` swaps the code they
come back with for a token. The address asks for `access_type=offline` and
`prompt=consent`, and for the same reason YouTube's does - leave either out
and Google hands back no refresh token, and the connection stops working in
an hour.

One Google account can manage several businesses, and each business can have
several locations, so `finish_login` lists every location across every
account this person can act on and answers `ChooseAccount` - even when there
is only one, the same rule every platform here holds to. `resume_login`
finishes the job with the one picked, and saves its resource name on
`Connection.extra["location_name"]`.

## What a post can carry

    Post(
        text="Fresh bread every morning until 2pm.",
        media=(Media.from_url("https://example.com/bread.jpg"),),
        options={"call_to_action_type": "SHOP", "call_to_action_url": "https://example.com"},
    )

**Google fetches the picture itself, from a web address**, the way Facebook
and Instagram do. `Media.from_url(...)` works; a local file is refused with a
message saying so. One picture at a time - `Limits.max_images` is 1.

**Only ordinary "What's New" posts are written here.** Google's API also has
event posts and offer posts, each wanting its own extra fields on top of
this; neither is written yet, and asking for one through `options` is
refused by name rather than silently becoming an ordinary post.

## What is deliberately not here

- **No scheduling.** `Feature.SCHEDULE` is missing. A local post goes out
  when you publish it.
- **No `check_state`.** A local post is live by the time Google answers, so
  there is nothing to ask about afterwards. Only `REJECTED` is treated as
  anything other than done.
- **No `read_stats`.** Google's Performance API reports how a *location* is
  doing in search and Maps - views, searches, calls - not how one post did.
  There is no honest number to hand back for a `post_id`, so this is left
  off rather than approximated.
- **Reviews and questions are read two ways, on purpose.** `fetch_updates`
  polls both, for an app that cannot receive pushed requests. Pub/Sub
  delivers them properly, through `check_signature` and `read_updates` - see
  below - and an app can use either, or both, the same way Meta's networks
  let you fall back to a timer.

## Replying is not the same as posting

A review or a question is answered through `Account.reply_to_update`, not
through `post()` - there is no post underneath either one, and `reply_to`
means something else entirely. `reply_to_update` reads `update.kind` to work
out whether it is answering a review or a question, and refuses anything
else by name.

## Verification never sees the proof

`Account.start_verification` is what makes Google act - mail a postcard,
place a call, send a text, send an email - to the business's own address,
phone or inbox. `Account.complete_verification` takes the code the business
owner was sent and nothing else. socialchimp never holds that code outside
of the one call that hands it to Google.

## Updates arrive through Pub/Sub, not a plain webhook

Every other pushing network here signs a request with a shared secret and
your platform checks it with plain HMAC - no network call, nothing but the
body, the headers and the secret. Google's Pub/Sub push instead signs with a
Google-issued OIDC token, whose signature can only really be checked against
Google's own public keys, and those keys rotate. Fetching them on every
webhook is a network call inside what is supposed to be a cheap, offline
check - so this platform does not fetch them itself.

Instead, `secret` here is not a password - it is a small JSON document your
app keeps refreshed and hands over on every check:

    {
        "keys": [<Google's public keys, in JWK form>],
        "audience": "https://you.example/webhooks/google_business",
        "service_account": "service-1234@gcp-sa-pubsub.iam.gserviceaccount.com"
    }

Refresh `keys` from Google's own JWKS endpoint on a timer - they last for
hours, not minutes - and keep `audience` as whatever address your Pub/Sub
push subscription is configured to send to. `check_signature` verifies the
token's signature against those keys and checks its audience and expiry
entirely offline; nothing here goes to the network.

**Audience alone does not prove the token came from your subscription.**
Google will sign an ID token for any audience a caller asks for, so anyone
who can mint their own Google-issued token could pick your webhook's address
as the audience and send it to you. What actually ties a token to *your*
Pub/Sub push subscription is the identity of the service account it was
issued to - the one you configured the subscription's push authentication
with, which defaults to
`service-{PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com` unless you
named your own. `service_account` here is that address, and `check_signature`
refuses a token whose verified `email` claim does not match it - a check
Google's own Pub/Sub push documentation calls out by name.

A `secret` that will not parse as that shape is a `ConfigError`, not a
`SignatureError` - it is your own setup that is wrong, not the request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import httpx

from socialchimp.errors import (
    AuthError,
    ConfigError,
    InvalidPostError,
    NotAllowedError,
    NotFoundError,
    NotSupportedError,
    PlatformError,
    RateLimitError,
    SignatureError,
    TokenExpiredError,
)
from socialchimp.events import Update, UpdateKind
from socialchimp.features import Feature, Limits, check_option_names, check_post
from socialchimp.http import HttpClient, error_from_response, read_body
from socialchimp.models import (
    BusinessLocation,
    Connection,
    MediaKind,
    Post,
    PostResult,
    PostState,
    RawData,
    Token,
    Verification,
    VerificationOption,
)
from socialchimp.platform import (
    AccountChoice,
    ChooseAccount,
    Finished,
    LoginRequest,
    SendToNetwork,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from socialchimp.errors import SocialChimpError
    from socialchimp.http import Retries
    from socialchimp.models import AppCredentials

__all__ = ["GoogleBusinessPlatform", "google_business_errors"]

PLATFORM_NAME: Final = "google_business"

ACCOUNT_MANAGEMENT_API: Final = "https://mybusinessaccountmanagement.googleapis.com/v1"
BUSINESS_INFORMATION_API: Final = (
    "https://mybusinessbusinessinformation.googleapis.com/v1"
)
LOCAL_API: Final = "https://mybusiness.googleapis.com/v4"
QANDA_API: Final = "https://mybusinessqanda.googleapis.com/v1"
VERIFICATIONS_API: Final = "https://mybusinessverifications.googleapis.com/v1"

SIGN_IN_URL: Final = "https://accounts.google.com/o/oauth2/v2/auth"
# A public address, the same for every app in the world - not a secret.
TOKEN_URL: Final = "https://oauth2.googleapis.com/token"  # noqa: S105

CONSOLE_URL: Final = "https://console.cloud.google.com/apis/credentials"
"""Where a person creates the OAuth client this all needs."""

DEFAULT_SCOPES: Final = ("https://www.googleapis.com/auth/business.manage",)

POST_OPTIONS: Final = ("call_to_action_type", "call_to_action_url")
"""The settings `Post.options` accepts here. Anything else is refused."""

CALL_TO_ACTION_TYPES: Final = ("BOOK", "ORDER", "SHOP", "LEARN_MORE", "SIGN_UP", "CALL")

MAX_SUMMARY_LENGTH: Final = 1500
MAX_IMAGES: Final = 1

_STATE_FOR: Final = {"REJECTED": PostState.FAILED}
"""What Google calls a local post's state, and what we call it.

A local post is live by the time Google answers, in every state but this
one - there is no `check_state` here for the same reason there is nothing to
ask YouTube about a word-only post.
"""

_NOTIFICATION_KIND_FOR: Final = {
    "NEW_REVIEW": "review_created",
    "UPDATED_REVIEW": "review_updated",
    "NEW_QUESTION": "question_created",
    "NEW_ANSWER": "answer_created",
}

_SHA256_DIGEST_INFO_PREFIX: Final = bytes.fromhex(
    "3031300d060960864801650304020105000420"
)
"""The fixed ASN.1 header PKCS#1 v1.5 puts in front of a SHA-256 hash.

Needed to verify an RS256 signature by hand, without a cryptography
dependency this project takes on nowhere else.
"""

_STATE_BYTES: Final = 24
_VERIFIER_BYTES: Final = 48

_UNTRUSTED: Final = (
    "This request could not be verified as having come from Google Pub/Sub. "
    "Refusing it."
)


def _now() -> datetime:
    """Return the current moment. Its own function so tests can fix it."""
    return datetime.now(UTC)


def _text(reply: RawData, key: str, when: str) -> str:
    """Read a value Google always sends, and complain plainly if it did not.

    Args:
        reply: What Google answered.
        key: The field we need.
        when: What we had asked it to do, for the message.

    Returns:
        The value.

    Raises:
        PlatformError: If the field is missing or empty.
    """
    value = reply.get(key)
    if isinstance(value, str) and value:
        return value
    message = (
        f"Google left {key!r} out of its reply when we asked it to {when}. "
        f"That should not happen. The whole reply is on this error."
    )
    raise PlatformError(message, platform=PLATFORM_NAME, raw=reply)


def _items_in(reply: RawData, key: str) -> list[RawData]:
    """Pull a named list out of one of Google's replies.

    Args:
        reply: What Google answered.
        key: Which field holds the list - `"accounts"`, `"locations"`,
            `"reviews"`, `"questions"`, all Google's own names.

    Returns:
        The items, or an empty list when there are none.
    """
    found = reply.get(key)
    if not isinstance(found, list):
        return []
    return [item for item in found if isinstance(item, dict)]


def _moment(text: str) -> datetime | None:
    """Read a time Google wrote, such as `"2026-08-31T10:00:00Z"`."""
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _location_title(item: RawData) -> str:
    """Read a location's name out of what Google said about it."""
    title = item.get("title")
    if isinstance(title, str) and title:
        return title
    return str(item.get("name", ""))


def _location_resource(connection: Connection) -> str:
    """Return the full `accounts/{a}/locations/{l}` this connection is for.

    Args:
        connection: The account to look at.

    Returns:
        The resource name saved at sign-in.

    Raises:
        ConfigError: If nothing was saved there - this connection was not
            made by `GoogleBusinessPlatform`'s own login.
    """
    saved = connection.extra.get("location_name")
    if isinstance(saved, str) and saved:
        return saved
    message = (
        "This connection has no location_name saved on Connection.extra, so "
        "there is no address to send this to. It was not created by "
        "GoogleBusinessPlatform's own login - connect the location again."
    )
    raise ConfigError(message)


def _bare_location(connection: Connection) -> str:
    """Return the `locations/{l}` form the Q&A and Verifications APIs want."""
    return "locations/" + _location_resource(connection).rsplit("/", 1)[-1]


def _pack(kept: RawData) -> str:
    """Turn what a paused login needs to remember into one piece of text."""
    written = json.dumps(kept, sort_keys=True).encode()
    return base64.urlsafe_b64encode(written).decode().rstrip("=")


def _unpack(packed: str) -> RawData:
    """Read back what `_pack` wrote.

    Raises:
        AuthError: If it cannot be read.
    """
    padded = packed + "=" * (-len(packed) % 4)
    try:
        parsed = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        message = (
            "This resume_token could not be read, so the login cannot be "
            "carried on. Hand back exactly the value from ChooseAccount, and "
            "start a new login if it has been lost."
        )
        raise AuthError(message, platform=PLATFORM_NAME)
    return parsed


def _challenge_for(verifier: str) -> str:
    """Hash the secret we keep, so only the hash travels to Google."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _expiry_from(reply: RawData) -> datetime:
    """Work out when an access token stops working."""
    seconds = reply.get("expires_in")
    lasts = seconds if isinstance(seconds, int) else 3600
    return _now() + timedelta(seconds=lasts)


def _app_or_refuse(app: AppCredentials | None, what: str) -> AppCredentials:
    """Insist on your app's credentials before going any further.

    Raises:
        ConfigError: If there are none.
    """
    if app is None:
        raise ConfigError(_no_credentials(what))
    return app


def _no_credentials(what: str) -> str:
    return (
        f"Google Business Profile needs your app's client id and secret to "
        f"{what}, and none were given. Somebody has to create a project at "
        f"{CONSOLE_URL}, turn on the Business Profile APIs, create an OAuth "
        f"client, add your redirect address to it, and apply for Business "
        f"Profile API access - a separate, manual approval that can take "
        f"weeks. Save what the console gives you with Storage.save_app, and "
        f"socialchimp hands them to every sign-in and every renewal."
    )


def _check_state(request: LoginRequest, callback: Mapping[str, str]) -> None:
    """Check the value that came back is the one we sent.

    A missing state is refused the same as a wrong one. Google not sending
    it back at all - the same as a value that does not match - is the shape
    a forged callback takes: cross the person's browser onto a login started
    for somebody else, and the check has nothing to compare against unless
    absence is treated as failure rather than "nothing to check".

    Raises:
        AuthError: If we sent a state and either none came back or it does
            not match.
    """
    if request.state is None:
        return
    returned = callback.get("state", "")
    if not returned or not hmac.compare_digest(returned, request.state):
        message = (
            "The state Google sent back did not match the one we sent. This "
            "login did not start here, so nothing has been saved. Start a "
            "new one."
        )
        raise AuthError(message, platform=PLATFORM_NAME)


def _code_from(callback: Mapping[str, str]) -> str:
    """Pull the login code out of what Google sent back.

    Raises:
        AuthError: If the person said no, or there is no code.
    """
    refused = callback.get("error")
    if refused:
        said = callback.get("error_description", "")
        detail = f" It said: {said}" if said else ""
        message = (
            f"Google did not sign this person in ({refused}). Usually they "
            f"pressed cancel on the approval page.{detail}"
        )
        raise AuthError(message, platform=PLATFORM_NAME)

    code = callback.get("code")
    if not code:
        message = (
            "Google sent no code back, so there is nothing to swap for a "
            "token. Check you are passing the whole query string from your "
            "redirect address."
        )
        raise AuthError(message, platform=PLATFORM_NAME)
    return code


def _verifier_from(remember: RawData | None) -> str:
    """Read the secret `start_login` made back out of what your app kept.

    Raises:
        AuthError: If it did not come back.
    """
    verifier = (remember or {}).get("code_verifier")
    if not isinstance(verifier, str) or not verifier:
        message = (
            "This sign-in cannot be finished because the secret made at the "
            "start did not come back. Pass SendToNetwork.remember to "
            "finish_login as remember. Keep it with that person's session "
            "rather than in memory."
        )
        raise AuthError(message, platform=PLATFORM_NAME)
    return verifier


def _is_googles_own_fault(refused: SocialChimpError) -> bool:
    """Say whether a refusal was Google struggling rather than a dead token."""
    if not isinstance(refused, PlatformError):
        return False
    return (
        refused.status_code is None
        or refused.status_code >= httpx.codes.INTERNAL_SERVER_ERROR
    )


def _reason_in(body: RawData) -> str:
    """Pull a short name for what went wrong out of a refusal.

    Two shapes have to be read: the legacy `error.errors[].reason` the v4
    Local Posts and Reviews APIs still use, and the plain `error.status` the
    newer Business Profile APIs answer with instead.
    """
    problem = body.get("error")
    if not isinstance(problem, dict):
        return ""

    listed = problem.get("errors")
    if isinstance(listed, list):
        for item in listed:
            if isinstance(item, dict):
                reason = item.get("reason")
                if isinstance(reason, str) and reason:
                    return reason

    status = problem.get("status")
    return status if isinstance(status, str) else ""


def _said_in(body: RawData) -> str:
    """Pull Google's own sentence out of a refusal, ready to append."""
    problem = body.get("error")
    if isinstance(problem, dict):
        said = problem.get("message")
        if isinstance(said, str) and said:
            return f" It said: {said}"
    return ""


_RATE_LIMITED: Final = frozenset({"quotaExceeded", "RESOURCE_EXHAUSTED"})
_UNAUTHENTICATED: Final = frozenset({"authError", "UNAUTHENTICATED"})
_FORBIDDEN: Final = frozenset({"forbidden", "PERMISSION_DENIED"})
_NOT_FOUND: Final = frozenset({"notFound", "NOT_FOUND"})


def google_business_errors(response: httpx.Response) -> SocialChimpError:
    """Turn an unhappy reply from any of Google's Business Profile APIs into ours.

    These APIs do not all answer errors the same way - the older v4 Local
    Posts and Reviews endpoints use the legacy `errors[].reason` shape, the
    newer ones a plain `status` string - so both are read here.

    Args:
        response: The reply to turn into an error.

    Returns:
        The error to raise.
    """
    body = read_body(response)
    reason = _reason_in(body)
    said = _said_in(body)

    if reason in _RATE_LIMITED:
        message = (
            f"Google's daily allowance for this project is used up "
            f"({reason}). Ask for more quota in the Google Cloud console, "
            f"or wait for it to reset.{said}"
        )
        return RateLimitError(message, platform=PLATFORM_NAME, raw=body)

    if reason in _UNAUTHENTICATED:
        message = (
            f"Google would not accept our sign-in ({reason}). The token has "
            f"run out or been taken away; renewing it, or asking the person "
            f"to connect their location again, is what fixes it.{said}"
        )
        return AuthError(message, platform=PLATFORM_NAME, raw=body)

    if reason in _FORBIDDEN:
        message = (
            f"Google will not let this location do that ({reason}). It is "
            f"usually a permission never asked for, or a project whose "
            f"Business Profile API access has not been approved yet.{said}"
        )
        return NotAllowedError(message, platform=PLATFORM_NAME, raw=body)

    if reason in _NOT_FOUND:
        message = f"Google has no such thing ({reason}).{said}"
        return NotFoundError(message, platform=PLATFORM_NAME, raw=body)

    return error_from_response(response, platform=PLATFORM_NAME)


def _b64url_decode(segment: str) -> bytes:
    """Decode a base64url piece of a JWT, padding it back out first."""
    padded = segment + "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(padded.encode())


def _b64url_uint(segment: str) -> int:
    """Read a base64url JWK field, such as `n` or `e`, as an integer."""
    return int.from_bytes(_b64url_decode(segment), "big")


def _rsa_pkcs1_sha256_verify(
    message: bytes, signature: bytes, *, n: int, e: int
) -> bool:
    """Check an RSA PKCS#1 v1.5 SHA-256 signature, with no library beyond stdlib.

    RSA verification is one exponentiation and a fixed byte pattern to
    compare against - no dependency socialchimp does not already have is
    needed to do it correctly.

    Args:
        message: The bytes that were signed - the JWT's header and payload,
            joined by a dot.
        signature: The signature to check.
        n: The key's modulus.
        e: The key's public exponent.

    Returns:
        Whether the signature matches.
    """
    modulus_bytes = (n.bit_length() + 7) // 8
    if len(signature) != modulus_bytes:
        return False

    sig_int = int.from_bytes(signature, "big")
    if sig_int >= n:
        return False

    recovered = pow(sig_int, e, n).to_bytes(modulus_bytes, "big")
    tail = _SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    padding_length = modulus_bytes - len(tail) - 3
    if padding_length < 8:
        return False

    expected = b"\x00\x01" + b"\xff" * padding_length + b"\x00" + tail
    return hmac.compare_digest(expected, recovered)


def _verify_google_id_token(
    token: str, *, keys: Sequence[RawData], audience: str, service_account: str
) -> RawData:
    """Check a Pub/Sub push token really came from Google and is still good.

    Entirely offline: no key is fetched here, only compared against what the
    caller already holds. See the module docstring for why, and for why the
    audience alone is not enough - the `email` claim, checked last, is what
    actually ties this to the one Pub/Sub subscription we trust.

    Args:
        token: The bearer token from the `Authorization` header.
        keys: Google's public keys, in JWK form.
        audience: The address this platform's Pub/Sub subscription is meant
            to be pushing to.
        service_account: The address of the service account our Pub/Sub push
            subscription authenticates as.

    Returns:
        The token's payload, once it has checked out.

    Raises:
        SignatureError: If anything about the token is wrong. Never says
            which check failed - see `check_signature`.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise SignatureError(_UNTRUSTED)
    header_b64, payload_b64, signature_b64 = parts

    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(signature_b64)
    except ValueError as broken:
        raise SignatureError(_UNTRUSTED) from broken

    if not isinstance(header, dict) or header.get("alg") != "RS256":
        raise SignatureError(_UNTRUSTED)
    if not isinstance(payload, dict):
        raise SignatureError(_UNTRUSTED)

    key = next(
        (candidate for candidate in keys if candidate.get("kid") == header.get("kid")),
        None,
    )
    if key is None:
        raise SignatureError(_UNTRUSTED)

    try:
        n = _b64url_uint(str(key["n"]))
        e = _b64url_uint(str(key["e"]))
    except (KeyError, ValueError) as broken:
        raise SignatureError(_UNTRUSTED) from broken

    signing_input = f"{header_b64}.{payload_b64}".encode()
    if not _rsa_pkcs1_sha256_verify(signing_input, signature, n=n, e=e):
        raise SignatureError(_UNTRUSTED)

    if payload.get("aud") != audience:
        raise SignatureError(_UNTRUSTED)

    expiry = payload.get("exp")
    if not isinstance(expiry, int | float) or expiry < _now().timestamp():
        raise SignatureError(_UNTRUSTED)

    # The audience check above only says the token was issued for this
    # address - Google will mint one for any audience a caller names, so
    # that alone does not say the caller was our own Pub/Sub subscription.
    # The email claim is what does: Google sets it to the service account
    # the token was issued to, and only our subscription's push
    # authentication is configured with this one.
    if (
        payload.get("email") != service_account
        or payload.get("email_verified") is not True
    ):
        raise SignatureError(_UNTRUSTED)

    return payload


def _webhook_config(secret: str) -> tuple[list[RawData], str, str]:
    """Read the JSON `check_signature` is handed here as `secret`.

    Raises:
        ConfigError: If it does not parse as
            `{"keys": [...], "audience": "...", "service_account": "..."}`.
            That is a mistake in what your app passed, not in the request.
    """
    try:
        parsed = json.loads(secret)
        keys = parsed["keys"]
        audience = parsed["audience"]
        service_account = parsed["service_account"]
    except (json.JSONDecodeError, KeyError, TypeError) as broken:
        message = (
            "secret could not be read as "
            '{"keys": [...Google\'s public keys as JWKs...], "audience": '
            '"https://you.example/webhooks/google_business", '
            '"service_account": "...@gcp-sa-pubsub.iam.gserviceaccount.com"}. '
            "See the module docstring for GoogleBusinessPlatform."
        )
        raise ConfigError(message) from broken
    if (
        not isinstance(keys, list)
        or not isinstance(audience, str)
        or not audience
        or not isinstance(service_account, str)
        or not service_account
    ):
        message = (
            '"keys" has to be a list, and "audience" and "service_account" '
            "non-empty text - see the module docstring for "
            "GoogleBusinessPlatform."
        )
        raise ConfigError(message)
    return [item for item in keys if isinstance(item, dict)], audience, service_account


def _update_from_notification(notification: RawData) -> Update:
    """Turn one decoded Pub/Sub notification into an update your app understands.

    Args:
        notification: The JSON Google published, already decoded from
            `message.data`.

    Returns:
        What happened, in socialchimp's own words. Never fails: a
        notification type nobody has seen yet still reaches your app as
        `UpdateKind.UNKNOWN`, with Google's own word kept.
    """
    kind_name = str(notification.get("notificationType", ""))
    subject = notification.get("review") or notification.get("question") or {}
    subject = subject if isinstance(subject, dict) else {}

    location_name = str(notification.get("locationName", ""))
    trailing = location_name.rsplit("/", 1)[-1] if location_name else ""

    when = (
        _moment(str(subject.get("updateTime") or subject.get("createTime") or ""))
        or _now()
    )
    identity = ":".join(
        [
            location_name,
            kind_name,
            str(subject.get("reviewId") or subject.get("name") or ""),
            str(subject.get("updateTime") or subject.get("createTime") or ""),
        ]
    )

    return Update.from_network(
        update_id=identity,
        kind_name=_NOTIFICATION_KIND_FOR.get(kind_name, kind_name),
        platform=PLATFORM_NAME,
        connection_id=f"{PLATFORM_NAME}:{trailing}",
        created_at=when,
        raw=subject or notification,
        envelope=notification,
    )


def _review_update(review: RawData, *, connection_id: str) -> Update | None:
    """Turn one review from the poller into an update.

    Returns `None` when it cannot be dated at all, rather than sorting it in
    an arbitrary place.
    """
    created = review.get("createTime")
    updated = review.get("updateTime")
    when = _moment(str(updated or created or ""))
    if when is None:
        return None
    kind_name = "review_created" if updated in (None, created) else "review_updated"
    return Update.from_network(
        update_id=str(review.get("reviewId", review.get("name", ""))),
        kind_name=kind_name,
        platform=PLATFORM_NAME,
        connection_id=connection_id,
        created_at=when,
        raw=review,
    )


def _question_update(question: RawData, *, connection_id: str) -> Update | None:
    """Turn one question from the poller into an update.

    Returns `None` when it cannot be dated at all.
    """
    when = _moment(str(question.get("updateTime") or question.get("createTime") or ""))
    if when is None:
        return None
    return Update.from_network(
        update_id=str(question.get("name", "")),
        kind_name="question_created",
        platform=PLATFORM_NAME,
        connection_id=connection_id,
        created_at=when,
        raw=question,
    )


def _business_location_from(reply: RawData, connection: Connection) -> BusinessLocation:
    """Turn a Business Information API location resource into ours."""
    phones = reply.get("phoneNumbers")
    phone = phones.get("primaryPhone") if isinstance(phones, dict) else None

    address = reply.get("storefrontAddress")
    address = address if isinstance(address, dict) else {}

    categories_field = reply.get("categories")
    categories_field = categories_field if isinstance(categories_field, dict) else {}
    names: list[str] = []
    primary = categories_field.get("primaryCategory")
    if isinstance(primary, dict):
        display = primary.get("displayName")
        if isinstance(display, str) and display:
            names.append(display)
    additional = categories_field.get("additionalCategories")
    if isinstance(additional, list):
        names.extend(
            item["displayName"]
            for item in additional
            if isinstance(item, dict) and isinstance(item.get("displayName"), str)
        )

    return BusinessLocation(
        id=connection.account_id,
        name=_location_title(reply),
        phone=phone if isinstance(phone, str) else None,
        address=address,
        categories=tuple(names),
        raw=reply,
    )


def _checked_call_to_action(options: RawData) -> RawData | None:
    """Read the call-to-action button off a post's options, checked.

    Raises:
        InvalidPostError: If a type is given that Google does not know, or
            a type other than CALL is given with no url to send people to.
    """
    action_type = options.get("call_to_action_type")
    if action_type is None:
        return None
    if action_type not in CALL_TO_ACTION_TYPES:
        message = (
            f"call_to_action_type is {action_type!r}, which Google does not "
            f"know. It accepts: {', '.join(CALL_TO_ACTION_TYPES)}."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME)

    url = options.get("call_to_action_url")
    if action_type != "CALL" and (not isinstance(url, str) or not url):
        message = (
            f"call_to_action_type is {action_type!r}, which needs "
            f"call_to_action_url alongside it - where people end up when "
            f"they tap the button."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME)

    action: RawData = {"actionType": action_type}
    if isinstance(url, str) and url:
        action["url"] = url
    return action


class GoogleBusinessPlatform:
    """Everything socialchimp does with a Google Business Profile location.

    Signing people in, picking a location, publishing a local post, reading
    and replying to reviews and questions, reading and changing the
    location's own information, and running Google's verification process.

        google_business = GoogleBusinessPlatform()

    It holds nothing belonging to one account and nothing belonging to your
    app. Your client id and secret arrive as an argument every time they are
    needed, so one of these serves every location and every app.

    Attributes:
        name: `"google_business"`.
        features: What this platform does here. No `SCHEDULE` - a local post
            goes out when you publish it. No `READ_STATS` - see the module
            docstring for why.
    """

    name: str = PLATFORM_NAME

    features: Feature = Feature.POST_TEXT | Feature.POST_IMAGE | Feature.PUSH_UPDATES

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        retries: Retries | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Set Google Business Profile up for one app.

        Args:
            timeout: Seconds to wait for Google to answer.
            retries: How many times to try again after a hiccup. Left out,
                the shared default is used.
            transport: Where requests actually go. Leave it out for ordinary
                calls; pass your own to send them somewhere else.
        """
        self._timeout = timeout
        self._retries = retries
        self._transport = transport

    def _client(self, base_url: str = "", token: str | None = None) -> HttpClient:
        """Make a client pointed at one of Google's several addresses.

        Args:
            base_url: What to join paths onto. Left out, every request
                carries its whole address - used for the token endpoint,
                which lives somewhere else again.
            token: The account's token, for anything that needs one.

        Returns:
            A client. Use it in an `async with` block so it closes itself.
        """
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        return HttpClient(
            base_url,
            platform=PLATFORM_NAME,
            headers=headers,
            timeout=self._timeout,
            transport=self._transport,
            retries=self._retries,
            errors=google_business_errors,
        )

    def api_base(self, connection: Connection) -> str:
        """Return where Google's business information lives.

        One of several addresses this platform talks to - see the module
        docstring - given because every platform's `api_base` returns one.

        Args:
            connection: The account we are about to act as. Not used.

        Returns:
            `"https://mybusinessbusinessinformation.googleapis.com/v1"`.
        """
        return BUSINESS_INFORMATION_API

    def auth_headers(self, connection: Connection) -> Mapping[str, str]:
        """Return the header that proves we may act as this location."""
        return {"Authorization": f"Bearer {connection.token.access_token}"}

    async def limits(self, connection: Connection) -> Limits:
        """Return what Google allows.

        Nothing is asked of Google - these numbers do not change from one
        location to the next.

        Args:
            connection: The account to ask about. Not used.

        Returns:
            What Google allows right now.
        """
        return Limits(max_text_length=MAX_SUMMARY_LENGTH, max_images=MAX_IMAGES)

    async def start_login(self, request: LoginRequest) -> SendToNetwork:
        """Build the address to send somebody to so they can approve your app.

        Raises:
            ConfigError: If the request carries no app credentials.
        """
        app = _app_or_refuse(request.app, "sign somebody in")
        state = request.state or secrets.token_urlsafe(_STATE_BYTES)
        verifier = secrets.token_urlsafe(_VERIFIER_BYTES)

        query = httpx.QueryParams(
            {
                "response_type": "code",
                "client_id": app.client_id,
                "redirect_uri": request.redirect_uri,
                "scope": " ".join(request.scopes or DEFAULT_SCOPES),
                "state": state,
                "code_challenge": _challenge_for(verifier),
                "code_challenge_method": "S256",
                "access_type": "offline",
                "prompt": "consent",
            }
        )
        return SendToNetwork(
            url=f"{SIGN_IN_URL}?{query}",
            state=state,
            remember={"code_verifier": verifier},
        )

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> ChooseAccount:
        """Swap the code for a token, then list every location this person has.

        Raises:
            AuthError: If the person said no, there is no code, the state
                does not match, the secret from `start_login` did not come
                back, Google issued no refresh token, or this person has no
                Business Profile location we can act on.
            ConfigError: If there are no app credentials anywhere.
            PlatformError: If Google answers without an access token.
        """
        app = _app_or_refuse(request.app, "sign somebody in")
        _check_state(request, callback)
        code = _code_from(callback)
        verifier = _verifier_from(remember)

        form: dict[str, Any] = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": app.client_id,
            "client_secret": app.client_secret,
            "redirect_uri": request.redirect_uri,
            "code_verifier": verifier,
        }
        async with self._client() as http:
            reply = await http.json("POST", TOKEN_URL, data=form)

        access_token = _text(reply, "access_token", "sign someone in")
        refresh_token = reply.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            message = (
                "Google signed this person in but sent no refresh token, so "
                "the connection would stop working in about an hour. That "
                "happens when the sign-in address is missing "
                "access_type=offline, or missing prompt=consent and the "
                "person has approved this app before. Nothing has been "
                "saved; start the login again through start_login."
            )
            raise AuthError(message, platform=PLATFORM_NAME, raw=reply)

        async with self._client(ACCOUNT_MANAGEMENT_API, access_token) as http:
            accounts_reply = await http.json("GET", "/accounts")
        accounts = _items_in(accounts_reply, "accounts")
        if not accounts:
            message = (
                "This Google account has no Business Profile account we can "
                "act on, so there is nothing to connect."
            )
            raise AuthError(message, platform=PLATFORM_NAME, raw=accounts_reply)

        locations: dict[str, str] = {}
        async with self._client(BUSINESS_INFORMATION_API, access_token) as http:
            for account_item in accounts:
                account_name = _text(
                    account_item, "name", "list this person's accounts"
                )
                locations_reply = await http.json(
                    "GET",
                    f"/{account_name}/locations",
                    params={"readMask": "title"},
                )
                for location_item in _items_in(locations_reply, "locations"):
                    location_name = _text(
                        location_item, "name", "list this account's locations"
                    )
                    locations[location_name] = _location_title(location_item)

        if not locations:
            message = (
                "This Google account manages no Business Profile location, "
                "so there is nothing to connect."
            )
            raise AuthError(message, platform=PLATFORM_NAME, raw=accounts_reply)

        granted = reply.get("scope")
        given = granted.split() if isinstance(granted, str) and granted else []

        return ChooseAccount(
            options=tuple(
                AccountChoice(id=name, name=title, kind="location")
                for name, title in locations.items()
            ),
            resume_token=_pack(
                {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expires_at": _expiry_from(reply).isoformat(),
                    "scopes": given or list(request.scopes or DEFAULT_SCOPES),
                    "locations": locations,
                }
            ),
        )

    async def resume_login(
        self,
        request: LoginRequest,
        *,
        resume_token: str,
        account_id: str,
        remember: RawData | None = None,
    ) -> Finished:
        """Finish the login now that a location has been picked.

        Raises:
            AuthError: If the resume token cannot be read, or the location
                is not one this person was actually offered.
        """
        kept = _unpack(resume_token)
        locations = kept.get("locations")
        offered = locations if isinstance(locations, dict) else {}

        if account_id not in offered:
            message = (
                f"{account_id!r} was not one of the locations this person "
                f"approved. They were offered: "
                f"{', '.join(sorted(offered)) or 'none'}. Pass one of those "
                f"as account_id, or start the login again."
            )
            raise AuthError(message, platform=PLATFORM_NAME)

        scopes = kept.get("scopes")
        expires_at = _moment(str(kept.get("expires_at", "")))
        trailing = account_id.rsplit("/", 1)[-1]

        return Finished(
            connection=Connection(
                id=f"{PLATFORM_NAME}:{trailing}",
                platform=PLATFORM_NAME,
                host=None,
                account_id=trailing,
                account_name=str(offered[account_id]),
                token=Token(
                    access_token=str(kept.get("access_token", "")),
                    refresh_token=str(kept.get("refresh_token", "")),
                    expires_at=expires_at,
                ),
                scopes=tuple(str(one) for one in scopes)
                if isinstance(scopes, list)
                else (),
                extra={"location_name": account_id},
            )
        )

    async def refresh(
        self,
        connection: Connection,
        app: AppCredentials | None = None,
    ) -> Token:
        """Get a fresh access token for a location.

        Raises:
            ConfigError: If no credentials arrived.
            TokenExpiredError: If there is no refresh token, or Google will
                not take the one we have.
            PlatformError: If Google answers without a token.
        """
        signing = _app_or_refuse(app, "renew a token")

        renewal = connection.token.refresh_token
        if renewal is None:
            message = (
                f"The token for {connection.id!r} has run out and there is "
                f"no refresh token to replace it with. The person has to "
                f"connect their location again."
            )
            raise TokenExpiredError(message, platform=PLATFORM_NAME)

        form: dict[str, Any] = {
            "grant_type": "refresh_token",
            "refresh_token": renewal,
            "client_id": signing.client_id,
            "client_secret": signing.client_secret,
        }

        async with self._client() as http:
            try:
                reply = await http.json("POST", TOKEN_URL, data=form)
            except (AuthError, PlatformError) as refused:
                if _is_googles_own_fault(refused):
                    raise
                message = (
                    f"Google will not renew the token for {connection.id!r}. "
                    f"Its refresh token has been revoked, or the person "
                    f"removed your app from their Google account. The person "
                    f"has to connect their location again."
                )
                raise TokenExpiredError(
                    message, platform=PLATFORM_NAME, raw=refused.raw
                ) from refused

        replacement = reply.get("refresh_token")
        return Token(
            access_token=_text(reply, "access_token", "renew a token"),
            refresh_token=replacement
            if isinstance(replacement, str) and replacement
            else renewal,
            expires_at=_expiry_from(reply),
        )

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        """Publish an ordinary local post.

        Raises:
            InvalidPostError: If the post breaks one of Google's limits, a
                setting is unknown, or a picture is not a web address.
            NotSupportedError: If the post needs something Google cannot do
                here, such as a video or scheduling.
            SocialChimpError: If Google refuses the post.
        """
        limits = await self.limits(connection)
        check_post(post, platform=PLATFORM_NAME, features=self.features, limits=limits)
        check_option_names(
            post.options,
            platform=PLATFORM_NAME,
            allowed=POST_OPTIONS,
        )
        call_to_action = _checked_call_to_action(post.options)

        pictures = [item for item in post.media if item.kind is MediaKind.IMAGE]
        for picture in pictures:
            if not picture.url:
                message = (
                    "Google Business Profile fetches the picture itself, "
                    "from a public web address - it does not accept an "
                    "upload. Media.from_url(...) works; put the file "
                    "somewhere public first."
                )
                raise InvalidPostError(message, platform=PLATFORM_NAME)

        location_name = _location_resource(connection)
        payload: dict[str, Any] = {
            "languageCode": "en",
            "topicType": "STANDARD",
            "summary": post.text,
        }
        if pictures:
            payload["media"] = [{"mediaFormat": "PHOTO", "sourceUrl": pictures[0].url}]
        if call_to_action is not None:
            payload["callToAction"] = call_to_action

        async with self._client(LOCAL_API, connection.token.access_token) as http:
            reply = await http.json(
                "POST", f"/{location_name}/localPosts", json=payload
            )

        name = _text(reply, "name", "publish a post")
        url = reply.get("searchUrl")
        state = _STATE_FOR.get(str(reply.get("state", "LIVE")), PostState.DONE)
        return PostResult(
            id=name,
            url=url if isinstance(url, str) and url else None,
            state=state,
            raw=reply,
        )

    async def fetch_updates(
        self,
        connection: Connection,
        since: datetime | None,
    ) -> Sequence[Update]:
        """Return the reviews and questions on this location since a moment.

        For an app that cannot receive Pub/Sub pushes. See `check_signature`
        and `read_updates` for the pushed way, which is the one this
        platform is built to prefer.

        Args:
            connection: The location to ask about.
            since: Only return things newer than this. `None` on the first
                call.

        Returns:
            Reviews and questions together, oldest first.
        """
        location_name = _location_resource(connection)
        bare = _bare_location(connection)

        async with self._client(LOCAL_API, connection.token.access_token) as http:
            reviews_reply = await http.json("GET", f"/{location_name}/reviews")
        async with self._client(QANDA_API, connection.token.access_token) as http:
            questions_reply = await http.json("GET", f"/{bare}/questions")

        updates: list[Update] = []
        for review in _items_in(reviews_reply, "reviews"):
            found = _review_update(review, connection_id=connection.id)
            if found is not None and (since is None or found.created_at > since):
                updates.append(found)
        for question in _items_in(questions_reply, "questions"):
            found = _question_update(question, connection_id=connection.id)
            if found is not None and (since is None or found.created_at > since):
                updates.append(found)

        updates.sort(key=lambda update: update.created_at)
        return updates

    async def reply_to_update(
        self,
        connection: Connection,
        update: Update,
        text: str,
    ) -> None:
        """Answer a review or a question in place.

        Raises:
            NotSupportedError: If this update is not a review or a question.
            SocialChimpError: If Google refuses the reply.
        """
        location_name = _location_resource(connection)

        if update.kind in (UpdateKind.REVIEW_CREATED, UpdateKind.REVIEW_UPDATED):
            review_name = update.raw.get("name")
            review_id = update.raw.get("reviewId", update.id)
            review_name = (
                review_name
                if isinstance(review_name, str) and review_name
                else f"{location_name}/reviews/{review_id}"
            )
            async with self._client(LOCAL_API, connection.token.access_token) as http:
                await http.json(
                    "PATCH",
                    f"/{review_name}",
                    params={"updateMask": "comment"},
                    json={"comment": text},
                )
            return

        if update.kind in (UpdateKind.QUESTION_CREATED, UpdateKind.ANSWER_CREATED):
            name = str(update.raw.get("name", ""))
            question_name = (
                name.rsplit("/answers/", 1)[0]
                if update.kind is UpdateKind.ANSWER_CREATED
                else name
            )
            async with self._client(QANDA_API, connection.token.access_token) as http:
                await http.json(
                    "POST",
                    f"/{question_name}/answers:upsert",
                    json={"answer": {"text": text}},
                )
            return

        raise NotSupportedError(
            platform=PLATFORM_NAME,
            what="answering this kind of update",
            suggestion="Only a review or a question can be answered here.",
        )

    async def get_location(self, connection: Connection) -> BusinessLocation:
        """Read the current business information for this location."""
        location_name = _location_resource(connection)
        async with self._client(
            BUSINESS_INFORMATION_API, connection.token.access_token
        ) as http:
            reply = await http.json(
                "GET",
                f"/{location_name}",
                params={"readMask": "title,phoneNumbers,storefrontAddress,categories"},
            )
        return _business_location_from(reply, connection)

    async def update_location(
        self,
        connection: Connection,
        fields: RawData,
    ) -> BusinessLocation:
        """Change some of this location's business information.

        Raises:
            ConfigError: If `fields` is empty - there is nothing to send
                Google a field mask for.
        """
        if not fields:
            message = (
                "fields is empty, so there is nothing to change. Name the "
                "fields to update, the way Google's own field mask does."
            )
            raise ConfigError(message)

        location_name = _location_resource(connection)
        async with self._client(
            BUSINESS_INFORMATION_API, connection.token.access_token
        ) as http:
            reply = await http.json(
                "PATCH",
                f"/{location_name}",
                params={"updateMask": ",".join(fields)},
                json=fields,
            )
        return _business_location_from(reply, connection)

    async def verification_options(
        self,
        connection: Connection,
    ) -> Sequence[VerificationOption]:
        """List the ways this location could be verified right now."""
        bare = _bare_location(connection)
        async with self._client(
            VERIFICATIONS_API, connection.token.access_token
        ) as http:
            reply = await http.json(
                "POST",
                f"/{bare}:fetchVerificationOptions",
                json={"languageCode": "en"},
            )
        return tuple(
            VerificationOption(
                method=_text(item, "verificationMethod", "list verification options"),
                display_data={
                    key: value
                    for key, value in item.items()
                    if key != "verificationMethod"
                },
            )
            for item in _items_in(reply, "options")
        )

    async def start_verification(
        self,
        connection: Connection,
        method: str,
    ) -> Verification:
        """Ask Google to verify this location by one of the offered ways."""
        bare = _bare_location(connection)
        async with self._client(
            VERIFICATIONS_API, connection.token.access_token
        ) as http:
            reply = await http.json(
                "POST",
                f"/{bare}:verify",
                json={"method": method, "languageCode": "en"},
            )
        return Verification(
            id=_text(reply, "name", "start a verification"),
            method=str(reply.get("method", method)),
            state=str(reply.get("state", "PENDING")),
            raw=reply,
        )

    async def complete_verification(
        self,
        connection: Connection,
        verification_id: str,
        pin: str,
    ) -> Verification:
        """Finish a verification with the code the business owner was sent."""
        async with self._client(
            VERIFICATIONS_API, connection.token.access_token
        ) as http:
            reply = await http.json(
                "POST",
                f"/{verification_id}:complete",
                json={"pin": pin},
            )
        return Verification(
            id=str(reply.get("name", verification_id)),
            method=str(reply.get("method", "")),
            state=str(reply.get("state", "COMPLETED")),
            raw=reply,
        )

    async def verification_state(self, connection: Connection) -> str:
        """Ask where this location's verification stands right now."""
        bare = _bare_location(connection)
        async with self._client(
            VERIFICATIONS_API, connection.token.access_token
        ) as http:
            reply = await http.json("GET", f"/{bare}/voiceOfMerchantState")

        if reply.get("hasVoiceOfMerchant") is True:
            return "VERIFIED"
        if "hasBusinessAuthority" in reply:
            return "PENDING"
        return "UNVERIFIED"

    def check_signature(
        self,
        body: bytes,
        headers: Mapping[str, str],
        *,
        secret: str,
    ) -> None:
        """Check a Pub/Sub push really came from Google and is still fresh.

        Entirely offline - see the module docstring for what `secret` holds
        here and why.

        Raises:
            ConfigError: If `secret` is not the JSON this platform expects.
            SignatureError: If the request cannot be trusted. Answer 401 and
                do nothing else with it.
        """
        keys, audience, service_account = _webhook_config(secret)

        authorization = ""
        for name, value in headers.items():
            if name.lower() == "authorization":
                authorization = value
                break

        if not authorization.startswith("Bearer "):
            raise SignatureError(_UNTRUSTED)

        _verify_google_id_token(
            authorization[len("Bearer ") :],
            keys=keys,
            audience=audience,
            service_account=service_account,
        )

    def read_updates(self, body: bytes) -> list[Update]:
        """Turn a checked Pub/Sub push into the update it carries.

        Raises:
            PlatformError: If the body is not a Pub/Sub push at all.
        """
        try:
            envelope = json.loads(body)
        except json.JSONDecodeError as broken:
            message = "This is not a Pub/Sub push - the body is not JSON."
            raise PlatformError(message, platform=PLATFORM_NAME) from broken

        message_field = envelope.get("message") if isinstance(envelope, dict) else None
        data = message_field.get("data") if isinstance(message_field, dict) else None
        if not isinstance(data, str) or not data:
            return []

        try:
            notification = json.loads(base64.b64decode(data))
        except ValueError:
            return []
        if not isinstance(notification, dict):
            return []

        return [_update_from_notification(notification)]

    def read_update(self, body: bytes, headers: Mapping[str, str]) -> Update:
        """Turn a checked Pub/Sub push into the one update it carries.

        Raises:
            PlatformError: If the push carried nothing to act on.
        """
        found = self.read_updates(body)
        if not found:
            message = (
                "This Pub/Sub message carried nothing socialchimp recognises "
                "- there was no message.data to decode. Check "
                "check_signature passed first."
            )
            raise PlatformError(message, platform=PLATFORM_NAME)
        return found[0]
