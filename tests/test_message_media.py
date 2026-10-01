"""Tests for sending pictures, video, sound and files in direct messages.

The parts every network shares: the new kinds of file, the limits a network
puts on a message, checking a message against them, and the account call.
Each network's own sending is tested in its own file.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from socialchimp import (
    AttachmentRule,
    Connection,
    Feature,
    InMemoryStorage,
    InvalidPostError,
    Limits,
    Media,
    MediaKind,
    Message,
    MessageLimits,
    NotSupportedError,
    Person,
    Post,
    SocialChimp,
    TextCount,
    Token,
    check_message,
)
from socialchimp.features import check_post
from socialchimp.platform import CanSendMessageMedia
from socialchimp.testing import FakePlatform

SIGNED = (
    "https://files.example/uploads/menu.jpg"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=abc123"
)


def a_person(person_id: str = "p1") -> Person:
    return Person(
        id=person_id, handle=None, display_name=None, avatar_url=None, url=None
    )


def instagram_like() -> MessageLimits:
    """Rules shaped like Instagram's: web addresses only, kinds not mixed."""
    return MessageLimits(
        max_text_bytes=1000,
        max_attachments=10,
        attachments=(
            AttachmentRule(
                kind=MediaKind.IMAGE,
                mime_types=("image/png", "image/jpeg"),
                max_bytes=8_000_000,
                max_count=10,
            ),
            AttachmentRule(
                kind=MediaKind.FILE,
                mime_types=("application/pdf",),
                max_bytes=25_000_000,
                max_count=1,
            ),
        ),
        one_kind_at_a_time=True,
        takes_web_addresses=True,
        takes_files=False,
    )


# ---------------------------------------------------------------------------
# The new kinds of file, and web addresses that carry a signature
# ---------------------------------------------------------------------------


class TestMediaKinds:
    @pytest.mark.parametrize("name", ["note.mp3", "note.m4a", "note.aac", "note.wav"])
    def test_sound_is_audio(self, name: str) -> None:
        assert Media.from_url(f"https://files.example/{name}").kind is MediaKind.AUDIO

    def test_a_pdf_is_a_file(self) -> None:
        assert Media.from_url("https://files.example/menu.pdf").kind is MediaKind.FILE

    def test_a_signed_address_is_read_by_its_path_not_its_signature(self) -> None:
        media = Media.from_url(SIGNED)

        assert media.kind is MediaKind.IMAGE
        assert media.filename == "menu.jpg"
        assert media.content_type == "image/jpeg"
        assert media.url == SIGNED

    def test_an_address_with_no_ending_can_say_its_type(self) -> None:
        media = Media.from_url(
            "https://files.example/download/42", mime_type="application/pdf"
        )

        assert media.kind is MediaKind.FILE
        assert media.content_type == "application/pdf"

    @pytest.mark.parametrize(
        ("mime_type", "kind"),
        [
            ("image/png", MediaKind.IMAGE),
            ("video/mp4", MediaKind.VIDEO),
            ("audio/mp4", MediaKind.AUDIO),
            ("application/zip", MediaKind.FILE),
        ],
    )
    def test_a_type_says_what_kind_it_is(self, mime_type: str, kind: MediaKind) -> None:
        media = Media.from_bytes(b"x", filename="upload", mime_type=mime_type)

        assert media.kind is kind
        assert media.content_type == mime_type

    def test_a_kind_given_outright_still_wins(self) -> None:
        media = Media.from_file(
            "clips/clip.bin", kind=MediaKind.AUDIO, mime_type="audio/wav"
        )

        assert media.kind is MediaKind.AUDIO
        assert media.content_type == "audio/wav"

    def test_nothing_to_go_on_is_still_refused(self) -> None:
        with pytest.raises(InvalidPostError, match="MediaKind"):
            Media.from_url("https://files.example/download/42")

    @pytest.mark.parametrize(
        ("kind", "expected"),
        [(MediaKind.AUDIO, "audio/mpeg"), (MediaKind.FILE, "application/octet-stream")],
    )
    def test_a_type_nobody_said_falls_back_by_kind(
        self, kind: MediaKind, expected: str
    ) -> None:
        media = Media.from_bytes(b"x", filename="upload", kind=kind)

        assert media.content_type == expected


class TestPostsStillTakeOnlyPicturesAndVideo:
    @pytest.mark.parametrize(
        ("name", "what"), [("note.mp3", "sound"), ("menu.pdf", "files")]
    )
    def test_a_post_with_sound_or_a_file_is_refused(self, name: str, what: str) -> None:
        post = Post(text="hi", media=(Media.from_url(f"https://files.example/{name}"),))

        with pytest.raises(NotSupportedError, match=what):
            check_post(
                post,
                platform="anywhere",
                features=Feature.POST_TEXT | Feature.POST_IMAGE | Feature.POST_VIDEO,
                limits=Limits(),
            )


# ---------------------------------------------------------------------------
# Limits on a message
# ---------------------------------------------------------------------------


