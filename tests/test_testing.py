"""Tests for the kit that checks a platform behaves like the others."""

import json
import subprocess
import sys
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
    MissingPermissionError,
    Person,
    Post,
    PostGoneError,
    PostResult,
    PostState,
    RateLimitError,
    SignatureError,
    Storage,
    Thread,
    Token,
    Update,
    UpdateKind,
)
from socialchimp import testing as testing_module
from socialchimp.errors import (
    AuthError,
    ConfigError,
    NotFoundError,
    NotSupportedError,
)
from socialchimp.features import TextCount
from socialchimp.http import HttpClient
from socialchimp.models import Media, MediaKind, RawData
from socialchimp.platform import (
    AccountChoice,
    AskForDetails,
    CanAnswerSetupCheck,
    CanCheckState,
    CanLike,
    CanMessage,
    CanReadLikes,
    CanReadPost,
    CanReadPushedUpdates,
    CanReadThread,
    CanReadUpdatesAfter,
    CanReply,
    CanResumeLogin,
    CanStartConversations,
    ChooseAccount,
    Finished,
    LoginField,
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
    """The methods a platform must have, and nothing else at all."""

    limits_reply: ClassVar[object] = Limits()
    address: ClassVar[object] = "https://x.example"
    headers_reply: ClassVar[object] = {"Authorization": "Bearer abc"}
    publish_error: Exception | None = None

    def api_base(self, connection: Connection) -> str:
        return cast(str, self.address)

    def auth_headers(self, connection: Connection) -> Mapping[str, str]:
        return cast(Mapping[str, str], self.headers_reply)

    async def limits(self, connection: Connection) -> Limits:
        return cast(Limits, self.limits_reply)

    async def start_login(self, request: LoginRequest) -> LoginStep:
        return SendToNetwork(url="https://x.example/authorize", state="s")

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> LoginStep:
        return Finished(connection=a_connection())

    async def refresh(
        self,
        connection: Connection,
        app: AppCredentials | None = None,
    ) -> Token:
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


class PicturesOnly(Methods):
    """A network that takes pictures and nothing written on its own."""

    name = "picturesonly"
    features = Feature.POST_IMAGE


class VideoOnly(Methods):
    """A network with no text-only post at all, the way YouTube has none."""

    name = "videoonly"
    features = Feature.POST_VIDEO
    limits_reply: ClassVar[object] = Limits(max_text_length=300)

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        if not post.media:
            raise NotSupportedError(platform=self.name, what="text-only posts")
        return PostResult(id="1")


class TakesWordsAnyway(Methods):
    """Says it cannot post text, then takes a post made of nothing else."""

    name = "takeswords"
    features = Feature.POST_VIDEO


class RefusesTextWithTheWrongError(Methods):
    """Turns words away, but not in a way an app can read."""

    name = "wrongtextrefusal"
    features = Feature.POST_VIDEO
    publish_error = InvalidPostError("no idea what you mean")


class LiesAboutApps(Bare):
    features = Feature.POST_TEXT | Feature.CREATE_APP


class LiesAboutPush(Bare):
    features = Feature.POST_TEXT | Feature.PUSH_UPDATES


class LiesAboutDeleting(Bare):
    features = Feature.POST_TEXT | Feature.DELETE_POST


class LiesAboutStats(Bare):
    features = Feature.POST_TEXT | Feature.READ_STATS


class LimitsIsADict(Methods):
    name = "limitsisadict"
    features = Feature.POST_TEXT
    limits_reply: ClassVar[object] = {"max_text_length": 100}


class ZeroLimits(Methods):
    name = "zerolimits"
    features = Feature.POST_TEXT
    limits_reply: ClassVar[object] = Limits(max_text_length=100, max_images=0)


# One thumbs-up with a skin tone on it: one letter, two characters.
A_BIG_LETTER = "\U0001f44d\U0001f3fd"


class CountsCharactersButSaysLetters(Methods):
    """Says it counts letters, then counts characters like everyone else."""

    name = "saysletters"
    features = Feature.POST_TEXT
    limits_reply: ClassVar[object] = Limits(
        max_text_length=300,
        max_text_bytes=3000,
        text_counted_in=TextCount.GRAPHEMES,
    )

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        if len(post.text) > 300:
            raise InvalidPostError("too long, sorry")
        return PostResult(id="1")


class SaysNothingRealAboutCounting(Methods):
    """Puts a word where a TextCount belongs."""

    name = "notatextcount"
    features = Feature.POST_TEXT

    limits_reply: ClassVar[object] = Limits(
        max_text_length=300,
        text_counted_in=cast(TextCount, "letters"),
    )


class RefusesWithTheWrongError(Methods):
    """Refuses a post that is too long, but not as an InvalidPostError."""

    name = "wrongerror"
    features = Feature.POST_TEXT
    limits_reply: ClassVar[object] = Limits(max_text_length=10)
    publish_error = NotFoundError("no idea what you mean")


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

    def api_base(self, connection: Connection) -> str:
        return "https://x.example"

    def auth_headers(self, connection: Connection) -> Mapping[str, str]:
        return {"Authorization": "Bearer abc"}

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

    async def refresh(
        self,
        connection: Connection,
        app: AppCredentials | None = None,
    ) -> Token:
        return Token(access_token="new")


class SyncRefresh:
    """`refresh` written as a plain def. Not inheriting Methods, because an
    override this wrong is a type error, which is the point being made."""

    name = "syncrefresh"
    features = Feature.POST_TEXT

    def api_base(self, connection: Connection) -> str:
        return "https://x.example"

    def auth_headers(self, connection: Connection) -> Mapping[str, str]:
        return {"Authorization": "Bearer abc"}

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


class AddressIsNotAnAddress(Methods):
    """Gives a bare server name where a whole address belongs."""

    name = "bareaddress"
    features = Feature.POST_TEXT
    address: ClassVar[object] = "x.example"


class AddressEndsInASlash(Methods):
    """Gives an address a path would be joined onto twice."""

    name = "trailingslash"
    features = Feature.POST_TEXT
    address: ClassVar[object] = "https://x.example/"


class AsyncApiBase:
    """`api_base` written as an `async def`. Not inheriting Methods, because
    an override this wrong is a type error, which is the point being made."""

    name = "asyncapibase"
    features = Feature.POST_TEXT

    async def api_base(self, connection: Connection) -> str:
        return "https://x.example"


class HeadersAreNotAMapping(Methods):
    """Hands back one header as text, rather than a mapping of them."""

    name = "headersaretext"
    features = Feature.POST_TEXT
    headers_reply: ClassVar[object] = "Bearer abc"


class HeadersAreNotText(Methods):
    """Hands back a header value that cannot go on a request as it is."""

    name = "headersarenumbers"
    features = Feature.POST_TEXT
    headers_reply: ClassVar[object] = {"X-Count": 3}


class AsksForNothing(Methods):
    """Says it has no sign-in page, then asks for nothing at all."""

    name = "asksfornothing"
    features = Feature.POST_TEXT

    async def start_login(self, request: LoginRequest) -> LoginStep:
        return AskForDetails(fields=())


class AsksForABlankBox(Methods):
    """Asks for something without saying what it is."""

    name = "blankbox"
    features = Feature.POST_TEXT

    async def start_login(self, request: LoginRequest) -> LoginStep:
        return AskForDetails(
            fields=(
                LoginField(name="handle", label="Your handle"),
                LoginField(name="app_password", label=""),
            )
        )


class CarriesOnWithoutWaiting(Methods):
    """Pauses to ask which page, then carries on without awaiting anything."""

    name = "carriesonwithoutwaiting"
    features = Feature.POST_TEXT

    def resume_login(
        self,
        request: LoginRequest,
        *,
        resume_token: str,
        account_id: str,
        remember: RawData | None = None,
    ) -> LoginStep:
        return Finished(connection=a_connection())


class CarriesOnItsOwnWay(Methods):
    """Has a resume_login, under names socialchimp never calls it with."""

    name = "carriesonitsownway"
    features = Feature.POST_TEXT

    async def resume_login(
        self,
        request: LoginRequest,
        token: str = "",
        account: str = "",
    ) -> LoginStep:
        return Finished(connection=a_connection())


class CarriesOnHoweverItIsAsked(Methods):
    """Takes whatever it is handed, which is nobody's mistake."""

    name = "carriesonhoweveritisasked"
    features = Feature.POST_TEXT

    async def resume_login(
        self,
        request: LoginRequest,
        **whatever: object,
    ) -> LoginStep:
        return Finished(connection=a_connection())


class WantsCredentialsFirst(Methods):
    """Will not start a login until your app is registered, as OAuth does."""

    name = "wantscredentials"
    features = Feature.POST_TEXT

    async def start_login(self, request: LoginRequest) -> LoginStep:
        raise ConfigError("this login request carries no app credentials")


class NeedsNoAppButWantsOneAnyway(Methods):
    """Claims there is no app to register, then refuses without one."""

    name = "needsnoapp"
    features = Feature.NEEDS_NO_APP | Feature.POST_TEXT

    async def start_login(self, request: LoginRequest) -> LoginStep:
        if request.app is None:
            raise ConfigError("no app credentials are stored for needsnoapp")
        return SendToNetwork(url="https://x.example/authorize", state="s")


class NeedsNoApp(Methods):
    """Signs somebody in with nothing saved first, the way Bluesky does."""

    name = "reallyneedsnoapp"
    features = Feature.NEEDS_NO_APP | Feature.POST_TEXT

    async def start_login(self, request: LoginRequest) -> LoginStep:
        return AskForDetails(fields=(LoginField(name="handle", label="Your handle"),))


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

    # The checks the fake has nothing to answer: it posts text, and it has
    # an app to register like every network but Bluesky. Skipping those two
    # is the right answer rather than a gap.
    NOTHING_TO_ANSWER = frozenset(
        {
            "test_a_text_only_post_is_refused_when_it_cannot_post_text",
            "test_a_platform_with_no_app_starts_a_login_without_one",
        }
    )

    def test_there_is_a_check_for_each_thing_we_promised(self) -> None:
        assert len(every_check()) == 17

    async def test_a_good_fake_platform_passes_every_check(self) -> None:
        for name in every_check():
            checks = good_checks()
            try:
                await getattr(checks, name)()
            except pytest.skip.Exception as skipped:
                if name not in self.NOTHING_TO_ANSWER:
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
        assert "api_base" in message
        assert "auth_headers" in message


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

    async def test_claiming_read_stats_without_the_method_fails(self) -> None:
        message = await failure_from(checks_for(LiesAboutStats()), self.name)

        assert "READ_STATS" in message
        assert "read_stats" in message


class TestTheAddressCheck:
    name = "test_it_says_where_its_api_lives"

    async def test_a_bare_server_name_is_refused(self) -> None:
        checks = checks_for(AddressIsNotAnAddress(), connection=a_connection())

        message = await failure_from(checks, self.name)

        assert "'x.example'" in message
        assert "https://" in message

    async def test_an_address_that_ends_in_a_slash_is_refused(self) -> None:
        checks = checks_for(AddressEndsInASlash(), connection=a_connection())

        message = await failure_from(checks, self.name)

        assert "ends in a slash" in message
        assert "two" in message

    async def test_working_it_out_only_when_awaited_is_refused(self) -> None:
        checks = checks_for(AsyncApiBase(), connection=a_connection())

        message = await failure_from(checks, self.name)

        assert "api_base" in message
        assert "async def" in message

    async def test_it_skips_without_an_account(self) -> None:
        message = await skip_from(checks_for(FakePlatform()), self.name)

        assert "make_connection" in message


class TestTheHeadersCheck:
    name = "test_it_says_what_headers_prove_who_we_are"

    async def test_something_that_is_not_a_mapping_is_refused(self) -> None:
        checks = checks_for(HeadersAreNotAMapping(), connection=a_connection())

        message = await failure_from(checks, self.name)

        assert "'Bearer abc'" in message
        assert "Authorization" in message

    async def test_a_header_that_is_not_text_is_refused(self) -> None:
        checks = checks_for(HeadersAreNotText(), connection=a_connection())

        message = await failure_from(checks, self.name)

        assert "X-Count" in message

    async def test_it_skips_without_an_account(self) -> None:
        message = await skip_from(checks_for(FakePlatform()), self.name)

        assert "make_connection" in message


class TestTheLoginFormCheck:
    name = "test_the_details_it_asks_for_can_be_shown_in_a_form"

    async def test_asking_for_nothing_at_all_is_refused(self) -> None:
        message = await failure_from(checks_for(AsksForNothing()), self.name)

        assert "no fields" in message

    async def test_a_box_with_no_label_on_it_is_refused(self) -> None:
        message = await failure_from(checks_for(AsksForABlankBox()), self.name)

        assert "app_password" in message
        assert "label" in message

    async def test_a_form_a_person_can_fill_in_passes(self) -> None:
        platform = FakePlatform(
            ask_for=(
                LoginField(name="handle", label="Your handle"),
                LoginField(name="app_password", label="App password", secret=True),
            )
        )

        await getattr(checks_for(platform), self.name)()

    async def test_a_platform_that_sends_people_to_a_sign_in_page_passes(self) -> None:
        # Nothing of ours is drawn, so there is no form to look at.
        await getattr(checks_for(FakePlatform()), self.name)()

    async def test_it_skips_a_platform_that_wants_credentials_first(self) -> None:
        message = await skip_from(checks_for(WantsCredentialsFirst()), self.name)

        assert "credentials" in message


class TestTheNoAppCheck:
    name = "test_a_platform_with_no_app_starts_a_login_without_one"

    async def test_claiming_no_app_and_then_asking_for_one_is_refused(self) -> None:
        message = await failure_from(
            checks_for(NeedsNoAppButWantsOneAnyway()), self.name
        )

        assert "NEEDS_NO_APP" in message
        assert "ConfigError" in message

    async def test_a_platform_that_really_needs_none_passes(self) -> None:
        await getattr(checks_for(NeedsNoApp()), self.name)()

    async def test_it_skips_a_platform_that_has_an_app_like_every_other(self) -> None:
        message = await skip_from(checks_for(FakePlatform()), self.name)

        assert "NEEDS_NO_APP" in message


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


class TestTheCountingCheck:
    name = "test_it_counts_text_the_way_it_says_it_does"

    async def test_counting_characters_while_claiming_letters_is_refused(
        self,
    ) -> None:
        # The bug this whole check exists for: 300 letters is 600 characters
        # once somebody uses emoji, and a platform counting characters
        # refuses a post the network would have taken.
        checks = checks_for(
            CountsCharactersButSaysLetters(),
            connection=a_connection(),
            transport=RecordingTransport(),
        )

        message = await failure_from(checks, self.name)

        assert "letters" in message
        assert "300" in message

    async def test_letting_through_a_post_over_its_own_count_is_refused(
        self,
    ) -> None:
        inner = RecordingTransport({"POST /posts": {"id": "1"}})

        class Checks(PlatformChecks):
            def make_platform(self) -> Platform:
                return cast(Platform, SendsThenRefuses(self.transport))

            def make_connection(self) -> Connection | None:
                return a_connection()

            def make_transport(self) -> httpx.AsyncBaseTransport | None:
                return inner

        message = await failure_from(Checks(), self.name)

        assert "sent" in message

    async def test_refusing_with_the_wrong_error_is_refused(self) -> None:
        checks = checks_for(
            RefusesWithTheWrongError(),
            connection=a_connection(),
            transport=RecordingTransport(),
        )

        message = await failure_from(checks, self.name)

        assert "InvalidPostError" in message
        assert "NotFoundError" in message

    async def test_something_that_is_not_a_way_of_counting_is_refused(
        self,
    ) -> None:
        checks = checks_for(
            SaysNothingRealAboutCounting(),
            connection=a_connection(),
            transport=RecordingTransport(),
        )

        message = await failure_from(checks, self.name)

        assert "TextCount" in message

    async def test_it_skips_when_no_length_limit_is_declared(self) -> None:
        checks = checks_for(
            Bare(),
            connection=a_connection(),
            transport=RecordingTransport(),
        )

        message = await skip_from(checks, self.name)

        assert "max_text_length" in message


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


class TestBuildingAPostToCheckWith:
    """The post the length checks measure, and where it comes from."""

    async def test_a_platform_that_takes_text_gets_words_and_nothing_else(
        self,
    ) -> None:
        post = checks_for(Bare()).make_post("hello")

        assert post.text == "hello"
        assert post.media == ()

    async def test_a_platform_with_no_text_post_gets_a_picture(self) -> None:
        # Otherwise the length checks measure a post the platform was always
        # going to turn away, and pass without measuring anything.
        post = checks_for(PicturesOnly()).make_post("hello")

        assert post.text == "hello"
        assert [item.kind for item in post.media] == [MediaKind.IMAGE]

    async def test_a_network_that_only_takes_video_gets_a_video(self) -> None:
        post = checks_for(VideoOnly()).make_post("hello")

        assert [item.kind for item in post.media] == [MediaKind.VIDEO]

    async def test_a_platform_that_can_post_nothing_skips(self) -> None:
        checks = checks_for(NoWayToPost())

        with pytest.raises(pytest.skip.Exception) as skipped:
            checks.make_post("hello")

        assert "make_post" in str(skipped.value)


class TestTheTextOnlyCheck:
    name = "test_a_text_only_post_is_refused_when_it_cannot_post_text"

    async def test_a_platform_that_says_so_plainly_passes(self) -> None:
        checks = checks_for(VideoOnly(), connection=a_connection())

        await getattr(checks, self.name)()

    async def test_publishing_words_anyway_is_refused(self) -> None:
        checks = checks_for(TakesWordsAnyway(), connection=a_connection())

        message = await failure_from(checks, self.name)

        assert "POST_TEXT" in message
        assert "NotSupportedError" in message

    async def test_refusing_with_the_wrong_error_is_refused(self) -> None:
        # An app catches NotSupportedError to say "this network cannot do
        # that". An InvalidPostError reads as "fix your post", which is
        # advice nobody can follow here.
        checks = checks_for(RefusesTextWithTheWrongError(), connection=a_connection())

        message = await failure_from(checks, self.name)

        assert "InvalidPostError" in message
        assert "NotSupportedError" in message

    async def test_it_skips_when_the_platform_can_post_text(self) -> None:
        message = await skip_from(checks_for(Bare()), self.name)

        assert "POST_TEXT" in message

    async def test_it_skips_without_a_connection(self) -> None:
        message = await skip_from(checks_for(VideoOnly()), self.name)

        assert "make_connection" in message


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


class TestNamingAFakeConnection:
    def test_a_connection_is_named_after_the_network_and_the_account(self) -> None:
        # Every real platform names them this way, and apps are told to
        # match webhooks on the id.
        assert FakePlatform().connection().id == "fake:42"
        assert FakePlatform().connection(account_id="7").id == "fake:7"

    def test_two_fake_networks_do_not_collide(self) -> None:
        # An app testing across nine fakes used to get nine rows with the
        # same primary key.
        first = FakePlatform(name="one").connection()
        second = FakePlatform(name="two").connection()

        assert {first.id, second.id} == {"one:42", "two:42"}

    def test_an_id_you_pass_is_still_the_one_used(self) -> None:
        given = FakePlatform().connection(connection_id="mine")

        assert given.id == "mine"


class TestAFakeSetupCheck:
    def test_a_fake_answers_the_handshake_the_way_meta_asks_it(self) -> None:
        platform = FakePlatform()

        answer = platform.answer_setup_check(
            {
                "hub.mode": "subscribe",
                "hub.verify_token": "chosen-by-me",
                "hub.challenge": "1158201444",
            },
            verify_token="chosen-by-me",
        )

        assert isinstance(platform, CanAnswerSetupCheck)
        assert answer == "1158201444"

    def test_the_wrong_token_is_refused(self) -> None:
        platform = FakePlatform()

        with pytest.raises(SignatureError):
            platform.answer_setup_check(
                {
                    "hub.mode": "subscribe",
                    "hub.verify_token": "somebody-elses",
                    "hub.challenge": "9",
                },
                verify_token="chosen-by-me",
            )

    def test_a_fake_told_not_to_answer_has_no_answer_at_all(self) -> None:
        # The same trick as accounts and states: a fake standing in for a
        # network that asks nothing first must not claim it can.
        quiet = FakePlatform(answers_setup_checks=False)

        assert not isinstance(quiet, CanAnswerSetupCheck)

    def test_a_subclass_that_wrote_its_own_keeps_it(self) -> None:
        class AlwaysAgrees(FakePlatform):
            def answer_setup_check(
                self,
                params: Mapping[str, str],
                *,
                verify_token: str,
            ) -> str:
                return "always"

        assert AlwaysAgrees().answer_setup_check({}, verify_token="x") == "always"


class TestFakePlatform:
    def test_a_pushed_request_can_be_read_the_way_facebook_is_read(self) -> None:
        # SocialChimp.read_updates looks for read_updates, so a fake standing
        # in for a pushing network has to have one too.
        platform = FakePlatform()
        body = json.dumps(
            {
                "id": "u9",
                "kind": "comment_created",
                "connection_id": "fake:42",
                "at": datetime.now(UTC).isoformat(),
            }
        ).encode()

        found = platform.read_updates(body)

        assert isinstance(platform, CanReadPushedUpdates)
        assert [update.id for update in found] == ["u9"]

    async def test_a_fake_with_no_states_has_no_check_state(self) -> None:
        # Most networks are finished by the time publish returns, so the
        # fake is too unless a test says otherwise.
        assert not isinstance(FakePlatform(), CanCheckState)

    async def test_a_fake_given_states_can_be_asked_how_a_post_is_going(
        self,
    ) -> None:
        platform = FakePlatform(states=(PostState.PROCESSING, PostState.DONE))
        connection = platform.connection()

        first = await platform.check_state(connection, "post-1")
        second = await platform.check_state(connection, "post-1")
        third = await platform.check_state(connection, "post-1")

        assert isinstance(platform, CanCheckState)
        assert first.state is PostState.PROCESSING
        # The last one repeats, so a post that is done stays done however
        # many times a test asks.
        assert second.state is PostState.DONE
        assert third.state is PostState.DONE
        assert platform.state_asked == [("fake:42", "post-1")] * 3

    async def test_a_subclass_that_wrote_its_own_check_state_keeps_it(self) -> None:
        class SaysItFailed(FakePlatform):
            async def check_state(
                self, connection: Connection, post_id: str
            ) -> PostResult:
                return PostResult(id=post_id, state=PostState.FAILED)

        platform = SaysItFailed(states=(PostState.DONE,))

        result = await platform.check_state(platform.connection(), "post-1")

        assert result.state is PostState.FAILED

    async def test_it_publishes_from_memory_without_a_transport(self) -> None:
        platform = FakePlatform()

        result = await platform.publish(platform.connection(), Post(text="hi"))

        assert result.id == "1"
        assert platform.published == [("fake:42", Post(text="hi"))]

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

    def test_it_says_where_it_lives_and_how_to_prove_who_we_are(self) -> None:
        platform = FakePlatform()
        connection = platform.connection()

        assert platform.api_base(connection) == "https://fake.example"
        assert platform.auth_headers(connection) == {
            "Authorization": "Bearer fake-access"
        }

    async def test_signing_in_hands_back_a_connection(self) -> None:
        platform = FakePlatform()
        request = LoginRequest(redirect_uri="https://app.example/back")

        sent = await platform.start_login(request)
        step = await platform.finish_login(request, {"code": "abc"})

        assert isinstance(sent, SendToNetwork)
        assert sent.state == "fake-state"
        assert sent.url.endswith("state=fake-state")
        assert isinstance(step, Finished)
        assert step.connection.account_id == "42"

    async def test_a_state_you_chose_is_kept(self) -> None:
        platform = FakePlatform()
        request = LoginRequest(redirect_uri="https://app.example/back", state="mine")

        sent = await platform.start_login(request)

        assert isinstance(sent, SendToNetwork)
        assert sent.state == "mine"

    async def test_it_can_ask_for_details_instead_of_sending_anyone_away(self) -> None:
        # What Bluesky and the bot-token networks do: there is no sign-in
        # page, so the fake says what a form should ask for.
        platform = FakePlatform(
            ask_for=(
                LoginField(name="app_password", label="App password", secret=True),
            )
        )

        step = await platform.start_login(
            LoginRequest(redirect_uri="https://app.example/back")
        )

        assert isinstance(step, AskForDetails)
        assert step.fields[0].name == "app_password"
        assert step.fields[0].secret is True
        assert step.help_url is not None

    async def test_giving_it_accounts_makes_signing_in_ask_which(self) -> None:
        platform = FakePlatform(accounts=(AccountChoice(id="7", name="A page"),))
        request = LoginRequest(redirect_uri="https://app.example/back")

        asked = await platform.finish_login(request, {"code": "abc"})
        assert isinstance(asked, ChooseAccount)
        assert asked.options[0].name == "A page"

        done = await platform.finish_login(request, {"code": "abc", "account": "7"})
        assert isinstance(done, Finished)
        assert done.connection.account_id == "7"

    async def test_it_carries_a_login_on_once_an_account_is_picked(self) -> None:
        platform = FakePlatform(accounts=(AccountChoice(id="7", name="A page"),))
        request = LoginRequest(redirect_uri="https://app.example/back")

        done = await platform.resume_login(
            request,
            resume_token="fake-resume",
            account_id="7",
            remember={"verifier": "fake-verifier"},
        )

        assert isinstance(done, Finished)
        assert done.connection.account_id == "7"
        assert platform.resumed == [("fake-resume", "7")]
        assert platform.last_remember == {"verifier": "fake-verifier"}

    def test_a_fake_that_never_asks_cannot_be_carried_on(self) -> None:
        # A platform that signs somebody in in one step has nothing to carry
        # on from, and socialchimp reads CanResumeLogin to decide. A fake
        # that claimed it anyway would let a wrong app pass its own tests.
        assert not isinstance(FakePlatform(), CanResumeLogin)
        assert isinstance(
            FakePlatform(accounts=(AccountChoice(id="7", name="A page"),)),
            CanResumeLogin,
        )

    async def test_a_subclass_keeps_its_own_way_of_carrying_on(self) -> None:
        class MyFake(FakePlatform):
            async def resume_login(
                self,
                request: LoginRequest,
                *,
                resume_token: str,
                account_id: str,
                remember: RawData | None = None,
            ) -> LoginStep:
                return Finished(connection=self.connection(account_id="mine"))

        platform = MyFake(accounts=(AccountChoice(id="7", name="A page"),))

        done = await platform.resume_login(
            LoginRequest(redirect_uri="https://app.example/back"),
            resume_token="fake-resume",
            account_id="7",
        )

        assert isinstance(done, Finished)
        assert done.connection.account_id == "mine"

    async def test_signing_in_can_be_made_to_fail(self) -> None:
        # A code already used, a person who changed their mind: the second
        # half of a sign-in is where a network refuses.
        refused = AuthError("that code has already been used", platform="fake")
        platform = FakePlatform(
            accounts=(AccountChoice(id="7", name="A page"),),
            login_fails_with=refused,
        )
        request = LoginRequest(redirect_uri="https://app.example/back")

        with pytest.raises(AuthError):
            await platform.finish_login(request, {"code": "abc"})

        with pytest.raises(AuthError):
            await platform.resume_login(
                request, resume_token="fake-resume", account_id="7"
            )

    async def test_a_failing_sign_in_still_writes_down_what_it_was_handed(
        self,
    ) -> None:
        platform = FakePlatform(login_fails_with=AuthError("no", platform="fake"))

        with pytest.raises(AuthError):
            await platform.finish_login(
                LoginRequest(redirect_uri="https://app.example/back"),
                {"code": "abc"},
                {"verifier": "fake-verifier"},
            )

        assert platform.last_remember == {"verifier": "fake-verifier"}

    async def test_refreshing_hands_back_a_new_token(self) -> None:
        platform = FakePlatform()

        token = await platform.refresh(platform.connection())

        assert token.refresh_token != "fake-refresh"
        assert token.expires_at is not None
        assert platform.refreshed == ["fake:42"]
        assert platform.refreshed_with == [None]

    async def test_it_writes_down_the_credentials_a_renewal_was_given(
        self,
    ) -> None:
        # Most networks will not renew without them, so a test needs a way
        # to say they really arrived rather than hoping they did.
        platform = FakePlatform()
        app = AppCredentials(
            platform="fake",
            host=None,
            client_id="client-id",
            client_secret="client-secret",
        )

        await platform.refresh(platform.connection(), app)

        assert platform.refreshed_with == [app]

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


class TestTheAskingHowItIsGoingCheck:
    name = "test_a_platform_that_keeps_working_can_be_asked_how_it_is_going"

    async def test_a_platform_that_finishes_while_we_wait_has_nothing_to_answer(
        self,
    ) -> None:
        await getattr(checks_for(FakePlatform()), self.name)()

    async def test_a_platform_that_can_be_asked_passes(self) -> None:
        platform = FakePlatform(states=(PostState.PROCESSING,))

        await getattr(checks_for(platform), self.name)()

    async def test_asking_as_a_plain_function_is_refused(self) -> None:
        class ChecksWithoutWaiting(Methods):
            name = "checkswithoutwaiting"
            features = Feature.POST_TEXT

            def check_state(self, connection: Connection, post_id: str) -> PostResult:
                return PostResult(id=post_id)

        message = await failure_from(checks_for(ChecksWithoutWaiting()), self.name)

        assert "check_state" in message
        assert "async def" in message

    async def test_a_check_state_that_is_not_even_callable_is_refused(self) -> None:
        class ChecksWithNothing(Methods):
            name = "checkswithnothing"
            features = Feature.POST_TEXT
            check_state = None

        message = await failure_from(checks_for(ChecksWithNothing()), self.name)

        assert "check_state" in message

    async def test_asking_for_something_other_than_a_post_is_refused(self) -> None:
        class ChecksItsOwnWay(Methods):
            name = "checksitsownway"
            features = Feature.POST_TEXT

            async def check_state(self, connection: Connection) -> PostResult:
                return PostResult(id="1")

        message = await failure_from(checks_for(ChecksItsOwnWay()), self.name)

        assert "connection and the post id" in message

    async def test_taking_whatever_it_is_given_passes(self) -> None:
        class ChecksHoweverItIsAsked(Methods):
            name = "checkshoweveritisasked"
            features = Feature.POST_TEXT

            async def check_state(self, *whatever: object) -> PostResult:
                return PostResult(id="1")

        await getattr(checks_for(ChecksHoweverItIsAsked()), self.name)()


class TestTheCarryingOnCheck:
    name = "test_a_platform_that_pauses_to_ask_can_carry_on"

    async def test_a_platform_that_never_pauses_has_nothing_to_answer(self) -> None:
        await getattr(checks_for(FakePlatform()), self.name)()

    async def test_a_platform_that_can_carry_on_passes(self) -> None:
        platform = FakePlatform(accounts=(AccountChoice(id="7", name="A page"),))

        await getattr(checks_for(platform), self.name)()

    async def test_carrying_on_as_a_plain_function_is_refused(self) -> None:
        message = await failure_from(checks_for(CarriesOnWithoutWaiting()), self.name)

        assert "resume_login" in message
        assert "async def" in message

    async def test_carrying_on_under_the_wrong_argument_names_is_refused(self) -> None:
        message = await failure_from(checks_for(CarriesOnItsOwnWay()), self.name)

        assert "resume_token" in message
        assert "account_id" in message

    async def test_a_resume_login_that_is_not_even_callable_is_refused(self) -> None:
        class CarriesOnWithNothing(Methods):
            name = "carriesonwithnothing"
            features = Feature.POST_TEXT
            resume_login = None

        message = await failure_from(checks_for(CarriesOnWithNothing()), self.name)

        assert "resume_login" in message

    async def test_taking_whatever_it_is_given_passes(self) -> None:
        await getattr(checks_for(CarriesOnHoweverItIsAsked()), self.name)()


# ---------------------------------------------------------------------------
# Working without pytest. The fakes are for building an app, not only for
# testing one, so they must not drag a test framework in with them.
# ---------------------------------------------------------------------------


def test_the_fakes_work_with_no_pytest_installed() -> None:
    # In a process of its own, because this one has pytest imported and
    # sys.modules would answer yes whatever socialchimp does. `None` in
    # sys.modules is what the import machinery treats as not installed.
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            'import sys; sys.modules["pytest"] = None;'
            "from socialchimp.testing import ("
            "FakePlatform, RecordingStorage, RecordingTransport, StorageCall);"
            "print(FakePlatform().name, RecordingStorage().calls, "
            "RecordingTransport({}).requests, StorageCall.__name__)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "fake [] [] StorageCall"


def test_the_checks_say_what_to_install_when_pytest_is_missing() -> None:
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            'import sys; sys.modules["pytest"] = None\n'
            "from socialchimp.testing import PlatformChecks\n"
            "try:\n"
            "    class TestMine(PlatformChecks): pass\n"
            "except Exception as problem:\n"
            "    print(type(problem).__name__, problem)\n",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert "ConfigError" in done.stdout
    assert 'pip install "socialchimp[testing]"' in done.stdout
    assert "ModuleNotFoundError" not in done.stdout


def test_reaching_for_pytest_without_it_says_what_to_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "pytest", None)

    with pytest.raises(ConfigError) as caught:
        testing_module._pytest()

    message = str(caught.value)
    assert 'pip install "socialchimp[testing]"' in message
    assert "FakePlatform" in message


