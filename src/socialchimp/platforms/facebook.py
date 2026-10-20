"""Facebook Pages: the first network that asks you a question part way through.

Facebook is the network people ask for first, and it is the easiest of Meta's
three to write. One request publishes a post. Scheduling is real - Facebook
takes the post now and puts it out later, which almost nothing else does. And
it tells you about new comments instead of making you check.

That is why Facebook Pages is the third network socialchimp supports. The
parts of it that are really parts of Meta - the sign-in, the token exchange,
the error codes, the signature on a pushed request - live in `_meta.py`, ready
for Instagram and Threads.

## You have to make the app by hand, and wait

There is no `create_app` here, and `Feature.CREATE_APP` is off. Meta has no
call for it. You:

1. Create the app yourself at https://developers.facebook.com/apps.
2. Wait for Meta to review it, because `pages_manage_posts` does nothing for
   the general public until it has passed.
3. Get your business verified, which means sending Meta documents about the
   company behind the app.

Until steps 2 and 3 are done, everything works for people who have a role on
the app in the portal and fails for everybody else. That is the single most
confusing thing about starting with Meta, so asking this platform to register
an app says all of it out loud instead of failing later.

Save the id and secret with `Storage.save_app` and socialchimp hands them to
every login for you.

## Signing someone in takes three steps, not two

Everywhere else, the person comes back from the network and you are done.
Here they come back and you still do not know **which page** to post to - a
person can manage a dozen.

1. `start_login` gives you an address. Send the person's browser there.
2. They come back with a code. `finish_login` swaps it for a token, makes
   that token last, asks Facebook which pages they manage, and answers with
   `ChooseAccount` - a list of pages and a `resume_token`.
3. Show the pages. When they pick one, call `resume_login` with its id and
   the `resume_token`, and you get the connection to save.

**It asks even when there is only one page.** Choosing silently would be one
less screen and a worse library: your app would need two code paths, one of
which almost never runs and so is never right, and somebody with two pages
would find out which one was connected when a post appeared on it. One page
today is two pages next year.

The `resume_token` carries the person's own token between steps 2 and 3.
Treat it the way you treat a password - keep it in that person's session,
never in a URL or a hidden form field - and hand it straight back.

## Two tokens, and only one of them expires

Signing in gives you a token for the **person**. Posting needs a token for
the **page**, and they are different things.

- The person's token is swapped for a long-lived one, good for about sixty
  days. That is what `resume_token` carries.
- The page token taken from a long-lived person's token **does not expire at
  all**. That is the one saved on the connection.

So `refresh` usually has nothing to do and hands the same token straight
back. Where a connection does carry a token with an expiry on it, `refresh`
trades it for a fresh sixty days, which is the only kind of renewal Meta
has: there is no refresh token anywhere here, and a token that has already
run out cannot be brought back - the person signs in again. That is why
socialchimp renews early rather than on the way past.

## What a post can carry

`Post.options` accepts one setting:

    Post(text="Read this", options={"link": "https://example.com/a"})

Anything else is refused before we send it, with a message listing what is
accepted.

Pictures are uploaded first, unpublished, and then named by the post that
carries them - one at a time or a dozen, always the same way. Facebook will
also fetch a picture or a video from a web address, so `Media.from_url` works
here where it does not on Mastodon or Bluesky.

## A video is not live when publish returns

Words and pictures are on the page the moment `publish` answers. A video is
not: Facebook takes the bytes, answers with an id, and carries on encoding,
which is why a video comes back as `PostState.PROCESSING`. Ask `check_state`
with that id and Facebook says whether it is `ready`, still `processing`, or
one it gave up on.

The whole file is read into memory first, unlike YouTube, TikTok and X, which
send a video in pieces. That is the reason for the limit below, and for
`biggest_video_bytes` on the constructor.

## Reading comments and likes

Two ways in, and they can be used together.

- **`read_stats`** says how one post is doing: its reactions (`likes`, which
  counts every kind of reaction, not only the thumbs-up), its comments and
  its shares. One request, and only the counts come back.
- **`fetch_updates`** hands back the comments on the page's most recent posts
  as `UpdateKind.COMMENT_CREATED`, in the same shape a webhook would have
  produced, so one handler serves both. It looks at the latest
  `recent_posts` posts (25 unless you say otherwise) and makes one request
  for each, plus more where a post has more comments than one request holds.
  A comment on an older post than that is not seen by polling - a webhook
  sees it whatever the age.

Reading what other people wrote needs `pages_read_user_content`, which is on
top of the permission that lets you read the page itself. Somebody who
connected before it was asked for has to connect again.

## Pushed comments and reactions

Facebook pushes a page's comments and reactions to your webhook, but only
once the page has **subscribed your app**, and signing in does not do that.
Call `subscribe_to_updates` (or `Account.subscribe_to_updates`) after a page
connects. It is safe to call again, so run it once over pages connected
before too. Where the pushes go is set for the whole app in Meta's
dashboard, not here.

It needs `pages_manage_metadata`, which `DEFAULT_SCOPES` leaves out: sign in
with `scopes=PUSH_SCOPES` to ask for it. A page connected without it has to
connect again before it can subscribe.

https://developers.facebook.com/docs/graph-api/reference/page/subscribed_apps/
https://developers.facebook.com/docs/graph-api/webhooks/getting-started/webhooks-for-pages/

Not written yet: a list of *who* reacted, and answering, hiding or deleting a
comment. Reactions do arrive as `UpdateKind.REACTION_ADDED` through the
webhook.

## What Facebook cannot do here

- **No replies yet.** `Feature.REPLY` is off. A reply on Facebook is a
  comment, which is a different kind of thing from a post.
- **No big videos.** Anything over a gigabyte has to go up in pieces, over
  several requests, and that is not written yet. A video over the line is
  refused with a message saying so, rather than half-uploaded.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx

from socialchimp.errors import (
    AuthError,
    ConfigError,
    InvalidPostError,
    NotSupportedError,
    PlatformError,
    TokenExpiredError,
)
from socialchimp.events import Update, UpdateBatch
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
    Media,
    MediaKind,
    Person,
    Post,
    PostResult,
    PostState,
    PostStats,
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
    meta_picture_url,
    page_by_id,
    pages_of,
    read_edge,
    required_text,
    sign_in_url,
    state_for,
    swap_code_for_token,
    where_to_post,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from socialchimp.errors import SocialChimpError
    from socialchimp.http import Retries
    from socialchimp.models import AppCredentials

__all__ = ["FacebookPlatform", "facebook_errors"]

PLATFORM_NAME: Final = "facebook"

DEFAULT_SCOPES: Final = (
    "pages_manage_posts",
    "pages_read_engagement",
    "pages_read_user_content",
    "pages_show_list",
    "business_management",
)
"""The permissions posting to a page, and reading what people say on it, needs.

- `pages_show_list` - see which pages the person manages, which is how we
  get to ask them which one.
- `pages_read_engagement` - read the page and its posts, and the counts of
  reactions and comments on them.
- `pages_read_user_content` - read what other people wrote on the page, such
  as their comments.
- `pages_manage_posts` - publish, schedule and delete.
- `business_management` - needed for pages that belong to a business rather
  than to a person, which is most pages worth posting to.

Every one of these needs Meta's review before it works for anybody but you.

`pages_manage_metadata`, which `subscribe_to_updates` needs, is left out on
purpose: not every app wants pushes, and polling needs none of it. Ask for
`PUSH_SCOPES` instead to have it.
"""

PUSH_SCOPES: Final = (*DEFAULT_SCOPES, "pages_manage_metadata")
"""`DEFAULT_SCOPES` plus what subscribing a page to your app's pushes needs.

    LoginRequest(..., scopes=PUSH_SCOPES)

`pages_manage_metadata` lets `subscribe_to_updates` subscribe the page, so
Facebook pushes its comments and reactions to your webhook. Meta reviews it
like the rest.
"""

POST_OPTIONS: Final = ("link",)
"""The settings `Post.options` accepts here. Anything else is refused."""

MAX_TEXT_LENGTH: Final = 63_206
"""Characters allowed in a post.

