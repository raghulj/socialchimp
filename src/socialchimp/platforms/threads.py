"""Threads: the Meta network that is barely a Meta network.

Threads looks like Instagram from a distance - you build a post, you wait for
it, you publish it - and then almost every address is somewhere else. It has
its own app id, its own sign-in page, its own API host, its own shape of
pushed message, and the only renewal in the whole of Meta that actually
works. What it does share with Facebook and Instagram is in `_meta.py`: the
error codes, the rate-limit headers, the signature on a pushed request, and
the two small checks every OAuth callback needs.

## The second app id is the thing that catches everybody

Adding the Threads use case to a Meta app **makes a second app id and a
second app secret**, sitting next to the ones the same app already uses for
Facebook and Instagram. They are not interchangeable, and reusing the
Facebook pair fails in the worst possible way: the sign-in page accepts it,
the person approves, and the token swap at the end refuses with a message
that mentions none of this.

So use the pair from the Threads use case, and save it under the platform
name `threads` with `Storage.save_app` - which keeps it apart from your
Facebook one anyway, because credentials are stored per network.

`SEPARATE_APP` is the sentence that says so, and it is put on every refusal
where a wrong app id is a plausible cause.

## Signing in is not Facebook Login

Two addresses, neither of them Facebook's:

- People approve your app at `https://threads.net/oauth/authorize`.
- The code is swapped at `https://graph.threads.net/oauth/access_token`.

`_meta.GRAPH_API` and `_meta.SIGN_IN_PAGE` do not apply here at all, and
neither do `swap_code_for_token` or `long_lived_token` - the paths, the
grant names and even the HTTP methods are different.

Signing in also **never stops to ask which account**. Facebook asks which
page, because a person can manage many. A Threads sign-in is one profile, so
`finish_login` finishes.

## Renewal that is really renewal

This is the one good surprise. Facebook and Instagram hand out no refresh
token at all: a token is extended by trading it in while it still works, or
the person signs in again. Threads has a real renewal:

    GET https://graph.threads.net/refresh_access_token
        ?grant_type=th_refresh_token&access_token=...

One request, no app secret, and the sixty-day clock starts again. It is a
cleaner contract than the rest of Meta, and worth knowing about because it
means a Threads connection can be kept alive indefinitely by a job that runs
once a month.

The one rule: **a token has to be at least 24 hours old**. A renewal asked
for sooner is refused here rather than by Threads, with the number of
seconds to wait on the error - see `refresh`.

## Publishing, step by step

    1. POST /{account}/threads          -> a container id
    2. GET  /{container}?fields=status  -> wait until it says FINISHED
    3. POST /{account}/threads_publish  -> the post is live

The same shape as Instagram, with two differences. A container's kind is
named outright - TEXT, IMAGE, VIDEO or CAROUSEL - and **TEXT is a real
kind**, so Threads takes a post of words alone where Instagram does not.
`Feature.POST_TEXT` is on.

Step 2 happens for every container, not only video - a picture container
answered `media_publish` with "not ready" on Instagram's identical shape a
fraction of a second after it was made, so nothing here assumes a container
is ready the instant step 1 answers. A container that already is costs one
extra look and no waiting; video is looked at every thirty seconds as
before, and everything else after 1, 2, 4... seconds, never more than 30
apart.

A carousel is two to twenty pieces: each becomes its own container, then a
parent names them all, then the parent is published - and the parent is
looked at too, whatever it is made of.

Threads fetches every picture and video itself, from a web address, exactly
as Instagram does - so `Media.from_url(...)` works and `Media.from_file` and
`Media.from_bytes` are refused with a message saying to put the file
somewhere public first.

## Five hundred bytes, not five hundred characters

Threads' limit is 500, counted in **UTF-8 bytes**. An emoji takes four of
them, so a post of 500 emoji is 2,000 and is refused - which is why
`Limits.text_counted_in` is `TextCount.UTF8_BYTES` rather than the default.
Counting characters here would send posts Threads turns away.

## Two allowances, counted apart

    GET /{account}/threads_publishing_limit

250 posts and 1,000 replies in a rolling 24 hours, and **replies do not come
out of the posts**. Both numbers are read rather than written down, because
Meta has moved them before. Posts left lands on `Limits.posts_left_today`,
and `check_post` refuses when there are none. `allowance` gives you both.
`publish` checks the reply allowance rather than the post one whenever
`Post.reply_to` is set, so answering somebody never spends one of the 250.

## Replying, reading replies, and answering

Reference:
https://developers.facebook.com/docs/threads/retrieve-and-manage-replies/create-replies

Threads takes a `reply_to_id` on the top-level container - the whole post,
or a carousel's parent, never one of its pieces - so `Post(reply_to=...)`
sends it there and nowhere else. `Feature.REPLY` is on.

**Replying needs permission on the post being answered.** Threads only lets
an app reply where it owns the root post, unless it also asks for
`threads_manage_mentions` or `threads_keyword_search` - and neither is in
`DEFAULT_SCOPES`, because most apps only ever answer what was said about
their own posts. Answering a mention on somebody else's post needs one of
those two added to `scopes` when the account signs in.

`read_replies(connection, post_id)` reads the replies to one post - see
https://developers.facebook.com/docs/threads/retrieve-and-manage-replies/replies-and-conversations
with `GET /{post_id}/replies` for the top-level ones, or
`GET /{post_id}/conversation` with `whole_conversation=True`, which flattens
every depth into one list - a reply to a reply comes back the same as one
sent straight to the post. Both build their `Update`s through the same
`_update_from` the `replies` webhook uses, so `update.id` and `update.raw`
match whichever way an app heard about a reply, and one handler and one
`SeenUpdates` serve both.

`fetch_updates(connection, since)` polls instead: the account's latest
`recent_posts` posts (25 unless told otherwise, checked the way Facebook
checks its own - between 1 and 100), each post's whole conversation read
newest first and left as soon as a reply no newer than `since` turns up.
`since=None` reads one page per post rather than the lot. It costs one
request to list the posts, plus one per post - `1 + recent_posts` at most,
every time it is polled.

`reply_to_update` answers a `replies` or a `mentions` update by publishing a
TEXT post with the update's own id as `reply_to`, through `publish` itself -
so the allowance check and the waiting both happen the same as any other
reply. Anything else, a `publish` or a `delete` update included, is refused
by name.

`set_comment_visibility` hides a reply with `POST /{id}/manage_reply` and
`hide=true`, or shows it again with `hide=false` - see
https://developers.facebook.com/docs/threads/reply-management. Meta says
this only works on a top-level reply, and hiding one hides whatever was
said back to it along with it. `delete_comment` always refuses: Threads has
no call for removing somebody else's reply, only for hiding it - and a
reply of your own is removed with `delete_post(update.raw["id"])` instead,
the same as any other post.

## A post's own numbers

    GET /{post_id}/insights

Reference: https://developers.facebook.com/docs/threads/insights

Six metrics come back in one request: `views`, `likes`, `replies`,
`reposts`, `quotes` and the share button's own count, which Threads also
calls `shares`. Three of them land on `PostStats` - `likes`, `replies`
becomes `comments`, and `reposts` becomes `shares`, because that is the
number people mean by "how many times was this passed on". The rest -
`views`, `quotes`, and the share-button metric - are not modelled here;
read them off `PostStats.raw` if you want them. A metric Threads leaves out
of its answer comes back `None`, never a made-up zero. Needs
`threads_manage_insights`, already one of `DEFAULT_SCOPES`.

## Its webhooks are narrower than the rest of Meta's

Reference: https://developers.facebook.com/docs/threads/webhooks

Four topics and no more: `replies`, `mentions`, `publish` and `delete`, which
reach your handlers as `COMMENT_CREATED`, `MENTION`, `POST_PUBLISHED` and
`POST_DELETED`. And **nothing at all where a private account is involved** -
a reply or a mention on media owned by a private account is never sent, and
only `publish` and `delete` arrive for a private account that has authorised
your app itself. So a Threads integration cannot assume it hears about
everything.

The signature is Meta's, so `_meta.check_meta_signature` does the job. The
**message inside is not**: where Facebook and Instagram send a list of
entries each holding a list of changes, Threads sends one change, wrapped
differently. `_meta.changes_in` cannot read it, and `_the_change_in` here can.

## What Threads cannot do here

- **No scheduling.** `Feature.SCHEDULE` is off. There is no call for it, and
  a post with `publish_at` is refused rather than quietly going out now.
- **No link attachments or topic tags yet.** Both are real Threads settings
  and neither is wired up; `POST_OPTIONS` is the list of what `Post.options`
  accepts today.
- **Not confirmed against a live account yet.** The reply, `read_replies`,
  `fetch_updates` and `insights` shapes here are built from Meta's published
  reference rather than watched against a real response.
"""

from __future__ import annotations