def test_reaching_for_pytest_hands_back_pytest() -> None:
    assert testing_module._pytest() is pytest


# ---------------------------------------------------------------------------
# The social inbox: reading, replying to, liking and messaging posts.
# ---------------------------------------------------------------------------


def a_person(person_id: str = "someone") -> Person:
    return Person(
        id=person_id,
        handle=f"{person_id}@example.social",
        display_name=None,
        avatar_url=None,
        url=None,
    )


class TestFakePlatformImplementsTheInboxProtocols:
    def test_it_claims_and_backs_up_every_inbox_feature(self) -> None:
        platform = FakePlatform()

        assert isinstance(platform, CanReadPost)
        assert isinstance(platform, CanReadThread)
        assert isinstance(platform, CanReply)
        assert isinstance(platform, CanLike)
        assert isinstance(platform, CanReadLikes)
        assert isinstance(platform, CanReadUpdatesAfter)
        assert isinstance(platform, CanMessage)
        assert isinstance(platform, CanStartConversations)
        for flag in (
            Feature.READ_POST,
            Feature.READ_THREAD,
            Feature.REPLY_TO_COMMENTS,
            Feature.LIKE,
            Feature.READ_LIKES,
            Feature.READ_UPDATES_AFTER,
            Feature.MESSAGES,
            Feature.START_CONVERSATIONS,
        ):
            assert flag in platform.features

    async def test_the_claims_check_agrees(self) -> None:
        # TestTheFakePlatform below already runs every PlatformChecks check,
        # this just names the one that matters most for a newly claimed
        # feature: that the flag and the method actually agree.
        checks = checks_for(FakePlatform(), connection=a_connection("fake"))
        await checks.test_everything_it_claims_in_features_it_can_actually_do()


