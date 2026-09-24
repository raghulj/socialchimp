"""Bluesky: the network that proves token renewal works.

Like Mastodon, Bluesky has no developer portal and nobody reviews your app,
so you can connect an account today. Unlike Mastodon, its tokens run out in
minutes and both of them are replaced every time you renew. Any library that
gets renewal slightly wrong will look fine on Mastodon and start logging
people out on Bluesky within the hour.

That is why Bluesky is the second network socialchimp supports.

## Signing someone in

There is no page to send anyone to. People sign in with a **handle** and an
**app password**, so `start_login` does not answer with an address - it
answers with `AskForDetails`, a list of what to ask for. Show a form with
those two boxes and pass the answers straight to `finish_login`:

    step = await bluesky.start_login(LoginRequest(redirect_uri=unused))
    # show step.fields, then:
    done = await bluesky.finish_login(
        request,
        {"handle": "someone.bsky.social", "app_password": "abcd-efgh-ijkl-mnop"},
    )

An app password is **not** the password they log in to Bluesky with. It is a
second password they create at Settings -> App Passwords, one per app, and
they can take it away again without changing anything else or touching their
other apps. Say that next to the box: people are right to be careful, and the
honest answer is the one that gets them to fill the form in.

Bluesky does have OAuth, and its own documentation says it is not the right
choice for a server that posts on people's behalf today. It asks your app to
hold a signing key and to register each sign-in with the server before
sending anybody to it, which is a lot of moving parts for the same result.
When that settles down it belongs here, alongside app passwords rather than
instead of them.

## Tokens run out in minutes

Signing in gives you two tokens. The first is good for a few minutes and is
sent with every request. The second buys a new pair, and **using it replaces
both** - the moment a renewal succeeds, the token you renewed with is dead.

So a renewal that is not saved locks the person out, and two workers renewing
at once means one of them ends up holding a token Bluesky has already thrown
away. `TokenManager` takes a lock and saves for exactly this reason. If you
call `refresh` yourself, save what it returns.

## Every account has a server

Most people are on `bsky.social`, and that is what is used when a connection
does not say otherwise. Anyone can run their own server instead, and their
connection carries its address in `host` - so nothing here assumes Bluesky's
own server, and an account elsewhere works the same way.

## Links are not links until you say so

Bluesky does not look at your words. Type an address into a post and it
arrives as grey text nobody can click, unless the post also carries a note
saying "the bytes from here to here are a link". socialchimp writes those
notes for you, for addresses and for `@handle` mentions.

They are counted in **bytes**, not letters. Every accented letter and every
emoji before a link shifts it along without changing its position on screen,
so a library that counts letters puts the note in the wrong place and the
link quietly stops working. That is the single most common Bluesky bug, and
`facets_for` exists so it is written once.

## Two limits on how long a post can be

Bluesky allows **300 letters and 3,000 bytes**, and a post has to be inside
both. Letters means letters as a person counts them: a family emoji is one
letter, seven characters and 25 bytes. So a post can be well under 300 and
still be too big, and a post of 700 characters can be perfectly fine.

Both are declared on `limits()`, and socialchimp counts both the same way
Bluesky does before anything is sent. `count_graphemes` does the counting if
you want to show somebody how much room they have left.

## What a post can carry

`Post.options` accepts one setting:

    Post(text="Hei", options={"langs": ["nb", "en"]})

Anything else is refused before we send it, with a message listing what is
accepted.

## What Bluesky cannot do

- **No scheduling.** `Feature.SCHEDULE` is missing, so a post with
  `publish_at` is refused rather than published now.
- **No app to register.** There is nothing to create, so there is no
  `create_app`, and `Feature.NEEDS_NO_APP` is on: socialchimp asks storage
  for no credentials before a sign-in, and the platform is handed
  `LoginRequest.app` as `None`.
- **No video here yet.** Bluesky takes video, but through a separate service
  with a token of its own. `Feature.POST_VIDEO` is off until that is written.
"""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import httpx

from socialchimp.errors import (
    AuthError,
    BlockedError,
    ConfigError,
    InvalidPostError,
    MissingPermissionError,
    NotFoundError,
    PlatformError,
    PostGoneError,
    TokenExpiredError,
)
from socialchimp.events import Update, UpdateBatch
from socialchimp.features import (
    Feature,
    Limits,
    TextCount,
    check_option_names,
    check_post,
    count_graphemes,
)
from socialchimp.http import HttpClient, error_from_response, read_body
from socialchimp.models import (
    Attachment,
    Connection,
    Conversation,
    Like,
    LikeResult,
    LinkKind,
    Media,
    Message,
    Page,
    Person,
    Post,
    PostDetails,
    PostResult,
    PostState,
    RawData,
    TextLink,
    Thread,
    Token,
    Unavailable,
)
from socialchimp.platform import AskForDetails, Finished, LoginField, LoginRequest

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from socialchimp.errors import SocialChimpError
    from socialchimp.http import Retries
    from socialchimp.models import AppCredentials

__all__ = ["BlueskyPlatform", "bluesky_errors", "count_graphemes", "facets_for"]

# Counting letters the way a person does is not a Bluesky idea - Bluesky is
# only where most people meet it first - so it lives in `features` with the
# rest of the shared checking. It is handed out from here as well, because
# this is where somebody goes looking for it.

PLATFORM_NAME: Final = "bluesky"

DEFAULT_HOST: Final = "bsky.social"
"""Where an account lives unless its connection says otherwise.

Almost everybody is on Bluesky's own server. Someone running their own has
its address on their connection, and everything here follows that instead.
"""

POST_COLLECTION: Final = "app.bsky.feed.post"
"""What Bluesky calls the pile of records an account's posts live in."""

POST_OPTIONS: Final = ("langs",)
"""The settings `Post.options` accepts here. Anything else is refused."""

MAX_GRAPHEMES: Final = 300
"""Letters allowed in a post, as a person would count them."""

MAX_TEXT_BYTES: Final = 3000
"""Bytes allowed in a post once written out. Emoji use several each."""

MAX_IMAGES: Final = 4
"""Pictures allowed on one post."""

MAX_IMAGE_BYTES: Final = 1_000_000
"""Biggest picture worth sending.

Bluesky is in the middle of raising this to two million, and which one you
get depends on the server the account is on. A picture resized to fit this
number works on either, which is why the smaller one is what we report. A
picture the server will not take comes back as an `InvalidPostError` saying
to shrink it.
"""

MAX_LANGUAGES: Final = 3
"""Language codes allowed on one post."""

HELP_PAGE: Final = "https://bsky.app/settings/app-passwords"
"""Where a person makes the app password this asks for."""

HANDLE_FIELD: Final = LoginField(
    name="handle",
    label="Your Bluesky handle",
    help_text="Such as someone.bsky.social. The @ is optional.",
)
"""The first thing to ask for."""

APP_PASSWORD_FIELD: Final = LoginField(
    name="app_password",
    label="An app password",
    secret=True,
    help_text=(
        "Not your Bluesky password. Make one at Settings, App Passwords - "
        "it looks like abcd-efgh-ijkl-mnop - and you can take it away again "
        "on its own, without changing your password or touching your other "
        "apps."
    ),
)
"""The second thing to ask for. Never write this one to a log."""

# What a note on a post is marking: a web address, or a person.
LINK_FEATURE: Final = "app.bsky.richtext.facet#link"
MENTION_FEATURE: Final = "app.bsky.richtext.facet#mention"

# How pictures are hung off a post.
IMAGES_EMBED: Final = "app.bsky.embed.images"

# Bluesky's word for something, and ours. A word missing from here is passed
# through as it is and lands as `UpdateKind.UNKNOWN` with Bluesky's own word
# kept on the update, so a kind we have never seen still reaches your app.
#
# Reposts and follows used to share `reaction_added` and `unknown` with
# everything else; from 0.8.0 each has a kind of its own, which is a
# documented change in behaviour rather than a bug fix. A quote is someone
# posting about one of the connected account's own posts rather than us, so
# it is folded into `mention` - the same "somebody is talking about you"
# shape mentions already have - rather than inventing a kind of its own for
# one word Bluesky uses and nothing else does yet.
_OUR_WORD_FOR: Final = {
    "like": "reaction_added",
    "repost": "repost_added",
    "reply": "comment_created",
    "mention": "mention",
    "quote": "mention",
    "follow": "followed",
}

# Reasons whose `reasonSubject` names one of the connected account's own
# posts directly - the post that was liked or reposted.
_SUBJECT_REASONS: Final = frozenset({"like", "repost"})

# Reasons that are themselves a post, whose own `record.reply` (when there is
# one) says what it concerns. A plain mention rarely has one; a reply always
# does; a quote never does, because quoting is not replying.
_ABOUT_POST_REASONS: Final = frozenset({"reply", "mention", "quote"})

# Names Bluesky gives a 400 when the trouble is really the sign-in. It
# answers 400 rather than 401 for a token that has run out, which sends
# people hunting in the wrong place, so we name them here.
_SIGN_IN_PROBLEMS: Final = ("ExpiredToken", "InvalidToken", "AuthenticationRequired")

# What Bluesky calls a block, whichever direction it runs.
_BLOCK_PROBLEMS: Final = ("BlockedActor", "BlockedByActor")

# What a direct-message app password that cannot send direct messages says.
# Any other InvalidToken - a malformed or expired token - is a sign-in
# problem instead, handled above; only this exact message, and only on a
# chat.bsky.* call, means the permission itself is missing.
_DM_PERMISSION_MESSAGE: Final = "Bad token method"

# Every chat.bsky.convo.* address lives under this, which is also how we
# tell a chat call apart from an ordinary one when reading an error - see
# `_is_chat_call`.
_CHAT_PATH_MARKER: Final = "/chat.bsky."

# Every call under chat.bsky.convo needs this so the person's own server
# knows to hand it on to Bluesky's chat service rather than answer it
# itself - the chat service is a separate thing from the PDS, reached
# through it.
_CHAT_PROXY_HEADER: Final = "atproto-proxy"
_CHAT_PROXY_TARGET: Final = "did:web:api.bsky.chat#bsky_chat"

# Every address we send to. Bluesky puts them all under /xrpc and names them
# after the definition each one follows.
_CREATE_SESSION: Final = "/com.atproto.server.createSession"
_REFRESH_SESSION: Final = "/com.atproto.server.refreshSession"
_CREATE_RECORD: Final = "/com.atproto.repo.createRecord"
_DELETE_RECORD: Final = "/com.atproto.repo.deleteRecord"
_UPLOAD_BLOB: Final = "/com.atproto.repo.uploadBlob"
_GET_POSTS: Final = "/app.bsky.feed.getPosts"
_GET_POST_THREAD: Final = "/app.bsky.feed.getPostThread"
_GET_LIKES: Final = "/app.bsky.feed.getLikes"
_RESOLVE_HANDLE: Final = "/com.atproto.identity.resolveHandle"
_LIST_NOTIFICATIONS: Final = "/app.bsky.notification.listNotifications"
_UPDATE_SEEN: Final = "/app.bsky.notification.updateSeen"
_LIST_CONVOS: Final = "/chat.bsky.convo.listConvos"
_GET_MESSAGES: Final = "/chat.bsky.convo.getMessages"
_SEND_MESSAGE: Final = "/chat.bsky.convo.sendMessage"
_UPDATE_READ: Final = "/chat.bsky.convo.updateRead"
_GET_CONVO_FOR_MEMBERS: Final = "/chat.bsky.convo.getConvoForMembers"