import json
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
from socialchimp.events import Update, UpdateKind
from socialchimp.events import answer_setup_check as echo_the_challenge
from socialchimp.features import (
    Feature,
    Limits,
    TextCount,
    check_option_names,
    check_post,
)
from socialchimp.http import HttpClient
from socialchimp.models import (
    AccountProfile,
    Connection,
    MediaKind,
    Post,
    PostResult,
    PostState,
    PostStats,
    RawData,
    Token,
    picture_url,
)
from socialchimp.platform import Finished, LoginRequest, SendToNetwork
from socialchimp.platforms._meta import (
    DEVELOPER_PORTAL,
    Graph,
    Usage,
    check_meta_signature,
    check_state,
    code_from,
    first_update,
    meta_errors,
    quota_left,
    read_edge,
    required_text,
    sign_in_url,
    state_for,
    token_from,
    where_to_post,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from socialchimp.http import Retries
    from socialchimp.models import AppCredentials

__all__ = ["Allowance", "ThreadsPlatform", "threads_errors"]

PLATFORM_NAME: Final = "threads"

THREADS_HOST: Final = "https://graph.threads.net"
"""Where Threads' API lives.

Not `graph.facebook.com`. Sending a Threads request to the Facebook Graph
gets you a 404 or, worse, an answer about something else entirely.
"""

THREADS_VERSION: Final = "v1.0"
"""Which version of the Threads API this talks to."""

THREADS_API: Final = f"{THREADS_HOST}/{THREADS_VERSION}"
"""The versioned half of the host, which every ordinary request goes to.

The three token addresses are the exception: they sit on the host itself,
with no version in front of them, which is why `THREADS_HOST` exists too.
"""

SIGN_IN_PAGE: Final = "https://threads.net/oauth/authorize"
"""The page people are sent to so they can approve your app.

**This is not Facebook Login.** Threads has its own, on its own domain, and
Facebook's dialog will not sign anybody in to a Threads app.
"""

SWAP_PATH: Final = "/oauth/access_token"
"""Where a code is swapped for a token that lasts an hour. A POST."""

MAKE_IT_LAST_PATH: Final = "/access_token"
"""Where an hour-long token is traded for a sixty-day one. A GET."""

RENEW_PATH: Final = "/refresh_access_token"
"""Where a sixty-day token gets another sixty days. A GET, and a real one."""

PROFILE_FIELDS: Final = "id,username,threads_profile_picture_url"
"""What to ask about the person who just signed in."""

ACCOUNT_PROFILE_FIELDS: Final = "username,threads_profile_picture_url"
"""What `read_profile` asks Threads for: a name and a picture, nothing else.

The account's id is already known by the caller, so unlike `PROFILE_FIELDS`
this does not ask for `id` again.
"""

DEFAULT_SCOPES: Final = (
    "threads_basic",
    "threads_content_publish",
    "threads_read_replies",
    "threads_manage_replies",
    "threads_manage_insights",
    "threads_delete",
)
"""The permissions a Threads app asks for.

- `threads_basic` - needed by every single call, including renewal.
- `threads_content_publish` - make and publish a post.
- `threads_read_replies` - read the replies to your posts.
- `threads_manage_replies` - answer them, and hide them.
- `threads_manage_insights` - read the numbers.
- `threads_delete` - remove a post.

None of these look like Facebook's or Instagram's, because Threads is a use
case of its own with permissions of its own.
"""

SEPARATE_APP: Final = (
    "Adding the Threads use case to a Meta app makes a second app id and app "
    "secret, separate from the pair the same app uses for Facebook and "
    "Instagram. Use the Threads app id and the Threads app secret here. The "
    "Facebook pair gets past the sign-in page and is then refused at the "
    "token swap, with a message that mentions none of this."
)
"""The sentence that saves somebody an afternoon.

Put on every refusal where the wrong app id is a plausible cause, because by
the time Threads answers it is far too late to guess.
"""

POST_OPTIONS: Final = ("carousel",)
"""The settings `Post.options` accepts here.

Only one, and only ever `True` or `False`:

    Post(media=(one, two), options={"carousel": True})

Two or more attachments already make a carousel without being asked, so this
is for saying so out loud. Anything else is refused before we send it.
"""

MAX_TEXT_BYTES: Final = 500
"""The longest a post may be, **counted in UTF-8 bytes**.

Threads' own documentation says 500 characters. It means bytes, so an emoji
costs four and an accented letter two, and 500 emoji are 2,000.
"""

FEWEST_IN_A_CAROUSEL: Final = 2
"""A carousel of one is not a carousel. Threads refuses it."""

MOST_IN_A_CAROUSEL: Final = 20
"""Pictures and videos allowed in one post, counted together.

Twice what Instagram takes, which is easy to trip over if you carry a limit
across from there.
"""

HOW_OFTEN_TO_CHECK: Final = 30.0
"""Seconds between two looks at a container Threads is still making.

Meta suggests waiting about thirty seconds before publishing, so that is how
long we leave between looks. Looking faster does not make Threads finish
sooner and does spend your hourly allowance.
"""

HOW_LONG_TO_WAIT: Final = 300.0
"""Seconds to keep looking before giving up - five minutes.

Giving up is not the same as failing. See `_stopped_waiting`.
"""

PICTURE_FIRST_WAIT: Final = 1.0
"""Seconds before the second look at a container that is not a video, then
twice that, and so on.

Applies to text and to a picture. Instagram's identical container shape
answers `media_publish` with "not ready" when a picture is published the
instant its container is made - the container was still not finished with,
0.27 seconds after it was created, in a real log. Nothing here confirms
Threads returns that same error, but the container is made the same way, so
it is looked at before being published rather than assumed ready, the same
as Instagram. A container that is already ready costs one request and no
wait.
"""

PICTURE_LONGEST_WAIT: Final = 30.0
"""The most the doubling above is allowed to grow to, in seconds."""

TOKEN_LIFE_SECONDS: Final = 60 * 24 * 60 * 60
"""How long a long-lived Threads token is good for: sixty days."""

OLD_ENOUGH_TO_RENEW_SECONDS: Final = 24 * 60 * 60
"""How old a token has to be before Threads will renew it: 24 hours."""

REPLY_FIELDS: Final = (
    "id,text,username,permalink,timestamp,media_type,shortcode,replied_to,"
    "root_post,is_reply,is_reply_owned_by_me,hide_status,has_replies"
)
"""What to ask about each reply, on `read_replies` and `fetch_updates`."""

RECENT_POSTS_TO_CHECK: Final = 25
"""How many of the account's latest posts `fetch_updates` looks at by default.

Every post costs one request, every time it is asked, so this is a balance
between how far back a reply is still noticed and how much of your hourly
allowance a poll spends. Change it with `recent_posts` on the constructor.
"""

MOST_POSTS_TO_CHECK: Final = 100
"""The most posts Threads lists in one request, and so the most we ask for."""

REPLIES_PER_REQUEST: Final = 100
"""How many replies to read at a time, on `read_replies` and `fetch_updates`."""

MOST_REPLY_PAGES: Final = 10
"""How many requests to spend reading one post's replies before moving on."""

INSIGHTS_METRICS: Final = "views,likes,replies,reposts,quotes,shares"
"""What `read_stats` asks Threads for.

Six metrics for one request. Three of them land on `PostStats` - see
`read_stats` for which - and the rest stay on `PostStats.raw` because
nothing here has a field of its own for them.
"""

# What Threads calls a container it has finished with, one it has already
# published, one it gave up on, and one it threw away. Anything else -
# IN_PROGRESS, or a word Meta adds next year - means keep looking.
_FINISHED: Final = "FINISHED"
_ALREADY_OUT: Final = "PUBLISHED"
_GAVE_UP: Final = "ERROR"
_THROWN_AWAY: Final = "EXPIRED"

# What Threads calls each kind of container.
_TEXT: Final = "TEXT"
_IMAGE: Final = "IMAGE"
_VIDEO: Final = "VIDEO"
_CAROUSEL: Final = "CAROUSEL"

# What Threads calls a thing it pushes to us, and what we call it. All four
# of its topics are here, because there are only four. A fifth, if Meta ever
# adds one, keeps Threads' own word and arrives as UNKNOWN.
#
# `delete` is the one worth explaining. Threads' own documentation does not
# say whether it fires for a post or for a reply, and the message carries a
# whole post - an id, an owner and a deleted_at - so `post_deleted` is what
# we can honestly tell from it. If it turns out to cover replies as well,
# "something of yours was removed" is still true, where guessing
# `comment_deleted` would be wrong half the time.
_OUR_WORD_FOR: Final = {
    "replies": "comment_created",
    "mentions": "mention",
    "publish": "post_published",
    "delete": "post_deleted",
}

# Threads glues this onto the end of the code it hands back. Sending it on
# gets the swap refused without any hint as to why.
_STUCK_ON_THE_CODE: Final = "#_"


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


def threads_errors(response: httpx.Response) -> SocialChimpError:
    """Turn an unhappy reply from Threads into a socialchimp error.

    Threads answers with Meta's own error object and Meta's own codes, so all
    of the work is shared with Facebook and Instagram and this only says
    which network is talking. See `_meta.meta_errors` for what the codes mean.

    Args:
        response: The reply to turn into an error.

    Returns:
        The error to raise.
    """
    return meta_errors(response, platform=PLATFORM_NAME)


# ---------------------------------------------------------------------------
# Your app, which is not the app you already had
# ---------------------------------------------------------------------------


def _app_or_refuse(request: LoginRequest, *, what: str) -> AppCredentials:
    """Insist on your Threads app's id and secret, and say which pair to use.

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
            f"threads needs your app's id and secret to {what}, and none "
            f"arrived. Make the app by hand at {DEVELOPER_PORTAL} and add the "
            f"Threads use case to it, then save the pair with "
            f"Storage.save_app under the platform name 'threads'. "
            f"{SEPARATE_APP}"
        )
        raise ConfigError(message)
    return request.app


def _app_must_be_made_by_hand() -> NotSupportedError:
    """Build the error for somebody who asked us to register a Threads app.

    Returns:
        The error to raise. Returned rather than raised so the type checker
        follows what happens next at the place it is used.
    """
    return NotSupportedError(
        platform=PLATFORM_NAME,
        what="registering an app for you",
        suggestion=(
            f"No Meta network does - there is no call for it. Make the app "
            f"by hand at {DEVELOPER_PORTAL}, add the Threads use case to it, "
            f"and save the id and secret with Storage.save_app. "
            f"{SEPARATE_APP} Meta also has to review the app before it works "
            f"for anybody but you."
        ),
    )


def _probably_the_wrong_app(refused: SocialChimpError) -> AuthError:
    """Say that a refused token swap is often the Facebook app id.

    Threads answers a wrong app id with whatever code it feels like, and none
    of its messages mention the two pairs, so this adds the sentence that
    does. Meta's own words are kept on the end and on `raw`.

    Args:
        refused: What Threads answered, already named by `meta_errors`.

    Returns:
        The error to raise.
    """
    message = (
        f"threads would not swap this sign-in for a token. The usual reason "
        f"is the app id. {SEPARATE_APP} It said: {refused}"
    )
    return AuthError(message, platform=PLATFORM_NAME, raw=refused.raw)


def _tidy(code: str) -> str:
    """Take off what Threads glues to the end of a login code.

    Args:
        code: The code exactly as it came back.

    Returns:
        The code Threads will accept. Threads appends `#_` to the code on the
        redirect, and a code with that still on it is refused at the swap
        without any hint as to why.
    """
    return code.removesuffix(_STUCK_ON_THE_CODE)


# ---------------------------------------------------------------------------
# What a post may carry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Attachment:
    """One picture or video, and the address Threads will fetch it from.

    Attributes:
        url: Where the file is. Always set, because a post that has no
            address for a file never gets this far.
        kind: Picture or video.
        alt_text: Description for people using a screen reader.
    """

    url: str
    kind: MediaKind
    alt_text: str | None = None


def _account_of(connection: Connection) -> str:
    """Work out which Threads account a connection posts to.

    Args:
        connection: The account to look at.

    Returns:
        Threads' identifier for it.

    Raises:
        ConfigError: If the connection names no account at all.
    """
    return where_to_post(
        connection,
        key="threads_id",
        what="Threads account",
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
                f"{key} is {value!r}, but it has to be True or False. True "
                f"sends the post as a carousel; leaving it out sends two or "
                f"more attachments as one anyway."
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
        what="being sent a file",
        suggestion=(
            "It fetches every picture and video itself, from a web address, "
            "and has no upload of any kind - so Media.from_file and "
            "Media.from_bytes cannot be published here. Put the file "
            "somewhere the public internet can reach it, such as object "
            "storage with a public link or your own web server, and use "
            "Media.from_url(...) instead."
        ),
    )


def _things_to_publish(post: Post) -> tuple[_Attachment, ...]:
    """Work out what this post is made of, and refuse it if we cannot send it.

    Args:
        post: The post about to be sent.

    Returns:
        The pictures and videos, in the order they were given. Empty for a
        post of words alone, which Threads takes and Instagram does not.

    Raises:
        NotSupportedError: If the post carries a file we would have to upload.
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
            f"and Threads takes at most {MOST_IN_A_CAROUSEL} in one post. "
            f"Send the rest as a second post."
        )
        raise InvalidPostError(message)

    return tuple(found)


