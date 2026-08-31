"""Tests for the kit that checks a platform behaves like the others."""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import ClassVar, cast

import httpx
import pytest

from socialchimp import (
    AppCredentials,
    Connection,
    Feature,
    InvalidPostError,
    Limits,
    Post,
    PostResult,
    SignatureError,
    Storage,
    Token,
    Update,
    UpdateKind,
)
from socialchimp.errors import ConfigError, NotFoundError
from socialchimp.http import HttpClient
from socialchimp.models import RawData
from socialchimp.platform import (
    AccountChoice,
    ChooseAccount,
    Finished,
    LoginRequest,
    LoginStep,
    Platform,
    SendToNetwork,
)
from socialchimp.testing import (
    FakePlatform,
    PlatformChecks,
    RecordingStorage,
    RecordingTransport,
    StorageCall,
)

# ---------------------------------------------------------------------------
# Small things every test here needs.
# ---------------------------------------------------------------------------


def a_connection(platform: str = "fake") -> Connection:
    return Connection(
        id="conn-1",
        platform=platform,
        host=None,
        account_id="42",
        account_name="someone",
        token=Token(access_token="abc"),
    )


def an_update(update_id: str = "u1", at: datetime | None = None) -> Update:
    return Update(
        id=update_id,
        kind=UpdateKind.COMMENT_CREATED,
        platform="fake",
        connection_id="conn-1",
        created_at=at if at is not None else datetime(2026, 1, 1, tzinfo=UTC),
    )


