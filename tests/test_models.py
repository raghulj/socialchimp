"""Tests for the data we pass around."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from socialchimp import (
    AppCredentials,
    Attachment,
    ConfigError,
    Connection,
    Conversation,
    InvalidPostError,
    Like,
    LikeResult,
    LinkKind,
    Media,
    MediaKind,
    Message,
    Page,
    Person,
    Post,
    PostDetails,
    PostResult,
    PostState,
    PostStats,
    SocialChimpError,
    TextLink,
    Thread,
    Token,
    Unavailable,
    Visibility,
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
        with pytest.raises(ConfigError, match="timezone"):
            Token(access_token="abc", expires_at=datetime(2030, 1, 1))  # noqa: DTZ001

    def test_a_refresh_token_with_no_expiry_never_runs_out(self) -> None:
        # Most networks never expire the refresh token, so this stays unset
        # and every question about it answers no.
        token = Token(access_token="abc", refresh_token="def")

        assert token.refresh_token_expires_at is None
        assert token.refresh_token_expires_within(seconds=999_999) is False
        assert token.refresh_token_is_expired is False

    def test_a_refresh_token_can_say_when_it_runs_out(self) -> None:
        # Pinterest's lasts sixty days, and an app that cannot see that
        # only finds out on the day the account stops working.
        in_a_month = datetime.now(UTC) + timedelta(days=30)
        token = Token(
            access_token="abc",
            refresh_token="def",
            refresh_token_expires_at=in_a_month,
        )

        assert token.refresh_token_expires_at == in_a_month
        assert token.refresh_token_expires_within(seconds=60 * 60 * 24 * 60) is True
        assert token.refresh_token_expires_within(seconds=60) is False
        assert token.refresh_token_is_expired is False

    def test_a_refresh_token_past_its_expiry_is_expired(self) -> None:
        past = datetime.now(UTC) - timedelta(seconds=1)
        token = Token(
            access_token="abc",
            refresh_token="def",
            refresh_token_expires_at=past,
        )

        assert token.refresh_token_is_expired is True

    def test_a_refresh_expiry_without_a_timezone_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="timezone"):
            Token(
                access_token="abc",
                refresh_token_expires_at=datetime(2030, 1, 1),  # noqa: DTZ001
            )


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

        with pytest.raises(InvalidPostError, match="url"):
            media.read()

    def test_the_kind_is_guessed_from_the_file_name(self) -> None:
        assert Media.from_bytes(b"", filename="a.png").kind is MediaKind.IMAGE
        assert Media.from_bytes(b"", filename="a.JPEG").kind is MediaKind.IMAGE
        assert Media.from_bytes(b"", filename="a.mp4").kind is MediaKind.VIDEO
        assert Media.from_bytes(b"", filename="a.mov").kind is MediaKind.VIDEO

    def test_an_unknown_file_type_is_rejected_with_a_helpful_message(self) -> None:
        with pytest.raises(InvalidPostError, match=r"cat\.xyz") as caught:
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
        with pytest.raises(InvalidPostError, match="text or media"):
            Post()

    def test_a_publish_time_without_a_timezone_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="timezone"):
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

    def test_cid_defaults_to_none_so_old_code_keeps_working(self) -> None:
        # PostResult existed before Bluesky's content hash did. Nothing that
        # built one before 0.8.0 passed cid, so it has to default quietly.
        result = PostResult(id="1")

        assert result.cid is None

    def test_bluesky_fills_cid_alongside_the_rest(self) -> None:
        result = PostResult(id="at://did:plc:abc/app.bsky.feed.post/1", cid="bafyabc")

        assert result.cid == "bafyabc"

    def test_it_can_still_be_built_by_position(self) -> None:
        # Every existing construction site passes these by position or by
        # keyword; either way, adding cid after raw must not break it.
        result = PostResult("1", "https://example.com/1", PostState.DONE)

        assert result.id == "1"
        assert result.cid is None

    def test_a_positional_raw_still_lands_in_raw(self) -> None:
        # Code written before cid existed could call
        # PostResult(id, url, state, raw) by position. cid has to sit after
        # raw, or this fourth argument lands in cid instead of raw.
        result = PostResult("1", "https://example.com/1", PostState.DONE, {"ok": True})

        assert result.raw == {"ok": True}
        assert result.cid is None


class TestPage:
    def test_a_page_holds_its_items_and_where_to_go_next(self) -> None:
        page = Page(items=(1, 2, 3), next="cursor-2")

        assert page.items == (1, 2, 3)
        assert page.next == "cursor-2"

    def test_no_next_means_no_more_pages(self) -> None:
        page: Page[int] = Page(items=())

        assert page.next is None


class TestPerson:
    def test_a_person_carries_what_a_network_says_about_them(self) -> None:
        person = Person(
            id="did:plc:abc",
            handle="ada.bsky.social",
            display_name="Ada",
            avatar_url="https://example.com/ada.jpg",
            url="https://bsky.app/profile/ada.bsky.social",
        )

        assert person.id == "did:plc:abc"
        assert person.handle == "ada.bsky.social"
        assert person.raw == {}

    def test_meta_messaging_has_no_handle(self) -> None:
        person = Person(
            id="psid-1",
            handle=None,
            display_name=None,
            avatar_url=None,
            url=None,
        )

        assert person.handle is None


class TestTextLink:
    def test_a_mention_names_the_person_and_the_offsets(self) -> None:
        link = TextLink(
            start=0, end=4, kind=LinkKind.MENTION, target="acct-1", url=None
        )

        assert link.kind is LinkKind.MENTION
        assert link.start == 0
        assert link.end == 4

    def test_a_tag_has_no_hash_in_its_target(self) -> None:
        link = TextLink(
            start=5, end=10, kind=LinkKind.TAG, target="socialchimp", url=None
        )

        assert link.target == "socialchimp"


class TestAttachment:
    def test_an_attachment_keeps_what_it_needs_to_show_the_file(self) -> None:
        attachment = Attachment(
            kind="image",
            url="https://example.com/cat.jpg",
            preview_url="https://example.com/cat-small.jpg",
            alt_text="A cat",
            width=800,
            height=600,
        )

        assert attachment.kind == "image"
        assert attachment.alt_text == "A cat"
        assert attachment.raw == {}


class TestPostDetails:
    def test_it_keeps_a_created_time_with_a_timezone(self) -> None:
        with pytest.raises(ConfigError, match="timezone"):
            PostDetails(
                id="1",
                cid=None,
                url=None,
                author=None,
                text="hi",
                html=None,
                links=(),
                attachments=(),
                created_at=datetime(2030, 1, 1),  # noqa: DTZ001
                visibility=None,
                parent_id=None,
                root_id=None,
                reply_count=None,
                like_count=None,
                repost_count=None,
                quote_count=None,
                liked_by_me=None,
                my_like_id=None,
                is_mine=False,
                unavailable=None,
            )

    def test_a_counted_thing_the_network_never_mentioned_is_none_not_zero(self) -> None:
        details = PostDetails(
            id="1",
            cid=None,
            url="https://example.com/1",
            author=None,
            text="hi",
            html=None,
            links=(),
            attachments=(),
            created_at=datetime.now(UTC),
            visibility=Visibility.PUBLIC,
            parent_id=None,
            root_id="1",
            reply_count=None,
            like_count=None,
            repost_count=None,
            quote_count=None,
            liked_by_me=None,
            my_like_id=None,
            is_mine=True,
            unavailable=None,
        )

        assert details.reply_count is None
        assert details.raw == {}

    def test_a_placeholder_for_an_unreachable_post_says_why(self) -> None:
        details = PostDetails(
            id="1",
            cid=None,
            url=None,
            author=None,
            text="",
            html=None,
            links=(),
            attachments=(),
            created_at=None,
            visibility=None,
            parent_id=None,
            root_id="1",
            reply_count=None,
            like_count=None,
            repost_count=None,
            quote_count=None,
            liked_by_me=None,
            my_like_id=None,
            is_mine=False,
            unavailable=Unavailable.DELETED,
        )

        assert details.unavailable is Unavailable.DELETED
        assert details.author is None


class TestThread:
    def test_a_thread_holds_the_post_and_its_replies_flat(self) -> None:
        post = _a_post_details("1")
        reply = _a_post_details("2", parent_id="1", root_id="1")

        thread = Thread(post=post, replies=(reply,), complete=True)

        assert thread.post.id == "1"
        assert thread.replies[0].parent_id == "1"
        assert thread.complete is True


class TestLikeResult:
    def test_bluesky_keeps_the_like_record_uri_to_save_a_lookup(self) -> None:
        result = LikeResult(
            post_id="1", like_id="at://did:plc:abc/app.bsky.feed.like/1"
        )

        assert result.like_id is not None

    def test_mastodon_has_nothing_to_keep(self) -> None:
        result = LikeResult(post_id="1", like_id=None)

        assert result.like_id is None


class TestLike:
    def test_a_like_can_carry_when_it_happened(self) -> None:
        when = datetime.now(UTC)
        like = Like(person=_a_person(), liked_at=when)

        assert like.liked_at == when

    def test_mastodon_never_says_when_a_favourite_happened(self) -> None:
        like = Like(person=_a_person(), liked_at=None)

        assert like.liked_at is None

    def test_a_liked_at_with_no_timezone_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="timezone"):
            Like(person=_a_person(), liked_at=datetime(2030, 1, 1))  # noqa: DTZ001


class TestMessage:
    def test_a_message_needs_a_timezone_on_when_it_was_sent(self) -> None:
        with pytest.raises(ConfigError, match="timezone"):
            Message(
                id="1",
                conversation_id="c1",
                sender=_a_person(),
                text="hi",
                sent_at=datetime(2030, 1, 1),  # noqa: DTZ001
                is_mine=True,
                deleted=False,
                attachments=(),
            )

    def test_a_deleted_message_has_no_text(self) -> None:
        message = Message(
            id="1",
            conversation_id="c1",
            sender=_a_person(),
            text="",
            sent_at=datetime.now(UTC),
            is_mine=False,
            deleted=True,
            attachments=(),
        )

        assert message.text == ""
        assert message.deleted is True


class TestConversation:
    def test_mastodon_reports_unread_as_one_or_zero(self) -> None:
        conversation = Conversation(
            id="c1",
            people=(_a_person(),),
            last_message=None,
            unread_count=1,
            updated_at=datetime.now(UTC),
            can_reply_until=None,
            full_history=False,
        )

        assert conversation.unread_count == 1
        assert conversation.full_history is False

    def test_meta_has_a_deadline_to_reply_by(self) -> None:
        deadline = datetime.now(UTC)
        conversation = Conversation(
            id="c1",
            people=(_a_person(),),
            last_message=None,
            unread_count=None,
            updated_at=None,
            can_reply_until=deadline,
            full_history=True,
        )

        assert conversation.can_reply_until == deadline

    def test_an_updated_at_with_no_timezone_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="timezone"):
            Conversation(
                id="c1",
                people=(),
                last_message=None,
                unread_count=None,
                updated_at=datetime(2030, 1, 1),  # noqa: DTZ001
                can_reply_until=None,
                full_history=True,
            )

    def test_a_reply_deadline_with_no_timezone_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="timezone"):
            Conversation(
                id="c1",
                people=(),
                last_message=None,
                unread_count=None,
                updated_at=None,
                can_reply_until=datetime(2030, 1, 1),  # noqa: DTZ001
                full_history=True,
            )


def _a_person(person_id: str = "1") -> Person:
    return Person(
        id=person_id,
        handle="someone",
        display_name="Someone",
        avatar_url=None,
        url=None,
    )


def _a_post_details(
    post_id: str,
    *,
    parent_id: str | None = None,
    root_id: str | None = None,
) -> PostDetails:
    return PostDetails(
        id=post_id,
        cid=None,
        url=None,
        author=_a_person(),
        text="hi",
        html=None,
        links=(),
        attachments=(),
        created_at=datetime.now(UTC),
        visibility=Visibility.PUBLIC,
        parent_id=parent_id,
        root_id=root_id if root_id is not None else post_id,
        reply_count=None,
        like_count=None,
        repost_count=None,
        quote_count=None,
        liked_by_me=None,
        my_like_id=None,
        is_mine=False,
        unavailable=None,
    )


class TestPostStats:
    def test_a_number_nobody_told_us_is_not_a_zero(self) -> None:
        stats = PostStats(id="1")

        assert stats.likes is None
        assert stats.comments is None
        assert stats.shares is None

    def test_zero_is_kept_as_zero(self) -> None:
        # A post nobody has liked and a network that does not count likes
        # are two different answers, and only one of them is a number.
        stats = PostStats(id="1", likes=0)

        assert stats.likes == 0


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


class TestReadingAFileInPieces:
    def test_the_size_of_data_we_hold_is_known(self) -> None:
        media = Media.from_bytes(b"12345", filename="a.png")

        assert media.size == 5

    def test_the_size_of_a_file_is_read_from_disk(self, tmp_path: Path) -> None:
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"x" * 2048)

        assert Media.from_file(video).size == 2048

    def test_the_size_of_something_online_is_unknown(self) -> None:
        # We would have to download it to find out, and the point of a url
        # is usually to avoid that.
        assert Media.from_url("https://example.com/a.mp4").size is None

    def test_a_piece_can_be_read_from_data_we_hold(self) -> None:
        media = Media.from_bytes(b"0123456789", filename="a.png")

        assert media.piece(start=2, length=3) == b"234"

    def test_a_piece_can_be_read_from_a_file_without_loading_it_all(
        self, tmp_path: Path
    ) -> None:
        # This is the whole point: a four gigabyte video must not become
        # four gigabytes of memory just to be sent.
        video = tmp_path / "clip.mp4"
        video.write_bytes(bytes(range(256)) * 8)

        assert Media.from_file(video).piece(start=256, length=4) == bytes(range(4))

    def test_a_piece_past_the_end_comes_back_short(self, tmp_path: Path) -> None:
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"12345")

        assert Media.from_file(video).piece(start=3, length=100) == b"45"

    def test_reading_a_piece_of_something_online_is_refused(self) -> None:
        with pytest.raises(InvalidPostError, match="url"):
            Media.from_url("https://example.com/a.mp4").piece(start=0, length=1)


# Everything in this module refuses by raising, and every one of those
# refusals is written out here. An app is told that catching
# `SocialChimpError` catches everything socialchimp reports; before 0.3.0
# these five raised a bare `ValueError` and went straight past it.
REFUSALS: list[tuple[str, Callable[[], object], type[SocialChimpError]]] = [
    ("a post with nothing in it", Post, InvalidPostError),
    (
        "a publish time with no timezone",
        lambda: Post(text="hi", publish_at=datetime(2030, 1, 1)),  # noqa: DTZ001
        ConfigError,
    ),
    (
        "an expiry with no timezone",
        lambda: Token(access_token="abc", expires_at=datetime(2030, 1, 1)),  # noqa: DTZ001
        ConfigError,
    ),
    (
        "a file ending nobody recognises",
        lambda: Media.from_bytes(b"", filename="cat.xyz"),
        InvalidPostError,
    ),
    (
        "reading something that is only online",
        lambda: Media.from_url("https://example.com/photo.jpg").read(),
        InvalidPostError,
    ),
    (
        "a piece of something that is only online",
        lambda: Media.from_url("https://example.com/a.mp4").piece(start=0, length=1),
        InvalidPostError,
    ),
]


class TestEveryRefusalHereCanBeCaught:
    @pytest.mark.parametrize(("what", "refuse", "expected"), REFUSALS)
    def test_it_is_a_socialchimp_error(
        self,
        what: str,
        refuse: Callable[[], object],
        expected: type[SocialChimpError],
    ) -> None:
        # The one an app is told to catch. A bare ValueError here walks
        # past `except SocialChimpError` and crashes the app.
        with pytest.raises(expected):
            refuse()

    @pytest.mark.parametrize(("what", "refuse", "expected"), REFUSALS)
    def test_it_is_still_a_value_error(
        self,
        what: str,
        refuse: Callable[[], object],
        expected: type[SocialChimpError],
    ) -> None:
        # Each of these was a ValueError before 0.3.0, and that is
        # documented behaviour in a published library, so it stays one.
        with pytest.raises(ValueError):
            refuse()