def _what_it_allows(posts_left_today: int | None = None) -> Limits:
    """Return the numbers Threads enforces.

    Args:
        posts_left_today: How many posts are left in the last 24 hours, when
            we have asked. `None` before we have, and whenever Threads'
            answer was not one we could read.

    Returns:
        What Threads allows right now.
    """
    return Limits(
        max_text_length=MAX_TEXT_BYTES,
        # Bytes, not characters. Threads says 500 characters and means this,
        # and the difference only shows up once somebody posts an emoji.
        text_counted_in=TextCount.UTF8_BYTES,
        # Twenty of anything, counted together: a carousel can mix pictures
        # and video, and twenty is the total.
        max_images=MOST_IN_A_CAROUSEL,
        max_videos=MOST_IN_A_CAROUSEL,
        # No file sizes. Nothing is ever uploaded from here, so we never see
        # a file to measure, and what Threads will fetch is between it and
        # your web server.
        posts_left_today=posts_left_today,
    )


@dataclass(frozen=True, slots=True)
class Allowance:
    """How much of today's posting Threads has left on one account.

    Threads counts posts and replies over a rolling 24 hours and keeps the
    two apart, which is worth knowing: answering a thousand people costs none
    of your 250 posts.

    Either may be `None`, which means "Threads did not say in a way we could
    read" - never "none left".

    Attributes:
        posts_left: How many more posts are allowed today. This is the one
            that lands on `Limits.posts_left_today`.
        replies_left: How many more replies are allowed today. Counted apart
            from posts, and socialchimp does not publish replies yet, so this
            is here to read rather than to act on.
    """

    posts_left: int | None = None
    replies_left: int | None = None


# ---------------------------------------------------------------------------
# Waiting for Threads to finish
# ---------------------------------------------------------------------------


def _threads_gave_up(container_id: str, reply: RawData) -> InvalidPostError:
    """Build the error for a container Threads could not make.

    Args:
        container_id: The half-made post it gave up on.
        reply: What it said when asked how that was getting on.

    Returns:
        The error to raise, carrying Threads' own words where it left any.
    """
    said = reply.get("error_message")
    detail = f" Threads said: {said}" if isinstance(said, str) and said else ""
    message = (
        f"Threads gave up while making this post (container {container_id!r}), "
        f"so nothing has been published. Almost always the file: an address "
        f"it could not reach, a picture too big or the wrong shape, or a "
        f"video that is not MP4 with H.264 video and AAC audio.{detail}"
    )
    return InvalidPostError(message, platform=PLATFORM_NAME, raw=reply)


def _thrown_away(container_id: str, reply: RawData) -> PlatformError:
    """Build the error for a container that sat around too long.

    Args:
        container_id: The half-made post Threads threw away.
        reply: What it said when asked how that was getting on.

    Returns:
        The error to raise.
    """
    message = (
        f"Threads threw away the half-made post before it could be published "
        f"(container {container_id!r}). A container is only good for 24 "
        f"hours, and this one is older than that, so nothing has gone out. "
        f"Send the post again."
    )
    return PlatformError(message, platform=PLATFORM_NAME, raw=reply)


def _stopped_waiting(container_id: str, waited: float) -> PlatformError:
    """Build the error for a container that is still not ready.

    We stop looking eventually, and stopping is not the same as failing. The
    message says so twice, because an app that treats this as a failure and
    sends the post again is the way the same video ends up on Threads twice -
    and nobody can undo that from here.

    Args:
        container_id: The half-made post we gave up watching.
        waited: How many seconds we watched it for.

    Returns:
        The error to raise.
    """
    message = (
        f"Threads was still working on this post {waited:.0f} seconds after "
        f"we sent it, so we have stopped watching. This is not the same as it "
        f"failing, and the post may still appear: Threads often finishes a "
        f"video minutes after this point, and the half-made post (container "
        f"{container_id!r}) stays good for 24 hours, so it can still be "
        f"published with that id. Look at the account before you send this "
        f"post again. To wait longer, build the platform with "
        f"ThreadsPlatform(wait_up_to_seconds=...)."
    )
    return PlatformError(message, platform=PLATFORM_NAME, raw={"id": container_id})


# ---------------------------------------------------------------------------
# Renewing
# ---------------------------------------------------------------------------


def _seconds_until_old_enough(expires_at: datetime) -> float:
    """Say how much longer a token has to sit before Threads will renew it.

    Nothing on a connection records when a token was made, and nothing needs
    to: a long-lived token is good for sixty days from that moment, so how
    much of the sixty days is left says how old it is.

    Args:
        expires_at: When the token stops working.

    Returns:
        Seconds to wait, or zero or less when it is already old enough.
    """
    still_good_for = (expires_at - _now()).total_seconds()
    return still_good_for - (TOKEN_LIFE_SECONDS - OLD_ENOUGH_TO_RENEW_SECONDS)


