"""Instagram: signed in on its own, no Facebook Page anywhere.

Meta ships two different ways to talk to an Instagram Business or Creator
account. The older one signs in through Facebook Login and only works for an
account linked to a Facebook Page. This is the other one - "Business Login
for Instagram", part of what Meta calls "Instagram API with Instagram
Login" - which signs in directly against Instagram and never asks about a
Page at all.

Publishing is the same three-call shape either way - build a container, wait
for it, publish it - because that part is genuinely Instagram's, not
Facebook's. What changes here is everything about getting a token: the
sign-in page, the host every request goes to, the app credentials, and the
scopes.

The parts that really are shared with the rest of Meta - the signature on a
pushed request, how a state and a login code are checked, the shape of
Meta's own error codes - still come from `_meta.py`. Nothing here talks to
`graph.facebook.com` at all.

## A different app id, found in a different place

Adding "Instagram API setup with Instagram login" to a Meta app makes an
**Instagram App ID and Instagram App Secret**, sitting in their own section
of the dashboard - not the Facebook App ID shown at the top of the page, and
not Threads' pair either. Meta's own documentation puts it plainly: apps
using this product "will use the Instagram app ID displayed on the
Instagram > API setup with Instagram login section of the dashboard." Using
the Facebook pair here gets past the sign-in page - Meta accepts the
redirect - and then fails at the token swap with a message that mentions
none of this.

`SEPARATE_APP` is the sentence that says so, on every refusal where the
wrong pair is a plausible cause.

## No Facebook Page, anywhere

The old flow asks which Facebook Page a person manages and looks for an
Instagram account attached to it, because that is the only way Facebook
Login can find one. This flow signs in directly as the Instagram account, so
there is no Page to ask about, no `pages_show_list` permission, and no
`instagram_business_account` field to read off anything. A personal
Instagram account still cannot use this - Meta's own sign-in refuses one
before we ever see it - but a Business or Creator account no longer needs a
Page linked to it to be reached this way.

## Three different hosts, none of them Facebook's

- People approve your app at `https://www.instagram.com/oauth/authorize`.
- The code is swapped for a token at `https://api.instagram.com/oauth/access_token`,
  on its own host, separate from every other request.
- Everything else - making the token last, renewing it, publishing, reading
  limits, reading the account back - goes to `https://graph.instagram.com`,
  which is versioned the same way `graph.facebook.com` is.

`_meta.GRAPH_API` and `_meta.SIGN_IN_PAGE` do not apply here, and neither do
`_meta.swap_code_for_token` or `_meta.long_lived_token` - the hosts, the
grant names, and which host each address lives on are all different.

## Signing in never stops to ask which account

Facebook Login can cover several Pages, each with its own Instagram account,
which is why the old flow paused with `ChooseAccount`. Signing in directly
against Instagram is signing in as one account - there is nothing to choose
between - so `finish_login` finishes the job outright, the same as
`ThreadsPlatform` does.

The account's id comes back on the very first reply, in the code-exchange
response's `user_id` field - Instagram hands it out before we have even made
the token last, which is a nicer contract than asking again later. A second
request, once the long-lived token is ready, reads the username to show a
person.

## The permissions are Instagram's own

    instagram_business_basic
    instagram_business_content_publish
    instagram_business_manage_comments
    instagram_business_manage_messages

No `pages_show_list`, no `business_management` - both of those exist to find
an account through a Page, and there is no Page here to find one through.

## Renewal that is real, on a timer of our own choosing

Like Threads, Instagram Login hands out a genuine refresh: one request, no
app secret, another sixty days. Unlike Threads, Meta's own documentation for
this product names no minimum token age before it will do that - it only
says a token is "valid for 60 days and can be refreshed before they expire."
Rather than guess at an undocumented server-side rule, `refresh` uses a rule
of its own: it does nothing at all while more than `REFRESH_AFTER_SECONDS`
(thirty days) remain, and only then asks Meta for another sixty. Called
early, it hands back the token you already had rather than spending a
request or risking a refusal for a precondition nobody has documented.

## Publishing, step by step

    1. POST /{account}/media          -> a container id
    2. GET  /{container}?fields=...   -> wait until it says FINISHED
    3. POST /{account}/media_publish  -> the post is live

Identical to the Facebook-linked flow, because this part belongs to
Instagram rather than to whichever login got you a token: the same waiting,
the same carousel shape, the same caption and hashtag limits, the same daily
allowance, the same error codes for a file Instagram could not fetch or a
video in the wrong format. See `docs/platforms.md` for the numbers.

## Instagram fetches your file; it will not take an upload

Exactly as before: there is no upload here. Instagram is given a web address
and fetches the file itself.

- `Media.from_url("https://...")` works, and is the only thing that does.
- `Media.from_file` and `Media.from_bytes` are **refused**, with a message
  saying to put the file somewhere public first.

## What Instagram cannot do here

- **No text-only post.** Every post is a picture or a video.
- **No scheduling.** Instagram's API has none.
- **No deleting.** There is no call for it.
- **No app registration.** `create_app` says so, and names where the
  Instagram App ID and Secret actually live.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import anyio
import httpx

from socialchimp.errors import (
    AuthError,
    ConfigError,
    InvalidPostError,
    NotSupportedError,
    PlatformError,
    RateLimitError,
    SocialChimpError,
    TokenExpiredError,
)
from socialchimp.events import Update
from socialchimp.events import answer_setup_check as echo_the_challenge
from socialchimp.features import (
    Feature,
    Limits,
    TextCount,
    check_option_names,
    check_post,
)
from socialchimp.http import HttpClient, read_body
from socialchimp.models import (
    Connection,
    MediaKind,
    Post,
    PostResult,
    PostState,
    RawData,
    Token,
)
from socialchimp.platform import Finished, LoginRequest, SendToNetwork
from socialchimp.platforms._meta import (
    DEVELOPER_PORTAL,
    Graph,
    Usage,
    changes_in,
    check_meta_signature,
    check_state,
    code_from,
    first_update,
    meta_errors,
    quota_left,
    required_text,
    sign_in_url,
    state_for,
    token_from,
    where_to_post,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from socialchimp.http import Retries
    from socialchimp.models import AppCredentials

__all__ = ["InstagramPlatform", "instagram_errors"]

PLATFORM_NAME: Final = "instagram"

IG_LOGIN_HOST: Final = "https://api.instagram.com"
"""Where the code Instagram sends back is swapped for a token.

Its own host, separate from everywhere else this file talks to. Sending this
one request to `graph.instagram.com` instead gets you a 404.
"""

IG_VERSION: Final = "v21.0"
"""Which version of Instagram's Graph API this talks to.

Kept the same as the version pinned for Facebook and Instagram's older flow
in `_meta.GRAPH_VERSION`, so the two age together.
"""

IG_GRAPH_HOST: Final = "https://graph.instagram.com"
"""Where a token is made to last and later renewed. No version in front of
either address - only the ordinary requests below are versioned."""

IG_GRAPH_API: Final = f"{IG_GRAPH_HOST}/{IG_VERSION}"
"""Where every ordinary request goes: publishing, limits, reading the
account back. Not `graph.facebook.com` - a token from this sign-in only
works against Instagram's own host."""

SIGN_IN_PAGE: Final = "https://www.instagram.com/oauth/authorize"
"""The page people approve your app on.

Not Facebook's dialog. Facebook Login will not sign anybody in to this
product, and this page will not sign anybody in to the Facebook-linked one.
"""

SWAP_PATH: Final = "/oauth/access_token"
"""Where a code is swapped for a token that lasts about an hour. A POST, on
`IG_LOGIN_HOST`."""

MAKE_IT_LAST_PATH: Final = "/access_token"
"""Where an hour-long token is traded for one good for sixty days. A GET, on
`IG_GRAPH_HOST`, unversioned."""

