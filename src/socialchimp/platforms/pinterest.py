"""Pinterest: the network where a post has to go somewhere.

Everywhere else socialchimp goes, a post lands in a feed and that is the end
of it. Pinterest has no feed to post to. **Every pin lives on a board**, and
there is no such thing as a pin without one. That single fact shapes this
whole file.

## Trial access, and why your pins look like they worked

This is the thing that costs people an afternoon, so it comes first.

A new app gets **Trial access**. On Trial your code runs against the real
API, over the real internet, with real credentials, and gets real 2xx replies
carrying real pin ids. And **only you can see the pins**. They are not on the
public profile, they are not in anybody's home feed, and a friend looking at
the account sees nothing. Nothing anywhere says this is happening.

So the first thing to check when a pin "did not appear" is not your code. It
is whether the app is on Trial.

Getting to **Standard access** is a review. You go to your app in the portal
and ask to upgrade, and Pinterest wants a **privacy policy** you can point at
and a **video recording** of your app taking a real person through the real
sign-in and doing something with the result. A screen recording is fine.
Then somebody reads it and emails you.

**There is no field anywhere in the API that says which tier you are on**, so
socialchimp cannot warn you at the moment it matters and does not pretend to.
Check here: https://developers.pinterest.com/docs/key-concepts/access-tiers/

## Before any of this works

There is no `create_app` here. Somebody has to make the app by hand at
https://developers.pinterest.com/apps, and add your redirect address to it.
Then hand socialchimp the client id and secret as `AppCredentials`.

## Signing someone in

Two steps. `start_login` gives you an address to send the person to, and
`finish_login` swaps the code they come back with for a token.

**There is no PKCE.** Pinterest's v5 API does not support it: send a
`code_challenge` and the sign-in does not become safer, it fails. So
`SendToNetwork.remember` comes back empty and there is nothing for your app
to carry between the two halves. That is a real property of Pinterest, not
something missing here, and it is written down so nobody "fixes" it.

The scopes asked for are `boards:read`, `boards:write`, `pins:read` and
`pins:write`. **Creating a pin needs all four**, not just the pin ones -
asking for a narrower set gets a 403 on the first pin that reads like
something else entirely. They go in the address separated by **commas**,
which is Pinterest's own spelling; spaces are read as part of a scope name.

## Tokens

An access token lasts **30 days**. The refresh token lasts **60 days** and is
**replaced every single time you use it** - so the one you renewed with is
dead the moment the renewal succeeds, and whatever comes back has to be
saved. `TokenManager` takes a lock and saves for you; if you call `refresh`
yourself, that part is yours.

An account nobody has posted from for two months therefore has to connect
again, because its refresh token ran out before anything renewed it. That day
is on `Token.refresh_token_expires_at`, because Pinterest tells us: ask
`connection.token.refresh_token_expires_within(seconds)` on a timer and you
can put a "reconnect Pinterest" prompt in front of somebody in week eight,
rather than finding out when a post fails in week nine.

Apps created before late 2025 were given a different kind of refresh token:
one that lasts a year and cannot be renewed at all. If yours is one of those
and you would rather have the renewable kind, build the platform with
`continuous_refresh=True` and every renewal asks for it.

## Where does the board come from?

Every pin needs a board, so something has to decide which. There were two
honest ways to do it, and this file picks the first:

1. **The post says.** `Post(..., options={"board_id": "..."})`.
2. A board chosen once at sign-in and kept on the connection.

The post wins, because **a board is a property of the pin, not of the
account**. The same person pins recipes to one board and holidays to another;
freezing one board at sign-in makes the wrong choice for most of their pins,
and makes it quietly. Worse, socialchimp would have to *pick* that board -
whichever one the API happened to list first - and somebody's pins would land
somewhere arbitrary and look like they had worked.

So **socialchimp never chooses a board**. `finish_login` does not put one on
the connection.

But an app that has decided on a board should not have to repeat it on every
post, and a `Post` shared across five networks should not need a
Pinterest-only setting on it. So a connection **may** carry
`extra={"board_id": "..."}`, and that is used when a post does not name one.
The difference is that your app put it there on purpose.

A pin with no board from either place is refused before anything is sent,
with a message naming both routes. Never a raw API error.

`boards()` lists what an account has, with ids, so you can build a picker:

    for board in await pinterest.boards(connection):
        print(board.id, board.name, board.privacy)

## What a post can carry

    Post(
        text="What the thing is",             # the pin's description
        media=(Media.from_file("chair.jpg"),),
        options={
            "board_id": "1234567890",         # which board
            "title": "A red chair",
            "link": "https://shop.example/chair",
            "alt_text": "A red velvet armchair",
            "board_section_id": "77",
            "dominant_color": "#6E7874",
        },
    )

`Post.text` is the **description**, and `title` is a separate setting, which
catches people out. The description takes 800 characters and the title 100.

Two to five pictures become one pin people can swipe through. They have to be
all files or all links, because Pinterest takes one kind or the other and not
a mixture.

**Pinterest really will fetch a picture from a web address**, which most
networks will not. So `Media.from_url` costs nothing here and is the cheapest
way to pin something already online. Files you hold are sent as part of the
pin itself.

## Video is three steps and a different server

A video is registered with Pinterest, uploaded **to Amazon** using a form
Pinterest hands you, and then waited for. Only when Pinterest says it has
finished with the video is the pin made.

The upload goes to somebody else's server, so the account's token is
deliberately not sent with it.

Pinterest gives out one form for one upload, so there is no piece-at-a-time
route here the way there is on YouTube or X: the whole video goes in a single
request and really does cost its own size in memory. That is the shape of
Pinterest's API rather than a shortcut taken here.

## What Pinterest cannot do here

- **No scheduling.** `Feature.SCHEDULE` is missing, so a post with
  `publish_at` is refused rather than pinned now.
- **No comments at all.** Not reading them, not writing them - there is no
  comment endpoint anywhere in v5. So `Feature.REPLY` is off and a post with
  `reply_to` is refused by name rather than quietly becoming a new pin.
- **No updates worth having.** There are no webhooks for ordinary pins, and
  nothing in the API reports that something *happened*: no comments, no
  likes, no mentions. What can be read is a list of the pins you made and
  some analytics numbers, and neither of those is an event. Dressing one up
  as an `Update` would mean handing your handlers something that never
  happened, so there is no `fetch_updates` here and `PUSH_UPDATES` is off.
  If Pinterest adds events, this is where they go.
- **No app to create.** See above.
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
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
    NotSupportedError,
    PlatformError,
    RateLimitError,
    SocialChimpError,
    TokenExpiredError,
)
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
    from collections.abc import Mapping

    from socialchimp.http import Retries
    from socialchimp.models import AppCredentials

__all__ = [
    "Board",
    "PinterestPlatform",
    "pinterest_errors",
]

PLATFORM_NAME: Final = "pinterest"

API_URL: Final = "https://api.pinterest.com/v5"
"""Where every request goes, including the ones about tokens."""

SIGN_IN_URL: Final = "https://www.pinterest.com/oauth/"
"""The page a person is sent to so they can approve your app."""

PORTAL_URL: Final = "https://developers.pinterest.com/apps"
"""Where somebody creates the app, by hand."""

ACCESS_URL: Final = "https://developers.pinterest.com/docs/key-concepts/access-tiers/"
"""Where Trial and Standard access are explained, and where you check yours."""

# Paths, all joined onto `API_URL`.
TOKEN_PATH: Final = "/oauth/token"  # noqa: S105 - a public address, not a secret
ACCOUNT_PATH: Final = "/user_account"
BOARDS_PATH: Final = "/boards"
PINS_PATH: Final = "/pins"
MEDIA_PATH: Final = "/media"

DEFAULT_SCOPES: Final = ("boards:read", "boards:write", "pins:read", "pins:write")
"""Enough to list an account's boards and to pin to them.

