"""Mastodon: the one network you can start using in five minutes.

Everywhere else, before a single line of your code runs, you fill in a form
in a developer portal and wait for somebody to approve your app. Mastodon has
no portal and no review. You ask a server to register your app, it answers
straight away with the two values you need, and you are signing people in.

That is why Mastodon is the first network socialchimp supports.

## Every server is its own network

Mastodon is thousands of separate servers. `mastodon.social` and
`fosstodon.org` share software, not accounts, not settings, and not your app.
An app registered on one means nothing on the other, so **every server needs
its own registration**, and everything here takes a `host`:

    app = await mastodon.create_app(
        name="My App",
        redirect_uri="https://myapp.example/callback",
        host="mastodon.social",
    )

Save what comes back and hand it to every login on that server. The next
server somebody signs in on needs a registration of its own.

## Signing someone in

Two steps, and socialchimp does the fiddly part:

1. `start_login` gives you a web address. Send the person's browser there.
   They see Mastodon's own "do you allow this app?" page.
2. Mastodon sends them back to your `redirect_uri` with a short code in the
   query string. Hand that whole query to `finish_login`, along with the
   `remember` value from step one, and you get a connection to save.

The address from step one carries a hashed secret (Mastodon and everybody
else call this PKCE). socialchimp makes the secret, sends only the hash to
Mastodon, and sends the secret itself when it swaps the code for a token.
That way a code stolen out of a browser's history is worth nothing without
the secret, which never left your server. Newer Mastodon servers expect this;
older ones ignore it.

The secret comes back to you in `SendToNetwork.remember`. Keep it with that
person's session - not in memory - because the person may be sent away by one
web worker and come back to another.

## Tokens do not expire

A Mastodon access token works until the person revokes it. There is no
refresh token because there is nothing to refresh. `Token.expires_at` stays
`None` and `refresh()` hands the same token straight back without calling
anything. That is a real property of Mastodon, not something missing here.

## What a post can carry

`Post.options` accepts four settings, all Mastodon's own:

    Post(
        text="Hello",
        options={
            "visibility": "unlisted",       # public, unlisted, private, direct
            "spoiler_text": "Film ending",  # the warning shown before the text
            "sensitive": True,              # hide pictures behind a click
            "language": "en",               # two-letter language code
        },
    )

Anything else is refused before we send it, with a message listing what is
accepted.

## Reading a post's numbers

`read_stats` hands back the three numbers Mastodon keeps about a status -
replies, favourites and boosts - in a single request. There is no reach, no
impressions and no click count anywhere in Mastodon's API, so nothing here
reports them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import replace
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlparse

# anyio comes with httpx, so waiting through it adds no new dependency and
# lets this run under trio as happily as under asyncio.
import anyio
import httpx

from socialchimp.errors import (
    AuthError,
    ConfigError,
    InvalidPostError,
    MissingPermissionError,
    NotFoundError,
    PlatformError,
    PostGoneError,
)
from socialchimp.events import Update, UpdateBatch
from socialchimp.features import (
    Feature,
    Limits,
    TextCount,
    check_option_names,
    check_post,
)
from socialchimp.http import HttpClient, error_from_response, read_body
from socialchimp.models import (
    AppCredentials,
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
    PostStats,
    RawData,
    TextLink,
    Thread,
    Token,
    Visibility,
)
from socialchimp.platform import Finished, LoginRequest, SendToNetwork

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from socialchimp.errors import SocialChimpError
    from socialchimp.http import Retries

__all__ = ["MastodonPlatform", "mastodon_errors", "post_fingerprint"]

PLATFORM_NAME: Final = "mastodon"

DEFAULT_SCOPES: Final = ("read", "write", "push")
"""Enough to read an account's own timeline, post as them, and use Web Push.

Mastodon also has narrower scopes such as `write:statuses`. Ask for those
instead if your app only ever posts - people are more likely to say yes to a
smaller request.

`push` is not used by anything in this release - Web Push arrives in a later
one, together with Meta's webhooks. It is asked for now anyway, on the
owner's decision, so a connection made today does not have to be remade the
day Web Push ships. A server that refuses it grants what it can; see
`Connection.scopes` for what actually came back.
"""

VISIBILITIES: Final = ("public", "unlisted", "private", "direct")
"""Who can see a post. `direct` is a message to the people it mentions."""

POST_OPTIONS: Final = ("visibility", "spoiler_text", "sensitive", "language")
"""The settings `Post.options` accepts here. Anything else is refused."""

# What Mastodon does when nobody has changed it. Servers do change it, which
# is why we ask the server rather than trusting these.
DEFAULT_MAX_CHARACTERS: Final = 500
DEFAULT_MAX_MEDIA: Final = 4

# Mastodon takes one video per post, and will not mix video with pictures.
MAX_VIDEOS_PER_POST: Final = 1

# The kinds of notification worth telling an app about. Mastodon has more -
# polls ending, moderation warnings - which nobody has asked for yet.
WATCHED_NOTIFICATIONS: Final = ("mention", "favourite", "reblog", "follow")

# Mastodon's word for something, and ours. `mention` is deliberately not
# here: what it becomes depends on the status it names, not the word alone -
# see `_kind_and_about`. A word missing from here is passed through as it is
# and lands as `UpdateKind.UNKNOWN` with Mastodon's own word kept on the
# update, so a kind we have never seen still reaches your app.
#
# CHANGE (0.8.0): `reblog` used to map to `reaction_added` and `follow` had
# no mapping at all, so it arrived as `UNKNOWN`. They have their own kinds
# now - `REPOST_ADDED` and `FOLLOWED` - see docs/social-inbox-contract.md.
_OUR_WORD_FOR: Final = {
    "favourite": "reaction_added",
    "reblog": "repost_added",
    "follow": "followed",
}

# The narrowest Mastodon visibility Mastodon's `favourited_by` list allows in
# one page.
_MAX_FAVOURITED_BY: Final = 80

# The most conversations Mastodon hands back in one page of
# GET /api/v1/conversations.
_MAX_CONVERSATIONS: Final = 40

# `in_reply_to_id` in a reply group that we cannot resolve gets this depth,
# rather than being dropped - see `_reply_depths`.
_FALLBACK_REPLY_DEPTH: Final = 1

# Mastodon writes who liked, followed, boosted or was replied to as its own
# `Account` object, and every list of them - favourited_by, conversations -
# pages the same way: a `Link` header with `rel="next"` pointing at the next
# `max_id`. `Page.next` here is that `max_id` value on its own; pass it back
# as `after=` and it is turned back into `max_id=` for you.
_LINK_ENTRY: Final = re.compile(r'<([^>]+)>\s*;\s*rel="([^"]+)"')

# Where Mastodon's own web app puts a link's target for these three kinds -
# a mentioned person's id, a hashtag's bare name, or a plain link's address.
_VISIBILITY_FROM_WIRE: Final[dict[str, Visibility]] = {
    "public": Visibility.PUBLIC,
    "unlisted": Visibility.UNLISTED,
    "private": Visibility.FOLLOWERS,
    "direct": Visibility.DIRECT,
}

# Narrowest first. A reply is never let out wider than the post it replies
# to - see `_narrower_visibility`.
_VISIBILITY_NARROWNESS: Final[dict[str, int]] = {
    "direct": 0,
    "private": 1,
    "unlisted": 2,
    "public": 3,
}

# Long enough that nobody can guess one, short enough to sit in a URL.
_STATE_BYTES: Final = 24
_VERIFIER_BYTES: Final = 48


async def _wait(seconds: float) -> None:
    """Pause while a server finishes working on a file.

    Kept as its own function so tests can watch the pauses instead of
    sitting through them.

    Args:
        seconds: How long to wait.
    """
    await anyio.sleep(seconds)


def _clean_host(host: str | None) -> str:
    """Turn whatever somebody wrote into a bare server name.

    `"https://mastodon.social/"` and `"mastodon.social"` mean the same
    server, and both are things people type.

    Args:
        host: The server, however it was written.

    Returns:
        Just the name, such as `"mastodon.social"`.

    Raises:
        ConfigError: If no server was named. Mastodon is thousands of
            separate servers, so there is no sensible one to guess.
    """
    cleaned = (
        (host or "").strip().removeprefix("https://").removeprefix("http://").strip("/")
    )
    if not cleaned:
        message = (
            "Mastodon needs to know which server. Every Mastodon server is "
            'separate, so pass host="mastodon.social" - or whichever server '
            "this account is on."
        )
        raise ConfigError(message)
    return cleaned


def _host_of(connection: Connection) -> str:
    """Work out which server a connected account lives on.

    Args:
        connection: The account to look at.

    Returns:
        The server name.

    Raises:
        ConfigError: If the connection was saved without one.
    """
    return _clean_host(connection.host)


def _text(reply: RawData, key: str, when: str) -> str:
    """Read a value Mastodon always sends, and complain plainly if it did not.

    Args:
        reply: What Mastodon answered.
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
        f"Mastodon left {key!r} out of its reply when we asked it to {when}. "
        f"That should not happen. The whole reply is on this error."
    )
    raise PlatformError(message, platform=PLATFORM_NAME, raw=reply)


def _section(parent: RawData, name: str) -> RawData:
    """Read one nested object out of a reply, or an empty one.

    Servers run different versions and leave parts out, so a missing section
    is normal rather than a failure.

    Args:
        parent: The object to look inside.
        name: The section to read.

    Returns:
        The section, or `{}` if it is missing or not an object.
    """
    value = parent.get(name)
    return value if isinstance(value, dict) else {}


