"""The data socialchimp passes around.

Everything here is frozen: once made, it never changes. A refresh produces a
new `Connection` rather than editing the old one, so a half-applied update is
impossible.

Anything holding a secret hides it from `repr()`. These objects end up in log
lines and tracebacks, and a token printed once is a token leaked forever.

Everything here refuses with a `SocialChimpError`, the same as a platform
does, so an app catches one thing wherever the problem came from. Each of
those is a `ValueError` as well - see the comment in `socialchimp.errors`.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any, Generic, TypeVar

# Safe in this direction only: `socialchimp.errors` imports nothing from
# here, and nothing from anywhere else in socialchimp. Keep it that way -
# an import back the other way makes the pair impossible to load.
from socialchimp.errors import ConfigError, InvalidPostError

__all__ = [
    "AppCredentials",
    "Attachment",
    "BusinessLocation",
    "Connection",
    "Conversation",
    "Like",
    "LikeResult",
    "LinkKind",
    "Media",
    "MediaKind",
    "Message",
    "Page",
    "Person",
    "Post",
    "PostDetails",
    "PostResult",
    "PostState",
    "PostStats",
    "RawData",
    "TextLink",
    "Thread",
    "Token",
    "Unavailable",
    "Verification",
    "VerificationOption",
    "Visibility",
    "require_timezone",
]

# The untouched reply from a social network, exactly as it arrived.
# We hand this back on every result so you are never blocked by a field we
# did not think to model.
RawData = dict[str, Any]

# What one page of a list call is made of. Every list call takes
# `after: str | None = None, limit: int | None = None` and hands back a
# `Page` of whatever it lists.
T = TypeVar("T")

# File endings we can recognise without being told.
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"})
_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"})


def require_timezone(value: datetime | None, name: str) -> None:
    """Refuse a datetime with no timezone.

    A datetime without a timezone compares wrongly against one that has a
    timezone, and the failure is silent. Better to refuse it at the door.

    Args:
        value: The datetime to check. `None` is allowed and does nothing.
        name: Field name, used in the error message.

    Raises:
        ConfigError: If `value` has no timezone attached. A `ConfigError`
            rather than an `InvalidPostError` because this is a mistake in
            the calling code and not a post a network would refuse - and
            because the same mistake is checked here on a token's expiry, an
            update's timestamp and a signed request's time, where "post"
            means nothing at all.
    """
    if value is not None and value.tzinfo is None:
        message = (
            f"{name} needs a timezone. "
            f"Use datetime.now(UTC) or add tzinfo=UTC to the value."
        )
        raise ConfigError(message)


class MediaKind(Enum):
    """What sort of file is being attached."""

    IMAGE = auto()
    VIDEO = auto()


class PostState(Enum):
    """How far along a post is.

    Most networks finish while we wait. YouTube and TikTok keep working after
    they accept the upload, so a post can come back as `PROCESSING` and finish
    later. You hear about it through an update (see `socialchimp.events`).
    """

    DONE = auto()
    """The post is live now."""

    SCHEDULED = auto()
    """The network accepted it and will publish it later."""

    PROCESSING = auto()
    """The network is still working on it, usually a video being encoded."""

    WAITING_FOR_PERSON = auto()
    """The network has finished, and now somebody has to tap a button.

    TikTok can put a video in a person's drafts rather than posting it, so
    they can add their own caption and publish it themselves. Nothing is
    wrong and nothing more will happen on its own, so do not sit and wait
    for this one to change.
    """

    FAILED = auto()
    """The network gave up on it."""


@dataclass(frozen=True, slots=True)
class Token:
    """Permission to act as someone on a social network.

    Attributes:
        access_token: The token used on every request.
        refresh_token: Used to get a new access token. `None` where the
            network does not offer one.
        expires_at: When the access token stops working. `None` means it does
            not expire on its own (Mastodon, Discord and Telegram work this
            way).
        refresh_token_expires_at: When the refresh token itself stops
            working. `None` on the networks that never expire theirs, which
            is most of them. Pinterest's lasts sixty days, and renewing an
            access token does not extend it - so an account nobody has
            posted from since the summer needs signing in again, and
            without this an app cannot know until the day it breaks.
    """

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: datetime | None = None
    refresh_token_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        """Check both expiries have a timezone."""
        require_timezone(self.expires_at, "expires_at")
        require_timezone(self.refresh_token_expires_at, "refresh_token_expires_at")

    @staticmethod
    def _runs_out_within(when: datetime | None, seconds: float) -> bool:
        """Say whether a moment is inside the next `seconds`.

        Args:
            when: The moment, or `None` for something that never happens.
            seconds: How far ahead to look.

        Returns:
            True if that moment is inside the window. Always False for
            `None`, because "we were never told" is not "it has run out".
        """
        if when is None:
            return False
        return datetime.now(UTC).timestamp() + seconds >= when.timestamp()

    def expires_within(self, seconds: float) -> bool:
        """Say whether this token runs out inside the next `seconds`.

        Used to refresh early, before a request fails.

        Args:
            seconds: How far ahead to look.

        Returns:
            True if the token expires within that window. Always False for a
            token that does not expire.
        """
        return self._runs_out_within(self.expires_at, seconds)

    @property
    def is_expired(self) -> bool:
        """Whether this token has already run out."""
        return self.expires_within(seconds=0)

    def refresh_token_expires_within(self, seconds: float) -> bool:
        """Say whether the refresh token runs out inside the next `seconds`.

        Nothing socialchimp does can renew a refresh token, so this is not a
        warning to act on in code - it is a warning to show a person, far
        enough ahead that they can connect their account again before
        anything stops working. A week is a reasonable window.

        Args:
            seconds: How far ahead to look.

        Returns:
            True if the refresh token expires within that window. Always
            False where the network never told us, which is most of them.
        """
        return self._runs_out_within(self.refresh_token_expires_at, seconds)

    @property
    def refresh_token_is_expired(self) -> bool:
        """Whether the refresh token has already run out.

        True here means the person has to sign in again. There is nothing
        left to renew with.
        """
        return self.refresh_token_expires_within(seconds=0)


@dataclass(frozen=True, slots=True)
class AppCredentials:
    """Your app's own identity on one social network.

    On most networks you create this by hand in a developer portal. On
    Mastodon socialchimp can create it for you, and it has to be created
    again for every server, because each Mastodon server is separate. That
    is why `host` is part of the key.

    Attributes:
        platform: Which network, for example `"mastodon"`.
        host: Which server, for networks that have more than one. `None`
            everywhere else.
        client_id: Public half, given to you by the network.
        client_secret: Private half. Never logged, never printed.
    """

    platform: str
    host: str | None
    client_id: str
    client_secret: str = field(repr=False)

    @property
    def key(self) -> tuple[str, str | None]:
        """How these credentials are looked up in storage."""
        return (self.platform, self.host)


@dataclass(frozen=True, slots=True)
class Connection:
    """One social account someone has connected to your app.

    This is the thing your app saves. socialchimp hands it to you; where and
    how you store it is entirely up to you.

    Attributes:
        id: Your identifier for this connection. You choose it.
        platform: Which network, for example `"bluesky"`.
        host: Which server, for networks that have more than one.
        account_id: The identifier the network itself uses.
        account_name: Something a person would recognise, shown in your UI.
        token: Current permission to act as this account.
        scopes: What this token is allowed to do.
        extra: Anything else one network needs, such as a Facebook page id
            or a YouTube channel id.
    """

    id: str
    platform: str
    host: str | None
    account_id: str
    account_name: str
    token: Token
    scopes: tuple[str, ...] = ()
    extra: RawData = field(default_factory=dict)

    def with_token(self, token: Token) -> Connection:
        """Return a copy of this connection carrying a new token.

        Used after a refresh. The original is left alone.

        Args:
            token: The replacement token.

        Returns:
            A new `Connection`, same in every other way.
        """
        return Connection(
            id=self.id,
            platform=self.platform,
            host=self.host,
            account_id=self.account_id,
            account_name=self.account_name,
            token=token,
            scopes=self.scopes,
            extra=self.extra,
        )


@dataclass(frozen=True, slots=True)
class Media:
    """A picture or video to attach to a post.

    Build one with `from_file`, `from_bytes` or `from_url` rather than calling
    `Media(...)` directly - those work out the kind for you.

    Attributes:
        kind: Picture or video.
        content: The bytes, when they were handed to us directly.
        path: Where the file lives on disk, read only when needed.
        url: Where the file lives online. Some networks fetch it themselves;
            for the rest socialchimp downloads it first.
        filename: Name to send along with the upload.
        alt_text: Description for people using a screen reader. Worth setting.
    """

    kind: MediaKind
    content: bytes | None = field(default=None, repr=False)
    path: Path | None = None
    url: str | None = None
    filename: str | None = None
    alt_text: str | None = None

    @staticmethod
    def _guess_kind(name: str, given: MediaKind | None) -> MediaKind:
        """Work out whether a filename points at a picture or a video.

        Args:
            name: The filename or URL to inspect.
            given: A kind supplied by the caller, which always wins.

        Returns:
            The kind of media.

        Raises:
            InvalidPostError: If the ending is not one we recognise and no
                kind was given.
        """
        if given is not None:
            return given

        suffix = Path(name).suffix.lower()
        if suffix in _IMAGE_SUFFIXES:
            return MediaKind.IMAGE
        if suffix in _VIDEO_SUFFIXES:
            return MediaKind.VIDEO

        message = (
            f"Cannot tell whether {name!r} is a picture or a video. "
            f"Pass kind=MediaKind.IMAGE or kind=MediaKind.VIDEO to say which."
        )
        raise InvalidPostError(message)

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        kind: MediaKind | None = None,
        alt_text: str | None = None,
    ) -> Media:
        """Attach a file from disk. It is read when the upload happens.

        Args:
            path: Where the file is.
            kind: Picture or video. Worked out from the name if left out.
            alt_text: Description for screen readers.

        Returns:
            The media, ready to attach to a post.
        """
        location = Path(path)
        return cls(
            kind=cls._guess_kind(location.name, kind),
            path=location,
            filename=location.name,
            alt_text=alt_text,
        )

    @classmethod
    def from_bytes(
        cls,
        content: bytes,
        *,
        filename: str,
        kind: MediaKind | None = None,
        alt_text: str | None = None,
    ) -> Media:
        """Attach data you already hold in memory.

        Args:
            content: The file's bytes.
            filename: Name to send with the upload. Also used to work out
                the kind.
            kind: Picture or video. Worked out from the name if left out.
            alt_text: Description for screen readers.

        Returns:
            The media, ready to attach to a post.
        """
        return cls(
            kind=cls._guess_kind(filename, kind),
            content=content,
            filename=filename,
            alt_text=alt_text,
        )

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        kind: MediaKind | None = None,
        alt_text: str | None = None,
    ) -> Media:
        """Point at a file already online.

        Args:
            url: Where the file is. It must be reachable by the network.
            kind: Picture or video. Worked out from the address if left out.
            alt_text: Description for screen readers.

        Returns:
            The media, ready to attach to a post.
        """
        return cls(
            kind=cls._guess_kind(url, kind),
            url=url,
            filename=Path(url).name or None,
            alt_text=alt_text,
        )

    @property
    def content_type(self) -> str:
        """The MIME type to send with the upload."""
        guessed, _ = mimetypes.guess_type(self.filename or "")
        if guessed is not None:
            return guessed
        return "image/jpeg" if self.kind is MediaKind.IMAGE else "video/mp4"

    @property
    def size(self) -> int | None:
        """How many bytes this is, when we can tell.

        `None` for a file that is only a web address, because finding out
        would mean downloading it - which is usually the thing a web address
        was used to avoid.

        Returns:
            The size in bytes, or `None` if it is not knowable yet.
        """
        if self.content is not None:
            return len(self.content)
        if self.path is not None:
            return self.path.stat().st_size
        return None

    def piece(self, start: int, length: int) -> bytes:
        """Read part of the file.

        Networks that take large video want it in pieces - YouTube, TikTok
        and Facebook all do. Reading a piece at a time keeps a four gigabyte
        video from becoming four gigabytes of memory, so use this rather
        than slicing what `read()` gives you.

        Args:
            start: How many bytes in to begin.
            length: How many bytes to read. Fewer come back at the end of
                the file, which is how you know you have reached it.

        Returns:
            The bytes read.

        Raises:
            InvalidPostError: If this media is only a web address.
        """
        if self.content is not None:
            return self.content[start : start + length]
        if self.path is not None:
            with self.path.open("rb") as opened:
                opened.seek(start)
                return opened.read(length)

        message = (
            f"This media is a url ({self.url!r}), so there are no bytes to "
            f"read yet. Download it first, or let the platform fetch it."
        )
        raise InvalidPostError(message)

    def read(self) -> bytes:
        """Return the file's bytes.

        Returns:
            The content, read from disk if it is not already in memory.

        Raises:
            InvalidPostError: If this media is only a URL. Download it
                first, or use a network that fetches URLs itself.
        """
        if self.content is not None:
            return self.content
        if self.path is not None:
            return self.path.read_bytes()

        message = (
            f"This media is a url ({self.url!r}), so there are no bytes to "
            f"read yet. Download it first, or let the platform fetch it."
        )
        raise InvalidPostError(message)


@dataclass(frozen=True, slots=True)
class Post:
    """Something to publish.

    The fields here work on most networks. Anything that belongs to one
    network only goes in `options`.

    Attributes:
        text: The words. Some networks call this a caption or a body.
        media: Pictures or videos to attach.
        reply_to: Identifier of the post being replied to.
        publish_at: When to publish, for networks that can schedule. Check
            `Feature.SCHEDULE` first - most cannot, and socialchimp will say
            so rather than quietly posting straight away.
        options: Settings for one network only, such as Pinterest's
            `board_id` or Mastodon's `visibility`. Each platform's page lists
            what it accepts.
    """

    text: str = ""
    media: tuple[Media, ...] = ()
    reply_to: str | None = None
    publish_at: datetime | None = None
    options: RawData = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Check the post has something in it and a valid publish time.

        Raises:
            InvalidPostError: If the post is empty.
            ConfigError: If `publish_at` has no timezone.
        """
        if not self.text and not self.media:
            message = "A post needs text or media. This one has neither."
            raise InvalidPostError(message)
        require_timezone(self.publish_at, "publish_at")


