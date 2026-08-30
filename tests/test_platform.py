"""Tests for the contract every platform file implements."""

from collections.abc import Mapping

from socialchimp import (
    Connection,
    Feature,
    Limits,
    Post,
    PostResult,
    Token,
)
from socialchimp.platform import (
    AccountChoice,
    CanCreateApp,
    ChooseAccount,
    Finished,
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

    async def limits(self, connection: Connection) -> Limits:
        return Limits(max_text_length=100)

    async def start_login(self, request: LoginRequest) -> SendToNetwork:
        return SendToNetwork(url="https://fake.example/authorize", state="xyz")

    async def finish_login(
        self, request: LoginRequest, callback: Mapping[str, str]
    ) -> LoginStep:
        return Finished(connection=_a_connection())

    async def refresh(self, connection: Connection) -> Token:
        return Token(access_token="new")

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        return PostResult(id="1", url="https://fake.example/1")


def _a_connection() -> Connection:
    return Connection(
        id="conn-1",
        platform="fake",
        host=None,
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


class TestLoginSteps:
    def test_the_first_step_sends_the_person_to_the_network(self) -> None:
        step = SendToNetwork(url="https://example.com/auth", state="abc")

        assert step.url == "https://example.com/auth"

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