class TestReadingAPost:
    async def test_a_seeded_post_reads_back(self) -> None:
        platform = FakePlatform()
        seeded = platform.add_post(text="hello there")

        read_back = await platform.read_post(platform.connection(), seeded.id)

        assert read_back == seeded
        assert read_back.text == "hello there"
        assert read_back.is_mine is False
        assert read_back.parent_id is None
        assert read_back.root_id == seeded.id
        assert read_back.reply_count == 0
        assert read_back.like_count == 0
        assert read_back.liked_by_me is False
        assert read_back.my_like_id is None

    async def test_an_unknown_id_is_gone(self) -> None:
        platform = FakePlatform()

        with pytest.raises(PostGoneError):
            await platform.read_post(platform.connection(), "no-such-post")

    async def test_a_published_post_can_be_read_straight_back(self) -> None:
        platform = FakePlatform()
        connection = platform.connection()

        result = await platform.publish(connection, Post(text="just posted"))
        read_back = await platform.read_post(connection, result.id)

        assert read_back.text == "just posted"
        assert read_back.is_mine is True
        assert read_back.root_id == result.id

    async def test_a_deleted_post_is_gone_when_read(self) -> None:
        platform = FakePlatform()
        connection = platform.connection()
        result = await platform.publish(connection, Post(text="temporary"))

        await platform.delete_post(connection, result.id)

        with pytest.raises(PostGoneError):
            await platform.read_post(connection, result.id)

    async def test_a_custom_author_and_id_are_kept(self) -> None:
        platform = FakePlatform()
        author = a_person("writer")

        seeded = platform.add_post(
            text="hi", post_id="mine", author=author, is_mine=True
        )

        assert seeded.id == "mine"
        assert seeded.author == author
        assert seeded.is_mine is True

    async def test_fail_next_makes_the_next_call_raise_and_then_stops(self) -> None:
        platform = FakePlatform()
        seeded = platform.add_post(text="hi")
        platform.fail_next("read_post", RateLimitError("slow down", retry_after=30))

        with pytest.raises(RateLimitError) as caught:
            await platform.read_post(platform.connection(), seeded.id)
        assert caught.value.retry_after == 30

        # The failure was one-shot: the next call behaves normally again.
        read_back = await platform.read_post(platform.connection(), seeded.id)
        assert read_back.id == seeded.id