@dataclass(frozen=True, slots=True)
class PostResult:
    """What came back after publishing.

    Attributes:
        id: The network's identifier for the new post.
        url: Link to the post, where the network gives us one.
        state: Whether the network has finished with it.
        raw: The network's untouched reply, for anything we did not model.
        cid: Bluesky's content hash for the new post. `None` everywhere
            else. Added in 0.8.0, after `raw`, so code from before 0.8.0
            that builds a `PostResult` by position - `id, url, state, raw`
            - still puts its fourth argument in `raw`, not here.
    """

    id: str
    url: str | None = None
    state: PostState = PostState.DONE
    raw: RawData = field(default_factory=dict, repr=False)
    cid: str | None = None

    @property
    def is_done(self) -> bool:
        """Whether the post is live. False while a network is still working."""
        return self.state is PostState.DONE


@dataclass(frozen=True, slots=True)
class PostStats:
    """How a published post is doing.

    Every number may be `None`, which means "this network does not count
    that" - never "zero". A post nobody has liked and a network that keeps
    no likes are two different answers, and only one of them is a number.

    Networks all use their own words for these: Mastodon counts favourites
    and boosts, X counts likes and reposts. They arrive here under one set
    of names, so an app does not learn a vocabulary per network.

    Only the numbers a network really publishes are here. Reach,
    impressions and clicks are deliberately missing: most networks do not
    give them out at all, and a field that could never be filled in reads
    like one that is always zero.

    Attributes:
        id: The network's identifier for the post these numbers are about -
            the same one `PostResult.id` carried.
        likes: How many people liked, favourited or reacted to it.
        comments: How many replies it has.
        shares: How many times it was passed on - boosted, reposted,
            reblogged, whichever word that network uses.
        raw: The network's untouched reply, for any number we did not model.
    """

    id: str
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    raw: RawData = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class BusinessLocation:
    """What a network like Google Business Profile calls a place.

    Only the fields worth a name of their own are modelled. A location
    resource has dozens of fields - service areas, opening date, more
    attributes than any app needs at once - and modelling all of them here
    would mean this file changing every time the network adds one. Anything
    not named here is still on `raw`.

    Attributes:
        id: The network's identifier for this location.
        name: The business name shown on the profile.
        phone: The primary phone number, where the location has one.
        address: The postal address, in the shape the network's own API
            returns it.
        categories: The primary category first, then any additional ones.
        raw: The location resource exactly as the network returned it.
    """

    id: str
    name: str
    phone: str | None = None
    address: RawData = field(default_factory=dict)
    categories: tuple[str, ...] = ()
    raw: RawData = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class VerificationOption:
    """One way a location could be verified.

    Attributes:
        method: The network's own name for it, such as `"PHONE_CALL"`,
            `"EMAIL"` or `"MAIL"`.
        display_data: Whatever the network says about it that is worth
            showing to a person before they pick - a phone number's last few
            digits, an email address with the rest starred out.
    """

    method: str
    display_data: RawData = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Verification:
    """One verification, in progress or finished.

    Attributes:
        id: The network's identifier for this verification.
        method: Which of the offered ways was chosen.
        state: The network's own word for where it has got to.
        raw: The network's untouched reply.
    """

    id: str
    method: str
    state: str
    raw: RawData = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    """One page of results from a list call.

    Every list call - reading likes, reading replies, reading conversations,
    reading messages - takes `after: str | None = None, limit: int | None =
    None` and hands one of these back.

    Attributes:
        items: What this page holds.
        next: Pass this back as `after=` to read the page after this one.
            `None` means there is no more. Treat it as opaque - store it as
            a string and never parse it. Mastodon fills it from the `Link`
            header it sends back; Bluesky and Meta fill it from the
            network's own cursor.
    """

    items: tuple[T, ...]
    next: str | None = None


