"""The data socialchimp passes around.

Everything here is frozen: once made, it never changes. A refresh produces a
new `Connection` rather than editing the old one, so a half-applied update is
impossible.

Anything holding a secret hides it from `repr()`. These objects end up in log
lines and tracebacks, and a token printed once is a token leaked forever.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any

__all__ = [
    "AppCredentials",
    "Connection",
    "Media",
    "MediaKind",
    "Post",
    "PostResult",
    "PostState",
    "RawData",
    "Token",
    "require_timezone",
]

# The untouched reply from a social network, exactly as it arrived.
# We hand this back on every result so you are never blocked by a field we
# did not think to model.
RawData = dict[str, Any]

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
        ValueError: If `value` has no timezone attached.
    """
    if value is not None and value.tzinfo is None:
        message = (
            f"{name} needs a timezone. "
            f"Use datetime.now(UTC) or add tzinfo=UTC to the value."
        )
        raise ValueError(message)


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
    """

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        """Check the expiry has a timezone."""
        require_timezone(self.expires_at, "expires_at")

    def expires_within(self, seconds: float) -> bool:
        """Say whether this token runs out inside the next `seconds`.

        Used to refresh early, before a request fails.

        Args:
            seconds: How far ahead to look.

        Returns:
            True if the token expires within that window. Always False for a
            token that does not expire.
        """
        if self.expires_at is None:
            return False
        deadline = datetime.now(UTC).timestamp() + seconds
        return deadline >= self.expires_at.timestamp()

    @property
    def is_expired(self) -> bool:
        """Whether this token has already run out."""
        return self.expires_within(seconds=0)


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
            ValueError: If the ending is not one we recognise and no kind
                was given.
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
        raise ValueError(message)

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

    def read(self) -> bytes:
        """Return the file's bytes.

        Returns:
            The content, read from disk if it is not already in memory.

        Raises:
            ValueError: If this media is only a URL. Download it first, or
                use a network that fetches URLs itself.
        """
        if self.content is not None:
            return self.content
        if self.path is not None:
            return self.path.read_bytes()

        message = (
            f"This media is a url ({self.url!r}), so there are no bytes to "
            f"read yet. Download it first, or let the platform fetch it."
        )
        raise ValueError(message)


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
            ValueError: If the post is empty.
        """
        if not self.text and not self.media:
            message = "A post needs text or media. This one has neither."
            raise ValueError(message)
        require_timezone(self.publish_at, "publish_at")


@dataclass(frozen=True, slots=True)
class PostResult:
    """What came back after publishing.

    Attributes:
        id: The network's identifier for the new post.
        url: Link to the post, where the network gives us one.
        state: Whether the network has finished with it.
        raw: The network's untouched reply, for anything we did not model.
    """

    id: str
    url: str | None = None
    state: PostState = PostState.DONE
    raw: RawData = field(default_factory=dict, repr=False)

    @property
    def is_done(self) -> bool:
        """Whether the post is live. False while a network is still working."""
        return self.state is PostState.DONE
