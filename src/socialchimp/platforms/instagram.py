"""Instagram: the first network where publishing is not one call.

Everywhere socialchimp has been so far, you send a post and it is up. Here
you build the post, wait for Instagram to finish making it, and then publish
it - three calls where there was one. That shape is why `PostResult` carries
a state at all, and Threads and TikTok work the same way.

The parts of this that are really parts of Meta - the sign-in, the token
swap, the error codes, the signature on a pushed request - live in
`_meta.py`, shared with Facebook. Only what is genuinely Instagram's is here.

## Only a Business or Creator account can post

This is the surprise that catches nearly everybody. A personal Instagram
account cannot be posted to through any API, by anyone, ever - not with the
right permissions, not with a reviewed app. The account has to be a
**Business or Creator** account, and it has to be linked to a Facebook Page.

Both of those are free and take a minute in the Instagram app, under
Settings, Account type and tools. Until they are done, the account simply
does not appear when we ask Meta which accounts a person can post to, so
`finish_login` says exactly that rather than letting a confusing permission
error turn up later.

## You have to make the app by hand, and wait

There is no `create_app` here, and `Feature.CREATE_APP` is off. Meta has no
call for it. You:

1. Create the app yourself at https://developers.facebook.com/apps.
2. Wait for Meta to review it.
3. Get your business verified, which means sending Meta documents about the
   company behind the app.

Until steps 2 and 3 are done, everything works for people who have a role on
the app in the portal and fails for everybody else.

The permissions are also not the ones written in most tutorials.
`instagram_basic` and `instagram_content_publish` stopped working in January
2025 and were replaced by `instagram_business_basic` and
`instagram_business_content_publish`. See `DEFAULT_SCOPES`.

## Signing someone in takes three steps

The same shape as Facebook, because it is the same sign-in:

1. `start_login` gives you an address. Send the person's browser there.
2. They come back with a code. `finish_login` swaps it for a token, makes
   that token last, asks Meta which of their Pages have an Instagram
   business account attached, and answers with `ChooseAccount`.
3. When they pick one, `resume_login` gives you the connection to save.

A Page with no Instagram account on it is left out of the list, because
picking it could never lead to a post. If none of them have one, that is the
Business-or-Creator problem above and the refusal says so.

The token saved on the connection is the **Page's** token, and a Page token
taken from a long-lived person's token does not expire. That is why
`refresh` usually has nothing to do.

## Publishing, step by step

    1. POST /{account}/media          -> a container id
    2. GET  /{container}?fields=...   -> wait until it says FINISHED
    3. POST /{account}/media_publish  -> the post is live

A **container** is Instagram's word for a half-made post: it has your file
and your caption, and nobody can see it. Step 3 is what turns one into a
post. A container is thrown away after 24 hours if nothing publishes it.

Step 2 only happens where it is needed. Instagram has to fetch and re-encode
a **video**, which takes anywhere from seconds to minutes, so we look once a
minute for up to five - Meta's own advice - and both numbers are settings.
A picture is ready by the time Instagram answers step 1, so checking it
would cost a request against your hourly allowance and tell you nothing; if
a picture cannot be fetched, step 3 says so instead.

A carousel is the same three steps with more of the first: every picture or
video becomes its own container, then a parent container names them all, then
the parent is published. Two to ten items.

**If the waiting runs out we say we do not know.** Instagram often finishes a
minute after we have stopped looking, and telling somebody their post failed
when it is about to go live is the worse mistake of the two.

## Instagram fetches your file; it will not take an upload

There is no upload here at all. You give Instagram a web address and it goes
and gets the file itself, which means:

- `Media.from_url("https://...")` works, and is the only thing that does.
- `Media.from_file` and `Media.from_bytes` are **refused**, with a message
  saying to put the file somewhere public first. Pretending to upload by
  quietly hosting the file somewhere would be a worse answer than a clear no.

Because no bytes ever pass through us, `Media.size` and `Media.piece` do not
come into it, and neither does a file-size limit of ours - what Instagram
will accept is between Instagram and your web server.

## How many posts are left today

Instagram counts posts over a rolling 24 hours and will tell you how many are
left: `GET /{account}/content_publishing_limit`. That number is **read, never
written down**, because Meta's own documentation gives it as 25, 50 and 100
in three different places. Whatever it really is today, Instagram knows and
we ask. It lands on `Limits.posts_left_today`, and `check_post` refuses when
there are none left.

## What Instagram cannot do here

- **No text-only post.** `Feature.POST_TEXT` is off. Every post is a picture
  or a video, so a post with neither is refused rather than turned into
  something else.
- **No scheduling.** `Feature.SCHEDULE` is off. Instagram's own app can
  schedule; its API cannot, and a post with `publish_at` is refused rather
  than quietly going out now.
- **No deleting.** There is no call for it. Someone has to remove the post in
  the Instagram app.
- **No reading posts back yet.** `GET /{account}/media` exists and this does
  not use it. Insights need a Business account, and some of the numbers need
  a hundred followers, which is worth knowing before you build on them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import anyio
import httpx

from socialchimp.errors import (
    AuthError,
    InvalidPostError,
    NotSupportedError,
    PlatformError,
    TokenExpiredError,
)
from socialchimp.events import Update
from socialchimp.events import answer_setup_check as echo_the_challenge
from socialchimp.features import Feature, Limits, TextCount, check_post
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
from socialchimp.platform import (
    AccountChoice,
    ChooseAccount,
    Finished,
    LoginRequest,
    SendToNetwork,
)
from socialchimp.platforms._meta import (
    GRAPH_API,
    Change,
    Graph,
    MetaPage,
    Usage,
    app_must_be_made_by_hand,
    changes_in,
    check_meta_signature,
    check_state,
    code_from,
    credentials_or_refuse,
    first_update,
    long_lived_token,
    meta_errors,
    pages_of,
    quota_left,
    required_text,
    sign_in_url,
    state_for,
    swap_code_for_token,
    where_to_post,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from socialchimp.errors import SocialChimpError
    from socialchimp.http import Retries
    from socialchimp.models import AppCredentials

__all__ = ["InstagramPlatform", "instagram_errors"]

PLATFORM_NAME: Final = "instagram"

DEFAULT_SCOPES: Final = (
    "instagram_business_basic",
    "instagram_business_content_publish",
    "instagram_business_manage_comments",
    "pages_show_list",
    "business_management",
)
"""The permissions posting to an Instagram business account needs.