def _too_soon_to_renew(connection: Connection, wait: float) -> RateLimitError:
    """Build the error for a token Threads is not ready to renew.

    Args:
        connection: The account whose token it is.
        wait: Seconds until Threads would take it.

    Returns:
        The error to raise, carrying the wait so a caller can act on it.
    """
    message = (
        f"Threads will not renew a token until it is 24 hours old, and the "
        f"one on {connection.id!r} is younger than that. There is nothing "
        f"wrong with it - it has almost all of its sixty days left and does "
        f"not need renewing yet. Wait {wait / 3600:.0f} hours and ask again; "
        f"socialchimp's own renewal runs long before the sixty days are up, "
        f"so this only happens when refresh is called by hand."
    )
    return RateLimitError(message, retry_after=wait, platform=PLATFORM_NAME)


def _cannot_be_renewed(connection: Connection, refused: AuthError) -> TokenExpiredError:
    """Build the error for a token Threads will not renew at all.

    Args:
        connection: The account whose token it is.
        refused: What Threads answered.

    Returns:
        The error to raise.
    """
    message = (
        f"Threads will not renew the token for {connection.id!r}. A token "
        f"that has gone sixty days without being renewed cannot be brought "
        f"back, and neither can one the person has taken away, so there is "
        f"nothing left to try - they have to connect their account again. "
        f"Renewing on a timer, well inside the sixty days, is what stops this "
        f"happening."
    )
    return TokenExpiredError(message, platform=PLATFORM_NAME, raw=refused.raw)


# ---------------------------------------------------------------------------
# Requests Threads pushes to us
# ---------------------------------------------------------------------------


def _when_it_happened(value: RawData, envelope: RawData) -> datetime:
    """Work out when a pushed message says something happened.

    Args:
        value: What Threads says happened.
        envelope: The whole message it arrived in.

    Returns:
        The moment, with a timezone. Threads puts a readable time on the
        thing itself and a plain count of seconds on the envelope, so the
        first is used where it can be read and the second where it cannot. A
        message with neither is stamped as it arrives, because every update
        socialchimp holds is ordered by this.
    """
    stamp = value.get("timestamp")
    if isinstance(stamp, str) and stamp:
        try:
            return datetime.fromisoformat(stamp)
        except ValueError:
            pass

    seconds = envelope.get("time")
    if isinstance(seconds, int | float) and not isinstance(seconds, bool):
        return datetime.fromtimestamp(float(seconds), UTC)
    return _now()


def _the_change_in(body: bytes) -> tuple[RawData, str, RawData] | None:
    """Unpack a message Threads pushed to us.

    Threads wraps things differently from the rest of Meta. Facebook and
    Instagram send a list of accounts, each holding a list of changes, and
    `_meta.changes_in` reads that. Threads sends one change, under a `values`
    key, with the account it happened on named at the top level - so nothing
    shared can read it.

    Args:
        body: The request body, exactly as it arrived. Check its signature
            with `check_meta_signature` first.

    Returns:
        The whole message, what Threads calls this kind of change, and what
        actually happened. `None` when the message carries nothing we can act
        on, which is not an error - Threads sends shapes we have no interest
        in.

    Raises:
        PlatformError: If the body is not a Threads message at all.
    """
    parsed = _read(body)

    values = parsed.get("values")
    if not isinstance(values, dict):
        return None

    field = values.get("field")
    if not (isinstance(field, str) and field):
        return None

    value = values.get("value")
    return parsed, field, value if isinstance(value, dict) else {}


def _read(body: bytes) -> RawData:
    """Read a pushed request as JSON, and complain plainly if it is not.

    Args:
        body: The request body, exactly as it arrived.

    Returns:
        The message, parsed.

    Raises:
        PlatformError: If the body is not a Threads message at all.
    """
    try:
        parsed = json.loads(body)
    except ValueError as problem:
        message = (
            "This request from threads could not be read as JSON, so there is "
            "nothing in it to act on. Pass the raw body, exactly as it "
            "arrived."
        )
        raise PlatformError(message, platform=PLATFORM_NAME) from problem

    if not isinstance(parsed, dict):
        message = (
            f"This request from threads could not be read as one of its "
            f"messages: it holds a {type(parsed).__name__} where an object "
            f"was expected."
        )
        raise PlatformError(message, platform=PLATFORM_NAME)
    return parsed


def _update_from(envelope: RawData, field: str, value: RawData) -> Update:
    """Turn one thing Threads pushed into an update your app understands.

    Args:
        envelope: The whole message. Kept beside the change, because the
            account and the subscription are named out there.
        field: What Threads calls this kind of change.
        value: What actually happened, in Threads' own words. This is what
            ends up on `Update.raw`.

    Returns:
        What happened, in socialchimp's own words. Anything we have no word
        for keeps Threads', and arrives as `UpdateKind.UNKNOWN`.
    """
    account_id = str(envelope.get("target_id", ""))
    when = _when_it_happened(value, envelope)

    return Update.from_network(
        # Meta puts no identifier on the message itself and promises to
        # deliver at least once, which is a promise to deliver twice
        # sometimes. The id of the thing it happened to is stable, so a
        # second delivery of the same change produces the same id.
        update_id=":".join(
            [
                account_id,
                field,
                str(value.get("id", "")) or str(int(when.timestamp())),
            ]
        ),
        kind_name=_OUR_WORD_FOR.get(field, field),
        platform=PLATFORM_NAME,
        # Threads names the account the subscription is for, not one of your
        # connections. A login here names a connection after its account, so
        # the two line up without your app keeping a table of its own.
        connection_id=f"{PLATFORM_NAME}:{account_id}",
        created_at=when,
        # What happened, so a handler reads `update.raw["text"]` straight.
        # The message goes alongside, because Threads names the account and
        # the subscription out there rather than on the change.
        raw=value,
        envelope=envelope,
    )