All four are needed to create a pin. Asking for only the `pins:` pair gets a
403 on the first pin, which reads like a problem with the board rather than a
permission that was never asked for.
"""

MAX_DESCRIPTION_CHARACTERS: Final = 800
MAX_TITLE_CHARACTERS: Final = 100
MAX_ALT_TEXT_CHARACTERS: Final = 500
MAX_LINK_CHARACTERS: Final = 2048

MAX_IMAGES: Final = 5
"""Pictures on one pin. Two or more become one people can swipe through."""

FEWEST_FOR_SEVERAL: Final = 2
"""Below this a pin carries one picture rather than a set of them."""

MAX_VIDEOS_PER_POST: Final = 1
"""One video per pin. Pinterest will not mix video with pictures."""

BOARD_OPTION: Final = "board_id"
"""The setting that says which board a pin goes on, in `Post.options` or in
`Connection.extra`."""

POST_OPTIONS: Final = (
    BOARD_OPTION,
    "board_section_id",
    "title",
    "link",
    "alt_text",
    "dominant_color",
)
"""The settings `Post.options` accepts here. Anything else is refused."""

# How long each half of a token lasts when Pinterest does not say. Thirty
# days and sixty; the second is only ever used in a message.
_DEFAULT_TOKEN_SECONDS: Final = 30 * 24 * 60 * 60
REFRESH_TOKEN_DAYS: Final = 60

# Long enough that nobody can guess one, short enough to sit in a URL.
_STATE_BYTES: Final = 24

# What Pinterest says while it is still working on a video, and what it says
# when it has given up. Anything else means the video is ready.
_STILL_WORKING: Final = ("registered", "processing")
_GAVE_UP: Final = "failed"

# The longest each of these may be, checked before anything is sent.
_LENGTHS: Final = {
    "title": MAX_TITLE_CHARACTERS,
    "alt_text": MAX_ALT_TEXT_CHARACTERS,
    "link": MAX_LINK_CHARACTERS,
}

# What Pinterest allows. Nothing is asked of Pinterest to know any of it.
#
# The two file sizes are deliberately left out. Pinterest publishes no number
# for either, and `None` means "we do not know" - which is true - where a
# guess would refuse pictures it would happily have taken.
_ALLOWED: Final = Limits(
    max_text_length=MAX_DESCRIPTION_CHARACTERS,
    # Pinterest really does mean characters when it says characters.
    text_counted_in=TextCount.CHARACTERS,
    max_images=MAX_IMAGES,
    max_title_length=MAX_TITLE_CHARACTERS,
    max_videos=MAX_VIDEOS_PER_POST,
)


@dataclass(frozen=True, slots=True)
class Board:
    """One of the boards an account has, for building a picker.

    Attributes:
        id: What to put in `Post.options["board_id"]`.
        name: What the person calls it.
        privacy: `"PUBLIC"`, `"PROTECTED"` or `"SECRET"`. A pin on a secret
            board is visible to nobody but its owner, which looks exactly
            like the Trial-access trap and is worth showing in your picker.
    """

    id: str
    name: str
    privacy: str


async def _wait(seconds: float) -> None:
    """Pause while Pinterest finishes working on a video.

    Kept as its own function so tests can watch the pauses instead of
    sitting through them.

    Args:
        seconds: How long to wait.
    """
    await anyio.sleep(seconds)


def _text(reply: RawData, key: str, when: str) -> str:
    """Read a value Pinterest always sends, and complain plainly if it did not.

    Args:
        reply: What Pinterest answered.
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
        f"Pinterest left {key!r} out of its reply when we asked it to "
        f"{when}. That should not happen. The whole reply is on this error."
    )
    raise PlatformError(message, platform=PLATFORM_NAME, raw=reply)