Facebook does not publish this number anywhere in its API reference; it is
the one everybody has measured, and it is so large that no ordinary post
comes near it. It is declared so a runaway loop is caught here rather than
by Facebook.
"""

MAX_VIDEOS_PER_POST: Final = 1
"""Facebook takes one video per post, and will not mix video with pictures."""

BIGGEST_SIMPLE_VIDEO: Final = 1_000_000_000
"""The biggest video we will send in a single request - a gigabyte.

Above this Facebook wants the file in pieces, over an upload it keeps open
across several requests. That is a job in itself rather than another branch,
so a bigger video is refused here with a message saying what to do. When it
is written it belongs beside `_publish_video`.

Until then the whole file is read into memory before it is sent, so this
number is also how much memory one video costs your own server. Lower it
with `biggest_video_bytes` if that is more than you have.
"""

RECENT_POSTS_TO_CHECK: Final = 25
"""How many of the page's latest posts `fetch_updates` looks at by default.

Every post costs one request, every time it is asked, so this is a balance
between how far back a comment is still noticed and how much of your hourly
allowance a poll spends. Change it with `recent_posts` on the constructor.
"""

MOST_POSTS_TO_CHECK: Final = 100
"""The most posts Facebook lists in one request, and so the most we ask for."""

COMMENTS_PER_REQUEST: Final = 100
"""How many comments to read at a time."""

MOST_COMMENT_PAGES: Final = 10
"""How many requests to spend on one post's comments before moving on."""

COMMENT_FIELDS: Final = "id,message,created_time,from,parent{id},permalink_url"
"""What to ask about each comment."""

POST_FIELDS: Final = "id,attachments{media_type,target{id}}"
"""What to ask about each of the page's posts when reading their comments.

The attachment says whether a post is a video, and if so the video's own id
- the id `publish` handed back - which differs from the post id the page's
feed lists it under. See
https://developers.facebook.com/docs/graph-api/reference/page/published_posts/
and the `attachments` field of
https://developers.facebook.com/docs/graph-api/reference/post/
"""

MARKER_PREFIX: Final = "fb1"
"""What every marker this platform makes starts with.

A marker is `fb1:<seconds since 1970>:<comment ids>` - the time of the newest
comment handed back, and every comment at that same second that was handed
back, because Facebook's times are to the second and two comments can share
one. Treat it as opaque; only this file reads it.
"""

STATS_FIELDS: Final = (
    "id,reactions.limit(0).summary(true),comments.limit(0).summary(true)"
)
"""What to ask about a post to get its numbers and nothing else.

`limit(0)` asks for no reactions and no comments themselves, only the count
that comes alongside them, so this stays one small request however popular
the post is. `shares` is added by `read_stats` where the id is a post's.
"""

PROFILE_FIELDS: Final = "name,picture{url}"
"""What `read_profile` asks Facebook for: a name and a picture, nothing else.

`PAGE_FIELDS` also carries `picture{url}`, but it asks for the category and
the page's own token as well, which `read_profile` has no use for.
"""

SOONEST_SCHEDULE_SECONDS: Final = 10 * 60
"""Facebook will not schedule anything less than ten minutes ahead."""

LATEST_SCHEDULE_SECONDS: Final = 75 * 24 * 60 * 60
"""Facebook will not schedule anything more than 75 days ahead."""

# What Facebook calls the state of a video, and what we call it. Facebook
# has three words for it and no more - see the VideoStatus node in the Graph
# API reference. Anything else, including a word Meta adds next year, means
# ask again rather than assume the video is live.
_OUR_STATE_FOR: Final = {
    "ready": PostState.DONE,
    "processing": PostState.PROCESSING,
    "error": PostState.FAILED,
}

# What Facebook calls a change on a page, and what we call it. Anything
# missing from here is passed through as Facebook's own words and lands as
# `UpdateKind.UNKNOWN`, so a kind nobody has seen yet still reaches your app.
_OUR_WORD_FOR: Final = {
    ("comment", "add"): "comment_created",
    ("comment", "edited"): "comment_edited",
    ("comment", "remove"): "comment_deleted",
    ("reaction", "add"): "reaction_added",
    ("reaction", "remove"): "reaction_removed",
    # Older pages still send likes under their own name rather than as a
    # reaction, and both mean the same thing to an app.
    ("like", "add"): "reaction_added",
    ("like", "remove"): "reaction_removed",
}

PUSH_FIELDS: Final = ("feed",)
"""What `subscribe_to_updates` subscribes a page to when not told otherwise.

`feed` carries the page's comments, reactions and posts - everything
`read_updates` turns into an `Update`. Meta's other Page fields are listed at
https://developers.facebook.com/docs/graph-api/webhooks/reference/page/
"""


def _push_fields(fields: Sequence[str]) -> tuple[str, ...]:
    """Check a list of webhook fields before Facebook sees it.

    Facebook takes them as one comma-separated list, so a comma or a space
    inside a name would quietly become a different list.

    Args:
        fields: The fields asked for.

    Returns:
        The same fields, as a tuple.

    Raises:
        ConfigError: If there are none, `fields` is one bare string, or a
            name is empty or has a comma or a space in it.
    """
    if isinstance(fields, str):
        message = (
            f"fields is the string {fields!r}, but it has to be a list of "
            f"names, such as ({fields!r},)."
        )
        raise ConfigError(message)
    wanted = tuple(fields)
    if not wanted:
        message = "fields is empty. Pass at least one, such as ('feed',)."
        raise ConfigError(message)
    for name in wanted:
        if not name or any(c == "," or c.isspace() for c in name):
            message = (
                f"{name!r} is not a field name Facebook can read: it has to "
                f"be one word, such as 'feed', with no commas or spaces."
            )
            raise ConfigError(message)
    return wanted


def _now() -> datetime:
    """Return the current moment.

    Kept as its own function so tests can say exactly when "in ten minutes"
    is.

    Returns:
        Now, with a timezone.
    """
    return datetime.now(UTC)


def facebook_errors(response: httpx.Response) -> SocialChimpError:
    """Turn an unhappy reply from Facebook into a socialchimp error.

    All of the work is Meta's, shared with Instagram and Threads, so this
    only says which network is talking. See `_meta.meta_errors` for what the
    codes mean.

    Args:
        response: The reply to turn into an error.

    Returns:
        The error to raise.
    """
    return meta_errors(response, platform=PLATFORM_NAME)


def _app_on(request: LoginRequest) -> tuple[str, str]:
    """Read your app's credentials off a login request.

    Args:
        request: The request being started or finished.

    Returns:
        The app's id and its secret.

    Raises:
        ConfigError: If the request carries none, saying where to get them.
    """
    app = credentials_or_refuse(
        request.app,
        platform=PLATFORM_NAME,
        what="sign somebody in",
    )
    return app.client_id, app.client_secret


def _checked_options(options: RawData) -> dict[str, str]:
    """Check every setting on a post before anything is sent.

    Args:
        options: What was put in `Post.options`.

    Returns:
        The same settings, as text a form can carry.

    Raises:
        InvalidPostError: If a setting is unknown or its value is wrong. This
            happens before any request, so a typo costs nothing.
    """
    check_option_names(options, platform=PLATFORM_NAME, allowed=POST_OPTIONS)

    checked: dict[str, str] = {}
    for key, value in options.items():
        if not isinstance(value, str) or not value:
            message = (
                f"{key} is {value!r}, but it has to be some text - a whole "
                f"web address, such as https://example.com/a."
            )
            raise InvalidPostError(message)
        checked[key] = value
    return checked