RENEW_PATH: Final = "/refresh_access_token"
"""Where a long-lived token gets another sixty days. A GET, on
`IG_GRAPH_HOST`, unversioned, and a real renewal - see `refresh`."""

ACCOUNT_FIELDS: Final = "user_id,username"
"""What to ask about the account once the long-lived token is ready.

The account's id itself is already known by this point, from the
code-exchange reply - this is only to read a username worth showing someone.
"""

DEFAULT_SCOPES: Final = (
    "instagram_business_basic",
    "instagram_business_content_publish",
    "instagram_business_manage_comments",
    "instagram_business_manage_messages",
)
"""The permissions this sign-in asks for.

- `instagram_business_basic` - read the account: who it is, what it has
  posted. Needed by every other call, including renewal.
- `instagram_business_content_publish` - make and publish a post.
- `instagram_business_manage_comments` - read the comments Instagram pushes
  to you, and answer them.
- `instagram_business_manage_messages` - Instagram's own name for reading
  and answering the account's messages. Not wired up here yet; asked for
  because Meta will not offer it later if it was left out at sign-in.

There is no `pages_show_list` and no `business_management` here. Both exist
to find an Instagram account through a Facebook Page, and there is no Page
in this flow to find one through.
"""

SEPARATE_APP: Final = (
    "Adding Instagram API setup with Instagram login to a Meta app makes an "
    "Instagram App ID and an Instagram App Secret, in their own section of "
    "the dashboard - not the Facebook App ID shown at the top of the page, "
    "and not Threads' pair either. Use the Instagram App ID and the "
    "Instagram App Secret here. The wrong pair gets past the sign-in page "
    "and is then refused at the token swap, with a message that mentions "
    "none of this."
)
"""The sentence that saves somebody an afternoon.

Put on every refusal where the wrong app id is a plausible cause, because by
the time Instagram answers it is far too late to guess.
"""

POST_OPTIONS: Final = ("carousel",)
"""The settings `Post.options` accepts here.

Only one, and only ever `True` or `False`:

    Post(media=(one, two), options={"carousel": True})

Two or more attachments already make a carousel without being asked, so this
is for saying so out loud. Anything else is refused before we send it.
"""

MAX_CAPTION_LENGTH: Final = 2_200
"""Characters allowed in a caption."""

MOST_HASHTAGS: Final = 30
"""Hashtags allowed in a caption.

Go over and Instagram takes the post and quietly shows it to nobody, which is
worse than a refusal, so this is checked here.
"""

FEWEST_IN_A_CAROUSEL: Final = 2
"""A carousel of one is not a carousel. Instagram refuses it."""

MOST_IN_A_CAROUSEL: Final = 10
"""Pictures and videos allowed in one post, counted together."""

HOW_OFTEN_TO_CHECK: Final = 60.0
"""Seconds between two looks at a container Instagram is still making.

Once a minute is what Meta's own guide suggests. Looking more often does not
make Instagram finish sooner and does spend your hourly allowance.
"""

HOW_LONG_TO_WAIT: Final = 300.0
"""Seconds to keep looking before giving up - five minutes, as Meta suggests.

Giving up is not the same as failing. See `_stopped_waiting`.
"""

PICTURE_FIRST_WAIT: Final = 1.0
"""Seconds to wait before the second look at a picture, then twice that, and so on.

A picture is usually ready within a second or two, and Instagram answers
`media_publish` with error 9007 if it is asked too soon - so a picture is
looked at straight away, and only then does the waiting start, short at first
and doubling each time. A video is different: it is re-encoded, which takes
minutes, and stays on `HOW_OFTEN_TO_CHECK`.
"""

PICTURE_LONGEST_WAIT: Final = 30.0
"""The most the doubling above is allowed to grow to, in seconds."""

TIMES_TO_PUBLISH_AGAIN: Final = 2
"""How many more times to ask for a post to go out after it is "not ready".

Only for error 9007, which means the post was fine and Instagram was not
finished with it, so asking again cannot publish anything twice. See
`_put_it_out`.
"""

WAIT_BEFORE_PUBLISHING_AGAIN: Final = 5.0
"""Seconds to wait before each of those tries."""

NOT_READY_RETRY_AFTER: Final = 30.0
"""What `RateLimitError.retry_after` says on an error 9007 we gave up on.

Meta's own error reference calls this error transient and says to try again
within thirty seconds to two minutes. We have already spent about ten seconds
on it by the time this is raised.
"""

TOKEN_LIFE_SECONDS: Final = 60 * 24 * 60 * 60
"""How long a long-lived Instagram token is good for: sixty days."""

REFRESH_AFTER_SECONDS: Final = 30 * 24 * 60 * 60
"""How much life a token has left before `refresh` bothers Meta about it.

Meta's own documentation for this product names no minimum age before it
will renew a token - unlike Threads, which is explicit about twenty-four
hours. Rather than guess at a rule nobody has written down, this is a
policy of ours: do nothing while more than thirty days remain of the sixty,
and only then ask. Called earlier than that, `refresh` hands back the token
you already had rather than spending a request against an undocumented
precondition.
"""

# What Instagram calls a container it has finished with, one it gave up on,
# and one it threw away. Anything else - IN_PROGRESS, or a word Meta adds
# next year - means keep looking.
_FINISHED: Final = "FINISHED"
_GAVE_UP: Final = "ERROR"
_THROWN_AWAY: Final = "EXPIRED"

# Instagram's own error codes, on top of the ones every Meta network shares.
_COULD_NOT_FETCH_THE_FILE: Final = 9004
_VIDEO_FORMAT_IT_WILL_NOT_TAKE: Final = 2_207_026
_NOBODY_KNOWS: Final = 24
# "Media ID is not available": the post was made and is fine, and Instagram is
# not finished with it. Sent as 9007, with 2207027 alongside.
_NOT_READY_YET: Final = 9007
_NOT_READY_YET_SUBCODE: Final = 2_207_027
# "The aspect ratio is not supported": a property of the picture, so trying
# again is never the fix.
_ASPECT_RATIO_IT_WILL_NOT_TAKE: Final = 36_003

# What Instagram calls a change it pushes to us, and what we call it.
# `story_insights` is missing on purpose: it is a bundle of numbers about a
# story that has finished, socialchimp has no name for that, and it arrives
# as UNKNOWN with Instagram's own word kept on the update.
_OUR_WORD_FOR: Final = {
    "comments": "comment_created",
    # A comment on a live video is still a comment to an app that answers
    # comments, and it stops being special the moment the video ends.
    "live_comments": "comment_created",
    "mentions": "mention",
}

# A hashtag runs from the # to the first thing that is not a letter, a digit
# or an underscore, which is how Instagram itself reads one.
_A_HASHTAG: Final = re.compile(r"#\w+")


def _now() -> datetime:
    """Return the current moment.

    Kept as its own function so tests can say how old a token is, and how
    long the waiting took, without sitting through either.

    Returns:
        Now, with a timezone.
    """
    return datetime.now(UTC)


async def _sleep(seconds: float) -> None:
    """Wait between two looks at a container.

    Kept as its own function for the same reason as `_now`. anyio comes with
    httpx, so waiting through it adds no dependency and lets this run under
    trio as happily as under asyncio.

    Args:
        seconds: How long to wait.
    """
    await anyio.sleep(seconds)


# ---------------------------------------------------------------------------
# Turning Instagram's refusals into ours
# ---------------------------------------------------------------------------


def _metas_words(error: RawData) -> str:
    """Pull Instagram's own message out of its error object.

    Args:
        error: The error object Meta sent.

    Returns:
        Its message, ready to add to the end of ours, or an empty string.
    """
    said = error.get("message")
    return f" Instagram said: {said}" if isinstance(said, str) and said else ""


