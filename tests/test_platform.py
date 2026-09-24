"""Tests for the contract every platform file implements."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import pytest

from socialchimp import (
    AppCredentials,
    Connection,
    Conversation,
    Feature,
    Like,
    LikeResult,
    Limits,
    Media,
    Message,
    Page,
    Person,
    Post,
    PostDetails,
    PostResult,
    PostState,
    RawData,
    Thread,
    Token,
)
from socialchimp.events import Update, UpdateBatch, UpdateKind
from socialchimp.platform import (
    AccountChoice,
    AskForDetails,
    CanAnswerSetupCheck,
    CanCheckSignature,
    CanCheckState,
    CanCreateApp,
    CanLike,
    CanMessage,
    CanReadLikes,
    CanReadPost,
    CanReadPushedUpdates,
    CanReadThread,
    CanReadUpdates,
    CanReadUpdatesAfter,
    CanReply,
    CanStartConversations,
    ChooseAccount,
    Finished,
    LoginField,
    LoginRequest,
    LoginStep,
    Platform,
    SendToNetwork,
)
from socialchimp.registry import available_platforms, get_platform_class


class FakePlatform:
    """The smallest thing that counts as a platform.

    If this stops satisfying `Platform`, the contract grew, and every
    platform file - including ones other people wrote - has to change.
    """

    name = "fake"
    features = Feature.POST_TEXT

    def api_base(self, connection: Connection) -> str:
        return "https://fake.example"

    def auth_headers(self, connection: Connection) -> Mapping[str, str]:
        return {"Authorization": f"Bearer {connection.token.access_token}"}

    async def limits(self, connection: Connection) -> Limits:
        return Limits(max_text_length=100)

    async def start_login(self, request: LoginRequest) -> LoginStep:
        return SendToNetwork(url="https://fake.example/authorize", state="xyz")

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> LoginStep:
        return Finished(connection=_a_connection())

    async def refresh(
        self,
        connection: Connection,
        app: AppCredentials | None = None,
    ) -> Token:
        return Token(access_token="new")

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        return PostResult(id="1", url="https://fake.example/1")


def _a_connection(host: str | None = None) -> Connection:
    return Connection(
        id="conn-1",
        platform="fake",
        host=host,
        account_id="42",
        account_name="someone",
        token=Token(access_token="abc"),
    )


def test_a_minimal_platform_satisfies_the_contract() -> None:
    # Checked by the type checker too - this variable is annotated, so mypy
    # fails the build if FakePlatform stops matching.
    platform: Platform = FakePlatform()

    assert platform.name == "fake"


def test_a_platform_says_what_it_cannot_do_by_leaving_it_out() -> None:
    platform: Platform = FakePlatform()

    assert Feature.POST_TEXT in platform.features
    assert Feature.SCHEDULE not in platform.features


def test_creating_an_app_is_an_extra_a_platform_opts_into() -> None:
    # Only Mastodon can do this. A platform that cannot simply does not
    # have the method, and asking is how we find out.
    assert not isinstance(FakePlatform(), CanCreateApp)


class TestWhereTheNetworkLives:
    def test_a_platform_says_where_its_api_is_and_how_to_prove_who_we_are(
        self,
    ) -> None:
        platform: Platform = FakePlatform()
        connection = _a_connection()

        assert platform.api_base(connection) == "https://fake.example"
        assert platform.auth_headers(connection) == {"Authorization": "Bearer abc"}

    def test_the_address_can_be_different_for_every_account(self) -> None:
        # Mastodon is thousands of separate servers, so the address cannot be
        # a plain attribute on the platform. That is why the connection is
        # passed in.
        class PerServer(FakePlatform):
            def api_base(self, connection: Connection) -> str:
                return f"https://{connection.host}"

        platform: Platform = PerServer()

        assert platform.api_base(_a_connection("one.example")) == "https://one.example"
        assert platform.api_base(_a_connection("two.example")) == "https://two.example"

    def test_a_network_can_prove_who_we_are_without_a_bearer_token(self) -> None:
        # Not every network uses Authorization: Bearer. A platform that signs
        # its requests some other way says so here instead of being guessed
        # at.
        class SignsItsOwnWay(FakePlatform):
            def auth_headers(self, connection: Connection) -> Mapping[str, str]:
                return {"X-Api-Key": connection.token.access_token}

        platform: Platform = SignsItsOwnWay()

        assert platform.auth_headers(_a_connection()) == {"X-Api-Key": "abc"}


class TestLoginSteps:
    def test_the_first_step_sends_the_person_to_the_network(self) -> None:
        step = SendToNetwork(url="https://example.com/auth", state="abc")

        assert step.url == "https://example.com/auth"

    def test_a_network_with_no_sign_in_page_asks_for_details_instead(self) -> None:
        # Bluesky takes an app password; Discord and Telegram take a bot
        # token. There is nowhere to send anybody, so the platform says what
        # to ask for and the app draws the form.
        step = AskForDetails(
            fields=(
                LoginField(name="handle", label="Your handle"),
                LoginField(
                    name="app_password",
                    label="App password",
                    secret=True,
                    help_text="Settings, then App Passwords.",
                ),
            ),
            help_url="https://bsky.app/settings/app-passwords",
        )

        assert [asked.name for asked in step.fields] == ["handle", "app_password"]
        assert step.fields[0].secret is False
        assert step.fields[1].secret is True
        assert step.fields[1].help_text == "Settings, then App Passwords."

    def test_a_form_can_be_asked_for_without_a_page_to_link_to(self) -> None:
        step = AskForDetails(fields=(LoginField(name="token", label="Bot token"),))

        assert step.help_url is None

    def test_a_network_can_pause_to_ask_which_account_to_use(self) -> None:
        # Facebook asks which page, YouTube asks which channel. A two-call
        # login cannot express this, so it is a step of its own.
        step = ChooseAccount(
            options=(
                AccountChoice(id="page-1", name="My Cafe"),
                AccountChoice(id="page-2", name="My Other Cafe"),
            ),
            resume_token="opaque",
        )

        assert len(step.options) == 2
        assert step.options[0].name == "My Cafe"

    def test_the_last_step_carries_the_connection_to_save(self) -> None:
        step = Finished(connection=_a_connection())

        assert step.connection.account_name == "someone"


class TestLoginRequest:
    def test_scopes_default_to_none_asked_for(self) -> None:
        request = LoginRequest(redirect_uri="https://myapp.example/callback")

        assert request.scopes == ()
        assert request.host is None

    def test_a_host_is_given_for_networks_with_many_servers(self) -> None:
        request = LoginRequest(
            redirect_uri="https://myapp.example/callback",
            host="mastodon.social",
            scopes=("read", "write"),
        )

        assert request.host == "mastodon.social"


class TestUpdateExtras:
    def test_a_platform_that_cannot_be_asked_for_updates_says_so(self) -> None:
        # Nothing is stubbed out. A network we cannot poll simply has no
        # fetch_updates method, and asking is how the wiring finds out.
        assert not isinstance(FakePlatform(), CanReadUpdates)

    def test_a_platform_that_cannot_check_signatures_says_so(self) -> None:
        assert not isinstance(FakePlatform(), CanCheckSignature)

    def test_a_platform_can_offer_both_ways_of_getting_updates(self) -> None:
        # Meta pushes updates, but an app behind a firewall may not be able
        # to receive them, so offering both is allowed on purpose.
        class PushesAndPolls(FakePlatform):
            features = Feature.POST_TEXT | Feature.PUSH_UPDATES

            async def fetch_updates(
                self, connection: Connection, since: datetime | None
            ) -> Sequence[Update]:
                return ()

            def check_signature(
                self, body: bytes, headers: Mapping[str, str], *, secret: str
            ) -> None:
                return None

            def read_update(self, body: bytes, headers: Mapping[str, str]) -> Update:
                return Update(
                    id="1",
                    kind=UpdateKind.MENTION,
                    platform="fake",
                    connection_id="conn-1",
                    created_at=datetime.now(UTC),
                )

        platform = PushesAndPolls()

        assert isinstance(platform, CanReadUpdates)
        assert isinstance(platform, CanCheckSignature)
        assert Feature.PUSH_UPDATES in platform.features


class TestCheckingOnAPostAfterwards:
    def test_a_network_that_finishes_while_we_wait_has_no_check_state(self) -> None:
        # Nothing is stubbed out. Most networks are done by the time publish
        # returns, so they simply have no check_state.
        assert not isinstance(FakePlatform(), CanCheckState)

    def test_a_network_that_keeps_working_afterwards_says_so(self) -> None:
        class StillEncoding(FakePlatform):
            async def check_state(
                self, connection: Connection, post_id: str
            ) -> PostResult:
                return PostResult(id=post_id, state=PostState.PROCESSING)

        assert isinstance(StillEncoding(), CanCheckState)


class TestAnsweringTheSetupCheck:
    def test_a_network_that_asks_nothing_first_has_no_answer(self) -> None:
        assert not isinstance(FakePlatform(), CanAnswerSetupCheck)

    def test_a_network_that_asks_before_pushing_says_so(self) -> None:
        class AsksFirst(FakePlatform):
            def answer_setup_check(
                self, params: Mapping[str, str], *, verify_token: str
            ) -> str:
                return params["hub.challenge"]

        assert isinstance(AsksFirst(), CanAnswerSetupCheck)


class TestReadingAWholePushedMessage:
    def test_a_network_we_cannot_unpack_a_message_from_says_so(self) -> None:
        assert not isinstance(FakePlatform(), CanReadPushedUpdates)

    def test_reading_every_update_is_separate_from_asking_for_them(self) -> None:
        # CanReadUpdates is "ask the network what has happened".
        # CanReadPushedUpdates is "unpack a message the network sent us".
        # A network can have either, both or neither.
        class Batches(FakePlatform):
            def read_updates(self, body: bytes) -> list[Update]:
                return []

        platform = Batches()

        assert isinstance(platform, CanReadPushedUpdates)
        assert not isinstance(platform, CanReadUpdates)


class TestRememberingBetweenTheTwoHalves:
    def test_a_platform_can_ask_for_something_back(self) -> None:
        # PKCE needs the secret half again when the person returns. It cannot
        # be held in memory: the two halves of a sign-in can land on
        # different web workers, so it travels through the app instead.
        step = SendToNetwork(
            url="https://example.com/auth",
            state="abc",
            remember={"code_verifier": "the-secret-half"},
        )

        assert step.remember["code_verifier"] == "the-secret-half"

    def test_platforms_that_need_nothing_back_get_an_empty_note(self) -> None:
        step = SendToNetwork(url="https://example.com/auth", state="abc")

        assert step.remember == {}


def _a_person() -> Person:
    return Person(id="1", handle="ada", display_name="Ada", avatar_url=None, url=None)


def _a_post_details(post_id: str = "1") -> PostDetails:
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
        visibility=None,
        parent_id=None,
        root_id=post_id,
        reply_count=None,
        like_count=None,
        repost_count=None,
        quote_count=None,
        liked_by_me=None,
        my_like_id=None,
        is_mine=False,
        unavailable=None,
    )


def _a_message() -> Message:
    return Message(
        id="m1",
        conversation_id="c1",
        sender=_a_person(),
        text="hi",
        sent_at=datetime.now(UTC),
        is_mine=True,
        deleted=False,
        attachments=(),
    )


class TestReadingOnePostBack:
    def test_a_platform_that_cannot_read_one_post_back_says_so(self) -> None:
        assert not isinstance(FakePlatform(), CanReadPost)

    def test_a_platform_that_can_read_one_post_back_says_so(self) -> None:
        class ReadsPosts(FakePlatform):
            async def read_post(
                self, connection: Connection, post_id: str
            ) -> PostDetails:
                return _a_post_details(post_id)

        assert isinstance(ReadsPosts(), CanReadPost)


class TestReadingAThread:
    def test_a_platform_that_cannot_read_a_thread_says_so(self) -> None:
        assert not isinstance(FakePlatform(), CanReadThread)

    def test_a_platform_that_can_read_a_thread_says_so(self) -> None:
        class ReadsThreads(FakePlatform):
            async def read_thread(
                self,
                connection: Connection,
                post_id: str,
                *,
                depth: int | None = None,
                limit: int | None = None,
            ) -> Thread:
                return Thread(post=_a_post_details(post_id), replies=(), complete=True)

        assert isinstance(ReadsThreads(), CanReadThread)


class TestReplyingToAComment:
    def test_a_platform_that_cannot_reply_to_a_comment_says_so(self) -> None:
        assert not isinstance(FakePlatform(), CanReply)

    def test_a_platform_that_can_reply_to_a_comment_says_so(self) -> None:
        class Replies(FakePlatform):
            async def reply(
                self,
                connection: Connection,
                post_id: str,
                text: str,
                *,
                media: tuple[Media, ...] = (),
                options: RawData | None = None,
            ) -> PostResult:
                return PostResult(id="new")

        assert isinstance(Replies(), CanReply)


class TestLikingAPost:
    def test_a_platform_that_cannot_like_says_so(self) -> None:
        assert not isinstance(FakePlatform(), CanLike)

    def test_a_platform_that_can_like_says_so(self) -> None:
        class Likes(FakePlatform):
            async def like(self, connection: Connection, post_id: str) -> LikeResult:
                return LikeResult(post_id=post_id, like_id=None)

            async def unlike(
                self,
                connection: Connection,
                post_id: str,
                *,
                like_id: str | None = None,
            ) -> None:
                return None

        assert isinstance(Likes(), CanLike)

    def test_a_platform_that_cannot_list_who_liked_says_so(self) -> None:
        assert not isinstance(FakePlatform(), CanReadLikes)

    def test_a_platform_that_can_list_who_liked_says_so(self) -> None:
        class ReadsLikes(FakePlatform):
            async def read_likes(
                self,
                connection: Connection,
                post_id: str,
                *,
                after: str | None = None,
                limit: int | None = None,
            ) -> Page[Like]:
                return Page(items=())

        assert isinstance(ReadsLikes(), CanReadLikes)


class TestReadingUpdatesAfterAMarker:
    def test_a_platform_that_cannot_be_asked_this_way_says_so(self) -> None:
        assert not isinstance(FakePlatform(), CanReadUpdatesAfter)

    def test_a_platform_that_can_be_asked_this_way_says_so(self) -> None:
        class ReadsUpdatesAfter(FakePlatform):
            async def fetch_updates_after(
                self,
                connection: Connection,
                marker: str | None,
                *,
                limit: int | None = None,
            ) -> UpdateBatch:
                return UpdateBatch(updates=(), marker=marker, more=False)

            async def mark_seen(self, connection: Connection, marker: str) -> None:
                return None

        assert isinstance(ReadsUpdatesAfter(), CanReadUpdatesAfter)


class TestDirectMessages:
    def test_a_platform_that_cannot_message_says_so(self) -> None:
        assert not isinstance(FakePlatform(), CanMessage)

    def test_a_platform_that_can_message_says_so(self) -> None:
        class Messages(FakePlatform):
            async def read_conversations(
                self,
                connection: Connection,
                *,
                after: str | None = None,
                limit: int | None = None,
            ) -> Page[Conversation]:
                return Page(items=())

            async def read_messages(
                self,
                connection: Connection,
                conversation_id: str,
                *,
                after: str | None = None,
                limit: int | None = None,
            ) -> Page[Message]:
                return Page(items=())

            async def send_message(
                self,
                connection: Connection,
                conversation_id: str,
                text: str,
                *,
                options: RawData | None = None,
            ) -> Message:
                return _a_message()

            async def mark_read(
                self, connection: Connection, conversation_id: str
            ) -> None:
                return None

        assert isinstance(Messages(), CanMessage)

    def test_a_platform_that_cannot_start_a_conversation_says_so(self) -> None:
        assert not isinstance(FakePlatform(), CanStartConversations)

    def test_a_platform_that_can_start_a_conversation_says_so(self) -> None:
        class StartsConversations(FakePlatform):
            async def start_conversation(
                self,
                connection: Connection,
                person_ids: Sequence[str],
                text: str,
            ) -> Message:
                return _a_message()

        assert isinstance(StartsConversations(), CanStartConversations)


# Every new (Feature, Protocol) pair social inbox added. Each flag matches
# exactly one protocol, and the two must always agree - a platform saying it
# can do something it has no method for, or having the method without saying
# so, is a mistake in that platform file.
NEW_FEATURE_PROTOCOL_PAIRS: tuple[tuple[Feature, type], ...] = (
    (Feature.READ_POST, CanReadPost),
    (Feature.READ_THREAD, CanReadThread),
    (Feature.REPLY_TO_COMMENTS, CanReply),
    (Feature.LIKE, CanLike),
    (Feature.READ_LIKES, CanReadLikes),
    (Feature.READ_UPDATES_AFTER, CanReadUpdatesAfter),
    (Feature.MESSAGES, CanMessage),
    (Feature.START_CONVERSATIONS, CanStartConversations),
)


class TestFlagAndProtocolAgree:
    """Every built-in platform, checked pair by pair.

    None of the built-in platforms implement any of this yet - Mastodon and
    Bluesky get their social inbox methods in a later step - so today this
    only proves that nobody has listed a flag with no method behind it, or
    written a method and forgotten to say so. It keeps proving that once
    they do.
    """

    @pytest.mark.parametrize("platform_name", sorted(available_platforms()))
    def test_every_built_in_platform_agrees_with_itself(
        self, platform_name: str
    ) -> None:
        platform = get_platform_class(platform_name)()
        for feature, protocol in NEW_FEATURE_PROTOCOL_PAIRS:
            has_the_flag = feature in platform.features
            has_the_protocol = isinstance(platform, protocol)
            assert has_the_flag == has_the_protocol, (
                f"{platform_name} and {feature} disagree with {protocol.__name__}: "
                f"flag={has_the_flag}, protocol={has_the_protocol}"
            )

    def test_a_flag_with_no_method_behind_it_is_caught(self) -> None:
        class ClaimsLikingButCannot(FakePlatform):
            features = Feature.POST_TEXT | Feature.LIKE

        platform = ClaimsLikingButCannot()

        assert Feature.LIKE in platform.features
        assert not isinstance(platform, CanLike)

    def test_a_method_with_no_flag_saying_so_is_caught(self) -> None:
        class CanLikeButDoesNotSayIt(FakePlatform):
            async def like(self, connection: Connection, post_id: str) -> LikeResult:
                return LikeResult(post_id=post_id, like_id=None)

            async def unlike(
                self,
                connection: Connection,
                post_id: str,
                *,
                like_id: str | None = None,
            ) -> None:
                return None

        platform = CanLikeButDoesNotSayIt()

        assert Feature.LIKE not in platform.features
        assert isinstance(platform, CanLike)