def _said_in(body: RawData) -> str:
    """Pull Pinterest's own explanation out of a refusal.

    Args:
        body: The reply, already read into a dictionary.

    Returns:
        The explanation, or an empty string when there is not one.
    """
    value = body.get("message")
    return f" It said: {value}" if isinstance(value, str) and value else ""


def pinterest_errors(response: httpx.Response) -> SocialChimpError:
    """Turn an unhappy reply from Pinterest into a socialchimp error.

    Two replies are worth naming rather than passing straight through:

    - A **403** is nearly always a scope that was never asked for, and the
      usual cause is asking for the two `pins:` scopes and forgetting that a
      pin also needs the two `boards:` ones. The message says so, because a
      403 on `/v5/pins` otherwise reads like a problem with the board.
    - A **429** may be a daily allowance rather than a request to slow down.
      On Trial access the count is per day, so the usual advice - wait a few
      seconds and try again - is wrong, and the message says that too.

    Everything else is the shared mapping: 401 is an `AuthError`, 404 a
    `NotFoundError`.

    Args:
        response: The reply to turn into an error.

    Returns:
        The error to raise.
    """
    if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
        body = read_body(response)
        message = (
            f"Pinterest is asking us to slow down (429). On Trial access the "
            f"allowance is counted per app per **a day**, so waiting a few "
            f"seconds may not help at all; on Standard it is counted per "
            f"minute and per second. Nothing in the reply says which you are "
            f"on.{_said_in(body)}"
        )
        return RateLimitError(
            message,
            retry_after=retry_after_seconds(response),
            platform=PLATFORM_NAME,
            raw=body,
        )

    if response.status_code == httpx.codes.FORBIDDEN:
        body = read_body(response)
        message = (
            f"Pinterest will not let this account do that (403). It is "
            f"usually a permission that was never asked for: making a pin "
            f"needs {', '.join(DEFAULT_SCOPES)} together, and asking for "
            f"only the pins: pair is the common mistake. The person has to "
            f"connect their account again to grant the rest.{_said_in(body)}"
        )
        return NotAllowedError(message, platform=PLATFORM_NAME, raw=body)

    return error_from_response(response, platform=PLATFORM_NAME)


def _app_or_refuse(app: AppCredentials | None, what: str) -> AppCredentials:
    """Insist on your app's credentials before going any further.

    Args:
        app: The credentials that arrived, which may be none at all.
        what: The thing we were trying to do, for the message.

    Returns:
        The credentials.

    Raises:
        ConfigError: If there are none, saying what somebody has to do by
            hand before Pinterest will talk to them at all.
    """
    if app is None:
        message = (
            f"Pinterest needs your app's client id and secret to {what}, and "
            f"none were given. socialchimp cannot make them for you: "
            f"somebody has to create an app at {PORTAL_URL} and add your "
            f"redirect address to it. Then save what they got with "
            f"Storage.save_app and socialchimp will pass it in for you. A "
            f"new app starts on Trial access, where the pins it makes are "
            f"visible only to the person who made them - see {ACCESS_URL}."
        )
        raise ConfigError(message)
    return app


def _basic_for(app: AppCredentials) -> dict[str, str]:
    """Build the header Pinterest's token endpoint wants.

    Args:
        app: Your app's credentials.

    Returns:
        One header. Pinterest takes the pair only this way - putting the
        secret in the form is refused.
    """
    pair = base64.b64encode(f"{app.client_id}:{app.client_secret}".encode()).decode()
    return {"Authorization": f"Basic {pair}"}


def _check_state(request: LoginRequest, callback: Mapping[str, str]) -> None:
    """Check the value that came back is the one we sent.

    This is what stops somebody handing your app a login they started
    themselves and having it saved against one of your users. It matters a
    little more here than elsewhere, because Pinterest has no PKCE and this
    is the only check of its kind in the flow.

    Args:
        request: The request used to start the login.
        callback: The query values Pinterest sent back.

    Raises:
        AuthError: If both sides have a state and they are different.
    """
    returned = callback.get("state", "")
    if request.state is not None and returned and returned != request.state:
        message = (
            "The state Pinterest sent back did not match the one we sent. "
            "This login did not start here, so nothing has been saved. Start "
            "a new one."
        )
        raise AuthError(message, platform=PLATFORM_NAME)