def _numbers_in(error: RawData) -> set[int]:
    """Collect the codes on one Meta error object.

    Meta puts some of these under `code` and some under `error_subcode`, and
    which one it uses for a given problem is not something to rely on - the
    same unsupported video comes back either way depending on which part of
    Instagram noticed. So both are read and treated the same.

    Args:
        error: The error object Meta sent.

    Returns:
        Every whole number it carried, which may be none at all.
    """
    return {
        found
        for found in (error.get("code"), error.get("error_subcode"))
        if isinstance(found, int) and not isinstance(found, bool)
    }


def _is_not_ready(error: object) -> bool:
    """Say whether an error object is Instagram's "not finished with it yet".

    Args:
        error: The `error` object from a reply, or anything else.

    Returns:
        True for error 9007 or subcode 2207027, whichever way round Instagram
        sent them.
    """
    if not isinstance(error, dict):
        return False
    return bool({_NOT_READY_YET, _NOT_READY_YET_SUBCODE} & _numbers_in(error))


def _instagram_error(body: RawData) -> SocialChimpError | None:
    """Name a refusal that belongs to Instagram rather than to Meta at large.

    Args:
        body: The reply, already read into a dictionary.

    Returns:
        The error to raise, or `None` when this is not one of Instagram's own
        codes - in which case Meta's shared names are the right ones.
    """
    error = body.get("error")
    if not isinstance(error, dict):
        return None

    codes = _numbers_in(error)
    raw = {"error": error}

    # Looked at before 24 below, which Instagram also uses for this and which
    # would otherwise call it "something went wrong".
    if _is_not_ready(error):
        message = (
            f"Instagram was not ready to publish this yet (error "
            f"{_NOT_READY_YET}, subcode {_NOT_READY_YET_SUBCODE}). The post "
            f"itself is fine - it is still being made, and nothing has been "
            f"published. Try the same post again in about "
            f"{NOT_READY_RETRY_AFTER:.0f} seconds.{_metas_words(error)}"
        )
        return RateLimitError(
            message,
            retry_after=NOT_READY_RETRY_AFTER,
            platform=PLATFORM_NAME,
            raw=raw,
        )

    if _ASPECT_RATIO_IT_WILL_NOT_TAKE in codes:
        message = (
            f"Instagram will not take this picture's shape (error "
            f'{_ASPECT_RATIO_IT_WILL_NOT_TAKE}, "the aspect ratio is not '
            f'supported"). A feed picture has to be between 4:5 (0.8) and '
            f"1.91:1 wide against its height. Crop it or add space around "
            f"it; nothing else about the post is wrong, and sending the same "
            f"picture again will not help.{_metas_words(error)}"
        )
        return InvalidPostError(message, platform=PLATFORM_NAME, raw=raw)

    if _COULD_NOT_FETCH_THE_FILE in codes:
        message = (
            f"Instagram could not fetch the file at that address (error "
            f"{_COULD_NOT_FETCH_THE_FILE}). It fetches every picture and "
            f"video itself, so the address has to be reachable from the "
            f"public internet - no login, no private network, no address "
            f"that only works inside your own - and it has to still be "
            f"there when Instagram asks, which can be a minute "
            f"later.{_metas_words(error)}"
        )
        return InvalidPostError(message, platform=PLATFORM_NAME, raw=raw)

    if _VIDEO_FORMAT_IT_WILL_NOT_TAKE in codes:
        message = (
            f"Instagram will not take this video's format (error "
            f"{_VIDEO_FORMAT_IT_WILL_NOT_TAKE}). It wants MP4 with H.264 "
            f"video and AAC audio. Converting it is the fix; nothing about "
            f"the post itself is wrong.{_metas_words(error)}"
        )
        return InvalidPostError(message, platform=PLATFORM_NAME, raw=raw)

    if _NOBODY_KNOWS in codes:
        message = (
            f"Instagram says something went wrong without saying what (error "
            f"{_NOBODY_KNOWS}). This one usually means the file: an address "
            f"it could not reach, a picture the wrong shape, a video too "
            f"long. Trying again in a minute is worth one go before you go "
            f"looking at the file.{_metas_words(error)}"
        )
        return PlatformError(message, platform=PLATFORM_NAME, raw=raw)

    return None


def instagram_errors(response: httpx.Response) -> SocialChimpError:
    """Turn an unhappy reply from Instagram into a socialchimp error.

    Instagram's own codes are looked at first, then Meta's shared ones, which
    are the same on every one of Meta's networks. See `_meta.meta_errors` for
    what those mean.

    Args:
        response: The reply to turn into an error.

    Returns:
        The error to raise.
    """
    found = _instagram_error(read_body(response))
    if found is not None:
        return found
    return meta_errors(response, platform=PLATFORM_NAME)


async def _ask(graph: Graph, method: str, path: str, **kwargs: object) -> RawData:
    """Send one request to Instagram and read the reply.

    Args:
        graph: The conversation to send it through.
        method: `"GET"`, `"POST"` and so on.
        path: Joined onto Instagram's address.
        **kwargs: Anything `HttpClient.request` takes.

    Returns:
        The reply, parsed.

    Raises:
        SocialChimpError: If Instagram refused.
    """
    try:
        return await graph.json(method, path, **kwargs)
    except PlatformError as refused:
        # Meta hides a refusal inside a perfectly happy 200 often enough that
        # `Graph` reads every body, and it names what it finds using the
        # codes every one of its networks shares. Those do not include
        # Instagram's own, so a 9004 arriving that way comes out as "no
        # better name for that code yet" unless we look again here. Anything
        # Meta could name is already a different class of error and never
        # reaches this.
        better = _instagram_error(refused.raw)
        if better is None:
            raise
        raise better from refused


# ---------------------------------------------------------------------------
# Your app, which is not the app on the Facebook-linked flow
# ---------------------------------------------------------------------------


def _app_or_refuse(request: LoginRequest, *, what: str) -> AppCredentials:
    """Insist on your Instagram app's id and secret, and say which pair to use.

    Args:
        request: The request being started or finished.
        what: What we were about to do, for the message.

    Returns:
        The credentials.

    Raises:
        ConfigError: If there are none, saying where to get the right ones.
    """
    if request.app is None:
        message = (
            f"instagram needs your app's id and secret to {what}, and none "
            f"arrived. Make the app by hand at {DEVELOPER_PORTAL}, add "
            f"Instagram API setup with Instagram login to it, then save the "
            f"pair with Storage.save_app under the platform name "
            f"'instagram'. {SEPARATE_APP}"
        )
        raise ConfigError(message)
    return request.app


def _app_must_be_made_by_hand() -> NotSupportedError:
    """Build the error for somebody who asked us to register an app.

    Returns:
        The error to raise. Returned rather than raised so the type checker
        follows what happens next at the place it is used.
    """
    return NotSupportedError(
        platform=PLATFORM_NAME,
        what="registering an app for you",
        suggestion=(
            f"Meta has no call for it. Make the app by hand at "
            f"{DEVELOPER_PORTAL}, add Instagram API setup with Instagram "
            f"login to it, and save the id and secret with "
            f"Storage.save_app. {SEPARATE_APP} Meta also has to review the "
            f"app before it works for anybody but you, and the account "
            f"signing in has to be a Business or Creator account - a "
            f"personal Instagram account cannot use this sign-in at all."
        ),
    )


def _probably_the_wrong_app(refused: SocialChimpError) -> AuthError:
    """Say that a refused token swap is often the wrong app id.

    Instagram answers a wrong app id with whatever code it feels like, and
    none of its messages mention the two pairs, so this adds the sentence
    that does. Meta's own words are kept on the end and on `raw`.

    Args:
        refused: What Instagram answered, already named by `meta_errors`.

    Returns:
        The error to raise.
    """
    message = (
        f"instagram would not swap this sign-in for a token. The usual "
        f"reason is the app id. {SEPARATE_APP} It said: {refused}"
    )
    return AuthError(message, platform=PLATFORM_NAME, raw=refused.raw)


