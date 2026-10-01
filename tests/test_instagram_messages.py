"""Tests for Instagram direct messages: reading, sending and what Meta pushes."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from socialchimp import (
    BlockedError,
    ConfigError,
    Connection,
    Feature,
    InvalidPostError,
    Media,
    MediaKind,
    MessageEventKind,
    MissingPermissionError,
    NotAllowedError,
    NotFoundError,
    NotSupportedError,
    RateLimitError,
    ReplyWindowClosedError,
    SocialChimpError,
    Token,
    UpdateKind,
    check_message,
)
from socialchimp.http import Retries
from socialchimp.platform import (
    CanMessage,
    CanReadPushedMessages,
    CanSendMessageMedia,
    CanStartConversations,
)
from socialchimp.platforms import instagram as instagram_module
from socialchimp.platforms.instagram import (
    DEFAULT_SCOPES,
    IG_GRAPH_API,
    MESSAGE_LIMITS,
    MESSAGES_PER_CONVERSATION,
    MOST_MESSAGE_BYTES,
    MOST_MESSAGES_WITH_DETAILS,
    InstagramPlatform,
)

FIXTURES = Path(__file__).parent / "fixtures" / "instagram"

IG_ID = "17841400000000000"
IG_NAME = "fridgedoor"
ADA = "1234567890123456"
SAM_NAME = "sourdough.sam"
ONCE = Retries(attempts=1)
NOW = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
DAY = timedelta(hours=24)
WEEK = timedelta(days=7)


def fixture(name: str) -> dict[str, Any]:
    """Read one of the Instagram fixture files."""
    found: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return found


def pushed(name: str) -> bytes:
    """One webhook body from `webhook_messaging.json`, as Meta sends it."""
    return json.dumps(fixture("webhook_messaging.json")[name]).encode()


def meta_error(name: str) -> httpx.Response:
    """One of the error replies from `errors.json`."""
    found = fixture("errors.json")[name]
    return httpx.Response(found["http_status"], json=found["body"])


def at(text: str) -> datetime:
    """Read a time the way Meta writes it."""
    return datetime.fromisoformat(text)


@pytest.fixture
def platform() -> InstagramPlatform:
    return InstagramPlatform(retries=ONCE)


@pytest.fixture
def agent_platform() -> InstagramPlatform:
    return InstagramPlatform(retries=ONCE, human_agent=True)


@pytest.fixture
def account() -> Connection:
    return Connection(
        id=f"instagram:{IG_ID}",
        platform="instagram",
        host=None,
        account_id=IG_ID,
        account_name=IG_NAME,
        token=Token(access_token="access-token"),
        scopes=DEFAULT_SCOPES,
        extra={"instagram_id": IG_ID, "username": IG_NAME},
    )


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(instagram_module, "_now", lambda: NOW)


@pytest.fixture
def network() -> Iterator[respx.Router]:
    with respx.mock(base_url=IG_GRAPH_API) as mocked:
        yield mocked


def conversations_without_notes() -> dict[str, Any]:
    reply: dict[str, Any] = fixture("conversations.json")
    reply.pop("_notes", None)
    return reply


def one_conversation() -> dict[str, Any]:
    """The first conversation, as `read_messages` asks for it by person."""
    first = conversations_without_notes()["data"][0]
    return {"data": [first]}


# ---------------------------------------------------------------------------
# What it says it can do
# ---------------------------------------------------------------------------


class TestWhatItSaysItCanDo:
    def test_it_reads_and_sends_direct_messages(
        self, platform: InstagramPlatform
    ) -> None:
        assert Feature.MESSAGES in platform.features
        assert isinstance(platform, CanMessage)

    def test_it_cannot_start_a_conversation(self, platform: InstagramPlatform) -> None:
        # Meta only lets a business answer: the person has to write first.
        assert Feature.START_CONVERSATIONS not in platform.features
        assert not isinstance(platform, CanStartConversations)

    def test_it_reads_pushed_messages(self, platform: InstagramPlatform) -> None:
        assert isinstance(platform, CanReadPushedMessages)

    def test_the_messages_scope_is_still_asked_for(self) -> None:
        assert "instagram_business_manage_messages" in DEFAULT_SCOPES

    def test_it_only_asks_for_what_meta_will_give_details_of(self) -> None:
        assert MOST_MESSAGES_WITH_DETAILS == 20
        assert MESSAGES_PER_CONVERSATION <= MOST_MESSAGES_WITH_DETAILS


# ---------------------------------------------------------------------------
# Reading conversations
# ---------------------------------------------------------------------------


class TestReadingConversations:
    async def test_it_asks_for_instagram_conversations_with_their_messages(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        route = network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=conversations_without_notes())
        )

        await platform.read_conversations(account, limit=2, after="cursor-1")

        sent = route.calls[0].request.url.params
        assert sent["platform"] == "instagram"
        assert sent["limit"] == "2"
        assert sent["after"] == "cursor-1"
        assert "participants" in sent["fields"]
        assert f"messages.limit({MESSAGES_PER_CONVERSATION})" in sent["fields"]
        assert route.calls[0].request.headers["Authorization"] == "Bearer access-token"

    async def test_a_conversation_is_named_after_the_other_person(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        # Webhooks only ever name the person, never Meta's conversation id,
        # so this is the id both ways of taking messages in can agree on.
        network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=conversations_without_notes())
        )

        page = await platform.read_conversations(account)

        first = page.items[0]
        raw = conversations_without_notes()["data"][0]
        assert first.id == ADA
        assert [person.id for person in first.people] == [ADA]
        assert first.people[0].handle == "ada.bakes"
        assert first.people[0].url == "https://www.instagram.com/ada.bakes"
        assert first.updated_at == at("2026-09-28T14:11:00+00:00")
        assert first.unread_count is None
        assert first.full_history is False
        assert first.raw["id"] == raw["id"]

    async def test_the_newest_message_is_the_last_one(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=conversations_without_notes())
        )

        page = await platform.read_conversations(account)

        last = page.items[0].last_message
        assert last is not None
        assert last.text == "Yes, holding one for you until 5pm."
        assert last.is_mine is True
        assert last.conversation_id == ADA

    async def test_the_reply_window_runs_24_hours_from_their_last_message(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=conversations_without_notes())
        )

        page = await platform.read_conversations(account)

        # Our own 14:11 reply does not open the window again; theirs at
        # 14:10 is what counts.
        assert page.items[0].can_reply_until == at("2026-09-28T14:10:00+00:00") + DAY

    async def test_with_the_human_agent_tag_it_runs_seven_days(
        self,
        agent_platform: InstagramPlatform,
        account: Connection,
        network: respx.Router,
    ) -> None:
        network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=conversations_without_notes())
        )

        page = await agent_platform.read_conversations(account)

        assert page.items[0].can_reply_until == at("2026-09-28T14:10:00+00:00") + WEEK

    async def test_the_next_page_is_metas_cursor(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        reply = conversations_without_notes()
        network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=reply)
        )

        page = await platform.read_conversations(account)

        assert page.next == reply["paging"]["cursors"]["after"]

    async def test_there_is_no_next_page_when_meta_gives_no_next(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        reply = conversations_without_notes()
        del reply["paging"]["next"]
        network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=reply)
        )

        page = await platform.read_conversations(account)

        assert page.next is None

    async def test_an_empty_list_is_an_empty_page(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        page = await platform.read_conversations(account)

        assert page.items == ()
        assert page.next is None

    async def test_when_none_of_the_messages_are_theirs_the_window_is_a_ceiling(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        # Their last message is older than every message we were given, so
        # the window closed at the latest 24 hours after the oldest of them.
        mine = {
            "id": "m2",
            "created_time": "2026-09-28T10:00:00+0000",
            "from": {"username": IG_NAME, "id": IG_ID},
            "message": "Still there?",
        }
        older = {**mine, "id": "m1", "created_time": "2026-09-28T09:00:00+0000"}
        reply = {
            "data": [
                {
                    "id": "conv-1",
                    "updated_time": "2026-09-28T10:00:00+0000",
                    "participants": {
                        "data": [
                            {"username": IG_NAME, "id": IG_ID},
                            {"username": "ada.bakes", "id": ADA},
                        ]
                    },
                    "messages": {"data": [mine, older]},
                }
            ]
        }
        network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=reply)
        )

        page = await platform.read_conversations(account)

        assert page.items[0].can_reply_until == at("2026-09-28T09:00:00+00:00") + DAY

    async def test_with_no_messages_at_all_the_window_runs_from_the_last_change(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        reply = {
            "data": [
                {
                    "id": "conv-1",
                    "updated_time": "2026-09-28T10:00:00+0000",
                    "participants": {"data": [{"username": "ada.bakes", "id": ADA}]},
                }
            ]
        }
        network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=reply)
        )

        page = await platform.read_conversations(account)

        assert page.items[0].last_message is None
        assert page.items[0].can_reply_until == at("2026-09-28T10:00:00+00:00") + DAY

    async def test_with_no_time_at_all_there_is_no_window_to_give(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        reply = {
            "data": [
                {
                    "id": "conv-1",
                    "participants": {"data": [{"username": "ada.bakes", "id": ADA}]},
                    "messages": {"data": "not a list"},
                }
            ]
        }
        network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=reply)
        )

        page = await platform.read_conversations(account)

        assert page.items[0].updated_at is None
        assert page.items[0].can_reply_until is None

    async def test_a_conversation_with_nobody_else_in_it_is_left_out(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        # There is nobody to send to, so there is nothing an inbox can do.
        reply = {
            "data": [
                {
                    "id": "conv-1",
                    "participants": {"data": [{"username": IG_NAME, "id": IG_ID}]},
                },
                {"id": "conv-2", "participants": "not a list"},
            ]
        }
        network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=reply)
        )

        page = await platform.read_conversations(account)

        assert page.items == ()

    async def test_the_account_is_recognised_by_its_name_as_well_as_its_id(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        # Instagram has two ids for one account; the username is the same
        # whichever it uses.
        reply = {
            "data": [
                {
                    "id": "conv-1",
                    "participants": {
                        "data": [
                            {"username": "FridgeDoor", "id": "another-id"},
                            {"username": "ada.bakes", "id": ADA},
                        ]
                    },
                }
            ]
        }
        network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=reply)
        )

        page = await platform.read_conversations(account)

        assert page.items[0].id == ADA


# ---------------------------------------------------------------------------
# Reading the messages in one conversation
# ---------------------------------------------------------------------------


class TestReadingMessages:
    async def test_it_finds_the_conversation_by_the_person(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        route = network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=one_conversation())
        )

        await platform.read_messages(account, ADA)

        sent = route.calls[0].request.url.params
        assert sent["platform"] == "instagram"
        assert sent["user_id"] == ADA
        assert f"messages.limit({MOST_MESSAGES_WITH_DETAILS})" in sent["fields"]

    async def test_a_smaller_limit_asks_for_fewer(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        route = network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=one_conversation())
        )

        await platform.read_messages(account, ADA, limit=5)

        assert "messages.limit(5)" in route.calls[0].request.url.params["fields"]

    async def test_a_bigger_limit_is_capped_at_what_meta_will_give(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        route = network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=one_conversation())
        )

        await platform.read_messages(account, ADA, limit=500)

        assert "messages.limit(20)" in route.calls[0].request.url.params["fields"]

    async def test_messages_come_back_newest_first_with_no_next_page(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=one_conversation())
        )

        page = await platform.read_messages(account, ADA)

        times = [message.sent_at for message in page.items]
        assert times == sorted(times, reverse=True)
        assert len(page.items) == 11
        assert page.next is None
        assert {message.conversation_id for message in page.items} == {ADA}

    async def test_who_sent_each_one_is_worked_out(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=one_conversation())
        )

        page = await platform.read_messages(account, ADA)

        newest, oldest = page.items[0], page.items[-1]
        assert newest.is_mine is True
        assert newest.sender.id == IG_ID
        assert oldest.is_mine is False
        assert oldest.sender.handle == "ada.bakes"
        assert oldest.text == "Hi! Do you still have the walnut loaf today?"
        assert oldest.deleted is False

    async def test_every_kind_of_attachment_comes_through_typed(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=one_conversation())
        )

        page = await platform.read_messages(account, ADA)

        kinds = [
            attachment.kind
            for message in page.items
            for attachment in message.attachments
        ]
        assert kinds == [
            "story_reply",
            "story_mention",
            "share",
            "file",
            "audio",
            "video",
            "image",
        ]
        every = {
            attachment.kind: attachment
            for message in page.items
            for attachment in message.attachments
        }
        assert all(attachment.url for attachment in every.values())
        assert every["image"].width == 1080
        assert every["image"].height == 1350
        assert every["image"].preview_url is not None
        assert every["video"].preview_url is not None
        assert every["share"].url == "https://www.instagram.com/p/DAbCdEfGhIj/"

    async def test_a_message_instagram_cannot_show_still_comes_through(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=one_conversation())
        )

        page = await platform.read_messages(account, ADA)

        unsupported = page.items[1]
        assert unsupported.text == ""
        assert unsupported.raw["is_unsupported"] is True

    async def test_odd_attachments_are_kept_rather_than_dropped(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        message = {
            "id": "m1",
            "created_time": "2026-09-28T10:00:00+0000",
            "from": {"username": "ada.bakes", "id": ADA},
            "attachments": {
                "data": [
                    {"id": "a1", "file_url": "https://cdn.example/x"},
                    {"id": "a2", "mime_type": "audio/mp4"},
                    "not an object",
                ]
            },
            "shares": {"data": [{"id": "s1"}]},
            "story": {"something_new": {"link": "https://cdn.example/s"}},
        }
        reply = {"data": [{"id": "conv-1", "messages": {"data": [message]}}]}
        network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=reply)
        )

        page = await platform.read_messages(account, ADA)

        found = [(a.kind, a.url) for a in page.items[0].attachments]
        assert found == [
            ("file", "https://cdn.example/x"),
            ("audio", None),
            ("share", None),
        ]

    async def test_a_message_missing_its_id_or_time_is_left_out(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        good = {
            "id": "m1",
            "created_time": "2026-09-28T10:00:00+0000",
            "from": {"id": ADA},
            "message": "Hi",
        }
        reply = {
            "data": [
                {
                    "id": "conv-1",
                    "messages": {
                        "data": [
                            good,
                            {"id": "m2"},
                            {"created_time": "2026-09-28T10:00:00+0000"},
                            {"id": "m3", "created_time": "not a time"},
                            "not an object",
                        ]
                    },
                }
            ]
        }
        network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=reply)
        )

        page = await platform.read_messages(account, ADA)

        assert [message.id for message in page.items] == ["m1"]
        assert page.items[0].sender.handle is None
        assert page.items[0].sender.url is None

    async def test_a_message_with_no_sender_is_the_other_persons(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        message = {"id": "m1", "created_time": "2026-09-28T10:00:00+0000"}
        reply = {"data": [{"id": "conv-1", "messages": {"data": [message]}}]}
        network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=reply)
        )

        page = await platform.read_messages(account, ADA)

        assert page.items[0].sender.id == ADA
        assert page.items[0].is_mine is False

    async def test_nobody_by_that_id_is_not_found(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        with pytest.raises(NotFoundError, match=ADA):
            await platform.read_messages(account, ADA)

    async def test_a_next_page_is_refused_because_none_was_ever_given(
        self, platform: InstagramPlatform, account: Connection
    ) -> None:
        with pytest.raises(ConfigError, match="20"):
            await platform.read_messages(account, ADA, after="anything")


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


class TestSendingAMessage:
    async def test_it_sends_the_words_to_the_person(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        route = network.post("/me/messages").mock(
            return_value=httpx.Response(200, json=fixture("send_message.json"))
        )

        sent = await platform.send_message(account, ADA, "See you at 5")

        assert json.loads(route.calls[0].request.content) == {
            "recipient": {"id": ADA},
            "message": {"text": "See you at 5"},
        }
        assert sent.id == fixture("send_message.json")["message_id"]
        assert sent.conversation_id == ADA
        assert sent.text == "See you at 5"
        assert sent.is_mine is True
        assert sent.deleted is False
        assert sent.sender.id == IG_ID
        assert sent.sender.handle == IG_NAME
        assert sent.sent_at == NOW
        assert sent.attachments == ()

    async def test_a_message_tag_goes_with_it(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        route = network.post("/me/messages").mock(
            return_value=httpx.Response(200, json=fixture("send_message.json"))
        )

        await platform.send_message(
            account, ADA, "Sorry for the wait", options={"tag": "HUMAN_AGENT"}
        )

        body = json.loads(route.calls[0].request.content)
        assert body["messaging_type"] == "MESSAGE_TAG"
        assert body["tag"] == "HUMAN_AGENT"

    async def test_a_human_agent_platform_tags_every_message(
        self,
        agent_platform: InstagramPlatform,
        account: Connection,
        network: respx.Router,
    ) -> None:
        route = network.post("/me/messages").mock(
            return_value=httpx.Response(200, json=fixture("send_message.json"))
        )

        await agent_platform.send_message(account, ADA, "Sorry for the wait")

        body = json.loads(route.calls[0].request.content)
        assert body["tag"] == "HUMAN_AGENT"

    async def test_no_tag_can_be_asked_for_on_a_human_agent_platform(
        self,
        agent_platform: InstagramPlatform,
        account: Connection,
        network: respx.Router,
    ) -> None:
        route = network.post("/me/messages").mock(
            return_value=httpx.Response(200, json=fixture("send_message.json"))
        )

        await agent_platform.send_message(account, ADA, "Hi", options={"tag": None})

        body = json.loads(route.calls[0].request.content)
        assert "tag" not in body
        assert "messaging_type" not in body

    @pytest.mark.parametrize(
        "options",
        [{"colour": "red"}, {"tag": 7}, {"tag": ""}],
    )
    async def test_a_setting_it_does_not_know_is_refused_before_sending(
        self,
        platform: InstagramPlatform,
        account: Connection,
        options: dict[str, object],
    ) -> None:
        with pytest.raises(InvalidPostError):
            await platform.send_message(account, ADA, "Hi", options=options)

    async def test_no_words_is_refused_before_sending(
        self, platform: InstagramPlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError, match="empty"):
            await platform.send_message(account, ADA, "   ")

    async def test_too_many_bytes_is_refused_before_sending(
        self, platform: InstagramPlatform, account: Connection
    ) -> None:
        # 1000 bytes, not characters: each of these is four.
        with pytest.raises(InvalidPostError, match="1000"):
            await platform.send_message(account, ADA, "\N{BREAD}" * 251)

    async def test_a_message_is_never_sent_twice_by_trying_again(
        self, account: Connection, network: respx.Router
    ) -> None:
        # Meta may have delivered it before the 503, and there is no way to
        # ask it not to deliver the same message twice.
        platform = InstagramPlatform(retries=Retries(attempts=3))
        route = network.post("/me/messages").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json=fixture("send_message.json")),
            ]
        )

        with pytest.raises(SocialChimpError):
            await platform.send_message(account, ADA, "Hi")

        assert route.call_count == 1

    async def test_reading_still_tries_again(
        self, account: Connection, network: respx.Router
    ) -> None:
        platform = InstagramPlatform(retries=Retries(attempts=2, first_wait=0))
        route = network.get("/me/conversations").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json={"data": []}),
            ]
        )

        await platform.read_conversations(account)

        assert route.call_count == 2

    async def test_a_limit_below_one_asks_for_one(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        route = network.get("/me/conversations").mock(
            return_value=httpx.Response(200, json=one_conversation())
        )

        await platform.read_messages(account, ADA, limit=0)

        assert "messages.limit(1)" in route.calls[0].request.url.params["fields"]

    async def test_a_reply_with_no_id_says_so(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        network.post("/me/messages").mock(
            return_value=httpx.Response(200, json={"recipient_id": ADA})
        )

        with pytest.raises(Exception, match="message_id"):
            await platform.send_message(account, ADA, "Hi")


class TestWhenInstagramWillNotSend:
    @pytest.mark.parametrize(
        "name", ["outside_window_2534022", "outside_window_2018278"]
    )
    async def test_after_the_window_closes_it_says_so(
        self,
        platform: InstagramPlatform,
        account: Connection,
        network: respx.Router,
        name: str,
    ) -> None:
        network.post("/me/messages").mock(return_value=meta_error(name))

        with pytest.raises(ReplyWindowClosedError) as refused:
            await platform.send_message(account, ADA, "Hi")

        assert refused.value.platform == "instagram"
        assert "24 hours" in str(refused.value)
        assert "HUMAN_AGENT" in str(refused.value)

    async def test_a_window_error_inside_a_happy_reply_is_still_named(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        body = fixture("errors.json")["outside_window_2534022"]["body"]
        network.post("/me/messages").mock(return_value=httpx.Response(200, json=body))

        with pytest.raises(ReplyWindowClosedError):
            await platform.send_message(account, ADA, "Hi")

    @pytest.mark.parametrize("name", ["user_unavailable_551", "cannot_receive_2018108"])
    async def test_someone_who_cannot_be_messaged_is_a_block(
        self,
        platform: InstagramPlatform,
        account: Connection,
        network: respx.Router,
        name: str,
    ) -> None:
        network.post("/me/messages").mock(return_value=meta_error(name))

        with pytest.raises(BlockedError):
            await platform.send_message(account, ADA, "Hi")

    async def test_someone_who_does_not_exist_is_not_found(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        network.post("/me/messages").mock(
            return_value=meta_error("recipient_not_found_2534014")
        )

        with pytest.raises(NotFoundError):
            await platform.send_message(account, ADA, "Hi")

    @pytest.mark.parametrize(
        "name",
        [
            "rate_limit_messaging_613",
            "rate_limit_app_4",
            "rate_limit_user_17",
            "rate_limit_page_32",
        ],
    )
    async def test_slow_down_is_a_rate_limit(
        self,
        platform: InstagramPlatform,
        account: Connection,
        network: respx.Router,
        name: str,
    ) -> None:
        network.post("/me/messages").mock(return_value=meta_error(name))

        with pytest.raises(RateLimitError):
            await platform.send_message(account, ADA, "Hi")

    @pytest.mark.parametrize(
        "name",
        ["dm_access_disabled_2534041", "missing_permission_200", "no_permission_10"],
    )
    async def test_a_missing_permission_names_it(
        self,
        platform: InstagramPlatform,
        account: Connection,
        network: respx.Router,
        name: str,
    ) -> None:
        network.post("/me/messages").mock(return_value=meta_error(name))

        with pytest.raises(MissingPermissionError) as refused:
            await platform.send_message(account, ADA, "Hi")

        assert "instagram_business_manage_messages" in refused.value.needs

    async def test_any_other_refusal_keeps_metas_name_for_it(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        network.post("/me/messages").mock(
            return_value=httpx.Response(
                400, json={"error": {"code": 368, "message": "Blocked"}}
            )
        )

        with pytest.raises(NotAllowedError) as refused:
            await platform.send_message(account, ADA, "Hi")

        assert not isinstance(refused.value, MissingPermissionError)

    async def test_a_refusal_with_no_meta_error_in_it_is_left_alone(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        network.post("/me/messages").mock(
            return_value=httpx.Response(404, text="Not here")
        )

        with pytest.raises(NotFoundError) as refused:
            await platform.send_message(account, ADA, "Hi")

        assert not isinstance(refused.value, MissingPermissionError)

    async def test_a_window_error_on_reading_is_named_too(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        network.get("/me/conversations").mock(
            return_value=meta_error("dm_access_disabled_2534041")
        )

        with pytest.raises(MissingPermissionError):
            await platform.read_conversations(account)


# ---------------------------------------------------------------------------
# Marking a conversation as read
# ---------------------------------------------------------------------------


class TestMarkingAsRead:
    async def test_it_sends_mark_seen_to_the_person(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        route = network.post("/me/messages").mock(
            return_value=httpx.Response(200, json=fixture("mark_seen.json"))
        )

        await platform.mark_read(account, ADA)

        assert json.loads(route.calls[0].request.content) == {
            "recipient": {"id": ADA},
            "sender_action": "mark_seen",
        }


# ---------------------------------------------------------------------------
# What Instagram pushes to us
# ---------------------------------------------------------------------------


class TestPushedMessages:
    def test_a_new_message_arrives_with_the_message_itself(
        self, platform: InstagramPlatform
    ) -> None:
        [event] = platform.read_message_events(pushed("text"))

        assert event.kind is MessageEventKind.RECEIVED
        assert event.platform == "instagram"
        assert event.connection_id == f"instagram:{IG_ID}"
        assert event.conversation_id == ADA
        assert event.person.id == ADA
        assert event.happened_at == datetime.fromtimestamp(1_790_604_060, UTC)
        message = event.message
        assert message is not None
        assert message.text == "Hi! Do you still have the walnut loaf today?"
        assert message.is_mine is False
        assert message.sender.id == ADA
        assert message.conversation_id == ADA
        assert event.message_id == message.id
        assert event.raw["message"]["mid"] == message.id

    def test_a_new_message_opens_the_reply_window(
        self, platform: InstagramPlatform, agent_platform: InstagramPlatform
    ) -> None:
        [event] = platform.read_message_events(pushed("text"))
        [tagged] = agent_platform.read_message_events(pushed("text"))

        conversation = event.conversation
        assert conversation is not None
        assert conversation.id == ADA
        assert conversation.last_message == event.message
        assert conversation.can_reply_until == event.happened_at + DAY
        assert tagged.conversation is not None
        assert tagged.conversation.can_reply_until == event.happened_at + WEEK

    @pytest.mark.parametrize(
        ("name", "kind"),
        [
            ("attachment_image", "image"),
            ("attachment_video", "video"),
            ("attachment_audio", "audio"),
            ("attachment_file", "file"),
            ("attachment_share", "share"),
            ("attachment_story_mention", "story_mention"),
            ("attachment_ig_reel", "reel"),
        ],
    )
    def test_each_kind_of_attachment_is_typed(
        self, platform: InstagramPlatform, name: str, kind: str
    ) -> None:
        [event] = platform.read_message_events(pushed(name))

        assert event.message is not None
        [attachment] = event.message.attachments
        assert attachment.kind == kind
        assert attachment.url is not None
        assert attachment.url.startswith("https://")

    def test_an_attachment_type_nobody_has_named_yet_keeps_metas_word(
        self, platform: InstagramPlatform
    ) -> None:
        body = json.loads(pushed("attachment_image"))
        attachments = body["entry"][0]["messaging"][0]["message"]["attachments"]
        attachments[0] = {"type": "sticker", "payload": "not an object"}
        attachments.append("not an object")

        [event] = platform.read_message_events(json.dumps(body).encode())

        assert event.message is not None
        assert [(a.kind, a.url) for a in event.message.attachments] == [
            ("sticker", None)
        ]

    def test_a_reply_to_a_story_carries_the_story(
        self, platform: InstagramPlatform
    ) -> None:
        [event] = platform.read_message_events(pushed("story_reply"))

        assert event.message is not None
        assert event.message.text == "Love this one, is it still available?"
        [story] = event.message.attachments
        assert story.kind == "story_reply"
        assert story.url is not None

    def test_the_accounts_own_message_comes_back_as_sent(
        self, platform: InstagramPlatform
    ) -> None:
        [event] = platform.read_message_events(pushed("echo"))

        assert event.kind is MessageEventKind.SENT
        assert event.conversation_id == ADA
        assert event.person.id == ADA
        assert event.conversation is None
        assert event.message is not None
        assert event.message.is_mine is True
        assert event.message.sender.id == IG_ID

    def test_an_unsent_message_is_handed_over_marked_deleted(
        self, platform: InstagramPlatform
    ) -> None:
        [event] = platform.read_message_events(pushed("deleted"))

        assert event.kind is MessageEventKind.DELETED
        assert event.message is not None
        assert event.message.deleted is True
        assert event.message.text == ""
        assert event.message_id == event.message.id
        assert event.conversation is None

    def test_a_message_instagram_cannot_show_still_arrives(
        self, platform: InstagramPlatform
    ) -> None:
        [event] = platform.read_message_events(pushed("unsupported"))

        assert event.kind is MessageEventKind.RECEIVED
        assert event.message is not None
        assert event.message.text == ""
        assert event.message.raw["is_unsupported"] is True

    def test_a_reaction_names_the_message_and_the_emoji(
        self, platform: InstagramPlatform
    ) -> None:
        [added] = platform.read_message_events(pushed("reaction_react"))
        [taken] = platform.read_message_events(pushed("reaction_unreact"))

        assert added.kind is MessageEventKind.REACTED
        assert taken.kind is MessageEventKind.UNREACTED
        assert added.reaction == "❤️"
        assert added.message_id is not None
        assert added.message is None
        assert added.conversation_id == ADA

    def test_a_reaction_with_no_emoji_keeps_metas_word(
        self, platform: InstagramPlatform
    ) -> None:
        body = json.loads(pushed("reaction_react"))
        del body["entry"][0]["messaging"][0]["reaction"]["emoji"]

        [event] = platform.read_message_events(json.dumps(body).encode())

        assert event.reaction == "love"

    def test_a_read_names_the_newest_message_read(
        self, platform: InstagramPlatform
    ) -> None:
        [event] = platform.read_message_events(pushed("seen"))

        assert event.kind is MessageEventKind.READ
        assert event.message_id is not None
        assert event.message is None

    def test_a_tapped_button_carries_its_words_and_value(
        self, platform: InstagramPlatform
    ) -> None:
        [event] = platform.read_message_events(pushed("postback"))

        assert event.kind is MessageEventKind.BUTTON_TAPPED
        assert event.payload == "SEE_MENU"
        assert event.message is not None
        assert event.message.text == "See menu"
        assert event.conversation is not None

    def test_a_quick_reply_carries_its_value(self, platform: InstagramPlatform) -> None:
        [event] = platform.read_message_events(pushed("quick_reply"))

        assert event.kind is MessageEventKind.RECEIVED
        assert event.payload == "PICKUP_MORNING"

    def test_a_reply_to_a_message_arrives_as_a_message(
        self, platform: InstagramPlatform
    ) -> None:
        [event] = platform.read_message_events(pushed("reply_to_message"))

        assert event.kind is MessageEventKind.RECEIVED
        assert event.message is not None
        assert event.message.raw["reply_to"]["mid"]

    def test_several_events_in_one_request_all_come_back(
        self, platform: InstagramPlatform
    ) -> None:
        body = json.loads(pushed("text"))
        body["entry"][0]["messaging"] += json.loads(pushed("seen"))["entry"][0][
            "messaging"
        ]

        found = platform.read_message_events(json.dumps(body).encode())

        assert [event.kind for event in found] == [
            MessageEventKind.RECEIVED,
            MessageEventKind.READ,
        ]

    @pytest.mark.parametrize(
        "event",
        [
            {"sender": {"id": ADA}, "recipient": {"id": IG_ID}, "referral": {}},
            {"recipient": {"id": IG_ID}, "read": {"mid": "m1"}},
            {"sender": "not an object", "read": {"mid": "m1"}},
            {"sender": {"id": ADA}, "message": {"text": "no mid"}},
            {"sender": {"id": ADA}, "postback": "not an object"},
            {"sender": {"id": None}, "read": {"mid": "m1"}},
            {"sender": {"id": ""}, "read": {"mid": "m1"}},
            {
                "sender": {"id": IG_ID},
                "message": {"mid": "m1", "text": "Hi", "is_echo": True},
            },
        ],
    )
    def test_what_it_cannot_make_sense_of_is_left_out(
        self, platform: InstagramPlatform, event: dict[str, Any]
    ) -> None:
        body = {"entry": [{"id": IG_ID, "time": 1, "messaging": [event]}]}

        assert platform.read_message_events(json.dumps(body).encode()) == []

    @pytest.mark.parametrize(
        "moment", [1e300, float("nan"), 99_999_999_999_999_999, -99_999_999_999_999]
    )
    def test_a_time_that_cannot_be_a_time_is_stamped_as_it_arrives(
        self, platform: InstagramPlatform, moment: float
    ) -> None:
        body = json.loads(pushed("seen"))
        body["entry"][0]["time"] = moment
        body["entry"][0]["messaging"][0]["timestamp"] = moment

        [event] = platform.read_message_events(json.dumps(body).encode())

        assert event.happened_at.tzinfo is not None

    def test_an_id_sent_as_a_number_is_read_as_text(
        self, platform: InstagramPlatform
    ) -> None:
        body = json.loads(pushed("seen"))
        body["entry"][0]["messaging"][0]["sender"]["id"] = int(ADA)

        [event] = platform.read_message_events(json.dumps(body).encode())

        assert event.conversation_id == ADA

    def test_a_postback_with_no_mid_still_arrives(
        self, platform: InstagramPlatform
    ) -> None:
        body = json.loads(pushed("postback"))
        del body["entry"][0]["messaging"][0]["postback"]["mid"]

        [event] = platform.read_message_events(json.dumps(body).encode())

        assert event.message_id is None
        assert event.message is not None
        assert event.message.id.startswith("postback:")

    def test_comments_are_not_messages(self, platform: InstagramPlatform) -> None:
        body = json.dumps(
            {
                "entry": [
                    {
                        "id": IG_ID,
                        "time": 1,
                        "changes": [{"field": "comments", "value": {"id": "c1"}}],
                    }
                ]
            }
        ).encode()

        assert platform.read_message_events(body) == []


class TestPushedMessagesAsUpdates:
    def test_a_new_message_is_a_message_received_update(
        self, platform: InstagramPlatform
    ) -> None:
        [update] = platform.read_updates(pushed("text"))

        assert update.kind is UpdateKind.MESSAGE_RECEIVED
        assert update.connection_id == f"instagram:{IG_ID}"
        assert update.conversation_id == ADA
        assert update.actor is not None
        assert update.actor.id == ADA
        assert update.post_id is not None
        assert update.id == f"{IG_ID}:message:{update.post_id}"
        assert update.raw["message"]["text"].startswith("Hi!")

    @pytest.mark.parametrize(
        "name", ["echo", "deleted", "reaction_react", "seen", "postback"]
    )
    def test_everything_else_in_a_conversation_is_only_a_message_event(
        self, platform: InstagramPlatform, name: str
    ) -> None:
        assert platform.read_updates(pushed(name)) == []


# ---------------------------------------------------------------------------
# Sending pictures, video, sound and files
# ---------------------------------------------------------------------------

PICTURE = "https://files.example/uploads/loaf.jpg?X-Amz-Signature=abc&X-Amz-Expires=600"


def attachment_reply(message_id: str = "mid-attachment") -> httpx.Response:
    return httpx.Response(200, json={"recipient_id": ADA, "message_id": message_id})


def attachment_error(name: str) -> httpx.Response:
    found = fixture("send_attachment_errors.json")[name]
    return httpx.Response(found["http_status"], json=found["body"])


class TestWhatAMessageMayCarry:
    def test_it_says_it_can_send_attachments(self, platform: InstagramPlatform) -> None:
        assert Feature.MESSAGE_MEDIA in platform.features
        assert isinstance(platform, CanSendMessageMedia)

    async def test_the_limits_say_what_a_message_may_carry(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        network.get(f"/{IG_ID}/content_publishing_limit").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        limits = await platform.limits(account)

        messages = limits.messages
        assert messages == MESSAGE_LIMITS
        assert messages is not None
        assert messages.max_text_bytes == MOST_MESSAGE_BYTES == 1000
        assert messages.takes_web_addresses is True
        assert messages.takes_files is False
        assert messages.one_kind_at_a_time is True
        image = messages.rule_for(MediaKind.IMAGE)
        assert image is not None
        assert image.max_count == 10
        assert image.max_bytes == 8 * 1024 * 1024
        assert "image/jpeg" in image.mime_types
        pdf = messages.rule_for(MediaKind.FILE)
        assert pdf is not None
        assert pdf.mime_types == ("application/pdf",)
        assert pdf.max_count == 1

    @pytest.mark.parametrize(
        ("name", "kind"),
        [
            ("clip.mov", MediaKind.VIDEO),
            ("clip.avi", MediaKind.VIDEO),
            ("note.m4a", MediaKind.AUDIO),
            ("note.aac", MediaKind.AUDIO),
            ("note.wav", MediaKind.AUDIO),
            ("menu.pdf", MediaKind.FILE),
            ("loaf.png", MediaKind.IMAGE),
        ],
    )
    def test_every_format_instagram_lists_passes_the_check(
        self, name: str, kind: MediaKind
    ) -> None:
        media = Media.from_url(f"https://files.example/{name}")

        assert media.kind is kind
        check_message("", (media,), platform="instagram", limits=MESSAGE_LIMITS)


class TestSendingAttachments:
    async def test_one_picture_goes_as_one_attachment(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        route = network.post("/me/messages").mock(return_value=attachment_reply())

        sent = await platform.send_message_with_media(
            account, ADA, "", (Media.from_url(PICTURE),)
        )

        assert json.loads(route.calls[0].request.content) == {
            "recipient": {"id": ADA},
            "message": {"attachment": {"type": "image", "payload": {"url": PICTURE}}},
        }
        assert sent.id == "mid-attachment"
        assert sent.text == ""
        assert sent.is_mine is True
        assert sent.conversation_id == ADA
        assert [(a.kind, a.url) for a in sent.attachments] == [("image", PICTURE)]
        assert sent.also_sent == ()

    async def test_several_pictures_go_together_in_one_message(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        route = network.post("/me/messages").mock(return_value=attachment_reply())
        other = "https://files.example/uploads/crumb.png"

        sent = await platform.send_message_with_media(
            account, ADA, "", (Media.from_url(PICTURE), Media.from_url(other))
        )

        assert route.call_count == 1
        assert json.loads(route.calls[0].request.content)["message"] == {
            "attachments": [
                {"type": "image", "payload": {"url": PICTURE}},
                {"type": "image", "payload": {"url": other}},
            ]
        }
        assert len(sent.attachments) == 2

    @pytest.mark.parametrize(
        ("name", "kind"),
        [("proof.mp4", "video"), ("hello.m4a", "audio"), ("menu.pdf", "file")],
    )
    async def test_video_sound_and_files_go_one_at_a_time(
        self,
        platform: InstagramPlatform,
        account: Connection,
        network: respx.Router,
        name: str,
        kind: str,
    ) -> None:
        route = network.post("/me/messages").mock(return_value=attachment_reply())
        address = f"https://files.example/{name}"

        sent = await platform.send_message_with_media(
            account, ADA, "", (Media.from_url(address),)
        )

        assert json.loads(route.calls[0].request.content)["message"] == {
            "attachment": {"type": kind, "payload": {"url": address}}
        }
        assert sent.attachments[0].kind == kind

    async def test_words_and_an_attachment_go_as_two_messages(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        route = network.post("/me/messages").mock(
            side_effect=[attachment_reply("mid-picture"), attachment_reply("mid-words")]
        )

        sent = await platform.send_message_with_media(
            account, ADA, "Fresh this morning", (Media.from_url(PICTURE),)
        )

        bodies = [json.loads(call.request.content) for call in route.calls]
        assert "attachment" in bodies[0]["message"]
        assert bodies[1]["message"] == {"text": "Fresh this morning"}
        # The attachment goes first: if it is refused, nothing has gone out.
        assert sent.id == "mid-picture"
        assert sent.text == ""
        [words] = sent.also_sent
        assert words.id == "mid-words"
        assert words.text == "Fresh this morning"
        assert words.attachments == ()

    async def test_a_tag_goes_on_every_message_it_sends(
        self,
        agent_platform: InstagramPlatform,
        account: Connection,
        network: respx.Router,
    ) -> None:
        route = network.post("/me/messages").mock(
            side_effect=[attachment_reply("a"), attachment_reply("b")]
        )

        await agent_platform.send_message_with_media(
            account, ADA, "Sorry for the wait", (Media.from_url(PICTURE),)
        )

        assert all(
            json.loads(call.request.content)["tag"] == "HUMAN_AGENT"
            for call in route.calls
        )

    async def test_words_refused_after_the_attachment_went_say_what_went(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        network.post("/me/messages").mock(
            side_effect=[
                attachment_reply("mid-picture"),
                meta_error("rate_limit_app_4"),
            ]
        )

        with pytest.raises(RateLimitError) as refused:
            await platform.send_message_with_media(
                account, ADA, "words", (Media.from_url(PICTURE),)
            )

        assert refused.value.raw["already_sent"] == ["mid-picture"]
        assert "mid-picture" in str(refused.value)

    async def test_an_attachment_is_never_sent_twice_by_trying_again(
        self, account: Connection, network: respx.Router
    ) -> None:
        platform = InstagramPlatform(retries=Retries(attempts=3))
        route = network.post("/me/messages").mock(
            side_effect=[httpx.Response(503), attachment_reply()]
        )

        with pytest.raises(SocialChimpError):
            await platform.send_message_with_media(
                account, ADA, "", (Media.from_url(PICTURE),)
            )

        assert route.call_count == 1

    async def test_a_file_on_disk_is_refused_before_sending(
        self, platform: InstagramPlatform, account: Connection
    ) -> None:
        on_disk = Media.from_bytes(b"x", filename="loaf.jpg")

        with pytest.raises(NotSupportedError, match="from_url"):
            await platform.send_message_with_media(account, ADA, "", (on_disk,))

    async def test_a_gif_is_refused_before_sending(
        self, platform: InstagramPlatform, account: Connection
    ) -> None:
        gif = Media.from_url("https://files.example/dance.gif")

        with pytest.raises(InvalidPostError, match="image/gif"):
            await platform.send_message_with_media(account, ADA, "", (gif,))

    async def test_mixing_kinds_is_refused_before_sending(
        self, platform: InstagramPlatform, account: Connection
    ) -> None:
        mixed = (Media.from_url(PICTURE), Media.from_url("https://f.example/a.pdf"))

        with pytest.raises(InvalidPostError, match="one kind"):
            await platform.send_message_with_media(account, ADA, "", mixed)

    async def test_an_unknown_option_is_refused_before_sending(
        self, platform: InstagramPlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError):
            await platform.send_message_with_media(
                account, ADA, "", (Media.from_url(PICTURE),), options={"colour": 1}
            )

    async def test_a_reply_with_no_id_says_so(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        network.post("/me/messages").mock(
            return_value=httpx.Response(200, json={"recipient_id": ADA})
        )

        with pytest.raises(SocialChimpError, match="message_id"):
            await platform.send_message_with_media(
                account, ADA, "", (Media.from_url(PICTURE),)
            )


class TestWhenInstagramWillNotTakeTheFile:
    @pytest.mark.parametrize(
        ("name", "says"),
        [
            ("upload_failure_2018047", "type"),
            ("url_fetch_failed_2018008", "fetch"),
            ("size_exceeded_2018109", "too big"),
            ("video_timeout_2018294", "75 seconds"),
            ("attachment_id_invalid_2018074", "attachment"),
        ],
    )
    async def test_each_refusal_says_what_is_wrong_with_the_file(
        self,
        platform: InstagramPlatform,
        account: Connection,
        network: respx.Router,
        name: str,
        says: str,
    ) -> None:
        network.post("/me/messages").mock(return_value=attachment_error(name))

        with pytest.raises(InvalidPostError) as refused:
            await platform.send_message_with_media(
                account, ADA, "", (Media.from_url(PICTURE),)
            )

        assert refused.value.platform == "instagram"
        assert says in str(refused.value)

    async def test_an_expired_window_is_still_the_window(
        self, platform: InstagramPlatform, account: Connection, network: respx.Router
    ) -> None:
        network.post("/me/messages").mock(
            return_value=meta_error("outside_window_2534022")
        )

        with pytest.raises(ReplyWindowClosedError):
            await platform.send_message_with_media(
                account, ADA, "", (Media.from_url(PICTURE),)
            )