- `instagram_business_basic` - read the account: who it is, what it has
  posted.
- `instagram_business_content_publish` - make and publish a post.
- `instagram_business_manage_comments` - read the comments Instagram pushes
  to you, and answer them.
- `pages_show_list` - see the Pages the person manages, which is how we find
  the Instagram account attached to one.
- `business_management` - needed for accounts owned by a business rather than
  by a person, which is most accounts worth posting to.

The first three used to be called `instagram_basic`,
`instagram_content_publish` and `instagram_manage_comments`. **Those names
stopped working in January 2025** and a sign-in that asks for them is refused
outright, which is worth knowing because most of the tutorials still use
them.

Every one of these needs Meta's review before it works for anybody but you.
"""

ACCOUNT_FIELDS: Final = "id,name,access_token,instagram_business_account{id,username}"
"""What to ask Meta about each Page a person manages.

The last part is the one that matters: `instagram_business_account` is only
there when the Page has an Instagram Business or Creator account linked to
it. A Page without it is a Page we can never post to.
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

    Kept as its own function so tests can say how long the waiting took
    without sitting through it.

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
    are the same on all three of its networks. See `_meta.meta_errors` for
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
        path: Joined onto Meta's address.
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
        # codes all three networks share. Those do not include Instagram's
        # own, so a 9004 arriving that way comes out as "no better name for
        # that code yet" unless we look again here. Anything Meta could name
        # is already a different class of error and never reaches this.
        better = _instagram_error(refused.raw)
        if better is None:
            raise
        raise better from refused


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _InstagramAccount:
    """One Instagram business account, and the Page token to act as it.

    Attributes:
        id: Instagram's identifier for the account. This is what a post is
            addressed to, and what a pushed update names.
        username: The @name, without the @. What to show a person.
        page_id: The Facebook Page it is attached to.
        page_name: What that Page is called.
        token: The Page's token, which is what posting uses. Not the
            person's. Kept out of `repr` so it does not reach a log.
    """

    id: str
    username: str
    page_id: str
    page_name: str
    token: str = field(repr=False)


