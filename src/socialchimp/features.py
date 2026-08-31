"""What each network can do, and checking a post before we send it.

Two kinds of answer live here:

`Feature` is fixed. Bluesky cannot schedule posts and never will within its
current API, so a platform file states that once.

`Limits` is looked up while running, because it genuinely changes. A Mastodon
server's post length is set by whoever runs that server - 500 characters by
default, 5,000 on plenty of servers. Instagram tells us how many posts are
left today. Numbers like these cannot be written into the code.

`TextCount` says what a network's "300" is actually counted in, because
hardly any of them mean characters. Bluesky counts letters as a person would
- a family emoji is one, not seven - and counts bytes as well. Threads counts
bytes. TikTok counts the way Java does, where an emoji is two. A platform
says which once, and `check_post` then counts the same way the network will
instead of guessing.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import Enum, Flag, auto
from typing import TYPE_CHECKING, Final

from socialchimp.errors import InvalidPostError, NotSupportedError
from socialchimp.models import MediaKind

if TYPE_CHECKING:
    from socialchimp.models import Post

__all__ = [
    "Feature",
    "Limits",
    "TextCount",
    "check_post",
    "count_graphemes",
    "measure_text",
]


class Feature(Flag):
    """Things a network either can or cannot do.

    A platform file lists the ones it supports. Combine them with `|` and
    test them with `in`:

        supported = Feature.POST_TEXT | Feature.POST_IMAGE
        Feature.POST_VIDEO in supported    # False
    """

    CREATE_APP = auto()
    """socialchimp can register your app automatically.

    True for Mastodon only, today. Everywhere else you create the app by hand
    in the network's developer portal first.
    """

    POST_TEXT = auto()
    """Can publish words."""

    POST_IMAGE = auto()
    """Can publish pictures."""

    POST_VIDEO = auto()
    """Can publish video."""

    SCHEDULE = auto()
    """Can be asked to publish later."""

    REPLY = auto()
    """Can reply to another post."""

    DELETE_POST = auto()
    """Can remove a post it published."""

    READ_POSTS = auto()
    """Can read posts back."""

    READ_STATS = auto()
    """Can read numbers such as likes and views."""

    PUSH_UPDATES = auto()
    """Tells us when something happens, instead of us having to check.

    Where this is missing, socialchimp checks on a timer and gives you the
    same updates anyway. Your code does not need to know which is happening.
    """


class TextCount(Enum):
    """How a network counts the length of a post.

    Nearly every network says "300 characters" and means something else by
    it, and the difference only shows up once somebody posts an emoji.

    A platform names one of these on its `Limits`, and `check_post` then
    counts the same way that network will. Left out, it is `CHARACTERS`,
    which is what Python's own `len` gives - so a platform written before
    this existed behaves exactly as it did.
    """

    CHARACTERS = "characters"
    """One per character, which is what `len(text)` gives.

    Mastodon counts this way, and so does anything that has not said
    otherwise.
    """

    GRAPHEMES = "graphemes"
    """Letters as a person would count them.

    A family emoji is seven characters and one letter; a flag is two and
    one. Bluesky's 300 is counted this way, so counting characters instead
    refuses posts it would happily have taken. See `count_graphemes`.
    """

    UTF8_BYTES = "utf8_bytes"
    """Bytes, once the text is written out.

    An emoji takes four of them and an accented letter two. Threads counts
    this way, and Bluesky has a second limit of this kind on top of its
    first.
    """

    UTF16_UNITS = "utf16_units"
    """The way Java and JavaScript count, where an emoji is two.

    Networks built on either tend to count this way - TikTok does - so a
    post of 200 emoji is 200 to Python and 400 to them.
    """

    @property
    def in_words(self) -> str:
        """What to call this in a message somebody has to read."""
        return _IN_WORDS[self]

    @property
    def the_catch(self) -> str:
        """The surprise worth naming, or nothing when there is none."""
        return _THE_CATCH[self]


# What each way of counting is called in a message, and the surprise worth
# naming underneath it. Somebody who counted the characters in their editor
# and had their post refused anyway is owed both halves.
_IN_WORDS: Final[dict[TextCount, str]] = {
    TextCount.CHARACTERS: "characters",
    TextCount.GRAPHEMES: "letters",
    TextCount.UTF8_BYTES: "bytes",
    TextCount.UTF16_UNITS: "characters",
}

_THE_CATCH: Final[dict[TextCount, str]] = {
    TextCount.CHARACTERS: "",
    TextCount.GRAPHEMES: (
        " Letters are counted here the way a person counts them, so a family "
        "emoji is one letter rather than seven."
    ),
    TextCount.UTF8_BYTES: (
        " This network counts bytes rather than characters, and an emoji "
        "takes four of them."
    ),
    TextCount.UTF16_UNITS: (
        " This network counts an emoji as two, so its number is higher than "
        "the one your editor shows you."
    ),
}

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


def measure_text(text: str, counted_in: TextCount = TextCount.CHARACTERS) -> int:
    """Measure some text the way one network counts it.

    Args:
        text: The words to measure.
        counted_in: How that network counts. Left out, characters.

    Returns:
        How long that network will think this post is.
    """
    if counted_in is TextCount.GRAPHEMES:
        return count_graphemes(text)
    if counted_in is TextCount.UTF8_BYTES:
        return len(text.encode())
    if counted_in is TextCount.UTF16_UNITS:
        # Two bytes to a unit. The little-endian form is used because the
        # plain one puts a marker at the front that would count as a unit.
        return len(text.encode("utf-16-le")) // 2
    return len(text)


@dataclass(frozen=True, slots=True)
class Limits:
    """Numbers that a network enforces right now.

    Every field may be `None`, which means "we do not know" - never "zero".
    An unknown limit is not checked.

    Attributes:
        max_text_length: Longest post this network or server accepts,
            counted the way `text_counted_in` says.
        max_text_bytes: Longest post once written out, for a network that
            has this limit as well as the first. Bluesky has both: 300
            letters and 3,000 bytes, and a post has to be inside both.
        text_counted_in: What `max_text_length` is counted in. Left out,
            characters, which is what most code assumes and what Mastodon
            means.
        max_images: Most pictures allowed on one post.
        max_image_bytes: Largest picture file allowed.
        max_videos: Most videos allowed on one post.
        max_video_bytes: Largest video file allowed.
        posts_left_today: How many more posts are allowed today, where the
            network tells us. Instagram and Threads both do.

    The two file sizes are here to be shown to your users and to size things
    before uploading. Nothing here opens a file to check them, because that
    would mean reading every picture off disk to send one post - the network
    is what enforces those, and a file that is too big comes back as an
    `InvalidPostError` from the platform.
    """

    max_text_length: int | None = None
    max_text_bytes: int | None = None
    text_counted_in: TextCount = TextCount.CHARACTERS
    max_images: int | None = None
    max_image_bytes: int | None = None
    max_videos: int | None = None
    max_video_bytes: int | None = None
    posts_left_today: int | None = None


def _check_media(post: Post, platform: str, features: Feature, limits: Limits) -> None:
    """Check attachments against what the network allows.

    Args:
        post: The post about to be sent.
        platform: Name of the network, used in messages.
        features: What the network can do.
        limits: What the network currently allows.

    Raises:
        NotSupportedError: If the network cannot take this kind of file.
        InvalidPostError: If there are more files than allowed.
    """
    pictures = [item for item in post.media if item.kind is MediaKind.IMAGE]
    videos = [item for item in post.media if item.kind is MediaKind.VIDEO]

    if pictures and Feature.POST_IMAGE not in features:
        raise NotSupportedError(platform=platform, what="posting pictures")
    if videos and Feature.POST_VIDEO not in features:
        raise NotSupportedError(platform=platform, what="posting video")

    if limits.max_images is not None and len(pictures) > limits.max_images:
        message = (
            f"This post has {len(pictures)} pictures but {platform} allows "
            f"at most {limits.max_images}."
        )
        raise InvalidPostError(message)

    if limits.max_videos is not None and len(videos) > limits.max_videos:
        message = (
            f"This post has {len(videos)} videos but {platform} allows "
            f"at most {limits.max_videos}."
        )
        raise InvalidPostError(message)


def _check_length(post: Post, platform: str, limits: Limits) -> None:
    """Check a post's text against however this network counts it.

    Args:
        post: The post about to be sent.
        platform: Name of the network, used in messages.
        limits: What the network currently allows.

    Raises:
        InvalidPostError: If the post is too long either way. A network can
            have both limits - Bluesky does - and a post has to be inside
            both of them.
    """
    counted = limits.text_counted_in
    if limits.max_text_length is not None:
        length = measure_text(post.text, counted)
        if length > limits.max_text_length:
            message = (
                f"This post is {length} {counted.in_words} but {platform} "
                f"allows at most {limits.max_text_length}.{counted.the_catch}"
            )
            raise InvalidPostError(message)

    if limits.max_text_bytes is not None:
        written = len(post.text.encode())
        if written > limits.max_text_bytes:
            message = (
                f"This post takes {written} bytes to write out, but "
                f"{platform} allows at most {limits.max_text_bytes}. Emoji "
                f"take four bytes each and accented letters two, so a post "
                f"with few enough letters can still be over."
            )
            raise InvalidPostError(message)


def check_post(
    post: Post,
    *,
    platform: str,
    features: Feature,
    limits: Limits,
) -> None:
    """Check a post against a network's rules before sending it.

    Catching problems here means a clear message instead of a network error
    code, and one less wasted request against a rate limit.

    Args:
        post: The post about to be sent.
        platform: Name of the network, used in messages.
        features: What the network can do.
        limits: What the network currently allows.

    Raises:
        NotSupportedError: If the post needs something the network cannot do.
        InvalidPostError: If the post breaks one of the network's limits.
    """
    if post.publish_at is not None and Feature.SCHEDULE not in features:
        raise NotSupportedError(platform=platform, what="scheduling posts")

    if post.reply_to is not None and Feature.REPLY not in features:
        raise NotSupportedError(platform=platform, what="replying to posts")

    _check_length(post, platform, limits)

    if limits.posts_left_today is not None and limits.posts_left_today <= 0:
        message = (
            f"No posts left on {platform} today. Its daily limit has been "
            f"used up. Try again tomorrow."
        )
        raise InvalidPostError(message)

    _check_media(post, platform, features, limits)