def checks_for(
    platform: object,
    *,
    connection: Connection | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> PlatformChecks:
    """Build a one-off subclass around a platform, the way an author would."""

    class Checks(PlatformChecks):
        def make_platform(self) -> Platform:
            # Cast because half the platforms in this file are wrong on
            # purpose; that is the whole point of checking them.
            return cast(Platform, platform)

        def make_connection(self) -> Connection | None:
            return connection

        def make_transport(self) -> httpx.AsyncBaseTransport | None:
            return transport

    return Checks()


def every_check() -> list[str]:
    return sorted(name for name in dir(PlatformChecks) if name.startswith("test_"))


async def failure_from(checks: PlatformChecks, name: str) -> str:
    with pytest.raises(pytest.fail.Exception) as caught:
        await getattr(checks, name)()
    return str(caught.value)


async def skip_from(checks: PlatformChecks, name: str) -> str:
    with pytest.raises(pytest.skip.Exception) as caught:
        await getattr(checks, name)()
    return str(caught.value)


# ---------------------------------------------------------------------------
# Platforms that are wrong in one way each, to prove the checks notice.
# ---------------------------------------------------------------------------


class Methods:
    """The five methods a platform must have, and nothing else at all."""

    limits_reply: ClassVar[object] = Limits()
    publish_error: Exception | None = None

    async def limits(self, connection: Connection) -> Limits:
        return cast(Limits, self.limits_reply)

    async def start_login(self, request: LoginRequest) -> SendToNetwork:
        return SendToNetwork(url="https://x.example/authorize", state="s")

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> LoginStep:
        return Finished(connection=a_connection())

    async def refresh(self, connection: Connection) -> Token:
        return Token(access_token="new")

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        if self.publish_error is not None:
            raise self.publish_error
        return PostResult(id="1")


class Bare(Methods):
    name = "bare"
    features = Feature.POST_TEXT


class Nameless(Methods):
    features = Feature.POST_TEXT


class BadName(Methods):
    name = "My Platform"
    features = Feature.POST_TEXT


class NotAFeature(Methods):
    name = "notafeature"
    features = "everything"


class NoWayToPost(Methods):
    name = "nowaytopost"
    features = Feature.READ_POSTS | Feature.READ_STATS


class LiesAboutApps(Bare):
    features = Feature.POST_TEXT | Feature.CREATE_APP


class LiesAboutPush(Bare):
    features = Feature.POST_TEXT | Feature.PUSH_UPDATES


class LiesAboutDeleting(Bare):
    features = Feature.POST_TEXT | Feature.DELETE_POST


class LimitsIsADict(Methods):
    name = "limitsisadict"
    features = Feature.POST_TEXT
    limits_reply: ClassVar[object] = {"max_text_length": 100}


class ZeroLimits(Methods):
    name = "zerolimits"
    features = Feature.POST_TEXT
    limits_reply: ClassVar[object] = Limits(max_text_length=100, max_images=0)


class Scheduler(Methods):
    name = "scheduler"
    features = Feature.POST_TEXT | Feature.SCHEDULE | Feature.REPLY


class RaisesAPlainError(Methods):
    name = "plainerror"
    features = Feature.POST_TEXT
    publish_error = ValueError("that is not how you do it")


class Reader(Methods):
    name = "reader"
    features = Feature.POST_TEXT
    updates_reply: ClassVar[object] = ()

    async def fetch_updates(
        self, connection: Connection, since: datetime | None
    ) -> Sequence[Update]:
        return cast(Sequence[Update], self.updates_reply)


class UpdatesAreNotASequence(Reader):
    updates_reply: ClassVar[object] = {"updates": []}


class UpdatesAreNotUpdates(Reader):
    updates_reply: ClassVar[object] = [object()]


class MissingPublish:
    """Four of the five methods. The easiest mistake there is."""

    name = "missingpublish"
    features = Feature.POST_TEXT

    async def limits(self, connection: Connection) -> Limits:
        return Limits()

    async def start_login(self, request: LoginRequest) -> SendToNetwork:
        return SendToNetwork(url="https://x.example/authorize", state="s")

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> LoginStep:
        return Finished(connection=a_connection())

    async def refresh(self, connection: Connection) -> Token:
        return Token(access_token="new")


class SyncRefresh:
    """`refresh` written as a plain def. Not inheriting Methods, because an
    override this wrong is a type error, which is the point being made."""

    name = "syncrefresh"
    features = Feature.POST_TEXT

    async def limits(self, connection: Connection) -> Limits:
        return Limits()

    async def start_login(self, request: LoginRequest) -> SendToNetwork:
        return SendToNetwork(url="https://x.example/authorize", state="s")

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> LoginStep:
        return Finished(connection=a_connection())

    def refresh(self, connection: Connection) -> Token:
        return Token(access_token="new")

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        return PostResult(id="1")


class SendsThenRefuses(Methods):
    """Asks the network first, then works out the post was never allowed."""

    name = "eager"
    features = Feature.POST_TEXT
    limits_reply: ClassVar[object] = Limits(max_text_length=10)

    def __init__(self, transport: httpx.AsyncBaseTransport | None) -> None:
        self._transport = transport

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        async with HttpClient(
            "https://eager.example", platform="eager", transport=self._transport
        ) as http:
            await http.json("POST", "/posts", json={"text": post.text})
        raise InvalidPostError("too long, sorry")


# ---------------------------------------------------------------------------
# The kit checking the fake it ships with. pytest collects this class, runs
# every inherited check, and that is the proof the base is collectable.
# ---------------------------------------------------------------------------


class TestTheFakePlatform(PlatformChecks):
    def make_platform(self) -> Platform:
        return FakePlatform(transport=self.transport, updates=(an_update(),))

    def make_connection(self) -> Connection | None:
        return a_connection()

    def make_transport(self) -> httpx.AsyncBaseTransport | None:
        return RecordingTransport({"POST /posts": {"id": "99"}})


def good_checks() -> PlatformChecks:
    return checks_for(
        FakePlatform(updates=(an_update(),)),
        connection=a_connection(),
        transport=RecordingTransport({"POST /posts": {"id": "99"}}),
    )


class TestTheKitItself:
    def test_the_base_class_is_not_collected_by_pytest(self) -> None:
        # pytest collects classes called Test..., so the base has to not be
        # one. Rename it and every subclass silently stops being checked.
        assert not PlatformChecks.__name__.startswith("Test")

    def test_there_is_a_check_for_each_thing_we_promised(self) -> None:
        assert len(every_check()) == 9

    async def test_a_good_fake_platform_passes_every_check(self) -> None:
        for name in every_check():
            checks = good_checks()
            try:
                await getattr(checks, name)()
            except pytest.skip.Exception as skipped:
                pytest.fail(f"{name} skipped instead of running: {skipped}")

    def test_a_subclass_must_say_how_to_build_its_platform(self) -> None:
        with pytest.raises(NotImplementedError) as caught:
            PlatformChecks().make_platform()

        assert "make_platform" in str(caught.value)

    def test_a_subclass_without_the_optional_hooks_gets_nothing(self) -> None:
        checks = PlatformChecks()

        assert checks.make_connection() is None
        assert checks.make_transport() is None
        assert checks.transport is None


class TestTheMustHaveCheck:
    async def test_a_missing_method_is_named(self) -> None:
        message = await failure_from(
            checks_for(MissingPublish()),
            "test_it_provides_everything_a_platform_must",
        )

        assert "publish" in message

    async def test_a_method_written_as_a_plain_def_is_named(self) -> None:
        message = await failure_from(
            checks_for(SyncRefresh()),
            "test_it_provides_everything_a_platform_must",
        )

        assert "refresh" in message
        assert "async def" in message

    async def test_a_platform_with_no_name_is_told_so(self) -> None:
        message = await failure_from(
            checks_for(Nameless()),
            "test_it_provides_everything_a_platform_must",
        )

        assert "name" in message
        assert "features" in message


class TestTheNameCheck:
    async def test_a_name_with_spaces_and_capitals_is_refused(self) -> None:
        message = await failure_from(
            checks_for(BadName()), "test_its_name_can_be_an_entry_point_name"
        )

        assert "'My Platform'" in message
        assert "lowercase" in message


class TestTheFeaturesCheck:
    async def test_features_that_are_not_a_feature_are_refused(self) -> None:
        message = await failure_from(
            checks_for(NotAFeature()), "test_it_declares_at_least_one_way_to_post"
        )

        assert "not a Feature" in message

    async def test_a_platform_that_cannot_post_anything_is_refused(self) -> None:
        message = await failure_from(
            checks_for(NoWayToPost()), "test_it_declares_at_least_one_way_to_post"
        )

        assert "POST_TEXT" in message


class TestTheClaimsCheck:
    name = "test_everything_it_claims_in_features_it_can_actually_do"

    async def test_claiming_nothing_extra_passes(self) -> None:
        await getattr(checks_for(Bare()), self.name)()

    async def test_claiming_create_app_without_the_method_fails(self) -> None:
        message = await failure_from(checks_for(LiesAboutApps()), self.name)

        assert "CREATE_APP" in message
        assert "create_app" in message
        assert "async def" in message

    async def test_claiming_push_updates_without_the_methods_fails(self) -> None:
        message = await failure_from(checks_for(LiesAboutPush()), self.name)

        assert "PUSH_UPDATES" in message
        assert "check_signature" in message
        assert "read_update" in message
        # These two are called from a web handler before the body is parsed,
        # so they are plain functions, and the message has to say so.
        assert "a plain def" in message

    async def test_claiming_delete_post_without_the_method_fails(self) -> None:
        message = await failure_from(checks_for(LiesAboutDeleting()), self.name)

        assert "DELETE_POST" in message
        assert "delete_post" in message


class TestTheLimitsCheck:
    name = "test_its_limits_are_never_zero_for_unknown"

    async def test_something_that_is_not_a_limits_is_refused(self) -> None:
        checks = checks_for(LimitsIsADict(), connection=a_connection())

        message = await failure_from(checks, self.name)

        assert "not a Limits" in message

    async def test_zero_standing_in_for_unknown_is_refused(self) -> None:
        checks = checks_for(ZeroLimits(), connection=a_connection())

        message = await failure_from(checks, self.name)

        assert "max_images" in message
        assert "None" in message

    async def test_it_skips_without_an_account(self) -> None:
        message = await skip_from(checks_for(FakePlatform()), self.name)

        assert "make_connection" in message


class TestTheNoRequestCheck:
    name = "test_a_post_over_a_limit_is_refused_before_any_request"

    async def test_asking_the_network_first_is_refused(self) -> None:
        inner = RecordingTransport({"POST /posts": {"id": "1"}})

        # Built the way an author is told to build one: the platform is
        # handed `self.transport`, so the checks can see what it sends.
        class Checks(PlatformChecks):
            def make_platform(self) -> Platform:
                return cast(Platform, SendsThenRefuses(self.transport))

            def make_connection(self) -> Connection | None:
                return a_connection()

            def make_transport(self) -> httpx.AsyncBaseTransport | None:
                return inner

        message = await failure_from(Checks(), self.name)

        assert "1 request" in message
        assert "check_post" in message

    async def test_it_skips_when_no_length_limit_is_declared(self) -> None:
        checks = checks_for(
            Bare(),
            connection=a_connection(),
            transport=RecordingTransport(),
        )

        message = await skip_from(checks, self.name)

        assert "max_text_length" in message

    async def test_it_skips_without_a_transport(self) -> None:
        checks = checks_for(FakePlatform(), connection=a_connection())

        message = await skip_from(checks, self.name)

        assert "make_transport" in message


class TestTheSchedulingCheck:
    name = "test_scheduling_is_refused_when_it_cannot_schedule"

    async def test_publishing_a_scheduled_post_anyway_is_refused(self) -> None:
        checks = checks_for(Bare(), connection=a_connection())

        message = await failure_from(checks, self.name)

        assert "quiet wrong answer" in message

    async def test_raising_the_wrong_error_is_refused(self) -> None:
        checks = checks_for(RaisesAPlainError(), connection=a_connection())

        message = await failure_from(checks, self.name)

        assert "ValueError" in message
        assert "NotSupportedError" in message

    async def test_it_skips_when_the_platform_can_schedule(self) -> None:
        message = await skip_from(checks_for(Scheduler()), self.name)

        assert "SCHEDULE" in message


class TestTheErrorFamilyCheck:
    name = "test_the_errors_it_raises_are_all_socialchimp_errors"

    async def test_a_plain_python_error_is_refused(self) -> None:
        checks = checks_for(RaisesAPlainError(), connection=a_connection())

        message = await failure_from(checks, self.name)

        assert "ValueError" in message
        assert "SocialChimpError" in message

    async def test_a_platform_that_refuses_nothing_passes(self) -> None:
        # Bare takes everything handed to it. Nothing to catch, so nothing
        # to complain about - this check only judges how it says no.
        await getattr(checks_for(Bare(), connection=a_connection()), self.name)()

    async def test_it_skips_when_nothing_can_be_ruled_out(self) -> None:
        checks = checks_for(Scheduler(), connection=a_connection())

        message = await skip_from(checks, self.name)

        assert "nothing it can refuse" in message


class TestTheUpdatesCheck:
    name = "test_the_updates_it_reads_come_back_as_updates"

    async def test_it_skips_a_platform_that_cannot_read_updates(self) -> None:
        message = await skip_from(checks_for(Bare()), self.name)

        assert "fetch_updates" in message

    async def test_something_that_is_not_a_sequence_is_refused(self) -> None:
        checks = checks_for(UpdatesAreNotASequence(), connection=a_connection())

        message = await failure_from(checks, self.name)

        assert "sequence" in message

    async def test_items_that_are_not_updates_are_refused(self) -> None:
        checks = checks_for(UpdatesAreNotUpdates(), connection=a_connection())

        message = await failure_from(checks, self.name)

        assert "Update.from_network" in message

    async def test_an_empty_answer_is_fine(self) -> None:
        await getattr(checks_for(Reader(), connection=a_connection()), self.name)()


class TestTheWatchedTransport:
    async def test_it_sees_every_request_the_platform_sends(self) -> None:
        inner = RecordingTransport({"POST /posts": {"id": "7"}})
        checks = checks_for(FakePlatform(), transport=inner)
        watched = checks.transport
        assert watched is not None

        platform = FakePlatform(transport=watched)
        result = await platform.publish(a_connection(), Post(text="hi"))

        assert result.id == "7"
        assert checks.requests_or_skip() == inner.requests
        assert inner.paths == ["POST /posts"]


# ---------------------------------------------------------------------------
# The doubles, on their own.
# ---------------------------------------------------------------------------


class TestRecordingTransport:
    async def test_it_answers_from_the_table_and_keeps_the_request(self) -> None:
        transport = RecordingTransport({"GET /me": {"id": "42"}})

        async with HttpClient("https://x.example", transport=transport) as http:
            body = await http.json("GET", "/me")

        assert body == {"id": "42"}
        assert transport.paths == ["GET /me"]

    async def test_the_status_it_answers_with_can_be_changed(self) -> None:
        transport = RecordingTransport(
            {"GET /me": {"error": "slow down"}}, status_code=429
        )

        async with HttpClient(
            "https://x.example", platform="x", transport=transport
        ) as http:
            with pytest.raises(Exception) as caught:
                await http.json("GET", "/me")

        assert "slow down" in str(caught.value)

    async def test_an_unknown_path_says_what_it_does_know(self) -> None:
        transport = RecordingTransport({"GET /me": {}})

        reply = await transport.handle_async_request(
            httpx.Request("GET", "https://x.example/other")
        )

        assert reply.status_code == 404
        assert "GET /me" in reply.json()["error"]

    async def test_an_empty_table_says_so(self) -> None:
        transport = RecordingTransport()

        reply = await transport.handle_async_request(
            httpx.Request("GET", "https://x.example/other")
        )

        assert "nothing" in reply.json()["error"]

    async def test_your_own_answer_function_wins(self) -> None:
        def answer(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"path": request.url.path})

        transport = RecordingTransport({"GET /me": {}}, answer=answer)

        reply = await transport.handle_async_request(
            httpx.Request("GET", "https://x.example/anything")
        )

        assert reply.json() == {"path": "/anything"}