def _account_on(page: MetaPage) -> _InstagramAccount | None:
    """Read the Instagram account attached to one Page, if there is one.

    Args:
        page: One Page, as Meta described it.

    Returns:
        The account, or `None` when this Page has none. A Page with no
        Instagram account on it can never be posted to, so offering it would
        mean somebody picks it and the sign-in fails at the last step.
    """
    linked = page.raw.get("instagram_business_account")
    if not isinstance(linked, dict):
        return None

    account_id = linked.get("id")
    if not (isinstance(account_id, str) and account_id):
        return None

    username = linked.get("username")
    return _InstagramAccount(
        id=account_id,
        # An account always has a username, but showing the id is better than
        # showing nothing if one ever arrives without.
        username=username if isinstance(username, str) and username else account_id,
        page_id=page.id,
        page_name=page.name,
        token=page.token,
    )


async def _accounts_of(graph: Graph) -> tuple[_InstagramAccount, ...]:
    """List the Instagram accounts one person can post to.

    Instagram has no "which accounts do you have" call of its own. You ask
    Meta which Pages the person manages and which of those have an Instagram
    account attached, which is why signing in needs `pages_show_list`.

    Args:
        graph: A conversation carrying that person's token.

    Returns:
        The accounts, in Meta's own order, leaving out every Page that has no
        Instagram account on it.

    Raises:
        SocialChimpError: If Meta refuses to list the Pages.
    """
    pages = await pages_of(graph, fields=ACCOUNT_FIELDS)
    found = (_account_on(page) for page in pages)
    return tuple(account for account in found if account is not None)


def _nobody_can_post() -> AuthError:
    """Build the error for a person with no Instagram account we can use.

    This is the single most common surprise on Instagram, so the message says
    all of it rather than leaving somebody to work it out from a permission
    error later.

    Returns:
        The error to raise.
    """
    return AuthError(
        "This person signed in, but none of their Facebook Pages has an "
        "Instagram account we can post to. Posting through any Instagram API "
        "needs two things that are free and take a minute each in the "
        "Instagram app: the account has to be a Business or Creator account "
        "rather than a personal one, and it has to be linked to a Facebook "
        "Page. A personal account cannot be posted to by anybody, however "
        "the app is set up. Both are under Settings, then Account type and "
        "tools. Once that is done, ask them to connect their account again - "
        "and to tick the Page on Meta's own picker while they do.",
        platform=PLATFORM_NAME,
    )


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
    for key, value in options.items():
        if key not in POST_OPTIONS:
            message = (
                f"Instagram does not know the post option {key!r}. It "
                f"accepts: {', '.join(POST_OPTIONS)}."
            )
            raise InvalidPostError(message)
        if not isinstance(value, bool):
            message = (
                f"{key} is {value!r}, but it has to be True or False. "
                f"True sends the post as a carousel; leaving it out sends "
                f"two or more attachments as one anyway."
            )
            raise InvalidPostError(message)
    return bool(options.get("carousel", False))


def _needs_a_web_address() -> NotSupportedError:
    """Build the error for a file we were handed the bytes of.

    Returns:
        The error to raise, saying what to do instead.
    """
    return NotSupportedError(
        platform=PLATFORM_NAME,
        what=(
            "being sent a file. It fetches every picture and video itself, "
            "from a web address, and has no upload of any kind - so "
            "Media.from_file and Media.from_bytes cannot be published here. "
            "Put the file somewhere the public internet can reach it, such "
            "as object storage with a public link or your own web server, "
            "and use Media.from_url(...) instead. socialchimp will not "
            "quietly host it for you, because a file that appeared somewhere "
            "you did not choose is a worse surprise than this message"
        ),
    )


def _things_to_publish(post: Post) -> tuple[_Attachment, ...]:
    """Work out what this post is actually made of, and refuse it if we cannot.

    Args:
        post: The post about to be sent.

    Returns:
        The pictures and videos, in the order they were given.

    Raises:
        NotSupportedError: If the post has nothing attached, or a file we
            would have to upload.
        InvalidPostError: If there are more attachments than fit in one post.
    """
    if not post.media:
        raise NotSupportedError(
            platform=PLATFORM_NAME,
            what=(
                "posting words on their own. Every Instagram post carries a "
                "picture or a video - there is no text-only post on it at "
                "all - so attach one with Media.from_url(...) and your words "
                "become its caption"
            ),
        )

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
            it to nobody, which is far harder to notice than an error.
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
    moved more than once. Whatever it is today, Instagram knows.

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
# Requests Instagram pushes to us
# ---------------------------------------------------------------------------