@dataclass(frozen=True, slots=True)
class Person:
    """Someone on a social network.

    The author of a post, the person behind a like, the other side of a
    conversation - all of them are a `Person`.

    Attributes:
        id: The network's identifier for them - a Mastodon account id, a
            Bluesky DID, or a Meta PSID, IGSID or user id.
        handle: Something like `"user@host"` or `"name.bsky.social"`.
            `None` where the network has no such thing, which is how Meta's
            messaging works.
        display_name: The name they chose to show, when the network gives
            one.
        avatar_url: Their picture, when the network gives one.
        url: Their profile page, when the network has one.
        raw: The network's untouched reply, for anything we did not model.
    """

    id: str
    handle: str | None
    display_name: str | None
    avatar_url: str | None
    url: str | None
    raw: RawData = field(default_factory=dict, repr=False)


class Visibility(Enum):
    """Who a post was shared with.

    `None` on `PostDetails.visibility` means the network has no such idea at
    all - Bluesky and Meta do not model this the way Mastodon does.
    """

    PUBLIC = "public"
    """Shown to anyone, including people who do not follow the author."""

    UNLISTED = "unlisted"
    """Public, but left out of public timelines and search. Mastodon only."""

    FOLLOWERS = "followers"
    """Shown only to people who follow the author. Mastodon calls this
    `"private"` on the wire; socialchimp uses the clearer word."""

    DIRECT = "direct"
    """Shown only to the people mentioned in it. Mastodon calls this
    `"direct"`."""


