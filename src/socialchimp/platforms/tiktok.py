"""TikTok: the network that will happily hide everything you post.

Most of TikTok works the way you would expect: sign somebody in, send a
video, wait while TikTok gets on with it. One thing about it does not, and it
is the reason most people's first day here ends in confusion.

## Read this before you write any code

**Until TikTok has audited your app, everything it posts is private.**

An app you have just created in the developer portal is unaudited. An
unaudited app can post for up to **five people in any 24 hours**, and every
single thing it posts is forced to `SELF_ONLY` - visible to the account
owner and to nobody else. Not to their followers, not on anybody's For You
page, not to a friend they send the link to.

TikTok does this quietly. You may ask for `PUBLIC_TO_EVERYONE`, TikTok will
answer that the post succeeded, the video will appear on the person's own
screen, and it will be invisible to the rest of the world. So people spend a
day reading their own code looking for the bug. There is no bug. The app has
not been audited.

Getting audited means submitting the app for TikTok's compliance review,
which usually takes a week or two. Until it passes, treat every post as
private, and tell your users that is what is happening - because they will
look at their own profile, see the video, and assume it worked.

socialchimp says this again in the message you get when there are no app
credentials, because that is the first thing anybody hits.

## Before any of this works

There is no `create_app` here, because there is nothing to automate. A person
has to:

1. Create an app at https://developers.tiktok.com/
2. Add **Login Kit** and the **Content Posting API** to it
3. Add your redirect address to the app
4. Submit the app for **audit**, or live with everything being private

Then hand the client key and client secret to socialchimp as
`AppCredentials`. TikTok calls them the client key and client secret;
`AppCredentials.client_id` is where the client key goes.

## Signing someone in

Two steps, like Mastodon and YouTube: `start_login` gives you an address to
send the person to, and `finish_login` swaps the code they come back with for
a token.

TikTok asks for PKCE - the trick where only the hash of a secret travels to
the network - on **desktop and mobile apps**, and not on web apps. So it is
off here by default, because a server holding a client secret is a web app.
Turn it on with `TikTokPlatform(pkce=True)` and the secret comes back to you
in `SendToNetwork.remember`, ready to hand to `finish_login`.

**TikTok writes the hash as hex**, where every other network writes it as
base64. Sending base64 gets you a refusal that does not say why.

## Tokens, and the one that destroys itself

An access token lasts 24 hours. A refresh token lasts 365 days. And **using
a refresh token replaces it** - the reply carries a new one, and the old one
stops working the moment it is used.

That is exactly the case socialchimp's token code is built for, so `refresh`
hands back both halves and the old refresh token is never seen again. Save
what it gives you. Keeping the old one, the way Google's platform has to,
would lock the person out at the next renewal.

## Two ways to post, and the difference matters

    Post(
        text="What the video is about",
        media=(Media.from_file("clip.mp4"),),
        options={"send_to": "profile"},
    )

- **`send_to="drafts"`** is the default. The video goes to the person's
  TikTok inbox as a draft, and they finish it and post it themselves in the
  app. It needs only the `video.upload` permission, and nothing ever reaches
  anybody's profile without a person tapping a button. **TikTok's inbox
  takes no caption**, so a post headed for the drafts must have no text - a
  caption with nowhere to go is refused here rather than quietly dropped.

- **`send_to="profile"`** posts straight to the person's profile. It needs
  the `video.publish` permission, which is a separate thing to ask TikTok
  for, and it carries the caption and all the settings below.

The default is the drafts because it is the one that cannot surprise
anybody, and because it works for every app - `video.publish` is the harder
of the two permissions to be granted.

`Post.options` for a post headed to the profile:

    options={
        "send_to": "profile",
        "privacy_level": "SELF_ONLY",   # or PUBLIC_TO_EVERYONE, and see above
        "disable_comment": False,
        "disable_duet": False,
        "disable_stitch": False,
        "video_cover_timestamp_ms": 1000,
        "brand_content_toggle": False,  # a paid partnership
        "brand_organic_toggle": False,  # promoting your own business
    }

**A video with no `privacy_level` goes up as `SELF_ONLY`.** Putting
somebody's video in front of the world by accident cannot be undone, so the
quiet default is the careful one.

## The video goes up in pieces

A TikTok can be four gigabytes. socialchimp reads the file a piece at a time
so a big video does not become a big pile of memory, and sends the pieces in
order.

TikTok's arithmetic here catches people out. A piece must be **at least
5 MB and at most 64 MB**, and the number of pieces is the file size divided
by the piece size **rounded down**, not up. So a 12 MB video sent in 10 MB
pieces is *one* piece of 12 MB, not two: the leftover rides along on the last
piece rather than becoming a piece of its own, which would be under the 5 MB
floor and refused. Anything under 5 MB goes whole, in one piece.

## Publishing finishes later

Taking the bytes is not publishing. TikTok encodes and moderates afterwards,
so `publish()` never hands back `PostState.DONE`. Which state it does hand
back depends on where the post was going:

- A post headed for the **profile** comes back `PostState.PROCESSING`.
  TikTok is still working, and it will be live or failed before long.
- A post headed for the **drafts** comes back
  `PostState.WAITING_FOR_PERSON`. As far as the network is concerned the
  waiting is over: the video is theirs now, and nothing else happens until
  they open the app. Do not sit checking this one.

There are two ways to find out what happened to a post that is still moving:

- `check_state()` asks. It is allowed 30 times a minute per person.
- TikTok pushes the answer to a URL of yours, which is what
  `Feature.PUSH_UPDATES`, `check_signature` and `read_update` are for. The
  four it sends are `post.publish.complete`,
  `post.publish.publicly_available`, `post.publish.inbox_delivered` - which
  arrives as `UpdateKind.POST_DRAFTED` - and `post.publish.failed`.

**A pushed message can arrive more than once.** TikTok promises to deliver at
least once and keeps retrying for 72 hours, so duplicates are normal rather
than a fault. Give `Dispatcher` a `SeenUpdates` - see `socialchimp.events` -
and the second copy is thrown away for you.

## Captions are counted the way Java counts

The limit is 2,200, and it is not 2,200 characters. TikTok counts UTF-16
units, where an emoji is two, so a caption of 1,101 thumbs-up is 1,101
characters to Python and 2,202 to TikTok. socialchimp counts the same way
TikTok does, so a caption is refused here for the reason TikTok would have
refused it.

## What is not here

- **No scheduling.** `Feature.SCHEDULE` is off. TikTok's API has no way to
  say "publish this on Friday", and pretending otherwise would mean posts
  quietly going out now.
- **No text-only post.** `Feature.POST_TEXT` is off. Everything on TikTok is
  a video; a post with no video is refused rather than turned into something
  else.
- **No photo carousels yet.** TikTok can post up to 35 pictures as a
  carousel, through `POST /v2/post/publish/content/init/` - but that endpoint
  only **fetches** pictures from public web addresses, and needs the domain
  proved to be yours first. It is a different way of moving a file, not
  another branch of this one, so `Feature.POST_IMAGE` is off and a post with
  pictures is refused with a message saying where it would go. When it is
  written it belongs beside `_start_upload`.
- **No deleting.** TikTok has no call for removing a post it published.
- **No reading posts back on a timer.** TikTok pushes what socialchimp needs
  to know, so there is nothing to ask for.
"""