class TestReadingAThread:
    async def test_replies_come_back_flat_and_oldest_first(self) -> None:
        platform = FakePlatform()
        root = platform.add_post(text="root")
        first = platform.add_reply(root.id, text="first reply")
        second = platform.add_reply(root.id, text="second reply")
        grandchild = platform.add_reply(first.id, text="a reply to the reply")

        thread = await platform.read_thread(platform.connection(), root.id)

        assert thread.post.id == root.id
        assert [reply.id for reply in thread.replies] == [
            first.id,
            second.id,
            grandchild.id,
        ]
        assert all(reply.parent_id is not None for reply in thread.replies)
        assert grandchild_parent_matches(thread, first.id, grandchild.id)
        assert thread.complete is True

    async def test_root_id_is_shared_down_the_whole_chain(self) -> None:
        platform = FakePlatform()
        root = platform.add_post(text="root")
        child = platform.add_reply(root.id, text="child")
        grandchild = platform.add_reply(child.id, text="grandchild")

        assert child.root_id == root.id
        assert grandchild.root_id == root.id

    async def test_depth_cuts_off_deeper_replies_and_marks_incomplete(self) -> None:
        platform = FakePlatform()
        root = platform.add_post(text="root")
        child = platform.add_reply(root.id, text="child")
        platform.add_reply(child.id, text="grandchild")

        thread = await platform.read_thread(platform.connection(), root.id, depth=1)

        assert [reply.id for reply in thread.replies] == [child.id]
        assert thread.complete is False

    async def test_limit_cuts_off_replies_and_marks_incomplete(self) -> None:
        platform = FakePlatform()
        root = platform.add_post(text="root")
        first = platform.add_reply(root.id, text="one")
        platform.add_reply(root.id, text="two")

        thread = await platform.read_thread(platform.connection(), root.id, limit=1)

        assert [reply.id for reply in thread.replies] == [first.id]
        assert thread.complete is False

    async def test_a_post_with_no_replies_is_complete(self) -> None:
        platform = FakePlatform()
        root = platform.add_post(text="alone")

        thread = await platform.read_thread(platform.connection(), root.id)

        assert thread.replies == ()
        assert thread.complete is True

    async def test_a_depth_deep_enough_to_hold_everything_is_still_complete(
        self,
    ) -> None:
        platform = FakePlatform()
        root = platform.add_post(text="root")
        platform.add_reply(root.id, text="child")

        thread = await platform.read_thread(platform.connection(), root.id, depth=5)

        assert len(thread.replies) == 1
        assert thread.complete is True

    async def test_an_unknown_root_is_gone(self) -> None:
        platform = FakePlatform()

        with pytest.raises(PostGoneError):
            await platform.read_thread(platform.connection(), "no-such-post")

    async def test_seeding_a_reply_to_an_unknown_post_is_gone(self) -> None:
        platform = FakePlatform()

        with pytest.raises(PostGoneError):
            platform.add_reply("no-such-post", text="hi")