class LinkKind(Enum):
    """What a `TextLink` inside a post's text points at."""

    MENTION = "mention"
    """Names another person."""

    LINK = "link"
    """Points at a web address."""

    TAG = "tag"
    """A hashtag."""


@dataclass(frozen=True, slots=True)
class TextLink:
    """A mention, a link or a tag, sitting inside `PostDetails.text`.

    Attributes:
        start: Where this link starts, as a Python string index into
            `PostDetails.text` - character offsets, not bytes.
        end: Where it ends, the same way.
        kind: What sort of link this is.
        target: What it points at: a URL for `LINK`, the mentioned person's
            id for `MENTION`, or the tag's name with no leading `#` for
            `TAG`.
        url: A clickable address for this link, where one is known.
    """

    start: int
    end: int
    kind: LinkKind
    target: str
    url: str | None


@dataclass(frozen=True, slots=True)
class Attachment:
    """A picture, video or other file attached to a post.

    Attributes:
        kind: What sort of file this is - `"image"`, `"video"`, `"gifv"`,
            `"audio"`, `"link"` or `"unknown"`.
        url: Where to fetch the file, when the network gives one.
        preview_url: A smaller version to show before the full file loads,
            when the network gives one.
        alt_text: A description for people using a screen reader, when the
            author wrote one.
        width: The file's width in pixels, when known.
        height: The file's height in pixels, when known.
        raw: The network's untouched reply, for anything we did not model.
    """

    kind: str
    url: str | None
    preview_url: str | None
    alt_text: str | None
    width: int | None
    height: int | None
    raw: RawData = field(default_factory=dict, repr=False)