def _code_from(callback: Mapping[str, str]) -> str:
    """Pull the login code out of what Pinterest sent back.

    Args:
        callback: The query values Pinterest sent back.

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
            f"Pinterest did not sign this person in ({refused}). Usually "
            f"they pressed cancel on the approval page.{detail}"
        )
        raise AuthError(message, platform=PLATFORM_NAME)

    code = callback.get("code")
    if not code:
        message = (
            "Pinterest sent no code back, so there is nothing to swap for a "
            "token. Check you are passing the whole query string from your "
            "redirect address."
        )
        raise AuthError(message, platform=PLATFORM_NAME)
    return code


def _expiry_from(reply: RawData) -> datetime:
    """Work out when an access token stops working.

    Args:
        reply: What Pinterest's token endpoint answered.

    Returns:
        The moment it runs out. Pinterest's access tokens last thirty days,
        and that is what is assumed when it does not say.
    """
    seconds = reply.get("expires_in")
    lasts = seconds if isinstance(seconds, int) else _DEFAULT_TOKEN_SECONDS
    return datetime.now(UTC) + timedelta(seconds=lasts)


def _refresh_expiry_from(
    reply: RawData, already_knew: datetime | None
) -> datetime | None:
    """Work out when the refresh token itself stops working.

    Pinterest is one of the few networks that says. Sixty days is the usual
    answer, and nothing renews it - so an account nobody has posted from
    since the summer needs the person to sign in again, and this is what
    lets an app say so before it happens rather than after.

    Args:
        reply: What Pinterest's token endpoint answered.
        already_knew: The date we were holding before this reply, if any.

    Returns:
        The moment it runs out, `already_knew` on a reply that does not
        mention it, or `None` when nobody has ever said. Nothing is guessed
        here: a guess would have an app telling somebody to reconnect an
        account that was fine.
    """
    seconds = reply.get("refresh_token_expires_in")
    if not isinstance(seconds, int):
        return already_knew
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _no_board() -> InvalidPostError:
    """Build the error for a pin that does not say where it goes.

    Returns:
        The error to raise, naming both places a board can come from and how
        to find out what boards there are. Pinterest's own answer here is a
        404 about a board id of `None`, which helps nobody.
    """
    message = (
        f"Pinterest has no feed to post to - every pin lives on a board, and "
        f"this post does not say which. Either put one on the post:\n\n"
        f'    Post(..., options={{"{BOARD_OPTION}": "1234567890"}})\n\n'
        f"or save one on the connection your app keeps, as "
        f'extra={{"{BOARD_OPTION}": "1234567890"}}, and every pin from that '
        f"account goes there unless a post says otherwise. Call "
        f"boards(connection) to list the boards this account has, with their "
        f"ids. socialchimp will not choose one for you: picking whichever "
        f"board came back first would send somebody's pins somewhere "
        f"arbitrary and look like it had worked."
    )
    return InvalidPostError(message, platform=PLATFORM_NAME)


def _board_for(connection: Connection, options: dict[str, str]) -> str:
    """Work out which board a pin goes on.

    The post wins, because a board is a property of the pin rather than of
    the account. A board on the connection is a fallback your app chose to
    put there - socialchimp never puts one there itself.

    Args:
        connection: The account to pin as.
        options: The post settings, already checked into text.

    Returns:
        The board's id.

    Raises:
        InvalidPostError: If neither says, or if what either says is not an
            id written as text.
    """
    on_the_post = options.get(BOARD_OPTION)
    if on_the_post is not None:
        return on_the_post

    # `Post.options` has already been checked; whatever is on the connection
    # has not, because your app put it there rather than socialchimp.
    saved = connection.extra.get(BOARD_OPTION)
    if saved is None:
        raise _no_board()

    if not isinstance(saved, str) or not saved:
        message = (
            f"The {BOARD_OPTION} saved on connection {connection.id!r} is "
            f"{saved!r}, but it has to be a board's id written as text. "
            f"Pinterest's ids are long numbers and it sends them as text for "
            f"the same reason: they lose their last digits in anything that "
            f"treats them as numbers."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME)
    return saved


def _checked_options(options: RawData) -> dict[str, str]:
    """Check every setting on a post before anything is sent.

    Args:
        options: What was put in `Post.options`.

    Returns:
        The same settings, ready to send.

    Raises:
        InvalidPostError: If a setting is unknown, is not text, or is too
            long. This happens before any request, so a typo costs nothing.
    """
    check_option_names(
        options,
        platform=PLATFORM_NAME,
        allowed=POST_OPTIONS,
        advice="Post.text is the pin's description.",
    )

    checked: dict[str, str] = {}
    for key, value in options.items():
        if not isinstance(value, str) or not value:
            message = f"{key} is {value!r}, but it has to be some text."
            raise InvalidPostError(message, platform=PLATFORM_NAME)

        allowed = _LENGTHS.get(key)
        if allowed is not None and len(value) > allowed:
            message = (
                f"This {key} is {len(value)} characters but Pinterest allows "
                f"at most {allowed}."
            )
            raise InvalidPostError(message, platform=PLATFORM_NAME)

        checked[key] = value
    return checked


def _one_picture(item: Media) -> RawData:
    """Say where one picture is, in whichever way Pinterest wants it.

    Args:
        item: The picture.

    Returns:
        The `media_source` for a pin carrying this one picture.
    """
    if item.url is not None:
        # Pinterest is one of the few networks that really will go and fetch
        # a picture, so downloading it here first would be wasted work.
        return {"source_type": "image_url", "url": item.url}
    return {
        "source_type": "image_base64",
        "content_type": item.content_type,
        "data": base64.b64encode(item.read()).decode(),
    }


def _several_pictures(items: tuple[Media, ...]) -> RawData:
    """Say where a set of pictures is, for a pin people can swipe through.

    Args:
        items: The pictures, in the order they should appear.

    Returns:
        The `media_source` for a pin carrying all of them.

    Raises:
        InvalidPostError: If some are links and some are files. Pinterest
            takes one kind or the other in a set, not a mixture.
    """
    links = [item for item in items if item.url is not None]
    if links and len(links) != len(items):
        message = (
            f"This pin has {len(links)} pictures given as links and "
            f"{len(items) - len(links)} given as files. Pinterest takes a "
            f"set of pictures as all links or all files, not a mixture. "
            f"Download the linked ones with Media.from_bytes, or put the "
            f"others online and use Media.from_url for every one."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME)

    if links:
        return {
            "source_type": "multiple_image_urls",
            "items": [{"url": item.url} for item in items],
        }
    return {
        "source_type": "multiple_image_base64",
        "items": [
            {
                "content_type": item.content_type,
                "data": base64.b64encode(item.read()).decode(),
            }
            for item in items
        ],
    }


# What `check_post` adds to its own "this network has no text-only post"
# message here. Pinterest calls the text a description and has a separate
# title, which is the thing people trip over.
WORDS_ALONE_ADVICE: Final = (
    "Media.from_file('chair.jpg') or Media.from_url(...) will do it, and "
    "Post.text becomes the pin's description."
)


def _the_media(post: Post) -> tuple[tuple[Media, ...], Media | None]:
    """Sort out what a pin is made of.

    Args:
        post: The post about to be sent.

    Returns:
        The pictures, and the video if there is one.

    Raises:
        InvalidPostError: If a pin carries both a video and pictures.
    """
    # `check_post` has already turned away a post with nothing attached, so
    # one of these two is not empty.
    pictures = tuple(item for item in post.media if item.kind is MediaKind.IMAGE)
    videos = tuple(item for item in post.media if item.kind is MediaKind.VIDEO)

    if videos and pictures:
        message = (
            f"This pin has a video and {len(pictures)} pictures on it. A pin "
            f"is a picture or a video, never both - send them as two pins."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME)

    return pictures, videos[0] if videos else None


class PinterestPlatform:
    """Everything socialchimp does with Pinterest.

    Signing people in, keeping their month-long tokens working, listing an
    account's boards, and pinning to them.

        pinterest = PinterestPlatform()

    It holds nothing belonging to one account and nothing belonging to your
    app. Your client id and secret arrive as an argument every time they are
    needed - on the `LoginRequest` for a sign-in, and on `refresh` for a
    renewal - so one of these serves every account and every app.

    Attributes:
        name: `"pinterest"`.
        features: What Pinterest can do. `POST_TEXT` is missing because
            there is no pin without a picture or a video, `REPLY` because
            there are no comments in the API at all, and `PUSH_UPDATES`
            because nothing here reports that anything happened.
    """

    name: str = PLATFORM_NAME

    features: Feature = Feature.POST_IMAGE | Feature.POST_VIDEO | Feature.DELETE_POST

    def __init__(
        self,
        *,
        timeout: float = 300.0,
        retries: Retries | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        continuous_refresh: bool = False,
        media_checks: int = 60,
        media_wait_seconds: float = 2.0,
    ) -> None:
        """Set Pinterest up for one app.

        Args:
            timeout: Seconds to wait for a reply. Five minutes by default,
                because a whole video goes in one request here.
            retries: How many times to try again after a hiccup. Left out,
                the shared default is used.
            transport: Where requests actually go. Leave it out for ordinary
                calls; pass your own to send them somewhere else.
            continuous_refresh: Ask for the renewable kind of refresh token
                on every renewal. Apps created from late 2025 get it anyway.
                Older ones were given a token that lasts a year and cannot
                be renewed at all, and this is how one of those moves over -
                which is a decision about somebody's app, so it is off
                unless you say.
            media_checks: How many times to ask whether a video has finished
                being processed before giving up.
            media_wait_seconds: How long to wait between those checks.
        """
        self._timeout = timeout
        self._retries = retries
        self._transport = transport
        self._continuous_refresh = continuous_refresh
        self._media_checks = media_checks
        self._media_wait_seconds = media_wait_seconds

    def _client(self, token: str | None = None) -> HttpClient:
        """Make a client pointed at Pinterest.

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
            errors=pinterest_errors,
        )

    def api_base(self, connection: Connection) -> str:
        """Return where Pinterest keeps its API.

        Args:
            connection: The account we are about to act as. Pinterest has
                one address for everybody, so this is not used.

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
        """Return what Pinterest allows.

        Nothing is asked, because there is nothing to ask: Pinterest has no
        endpoint that reports what an account may pin. This stays `async`
        because every platform's `limits` is.

        Neither file size is filled in. Pinterest publishes no number for
        either, and `None` says exactly that - a guess here would refuse
        pictures it would happily have taken.

        Args:
            connection: The account to ask about. Not used here.

        Returns:
            What Pinterest allows right now.
        """
        return _ALLOWED

    async def start_login(self, request: LoginRequest) -> SendToNetwork:
        """Build the address to send somebody to so they can approve your app.

        Nothing is sent to Pinterest here, and nothing has to be kept: the
        `remember` on what comes back is empty on purpose.

        **Pinterest's v5 API does not support PKCE.** There is no
        `code_challenge` in this address, and adding one does not make the
        sign-in safer - it makes Pinterest refuse it. The `state` value is
        what ties the two halves together here, so let socialchimp make one
        or pass your own, and check it comes back.

        Args:
            request: Where to send them back to, what to ask for, and your
                app's credentials.

        Returns:
            Always a `SendToNetwork`: Pinterest has an approval page, so
            there is somewhere to send people.

        Raises:
            ConfigError: If the request carries no credentials.
        """
        app = _app_or_refuse(request.app, "sign somebody in")
        state = request.state or secrets.token_urlsafe(_STATE_BYTES)

        query = httpx.QueryParams(
            {
                "response_type": "code",
                "client_id": app.client_id,
                "redirect_uri": request.redirect_uri,
                # Commas, not spaces. Pinterest reads a space as part of the
                # scope name in front of it and grants nothing.
                "scope": ",".join(request.scopes or DEFAULT_SCOPES),
                "state": state,
            }
        )
        return SendToNetwork(url=f"{SIGN_IN_URL}?{query}", state=state)

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> Finished:
        """Swap the code Pinterest sent back for a token, and build a connection.

        Hand this the whole query string Pinterest put on your redirect
        address, as a dictionary.

        No board is chosen here. Which board a pin goes on is a property of
        the pin, and choosing one now would mean picking whichever board came
        back first and sending somebody's pins somewhere arbitrary. If your
        app wants a default, put one on `Connection.extra["board_id"]`
        yourself - `boards()` lists what there is to choose from.

        Args:
            request: The same request used to start the login.
            callback: The query values Pinterest sent back. It must have
                `code`; `state` is checked when it is there.
            remember: Not used. Pinterest has no PKCE, so nothing had to
                survive between the two calls.

        Returns:
            The finished connection. Save it.

        Raises:
            AuthError: If the person said no, if there is no code, or if the
                state that came back is not the one we sent.
            ConfigError: If the request carries no credentials.
            PlatformError: If Pinterest answered without a token.
        """
        app = _app_or_refuse(request.app, "finish signing somebody in")
        _check_state(request, callback)
        code = _code_from(callback)

        form: dict[str, Any] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": request.redirect_uri,
        }

        asked_for = request.scopes or DEFAULT_SCOPES

        async with self._client() as http:
            reply = await http.json(
                "POST", TOKEN_PATH, data=form, headers=_basic_for(app)
            )
            access_token = _text(reply, "access_token", "sign someone in")
            me = await http.json(
                "GET",
                ACCOUNT_PATH,
                headers={"Authorization": f"Bearer {access_token}"},
            )

        granted = reply.get("scope")
        given = granted.split() if isinstance(granted, str) and granted else []
        scopes = tuple(given) if given else asked_for

        username = _text(me, "username", "say who just signed in")
        renewal = reply.get("refresh_token")

        return Finished(
            connection=Connection(
                id=f"{PLATFORM_NAME}:{username}",
                platform=PLATFORM_NAME,
                host=None,
                # Pinterest's own endpoints all work off the token rather
                # than an id, so the username is what identifies an account
                # here and what a person recognises.
                account_id=username,
                account_name=username,
                token=Token(
                    access_token=access_token,
                    refresh_token=renewal if isinstance(renewal, str) else None,
                    expires_at=_expiry_from(reply),
                    refresh_token_expires_at=_refresh_expiry_from(reply, None),
                ),
                scopes=scopes,
                extra={
                    "profile_url": f"https://www.pinterest.com/{username}/",
                    "account_type": me.get("account_type"),
                },
            )
        )

    async def refresh(
        self,
        connection: Connection,
        app: AppCredentials | None = None,
    ) -> Token:
        """Get a fresh pair of tokens for an account.

        Pinterest hands out a **new refresh token every single time**, and
        the one used here stops working the instant this succeeds. Whatever
        comes back has to be saved. `TokenManager` takes a lock and saves for
        you; if you call this yourself, that part is yours.

        Args:
            connection: The account whose token is running out.
            app: Your app's client id and secret. Pinterest signs a renewal
                with them, so this is not optional here - `SocialChimp`
                reads them out of your storage and passes them in.

        Returns:
            The new pair. Save them.

        Raises:
            ConfigError: If no credentials arrived.
            TokenExpiredError: If there is no refresh token, or Pinterest
                will not take the one we have.
            PlatformError: If Pinterest answered without a token.
        """
        signing = _app_or_refuse(app, "renew a token")

        renewal = connection.token.refresh_token
        if renewal is None:
            message = (
                f"The token for {connection.id!r} has run out and there is "
                f"no refresh token to replace it with, so the person has to "
                f"connect their account again."
            )
            raise TokenExpiredError(message, platform=PLATFORM_NAME)

        form: dict[str, Any] = {
            "grant_type": "refresh_token",
            "refresh_token": renewal,
        }
        if self._continuous_refresh:
            form["continuous_refresh"] = "true"

        async with self._client() as http:
            try:
                reply = await http.json(
                    "POST", TOKEN_PATH, data=form, headers=_basic_for(signing)
                )
            except (AuthError, PlatformError) as refused:
                # Pinterest having trouble of its own is not the same as a
                # dead refresh token, and treating it as one would have apps
                # throwing away connections that were fine.
                if _is_pinterests_own_fault(refused):
                    raise
                message = (
                    f"Pinterest will not renew the token for "
                    f"{connection.id!r}. Its refresh token has run out or "
                    f"been used already. They last {REFRESH_TOKEN_DAYS} days "
                    f"and using one replaces it, so an account nobody has "
                    f"posted from in two months reaches this whatever your "
                    f"code does. The person has to connect their account "
                    f"again."
                )
                raise TokenExpiredError(
                    message, platform=PLATFORM_NAME, raw=refused.raw
                ) from refused

        replacement = reply.get("refresh_token")
        return Token(
            access_token=_text(reply, "access_token", "renew a token"),
            # Pinterest does replace it. On a reply that leaves it out,
            # keeping the one we had beats setting it to nothing and locking
            # the person out at the next renewal.
            refresh_token=(
                replacement if isinstance(replacement, str) and replacement else renewal
            ),
            expires_at=_expiry_from(reply),
            # Same reasoning as the refresh token above: a reply that does
            # not mention this leaves what we already knew alone, rather
            # than throwing away the one date that says when this account
            # will need signing in again.
            refresh_token_expires_at=_refresh_expiry_from(
                reply, connection.token.refresh_token_expires_at
            ),
        )

    async def boards(
        self,
        connection: Connection,
        *,
        page_size: int = 25,
    ) -> list[Board]:
        """List the boards an account has, so somebody can pick one.

        Every pin needs a board and socialchimp never chooses one, so this
        is how your app builds the picker. Pinterest pages these; the first
        page is usually all anybody needs, and `page_size` goes up to 250.

        Args:
            connection: The account to ask about.
            page_size: How many to read. Pinterest allows up to 250.

        Returns:
            The boards, in the order Pinterest listed them. A board it
            described in a way we cannot read is left out rather than
            failing the whole call.
        """
        async with self._client(connection.token.access_token) as http:
            reply = await http.json(
                "GET", BOARDS_PATH, params={"page_size": str(page_size)}
            )

        found = reply.get("items")
        if not isinstance(found, list):
            return []

        return [
            Board(
                id=str(item["id"]),
                name=str(item.get("name", "")),
                privacy=str(item.get("privacy", "")),
            )
            for item in found
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        """Make a pin.

        A video is registered, uploaded and waited for first, because a pin
        naming a video Pinterest has not finished with is refused.

        Args:
            connection: The account to pin as.
            post: What to pin.

        Returns:
            What Pinterest said about the new pin.

        Raises:
            InvalidPostError: If the post names no board, if a setting is
                unknown, if it breaks one of Pinterest's limits, or if
                Pinterest gives up on the video.
            NotAllowedError: If the connection is missing a permission a pin
                needs.
            NotSupportedError: If the post asks for something Pinterest
                cannot do, such as being a pin of words alone.
            PlatformError: If a video never finishes being processed.
        """
        if post.reply_to is not None:
            raise NotSupportedError(
                platform=PLATFORM_NAME,
                what="replying to pins",
                suggestion=(
                    "The Pinterest API has no comments in it at all, neither "
                    "reading them nor writing them, so there is nothing to "
                    "reply to."
                ),
            )

        check_post(
            post,
            platform=PLATFORM_NAME,
            features=self.features,
            limits=_ALLOWED,
            words_alone_advice=WORDS_ALONE_ADVICE,
        )

        pictures, video = _the_media(post)
        options = _checked_options(post.options)
        board_id = _board_for(connection, options)
        _check_scopes(connection)

        body: dict[str, Any] = {
            "board_id": board_id,
            # `Post.text` is the description everywhere on Pinterest. The
            # title is separate, which is the thing people trip over.
            "description": post.text,
            **{key: value for key, value in options.items() if key != BOARD_OPTION},
        }

        async with self._client(connection.token.access_token) as http:
            if video is not None:
                body["media_source"] = {
                    "source_type": "video_id",
                    "media_id": await self._upload(http, video),
                }
            elif len(pictures) >= FEWEST_FOR_SEVERAL:
                body["media_source"] = _several_pictures(pictures)
            else:
                body["media_source"] = _one_picture(pictures[0])

            reply = await http.json("POST", PINS_PATH, json=body)

        pin_id = _text(reply, "id", "make a pin")
        return PostResult(
            id=pin_id,
            # Built here rather than read off the reply. Pinterest's `link`
            # is where the pin *points* - somebody's shop - not where the pin
            # is, and handing that back sends people to the wrong place.
            url=f"https://www.pinterest.com/pin/{pin_id}/",
            state=PostState.DONE,
            raw=reply,
        )

    async def _upload(self, http: HttpClient, video: Media) -> str:
        """Get a video onto Pinterest and wait until it can be pinned.

        Three steps: register it with Pinterest, upload it to Amazon with
        the form Pinterest hands back, then ask Pinterest until it says it
        has finished with it.

        Args:
            http: A client already signed as this account.
            video: The video to send.

        Returns:
            Pinterest's id for the video, to name on the pin.

        Raises:
            InvalidPostError: If all we have is a link to it, or if
                Pinterest gives up on it.
            PlatformError: If Pinterest answers without an id, or never
                finishes with the video.
        """
        if video.content is None and video.path is None:
            message = (
                f"Pinterest will not fetch {video.url!r} for you. It fetches "
                f"pictures, but a video has to be uploaded. Download the "
                f"file first, then use Media.from_file or Media.from_bytes."
            )
            raise InvalidPostError(message, platform=PLATFORM_NAME)

        registered = await http.json("POST", MEDIA_PATH, json={"media_type": "video"})
        media_id = _text(registered, "media_id", "register a video")
        where = _text(registered, "upload_url", "register a video")

        given = registered.get("upload_parameters")
        form = (
            {str(key): str(value) for key, value in given.items()}
            if isinstance(given, dict)
            else {}
        )

        # This goes to Amazon rather than to Pinterest, so it gets a client
        # of its own with **no Authorization header on it**. The one signing
        # everything else carries the person's access token, and sending
        # that to somebody else's server hands it to them.
        #
        # Pinterest gives out one form for one upload, so the whole video
        # goes in this single request and really does cost its own size in
        # memory. There is no piece-at-a-time route to use instead.
        async with self._client() as amazon:
            await amazon.post(
                where,
                data=form,
                files={"file": (video.filename or "video", video.read())},
            )

        await self._wait_until_ready(http, media_id)
        return media_id

    async def _wait_until_ready(self, http: HttpClient, media_id: str) -> None:
        """Keep asking about a video until Pinterest has finished with it.

        Args:
            http: A client already signed as this account.
            media_id: The video to ask about.

        Raises:
            InvalidPostError: If Pinterest gives up on it.
            PlatformError: If it is still not ready after all our checks.
        """
        for _ in range(self._media_checks):
            await _wait(self._media_wait_seconds)

            reply = await http.json("GET", f"{MEDIA_PATH}/{media_id}")
            said = str(reply.get("status", ""))

            if said == _GAVE_UP:
                message = (
                    f"Pinterest gave up on this video ({media_id}). It is "
                    f"usually the format or the encoding rather than the "
                    f"size. Nothing was pinned."
                )
                raise InvalidPostError(message, platform=PLATFORM_NAME, raw=reply)

            # Anything that is not "still working" and not "gave up" means
            # it is ready. Treating a word we have never seen as a failure
            # would throw away a video Pinterest was perfectly happy with.
            if said not in _STILL_WORKING:
                return

        message = (
            f"Pinterest is still working on video {media_id} after "
            f"{self._media_checks} checks. Long videos take longer than "
            f"this; raise media_checks or media_wait_seconds and try again. "
            f"The video is not lost - it is uploaded, and pinning it later "
            f"with its media_id will work."
        )
        raise PlatformError(message, platform=PLATFORM_NAME)

    async def delete_post(self, connection: Connection, post_id: str) -> None:
        """Remove a pin.

        Args:
            connection: The account that made it.
            post_id: Pinterest's id for the pin.

        Raises:
            NotFoundError: If there is no such pin on this account.
        """
        async with self._client(connection.token.access_token) as http:
            await http.delete(f"{PINS_PATH}/{post_id}")


def _check_scopes(connection: Connection) -> None:
    """Refuse a pin the connection was never given permission to make.

    Args:
        connection: The account to pin as.

    Raises:
        NotAllowedError: If the connection says what it has and one of the
            four is not in it. Pinterest's own answer is a 403 that reads
            like a problem with the board, so this is worth catching first.
    """
    # An empty `scopes` means we were never told, not that the account has
    # none. Refusing on that would break every connection saved before an
    # app started recording them.
    if not connection.scopes:
        return

    missing = [scope for scope in DEFAULT_SCOPES if scope not in connection.scopes]
    if not missing:
        return

    message = (
        f"This connection is not allowed to make a pin: it is missing "
        f"{', '.join(missing)}. Making a pin needs "
        f"{', '.join(DEFAULT_SCOPES)} together - the board ones as well as "
        f"the pin ones, because a pin has to be put on a board. Ask for all "
        f"four when the person connects their account, and have this person "
        f"connect theirs again."
    )
    raise NotAllowedError(message, platform=PLATFORM_NAME)


def _is_pinterests_own_fault(refused: SocialChimpError) -> bool:
    """Say whether a failed renewal was Pinterest's trouble, not a bad token.

    Args:
        refused: The error a renewal raised.

    Returns:
        True for Pinterest's own failures. Those are worth trying again;
        they do not mean the person has to sign in again, and telling an app
        to throw a connection away over a bad five minutes at Pinterest is
        the sort of quiet damage this library exists to avoid.
    """
    if not isinstance(refused, PlatformError):
        return False
    return (
        refused.status_code is None
        or refused.status_code >= httpx.codes.INTERNAL_SERVER_ERROR
    )