class TestRecordingStorage:
    def test_it_is_a_valid_storage(self) -> None:
        accepted: Storage = RecordingStorage()

        assert isinstance(accepted, Storage)

    async def test_it_stores_things_properly(self) -> None:
        storage = RecordingStorage()
        connection = a_connection()

        await storage.save_connection(connection)
        found = await storage.get_connection("conn-1")

        assert found == connection

    async def test_it_starts_with_whatever_you_seed_it_with(self) -> None:
        app = AppCredentials(
            platform="mastodon", host="m.example", client_id="c", client_secret="s"
        )
        storage = RecordingStorage(connections=[a_connection()], apps=[app])

        assert await storage.get_connection("conn-1") is not None
        assert await storage.get_app("mastodon", "m.example") == app

    async def test_it_remembers_every_call_in_order(self) -> None:
        storage = RecordingStorage()
        connection = a_connection()

        await storage.save_connection(connection)
        await storage.get_connection("conn-1")
        await storage.delete_connection("conn-1")
        await storage.save_app(
            AppCredentials(platform="p", host=None, client_id="c", client_secret="s")
        )
        await storage.get_app("p", None)

        assert storage.names() == [
            "save_connection",
            "get_connection",
            "delete_connection",
            "save_app",
            "get_app",
        ]
        assert storage.calls_to("get_app") == [StorageCall("get_app", ("p", None))]

    async def test_deleting_something_that_is_gone_is_quiet(self) -> None:
        storage = RecordingStorage()

        await storage.delete_connection("never-existed")

        assert await storage.get_connection("never-existed") is None

    async def test_it_can_be_told_to_fail(self) -> None:
        storage = RecordingStorage()
        storage.fails("save_connection", ConfigError("the database is down"))

        with pytest.raises(ConfigError):
            await storage.save_connection(a_connection())

        # The call still happened, so it is still recorded.
        assert storage.names() == ["save_connection"]

    async def test_resetting_forgets_the_calls_but_not_the_data(self) -> None:
        storage = RecordingStorage()
        await storage.save_connection(a_connection())

        storage.reset()

        assert storage.names() == []
        assert await storage.get_connection("conn-1") is not None