from __future__ import annotations

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
from socialchimp.events import (
    DEFAULT_ALLOWED_AGE_SECONDS,
    Update,
    check_not_too_old,
)
from socialchimp.features import Feature, Limits, TextCount, check_post
from socialchimp.http import (
    HttpClient,
    error_from_response,
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
    from collections.abc import Callable, Mapping

    from socialchimp.errors import SocialChimpError
    from socialchimp.http import Retries
    from socialchimp.models import AppCredentials

__all__ = ["TikTokPlatform", "tiktok_errors"]

PLATFORM_NAME: Final = "tiktok"

API_URL: Final = "https://open.tiktokapis.com/v2"
"""Where everything except the sign-in page and the upload itself is sent."""

SIGN_IN_URL: Final = "https://www.tiktok.com/v2/auth/authorize/"
"""The page a person approves your app on. On tiktok.com, not on the API."""

# Where a code, or a refresh token, is swapped for an access token. The
# linter reads any name ending in TOKEN as a secret; this one is a public
# address, the same for every app in the world.
TOKEN_URL: Final = "https://open.tiktokapis.com/v2/oauth/token/"  # noqa: S105

PORTAL_URL: Final = "https://developers.tiktok.com/"
"""Where a person creates the app, and later submits it for audit."""

SIGNATURE_HEADER: Final = "TikTok-Signature"
"""The header TikTok signs a pushed message with."""

DEFAULT_SCOPES: Final = ("user.info.basic", "video.upload", "video.publish")
"""Enough to know who somebody is and to post for them.

- `user.info.basic` - their name and open id, so a connection has something
  a person would recognise on it.
- `video.upload` - put a video in their drafts for them to finish.
- `video.publish` - put a video straight on their profile.

Ask for fewer if your app only ever fills the drafts: leaving out
`video.publish` is the difference between a permission TikTok grants readily
and one it looks at harder.
"""

TO_DRAFTS: Final = "drafts"
"""`send_to` for a video the person finishes in the TikTok app themselves."""

TO_PROFILE: Final = "profile"
"""`send_to` for a video that goes straight onto the person's profile."""

SEND_TO_CHOICES: Final = (TO_DRAFTS, TO_PROFILE)
"""Where a post can be sent. Anything else is refused."""

PRIVACY_LEVELS: Final = (
    "SELF_ONLY",
    "MUTUAL_FOLLOW_FRIENDS",
    "FOLLOWER_OF_CREATOR",
    "PUBLIC_TO_EVERYONE",
)
"""Who can see a video.

Which of these a particular person may use depends on whether their account
is public or private, and an unaudited app gets `SELF_ONLY` whatever it asks
for. TikTok says no to a level the person cannot use, and that comes back as
an `InvalidPostError`.
"""

POST_OPTIONS: Final = (
    "send_to",
    "privacy_level",
    "disable_comment",
    "disable_duet",
    "disable_stitch",
    "video_cover_timestamp_ms",
    "brand_content_toggle",
    "brand_organic_toggle",
)
"""The settings `Post.options` accepts here. Anything else is refused."""

SWITCHES: Final = (
    "disable_comment",
    "disable_duet",
    "disable_stitch",
    "brand_content_toggle",
    "brand_organic_toggle",
)
"""The settings that are simply true or false."""

MAX_CAPTION_UNITS: Final = 2200
"""How long a caption may be, counted the way Java counts - an emoji is two."""

MIN_CHUNK_BYTES: Final = 5 * 1024**2
"""The smallest piece TikTok takes, except when the whole file is smaller."""

MAX_CHUNK_BYTES: Final = 64 * 1024**2
"""The largest piece TikTok takes."""

MAX_CHUNKS: Final = 1000
"""The most pieces one video may be sent in."""

MAX_VIDEO_BYTES: Final = 4 * 1024**3
"""The largest video TikTok takes - four gigabytes."""

DEFAULT_CHUNK_BYTES: Final = 10 * 1024**2
"""How much of a video to send at a time.

Ten megabytes: comfortably over TikTok's five megabyte floor, and small
enough that a dropped connection costs seconds rather than minutes.
"""

MAX_IMAGES_PER_CAROUSEL: Final = 35
"""How many pictures a photo carousel can hold, for the message that says so."""

CAROUSEL_ENDPOINT: Final = "/v2/post/publish/content/init/"
"""Where photo carousels would go, for the message that says they do not."""

OAUTH_REFUSALS: Final = (
    "invalid_client",
    "invalid_grant",
    "invalid_request",
    "unauthorized_client",
    "unsupported_grant_type",
)
"""What the token endpoint calls its refusals.

It writes these the plain OAuth way - a word under `error` rather than an
object - and often answers 200 while doing it, so they are read from the
body rather than from the status.
"""

VIDEO_TYPES: Final = ("video/mp4", "video/quicktime", "video/webm")
"""The only three kinds of file TikTok will take."""

MAX_VIDEOS_PER_POST: Final = 1
"""One video per post. There is no such thing as a TikTok with two."""

INIT_PER_MINUTE: Final = 6
"""How many videos one person may start in a minute."""

STATUS_CHECKS_PER_MINUTE: Final = 30
"""How many times one person's posts may be asked about in a minute."""

POSTS_PER_CREATOR_PER_DAY: Final = 15
"""Roughly how many posts a creator may receive in a day, across every app."""

# Long enough that nobody can guess one, short enough to sit in a URL. PKCE
# asks for a secret of 43 to 128 characters, and 48 random bytes written the
# URL-safe way comes to 64 of them.
_STATE_BYTES: Final = 24
_VERIFIER_BYTES: Final = 48

# A token TikTok did not put a lifetime on. Its own tokens last a day.
_A_DAY_IN_SECONDS: Final = 86400

# What TikTok says about a post, and what we call it. Anything not listed
# here is treated as "still working on it", because a status we have never
# heard of is far more likely to be a new step in the middle than a new kind
# of failure.
#
# `SEND_TO_USER_INBOX` is the interesting one: TikTok has finished its part,
# and the video is sitting in the person's drafts waiting for them to open
# the app. That is `WAITING_FOR_PERSON` rather than `PROCESSING`, because
# nothing more happens on its own and an app told "processing" would sit
# checking forever for a change only a person can make.
_OUR_STATE_FOR: Final = {
    "PROCESSING_UPLOAD": PostState.PROCESSING,
    "PROCESSING_DOWNLOAD": PostState.PROCESSING,
    "SEND_TO_USER_INBOX": PostState.WAITING_FOR_PERSON,
    "PUBLISH_COMPLETE": PostState.DONE,
    "FAILED": PostState.FAILED,
}

# What TikTok calls a pushed message, and what socialchimp calls it. An
# event that is not here keeps TikTok's own word, so an app listening for
# everything still learns what happened.
_OUR_WORD_FOR: Final = {
    "post.publish.complete": "post_published",
    "post.publish.publicly_available": "post_published",
    "post.publish.failed": "post_failed",
    "post.publish.inbox_delivered": "post_drafted",
    "authorization.removed": "connection_revoked",
}


def _text(reply: RawData, key: str, when: str) -> str:
    """Read a value TikTok always sends, and complain plainly if it did not.

    Args:
        reply: What TikTok answered.
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
        f"TikTok left {key!r} out of its reply when we asked it to {when}. "
        f"That should not happen. The whole reply is on this error."
    )
    raise PlatformError(message, platform=PLATFORM_NAME, raw=reply)


def _data_in(reply: RawData) -> RawData:
    """Pull the useful half out of one of TikTok's replies.

    Every reply is the same shape: what you asked for under `data`, and how
    it went under `error`.

    Args:
        reply: What TikTok answered.

    Returns:
        The `data` object, or an empty one when there was none.
    """
    found = reply.get("data")
    return found if isinstance(found, dict) else {}


def _refusal_in(reply: RawData) -> tuple[str, str]:
    """Read TikTok's own word for what went wrong out of a reply.

    TikTok writes this two ways. Most of its API puts an object under
    `error` with a `code` inside, where `"ok"` means it was happy. Its token
    endpoint instead puts a word straight under `error`, with the
    explanation under `error_description` - and answers 200 while doing it,
    so a platform that only reads the status never notices.

    Args:
        reply: What TikTok answered, already read into a dictionary.

    Returns:
        TikTok's code for the trouble and its own sentence about it, or two
        empty strings when it was happy.
    """
    problem = reply.get("error")

    if isinstance(problem, str):
        said = reply.get("error_description")
        return problem, said if isinstance(said, str) else ""

    if isinstance(problem, dict):
        code = problem.get("code")
        said = problem.get("message")
        if isinstance(code, str) and code and code != "ok":
            return code, said if isinstance(said, str) else ""

    return "", ""


def _said(explained: str) -> str:
    """Turn TikTok's own sentence into something to append to ours.

    Args:
        explained: What TikTok said, which may be nothing.

    Returns:
        `" It said: ..."`, or an empty string when it said nothing.
    """
    return f" It said: {explained}" if explained else ""


def tiktok_errors(response: httpx.Response) -> SocialChimpError:
    """Turn an unhappy reply from TikTok into a socialchimp error.

    TikTok's status codes are not enough on their own, so this reads the
    code it puts in the body. The ones worth naming:

    - **unaudited_client_can_only_post_to_private_accounts** is the one this
      whole file warns about. It arrives as a 403, and the message spells out
      what an audit is and why the post would have been invisible anyway.
    - **spam_risk_too_many_posts** is the creator's own daily cap, about 15
      posts across every app they use - not a request to slow down.
    - **spam_risk_too_many_pending_share** is the five-drafts-at-once cap.
    - **rate_limit_exceeded** really is a request to slow down.
    - **scope_not_authorized** is a permission that was never granted, which
      for posting is almost always `video.publish`.
    - **invalid_param** is a problem with the post rather than a mystery.

    Everything else falls through to the shared mapping: 401 is an
    `AuthError`, 403 a `NotAllowedError`, 404 a `NotFoundError`, 429 a
    `RateLimitError`.

    Args:
        response: The reply to turn into an error.

    Returns:
        The error to raise.
    """
    body = read_body(response)
    code, explained = _refusal_in(body)
    said = _said(explained)

    if code == "unaudited_client_can_only_post_to_private_accounts":
        message = (
            f"TikTok will not let this app post to a public account, because "
            f"the app has not been audited yet "
            f"(unaudited_client_can_only_post_to_private_accounts). Until it "
            f"is, it may post for at most five people a day and everything "
            f"it posts is forced to SELF_ONLY - visible to the account owner "
            f"and to nobody else, whatever privacy_level you asked for. "
            f"Either set privacy_level='SELF_ONLY' and tell your users their "
            f"video will be private, or submit the app for audit at "
            f"{PORTAL_URL}.{said}"
        )
        return NotAllowedError(message, platform=PLATFORM_NAME, raw=body)

    if code == "spam_risk_too_many_posts":
        message = (
            f"This creator has had as many posts today as TikTok allows "
            f"(spam_risk_too_many_posts). The cap is about "
            f"{POSTS_PER_CREATOR_PER_DAY} a day and it belongs to the "
            f"creator, not to your app - every app they use spends from the "
            f"same allowance, so this can happen on your first post of the "
            f"day. It is a daily allowance rather than a request to slow "
            f"down: waiting a few seconds changes nothing, and tomorrow "
            f"does.{said}"
        )
        # No `retry_after`: there is nothing to wait a handful of seconds
        # for, and putting a number here would have callers retrying all day.
        return RateLimitError(message, platform=PLATFORM_NAME, raw=body)

    if code == "spam_risk_too_many_pending_share":
        message = (
            f"This creator already has five videos waiting in their TikTok "
            f"drafts from apps (spam_risk_too_many_pending_share). TikTok "
            f"allows five unfinished ones in any 24 hours. They have to open "
            f"the app and finish or discard some before another will fit."
            f"{said}"
        )
        return RateLimitError(message, platform=PLATFORM_NAME, raw=body)

    if code == "spam_risk_user_banned_from_posting":
        message = (
            f"TikTok has stopped this creator posting "
            f"(spam_risk_user_banned_from_posting). Nothing your app does "
            f"will change that; it is between them and TikTok.{said}"
        )
        return NotAllowedError(message, platform=PLATFORM_NAME, raw=body)

    if code in ("access_token_invalid", "access_token_expired"):
        message = (
            f"TikTok would not accept our sign-in ({code}). The token has run "
            f"out or been taken away; renewing it, or asking the person to "
            f"connect their account again, is what fixes it.{said}"
        )
        return AuthError(message, platform=PLATFORM_NAME, raw=body)

    if code in ("scope_not_authorized", "scope_permission_missed"):
        message = (
            f"This account never granted the permission that would allow "
            f"that ({code}). Posting straight to a profile needs "
            f"video.publish; putting a video in the drafts needs only "
            f"video.upload. Ask for the one you need when the person "
            f"connects their account, or use options={{'send_to': "
            f"'{TO_DRAFTS}'}}.{said}"
        )
        return NotAllowedError(message, platform=PLATFORM_NAME, raw=body)

    if code == "rate_limit_exceeded":
        message = (
            f"TikTok is asking us to slow down (rate_limit_exceeded). One "
            f"person may start {INIT_PER_MINUTE} posts a minute and ask "
            f"about {STATUS_CHECKS_PER_MINUTE} of them a minute. Waiting and "
            f"trying again is the right move here.{said}"
        )
        return RateLimitError(
            message,
            retry_after=retry_after_seconds(response),
            platform=PLATFORM_NAME,
            raw=body,
        )

    if code == "url_ownership_unverified":
        message = (
            f"TikTok will not fetch from that web address until you have "
            f"proved the domain is yours (url_ownership_unverified). Prove "
            f"it at {PORTAL_URL}, under your app's URL properties.{said}"
        )
        return NotAllowedError(message, platform=PLATFORM_NAME, raw=body)

    if code in ("invalid_param", "privacy_level_option_mismatch"):
        message = (
            f"TikTok would not accept this post ({code}). Something in it is "
            f"not something TikTok will take - most often a privacy_level "
            f"this creator is not allowed to use, or a file it cannot "
            f"read.{said}"
        )
        return InvalidPostError(message, platform=PLATFORM_NAME, raw=body)

    if code in OAUTH_REFUSALS:
        message = (
            f"TikTok would not take this sign-in ({code}). Either the code or "
            f"the refresh token has been used already or gone stale - both "
            f"are one-use - or the client key and secret are not the ones "
            f"this app was given. The person has to sign in again.{said}"
        )
        return AuthError(message, platform=PLATFORM_NAME, raw=body)

    if code == "publish_id_not_found":
        message = (
            f"TikTok has no post with that publish id "
            f"(publish_id_not_found). A publish id belongs to the account "
            f"that made it, so this can also mean it belongs to a different "
            f"connection than the one being asked with.{said}"
        )
        return NotFoundError(message, platform=PLATFORM_NAME, raw=body)

    return error_from_response(response, platform=PLATFORM_NAME)


def _app_or_refuse(app: AppCredentials | None, what: str) -> AppCredentials:
    """Insist on your app's credentials before going any further.

    Args:
        app: The credentials that arrived, which may be none at all.
        what: The thing we were trying to do, for the message.

    Returns:
        The credentials.

    Raises:
        ConfigError: If there are none, saying where to get some - and
            warning about the audit, because that is the next thing to trip
            over after this one.
    """
    if app is None:
        raise ConfigError(_no_credentials(what))
    return app


def _no_credentials(what: str) -> str:
    """Say what to do when there are no credentials to work with.

    Args:
        what: The thing we were trying to do, for the first sentence.

    Returns:
        A message naming every step somebody has to take by hand, including
        the audit - which is not needed to make this error go away, but is
        needed before anything posted is visible to anybody.
    """
    return (
        f"TikTok needs your app's client key and client secret to {what}, and "
        f"none were given. socialchimp cannot make them for you: somebody has "
        f"to create an app at {PORTAL_URL}, add Login Kit and the Content "
        f"Posting API to it, and add your redirect address. Save what the "
        f"portal gives you with Storage.save_app - the client key goes in "
        f"client_id - and socialchimp hands them to every sign-in and every "
        f"renewal.\n\n"
        f"Then read this bit, because it is the one that wastes people's "
        f"day: until TikTok has audited your app it may post for at most 5 "
        f"people in any 24 hours, and everything it posts is forced to "
        f"SELF_ONLY - private to the account owner, whatever privacy_level "
        f"you ask for. Posting will look like it worked. Submit the app for "
        f"audit at {PORTAL_URL} before you promise anybody a public video."
    )


def _challenge_for(verifier: str) -> str:
    """Hash the secret we keep, so only the hash travels to TikTok.

    Args:
        verifier: The secret made at the start of a login.

    Returns:
        The hash written as hex. Every other network in this library wants
        it as base64; TikTok is the exception, and sending base64 here gets
        a refusal that does not say why.
    """
    return hashlib.sha256(verifier.encode()).hexdigest()


def _check_state(request: LoginRequest, callback: Mapping[str, str]) -> None:
    """Check the value that came back is the one we sent.

    This is what stops somebody handing your app a login they started
    themselves and having it saved against one of your users.

    Args:
        request: The request used to start the login.
        callback: The query values TikTok sent back.

    Raises:
        AuthError: If both sides have a state and they are different.
    """
    returned = callback.get("state", "")
    if request.state is not None and returned and returned != request.state:
        message = (
            "The state TikTok sent back did not match the one we sent. This "
            "login did not start here, so nothing has been saved. Start a "
            "new one."
        )
        raise AuthError(message, platform=PLATFORM_NAME)


def _code_from(callback: Mapping[str, str]) -> str:
    """Pull the login code out of what TikTok sent back.

    Args:
        callback: The query values TikTok sent back.

    Returns:
        The code to swap for a token.

    Raises:
        AuthError: If the person said no, or if there is no code.
    """
    refused = callback.get("error")
    if refused:
        detail = _said(callback.get("error_description", ""))
        message = (
            f"TikTok did not sign this person in ({refused}). Usually they "
            f"pressed cancel on the approval page.{detail}"
        )
        raise AuthError(message, platform=PLATFORM_NAME)

    code = callback.get("code")
    if not code:
        message = (
            "TikTok sent no code back, so there is nothing to swap for a "
            "token. Check you are passing the whole query string from your "
            "redirect address."
        )
        raise AuthError(message, platform=PLATFORM_NAME)
    return code


def _right_now() -> datetime:
    """Return the current moment.

    Its own function so that a platform can be handed a different one, which
    is what makes the age check on a pushed message testable without the
    test depending on how long it took to run.

    Returns:
        Now, with a timezone.
    """
    return datetime.now(UTC)


def _expiry_from(reply: RawData, now: datetime) -> datetime:
    """Work out when an access token stops working.

    Args:
        reply: What TikTok's token endpoint answered.
        now: What to treat as the current moment.

    Returns:
        The moment it runs out. TikTok's access tokens last a day, and that
        is what is assumed when it does not say.
    """
    seconds = reply.get("expires_in")
    lasts = seconds if isinstance(seconds, int) else _A_DAY_IN_SECONDS
    return now + timedelta(seconds=lasts)


def _chunk_plan(total: int, wanted: int) -> tuple[int, int]:
    """Work out how big each piece of a video is, and how many there are.

    This is the arithmetic people get wrong. TikTok's rule is that the
    number of pieces is the file size divided by the piece size **rounded
    down**, and the leftover rides along on the last piece. Rounding up
    instead makes a final piece under TikTok's five megabyte floor, which it
    refuses - halfway through the upload, not at the start.

    Args:
        total: How many bytes of video there are.
        wanted: How big we would like each piece to be, already known to be
            between TikTok's five and sixty-four megabytes.

    Returns:
        The size to tell TikTok each piece is, and how many pieces there
        are. The last piece is whatever is left, which can be nearly twice
        the size given here - TikTok allows that, up to 128 MB, and this
        arithmetic cannot produce one bigger than two pieces less a byte.
    """
    # Under the floor there is nothing to divide: TikTok wants the whole
    # file as a single piece the size of the file.
    if total < MIN_CHUNK_BYTES:
        return total, 1

    size = min(wanted, total)
    return size, total // size


def _how_big(video: Media) -> int:
    """Find out how many bytes a video is, without reading any of them.

    Nothing here ever holds the whole file. `Media.size` reads the size off
    disk, and `Media.piece` hands out one piece at a time, so a four
    gigabyte video costs one piece of memory rather than four gigabytes.

    Args:
        video: The video attached to the post.

    Returns:
        How many bytes there are altogether.

    Raises:
        InvalidPostError: If all we have is a link to the file, or if the
            file is bigger than TikTok takes.
    """
    total = video.size
    if total is None:
        message = (
            f"TikTok will not fetch {video.url!r} for you - not for a video "
            f"sent this way. Download the file first, then use "
            f"Media.from_file, which reads it off disk a piece at a time "
            f"rather than holding all of it in memory."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME)

    if total > MAX_VIDEO_BYTES:
        message = (
            f"This video is {total:,} bytes and TikTok takes at most "
            f"{MAX_VIDEO_BYTES:,} - four gigabytes. Nothing has been sent. "
            f"Shorten it or encode it smaller."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME)
    return total


def _the_video(post: Post) -> Media:
    """Find the one video a TikTok post is made of.

    Args:
        post: The post about to be sent.

    Returns:
        The video to upload.

    Raises:
        NotSupportedError: If there is no video. This is not a gap in
            socialchimp - TikTok has no text-only post to fall back to.
        InvalidPostError: If TikTok will not take this kind of file.
    """
    # By the time this runs, the carousel check has already turned away
    # pictures and `check_post` any post carrying more than one video. So
    # what is left is either exactly one video or nothing at all.
    if not post.media:
        what = (
            "text-only posts. Everything on TikTok is a video, so attach one "
            "with Media.from_file('clip.mp4')"
        )
        raise NotSupportedError(platform=PLATFORM_NAME, what=what)

    video = post.media[0]
    if video.content_type not in VIDEO_TYPES:
        message = (
            f"TikTok will not take a {video.content_type} file. It reads "
            f"three kinds: {', '.join(VIDEO_TYPES)} - so an .mp4, a .mov or "
            f"a .webm. Convert the file, or pass a filename that says which "
            f"of those it really is."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME)
    return video


def _no_carousels(post: Post) -> None:
    """Turn away a post with pictures on it, and say where they would go.

    This runs before the shared checks rather than after, which is unusual.
    `check_post` would refuse the same post, correctly, with "tiktok does
    not support posting pictures" - and somebody who has just attached
    twelve holiday photos deserves to be told that TikTok does have
    carousels, and why this is not one.

    Args:
        post: The post about to be sent.

    Raises:
        NotSupportedError: If any of the attachments is a picture.
    """
    if not any(item.kind is MediaKind.IMAGE for item in post.media):
        return

    what = (
        f"posting pictures yet. TikTok does have photo carousels - up to "
        f"{MAX_IMAGES_PER_CAROUSEL} pictures in one post - but they go "
        f"through a different call ({CAROUSEL_ENDPOINT}) that makes TikTok "
        f"fetch each picture from a public web address rather than taking an "
        f"upload, and that address has to be on a domain you have proved is "
        f"yours. That is a different way of moving a file rather than "
        f"another setting, and socialchimp does not do it yet. Post a video "
        f"instead, or put the carousel up in the TikTok app"
    )
    raise NotSupportedError(platform=PLATFORM_NAME, what=what)


def _checked_options(options: RawData) -> None:
    """Refuse a setting TikTok has never heard of, before anything is sent.

    Args:
        options: What was put in `Post.options`.

    Raises:
        InvalidPostError: If a setting is not one of ours. A typo costs no
            request and no part of the minute's allowance.
    """
    for key in options:
        if key not in POST_OPTIONS:
            message = (
                f"TikTok does not know the post option {key!r}. It accepts: "
                f"{', '.join(POST_OPTIONS)}."
            )
            raise InvalidPostError(message, platform=PLATFORM_NAME)


def _where_to_send(options: RawData) -> str:
    """Work out whether this post goes to the drafts or to the profile.

    Args:
        options: What was put in `Post.options`.

    Returns:
        One of `SEND_TO_CHOICES`.

    Raises:
        InvalidPostError: If it is something else.
    """
    # Nothing said means the drafts. It needs the lesser permission, and
    # nothing reaches a profile without the person tapping a button.
    asked = options.get("send_to", TO_DRAFTS)
    if asked not in SEND_TO_CHOICES:
        message = (
            f"send_to is {asked!r}, which is not somewhere TikTok can send a "
            f"post. It takes {TO_DRAFTS!r}, which puts the video in the "
            f"person's TikTok drafts for them to finish, and {TO_PROFILE!r}, "
            f"which posts it straight to their profile and needs the "
            f"video.publish permission."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME)
    return str(asked)


def _checked_switch(options: RawData, key: str) -> bool | None:
    """Read a setting that has to be true or false, if it is there at all.

    Args:
        options: What was put in `Post.options`.
        key: Which setting to read.

    Returns:
        The answer, or `None` when the setting was left out.

    Raises:
        InvalidPostError: If it is there but is not true or false.
    """
    if key not in options:
        return None

    value = options[key]
    if not isinstance(value, bool):
        message = f"{key} is {value!r}, but it has to be True or False."
        raise InvalidPostError(message, platform=PLATFORM_NAME)
    return value


def _checked_cover(options: RawData) -> int | None:
    """Read which moment of the video should be its cover picture.

    Args:
        options: What was put in `Post.options`.

    Returns:
        The moment in milliseconds, or `None` when it was left out and
        TikTok should use the first frame.

    Raises:
        InvalidPostError: If it is not a whole number of milliseconds.
    """
    if "video_cover_timestamp_ms" not in options:
        return None

    value = options["video_cover_timestamp_ms"]
    # `True` is an int in Python and would sail straight through an
    # isinstance check, then be sent to TikTok as a cover time of 1ms.
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        message = (
            f"video_cover_timestamp_ms is {value!r}, but it has to be a whole "
            f"number of milliseconds into the video: 1500 for a second and a "
            f"half. Leave it out and TikTok uses the first frame."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME)
    return value


def _checked_privacy(options: RawData) -> str:
    """Work out who should be able to see this video.

    Args:
        options: What was put in `Post.options`.

    Returns:
        The privacy level to send.

    Raises:
        InvalidPostError: If it is not one TikTok knows.
    """
    # Nothing said means private. Putting somebody's video in front of the
    # world by accident cannot be undone, so the quiet answer is the
    # careful one - and it is the only one an unaudited app gets anyway.
    asked = options.get("privacy_level", "SELF_ONLY")
    if asked not in PRIVACY_LEVELS:
        message = (
            f"privacy_level is {asked!r}, which TikTok does not know. It "
            f"accepts: {', '.join(PRIVACY_LEVELS)}. Which of them a "
            f"particular person may use depends on whether their account is "
            f"public or private, and an app TikTok has not audited gets "
            f"SELF_ONLY whatever it asks for."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME)
    return str(asked)


def _post_info_for(post: Post) -> RawData:
    """Build what TikTok is told about a video going onto a profile.

    Args:
        post: The post about to be sent.

    Returns:
        The `post_info` object. Only settings that were actually given are
        in it, so TikTok's own defaults apply to the rest.

    Raises:
        InvalidPostError: If any of the settings is wrong.
    """
    options = post.options
    # TikTok calls the caption the `title`. There is no second field: what
    # people see under a video is this, and it is `Post.text` here.
    info: RawData = {
        "title": post.text,
        "privacy_level": _checked_privacy(options),
    }

    for key in SWITCHES:
        switched = _checked_switch(options, key)
        if switched is not None:
            info[key] = switched

    cover = _checked_cover(options)
    if cover is not None:
        info["video_cover_timestamp_ms"] = cover

    return info


def _no_caption_in_the_drafts(post: Post) -> None:
    """Refuse a caption on a post headed for somebody's drafts.

    Args:
        post: The post about to be sent.

    Raises:
        InvalidPostError: If there is any text. TikTok's inbox call takes
            the file and nothing else, so a caption sent this way would
            simply vanish - and a caption that quietly disappears is worse
            than one that is refused with a way out.
    """
    if not post.text:
        return

    message = (
        f"This post has a caption, but it is headed for the person's TikTok "
        f"drafts, and TikTok's drafts take the video and nothing else - they "
        f"write the caption themselves in the app. Rather than drop your "
        f"words quietly, socialchimp is refusing. Either clear Post.text, or "
        f"post straight to the profile with "
        f"options={{'send_to': '{TO_PROFILE}'}}, which carries the caption "
        f"and needs the video.publish permission."
    )
    raise InvalidPostError(message, platform=PLATFORM_NAME)


def _no_settings_in_the_drafts(options: RawData) -> None:
    """Refuse settings that only mean something on a profile post.

    Args:
        options: What was put in `Post.options`.

    Raises:
        InvalidPostError: If any of them is there. TikTok's inbox call takes
            none of these, so sending them would change nothing and the
            person would find out by looking at the video.
    """
    ignored = sorted(key for key in options if key != "send_to")
    if not ignored:
        return

    message = (
        f"{', '.join(ignored)} only mean something on a post going "
        f"straight to a profile, and this one is going to the person's "
        f"drafts, where TikTok takes the video and nothing else - they "
        f"choose all of that themselves in the app. Take these out, or add "
        f"options={{'send_to': '{TO_PROFILE}'}}."
    )
    raise InvalidPostError(message, platform=PLATFORM_NAME)


def _watch_url(username: str, post_id: str) -> str:
    """Return the address a person would use to watch a post.

    Args:
        username: The creator's `@` name, without the `@`.
        post_id: TikTok's id for the published video.

    Returns:
        The address.
    """
    return f"https://www.tiktok.com/@{username}/video/{post_id}"


def _live_post_id(said: RawData) -> str | None:
    """Read the id of the video TikTok has actually put up, if it has.

    TikTok only fills this in once a post has been through moderation, and
    it hands back a list because one call can produce several posts.

    Args:
        said: The `data` from a status check.

    Returns:
        The first id, as text, or `None` when there is not one yet.
    """
    # `publicaly_available_post_id` is TikTok's own spelling, typo and all.
    # Correcting it here would simply read a field that does not exist.
    found = said.get("publicaly_available_post_id")
    if isinstance(found, list) and found:
        return str(found[0])
    return None


def _username_of(connection: Connection) -> str | None:
    """Work out a connection's `@` name, when a login wrote one down.

    Args:
        connection: The account to look at.

    Returns:
        The name without its `@`, or `None` for a connection saved before
        the name was being kept.
    """
    saved = connection.extra.get("username")
    return saved if isinstance(saved, str) and saved else None


def _signature_parts(sent: str) -> tuple[str, str]:
    """Split TikTok's signature header into the time and the signature.

    It arrives as `t=1633174587,s=1849...`. Both halves matter: the time is
    part of what was signed, so it cannot be changed without breaking the
    signature, and it is what lets an old copied request be turned away.

    Args:
        sent: The header's value.

    Returns:
        The time as TikTok wrote it, and the signature.

    Raises:
        SignatureError: If it is not in that shape.
    """
    found: dict[str, str] = {}
    for piece in sent.split(","):
        name, _, value = piece.partition("=")
        found[name.strip()] = value.strip()

    when = found.get("t")
    signature = found.get("s")
    if not when or not signature:
        message = (
            f"The {SIGNATURE_HEADER} header is not in the shape TikTok sends "
            f"- `t=<time>,s=<signature>`. Refusing it."
        )
        raise SignatureError(message)
    return when, signature


def _content_in(sent: RawData) -> object:
    """Unpack the half of a pushed message TikTok wraps in a string.

    TikTok puts the interesting part - the publish id, the reason something
    failed - into `content` as a *string* of JSON rather than as an object.
    Anyone reading the message without unpacking that finds a long piece of
    text where they expected fields, which is the thing that catches people
    out here.

    Args:
        sent: The whole message, already read from JSON.

    Returns:
        The contents as an object where it can be read that way, and exactly
        as it arrived where it cannot.
    """
    inside = sent.get("content")
    if not isinstance(inside, str):
        return inside

    try:
        return json.loads(inside)
    except ValueError:
        # TikTok said something we cannot read. Handing it back untouched
        # beats throwing away the only copy of it.
        return inside


class TikTokPlatform:
    """Everything socialchimp does with TikTok.

    Signing people in, keeping their tokens working, sending a video up in
    pieces, asking what happened to it afterwards, and reading the messages
    TikTok pushes when it finishes.

        tiktok = TikTokPlatform()

    It holds nothing belonging to one account and nothing belonging to your
    app. Your client key and secret arrive as an argument every time they
    are needed - on the `LoginRequest` for a sign-in, and on `refresh` for a
    renewal - so one of these serves every account and every app.

    **Everything an unaudited app posts is private.** See this module's
    documentation; it is the first thing to know about TikTok and the last
    thing anybody works out on their own.

    Attributes:
        name: `"tiktok"`.
        features: What TikTok can do. Notably `Feature.POST_TEXT` and
            `Feature.SCHEDULE` are missing, because TikTok has neither.
    """

    name: str = PLATFORM_NAME

    features: Feature = Feature.POST_VIDEO | Feature.PUSH_UPDATES

    def __init__(
        self,
        *,
        timeout: float = 300.0,
        retries: Retries | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        pkce: bool = False,
        allowed_age_seconds: float = DEFAULT_ALLOWED_AGE_SECONDS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """Set TikTok up for one app.

        Args:
            timeout: Seconds to wait for a reply. Five minutes by default,
                because a single piece of a video takes far longer to send
                than an ordinary request.
            retries: How many times to try again after a hiccup. Left out,
                the shared default is used.
            transport: Where requests actually go. Leave it out for ordinary
                calls; pass your own to send them somewhere else.
            chunk_bytes: How much of a video to send at a time. Between
                5 MB and 64 MB, which is what TikTok takes.
            pkce: Whether to use PKCE, where only the hash of a secret
                travels to TikTok. TikTok asks for it on desktop and mobile
                apps and not on web apps, so it is off by default - a server
                holding a client secret is a web app. Turn it on and
                `start_login` puts the secret in `SendToNetwork.remember`
                for you to hand back to `finish_login`.
            allowed_age_seconds: How old a pushed message may be before
                `check_signature` refuses it. Five minutes by default. A
                signature stays correct forever, so this is what stops
                anybody who copied one request sending it again next year.
            now: What to treat as the current moment. Only useful in tests -
                `check_signature` has nowhere to take a moment as an
                argument, because every platform's is the same shape, so a
                test that could not set the clock could only check the age
                rule against however long the test itself took to run.

        Raises:
            ConfigError: If `chunk_bytes` is outside what TikTok takes.
                TikTok refuses a bad piece size halfway through an upload
                rather than at the start, so it is checked here instead.
        """
        if not MIN_CHUNK_BYTES <= chunk_bytes <= MAX_CHUNK_BYTES:
            message = (
                f"chunk_bytes is {chunk_bytes:,}, but TikTok takes pieces of "
                f"between 5 MB ({MIN_CHUNK_BYTES:,} bytes) and 64 MB "
                f"({MAX_CHUNK_BYTES:,} bytes). Try {DEFAULT_CHUNK_BYTES:,} "
                f"for 10 MB at a time. A video smaller than 5 MB is sent "
                f"whole whatever this says."
            )
            raise ConfigError(message)

        self._timeout = timeout
        self._retries = retries
        self._transport = transport
        self._chunk_bytes = chunk_bytes
        self._pkce = pkce
        self._allowed_age_seconds = allowed_age_seconds
        self._now = now if now is not None else _right_now

    def _client(self, token: str | None = None) -> HttpClient:
        """Make a client pointed at TikTok's API.

        Args:
            token: The account's token, for anything that needs one. The
                sign-in and the upload both leave this out - the first
                because there is no token yet, the second because the
                upload address is signed by TikTok and lives on a different
                machine, which has no business seeing an account's token.

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
            errors=tiktok_errors,
        )

    def api_base(self, connection: Connection) -> str:
        """Return where TikTok's API lives.

        The same address for everybody. The sign-in page and the upload go
        elsewhere, and this file handles both itself rather than bending
        this.

        Args:
            connection: The account we are about to act as. Not used.

        Returns:
            `"https://open.tiktokapis.com/v2"`.
        """
        return API_URL

    def auth_headers(self, connection: Connection) -> Mapping[str, str]:
        """Return the header that proves we may act as this account.

        Args:
            connection: The account we are acting as.

        Returns:
            TikTok's `Authorization` header.
        """
        return {"Authorization": f"Bearer {connection.token.access_token}"}

    async def limits(self, connection: Connection) -> Limits:
        """Return what TikTok allows.

        Nothing is asked of TikTok here. None of these numbers changes from
        one account to the next, and a person is only allowed six posting
        calls a minute - so spending one to be told a number that is written
        in the documentation would be a poor trade.

        The one number that does vary per person is how long their videos
        may be, which TikTok will tell you through its creator info call.
        `Limits` has nowhere to put a duration, and reading it would cost a
        request on every post, so it is left to TikTok to refuse.

        Args:
            connection: The account to ask about. Not used.

        Returns:
            What TikTok allows.
        """
        return Limits(
            # The caption, which is what `Post.text` becomes. TikTok counts
            # the way Java does, so an emoji costs two of the 2,200.
            max_text_length=MAX_CAPTION_UNITS,
            text_counted_in=TextCount.UTF16_UNITS,
            # No pictures at all, so there is no number to give. `None`
            # means "we do not know"; the refusal comes from the missing
            # `Feature.POST_IMAGE` instead, which says the true thing.
            max_images=None,
            # The caption *is* `Post.text` here. There is no second field
            # for a title, so there is no separate length to declare.
            max_title_length=None,
            max_videos=MAX_VIDEOS_PER_POST,
            max_video_bytes=MAX_VIDEO_BYTES,
        )

    async def start_login(self, request: LoginRequest) -> SendToNetwork:
        """Build the address to send somebody to so they can approve your app.

        Nothing is sent to TikTok here.

        Args:
            request: Where to send them back to, what to ask for, and your
                app's credentials.

        Returns:
            The address to redirect their browser to, the state value that
            will come back with them, and - when this platform was built
            with `pkce=True` - the secret to hand back to `finish_login`.

        Raises:
            ConfigError: If there are no app credentials anywhere.
        """
        app = _app_or_refuse(request.app, "sign somebody in")

        state = request.state or secrets.token_urlsafe(_STATE_BYTES)
        remember: RawData = {}
        query: dict[str, str] = {
            "client_key": app.client_id,
            "response_type": "code",
            # Commas, not the spaces every other OAuth network uses.
            "scope": ",".join(request.scopes or DEFAULT_SCOPES),
            "redirect_uri": request.redirect_uri,
            "state": state,
        }

        if self._pkce:
            verifier = secrets.token_urlsafe(_VERIFIER_BYTES)
            remember["code_verifier"] = verifier
            query["code_challenge"] = _challenge_for(verifier)
            query["code_challenge_method"] = "S256"

        return SendToNetwork(
            url=f"{SIGN_IN_URL}?{httpx.QueryParams(query)}",
            state=state,
            remember=remember,
        )

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> Finished:
        """Swap the code TikTok sent back for a token, and read who it is for.

        One TikTok token is for one account, so this finishes the job -
        there is nothing here to choose between, and no `ChooseAccount` step.

        Args:
            request: The same request used to start the login.
            callback: The query values TikTok sent back. It must have
                `code`; `state` is checked when it is there.
            remember: What `start_login` put in `SendToNetwork.remember`.
                Only used when this platform was built with `pkce=True`.

        Returns:
            The finished connection. Save it.

        Raises:
            AuthError: If the person said no, if there is no code, if the
                state does not match, or if TikTok refused the swap.
            ConfigError: If there are no app credentials anywhere.
            PlatformError: If TikTok answered without an access token.
        """
        app = _app_or_refuse(request.app, "sign somebody in")
        _check_state(request, callback)
        code = _code_from(callback)

        form: dict[str, Any] = {
            "client_key": app.client_id,
            "client_secret": app.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": request.redirect_uri,
        }

        # Sent only when there is one. TikTok wants it from desktop and
        # mobile apps and not from web apps, and sending one it did not ask
        # for is a refusal rather than a shrug.
        verifier = (remember or {}).get("code_verifier")
        if isinstance(verifier, str) and verifier:
            form["code_verifier"] = verifier

        async with self._client() as http:
            reply = await http.json("POST", TOKEN_URL, data=form)

        code_said, explained = _refusal_in(reply)
        if code_said:
            message = (
                f"TikTok would not swap this code for a token ({code_said}). "
                f"A code can only be swapped once and it goes stale in "
                f"minutes, so this usually means it has already been used or "
                f"the person took too long. Nothing has been saved; start "
                f"the login again.{_said(explained)}"
            )
            raise AuthError(message, platform=PLATFORM_NAME, raw=reply)

        access_token = _text(reply, "access_token", "sign someone in")
        # The account this token is for. Everything socialchimp saves is
        # named after it, so a connection without one is a broken
        # connection - better to say so now than to save it.
        open_id = _text(reply, "open_id", "sign someone in")
        granted = reply.get("scope")
        given = (
            granted.split(",")
            if isinstance(granted, str) and granted
            else list(request.scopes or DEFAULT_SCOPES)
        )

        async with self._client(access_token) as http:
            about = await http.json(
                "GET",
                "/user/info/",
                params={"fields": "open_id,display_name,username"},
            )

        person = _data_in(about).get("user")
        person = person if isinstance(person, dict) else {}
        username = person.get("username")
        display_name = person.get("display_name")

        extra: RawData = {"open_id": open_id}
        if isinstance(username, str) and username:
            extra["username"] = username
            extra["profile_url"] = f"https://www.tiktok.com/@{username}"

        refresh_token = reply.get("refresh_token")

        return Finished(
            connection=Connection(
                id=f"{PLATFORM_NAME}:{open_id}",
                platform=PLATFORM_NAME,
                host=None,
                account_id=open_id,
                account_name=str(display_name or username or open_id),
                token=Token(
                    access_token=access_token,
                    refresh_token=refresh_token
                    if isinstance(refresh_token, str) and refresh_token
                    else None,
                    expires_at=_expiry_from(reply, self._now()),
                ),
                scopes=tuple(given),
                extra=extra,
            )
        )

    async def refresh(
        self,
        connection: Connection,
        app: AppCredentials | None = None,
    ) -> Token:
        """Get a fresh access token for an account.

        **TikTok replaces the refresh token every time one is used.** The
        reply carries a new one and the old one stops working there and
        then, so both halves come back from here and both have to be saved.
        This is the case socialchimp's token code is written for; it is the
        opposite of Google, where the refresh token we already hold has to
        be carried across because the reply never mentions it.

        Renewing needs your app's client key and secret. They arrive as an
        argument, the same way they arrive for a sign-in.

        Args:
            connection: The account whose token is running out.
            app: Your app's client key and client secret. TikTok signs a
                renewal with both, so this is not optional here -
                `SocialChimp` reads them out of your storage and passes
                them in.

        Returns:
            The new access token and the new refresh token. Save both.

        Raises:
            ConfigError: If no credentials arrived.
            TokenExpiredError: If there is no refresh token, or TikTok will
                not take the one we have.
            PlatformError: If TikTok answered without a token.
        """
        signing = _app_or_refuse(app, "renew a token")

        renewal = connection.token.refresh_token
        if renewal is None:
            message = (
                f"The token for {connection.id!r} has run out and there is "
                f"no refresh token to replace it with. The person has to "
                f"connect their TikTok account again."
            )
            raise TokenExpiredError(message, platform=PLATFORM_NAME)

        form: dict[str, Any] = {
            "client_key": signing.client_id,
            "client_secret": signing.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": renewal,
        }

        async with self._client() as http:
            try:
                reply = await http.json("POST", TOKEN_URL, data=form)
            except (AuthError, PlatformError) as refused:
                # TikTok having trouble of its own is not the same as a dead
                # refresh token, and treating it as one would have apps
                # throwing away connections that were perfectly fine.
                if _is_tiktoks_own_fault(refused):
                    raise
                raise self._cannot_renew(connection, refused.raw) from refused

        code_said, explained = _refusal_in(reply)
        if code_said:
            # A 200 with the trouble inside. The token endpoint does this.
            raise self._cannot_renew(connection, reply, code_said, explained)

        # TikTok almost always sends a new refresh token, and the one we
        # sent has stopped working by now. On the rare occasion it sends
        # none, the one we had is still the current one.
        replacement = reply.get("refresh_token")
        return Token(
            access_token=_text(reply, "access_token", "renew a token"),
            refresh_token=replacement
            if isinstance(replacement, str) and replacement
            else renewal,
            expires_at=_expiry_from(reply, self._now()),
        )

    def _cannot_renew(
        self,
        connection: Connection,
        raw: RawData,
        code: str = "",
        explained: str = "",
    ) -> TokenExpiredError:
        """Build the error for a renewal TikTok turned down.

        Args:
            connection: The account whose token cannot be renewed.
            raw: TikTok's untouched reply, kept on the error.
            code: TikTok's word for the trouble, when it gave one.
            explained: TikTok's own sentence, when it gave one.

        Returns:
            The error to raise.
        """
        named = f" ({code})" if code else ""
        message = (
            f"TikTok will not renew the token for {connection.id!r}{named}. "
            f"Its refresh token has been used already - TikTok destroys one "
            f"the moment it is used, so a saved copy of an old one never "
            f"works - or it has expired after its year, or the person "
            f"removed your app. The person has to connect their account "
            f"again.{_said(explained)}"
        )
        return TokenExpiredError(message, platform=PLATFORM_NAME, raw=raw)

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        """Send a video to TikTok.

        Where it lands is up to `Post.options["send_to"]`: `"drafts"`, the
        default, puts it in the person's TikTok inbox for them to finish
        themselves, and `"profile"` posts it straight to their profile.

        The video is sent a piece at a time, read off disk as it goes, so a
        four gigabyte file does not become four gigabytes of memory.

        Args:
            connection: The account to publish as.
            post: What to publish. `Post.text` becomes the caption, on a
                post going to a profile.

        Returns:
            What TikTok said, never `PostState.DONE` - taking the bytes is
            not publishing. A post headed for the profile comes back
            `PROCESSING`, because TikTok is still encoding and moderating
            it; ask `check_state` later, or wait to be told. A post headed
            for the drafts comes back `WAITING_FOR_PERSON`, because that is
            where it stays until somebody opens the app.

        Raises:
            NotSupportedError: If the post has no video, has pictures on it,
                or asks to be scheduled. TikTok can do none of the three.
            InvalidPostError: If the post breaks one of TikTok's rules, or
                if a setting is missing or wrong.
            PlatformError: If TikTok never hands back somewhere to send the
                video.
        """
        # Pictures are turned away before the shared checks rather than
        # after. `check_post` would refuse them too, correctly, but with a
        # sentence that does not mention that TikTok has carousels at all.
        _no_carousels(post)

        limits = await self.limits(connection)
        check_post(post, platform=PLATFORM_NAME, features=self.features, limits=limits)

        _checked_options(post.options)
        where = _where_to_send(post.options)
        video = _the_video(post)

        if where == TO_PROFILE:
            body: RawData = {"post_info": _post_info_for(post)}
            path = "/post/publish/video/init/"
        else:
            _no_caption_in_the_drafts(post)
            _no_settings_in_the_drafts(post.options)
            body = {}
            path = "/post/publish/inbox/video/init/"

        # Last, because it goes to the disk for the file's size and there is
        # no point doing that for a post we were going to turn away anyway.
        total = _how_big(video)
        size, count = _chunk_plan(total, self._chunk_bytes)
        body["source_info"] = {
            "source": "FILE_UPLOAD",
            "video_size": total,
            "chunk_size": size,
            "total_chunk_count": count,
        }

        async with self._client(connection.token.access_token) as http:
            started = await http.json("POST", path, json=body)

        said = self._started_or_complain(started)
        publish_id = _text(said, "publish_id", "take a video")
        upload_to = _text(said, "upload_url", "take a video")

        await self._send_video(upload_to, video, total, size, count)

        return PostResult(
            id=publish_id,
            # TikTok has no address for a post until it is live, and it may
            # never be. `check_state` fills one in once there is one.
            url=None,
            state=(
                PostState.PROCESSING
                if where == TO_PROFILE
                # A post sent to the drafts is not going anywhere else on
                # its own, so this says so now rather than letting an app
                # check back for a change that only a person can make.
                else PostState.WAITING_FOR_PERSON
            ),
            raw=started,
        )

    def _started_or_complain(self, started: RawData) -> RawData:
        """Read the useful half of an init reply, or say what went wrong.

        Args:
            started: What TikTok answered when we said a video was coming.

        Returns:
            The `data` object.

        Raises:
            SocialChimpError: If TikTok refused inside a reply it called a
                success, which its posting calls sometimes do.
        """
        code, _ = _refusal_in(started)
        if code:
            # TikTok answered 200 and put the refusal inside, so nothing has
            # raised yet. Hand it to the same reader that names every other
            # refusal, so the message is the one it would have been.
            raise tiktok_errors(httpx.Response(httpx.codes.BAD_REQUEST, json=started))
        return _data_in(started)

    async def _send_video(
        self,
        upload_to: str,
        video: Media,
        total: int,
        size: int,
        count: int,
    ) -> None:
        """Send the video, a piece at a time, in order.

        Unlike YouTube, TikTok does not say after each piece how much of it
        arrived - the pieces simply go in order and it puts them back
        together. A piece that fails is sent again by `HttpClient`.

        Args:
            upload_to: The address TikTok gave us. It is signed, lives on a
                different machine, and is used exactly as it arrived - with
                no `Authorization` header, because that token is none of
                that machine's business.
            video: The video to send. Only the piece on its way to TikTok is
                ever in memory, because `Media.piece` reads it off disk as
                it goes.
            total: How many bytes of video there are.
            size: How big each piece is, except the last.
            count: How many pieces there are.
        """
        async with self._client() as http:
            for index in range(count):
                start = index * size
                # The last piece carries everything that is left, which can
                # be nearly twice `size`. Cutting it at `size` instead would
                # leave the end of the video behind, and TikTok would sit
                # waiting for bytes that never come.
                last = total - 1 if index == count - 1 else start + size - 1
                await http.put(
                    upload_to,
                    content=video.piece(start, last - start + 1),
                    headers={
                        "Content-Range": f"bytes {start}-{last}/{total}",
                        "Content-Type": video.content_type,
                    },
                )

    async def check_state(self, connection: Connection, post_id: str) -> PostResult:
        """Ask TikTok how far it has got with a post.

        `publish` comes back while TikTok is still working, so this is how
        you find out what happened next - the other way being to let TikTok
        tell you, which is what `check_signature` and `read_update` are for.
        One account may ask 30 times a minute.

        A post sitting in somebody's drafts comes back
        `PostState.WAITING_FOR_PERSON`. As far as TikTok is concerned the
        waiting is over - it has done everything it is going to do - and
        nothing changes until that person opens the app, which may be never.
        There is nothing to be gained by asking again.

        Args:
            connection: The account the post belongs to.
            post_id: The `publish_id` that `publish` handed back.

        Returns:
            Where the post has got to. `PROCESSING` while TikTok is working
            on it, `WAITING_FOR_PERSON` once it is sitting in the drafts,
            `DONE` once it is live, and `FAILED` if TikTok gave up on it.

        Raises:
            NotFoundError: If TikTok has no post with that publish id.
            PlatformError: If TikTok answers without a status.
        """
        async with self._client(connection.token.access_token) as http:
            reply = await http.json(
                "POST",
                "/post/publish/status/fetch/",
                json={"publish_id": post_id},
            )

        said = _data_in(reply)
        reported = _text(said, "status", "say how a post is getting on")

        live = _live_post_id(said)
        username = _username_of(connection)

        return PostResult(
            id=post_id,
            url=_watch_url(username, live) if live and username else None,
            state=_OUR_STATE_FOR.get(reported, PostState.PROCESSING),
            raw=said,
        )

    def check_signature(
        self,
        body: bytes,
        headers: Mapping[str, str],
        *,
        secret: str,
    ) -> None:
        """Check a message TikTok pushed to us really came from TikTok.

        TikTok signs the time and the body together, as `<time>.<body>`, so
        neither can be changed without breaking the signature.

        The signature covers the **raw bytes** of the body. A framework that
        parses the JSON and builds it again first changes the spacing and
        the key order, and this then fails on a message that was perfectly
        good. Read the body, check it here, and parse it afterwards.

        The age of the message is checked too. A signature stays correct
        forever, so without that anybody who got hold of one request - from
        a log, a proxy, a screenshot - could send it again next year.

        Args:
            body: The request body, exactly as it arrived.
            headers: The request headers. Case does not matter.
            secret: Your app's **client secret** from the developer portal.

        Raises:
            SignatureError: If the message cannot be trusted. Answer 401 and
                do nothing else with it.
        """
        sent = _header(headers, SIGNATURE_HEADER)
        if sent is None:
            message = (
                f"This request has no {SIGNATURE_HEADER} header, so there is "
                f"nothing to check it against. Refusing it."
            )
            raise SignatureError(message)

        when, signature = _signature_parts(sent)

        signed = f"{when}.".encode() + body
        expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()

        # Compared this way so that how long the comparison takes says
        # nothing about how much of the signature was right.
        if not hmac.compare_digest(signature, expected):
            message = (
                "The signature does not match the body. Either the body was "
                "changed on the way here, or it was signed with a different "
                "secret - TikTok signs with your client secret. If you are "
                "sure the secret is right, check that nothing parsed and "
                "rebuilt the body before it reached this function."
            )
            raise SignatureError(message)

        try:
            sent_at = float(when)
        except ValueError as problem:
            message = (
                f"The time in the {SIGNATURE_HEADER} header is {when!r}, "
                f"which is not a moment we can read. Refusing it."
            )
            raise SignatureError(message) from problem

        check_not_too_old(
            sent_at,
            allowed_age_seconds=self._allowed_age_seconds,
            now=self._now(),
        )

    def read_update(
        self,
        body: bytes,
        headers: Mapping[str, str],
    ) -> Update:
        """Turn a checked message into an update your app understands.

        Only call this after `check_signature` has passed.

        **The same message can arrive more than once.** TikTok promises to
        deliver at least once and keeps retrying for 72 hours, so duplicates
        are normal. Every copy of one message comes back with the same `id`
        from here, so giving `Dispatcher` a `SeenUpdates` - see
        `socialchimp.events` - is enough to handle it once.

        Args:
            body: The request body, untouched.
            headers: The request headers. Not needed here; the signature
                header has already done its job by this point.

        Returns:
            What happened, in socialchimp's own words: a finished post and a
            post that has gone public are both `POST_PUBLISHED`, a video
            that reached somebody's drafts is `POST_DRAFTED`, a failure is
            `POST_FAILED`, and somebody removing your app is
            `CONNECTION_REVOKED`. Anything TikTok adds later arrives as
            `UpdateKind.UNKNOWN` with TikTok's own word on `kind_name`.

        Raises:
            PlatformError: If the body is not one of TikTok's messages.
        """
        try:
            parsed = json.loads(body)
        except ValueError as problem:
            message = (
                "This request from TikTok could not be read as JSON, so "
                "there is nothing in it to act on. Pass the raw body, "
                "exactly as it arrived."
            )
            raise PlatformError(message, platform=PLATFORM_NAME) from problem

        if not isinstance(parsed, dict):
            message = (
                f"This request from TikTok could not be read as one of its "
                f"messages: it holds a {type(parsed).__name__} where an "
                f"object was expected."
            )
            raise PlatformError(message, platform=PLATFORM_NAME)

        sent: RawData = parsed
        event = str(sent.get("event", ""))
        who = str(sent.get("user_openid", ""))
        inside = _content_in(sent)
        # Kept unpacked, so an app reading `raw` finds fields rather than a
        # long piece of text where fields should be.
        sent["content"] = inside

        when = sent.get("create_time")
        happened = (
            datetime.fromtimestamp(float(when), UTC)
            if isinstance(when, int | float) and not isinstance(when, bool)
            # A message with no time on it is stamped as it arrives. A
            # little late beats no time at all, since every update
            # socialchimp holds is ordered by this.
            else self._now()
        )

        publish_id = (
            str(inside.get("publish_id", "")) if isinstance(inside, dict) else ""
        )

        return Update.from_network(
            # TikTok puts no identifier on a message, so this is built only
            # out of what it said - which makes a retry of the same message
            # produce the same id, which is what SeenUpdates needs.
            update_id=f"{event}:{who}:{publish_id}:{sent.get('create_time', '')}",
            # TikTok's own word when we have none of our own, so an app
            # listening for everything still learns what happened.
            kind_name=_OUR_WORD_FOR.get(event, event),
            platform=PLATFORM_NAME,
            # TikTok says which person, not which of your connections. A
            # login here names a connection after their open id, so the two
            # line up without your app keeping a table of its own.
            connection_id=f"{PLATFORM_NAME}:{who}",
            created_at=happened,
            raw=sent,
        )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Find a header whatever case it was sent in.

    HTTP header names do not care about case, but a plain dict does. Every
    framework hands them over differently, so we look for ourselves.

    Args:
        headers: The request's headers.
        name: The header to look for.

    Returns:
        Its value, or `None` if it is not there.
    """
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _is_tiktoks_own_fault(refused: SocialChimpError) -> bool:
    """Say whether a refusal was TikTok struggling rather than a dead token.

    Args:
        refused: The error the token endpoint gave us.

    Returns:
        True for a 500 and for a reply we could not read at all. Both are
        worth trying again; neither means the person has to sign in again,
        and telling an app to delete a connection over a bad five minutes at
        TikTok is the sort of quiet damage this library exists to avoid.
    """
    if not isinstance(refused, PlatformError):
        return False
    return (
        refused.status_code is None
        or refused.status_code >= httpx.codes.INTERNAL_SERVER_ERROR
    )