LIKE_COLLECTION: Final = "app.bsky.feed.like"
"""What Bluesky calls the pile of records an account's likes live in."""

_DEFAULT_THREAD_DEPTH: Final = 6
"""How many reply levels `getPostThread` fetches when nobody asks for a
particular depth - Bluesky's own default."""

_MAX_THREAD_DEPTH: Final = 1000
"""The most reply levels Bluesky will ever fetch in one call, however deep
somebody asks for."""

_MAX_LIKES_PAGE: Final = 100
"""The most likes `getLikes` will hand back in one page."""

_MAX_MARKER_PAGES: Final = 5
"""How many pages `fetch_updates_after` will read looking for a marker
before giving up and saying there is more waiting.

Bluesky's notifications only page backwards, so catching up after a long
gap between checks means reading page after page until the last marker
turns up. Reading without end would turn one slow check into an unbounded
one, so a page is asked for, then another, up to this many - and if the
marker still has not turned up, `UpdateBatch.more` comes back `True` so the
caller reads the rest with another call straight away, rather than us
holding one request open for however long that takes.
"""

# The kinds of node `getPostThread` can put where a reply belongs, besides
# an ordinary post that hydrated fine.
_NOT_FOUND_POST: Final = "app.bsky.feed.defs#notFoundPost"
_BLOCKED_POST: Final = "app.bsky.feed.defs#blockedPost"

# What a note inside a post's own text is marking, read back rather than
# written - see `LINK_FEATURE` and `MENTION_FEATURE` above for the write
# side of the same three.
_TAG_FEATURE: Final = "app.bsky.richtext.facet#tag"

# The kinds of embed a post's view side can carry, read back into
# `Attachment`s.
_IMAGES_VIEW: Final = "app.bsky.embed.images#view"
_VIDEO_VIEW: Final = "app.bsky.embed.video#view"
_EXTERNAL_VIEW: Final = "app.bsky.embed.external#view"
_RECORD_WITH_MEDIA_VIEW: Final = "app.bsky.embed.recordWithMedia#view"

# What kind of message view a chat message arrived as - a real one, or a
# placeholder for one that was deleted since.
_DELETED_MESSAGE_VIEW: Final = "chat.bsky.convo.defs#deletedMessageView"

# Joins the two halves of a marker together. `at://` uris and ISO-8601
# timestamps never contain this, so splitting on it is unambiguous.
_MARKER_SEPARATOR: Final = "::"

# A token is three pieces joined by dots.
_JWT_PIECES: Final = 3

# How long to trust a token whose expiry we could not read. Short on purpose:
# renewing a token that had hours left costs one request, while trusting one
# that had seconds left costs a failed post.
_ASSUMED_LIFETIME_SECONDS: Final = 60.0

# Both patterns are run against the **bytes** of the text, not its letters,
# so the offsets they report are already the byte offsets Bluesky wants and
# nothing has to be converted afterwards. That is the whole trick.
#
# The look-behind checks the byte before without eating it. Matching that
# byte instead would swallow half of any accented letter sitting in front of
# a link, and every offset after it would be one out.
_LINK: Final = re.compile(
    rb"(?<![\w@/.-])(https?://(?:[\w-]+\.)+[a-z]{2,}(?:/[^\s]*)?)",
    re.IGNORECASE,
)
_MENTION: Final = re.compile(
    rb"(?<![\w@/.-])@((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,})",
    re.IGNORECASE,
)

# Punctuation that ends a sentence rather than an address.
_TRAILING: Final = b".,;:!?'\")]"


def _now() -> datetime:
    """Return the current moment.

    Kept as its own function so tests can say exactly what a post's
    timestamp should be.

    Returns:
        Now, with a timezone.
    """
    return datetime.now(UTC)


def _clean_host(host: str | None) -> str:
    """Turn whatever is on a connection into a bare server name.

    Args:
        host: The server, however it was written, or nothing at all.

    Returns:
        Just the name, such as `"bsky.social"`. Unlike Mastodon, a missing
        server is not a problem here: nearly everybody is on Bluesky's own,
        so that is what we use.
    """
    cleaned = (
        (host or "").strip().removeprefix("https://").removeprefix("http://").strip("/")
    )
    return cleaned or DEFAULT_HOST


def _address_of(host: str) -> str:
    """Return where one server's API lives.

    Args:
        host: The server name.

    Returns:
        The address, with no trailing slash.
    """
    return f"https://{host}/xrpc"


