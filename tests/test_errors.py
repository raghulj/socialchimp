"""Tests for the one set of errors every network maps onto."""

import pytest

from socialchimp import (
    AuthError,
    ConfigError,
    InvalidPostError,
    NotAllowedError,
    NotFoundError,
    NotSupportedError,
    PlatformError,
    RateLimitError,
    SocialChimpError,
    TokenExpiredError,
)


@pytest.mark.parametrize(
    "error_type",
    [
        ConfigError,
        AuthError,
        TokenExpiredError,
        NotAllowedError,
        NotFoundError,
        InvalidPostError,
    ],
)
def test_every_error_can_be_caught_as_one_type(
    error_type: type[SocialChimpError],
) -> None:
    # The whole point: catch SocialChimpError and you have caught them all,
    # whichever network raised it.
    with pytest.raises(SocialChimpError):
        raise error_type("something went wrong")


def test_an_expired_token_is_an_auth_problem() -> None:
    # Callers who want to re-run the login flow catch AuthError and get both.
    with pytest.raises(AuthError):
        raise TokenExpiredError("token ran out")


class TestRateLimitError:
    def test_it_says_how_long_to_wait(self) -> None:
        error = RateLimitError("slow down", retry_after=30.0)

        assert error.retry_after == 30.0

    def test_the_wait_is_optional_because_some_networks_do_not_say(self) -> None:
        error = RateLimitError("slow down")

        assert error.retry_after is None


class TestNotSupportedError:
    def test_the_message_names_the_network_and_the_missing_feature(self) -> None:
        # This error means "this network genuinely cannot do that", so the
        # message has to be clear enough that nobody files it as a bug.
        error = NotSupportedError(platform="bluesky", what="scheduling posts")

        assert "bluesky" in str(error)
        assert "scheduling posts" in str(error)
        assert error.platform == "bluesky"

    def test_without_a_suggestion_the_message_is_one_sentence(self) -> None:
        error = NotSupportedError(platform="bluesky", what="scheduling posts")

        assert str(error) == "bluesky does not support scheduling posts."
        assert error.suggestion is None

    def test_a_suggestion_reads_as_a_sentence_after_the_first(self) -> None:
        # Without somewhere to put it, "here is what to do instead" ends up
        # inside `what`, and the first sentence runs on for a paragraph.
        error = NotSupportedError(
            platform="pinterest",
            what="replying to pins",
            suggestion=(
                "The Pinterest API has no comments in it at all, neither "
                "reading them nor writing them."
            ),
        )

        assert str(error) == (
            "pinterest does not support replying to pins. The Pinterest API "
            "has no comments in it at all, neither reading them nor writing "
            "them."
        )
        assert error.what == "replying to pins"
        assert error.suggestion is not None


class TestPlatformError:
    def test_it_keeps_what_the_network_actually_said(self) -> None:
        error = PlatformError(
            "Instagram refused the post",
            platform="instagram",
            status_code=400,
            raw={"error": {"code": 9004}},
        )

        assert error.status_code == 400
        assert error.raw["error"]["code"] == 9004
        assert error.platform == "instagram"