def _scheduled_time(post: Post) -> int | None:
    """Work out what to tell Facebook about publishing this later.

    Args:
        post: The post about to be sent.

    Returns:
        The moment as seconds since 1970, which is what Facebook wants, or
        `None` for a post going out now.

    Raises:
        InvalidPostError: If the moment is outside the window Facebook
            accepts. Checked here so the message says what is wrong instead
            of Facebook answering with a code.
    """
    if post.publish_at is None:
        return None

    ahead = (post.publish_at - _now()).total_seconds()
    if ahead < SOONEST_SCHEDULE_SECONDS:
        message = (
            f"Facebook will not schedule a post less than ten minutes ahead, "
            f"and this one is {int(ahead / 60)} minutes away. Publish it now "
            f"by leaving publish_at out, or move it further out."
        )
        raise InvalidPostError(message)

    if ahead > LATEST_SCHEDULE_SECONDS:
        message = (
            f"Facebook will not schedule a post more than 75 days ahead, and "
            f"this one is {int(ahead / 86_400)} days away."
        )
        raise InvalidPostError(message)

    return int(post.publish_at.timestamp())


def _split_media(post: Post) -> tuple[list[Media], list[Media]]:
    """Sort a post's files into pictures and video.

    Args:
        post: The post about to be sent.

    Returns:
        The pictures and the videos, in the order they were given.

    Raises:
        InvalidPostError: If the post has both. Facebook has no kind of post
            that carries a video and pictures together, so this is refused
            here rather than half-uploaded and then refused by Facebook.
    """
    pictures = [item for item in post.media if item.kind is MediaKind.IMAGE]
    videos = [item for item in post.media if item.kind is MediaKind.VIDEO]

    if pictures and videos:
        message = (
            "This post has both pictures and a video. A Facebook post carries "
            "one or the other, not both. Send two posts."
        )
        raise InvalidPostError(message)
    return pictures, videos


def _update_from(change: Change, *, root_id: str | None = None) -> Update:
    """Turn one change Facebook pushed into an update your app understands.

    The shared fields are filled in the way the social inbox contract says,
    the same as Mastodon and Bluesky:

    - `actor`: who did it, from the change's `from` (an id and a name;
      Facebook sends no picture here).
    - A comment: `post_id` is the comment itself; `thread_root_id` is the
      page's post it sits under; `about_post_id` is what it answers - the
      post for a comment on the post, the comment for a reply.
    - A reaction: `about_post_id` is what was reacted to (a comment, or the
      post), `thread_root_id` the post. It has no id of its own.
    - Anything else naming a post, such as a visitor's post on the page:
      `post_id` is that post.

    See the `feed` field of the Page webhooks reference:
    https://developers.facebook.com/docs/graph-api/webhooks/reference/page/#feed

    Args:
        change: One change, already unwrapped from Meta's envelope.
        root_id: The id to use for the page's post instead of the one in
            the change. Reading comments by asking passes the video's own
            id here for a video, so `thread_root_id` is the id `publish`
            handed back; a webhook has no way to know it.

    Returns:
        What happened, in socialchimp's own words.
    """
    item = str(change.value.get("item", ""))
    verb = str(change.value.get("verb", ""))
    # Facebook's own words when we have no name of our own, so an app that
    # listens for everything still learns what happened.
    theirs = f"{item} {verb}".strip() or change.topic
    word = _OUR_WORD_FOR.get((item, verb), theirs)

    when = change.value.get("created_time")
    happened = (
        datetime.fromtimestamp(float(when), UTC)
        if isinstance(when, int | float) and not isinstance(when, bool)
        else change.when
    )

    return Update.from_network(
        update_id=_update_id(change, item=item, verb=verb),
        kind_name=word,
        platform=PLATFORM_NAME,
        # Meta says which page, not which of your connections. A login here
        # names a connection after its page, so the two line up without your
        # app keeping a table of its own. Rename connections and you match
        # them up yourself, from the page id on `envelope`.
        connection_id=f"{PLATFORM_NAME}:{change.account_id}",
        created_at=happened,
        # The change itself, so a handler reads `update.raw["message"]`
        # rather than walking the entry looking for its own change again.
        # The entry goes alongside, because the page id and the time are
        # only out there.
        raw=change.value,
        envelope=change.envelope,
        **_normalised(change.value, item=item, root_id=root_id),
    )


def _normalised(
    value: RawData,
    *,
    item: str,
    root_id: str | None,
) -> dict[str, Any]:
    """Work out the shared `Update` fields for one Facebook change.

    Args:
        value: The change, as Facebook pushes it.
        item: What it happened to, such as `"comment"`.
        root_id: The page post's id to use instead of the change's own.

    Returns:
        `actor`, `post_id`, `about_post_id` and `thread_root_id`, ready to
        hand to `Update.from_network`.
    """
    post_id = _text_of(value.get("post_id"))
    comment_id = _text_of(value.get("comment_id"))
    root = root_id or post_id
    fields: dict[str, Any] = {"actor": _person_from(value.get("from"))}

    if item == "comment":
        parent = _text_of(value.get("parent_id"))
        fields["post_id"] = comment_id
        fields["thread_root_id"] = root
        # A comment on the post names the post as its parent.
        fields["about_post_id"] = parent if parent and parent != post_id else root
    elif item in {"reaction", "like"}:
        fields["about_post_id"] = comment_id or root
        fields["thread_root_id"] = root
    else:
        fields["post_id"] = post_id
    return fields


def _text_of(value: object) -> str | None:
    """Keep a value only if it is a string with something in it.

    Args:
        value: Anything.

    Returns:
        The string, or `None`.
    """
    return value if isinstance(value, str) and value else None


def _person_from(raw: object) -> Person | None:
    """Build the person a change names in `from`.

    Args:
        raw: Facebook's `{"id", "name"}`, or anything else.

    Returns:
        The person, or `None` when Facebook did not say who - it leaves
        `from` off for people who have not allowed apps to see them.
    """
    if not isinstance(raw, dict):
        return None
    person_id = _text_of(raw.get("id"))
    if person_id is None:
        return None
    return Person(
        id=person_id,
        handle=None,
        display_name=_text_of(raw.get("name")),
        avatar_url=None,
        url=None,
        raw=raw,
    )


def _update_id(change: Change, *, item: str, verb: str) -> str:
    """Build an id that is the same every time this change arrives.

    Meta puts no identifier of its own on a change, and promises to deliver
    at least once - which is a promise to deliver twice sometimes. Without
    something stable here, one comment gets answered twice.

    Args:
        change: The change to name.
        item: What it happened to, such as `"comment"`.
        verb: What happened to it, such as `"add"`.

    Returns:
        An id built only from what Facebook said, so a retry of the same
        change produces the same one.
    """
    value = change.value
    who = value.get("from")
    parts = [
        change.account_id,
        change.topic,
        item,
        verb,
        str(value.get("comment_id") or value.get("post_id") or ""),
        str(who.get("id", "")) if isinstance(who, dict) else "",
        str(value.get("created_time") or int(change.when.timestamp())),
    ]
    return ":".join(parts)