def _text(reply: RawData, key: str, when: str) -> str:
    """Read a value Bluesky always sends, and complain plainly if it did not.

    Args:
        reply: What Bluesky answered.
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
        f"Bluesky left {key!r} out of its reply when we asked it to {when}. "
        f"That should not happen. The whole reply is on this error."
    )
    raise PlatformError(message, platform=PLATFORM_NAME, raw=reply)


def _moment(text: str) -> datetime | None:
    """Read a time Bluesky wrote, such as `"2026-08-31T10:00:00.000Z"`.

    Args:
        text: The time as it arrived.

    Returns:
        The moment, always with a timezone, or `None` if it cannot be read.
    """
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    # Bluesky always writes a timezone, but a server run by somebody else
    # might not, and a time with no timezone compares wrongly against every
    # other time we hold.
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _seconds_in(access_jwt: str) -> float | None:
    """Read the expiry out of an access token.

    We read the middle of the token and **do not check its signature**. That
    looks alarming written down, so: the signature is there for Bluesky to
    check, not us. We are not who it was issued to and we hold no key that
    could check it. Nothing here decides whether to trust anybody - we are
    only reading when to ask for a new one.

    Args:
        access_jwt: The token as Bluesky sent it.

    Returns:
        When it runs out, in seconds since 1970, or `None` if the token is
        not written the way we expect.
    """
    pieces = access_jwt.split(".")
    if len(pieces) != _JWT_PIECES:
        return None

    middle = pieces[1]
    try:
        # The padding is left off in a token, so it has to be put back.
        payload = json.loads(
            base64.urlsafe_b64decode(middle + "=" * (-len(middle) % 4))
        )
    except ValueError:
        return None

    if not isinstance(payload, dict):
        return None
    runs_out = payload.get("exp")
    return float(runs_out) if isinstance(runs_out, int | float) else None


def _expires_at(access_jwt: str) -> datetime:
    """Work out when a token stops working.

    Args:
        access_jwt: The token as Bluesky sent it.

    Returns:
        The moment it runs out. A token we cannot read is treated as nearly
        used up, so it is renewed before the next post rather than after a
        failed one.
    """
    seconds = _seconds_in(access_jwt)
    if seconds is None:
        return _now() + timedelta(seconds=_ASSUMED_LIFETIME_SECONDS)
    return datetime.fromtimestamp(seconds, UTC)


def _facet(start: int, end: int, feature: RawData) -> RawData:
    """Build one note saying what a stretch of bytes is.

    Args:
        start: Where it begins, counted in bytes.
        end: Where it ends, counted in bytes.
        feature: What that stretch is - a link or a mention.

    Returns:
        The note, ready to hang off a post.
    """
    return {"index": {"byteStart": start, "byteEnd": end}, "features": [feature]}


def _where(facet: RawData) -> int:
    """Return where a note starts, for putting a list of them in order.

    Args:
        facet: The note to look at.

    Returns:
        Its first byte.
    """
    start: int = facet["index"]["byteStart"]
    return start


def facets_for(text: str) -> list[RawData]:
    """Mark up the web addresses in some text so Bluesky makes them clickable.

    Bluesky never looks at your words, so an address in a post is grey text
    until something says otherwise. This says otherwise.

    The offsets are into the text's bytes rather than its letters, which is
    what Bluesky asks for and what almost everybody gets wrong. Both are the
    same until the post contains an accent or an emoji, and then they are
    not, and the link silently stops being a link.

    A full stop that ends the sentence is left out of the address.

    Args:
        text: The words about to be posted.

    Returns:
        One note per address, in the order they appear.
    """
    written = text.encode()
    found: list[RawData] = []

    for match in _LINK.finditer(written):
        address = match.group(1).rstrip(_TRAILING)
        start = match.start(1)
        found.append(
            _facet(
                start,
                start + len(address),
                {"$type": LINK_FEATURE, "uri": address.decode()},
            )
        )
    return found


def _handles_in(text: str) -> list[tuple[int, int, str]]:
    """Find the `@somebody` mentions in some text.

    Args:
        text: The words about to be posted.

    Returns:
        Where each mention starts and ends in bytes, and the handle itself
        without its `@`.
    """
    written = text.encode()
    return [
        (match.start(), match.end(), match.group(1).decode())
        for match in _MENTION.finditer(written)
    ]


def _is_chat_call(response: httpx.Response) -> bool:
    """Say whether a reply came from one of the `chat.bsky.convo.*` calls.

    Every one of those needs an app password made with direct-message
    access, and Bluesky's answer when that permission is missing (400
    `InvalidToken`, "Bad token method") uses the same name as a plain
    malformed token. The only way to tell them apart is which address the
    request went to, so `bluesky_errors` asks this before deciding.

    Args:
        response: The reply to look at.

    Returns:
        True if the request this answers was a chat call.
    """
    return _CHAT_PATH_MARKER in response.request.url.path


def bluesky_errors(response: httpx.Response) -> SocialChimpError:
    """Turn an unhappy reply from Bluesky into a socialchimp error.

    Bluesky puts a short name in every refusal, and only its 400s need us to
    read it. A few of those names are worth naming here:

    - A token that has run out comes back as **400 ExpiredToken**, not 401.
      Anyone who maps by status alone reads that as a bad post and never
      renews, so it becomes an `AuthError` here.
    - `InvalidRequest` is what a post that breaks a rule looks like, so it
      becomes an `InvalidPostError` with whatever Bluesky said kept on it.
    - `NotFound` is what `getPostThread` says when the post asked for is
      gone, so it becomes a `PostGoneError`.
    - `BlockedActor` and `BlockedByActor` mean a block runs between the two
      accounts, in either direction, so both become a `BlockedError`.
    - `InvalidToken` saying "Bad token method" on a `chat.bsky.convo.*` call
      means this app password was made without direct-message access, so it
      becomes a `MissingPermissionError` naming the fix - a new app password
      cannot have the box ticked after the fact.

    Everything else is the shared mapping: 401 is an `AuthError`, 403 a
    `NotAllowedError`, 404 a `NotFoundError`, 429 a `RateLimitError`.

    Args:
        response: The reply to turn into an error.

    Returns:
        The error to raise.
    """
    if response.status_code != httpx.codes.BAD_REQUEST:
        return error_from_response(response, platform=PLATFORM_NAME)

    body = read_body(response)
    named = body.get("error")
    said = body.get("message")
    detail = f" It said: {said}" if isinstance(said, str) and said else ""

    is_dm_permission_problem = (
        named == "InvalidToken"
        and said == _DM_PERMISSION_MESSAGE
        and _is_chat_call(response)
    )
    if is_dm_permission_problem:
        return MissingPermissionError(
            needs="direct messages",
            suggestion=(
                'Make a new app password with "Allow access to your direct '
                'messages" ticked, then reconnect the account - the box '
                "cannot be ticked on an app password that already exists."
            ),
            platform=PLATFORM_NAME,
            raw=body,
        )

    if named in _SIGN_IN_PROBLEMS:
        message = (
            f"Bluesky would not accept our sign-in ({named}), which it "
            f"reports as a 400 rather than a 401. The token has run out or "
            f"been taken away; renewing it, or asking the person to connect "
            f"their account again, is what fixes it.{detail}"
        )
        return AuthError(message, platform=PLATFORM_NAME, raw=body)

    if named == "NotFound":
        message = f"Bluesky has no such post any more (400 {named}).{detail}"
        return PostGoneError(message, platform=PLATFORM_NAME, raw=body)

    if named in _BLOCK_PROBLEMS:
        message = (
            f"Bluesky refused this because of a block between the two "
            f"accounts (400 {named}).{detail}"
        )
        return BlockedError(message, platform=PLATFORM_NAME, raw=body)

    if named == "BlobTooLarge":
        message = (
            f"Bluesky will not take a picture this big. Send a smaller one - "
            f"resizing to about a megabyte is usually enough. The limit is "
            f"moving from one megabyte to two, so it depends on the server "
            f"this account is on.{detail}"
        )
        return InvalidPostError(message, platform=PLATFORM_NAME, raw=body)

    if named == "InvalidRequest":
        message = (
            f"Bluesky would not accept this post (400 {named}). Something in "
            f"it breaks one of Bluesky's rules.{detail}"
        )
        return InvalidPostError(message, platform=PLATFORM_NAME, raw=body)

    return error_from_response(response, platform=PLATFORM_NAME)


def _checked_langs(options: RawData) -> list[str]:
    """Check the settings on a post and read the languages out of them.

    Args:
        options: What was put in `Post.options`.

    Returns:
        The language codes, which may be none at all.

    Raises:
        InvalidPostError: If a setting is unknown or its value is wrong. This
            happens before any request, so a typo costs nothing.
    """
    check_option_names(options, platform=PLATFORM_NAME, allowed=POST_OPTIONS)

    given = options.get("langs")
    if given is None:
        return []

    codes = [given] if isinstance(given, str) else given
    if (
        not isinstance(codes, list | tuple)
        or not codes
        or len(codes) > MAX_LANGUAGES
        or not all(isinstance(code, str) and code for code in codes)
    ):
        message = (
            f"langs is {given!r}, but it has to be one language code such as "
            f'"en", or up to {MAX_LANGUAGES} of them in a list.'
        )
        raise InvalidPostError(message)
    return [str(code) for code in codes]


def _rkey_of(post_id: str) -> str:
    """Pull the short id out of a post's address.

    A post is `at://did:plc:.../app.bsky.feed.post/3kaposted`, and everything
    that changes a post wants only the last part of that.

    Args:
        post_id: The whole address, or the short id on its own.

    Returns:
        The short id.
    """
    return post_id.rsplit("/", 1)[-1]


def _is_a_post_reference(value: object) -> bool:
    """Say whether this is a complete reference to a post.

    Args:
        value: What Bluesky gave us.

    Returns:
        True if it names both a post and the version of it we mean.
    """
    return (
        isinstance(value, dict)
        and isinstance(value.get("uri"), str)
        and isinstance(value.get("cid"), str)
    )


async def _did_for(http: HttpClient, handle: str) -> str | None:
    """Look up who a handle belongs to.

    Args:
        http: A client already pointed at the right server.
        handle: The handle, without its `@`.

    Returns:
        Bluesky's permanent identifier for that person, or `None` if there is
        nobody by that name. A handle that has been given up is not a reason
        to refuse somebody's whole post, so it is left as plain words.
    """
    try:
        reply = await http.json("GET", _RESOLVE_HANDLE, params={"handle": handle})
    except (InvalidPostError, NotFoundError):
        # Bluesky answers a name it cannot find with a 400, which our mapping
        # reads as a problem with the post. Here it is not one.
        return None

    did = reply.get("did")
    return did if isinstance(did, str) and did else None


async def _notes_on(http: HttpClient, text: str) -> list[RawData]:
    """Mark up the links and the mentions in a post.

    Args:
        http: A client already pointed at the right server.
        text: The words about to be posted.

    Returns:
        The notes, in the order they appear in the text.
    """
    notes = facets_for(text)

    for start, end, handle in _handles_in(text):
        did = await _did_for(http, handle)
        if did is not None:
            notes.append(_facet(start, end, {"$type": MENTION_FEATURE, "did": did}))

    notes.sort(key=_where)
    return notes


async def _reply_reference(http: HttpClient, parent_id: str) -> RawData:
    """Work out what a reply has to point at.

    Bluesky hangs a whole conversation off its first post, so every reply
    names two things: the post being answered, and the post that started the
    conversation. Given only the first, we have to look it up to find the
    second - and when the post being answered is itself a reply, the one that
    started the conversation is its root, not the post being answered.

    Args:
        http: A client already pointed at the right server.
        parent_id: The address of the post being replied to.

    Returns:
        The pair of references to put on the new post.

    Raises:
        InvalidPostError: If there is no such post to reply to.
    """
    reply = await http.json("GET", _GET_POSTS, params={"uris": parent_id})
    posts = reply.get("posts")
    found = posts[0] if isinstance(posts, list) and posts else None

    if not isinstance(found, dict):
        message = (
            f"Bluesky has no post at {parent_id!r}, so there is nothing to "
            f"reply to. It may have been deleted, or the id may be from "
            f"another network."
        )
        raise InvalidPostError(message, platform=PLATFORM_NAME, raw=reply)

    parent = {
        "uri": _text(found, "uri", "find the post being replied to"),
        "cid": _text(found, "cid", "find the post being replied to"),
    }

    record = found.get("record")
    answered = record.get("reply") if isinstance(record, dict) else None
    root = answered.get("root") if isinstance(answered, dict) else None
    return {"root": root if _is_a_post_reference(root) else parent, "parent": parent}


async def _upload(http: HttpClient, item: Media) -> RawData:
    """Send one picture to a server and get back what to call it.

    Args:
        http: A client already pointed at the right server.
        item: The picture to send.

    Returns:
        Bluesky's receipt for the file, to name in the post.

    Raises:
        InvalidPostError: If all we have is a link to the file.
        PlatformError: If the server took the file without saying so.
    """
    if item.content is None and item.path is None:
        message = (
            f"Bluesky will not fetch {item.url!r} for you - it only takes "
            f"files sent to it. Download the file first, then use "
            f"Media.from_bytes or Media.from_file."
        )
        raise InvalidPostError(message)

    reply = await http.json(
        "POST",
        _UPLOAD_BLOB,
        content=item.read(),
        headers={"Content-Type": item.content_type},
    )

    receipt = reply.get("blob")
    if not isinstance(receipt, dict):
        message = (
            "Bluesky answered our picture upload without a blob in it, so "
            "there is nothing to put on the post. The whole reply is on this "
            "error."
        )
        raise PlatformError(message, platform=PLATFORM_NAME, raw=reply)
    return receipt


# ---------------------------------------------------------------------------
# Reading a post back - shared by read_post and read_thread.
# ---------------------------------------------------------------------------


def _text_or_none(value: object) -> str | None:
    """Read a string Bluesky may have left out or sent empty.

    Args:
        value: Whatever was under the key.

    Returns:
        The string, or `None` if it was missing, empty, or not a string.
    """
    return value if isinstance(value, str) and value else None


def _int_or_none(value: object) -> int | None:
    """Read a count Bluesky may have left out.

    Args:
        value: Whatever sat under a count field.

    Returns:
        The count, or `None` if it was missing - never `0` in its place. A
        network that says nothing about a count is not the same as one that
        counted zero.
    """
    return value if isinstance(value, int) else None


def _person_from(raw: RawData) -> Person:
    """Build a `Person` out of an actor Bluesky sent us.

    Only ever called where the caller has already checked `did` is there -
    every post's author, every like's actor, every notification's author and
    every chat member carries one. A `blockedPost`'s author, which carries
    only `did` and a block flag, never reaches here: it becomes a
    placeholder with `author=None` instead.

    Args:
        raw: The actor.

    Returns:
        The person, with anything Bluesky left out as `None`.
    """
    handle = _text_or_none(raw.get("handle"))
    return Person(
        id=str(raw.get("did", "")),
        handle=handle,
        display_name=_text_or_none(raw.get("displayName")),
        avatar_url=_text_or_none(raw.get("avatar")),
        url=f"https://bsky.app/profile/{handle}" if handle else None,
        raw=raw,
    )


def _char_offset(text_bytes: bytes, byte_offset: int) -> int:
    """Turn a facet's byte offset into a Python string index.

    Bluesky counts a facet's position in bytes; `PostDetails.text` is a
    plain Python string, indexed in characters the way every other Python
    string is. The two agree until the text holds an accent or an emoji,
    and quietly disagree for everything after it.

    Args:
        text_bytes: The post's text, encoded once by the caller rather than
            re-encoded for every facet.
        byte_offset: Where the facet starts or ends, in bytes.

    Returns:
        The same position, as a character index.
    """
    return len(text_bytes[:byte_offset].decode())


def _link_kind_and_target(feature: RawData) -> tuple[LinkKind, str] | None:
    """Read what one facet feature marks, and what it points at.

    Args:
        feature: One entry from a facet's `features` list.

    Returns:
        The kind of link and its target, or `None` for a feature this is
        not one of the three socialchimp models, or one missing the field
        it needs.
    """
    kind_name = feature.get("$type")
    if kind_name == MENTION_FEATURE:
        return (
            (LinkKind.MENTION, did)
            if (did := _text_or_none(feature.get("did")))
            else None
        )
    if kind_name == LINK_FEATURE:
        return (
            (LinkKind.LINK, uri) if (uri := _text_or_none(feature.get("uri"))) else None
        )
    if kind_name == _TAG_FEATURE:
        return (
            (LinkKind.TAG, tag) if (tag := _text_or_none(feature.get("tag"))) else None
        )
    return None


def _links_from(text: str, facets: object) -> tuple[TextLink, ...]:
    """Turn a record's facets into `TextLink`s, in character offsets.

    Args:
        text: The post's own text, so byte offsets can be turned into
            character ones.
        facets: Whatever `record.facets` held - a list when the post has
            any, anything else when it has none.

    Returns:
        One `TextLink` per feature socialchimp models, in the order the
        facets arrived in.
    """
    if not isinstance(facets, list):
        return ()

    written = text.encode()
    found: list[TextLink] = []
    for facet in facets:
        if not isinstance(facet, dict):
            continue
        index = facet.get("index")
        features = facet.get("features")
        if not isinstance(index, dict) or not isinstance(features, list):
            continue
        byte_start = index.get("byteStart")
        byte_end = index.get("byteEnd")
        if not isinstance(byte_start, int) or not isinstance(byte_end, int):
            continue

        for feature in features:
            if not isinstance(feature, dict):
                continue
            matched = _link_kind_and_target(feature)
            if matched is None:
                continue
            kind, target = matched
            found.append(
                TextLink(
                    start=_char_offset(written, byte_start),
                    end=_char_offset(written, byte_end),
                    kind=kind,
                    target=target,
                    url=target if kind is LinkKind.LINK else None,
                )
            )
    return tuple(found)


def _aspect_ratio(embed_item: RawData) -> tuple[int | None, int | None]:
    """Read a picture or video's width and height, when Bluesky sent one.

    Args:
        embed_item: One image, or a video embed, from the view side.

    Returns:
        Width and height, or `(None, None)` when there is no aspect ratio.
    """
    ratio = embed_item.get("aspectRatio")
    if not isinstance(ratio, dict):
        return None, None
    width = ratio.get("width")
    height = ratio.get("height")
    return _int_or_none(width), _int_or_none(height)


def _image_attachments(embed: RawData) -> tuple[Attachment, ...]:
    """Turn `app.bsky.embed.images#view` into `Attachment`s.

    Args:
        embed: The images embed, from the view side.

    Returns:
        One attachment per picture.
    """
    images = embed.get("images")
    if not isinstance(images, list):
        return ()

    found: list[Attachment] = []
    for image in images:
        if not isinstance(image, dict):
            continue
        width, height = _aspect_ratio(image)
        found.append(
            Attachment(
                kind="image",
                url=_text_or_none(image.get("fullsize")),
                preview_url=_text_or_none(image.get("thumb")),
                alt_text=_text_or_none(image.get("alt")),
                width=width,
                height=height,
                raw=image,
            )
        )
    return tuple(found)


def _video_attachments(embed: RawData) -> tuple[Attachment, ...]:
    """Turn `app.bsky.embed.video#view` into an `Attachment`.

    Args:
        embed: The video embed, from the view side.

    Returns:
        One attachment, holding the playable video.
    """
    width, height = _aspect_ratio(embed)
    return (
        Attachment(
            kind="video",
            url=_text_or_none(embed.get("playlist")),
            preview_url=_text_or_none(embed.get("thumbnail")),
            alt_text=_text_or_none(embed.get("alt")),
            width=width,
            height=height,
            raw=embed,
        ),
    )


def _external_attachments(embed: RawData) -> tuple[Attachment, ...]:
    """Turn `app.bsky.embed.external#view` into an `Attachment`.

    Bluesky calls this a link card: a web address with a title, a
    description and sometimes a picture. socialchimp files it as a
    `"link"` attachment rather than an `"image"`, since the address is the
    point of it and there is no `alt_text` field of its own to read.

    Args:
        embed: The external embed, from the view side.

    Returns:
        One attachment pointing at the linked address, or none at all if
        Bluesky left out the `external` object itself.
    """
    external = embed.get("external")
    if not isinstance(external, dict):
        return ()
    return (
        Attachment(
            kind="link",
            url=_text_or_none(external.get("uri")),
            preview_url=_text_or_none(external.get("thumb")),
            alt_text=None,
            width=None,
            height=None,
            raw=embed,
        ),
    )


def _attachments_from(embed: object) -> tuple[Attachment, ...]:
    """Turn a post's view-side embed into `Attachment`s.

    A quoted post (`app.bsky.embed.record#view`) carries no file of its
    own, so it is left out - there is nothing here for `Attachment` to
    describe, and quoting shows up on `Update` instead when it is someone
    else's quote of this account's own post.

    Args:
        embed: Whatever `post.embed` held.

    Returns:
        The attachments found, empty for a post with none socialchimp
        models.
    """
    if not isinstance(embed, dict):
        return ()

    kind = embed.get("$type")
    if kind == _IMAGES_VIEW:
        return _image_attachments(embed)
    if kind == _VIDEO_VIEW:
        return _video_attachments(embed)
    if kind == _EXTERNAL_VIEW:
        return _external_attachments(embed)
    if kind == _RECORD_WITH_MEDIA_VIEW:
        return _attachments_from(embed.get("media"))
    return ()


def _strong_ref_uri(value: object) -> str | None:
    """Read the address out of a `com.atproto.repo.strongRef`.

    Args:
        value: Whatever sat under `reply.parent` or `reply.root`.

    Returns:
        The address, or `None` if this was not a proper reference.
    """
    if not isinstance(value, dict):
        return None
    return _text_or_none(value.get("uri"))


def _post_reply_refs(record: object) -> tuple[str | None, str | None]:
    """Read what a post's own `record.reply` says it answers.

    Shared by every post view and every reply/mention/quote notification,
    since both carry the same `record` shape.

    Args:
        record: Whatever `record` held.

    Returns:
        The parent's address and the thread root's address. Either or both
        are `None` when this is not a reply, or does not say.
    """
    if not isinstance(record, dict):
        return None, None
    reply = record.get("reply")
    if not isinstance(reply, dict):
        return None, None
    return _strong_ref_uri(reply.get("parent")), _strong_ref_uri(reply.get("root"))


def _viewer_like(view: RawData) -> tuple[bool | None, str | None]:
    """Read whether the connected account has already liked this post.

    Args:
        view: A post, from the view side.

    Returns:
        Whether it is liked, and the like's own address when it is. Both
        `None` when Bluesky sent no viewer state at all - which means we do
        not know, not that the answer is no.
    """
    viewer = view.get("viewer")
    if not isinstance(viewer, dict):
        return None, None
    return "like" in viewer, _text_or_none(viewer.get("like"))


def _post_details_from(view: RawData, *, connection: Connection) -> PostDetails:
    """Build a `PostDetails` out of one post the view side hydrated.

    Used for `read_post`, for the anchor and every real reply `read_thread`
    finds, and by `reply` once it has published, to fill everything
    `publish` alone cannot.

    Args:
        view: The post, exactly as `getPosts` or `getPostThread` sent it -
            a `postView`.
        connection: The account reading it, so `is_mine` can be worked out.

    Returns:
        The post, in full.
    """
    uri = _text_or_none(view.get("uri")) or ""

    author_raw = view.get("author")
    author = (
        _person_from(author_raw)
        if isinstance(author_raw, dict) and isinstance(author_raw.get("did"), str)
        else None
    )

    record = view.get("record")
    text = ""
    links: tuple[TextLink, ...] = ()
    created_at: datetime | None = None
    if isinstance(record, dict):
        text = _text_or_none(record.get("text")) or ""
        links = _links_from(text, record.get("facets"))
        created_raw = record.get("createdAt")
        if isinstance(created_raw, str):
            created_at = _moment(created_raw)

    parent_id, root_id = _post_reply_refs(record)
    if root_id is None:
        root_id = uri

    liked_by_me, my_like_id = _viewer_like(view)

    handle = author.handle if author is not None else None
    url = f"https://bsky.app/profile/{handle}/post/{_rkey_of(uri)}" if handle else None

    return PostDetails(
        id=uri,
        cid=_text_or_none(view.get("cid")),
        url=url,
        author=author,
        text=text,
        html=None,
        links=links,
        attachments=_attachments_from(view.get("embed")),
        created_at=created_at,
        visibility=None,
        parent_id=parent_id,
        root_id=root_id,
        reply_count=_int_or_none(view.get("replyCount")),
        like_count=_int_or_none(view.get("likeCount")),
        repost_count=_int_or_none(view.get("repostCount")),
        quote_count=_int_or_none(view.get("quoteCount")),
        liked_by_me=liked_by_me,
        my_like_id=my_like_id,
        is_mine=author is not None and author.id == connection.account_id,
        unavailable=None,
        raw=view,
    )


def _placeholder_post(
    node: RawData, *, parent_id: str, root_id: str, unavailable: Unavailable
) -> PostDetails:
    """Stand in for a reply `getPostThread` could not hydrate.

    Args:
        node: The `notFoundPost` or `blockedPost` node.
        parent_id: The post it sits directly under, from where it was found
            in the tree - there is no `record` here to read this from.
        root_id: The top of the thread it sits in.
        unavailable: Why it could not be read.

    Returns:
        A `PostDetails` with everything socialchimp was not sent left
        empty, and `unavailable` set so the app can say why.
    """
    return PostDetails(
        id=_text_or_none(node.get("uri")) or "",
        cid=None,
        url=None,
        author=None,
        text="",
        html=None,
        links=(),
        attachments=(),
        created_at=None,
        visibility=None,
        parent_id=parent_id,
        root_id=root_id,
        reply_count=None,
        like_count=None,
        repost_count=None,
        quote_count=None,
        liked_by_me=None,
        my_like_id=None,
        is_mine=False,
        unavailable=unavailable,
        raw=node,
    )


_UNTIMED: Final = datetime.max.replace(tzinfo=UTC)
"""Sorts after everything else.

