"""What each network can do, and checking a post before we send it.

Two kinds of answer live here:

`Feature` is fixed. Bluesky cannot schedule posts and never will within its
current API, so a platform file states that once.

`Limits` is looked up while running, because it genuinely changes. A Mastodon
server's post length is set by whoever runs that server - 500 characters by
default, 5,000 on plenty of servers. Instagram tells us how many posts are
left today. Numbers like these cannot be written into the code.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Flag, auto
from typing import TYPE_CHECKING

from socialchimp.errors import InvalidPostError, NotSupportedError
from socialchimp.models import MediaKind

if TYPE_CHECKING:
    from socialchimp.models import Post

__all__ = ["Feature", "Limits", "check_post"]


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


@dataclass(frozen=True, slots=True)
class Limits:
    """Numbers that a network enforces right now.

    Every field may be `None`, which means "we do not know" - never "zero".
    An unknown limit is not checked.

    Attributes:
        max_text_length: Longest post this network or server accepts.
        max_images: Most pictures allowed on one post.
        max_videos: Most videos allowed on one post.
        max_video_bytes: Largest video file allowed.
        posts_left_today: How many more posts are allowed today, where the
            network tells us. Instagram and Threads both do.
    """

    max_text_length: int | None = None
    max_images: int | None = None
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

    if limits.max_text_length is not None and len(post.text) > limits.max_text_length:
        message = (
            f"This post is {len(post.text)} characters but {platform} allows "
            f"at most {limits.max_text_length}."
        )
        raise InvalidPostError(message)

    if limits.posts_left_today is not None and limits.posts_left_today <= 0:
        message = (
            f"No posts left on {platform} today. Its daily limit has been "
            f"used up. Try again tomorrow."
        )
        raise InvalidPostError(message)

    _check_media(post, platform, features, limits)