def _update_id(change: Change) -> str:
    """Build an id that is the same every time this change arrives.

    Meta puts no identifier of its own on a change, and promises to deliver
    at least once - which is a promise to deliver twice sometimes. Without
    something stable here, one comment gets answered twice.

    Args:
        change: The change to name.

    Returns:
        An id built only from what Instagram said, so a second delivery of
        the same change produces the same one.
    """
    value = change.value
    named = value.get("id") or value.get("comment_id") or value.get("media_id") or ""
    return ":".join(
        [
            change.account_id,
            change.topic,
            str(named) or str(int(change.when.timestamp())),
        ]
    )


def _update_from(change: Change) -> Update:
    """Turn one change Instagram pushed into an update your app understands.

    Args:
        change: One change, already unwrapped from Meta's envelope.

    Returns:
        What happened, in socialchimp's own words. Anything we have no word
        for keeps Instagram's, and arrives as `UpdateKind.UNKNOWN`.
    """
    return Update.from_network(
        update_id=_update_id(change),
        kind_name=_OUR_WORD_FOR.get(change.topic, change.topic),
        platform=PLATFORM_NAME,
        # Meta names the Instagram account, not one of your connections. A
        # login here names a connection after its account, so the two line up
        # without your app keeping a table of its own.
        connection_id=f"{PLATFORM_NAME}:{change.account_id}",
        # Instagram puts no time on the change itself, only on the batch it
        # arrived in, so that is the closest we have to when it happened.
        created_at=change.when,
        raw=change.raw,
    )


