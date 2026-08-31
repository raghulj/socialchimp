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
from socialchimp.features import (
    TextCount,
    check_option_names,
    count_graphemes,
    measure_text,
)

# One thumbs-up with a skin tone on it. One letter to a person, two
# characters to Python, four bytes written out, two units to a network
# counting the way Java and JavaScript do.
BIG_LETTER = "\U0001f44d\U0001f3fd"

# A family. Seven characters, and still one letter.
FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466"

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

    def test_words_alone_on_a_network_with_no_text_post_are_refused(self) -> None:
        # YouTube, Instagram, Pinterest and TikTok all have nothing to
        # publish a post of words as. Each of them used to say so itself,
        # four times over, in four slightly different sentences.
        with pytest.raises(NotSupportedError) as refused:
            check_post(
                Post(text="hello"),
                platform="youtube",
                features=Feature.POST_VIDEO,
                limits=Limits(),
            )

        assert refused.value.what == "posting words on their own"
        assert str(refused.value) == (
            "youtube does not support posting words on their own. Every post "
            "here carries a video, so attach one."
        )

    def test_it_names_both_when_the_network_takes_either(self) -> None:
        with pytest.raises(NotSupportedError) as refused:
            check_post(
                Post(text="hello"),
                platform="pinterest",
                features=Feature.POST_IMAGE | Feature.POST_VIDEO,
                limits=Limits(),
            )

        assert "a picture or a video" in str(refused.value)

    def test_a_platform_can_add_a_sentence_of_its_own(self) -> None:
        with pytest.raises(NotSupportedError) as refused:
            check_post(
                Post(text="hello"),
                platform="youtube",
                features=Feature.POST_VIDEO,
                limits=Limits(),
                words_alone_advice="Community posts are not in the API.",
            )

        assert str(refused.value).endswith(
            "so attach one. Community posts are not in the API."
        )

    def test_a_network_with_nothing_to_attach_says_only_that(self) -> None:
        # A platform claiming none of the three ways to post cannot take
        # anything at all, so there is no "attach one instead" to offer.
        with pytest.raises(NotSupportedError) as refused:
            check_post(
                Post(text="hello"),
                platform="nowhere",
                features=Feature.REPLY,
                limits=Limits(),
            )

        assert str(refused.value) == (
            "nowhere does not support posting words on their own."
        )

    def test_a_post_with_media_gets_past_that(self) -> None:
        check_post(
            Post(text="hello", media=(Media.from_bytes(b"x", filename="a.mp4"),)),
            platform="youtube",
            features=Feature.POST_VIDEO,
            limits=Limits(),
        )

    def test_a_network_that_can_post_text_takes_words_alone(self) -> None:
        check_post(
            Post(text="hello"),
            platform="mastodon",
            features=Feature.POST_TEXT,
            limits=Limits(),
        )

    def test_a_post_too_long_to_send_is_refused_for_that_first(self) -> None:
        # Both wrong at once. The length is the one the person can fix
        # without changing what they are posting, so it is the one to say.
        with pytest.raises(InvalidPostError):
            check_post(
                Post(text="far too long"),
                platform="youtube",
                features=Feature.POST_VIDEO,
                limits=Limits(max_text_length=3),
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


class TestCheckingOptionNames:
    def test_a_setting_the_network_knows_is_left_alone(self) -> None:
        check_option_names(
            {"visibility": "public"},
            platform="mastodon",
            allowed=("visibility", "language"),
        )

    def test_no_settings_at_all_is_fine(self) -> None:
        check_option_names({}, platform="mastodon", allowed=("visibility",))

    def test_a_setting_it_has_never_heard_of_is_refused(self) -> None:
        # A typo here used to cost a request and a slice of the rate limit
        # before the network said no in its own words.
        with pytest.raises(InvalidPostError) as refused:
            check_option_names(
                {"visibilty": "public"},
                platform="mastodon",
                allowed=("visibility", "language"),
            )

        assert str(refused.value) == (
            "mastodon does not know the post option 'visibilty'. It "
            "accepts: visibility, language."
        )
        assert refused.value.platform == "mastodon"

    def test_the_first_unknown_setting_is_the_one_named(self) -> None:
        with pytest.raises(InvalidPostError, match="'one'"):
            check_option_names(
                {"one": 1, "two": 2},
                platform="mastodon",
                allowed=(),
            )

    def test_a_platform_can_add_a_sentence_of_its_own(self) -> None:
        with pytest.raises(InvalidPostError) as refused:
            check_option_names(
                {"description": "a chair"},
                platform="pinterest",
                allowed=("title",),
                advice="Post.text is the pin's description.",
            )

        assert str(refused.value).endswith(
            "It accepts: title. Post.text is the pin's description."
        )


class TestCountingLetters:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("", 0),
            ("hello", 5),
            ("e\u0301", 1),
            ("\U0001f1ec\U0001f1e7", 1),
            ("\U0001f1ec\U0001f1e7\U0001f1eb\U0001f1f7", 2),
            ("\U0001f1ec\U0001f1e7a", 2),
            (BIG_LETTER, 1),
            ("#\ufe0f\u20e3", 1),
            (FAMILY, 1),
        ],
    )
    def test_it_counts_what_a_person_would_call_a_letter(
        self,
        text: str,
        expected: int,
    ) -> None:
        assert count_graphemes(text) == expected