class FacebookPlatform:
    """Everything socialchimp does with Facebook Pages.

    Signing people in, asking which of their pages to use, publishing now or
    later, and reading what Facebook pushes to you.

        facebook = FacebookPlatform()
        step = await facebook.start_login(request)

    It holds nothing between calls. Everything about a page arrives on the
    `Connection` and everything about your app on the `LoginRequest`, so one
    of these can be shared by your whole process, and two of them behave the
    same as one.

    Attributes:
        name: `"facebook"`.
        features: What Facebook can do here. It really can schedule, which
            almost nothing else can, and it really does push updates. There
            is no app to register anywhere in Meta, and a reply is a comment
            rather than a post, so `CREATE_APP` and `REPLY` are missing.
            A video is still encoding when `publish` answers, so this also
            has `check_state`.
    """

    name: str = PLATFORM_NAME

    features: Feature = (
        Feature.POST_TEXT
        | Feature.POST_IMAGE
        | Feature.POST_VIDEO
        | Feature.SCHEDULE
        | Feature.DELETE_POST
        | Feature.READ_STATS
        | Feature.PUSH_UPDATES
        | Feature.READ_UPDATES_AFTER
        | Feature.SUBSCRIBE_APP
    )

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        retries: Retries | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        biggest_video_bytes: int = BIGGEST_SIMPLE_VIDEO,
        recent_posts: int = RECENT_POSTS_TO_CHECK,
    ) -> None:
        """Set Facebook up for one app.

        Args:
            timeout: Seconds to wait for Facebook to answer. Worth raising
                if you send video, because the upload happens inside it.
            retries: How many times to try again after a hiccup. Left out,
                the shared default is used.
            transport: Where requests actually go. Leave it out for ordinary
                calls; pass your own to send them somewhere else.
            biggest_video_bytes: The largest video to send in one request.
                Lower it if your own server cannot hold a big file in
                memory - a failed upload here reads the whole thing again.
            recent_posts: How many of the page's latest posts `fetch_updates`
                reads comments from, between 1 and 100. Each one costs a
                request every time you poll.

        Raises:
            ConfigError: If `recent_posts` is outside 1 to 100.
        """
        if not 1 <= recent_posts <= MOST_POSTS_TO_CHECK:
            message = (
                f"recent_posts is {recent_posts}, but it has to be between 1 "
                f"and {MOST_POSTS_TO_CHECK} - Facebook lists no more than "
                f"that many posts in one request."
            )
            raise ConfigError(message)

        self._timeout = timeout
        self._retries = retries
        self._transport = transport
        self._biggest_video = biggest_video_bytes
        self._recent_posts = recent_posts
        self._usage: Usage | None = None

    @property
    def usage(self) -> Usage | None:
        """How much of your app's hourly allowance Facebook last said is gone.

        `None` until a reply mentions it. This is your whole app rather than
        one page, because that is how Meta counts. Watching it is how you
        slow down before Facebook stops answering rather than after.
        """
        return self._usage

    def _graph(self, token: str | None = None) -> Graph:
        """Start a conversation with Facebook.

        Args:
            token: The token to sign requests with - a page's for posting, a
                person's while signing in, and none at all while swapping a
                code.

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
                errors=facebook_errors,
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
        """Return where Facebook's API lives.

        One address for everybody, unlike Mastodon. The connection is taken
        because every platform's is.

        Args:
            connection: The account we are about to act as. Not used here.

        Returns:
            The address, with no trailing slash.
        """
        return GRAPH_API

    def auth_headers(self, connection: Connection) -> Mapping[str, str]:
        """Return the header that proves we may act as this page.

        Meta also takes the token as an `access_token` query parameter, and
        this uses the header instead: a token in a web address ends up in
        server logs, proxy logs and browser history, and stays there.

        Args:
            connection: The account we are acting as.

        Returns:
            One `Authorization` header carrying the page's own token.
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
        """Return what Facebook allows.

        Nothing is asked, because there is nothing to ask: unlike Mastodon,
        where whoever runs a server sets its post length, these numbers are
        the same for every page. This stays `async` because every platform's
        `limits` is.

        How many pictures one post may carry is left out rather than guessed
        at - Meta publishes no number, and `None` means "we do not know"
        while a made-up number would refuse posts Facebook would have taken.

        Args:
            connection: The account to ask about. Not used here.

        Returns:
            What Facebook allows right now.
        """
        return Limits(
            max_text_length=MAX_TEXT_LENGTH,
            # Facebook really does mean characters, which is rarer than it
            # sounds - Bluesky means letters and Threads means bytes.
            text_counted_in=TextCount.CHARACTERS,
            max_videos=MAX_VIDEOS_PER_POST,
            max_video_bytes=self._biggest_video,
        )

    async def start_login(self, request: LoginRequest) -> SendToNetwork:
        """Build the address to send somebody to so they can approve your app.

        Nothing is sent to Facebook here. There is also nothing to remember
        between this call and the next: unlike Mastodon there is no secret to
        carry, because the swap at the end is signed with your app secret,
        which never leaves your server.

        Args:
            request: Where to send them back to, what to ask for, and your
                app's credentials.

        Returns:
            The address to redirect to, and the state that will come back.

        Raises:
            ConfigError: If the request carries no app credentials.
        """
        client_id, _ = _app_on(request)
        state = state_for(request)

        return SendToNetwork(
            url=sign_in_url(
                client_id=client_id,
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
        """Swap the code for a token and ask which page to use.

        Three things happen here. The code becomes a token that lasts an
        hour; that token is traded for one that lasts about sixty days,
        which has to happen while the first still works; and Facebook is
        asked which pages this person manages.

        This never finishes a login on its own. Even somebody with a single
        page is asked, so your app has one path through this rather than two.

        Args:
            request: The same request used to start the login.
            callback: The query values Facebook sent back. It must have
                `code`; `state` is checked when it is there.
            remember: Not used. Nothing has to survive between the two calls
                here.

        Returns:
            The pages to choose between, and a `resume_token` to hand back to
            `resume_login`. That token is the person's own - keep it in their
            session, not in a URL.

        Raises:
            AuthError: If the person said no, if there is no code, if the
                state that came back is not the one we sent, or if they
                manage no pages we can post to.
            ConfigError: If the request carries no app credentials.
            SocialChimpError: If Facebook refuses any of the three steps.
        """
        client_id, client_secret = _app_on(request)
        check_state(request, callback, platform=PLATFORM_NAME)
        code = code_from(callback, platform=PLATFORM_NAME)

        async with self._graph() as graph:
            short = await swap_code_for_token(
                graph,
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=request.redirect_uri,
                code=code,
            )
            # Traded now rather than later because there is no later: Meta
            # gives out no refresh token, and once the hour is up the only
            # way back is to sign the person in again.
            long = await long_lived_token(
                graph,
                client_id=client_id,
                client_secret=client_secret,
                token=short.access_token,
            )
            self._note(graph)

        async with self._graph(long.access_token) as graph:
            pages = await pages_of(graph)
            self._note(graph)

        if not pages:
            message = (
                "This person signed in, but there are no Facebook pages we "
                "can post to. Either they manage none, or they left every "
                "page unticked on Facebook's own page picker while signing "
                "in. Ask them to connect their account again and tick the "
                "page they want."
            )
            raise AuthError(message, platform=PLATFORM_NAME)

        return ChooseAccount(
            options=tuple(
                AccountChoice(id=page.id, name=page.name, kind="page") for page in pages
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
        """Finish the login with the page the person picked.

        The page is looked up again rather than remembered, so a page they
        no longer manage is caught here with a message rather than saved as a
        connection that cannot post.

        Args:
            request: The same request the login was started with.
            resume_token: The value from `ChooseAccount`. It carries the
                person's own token.
            account_id: The id of the page they picked.
            remember: Not used.

        Returns:
            The finished connection. Save it. Its token is the page's own,
            and that one does not expire.

        Raises:
            AuthError: If the resume token did not come back, or Facebook
                will not give us a token for that page.
            SocialChimpError: If Facebook refuses the lookup.
        """
        if not resume_token:
            message = (
                "This sign-in cannot be carried on because the resume_token "
                "from ChooseAccount did not come back. Keep it with that "
                "person's session and pass it to resume_login. Without it "
                "there is no way to ask Facebook for the page's token, so "
                "start a new one."
            )
            raise AuthError(message, platform=PLATFORM_NAME)

        async with self._graph(resume_token) as graph:
            page = await page_by_id(graph, page_id=account_id)
            self._note(graph)

        return Finished(
            connection=Connection(
                # Named after the page rather than the person, because the
                # page is what gets posted to - and because an update pushed
                # to us names the page and nothing else.
                id=f"{PLATFORM_NAME}:{page.id}",
                platform=PLATFORM_NAME,
                host=None,
                account_id=page.id,
                account_name=page.name,
                # A page token taken from a long-lived person's token does
                # not expire, so no expiry is set. That surprises people, and
                # it is the reason `refresh` below has nothing to do.
                token=Token(access_token=page.token),
                scopes=request.scopes or DEFAULT_SCOPES,
                # Read off the same lookup above - `page_by_id` already asks
                # for `picture{url}` through `PAGE_FIELDS` - so this costs no
                # extra request. Already a clean address or `None`, courtesy
                # of `meta_picture_url`.
                avatar_url=page.avatar_url,
                extra={
                    "page_id": page.id,
                    "page_name": page.name,
                    "category": page.category,
                    "profile_url": f"https://www.facebook.com/{page.id}",
                },
            )
        )

    async def read_profile(self, connection: Connection) -> AccountProfile:
        """Ask Facebook for this page's current name and picture.

        One request, to the page's own object - the same one `resume_login`
        already looks up, but asking for only a name and a picture. Worth
        calling again later: Facebook's picture address goes stale after a
        while, which is why it is not something to keep saved forever.

        Args:
            connection: The page to ask about.

        Returns:
            The page's name, and its picture if it has one right now.

        Raises:
            ConfigError: If the connection names no Facebook page.
            SocialChimpError: If Facebook refuses the question.
        """
        page_id = where_to_post(
            connection,
            key="page_id",
            what="Facebook page",
            platform=PLATFORM_NAME,
        )

        async with self._graph(connection.token.access_token) as graph:
            reply = await graph.json(
                "GET", f"/{page_id}", params={"fields": PROFILE_FIELDS}
            )
            self._note(graph)

        name = reply.get("name")
        return AccountProfile(
            # A page always has a name, but showing the id is better than
            # showing nothing if one ever arrives without.
            name=name if isinstance(name, str) and name else page_id,
            avatar_url=meta_picture_url(reply),
        )

    async def refresh(
        self,
        connection: Connection,
        app: AppCredentials | None = None,
    ) -> Token:
        """Give the connection a token that is good for a while yet.

        A page token taken from a long-lived person's token does not expire,
        which is why most connections here have no expiry at all and this
        hands the same token straight back without asking Meta anything.

        A token that does have an expiry is traded in for a fresh sixty
        days. That trade is the whole of renewal on Meta: there is no
        refresh token anywhere in it, so a token is extended while it still
        works or it is gone. socialchimp renews before the expiry rather
        than after it, which is what makes this possible at all.

        Args:
            connection: The account whose token is running out.
            app: Your app's id and secret. Meta signs the trade with both,
                so a token with an expiry cannot be extended without them.

        Returns:
            The token to save. The same one for a page token that never
            expires, a new one for anything else.

        Raises:
            ConfigError: If the token needs extending and no credentials
                arrived.
            TokenExpiredError: If Meta will not make the trade, which means
                the token has already run out or been revoked. The person
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
                    f"It has already run out, or the person removed your app "
                    f"from their Facebook settings, or the page changed "
                    f"hands. There is no refresh token to fall back on, so "
                    f"the person has to connect their account again - and "
                    f"signing in through socialchimp saves the page's own "
                    f"token, which does not expire."
                )
                raise TokenExpiredError(
                    message, platform=PLATFORM_NAME, raw=refused.raw
                ) from refused
            self._note(graph)

        return extended

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        """Publish a post, now or later.

        Words and pictures go to the page's feed; a video goes somewhere
        else, because Facebook keeps them apart. Pictures are uploaded first
        without being published, so nothing appears on the page until the
        post that names them does.

        Args:
            connection: The page to publish as.
            post: What to publish.

        Returns:
            What Facebook said about the new post. A scheduled post comes
            back as `PostState.SCHEDULED`; a video comes back as
            `PostState.PROCESSING`, because Facebook is still encoding it
            when it answers - ask `check_state` with the id to find out how
            that ended.

        Raises:
            ConfigError: If the connection names no page.
            InvalidPostError: If a setting is unknown, if the post breaks one
                of Facebook's limits, or if it mixes video with pictures.
            NotSupportedError: If the post needs something Facebook cannot do
                here, such as replying, or carrying a video over a gigabyte.
            SocialChimpError: If Facebook refuses the post.
        """
        page_id = where_to_post(
            connection,
            key="page_id",
            what="Facebook page",
            platform=PLATFORM_NAME,
        )
        # Everything that can be judged without asking Facebook is judged
        # first, so a mistake costs no request and no part of the hourly
        # allowance.
        options = _checked_options(post.options)
        check_post(
            post,
            platform=PLATFORM_NAME,
            features=self.features,
            limits=await self.limits(connection),
        )
        pictures, videos = _split_media(post)
        when = _scheduled_time(post)

        async with self._graph(connection.token.access_token) as graph:
            try:
                if videos:
                    return await self._publish_video(
                        graph, page_id, post, videos[0], when
                    )
                return await self._publish_to_feed(
                    graph, page_id, post, pictures, options, when
                )
            finally:
                self._note(graph)

    async def _publish_to_feed(
        self,
        graph: Graph,
        page_id: str,
        post: Post,
        pictures: list[Media],
        options: dict[str, str],
        when: int | None,
    ) -> PostResult:
        """Put words, and any pictures, on the page's feed.

        Args:
            graph: A conversation signed with the page's token.
            page_id: Which page.
            post: What to publish.
            pictures: The pictures to hang off it, which may be none.
            options: The settings already checked.
            when: When to publish, as seconds since 1970, or `None` for now.

        Returns:
            What Facebook said about the new post.

        Raises:
            PlatformError: If Facebook answered without an id.
        """
        form: dict[str, Any] = {"message": post.text, **options}

        for index, picture in enumerate(pictures):
            # Every picture takes the same route, one or twelve. A single
            # picture could go straight to /photos with a caption instead,
            # and having two ways to do it would mean the rarer one is the
            # one that breaks.
            form[f"attached_media[{index}]"] = json.dumps(
                {"media_fbid": await self._upload_picture(graph, page_id, picture)}
            )

        _add_timing(form, when)
        reply = await graph.json("POST", f"/{page_id}/feed", data=form)
        post_id = required_text(
            reply, "id", platform=PLATFORM_NAME, when="publish a post"
        )

        if when is not None:
            # A scheduled post has no address yet, because it is not on the
            # page yet. Facebook has taken a plan, not published a post.
            return PostResult(
                id=post_id, url=None, state=PostState.SCHEDULED, raw=reply
            )
        return PostResult(
            id=post_id,
            url=f"https://www.facebook.com/{post_id}",
            state=PostState.DONE,
            raw=reply,
        )

    async def _upload_picture(
        self,
        graph: Graph,
        page_id: str,
        picture: Media,
    ) -> str:
        """Send one picture to a page without publishing it.

        Args:
            graph: A conversation signed with the page's token.
            page_id: Which page.
            picture: The picture to send.

        Returns:
            Facebook's id for it, to name on the post.

        Raises:
            PlatformError: If Facebook answered without an id.
        """
        # Unpublished, so nothing appears on the page until the post that
        # names it does. Facebook throws away an unused one after a day.
        form: dict[str, Any] = {"published": "false"}
        if picture.alt_text:
            form["alt_text_custom"] = picture.alt_text

        if picture.content is None and picture.path is None:
            # Facebook will fetch it for us, which Mastodon and Bluesky will
            # not - so a Media.from_url costs nothing here.
            form["url"] = picture.url
            reply = await graph.json("POST", f"/{page_id}/photos", data=form)
        else:
            reply = await graph.json(
                "POST",
                f"/{page_id}/photos",
                data=form,
                files={
                    "source": (
                        picture.filename or "upload",
                        picture.read(),
                        picture.content_type,
                    )
                },
            )

        return required_text(reply, "id", platform=PLATFORM_NAME, when="take a picture")

    async def _publish_video(
        self,
        graph: Graph,
        page_id: str,
        post: Post,
        video: Media,
        when: int | None,
    ) -> PostResult:
        """Send one video in a single request.

        A bigger video has to go up in pieces, over an upload Facebook keeps
        open across several requests. That is a job in itself, so this
        refuses rather than half-doing it - and the refusal is where that
        work belongs when somebody writes it.

        Args:
            graph: A conversation signed with the page's token.
            page_id: Which page.
            post: What to publish, whose text becomes the description.
            video: The video to send.
            when: When to publish, as seconds since 1970, or `None` for now.

        Returns:
            What Facebook said about the new video.

        Raises:
            NotSupportedError: If the video is bigger than one request can
                carry.
            PlatformError: If Facebook answered without an id.
        """
        form: dict[str, Any] = {"description": post.text}
        _add_timing(form, when)

        content = None if video.content is None and video.path is None else video.read()
        if content is None:
            form["file_url"] = video.url
            reply = await graph.json("POST", f"/{page_id}/videos", data=form)
        else:
            if len(content) > self._biggest_video:
                raise _video_too_big(len(content), self._biggest_video)
            reply = await graph.json(
                "POST",
                f"/{page_id}/videos",
                data=form,
                files={
                    "source": (
                        video.filename or "upload",
                        content,
                        video.content_type,
                    )
                },
            )

        video_id = required_text(
            reply, "id", platform=PLATFORM_NAME, when="publish a video"
        )
        if when is not None:
            return PostResult(
                id=video_id, url=None, state=PostState.SCHEDULED, raw=reply
            )
        return PostResult(
            id=video_id,
            url=f"https://www.facebook.com/{page_id}/videos/{video_id}",
            # Facebook carries on encoding after it answers, so the post is
            # not live the moment this returns.
            state=PostState.PROCESSING,
            raw=reply,
        )

    async def check_state(self, connection: Connection, post_id: str) -> PostResult:
        """Ask Facebook how far it has got with a video.

        A video comes back from `publish` as `PostState.PROCESSING`, because
        Facebook is still encoding it when it answers. This is how that gets
        resolved: one cheap read of the video's `status`, as often as you
        like, until it says `DONE` or `FAILED`.

        Only a video needs this. Words and pictures are live the moment
        `publish` returns, and a scheduled post is a plan Facebook is
        holding rather than something it is working on.

        Args:
            connection: The page the video is on.
            post_id: Facebook's id for the video, as `publish` handed it
                back.

        Returns:
            Where the video has got to. `PROCESSING` while Facebook is still
            encoding it, `DONE` once it is live, and `FAILED` if Facebook
            gave up on it. The address is the same one `publish` gave, so a
            result from either can be treated the same way.

        Raises:
            ConfigError: If the connection names no page.
            NotFoundError: If there is no such video on this page.
            PlatformError: If Facebook answers without saying anything about
                the state, which is what happens when the id is not a
                video's.
            SocialChimpError: If Facebook refuses the question.
        """
        page_id = where_to_post(
            connection,
            key="page_id",
            what="Facebook page",
            platform=PLATFORM_NAME,
        )

        async with self._graph(connection.token.access_token) as graph:
            reply = await graph.json("GET", f"/{post_id}", params={"fields": "status"})
            self._note(graph)

        said = reply.get("status")
        if not isinstance(said, dict):
            raise _nothing_said_about(post_id, reply)

        reported = str(said.get("video_status"))
        return PostResult(
            id=post_id,
            url=f"https://www.facebook.com/{page_id}/videos/{post_id}",
            state=_OUR_STATE_FOR.get(reported, PostState.PROCESSING),
            raw=reply,
        )

    async def delete_post(self, connection: Connection, post_id: str) -> None:
        """Remove a post.

        Args:
            connection: The page that published it.
            post_id: Facebook's id for the post, as `publish` handed it back.

        Raises:
            NotFoundError: If there is no such post on this page.
            SocialChimpError: If Facebook refuses.
        """
        async with self._graph(connection.token.access_token) as graph:
            await graph.json("DELETE", f"/{post_id}")
            self._note(graph)

    async def read_stats(self, connection: Connection, post_id: str) -> PostStats:
        """Ask Facebook how a post is doing.

        One small request: Facebook is asked for the counts and for none of
        the reactions or comments behind them.

        `likes` is every kind of reaction added together - love, wow and the
        rest as well as the thumbs-up - because that is the one number
        Facebook gives. `comments` is what Facebook reports for the post. A
        video's `shares` is `None`, not `0`: we have no way to ask a video
        that, and a zero somebody can chart is worse than a gap they can see.
        A post nobody has shared has no `shares` on it at all, and that does
        mean `0`.

        Args:
            connection: The page the post is on.
            post_id: Facebook's id for the post, as `publish` handed it back.

        Returns:
            Its reactions, comments and shares, under socialchimp's own
            names: `likes`, `comments` and `shares`.

        Raises:
            NotFoundError: If there is no such post.
            PlatformError: If Facebook answers without an id.
            SocialChimpError: If Facebook refuses - most often because the
                person connected before `pages_read_engagement` was asked for.
        """
        # A post's id is `<page>_<post>`; a video's is a bare number, and a
        # video has no `shares` to ask about.
        is_a_post = "_" in post_id
        fields = f"{STATS_FIELDS},shares" if is_a_post else STATS_FIELDS

        async with self._graph(connection.token.access_token) as graph:
            reply = await graph.json("GET", f"/{post_id}", params={"fields": fields})
            self._note(graph)

        return PostStats(
            id=required_text(
                reply, "id", platform=PLATFORM_NAME, when="read a post's numbers"
            ),
            likes=_total_in(reply, "reactions"),
            comments=_total_in(reply, "comments"),
            shares=_shares_in(reply) if is_a_post else None,
            raw=reply,
        )

    async def fetch_updates(
        self,
        connection: Connection,
        since: datetime | None,
    ) -> Sequence[Update]:
        """Read the comments on the page's latest posts.

        Facebook can push these to you, and this is for when you would rather
        ask, or cannot receive a request from the internet. What comes back
        has the same shape a webhook's would - and the same `id`, so a
        `Dispatcher` given a `SeenUpdates` answers a comment once even when
        it turns up both ways.

        The latest `recent_posts` posts are read, one request each, newest
        comment first. A post is left as soon as a comment no newer than
        `since` turns up, so a poll of a quiet page costs about one request
        per post. Replies to comments are included.

        Args:
            connection: The page to read.
            since: Only return comments newer than this. `None` on the first
                call, when there is no marker saved yet - the latest hundred
                comments on each post, rather than the whole history.

        Returns:
            The comments, oldest first, as `UpdateKind.COMMENT_CREATED`.

        Raises:
            ConfigError: If the connection names no page.
            SocialChimpError: If Facebook refuses - most often because the
                person connected before `pages_read_user_content` was asked
                for and has to connect again.
        """
        page_id = where_to_post(
            connection,
            key="page_id",
            what="Facebook page",
            platform=PLATFORM_NAME,
        )

        found: list[Update] = []
        async with self._graph(connection.token.access_token) as graph:
            try:
                for read_from, root_id in await self._recent_posts_of(graph, page_id):
                    found.extend(
                        await _comments_on(
                            graph, page_id, read_from, since, root_id=root_id
                        )
                    )
            finally:
                self._note(graph)

        found.sort(key=lambda update: update.created_at)
        return found

    async def _recent_posts_of(
        self,
        graph: Graph,
        page_id: str,
    ) -> list[tuple[str, str]]:
        """List the page's latest posts, and the id each is known by.

        One request: `GET /{page}/published_posts`.

        Args:
            graph: A conversation signed with the page's token.
            page_id: Which page.

        Returns:
            For each post, the id to read its comments from and the id to
            name it by - the same for most, and the video's own id for a
            video, which is what `publish` handed back.
        """
        posts, _ = await read_edge(
            graph,
            f"/{page_id}/published_posts",
            params={"fields": POST_FIELDS, "limit": self._recent_posts},
        )
        found: list[tuple[str, str]] = []
        for post in posts:
            post_id = _text_of(post.get("id"))
            if post_id is not None:
                found.append((post_id, _video_id_of(post) or post_id))
        return found

    async def fetch_updates_after(
        self,
        connection: Connection,
        marker: str | None,
        *,
        limit: int | None = None,
    ) -> UpdateBatch:
        """Read the comments that are new since a marker.

        Facebook has no list of every comment on a page, so this reads the
        page's latest `recent_posts` posts (25 unless the platform was built
        with another number) and each one's comments, newest first, stopping
        at the first comment older than the marker. One request lists the
        posts and usually one more per post: 26 on a quiet page with the
        default 25 posts. A busy post can take up to `MOST_COMMENT_PAGES`,
        so the most one call ever sends is 1 + `recent_posts` x 10 (251 by
        default); the first call, with no marker, sends at most
        1 + `recent_posts`. Replies to comments are included. It needs
        `pages_read_user_content`, which is in `DEFAULT_SCOPES`. See
        https://developers.facebook.com/docs/graph-api/reference/object/comments/

        The marker is the time of the newest comment handed back, with the
        ids of every comment at that second that was - so nothing is handed
        back twice and nothing sharing that second is lost.

        What this cannot see, and webhooks (`Feature.PUSH_UPDATES`) can -
        each lost for good, not just late, since the marker moves on past it:

        - a comment on a post older than the latest `recent_posts`;
        - on one post, more than `MOST_COMMENT_PAGES` x
          `COMMENTS_PER_REQUEST` (1,000) new comments between two calls -
          the oldest past that are never reached;
        - a comment Facebook only shows after one with a later time was
          already handed back - one held for review and then approved, say -
          because its time is behind the marker.

        Args:
            connection: The page to read.
            marker: The `UpdateBatch.marker` from the last call, or `None`
                the first time. `None` reads the latest page of comments on
                each post as a starting point, not the whole history.
            limit: The most comments to hand back. With a marker, the oldest
                that many, with `more` set when there are others waiting; on
                the first call, the newest. `None` hands back all of them.

        Returns:
            The new comments, oldest first, as `COMMENT_CREATED` updates,
            and the marker to store for next time. The marker is `None` only
            while the page has never had a comment.

        Raises:
            ConfigError: If the connection names no page, or `marker` is not
                one this platform made. Starting afresh instead would quietly
                skip whatever came in between, so it refuses - pass `None` to
                start afresh on purpose.
            SocialChimpError: If Facebook refuses - most often because the
                person connected before `pages_read_user_content` was asked
                for, and has to connect again.
        """
        if limit is not None and limit < 1:
            message = (
                f"limit is {limit}, but it has to be 1 or more - or None for "
                f"every new comment."
            )
            raise ConfigError(message)
        since = _own_marker(marker) if marker is not None else None
        page_id = where_to_post(
            connection,
            key="page_id",
            what="Facebook page",
            platform=PLATFORM_NAME,
        )

        found: list[Update] = []
        async with self._graph(connection.token.access_token) as graph:
            try:
                for read_from, root_id in await self._recent_posts_of(graph, page_id):
                    found.extend(
                        await _comments_on(
                            graph,
                            page_id,
                            read_from,
                            since[0] if since is not None else None,
                            root_id=root_id,
                            seen=since[1] if since is not None else frozenset(),
                        )
                    )
            finally:
                self._note(graph)

        found.sort(key=lambda update: (update.created_at, update.id))
        more = False
        if limit is not None and len(found) > limit:
            if since is None:
                # A starting point is the latest comments, not the oldest.
                found = found[-limit:]
            else:
                found = found[:limit]
                more = True

        return UpdateBatch(
            updates=tuple(found),
            marker=_marker_after(found, since) if found else marker,
            more=more,
        )

    async def mark_seen(self, connection: Connection, marker: str) -> None:
        """Check a marker is one of ours. Facebook itself is not told.

        Facebook keeps no "seen" for a page's comments, so there is nothing
        to send; this exists so `Account.mark_seen` works the same on every
        network that reads from a marker.

        Args:
            connection: The page. Not used.
            marker: The marker that has been handled.

        Raises:
            ConfigError: If `marker` is not one this platform made.
        """
        _own_marker(marker)

    async def subscribe_to_updates(
        self,
        connection: Connection,
        *,
        fields: Sequence[str] | None = None,
    ) -> None:
        """Subscribe the page to your app, so Facebook pushes its updates.

        Without this a page sends your webhook nothing, however the webhook
        is set up in Meta's dashboard. Facebook keeps one subscription per
        app and page, so calling this again is harmless.

        **The fields replace the ones already there.** Subscribing to
        `("feed",)` after `("feed", "mention")` stops the mentions; pass
        every field you want, every time.

        One request: `POST /{page-id}/subscribed_apps` with the page's own
        token. See
        https://developers.facebook.com/docs/graph-api/reference/page/subscribed_apps/

        Args:
            connection: The page to subscribe.
            fields: Facebook's names for what to push, such as `"feed"` or
                `"mention"`. `None` is `PUSH_FIELDS`: the page's comments,
                reactions and posts.

        Raises:
            ConfigError: If the connection names no page, or `fields` is
                empty, a bare string, or has a name Facebook could not read.
            NotAllowedError: If the page never granted
                `pages_manage_metadata` - it signed in without
                `PUSH_SCOPES`, and has to connect again with them.
            PlatformError: If Facebook answers that it did not work.
            SocialChimpError: If Facebook refuses for any other reason.
        """
        wanted = _push_fields(PUSH_FIELDS if fields is None else fields)
        page_id = where_to_post(
            connection,
            key="page_id",
            what="Facebook page",
            platform=PLATFORM_NAME,
        )
        async with self._graph(connection.token.access_token) as graph:
            reply = await graph.json(
                "POST",
                f"/{page_id}/subscribed_apps",
                data={"subscribed_fields": ",".join(wanted)},
            )
            self._note(graph)
        if reply.get("success") is not True:
            message = (
                f"Facebook did not subscribe page {page_id} to the app; it "
                f"answered {reply!r}."
            )
            raise PlatformError(message, platform=PLATFORM_NAME, raw=reply)

    def check_signature(
        self,
        body: bytes,
        headers: Mapping[str, str],
        *,
        secret: str,
    ) -> None:
        """Check a request Facebook pushed to us really came from Facebook.

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
        """Answer the one-off question Facebook asks before it sends anything.

        Point Facebook at a URL of yours and it does a GET to it first, with
        a token you chose and a challenge. Echo the challenge back as plain
        text and the URL starts working. Get it wrong and Facebook says the
        URL could not be verified, without saying why.

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

        Facebook batches when it is busy, which is exactly when you least
        want to drop the rest, so this hands back all of them.

        Args:
            body: The request body, untouched. Check its signature first.

        Returns:
            What happened, in the order Facebook listed it. Empty when the
            message carried nothing we can act on.

        Raises:
            PlatformError: If the body is not one of Facebook's messages.
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

        One message from Facebook can carry several changes, and this hands
        back the first of them. Use `read_updates` to see them all - on a
        busy page that is the one you want.

        Only call this after `check_signature` has passed.

        Args:
            body: The request body, untouched.
            headers: The request headers. Not needed here; the signature
                header has already done its job by this point.

        Returns:
            What happened, in socialchimp's own words.

        Raises:
            PlatformError: If the body is not one of Facebook's messages, or
                carries no change at all.
        """
        return first_update(self.read_updates(body), platform=PLATFORM_NAME)


def _total_in(reply: RawData, edge: str) -> int | None:
    """Read the count Facebook sends alongside a list it was told not to send.

    Args:
        reply: What Facebook answered about a post.
        edge: Which list, such as `"reactions"`.

    Returns:
        The total, or `None` when the reply has no such number - which is not
        the same as none at all.
    """
    listed = reply.get(edge)
    summary = listed.get("summary") if isinstance(listed, dict) else None
    total = summary.get("total_count") if isinstance(summary, dict) else None
    if isinstance(total, int) and not isinstance(total, bool):
        return total
    return None


def _shares_in(reply: RawData) -> int:
    """Read how many times a post was shared.

    Args:
        reply: What Facebook answered about a post, which was asked for its
            `shares`.

    Returns:
        The count. Facebook leaves `shares` off a post nobody has shared
        rather than sending a zero, so a reply without it is `0`.
    """
    shared = reply.get("shares")
    count = shared.get("count") if isinstance(shared, dict) else None
    if isinstance(count, int) and not isinstance(count, bool):
        return count
    return 0


def _when_written(value: object) -> datetime | None:
    """Read the time on a comment.

    Facebook writes it as text (`2026-09-22T10:00:00+0000`) when asked for a
    comment and as seconds since 1970 when it pushes one, so both are read.

    Args:
        value: What Facebook put in `created_time`.

    Returns:
        The moment, with a timezone, or `None` if it is not a time we can
        read.
    """
    if isinstance(value, int | float) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), UTC)
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _as_pushed(comment: RawData, *, post_id: str) -> RawData | None:
    """Reshape a comment we asked for into the shape Facebook pushes.

    An app that handles a comment should not have to care which way it
    arrived, so `raw` looks the same either way.

    Args:
        comment: One entry from a post's comments.
        post_id: The post it is on.

    Returns:
        What a webhook would have carried, or `None` if the comment has no id
        or no readable time - without those there is nothing stable to name
        it by.
    """
    comment_id = comment.get("id")
    written = _when_written(comment.get("created_time"))
    if not isinstance(comment_id, str) or not comment_id or written is None:
        return None

    parent = comment.get("parent")
    parent_id = parent.get("id") if isinstance(parent, dict) else None

    value: RawData = {
        "item": "comment",
        "verb": "add",
        "comment_id": comment_id,
        "post_id": post_id,
        # A reply names the comment it answers; a comment on the post itself
        # names the post.
        "parent_id": parent_id if isinstance(parent_id, str) else post_id,
        "message": comment.get("message", ""),
        "created_time": int(written.timestamp()),
    }
    for key in ("from", "permalink_url"):
        if key in comment:
            value[key] = comment[key]
    return value


async def _comments_on(
    graph: Graph,
    page_id: str,
    post_id: str,
    since: datetime | None,
    *,
    root_id: str,
    seen: frozenset[str] | None = None,
) -> list[Update]:
    """Read the comments on one post that are newer than a moment.

    Args:
        graph: A conversation signed with the page's token.
        page_id: Which page.
        post_id: Which post, as the page's feed lists it.
        since: Only comments newer than this, or `None` for the latest page
            of them.
        root_id: The id to name the post by on each update - its own, or
            the video's for a video.
        seen: For a marker: the comments at exactly `since` already handed
            back. Others at that second still count as new. `None` for the
            older `fetch_updates`, where `since` itself counts as old.

    Returns:
        The comments as updates, in the order Facebook listed them.
    """
    updates: list[Update] = []
    after: str | None = None
    # With nothing to compare against there is no natural place to stop, so
    # a first call reads one page - the latest - rather than a whole history.
    most_pages = 1 if since is None else MOST_COMMENT_PAGES

    for _ in range(most_pages):
        params: dict[str, object] = {
            # Every comment, replies included, and newest first: that is what
            # lets us stop at the first one we have already seen.
            "filter": "stream",
            "order": "reverse_chronological",
            "fields": COMMENT_FIELDS,
            "limit": COMMENTS_PER_REQUEST,
        }
        if after is not None:
            params["after"] = after
        entries, after = await read_edge(graph, f"/{post_id}/comments", params=params)

        caught_up = False
        for entry in entries:
            value = _as_pushed(entry, post_id=post_id)
            if value is None:
                continue
            written = datetime.fromtimestamp(float(value["created_time"]), UTC)
            if since is not None and (
                written < since or (written == since and seen is None)
            ):
                caught_up = True
                break
            if seen is not None and written == since and value["comment_id"] in seen:
                continue
            updates.append(
                _update_from(
                    Change(
                        account_id=page_id,
                        when=written,
                        topic="feed",
                        value=value,
                    ),
                    root_id=root_id,
                )
            )
        if caught_up or after is None:
            break
    return updates


def _video_id_of(post: RawData) -> str | None:
    """Find the video's own id on a post that is a video.

    Args:
        post: One of the page's posts, asked for with `POST_FIELDS`.

    Returns:
        The video's id - the one `publish` handed back - or `None` for a
        post that is not a video.
    """
    attachments = post.get("attachments")
    data = attachments.get("data") if isinstance(attachments, dict) else None
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or item.get("media_type") != "video":
            continue
        target = item.get("target")
        if isinstance(target, dict):
            return _text_of(target.get("id"))
    return None


# What we say about a marker this platform did not make. Treating it as
# `None` would quietly start again from the latest comments and skip
# everything in between, so it is refused instead.
_BAD_MARKER_MESSAGE: Final = (
    "This marker was not made by Facebook's platform, so there is nothing "
    "safe to resume from. Pass None to start afresh."
)


def _own_marker(marker: str) -> tuple[datetime, frozenset[str]]:
    """Read a marker, insisting it is one this platform made.

    Args:
        marker: The marker to read.

    Returns:
        The moment it names, and the comments at that second already handed
        back.

    Raises:
        ConfigError: If this is not a marker this platform made.
    """
    prefix, _, rest = marker.partition(":")
    seconds, _, ids = rest.partition(":")
    seen = frozenset(found for found in ids.split(",") if found)
    if (
        prefix != MARKER_PREFIX
        or not (seconds.isascii() and seconds.isdigit())
        or not seen
    ):
        raise ConfigError(_BAD_MARKER_MESSAGE)
    try:
        return datetime.fromtimestamp(int(seconds), UTC), seen
    except (OverflowError, ValueError, OSError) as problem:
        raise ConfigError(_BAD_MARKER_MESSAGE) from problem


def _marker_after(
    handed_back: list[Update],
    since: tuple[datetime, frozenset[str]] | None,
) -> str:
    """Make the marker that resumes just after what was handed back.

    Args:
        handed_back: The updates handed back, oldest first. Never empty.
        since: What the last marker said, if there was one.

    Returns:
        The new marker.
    """
    newest = handed_back[-1].created_at
    seen = {
        str(update.raw["comment_id"])
        for update in handed_back
        if update.created_at == newest
    }
    if since is not None and since[0] == newest:
        seen |= since[1]
    return f"{MARKER_PREFIX}:{int(newest.timestamp())}:{','.join(sorted(seen))}"


def _add_timing(form: dict[str, Any], when: int | None) -> None:
    """Say whether a post goes out now or later.

    Args:
        form: The form about to be sent, changed in place.
        when: The moment to publish, as seconds since 1970, or `None`.
    """
    if when is None:
        form["published"] = "true"
        return
    # Both are needed together. `scheduled_publish_time` on its own is
    # ignored, and the post goes out immediately.
    form["published"] = "false"
    form["scheduled_publish_time"] = str(when)


def _nothing_said_about(post_id: str, reply: RawData) -> PlatformError:
    """Build the error for a reply that says nothing about a video's state.

    Args:
        post_id: The id we asked about.
        reply: What Facebook answered, kept on the error.

    Returns:
        The error to raise, saying what `check_state` is for.
    """
    message = (
        f"Facebook said nothing about how far it has got with {post_id!r}. "
        f"check_state is for the video ids publish handed back as "
        f"PROCESSING; a post of words or pictures is already live when "
        f"publish answers, and a scheduled one goes out on its own. The "
        f"whole reply is on this error."
    )
    return PlatformError(message, platform=PLATFORM_NAME, raw=reply)


def _video_too_big(size: int, allowed: int) -> NotSupportedError:
    """Build the error for a video that will not fit in one request.

    Args:
        size: How big the video is, in bytes.
        allowed: The largest we will send in one request.

    Returns:
        The error to raise, saying what to do about it.
    """
    return NotSupportedError(
        platform=PLATFORM_NAME,
        what=(
            f"sending a video this big in one request - {size:,} bytes, "
            f"where {allowed:,} is the most this sends at once"
        ),
        suggestion=(
            "Facebook wants anything larger in pieces, over an upload it "
            "keeps open across several requests, and socialchimp does not do "
            "that yet. Send a smaller or shorter file - under a gigabyte and "
            "under twenty minutes - or put it on Facebook another way."
        ),
    )