class TestFakePlatform:
    async def test_it_publishes_from_memory_without_a_transport(self) -> None:
        platform = FakePlatform()

        result = await platform.publish(platform.connection(), Post(text="hi"))

        assert result.id == "1"
        assert platform.published == [("fake-connection", Post(text="hi"))]

    async def test_it_really_sends_a_request_when_given_a_transport(self) -> None:
        transport = RecordingTransport({"POST /posts": {"id": "abc"}})
        platform = FakePlatform(transport=transport)

        result = await platform.publish(platform.connection(), Post(text="hi"))

        assert result.id == "abc"
        assert result.raw == {"id": "abc"}
        assert json.loads(transport.requests[0].content) == {"text": "hi"}

    async def test_it_refuses_a_post_that_breaks_its_own_limits(self) -> None:
        platform = FakePlatform(limits=Limits(max_text_length=5))

        with pytest.raises(InvalidPostError):
            await platform.publish(platform.connection(), Post(text="far too long"))

    async def test_it_can_be_told_to_fail_every_post(self) -> None:
        platform = FakePlatform(publish_fails_with=NotFoundError("gone"))

        with pytest.raises(NotFoundError):
            await platform.publish(platform.connection(), Post(text="hi"))

        assert platform.published == []

    async def test_signing_in_hands_back_a_connection(self) -> None:
        platform = FakePlatform()
        request = LoginRequest(redirect_uri="https://app.example/back")

        sent = await platform.start_login(request)
        step = await platform.finish_login(request, {"code": "abc"})

        assert sent.state == "fake-state"
        assert sent.url.endswith("state=fake-state")
        assert isinstance(step, Finished)
        assert step.connection.account_id == "42"

    async def test_a_state_you_chose_is_kept(self) -> None:
        platform = FakePlatform()
        request = LoginRequest(redirect_uri="https://app.example/back", state="mine")

        sent = await platform.start_login(request)

        assert sent.state == "mine"

    async def test_giving_it_accounts_makes_signing_in_ask_which(self) -> None:
        platform = FakePlatform(accounts=(AccountChoice(id="7", name="A page"),))
        request = LoginRequest(redirect_uri="https://app.example/back")

        asked = await platform.finish_login(request, {"code": "abc"})
        assert isinstance(asked, ChooseAccount)
        assert asked.options[0].name == "A page"

        done = await platform.finish_login(request, {"code": "abc", "account": "7"})
        assert isinstance(done, Finished)
        assert done.connection.account_id == "7"

    async def test_refreshing_hands_back_a_new_token(self) -> None:
        platform = FakePlatform()

        token = await platform.refresh(platform.connection())

        assert token.refresh_token != "fake-refresh"
        assert token.expires_at is not None
        assert platform.refreshed == ["fake-connection"]

    async def test_a_token_that_never_expires(self) -> None:
        platform = FakePlatform(token_lifetime=None)

        token = await platform.refresh(platform.connection())

        assert token.expires_at is None
        assert platform.connection().token.expires_at is None

    async def test_it_registers_an_app(self) -> None:
        platform = FakePlatform()

        app = await platform.create_app(
            name="My App", redirect_uri="https://app.example/back", host="m.example"
        )

        assert app.key == ("fake", "m.example")
        assert platform.created_apps == [app]

    async def test_it_deletes_a_post_it_published(self) -> None:
        platform = FakePlatform()
        result = await platform.publish(platform.connection(), Post(text="hi"))

        await platform.delete_post(platform.connection(), result.id)

        assert platform.deleted == [result.id]

    async def test_deleting_a_post_it_never_made_is_not_found(self) -> None:
        platform = FakePlatform()

        with pytest.raises(NotFoundError):
            await platform.delete_post(platform.connection(), "nope")

    async def test_it_reads_updates_since_a_moment(self) -> None:
        old = an_update("old", datetime(2026, 1, 1, tzinfo=UTC))
        new = an_update("new", datetime(2026, 6, 1, tzinfo=UTC))
        platform = FakePlatform(updates=(old, new))

        everything = await platform.fetch_updates(platform.connection(), None)
        recent = await platform.fetch_updates(
            platform.connection(), datetime(2026, 3, 1, tzinfo=UTC)
        )

        assert list(everything) == [old, new]
        assert list(recent) == [new]

    def test_it_checks_a_signature_it_made_itself(self) -> None:
        platform = FakePlatform()
        body = b'{"id": "1"}'

        platform.check_signature(body, platform.sign(body), secret=platform.secret)

    def test_a_signature_made_with_another_secret_is_refused(self) -> None:
        platform = FakePlatform()
        body = b'{"id": "1"}'

        with pytest.raises(SignatureError):
            platform.check_signature(
                body, platform.sign(body, secret="wrong"), secret=platform.secret
            )

    def test_it_turns_a_pushed_body_into_an_update(self) -> None:
        platform = FakePlatform()
        body = json.dumps(
            {
                "id": "9",
                "kind": "comment_created",
                "connection_id": "conn-1",
                "at": "2026-01-01T00:00:00+00:00",
            }
        ).encode()

        update = platform.read_update(body, platform.sign(body))

        assert update.kind is UpdateKind.COMMENT_CREATED
        assert update.created_at == datetime(2026, 1, 1, tzinfo=UTC)
        assert update.platform == "fake"
