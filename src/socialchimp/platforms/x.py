"""X (Twitter): the network where the bill arrives before the first post.

Everywhere else socialchimp goes, the hard part is the code. Here the hard
part is getting an app that is allowed to post at all.

## Access is paid, and it is tiered

**You cannot post to X for free.** There is no self-serve free tier worth
planning around any more: an app that may publish is one somebody has paid
for, the plans are metered, and what a plan lets you do changes without
notice. Because of that there is **not a single price or monthly cap written
anywhere in this file**. Any number put here would be out of date within
months, and somebody would build a schedule around it.

Two addresses are worth keeping open while you set this up:

- Create the app and its OAuth client by hand in the developer portal:
  https://developer.x.com/en/portal/dashboard
- Look up, buy and change what your app is allowed to do:
  https://console.x.com

There is no `create_app` here, because there is nothing to automate. Somebody
has to fill in the form, agree to the terms, choose a plan, and add your
redirect address to the OAuth client. Then hand socialchimp the client id and
secret as `AppCredentials`.

**When X refuses because of the plan**, it does not say "you have not paid".
It answers 403 with `reason: client-not-enrolled`, which reads exactly like a
permission your app forgot to ask for - and people spend afternoons rewriting
scopes over it. socialchimp names that one on sight: the message says the
plan is what refused this, that it is not a bug in your code, and where to go
and change it.

## Signing someone in

Two steps, like Mastodon. `start_login` gives you an address to send the
person to; `finish_login` swaps the code they come back with for a token. The
address carries the hash of a secret (PKCE), the secret itself comes back to
you in `SendToNetwork.remember`, and you hand it to `finish_login`. Keep it
with that person's session rather than in memory - they may be sent away by
one web worker and come back to another.

The scopes asked for are `tweet.read`, `tweet.write`, `users.read` and
**`offline.access`**.

**Leave `offline.access` out and X sends back no refresh token at all.** The
access token then dies two hours later and the person has to sign in again.
This is the single most expensive mistake to make here, because it works
perfectly on the morning you write it and starts logging people out after
lunch. If you pass your own `scopes`, put `offline.access` in them.

## Tokens

An access token lasts two hours. Renewing needs your app's client id and
secret, so `refresh` is handed them the same way a sign-in is - `SocialChimp`
reads them out of your storage for you.

X replaces the refresh token as well, so **whatever comes back from a renewal
has to be saved**. `TokenManager` takes a lock and saves for you; if you call
`refresh` yourself, that part is yours.

## What a post can carry

    Post(
        text="Hello",
        media=(Media.from_file("clip.mp4"),),
        reply_to="1800000000000000000",
        options={
            "reply_settings": "following",   # who may reply
            "quote_tweet_id": "1800000000000000001",
        },
    )

Anything else is refused before we send it, with a message listing what is
accepted.

The limit is **280 characters**. Subscribers to X's paid tier can post far
longer ones, and there is nothing in the API that tells us whether the
account we are posting as is one of them. So we do not guess: 280 is what is
declared, and an account entitled to more can send its own longer post past
socialchimp's check by asking for the limit it knows it has.

## Files go up the old way

A post is one request. A file is four: **INIT**, then **APPEND** once per
piece, then **FINALIZE**, and for video a **STATUS** call in a loop until X
has finished encoding it. Only then can the file be named on a post.

Pieces are read off disk one at a time through `Media.piece`, so a large
video costs one piece of memory rather than all of it.

**Alt text is a fifth request.** X has nowhere to carry a description on the
upload itself, so a `Media.alt_text` goes up on its own afterwards, through
`POST /2/media/metadata`, and it has to happen before the file is named on a
post - X will not take a description for a file that is already published.
A file with no alt text sends nothing extra.

## A thread is five posts, not one thing

X has no thread. A thread is posts chained together, each one replying to the
one before, and `publish_thread` does that chaining for you.

Which raises the question this file has to answer out loud: **what happens
when the third post of five fails?** Two posts are already live and public.
The choices are to carry on, to delete what went out, or to stop.

socialchimp stops. Posts four and five are never sent - they would be
replying to a post that does not exist, so they would either fail as well or,
worse, land under post two and read as somebody's half-finished thought.
Nothing already published is deleted either: deleting a person's words on
their behalf, in reaction to an error, is not a decision a library gets to
make.

What you get is a `PartialThreadError` saying exactly that: how far it got,
which post broke, how many never went out, and the results of the ones that
are live so you can carry the thread on yourself from the last id.

## What X cannot do here

- **No scheduling.** `Feature.SCHEDULE` is missing, so a post with
  `publish_at` is refused rather than published now.
- **No app to create.** See above.
- **No pushed updates.** X does have streaming and account activity, and
  both are gated behind paid products of their own. So `Feature.PUSH_UPDATES`
  is off and mentions are read on a timer through `fetch_updates`, which
  works on every plan that can read at all.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

# anyio comes with httpx, so waiting through it adds no new dependency and
# lets this run under trio as happily as under asyncio.
import anyio
import httpx

from socialchimp.errors import (
    AuthError,
    ConfigError,
    InvalidPostError,
    NotAllowedError,
    PlatformError,
    RateLimitError,
    SocialChimpError,
    TokenExpiredError,
)
from socialchimp.events import Update
from socialchimp.features import (
    Feature,
    Limits,
    TextCount,
    check_option_names,
    check_post,
)
from socialchimp.http import (
    HttpClient,
    error_from_response,
    rate_limit_from_headers,
    read_body,
    retry_after_seconds,
)
from socialchimp.models import (
    Connection,
    Media,
    MediaKind,
    Post,
    PostResult,
    PostState,
    RawData,
    Token,
)
from socialchimp.platform import Finished, LoginRequest, SendToNetwork

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from socialchimp.http import Retries
    from socialchimp.models import AppCredentials

__all__ = [
    "PartialThreadError",
    "XPlatform",
    "x_errors",
]

PLATFORM_NAME: Final = "x"

API_URL: Final = "https://api.x.com/2"
"""Where every request goes, including the ones about tokens."""

SIGN_IN_URL: Final = "https://x.com/i/oauth2/authorize"
"""The page a person is sent to so they can approve your app."""

PORTAL_URL: Final = "https://developer.x.com/en/portal/dashboard"
"""Where somebody creates the app and its OAuth client, by hand."""

PLANS_URL: Final = "https://console.x.com"
"""Where somebody looks up and changes what their app is allowed to do."""

# Paths, all joined onto `API_URL`.
TOKEN_PATH: Final = "/oauth2/token"  # noqa: S105 - a public address, not a secret
TWEETS_PATH: Final = "/tweets"
MEDIA_PATH: Final = "/media/upload"
MEDIA_METADATA_PATH: Final = "/media/metadata"
ME_PATH: Final = "/users/me"

DEFAULT_SCOPES: Final = ("tweet.read", "tweet.write", "users.read", "offline.access")
"""Enough to read an account's mentions and to post as them.