class TestMessageLimits:
    def test_limits_say_nothing_about_messages_unless_the_network_does(self) -> None:
        assert Limits().messages is None

    def test_a_network_with_no_attachments_says_so(self) -> None:
        limits = MessageLimits(max_text_length=1000)

        assert limits.attachments == ()
        assert limits.max_attachments == 0
        assert limits.text_counted_in is TextCount.CHARACTERS
        assert limits.rule_for(MediaKind.IMAGE) is None

    def test_a_rule_can_be_found_by_kind(self) -> None:
        rule = instagram_like().rule_for(MediaKind.FILE)

        assert rule is not None
        assert rule.mime_types == ("application/pdf",)


class TestCheckingAMessage:
    def test_words_alone_inside_the_limits_pass(self) -> None:
        check_message("hello", (), platform="insta", limits=instagram_like())

    def test_no_words_and_nothing_attached_is_refused(self) -> None:
        with pytest.raises(InvalidPostError, match="empty"):
            check_message("  ", (), platform="insta", limits=instagram_like())

    def test_an_attachment_alone_needs_no_words(self) -> None:
        check_message(
            "", (Media.from_url(SIGNED),), platform="insta", limits=instagram_like()
        )

    def test_too_many_bytes_is_refused(self) -> None:
        with pytest.raises(InvalidPostError, match="1000 bytes"):
            check_message(
                "\N{BREAD}" * 251, (), platform="insta", limits=instagram_like()
            )

    def test_too_many_letters_is_refused(self) -> None:
        limits = MessageLimits(max_text_length=3, text_counted_in=TextCount.GRAPHEMES)

        with pytest.raises(InvalidPostError, match="3 letters"):
            check_message("abcd", (), platform="sky", limits=limits)

    def test_a_network_with_no_attachments_refuses_one(self) -> None:
        with pytest.raises(NotSupportedError, match="attachments"):
            check_message(
                "hi",
                (Media.from_url(SIGNED),),
                platform="sky",
                limits=MessageLimits(),
            )

    def test_a_kind_the_network_does_not_take_is_refused(self) -> None:
        clip = Media.from_url("https://files.example/clip.mp4")

        with pytest.raises(NotSupportedError, match="video"):
            check_message("", (clip,), platform="insta", limits=instagram_like())

    def test_too_many_of_one_kind_is_refused(self) -> None:
        menus = tuple(Media.from_url(f"https://f.example/{n}.pdf") for n in range(2))

        with pytest.raises(InvalidPostError, match="at most 1"):
            check_message("", menus, platform="insta", limits=instagram_like())

    def test_too_many_altogether_is_refused(self) -> None:
        limits = MessageLimits(
            max_attachments=1,
            attachments=(AttachmentRule(kind=MediaKind.IMAGE),),
            takes_web_addresses=True,
        )
        pictures = (Media.from_url(SIGNED), Media.from_url(SIGNED))

        with pytest.raises(InvalidPostError, match="at most 1"):
            check_message("", pictures, platform="insta", limits=limits)

    def test_mixing_kinds_is_refused_where_the_network_will_not(self) -> None:
        mixed = (Media.from_url(SIGNED), Media.from_url("https://f.example/m.pdf"))

        with pytest.raises(InvalidPostError, match="one kind"):
            check_message("", mixed, platform="insta", limits=instagram_like())

    def test_a_type_the_network_does_not_take_is_refused(self) -> None:
        gif = Media.from_url("https://files.example/dance.gif", mime_type="image/gif")

        with pytest.raises(InvalidPostError, match="image/gif"):
            check_message("", (gif,), platform="insta", limits=instagram_like())

    def test_a_given_type_is_read_whatever_its_case_and_extras(self) -> None:
        pdf = Media.from_url(
            "https://f.example/m", mime_type="Application/PDF; charset=binary"
        )

        check_message("", (pdf,), platform="insta", limits=instagram_like())

    def test_a_type_only_guessed_from_the_name_is_left_to_the_network(self) -> None:
        # Python's guess for a name differs between machines - one calls an
        # .m4a "audio/mp4a-latm" - so a guess never refuses a file the
        # network would have taken.
        gif = Media.from_url("https://files.example/dance.gif")

        check_message("", (gif,), platform="insta", limits=instagram_like())

    def test_a_type_that_cannot_be_told_is_left_to_the_network(self) -> None:
        unknown = Media.from_url("https://f.example/x", kind=MediaKind.IMAGE)

        check_message("", (unknown,), platform="insta", limits=instagram_like())

    def test_a_file_too_big_is_refused_when_its_size_is_known(self) -> None:
        limits = MessageLimits(
            max_attachments=1,
            attachments=(AttachmentRule(kind=MediaKind.IMAGE, max_bytes=3),),
            takes_files=True,
        )
        big = Media.from_bytes(b"1234", filename="a.png")

        with pytest.raises(InvalidPostError, match="4 bytes"):
            check_message("", (big,), platform="masto", limits=limits)

    def test_a_web_address_is_refused_where_the_network_needs_the_file(self) -> None:
        limits = MessageLimits(
            max_attachments=1,
            attachments=(AttachmentRule(kind=MediaKind.IMAGE),),
            takes_files=True,
        )

        with pytest.raises(InvalidPostError, match="from_bytes"):
            check_message(
                "", (Media.from_url(SIGNED),), platform="masto", limits=limits
            )

    def test_a_file_is_refused_where_the_network_needs_a_web_address(self) -> None:
        on_disk = Media.from_bytes(b"x", filename="a.png")

        with pytest.raises(NotSupportedError, match="from_url"):
            check_message("", (on_disk,), platform="insta", limits=instagram_like())