class Unavailable(Enum):
    """Why a post that should be here could not be shown.

    Set on a `PostDetails` standing in for a post a thread could not
    actually fetch - a placeholder rather than the real thing.
    """

    DELETED = "deleted"
    """The post was removed, or never existed."""

    BLOCKED = "blocked"
    """The author blocked us, or we blocked them."""

    HIDDEN = "hidden"
    """Hidden by moderation - a hidden comment on Meta, or a Bluesky label
    or threadgate."""


@dataclass(frozen=True, slots=True)
class PostDetails:
    """A post, read back in full - not just what publishing it returned.

    Attributes:
        id: The network's identifier for it - a Mastodon status id, a
            Bluesky `at://` uri, or a Meta object id.
        cid: Bluesky's content hash. `None` everywhere else.
        url: The permalink on the network's own website, when there is one.
        author: Who wrote it. `None` only when `unavailable` is set.
        text: The words, as plain text. Mastodon's HTML is converted to
            plain text here. Empty when `unavailable` is set.
        html: The network's own HTML, where it has one - Mastodon does.
            **Untrusted**: sanitise it yourself before showing it to anyone.
        links: The mentions, links and tags inside `text`.
        attachments: The pictures, videos and other files on this post.
        created_at: When it was posted, according to the network.
        visibility: Who it was shared with. `None` when the network has no
            such idea at all - Bluesky and Meta do not.
        parent_id: The post this one replies to, when it replies to one.
        root_id: The top of the thread this post sits in. The same as `id`
            for a top-level post.
        reply_count: How many replies it has. `None` means the network does
            not say - never "zero".
        like_count: How many people liked it. `None` means the network does
            not say.
        repost_count: How many times it was reposted. `None` means the
            network does not say.
        quote_count: How many times it was quoted. `None` means the network
            does not say.
        liked_by_me: Whether the connected account has liked it. `None`
            means we do not know.
        my_like_id: Bluesky's like-record uri for the connected account's
            own like, when there is one. Pass it to `unlike` to save a
            lookup.
        is_mine: Whether the connected account wrote this post.
        unavailable: Set when this is a placeholder standing in for a post a
            thread could not actually fetch, and says why.
        raw: The network's untouched reply, for anything we did not model.
    """

    id: str
    cid: str | None
    url: str | None
    author: Person | None
    text: str
    html: str | None
    links: tuple[TextLink, ...]
    attachments: tuple[Attachment, ...]
    created_at: datetime | None
    visibility: Visibility | None
    parent_id: str | None
    root_id: str | None
    reply_count: int | None
    like_count: int | None
    repost_count: int | None
    quote_count: int | None
    liked_by_me: bool | None
    my_like_id: str | None
    is_mine: bool
    unavailable: Unavailable | None
    raw: RawData = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Check the post's time has a timezone.

        Raises:
            ConfigError: If `created_at` has no timezone.
        """
        require_timezone(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class Thread:
    """A post together with its replies.

    Attributes:
        post: The post that was asked for.
        replies: Every reply read back, flat and oldest first. Build the
            tree yourself by matching each one's `parent_id`.
        complete: `False` if `depth`, `limit` or one of the network's own
            caps cut the replies off before the end.
        raw: The network's untouched reply, for anything we did not model.
    """

    post: PostDetails
    replies: tuple[PostDetails, ...]
    complete: bool
    raw: RawData = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class LikeResult:
    """What came back after liking a post.

    Attributes:
        post_id: The post that was liked.
        like_id: Bluesky's like-record uri, worth keeping so `unlike` can
            skip a lookup. `None` on Mastodon and Meta - there is nothing to
            keep.
        raw: The network's untouched reply, for anything we did not model.
    """

    post_id: str
    like_id: str | None
    raw: RawData = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class Like:
    """One person's like on a post.

    Attributes:
        person: Who liked it.
        liked_at: When they liked it. Bluesky has this; Mastodon never does,
            so it is always `None` there.
        raw: The network's untouched reply, for anything we did not model.
    """

    person: Person
    liked_at: datetime | None
    raw: RawData = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Check the time has a timezone.

        Raises:
            ConfigError: If `liked_at` has no timezone.
        """
        require_timezone(self.liked_at, "liked_at")