class TestMeasuringText:
    def test_it_counts_characters_unless_told_otherwise(self) -> None:
        assert measure_text(FAMILY) == 7
        assert measure_text(FAMILY, TextCount.CHARACTERS) == 7

    def test_it_can_count_letters_the_way_a_person_would(self) -> None:
        assert measure_text(FAMILY, TextCount.GRAPHEMES) == 1

    def test_it_can_count_the_bytes_a_post_takes_to_write_out(self) -> None:
        assert measure_text("hi", TextCount.UTF8_BYTES) == 2
        assert measure_text(BIG_LETTER, TextCount.UTF8_BYTES) == 8

    def test_it_can_count_the_way_networks_built_on_java_do(self) -> None:
        # An emoji is one character to Python and two units here, which is
        # why a post of 150 emoji is refused by a limit of 300.
        assert measure_text("hi", TextCount.UTF16_UNITS) == 2
        assert measure_text(BIG_LETTER, TextCount.UTF16_UNITS) == 4

    @pytest.mark.parametrize("counted_in", list(TextCount))
    def test_every_way_of_counting_has_a_word_for_itself(
        self,
        counted_in: TextCount,
    ) -> None:
        # The words end up in the message somebody reads when their post is
        # refused, so every one of them has to have some.
        assert counted_in.in_words
        assert isinstance(counted_in.the_catch, str)


class TestHowANetworkCountsText:
    def test_a_network_counting_characters_is_unchanged(self) -> None:
        with pytest.raises(InvalidPostError) as caught:
            check_post(
                Post(text="x" * 301),
                platform="somewhere",
                features=EVERYTHING,
                limits=Limits(max_text_length=300),
            )

        assert "301 characters" in str(caught.value)

    def test_a_post_of_emoji_fits_where_letters_are_counted(self) -> None:
        # 300 letters to a person, 2,100 characters to Python. Counting
        # characters here refuses a post Bluesky would have taken, which is
        # the whole reason a network says how it counts.
        long_looking = FAMILY * 300

        check_post(
            Post(text=long_looking),
            platform="bluesky",
            features=EVERYTHING,
            limits=Limits(
                max_text_length=300,
                text_counted_in=TextCount.GRAPHEMES,
            ),
        )

    def test_the_same_post_is_refused_where_characters_are_counted(self) -> None:
        with pytest.raises(InvalidPostError, match="characters"):
            check_post(
                Post(text=FAMILY * 300),
                platform="somewhere",
                features=EVERYTHING,
                limits=Limits(max_text_length=300),
            )

    def test_too_many_letters_is_refused_and_says_so_in_letters(self) -> None:
        with pytest.raises(InvalidPostError) as caught:
            check_post(
                Post(text=BIG_LETTER * 301),
                platform="bluesky",
                features=EVERYTHING,
                limits=Limits(
                    max_text_length=300,
                    text_counted_in=TextCount.GRAPHEMES,
                ),
            )

        message = str(caught.value)
        assert "301 letters" in message
        assert "300" in message

    def test_a_post_that_fits_the_letters_can_still_be_too_many_bytes(self) -> None:
        # 250 letters, well under the 300 allowed, and 6,250 bytes written
        # out. Bluesky has both limits and this is the post that finds them.
        with pytest.raises(InvalidPostError) as caught:
            check_post(
                Post(text=FAMILY * 250),
                platform="bluesky",
                features=EVERYTHING,
                limits=Limits(
                    max_text_length=300,
                    max_text_bytes=3000,
                    text_counted_in=TextCount.GRAPHEMES,
                ),
            )

        message = str(caught.value)
        assert "bytes" in message
        assert "3000" in message

    def test_a_post_inside_both_limits_is_fine(self) -> None:
        check_post(
            Post(text=FAMILY * 100),
            platform="bluesky",
            features=EVERYTHING,
            limits=Limits(
                max_text_length=300,
                max_text_bytes=3000,
                text_counted_in=TextCount.GRAPHEMES,
            ),
        )

    def test_an_unknown_byte_limit_is_not_enforced(self) -> None:
        check_post(
            Post(text=FAMILY * 5000),
            platform="somewhere",
            features=EVERYTHING,
            limits=Limits(max_text_bytes=None),
        )

    def test_a_network_counting_in_units_refuses_a_post_python_calls_short(
        self,
    ) -> None:
        # 200 characters, and 400 units to the network. A platform using
        # Python's own count would have sent this and been refused.
        emoji = "\U0001f600" * 200

        with pytest.raises(InvalidPostError, match="400"):
            check_post(
                Post(text=emoji),
                platform="tiktok",
                features=EVERYTHING,
                limits=Limits(
                    max_text_length=300,
                    text_counted_in=TextCount.UTF16_UNITS,
                ),
            )

    def test_a_network_counting_in_bytes_refuses_a_post_python_calls_short(
        self,
    ) -> None:
        emoji = "\U0001f600" * 200

        with pytest.raises(InvalidPostError, match="800"):
            check_post(
                Post(text=emoji),
                platform="threads",
                features=EVERYTHING,
                limits=Limits(
                    max_text_length=500,
                    text_counted_in=TextCount.UTF8_BYTES,
                ),
            )


class TestTheNewLimits:
    def test_the_new_numbers_are_unknown_by_default(self) -> None:
        limits = Limits()

        assert limits.max_text_bytes is None
        assert limits.max_image_bytes is None

    def test_a_network_counts_characters_unless_it_says_otherwise(self) -> None:
        # Every platform written before this existed keeps working exactly
        # as it did, which is the point of the default.
        assert Limits().text_counted_in is TextCount.CHARACTERS