A placeholder has no time of its own, and this is safer than guessing one:
it can only ever push a reply we know nothing about to the end of the flat
list, never in front of one we do know the time of.
"""


def _reply_moment(view: RawData) -> datetime | None:
    """Read when a reply was made, for putting a thread's replies in order.

    Args:
        view: The reply, from the view side.

    Returns:
        Its own `createdAt`, falling back to when the network indexed it,
        or `None` if neither can be read.
    """
    record = view.get("record")
    if isinstance(record, dict):
        created_raw = record.get("createdAt")
        if isinstance(created_raw, str):
            when = _moment(created_raw)
            if when is not None:
                return when
    indexed_raw = view.get("indexedAt")
    return _moment(indexed_raw) if isinstance(indexed_raw, str) else None


def _walk_replies(
    node: RawData, *, parent_id: str, root_id: str, connection: Connection
) -> tuple[list[tuple[datetime, PostDetails]], bool]:
    """Collect one reply and everything nested under it.

    Args:
        node: A `threadViewPost`, `notFoundPost` or `blockedPost`, from a
            thread's `replies`.
        parent_id: The post this one sits directly under.
        root_id: The top of the whole thread, handed down for placeholders
            that carry no `record` of their own to read it from.
        connection: The account reading the thread.

    Returns:
        Every reply found at or below this node, each paired with a sort
        key, and whether a depth cut was hit somewhere in this branch - a
        node with no `replies` of its own but a `replyCount` above zero,
        meaning Bluesky stopped nesting before it ran out of real replies.
    """
    node_type = node.get("$type")
    if node_type == _NOT_FOUND_POST:
        placeholder = _placeholder_post(
            node, parent_id=parent_id, root_id=root_id, unavailable=Unavailable.DELETED
        )
        return [(_UNTIMED, placeholder)], False
    if node_type == _BLOCKED_POST:
        placeholder = _placeholder_post(
            node, parent_id=parent_id, root_id=root_id, unavailable=Unavailable.BLOCKED
        )
        return [(_UNTIMED, placeholder)], False

    post_view = node.get("post")
    if not isinstance(post_view, dict):
        return [], False

    detail = _post_details_from(post_view, connection=connection)
    when = _reply_moment(post_view)
    collected: list[tuple[datetime, PostDetails]] = [(when or _UNTIMED, detail)]

    hit_cut = False
    replies = node.get("replies")
    if isinstance(replies, list):
        for child in replies:
            if isinstance(child, dict):
                child_collected, child_cut = _walk_replies(
                    child, parent_id=detail.id, root_id=root_id, connection=connection
                )
                collected.extend(child_collected)
                hit_cut = hit_cut or child_cut
    else:
        reply_count = post_view.get("replyCount")
        if isinstance(reply_count, int) and reply_count > 0:
            hit_cut = True

    return collected, hit_cut


# ---------------------------------------------------------------------------
# Updates - shared by fetch_updates and fetch_updates_after.
# ---------------------------------------------------------------------------


def _update_from(raw: RawData, *, connection: Connection) -> Update | None:
    """Turn one notification into an `Update`, socialchimp's own shape.

    Shared by `fetch_updates` and `fetch_updates_after`, so a like, a
    repost, a reply, a mention, a quote and a follow are read the same way
    however they were asked for.

    Args:
        raw: One notification, exactly as `listNotifications` sent it.
        connection: The account this notification concerns.

    Returns:
        The update, or `None` if `indexedAt` cannot be read - there is no
        sensible time to give it, and dropping one unreadable notification
        beats failing the whole page.
    """
    when = _moment(str(raw.get("indexedAt", "")))
    if when is None:
        return None

    reason = str(raw.get("reason", ""))
    uri = _text_or_none(raw.get("uri")) or ""

    author_raw = raw.get("author")
    actor = (
        _person_from(author_raw)
        if isinstance(author_raw, dict) and isinstance(author_raw.get("did"), str)
        else None
    )

    post_id: str | None = None
    about_post_id: str | None = None
    thread_root_id: str | None = None

    if reason in _SUBJECT_REASONS:
        about_post_id = _text_or_none(raw.get("reasonSubject"))
    elif reason in _ABOUT_POST_REASONS:
        post_id = uri or None
        about_post_id, thread_root_id = _post_reply_refs(raw.get("record"))

    return Update.from_network(
        update_id=uri,
        kind_name=_OUR_WORD_FOR.get(reason, reason),
        platform=PLATFORM_NAME,
        connection_id=connection.id,
        created_at=when,
        raw=raw,
        actor=actor,
        post_id=post_id,
        about_post_id=about_post_id,
        thread_root_id=thread_root_id,
    )


def _marker_for(raw: RawData) -> str | None:
    """Build a marker out of one notification.

    Args:
        raw: The notification to remember as the newest one seen.

    Returns:
        `indexedAt` and `uri` joined together, or `None` if either is
        missing - a marker built from half a notification would resume from
        somewhere that never happened.
    """
    when = _text_or_none(raw.get("indexedAt"))
    uri = _text_or_none(raw.get("uri"))
    return f"{when}{_MARKER_SEPARATOR}{uri}" if when and uri else None


def _parse_marker(marker: str) -> tuple[str, str] | None:
    """Split a marker back into the moment and the address it names.

    Args:
        marker: A marker this platform built earlier.

    Returns:
        The moment and the address, or `None` if this is not a marker this
        platform recognises.
    """
    when, separator, uri = marker.partition(_MARKER_SEPARATOR)
    if not separator or not when or not uri:
        return None
    return when, uri


# What we say when a marker was not built by this platform. Treating it the
# same as `None` would silently restart from the latest page and drop
# whatever came after it, so this refuses instead - loudly, on the first
# call that sees it, rather than quietly on every call after.
_BAD_MARKER_MESSAGE: Final = (
    "This marker was not made by Bluesky's platform, so there is nothing "
    "safe to resume from. Pass None to start afresh."
)


def _own_marker(marker: str) -> tuple[str, str]:
    """Parse a marker, insisting it is one this platform built.

    Args:
        marker: The marker to check.

    Returns:
        The moment and the address it names.

    Raises:
        ConfigError: If this is not a marker this platform recognises.
    """
    parsed = _parse_marker(marker)
    if parsed is None:
        raise ConfigError(_BAD_MARKER_MESSAGE)
    return parsed


# ---------------------------------------------------------------------------
# Direct messages.
# ---------------------------------------------------------------------------


def _profile_lookup(
    did: str, profiles: Sequence[RawData], *, connection: Connection
) -> Person:
    """Find who a chat message's sender was.

    `getMessages` names a sender by `did` alone and sends the rest of what
    it knows about the people in the conversation separately, under
    `relatedProfiles`; `listConvos` and `getConvoForMembers` put the same
    kind of information on each conversation's own `members` instead.
    Either one is passed in here as `profiles`.

    Args:
        did: Whoever sent the message.
        profiles: Profiles Bluesky sent alongside the message, in whichever
            of the two shapes above.
        connection: The account reading the conversation, so a message it
            sent itself can still be named even when it is missing from
            `profiles`.

    Returns:
        The sender, with whatever socialchimp could find out about them -
        just their id, when nothing else is known.
    """
    for profile in profiles:
        if profile.get("did") == did:
            return _person_from(profile)

    if did == connection.account_id:
        handle = _text_or_none(connection.extra.get("handle"))
        return Person(
            id=did,
            handle=handle,
            display_name=None,
            avatar_url=None,
            url=f"https://bsky.app/profile/{handle}" if handle else None,
            raw={},
        )

    return Person(
        id=did, handle=None, display_name=None, avatar_url=None, url=None, raw={}
    )


def _message_moment(raw: RawData, when: str) -> datetime:
    """Read a message's `sentAt`, which Bluesky always sends.

    Args:
        raw: The message, from either side of a chat call.
        when: What we had asked it to do, for the message if it is missing.

    Returns:
        The moment it was sent.

    Raises:
        PlatformError: If `sentAt` is missing or cannot be read.
    """
    sent = raw.get("sentAt")
    parsed = _moment(sent) if isinstance(sent, str) else None
    if parsed is None:
        message = (
            f"Bluesky sent a message with no readable sentAt when we asked "
            f"it to {when}. The whole reply is on this error."
        )
        raise PlatformError(message, platform=PLATFORM_NAME, raw=raw)
    return parsed


def _message_from(
    raw: RawData,
    *,
    conversation_id: str,
    profiles: Sequence[RawData],
    connection: Connection,
) -> Message:
    """Build a `Message` out of one chat message view.

    Args:
        raw: A `messageView` or `deletedMessageView`.
        conversation_id: Which conversation this belongs to - a chat
            message does not carry this itself.
        profiles: Everything Bluesky told us about the people in this
            conversation, for naming the sender.
        connection: The account reading the conversation.

    Returns:
        The message.
    """
    deleted = raw.get("$type") == _DELETED_MESSAGE_VIEW
    sender_raw = raw.get("sender")
    sender_did = (
        _text_or_none(sender_raw.get("did")) if isinstance(sender_raw, dict) else None
    )
    sender = _profile_lookup(sender_did or "", profiles, connection=connection)

    return Message(
        id=_text_or_none(raw.get("id")) or "",
        conversation_id=conversation_id,
        sender=sender,
        text="" if deleted else (_text_or_none(raw.get("text")) or ""),
        sent_at=_message_moment(raw, "read a message"),
        is_mine=sender_did == connection.account_id,
        deleted=deleted,
        attachments=(),
        raw=raw,
    )


def _conversation_from(raw: RawData, *, connection: Connection) -> Conversation:
    """Build a `Conversation` out of one convo Bluesky sent us.

    Args:
        raw: A `convoView`.
        connection: The account reading its conversations.

    Returns:
        The conversation. `updated_at` comes from its `lastMessage`, when
        there is one - Bluesky's alternative, decoding a time out of the
        convo's own `rev`, is not implemented: the exact algorithm is not
        given anywhere the approved contract points at, and no conversation
        Bluesky sends back is ever without a `lastMessage` in practice, so
        there is nothing here to verify it against.
    """
    members_raw = raw.get("members")
    members = (
        [member for member in members_raw if isinstance(member, dict)]
        if isinstance(members_raw, list)
        else []
    )
    people = tuple(
        _person_from(member)
        for member in members
        if member.get("did") != connection.account_id
    )

    conversation_id = _text_or_none(raw.get("id")) or ""

    last_raw = raw.get("lastMessage")
    last_message = (
        _message_from(
            last_raw,
            conversation_id=conversation_id,
            profiles=members,
            connection=connection,
        )
        if isinstance(last_raw, dict)
        else None
    )

    return Conversation(
        id=conversation_id,
        people=people,
        last_message=last_message,
        unread_count=_int_or_none(raw.get("unreadCount")),
        updated_at=last_message.sent_at if last_message is not None else None,
        can_reply_until=None,
        full_history=True,
        raw=raw,
    )


class BlueskyPlatform:
    """Everything socialchimp does with Bluesky.

    Signing people in with an app password, keeping their short-lived tokens
    working, publishing, and reading what has happened since.

        bluesky = BlueskyPlatform()
        step = await bluesky.start_login(LoginRequest(redirect_uri=unused))

    It holds nothing between calls. Everything about a person arrives on the
    `Connection`, so one of these can be shared by your whole process, and
    two of them behave the same as one.

    Attributes:
        name: `"bluesky"`.
        features: What Bluesky can do here. There is no app to register
            and no way to ask for a post later, so `NEEDS_NO_APP` is on and
            `CREATE_APP` and `SCHEDULE` are missing - `start_login` works
            with nothing saved. Video is missing too - Bluesky takes it,
            through a separate service we have not written yet.
            `SUBSCRIBE_UPDATES` is missing on purpose: Bluesky has no
            webhooks at all, and that flag is reserved for the release that
            adds push delivery elsewhere.
    """

    name: str = PLATFORM_NAME

    features: Feature = (
        Feature.NEEDS_NO_APP
        | Feature.POST_TEXT
        | Feature.POST_IMAGE
        | Feature.REPLY
        | Feature.DELETE_POST
        | Feature.READ_POSTS
        | Feature.READ_POST
        | Feature.READ_THREAD
        | Feature.REPLY_TO_COMMENTS
        | Feature.LIKE
        | Feature.READ_LIKES
        | Feature.READ_UPDATES_AFTER
        | Feature.MESSAGES
        | Feature.START_CONVERSATIONS
    )

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        retries: Retries | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        updates_per_check: int = 40,
    ) -> None:
        """Set Bluesky up for one app.

        Args:
            timeout: Seconds to wait for a server to answer.
            retries: How many times to try again after a hiccup. Left out,
                the shared default is used.
            transport: Where requests actually go. Leave it out for ordinary
                calls; pass your own to send them somewhere else.
            updates_per_check: How many notifications to read at a time.
                Bluesky allows up to 100.
        """
        self._timeout = timeout
        self._retries = retries
        self._transport = transport
        self._updates_per_check = updates_per_check

    def _client(self, host: str, token: str | None = None) -> HttpClient:
        """Make a client pointed at one server.

        Args:
            host: The server to talk to.
            token: The token to sign requests with. Usually the access
                token - `refresh` is the one exception.

        Returns:
            A client. Use it in an `async with` block so it closes itself.
        """
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        return HttpClient(
            _address_of(host),
            platform=PLATFORM_NAME,
            headers=headers,
            timeout=self._timeout,
            transport=self._transport,
            retries=self._retries,
            errors=bluesky_errors,
        )

    def _chat_client(self, host: str, token: str) -> HttpClient:
        """Make a client for the `chat.bsky.convo.*` calls.

        The same as `_client`, plus the one header every direct-message
        call needs so the person's own server hands it on to Bluesky's chat
        service instead of trying to answer it itself.

        Args:
            host: The server to talk to.
            token: The access token to sign requests with.

        Returns:
            A client. Use it in an `async with` block so it closes itself.
        """
        return HttpClient(
            _address_of(host),
            platform=PLATFORM_NAME,
            headers={
                "Authorization": f"Bearer {token}",
                _CHAT_PROXY_HEADER: _CHAT_PROXY_TARGET,
            },
            timeout=self._timeout,
            transport=self._transport,
            retries=self._retries,
            errors=bluesky_errors,
        )

    def api_base(self, connection: Connection) -> str:
        """Return where this account's server keeps its API.

        Args:
            connection: The account we are about to act as.

        Returns:
            The address, with no trailing slash. Bluesky puts everything
            under `/xrpc`, so this is where your own calls go too.
        """
        return _address_of(_clean_host(connection.host))

    def auth_headers(self, connection: Connection) -> Mapping[str, str]:
        """Return the headers that prove we may act as this account.

        Args:
            connection: The account we are acting as.

        Returns:
            One `Authorization` header carrying the access token. By the time
            this runs the token has already been renewed if it needed it.
        """
        return {"Authorization": f"Bearer {connection.token.access_token}"}

    async def limits(self, connection: Connection) -> Limits:
        """Return what Bluesky allows.

        Nothing is asked, because there is nothing to ask. Unlike Mastodon,
        where whoever runs a server sets its post length, these numbers are
        part of what a Bluesky post *is* and are the same everywhere. This
        stays `async` because every platform's `limits` is.

        Both text limits are real and a post has to be inside both: 300
        letters as a person would count them, and 3,000 bytes once written
        out. `check_post` counts both, the same way Bluesky will.

        Args:
            connection: The account to ask about. Not used here.

        Returns:
            What Bluesky allows right now.
        """
        return Limits(
            max_text_length=MAX_GRAPHEMES,
            max_text_bytes=MAX_TEXT_BYTES,
            text_counted_in=TextCount.GRAPHEMES,
            max_images=MAX_IMAGES,
            max_image_bytes=MAX_IMAGE_BYTES,
        )

    async def start_login(self, request: LoginRequest) -> AskForDetails:
        """Say what to ask a person for.

        There is nowhere to send anybody. Bluesky sign-in is a handle and an
        app password, so this answers with the two boxes to put on a form.
        Nothing is sent to Bluesky here.

        Show the fields in the order given, link `help_url` beside them, and
        pass what the person types to `finish_login` as the `callback`.

        Args:
            request: Not used. Bluesky needs nothing to get started - no
                credentials, no address to come back to.

        Returns:
            The two things to ask for, and where a person makes the second.
        """
        return AskForDetails(
            fields=(HANDLE_FIELD, APP_PASSWORD_FIELD),
            help_url=HELP_PAGE,
        )

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> Finished:
        """Swap a handle and an app password for a pair of tokens.

        Args:
            request: The same request used to start the login. Its `host` is
                used for somebody on their own server; left out, Bluesky's
                own server is used.
            callback: What the person typed, under the names `start_login`
                asked for: `handle` and `app_password`.
            remember: Not used. Nothing has to survive between the two calls
                here, because nobody was sent anywhere.

        Returns:
            The finished connection. Save it.

        Raises:
            AuthError: If either answer is missing, or Bluesky refuses them.
            PlatformError: If Bluesky answered without a token.
        """
        server = _clean_host(request.host)
        # Bluesky calls this an identifier rather than a handle because it
        # also takes an account's email address or its permanent id.
        identifier = _answer_to(callback, HANDLE_FIELD).removeprefix("@").lower()
        password = _answer_to(callback, APP_PASSWORD_FIELD)

        async with self._client(server) as http:
            reply = await http.json(
                "POST",
                _CREATE_SESSION,
                json={"identifier": identifier, "password": password},
            )

        did = _text(reply, "did", "sign someone in")
        handle = _text(reply, "handle", "sign someone in")

        return Finished(
            connection=Connection(
                # Built from the permanent id rather than the handle, because
                # people rename themselves and this has to keep matching.
                id=f"{PLATFORM_NAME}:{did}",
                platform=PLATFORM_NAME,
                host=server,
                account_id=did,
                account_name=f"@{handle}",
                token=_token_from(reply, "sign someone in"),
                # An app password is all or nothing - there is nothing
                # narrower to ask for, so there is nothing to record here.
                scopes=(),
                extra={
                    "handle": handle,
                    "profile_url": f"https://bsky.app/profile/{handle}",
                },
            )
        )

    async def refresh(
        self,
        connection: Connection,
        app: AppCredentials | None = None,
    ) -> Token:
        """Get a fresh pair of tokens.

        Two things about this call catch people out, and both are on purpose
        rather than mistakes to tidy up:

        1. It is signed with the **refresh** token, not the access token that
           signs everything else. Sending the usual one gets a refusal that
           reads like the person has been signed out.
        2. Both tokens come back new. The refresh token used here stops
           working the instant this succeeds, so whatever comes back has to
           be saved. `TokenManager` takes a lock and saves for you; if you
           call this yourself, that part is yours.

        Args:
            connection: The account whose token is running out.
            app: Your app's credentials. Taken and ignored: Bluesky is
                signed in to with an app password rather than a registered
                app, so there is nothing here for a client id and secret to
                say.

        Returns:
            The new pair. Save them.

        Raises:
            TokenExpiredError: If there is no refresh token, or Bluesky will
                not take the one we have. The person has to sign in again.
            PlatformError: If Bluesky answered without a token.
        """
        renewal = connection.token.refresh_token
        if renewal is None:
            message = (
                f"The token for {connection.id!r} has run out and there is "
                f"no refresh token to replace it with. Bluesky tokens last "
                f"minutes, so this connection cannot be used again - the "
                f"person needs to connect their account again."
            )
            raise TokenExpiredError(message, platform=PLATFORM_NAME)

        async with self._client(_clean_host(connection.host), renewal) as http:
            try:
                reply = await http.json("POST", _REFRESH_SESSION)
            except AuthError as refused:
                message = (
                    f"Bluesky will not renew the token for {connection.id!r}. "
                    f"Its refresh token has run out, been used already, or "
                    f"the app password behind it was taken away. The person "
                    f"has to connect their account again."
                )
                raise TokenExpiredError(
                    message, platform=PLATFORM_NAME, raw=refused.raw
                ) from refused

        return _token_from(reply, "renew a token")

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        """Publish a post.

        Links and mentions are marked up first, pictures are uploaded next,
        and the post itself goes last, so nothing half-finished is left on
        the account if a picture is refused.

        Args:
            connection: The account to publish as.
            post: What to publish.

        Returns:
            What Bluesky said about the new post.

        Raises:
            InvalidPostError: If a setting is unknown, if the post is too
                long, if it has more than four pictures, or if the post it
                replies to is gone.
            NotSupportedError: If the post asks for something Bluesky cannot
                do here, such as being published later or carrying video.
            PlatformError: If Bluesky answered in a way we cannot use.
        """
        # Settings are checked before anything is sent, so a typo costs no
        # request and no part of the account's allowance.
        langs = _checked_langs(post.options)

        allowed = await self.limits(connection)
        check_post(
            post,
            platform=PLATFORM_NAME,
            features=self.features,
            limits=allowed,
        )

        record: dict[str, Any] = {
            "$type": POST_COLLECTION,
            "text": post.text,
            "createdAt": _now().isoformat(),
        }

        async with self._client(
            _clean_host(connection.host), connection.token.access_token
        ) as http:
            notes = await _notes_on(http, post.text)
            if notes:
                record["facets"] = notes

            if post.reply_to is not None:
                record["reply"] = await _reply_reference(http, post.reply_to)

            if post.media:
                # Video would go here, as its own kind of embed. It needs a
                # second token from a service of its own, so it is a job in
                # itself rather than another branch.
                record["embed"] = {
                    "$type": IMAGES_EMBED,
                    "images": [
                        {
                            # Bluesky asks for this even when it is empty. It
                            # is worth filling in: a picture nobody describes
                            # is a picture some people cannot see.
                            "alt": item.alt_text or "",
                            "image": await _upload(http, item),
                        }
                        for item in post.media
                    ],
                }

            if langs:
                record["langs"] = langs

            reply = await http.json(
                "POST",
                _CREATE_RECORD,
                json={
                    "repo": connection.account_id,
                    "collection": POST_COLLECTION,
                    "record": record,
                },
            )

        uri = _text(reply, "uri", "publish a post")
        return PostResult(
            id=uri,
            # Built from the account's permanent id rather than its handle,
            # so the link still works after somebody renames themselves.
            url=(
                f"https://bsky.app/profile/{connection.account_id}/post/{_rkey_of(uri)}"
            ),
            state=PostState.DONE,
            cid=_text_or_none(reply.get("cid")),
            raw=reply,
        )

    async def delete_post(self, connection: Connection, post_id: str) -> None:
        """Remove a post.

        Args:
            connection: The account that published it.
            post_id: The post's address, as `publish` handed it back. The
                short id on its own works too.

        Raises:
            NotFoundError: If there is no such post on this account.
        """
        async with self._client(
            _clean_host(connection.host), connection.token.access_token
        ) as http:
            await http.json(
                "POST",
                _DELETE_RECORD,
                json={
                    "repo": connection.account_id,
                    "collection": POST_COLLECTION,
                    "rkey": _rkey_of(post_id),
                },
            )

    # Bluesky can also hold a socket open and send every change on the whole
    # network as it happens. That would go alongside this method, with
    # something to pick out the one account we care about. Checking on a
    # timer comes first because it needs nothing kept running, survives a
    # restart with no lost updates, and does not mean reading everybody
    # else's posts to find one person's.
    async def fetch_updates(
        self,
        connection: Connection,
        since: datetime | None,
    ) -> Sequence[Update]:
        """Return what has happened on this account since a moment in time.

        Bluesky pages its notifications from newest to oldest, so we read a
        recent page and drop anything older than the marker. Check often
        enough that a page covers the gap - the default of 40 is plenty for
        most accounts.

        A follow, a quote and the rest have no name of ours, so they arrive
        as `UpdateKind.UNKNOWN` with Bluesky's own word kept on `kind_name`.

        Args:
            connection: The account to ask about.
            since: Only return things newer than this. `None` on the first
                call.

        Returns:
            The updates, oldest first.
        """
        async with self._client(
            _clean_host(connection.host), connection.token.access_token
        ) as http:
            reply = await http.json(
                "GET",
                _LIST_NOTIFICATIONS,
                params={"limit": self._updates_per_check},
            )

        found = reply.get("notifications")
        items = (
            [raw for raw in found if isinstance(raw, dict)]
            if isinstance(found, list)
            else []
        )

        updates: list[Update] = []
        for raw in items:
            update = _update_from(raw, connection=connection)
            if update is None or (since is not None and update.created_at <= since):
                continue
            updates.append(update)

        # Bluesky hands back the newest first; socialchimp wants the oldest.
        updates.reverse()
        return updates

    async def read_post(self, connection: Connection, post_id: str) -> PostDetails:
        """Read one post back in full.

        One request: `app.bsky.feed.getPosts`.

        Args:
            connection: The account to read it as.
            post_id: The post's address, or its short id on its own.

        Returns:
            The post, in full.

        Raises:
            PostGoneError: If there is no such post any more.
        """
        async with self._client(
            _clean_host(connection.host), connection.token.access_token
        ) as http:
            reply = await http.json("GET", _GET_POSTS, params={"uris": post_id})

        posts = reply.get("posts")
        found = posts[0] if isinstance(posts, list) and posts else None
        if not isinstance(found, dict):
            message = (
                f"Bluesky has no post at {post_id!r} any more. It may have "
                f"been deleted, or the id may be from another network."
            )
            raise PostGoneError(message, platform=PLATFORM_NAME, raw=reply)

        return _post_details_from(found, connection=connection)

    async def read_thread(
        self,
        connection: Connection,
        post_id: str,
        *,
        depth: int | None = None,
        limit: int | None = None,
    ) -> Thread:
        """Read a post together with the replies underneath it.

        One request: `app.bsky.feed.getPostThread`. Replies come back flat
        and oldest first, by their own `createdAt` - not in the order
        Bluesky nested them, which is not guaranteed to be chronological.

        A reply Bluesky could not hydrate becomes a placeholder with
        `unavailable` set: `#notFoundPost` as `Unavailable.DELETED`,
        `#blockedPost` as `Unavailable.BLOCKED`. If the post asked for
        itself is gone, `getPostThread` answers with a 400 `NotFound`
        rather than a placeholder, which `bluesky_errors` turns into a
        `PostGoneError` before this method sees a reply at all.

        Args:
            connection: The account to read it as.
            post_id: The post's address, or its short id on its own.
            depth: How many reply levels to fetch. `None` uses Bluesky's own
                default of six; passing more than 1,000 is capped at 1,000,
                which is as many as Bluesky will ever fetch in one call.
            limit: A cap on how many replies come back, applied here rather
                than by Bluesky, which has no such setting of its own.

        Returns:
            The post and its replies. `complete` is `False` if `limit` cut
            the replies short, or if Bluesky stopped nesting before a
            branch ran out of real replies - `depth` was not enough to
            reach the end of it.

        Raises:
            PostGoneError: If the post asked for is gone.
        """
        depth_param = min(
            depth if depth is not None else _DEFAULT_THREAD_DEPTH, _MAX_THREAD_DEPTH
        )

        async with self._client(
            _clean_host(connection.host), connection.token.access_token
        ) as http:
            reply = await http.json(
                "GET",
                _GET_POST_THREAD,
                params={"uri": post_id, "depth": depth_param, "parentHeight": 0},
            )

        thread = reply.get("thread")
        if not isinstance(thread, dict):
            message = (
                "Bluesky answered getPostThread without a thread in it. The "
                "whole reply is on this error."
            )
            raise PlatformError(message, platform=PLATFORM_NAME, raw=reply)

        anchor_view = thread.get("post")
        if not isinstance(anchor_view, dict):
            message = (
                "Bluesky answered getPostThread without the post itself in "
                "it. The whole reply is on this error."
            )
            raise PlatformError(message, platform=PLATFORM_NAME, raw=reply)

        anchor = _post_details_from(anchor_view, connection=connection)

        collected: list[tuple[datetime, PostDetails]] = []
        hit_cut = False
        replies_raw = thread.get("replies")
        if isinstance(replies_raw, list):
            for child in replies_raw:
                if isinstance(child, dict):
                    child_collected, child_cut = _walk_replies(
                        child,
                        parent_id=anchor.id,
                        root_id=anchor.root_id or anchor.id,
                        connection=connection,
                    )
                    collected.extend(child_collected)
                    hit_cut = hit_cut or child_cut
        else:
            anchor_reply_count = anchor_view.get("replyCount")
            if isinstance(anchor_reply_count, int) and anchor_reply_count > 0:
                hit_cut = True

        collected.sort(key=lambda item: item[0])
        replies = [detail for _, detail in collected]

        complete = not hit_cut
        if limit is not None and len(replies) > limit:
            replies = replies[:limit]
            complete = False

        return Thread(post=anchor, replies=tuple(replies), complete=complete, raw=reply)

    async def reply(
        self,
        connection: Connection,
        post_id: str,
        text: str,
        *,
        media: tuple[Media, ...] = (),
        options: RawData | None = None,
    ) -> PostResult:
        """Reply to a post or comment, at any depth.

        The same as `publish(Post(reply_to=post_id, ...))` - `reply_to`
        already builds the root and parent references a reply needs. This
        is the recommended way to reply; `publish` keeps working for
        anyone already using it.

        Args:
            connection: The account to reply as.
            post_id: The post or comment being replied to, at any depth.
            text: The reply's words.
            media: Pictures to attach to the reply. Bluesky has no video
                here yet - see `Feature.POST_VIDEO`.
            options: The same settings `publish` takes, such as `langs`.

        Returns:
            What Bluesky said about the new reply, including its `cid`.

        Raises:
            InvalidPostError: If the post being replied to is gone, or the
                reply itself breaks one of Bluesky's rules.
        """
        return await self.publish(
            connection,
            Post(
                text=text,
                media=media,
                reply_to=post_id,
                options=dict(options) if options is not None else {},
            ),
        )

    async def like(self, connection: Connection, post_id: str) -> LikeResult:
        """Like a post or a comment.

        `createRecord` is not deduplicated by Bluesky the way Mastodon's
        favourite is, so liking something already liked would otherwise
        make a second, pointless like record. This reads `viewer.like`
        first and hands back the existing like instead.

        Two requests the first time a post is liked: `getPosts`, then
        `createRecord`. One request when it is already liked: `getPosts`
        alone.

        Args:
            connection: The account doing the liking.
            post_id: The post or comment to like.

        Returns:
            What Bluesky said about the like.

        Raises:
            PostGoneError: If there is no such post any more.
        """
        async with self._client(
            _clean_host(connection.host), connection.token.access_token
        ) as http:
            lookup = await http.json("GET", _GET_POSTS, params={"uris": post_id})
            posts = lookup.get("posts")
            found = posts[0] if isinstance(posts, list) and posts else None
            if not isinstance(found, dict):
                message = (
                    f"Bluesky has no post at {post_id!r} any more, so there "
                    f"is nothing to like."
                )
                raise PostGoneError(message, platform=PLATFORM_NAME, raw=lookup)

            already_liked, existing_like = _viewer_like(found)
            if already_liked and existing_like:
                return LikeResult(post_id=post_id, like_id=existing_like, raw=found)

            subject = {
                "uri": _text(found, "uri", "like a post"),
                "cid": _text(found, "cid", "like a post"),
            }
            created = await http.json(
                "POST",
                _CREATE_RECORD,
                json={
                    "repo": connection.account_id,
                    "collection": LIKE_COLLECTION,
                    "record": {
                        "$type": LIKE_COLLECTION,
                        "subject": subject,
                        "createdAt": _now().isoformat(),
                    },
                },
            )

        return LikeResult(
            post_id=post_id,
            like_id=_text(created, "uri", "like a post"),
            raw=created,
        )

    async def unlike(
        self,
        connection: Connection,
        post_id: str,
        *,
        like_id: str | None = None,
    ) -> None:
        """Take back a like.

        Passing `like_id` - kept from `LikeResult.like_id` or
        `PostDetails.my_like_id` - costs nothing: one `deleteRecord` and
        that is all. Left out, this looks the like up first: one
        `getPosts`, then one `deleteRecord` if a like was actually found.
        A post that was never liked, or a like already gone, both succeed
        and do nothing.

        Args:
            connection: The account taking the like back.
            post_id: The post or comment to unlike.
            like_id: The like's own address, when it is already known.
        """
        async with self._client(
            _clean_host(connection.host), connection.token.access_token
        ) as http:
            rkey: str | None = None
            if like_id is not None:
                rkey = _rkey_of(like_id)
            else:
                lookup = await http.json("GET", _GET_POSTS, params={"uris": post_id})
                posts = lookup.get("posts")
                found = posts[0] if isinstance(posts, list) and posts else None
                if isinstance(found, dict):
                    _, existing_like = _viewer_like(found)
                    if existing_like is not None:
                        rkey = _rkey_of(existing_like)

            if rkey is None:
                return

            await http.json(
                "POST",
                _DELETE_RECORD,
                json={
                    "repo": connection.account_id,
                    "collection": LIKE_COLLECTION,
                    "rkey": rkey,
                },
            )

    async def read_likes(
        self,
        connection: Connection,
        post_id: str,
        *,
        after: str | None = None,
        limit: int | None = None,
    ) -> Page[Like]:
        """List who liked a post.

        One request: `app.bsky.feed.getLikes`.

        Args:
            connection: The account to ask as.
            post_id: The post or comment to list likes for.
            after: A `Page.next` from a previous call.
            limit: A cap on how many come back. `None` uses Bluesky's own
                default; more than 100 is capped at 100.

        Returns:
            One page of likes, each with when it happened.
        """
        params: dict[str, Any] = {"uri": post_id}
        if after is not None:
            params["cursor"] = after
        if limit is not None:
            params["limit"] = min(limit, _MAX_LIKES_PAGE)

        async with self._client(
            _clean_host(connection.host), connection.token.access_token
        ) as http:
            reply = await http.json("GET", _GET_LIKES, params=params)

        likes_raw = reply.get("likes")
        items: list[Like] = []
        if isinstance(likes_raw, list):
            for raw in likes_raw:
                if not isinstance(raw, dict):
                    continue
                actor = raw.get("actor")
                if not isinstance(actor, dict) or not isinstance(actor.get("did"), str):
                    continue
                created_raw = raw.get("createdAt")
                liked_at = (
                    _moment(created_raw) if isinstance(created_raw, str) else None
                )
                items.append(
                    Like(person=_person_from(actor), liked_at=liked_at, raw=raw)
                )

        return Page(items=tuple(items), next=_text_or_none(reply.get("cursor")))

    async def fetch_updates_after(
        self,
        connection: Connection,
        marker: str | None,
        *,
        limit: int | None = None,
    ) -> UpdateBatch:
        """Read what is new since a marker.

        Bluesky's notifications only page backwards from the newest, so the
        marker is not a cursor Bluesky gave us - it is the newest
        `indexedAt` and `uri` this platform has already handed back,
        joined together. Finding everything newer than that means reading a
        page, checking whether the marker is on it, and reading another
        page back if it is not.

        One request when `marker` is `None`: the latest page becomes the
        starting point. Otherwise, one request per page read looking for
        the marker, up to `_MAX_MARKER_PAGES` - if it still has not turned
        up by then, `more` comes back `True` so the caller reads on with
        another call straight away rather than this one holding a request
        open indefinitely.

        A notification sharing its `indexedAt` with the marker can come
        back again on the next call, alongside whatever is genuinely new -
        that is a repeat, never a loss. `SeenUpdates` and `Dispatcher` (see
        `socialchimp.events`) dedupe by `Update.id`, so handling the same
        update twice costs nothing.

        Args:
            connection: The account to ask about.
            marker: The marker from the last call's `UpdateBatch.marker`.
                `None` on the first call.
            limit: A cap on how many notifications come back per page.
                `None` uses this platform's own default; more than 100 is
                capped at 100.

        Returns:
            The new updates, oldest first, and a marker to store for next
            time.

        Raises:
            ConfigError: If `marker` is not `None` and not a marker this
                platform built. Treating it like `None` would silently
                restart from the latest page and drop whatever came after
                it, so this refuses instead of guessing.
        """
        page_limit = (
            min(limit, _MAX_LIKES_PAGE)
            if limit is not None
            else (self._updates_per_check)
        )
        parsed_marker = _own_marker(marker) if marker is not None else None

        collected: list[RawData] = []
        cursor: str | None = None
        more = False

        async with self._client(
            _clean_host(connection.host), connection.token.access_token
        ) as http:
            for _page in range(_MAX_MARKER_PAGES):
                params: dict[str, Any] = {"limit": page_limit}
                if cursor is not None:
                    params["cursor"] = cursor

                reply = await http.json("GET", _LIST_NOTIFICATIONS, params=params)
                found = reply.get("notifications")
                items = (
                    [raw for raw in found if isinstance(raw, dict)]
                    if isinstance(found, list)
                    else []
                )

                if parsed_marker is None:
                    collected = items
                    break

                marker_when, marker_uri = parsed_marker
                reached_marker = False
                for raw in items:
                    when = str(raw.get("indexedAt", ""))
                    uri = str(raw.get("uri", ""))
                    if when < marker_when or (
                        when == marker_when and uri == marker_uri
                    ):
                        reached_marker = True
                        break
                    collected.append(raw)

                if reached_marker:
                    break

                next_cursor = reply.get("cursor")
                if (
                    not isinstance(next_cursor, str)
                    or not next_cursor
                    or next_cursor == cursor
                ):
                    # Bluesky ran out of pages before the marker turned up -
                    # nothing more to read, whatever the marker once named.
                    break
                cursor = next_cursor
            else:
                more = True

        updates = tuple(
            update
            for raw in reversed(collected)
            if (update := _update_from(raw, connection=connection)) is not None
        )
        new_marker = _marker_for(collected[0]) if collected else marker

        return UpdateBatch(updates=updates, marker=new_marker, more=more)

    async def mark_seen(self, connection: Connection, marker: str) -> None:
        """Tell Bluesky a marker has been seen.

        One request: `app.bsky.notification.updateSeen`.

        Args:
            connection: The account to mark it for.
            marker: The marker that has been handled. Its `indexedAt` half
                is what is sent as `seenAt`.

        Raises:
            ConfigError: If `marker` is not a marker this platform built.
        """
        seen_at, _uri = _own_marker(marker)

        async with self._client(
            _clean_host(connection.host), connection.token.access_token
        ) as http:
            await http.json("POST", _UPDATE_SEEN, json={"seenAt": seen_at})

    async def read_conversations(
        self,
        connection: Connection,
        *,
        after: str | None = None,
        limit: int | None = None,
    ) -> Page[Conversation]:
        """List this account's direct message conversations.

        One request: `chat.bsky.convo.listConvos`.

        Args:
            connection: The account to ask as.
            after: A `Page.next` from a previous call.
            limit: A cap on how many come back.

        Returns:
            One page of conversations.

        Raises:
            MissingPermissionError: If this app password has no direct
                message access.
        """
        params: dict[str, Any] = {}
        if after is not None:
            params["cursor"] = after
        if limit is not None:
            params["limit"] = limit

        async with self._chat_client(
            _clean_host(connection.host), connection.token.access_token
        ) as http:
            reply = await http.json("GET", _LIST_CONVOS, params=params)

        convos_raw = reply.get("convos")
        items = (
            [
                _conversation_from(raw, connection=connection)
                for raw in convos_raw
                if isinstance(raw, dict)
            ]
            if isinstance(convos_raw, list)
            else []
        )

        return Page(items=tuple(items), next=_text_or_none(reply.get("cursor")))

    async def read_messages(
        self,
        connection: Connection,
        conversation_id: str,
        *,
        after: str | None = None,
        limit: int | None = None,
    ) -> Page[Message]:
        """Read the messages in one conversation, newest first.

        One request: `chat.bsky.convo.getMessages`. Bluesky sends the
        people in the conversation separately, under `relatedProfiles`,
        rather than on each message - that is where a message's sender is
        looked up.

        Args:
            connection: The account to ask as.
            conversation_id: Which conversation to read.
            after: A `Page.next` from a previous call. Passing it goes
                further back in time.
            limit: A cap on how many come back.

        Returns:
            One page of messages, newest first.

        Raises:
            MissingPermissionError: If this app password has no direct
                message access.
        """
        params: dict[str, Any] = {"convoId": conversation_id}
        if after is not None:
            params["cursor"] = after
        if limit is not None:
            params["limit"] = limit

        async with self._chat_client(
            _clean_host(connection.host), connection.token.access_token
        ) as http:
            reply = await http.json("GET", _GET_MESSAGES, params=params)

        profiles_raw = reply.get("relatedProfiles")
        profiles = (
            [profile for profile in profiles_raw if isinstance(profile, dict)]
            if isinstance(profiles_raw, list)
            else []
        )

        messages_raw = reply.get("messages")
        items = (
            [
                _message_from(
                    raw,
                    conversation_id=conversation_id,
                    profiles=profiles,
                    connection=connection,
                )
                for raw in messages_raw
                if isinstance(raw, dict)
            ]
            if isinstance(messages_raw, list)
            else []
        )

        return Page(items=tuple(items), next=_text_or_none(reply.get("cursor")))

    async def send_message(
        self,
        connection: Connection,
        conversation_id: str,
        text: str,
        *,
        options: RawData | None = None,
    ) -> Message:
        """Send a message into an existing conversation.

        One request: `chat.bsky.convo.sendMessage`. A web address in `text`
        is marked up as a link the same way `publish` marks one up in a
        post; an `@handle` is left as plain words, since marking one up
        would mean a `resolveHandle` lookup on every message sent, which is
        a cost `publish` only ever pays for a post, not a reply to one.

        Args:
            connection: The account to send as.
            conversation_id: Which conversation to send into.
            text: The message's words.
            options: Not used - Bluesky's chat messages take no settings of
                their own yet. Passing any raises.

        Returns:
            The message that was sent.

        Raises:
            InvalidPostError: If `options` names a setting.
            MissingPermissionError: If this app password has no direct
                message access.
        """
        if options:
            check_option_names(options, platform=PLATFORM_NAME, allowed=())

        message: dict[str, Any] = {"text": text}
        facets = facets_for(text)
        if facets:
            message["facets"] = facets

        async with self._chat_client(
            _clean_host(connection.host), connection.token.access_token
        ) as http:
            reply = await http.json(
                "POST",
                _SEND_MESSAGE,
                json={"convoId": conversation_id, "message": message},
            )

        return _message_from(
            reply, conversation_id=conversation_id, profiles=(), connection=connection
        )

    async def mark_read(self, connection: Connection, conversation_id: str) -> None:
        """Mark a conversation as read.

        One request: `chat.bsky.convo.updateRead`.

        Args:
            connection: The account to mark it for.
            conversation_id: Which conversation to mark.

        Raises:
            MissingPermissionError: If this app password has no direct
                message access.
        """
        async with self._chat_client(
            _clean_host(connection.host), connection.token.access_token
        ) as http:
            await http.json("POST", _UPDATE_READ, json={"convoId": conversation_id})

    async def start_conversation(
        self,
        connection: Connection,
        person_ids: Sequence[str],
        text: str,
    ) -> Message:
        """Start a conversation with one or more people.

        Two requests: `chat.bsky.convo.getConvoForMembers` to find or
        create the conversation, then `chat.bsky.convo.sendMessage` into
        it.

        Args:
            connection: The account to send as.
            person_ids: Who to start it with, by their DIDs.
            text: The first message's words.

        Returns:
            The message that was sent.

        Raises:
            MissingPermissionError: If this app password has no direct
                message access.
            PlatformError: If Bluesky answered without a conversation to
                send into.
        """
        async with self._chat_client(
            _clean_host(connection.host), connection.token.access_token
        ) as http:
            reply = await http.json(
                "GET",
                _GET_CONVO_FOR_MEMBERS,
                params={"members": list(person_ids)},
            )
            convo = reply.get("convo")
            convo_id = convo.get("id") if isinstance(convo, dict) else None
            if not isinstance(convo_id, str) or not convo_id:
                message_text = (
                    "Bluesky answered getConvoForMembers without a "
                    "conversation to send into. The whole reply is on this "
                    "error."
                )
                raise PlatformError(message_text, platform=PLATFORM_NAME, raw=reply)

            message: dict[str, Any] = {"text": text}
            facets = facets_for(text)
            if facets:
                message["facets"] = facets

            sent = await http.json(
                "POST",
                _SEND_MESSAGE,
                json={"convoId": convo_id, "message": message},
            )

        return _message_from(
            sent, conversation_id=convo_id, profiles=(), connection=connection
        )


def _answer_to(callback: Mapping[str, str], field: LoginField) -> str:
    """Read one of the answers off the form your app showed.

    Args:
        callback: What the person typed.
        field: Which answer we are after.

    Returns:
        The answer, with any stray spaces taken off - people paste an app
        password with a space on the end more often than you would think.

    Raises:
        AuthError: If it is missing or empty, saying what to ask for.
    """
    answer = (callback.get(field.name) or "").strip()
    if not answer:
        message = (
            f"This sign-in has no {field.name!r} in it. Bluesky needs a "
            f"handle and an app password, and `start_login` says exactly "
            f"what to put on the form. Pass both answers to finish_login as "
            f"the callback."
        )
        raise AuthError(message, platform=PLATFORM_NAME)
    return answer


def _token_from(reply: RawData, when: str) -> Token:
    """Build a token out of what Bluesky answered a sign-in or a renewal with.

    Args:
        reply: What Bluesky said.
        when: What we had asked it to do, for the message if a piece is
            missing.

    Returns:
        The pair of tokens, with the expiry read out of the first one.

    Raises:
        PlatformError: If either token is missing.
    """
    access = _text(reply, "accessJwt", when)
    return Token(
        access_token=access,
        refresh_token=_text(reply, "refreshJwt", when),
        expires_at=_expires_at(access),
    )