def _account_id_from(reply: RawData) -> str:
    """Read the account's id out of the code-exchange reply.

    Instagram hands this back as `user_id`, and as a JSON number rather than
    a string - the one place in this whole file that is true, which is why
    `_meta.required_text` cannot be used for it.

    Args:
        reply: What Instagram answered when the code was swapped.

    Returns:
        The id, as a string.

    Raises:
        PlatformError: If there is no usable id in the reply.
    """
    found = reply.get("user_id")
    if isinstance(found, bool):
        pass
    elif isinstance(found, int):
        return str(found)
    elif isinstance(found, str) and found:
        return found

    message = (
        "instagram left 'user_id' out of its reply when we asked it to sign "
        "someone in. That should not happen. The whole reply is on this "
        "error."
    )
    raise PlatformError(message, platform=PLATFORM_NAME, raw=reply)


# ---------------------------------------------------------------------------
# What a post may carry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Attachment:
    """One picture or video, and the address Instagram will fetch it from.

    Attributes:
        url: Where the file is. Always set, because a post that has no
            address for a file never gets this far.
        kind: Picture or video.
        alt_text: Description for people using a screen reader.
    """

    url: str
    kind: MediaKind
    alt_text: str | None = None


def _instagram_account_of(connection: Connection) -> str:
    """Work out which Instagram account a connection posts to.

    Args:
        connection: The account to look at.

    Returns:
        Instagram's identifier for it.

    Raises:
        ConfigError: If the connection names no account at all.
    """
    return where_to_post(
        connection,
        key="instagram_id",
        what="Instagram account",
        platform=PLATFORM_NAME,
    )


def _checked_options(options: RawData) -> bool:
    """Check every setting on a post, and say whether a carousel was asked for.

    Args:
        options: What was put in `Post.options`.

    Returns:
        True if this post was asked to go out as a carousel.

    Raises:
        InvalidPostError: If a setting is unknown or its value is wrong. This
            happens before any request, so a typo costs nothing.
    """
    check_option_names(options, platform=PLATFORM_NAME, allowed=POST_OPTIONS)

    for key, value in options.items():
        if not isinstance(value, bool):
            message = (
                f"{key} is {value!r}, but it has to be True or False. "
                f"True sends the post as a carousel; leaving it out sends "
                f"two or more attachments as one anyway."
            )
            raise InvalidPostError(message)
    return bool(options.get("carousel", False))


# What `check_post` adds to its own "this network has no text-only post"
# message here. Instagram fetches every file itself, so the usual advice of
# "attach one" would send somebody straight to Media.from_file and a second
# refusal.
WORDS_ALONE_ADVICE: Final = (
    "Instagram fetches every file itself, so it has to be a "
    "Media.from_url(...); your words become its caption."
)


def _needs_a_web_address() -> NotSupportedError:
    """Build the error for a file we were handed the bytes of.

    Returns:
        The error to raise, saying what to do instead.
    """
    return NotSupportedError(
        platform=PLATFORM_NAME,
        what="being sent a file",
        suggestion=(
            "It fetches every picture and video itself, from a web address, "
            "and has no upload of any kind - so Media.from_file and "
            "Media.from_bytes cannot be published here. Put the file "
            "somewhere the public internet can reach it, such as object "
            "storage with a public link or your own web server, and use "
            "Media.from_url(...) instead. socialchimp will not quietly host "
            "it for you, because a file that appeared somewhere you did not "
            "choose is a worse surprise than this message."
        ),
    )


def _things_to_publish(post: Post) -> tuple[_Attachment, ...]:
    """Work out what this post is actually made of, and refuse it if we cannot.

    Args:
        post: The post about to be sent.

    Returns:
        The pictures and videos, in the order they were given.

    Raises:
        NotSupportedError: If the post carries a file we would have to
            upload.
        InvalidPostError: If there are more attachments than fit in one post.
    """
    found: list[_Attachment] = []
    for item in post.media:
        if item.url is None:
            raise _needs_a_web_address()
        found.append(_Attachment(url=item.url, kind=item.kind, alt_text=item.alt_text))

    if len(found) > MOST_IN_A_CAROUSEL:
        message = (
            f"This post has {len(found)} pictures and videos between them, "
            f"and Instagram takes at most {MOST_IN_A_CAROUSEL} in one post. "
            f"Send the rest as a second post."
        )
        raise InvalidPostError(message)

    return tuple(found)


def _check_hashtags(caption: str) -> None:
    """Count the hashtags in a caption and refuse a caption with too many.

    Args:
        caption: The words about to be sent.

    Raises:
        InvalidPostError: If there are more than Instagram allows. Going over
            does not get the post refused - Instagram takes it and then shows
            it to nobody, which is worse than a refusal, so this is checked
            here.
    """
    found = len(_A_HASHTAG.findall(caption))
    if found > MOST_HASHTAGS:
        message = (
            f"This caption has {found} hashtags and Instagram allows at most "
            f"{MOST_HASHTAGS}. Going over does not get the post refused: "
            f"Instagram takes it and then shows it to hardly anyone, so this "
            f"is refused here where you can still see why."
        )
        raise InvalidPostError(message)


def _what_it_allows(posts_left_today: int | None = None) -> Limits:
    """Return the numbers Instagram enforces.

    Args:
        posts_left_today: How many posts are left in the last 24 hours, when
            we have asked. `None` before we have, and whenever Instagram's
            answer was not one we could read.

    Returns:
        What Instagram allows right now.
    """
    return Limits(
        max_text_length=MAX_CAPTION_LENGTH,
        text_counted_in=TextCount.CHARACTERS,
        # Ten of anything, counted together: a carousel can mix pictures and
        # video, and ten is the total. A post over that is caught here as
        # well, by whichever kind it is mostly made of.
        max_images=MOST_IN_A_CAROUSEL,
        max_videos=MOST_IN_A_CAROUSEL,
        # No file sizes. Nothing is ever uploaded from here, so we never see
        # a file to measure, and what Instagram will fetch is between it and
        # your web server.
        posts_left_today=posts_left_today,
    )


async def _posts_left_today(graph: Graph, account_id: str) -> int | None:
    """Ask Instagram how many posts are left in the last 24 hours.

    The number is asked for rather than written down here on purpose. Meta's
    own pages give it as 25, 50 and 100 in three different places, and it has
    moved more than once. Whatever it is today, Instagram knows and we ask.

    Args:
        graph: A conversation carrying the account's token.
        account_id: Which account to ask about.

    Returns:
        How many posts are left, or `None` when Instagram did not say in a
        way we could read.

    Raises:
        SocialChimpError: If Instagram refuses the question.
    """
    reply = await _ask(
        graph,
        "GET",
        f"/{account_id}/content_publishing_limit",
        params={"fields": "config,quota_usage"},
    )
    return quota_left(reply)


# ---------------------------------------------------------------------------
# Waiting for Instagram to finish
# ---------------------------------------------------------------------------


def _instagram_gave_up(container_id: str, reply: RawData) -> InvalidPostError:
    """Build the error for a container Instagram could not make.

    Args:
        container_id: The half-made post it gave up on.
        reply: What it said when asked how that was getting on.

    Returns:
        The error to raise, carrying Instagram's own words where it left any.
    """
    said = reply.get("status")
    detail = f" Instagram said: {said}" if isinstance(said, str) and said else ""
    message = (
        f"Instagram gave up while making this post (container "
        f"{container_id!r}), so nothing has been published. Almost always "
        f"the file: an address it could not reach, a picture too big or the "
        f"wrong shape, or a video that is not MP4 with H.264 video and AAC "
        f"audio.{detail}"
    )
    return InvalidPostError(message, platform=PLATFORM_NAME, raw=reply)


