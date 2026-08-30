"""Tests for the data we pass around."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from socialchimp import (
    AppCredentials,
    Connection,
    Media,
    MediaKind,
    Post,
    PostResult,
    PostState,
    Token,
)


class TestToken:
    def test_a_token_without_an_expiry_never_expires(self) -> None:
        token = Token(access_token="abc")

        assert token.expires_within(seconds=999_999) is False
        assert token.is_expired is False

    def test_a_token_expiring_soon_is_reported_early(self) -> None:
        soon = datetime.now(UTC) + timedelta(seconds=30)
        token = Token(access_token="abc", expires_at=soon)

        assert token.expires_within(seconds=60) is True
        assert token.expires_within(seconds=10) is False

    def test_a_token_past_its_expiry_is_expired(self) -> None:
        past = datetime.now(UTC) - timedelta(seconds=1)
        token = Token(access_token="abc", expires_at=past)

        assert token.is_expired is True

    def test_the_access_token_is_hidden_when_printed(self) -> None:
        # Tokens end up in logs and tracebacks. They must not leak there.
        token = Token(access_token="super-secret", refresh_token="also-secret")

        printed = repr(token)

        assert "super-secret" not in printed
        assert "also-secret" not in printed
        assert "Token(" in printed

    def test_an_expiry_without_a_timezone_is_rejected(self) -> None:
        # A naive datetime silently compares wrong against an aware one.
        with pytest.raises(ValueError, match="timezone"):
            Token(access_token="abc", expires_at=datetime(2030, 1, 1))  # noqa: DTZ001


class TestMedia:
    def test_media_can_be_built_from_a_file(self, tmp_path: Path) -> None:
        picture = tmp_path / "cat.png"
        picture.write_bytes(b"not really a png")

        media = Media.from_file(picture, alt_text="A cat")

        assert media.kind is MediaKind.IMAGE
        assert media.filename == "cat.png"
        assert media.alt_text == "A cat"
        assert media.read() == b"not really a png"

    def test_media_can_be_built_from_bytes(self) -> None:
        media = Media.from_bytes(b"data", filename="clip.mp4")

        assert media.kind is MediaKind.VIDEO
        assert media.read() == b"data"

    def test_media_can_point_at_a_url(self) -> None:
        media = Media.from_url("https://example.com/photo.jpg")

        assert media.kind is MediaKind.IMAGE
        assert media.url == "https://example.com/photo.jpg"

    def test_reading_a_url_without_downloading_it_is_refused(self) -> None:
        media = Media.from_url("https://example.com/photo.jpg")

        with pytest.raises(ValueError, match="url"):
            media.read()

    def test_the_kind_is_guessed_from_the_file_name(self) -> None:
        assert Media.from_bytes(b"", filename="a.png").kind is MediaKind.IMAGE
        assert Media.from_bytes(b"", filename="a.JPEG").kind is MediaKind.IMAGE
        assert Media.from_bytes(b"", filename="a.mp4").kind is MediaKind.VIDEO
        assert Media.from_bytes(b"", filename="a.mov").kind is MediaKind.VIDEO

    def test_an_unknown_file_type_is_rejected_with_a_helpful_message(self) -> None:
        with pytest.raises(ValueError, match=r"cat\.xyz") as caught:
            Media.from_bytes(b"", filename="cat.xyz")

        # The message should say what to do, not just what went wrong.
        assert "kind=" in str(caught.value)

    def test_the_kind_can_be_given_when_the_name_does_not_say(self) -> None:
        media = Media.from_bytes(b"", filename="cat.xyz", kind=MediaKind.IMAGE)

        assert media.kind is MediaKind.IMAGE


class TestPost:
    def test_a_post_is_text_by_default(self) -> None:
        post = Post(text="hello")

        assert post.text == "hello"
        assert post.media == ()
        assert post.options == {}

    def test_a_post_carries_options_meant_for_one_network(self) -> None:
        # Pinterest needs a board. Nothing else does. This is where it goes.
        post = Post(text="hi", options={"board_id": "123"})

        assert post.options["board_id"] == "123"

    def test_a_post_needs_either_text_or_media(self) -> None:
        with pytest.raises(ValueError, match="text or media"):
            Post()

    def test_a_publish_time_without_a_timezone_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            Post(text="hi", publish_at=datetime(2030, 1, 1))  # noqa: DTZ001


class TestConnection:
    def test_a_connection_holds_the_account_and_its_token(self) -> None:
        connection = Connection(
            id="conn-1",
            platform="mastodon",
            host="mastodon.social",
            account_id="42",
            account_name="@someone@mastodon.social",
            token=Token(access_token="abc"),
        )

        assert connection.platform == "mastodon"
        assert connection.host == "mastodon.social"

    def test_a_connection_with_a_new_token_is_a_copy(self) -> None:
        # Connections never change in place, so a refresh cannot half-apply.
        original = Connection(
            id="conn-1",
            platform="mastodon",
            host=None,
            account_id="42",
            account_name="someone",
            token=Token(access_token="old"),
        )

        updated = original.with_token(Token(access_token="new"))

        assert updated.token.access_token == "new"
        assert original.token.access_token == "old"
        assert updated.id == original.id

    def test_the_token_is_hidden_when_a_connection_is_printed(self) -> None:
        connection = Connection(
            id="conn-1",
            platform="mastodon",
            host=None,
            account_id="42",
            account_name="someone",
            token=Token(access_token="super-secret"),
        )

        assert "super-secret" not in repr(connection)


class TestAppCredentials:
    def test_credentials_are_keyed_by_platform_and_host(self) -> None:
        # Mastodon needs its own app on every server, so the host is part
        # of the key. Networks with one server leave it as None.
        credentials = AppCredentials(
            platform="mastodon",
            host="mastodon.social",
            client_id="id",
            client_secret="secret",
        )

        assert credentials.key == ("mastodon", "mastodon.social")

    def test_the_secret_is_hidden_when_printed(self) -> None:
        credentials = AppCredentials(
            platform="mastodon",
            host=None,
            client_id="id",
            client_secret="super-secret",
        )

        assert "super-secret" not in repr(credentials)


class TestPostResult:
    def test_a_finished_post_reports_done(self) -> None:
        result = PostResult(id="1", url="https://example.com/1")

        assert result.state is PostState.DONE
        assert result.is_done is True

    def test_a_post_still_being_processed_is_not_done(self) -> None:
        # TikTok and YouTube keep working after we hand the upload over.
        result = PostResult(id="1", url=None, state=PostState.PROCESSING)

        assert result.is_done is False


class TestContentType:
    def test_the_type_is_worked_out_from_the_file_name(self) -> None:
        assert Media.from_bytes(b"", filename="a.png").content_type == "image/png"
        assert Media.from_bytes(b"", filename="a.mp4").content_type == "video/mp4"

    def test_an_unrecognised_name_falls_back_by_kind(self) -> None:
        # We still have to send something, so pick the common case rather
        # than failing the upload over a missing file extension.
        picture = Media.from_bytes(b"", filename="photo", kind=MediaKind.IMAGE)
        video = Media.from_bytes(b"", filename="clip", kind=MediaKind.VIDEO)

        assert picture.content_type == "image/jpeg"
        assert video.content_type == "video/mp4"