def grandchild_parent_matches(
    thread: Thread, parent_id: str, grandchild_id: str
) -> bool:
    grandchild = next(reply for reply in thread.replies if reply.id == grandchild_id)
    return grandchild.parent_id == parent_id


class TestReplying:
    async def test_a_reply_is_mine_and_targets_the_post(self) -> None:
        platform = FakePlatform()
        connection = platform.connection()
        target = platform.add_post(text="root")

        result = await platform.reply(connection, target.id, "my reply")
        read_back = await platform.read_post(connection, result.id)

        assert read_back.text == "my reply"
        assert read_back.is_mine is True
        assert read_back.parent_id == target.id
        assert read_back.root_id == target.id
        assert platform.replied == [(connection.id, target.id, "my reply")]

    async def test_a_reply_with_a_picture_reads_back_with_one(self) -> None:
        platform = FakePlatform()
        connection = platform.connection()
        target = platform.add_post(text="root")
        picture = Media.from_bytes(b"not really a picture", filename="a.png")

        result = await platform.reply(connection, target.id, "look", media=(picture,))
        read_back = await platform.read_post(connection, result.id)

        assert len(read_back.attachments) == 1
        assert read_back.attachments[0].kind == "image"

    async def test_a_reply_shows_up_in_the_targets_thread(self) -> None:
        platform = FakePlatform()
        connection = platform.connection()
        target = platform.add_post(text="root")

        result = await platform.reply(connection, target.id, "my reply")
        thread = await platform.read_thread(connection, target.id)

        assert [reply.id for reply in thread.replies] == [result.id]

    async def test_replying_to_an_unknown_post_is_gone(self) -> None:
        platform = FakePlatform()

        with pytest.raises(PostGoneError):
            await platform.reply(platform.connection(), "no-such-post", "hi")

    async def test_fail_next_makes_reply_raise_once(self) -> None:
        platform = FakePlatform()
        target = platform.add_post(text="root")
        platform.fail_next("reply", MissingPermissionError(needs="write"))

        with pytest.raises(MissingPermissionError):
            await platform.reply(platform.connection(), target.id, "hi")

        # Back to normal.
        result = await platform.reply(platform.connection(), target.id, "hi")
        assert result.id