# ---------------------------------------------------------------------------
# One call that sent several messages
# ---------------------------------------------------------------------------


class TestAlsoSent:
    def test_a_message_sent_alone_has_nothing_alongside_it(self) -> None:
        message = Message(
            id="m1",
            conversation_id="c1",
            sender=a_person(),
            text="hi",
            sent_at=datetime(2026, 1, 1, tzinfo=UTC),
            is_mine=True,
            deleted=False,
            attachments=(),
        )

        assert message.also_sent == ()


# ---------------------------------------------------------------------------
# The account call, and the fake
# ---------------------------------------------------------------------------


class TextOnlyMessenger(FakePlatform):
    """Messages, but no attachments - the way Bluesky is."""

    name = "text-only"

    def __init__(self) -> None:
        super().__init__(features=FakePlatform().features & ~Feature.MESSAGE_MEDIA)


async def an_account_on(platform: FakePlatform) -> tuple[SocialChimp, Connection]:
    connection = Connection(
        id="conn-1",
        platform=platform.name,
        host=None,
        account_id="me",
        account_name="me",
        token=Token(access_token="t"),
    )
    storage = InMemoryStorage()
    await storage.save_connection(connection)
    return SocialChimp(storage, platforms={platform.name: platform}), connection


class TestSendingThroughAnAccount:
    async def test_the_fake_says_it_can_send_attachments(self) -> None:
        platform = FakePlatform()

        assert Feature.MESSAGE_MEDIA in platform.features
        assert isinstance(platform, CanSendMessageMedia)

    async def test_media_goes_with_the_message(self) -> None:
        platform = FakePlatform()
        conversation = platform.add_conversation((a_person(),))
        sc, _ = await an_account_on(platform)

        sent = await sc.account("conn-1").send_message(
            conversation.id, "The menu", media=(Media.from_url(SIGNED),)
        )

        assert sent.text == "The menu"
        assert [(a.kind, a.url) for a in sent.attachments] == [("image", SIGNED)]
        assert platform.sent_messages[-1] == sent

    async def test_sound_and_files_read_back_with_their_kind(self) -> None:
        platform = FakePlatform()
        conversation = platform.add_conversation((a_person(),))
        sc, _ = await an_account_on(platform)

        sent = await sc.account("conn-1").send_message(
            conversation.id,
            "",
            media=(
                Media.from_url("https://f.example/a.mp3"),
                Media.from_url("https://f.example/b.pdf"),
            ),
        )

        assert [a.kind for a in sent.attachments] == ["audio", "file"]

    async def test_no_media_still_goes_the_old_way(self) -> None:
        platform = FakePlatform()
        conversation = platform.add_conversation((a_person(),))
        sc, _ = await an_account_on(platform)

        sent = await sc.account("conn-1").send_message(conversation.id, "hi")

        assert sent.attachments == ()

    async def test_a_network_without_attachments_refuses_them_by_name(self) -> None:
        platform = TextOnlyMessenger()
        conversation = platform.add_conversation((a_person(),))
        sc, _ = await an_account_on(platform)

        with pytest.raises(NotSupportedError, match="attachments"):
            await sc.account("conn-1").send_message(
                conversation.id, "hi", media=(Media.from_url(SIGNED),)
            )

    async def test_a_network_without_attachments_still_sends_words(self) -> None:
        platform = TextOnlyMessenger()
        conversation = platform.add_conversation((a_person(),))
        sc, _ = await an_account_on(platform)

        sent = await sc.account("conn-1").send_message(conversation.id, "hi")

        assert sent.text == "hi"

    async def test_the_fake_checks_against_its_own_message_limits(self) -> None:
        platform = FakePlatform(
            limits=Limits(messages=MessageLimits(max_text_length=2))
        )
        conversation = platform.add_conversation((a_person(),))

        with pytest.raises(InvalidPostError):
            await platform.send_message_with_media(
                platform.connection(), conversation.id, "hello", ()
            )

    async def test_the_fake_can_be_told_to_fail(self) -> None:
        platform = FakePlatform()
        conversation = platform.add_conversation((a_person(),))
        platform.fail_next(
            "send_message_with_media", NotSupportedError(platform="fake", what="x")
        )

        with pytest.raises(NotSupportedError):
            await platform.send_message_with_media(
                platform.connection(), conversation.id, "hello", ()
            )
