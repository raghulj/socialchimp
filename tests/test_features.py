"""Tests for what each network can do, and for checking a post against it."""

from datetime import UTC, datetime, timedelta

import pytest

from socialchimp import (
    Feature,
    InvalidPostError,
    Limits,
    Media,
    NotSupportedError,
    Post,
    check_post,
)

EVERYTHING = (
    Feature.POST_TEXT
    | Feature.POST_IMAGE
    | Feature.POST_VIDEO
    | Feature.SCHEDULE
    | Feature.REPLY
)


class TestFeature:
    def test_features_combine_and_can_be_tested(self) -> None:
        supported = Feature.POST_TEXT | Feature.POST_IMAGE

        assert Feature.POST_TEXT in supported
        assert Feature.POST_VIDEO not in supported


class TestLimits:
    def test_limits_are_unset_by_default(self) -> None:
        # An unset limit means "we do not know", never "zero".
        limits = Limits()

        assert limits.max_text_length is None
        assert limits.posts_left_today is None


class TestCheckPost:
    def test_a_fine_post_raises_nothing(self) -> None:
        check_post(
            Post(text="hello"),
            platform="mastodon",
            features=EVERYTHING,
            limits=Limits(max_text_length=500),
        )

    def test_text_that_is_too_long_is_refused_before_sending(self) -> None:
        # Better to fail here, with a clear message, than to let the network
        # reject it with a code nobody can read.
        with pytest.raises(InvalidPostError) as caught:
            check_post(
                Post(text="x" * 301),
                platform="bluesky",
                features=EVERYTHING,
                limits=Limits(max_text_length=300),
            )

        message = str(caught.value)
        assert "301" in message
        assert "300" in message

    def test_an_unknown_text_limit_is_not_enforced(self) -> None:
        check_post(
            Post(text="x" * 10_000),
            platform="mastodon",
            features=EVERYTHING,
            limits=Limits(max_text_length=None),
        )

    def test_scheduling_on_a_network_that_cannot_schedule_is_refused(self) -> None:
        later = datetime.now(UTC) + timedelta(hours=1)

        with pytest.raises(NotSupportedError, match="schedul"):
            check_post(
                Post(text="hi", publish_at=later),
                platform="bluesky",
                features=Feature.POST_TEXT,
                limits=Limits(),
            )

    def test_a_picture_on_a_text_only_network_is_refused(self) -> None:
        with pytest.raises(NotSupportedError, match="picture"):
            check_post(
                Post(text="hi", media=(Media.from_bytes(b"", filename="a.png"),)),
                platform="somewhere",
                features=Feature.POST_TEXT,
                limits=Limits(),
            )

    def test_a_video_on_a_network_without_video_is_refused(self) -> None:
        with pytest.raises(NotSupportedError, match="video"):
            check_post(
                Post(text="hi", media=(Media.from_bytes(b"", filename="a.mp4"),)),
                platform="somewhere",
                features=Feature.POST_TEXT | Feature.POST_IMAGE,
                limits=Limits(),
            )

    def test_too_many_pictures_is_refused(self) -> None:
        four_pictures = tuple(
            Media.from_bytes(b"", filename=f"{index}.png") for index in range(4)
        )

        with pytest.raises(InvalidPostError, match="4"):
            check_post(
                Post(text="hi", media=four_pictures),
                platform="somewhere",
                features=EVERYTHING,
                limits=Limits(max_images=3),
            )

    def test_a_reply_on_a_network_that_cannot_reply_is_refused(self) -> None:
        with pytest.raises(NotSupportedError, match="repl"):
            check_post(
                Post(text="hi", reply_to="123"),
                platform="somewhere",
                features=Feature.POST_TEXT,
                limits=Limits(),
            )

    def test_running_out_of_posts_for_today_is_refused(self) -> None:
        # Instagram and Threads both cap this, and both tell us the number.
        with pytest.raises(InvalidPostError, match="today"):
            check_post(
                Post(text="hi"),
                platform="instagram",
                features=EVERYTHING,
                limits=Limits(posts_left_today=0),
            )

    def test_too_many_videos_is_refused(self) -> None:
        two_videos = tuple(
            Media.from_bytes(b"", filename=f"{index}.mp4") for index in range(2)
        )

        with pytest.raises(InvalidPostError, match="2 videos"):
            check_post(
                Post(text="hi", media=two_videos),
                platform="tiktok",
                features=EVERYTHING,
                limits=Limits(max_videos=1),
            )