class TestLikingAndUnliking:
    async def test_liking_updates_the_post_read_back(self) -> None:
        platform = FakePlatform()
        connection = platform.connection()
        post = platform.add_post(text="hi")

        result = await platform.like(connection, post.id)
        read_back = await platform.read_post(connection, post.id)

        assert read_back.liked_by_me is True
        assert read_back.my_like_id == result.like_id
        assert read_back.like_count == 1
        assert platform.liked == [(connection.id, post.id)]

    async def test_liking_twice_returns_the_same_like(self) -> None:
        platform = FakePlatform()
        connection = platform.connection()
        post = platform.add_post(text="hi")

        first = await platform.like(connection, post.id)
        second = await platform.like(connection, post.id)

        assert first.like_id == second.like_id
        read_back = await platform.read_post(connection, post.id)
        assert read_back.like_count == 1

    async def test_unliking_removes_the_like(self) -> None:
        platform = FakePlatform()
        connection = platform.connection()
        post = platform.add_post(text="hi")
        await platform.like(connection, post.id)

        await platform.unlike(connection, post.id)

        read_back = await platform.read_post(connection, post.id)
        assert read_back.liked_by_me is False
        assert read_back.like_count == 0
        assert platform.unliked == [(connection.id, post.id)]

    async def test_unliking_something_never_liked_does_nothing(self) -> None:
        platform = FakePlatform()
        connection = platform.connection()
        post = platform.add_post(text="hi")

        await platform.unlike(connection, post.id)

        read_back = await platform.read_post(connection, post.id)
        assert read_back.liked_by_me is False

    async def test_unliking_by_like_id_leaves_other_likes_alone(self) -> None:
        platform = FakePlatform()
        connection = platform.connection()
        post = platform.add_post(text="hi")
        other = a_person("someone-else")
        platform.add_like(post.id, other)
        mine = await platform.like(connection, post.id)

        await platform.unlike(connection, post.id, like_id=mine.like_id)

        read_back = await platform.read_post(connection, post.id)
        assert read_back.like_count == 1
        assert read_back.liked_by_me is False

    async def test_liking_an_unknown_post_is_gone(self) -> None:
        platform = FakePlatform()

        with pytest.raises(PostGoneError):
            await platform.like(platform.connection(), "no-such-post")

    async def test_unliking_an_unknown_post_is_gone(self) -> None:
        platform = FakePlatform()

        with pytest.raises(PostGoneError):
            await platform.unlike(platform.connection(), "no-such-post")

    async def test_fail_next_makes_like_and_unlike_raise_once(self) -> None:
        platform = FakePlatform()
        connection = platform.connection()
        post = platform.add_post(text="hi")

        platform.fail_next("like", RateLimitError("slow down"))
        with pytest.raises(RateLimitError):
            await platform.like(connection, post.id)
        await platform.like(connection, post.id)

        platform.fail_next("unlike", RateLimitError("slow down"))
        with pytest.raises(RateLimitError):
            await platform.unlike(connection, post.id)
        await platform.unlike(connection, post.id)