def _thrown_away(container_id: str, reply: RawData) -> PlatformError:
    """Build the error for a container that sat around too long.

    Args:
        container_id: The half-made post Instagram threw away.
        reply: What it said when asked how that was getting on.

    Returns:
        The error to raise.
    """
    message = (
        f"Instagram threw away the half-made post before it could be "
        f"published (container {container_id!r}). A container is only good "
        f"for 24 hours, and this one is older than that, so nothing has gone "
        f"out. Send the post again."
    )
    return PlatformError(message, platform=PLATFORM_NAME, raw=reply)


def _stopped_waiting(container_id: str, waited: float) -> PlatformError:
    """Build the error for a container that is still not ready.

    We stop looking eventually, and stopping is not the same as failing. The
    message says so twice, because an app that treats this as a failure and
    sends the post again is the way the same picture ends up on Instagram
    twice - and nobody can undo that from here.

    Args:
        container_id: The half-made post we gave up watching.
        waited: How many seconds we watched it for.

    Returns:
        The error to raise.
    """
    message = (
        f"Instagram was still working on this post {waited:.0f} seconds "
        f"after we sent it, so we have stopped watching. This is not the "
        f"same as it failing, and the post may still appear: Instagram "
        f"often finishes a video minutes after this point, and the half-made "
        f"post (container {container_id!r}) stays good for 24 hours, so it "
        f"can still be published with that id. Look at the account before "
        f"you send this post again. To wait longer, build the platform with "
        f"InstagramPlatform(wait_up_to_seconds=...)."
    )
    return PlatformError(message, platform=PLATFORM_NAME, raw={"id": container_id})


# ---------------------------------------------------------------------------
# Renewing
# ---------------------------------------------------------------------------


def _cannot_be_renewed(connection: Connection, refused: AuthError) -> TokenExpiredError:
    """Build the error for a token Instagram will not renew at all.

    Args:
        connection: The account whose token it is.
        refused: What Instagram answered.

    Returns:
        The error to raise.
    """
    message = (
        f"Instagram will not renew the token for {connection.id!r}. A token "
        f"that has gone sixty days without being renewed cannot be brought "
        f"back, and neither can one the person has taken away, so there is "
        f"nothing left to try - they have to connect their account again. "
        f"Renewing on a timer, well inside the sixty days, is what stops "
        f"this happening."
    )
    return TokenExpiredError(message, platform=PLATFORM_NAME, raw=refused.raw)


# ---------------------------------------------------------------------------
# Requests Instagram pushes to us
# ---------------------------------------------------------------------------


def _update_from(
    *,
    account_id: str,
    topic: str,
    value: RawData,
    when: datetime,
    envelope: RawData,
) -> Update:
    """Turn one change Instagram pushed into an update your app understands.

    Args:
        account_id: Which account it happened on.
        topic: What Instagram calls this kind of change, such as `"comments"`.
        value: What actually happened, in Meta's own words.
        when: When Meta says it happened.
        envelope: The whole untouched entry this came in.

    Returns:
        What happened, in socialchimp's own words. Anything we have no word
        for keeps Instagram's, and arrives as `UpdateKind.UNKNOWN`.
    """
    named = value.get("id") or value.get("comment_id") or value.get("media_id") or ""
    update_id = ":".join([account_id, topic, str(named) or str(int(when.timestamp()))])

    return Update.from_network(
        update_id=update_id,
        kind_name=_OUR_WORD_FOR.get(topic, topic),
        platform=PLATFORM_NAME,
        # Meta names the Instagram account, not one of your connections. A
        # login here names a connection after its account, so the two line up
        # without your app keeping a table of its own.
        connection_id=f"{PLATFORM_NAME}:{account_id}",
        created_at=when,
        # The change itself, so a handler reads `update.raw["text"]` rather
        # than walking the entry looking for its own change again. The entry
        # goes alongside, because the account id and the time are only out
        # there.
        raw=value,
        envelope=envelope,
    )