@dataclass(frozen=True, slots=True)
class Conversation:
    """A direct message conversation with one or more people.

    Attributes:
        id: The network's identifier for this conversation.
        people: Everyone in it except the connected account.
        last_message: The most recent message, when there is one to show.
        unread_count: How many messages are unread. Mastodon only says yes
            or no, so it reports `1` or `0` rather than a real count.
        updated_at: When this conversation last changed.
        can_reply_until: Meta's 24-hour window to reply closes at this
            moment. `None` means there is no deadline.
        full_history: `False` on Mastodon, which has no "every message"
            call - `read_messages` there only reaches as far as the last
            status's own thread.
        raw: The network's untouched reply, for anything we did not model.
    """

    id: str
    people: tuple[Person, ...]
    last_message: Message | None
    unread_count: int | None
    updated_at: datetime | None
    can_reply_until: datetime | None
    full_history: bool
    raw: RawData = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Check both times have a timezone.

        Raises:
            ConfigError: If `updated_at` or `can_reply_until` has no
                timezone.
        """
        require_timezone(self.updated_at, "updated_at")
        require_timezone(self.can_reply_until, "can_reply_until")


@dataclass(frozen=True, slots=True)
class Message:
    """One message inside a `Conversation`.

    Attributes:
        id: The network's identifier for this message.
        conversation_id: Which conversation it belongs to.
        sender: Who sent it.
        text: The words. Empty when `deleted` is set.
        sent_at: When it was sent.
        is_mine: Whether the connected account sent it.
        deleted: Whether it has been deleted since.
        attachments: Pictures, videos or other files sent with it.
        raw: The network's untouched reply, for anything we did not model.
    """

    id: str
    conversation_id: str
    sender: Person
    text: str
    sent_at: datetime
    is_mine: bool
    deleted: bool
    attachments: tuple[Attachment, ...]
    raw: RawData = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Check the time has a timezone.

        Raises:
            ConfigError: If `sent_at` has no timezone.
        """
        require_timezone(self.sent_at, "sent_at")