class TestReadingLikes:
    async def test_seeded_likes_read_back(self) -> None:
        platform = FakePlatform()
        post = platform.add_post(text="hi")
        alice = a_person("alice")
        platform.add_like(post.id, alice)

        page = await platform.read_likes(platform.connection(), post.id)

        assert [like.person for like in page.items] == [alice]
        assert page.items[0].liked_at is not None

    async def test_add_like_without_a_person_makes_one_up(self) -> None:
        platform = FakePlatform()
        post = platform.add_post(text="hi")

        platform.add_like(post.id)

        page = await platform.read_likes(platform.connection(), post.id)
        assert len(page.items) == 1

    async def test_paging_uses_the_configured_page_size(self) -> None:
        platform = FakePlatform(page_size=2)
        post = platform.add_post(text="hi")
        for name in ("a", "b", "c"):
            platform.add_like(post.id, a_person(name))

        first_page = await platform.read_likes(platform.connection(), post.id)
        assert len(first_page.items) == 2
        assert first_page.next is not None

        second_page = await platform.read_likes(
            platform.connection(), post.id, after=first_page.next
        )
        assert len(second_page.items) == 1
        assert second_page.next is None

    async def test_limit_overrides_the_configured_page_size(self) -> None:
        platform = FakePlatform(page_size=2)
        post = platform.add_post(text="hi")
        for name in ("a", "b", "c"):
            platform.add_like(post.id, a_person(name))

        page = await platform.read_likes(platform.connection(), post.id, limit=1)

        assert len(page.items) == 1
        assert page.next is not None

    async def test_reading_likes_on_an_unknown_post_is_gone(self) -> None:
        platform = FakePlatform()

        with pytest.raises(PostGoneError):
            await platform.read_likes(platform.connection(), "no-such-post")

    async def test_seeding_a_like_on_an_unknown_post_is_gone(self) -> None:
        platform = FakePlatform()

        with pytest.raises(PostGoneError):
            platform.add_like("no-such-post", a_person())


class TestPollingUpdatesWithAMarker:
    async def test_marker_none_returns_everything_queued(self) -> None:
        platform = FakePlatform()
        first = platform.add_update(UpdateKind.COMMENT_CREATED)
        second = platform.add_update(UpdateKind.REACTION_ADDED)

        batch = await platform.fetch_updates_after(platform.connection(), None)

        assert [update.id for update in batch.updates] == [first.id, second.id]
        assert batch.marker == second.id
        assert batch.more is False

    async def test_a_marker_returns_only_whats_newer(self) -> None:
        platform = FakePlatform()
        first = platform.add_update(UpdateKind.COMMENT_CREATED)
        second = platform.add_update(UpdateKind.REACTION_ADDED)

        batch = await platform.fetch_updates_after(platform.connection(), first.id)

        assert [update.id for update in batch.updates] == [second.id]
        assert batch.marker == second.id

    async def test_a_limit_caps_the_page_and_sets_more(self) -> None:
        platform = FakePlatform()
        first = platform.add_update(UpdateKind.COMMENT_CREATED)
        platform.add_update(UpdateKind.REACTION_ADDED)
        platform.add_update(UpdateKind.MENTION)

        batch = await platform.fetch_updates_after(platform.connection(), None, limit=1)

        assert [update.id for update in batch.updates] == [first.id]
        assert batch.more is True

    async def test_an_empty_queue_gives_no_marker(self) -> None:
        platform = FakePlatform()

        batch = await platform.fetch_updates_after(platform.connection(), None)

        assert batch.updates == ()
        assert batch.marker is None
        assert batch.more is False

    async def test_add_update_fills_in_the_extra_fields(self) -> None:
        platform = FakePlatform()
        actor = a_person("someone")

        made = platform.add_update(
            UpdateKind.MESSAGE_RECEIVED,
            actor=actor,
            post_id="p1",
            about_post_id="p2",
            thread_root_id="p0",
            conversation_id="c1",
        )

        assert made.kind is UpdateKind.MESSAGE_RECEIVED
        assert made.actor == actor
        assert made.post_id == "p1"
        assert made.about_post_id == "p2"
        assert made.thread_root_id == "p0"
        assert made.conversation_id == "c1"

    async def test_mark_seen_is_recorded(self) -> None:
        platform = FakePlatform()
        update = platform.add_update(UpdateKind.COMMENT_CREATED)

        await platform.mark_seen(platform.connection(), update.id)

        assert platform.marked_seen == [update.id]

    async def test_fail_next_makes_fetch_updates_after_raise_once(self) -> None:
        platform = FakePlatform()
        platform.fail_next("fetch_updates_after", RateLimitError("slow down"))

        with pytest.raises(RateLimitError):
            await platform.fetch_updates_after(platform.connection(), None)

        batch = await platform.fetch_updates_after(platform.connection(), None)
        assert batch.updates == ()