def _number(section: RawData, name: str, fallback: int | None) -> int | None:
    """Read a count out of a reply, falling back when it is not there.

    Args:
        section: The object to look inside.
        name: The field to read.
        fallback: What to use when the field is missing or not a number.

    Returns:
        The count, or the fallback.
    """
    value = section.get(name)
    return value if isinstance(value, int) else fallback


def _moment(text: str) -> datetime | None:
    """Read a time Mastodon wrote, such as `"2026-08-31T10:00:00.000Z"`.

    Args:
        text: The time as it arrived.

    Returns:
        The moment, always with a timezone, or `None` if it cannot be read.
    """
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    # Mastodon always writes UTC, but a fork might leave the zone off, and a
    # time with no zone compares wrongly against every other time we hold.
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _challenge_for(verifier: str) -> str:
    """Hash the secret we keep, so only the hash travels to Mastodon.

    Args:
        verifier: The secret made at the start of a login.

    Returns:
        The hash, written the way Mastodon expects it.
    """
    digest = hashlib.sha256(verifier.encode()).digest()
    # Base64 with the two URL-unsafe characters swapped and the padding
    # dropped, which is what the PKCE rules ask for.
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def mastodon_errors(response: httpx.Response) -> SocialChimpError:
    """Turn an unhappy reply from Mastodon into a socialchimp error.

    Three statuses need a word of their own. Mastodon answers 422 when a post
    breaks one of its rules - too long, empty, a picture it will not take -
    and that is a problem with the post rather than a mystery, so it comes
    back as `InvalidPostError`. A 404 on a status endpoint - reading it,
    replying to it, favouriting it - means the post is gone, so it comes back
    as `PostGoneError` rather than a plain `NotFoundError`, though it still is
    one. A 403 that names the missing scope comes back as
    `MissingPermissionError`.

    What does not get a word of its own: Mastodon answers "This action is not
    allowed" for a genuine block *and* for every other permission its
    policies refuse (favouriting a post whose author blocked you passes the
    same check that a plain missing permission fails) - there is no way to
    tell a block apart from that message alone, so it stays a plain
    `NotAllowedError` rather than guessing at `BlockedError`.

    Everything else is the shared mapping: 401 is an `AuthError`, 403 a
    `NotAllowedError`, 404 a `NotFoundError`, 429 a `RateLimitError`.

    Args:
        response: The reply to turn into an error.

    Returns:
        The error to raise.
    """
    if response.status_code == httpx.codes.UNPROCESSABLE_ENTITY:
        body = read_body(response)
        said = body.get("error")
        detail = f" It said: {said}" if isinstance(said, str) and said else ""
        message = (
            f"Mastodon would not accept this post (422). Something in it "
            f"breaks a rule of that server.{detail}"
        )
        return InvalidPostError(message, platform=PLATFORM_NAME, raw=body)

    if (
        response.status_code == httpx.codes.NOT_FOUND
        and "/api/v1/statuses/" in response.request.url.path
    ):
        body = read_body(response)
        said = body.get("error")
        detail = f" It said: {said}" if isinstance(said, str) and said else ""
        message = (
            f"Mastodon has no such post (404). It was deleted, never "
            f"existed, or its author has blocked us - Mastodon answers the "
            f"same way for all three.{detail}"
        )
        return PostGoneError(message, platform=PLATFORM_NAME, raw=body)

    if response.status_code == httpx.codes.FORBIDDEN:
        body = read_body(response)
        said = body.get("error")
        if isinstance(said, str) and "outside the authorized scopes" in said:
            return MissingPermissionError(
                needs="a wider scope",
                suggestion=(
                    "Reconnect this account and ask for the scope this call "
                    "needs - this token was granted less than it is being "
                    "asked to do."
                ),
                platform=PLATFORM_NAME,
                raw=body,
            )

    return error_from_response(response, platform=PLATFORM_NAME)


def post_fingerprint(post: Post) -> str:
    """Return a short code that stands for this exact post.

    It goes out as Mastodon's `Idempotency-Key` header. Mastodon remembers
    that header for an hour: send the same one twice and the second request
    gives you back the first post instead of making a second one. So if a
    reply is lost on the way back to us and the request is sent again, the
    person's followers still see one post, not two.

    The code is built from what you asked for - the words, what it replies
    to, when it should go out, the settings, and the files by name - and not
    from anything the server hands back. A file uploaded twice gets two
    different ids, and hashing those would give a different code every time,
    which is exactly the case this is meant to protect.

    Args:
        post: The post about to be sent.

    Returns:
        A hex string, the same every time for the same post.
    """
    parts = {
        "text": post.text,
        "reply_to": post.reply_to,
        "publish_at": (
            post.publish_at.isoformat() if post.publish_at is not None else None
        ),
        "options": {str(key): str(value) for key, value in post.options.items()},
        "media": [
            {
                "kind": item.kind.name,
                "filename": item.filename,
                "url": item.url,
                "alt_text": item.alt_text,
            }
            for item in post.media
        ],
    }
    written = json.dumps(parts, sort_keys=True).encode()
    return hashlib.sha256(written).hexdigest()


def _checked_option(key: str, value: object) -> str:
    """Check one post setting and turn it into what a form can carry.

    Args:
        key: Which setting it is.
        value: What was given for it.

    Returns:
        The value as text, ready to send.

    Raises:
        InvalidPostError: If the value is not one Mastodon takes. The message
            lists what is accepted.
    """
    if key == "visibility":
        if value not in VISIBILITIES:
            message = (
                f"visibility is {value!r}, which Mastodon does not know. It "
                f"accepts: {', '.join(VISIBILITIES)}."
            )
            raise InvalidPostError(message)
        return str(value)

    if key == "sensitive":
        if not isinstance(value, bool):
            message = (
                f"sensitive is {value!r}, but it has to be True or False. It "
                f"decides whether pictures are hidden behind a click."
            )
            raise InvalidPostError(message)
        return "true" if value else "false"

    if not isinstance(value, str) or not value:
        message = f"{key} is {value!r}, but it has to be some text."
        raise InvalidPostError(message)
    return value


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
    return {key: _checked_option(key, value) for key, value in options.items()}


def _limits_from_instance(reply: RawData) -> Limits:
    """Read what one server currently allows out of its own description.

    Args:
        reply: What `/api/v2/instance` answered.

    Returns:
        The limits, falling back to Mastodon's defaults for anything the
        server did not mention.
    """
    configuration = _section(reply, "configuration")
    statuses = _section(configuration, "statuses")
    attachments = _section(configuration, "media_attachments")

    return Limits(
        max_text_length=_number(statuses, "max_characters", DEFAULT_MAX_CHARACTERS),
        # Mastodon is one of the few networks that really does mean
        # characters when it says characters, so this is said out loud
        # rather than left to the default. A family emoji costs seven of a
        # server's 500 here, where on Bluesky it costs one of 300.
        text_counted_in=TextCount.CHARACTERS,
        max_images=_number(statuses, "max_media_attachments", DEFAULT_MAX_MEDIA),
        max_image_bytes=_number(attachments, "image_size_limit", None),
        max_videos=MAX_VIDEOS_PER_POST,
        max_video_bytes=_number(attachments, "video_size_limit", None),
    )


def _dicts_from(raw: object) -> list[RawData]:
    """Keep only the objects in a list Mastodon sent, dropping anything else.

    Args:
        raw: Whatever was under a key that should hold a list of objects.

    Returns:
        The objects, in the order they arrived. Empty if `raw` was not a
        list at all.
    """
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _handle_for(account: RawData, server: str) -> str | None:
    """Turn Mastodon's `acct` into a full `user@host` handle.

    Mastodon only writes the host on a remote account's `acct` - a local
    one, on the server we are asking, comes back bare.

    Args:
        account: The `Account` object.
        server: The server this account was read from.

    Returns:
        The handle, or `None` if Mastodon sent no `acct` at all.
    """
    acct = account.get("acct")
    if not isinstance(acct, str) or not acct:
        return None
    return acct if "@" in acct else f"{acct}@{server}"


def _account_person(account: RawData, server: str) -> Person:
    """Build a `Person` out of one of Mastodon's own `Account` objects.

    Args:
        account: The `Account` object, exactly as Mastodon sent it.
        server: The server this account was read from, needed to turn a
            bare local username into a full handle - see `_handle_for`.

    Returns:
        The person.
    """
    account_id = account.get("id")
    display_name = account.get("display_name")
    avatar = account.get("avatar")
    url = account.get("url")
    return Person(
        id=str(account_id) if account_id is not None else "",
        handle=_handle_for(account, server),
        display_name=(
            display_name if isinstance(display_name, str) and display_name else None
        ),
        avatar_url=avatar if isinstance(avatar, str) and avatar else None,
        url=url if isinstance(url, str) and url else None,
        raw=account,
    )


def _attachment_from(media: RawData) -> Attachment:
    """Build an `Attachment` out of one of Mastodon's `MediaAttachment` objects.

    Args:
        media: The `MediaAttachment` object.

    Returns:
        The attachment.
    """
    kind = media.get("type")
    original = _section(_section(media, "meta"), "original")
    url = media.get("url")
    preview = media.get("preview_url")
    description = media.get("description")
    return Attachment(
        kind=kind if isinstance(kind, str) and kind else "unknown",
        url=url if isinstance(url, str) and url else None,
        preview_url=preview if isinstance(preview, str) and preview else None,
        alt_text=description if isinstance(description, str) and description else None,
        width=_number(original, "width", None),
        height=_number(original, "height", None),
        raw=media,
    )


def _attachments_from(raw: object) -> tuple[Attachment, ...]:
    """Build every `Attachment` on a status.

    Args:
        raw: What was under `media_attachments`.

    Returns:
        The attachments, in the order Mastodon sent them.
    """
    return tuple(_attachment_from(item) for item in _dicts_from(raw))


