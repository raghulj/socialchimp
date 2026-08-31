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

## What a post can carry

`Post.options` accepts one setting:

    Post(text="Hei", options={"langs": ["nb", "en"]})

Anything else is refused before we send it, with a message listing what is
accepted.

## What Bluesky cannot do

- **No scheduling.** `Feature.SCHEDULE` is missing, so a post with
  `publish_at` is refused rather than published now.
- **No app to register.** There is nothing to create, so there is no
  `create_app`.
- **No video here yet.** Bluesky takes video, but through a separate service
  with a token of its own. `Feature.POST_VIDEO` is off until that is written.
"""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import httpx

from socialchimp.errors import (
    AuthError,
    InvalidPostError,
    NotFoundError,
    PlatformError,
    TokenExpiredError,
)
from socialchimp.events import Update
from socialchimp.features import Feature, Limits, check_post
from socialchimp.http import HttpClient, error_from_response, read_body
from socialchimp.models import (
    Connection,
    Media,
    Post,
    PostResult,
    PostState,
    RawData,
    Token,
)
from socialchimp.platform import AskForDetails, Finished, LoginField, LoginRequest

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from socialchimp.errors import SocialChimpError
    from socialchimp.http import Retries

__all__ = ["BlueskyPlatform", "bluesky_errors", "count_graphemes", "facets_for"]

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
_OUR_WORD_FOR: Final = {
    "like": "reaction_added",
    "repost": "reaction_added",
    "reply": "comment_created",
    "mention": "mention",
}

# Names Bluesky gives a 400 when the trouble is really the sign-in. It
# answers 400 rather than 401 for a token that has run out, which sends
# people hunting in the wrong place, so we name them here.
_SIGN_IN_PROBLEMS: Final = ("ExpiredToken", "InvalidToken", "AuthenticationRequired")

# Every address we send to. Bluesky puts them all under /xrpc and names them
# after the definition each one follows.
_CREATE_SESSION: Final = "/com.atproto.server.createSession"
_REFRESH_SESSION: Final = "/com.atproto.server.refreshSession"
_CREATE_RECORD: Final = "/com.atproto.repo.createRecord"
_DELETE_RECORD: Final = "/com.atproto.repo.deleteRecord"
_UPLOAD_BLOB: Final = "/com.atproto.repo.uploadBlob"
_GET_POSTS: Final = "/app.bsky.feed.getPosts"
_RESOLVE_HANDLE: Final = "/com.atproto.identity.resolveHandle"
_LIST_NOTIFICATIONS: Final = "/app.bsky.notification.listNotifications"

# A token is three pieces joined by dots.
_JWT_PIECES: Final = 3

# How long to trust a token whose expiry we could not read. Short on purpose:
# renewing a token that had hours left costs one request, while trusting one
# that had seconds left costs a failed post.
_ASSUMED_LIFETIME_SECONDS: Final = 60.0

# Marks that hang off the letter before them - accents, the character that
# turns a symbol into an emoji, the ring drawn around a keycap.
_MARKS: Final = frozenset({"Mn", "Mc", "Me"})

# Joins two pictures into one, as in a family emoji. Written as an escape
# because the character itself is invisible: a literal one here looks like
# an empty string, and gets "tidied up" by the next person who reads it.
_JOINER: Final = "\u200d"

# The five skin tones. They are symbols rather than marks, so they have to be
# named here to be counted as part of the emoji they follow.
_SKIN_TONES: Final = frozenset(chr(point) for point in range(0x1F3FB, 0x1F400))

# Flags are written as two of these letters together.
_FIRST_FLAG_LETTER: Final = "\U0001f1e6"
_LAST_FLAG_LETTER: Final = "\U0001f1ff"

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


def _attaches_to_the_letter_before(character: str) -> bool:
    """Say whether this is part of the letter in front of it.

    Args:
        character: The character to judge.

    Returns:
        True for accents, for the mark that turns a symbol into an emoji,
        and for the five skin tones - none of which a person would call a
        letter of their own.
    """
    return unicodedata.category(character) in _MARKS or character in _SKIN_TONES


def count_graphemes(text: str) -> int:
    """Count what a person would call the letters in some text.

    Bluesky's limit of 300 is counted this way, not in characters. A family
    emoji is seven characters and one letter; a flag is two and one; an
    accented letter can be either. Counting characters instead refuses posts
    Bluesky would have taken.

    This is an approximation, and worth being honest about where it sits.
    Python has no grapheme splitter of its own and we add no dependencies,
    so this handles accents and other marks, skin tones, joined emoji and
    flags - which is what people actually type. It over-counts a few writing
    systems where a syllable is built from several characters, and the cost
    of that is refusing a post that would have been fine. If you need it
    exact, count with a library of your own and check before you post.

    Args:
        text: The words to count.

    Returns:
        How many letters a person would say that is.
    """
    count = 0
    joined_to_the_last = False
    half_a_flag = False

    for character in text:
        if character == _JOINER:
            joined_to_the_last = True
            continue

        if _attaches_to_the_letter_before(character):
            continue

        if joined_to_the_last:
            joined_to_the_last = False
            continue

        if _FIRST_FLAG_LETTER <= character <= _LAST_FLAG_LETTER:
            # Flags come in pairs, and the pair is one letter.
            half_a_flag = not half_a_flag
            if not half_a_flag:
                continue
            count += 1
            continue

        half_a_flag = False
        count += 1

    return count


def _check_length(text: str) -> None:
    """Check a post against both of Bluesky's limits before sending it.

    Args:
        text: The words about to be posted.

    Raises:
        InvalidPostError: If the post is too long either way.
    """
    letters = count_graphemes(text)
    if letters > MAX_GRAPHEMES:
        message = (
            f"This post is {letters} letters but bluesky allows at most "
            f"{MAX_GRAPHEMES}."
        )
        raise InvalidPostError(message)

    written = len(text.encode())
    if written > MAX_TEXT_BYTES:
        message = (
            f"This post is {MAX_GRAPHEMES} letters or fewer, but takes "
            f"{written} bytes to write out, and bluesky allows at most "
            f"{MAX_TEXT_BYTES}. Emoji take four bytes each and accented "
            f"letters two, so a short post can still be over."
        )
        raise InvalidPostError(message)


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


def bluesky_errors(response: httpx.Response) -> SocialChimpError:
    """Turn an unhappy reply from Bluesky into a socialchimp error.

    Bluesky puts a short name in every refusal, and only its 400s need us to
    read it. Two of those names are worth naming here:

    - A token that has run out comes back as **400 ExpiredToken**, not 401.
      Anyone who maps by status alone reads that as a bad post and never
      renews, so it becomes an `AuthError` here.
    - `InvalidRequest` is what a post that breaks a rule looks like, so it
      becomes an `InvalidPostError` with whatever Bluesky said kept on it.

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

    if named in _SIGN_IN_PROBLEMS:
        message = (
            f"Bluesky would not accept our sign-in ({named}), which it "
            f"reports as a 400 rather than a 401. The token has run out or "
            f"been taken away; renewing it, or asking the person to connect "
            f"their account again, is what fixes it.{detail}"
        )
        return AuthError(message, platform=PLATFORM_NAME, raw=body)

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
    unknown = [key for key in options if key not in POST_OPTIONS]
    if unknown:
        message = (
            f"Bluesky does not know the post option {unknown[0]!r}. It "
            f"accepts: {', '.join(POST_OPTIONS)}."
        )
        raise InvalidPostError(message)

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
        features: What Bluesky can do here. There is no app to register and
            no way to ask for a post later, so `CREATE_APP` and `SCHEDULE`
            are missing. Video is missing too - Bluesky takes it, through a
            separate service we have not written yet.
    """

    name: str = PLATFORM_NAME

    features: Feature = (
        Feature.POST_TEXT
        | Feature.POST_IMAGE
        | Feature.REPLY
        | Feature.DELETE_POST
        | Feature.READ_POSTS
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

        The 300 is counted in letters as a person would count them, not in
        characters - see `count_graphemes`.

        Args:
            connection: The account to ask about. Not used here.

        Returns:
            What Bluesky allows right now.
        """
        return Limits(max_text_length=MAX_GRAPHEMES, max_images=MAX_IMAGES)

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

    async def refresh(self, connection: Connection) -> Token:
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

        # Bluesky counts letters as a person would and bytes as a machine
        # does, and refuses a post that is over either. `check_post` counts
        # characters, which is neither, so the length is checked here and
        # taken out of what it is given - otherwise a post of family emoji
        # would be refused for a limit Bluesky does not have.
        _check_length(post.text)
        allowed = await self.limits(connection)
        check_post(
            post,
            platform=PLATFORM_NAME,
            features=self.features,
            limits=replace(allowed, max_text_length=None),
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
            when = _moment(str(raw.get("indexedAt", "")))
            if when is None or (since is not None and when <= since):
                continue
            word = str(raw.get("reason", ""))
            updates.append(
                Update.from_network(
                    update_id=str(raw.get("uri", "")),
                    kind_name=_OUR_WORD_FOR.get(word, word),
                    platform=PLATFORM_NAME,
                    connection_id=connection.id,
                    created_at=when,
                    raw=raw,
                )
            )

        # Bluesky hands back the newest first; socialchimp wants the oldest.
        updates.reverse()
        return updates


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