class InstagramPlatform:
    """Everything socialchimp does with Instagram, signed in on its own.

    Signing people in directly against Instagram - no Facebook Page, no
    Facebook Login - renewing their tokens for real, publishing a picture, a
    video or a carousel, and reading what Instagram pushes to you.

        instagram = InstagramPlatform()
        step = await instagram.start_login(request)

    It holds nothing between calls. Everything about an account arrives on
    the `Connection` and everything about your app on the `LoginRequest`, so
    one of these can be shared by your whole process.

    Attributes:
        name: `"instagram"`.
        features: What Instagram can do here. There is no text-only post on
            Instagram, no scheduling in its API and no way to delete, so
            `POST_TEXT`, `SCHEDULE` and `DELETE_POST` are all missing - and
            there is no app to register anywhere in Meta, so `CREATE_APP` is
            too.
    """

    name: str = PLATFORM_NAME

    features: Feature = Feature.POST_IMAGE | Feature.POST_VIDEO | Feature.PUSH_UPDATES

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        retries: Retries | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        check_every_seconds: float = HOW_OFTEN_TO_CHECK,
        wait_up_to_seconds: float = HOW_LONG_TO_WAIT,
    ) -> None:
        """Set Instagram up for one app.

        Args:
            timeout: Seconds to wait for Instagram to answer one request.
                This is per request, not for the whole of publishing - the
                waiting below has its own settings.
            retries: How many times to try again after a hiccup. Left out,
                the shared default is used.
            transport: Where requests actually go. Leave it out for ordinary
                calls; pass your own to send them somewhere else.
            check_every_seconds: How often to ask whether Instagram has
                finished making a video post. Once a minute is Meta's own
                advice; asking faster does not make it finish sooner.
            wait_up_to_seconds: How long to keep asking before giving up.
                Raise it if you post long video, and read `_stopped_waiting`
                first - giving up here does not mean the post failed.
        """
        self._timeout = timeout
        self._retries = retries
        self._transport = transport
        self._check_every = check_every_seconds
        self._wait_up_to = wait_up_to_seconds
        self._usage: Usage | None = None

    @property
    def usage(self) -> Usage | None:
        """How much of your app's hourly allowance Meta last said is gone.

        `None` until a reply mentions it. This is your whole app rather than
        one account, because that is how Meta counts. It is a different thing
        from `Limits.posts_left_today`, which is how many posts Instagram
        will take today.
        """
        return self._usage

    def _graph(self, token: str | None = None, *, at: str = IG_GRAPH_API) -> Graph:
        """Start a conversation with Instagram.

        Args:
            token: The token to sign requests with - the account's own while
                publishing, and none at all while swapping a code.
            at: Which host to talk to. The versioned one for ordinary
                requests; `IG_GRAPH_HOST` for the two addresses that make a
                token last and renew it, which carry no version.

        Returns:
            A conversation. Use it in an `async with` block so it closes
            itself.
        """
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        return Graph(
            HttpClient(
                at,
                platform=PLATFORM_NAME,
                headers=headers,
                timeout=self._timeout,
                transport=self._transport,
                retries=self._retries,
                errors=instagram_errors,
            ),
            platform=PLATFORM_NAME,
        )

    def _tokens(self) -> Graph:
        """Start a conversation with the addresses that make a token last and renew.

        Returns:
            A conversation pointed at `IG_GRAPH_HOST` rather than the
            versioned API, carrying no token of its own.
        """
        return self._graph(at=IG_GRAPH_HOST)

    def _codes(self) -> Graph:
        """Start a conversation with the address that swaps a code for a token.

        Returns:
            A conversation pointed at `IG_LOGIN_HOST`, on its own domain and
            carrying no token of its own.
        """
        return self._graph(at=IG_LOGIN_HOST)

    def _note(self, graph: Graph) -> None:
        """Keep whatever the last reply said about the allowance.

        Args:
            graph: The conversation that has just finished.
        """
        if graph.usage is not None:
            self._usage = graph.usage

    def api_base(self, connection: Connection) -> str:
        """Return where Instagram's API lives.

        Not `graph.facebook.com` - a token from this sign-in only works
        against Instagram's own host.

        Args:
            connection: The account we are about to act as. Not used here.

        Returns:
            The address, with no trailing slash.
        """
        return IG_GRAPH_API

    def auth_headers(self, connection: Connection) -> Mapping[str, str]:
        """Return the header that proves we may act as this account.

        The token is the account's own - there is no Facebook Page here, and
        so no Page token standing in for it the way the older flow needed.

        Meta also takes a token as an `access_token` query parameter, and
        this uses the header instead: a token in a web address ends up in
        server logs, proxy logs and browser history, and stays there.

        Args:
            connection: The account we are acting as.

        Returns:
            One `Authorization` header.
        """
        return {"Authorization": f"Bearer {connection.token.access_token}"}

    async def create_app(
        self,
        *,
        name: str,
        redirect_uri: str,
        host: str | None = None,
        scopes: tuple[str, ...] = (),
    ) -> AppCredentials:
        """Say, plainly, that Meta has no way to do this.

        This method exists only to give a useful answer. socialchimp reads
        `features` before calling anything and `Feature.CREATE_APP` is off,
        so nothing reaches here by accident - but somebody calling this
        platform directly deserves the address of the portal and a warning
        about the separate Instagram App ID, rather than an AttributeError.

        Args:
            name: Ignored.
            redirect_uri: Ignored.
            host: Ignored.
            scopes: Ignored.

        Returns:
            Nothing. It always raises.

        Raises:
            NotSupportedError: Always. The message names the portal, the
                Instagram App ID's own section of the dashboard, and the
                review.
        """
        raise _app_must_be_made_by_hand()

    async def limits(self, connection: Connection) -> Limits:
        """Return what Instagram allows this account right now.

        One request, to find out how many posts are left today. Everything
        else here is the same for every account, but that number is not: it
        counts down as you post and back up 24 hours later. Worth caching for
        a minute or two if you are about to check it repeatedly.

        Args:
            connection: The account to ask about.

        Returns:
            What Instagram allows. `posts_left_today` is `None` rather than
            a guess when Instagram's answer could not be read.

        Raises:
            ConfigError: If the connection names no Instagram account.
            SocialChimpError: If Instagram refuses the question.
        """
        account_id = _instagram_account_of(connection)

        async with self._graph(connection.token.access_token) as graph:
            try:
                return _what_it_allows(await _posts_left_today(graph, account_id))
            finally:
                self._note(graph)

    async def start_login(self, request: LoginRequest) -> SendToNetwork:
        """Build the address to send somebody to so they can approve your app.

        Nothing is sent to Instagram here. There is also nothing to remember
        between this call and the next: the swap at the end is signed with
        your app secret, which never leaves your server.

        Args:
            request: Where to send them back to, what to ask for, and your
                **Instagram** app's credentials.

        Returns:
            The address to redirect to, and the state that will come back.

        Raises:
            ConfigError: If the request carries no app credentials.
        """
        app = _app_or_refuse(request, what="start a sign-in")
        state = state_for(request)

        return SendToNetwork(
            url=sign_in_url(
                client_id=app.client_id,
                redirect_uri=request.redirect_uri,
                scopes=request.scopes or DEFAULT_SCOPES,
                state=state,
                # Instagram's own page, on its own domain. Facebook Login
                # will not sign anybody in to this product.
                page=SIGN_IN_PAGE,
            ),
            state=state,
        )

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> Finished:
        """Swap the code for a token, make it last, and read the account.

        Unlike the Facebook-linked flow this finishes the job outright.
        There is no Page and no set of accounts to choose between - signing
        in here is signing in as one Instagram account - so nothing here
        answers with `ChooseAccount`.

        Args:
            request: The same request used to start the login.
            callback: The query values Instagram sent back. It must have
                `code`; `state` is checked when it is there.
            remember: Not used. Nothing has to survive between the two calls
                here.

        Returns:
            The finished connection. Save it. Its token is good for sixty
            days and can be renewed for another sixty - see `refresh`.

        Raises:
            AuthError: If the person said no, if there is no code, if the
                state that came back is not the one we sent, or if Instagram
                will not make the swap - which is usually the app id.
            ConfigError: If the request carries no app credentials.
            SocialChimpError: If Instagram refuses for some other reason.
        """
        app = _app_or_refuse(request, what="finish a sign-in")
        check_state(request, callback, platform=PLATFORM_NAME)
        code = code_from(callback, platform=PLATFORM_NAME)

        async with self._codes() as graph:
            try:
                short, account_id = await self._swap(
                    graph, app, request.redirect_uri, code
                )
            finally:
                self._note(graph)

        async with self._tokens() as graph:
            try:
                # Traded now rather than later because the first token is
                # good for about an hour, and nothing can be done with an
                # expired one but sign the person in again.
                long = await self._make_it_last(graph, app, short.access_token)
            finally:
                self._note(graph)

        async with self._graph(long.access_token) as graph:
            profile = await graph.json(
                "GET", f"/{account_id}", params={"fields": ACCOUNT_FIELDS}
            )
            self._note(graph)

        username = profile.get("username")
        # An account always has a username, but showing the id is better
        # than showing nothing if one ever arrives without.
        name = username if isinstance(username, str) and username else account_id

        return Finished(
            connection=Connection(
                id=f"{PLATFORM_NAME}:{account_id}",
                platform=PLATFORM_NAME,
                host=None,
                account_id=account_id,
                account_name=name,
                token=long,
                scopes=request.scopes or DEFAULT_SCOPES,
                extra={
                    "instagram_id": account_id,
                    "username": name,
                    "profile_url": f"https://www.instagram.com/{name}",
                },
            )
        )

    async def _swap(
        self,
        graph: Graph,
        app: AppCredentials,
        redirect_uri: str,
        code: str,
    ) -> tuple[Token, str]:
        """Swap the code Instagram sent back for a token and the account's id.

        A POST with a form, on `IG_LOGIN_HOST` - its own host, separate from
        every other request in this file.

        Args:
            graph: A conversation with `IG_LOGIN_HOST`.
            app: Your **Instagram** app's id and secret.
            redirect_uri: The same address the sign-in was started with.
            code: What Instagram put on the end of your redirect address.

        Returns:
            A token that works for about an hour, and the account's id -
            Instagram hands both back on this one reply.

        Raises:
            AuthError: If Instagram will not make the swap, whatever code it
                used to say so.
        """
        try:
            reply = await graph.json(
                "POST",
                SWAP_PATH,
                data={
                    "client_id": app.client_id,
                    "client_secret": app.client_secret,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                    "code": code,
                },
            )
        except SocialChimpError as refused:
            # Whatever code Instagram used to say so: a wrong app id comes
            # back as several of them and never mentions the two pairs.
            raise _probably_the_wrong_app(refused) from refused

        token = token_from(reply, platform=PLATFORM_NAME, when="sign someone in")
        return token, _account_id_from(reply)

    async def _make_it_last(
        self,
        graph: Graph,
        app: AppCredentials,
        token: str,
    ) -> Token:
        """Trade an hour-long token for one that lasts sixty days.

        Args:
            graph: A conversation with `IG_GRAPH_HOST`.
            app: Your Instagram app's credentials. Only the secret is sent;
                Instagram works out the app from the token.
            token: The short-lived token to trade in.

        Returns:
            The long-lived token, good for about sixty days and renewable -
            see `refresh`.

        Raises:
            AuthError: If Instagram will not make the trade.
        """
        try:
            reply = await graph.json(
                "GET",
                MAKE_IT_LAST_PATH,
                params={
                    "grant_type": "ig_exchange_token",
                    "client_secret": app.client_secret,
                    "access_token": token,
                },
            )
        except SocialChimpError as refused:
            raise _probably_the_wrong_app(refused) from refused

        return token_from(reply, platform=PLATFORM_NAME, when="extend a token")

    async def refresh(
        self,
        connection: Connection,
        app: AppCredentials | None = None,
    ) -> Token:
        """Give the connection another sixty days, if it is worth asking yet.

        This is a real renewal, the same shape as Threads': one request, no
        app secret, another sixty days. What Instagram's own documentation
        does not say is how young a token can be and still be renewed, so
        rather than guess this uses a rule of its own - see
        `REFRESH_AFTER_SECONDS`. Called while more than thirty days remain,
        nothing is sent and you get back the token you already had.

        Args:
            connection: The account whose token may be running out.
            app: Your app's id and secret. Accepted because every platform's
                `refresh` is, and not sent - Instagram works out the app from
                the token itself.

        Returns:
            The token to save: the same one, unchanged, if it still has
            plenty of life left, or a fresh one good for another sixty days.

        Raises:
            TokenExpiredError: If Instagram will not renew it, which means it
                has already gone sixty days without renewal or the person has
                taken your app's access away. Either way they have to connect
                their account again.
            SocialChimpError: If Instagram refused for some other reason.
        """
        expires_at = connection.token.expires_at
        if expires_at is not None:
            still_good_for = (expires_at - _now()).total_seconds()
            if still_good_for > REFRESH_AFTER_SECONDS:
                return connection.token

        async with self._tokens() as graph:
            try:
                reply = await graph.json(
                    "GET",
                    RENEW_PATH,
                    params={
                        "grant_type": "ig_refresh_token",
                        "access_token": connection.token.access_token,
                    },
                )
            except AuthError as refused:
                raise _cannot_be_renewed(connection, refused) from refused
            finally:
                self._note(graph)

        return token_from(reply, platform=PLATFORM_NAME, when="renew a token")

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        """Publish a post: build it, wait for it, then put it out.

        One picture or video goes out on its own; two to ten go out as a
        carousel. Where there is video, this waits for Instagram to finish
        making the post before publishing it, which is why publishing a video
        can take minutes rather than a moment.

        Args:
            connection: The account to publish as.
            post: What to publish. Its text becomes the caption, and every
                attachment has to be a `Media.from_url`.

        Returns:
            What Instagram said about the new post, always `PostState.DONE` -
            the waiting happens in here, so a result that comes back at all
            is a post that is live. There is no link on it: Instagram's id
            for a post is not its web address, and the address uses a short
            code that only another request would tell us. Ask for it with
            `GET /{id}?fields=permalink` if you need it.

        Raises:
            ConfigError: If the connection names no Instagram account.
            InvalidPostError: If the post breaks one of Instagram's limits,
                if a setting is unknown, or if Instagram gave up making it.
            NotSupportedError: If the post is words alone, or carries a
                file Instagram would have to be sent rather than fetch.
            PlatformError: If Instagram was still working when we stopped
                watching. That is not a failure - see `_stopped_waiting`.
            SocialChimpError: If Instagram refuses any of the three steps.
        """
        account_id = _instagram_account_of(connection)

        # Everything that can be judged without asking Instagram is judged
        # first, so a mistake costs no request and no part of the hourly
        # allowance.
        as_carousel = _checked_options(post.options)
        check_post(
            post,
            platform=PLATFORM_NAME,
            features=self.features,
            limits=_what_it_allows(),
            words_alone_advice=WORDS_ALONE_ADVICE,
        )
        _check_hashtags(post.text)
        things = _things_to_publish(post)

        if as_carousel and len(things) < FEWEST_IN_A_CAROUSEL:
            message = (
                f"A carousel needs between {FEWEST_IN_A_CAROUSEL} and "
                f"{MOST_IN_A_CAROUSEL} pictures or videos, and this post has "
                f"{len(things)}. Leave the carousel option out and it goes "
                f"out as an ordinary post instead."
            )
            raise InvalidPostError(message)

        # Two or more attachments are a carousel whether or not anybody said
        # so, because Instagram has no other way to carry them.
        as_carousel = as_carousel or len(things) > 1

        async with self._graph(connection.token.access_token) as graph:
            try:
                left = await _posts_left_today(graph, account_id)
                # The daily allowance is the one rule we cannot know without
                # asking, so it is checked here rather than above, now that
                # we have the number.
                check_post(
                    post,
                    platform=PLATFORM_NAME,
                    features=self.features,
                    limits=_what_it_allows(left),
                )

                container = await self._build(
                    graph,
                    account_id,
                    post,
                    things,
                    as_carousel=as_carousel,
                )
                return await self._put_it_out(graph, account_id, container)
            finally:
                self._note(graph)

    async def _build(
        self,
        graph: Graph,
        account_id: str,
        post: Post,
        things: tuple[_Attachment, ...],
        *,
        as_carousel: bool,
    ) -> str:
        """Make the half-finished post Instagram will publish, and wait for it.

        Args:
            graph: A conversation signed with the account's own token.
            account_id: Which Instagram account.
            post: What to publish, whose text becomes the caption.
            things: The pictures and videos, already checked.
            as_carousel: Whether these go out as one carousel.

        Returns:
            The container id to publish.

        Raises:
            InvalidPostError: If Instagram gave up making any part of it.
            PlatformError: If it was still working when we stopped watching.
            SocialChimpError: If Instagram refuses one of the requests.
        """
        if not as_carousel:
            only = things[0]
            container = await self._start_one(
                graph, account_id, only, caption=post.text, in_a_carousel=False
            )
            await self._wait_for(graph, container, video=only.kind is MediaKind.VIDEO)
            return container

        children: list[str] = []
        for item in things:
            child = await self._start_one(
                graph, account_id, item, caption=None, in_a_carousel=True
            )
            # Each one has to be finished before the parent can name it.
            await self._wait_for(graph, child, video=item.kind is MediaKind.VIDEO)
            children.append(child)

        reply = await _ask(
            graph,
            "POST",
            f"/{account_id}/media",
            data={
                "caption": post.text,
                "media_type": "CAROUSEL",
                "children": ",".join(children),
            },
        )
        parent = required_text(
            reply, "id", platform=PLATFORM_NAME, when="start a carousel"
        )

        # The parent is a container of its own, and is as capable of not being
        # ready as anything in it - pictures only included.
        await self._wait_for(
            graph,
            parent,
            video=any(item.kind is MediaKind.VIDEO for item in things),
        )
        return parent

    async def _start_one(
        self,
        graph: Graph,
        account_id: str,
        item: _Attachment,
        *,
        caption: str | None,
        in_a_carousel: bool,
    ) -> str:
        """Ask Instagram to start making one picture or video into a post.

        Nothing is uploaded here. Instagram is given the address and goes and
        fetches the file itself, which is why this can come back long before
        the post is ready.

        Args:
            graph: A conversation signed with the account's own token.
            account_id: Which Instagram account.
            item: The picture or video.
            caption: The words to put on it, or `None` for a carousel item -
                there the caption belongs to the carousel, not its pieces.
            in_a_carousel: Whether this is one piece of a carousel.

        Returns:
            The container id.

        Raises:
            PlatformError: If Instagram answered without an id.
            SocialChimpError: If Instagram refuses.
        """
        form: dict[str, str] = {}
        if caption is not None:
            form["caption"] = caption

        if item.kind is MediaKind.VIDEO:
            # A video on its own is a reel - Instagram retired the plain
            # video post and takes nothing else now - but a video inside a
            # carousel is not one, and asking for REELS there is refused.
            form["media_type"] = "VIDEO" if in_a_carousel else "REELS"
            form["video_url"] = item.url
        else:
            form["image_url"] = item.url
            # Instagram takes alt text on a picture and nowhere else - not on
            # a reel, not on a story - so it is only sent where it is read.
            if item.alt_text:
                form["alt_text"] = item.alt_text

        if in_a_carousel:
            form["is_carousel_item"] = "true"

        reply = await _ask(graph, "POST", f"/{account_id}/media", data=form)
        return required_text(reply, "id", platform=PLATFORM_NAME, when="start a post")

    async def _wait_for(
        self,
        graph: Graph,
        container_id: str,
        *,
        video: bool,
    ) -> None:
        """Keep asking whether Instagram has finished making a post.

        Everything goes through here - a picture, every piece of a carousel
        and the carousel itself - because Instagram answers the request that
        starts one long before it has finished it. A picture asked about
        straight away can still be unready: `media_publish` then fails with
        error 9007, a quarter of a second after the container was made.

        The first look is always immediate, so a container that is already
        ready costs one request and no waiting. After that the pace depends
        on what is being made. A picture is looked at again after a second,
        then two, doubling up to `PICTURE_LONGEST_WAIT`. A video is
        re-encoded, which takes minutes, so it is looked at every
        `check_every_seconds`, as Meta's guide advises.

        A picture whose reply says nothing at all about `status_code` is
        taken to be one Instagram does not track, and is published rather than
        waited on; if it turns out not to be ready, `_put_it_out` copes.

        Args:
            graph: A conversation signed with the account's own token.
            container_id: The half-made post to watch.
            video: Whether it is, or holds, a video.

        Raises:
            InvalidPostError: If Instagram gave up on it.
            PlatformError: If Instagram threw it away, or if it is still not
                ready when we stop watching.
            SocialChimpError: If Instagram refuses the question.
        """
        give_up_at = _now() + timedelta(seconds=self._wait_up_to)
        wait = self._check_every if video else PICTURE_FIRST_WAIT

        while True:
            reply = await _ask(
                graph,
                "GET",
                f"/{container_id}",
                # `status` is the sentence a person can read; `status_code` is
                # the word we branch on.
                params={"fields": "status_code,status"},
            )
            if not video and "status_code" not in reply:
                return
            said = str(reply.get("status_code", ""))

            if said == _FINISHED:
                return
            if said == _GAVE_UP:
                raise _instagram_gave_up(container_id, reply)
            if said == _THROWN_AWAY:
                raise _thrown_away(container_id, reply)

            # Anything else - IN_PROGRESS, or a word Meta adds next year -
            # means carry on looking. Guessing at a word we do not know would
            # either publish something half-made or throw away a good post.
            if _now() >= give_up_at:
                raise _stopped_waiting(container_id, self._wait_up_to)
            await _sleep(wait)
            if not video:
                wait = min(wait * 2, PICTURE_LONGEST_WAIT)

    async def _put_it_out(
        self,
        graph: Graph,
        account_id: str,
        container_id: str,
    ) -> PostResult:
        """Publish a container that Instagram has finished making.

        Instagram can still say "not ready" here (error 9007) even after a
        container reported `FINISHED`. That refusal means the post was fine
        and nothing was published, so asking again is safe and cannot make a
        second post. It is asked again `TIMES_TO_PUBLISH_AGAIN` times, after
        `WAIT_BEFORE_PUBLISHING_AGAIN` seconds each, and then handed on. No
        other refusal is asked again - not even another `RateLimitError`,
        which means the hour's allowance is gone and seconds will not fix it.

        Args:
            graph: A conversation signed with the account's own token.
            account_id: Which Instagram account.
            container_id: The finished half-made post.

        Returns:
            What Instagram said about the new post.

        Raises:
            RateLimitError: If Instagram is still not ready after every try.
            PlatformError: If Instagram answered without an id.
            SocialChimpError: If Instagram refuses.
        """
        tries_left = TIMES_TO_PUBLISH_AGAIN
        while True:
            try:
                reply = await _ask(
                    graph,
                    "POST",
                    f"/{account_id}/media_publish",
                    data={"creation_id": container_id},
                )
            except RateLimitError as refused:
                if tries_left == 0 or not _is_not_ready(refused.raw.get("error")):
                    raise
                tries_left -= 1
                await _sleep(WAIT_BEFORE_PUBLISHING_AGAIN)
                continue
            break

        return PostResult(
            id=required_text(
                reply, "id", platform=PLATFORM_NAME, when="publish a post"
            ),
            # Instagram's id for a post is not its web address: the address
            # uses a short code, and only another request would tell us it.
            url=None,
            state=PostState.DONE,
            raw=reply,
        )

    def check_signature(
        self,
        body: bytes,
        headers: Mapping[str, str],
        *,
        secret: str,
    ) -> None:
        """Check a request Instagram pushed to us really came from Instagram.

        The signature covers the **raw bytes** of the body. A framework that
        parses the JSON and builds it again first changes the spacing and the
        key order, and this then fails on a request that was perfectly good.
        Read the body, check it here, and parse it afterwards.

        Args:
            body: The request body, exactly as it arrived.
            headers: The request headers.
            secret: Your **Instagram app secret** from the developer portal -
                the one from Instagram API setup with Instagram login, not
                any Facebook app's, and not the verify token you typed into
                the webhook form.

        Raises:
            SignatureError: If the request cannot be trusted. Answer 401 and
                do nothing else with it.
        """
        check_meta_signature(body, headers, secret=secret)

    def answer_setup_check(
        self,
        params: Mapping[str, str],
        *,
        verify_token: str,
    ) -> str:
        """Answer the one-off question Meta asks before it sends anything.

        Point Meta at a URL of yours and it does a GET to it first, with a
        token you chose and a challenge. Echo the challenge back as plain
        text and the URL starts working. Get it wrong and Meta says the URL
        could not be verified, without saying why.

        The topics worth subscribing to for Instagram are `comments`,
        `mentions`, `live_comments` and `story_insights`.

        Args:
            params: The query values from that GET, such as Django's
                `request.GET` or FastAPI's `request.query_params`.
            verify_token: The token you typed into Meta's webhook form.

        Returns:
            The challenge. Send it back as the whole body, with a 200 and a
            content type of `text/plain`.

        Raises:
            SignatureError: If this is not a setup check, or the token is
                wrong. Answer 403 and send nothing back.
        """
        return echo_the_challenge(params, expected_token=verify_token)

    def read_updates(self, body: bytes) -> list[Update]:
        """Turn a checked request into every update it carries.

        Instagram batches when it is busy, which is exactly when you least
        want to drop the rest, so this hands back all of them.

        Args:
            body: The request body, untouched. Check its signature first.

        Returns:
            What happened, in the order Instagram listed it. Empty when the
            message carried nothing we can act on.

        Raises:
            PlatformError: If the body is not one of Meta's messages.
        """
        found: list[Update] = []
        for change in changes_in(body, platform=PLATFORM_NAME):
            found.append(
                _update_from(
                    account_id=change.account_id,
                    topic=change.topic,
                    value=change.value,
                    when=change.when,
                    envelope=change.envelope,
                )
            )
        return found

    def read_update(
        self,
        body: bytes,
        headers: Mapping[str, str],
    ) -> Update:
        """Turn a checked request into one update your app understands.

        One message from Instagram can carry several changes, and this hands
        back the first of them. Use `read_updates` to see them all - on a
        busy account that is the one you want.

        Only call this after `check_signature` has passed.

        Args:
            body: The request body, untouched.
            headers: The request headers. Not needed here; the signature
                header has already done its job by this point.

        Returns:
            What happened, in socialchimp's own words.

        Raises:
            PlatformError: If the body is not one of Meta's messages, or
                carries no change at all.
        """
        return first_update(self.read_updates(body), platform=PLATFORM_NAME)
