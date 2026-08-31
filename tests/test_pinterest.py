"""Tests for the Pinterest platform."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from socialchimp import (
    AppCredentials,
    AuthError,
    ConfigError,
    Connection,
    Feature,
    InvalidPostError,
    Media,
    NotAllowedError,
    NotFoundError,
    NotSupportedError,
    PlatformError,
    Post,
    PostState,
    RateLimitError,
    Token,
    TokenExpiredError,
)
from socialchimp.features import TextCount
from socialchimp.http import Retries
from socialchimp.platform import (
    CanCreateApp,
    CanDeletePosts,
    CanReadUpdates,
    LoginRequest,
    Platform,
    SendToNetwork,
)
from socialchimp.platforms import pinterest as pinterest_module
from socialchimp.platforms.pinterest import (
    DEFAULT_SCOPES,
    Board,
    PinterestPlatform,
)
from socialchimp.testing import PlatformChecks, RecordingTransport

API = "https://api.pinterest.com/v5"
REDIRECT = "https://app.example/callback"

# One try and no waiting, so the error tests do not spend real seconds asleep.
ONCE = Retries(attempts=1)

ACCOUNT = "ada"
BOARD = "1234567890"

APP = AppCredentials(
    platform="pinterest",
    host=None,
    client_id="client-id",
    client_secret="client-secret",
)

A_PIN: dict[str, Any] = {
    "id": "9876543210",
    "created_at": "2026-08-31T10:00:00",
    "link": "https://shop.example/thing",
    "board_id": BOARD,
    "title": "A thing",
}

A_PICTURE = Media.from_bytes(b"a picture", filename="a.png")


@pytest.fixture
def platform() -> PinterestPlatform:
    """A platform that gives up after one try and never really sleeps."""
    return PinterestPlatform(retries=ONCE, media_wait_seconds=0.0)


@pytest.fixture
def account() -> Connection:
    """A connected Pinterest account, with every scope a pin needs."""
    return Connection(
        id=f"pinterest:{ACCOUNT}",
        platform="pinterest",
        host=None,
        account_id=ACCOUNT,
        account_name=ACCOUNT,
        token=Token(access_token="access-one", refresh_token="refresh-one"),
        scopes=DEFAULT_SCOPES,
    )


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every pause instead of taking it."""
    recorded: list[float] = []

    async def remember(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(pinterest_module, "_wait", remember)
    return recorded


def login(
    *,
    state: str | None = None,
    scopes: tuple[str, ...] = (),
    app: AppCredentials | None = APP,
) -> LoginRequest:
    """A login request with the everyday values already filled in."""
    return LoginRequest(redirect_uri=REDIRECT, scopes=scopes, state=state, app=app)


async def start(platform: PinterestPlatform, request: LoginRequest) -> SendToNetwork:
    """Start a login, and insist Pinterest answered with an address to visit."""
    step = await platform.start_login(request)
    assert isinstance(step, SendToNetwork)
    return step


def form_of(request: httpx.Request) -> dict[str, list[str]]:
    """Read a sent form back into a dictionary."""
    return parse_qs(request.content.decode(), keep_blank_values=True)


def a_token(**extra: object) -> dict[str, Any]:
    """What Pinterest answers a code swap or a renewal with."""
    return {
        "response_type": "authorization_code",
        "access_token": "access-one",
        "refresh_token": "refresh-one",
        "token_type": "bearer",
        "expires_in": 2_592_000,
        "refresh_token_expires_in": 5_184_000,
        "scope": " ".join(DEFAULT_SCOPES),
        **extra,
    }


def stub_me(network: respx.Router) -> respx.Route:
    """Answer the "who just signed in?" question."""
    return network.get(f"{API}/user_account").mock(
        return_value=httpx.Response(
            200,
            json={
                "username": ACCOUNT,
                "id": "700000000000000000",
                "account_type": "BUSINESS",
                "profile_image": "https://i.pinimg.example/ada.jpg",
            },
        )
    )


def a_post(
    *,
    text: str = "",
    reply_to: str | None = None,
    publish_at: datetime | None = None,
    options: dict[str, Any] | None = None,
) -> Post:
    """A pin Pinterest would look at: a picture, on a board."""
    return Post(
        text=text,
        media=(A_PICTURE,),
        reply_to=reply_to,
        publish_at=publish_at,
        options={"board_id": BOARD, **(options or {})},
    )


class TestWhatItSaysItCanDo:
    def test_it_provides_everything_a_platform_must(
        self,
        platform: PinterestPlatform,
    ) -> None:
        checked: Platform = platform
        deletes: CanDeletePosts = platform

        assert isinstance(checked, Platform)
        assert isinstance(deletes, CanDeletePosts)
        assert platform.name == "pinterest"

    def test_it_lists_the_features_pinterest_really_has(
        self,
        platform: PinterestPlatform,
    ) -> None:
        for feature in (
            Feature.POST_IMAGE,
            Feature.POST_VIDEO,
            Feature.DELETE_POST,
        ):
            assert feature in platform.features

    def test_there_is_no_text_only_pin_and_no_scheduling(
        self,
        platform: PinterestPlatform,
    ) -> None:
        assert Feature.POST_TEXT not in platform.features
        assert Feature.SCHEDULE not in platform.features
        assert Feature.CREATE_APP not in platform.features
        assert not isinstance(platform, CanCreateApp)

    def test_it_does_not_pretend_pinterest_has_comments(
        self,
        platform: PinterestPlatform,
    ) -> None:
        # There is no comment endpoint in v5 at all - not to read one, not
        # to write one - so there is nothing to reply to.
        assert Feature.REPLY not in platform.features

    def test_it_does_not_claim_updates_it_cannot_get(
        self,
        platform: PinterestPlatform,
    ) -> None:
        # No webhooks for ordinary pins, and nothing on the account that
        # reports a thing having happened. Inventing an update out of a list
        # of your own pins would be worse than saying so.
        assert Feature.PUSH_UPDATES not in platform.features
        assert not isinstance(platform, CanReadUpdates)
        assert not hasattr(platform, "fetch_updates")
        assert not hasattr(platform, "check_signature")

    def test_the_page_says_plainly_why_there_are_no_updates(self) -> None:
        said = pinterest_module.__doc__
        assert said is not None
        assert "no comments" in said.lower()
        assert "fetch_updates" in said


class TestTheTrialTrap:
    def test_the_page_warns_that_a_trial_pin_is_only_visible_to_you(self) -> None:
        said = pinterest_module.__doc__
        assert said is not None
        lowered = said.lower()

        assert "trial" in lowered
        assert "standard" in lowered
        assert "only you" in lowered
        assert "privacy policy" in lowered
        assert "video" in lowered
        assert pinterest_module.ACCESS_URL in said

    def test_it_is_honest_that_we_cannot_tell_which_tier_you_are_on(self) -> None:
        # Nothing in the API reports it, so nothing here pretends to.
        said = pinterest_module.__doc__
        assert said is not None
        assert "no field" in said.lower()


class TestWhereTheApiIs:
    def test_the_address_is_the_same_for_every_account(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        assert platform.api_base(account) == API

    def test_the_headers_carry_the_accounts_own_token(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        assert platform.auth_headers(account) == {"Authorization": "Bearer access-one"}


class TestSigningSomeoneIn:
    async def test_the_address_is_pinterests_own_approval_page(
        self,
        platform: PinterestPlatform,
    ) -> None:
        step = await start(platform, login())

        address = urlparse(step.url)
        sent = {name: values[0] for name, values in parse_qs(address.query).items()}

        assert f"{address.scheme}://{address.netloc}{address.path}" == (
            pinterest_module.SIGN_IN_URL
        )
        assert sent["response_type"] == "code"
        assert sent["client_id"] == "client-id"
        assert sent["redirect_uri"] == REDIRECT

    async def test_the_scopes_are_separated_by_commas(
        self,
        platform: PinterestPlatform,
    ) -> None:
        step = await start(platform, login())

        # Pinterest wants commas here. Spaces, which nearly every other
        # network uses, are read as part of the first scope's name.
        assert parse_qs(urlparse(step.url).query)["scope"] == [
            "boards:read,boards:write,pins:read,pins:write"
        ]

    async def test_it_asks_for_both_board_and_pin_permissions(
        self,
        platform: PinterestPlatform,
    ) -> None:
        # Creating a pin needs all four. Asking for only the pin ones gets a
        # 403 that reads like something else entirely.
        assert DEFAULT_SCOPES == (
            "boards:read",
            "boards:write",
            "pins:read",
            "pins:write",
        )

    async def test_there_is_no_pkce_here_and_nothing_to_remember(
        self,
        platform: PinterestPlatform,
    ) -> None:
        step = await start(platform, login())

        # Pinterest v5 does not support PKCE. Sending a code_challenge does
        # not make the sign-in safer, it makes it fail.
        assert "code_challenge" not in step.url
        assert step.remember == {}

    async def test_it_makes_a_state_when_you_do_not(
        self,
        platform: PinterestPlatform,
    ) -> None:
        step = await start(platform, login())

        assert step.state
        assert parse_qs(urlparse(step.url).query)["state"] == [step.state]

    async def test_it_keeps_the_state_you_gave_it(
        self,
        platform: PinterestPlatform,
    ) -> None:
        step = await start(platform, login(state="mine"))

        assert step.state == "mine"

    async def test_it_asks_for_exactly_the_scopes_you_named(
        self,
        platform: PinterestPlatform,
    ) -> None:
        step = await start(platform, login(scopes=("pins:read", "boards:read")))

        assert parse_qs(urlparse(step.url).query)["scope"] == ["pins:read,boards:read"]

    async def test_starting_without_credentials_says_where_to_get_them(
        self,
        platform: PinterestPlatform,
    ) -> None:
        with pytest.raises(ConfigError) as refused:
            await platform.start_login(login(app=None))

        said = str(refused.value)
        assert pinterest_module.PORTAL_URL in said
        assert "save_app" in said


class TestFinishingTheSignIn:
    async def test_it_swaps_the_code_for_a_pair_of_tokens(
        self,
        platform: PinterestPlatform,
    ) -> None:
        with respx.mock() as network:
            swap = network.post(f"{API}/oauth/token").mock(
                return_value=httpx.Response(200, json=a_token())
            )
            stub_me(network)

            done = await platform.finish_login(
                login(state="mine"), {"code": "the-code", "state": "mine"}
            )

        sent = form_of(swap.calls.last.request)
        assert sent["grant_type"] == ["authorization_code"]
        assert sent["code"] == ["the-code"]
        assert sent["redirect_uri"] == [REDIRECT]
        assert "continuous_refresh" not in sent

        expected = base64.b64encode(b"client-id:client-secret").decode()
        assert swap.calls.last.request.headers["Authorization"] == f"Basic {expected}"

        connection = done.connection
        assert connection.id == f"pinterest:{ACCOUNT}"
        assert connection.platform == "pinterest"
        assert connection.account_id == ACCOUNT
        assert connection.account_name == ACCOUNT
        assert connection.token.access_token == "access-one"
        assert connection.token.refresh_token == "refresh-one"
        assert connection.token.expires_at is not None
        assert connection.scopes == DEFAULT_SCOPES
        assert (
            connection.extra["profile_url"] == f"https://www.pinterest.com/{ACCOUNT}/"
        )
        assert connection.extra["account_type"] == "BUSINESS"

    async def test_it_does_not_choose_a_board_for_anybody(
        self,
        platform: PinterestPlatform,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/oauth/token").mock(
                return_value=httpx.Response(200, json=a_token())
            )
            stub_me(network)

            done = await platform.finish_login(login(), {"code": "the-code"})

        # Picking whichever board the API happened to list first would send
        # somebody's pins somewhere arbitrary and look like it worked.
        assert "board_id" not in done.connection.extra

    async def test_it_records_what_pinterest_actually_granted(
        self,
        platform: PinterestPlatform,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/oauth/token").mock(
                return_value=httpx.Response(
                    200, json=a_token(scope="pins:read pins:write")
                )
            )
            stub_me(network)

            done = await platform.finish_login(login(), {"code": "the-code"})

        assert done.connection.scopes == ("pins:read", "pins:write")

    async def test_a_reply_with_no_scope_keeps_what_we_asked_for(
        self,
        platform: PinterestPlatform,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/oauth/token").mock(
                return_value=httpx.Response(200, json=a_token(scope=""))
            )
            stub_me(network)

            done = await platform.finish_login(login(), {"code": "the-code"})

        assert done.connection.scopes == DEFAULT_SCOPES

    async def test_it_records_when_the_refresh_token_itself_runs_out(
        self,
        platform: PinterestPlatform,
    ) -> None:
        # Pinterest is one of the few networks that says. An app that keeps
        # this can warn somebody in week eight; an app that cannot only
        # finds out on the day the account stops working.
        before = datetime.now(UTC)

        with respx.mock() as network:
            network.post(f"{API}/oauth/token").mock(
                return_value=httpx.Response(200, json=a_token())
            )
            stub_me(network)

            done = await platform.finish_login(login(), {"code": "the-code"})

        runs_out = done.connection.token.refresh_token_expires_at
        assert runs_out is not None
        assert runs_out >= before + timedelta(days=59)

    async def test_a_reply_that_does_not_say_leaves_that_unknown(
        self,
        platform: PinterestPlatform,
    ) -> None:
        # A guess here would have an app telling somebody to reconnect an
        # account that was fine. Unknown means unknown.
        with respx.mock() as network:
            reply = a_token()
            del reply["refresh_token_expires_in"]
            network.post(f"{API}/oauth/token").mock(
                return_value=httpx.Response(200, json=reply)
            )
            stub_me(network)

            done = await platform.finish_login(login(), {"code": "the-code"})

        assert done.connection.token.refresh_token_expires_at is None

    async def test_a_token_with_no_expiry_is_treated_as_lasting_a_month(
        self,
        platform: PinterestPlatform,
    ) -> None:
        before = datetime.now(UTC)

        with respx.mock() as network:
            reply = a_token()
            del reply["expires_in"]
            network.post(f"{API}/oauth/token").mock(
                return_value=httpx.Response(200, json=reply)
            )
            stub_me(network)

            done = await platform.finish_login(login(), {"code": "the-code"})

        runs_out = done.connection.token.expires_at
        assert runs_out is not None
        assert runs_out >= before + timedelta(days=30)

    async def test_a_state_that_does_not_match_is_refused(
        self,
        platform: PinterestPlatform,
    ) -> None:
        with pytest.raises(AuthError, match="did not start here"):
            await platform.finish_login(
                login(state="mine"), {"code": "the-code", "state": "somebody-elses"}
            )

    async def test_a_person_who_pressed_cancel_is_not_an_error_to_hunt_for(
        self,
        platform: PinterestPlatform,
    ) -> None:
        with pytest.raises(AuthError, match="pressed cancel"):
            await platform.finish_login(
                login(), {"error": "access_denied", "error_description": "no thanks"}
            )

    async def test_a_callback_with_no_code_says_what_to_pass(
        self,
        platform: PinterestPlatform,
    ) -> None:
        with pytest.raises(AuthError, match="whole query string"):
            await platform.finish_login(login(), {})

    async def test_finishing_without_credentials_says_where_to_get_them(
        self,
        platform: PinterestPlatform,
    ) -> None:
        with pytest.raises(ConfigError, match="save_app"):
            await platform.finish_login(login(app=None), {"code": "c"})

    async def test_a_reply_with_no_token_in_it_says_so_plainly(
        self,
        platform: PinterestPlatform,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/oauth/token").mock(
                return_value=httpx.Response(200, json={"token_type": "bearer"})
            )

            with pytest.raises(PlatformError, match="access_token"):
                await platform.finish_login(login(), {"code": "c"})


class TestRenewingAToken:
    async def test_both_halves_are_replaced_and_both_have_to_be_saved(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            renew = network.post(f"{API}/oauth/token").mock(
                return_value=httpx.Response(
                    200,
                    json=a_token(
                        access_token="access-two", refresh_token="refresh-two"
                    ),
                )
            )

            token = await platform.refresh(account, APP)

        sent = form_of(renew.calls.last.request)
        assert sent["grant_type"] == ["refresh_token"]
        assert sent["refresh_token"] == ["refresh-one"]

        # Pinterest hands out a new refresh token every single time, so the
        # one we renewed with is already dead.
        assert token.access_token == "access-two"
        assert token.refresh_token == "refresh-two"
        assert token.expires_at is not None

    async def test_an_older_app_can_ask_for_the_rotating_kind(
        self,
        account: Connection,
    ) -> None:
        platform = PinterestPlatform(retries=ONCE, continuous_refresh=True)

        with respx.mock() as network:
            renew = network.post(f"{API}/oauth/token").mock(
                return_value=httpx.Response(200, json=a_token())
            )

            await platform.refresh(account, APP)

        assert form_of(renew.calls.last.request)["continuous_refresh"] == ["true"]

    async def test_a_renewal_that_says_nothing_new_keeps_the_one_we_had(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            reply = a_token(access_token="access-two")
            del reply["refresh_token"]
            network.post(f"{API}/oauth/token").mock(
                return_value=httpx.Response(200, json=reply)
            )

            token = await platform.refresh(account, APP)

        assert token.refresh_token == "refresh-one"

    async def test_a_renewal_records_the_new_refresh_token_expiry(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        before = datetime.now(UTC)

        with respx.mock() as network:
            network.post(f"{API}/oauth/token").mock(
                return_value=httpx.Response(200, json=a_token())
            )

            token = await platform.refresh(account, APP)

        runs_out = token.refresh_token_expires_at
        assert runs_out is not None
        assert runs_out >= before + timedelta(days=59)

    async def test_a_renewal_that_does_not_say_keeps_what_we_already_knew(
        self,
        platform: PinterestPlatform,
    ) -> None:
        knew = datetime.now(UTC) + timedelta(days=40)
        account = Connection(
            id=f"pinterest:{ACCOUNT}",
            platform="pinterest",
            host=None,
            account_id=ACCOUNT,
            account_name=ACCOUNT,
            token=Token(
                access_token="access-one",
                refresh_token="refresh-one",
                refresh_token_expires_at=knew,
            ),
            scopes=DEFAULT_SCOPES,
        )

        with respx.mock() as network:
            reply = a_token(access_token="access-two")
            del reply["refresh_token_expires_in"]
            network.post(f"{API}/oauth/token").mock(
                return_value=httpx.Response(200, json=reply)
            )

            token = await platform.refresh(account, APP)

        assert token.refresh_token_expires_at == knew

    async def test_renewing_without_credentials_says_where_to_get_them(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(ConfigError, match="save_app"):
            await platform.refresh(account, None)

    async def test_no_refresh_token_means_signing_in_again(
        self,
        platform: PinterestPlatform,
    ) -> None:
        alone = Connection(
            id="pinterest:nobody",
            platform="pinterest",
            host=None,
            account_id="nobody",
            account_name="nobody",
            token=Token(access_token="access-one"),
        )

        with pytest.raises(TokenExpiredError, match="connect their account again"):
            await platform.refresh(alone, APP)

    async def test_a_refusal_names_the_sixty_days(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/oauth/token").mock(
                return_value=httpx.Response(401, json={"code": 2, "message": "nope"})
            )

            with pytest.raises(TokenExpiredError) as refused:
                await platform.refresh(account, APP)

        assert "60 days" in str(refused.value)

    async def test_pinterest_having_trouble_of_its_own_is_not_a_dead_token(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/oauth/token").mock(
                return_value=httpx.Response(503, json={"message": "Down"})
            )

            with pytest.raises(PlatformError) as trouble:
                await platform.refresh(account, APP)

        assert not isinstance(trouble.value, TokenExpiredError)


class TestWhatPinterestAllows:
    async def test_the_description_and_title_have_their_own_limits(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        limits = await platform.limits(account)

        assert limits.max_text_length == 800
        assert limits.max_title_length == 100
        assert limits.text_counted_in is TextCount.CHARACTERS
        assert limits.max_images == 5
        assert limits.max_videos == 1

    async def test_it_says_it_does_not_know_rather_than_guessing_a_file_size(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        limits = await platform.limits(account)

        # None means "we do not know", never "nothing allowed". Pinterest
        # publishes no number here, so neither do we.
        assert limits.max_image_bytes is None
        assert limits.max_video_bytes is None


class TestEveryPinNeedsABoard:
    async def test_a_post_with_no_board_says_exactly_what_to_do(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(assert_all_called=False) as network:
            route = network.post(f"{API}/pins").mock(
                return_value=httpx.Response(201, json=A_PIN)
            )

            with pytest.raises(InvalidPostError) as refused:
                await platform.publish(account, Post(media=(A_PICTURE,)))

        said = str(refused.value)
        assert "every pin lives on a board" in said
        assert 'options={"board_id"' in said
        assert 'extra={"board_id"' in said
        assert "boards(" in said
        # Never a raw API error: nothing was sent at all.
        assert not route.called

    async def test_the_board_on_the_post_is_the_one_used(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            route = network.post(f"{API}/pins").mock(
                return_value=httpx.Response(201, json=A_PIN)
            )

            await platform.publish(account, a_post())

        assert (
            route.calls.last.request.read()
            .decode()
            .startswith('{"board_id":"1234567890"')
        )

    async def test_a_board_your_app_saved_on_the_connection_is_the_fallback(
        self,
        platform: PinterestPlatform,
    ) -> None:
        settled = Connection(
            id="pinterest:ada",
            platform="pinterest",
            host=None,
            account_id=ACCOUNT,
            account_name=ACCOUNT,
            token=Token(access_token="access-one"),
            scopes=DEFAULT_SCOPES,
            extra={"board_id": "55555"},
        )

        with respx.mock() as network:
            route = network.post(f"{API}/pins").mock(
                return_value=httpx.Response(201, json=A_PIN)
            )

            await platform.publish(settled, Post(media=(A_PICTURE,)))

        assert '"board_id":"55555"' in route.calls.last.request.read().decode()

    async def test_the_post_beats_the_connection(
        self,
        platform: PinterestPlatform,
    ) -> None:
        settled = Connection(
            id="pinterest:ada",
            platform="pinterest",
            host=None,
            account_id=ACCOUNT,
            account_name=ACCOUNT,
            token=Token(access_token="access-one"),
            scopes=DEFAULT_SCOPES,
            extra={"board_id": "55555"},
        )

        with respx.mock() as network:
            route = network.post(f"{API}/pins").mock(
                return_value=httpx.Response(201, json=A_PIN)
            )

            await platform.publish(settled, a_post())

        assert '"board_id":"1234567890"' in route.calls.last.request.read().decode()

    async def test_a_board_id_that_is_not_text_is_a_mistake(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(InvalidPostError, match="board_id is 1234567890"):
            await platform.publish(
                account, Post(media=(A_PICTURE,), options={"board_id": 1234567890})
            )

    async def test_a_board_id_saved_as_a_number_says_which_connection(
        self,
        platform: PinterestPlatform,
    ) -> None:
        # This one comes off the connection your app saved rather than off
        # the post, so the message has to say where to go and fix it.
        wrong = Connection(
            id="pinterest:ada",
            platform="pinterest",
            host=None,
            account_id=ACCOUNT,
            account_name=ACCOUNT,
            token=Token(access_token="access-one"),
            scopes=DEFAULT_SCOPES,
            extra={"board_id": 55555},
        )

        with pytest.raises(InvalidPostError, match="saved on connection"):
            await platform.publish(wrong, Post(media=(A_PICTURE,)))


class TestListingBoards:
    async def test_it_reads_the_boards_this_account_has(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            route = network.get(f"{API}/boards").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "items": [
                            {"id": "1", "name": "Recipes", "privacy": "PUBLIC"},
                            {"id": "2", "name": "Ideas", "privacy": "SECRET"},
                            "not a board",
                            {"name": "no id at all"},
                        ],
                        "bookmark": None,
                    },
                )
            )

            found = await platform.boards(account)

        assert found == [
            Board(id="1", name="Recipes", privacy="PUBLIC"),
            Board(id="2", name="Ideas", privacy="SECRET"),
        ]
        assert route.calls.last.request.url.params["page_size"] == "25"

    async def test_you_can_ask_for_a_bigger_page(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            route = network.get(f"{API}/boards").mock(
                return_value=httpx.Response(200, json={"items": []})
            )

            assert await platform.boards(account, page_size=100) == []

        assert route.calls.last.request.url.params["page_size"] == "100"

    async def test_a_reply_that_is_not_a_list_gives_nothing(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.get(f"{API}/boards").mock(
                return_value=httpx.Response(200, json={"items": "odd"})
            )

            assert await platform.boards(account) == []


class TestPublishing:
    async def test_a_picture_goes_up_as_part_of_the_pin(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            route = network.post(f"{API}/pins").mock(
                return_value=httpx.Response(201, json=A_PIN)
            )

            result = await platform.publish(
                account, a_post(text="What it is", options={"title": "A thing"})
            )

        sent = route.calls.last.request.read().decode()
        assert '"description":"What it is"' in sent
        assert '"title":"A thing"' in sent
        assert '"source_type":"image_base64"' in sent
        assert '"content_type":"image/png"' in sent
        assert f'"data":"{base64.b64encode(b"a picture").decode()}"' in sent

        assert result.id == "9876543210"
        # The `link` on the reply is where the pin points, not where the pin
        # is. Handing that back would send people to somebody's shop.
        assert result.url == "https://www.pinterest.com/pin/9876543210/"
        assert result.state is PostState.DONE
        assert result.raw == A_PIN

    async def test_a_picture_already_online_is_left_where_it_is(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        online = Media.from_url("https://pics.example/a.jpg")

        with respx.mock() as network:
            route = network.post(f"{API}/pins").mock(
                return_value=httpx.Response(201, json=A_PIN)
            )

            await platform.publish(
                account, Post(media=(online,), options={"board_id": BOARD})
            )

        sent = route.calls.last.request.read().decode()
        # Pinterest is one of the few networks that really will go and fetch
        # a picture, so downloading it first would be wasted work.
        assert '"source_type":"image_url"' in sent
        assert '"url":"https://pics.example/a.jpg"' in sent

    async def test_several_pictures_become_one_pin_you_can_swipe(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            route = network.post(f"{API}/pins").mock(
                return_value=httpx.Response(201, json=A_PIN)
            )

            await platform.publish(
                account,
                Post(
                    media=(A_PICTURE, Media.from_bytes(b"another", filename="b.jpg")),
                    options={"board_id": BOARD},
                ),
            )

        sent = route.calls.last.request.read().decode()
        assert '"source_type":"multiple_image_base64"' in sent
        assert sent.count('"content_type"') == 2

    async def test_several_pictures_already_online_stay_online(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            route = network.post(f"{API}/pins").mock(
                return_value=httpx.Response(201, json=A_PIN)
            )

            await platform.publish(
                account,
                Post(
                    media=(
                        Media.from_url("https://pics.example/a.jpg"),
                        Media.from_url("https://pics.example/b.jpg"),
                    ),
                    options={"board_id": BOARD},
                ),
            )

        assert (
            '"source_type":"multiple_image_urls"'
            in route.calls.last.request.read().decode()
        )

    async def test_mixing_links_and_files_in_one_pin_is_refused(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(InvalidPostError, match="all links or all files"):
            await platform.publish(
                account,
                Post(
                    media=(A_PICTURE, Media.from_url("https://pics.example/b.jpg")),
                    options={"board_id": BOARD},
                ),
            )

    async def test_mixing_a_video_with_pictures_is_refused(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(InvalidPostError, match="a picture or a video"):
            await platform.publish(
                account,
                Post(
                    media=(A_PICTURE, Media.from_bytes(b"clip", filename="a.mp4")),
                    options={"board_id": BOARD},
                ),
            )

    async def test_a_pin_of_words_alone_is_refused_by_name(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(NotSupportedError) as refused:
            await platform.publish(account, Post(text="just some words"))

        said = str(refused.value)
        assert "a picture or a video" in said
        assert "the pin's description" in said

    async def test_replying_says_pinterest_has_no_comments_at_all(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(NotSupportedError, match="no comments in it at all"):
            await platform.publish(account, a_post(reply_to="9876543210"))

    async def test_the_reason_for_that_is_a_sentence_of_its_own(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        # `what` finishes the sentence "pinterest does not support ...", so
        # a paragraph in there reads as one enormous sentence.
        with pytest.raises(NotSupportedError) as refused:
            await platform.publish(account, a_post(reply_to="9876543210"))

        assert refused.value.what == "replying to pins"
        assert refused.value.suggestion is not None
        assert "no comments in it at all" in refused.value.suggestion

    async def test_scheduling_is_refused_rather_than_pinned_now(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(NotSupportedError, match="scheduling posts"):
            await platform.publish(
                account,
                a_post(publish_at=datetime.now(UTC) + timedelta(hours=1)),
            )

    @pytest.mark.parametrize(
        ("options", "wanted"),
        [
            ({"link": "https://shop.example"}, '"link":"https://shop.example"'),
            ({"alt_text": "A red chair"}, '"alt_text":"A red chair"'),
            ({"dominant_color": "#6E7874"}, '"dominant_color":"#6E7874"'),
            ({"board_section_id": "77"}, '"board_section_id":"77"'),
        ],
    )
    async def test_the_settings_it_accepts_reach_pinterest(
        self,
        platform: PinterestPlatform,
        account: Connection,
        options: dict[str, Any],
        wanted: str,
    ) -> None:
        with respx.mock() as network:
            route = network.post(f"{API}/pins").mock(
                return_value=httpx.Response(201, json=A_PIN)
            )

            await platform.publish(account, a_post(options=options))

        assert wanted in route.calls.last.request.read().decode()

    @pytest.mark.parametrize(
        ("options", "wanted"),
        [
            ({"visibility": "public"}, "does not know the post option"),
            ({"title": "x" * 101}, "101 characters"),
            ({"alt_text": 7}, "alt_text is 7"),
        ],
    )
    async def test_a_setting_pinterest_does_not_know_costs_no_request(
        self,
        platform: PinterestPlatform,
        account: Connection,
        options: dict[str, Any],
        wanted: str,
    ) -> None:
        with respx.mock(assert_all_called=False) as network:
            route = network.post(f"{API}/pins").mock(
                return_value=httpx.Response(201, json=A_PIN)
            )

            with pytest.raises(InvalidPostError, match=wanted):
                await platform.publish(account, a_post(options=options))

        assert not route.called

    async def test_a_description_over_the_limit_never_leaves_the_building(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(assert_all_called=False) as network:
            route = network.post(f"{API}/pins").mock(
                return_value=httpx.Response(201, json=A_PIN)
            )

            with pytest.raises(InvalidPostError, match="801 characters"):
                await platform.publish(account, a_post(text="x" * 801))

        assert not route.called

    async def test_more_than_five_pictures_never_leaves_the_building(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(assert_all_called=False) as network:
            route = network.post(f"{API}/pins").mock(
                return_value=httpx.Response(201, json=A_PIN)
            )

            with pytest.raises(InvalidPostError, match="at most 5"):
                await platform.publish(
                    account,
                    Post(media=(A_PICTURE,) * 6, options={"board_id": BOARD}),
                )

        assert not route.called

    async def test_a_pin_missing_a_permission_is_named_before_we_ask(
        self,
        platform: PinterestPlatform,
    ) -> None:
        narrow = Connection(
            id="pinterest:ada",
            platform="pinterest",
            host=None,
            account_id=ACCOUNT,
            account_name=ACCOUNT,
            token=Token(access_token="access-one"),
            scopes=("pins:read", "pins:write"),
        )

        with respx.mock(assert_all_called=False) as network:
            route = network.post(f"{API}/pins").mock(
                return_value=httpx.Response(201, json=A_PIN)
            )

            with pytest.raises(NotAllowedError) as refused:
                await platform.publish(narrow, a_post())

        said = str(refused.value)
        assert "boards:read" in said
        assert "boards:write" in said
        assert not route.called

    async def test_a_connection_that_never_said_what_it_has_is_left_alone(
        self,
        platform: PinterestPlatform,
    ) -> None:
        quiet = Connection(
            id="pinterest:ada",
            platform="pinterest",
            host=None,
            account_id=ACCOUNT,
            account_name=ACCOUNT,
            token=Token(access_token="access-one"),
        )

        with respx.mock() as network:
            network.post(f"{API}/pins").mock(
                return_value=httpx.Response(201, json=A_PIN)
            )

            # An empty `scopes` means we were never told, not that the
            # account has none. Refusing on that would break every
            # connection saved before scopes were recorded.
            await platform.publish(quiet, a_post())

    async def test_a_reply_with_no_id_in_it_says_so_plainly(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/pins").mock(return_value=httpx.Response(201, json={}))

            with pytest.raises(PlatformError, match="'id'"):
                await platform.publish(account, a_post())


class TestSendingAVideo:
    async def test_it_registers_uploads_and_waits_before_making_the_pin(
        self,
        platform: PinterestPlatform,
        account: Connection,
        waits: list[float],
    ) -> None:
        clip = Media.from_bytes(b"a clip", filename="a.mp4")

        with respx.mock() as network:
            register = network.post(f"{API}/media").mock(
                return_value=httpx.Response(
                    201,
                    json={
                        "media_id": "media-1",
                        "media_type": "video",
                        "upload_url": "https://pinterest-media.s3.example/upload",
                        "upload_parameters": {
                            "key": "uploads/abc",
                            "policy": "a-policy",
                            "x-amz-signature": "a-signature",
                        },
                    },
                )
            )
            store = network.post("https://pinterest-media.s3.example/upload").mock(
                return_value=httpx.Response(204)
            )
            status = network.get(f"{API}/media/media-1").mock(
                side_effect=[
                    httpx.Response(200, json={"status": "processing"}),
                    httpx.Response(200, json={"status": "succeeded"}),
                ]
            )
            pin = network.post(f"{API}/pins").mock(
                return_value=httpx.Response(201, json=A_PIN)
            )

            await platform.publish(
                account, Post(media=(clip,), options={"board_id": BOARD})
            )

        assert register.calls.last.request.read() == b'{"media_type":"video"}'

        sent = store.calls.last.request.read()
        assert b'name="key"' in sent
        assert b"uploads/abc" in sent
        assert b'name="x-amz-signature"' in sent
        assert b'name="file"' in sent
        assert b"a clip" in sent
        # The file goes to Amazon, not to Pinterest. Sending the account's
        # token there would hand it to somebody else entirely.
        assert "Authorization" not in store.calls.last.request.headers

        assert status.call_count == 2
        assert waits == [0.0, 0.0]

        body = pin.calls.last.request.read().decode()
        assert '"source_type":"video_id"' in body
        assert '"media_id":"media-1"' in body

    async def test_a_video_pinterest_gives_up_on_is_a_problem_with_the_file(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/media").mock(
                return_value=httpx.Response(
                    201,
                    json={
                        "media_id": "media-1",
                        "upload_url": "https://pinterest-media.s3.example/upload",
                        "upload_parameters": {},
                    },
                )
            )
            network.post("https://pinterest-media.s3.example/upload").mock(
                return_value=httpx.Response(204)
            )
            network.get(f"{API}/media/media-1").mock(
                return_value=httpx.Response(200, json={"status": "failed"})
            )

            with pytest.raises(InvalidPostError, match="gave up on this video"):
                await platform.publish(
                    account,
                    Post(
                        media=(Media.from_bytes(b"clip", filename="a.mp4"),),
                        options={"board_id": BOARD},
                    ),
                )

    async def test_a_video_that_never_finishes_says_what_to_turn_up(
        self,
        account: Connection,
        waits: list[float],
    ) -> None:
        platform = PinterestPlatform(retries=ONCE, media_checks=3)

        with respx.mock() as network:
            network.post(f"{API}/media").mock(
                return_value=httpx.Response(
                    201,
                    json={
                        "media_id": "media-1",
                        "upload_url": "https://pinterest-media.s3.example/upload",
                        "upload_parameters": {},
                    },
                )
            )
            network.post("https://pinterest-media.s3.example/upload").mock(
                return_value=httpx.Response(204)
            )
            network.get(f"{API}/media/media-1").mock(
                return_value=httpx.Response(200, json={"status": "processing"})
            )

            with pytest.raises(PlatformError, match="media_checks"):
                await platform.publish(
                    account,
                    Post(
                        media=(Media.from_bytes(b"clip", filename="a.mp4"),),
                        options={"board_id": BOARD},
                    ),
                )

        assert len(waits) == 3

    async def test_a_video_that_is_only_a_link_says_what_to_do_instead(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(InvalidPostError, match=r"Media\.from_file"):
            await platform.publish(
                account,
                Post(
                    media=(Media.from_url("https://clips.example/a.mp4"),),
                    options={"board_id": BOARD},
                ),
            )

    async def test_a_registration_with_no_id_in_it_says_so_plainly(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/media").mock(
                return_value=httpx.Response(201, json={"upload_url": "https://s3/x"})
            )

            with pytest.raises(PlatformError, match="media_id"):
                await platform.publish(
                    account,
                    Post(
                        media=(Media.from_bytes(b"clip", filename="a.mp4"),),
                        options={"board_id": BOARD},
                    ),
                )

    async def test_upload_parameters_that_are_not_an_object_are_shrugged_off(
        self,
        platform: PinterestPlatform,
        account: Connection,
        waits: list[float],
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/media").mock(
                return_value=httpx.Response(
                    201,
                    json={
                        "media_id": "media-1",
                        "upload_url": "https://pinterest-media.s3.example/upload",
                        "upload_parameters": None,
                    },
                )
            )
            store = network.post("https://pinterest-media.s3.example/upload").mock(
                return_value=httpx.Response(204)
            )
            network.get(f"{API}/media/media-1").mock(
                return_value=httpx.Response(200, json={"status": "succeeded"})
            )
            network.post(f"{API}/pins").mock(
                return_value=httpx.Response(201, json=A_PIN)
            )

            await platform.publish(
                account,
                Post(
                    media=(Media.from_bytes(b"clip", filename="a.mp4"),),
                    options={"board_id": BOARD},
                ),
            )

        assert b'name="file"' in store.calls.last.request.read()


class TestRemovingAPin:
    async def test_it_asks_pinterest_to_delete_the_pin(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            route = network.delete(f"{API}/pins/9876543210").mock(
                return_value=httpx.Response(204)
            )

            await platform.delete_post(account, "9876543210")

        assert route.called

    async def test_a_pin_that_is_not_there_says_so(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.delete(f"{API}/pins/1").mock(
                return_value=httpx.Response(
                    404, json={"code": 2, "message": "Pin not found"}
                )
            )

            with pytest.raises(NotFoundError, match="Pin not found"):
                await platform.delete_post(account, "1")


class TestWhenPinterestSaysNo:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, AuthError),
            (403, NotAllowedError),
            (404, NotFoundError),
        ],
    )
    async def test_it_names_the_problem_in_our_own_words(
        self,
        platform: PinterestPlatform,
        account: Connection,
        status: int,
        expected: type[Exception],
    ) -> None:
        with respx.mock() as network:
            network.delete(f"{API}/pins/1").mock(
                return_value=httpx.Response(status, json={"code": 3, "message": "Nope"})
            )

            with pytest.raises(expected, match="Nope"):
                await platform.delete_post(account, "1")

    async def test_a_refused_pin_names_the_four_permissions_it_needs(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/pins").mock(
                return_value=httpx.Response(
                    403, json={"code": 7, "message": "Not authorized"}
                )
            )

            with pytest.raises(NotAllowedError) as refused:
                await platform.publish(account, a_post())

        assert "boards:read" in str(refused.value)

    async def test_a_slow_down_says_the_allowance_may_be_a_daily_one(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.delete(f"{API}/pins/1").mock(
                return_value=httpx.Response(
                    429,
                    headers={"x-ratelimit-limit": "1000", "x-ratelimit-remaining": "0"},
                    json={"code": 29, "message": "Too many requests"},
                )
            )

            with pytest.raises(RateLimitError) as slow:
                await platform.delete_post(account, "1")

        said = str(slow.value)
        # On Trial the allowance is counted per day, so the usual "wait a
        # few seconds" advice is wrong and worth saying so.
        assert "a day" in said
        assert "Trial" in said

    async def test_a_slow_down_passes_on_how_long_to_wait_when_it_says(
        self,
        platform: PinterestPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.delete(f"{API}/pins/1").mock(
                return_value=httpx.Response(
                    429, headers={"Retry-After": "30"}, json={"code": 29}
                )
            )

            with pytest.raises(RateLimitError) as slow:
                await platform.delete_post(account, "1")

        assert slow.value.retry_after == 30.0


class TestSettingItUp:
    async def test_the_pause_between_checks_is_a_real_pause(self) -> None:
        # The rest of these tests watch the pauses instead of taking them,
        # so this is the one that runs the waiting itself.
        await pinterest_module._wait(0.0)


class TestPinterestBehavesLikeTheOthers(PlatformChecks):
    def make_platform(self) -> Platform:
        return PinterestPlatform(transport=self.transport, retries=ONCE)

    def make_post(self, text: str) -> Post:
        # Pinterest looks at nothing without a picture and a board, so a
        # post of words alone would be refused for one of those instead and
        # its length would never be measured at all.
        return Post(
            text=text,
            media=(Media.from_bytes(b"a picture", filename="a.png"),),
            options={"board_id": BOARD},
        )

    def make_connection(self) -> Connection | None:
        return Connection(
            id=f"pinterest:{ACCOUNT}",
            platform="pinterest",
            host=None,
            account_id=ACCOUNT,
            account_name=ACCOUNT,
            token=Token(access_token="access-one", refresh_token="refresh-one"),
            scopes=DEFAULT_SCOPES,
        )

    def make_transport(self) -> httpx.AsyncBaseTransport | None:
        return RecordingTransport({"POST /v5/pins": A_PIN})