class InstagramPlatform:
    """Everything socialchimp does with Instagram.

    Signing people in, asking which of their Instagram accounts to use,
    publishing a picture, a video or a carousel, and reading what Instagram
    pushes to you.

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

    def _graph(self, token: str | None = None) -> Graph:
        """Start a conversation with Instagram.

        Args:
            token: The token to sign requests with - the Page's for posting,
                the person's while signing in, and none at all while swapping
                a code.

        Returns:
            A conversation. Use it in an `async with` block so it closes
            itself.
        """
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        return Graph(
            HttpClient(
                GRAPH_API,
                platform=PLATFORM_NAME,
                headers=headers,
                timeout=self._timeout,
                transport=self._transport,
                retries=self._retries,
                errors=instagram_errors,
            ),
            platform=PLATFORM_NAME,
        )

    def _note(self, graph: Graph) -> None:
        """Keep whatever the last reply said about the allowance.

        Args:
            graph: The conversation that has just finished.
        """
        if graph.usage is not None:
            self._usage = graph.usage

    def api_base(self, connection: Connection) -> str:
        """Return where Instagram's API lives.

        It is Meta's own address: Instagram has no API of its own here, it is
        a part of the Graph API. One address for everybody, unlike Mastodon.

        Args:
            connection: The account we are about to act as. Not used here.

        Returns:
            The address, with no trailing slash.
        """
        return GRAPH_API

    def auth_headers(self, connection: Connection) -> Mapping[str, str]:
        """Return the header that proves we may act as this account.

        The token is the Facebook Page's, not the Instagram account's -
        Instagram issues none of its own on this route.

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
        about the review, rather than an AttributeError.

        Args:
            name: Ignored.
            redirect_uri: Ignored.
            host: Ignored.
            scopes: Ignored.

        Returns:
            Nothing. It always raises.

        Raises:
            NotSupportedError: Always. The message names the portal, the app
                review and the business verification.
        """
        raise app_must_be_made_by_hand(PLATFORM_NAME)

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

        Nothing is sent to Meta here. There is also nothing to remember
        between this call and the next: the swap at the end is signed with
        your app secret, which never leaves your server.

        Args:
            request: Where to send them back to, what to ask for, and your
                app's credentials.

        Returns:
            The address to redirect to, and the state that will come back.

        Raises:
            ConfigError: If the request carries no app credentials.
        """
        app = credentials_or_refuse(
            request.app,
            platform=PLATFORM_NAME,
            what="start a sign-in",
        )
        state = state_for(request)

        return SendToNetwork(
            url=sign_in_url(
                client_id=app.client_id,
                redirect_uri=request.redirect_uri,
                scopes=request.scopes or DEFAULT_SCOPES,
                state=state,
            ),
            state=state,
        )

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> ChooseAccount:
        """Swap the code for a token and ask which Instagram account to use.

        Three things happen here. The code becomes a token that lasts an
        hour; that token is traded for one that lasts about sixty days, which
        has to happen while the first still works; and Meta is asked which of
        this person's Pages have an Instagram account attached.

        This never finishes a login on its own. Even somebody with a single
        account is asked, so your app has one path through this rather than
        two.

        Args:
            request: The same request used to start the login.
            callback: The query values Meta sent back. It must have `code`;
                `state` is checked when it is there.
            remember: Not used. Nothing has to survive between the two calls
                here.

        Returns:
            The accounts to choose between, and a `resume_token` to hand back
            to `resume_login`. That token is the person's own - keep it in
            their session, not in a URL.

        Raises:
            AuthError: If the person said no, if there is no code, if the
                state that came back is not the one we sent, or if none of
                their Pages has an Instagram account we can post to.
            ConfigError: If the request carries no app credentials.
            SocialChimpError: If Meta refuses any of the three steps.
        """
        app = credentials_or_refuse(
            request.app,
            platform=PLATFORM_NAME,
            what="finish a sign-in",
        )
        check_state(request, callback, platform=PLATFORM_NAME)
        code = code_from(callback, platform=PLATFORM_NAME)

        async with self._graph() as graph:
            short = await swap_code_for_token(
                graph,
                client_id=app.client_id,
                client_secret=app.client_secret,
                redirect_uri=request.redirect_uri,
                code=code,
            )
            # Traded now rather than later because there is no later: Meta
            # gives out no refresh token, and once the hour is up the only
            # way back is to sign the person in again.
            long = await long_lived_token(
                graph,
                client_id=app.client_id,
                client_secret=app.client_secret,
                token=short.access_token,
            )
            self._note(graph)

        async with self._graph(long.access_token) as graph:
            accounts = await _accounts_of(graph)
            self._note(graph)

        if not accounts:
            raise _nobody_can_post()

        return ChooseAccount(
            options=tuple(
                AccountChoice(
                    id=account.id,
                    name=account.username,
                    kind="instagram_account",
                )
                for account in accounts
            ),
            resume_token=long.access_token,
        )

    async def resume_login(
        self,
        request: LoginRequest,
        *,
        resume_token: str,
        account_id: str,
        remember: RawData | None = None,
    ) -> Finished:
        """Finish the login with the account the person picked.

        The accounts are looked up again rather than remembered, so one that
        has been unlinked since the list was shown is caught here with a
        message rather than saved as a connection that cannot post.

        Args:
            request: The same request the login was started with.
            resume_token: The value from `ChooseAccount`. It carries the
                person's own token.
            account_id: The id of the Instagram account they picked.
            remember: Not used.

        Returns:
            The finished connection. Save it. Its token is the Facebook
            Page's, and that one does not expire.

        Raises:
            AuthError: If the resume token did not come back, or that account
                is no longer one this person can post to.
            SocialChimpError: If Meta refuses the lookup.
        """
        if not resume_token:
            message = (
                "This sign-in cannot be carried on because the resume_token "
                "from ChooseAccount did not come back. Keep it with that "
                "person's session and pass it to resume_login. Without it "
                "there is no way to ask Meta for the account's token, so "
                "start a new one."
            )
            raise AuthError(message, platform=PLATFORM_NAME)

        async with self._graph(resume_token) as graph:
            accounts = await _accounts_of(graph)
            self._note(graph)

        picked = next(
            (account for account in accounts if account.id == account_id), None
        )
        if picked is None:
            message = (
                f"Instagram account {account_id!r} is not one this person can "
                f"post to. Either it was never on the list, or it has been "
                f"unlinked from its Facebook Page since they picked it. Show "
                f"them the list again by starting the sign-in again."
            )
            raise AuthError(message, platform=PLATFORM_NAME)

        return Finished(
            connection=Connection(
                # Named after the Instagram account rather than the person or
                # the Page, because that is what gets posted to - and because
                # an update pushed to us names the account and nothing else.
                id=f"{PLATFORM_NAME}:{picked.id}",
                platform=PLATFORM_NAME,
                host=None,
                account_id=picked.id,
                account_name=picked.username,
                # A Page token taken from a long-lived person's token does not
                # expire, so no expiry is set.
                token=Token(access_token=picked.token),
                scopes=request.scopes or DEFAULT_SCOPES,
                extra={
                    "instagram_id": picked.id,
                    "username": picked.username,
                    "page_id": picked.page_id,
                    "page_name": picked.page_name,
                    "profile_url": f"https://www.instagram.com/{picked.username}",
                },
            )
        )

    async def refresh(
        self,
        connection: Connection,
        app: AppCredentials | None = None,
    ) -> Token:
        """Give the connection a token that is good for a while yet.

        A Page token taken from a long-lived person's token does not expire,
        which is why a connection made by signing in through socialchimp has
        no expiry at all and this hands the same token straight back without
        asking Meta anything.

        A token that does have an expiry is traded in for a fresh sixty days.
        That trade is the whole of renewal on Meta: there is no refresh token
        anywhere in it, so a token is extended while it still works or it is
        gone.

        Args:
            connection: The account whose token is running out.
            app: Your app's id and secret. Meta signs the trade with both, so
                a token with an expiry cannot be extended without them.

        Returns:
            The token to save.

        Raises:
            ConfigError: If the token needs extending and no credentials
                arrived.
            TokenExpiredError: If Meta will not make the trade, which means
                the token has already run out or been taken away. The person
                has to connect their account again.
            SocialChimpError: If Meta refused for some other reason.
        """
        if connection.token.expires_at is None:
            return connection.token

        signing = credentials_or_refuse(
            app,
            platform=PLATFORM_NAME,
            what="extend a token",
        )

        # No token on the conversation itself: the one being traded goes in
        # the query, and Meta reads the app's id and secret as who is asking.
        async with self._graph() as graph:
            try:
                extended = await long_lived_token(
                    graph,
                    client_id=signing.client_id,
                    client_secret=signing.client_secret,
                    token=connection.token.access_token,
                )
            except AuthError as refused:
                message = (
                    f"Meta will not extend the token for {connection.id!r}. "
                    f"It has already run out, or the person removed your app, "
                    f"or the Instagram account was unlinked from its Page. "
                    f"There is no refresh token to fall back on, so the "
                    f"person has to connect their account again - and signing "
                    f"in through socialchimp saves the Page's own token, "
                    f"which does not expire."
                )
                raise TokenExpiredError(
                    message, platform=PLATFORM_NAME, raw=refused.raw
                ) from refused
            self._note(graph)

        return extended

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
            NotSupportedError: If the post is words alone, or carries a file
                Instagram would have to be sent rather than fetch.
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
            graph: A conversation signed with the Page's token.
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
            if only.kind is MediaKind.VIDEO:
                await self._wait_for(graph, container)
            return container

        children: list[str] = []
        for item in things:
            child = await self._start_one(
                graph, account_id, item, caption=None, in_a_carousel=True
            )
            # Each one has to be finished before the parent can name it.
            if item.kind is MediaKind.VIDEO:
                await self._wait_for(graph, child)
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

        if any(item.kind is MediaKind.VIDEO for item in things):
            await self._wait_for(graph, parent)
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
            graph: A conversation signed with the Page's token.
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

    async def _wait_for(self, graph: Graph, container_id: str) -> None:
        """Keep asking whether Instagram has finished making a post.

        Only video goes through here. Instagram has to fetch and re-encode
        it, which takes anywhere from seconds to minutes; a picture is ready
        by the time the first request comes back, so asking about one would
        cost a request and tell us nothing.

        Args:
            graph: A conversation signed with the Page's token.
            container_id: The half-made post to watch.

        Raises:
            InvalidPostError: If Instagram gave up on it.
            PlatformError: If Instagram threw it away, or if it is still not
                ready when we stop watching.
            SocialChimpError: If Instagram refuses the question.
        """
        give_up_at = _now() + timedelta(seconds=self._wait_up_to)

        while True:
            reply = await _ask(
                graph,
                "GET",
                f"/{container_id}",
                # `status` is the sentence a person can read; `status_code` is
                # the word we branch on.
                params={"fields": "status_code,status"},
            )
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
            await _sleep(self._check_every)

    async def _put_it_out(
        self,
        graph: Graph,
        account_id: str,
        container_id: str,
    ) -> PostResult:
        """Publish a container that Instagram has finished making.

        Args:
            graph: A conversation signed with the Page's token.
            account_id: Which Instagram account.
            container_id: The finished half-made post.

        Returns:
            What Instagram said about the new post.

        Raises:
            PlatformError: If Instagram answered without an id.
            SocialChimpError: If Instagram refuses.
        """
        reply = await _ask(
            graph,
            "POST",
            f"/{account_id}/media_publish",
            data={"creation_id": container_id},
        )
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
            secret: Your **app secret** from the developer portal. Not the
                verify token you typed into the webhook form - that one is
                only for `answer_setup_check`.

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
        return [
            _update_from(change) for change in changes_in(body, platform=PLATFORM_NAME)
        ]

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
