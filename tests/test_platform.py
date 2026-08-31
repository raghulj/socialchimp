"""Tests for the contract every platform file implements."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from socialchimp import (
    AppCredentials,
    Connection,
    Feature,
    Limits,
    Post,
    PostResult,
    RawData,
    Token,
)
from socialchimp.events import Update, UpdateKind
from socialchimp.platform import (
    AccountChoice,
    AskForDetails,
    CanCheckSignature,
    CanCreateApp,
    CanReadUpdates,
    ChooseAccount,
    Finished,
    LoginField,
    LoginRequest,
    LoginStep,
    Platform,
    SendToNetwork,
)


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