`offline.access` is the one that matters. Without it X sends back no refresh
token, the access token dies two hours later, and the person has to sign in
again. Keep it in any list of your own.
"""

OFFLINE_SCOPE: Final = "offline.access"
"""The scope that decides whether there is a refresh token at all."""

MAX_TEXT_LENGTH: Final = 280
"""Characters in an ordinary post, counted the way X counts them.

X's paid subscribers can post far longer ones. Nothing in the API says
whether an account is one of them, so this is what we declare for everybody.
"""

MAX_IMAGES: Final = 4
"""Pictures allowed on one post."""

MAX_VIDEOS_PER_POST: Final = 1
"""X takes one video per post, and will not mix video with pictures."""

MAX_IMAGE_BYTES: Final = 5 * 1024 * 1024
MAX_VIDEO_BYTES: Final = 512 * 1024 * 1024

DEFAULT_CHUNK_BYTES: Final = 4 * 1024 * 1024
"""How much of a file to send in one APPEND. X takes up to five megabytes."""

REPLY_SETTINGS: Final = ("everyone", "mentionedUsers", "following", "subscribers")
"""Who may reply to a post."""

POST_OPTIONS: Final = ("reply_settings", "quote_tweet_id")
"""The settings `Post.options` accepts here. Anything else is refused."""

RATE_LIMIT_WINDOW: Final = "15 minutes"
"""How long most of X's counting windows are. Some endpoints differ, and the
plan changes the numbers, so this is only ever said as a rough shape."""

# What X allows. Nothing is asked of X to know any of it, so this is built
# once and handed out rather than made again for every post in a thread.
_ALLOWED: Final = Limits(
    max_text_length=MAX_TEXT_LENGTH,
    # X counts an emoji as two, the way JavaScript does, so its 280 is not
    # Python's 280. Counting characters instead would send posts X refuses.
    # It also counts most Chinese, Japanese and Korean characters as two,
    # which this way of counting does not, so a post in one of those can
    # still come back refused - with X's own message rather than a wrong one
    # of ours.
    text_counted_in=TextCount.UTF16_UNITS,
    max_images=MAX_IMAGES,
    max_image_bytes=MAX_IMAGE_BYTES,
    max_videos=MAX_VIDEOS_PER_POST,
    max_video_bytes=MAX_VIDEO_BYTES,
)

# How long an access token lasts when X does not say. Two hours is what it
# has always been.
_DEFAULT_TOKEN_SECONDS: Final = 7200

# Long enough that nobody can guess one, short enough to sit in a URL.
_STATE_BYTES: Final = 24
_VERIFIER_BYTES: Final = 48

# What X says while it is still working on a file, and what it says when it
# has given up. Anything else means the file is ready.
_STILL_WORKING: Final = ("pending", "in_progress")
_GAVE_UP: Final = "failed"

# What kind of file X thinks it is being sent. Getting this wrong makes an
# upload fail at FINALIZE rather than at INIT, which is a long way to go to
# find out.
_IMAGE_CATEGORY: Final = "tweet_image"
_GIF_CATEGORY: Final = "tweet_gif"
_VIDEO_CATEGORY: Final = "tweet_video"

# The word in a 403 that means "your plan does not include this endpoint".
_NOT_ENROLLED: Final = "client-not-enrolled"


async def _wait(seconds: float) -> None:
    """Pause while X finishes working on a file.

    Kept as its own function so tests can watch the pauses instead of
    sitting through them.

    Args:
        seconds: How long to wait.
    """
    await anyio.sleep(seconds)


def _text(reply: RawData, key: str, when: str) -> str:
    """Read a value X always sends, and complain plainly if it did not.

    Args:
        reply: What X answered.
        key: The field we need.
        when: What we had asked it to do, for the message.

    Returns:
        The value.

    Raises:
        PlatformError: If the field is missing or empty. The whole reply is
            kept on the error so you can see what did arrive.
    """
    value = reply.get(key)
    if isinstance(value, str) and value:
        return value

    message = (
        f"X left {key!r} out of its reply when we asked it to {when}. That "
        f"should not happen. The whole reply is on this error."
    )
    raise PlatformError(message, platform=PLATFORM_NAME, raw=reply)


def _inside(reply: RawData) -> RawData:
    """Return the part of a reply that holds the answer.

    Everything under `/2` wraps its answer in `data`. The media endpoints
    were moved here out of the older v1.1 API and some replies still arrive
    the old way, flat. Reading both means one code path rather than two, and
    no surprise on the day X finishes the move.

    Args:
        reply: What X answered.

    Returns:
        The wrapped part, or the whole reply when there is no wrapper.
    """
    found = reply.get("data")
    return found if isinstance(found, dict) else reply


def _moment(text: str) -> datetime | None:
    """Read a time X wrote, such as `"2026-08-31T10:00:00.000Z"`.

    Args:
        text: The time as it arrived.

    Returns:
        The moment, always with a timezone, or `None` if it cannot be read.
    """
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    # X always writes UTC, but a time with no timezone compares wrongly
    # against every other time we hold, and it does it silently.
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _rfc3339(when: datetime) -> str:
    """Write a moment the way X's `start_time` wants it.

    X takes whole seconds and a `Z` on the end. What Python writes by
    default - `+00:00`, and fractions of a second when it has them - is
    refused, and the refusal reads like a problem with the account.

    Args:
        when: The moment to write.

    Returns:
        The moment as text, such as `"2026-08-31T10:00:00Z"`.
    """
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _challenge_for(verifier: str) -> str:
    """Hash the secret we keep, so only the hash travels to X.

    Args:
        verifier: The secret made at the start of a login.

    Returns:
        The hash, written the way the PKCE rules ask for.
    """
    digest = hashlib.sha256(verifier.encode()).digest()
    # Base64 with the two URL-unsafe characters swapped and the padding
    # dropped, which is what the PKCE rules ask for.
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _how_long_to_wait(response: httpx.Response) -> float | None:
    """Work out how long to wait after X has asked us to slow down.

    Args:
        response: The 429 to read.

    Returns:
        Seconds to wait, or `None` if X did not say. `Retry-After` wins when
        it is there, because that is X answering the question directly.
        Otherwise the moment its counting window starts again is turned into
        a wait. Never negative: a window that has already turned means there
        is nothing left to wait for.
    """
    asked_for = retry_after_seconds(response)
    if asked_for is not None:
        return asked_for

    left = rate_limit_from_headers(response.headers)
    if left is None or left.resets_at is None:
        return None
    return max((left.resets_at - datetime.now(UTC)).total_seconds(), 0.0)


def _said_in(body: RawData) -> str:
    """Pull X's own explanation out of a refusal.

    Args:
        body: The reply, already read into a dictionary.

    Returns:
        The explanation, or an empty string when there is not one.
    """
    for key in ("detail", "title"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return f" It said: {value}"
    return ""


def _it_is_the_plan(body: RawData) -> SocialChimpError:
    """Build the error for a 403 that is really about what has been paid for.

    Args:
        body: X's reply, already read into a dictionary.

    Returns:
        The error to raise, saying plainly that the plan refused this.
    """
    needed = body.get("required_enrollment")
    names = f" It wants: {needed}." if isinstance(needed, str) and needed else ""
    message = (
        f"X refused this because of your app's plan, not because of anything "
        f"in your code (403 {_NOT_ENROLLED}). This is not a bug and no "
        f"change to your scopes will fix it: the endpoint is not part of "
        f"what this app is enrolled for.{names} Look up what the app has, "
        f"and change it, at {PLANS_URL}.{_said_in(body)}"
    )
    return NotAllowedError(message, platform=PLATFORM_NAME, raw=body)


def x_errors(response: httpx.Response) -> SocialChimpError:
    """Turn an unhappy reply from X into a socialchimp error.

    Three replies are worth naming rather than passing straight through:

    - A **403 with `client-not-enrolled`** is the plan, not a permission.
      It reads exactly like a missing scope, and people rewrite their scopes
      over it for an afternoon. It becomes a `NotAllowedError` whose message
      says what it really is and where to go and change it.
    - A **403 about duplicate content** is X refusing to post the same words
      twice. That is a problem with the post, so it becomes an
      `InvalidPostError`.
    - A **429** carries no `Retry-After`. X says instead when its counting
      window starts again, in `x-rate-limit-reset`, so that is turned into
      the wait.

    Everything else is the shared mapping: 401 is an `AuthError`, 403 a
    `NotAllowedError`, 404 a `NotFoundError`.

    Args:
        response: The reply to turn into an error.

    Returns:
        The error to raise.
    """
    if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
        body = read_body(response)
        message = (
            f"X is asking us to slow down (429). It counts in windows of "
            f"about {RATE_LIMIT_WINDOW}, and how many requests fit in one "
            f"depends on the endpoint and on your plan.{_said_in(body)}"
        )
        return RateLimitError(
            message,
            # A 429 from X almost never carries Retry-After, so the reset
            # header is what a caller has to go on.
            retry_after=_how_long_to_wait(response),
            platform=PLATFORM_NAME,
            raw=body,
        )

    if response.status_code == httpx.codes.FORBIDDEN:
        body = read_body(response)

        if body.get("reason") == _NOT_ENROLLED or "required_enrollment" in body:
            return _it_is_the_plan(body)

        detail = body.get("detail")
        if isinstance(detail, str) and "duplicate" in detail.lower():
            message = (
                f"X will not take this post because it has already posted "
                f"these exact words for this account (403). Change something "
                f"in the text, or leave it - it is already up.{_said_in(body)}"
            )
            return InvalidPostError(message, platform=PLATFORM_NAME, raw=body)

    return error_from_response(response, platform=PLATFORM_NAME)


class PartialThreadError(SocialChimpError):
    """A thread stopped part way through, and part of it is already public.

    Raised by `XPlatform.publish_thread`. X has no thread of its own - a
    thread is separate posts chained together - so there is no way to send
    one and have all of it or none of it arrive.

    When one post is refused, socialchimp stops there. The posts after it are
    never sent, because they would be replying to a post that does not exist.
    The posts before it are left alone, because deleting somebody's published
    words in reaction to an error is not a library's decision to make.

    Carry on from `published[-1].id` once you have fixed whatever X objected
    to, and the thread reads as if nothing happened. What X actually said is
    on `__cause__`.

    Attributes:
        published: What did go out, in order. These posts are live.
        failed_at: Which post broke, counting from one.
        posts_left: How many were never sent.
    """

    def __init__(
        self,
        *,
        published: Sequence[PostResult],
        failed_at: int,
        of_how_many: int,
    ) -> None:
        """Say how far the thread got before it stopped.

        Args:
            published: The posts that are live, in order.
            failed_at: Which post broke, counting from one.
            of_how_many: How many posts the thread was meant to have.
        """
        left = of_how_many - failed_at
        message = (
            f"This thread stopped at post {failed_at} of {of_how_many}. "
            f"{len(published)} posts are live and have not been deleted; the "
            f"remaining {left} were not sent, because they would have "
            f"replied to a post that does not exist. Carry on from the last "
            f"id on `published` once the problem is fixed. What X said is on "
            f"this error's __cause__."
        )
        super().__init__(message, platform=PLATFORM_NAME)
        self.published = tuple(published)
        self.failed_at = failed_at
        self.posts_left = left


def _app_or_refuse(app: AppCredentials | None, what: str) -> AppCredentials:
    """Insist on your app's credentials before going any further.

    Args:
        app: The credentials that arrived, which may be none at all.
        what: The thing we were trying to do, for the message.

    Returns:
        The credentials.

    Raises:
        ConfigError: If there are none, saying every step somebody has to
            take by hand before X will talk to them at all.
    """
    if app is None:
        message = (
            f"X needs your app's client id and secret to {what}, and none "
            f"were given. socialchimp cannot make them for you: somebody has "
            f"to create an app at {PORTAL_URL}, pick a plan that allows "
            f"posting at {PLANS_URL}, set up an OAuth 2.0 client on it, and "
            f"add your redirect address to that client. Then save what they "
            f"got with Storage.save_app and socialchimp will pass it in for "
            f"you."
        )
        raise ConfigError(message)
    return app


def _check_state(request: LoginRequest, callback: Mapping[str, str]) -> None:
    """Check the value that came back is the one we sent.

    This is what stops somebody handing your app a login they started
    themselves and having it saved against one of your users.

    Args:
        request: The request used to start the login.
        callback: The query values X sent back.

    Raises:
        AuthError: If both sides have a state and they are different.
    """
    returned = callback.get("state", "")
    if request.state is not None and returned and returned != request.state:
        message = (
            "The state X sent back did not match the one we sent. This login "
            "did not start here, so nothing has been saved. Start a new one."
        )
        raise AuthError(message, platform=PLATFORM_NAME)


def _code_from(callback: Mapping[str, str]) -> str:
    """Pull the login code out of what X sent back.

    Args:
        callback: The query values X sent back.

    Returns:
        The code to swap for a token.

    Raises:
        AuthError: If the person said no, or if there is no code.
    """
    refused = callback.get("error")
    if refused:
        said = callback.get("error_description", "")
        detail = f" It said: {said}" if said else ""
        message = (
            f"X did not sign this person in ({refused}). Usually they "
            f"pressed cancel on the approval page.{detail}"
        )
        raise AuthError(message, platform=PLATFORM_NAME)

    code = callback.get("code")
    if not code:
        message = (
            "X sent no code back, so there is nothing to swap for a token. "
            "Check you are passing the whole query string from your redirect "
            "address."
        )
        raise AuthError(message, platform=PLATFORM_NAME)
    return code


def _verifier_from(remember: RawData | None) -> str:
    """Read the secret `start_login` made back out of what your app kept.

    Args:
        remember: What `start_login` put in `SendToNetwork.remember`.

    Returns:
        The secret to send with the code.

    Raises:
        AuthError: If it did not come back. Without it X cannot tell that
            this is the same sign-in it started, and will refuse the code -
            so saying it here is clearer than letting X say it in its own
            words.
    """
    verifier = (remember or {}).get("code_verifier")
    if not isinstance(verifier, str) or not verifier:
        message = (
            "This sign-in cannot be finished because the secret made at the "
            "start did not come back. Pass SendToNetwork.remember to "
            "finish_login as `remember`. Keep it with that person's session "
            "rather than in memory: they may be sent away by one web worker "
            "and come back to another."
        )
        raise AuthError(message, platform=PLATFORM_NAME)
    return verifier


def _proof_of_app(app: AppCredentials) -> tuple[dict[str, str], dict[str, str]]:
    """Work out how this app proves who it is at the token endpoint.

    X has two kinds of OAuth client. One holds a secret and proves itself
    with an ordinary Basic header. The other - a mobile app, a single-page
    app, anything whose code a person could read - holds no secret and names
    itself in the form instead. Which one you have is chosen in the portal.

    Args:
        app: Your app's credentials.

    Returns:
        The headers to add, and the extra form values to send. One of the
        two is always empty.
    """
    if app.client_secret:
        pair = base64.b64encode(
            f"{app.client_id}:{app.client_secret}".encode()
        ).decode()
        # X still wants the id in the form as well, and sending it costs
        # nothing, so both kinds of app send the same form.
        return {"Authorization": f"Basic {pair}"}, {"client_id": app.client_id}
    return {}, {"client_id": app.client_id}


def _expiry_from(reply: RawData) -> datetime:
    """Work out when an access token stops working.

    Args:
        reply: What X's token endpoint answered.

    Returns:
        The moment it runs out. X's tokens last two hours, and that is what
        is assumed when it does not say.
    """
    seconds = reply.get("expires_in")
    lasts = seconds if isinstance(seconds, int) else _DEFAULT_TOKEN_SECONDS
    return datetime.now(UTC) + timedelta(seconds=lasts)


def _checked_option(key: str, value: object) -> str:
    """Check one post setting and turn it into what X wants to see.

    Args:
        key: Which setting it is.
        value: What was given for it.

    Returns:
        The value as text, ready to send.

    Raises:
        InvalidPostError: If the value is not one X takes. The message lists
            what is accepted.
    """
    if key == "reply_settings":
        if value not in REPLY_SETTINGS:
            message = (
                f"reply_settings is {value!r}, which X does not know. It "
                f"accepts: {', '.join(REPLY_SETTINGS)}."
            )
            raise InvalidPostError(message, platform=PLATFORM_NAME)
        return str(value)

    if not isinstance(value, str) or not value:
        message = (
            f"{key} is {value!r}, but it has to be a post's id, written as "
            f"text. X's ids are longer than a whole number safely holds in "
            f"a lot of places, which is why it sends them as text too."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME)
    return value


def _checked_options(options: RawData) -> dict[str, str]:
    """Check every setting on a post before anything is sent.

    Args:
        options: What was put in `Post.options`.

    Returns:
        The same settings, ready to send.

    Raises:
        InvalidPostError: If a setting is unknown or its value is wrong. This
            happens before any request, so a typo costs nothing.
    """
    check_option_names(options, platform=PLATFORM_NAME, allowed=POST_OPTIONS)
    return {key: _checked_option(key, value) for key, value in options.items()}


def _body_for(post: Post, options: dict[str, str], media_ids: list[str]) -> RawData:
    """Build the JSON for one post.

    Args:
        post: The post about to be sent.
        options: The settings, already checked.
        media_ids: What X called each file that was uploaded.

    Returns:
        The body to send.
    """
    body: dict[str, Any] = {"text": post.text}
    if post.reply_to is not None:
        body["reply"] = {"in_reply_to_tweet_id": post.reply_to}
    if media_ids:
        body["media"] = {"media_ids": media_ids}
    body.update(options)
    return body


def _category_for(item: Media) -> str:
    """Say what kind of file X is about to be sent.

    X wants to be told at INIT, and a wrong answer is only noticed at
    FINALIZE - a long way to go to find out. An animated picture is its own
    kind here, separate from an ordinary one.

    Args:
        item: The file about to go up.

    Returns:
        X's word for this kind of file.
    """
    if item.kind is MediaKind.VIDEO:
        return _VIDEO_CATEGORY
    return _GIF_CATEGORY if item.content_type == "image/gif" else _IMAGE_CATEGORY


def _how_big(item: Media) -> int:
    """Find out how many bytes a file is, without reading any of them.

    Args:
        item: The file attached to the post.

    Returns:
        How many bytes there are altogether.

    Raises:
        InvalidPostError: If all we have is a link to it. `Media.size` says
            `None` for one, because finding out would mean downloading it -
            and X will not fetch it either.
    """
    total = item.size
    if total is None:
        message = (
            f"X will not fetch {item.url!r} for you - it only takes files "
            f"sent to it, a piece at a time. Download the file first, then "
            f"use Media.from_file, which reads it off disk as it goes rather "
            f"than holding all of it in memory."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME)
    return total


def _link_to(connection: Connection, post_id: str) -> str:
    """Build the address of a post somebody can open.

    Args:
        connection: The account that published it.
        post_id: X's id for the post.

    Returns:
        The address. Built from the handle when we know it, and from X's own
        redirect when we do not - `/i/status/<id>` sends a browser to the
        right place whoever wrote the post.
    """
    handle = connection.account_name.lstrip("@")
    return f"https://x.com/{handle or 'i'}/status/{post_id}"


def _the_state_of(reply: RawData) -> tuple[str, RawData]:
    """Read how far X has got with a file.

    Args:
        reply: What INIT, FINALIZE or STATUS answered.

    Returns:
        The state, and the whole block X sent about it. An empty state means
        X said nothing, which is what a picture looks like: ready already.
    """
    progress = _inside(reply).get("processing_info")
    if not isinstance(progress, dict):
        return "", {}
    said = progress.get("state")
    return (said if isinstance(said, str) else ""), progress


def _x_gave_up_on_the_file(progress: RawData) -> SocialChimpError:
    """Build the error for a file X could not make anything of.

    Args:
        progress: The `processing_info` block X sent.

    Returns:
        The error to raise. This is a problem with the file rather than a
        mystery, so it is one an app can explain to a person.
    """
    trouble = progress.get("error")
    said = trouble.get("message") if isinstance(trouble, dict) else None
    detail = f" It said: {said}" if isinstance(said, str) and said else ""
    message = (
        f"X could not make anything of this file and gave up on it. It is "
        f"usually the format or the encoding rather than the size - X takes "
        f"far less than it lets you upload.{detail}"
    )
    return InvalidPostError(message, platform=PLATFORM_NAME, raw=progress)


class XPlatform:
    """Everything socialchimp does with X.

    Signing people in, keeping their two-hour tokens working, publishing,
    posting threads, uploading files a piece at a time, and reading mentions.

        x = XPlatform()

    It holds nothing belonging to one account and nothing belonging to your
    app. Your client id and secret arrive as an argument every time they are
    needed - on the `LoginRequest` for a sign-in, and on `refresh` for a
    renewal - so one of these serves every account and every app.

    Attributes:
        name: `"x"`.
        features: What X can do here. `CREATE_APP` is missing because there
            is nothing to automate, `SCHEDULE` because X has no scheduling,
            and `PUSH_UPDATES` because both of the products that would push
            are behind paid plans of their own.
    """

    name: str = PLATFORM_NAME

    features: Feature = (
        Feature.POST_TEXT
        | Feature.POST_IMAGE
        | Feature.POST_VIDEO
        | Feature.REPLY
        | Feature.DELETE_POST
        | Feature.READ_POSTS
    )

    def __init__(
        self,
        *,
        timeout: float = 300.0,
        retries: Retries | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        media_checks: int = 30,
        media_wait_seconds: float = 1.0,
        updates_per_check: int = 40,
    ) -> None:
        """Set X up for one app.

        Args:
            timeout: Seconds to wait for a reply. Five minutes by default,
                because a single piece of a video takes far longer to send
                than an ordinary request.
            retries: How many times to try again after a hiccup. Left out,
                the shared default is used.
            transport: Where requests actually go. Leave it out for ordinary
                calls; pass your own to send them somewhere else.
            chunk_bytes: How much of a file to send in one piece. X takes up
                to five megabytes.
            media_checks: How many times to ask whether a video has finished
                being encoded before giving up.
            media_wait_seconds: How long to wait between those checks, when
                X does not say how long to wait itself.
            updates_per_check: How many mentions to read at a time. X allows
                between 5 and 100.

        Raises:
            ConfigError: If `chunk_bytes` is not a positive number of bytes.
        """
        if chunk_bytes <= 0:
            message = (
                f"chunk_bytes is {chunk_bytes}, but a piece of a file has to "
                f"be at least one byte. Try {DEFAULT_CHUNK_BYTES} for four "
                f"megabytes at a time."
            )
            raise ConfigError(message)

        self._timeout = timeout
        self._retries = retries
        self._transport = transport
        self._chunk_bytes = chunk_bytes
        self._media_checks = media_checks
        self._media_wait_seconds = media_wait_seconds
        self._updates_per_check = updates_per_check

    def _client(self, token: str | None = None) -> HttpClient:
        """Make a client pointed at X.

        Args:
            token: The account's token, for anything that needs one.

        Returns:
            A client. Use it in an `async with` block so it closes itself.
        """
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        return HttpClient(
            API_URL,
            platform=PLATFORM_NAME,
            headers=headers,
            timeout=self._timeout,
            transport=self._transport,
            retries=self._retries,
            errors=x_errors,
        )

    def api_base(self, connection: Connection) -> str:
        """Return where X keeps its API.

        Args:
            connection: The account we are about to act as. X has one
                address for everybody, so this is not used.

        Returns:
            The address, with no trailing slash.
        """
        return API_URL

    def auth_headers(self, connection: Connection) -> Mapping[str, str]:
        """Return the header that proves we may act as this account.

        Args:
            connection: The account we are acting as.

        Returns:
            One `Authorization` header carrying the access token. By the time
            this runs the token has already been renewed if it needed it.
        """
        return {"Authorization": f"Bearer {connection.token.access_token}"}

    async def limits(self, connection: Connection) -> Limits:
        """Return what X allows.

        Nothing is asked, because there is nothing to ask: X has no endpoint
        that reports what an account may post. This stays `async` because
        every platform's `limits` is.

        The 280 is the ordinary limit. X's paid subscribers can post much
        longer ones, and **there is no field anywhere in the API that says
        whether the account we are posting as is one of them**. Guessing
        would mean either refusing posts a subscriber could have sent or
        sending posts X will refuse, so we do neither and declare the number
        that is true for everybody.

        Args:
            connection: The account to ask about. Not used here.

        Returns:
            What X allows right now.
        """
        return _ALLOWED

    async def start_login(self, request: LoginRequest) -> SendToNetwork:
        """Build the address to send somebody to so they can approve your app.

        Nothing is sent to X here. The address carries the hash of a secret;
        the secret itself comes back to you in `remember`, and is sent later,
        in `finish_login`, to prove the code came back to the same place that
        asked for it.

        Keep `remember` with that person's session and hand it back. Nothing
        is held here between the two calls, because the person may be sent
        away by one web worker and come back to another.

        Args:
            request: Where to send them back to, what to ask for, and your
                app's credentials.

        Returns:
            Always a `SendToNetwork`: X has an approval page, so there is
            somewhere to send people.

        Raises:
            ConfigError: If the request carries no credentials.
        """
        app = _app_or_refuse(request.app, "sign somebody in")

        state = request.state or secrets.token_urlsafe(_STATE_BYTES)
        verifier = secrets.token_urlsafe(_VERIFIER_BYTES)

        # If you replace these scopes, keep `offline.access`. Without it X
        # sends back no refresh token at all: the access token dies two hours
        # later and the person has to sign in again. It works perfectly the
        # morning you write it and starts logging people out after lunch,
        # which is why it is spelled out here rather than left to the docs.
        asked_for = request.scopes or DEFAULT_SCOPES

        query = httpx.QueryParams(
            {
                "response_type": "code",
                "client_id": app.client_id,
                "redirect_uri": request.redirect_uri,
                "scope": " ".join(asked_for),
                "state": state,
                "code_challenge": _challenge_for(verifier),
                "code_challenge_method": "S256",
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
    ) -> Finished:
        """Swap the code X sent back for a token, and build a connection.

        Hand this the whole query string X put on your redirect address, as a
        dictionary, along with the `remember` value `start_login` gave you.

        Args:
            request: The same request used to start the login.
            callback: The query values X sent back. It must have `code`;
                `state` is checked when it is there.
            remember: What `start_login` put in `SendToNetwork.remember`.

        Returns:
            The finished connection. Save it.

        Raises:
            AuthError: If the person said no, if there is no code, if the
                state that came back is not the one we sent, or if the secret
                from `start_login` did not come back.
            ConfigError: If the request carries no credentials.
            PlatformError: If X answered without a token.
        """
        app = _app_or_refuse(request.app, "finish signing somebody in")
        _check_state(request, callback)
        code = _code_from(callback)
        verifier = _verifier_from(remember)

        headers, named = _proof_of_app(app)
        form: dict[str, Any] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": request.redirect_uri,
            # The other half of the pair from `start_login`. X hashes it and
            # checks the result against what it was sent earlier.
            "code_verifier": verifier,
            **named,
        }

        asked_for = request.scopes or DEFAULT_SCOPES

        async with self._client() as http:
            reply = await http.json("POST", TOKEN_PATH, data=form, headers=headers)
            access_token = _text(reply, "access_token", "sign someone in")
            me = _inside(
                await http.json(
                    "GET",
                    ME_PATH,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            )

        # X may grant less than we asked for, and it says so here.
        granted = reply.get("scope")
        given = granted.split() if isinstance(granted, str) and granted else []
        scopes = tuple(given) if given else asked_for

        account_id = _text(me, "id", "say who just signed in")
        handle = _text(me, "username", "say who just signed in")
        renewal = reply.get("refresh_token")

        return Finished(
            connection=Connection(
                id=f"{PLATFORM_NAME}:{account_id}",
                platform=PLATFORM_NAME,
                host=None,
                account_id=account_id,
                account_name=f"@{handle}",
                token=Token(
                    access_token=access_token,
                    refresh_token=renewal if isinstance(renewal, str) else None,
                    expires_at=_expiry_from(reply),
                ),
                scopes=scopes,
                extra={"profile_url": f"https://x.com/{handle}"},
            )
        )

    async def refresh(
        self,
        connection: Connection,
        app: AppCredentials | None = None,
    ) -> Token:
        """Get a fresh pair of tokens for an account.

        X replaces the refresh token as well as the access token, so the one
        we renewed with stops working the instant this succeeds. **Whatever
        comes back has to be saved.** `TokenManager` takes a lock and saves
        for you; if you call this yourself, that part is yours.

        Args:
            connection: The account whose token is running out.
            app: Your app's client id and secret. X signs a renewal with
                them, so this is not optional here - `SocialChimp` reads them
                out of your storage and passes them in.

        Returns:
            The new pair. Save them.

        Raises:
            ConfigError: If no credentials arrived.
            TokenExpiredError: If there is no refresh token, or X will not
                take the one we have.
            PlatformError: If X answered without a token.
        """
        signing = _app_or_refuse(app, "renew a token")

        renewal = connection.token.refresh_token
        if renewal is None:
            message = (
                f"The token for {connection.id!r} has run out and there is "
                f"no refresh token to replace it with. That happens when the "
                f"sign-in did not ask for the {OFFLINE_SCOPE!r} scope - "
                f"without it X issues no refresh token at all and every "
                f"account stops working two hours after it connects. Add it "
                f"to your scopes, and ask this person to connect their "
                f"account again."
            )
            raise TokenExpiredError(message, platform=PLATFORM_NAME)

        headers, named = _proof_of_app(signing)
        form: dict[str, Any] = {
            "grant_type": "refresh_token",
            "refresh_token": renewal,
            **named,
        }

        async with self._client() as http:
            try:
                reply = await http.json("POST", TOKEN_PATH, data=form, headers=headers)
            except (AuthError, PlatformError) as refused:
                # X having trouble of its own is not the same as a dead
                # refresh token, and treating it as one would have apps
                # throwing away connections that were fine.
                if _is_xs_own_fault(refused):
                    raise
                message = (
                    f"X will not renew the token for {connection.id!r}. Its "
                    f"refresh token has run out, been used already, or the "
                    f"person removed your app from their X account. The "
                    f"person has to connect their account again."
                )
                raise TokenExpiredError(
                    message, platform=PLATFORM_NAME, raw=refused.raw
                ) from refused

        # X does replace the refresh token. On the rare reply that leaves it
        # out, keeping the one we had is better than setting it to nothing
        # and locking the person out at the next renewal.
        replacement = reply.get("refresh_token")
        return Token(
            access_token=_text(reply, "access_token", "renew a token"),
            refresh_token=(
                replacement if isinstance(replacement, str) and replacement else renewal
            ),
            expires_at=_expiry_from(reply),
        )

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        """Publish a post.

        Files are uploaded first, one at a time, and a video is waited for -
        naming a file X has not finished encoding gets the post refused.

        Args:
            connection: The account to publish as.
            post: What to publish.

        Returns:
            What X said about the new post.

        Raises:
            InvalidPostError: If a setting is unknown, if the post breaks one
                of X's limits, or if X refuses it.
            NotAllowedError: If your app's plan does not include posting.
            NotSupportedError: If the post asks for something X cannot do,
                such as being published later.
            PlatformError: If a video never finishes being encoded.
        """
        options = self._checked(post)

        async with self._client(connection.token.access_token) as http:
            return await self._send(http, connection, post, options)

    async def publish_thread(
        self,
        connection: Connection,
        posts: Sequence[Post],
    ) -> list[PostResult]:
        """Publish several posts as a thread, each replying to the one before.

        X has no thread. A thread is separate posts chained together, so this
        sends them one after another and points each at the id of the last.
        The first one may reply to somebody else's post, which is how you
        answer a conversation with a thread; the rest may not, because their
        parent is decided here.

        **If one of them fails, this stops.** Nothing after it is sent, and
        nothing before it is deleted. See `PartialThreadError`, which carries
        what did go out so you can carry on from the last id.

        Every post is checked before the first one is sent, so a mistake in
        post five costs no posts at all.

        Args:
            connection: The account to publish as.
            posts: The posts, in the order they should appear.

        Returns:
            What X said about each post, in the same order.

        Raises:
            InvalidPostError: If there are no posts, if one of them breaks
                one of X's limits, or if any but the first names its own
                `reply_to`.
            PartialThreadError: If a post fails once one is already live.
            SocialChimpError: Whatever X said, if the very first post fails.
                Nothing is public at that point, so there is no half-posted
                thread to describe.
        """
        wanted = list(posts)
        if not wanted:
            message = (
                "This thread has no posts in it. Pass the posts to publish, "
                "in the order they should appear."
            )
            raise InvalidPostError(message, platform=PLATFORM_NAME)

        checked = [
            self._checked(post, place=(number, len(wanted)))
            for number, post in enumerate(wanted, start=1)
        ]

        published: list[PostResult] = []
        answering: str | None = None

        async with self._client(connection.token.access_token) as http:
            for number, (asked, options) in enumerate(
                zip(wanted, checked, strict=True), start=1
            ):
                # The first post keeps whatever it was given, so a thread can
                # answer somebody else. Every later one answers the last, on
                # a copy - the caller's own post is left exactly as it was.
                sending = (
                    asked
                    if answering is None
                    else Post(
                        text=asked.text,
                        media=asked.media,
                        reply_to=answering,
                        options=asked.options,
                    )
                )

                try:
                    result = await self._send(http, connection, sending, options)
                except SocialChimpError as refused:
                    if not published:
                        # Nothing is public, so there is no half-posted
                        # thread to describe. What X said is the whole story.
                        raise
                    raise PartialThreadError(
                        published=published,
                        failed_at=number,
                        of_how_many=len(wanted),
                    ) from refused

                published.append(result)
                answering = result.id

        return published

    def _checked(
        self,
        post: Post,
        place: tuple[int, int] | None = None,
    ) -> dict[str, str]:
        """Check one post against X's rules before anything is sent.

        Args:
            post: The post about to be sent.
            place: Which post of how many, when this is part of a thread.
                Used to say where the problem is.

        Returns:
            The post's settings, ready to send.

        Raises:
            InvalidPostError: If a setting is unknown, if the post breaks one
                of X's limits, or if a post after the first in a thread names
                its own `reply_to`.
            NotSupportedError: If the post asks for something X cannot do.
        """
        if place is not None and place[0] > 1 and post.reply_to is not None:
            message = (
                f"post {place[0]} of {place[1]} in this thread has its own "
                f"reply_to, but every post after the first replies to the "
                f"one before it, and that id does not exist until the "
                f"thread is being sent. Take reply_to off it. Only the first "
                f"post may name what the thread answers."
            )
            raise InvalidPostError(message, platform=PLATFORM_NAME)

        options = _checked_options(post.options)
        # Nothing is asked of X to know its limits, so checking every post
        # in a thread costs no requests.
        check_post(
            post,
            platform=PLATFORM_NAME,
            features=self.features,
            limits=_ALLOWED,
        )
        return options

    async def _send(
        self,
        http: HttpClient,
        connection: Connection,
        post: Post,
        options: dict[str, str],
    ) -> PostResult:
        """Upload whatever the post carries, then publish it.

        Args:
            http: A client already signed as this account.
            connection: The account to publish as.
            post: What to publish, already checked.
            options: The post's settings, already checked.

        Returns:
            What X said about the new post.

        Raises:
            PlatformError: If X answered without an id.
        """
        media_ids = [await self._upload(http, item) for item in post.media]
        reply = _inside(
            await http.json(
                "POST",
                TWEETS_PATH,
                json=_body_for(post, options, media_ids),
            )
        )

        post_id = _text(reply, "id", "publish a post")
        return PostResult(
            id=post_id,
            url=_link_to(connection, post_id),
            state=PostState.DONE,
            raw=reply,
        )

    async def _upload(self, http: HttpClient, item: Media) -> str:
        """Send one file to X and wait until it can be used.

        Say what is coming, send it a piece at a time, say that is all of
        it, and - for video - wait while X encodes it. A file with
        `Media.alt_text` on it takes one more request after that, because
        alt text is its own call here.

        Args:
            http: A client already signed as this account.
            item: The picture or video to send.

        Returns:
            X's id for the file, to name on the post.

        Raises:
            InvalidPostError: If all we have is a link to the file, or if X
                gives up on it.
            PlatformError: If X answers without an id for the file, or never
                finishes encoding it.
        """
        total = _how_big(item)

        started = await http.json(
            "POST",
            MEDIA_PATH,
            data={
                "command": "INIT",
                "total_bytes": str(total),
                "media_type": item.content_type,
                "media_category": _category_for(item),
            },
        )
        media_id = self._media_id(started)

        # `Media.piece` reads one piece off disk at a time, so a large video
        # costs one piece of memory rather than all of it.
        segment = 0
        sent = 0
        while sent < total:
            piece = item.piece(sent, self._chunk_bytes)
            await http.post(
                MEDIA_PATH,
                data={
                    "command": "APPEND",
                    "media_id": media_id,
                    "segment_index": str(segment),
                },
                # X wants the bytes as a file part, which is what makes this
                # request multipart rather than an ordinary form.
                files={"media": ("piece", piece, "application/octet-stream")},
            )
            sent += len(piece)
            segment += 1

        finished = await http.json(
            "POST",
            MEDIA_PATH,
            data={"command": "FINALIZE", "media_id": media_id},
        )

        state, progress = _the_state_of(finished)
        if state:
            # Only video comes back with anything to wait for. A picture is
            # ready the moment it is finalised and says nothing here.
            await self._wait_until_ready(http, media_id, state, progress)

        await self._describe(http, media_id, item.alt_text)
        return media_id

    async def _describe(
        self,
        http: HttpClient,
        media_id: str,
        alt_text: str | None,
    ) -> None:
        """Tell X what a file shows, for anybody using a screen reader.

        Alt text is a request of its own - the upload has nowhere to carry
        it - and it has to happen while the file is still loose. Once the
        file is named on a post X will not take a description for it, so
        this goes between the upload and the post rather than after both.

        Args:
            http: A client already signed as this account.
            media_id: The file to describe.
            alt_text: What the file shows. Nothing is sent when there is
                none, so a file with no description costs no extra request.

        Raises:
            SocialChimpError: If X refuses the description. Nothing has been
                published at this point, so the post is not half-sent.
        """
        if not alt_text:
            return

        await http.post(
            MEDIA_METADATA_PATH,
            json={"id": media_id, "metadata": {"alt_text": {"text": alt_text}}},
        )

    def _media_id(self, reply: RawData) -> str:
        """Read X's id for a file out of what it answered.

        Args:
            reply: What INIT answered.

        Returns:
            The id to name in APPEND, FINALIZE and on the post itself.

        Raises:
            PlatformError: If there is no id anywhere in the reply.
        """
        inside = _inside(reply)
        # `id` is what the endpoint answers now that it lives under /2.
        # `media_id_string` is what it answered as part of v1.1, and some
        # replies still arrive that way. Never `media_id`, which is the same
        # number and loses its last digits in anything using floats.
        for key in ("id", "media_id_string"):
            found = inside.get(key)
            if isinstance(found, str) and found:
                return found

        message = (
            "X took the start of a file upload but sent no id for the file "
            "back, so there is nothing to send the rest of it against. The "
            "whole reply is on this error."
        )
        raise PlatformError(message, platform=PLATFORM_NAME, raw=reply)

    async def _wait_until_ready(
        self,
        http: HttpClient,
        media_id: str,
        state: str,
        progress: RawData,
    ) -> None:
        """Keep asking about a video until X has finished encoding it.

        Args:
            http: A client already signed as this account.
            media_id: The file to ask about.
            state: What FINALIZE already said, so a video that was ready
                straight away costs no extra request.
            progress: The rest of what FINALIZE said about it.

        Raises:
            InvalidPostError: If X gives up on the file.
            PlatformError: If it is still not ready after all our checks.
        """
        for _ in range(self._media_checks):
            if state == _GAVE_UP:
                raise _x_gave_up_on_the_file(progress)
            if state not in _STILL_WORKING:
                return

            # X says how long to leave it, and that number is worth using:
            # it grows with the length of the video, so asking on our own
            # timer either wastes requests or gives up too early.
            asked_for = progress.get("check_after_secs")
            await _wait(
                float(asked_for)
                if isinstance(asked_for, int | float)
                else self._media_wait_seconds
            )

            state, progress = _the_state_of(
                await http.json(
                    "GET",
                    MEDIA_PATH,
                    params={"command": "STATUS", "media_id": media_id},
                )
            )

        message = (
            f"X is still encoding file {media_id} after {self._media_checks} "
            f"checks. Long videos take longer than this; raise media_checks "
            f"and try again. The file is not lost - it is on the account, "
            f"and X finishes with it whether we are watching or not."
        )
        raise PlatformError(message, platform=PLATFORM_NAME)

    async def delete_post(self, connection: Connection, post_id: str) -> None:
        """Remove a post.

        Args:
            connection: The account that published it.
            post_id: X's id for the post.

        Raises:
            NotFoundError: If there is no such post on this account.
        """
        async with self._client(connection.token.access_token) as http:
            await http.delete(f"{TWEETS_PATH}/{post_id}")

    # X can also hold a socket open (its filtered stream) and post to a URL
    # of yours (account activity). Both are separate products on paid plans
    # of their own, so a great many apps that can post cannot use either.
    # Reading mentions on a timer works on every plan that can read at all,
    # needs nothing kept running, and survives a restart with no lost
    # updates - so that is what is here.
    async def fetch_updates(
        self,
        connection: Connection,
        since: datetime | None,
    ) -> Sequence[Update]:
        """Return the mentions of this account since a moment in time.

        Args:
            connection: The account to ask about.
            since: Only return things newer than this. `None` on the first
                call, when a recent page is read instead.

        Returns:
            The mentions, oldest first.
        """
        params: dict[str, str] = {
            "max_results": str(self._updates_per_check),
            "tweet.fields": "created_at,author_id,conversation_id",
        }
        if since is not None:
            params["start_time"] = _rfc3339(since)

        async with self._client(connection.token.access_token) as http:
            reply = await http.json(
                "GET",
                f"/users/{connection.account_id}/mentions",
                params=params,
            )

        found = reply.get("data")
        items = (
            [raw for raw in found if isinstance(raw, dict)]
            if isinstance(found, list)
            else []
        )

        updates: list[Update] = []
        for raw in items:
            when = _moment(str(raw.get("created_at", "")))
            # X's `start_time` is exclusive, but dropping anything on the
            # marker as well costs nothing and means a clock that disagrees
            # by a second cannot hand the same mention on twice.
            if when is None or (since is not None and when <= since):
                continue
            updates.append(
                Update.from_network(
                    update_id=str(raw.get("id", "")),
                    kind_name="mention",
                    platform=PLATFORM_NAME,
                    connection_id=connection.id,
                    created_at=when,
                    raw=raw,
                )
            )

        # X hands back the newest first; socialchimp wants the oldest.
        updates.reverse()
        return updates


def _is_xs_own_fault(refused: SocialChimpError) -> bool:
    """Say whether a failed renewal was X having trouble rather than a bad token.

    Args:
        refused: The error a renewal raised.

    Returns:
        True for X's own failures. Those are worth trying again; they do not
        mean the person has to sign in again, and telling an app to throw a
        connection away over a bad five minutes at X is the sort of quiet
        damage this library exists to avoid.
    """
    if not isinstance(refused, PlatformError):
        return False
    return (
        refused.status_code is None
        or refused.status_code >= httpx.codes.INTERNAL_SERVER_ERROR
    )