class TestDirectMessages:
    async def test_a_seeded_conversation_reads_back(self) -> None:
        platform = FakePlatform()
        alice = a_person("alice")

        conversation = platform.add_conversation((alice,), unread_count=2)

        page = await platform.read_conversations(platform.connection())
        assert [found.id for found in page.items] == [conversation.id]
        assert page.items[0].unread_count == 2
        assert page.items[0].people == (alice,)

    async def test_conversations_come_back_newest_updated_first(self) -> None:
        platform = FakePlatform()
        older = platform.add_conversation((a_person("alice"),))
        newer = platform.add_conversation((a_person("bob"),))
        # Touch "older" so it is now the most recently updated one.
        platform.add_message(older.id, a_person("alice"), "ping")

        page = await platform.read_conversations(platform.connection())

        assert [found.id for found in page.items] == [older.id, newer.id]

    async def test_a_message_from_someone_else_bumps_unread(self) -> None:
        platform = FakePlatform()
        conversation = platform.add_conversation((a_person("alice"),))

        platform.add_message(conversation.id, a_person("alice"), "hi")

        page = await platform.read_conversations(platform.connection())
        assert page.items[0].unread_count == 1

    async def test_a_message_seeded_as_mine_does_not_bump_unread(self) -> None:
        platform = FakePlatform()
        connection = platform.connection()
        conversation = platform.add_conversation((a_person("alice"),))

        platform.add_message(conversation.id, a_person("me"), "hi", is_mine=True)

        page = await platform.read_conversations(connection)
        assert page.items[0].unread_count == 0

    async def test_mark_read_zeroes_unread(self) -> None:
        platform = FakePlatform()
        connection = platform.connection()
        conversation = platform.add_conversation((a_person("alice"),))
        platform.add_message(conversation.id, a_person("alice"), "hi")

        await platform.mark_read(connection, conversation.id)

        page = await platform.read_conversations(connection)
        assert page.items[0].unread_count == 0
        assert platform.marked_read == [conversation.id]

    async def test_messages_read_back_newest_first_and_page(self) -> None:
        platform = FakePlatform(page_size=2)
        connection = platform.connection()
        conversation = platform.add_conversation((a_person("alice"),))
        first = platform.add_message(conversation.id, a_person("alice"), "one")
        second = platform.add_message(conversation.id, a_person("alice"), "two")
        third = platform.add_message(conversation.id, a_person("alice"), "three")

        page = await platform.read_messages(connection, conversation.id)

        assert [message.id for message in page.items] == [third.id, second.id]
        assert page.next is not None

        next_page = await platform.read_messages(
            connection, conversation.id, after=page.next
        )
        assert [message.id for message in next_page.items] == [first.id]

    async def test_send_message_is_mine_and_recorded(self) -> None:
        platform = FakePlatform()
        connection = platform.connection()
        conversation = platform.add_conversation((a_person("alice"),))

        sent = await platform.send_message(connection, conversation.id, "hello")

        assert sent.is_mine is True
        assert sent.text == "hello"
        assert platform.sent_messages == [sent]
        page = await platform.read_messages(connection, conversation.id)
        assert page.items[0] == sent

    async def test_start_conversation_makes_a_new_one(self) -> None:
        platform = FakePlatform()
        connection = platform.connection()

        message = await platform.start_conversation(connection, ["alice"], "hi")

        conversations = await platform.read_conversations(connection)
        assert len(conversations.items) == 1
        assert conversations.items[0].people == (
            Person(
                id="alice", handle=None, display_name=None, avatar_url=None, url=None
            ),
        )
        assert message.text == "hi"
        assert message.conversation_id == conversations.items[0].id

    async def test_start_conversation_reuses_a_matching_one(self) -> None:
        platform = FakePlatform()
        connection = platform.connection()
        first_message = await platform.start_conversation(connection, ["alice"], "hi")

        second_message = await platform.start_conversation(
            connection, ["alice"], "again"
        )

        assert second_message.conversation_id == first_message.conversation_id
        conversations = await platform.read_conversations(connection)
        assert len(conversations.items) == 1

    async def test_messaging_an_unknown_conversation_is_not_found(self) -> None:
        platform = FakePlatform()

        with pytest.raises(NotFoundError):
            await platform.read_messages(platform.connection(), "no-such-conversation")
        with pytest.raises(NotFoundError):
            await platform.send_message(
                platform.connection(), "no-such-conversation", "hi"
            )
        with pytest.raises(NotFoundError):
            await platform.mark_read(platform.connection(), "no-such-conversation")
        with pytest.raises(NotFoundError):
            platform.add_message("no-such-conversation", a_person("alice"), "hi")

    async def test_fail_next_makes_messaging_raise_once(self) -> None:
        platform = FakePlatform()
        connection = platform.connection()
        conversation = platform.add_conversation((a_person("alice"),))

        platform.fail_next("read_conversations", RateLimitError("slow down"))
        with pytest.raises(RateLimitError):
            await platform.read_conversations(connection)
        await platform.read_conversations(connection)

        platform.fail_next("read_messages", RateLimitError("slow down"))
        with pytest.raises(RateLimitError):
            await platform.read_messages(connection, conversation.id)
        await platform.read_messages(connection, conversation.id)

        platform.fail_next("send_message", RateLimitError("slow down"))
        with pytest.raises(RateLimitError):
            await platform.send_message(connection, conversation.id, "hi")
        await platform.send_message(connection, conversation.id, "hi")

        platform.fail_next("mark_read", RateLimitError("slow down"))
        with pytest.raises(RateLimitError):
            await platform.mark_read(connection, conversation.id)
        await platform.mark_read(connection, conversation.id)

        platform.fail_next("start_conversation", RateLimitError("slow down"))
        with pytest.raises(RateLimitError):
            await platform.start_conversation(connection, ["bob"], "hi")
        await platform.start_conversation(connection, ["bob"], "hi")