class ThreadsPlatform:
    """Everything socialchimp does with Threads.

    Signing people in, renewing their tokens properly, publishing words, a
    picture, a video or a carousel, removing a post, and reading what Threads
    pushes to you.

        threads = ThreadsPlatform()
        step = await threads.start_login(request)

    It holds nothing between calls. Everything about an account arrives on
    the `Connection` and everything about your app on the `LoginRequest`, so
    one of these can be shared by your whole process.

    Attributes:
        name: `"threads"`.
        features: What Threads can do here. It takes a post of words alone,
            which Instagram does not, it can delete, which Instagram cannot,
            and it can reply - `Post(reply_to=...)` sends `reply_to_id` on
            the top-level container. There is no scheduling in its API and
            no app to register anywhere in Meta, so `SCHEDULE` and
            `CREATE_APP` are both missing.
    """

    name: str = PLATFORM_NAME

    features: Feature = (
        Feature.POST_TEXT
        | Feature.POST_IMAGE
        | Feature.POST_VIDEO
        | Feature.REPLY
        | Feature.DELETE_POST
        | Feature.READ_STATS
        | Feature.PUSH_UPDATES
    )

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        retries: Retries | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        check_every_seconds: float = HOW_OFTEN_TO_CHECK,
        wait_up_to_seconds: float = HOW_LONG_TO_WAIT,
        recent_posts: int = RECENT_POSTS_TO_CHECK,
    ) -> None:
        """Set Threads up for one app.

        Args:
            timeout: Seconds to wait for Threads to answer one request. This
                is per request, not for the whole of publishing - the waiting
                below has its own settings.
            retries: How many times to try again after a hiccup. Left out,
                the shared default is used.
            transport: Where requests actually go. Leave it out for ordinary
                calls; pass your own to send them somewhere else.
            check_every_seconds: How often to ask whether Threads has finished
                making a video post. Thirty seconds is Meta's own advice;
                asking faster does not make it finish sooner.
            wait_up_to_seconds: How long to keep asking before giving up.
                Raise it if you post long video, and read `_stopped_waiting`
                first - giving up here does not mean the post failed.
            recent_posts: How many of the account's latest posts
                `fetch_updates` reads replies from, between 1 and 100. Each
                one costs a request every time you poll.

        Raises:
            ConfigError: If `recent_posts` is outside 1 to 100.
        """
        if not 1 <= recent_posts <= MOST_POSTS_TO_CHECK:
            message = (
                f"recent_posts is {recent_posts}, but it has to be between 1 "
                f"and {MOST_POSTS_TO_CHECK} - Threads lists no more than "
                f"that many posts in one request."
            )
            raise ConfigError(message)

        self._timeout = timeout
        self._retries = retries
        self._transport = transport
        self._check_every = check_every_seconds
        self._wait_up_to = wait_up_to_seconds
        self._recent_posts = recent_posts
        self._usage: Usage | None = None

    @property
    def usage(self) -> Usage | None:
        """How much of your app's hourly allowance Meta last said is gone.

        `None` until a reply mentions it. This is your whole app rather than
        one account, because that is how Meta counts. It is a different thing
        from `Allowance`, which is how many posts and replies Threads will
        take today.
        """
        return self._usage

    def _graph(self, token: str | None = None, *, at: str = THREADS_API) -> Graph:
        """Start a conversation with Threads.

        Args:
            token: The token to sign requests with, and none at all while
                swapping a code.
            at: Which half of the host to talk to. The versioned one for
                ordinary requests; `THREADS_HOST` for the three token
                addresses, which have no version in front of them.

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
                errors=threads_errors,
            ),
            platform=PLATFORM_NAME,
        )

    def _tokens(self) -> Graph:
        """Start a conversation with the three addresses that hand out tokens.

        Returns:
            A conversation pointed at the host itself rather than at the
            versioned half of it, carrying no token of its own.
        """
        return self._graph(at=THREADS_HOST)

    def _note(self, graph: Graph) -> None:
        """Keep whatever the last reply said about the allowance.

        Args:
            graph: The conversation that has just finished.
        """
        if graph.usage is not None:
            self._usage = graph.usage

    def api_base(self, connection: Connection) -> str:
        """Return where Threads' API lives.

        One address for everybody, and **not** the one the rest of Meta uses.
        The connection is taken because every platform's is.

        Args:
            connection: The account we are about to act as. Not used here.

        Returns:
            The address, with no trailing slash.
        """
        return THREADS_API

    def auth_headers(self, connection: Connection) -> Mapping[str, str]:
        """Return the header that proves we may act as this account.

        Threads also takes the token as an `access_token` query parameter,
        and every example in its documentation does it that way. This uses
        the header instead: a token in a web address ends up in server logs,
        proxy logs and browser history, and stays there.

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
        about the second app id, rather than an AttributeError.

        Args:
            name: Ignored.
            redirect_uri: Ignored.
            host: Ignored.
            scopes: Ignored.

        Returns:
            Nothing. It always raises.

        Raises:
            NotSupportedError: Always. The message names the portal, the
                Threads use case and the second app id.
        """
        raise _app_must_be_made_by_hand()

    async def allowance(self, connection: Connection) -> Allowance:
        """Ask Threads how much of today's posting is left.

        Both numbers are asked for rather than written down. Meta has moved
        them before, and whatever they are today, Threads knows.

        Args:
            connection: The account to ask about.

        Returns:
            How many posts and how many replies are left, each `None` rather
            than a guess when Threads' answer could not be read.

        Raises:
            ConfigError: If the connection names no Threads account.
            SocialChimpError: If Threads refuses the question.
        """
        account_id = _account_of(connection)

        async with self._graph(connection.token.access_token) as graph:
            try:
                return _allowance_in(await self._ask_the_limit(graph, account_id))
            finally:
                self._note(graph)

    async def _ask_the_limit(self, graph: Graph, account_id: str) -> RawData:
        """Read the publishing limit off Threads.

        Args:
            graph: A conversation carrying the account's token.
            account_id: Which account to ask about.

        Returns:
            What Threads answered.

        Raises:
            SocialChimpError: If Threads refuses the question.
        """
        return await graph.json(
            "GET",
            f"/{account_id}/threads_publishing_limit",
            params={
                "fields": "quota_usage,config,reply_quota_usage,reply_config",
            },
        )

    async def limits(self, connection: Connection) -> Limits:
        """Return what Threads allows this account right now.

        One request, to find out how many posts are left today. Everything
        else here is the same for every account, but that number is not: it
        counts down as you post and back up 24 hours later. Worth caching for
        a minute or two if you are about to check it repeatedly.

        Args:
            connection: The account to ask about.

        Returns:
            What Threads allows. `posts_left_today` is `None` rather than a
            guess when Threads' answer could not be read.

        Raises:
            ConfigError: If the connection names no Threads account.
            SocialChimpError: If Threads refuses the question.
        """
        return _what_it_allows((await self.allowance(connection)).posts_left)

    async def start_login(self, request: LoginRequest) -> SendToNetwork:
        """Build the address to send somebody to so they can approve your app.

        Nothing is sent to Threads here. There is also nothing to remember
        between this call and the next: the swap at the end is signed with
        your app secret, which never leaves your server.

        Args:
            request: Where to send them back to, what to ask for, and your
                **Threads** app's credentials.

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
                # Threads' own page, on its own domain. Facebook's dialog
                # will not sign anybody in to a Threads app.
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
        """Swap the code for a token, make it last, and read the profile.

        Unlike Facebook and Instagram this finishes the job. There is no
        page or business account to choose between - a Threads sign-in is one
        profile - so nothing here answers with `ChooseAccount`.

        Args:
            request: The same request used to start the login.
            callback: The query values Threads sent back. It must have
                `code`; `state` is checked when it is there.
            remember: Not used. Nothing has to survive between the two calls
                here.

        Returns:
            The finished connection. Save it. Its token is good for sixty
            days and can be renewed for another sixty, which is the one thing
            Threads does better than the rest of Meta.

        Raises:
            AuthError: If the person said no, if there is no code, if the
                state that came back is not the one we sent, or if Threads
                will not make the swap - which is usually the app id.
            ConfigError: If the request carries no app credentials.
            SocialChimpError: If Threads refuses for some other reason.
        """
        app = _app_or_refuse(request, what="finish a sign-in")
        check_state(request, callback, platform=PLATFORM_NAME)
        code = _tidy(code_from(callback, platform=PLATFORM_NAME))

        async with self._tokens() as graph:
            try:
                short = await self._swap(graph, app, request.redirect_uri, code)
                # Traded now rather than later because the first token is
                # good for an hour, and nothing can be done with an expired
                # one but sign the person in again.
                long = await self._make_it_last(graph, app, short.access_token)
            finally:
                self._note(graph)

        async with self._graph(long.access_token) as graph:
            profile = await graph.json("GET", "/me", params={"fields": PROFILE_FIELDS})
            self._note(graph)

        account_id = required_text(
            profile, "id", platform=PLATFORM_NAME, when="say who just signed in"
        )
        username = profile.get("username")
        # An account always has a username, but showing the id is better than
        # showing nothing if one ever arrives without.
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
                avatar_url=picture_url(profile.get("threads_profile_picture_url")),
                extra={
                    "threads_id": account_id,
                    "username": name,
                    "profile_url": f"https://www.threads.net/@{name}",
                },
            )
        )

    async def _swap(
        self,
        graph: Graph,
        app: AppCredentials,
        redirect_uri: str,
        code: str,
    ) -> Token:
        """Swap the code Threads sent back for a token that lasts an hour.

        A POST with a form, where Facebook's is a GET with a query. Threads
        refuses a GET here, which is a quiet half hour if you carry the
        Facebook code across.

        Args:
            graph: A conversation with the token addresses.
            app: Your **Threads** app's id and secret.
            redirect_uri: The same address the sign-in was started with.
            code: What Threads put on the end of your redirect address.

        Returns:
            A token that works for about an hour.

        Raises:
            AuthError: If Threads will not make the swap, whatever code it
                used to say so.
            RateLimitError: If Threads is asking us to slow down, which is
                nothing to do with the app id.
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
        except RateLimitError:
            # Slow down means slow down, whichever app id was sent.
            raise
        except SocialChimpError as refused:
            # Anything else, whatever Meta code it arrived under: a wrong app
            # id comes back as several of them and never mentions the pair.
            raise _probably_the_wrong_app(refused) from refused

        return token_from(reply, platform=PLATFORM_NAME, when="sign someone in")

    async def _make_it_last(
        self,
        graph: Graph,
        app: AppCredentials,
        token: str,
    ) -> Token:
        """Trade an hour-long token for one that lasts sixty days.

        Args:
            graph: A conversation with the token addresses.
            app: Your Threads app's credentials. Only the secret is sent;
                Threads works out the app from the token.
            token: The short-lived token to trade in.

        Returns:
            The long-lived token, good for about sixty days and renewable
            after the first 24 hours.

        Raises:
            AuthError: If Threads will not make the trade.
        """
        try:
            reply = await graph.json(
                "GET",
                MAKE_IT_LAST_PATH,
                params={
                    "grant_type": "th_exchange_token",
                    "client_secret": app.client_secret,
                    "access_token": token,
                },
            )
        except RateLimitError:
            raise
        except SocialChimpError as refused:
            raise _probably_the_wrong_app(refused) from refused

        return token_from(reply, platform=PLATFORM_NAME, when="extend a token")

    async def refresh(
        self,
        connection: Connection,
        app: AppCredentials | None = None,
    ) -> Token:
        """Give the connection another sixty days.

        This is the only real renewal anywhere in Meta. Facebook and
        Instagram hand out no refresh token and extend a token by trading it
        in; Threads has one address that takes a working token and gives back
        a fresh one, with no app secret and nothing else to arrange. A job
        that runs once a month keeps a Threads connection alive for good.

        The one rule is that **a token has to be at least 24 hours old**.
        Nothing here writes down when a token was made, and nothing needs to:
        a long-lived token is good for sixty days from that moment, so what
        is left says how old it is.

        Args:
            connection: The account whose token is running out.
            app: Your app's id and secret. Accepted because every platform's
                `refresh` is, and not sent - Threads works out the app from
                the token, which is why this renewal is so much simpler than
                the rest of Meta's.

        Returns:
            The new token, good for another sixty days from now. Save it: the
            old one carries on working until its own expiry, but the clock
            that matters is on this one.

        Raises:
            RateLimitError: If the token is not yet 24 hours old. Nothing is
                wrong with it and nothing is sent; `retry_after` says how
                long to leave it.
            TokenExpiredError: If Threads will not renew it, which means it
                has already gone sixty days without renewal or the person has
                taken your app's access away. Either way they have to connect
                their account again.
            SocialChimpError: If Threads refused for some other reason.
        """
        expires_at = connection.token.expires_at
        if expires_at is not None:
            wait = _seconds_until_old_enough(expires_at)
            if wait > 0:
                raise _too_soon_to_renew(connection, wait)
        # A token with no expiry on it says nothing about how old it is, so
        # rather than guess we ask Threads and let it answer.

        async with self._tokens() as graph:
            try:
                reply = await graph.json(
                    "GET",
                    RENEW_PATH,
                    params={
                        "grant_type": "th_refresh_token",
                        "access_token": connection.token.access_token,
                    },
                )
            except AuthError as refused:
                raise _cannot_be_renewed(connection, refused) from refused
            finally:
                self._note(graph)

        return token_from(reply, platform=PLATFORM_NAME, when="renew a token")

    async def read_profile(self, connection: Connection) -> AccountProfile:
        """Ask Threads for this account's current username and picture.

        One request, to the account's own object - the same shape
        `finish_login` already reads at sign-in, but asking for only a
        username and a picture.

        Args:
            connection: The account to ask about.

        Returns:
            The account's username, and its picture if it has one right now.

        Raises:
            ConfigError: If the connection names no Threads account.
            SocialChimpError: If Threads refuses the question.
        """
        account_id = _account_of(connection)

        async with self._graph(connection.token.access_token) as graph:
            try:
                reply = await graph.json(
                    "GET",
                    f"/{account_id}",
                    params={"fields": ACCOUNT_PROFILE_FIELDS},
                )
            finally:
                self._note(graph)

        username = reply.get("username")
        return AccountProfile(
            # An account always has a username, but showing the id is better
            # than showing nothing if one ever arrives without.
            name=username if isinstance(username, str) and username else account_id,
            avatar_url=picture_url(reply.get("threads_profile_picture_url")),
        )

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        """Publish a post: build it, wait for it if it needs waiting on, put it out.

        Words go out on their own, which is the thing Threads does that
        Instagram does not. One picture or video goes out on its own too; two
        to twenty go out as a carousel. Where there is video, this waits for
        Threads to finish making the post before publishing it.

        Args:
            connection: The account to publish as.
            post: What to publish. Its text is the post, or the caption where
                there is a file, and every attachment has to be a
                `Media.from_url`.

        Returns:
            What Threads said about the new post, always `PostState.DONE` -
            the waiting happens in here, so a result that comes back at all
            is a post that is live. There is no link on it: Threads' id for a
            post is not its web address, and the address uses a short code
            that only another request would tell us. Ask for it with
            `GET /{id}?fields=permalink` if you need it.

        A post with `reply_to` set is a reply, and Threads takes it out of
        the 1,000-a-day reply allowance rather than the 250-a-day post one -
        so the check below asks the right number before sending it.

        Reference:
        https://developers.facebook.com/docs/threads/retrieve-and-manage-replies/create-replies

        Raises:
            ConfigError: If the connection names no Threads account.
            InvalidPostError: If the post breaks one of Threads' limits, if a
                setting is unknown, or if Threads gave up making it.
            NotSupportedError: If the post needs something Threads cannot do
                here, such as scheduling, or carries a file Threads would have
                to be sent rather than fetch.
            PlatformError: If Threads was still working when we stopped
                watching. That is not a failure - see `_stopped_waiting`.
            SocialChimpError: If Threads refuses any of the steps.
        """
        account_id = _account_of(connection)

        # Everything that can be judged without asking Threads is judged
        # first, so a mistake costs no request and no part of the hourly
        # allowance.
        as_carousel = _checked_options(post.options)
        check_post(
            post,
            platform=PLATFORM_NAME,
            features=self.features,
            limits=_what_it_allows(),
        )
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
        # so, because Threads has no other way to carry them.
        as_carousel = as_carousel or len(things) > 1

        async with self._graph(connection.token.access_token) as graph:
            try:
                left = _allowance_in(await self._ask_the_limit(graph, account_id))
                # The daily allowance is the one rule we cannot know without
                # asking, so it is checked here rather than above, now that
                # we have the number. A reply is counted against the reply
                # allowance, not the post one - answering a thousand people
                # never spends one of the 250 posts.
                left_for_this_post = (
                    left.replies_left if post.reply_to is not None else left.posts_left
                )
                check_post(
                    post,
                    platform=PLATFORM_NAME,
                    features=self.features,
                    limits=_what_it_allows(left_for_this_post),
                )

                container = await self._build(
                    graph, account_id, post, things, as_carousel=as_carousel
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
        """Make the half-finished post Threads will publish, and wait for it.

        Args:
            graph: A conversation signed with the account's token.
            account_id: Which Threads account.
            post: What to publish. `reply_to`, when set, is put on the
                top-level container - the whole post, or a carousel's
                parent - and nowhere else.
            things: The pictures and videos, already checked. Empty for a
                post of words alone.
            as_carousel: Whether these go out as one carousel.

        Returns:
            The container id to publish.

        Raises:
            InvalidPostError: If Threads gave up making any part of it.
            PlatformError: If it was still working when we stopped watching.
            SocialChimpError: If Threads refuses one of the requests.
        """
        if not things:
            form = {"media_type": _TEXT}
            _add_reply_to(form, post)
            container = await self._start(graph, account_id, form, post.text)
            await self._wait_for(graph, container, video=False)
            return container

        if not as_carousel:
            only = things[0]
            form = _form_for(only)
            _add_reply_to(form, post)
            container = await self._start(graph, account_id, form, post.text)
            await self._wait_for(graph, container, video=only.kind is MediaKind.VIDEO)
            return container

        children: list[str] = []
        for item in things:
            form = _form_for(item)
            form["is_carousel_item"] = "true"
            # No text on a piece: the words belong to the carousel, and so
            # does what it is replying to.
            child = await self._start(graph, account_id, form, None)
            # Each one has to be finished before the parent can name it.
            await self._wait_for(graph, child, video=item.kind is MediaKind.VIDEO)
            children.append(child)

        parent_form = {"media_type": _CAROUSEL, "children": ",".join(children)}
        _add_reply_to(parent_form, post)
        parent = await self._start(graph, account_id, parent_form, post.text)
        # The parent is a container of its own, and as capable of not being
        # finished with as anything inside it - pictures and words included.
        await self._wait_for(
            graph,
            parent,
            video=any(item.kind is MediaKind.VIDEO for item in things),
        )
        return parent

    async def _start(
        self,
        graph: Graph,
        account_id: str,
        form: dict[str, str],
        text: str | None,
    ) -> str:
        """Ask Threads to start making one container.

        Nothing is uploaded here. Threads is given the address and goes and
        fetches the file itself, which is why this can come back long before
        the post is ready.

        Args:
            graph: A conversation signed with the account's token.
            account_id: Which Threads account.
            form: What kind of container, and where its file is.
            text: The words to put on it, or `None` for a carousel piece.

        Returns:
            The container id.

        Raises:
            PlatformError: If Threads answered without an id.
            SocialChimpError: If Threads refuses.
        """
        if text is not None:
            form["text"] = text
        reply = await graph.json("POST", f"/{account_id}/threads", data=form)
        return required_text(reply, "id", platform=PLATFORM_NAME, when="start a post")

    async def _wait_for(
        self,
        graph: Graph,
        container_id: str,
        *,
        video: bool,
    ) -> None:
        """Keep asking whether Threads has finished making a post.

        Every container goes through here - words, a picture, each piece of a
        carousel and the carousel itself - because a container answers the
        request that made it long before Threads may be finished with it.
        Instagram's identical shape can refuse `media_publish` a fraction of
        a second after the container was made; this looks before publishing
        rather than assume any of them are ready, the way it already did for
        video.

        The first look is always immediate, so a container that is already
        ready costs one request and no waiting. After that, video is looked
        at every `check_every_seconds`, as before. Anything else is looked at
        again after a second, then two, doubling up to `PICTURE_LONGEST_WAIT`.

        A non-video container whose reply carries no `status` at all is
        published rather than waited on: nothing here confirms every kind of
        container reports one, and waiting the full five minutes on a word
        Threads never sends would be worse than publishing early.

        Args:
            graph: A conversation signed with the account's token.
            container_id: The half-made post to watch.
            video: Whether it is, or holds, a video.

        Raises:
            InvalidPostError: If Threads gave up on it.
            PlatformError: If Threads threw it away, or if it is still not
                ready when we stop watching.
            SocialChimpError: If Threads refuses the question.
        """
        give_up_at = _now() + timedelta(seconds=self._wait_up_to)
        wait = self._check_every if video else PICTURE_FIRST_WAIT

        while True:
            reply = await graph.json(
                "GET",
                f"/{container_id}",
                # `status` is the word we branch on; `error_message` is the
                # sentence a person can read when it went wrong.
                params={"fields": "status,error_message"},
            )
            if not video and "status" not in reply:
                return
            said = str(reply.get("status", ""))

            if said in (_FINISHED, _ALREADY_OUT):
                return
            if said == _GAVE_UP:
                raise _threads_gave_up(container_id, reply)
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
        """Publish a container that Threads has finished making.

        Args:
            graph: A conversation signed with the account's token.
            account_id: Which Threads account.
            container_id: The finished half-made post.

        Returns:
            What Threads said about the new post.

        Raises:
            PlatformError: If Threads answered without an id.
            SocialChimpError: If Threads refuses.
        """
        reply = await graph.json(
            "POST",
            f"/{account_id}/threads_publish",
            data={"creation_id": container_id},
        )
        return PostResult(
            id=required_text(
                reply, "id", platform=PLATFORM_NAME, when="publish a post"
            ),
            # Threads' id for a post is not its web address: the address uses
            # a short code, and only another request would tell us it.
            url=None,
            state=PostState.DONE,
            raw=reply,
        )

    async def delete_post(self, connection: Connection, post_id: str) -> None:
        """Remove a post.

        Instagram has no call for this and Threads does, which is one of the
        few places Threads is the easier of the two. Threads allows 100
        deletions a day per account.

        Args:
            connection: The account that published it.
            post_id: Threads' id for the post, as `publish` handed it back.

        Raises:
            NotFoundError: If there is no such post on this account.
            SocialChimpError: If Threads refuses.
        """
        async with self._graph(connection.token.access_token) as graph:
            await graph.json("DELETE", f"/{post_id}")
            self._note(graph)

    async def read_stats(self, connection: Connection, post_id: str) -> PostStats:
        """Ask Threads how a post is doing.

        One request, for six numbers Threads calls metrics: `views`,
        `likes`, `replies`, `reposts`, `quotes` and the share button's own
        count, which Threads also calls `shares`. Three of them land on the
        fields every platform here uses - `likes` stays `likes`, `replies`
        becomes `comments`, and `reposts` becomes `shares`, because that is
        the number people mean by "how many times was this passed on".
        `views`, `quotes` and the share-button `shares` metric are not
        modelled here; read them off `raw` if you want them.

        Needs `threads_manage_insights`, already one of `DEFAULT_SCOPES`.

        Reference: https://developers.facebook.com/docs/threads/insights

        Args:
            connection: The account the post is on.
            post_id: Threads' id for the post, as `publish` handed it back.

        Returns:
            `likes`, `comments` and `shares`, each `None` rather than a
            guess when Threads left the metric out of its answer.

        Raises:
            SocialChimpError: If Threads refuses the question.
        """
        async with self._graph(connection.token.access_token) as graph:
            try:
                reply = await graph.json(
                    "GET",
                    f"/{post_id}/insights",
                    params={"metric": INSIGHTS_METRICS},
                )
            finally:
                self._note(graph)

        return PostStats(
            id=post_id,
            likes=_metric_value(reply, "likes"),
            comments=_metric_value(reply, "replies"),
            shares=_metric_value(reply, "reposts"),
            raw=reply,
        )

    async def read_replies(
        self,
        connection: Connection,
        post_id: str,
        *,
        whole_conversation: bool = False,
    ) -> Sequence[Update]:
        """Read the replies to one post.

        `GET /{post_id}/replies` by default, which is only the top-level
        replies - the ones sent straight to the post, not to another reply
        underneath it. Pass `whole_conversation=True` for
        `GET /{post_id}/conversation` instead, which flattens every depth
        into one list, a reply to a reply included.

        Every reply comes back through the same `_update_from` the `replies`
        webhook builds its updates with, so `update.id` and `update.raw`
        match whichever way an app heard about it - one handler and one
        `SeenUpdates` serve both.

        Reference:
        https://developers.facebook.com/docs/threads/retrieve-and-manage-replies/replies-and-conversations

        Args:
            connection: The account that owns the post.
            post_id: Threads' id for the post, as `publish` handed it back.
            whole_conversation: `True` to read every depth of the thread
                rather than only what was sent straight to the post.

        Returns:
            The replies, oldest first.

        Raises:
            ConfigError: If the connection names no Threads account.
            SocialChimpError: If Threads refuses the question.
        """
        account_id = _account_of(connection)
        edge = "conversation" if whole_conversation else "replies"

        updates: list[Update] = []
        async with self._graph(connection.token.access_token) as graph:
            try:
                after: str | None = None
                for _ in range(MOST_REPLY_PAGES):
                    params: dict[str, object] = {
                        "fields": REPLY_FIELDS,
                        "reverse": "false",
                        "limit": REPLIES_PER_REQUEST,
                    }
                    if after is not None:
                        params["after"] = after
                    found, after = await read_edge(
                        graph, f"/{post_id}/{edge}", params=params
                    )
                    updates.extend(_replies_to_updates(found, account_id=account_id))
                    if after is None:
                        break
            finally:
                self._note(graph)

        updates.sort(key=lambda update: update.created_at)
        return updates

    async def fetch_updates(
        self,
        connection: Connection,
        since: datetime | None,
    ) -> Sequence[Update]:
        """Read the replies on the account's latest posts.

        Threads can push these to you, and this is for when you would rather
        ask, or cannot receive a request from the internet. What comes back
        has the same shape a webhook's would - and the same `id`, so a
        `Dispatcher` given a `SeenUpdates` answers a reply once even when it
        turns up both ways.

        The latest `recent_posts` posts are read (`GET /{account}/threads`),
        one request to list them and then each post's whole conversation
        read newest first, left as soon as a reply no newer than `since`
        turns up - so a poll of a quiet account costs `1 + recent_posts`
        requests at most, and a poll of a busy one costs more.

        Reference:
        https://developers.facebook.com/docs/threads/retrieve-and-manage-replies/replies-and-conversations

        Args:
            connection: The account to read.
            since: Only return replies newer than this. `None` on the first
                call, when there is no marker saved yet - the latest page of
                replies on each post, rather than the whole history.

        Returns:
            The replies, oldest first, as `UpdateKind.COMMENT_CREATED`.

        Raises:
            ConfigError: If the connection names no Threads account.
            SocialChimpError: If Threads refuses.
        """
        account_id = _account_of(connection)

        found: list[Update] = []
        async with self._graph(connection.token.access_token) as graph:
            try:
                posts, _ = await read_edge(
                    graph,
                    f"/{account_id}/threads",
                    params={"fields": "id", "limit": self._recent_posts},
                )
                for post in posts:
                    post_id = post.get("id")
                    if isinstance(post_id, str) and post_id:
                        found.extend(
                            await _replies_on(graph, account_id, post_id, since)
                        )
            finally:
                self._note(graph)

        found.sort(key=lambda update: update.created_at)
        return found

    async def reply_to_update(
        self,
        connection: Connection,
        update: Update,
        text: str,
    ) -> None:
        """Answer a reply or a mention by publishing a reply of your own.

        Goes through `publish` itself, with `reply_to` set to the update's
        own id - so this spends part of the daily reply allowance, and waits
        for Threads to finish with it, the same as any other reply.

        **Replying needs permission on the post being answered.** Threads
        only allows it where the app owns the root post, unless it also
        holds `threads_manage_mentions` or `threads_keyword_search` - see
        the module's own docstring.

        Reference:
        https://developers.facebook.com/docs/threads/retrieve-and-manage-replies/create-replies

        Args:
            connection: The account to answer as.
            update: The update to answer, exactly as `fetch_updates` or
                `read_updates` handed it back. Its `raw["id"]` is what this
                answers.
            text: The reply.

        Raises:
            NotSupportedError: If this is not a reply or a mention - a
                `publish` or a `delete` update, or one Threads has no name
                for.
            PlatformError: If the update carries no id to reply to.
            SocialChimpError: If Threads refuses the reply, including for
                lacking permission on the post being answered.
        """
        if update.kind not in (UpdateKind.COMMENT_CREATED, UpdateKind.MENTION):
            raise NotSupportedError(
                platform=PLATFORM_NAME,
                what=f"answering an update of kind {update.kind_name!r}",
                suggestion="Only a reply or a mention can be answered here.",
            )

        await self.publish(connection, Post(text=text, reply_to=_id_on(update)))

    async def delete_comment(self, connection: Connection, update: Update) -> None:
        """Say, plainly, that Threads has no way to remove somebody else's reply.

        Args:
            connection: Ignored.
            update: Ignored.

        Raises:
            NotSupportedError: Always. The message says to hide the reply
                instead, and how to remove one of your own.
        """
        raise NotSupportedError(
            platform=PLATFORM_NAME,
            what="removing a reply that is not yours",
            suggestion=(
                "There is no call for it. Hide it instead with "
                "set_comment_visibility(hidden=True), which is the "
                "moderation Threads actually offers. For a reply you "
                "published yourself, use delete_post(update.raw['id']) - "
                "it is a post like any other."
            ),
        )

    async def set_comment_visibility(
        self,
        connection: Connection,
        update: Update,
        *,
        hidden: bool,
    ) -> None:
        """Hide a reply from public view, or show one again.

        `POST /{reply}/manage_reply` with `hide=true` or `hide=false`. Meta
        says this only works on a top-level reply - one sent straight to the
        post rather than to another reply underneath it - and hiding one
        hides whatever was said back to it along with it.

        Reference: https://developers.facebook.com/docs/threads/reply-management

        Args:
            connection: The account the reply belongs to.
            update: The reply to hide or show, exactly as `fetch_updates` or
                `read_updates` handed it back. Its `raw["id"]` is which one.
            hidden: `True` to hide it, `False` to show it again.

        Raises:
            PlatformError: If the update carries no id to act on.
            SocialChimpError: If Threads refuses.
        """
        reply_id = _id_on(update)

        async with self._graph(connection.token.access_token) as graph:
            try:
                await graph.json(
                    "POST",
                    f"/{reply_id}/manage_reply",
                    data={"hide": "true" if hidden else "false"},
                )
            finally:
                self._note(graph)

    def check_signature(
        self,
        body: bytes,
        headers: Mapping[str, str],
        *,
        secret: str,
    ) -> None:
        """Check a request Threads pushed to us really came from Threads.

        The signature is Meta's own, so this is the same check Facebook and
        Instagram do. It covers the **raw bytes** of the body: a framework
        that parses the JSON and builds it again first changes the spacing
        and the key order, and this then fails on a request that was
        perfectly good. Read the body, check it here, and parse it afterwards.

        Args:
            body: The request body, exactly as it arrived.
            headers: The request headers.
            secret: Your **Threads app secret** from the developer portal -
                the one that came with the Threads use case, not your
                Facebook app's, and not the verify token you typed into the
                webhook form.

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
        text and the URL starts working.

        The only topics Threads has are `replies`, `mentions`, `publish` and
        `delete` - a much shorter list than Facebook's or Instagram's - and
        none of them arrives for media owned by a private account.

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

        One message from Threads carries one change, unlike Facebook and
        Instagram, which batch. This still hands back a list, so that an app
        written against one Meta network works against all three.

        Reference: https://developers.facebook.com/docs/threads/webhooks

        Args:
            body: The request body, untouched. Check its signature first.

        Returns:
            What happened, as a list of one - or an empty list when the
            message carried nothing we can act on.

        Raises:
            PlatformError: If the body is not one of Threads' messages.
        """
        found = _the_change_in(body)
        if found is None:
            return []
        envelope, field, value = found
        return [_update_from(envelope, field, value)]

    def read_update(
        self,
        body: bytes,
        headers: Mapping[str, str],
    ) -> Update:
        """Turn a checked request into one update your app understands.

        Only call this after `check_signature` has passed.

        Args:
            body: The request body, untouched.
            headers: The request headers. Not needed here; the signature
                header has already done its job by this point.

        Returns:
            What happened, in socialchimp's own words.

        Raises:
            PlatformError: If the body is not one of Threads' messages, or
                carries nothing we can act on.
        """
        return first_update(self.read_updates(body), platform=PLATFORM_NAME)


def _form_for(item: _Attachment) -> dict[str, str]:
    """Say what kind of container one picture or video needs.

    Args:
        item: The picture or video.

    Returns:
        The kind, the address Threads should fetch it from, and its
        description where there is one.
    """
    if item.kind is MediaKind.VIDEO:
        form = {"media_type": _VIDEO, "video_url": item.url}
    else:
        form = {"media_type": _IMAGE, "image_url": item.url}
    if item.alt_text:
        form["alt_text"] = item.alt_text
    return form


def _add_reply_to(form: dict[str, str], post: Post) -> None:
    """Say which post this one answers, on the top-level container's form.

    Only ever called on the container that is about to be published - the
    whole post, or a carousel's parent - never on one of a carousel's
    pieces, which have nothing of their own to reply to.

    Args:
        form: The form about to be sent, changed in place.
        post: The post being published, which may carry `reply_to`.
    """
    if post.reply_to is not None:
        form["reply_to_id"] = post.reply_to


def _allowance_in(reply: RawData) -> Allowance:
    """Read both of today's allowances out of one answer from Threads.

    Args:
        reply: What Threads answered.

    Returns:
        Posts and replies left, each `None` where the answer was not one we
        could read.
    """
    return Allowance(
        posts_left=quota_left(reply),
        replies_left=quota_left(
            reply, used="reply_quota_usage", allowed_in="reply_config"
        ),
    )


def _id_on(update: Update) -> str:
    """Read the id an update names, and complain plainly if it has none.

    Args:
        update: The update naming a reply.

    Returns:
        The id.

    Raises:
        PlatformError: If there is none. `update.raw` should be what
            `fetch_updates` or `read_updates` handed back, with Threads' own
            id for the reply still on it.
    """
    found = update.raw.get("id")
    if isinstance(found, str) and found:
        return found

    message = (
        "This update carries no id, so there is nothing to act on. "
        "update.raw should be what fetch_updates or read_updates handed "
        "back, with Threads' own id for the reply or mention still on it."
    )
    raise PlatformError(message, platform=PLATFORM_NAME, raw=update.raw)


def _replies_to_updates(found: list[RawData], *, account_id: str) -> list[Update]:
    """Turn one page of replies into updates, in the same shape the webhook uses.

    Args:
        found: One page of replies, as Threads answered.
        account_id: The account the post being read belongs to.

    Returns:
        The replies as updates, in the order Threads listed them.
    """
    envelope = {"target_id": account_id}
    return [_update_from(envelope, "replies", value) for value in found]


async def _replies_on(
    graph: Graph,
    account_id: str,
    post_id: str,
    since: datetime | None,
) -> list[Update]:
    """Read the replies on one post that are newer than a moment.

    Args:
        graph: A conversation signed with the account's token.
        account_id: Which Threads account the post belongs to.
        post_id: Which post.
        since: Only replies newer than this, or `None` for the latest page
            of them.

    Returns:
        The replies as updates, in the order Threads listed them - newest
        first, because that is what lets a caller stop early.
    """
    updates: list[Update] = []
    after: str | None = None
    # With nothing to compare against there is no natural place to stop, so
    # a first call reads one page - the latest - rather than a whole history.
    most_pages = 1 if since is None else MOST_REPLY_PAGES

    for _ in range(most_pages):
        params: dict[str, object] = {
            "fields": REPLY_FIELDS,
            "reverse": "true",
            "limit": REPLIES_PER_REQUEST,
        }
        if after is not None:
            params["after"] = after
        entries, after = await read_edge(
            graph, f"/{post_id}/conversation", params=params
        )

        caught_up = False
        for entry in _replies_to_updates(entries, account_id=account_id):
            if since is not None and entry.created_at <= since:
                caught_up = True
                break
            updates.append(entry)
        if caught_up or after is None:
            break
    return updates


def _metric_value(reply: RawData, name: str) -> int | None:
    """Read one number out of a Threads insights reply.

    Args:
        reply: What Threads answered.
        name: Which metric to look for, such as `"likes"`.

    Returns:
        The number, or `None` when the metric is missing from the reply, or
        its value could not be read. Never a guess: a made-up number here
        would show a count Threads never gave.
    """
    entries = reply.get("data")
    if not isinstance(entries, list):
        return None

    for entry in entries:
        if not isinstance(entry, dict) or entry.get("name") != name:
            continue

        total = entry.get("total_value")
        if isinstance(total, dict):
            found = _whole_number(total.get("value"))
            if found is not None:
                return found

        values = entry.get("values")
        if isinstance(values, list) and values and isinstance(values[0], dict):
            found = _whole_number(values[0].get("value"))
            if found is not None:
                return found

    return None


def _whole_number(value: object) -> int | None:
    """Read a value that should be a count.

    Args:
        value: Whatever arrived under that key.

    Returns:
        The number, or `None` for anything that is not one. `True` is not a
        number here, whatever Python thinks.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None