class _ContentParser(HTMLParser):
    """Turns one status's `content` HTML into plain text and its links.

    Mastodon always sends fully-formed HTML for a status's words: `<p>` for
    each paragraph, `<br>` for a line break typed inside one, and `<span
    class="invisible">` around the parts of a link nobody needs to read - the
    `https://` at the front, the rest of the address once thirty characters
    of it have been shown. This walks that HTML once and produces both the
    plain text a person would read and the character offsets of every
    mention, hashtag and link inside it.

    A `<span class="ellipsis">` is not invisible, so its text stays in - a
    long link that Mastodon has shortened for display still reads correctly,
    even though its `TextLink.target` is the whole address, not the
    shortened text.
    """

    def __init__(self, mentions: Sequence[RawData]) -> None:
        """Get ready to read one status's `content`.

        Args:
            mentions: The status's own `mentions` list, used to turn a
                mention link's address into the mentioned person's id.
        """
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._length = 0
        self._invisible_depth = 0
        self._span_is_invisible: list[bool] = []
        self._anchor_starts: list[tuple[int, LinkKind, str]] = []
        self._seen_a_paragraph = False
        self._mention_ids_by_url: dict[str, str] = {
            str(mention["url"]): str(mention["id"])
            for mention in mentions
            if isinstance(mention.get("url"), str) and mention.get("id") is not None
        }
        self.links: list[TextLink] = []

    @property
    def text(self) -> str:
        """The plain text read so far."""
        return "".join(self._chunks)

    def _append(self, piece: str) -> None:
        """Add to the plain text, unless we are inside an invisible span.

        Args:
            piece: The text to add.
        """
        if not piece or self._invisible_depth:
            return
        self._chunks.append(piece)
        self._length += len(piece)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Act on one opening tag.

        Args:
            tag: The tag's name.
            attrs: Its attributes.
        """
        values = dict(attrs)
        if tag == "p":
            if self._seen_a_paragraph:
                self._append("\n\n")
            self._seen_a_paragraph = True
        elif tag == "br":
            self._append("\n")
        elif tag == "span":
            invisible = "invisible" in (values.get("class") or "").split()
            self._span_is_invisible.append(invisible)
            if invisible:
                self._invisible_depth += 1
        elif tag == "a":
            classes = (values.get("class") or "").split()
            href = values.get("href") or ""
            if "hashtag" in classes:
                kind = LinkKind.TAG
            elif "mention" in classes:
                kind = LinkKind.MENTION
            else:
                kind = LinkKind.LINK
            self._anchor_starts.append((self._length, kind, href))

    # `handle_startendtag` is not overridden: the base class's own version -
    # call `handle_starttag` then `handle_endtag` - is exactly right for
    # Mastodon's self-closing `<br />`, the only tag of that shape it sends.

    def handle_endtag(self, tag: str) -> None:
        """Act on one closing tag.

        Args:
            tag: The tag's name.
        """
        if tag == "span" and self._span_is_invisible:
            if self._span_is_invisible.pop():
                self._invisible_depth -= 1
        elif tag == "a" and self._anchor_starts:
            start, kind, href = self._anchor_starts.pop()
            self.links.append(self._link_for(start, self._length, kind, href))

    def _link_for(self, start: int, end: int, kind: LinkKind, href: str) -> TextLink:
        """Build the `TextLink` for one anchor that has just closed.

        Args:
            start: Where its visible text started.
            end: Where its visible text ended.
            kind: What sort of link it is.
            href: Its `href` attribute.

        Returns:
            The link.
        """
        url = href or None
        if kind is LinkKind.MENTION:
            return TextLink(
                start=start,
                end=end,
                kind=kind,
                target=self._mention_ids_by_url.get(href, href),
                url=url,
            )
        if kind is LinkKind.TAG:
            shown = self.text[start:end]
            return TextLink(
                start=start,
                end=end,
                kind=kind,
                target=shown[1:] if shown.startswith("#") else shown,
                url=url,
            )
        return TextLink(start=start, end=end, kind=kind, target=href, url=url)

    def handle_data(self, data: str) -> None:
        """Act on a run of plain text between two tags.

        Args:
            data: The text.
        """
        self._append(data)


def _content_to_text(
    html_content: str, mentions: Sequence[RawData]
) -> tuple[str, tuple[TextLink, ...]]:
    """Convert one status's `content` HTML into plain text and its links.

    Args:
        html_content: The status's `content`, exactly as Mastodon sent it.
        mentions: The status's own `mentions` list.

    Returns:
        The plain text, and every mention, hashtag and link inside it.
    """
    parser = _ContentParser(mentions)
    parser.feed(html_content)
    parser.close()
    return parser.text, tuple(parser.links)


def _post_details_from(
    status: RawData, connection: Connection, server: str
) -> PostDetails:
    """Build a `PostDetails` out of one of Mastodon's own `Status` objects.

    Args:
        status: The `Status` object, exactly as Mastodon sent it.
        connection: The account reading it, used to say whether it is
            theirs.
        server: The server it was read from.

    Returns:
        The post, in full. `unavailable` is always `None` - Mastodon simply
        leaves a post out of a list rather than sending a placeholder for
        one we cannot see, unlike Bluesky.
    """
    account = _section(status, "account")
    author = _account_person(account, server) if account else None

    content = status.get("content")
    mentions = _dicts_from(status.get("mentions"))
    text, links = _content_to_text(
        content if isinstance(content, str) else "", mentions
    )

    visibility_word = status.get("visibility")
    visibility = (
        _VISIBILITY_FROM_WIRE.get(visibility_word)
        if isinstance(visibility_word, str)
        else None
    )

    parent_id_raw = status.get("in_reply_to_id")
    parent_id = str(parent_id_raw) if isinstance(parent_id_raw, str) else None
    status_id = str(status.get("id", ""))

    author_id = account.get("id")
    favourited = status.get("favourited")
    url = status.get("url")

    return PostDetails(
        id=status_id,
        cid=None,
        url=url if isinstance(url, str) and url else None,
        author=author,
        text=text,
        html=content if isinstance(content, str) else None,
        links=links,
        attachments=_attachments_from(status.get("media_attachments")),
        created_at=_moment(str(status.get("created_at", ""))),
        visibility=visibility,
        parent_id=parent_id,
        # A top-level post is its own root. A reply's root is only known
        # once its ancestors have been read - see `read_thread` - so it is
        # left `None` here rather than spending a request to find out.
        root_id=status_id if parent_id is None else None,
        reply_count=_number(status, "replies_count", None),
        like_count=_number(status, "favourites_count", None),
        repost_count=_number(status, "reblogs_count", None),
        quote_count=_number(status, "quotes_count", None),
        liked_by_me=favourited if isinstance(favourited, bool) else None,
        # Nothing to keep: Mastodon's favourite/unfavourite need only the
        # post's own id, unlike Bluesky's like-record uri.
        my_like_id=None,
        is_mine=author_id is not None and str(author_id) == connection.account_id,
        unavailable=None,
        raw=status,
    )


def _sort_key(item: RawData) -> datetime:
    """Return a status's time, for sorting a list of them oldest first.

    Args:
        item: The `Status` object.

    Returns:
        Its `created_at`, or the earliest possible moment if that cannot be
        read - which sorts it first rather than dropping it.
    """
    return _moment(str(item.get("created_at", ""))) or datetime.min.replace(tzinfo=UTC)


def _reply_depths(root_id: str, oldest_first: Sequence[RawData]) -> dict[str, int]:
    """Work out how many reply levels below the root each status sits.

    Args:
        root_id: The id of the post everything here replies to, directly or
            otherwise.
        oldest_first: Every descendant, oldest first - the order a reply's
            parent is expected to already have a known depth in.

    Returns:
        Each status id's depth: `1` for a direct reply to the root, `2` for
        a reply to one of those, and so on. A status whose parent's depth
        could not be worked out - a clock that disagrees, or a parent
        outside this list - gets `_FALLBACK_REPLY_DEPTH` rather than being
        silently dropped.
    """
    depths: dict[str, int] = {root_id: 0}
    remaining = list(oldest_first)

    progress = True
    while remaining and progress:
        progress = False
        still_unresolved: list[RawData] = []
        for item in remaining:
            parent = item.get("in_reply_to_id")
            parent_id = str(parent) if isinstance(parent, str) else None
            if parent_id is not None and parent_id in depths:
                depths[str(item.get("id", ""))] = depths[parent_id] + 1
                progress = True
            else:
                still_unresolved.append(item)
        remaining = still_unresolved

    for item in remaining:
        depths[str(item.get("id", ""))] = _FALLBACK_REPLY_DEPTH

    return depths


def _mention_prefix(text: str, parent: RawData, connection: Connection) -> str:
    """Build the `@acct` prefix Mastodon's own web app adds to a reply.

    The parent's author is named first, then anyone else the parent
    mentions - skipping the connected account itself, and skipping anyone
    already named in `text`.

    Args:
        text: What the person typed.
        parent: The status being replied to.
        connection: The account doing the replying.

    Returns:
        `text`, with the mentions it was missing added in front of it.
    """
    lowered_text = text.lower()
    seen: set[str] = set()
    accts: list[str] = []

    def consider(account_id: object, acct: object) -> None:
        if not isinstance(acct, str) or not acct:
            return
        if account_id is not None and str(account_id) == connection.account_id:
            return
        key = acct.lower()
        if key in seen or f"@{key}" in lowered_text:
            return
        seen.add(key)
        accts.append(acct)

    author = _section(parent, "account")
    consider(author.get("id"), author.get("acct"))
    for mention in _dicts_from(parent.get("mentions")):
        consider(mention.get("id"), mention.get("acct"))

    if not accts:
        return text
    return " ".join(f"@{acct}" for acct in accts) + " " + text


def _narrower_visibility(parent_visibility: object, requested: str) -> str:
    """Return whichever of two visibilities is narrower.

    A reply is never let out wider than the post it replies to - a reply to
    a `direct` status stays `direct`, whatever `Post.options` or an
    account's own default asks for.

    Args:
        parent_visibility: The parent status's own `visibility`.
        requested: The visibility the reply would otherwise get.

    Returns:
        Whichever of the two is narrower.
    """
    parent = parent_visibility if isinstance(parent_visibility, str) else "public"
    parent_rank = _VISIBILITY_NARROWNESS.get(parent, 3)
    requested_rank = _VISIBILITY_NARROWNESS.get(requested, 3)
    return parent if parent_rank <= requested_rank else requested


def _mentioning(accounts: Sequence[RawData], text: str) -> str:
    """Prepend an `@acct` for every account in a list.

    Used to name every participant on a fresh direct status - Mastodon has
    no idea of a conversation until a status has actually been exchanged
    between these people.

    Args:
        accounts: The `Account` objects to mention.
        text: The words to put after the mentions.

    Returns:
        `text`, with everyone in `accounts` mentioned in front of it.
    """
    accts = [
        str(account["acct"])
        for account in accounts
        if isinstance(account.get("acct"), str) and account.get("acct")
    ]
    if not accts:
        return text
    return " ".join(f"@{acct}" for acct in accts) + " " + text


def _message_from(
    status: RawData, server: str, connection: Connection, conversation_id: str
) -> Message:
    """Build a `Message` out of one of Mastodon's own `Status` objects.

    Args:
        status: The `Status` object.
        server: The server it was read from.
        connection: The account reading it, used to say whether it is
            theirs.
        conversation_id: Which conversation this belongs to - Mastodon does
            not put this on a status itself.

    Returns:
        The message. `deleted` is always `False`: Mastodon simply removes a
        deleted status from `/context` rather than leaving a marker behind.
    """
    account = _section(status, "account")
    author_id = account.get("id")
    content = status.get("content")
    text, _links = _content_to_text(
        content if isinstance(content, str) else "", _dicts_from(status.get("mentions"))
    )
    return Message(
        id=str(status.get("id", "")),
        conversation_id=conversation_id,
        sender=_account_person(account, server),
        text=text,
        sent_at=_moment(str(status.get("created_at", ""))) or datetime.now(UTC),
        is_mine=author_id is not None and str(author_id) == connection.account_id,
        deleted=False,
        attachments=_attachments_from(status.get("media_attachments")),
        raw=status,
    )


def _conversation_from(
    raw: RawData, server: str, connection: Connection
) -> Conversation:
    """Build a `Conversation` out of one of Mastodon's own `Conversation` objects.

    Args:
        raw: The `Conversation` object.
        server: The server it was read from.
        connection: The account reading it.

    Returns:
        The conversation. `can_reply_until` is always `None` - Mastodon has
        no reply window. `full_history` is always `False` - see
        `MastodonPlatform.read_messages`.
    """
    conversation_id = str(raw.get("id", ""))
    people = tuple(
        _account_person(account, server) for account in _dicts_from(raw.get("accounts"))
    )
    last_status = raw.get("last_status")
    last_message = (
        _message_from(last_status, server, connection, conversation_id)
        if isinstance(last_status, dict)
        else None
    )
    return Conversation(
        id=conversation_id,
        people=people,
        last_message=last_message,
        unread_count=1 if raw.get("unread") is True else 0,
        updated_at=last_message.sent_at if last_message is not None else None,
        can_reply_until=None,
        full_history=False,
        raw=raw,
    )


def _link_rels(headers: httpx.Headers) -> dict[str, str]:
    """Read a `Link` header into `{rel: url}`.

    Args:
        headers: The reply's headers.

    Returns:
        Every relation the header named. Empty if there is no `Link` header.
    """
    header = headers.get("link")
    if not header:
        return {}
    return {rel: url for url, rel in _LINK_ENTRY.findall(header)}


def _next_max_id(headers: httpx.Headers) -> str | None:
    """Read the `max_id` to ask for next, out of a `Link` header.

    Mastodon paginates several list endpoints this way - who favourited a
    post, an account's conversations - rather than handing back a cursor of
    its own. This is what fills `Page.next`: Mastodon's own `max_id` value on
    its own, ready to send back as `after=`. Treat it as opaque all the same;
    it is still a Mastodon implementation detail, not a promise.

    Args:
        headers: The reply's headers.

    Returns:
        The `max_id` to ask for next, or `None` if there is no further page.
    """
    next_url = _link_rels(headers).get("next")
    if next_url is None:
        return None
    max_id = httpx.QueryParams(urlparse(next_url).query).get("max_id")
    return max_id if max_id else None


def _kind_and_about(
    notification_type: str, status: RawData | None, connection_account_id: str
) -> tuple[str, str | None]:
    """Work out what word to use for one notification, and what it concerns.

    A `mention` notification whose status is `direct`-visibility always
    comes out as `message_received`, even when that same status also
    replies to one of the connected account's own posts. Direct wins over
    reply: that precedence is intentional, not an oversight, so a direct
    message never gets misread as a comment just because it happens to
    quote-reply something we posted.

    Args:
        notification_type: Mastodon's own word for the notification.
        status: The notification's `status`, when it has one.
        connection_account_id: The connected account's own id.

    Returns:
        The word for `Update.kind_name`, and `about_post_id` - the connected
        account's own post this concerns, or `None` when there is none.
    """
    if notification_type == "mention" and status is not None:
        if status.get("visibility") == "direct":
            return "message_received", None
        in_reply_to_account_id = status.get("in_reply_to_account_id")
        if (
            isinstance(in_reply_to_account_id, str)
            and in_reply_to_account_id == connection_account_id
        ):
            in_reply_to_id = status.get("in_reply_to_id")
            about = str(in_reply_to_id) if isinstance(in_reply_to_id, str) else None
            return "comment_created", about
        return "mention", None

    if notification_type in ("favourite", "reblog") and status is not None:
        status_id = status.get("id")
        about = str(status_id) if status_id is not None else None
        return _OUR_WORD_FOR.get(notification_type, notification_type), about

    return _OUR_WORD_FOR.get(notification_type, notification_type), None


def _update_from_notification(
    raw: RawData, *, server: str, connection: Connection
) -> Update | None:
    """Build an `Update` out of one of Mastodon's own `Notification` objects.

    Args:
        raw: The `Notification` object.
        server: The server it was read from.
        connection: The account it concerns.

    Returns:
        The update, or `None` if it carries no readable `created_at` - a
        notification we cannot place in time is one we cannot safely say is
        new or not, so it is left out rather than guessed at.
    """
    when = _moment(str(raw.get("created_at", "")))
    if when is None:
        return None

    notification_type = str(raw.get("type", ""))
    status = raw.get("status")
    status = status if isinstance(status, dict) else None
    account = raw.get("account")

    kind_name, about_post_id = _kind_and_about(
        notification_type, status, connection.account_id
    )

    post_id = None
    if status is not None:
        status_id = status.get("id")
        post_id = str(status_id) if status_id is not None else None

    return Update.from_network(
        update_id=str(raw.get("id", "")),
        kind_name=kind_name,
        platform=PLATFORM_NAME,
        connection_id=connection.id,
        created_at=when,
        raw=raw,
        # Mastodon does not put a conversation id on a notification, so a
        # `message_received` update always leaves this `None` - filling it
        # in would need a request of its own.
        actor=_account_person(account, server) if isinstance(account, dict) else None,
        post_id=post_id,
        about_post_id=about_post_id,
    )


def _is_after_marker(candidate_id: str, marker: str) -> bool:
    """Say whether one notification id is newer than a marker.

    Args:
        candidate_id: The id to check.
        marker: The marker to check it against.

    Returns:
        True if `candidate_id` is newer. Mastodon's ids are meant to sort
        the same as text or as numbers, but comparing as numbers is what
        actually matters here, and text comparison silently gets it wrong
        once the two ids have a different number of digits - so numbers are
        tried first, falling back to text only if one is not a number.
    """
    try:
        return int(candidate_id) > int(marker)
    except ValueError:
        return candidate_id > marker


class MastodonPlatform:
    """Everything socialchimp does with Mastodon.

    Registering an app on each server it meets, signing people in,
    publishing, reading what has happened since, and reading how a post that
    went out is doing.

        mastodon = MastodonPlatform()

        app = await mastodon.create_app(
            name="My App",
            redirect_uri="https://myapp.example/callback",
            host="mastodon.social",
        )

    It holds nothing between calls except what one server said its limits
    were. Credentials arrive on the `LoginRequest`, and anything a sign-in
    needs a second time travels through your app. So one of these can be
    shared by your whole process, and two of them behave the same as one.

    Attributes:
        name: `"mastodon"`.
        features: What Mastodon can do. Notably it cannot push updates to a
            single account yet, so `Feature.PUSH_UPDATES` and
            `Feature.SUBSCRIBE_UPDATES` are missing and socialchimp checks on
            a timer instead - Web Push arrives in a later release.
    """

    name: str = PLATFORM_NAME

    features: Feature = (
        Feature.CREATE_APP
        | Feature.POST_TEXT
        | Feature.POST_IMAGE
        | Feature.POST_VIDEO
        | Feature.SCHEDULE
        | Feature.REPLY
        | Feature.DELETE_POST
        | Feature.READ_POSTS
        | Feature.READ_STATS
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
        website: str | None = None,
        timeout: float = 30.0,
        retries: Retries | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        limits_cache_seconds: float = 300.0,
        media_checks: int = 30,
        media_wait_seconds: float = 1.0,
        updates_per_check: int = 40,
    ) -> None:
        """Set Mastodon up for one app.

        Args:
            website: Your app's home page, shown to people on the approval
                page. Left out, none is sent.
            timeout: Seconds to wait for a server to answer.
            retries: How many times to try again after a hiccup. Left out,
                the shared default is used.
            transport: Where requests actually go. Leave it out for ordinary
                calls; pass your own to send them somewhere else.
            limits_cache_seconds: How long to trust what a server said about
                its own limits before asking again.
            media_checks: How many times to ask whether a video has finished
                being processed before giving up.
            media_wait_seconds: How long to wait between those checks.
            updates_per_check: How many notifications to read at a time.
                Mastodon allows up to 80.
        """
        self._website = website
        self._timeout = timeout
        self._retries = retries
        self._transport = transport
        self._limits_cache_seconds = limits_cache_seconds
        self._media_checks = media_checks
        self._media_wait_seconds = media_wait_seconds
        self._updates_per_check = updates_per_check

        # What each server last said it allows, and when to stop believing
        # it. Keyed by server, because two servers rarely agree. This is the
        # only thing kept between calls, and losing it costs one request.
        self._known_limits: dict[str, tuple[float, Limits]] = {}

    def _client(self, host: str, token: str | None = None) -> HttpClient:
        """Make a client pointed at one server.

        Args:
            host: The server to talk to.
            token: The account's token, for anything that needs one.

        Returns:
            A client. Use it in an `async with` block so it closes itself.
        """
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        return HttpClient(
            f"https://{host}",
            platform=PLATFORM_NAME,
            headers=headers,
            timeout=self._timeout,
            transport=self._transport,
            retries=self._retries,
            errors=mastodon_errors,
        )

    def api_base(self, connection: Connection) -> str:
        """Return the address of the server this account is on.

        Mastodon is thousands of separate servers, so there is no one
        address to write down here. The account says which server it is on,
        and that is where its requests go.

        Args:
            connection: The account we are about to act as.

        Returns:
            The server's address, such as `"https://mastodon.social"`.

        Raises:
            ConfigError: If the connection was saved without a server on it.
        """
        return f"https://{_host_of(connection)}"

    def auth_headers(self, connection: Connection) -> Mapping[str, str]:
        """Return the header that proves we may act as this account.

        Args:
            connection: The account we are acting as.

        Returns:
            Mastodon's `Authorization` header. Its tokens do not expire, so
            the one on the connection is always the right one.
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
        """Register your app on one Mastodon server.

        No portal, no review, no waiting. The server answers with the two
        values you need and you can sign somebody in straight away.

        Save what comes back and put it on the `LoginRequest` for every
        login on this server - `SocialChimp` does that for you. Registering
        again would work, but it leaves an unused record on somebody else's
        server, so it is worth saving.

        Args:
            name: What people see on the approval page.
            redirect_uri: Where Mastodon sends people back to. It has to
                match exactly at login time.
            host: Which server to register on. Required - `mastodon.social`
                and `fosstodon.org` are different networks.
            scopes: Permissions to ask for. Left out, `read write`.

        Returns:
            The credentials for this server. Save them.

        Raises:
            ConfigError: If no server was named.
            PlatformError: If the server answered without credentials.
        """
        server = _clean_host(host)
        form: dict[str, Any] = {
            "client_name": name,
            "redirect_uris": redirect_uri,
            "scopes": " ".join(scopes or DEFAULT_SCOPES),
        }
        if self._website is not None:
            form["website"] = self._website

        async with self._client(server) as http:
            reply = await http.json("POST", "/api/v1/apps", data=form)

        return AppCredentials(
            platform=PLATFORM_NAME,
            # Stamped with the server it works on, because that is half of
            # what makes it findable again.
            host=server,
            client_id=_text(reply, "client_id", "register an app"),
            client_secret=_text(reply, "client_secret", "register an app"),
        )

    async def start_login(self, request: LoginRequest) -> SendToNetwork:
        """Build the address to send somebody to so they can approve your app.

        Nothing is sent to Mastodon here. The address carries the hash of a
        secret; the secret itself comes back to you in `remember`, and is
        sent later, in `finish_login`, to prove the code came back to the
        same place that asked for it.

        Keep `remember` with that person's session and hand it back. Nothing
        is held here between the two calls, because the person may be sent
        away by one web worker and come back to another.

        Args:
            request: Where to send them back to, which server, what to ask
                for, and your app's credentials for that server.

        Returns:
            Always a `SendToNetwork`: Mastodon has an approval page, so
            there is somewhere to send people. It carries the address to
            redirect their browser to, the state value that will come back
            with them, and the secret to hand back.

        Raises:
            ConfigError: If no server was named, or the request carries no
                credentials for it.
        """
        server = _clean_host(request.host)
        app = _app_on(request, server)

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
            }
        )
        return SendToNetwork(
            url=f"https://{server}/oauth/authorize?{query}",
            state=state,
            remember={"code_verifier": verifier},
        )

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> Finished:
        """Swap the code Mastodon sent back for a token, and build a connection.

        Hand this the whole query string Mastodon put on your redirect
        address, as a dictionary, along with the `remember` value
        `start_login` gave you.

        Args:
            request: The same request used to start the login.
            callback: The query values Mastodon sent back. It must have
                `code`; `state` is checked when it is there.
            remember: What `start_login` put in `SendToNetwork.remember`.

        Returns:
            The finished connection. Save it.

        Raises:
            AuthError: If the person said no, if there is no code, if the
                state that came back is not the one we sent, or if the secret
                from `start_login` did not come back.
            ConfigError: If no server was named, or the request carries no
                credentials for it.
            PlatformError: If Mastodon answered without a token.
        """
        server = _clean_host(request.host)
        app = _app_on(request, server)
        _check_state(request, callback)
        code = _code_from(callback)
        verifier = _verifier_from(remember)

        asked_for = request.scopes or DEFAULT_SCOPES
        form: dict[str, Any] = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": app.client_id,
            "client_secret": app.client_secret,
            "redirect_uri": request.redirect_uri,
            "scope": " ".join(asked_for),
            # The other half of the pair from `start_login`. Mastodon hashes
            # it and checks the result against what it was sent earlier.
            "code_verifier": verifier,
        }

        async with self._client(server) as http:
            reply = await http.json("POST", "/oauth/token", data=form)
            access_token = _text(reply, "access_token", "sign someone in")
            me = await http.json(
                "GET",
                "/api/v1/accounts/verify_credentials",
                headers={"Authorization": f"Bearer {access_token}"},
            )

        # A server may grant less than we asked for, and it says so here.
        granted = reply.get("scope")
        given = granted.split() if isinstance(granted, str) and granted else []
        scopes = tuple(given) if given else asked_for
        account_id = _text(me, "id", "say who just signed in")
        handle = _text(me, "acct", "say who just signed in")

        return Finished(
            connection=Connection(
                # A name that cannot collide with the same person's account
                # on another server. Rename it if your app prefers its own.
                id=f"{PLATFORM_NAME}:{server}:{account_id}",
                platform=PLATFORM_NAME,
                host=server,
                account_id=account_id,
                # verify_credentials always answers about a local account, so
                # `acct` is the bare username and the server has to be added.
                account_name=f"@{handle}@{server}",
                token=Token(access_token=access_token),
                scopes=scopes,
                extra={"profile_url": me.get("url")},
            )
        )

    async def refresh(
        self,
        connection: Connection,
        app: AppCredentials | None = None,
    ) -> Token:
        """Hand back the token that is already there.

        Mastodon access tokens do not expire. They work until the person
        revokes them, and there is no refresh token because there is nothing
        to refresh. So this calls nothing and changes nothing.

        Args:
            connection: The account socialchimp was about to renew.
            app: Your app's credentials for this server. Taken and ignored:
                Google and Meta need them to renew a token, Mastodon has no
                renewal to sign in the first place.

        Returns:
            The token the connection already holds.
        """
        return connection.token

    async def limits(self, connection: Connection) -> Limits:
        """Ask a server what it currently allows.

        Worth asking rather than assuming. Mastodon's own default is 500
        characters, but whoever runs a server can change it, and plenty run
        at 5,000. The answer is kept for a few minutes so a burst of posts
        does not ask again for every one.

        Args:
            connection: The account whose server to ask.

        Returns:
            What that server allows right now.

        Raises:
            ConfigError: If the connection has no server on it.
        """
        server = _host_of(connection)

        remembered = self._known_limits.get(server)
        now = time.monotonic()
        if remembered is not None and remembered[0] > now:
            return remembered[1]

        async with self._client(server, connection.token.access_token) as http:
            reply = await http.json("GET", "/api/v2/instance")

        found = _limits_from_instance(reply)
        self._known_limits[server] = (now + self._limits_cache_seconds, found)
        return found

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        """Publish a post.

        Files are uploaded first, one at a time. A video usually comes back
        as "still being processed", and this waits for it, because a post
        that names a file the server has not finished with is refused.

        Args:
            connection: The account to publish as.
            post: What to publish.

        Returns:
            What Mastodon said about the new post. A scheduled post comes
            back as `PostState.SCHEDULED` - Mastodon has taken it, and it
            goes live later.

        Raises:
            ConfigError: If the connection has no server on it.
            InvalidPostError: If a setting is unknown, if the post breaks one
                of the server's limits, or if Mastodon refuses it.
            PlatformError: If a video never finishes processing.
        """
        server = _host_of(connection)
        # Settings are checked before anything is sent, so a typo costs no
        # request and no part of the account's allowance.
        options = _checked_options(post.options)

        allowed = await self.limits(connection)
        check_post(
            post,
            platform=PLATFORM_NAME,
            features=self.features,
            limits=allowed,
        )

        async with self._client(server, connection.token.access_token) as http:
            media_ids = [await self._upload(http, item) for item in post.media]

            form: dict[str, Any] = {"status": post.text, **options}
            if media_ids:
                form["media_ids[]"] = media_ids
            if post.reply_to is not None:
                form["in_reply_to_id"] = post.reply_to
            if post.publish_at is not None:
                form["scheduled_at"] = post.publish_at.isoformat()

            reply = await http.json(
                "POST",
                "/api/v1/statuses",
                data=form,
                headers={"Idempotency-Key": post_fingerprint(post)},
            )

        post_id = _text(reply, "id", "publish a post")
        if post.publish_at is not None:
            # A scheduled post has no address yet, because it does not exist
            # yet. Mastodon answers with a plan, not a post.
            return PostResult(
                id=post_id,
                url=None,
                state=PostState.SCHEDULED,
                raw=reply,
            )

        url = reply.get("url")
        return PostResult(
            id=post_id,
            url=url if isinstance(url, str) else None,
            state=PostState.DONE,
            raw=reply,
        )

    async def _upload(self, http: HttpClient, item: Media) -> str:
        """Send one file to a server and wait until it is usable.

        Args:
            http: A client already pointed at the right server.
            item: The picture or video to send.

        Returns:
            The server's id for the file, to name in the post.

        Raises:
            InvalidPostError: If all we have is a link to the file.
            PlatformError: If the server never finishes processing it.
        """
        if item.content is None and item.path is None:
            message = (
                f"Mastodon will not fetch {item.url!r} for you - it only "
                f"takes files sent to it. Download the file first, then use "
                f"Media.from_bytes or Media.from_file."
            )
            raise InvalidPostError(message)

        files = {"file": (item.filename or "upload", item.read(), item.content_type)}
        described = {"description": item.alt_text} if item.alt_text else {}

        response = await http.post("/api/v2/media", files=files, data=described)
        reply = read_body(response)
        media_id = _text(reply, "id", "take a file")

        # A picture is usually ready at once (200). Video and audio come back
        # as "accepted, still working on it" (202).
        if response.status_code == httpx.codes.ACCEPTED:
            await self._wait_until_ready(http, media_id)
        return media_id

    async def _wait_until_ready(self, http: HttpClient, media_id: str) -> None:
        """Keep asking about a file until the server has finished with it.

        Args:
            http: A client already pointed at the right server.
            media_id: The file to ask about.

        Raises:
            PlatformError: If it is still not ready after all our checks. The
                file is not lost - it is on the account, and posting it again
                later will work.
        """
        for _ in range(self._media_checks):
            await _wait(self._media_wait_seconds)
            # 200 means the file is ready. **206** means it is still being
            # processed - not 202. The upload answers 202 the first time, but
            # every later check answers 206, and the docs put those two codes
            # on separate pages, which is easy to misread.
            response = await http.get(f"/api/v1/media/{media_id}")
            if response.status_code == httpx.codes.OK:
                return

        message = (
            f"Mastodon is still working on file {media_id} after "
            f"{self._media_checks} checks. Big videos take longer than this; "
            f"raise media_checks or media_wait_seconds and try again."
        )
        raise PlatformError(message, platform=PLATFORM_NAME)

    async def delete_post(self, connection: Connection, post_id: str) -> None:
        """Remove a post.

        Args:
            connection: The account that published it.
            post_id: Mastodon's id for the post.

        Raises:
            ConfigError: If the connection has no server on it.
            NotFoundError: If there is no such post on this account.
        """
        server = _host_of(connection)
        async with self._client(server, connection.token.access_token) as http:
            await http.delete(f"/api/v1/statuses/{post_id}")

    async def read_stats(self, connection: Connection, post_id: str) -> PostStats:
        """Read how a published post is doing.

        Mastodon keeps three numbers about a status - replies, favourites
        and boosts - and hands all three back on the status itself, so this
        is one request rather than one per number.

        There is no reach, no impressions and no click count anywhere in
        Mastodon's API. Those are missing here rather than reported as zero,
        because a zero somebody can chart is worse than a gap they can see.

        A server on an older version, or a fork, can leave a count out. That
        number comes back as `None`, which is not the same as `0`.

        Args:
            connection: The account that published it.
            post_id: Mastodon's id for the post, which is what `publish`
                handed back.

        Returns:
            Its replies, favourites and boosts, under socialchimp's own
            names for them: `comments`, `likes` and `shares`.

        Raises:
            ConfigError: If the connection has no server on it.
            NotFoundError: If there is no such post. Mastodon answers the
                same way for a post that was deleted and one that never
                existed, so this is also what a post taken down looks like.
            AuthError: If the token has been revoked. Mastodon's tokens do
                not expire on their own, so this means somebody revoked it
                and the person has to connect their account again.
            RateLimitError: If the server is asking us to slow down.
            PlatformError: If the reply arrives without an id.
        """
        server = _host_of(connection)
        async with self._client(server, connection.token.access_token) as http:
            reply = await http.json("GET", f"/api/v1/statuses/{post_id}")

        return PostStats(
            id=_text(reply, "id", "read a post's numbers"),
            likes=_number(reply, "favourites_count", None),
            comments=_number(reply, "replies_count", None),
            shares=_number(reply, "reblogs_count", None),
            raw=reply,
        )

    # Mastodon can also hold a socket open and tell us the moment something
    # happens (`/api/v1/streaming`). That would go alongside this method, as
    # a `CanCheckSignature`-style listener. Checking on a timer comes first
    # because it needs nothing kept running, survives a restart with no lost
    # updates, and works the same on every server - a socket that drops
    # silently loses updates until somebody notices.
    async def fetch_updates(
        self,
        connection: Connection,
        since: datetime | None,
    ) -> Sequence[Update]:
        """Return what has happened on this account since a moment in time.

        Mastodon pages its notifications by id rather than by time, and ids
        are not comparable across servers. So we read a recent page and drop
        anything older than the marker. Check often enough that a page covers
        the gap - the default of 40 is plenty for most accounts.

        A reply comes back as `COMMENT_CREATED`, a direct-visibility mention
        as `MESSAGE_RECEIVED`, and anything else Mastodon calls a mention
        stays `MENTION` - see `_kind_and_about`.

        Args:
            connection: The account to ask about.
            since: Only return things newer than this. `None` on the first
                call.

        Returns:
            The updates, oldest first.

        Raises:
            ConfigError: If the connection has no server on it.
        """
        server = _host_of(connection)
        params = [("types[]", word) for word in WATCHED_NOTIFICATIONS]
        params.append(("limit", str(self._updates_per_check)))

        async with self._client(server, connection.token.access_token) as http:
            response = await http.get("/api/v1/notifications", params=params)

        items = _dicts_from(read_body(response).get("body"))

        updates: list[Update] = []
        for raw in items:
            update = _update_from_notification(
                raw, server=server, connection=connection
            )
            if update is None or (since is not None and update.created_at <= since):
                continue
            updates.append(update)

        # Mastodon hands back the newest first; socialchimp wants the oldest.
        updates.reverse()
        return updates

    async def fetch_updates_after(
        self,
        connection: Connection,
        marker: str | None,
        *,
        limit: int | None = None,
    ) -> UpdateBatch:
        """Read what is new since a marker.

        One request: `GET /api/v1/notifications`, with `min_id=marker` once
        there is one. Mastodon does not promise that a single page holds
        every new notification - a very busy account can have more waiting
        than `limit` allows - so `more` is `True` whenever a full page came
        back, on the basis that there could be more behind it.

        Args:
            connection: The account to ask about.
            marker: The marker from the last call's `UpdateBatch.marker`.
                `None` on the first call, which reads the latest page.
            limit: A cap on how many updates come back in this page. `None`
                uses `updates_per_check`.

        Returns:
            The new updates, oldest first, and a marker to store for next
            time.

        Raises:
            ConfigError: If the connection has no server on it.
        """
        server = _host_of(connection)
        page_limit = limit if limit is not None else self._updates_per_check
        params = [("types[]", word) for word in WATCHED_NOTIFICATIONS]
        params.append(("limit", str(page_limit)))
        if marker is not None:
            params.append(("min_id", marker))

        async with self._client(server, connection.token.access_token) as http:
            response = await http.get("/api/v1/notifications", params=params)

        items = _dicts_from(read_body(response).get("body"))

        updates: list[Update] = []
        for raw in items:
            update = _update_from_notification(
                raw, server=server, connection=connection
            )
            if update is None:
                continue
            if marker is not None and not _is_after_marker(update.id, marker):
                continue
            updates.append(update)

        updates.sort(key=lambda found: found.created_at)

        new_marker = updates[-1].id if updates else marker
        more = page_limit > 0 and len(items) >= page_limit

        return UpdateBatch(updates=tuple(updates), marker=new_marker, more=more)

    async def mark_seen(self, connection: Connection, marker: str) -> None:
        """Tell Mastodon a marker from `fetch_updates_after` has been seen.

        One request, `POST /api/v1/markers`, tried twice at most: Mastodon
        guards this marker with an optimistic-locking version number, and
        answers 409 if something else moved it between our last read and
        this write. One retry picks up whatever changed and tries again; a
        second conflict is let through rather than retried forever.

        Args:
            connection: The account to mark it for.
            marker: The marker that has been handled.

        Raises:
            ConfigError: If the connection has no server on it.
        """
        server = _host_of(connection)
        form = {"notifications[last_read_id]": marker}
        async with self._client(server, connection.token.access_token) as http:
            try:
                await http.post("/api/v1/markers", data=form)
            except PlatformError as failure:
                if failure.status_code != httpx.codes.CONFLICT:
                    raise
                await http.post("/api/v1/markers", data=form)

    async def read_post(self, connection: Connection, post_id: str) -> PostDetails:
        """Read one post back in full, with everything socialchimp models about it.

        One request: `GET /api/v1/statuses/:id`.

        Mastodon's own HTML is turned into plain text here: paragraphs
        become blank lines, `<br>` becomes a line break, and the parts of a
        long link nobody reads - the `https://` at the front, the address
        past what is actually shown - are left out. The original HTML stays
        on `PostDetails.html`, untouched and untrusted.

        `root_id` is only filled in here when this post has no parent -
        working one out for a reply needs `read_thread`'s extra request,
        which this does not spend.

        Args:
            connection: The account to read it as.
            post_id: Mastodon's id for the status.

        Returns:
            The post, in full.

        Raises:
            ConfigError: If the connection has no server on it.
            PostGoneError: If the post was deleted, or never existed.
        """
        server = _host_of(connection)
        async with self._client(server, connection.token.access_token) as http:
            reply = await http.json("GET", f"/api/v1/statuses/{post_id}")
        return _post_details_from(reply, connection, server)

    async def read_thread(
        self,
        connection: Connection,
        post_id: str,
        *,
        depth: int | None = None,
        limit: int | None = None,
    ) -> Thread:
        """Read a post together with the replies underneath it.

        Two requests: `GET /api/v1/statuses/:id` and its `/context`.
        Mastodon has no pagination here and takes no `depth` of its own - it
        hands back every reply it holds, up to 4096, and `depth` and `limit`
        are both applied here, to what came back, rather than sent to it.

        Args:
            connection: The account to read it as.
            post_id: Mastodon's id for the status to read.
            depth: How many reply levels below `post_id` to keep. `None`
                keeps all of them.
            limit: A cap on how many replies to keep, oldest first.

        Returns:
            The post and its replies. `Thread.complete` is `False` if
            `depth` or `limit` cut anything off, or if Mastodon's own
            `Mastodon-Async-Refresh` header says remote replies are still
            arriving.

        Raises:
            ConfigError: If the connection has no server on it.
            PostGoneError: If the post was deleted, or never existed.
        """
        server = _host_of(connection)
        async with self._client(server, connection.token.access_token) as http:
            status_reply = await http.json("GET", f"/api/v1/statuses/{post_id}")
            context_response = await http.get(f"/api/v1/statuses/{post_id}/context")

        context = read_body(context_response)
        ancestors = _dicts_from(context.get("ancestors"))
        descendants = _dicts_from(context.get("descendants"))

        root_id = (
            str(ancestors[0]["id"]) if ancestors else str(status_reply.get("id", ""))
        )
        post = replace(
            _post_details_from(status_reply, connection, server), root_id=root_id
        )

        oldest_first = sorted(descendants, key=_sort_key)
        depths = _reply_depths(str(status_reply.get("id", "")), oldest_first)

        kept = [
            item
            for item in oldest_first
            if depth is None or depths.get(str(item.get("id", "")), 1) <= depth
        ]
        depth_cut = len(kept) < len(oldest_first)

        limit_cut = limit is not None and len(kept) > limit
        if limit is not None:
            kept = kept[:limit]

        async_refresh = "mastodon-async-refresh" in context_response.headers

        replies = tuple(
            replace(_post_details_from(item, connection, server), root_id=root_id)
            for item in kept
        )

        return Thread(
            post=post,
            replies=replies,
            complete=not (depth_cut or limit_cut or async_refresh),
            raw=context,
        )

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

        One request to read the parent, then whatever `publish` costs for
        the reply itself - a server's limits are looked up at most once, one
        request goes out per file attached, and one more sends the post.

        A visibility passed in `options` is narrowed to the parent's - a
        reply to a `direct` status stays `direct`, whatever `options` asks
        for. Ask for nothing, and this sends no visibility at all, so the
        account's own default applies - except when the parent is `private`
        or `direct`, where a reply that carried no visibility of its own
        would otherwise go out wider than the post it replies to; there, the
        parent's own visibility is sent instead. Either way, this also names
        the parent's author and anyone else it mentions, the way Mastodon's
        own web app does, skipping the connected account itself and anyone
        already named in `text`.

        Args:
            connection: The account to reply as.
            post_id: The post or comment being replied to, at any depth.
            text: The reply's words.
            media: Pictures or videos to attach to the reply.
            options: The same settings `publish` takes on `Post.options`.

        Returns:
            What Mastodon said about the new reply.

        Raises:
            ConfigError: If the connection has no server on it.
            PostGoneError: If the post being replied to is gone.
            InvalidPostError: If a setting is unknown, or the reply breaks
                one of the server's limits.
        """
        server = _host_of(connection)
        async with self._client(server, connection.token.access_token) as http:
            parent = await http.json("GET", f"/api/v1/statuses/{post_id}")

        requested = (options or {}).get("visibility")
        parent_visibility = parent.get("visibility")

        final_options = dict(options) if options else {}
        if isinstance(requested, str):
            # A visibility was asked for - keep it, unless the parent is
            # narrower.
            final_options["visibility"] = _narrower_visibility(
                parent_visibility, requested
            )
        elif parent_visibility in ("private", "direct"):
            # Nothing was asked for, but sending no visibility here would
            # let the server's default widen the reply past a parent it was
            # never meant to be seen beyond.
            final_options["visibility"] = parent_visibility
        else:
            # Nothing was asked for and the parent is public or unlisted -
            # send no visibility, so the account's own default applies.
            final_options.pop("visibility", None)

        return await self.publish(
            connection,
            Post(
                text=_mention_prefix(text, parent, connection),
                media=media,
                reply_to=post_id,
                options=final_options,
            ),
        )

    async def like(self, connection: Connection, post_id: str) -> LikeResult:
        """Like a post or a comment.

        One request. Mastodon's own favourite is already idempotent, so
        liking something already liked succeeds and changes nothing -
        there is nothing here worth keeping either way, so
        `LikeResult.like_id` is always `None`.

        Args:
            connection: The account doing the liking.
            post_id: The post or comment to like.

        Returns:
            What Mastodon said about the like.

        Raises:
            ConfigError: If the connection has no server on it.
            PostGoneError: If the post or comment is gone.
        """
        server = _host_of(connection)
        async with self._client(server, connection.token.access_token) as http:
            reply = await http.json("POST", f"/api/v1/statuses/{post_id}/favourite")
        return LikeResult(post_id=post_id, like_id=None, raw=reply)

    async def unlike(
        self,
        connection: Connection,
        post_id: str,
        *,
        like_id: str | None = None,
    ) -> None:
        """Take back a like on a post or a comment.

        One request. `like_id` is taken and ignored: Mastodon keeps nothing
        of the kind, and unfavouriting something not liked already succeeds
        and does nothing.

        Args:
            connection: The account taking the like back.
            post_id: The post or comment to unlike.
            like_id: Ignored. Kept so this matches `CanLike` - Bluesky needs
                it to skip a lookup; Mastodon has nothing to look up.

        Raises:
            ConfigError: If the connection has no server on it.
            PostGoneError: If the post or comment is gone.
        """
        server = _host_of(connection)
        async with self._client(server, connection.token.access_token) as http:
            await http.post(f"/api/v1/statuses/{post_id}/unfavourite")

    async def read_likes(
        self,
        connection: Connection,
        post_id: str,
        *,
        after: str | None = None,
        limit: int | None = None,
    ) -> Page[Like]:
        """List who liked a post.

        One request: `GET /api/v1/statuses/:id/favourited_by`. Mastodon does
        not say when a like happened, so `Like.liked_at` is always `None`.

        Args:
            connection: The account to ask as.
            post_id: The post or comment to list likes for.
            after: A `Page.next` from a previous call - Mastodon's own
                `max_id` for this list, read out of its `Link` header. Treat
                it as opaque all the same.
            limit: A cap on how many come back. Mastodon allows at most 80;
                asking for more is quietly capped rather than refused.

        Returns:
            One page of likes.

        Raises:
            ConfigError: If the connection has no server on it.
            PostGoneError: If the post or comment is gone.
        """
        server = _host_of(connection)
        params: dict[str, str] = {}
        if after is not None:
            params["max_id"] = after
        if limit is not None:
            params["limit"] = str(min(limit, _MAX_FAVOURITED_BY))

        async with self._client(server, connection.token.access_token) as http:
            response = await http.get(
                f"/api/v1/statuses/{post_id}/favourited_by", params=params
            )

        accounts = _dicts_from(read_body(response).get("body"))
        likes = tuple(
            Like(person=_account_person(account, server), liked_at=None, raw=account)
            for account in accounts
        )
        return Page(items=likes, next=_next_max_id(response.headers))

    async def _find_conversation(
        self, http: HttpClient, conversation_id: str
    ) -> RawData | None:
        """Look up one conversation by id.

        Mastodon has no `GET /api/v1/conversations/:id`, only the list, so
        this pages through `GET /api/v1/conversations` with `max_id` until
        it finds the one asked for or runs out of pages - one request per
        page along the way, which only matters on an account with a long
        history of conversations nobody has read in a while.

        Args:
            http: A client already pointed at the right server.
            conversation_id: Which conversation to find.

        Returns:
            The conversation, exactly as Mastodon sent it, or `None` if
            there is no such conversation.
        """
        after: str | None = None
        while True:
            params = {"max_id": after} if after is not None else {}
            response = await http.get("/api/v1/conversations", params=params)
            items = _dicts_from(read_body(response).get("body"))
            for raw in items:
                if str(raw.get("id", "")) == conversation_id:
                    return raw
            after = _next_max_id(response.headers)
            if after is None or not items:
                return None

    async def read_conversations(
        self,
        connection: Connection,
        *,
        after: str | None = None,
        limit: int | None = None,
    ) -> Page[Conversation]:
        """List this account's conversations.

        One request: `GET /api/v1/conversations`.

        Args:
            connection: The account to ask as.
            after: A `Page.next` from a previous call - Mastodon's own
                `max_id`, read out of its `Link` header.
            limit: A cap on how many come back. Mastodon allows at most 40;
                asking for more is quietly capped rather than refused.

        Returns:
            One page of conversations.

        Raises:
            ConfigError: If the connection has no server on it.
        """
        server = _host_of(connection)
        params: dict[str, str] = {}
        if after is not None:
            params["max_id"] = after
        if limit is not None:
            params["limit"] = str(min(limit, _MAX_CONVERSATIONS))

        async with self._client(server, connection.token.access_token) as http:
            response = await http.get("/api/v1/conversations", params=params)

        items = _dicts_from(read_body(response).get("body"))
        conversations = tuple(
            _conversation_from(raw, server, connection) for raw in items
        )
        return Page(items=conversations, next=_next_max_id(response.headers))

    async def read_messages(
        self,
        connection: Connection,
        conversation_id: str,
        *,
        after: str | None = None,
        limit: int | None = None,
    ) -> Page[Message]:
        """Read the messages in one conversation, newest first.

        Mastodon has no "every message in this conversation" call. This
        finds the conversation (see `_find_conversation` - at least one
        request, more on an account with a long history) and reads the
        `/context` of its `last_status` (one more request), keeping only the
        `direct`-visibility statuses in that thread. That is why
        `Conversation.full_history` is `False`, and why this never returns a
        `next` - there is nothing further back than this one call already
        reached.

        Args:
            connection: The account to ask as.
            conversation_id: Which conversation to read.
            after: The id of a message already read. It, and everything
                before it, is dropped from what comes back.
            limit: A cap on how many come back.

        Returns:
            One page of messages, newest first. Empty, with no error, if
            there is no such conversation any more - it may have been
            deleted between listing it and reading it.

        Raises:
            ConfigError: If the connection has no server on it.
        """
        server = _host_of(connection)
        async with self._client(server, connection.token.access_token) as http:
            conversation = await self._find_conversation(http, conversation_id)
            last_status = (
                conversation.get("last_status") if conversation is not None else None
            )
            last_status = last_status if isinstance(last_status, dict) else None
            if last_status is None:
                return Page(items=(), next=None)

            context_response = await http.get(
                f"/api/v1/statuses/{last_status.get('id', '')}/context"
            )

        context = read_body(context_response)
        everything = [
            *_dicts_from(context.get("ancestors")),
            last_status,
            *_dicts_from(context.get("descendants")),
        ]
        direct_only = [
            item for item in everything if item.get("visibility") == "direct"
        ]
        direct_only.sort(key=_sort_key, reverse=True)

        if after is not None:
            ids = [str(item.get("id", "")) for item in direct_only]
            if after in ids:
                direct_only = direct_only[ids.index(after) + 1 :]

        if limit is not None:
            direct_only = direct_only[:limit]

        messages = tuple(
            _message_from(item, server, connection, conversation_id)
            for item in direct_only
        )
        return Page(items=messages, next=None)

    async def send_message(
        self,
        connection: Connection,
        conversation_id: str,
        text: str,
        *,
        options: RawData | None = None,
    ) -> Message:
        """Send a message into an existing conversation.

        At least two requests: finding the conversation (see
        `_find_conversation`) and posting the reply.

        Args:
            connection: The account to send as.
            conversation_id: Which conversation to send into.
            text: The message's words.
            options: Ignored. Kept so this matches `CanMessage` - Mastodon
                has nothing like Meta's message tags.

        Returns:
            The message that was sent.

        Raises:
            ConfigError: If the connection has no server on it.
            NotFoundError: If there is no such conversation.
        """
        server = _host_of(connection)
        async with self._client(server, connection.token.access_token) as http:
            conversation = await self._find_conversation(http, conversation_id)
            if conversation is None:
                message = (
                    f"There is no conversation {conversation_id!r} to send "
                    f"a message into. It may have been deleted."
                )
                raise NotFoundError(message, platform=PLATFORM_NAME)

            full_text = _mentioning(_dicts_from(conversation.get("accounts")), text)

            form: dict[str, Any] = {"status": full_text, "visibility": "direct"}
            last_status = conversation.get("last_status")
            if isinstance(last_status, dict):
                form["in_reply_to_id"] = last_status.get("id")

            reply = await http.json("POST", "/api/v1/statuses", data=form)

        return _message_from(reply, server, connection, conversation_id)

    async def mark_read(self, connection: Connection, conversation_id: str) -> None:
        """Mark a conversation as read.

        One request: `POST /api/v1/conversations/:id/read`.

        Args:
            connection: The account to mark it for.
            conversation_id: Which conversation to mark.

        Raises:
            ConfigError: If the connection has no server on it.
        """
        server = _host_of(connection)
        async with self._client(server, connection.token.access_token) as http:
            await http.post(f"/api/v1/conversations/{conversation_id}/read")

    async def _find_conversation_for_status(
        self, http: HttpClient, status_id: str
    ) -> str | None:
        """Look for the conversation whose most recent message is this status.

        Only the most recent page is read - see `start_conversation`, which
        is the only caller.

        Args:
            http: A client already pointed at the right server.
            status_id: The status to look for.

        Returns:
            That conversation's id, or `None` if it is not on the first
            page.
        """
        response = await http.get("/api/v1/conversations")
        for raw in _dicts_from(read_body(response).get("body")):
            last_status = raw.get("last_status")
            if (
                isinstance(last_status, dict)
                and str(last_status.get("id", "")) == status_id
            ):
                return str(raw.get("id", ""))
        return None

    async def start_conversation(
        self,
        connection: Connection,
        person_ids: Sequence[str],
        text: str,
    ) -> Message:
        """Start a conversation with one or more people.

        One request per person - Mastodon's own account ids have to be
        turned into `acct`s before they can be named in a status - plus one
        to post the status, plus one more to find the conversation Mastodon
        just made for it, so the returned `Message.conversation_id` is
        right. That last lookup only reads the most recent page: the
        conversation just created is almost always on it, and on the rare
        occasion it is not - a burst of other direct messages arriving in
        the same instant - this falls back to using the new status's own id.

        Args:
            connection: The account to send as.
            person_ids: Mastodon account ids to start the conversation with.
            text: The first message's words.

        Returns:
            The message that was sent.

        Raises:
            ConfigError: If the connection has no server on it.
        """
        server = _host_of(connection)
        async with self._client(server, connection.token.access_token) as http:
            accounts = [
                await http.json("GET", f"/api/v1/accounts/{person_id}")
                for person_id in person_ids
            ]
            reply = await http.json(
                "POST",
                "/api/v1/statuses",
                data={
                    "status": _mentioning(accounts, text),
                    "visibility": "direct",
                },
            )
            found_id = await self._find_conversation_for_status(
                http, str(reply.get("id", ""))
            )

        conversation_id = found_id if found_id is not None else str(reply.get("id", ""))
        return _message_from(reply, server, connection, conversation_id)


def _app_on(request: LoginRequest, host: str) -> AppCredentials:
    """Read your app's credentials off a login request.

    Args:
        request: The request being started or finished.
        host: The server they have to be for, used in the message.

    Returns:
        The credentials.

    Raises:
        ConfigError: If the request carries none, saying what to call.
    """
    if request.app is None:
        message = (
            f"This login request carries no app credentials for {host}. "
            f"Every Mastodon server is separate, so an app registered on one "
            f'means nothing on another. Call create_app(host="{host}", ...) '
            f"once, save what comes back, and put it on the LoginRequest."
        )
        raise ConfigError(message)
    return request.app


def _check_state(request: LoginRequest, callback: Mapping[str, str]) -> None:
    """Check the value that came back is the one we sent.

    This is what stops somebody handing your app a login they started
    themselves and having it saved against one of your users.

    Args:
        request: The request used to start the login.
        callback: The query values Mastodon sent back.

    Raises:
        AuthError: If both sides have a state and they are different.
    """
    returned = callback.get("state", "")
    if request.state is not None and returned and returned != request.state:
        message = (
            "The state Mastodon sent back did not match the one we sent. "
            "This login did not start here, so nothing has been saved. Start "
            "a new one."
        )
        raise AuthError(message)


def _verifier_from(remember: RawData | None) -> str:
    """Read the secret `start_login` made back out of what your app kept.

    Args:
        remember: What `start_login` put in `SendToNetwork.remember`.

    Returns:
        The secret to send with the code.

    Raises:
        AuthError: If it did not come back. Without it Mastodon cannot tell
            that this is the same sign-in it started, and will refuse the
            code - so saying it here is clearer than letting the server say
            it in its own words.
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
        raise AuthError(message)
    return verifier


def _code_from(callback: Mapping[str, str]) -> str:
    """Pull the login code out of what Mastodon sent back.

    Args:
        callback: The query values Mastodon sent back.

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
            f"Mastodon did not sign this person in ({refused}). Usually they "
            f"pressed cancel on the approval page.{detail}"
        )
        raise AuthError(message)

    code = callback.get("code")
    if not code:
        message = (
            "Mastodon sent no code back, so there is nothing to swap for a "
            "token. Check you are passing the whole query string from your "
            "redirect address."
        )
        raise AuthError(message)
    return code
