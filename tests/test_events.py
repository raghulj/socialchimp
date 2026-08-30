"""Tests for updates: checking they are genuine, and passing them on."""

import asyncio
import hashlib
import hmac
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from socialchimp.errors import SocialChimpError
from socialchimp.events import (
    Dispatcher,
    InMemorySeenUpdates,
    Poller,
    SeenUpdates,
    SignatureError,
    Update,
    UpdateKind,
    answer_setup_check,
    check_not_too_old,
    poll,
    verify_hmac_sha256,
    verify_shared_secret,
)

SECRET = "shhh"
BODY = b'{"entry": [{"id": "42"}]}'

MONDAY = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
TUESDAY = datetime(2026, 1, 6, 12, 0, tzinfo=UTC)
WEDNESDAY = datetime(2026, 1, 7, 12, 0, tzinfo=UTC)


def meta_header(body: bytes = BODY, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def an_update(
    update_id: str = "u1",
    *,
    kind: UpdateKind = UpdateKind.COMMENT_CREATED,
    at: datetime = MONDAY,
) -> Update:
    return Update(
        id=update_id,
        kind=kind,
        platform="linkedin",
        connection_id="conn-1",
        created_at=at,
    )


class TestUpdate:
    def test_an_update_carries_everything_needed_to_act_on_it(self) -> None:
        update = Update(
            id="u1",
            kind=UpdateKind.COMMENT_CREATED,
            platform="facebook",
            connection_id="conn-1",
            created_at=MONDAY,
            raw={"comment_id": "9"},
        )

        assert update.platform == "facebook"
        assert update.connection_id == "conn-1"
        assert update.raw == {"comment_id": "9"}

    def test_an_update_without_a_timezone_is_refused(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            Update(
                id="u1",
                kind=UpdateKind.MENTION,
                platform="facebook",
                connection_id="conn-1",
                created_at=datetime(2026, 1, 5, 12, 0),  # noqa: DTZ001
            )

    def test_a_known_word_from_a_network_becomes_a_known_kind(self) -> None:
        update = Update.from_network(
            update_id="u1",
            kind_name="comment_created",
            platform="facebook",
            connection_id="conn-1",
            created_at=MONDAY,
            raw={"anything": True},
        )

        assert update.kind is UpdateKind.COMMENT_CREATED
        assert update.kind_name == "comment_created"
        assert update.raw == {"anything": True}

    def test_a_word_we_do_not_know_is_kept_rather_than_crashing(self) -> None:
        # Networks add new kinds without warning. An app that only handles
        # comments should keep working the day one appears.
        update = Update.from_network(
            update_id="u1",
            kind_name="story_insight_reply",
            platform="instagram",
            connection_id="conn-1",
            created_at=MONDAY,
        )

        assert update.kind is UpdateKind.UNKNOWN
        assert update.kind_name == "story_insight_reply"
        assert update.raw == {}

    def test_a_kind_built_by_hand_names_itself(self) -> None:
        assert an_update().kind_name == "comment_created"


class TestVerifyHmacSha256:
    def test_a_signature_made_with_the_right_secret_is_accepted(self) -> None:
        verify_hmac_sha256(
            BODY,
            {"X-Hub-Signature-256": meta_header()},
            secret=SECRET,
        )

    def test_the_header_is_found_whatever_its_capitalisation(self) -> None:
        verify_hmac_sha256(
            BODY,
            {"x-hub-signature-256": meta_header()},
            secret=SECRET,
        )

    def test_the_header_is_found_among_all_the_others(self) -> None:
        verify_hmac_sha256(
            BODY,
            {
                "Content-Type": "application/json",
                "User-Agent": "facebookplatform/1.0",
                "X-Hub-Signature-256": meta_header(),
            },
            secret=SECRET,
        )

    def test_a_changed_body_is_rejected(self) -> None:
        signature = meta_header()

        with pytest.raises(SignatureError, match="does not match"):
            verify_hmac_sha256(
                b'{"entry": [{"id": "43"}]}',
                {"X-Hub-Signature-256": signature},
                secret=SECRET,
            )

    def test_a_signature_made_with_another_secret_is_rejected(self) -> None:
        with pytest.raises(SignatureError, match="does not match"):
            verify_hmac_sha256(
                BODY,
                {"X-Hub-Signature-256": meta_header(secret="wrong")},
                secret=SECRET,
            )

    def test_a_request_with_no_signature_at_all_is_rejected(self) -> None:
        with pytest.raises(SignatureError, match="X-Hub-Signature-256"):
            verify_hmac_sha256(BODY, {}, secret=SECRET)

    def test_a_signature_missing_the_sha256_prefix_is_rejected(self) -> None:
        digest = hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()

        with pytest.raises(SignatureError, match="sha256="):
            verify_hmac_sha256(
                BODY,
                {"X-Hub-Signature-256": digest},
                secret=SECRET,
            )

    def test_a_network_that_sends_a_bare_signature_can_say_so(self) -> None:
        digest = hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()

        verify_hmac_sha256(
            BODY,
            {"X-Signature": digest},
            secret=SECRET,
            header_name="X-Signature",
            prefix="",
        )

    def test_the_error_is_one_of_ours(self) -> None:
        # Apps catch SocialChimpError to catch everything we raise.
        assert issubclass(SignatureError, SocialChimpError)


class TestVerifySharedSecret:
    def test_the_agreed_secret_coming_back_is_accepted(self) -> None:
        verify_shared_secret(
            {"X-Telegram-Bot-Api-Secret-Token": SECRET},
            secret=SECRET,
        )

    def test_a_different_secret_is_rejected(self) -> None:
        with pytest.raises(SignatureError, match="does not match"):
            verify_shared_secret(
                {"X-Telegram-Bot-Api-Secret-Token": "guessed"},
                secret=SECRET,
            )

    def test_no_secret_at_all_is_rejected(self) -> None:
        with pytest.raises(SignatureError, match="X-Telegram-Bot-Api-Secret-Token"):
            verify_shared_secret({}, secret=SECRET)


class TestCheckNotTooOld:
    def test_a_request_signed_a_moment_ago_is_fine(self) -> None:
        check_not_too_old(MONDAY - timedelta(seconds=10), now=MONDAY)

    def test_an_old_request_is_refused_and_the_reason_is_named(self) -> None:
        with pytest.raises(SignatureError, match="sent again"):
            check_not_too_old(MONDAY - timedelta(hours=2), now=MONDAY)

    def test_the_window_can_be_widened(self) -> None:
        check_not_too_old(
            MONDAY - timedelta(hours=2),
            allowed_age_seconds=10_800,
            now=MONDAY,
        )

    def test_seconds_since_1970_are_understood_too(self) -> None:
        # Discord and Meta both send the time as a plain number.
        with pytest.raises(SignatureError, match="sent again"):
            check_not_too_old((MONDAY - timedelta(hours=2)).timestamp(), now=MONDAY)

    def test_it_measures_against_the_real_clock_by_default(self) -> None:
        check_not_too_old(datetime.now(UTC))

    def test_a_time_without_a_timezone_is_refused(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            check_not_too_old(datetime(2026, 1, 5, 12, 0))  # noqa: DTZ001


class TestAnswerSetupCheck:
    def test_a_genuine_setup_check_gets_its_challenge_back(self) -> None:
        answer = answer_setup_check(
            {
                "hub.mode": "subscribe",
                "hub.verify_token": SECRET,
                "hub.challenge": "1158201444",
            },
            expected_token=SECRET,
        )

        assert answer == "1158201444"

    def test_a_setup_check_with_the_wrong_token_is_refused(self) -> None:
        with pytest.raises(SignatureError, match="token"):
            answer_setup_check(
                {
                    "hub.mode": "subscribe",
                    "hub.verify_token": "guessed",
                    "hub.challenge": "1158201444",
                },
                expected_token=SECRET,
            )

    def test_a_request_that_is_not_a_setup_check_is_refused(self) -> None:
        with pytest.raises(SignatureError, match="setup"):
            answer_setup_check({"hub.mode": "unsubscribe"}, expected_token=SECRET)


class TestInMemorySeenUpdates:
    def test_the_bundled_memory_is_a_valid_memory(self) -> None:
        accepted: SeenUpdates = InMemorySeenUpdates()

        assert isinstance(accepted, SeenUpdates)

    async def test_an_update_we_remembered_has_been_seen(self) -> None:
        memory = InMemorySeenUpdates()

        await memory.remember("u1")

        assert await memory.seen("u1") is True

    async def test_an_update_we_have_never_met_has_not_been_seen(self) -> None:
        assert await InMemorySeenUpdates().seen("u1") is False

    async def test_the_oldest_are_forgotten_so_it_cannot_grow_forever(self) -> None:
        memory = InMemorySeenUpdates(max_size=3)

        for number in range(4):
            await memory.remember(f"u{number}")

        assert await memory.seen("u0") is False
        assert await memory.seen("u3") is True


class TestDispatcher:
    async def test_a_handler_registered_for_a_kind_is_called(self) -> None:
        seen_by_handler: list[str] = []
        dispatcher = Dispatcher()

        async def note(update: Update) -> None:
            seen_by_handler.append(update.id)

        dispatcher.on(UpdateKind.COMMENT_CREATED, note)
        await dispatcher.deliver(an_update("u1"))

        assert seen_by_handler == ["u1"]

    async def test_handlers_for_other_kinds_are_left_alone(self) -> None:
        called: list[str] = []
        dispatcher = Dispatcher()

        async def note(update: Update) -> None:
            called.append(update.id)

        dispatcher.on(UpdateKind.POST_FAILED, note)
        await dispatcher.deliver(an_update("u1", kind=UpdateKind.MENTION))

        assert called == []

    async def test_a_catch_all_handler_hears_about_everything(self) -> None:
        heard: list[UpdateKind] = []
        dispatcher = Dispatcher()

        async def note(update: Update) -> None:
            heard.append(update.kind)

        dispatcher.on_any(note)
        await dispatcher.deliver(an_update("u1", kind=UpdateKind.MENTION))
        await dispatcher.deliver(an_update("u2", kind=UpdateKind.UNKNOWN))

        assert heard == [UpdateKind.MENTION, UpdateKind.UNKNOWN]

    async def test_a_handler_that_throws_does_not_stop_the_others(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        survivors: list[str] = []
        dispatcher = Dispatcher()

        async def explode(update: Update) -> None:
            message = "the database is down"
            raise RuntimeError(message)

        async def note(update: Update) -> None:
            survivors.append(update.id)

        dispatcher.on(UpdateKind.COMMENT_CREATED, explode)
        dispatcher.on(UpdateKind.COMMENT_CREATED, note)
        await dispatcher.deliver(an_update("u1"))

        assert survivors == ["u1"]
        assert "the database is down" in caplog.text

    async def test_the_same_update_arriving_twice_is_handled_once(self) -> None:
        # Networks promise to deliver at least once, so this is normal.
        calls: list[str] = []
        dispatcher = Dispatcher(seen=InMemorySeenUpdates())

        async def note(update: Update) -> None:
            calls.append(update.id)

        dispatcher.on_any(note)
        await dispatcher.deliver(an_update("u1"))
        await dispatcher.deliver(an_update("u1"))

        assert calls == ["u1"]

    async def test_an_update_is_only_remembered_once_it_has_been_handled(self) -> None:
        # Remembering first would lose the update if a handler crashed.
        memory = InMemorySeenUpdates()
        dispatcher = Dispatcher(seen=memory)

        assert await memory.seen("u1") is False

        await dispatcher.deliver(an_update("u1"))

        assert await memory.seen("u1") is True


class TestPoller:
    async def test_only_items_newer_than_the_marker_count_as_new(self) -> None:
        delivered: list[str] = []

        async def fetch() -> Sequence[Update]:
            return [an_update("wed", at=WEDNESDAY), an_update("mon", at=MONDAY)]

        async def deliver(update: Update) -> None:
            delivered.append(update.id)

        poller = Poller(fetch=fetch, deliver=deliver, since=TUESDAY)

        assert [update.id for update in await poller.check_once()] == ["wed"]
        assert delivered == ["wed"]

    async def test_nothing_is_new_the_second_time_around(self) -> None:
        async def fetch() -> Sequence[Update]:
            return [an_update("mon", at=MONDAY)]

        async def deliver(update: Update) -> None:
            return None

        poller = Poller(fetch=fetch, deliver=deliver)

        assert len(await poller.check_once()) == 1
        assert await poller.check_once() == []

    async def test_with_no_marker_everything_fetched_is_new(self) -> None:
        async def fetch() -> Sequence[Update]:
            return [an_update("mon", at=MONDAY), an_update("wed", at=WEDNESDAY)]

        async def deliver(update: Update) -> None:
            return None

        poller = Poller(fetch=fetch, deliver=deliver)

        assert [update.id for update in await poller.check_once()] == ["mon", "wed"]

    async def test_updates_arrive_oldest_first(self) -> None:
        delivered: list[str] = []

        async def fetch() -> Sequence[Update]:
            return [an_update("wed", at=WEDNESDAY), an_update("mon", at=MONDAY)]

        async def deliver(update: Update) -> None:
            delivered.append(update.id)

        await Poller(fetch=fetch, deliver=deliver).check_once()

        assert delivered == ["mon", "wed"]

    async def test_the_marker_is_handed_back_so_a_restart_can_carry_on(self) -> None:
        saved: list[datetime] = []

        async def fetch() -> Sequence[Update]:
            return [an_update("wed", at=WEDNESDAY)]

        async def deliver(update: Update) -> None:
            return None

        async def save_marker(marker: datetime) -> None:
            saved.append(marker)

        poller = Poller(fetch=fetch, deliver=deliver, save_marker=save_marker)
        await poller.check_once()

        assert saved == [WEDNESDAY]

    async def test_a_round_that_finds_nothing_leaves_the_marker_alone(self) -> None:
        saved: list[datetime] = []

        async def fetch() -> Sequence[Update]:
            return []

        async def deliver(update: Update) -> None:
            return None

        async def save_marker(marker: datetime) -> None:
            saved.append(marker)

        poller = Poller(fetch=fetch, deliver=deliver, save_marker=save_marker)
        await poller.check_once()

        assert saved == []

    async def test_a_failed_round_is_logged_and_the_next_round_still_runs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        rounds = 0
        arrived = asyncio.Event()

        async def fetch() -> Sequence[Update]:
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                message = "the network timed out"
                raise RuntimeError(message)
            return [an_update("wed", at=WEDNESDAY)]

        async def deliver(update: Update) -> None:
            arrived.set()

        poller = Poller(fetch=fetch, deliver=deliver, every_seconds=0)
        task = asyncio.create_task(poller.run_forever())
        await asyncio.wait_for(arrived.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert "the network timed out" in caplog.text

    async def test_the_poller_stops_cleanly_when_it_is_cancelled(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)
        started = asyncio.Event()

        async def fetch() -> Sequence[Update]:
            started.set()
            return []

        async def deliver(update: Update) -> None:
            return None

        task = asyncio.create_task(
            Poller(fetch=fetch, deliver=deliver, every_seconds=60).run_forever()
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert "Stopped checking for updates." in caplog.text


class TestPoll:
    async def test_poll_keeps_checking_until_it_is_cancelled(self) -> None:
        rounds = 0
        twice = asyncio.Event()

        async def fetch() -> Sequence[Update]:
            nonlocal rounds
            rounds += 1
            if rounds == 2:
                twice.set()
            return []

        async def deliver(update: Update) -> None:
            return None

        task = asyncio.create_task(poll(fetch=fetch, deliver=deliver, every_seconds=0))
        await asyncio.wait_for(twice.wait(), timeout=1)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert rounds >= 2
