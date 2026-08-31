"""Tests for the X (Twitter) platform."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
    UpdateKind,
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
from socialchimp.platforms import x as x_module
from socialchimp.platforms.x import (
    DEFAULT_SCOPES,
    PartialThreadError,
    XPlatform,
    rate_limit_in,
)
from socialchimp.testing import PlatformChecks, RecordingTransport

API = "https://api.x.com/2"
REDIRECT = "https://app.example/callback"

# One try and no waiting, so the error tests do not spend real seconds asleep.
ONCE = Retries(attempts=1)

ACCOUNT_ID = "1234567890"
HANDLE = "ada"

APP = AppCredentials(
    platform="x",
    host=None,
    client_id="client-id",
    client_secret="client-secret",
)

A_TWEET: dict[str, Any] = {
    "data": {
        "id": "1800000000000000001",
        "text": "Hello",
        "edit_history_tweet_ids": ["1800000000000000001"],
    }
}

A_MENTION: dict[str, Any] = {
    "id": "1800000000000000009",
    "text": "@ada hello",
    "author_id": "999",
    "created_at": "2026-08-31T10:00:00.000Z",
}

# One thumbs-up with a skin tone: two characters to Python, four units to
# anything counting the way JavaScript does - which is how X counts.
BIG_LETTER = "\U0001f44d\U0001f3fd"


@pytest.fixture
def platform() -> XPlatform:
    """A platform that gives up after one try and never really sleeps."""
    return XPlatform(retries=ONCE, media_wait_seconds=0.0)


@pytest.fixture
def account() -> Connection:
    """A connected X account."""
    return Connection(
        id=f"x:{ACCOUNT_ID}",
        platform="x",
        host=None,
        account_id=ACCOUNT_ID,
        account_name=f"@{HANDLE}",
        token=Token(access_token="access-one", refresh_token="refresh-one"),
        scopes=DEFAULT_SCOPES,
    )


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every pause instead of taking it."""
    recorded: list[float] = []

    async def remember(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(x_module, "_wait", remember)
    return recorded


def login(
    *,
    state: str | None = None,
    scopes: tuple[str, ...] = (),
    app: AppCredentials | None = APP,
) -> LoginRequest:
    """A login request with the everyday values already filled in."""
    return LoginRequest(redirect_uri=REDIRECT, scopes=scopes, state=state, app=app)


async def start(platform: XPlatform, request: LoginRequest) -> SendToNetwork:
    """Start a login, and insist X answered with an address to visit."""
    step = await platform.start_login(request)
    assert isinstance(step, SendToNetwork)
    return step


def form_of(request: httpx.Request) -> dict[str, list[str]]:
    """Read a sent form back into a dictionary."""
    return parse_qs(request.content.decode(), keep_blank_values=True)


def challenge_for(verifier: str) -> str:
    """Work out the code challenge a verifier should produce."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def a_token(**extra: object) -> dict[str, Any]:
    """What X answers a code swap or a renewal with."""
    return {
        "token_type": "bearer",
        "expires_in": 7200,
        "access_token": "access-one",
        "refresh_token": "refresh-one",
        "scope": " ".join(DEFAULT_SCOPES),
        **extra,
    }


def stub_me(network: respx.Router) -> respx.Route:
    """Answer the "who just signed in?" question."""
    return network.get(f"{API}/users/me").mock(
        return_value=httpx.Response(
            200,
            json={"data": {"id": ACCOUNT_ID, "username": HANDLE, "name": "Ada"}},
        )
    )


class TestWhatItSaysItCanDo:
    def test_it_provides_everything_a_platform_must(
        self,
        platform: XPlatform,
    ) -> None:
        checked: Platform = platform
        deletes: CanDeletePosts = platform
        reads: CanReadUpdates = platform

        assert isinstance(checked, Platform)
        assert isinstance(deletes, CanDeletePosts)
        assert isinstance(reads, CanReadUpdates)
        assert platform.name == "x"

    def test_it_lists_the_features_x_really_has(self, platform: XPlatform) -> None:
        for feature in (
            Feature.POST_TEXT,
            Feature.POST_IMAGE,
            Feature.POST_VIDEO,
            Feature.REPLY,
            Feature.DELETE_POST,
            Feature.READ_POSTS,
        ):
            assert feature in platform.features

    def test_there_is_no_app_to_create_here(self, platform: XPlatform) -> None:
        # You fill in a form in X's own portal and pay for a plan. There is
        # nothing socialchimp can do for you.
        assert Feature.CREATE_APP not in platform.features
        assert not isinstance(platform, CanCreateApp)
        assert not hasattr(platform, "create_app")

    def test_it_does_not_claim_to_schedule_or_to_push_updates(
        self,
        platform: XPlatform,
    ) -> None:
        assert Feature.SCHEDULE not in platform.features
        # Streaming and account activity are both behind a paid product, so
        # mentions are read on a timer instead.
        assert Feature.PUSH_UPDATES not in platform.features


class TestBeingHonestAboutTheMoney:
    def test_the_page_says_access_is_paid_and_points_at_the_portal(self) -> None:
        said = x_module.__doc__
        assert said is not None
        lowered = said.lower()

        assert "paid" in lowered
        assert x_module.PORTAL_URL in said
        assert x_module.PLANS_URL in said

    def test_it_writes_down_no_price_and_no_monthly_cap(self) -> None:
        # X's numbers change constantly. A number written here would be
        # wrong within months and someone would plan around it.
        said = x_module.__doc__
        assert said is not None
        assert "$" not in said

    async def test_a_plan_refusal_says_it_is_the_plan_and_not_your_code(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/tweets").mock(
                return_value=httpx.Response(
                    403,
                    json={
                        "client_id": "client-id",
                        "detail": "When authenticating requests...",
                        "registration_url": "https://developer.x.com/...",
                        "required_enrollment": "Appropriate Level of API Access",
                        "reason": "client-not-enrolled",
                        "title": "Client Forbidden",
                        "type": ("https://api.twitter.com/2/problems/client-forbidden"),
                    },
                )
            )

            with pytest.raises(NotAllowedError) as refused:
                await platform.publish(account, Post(text="Hello"))

        said = str(refused.value)
        assert "plan" in said
        assert "not a bug" in said
        assert x_module.PLANS_URL in said
        assert "Appropriate Level of API Access" in said

    async def test_an_ordinary_403_is_still_an_ordinary_403(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.delete(f"{API}/tweets/1").mock(
                return_value=httpx.Response(
                    403,
                    json={"title": "Forbidden", "detail": "Not your post"},
                )
            )

            with pytest.raises(NotAllowedError, match="Not your post"):
                await platform.delete_post(account, "1")


class TestWhereTheApiIs:
    def test_the_address_is_the_same_for_every_account(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        assert platform.api_base(account) == API

    def test_the_headers_carry_the_accounts_own_token(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        assert platform.auth_headers(account) == {"Authorization": "Bearer access-one"}


class TestSigningSomeoneIn:
    async def test_the_address_carries_a_hashed_secret(
        self,
        platform: XPlatform,
    ) -> None:
        step = await start(platform, login())

        address = urlparse(step.url)
        sent = {name: values[0] for name, values in parse_qs(address.query).items()}

        assert f"{address.scheme}://{address.netloc}{address.path}" == (
            x_module.SIGN_IN_URL
        )
        assert sent["response_type"] == "code"
        assert sent["client_id"] == "client-id"
        assert sent["redirect_uri"] == REDIRECT
        assert sent["code_challenge_method"] == "S256"

        verifier = step.remember["code_verifier"]
        assert isinstance(verifier, str)
        assert sent["code_challenge"] == challenge_for(verifier)
        # Only the hash travels; the secret comes back to your app.
        assert verifier not in step.url

    async def test_it_asks_for_a_refresh_token_by_default(
        self,
        platform: XPlatform,
    ) -> None:
        step = await start(platform, login())

        asked = parse_qs(urlparse(step.url).query)["scope"][0].split()

        # Without offline.access X hands back no refresh token at all and the
        # account stops working two hours later.
        assert "offline.access" in asked
        assert asked == list(DEFAULT_SCOPES)

    async def test_it_asks_for_exactly_the_scopes_you_named(
        self,
        platform: XPlatform,
    ) -> None:
        step = await start(platform, login(scopes=("tweet.write", "offline.access")))

        asked = parse_qs(urlparse(step.url).query)["scope"][0]
        assert asked == "tweet.write offline.access"

    async def test_it_makes_a_state_when_you_do_not(
        self,
        platform: XPlatform,
    ) -> None:
        step = await start(platform, login())

        assert step.state
        assert parse_qs(urlparse(step.url).query)["state"] == [step.state]

    async def test_it_keeps_the_state_you_gave_it(self, platform: XPlatform) -> None:
        step = await start(platform, login(state="mine"))

        assert step.state == "mine"

    async def test_starting_without_credentials_says_where_to_get_them(
        self,
        platform: XPlatform,
    ) -> None:
        with pytest.raises(ConfigError) as refused:
            await platform.start_login(login(app=None))

        said = str(refused.value)
        assert x_module.PORTAL_URL in said
        assert "save_app" in said


class TestFinishingTheSignIn:
    async def test_it_swaps_the_code_for_a_pair_of_tokens(
        self,
        platform: XPlatform,
    ) -> None:
        with respx.mock() as network:
            swap = network.post(f"{API}/oauth2/token").mock(
                return_value=httpx.Response(200, json=a_token())
            )
            stub_me(network)

            done = await platform.finish_login(
                login(state="mine"),
                {"code": "the-code", "state": "mine"},
                {"code_verifier": "the-secret"},
            )

        sent = form_of(swap.calls.last.request)
        assert sent["grant_type"] == ["authorization_code"]
        assert sent["code"] == ["the-code"]
        assert sent["redirect_uri"] == [REDIRECT]
        assert sent["code_verifier"] == ["the-secret"]

        # A confidential app proves who it is with an ordinary Basic header.
        expected = base64.b64encode(b"client-id:client-secret").decode()
        assert swap.calls.last.request.headers["Authorization"] == f"Basic {expected}"

        connection = done.connection
        assert connection.id == f"x:{ACCOUNT_ID}"
        assert connection.platform == "x"
        assert connection.account_id == ACCOUNT_ID
        assert connection.account_name == f"@{HANDLE}"
        assert connection.token.access_token == "access-one"
        assert connection.token.refresh_token == "refresh-one"
        assert connection.token.expires_at is not None
        assert connection.scopes == DEFAULT_SCOPES
        assert connection.extra["profile_url"] == f"https://x.com/{HANDLE}"

    async def test_an_app_with_no_secret_names_itself_in_the_form(
        self,
        platform: XPlatform,
    ) -> None:
        public = AppCredentials(
            platform="x", host=None, client_id="public-id", client_secret=""
        )

        with respx.mock() as network:
            swap = network.post(f"{API}/oauth2/token").mock(
                return_value=httpx.Response(200, json=a_token())
            )
            stub_me(network)

            await platform.finish_login(
                login(app=public), {"code": "the-code"}, {"code_verifier": "s"}
            )

        request = swap.calls.last.request
        assert "Authorization" not in request.headers
        assert form_of(request)["client_id"] == ["public-id"]

    async def test_it_records_what_x_actually_granted(
        self,
        platform: XPlatform,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/oauth2/token").mock(
                return_value=httpx.Response(
                    200, json=a_token(scope="tweet.write users.read")
                )
            )
            stub_me(network)

            done = await platform.finish_login(
                login(), {"code": "the-code"}, {"code_verifier": "s"}
            )

        assert done.connection.scopes == ("tweet.write", "users.read")

    async def test_a_reply_with_no_scope_keeps_what_we_asked_for(
        self,
        platform: XPlatform,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/oauth2/token").mock(
                return_value=httpx.Response(200, json=a_token(scope=""))
            )
            stub_me(network)

            done = await platform.finish_login(
                login(), {"code": "the-code"}, {"code_verifier": "s"}
            )

        assert done.connection.scopes == DEFAULT_SCOPES

    async def test_a_token_with_no_expiry_is_treated_as_lasting_two_hours(
        self,
        platform: XPlatform,
    ) -> None:
        before = datetime.now(UTC)

        with respx.mock() as network:
            reply = a_token()
            del reply["expires_in"]
            network.post(f"{API}/oauth2/token").mock(
                return_value=httpx.Response(200, json=reply)
            )
            stub_me(network)

            done = await platform.finish_login(
                login(), {"code": "the-code"}, {"code_verifier": "s"}
            )

        runs_out = done.connection.token.expires_at
        assert runs_out is not None
        assert runs_out >= before + timedelta(seconds=7200)

    async def test_a_state_that_does_not_match_is_refused(
        self,
        platform: XPlatform,
    ) -> None:
        with pytest.raises(AuthError, match="did not start here"):
            await platform.finish_login(
                login(state="mine"),
                {"code": "the-code", "state": "somebody-elses"},
                {"code_verifier": "s"},
            )

    async def test_a_person_who_pressed_cancel_is_not_an_error_to_hunt_for(
        self,
        platform: XPlatform,
    ) -> None:
        with pytest.raises(AuthError, match="pressed cancel"):
            await platform.finish_login(
                login(),
                {"error": "access_denied", "error_description": "no thanks"},
                {"code_verifier": "s"},
            )

    async def test_a_callback_with_no_code_says_what_to_pass(
        self,
        platform: XPlatform,
    ) -> None:
        with pytest.raises(AuthError, match="whole query string"):
            await platform.finish_login(login(), {}, {"code_verifier": "s"})

    async def test_the_secret_has_to_come_back_too(
        self,
        platform: XPlatform,
    ) -> None:
        with pytest.raises(AuthError, match="did not come back"):
            await platform.finish_login(login(), {"code": "the-code"}, None)

    async def test_finishing_without_credentials_says_where_to_get_them(
        self,
        platform: XPlatform,
    ) -> None:
        with pytest.raises(ConfigError, match="save_app"):
            await platform.finish_login(
                login(app=None), {"code": "c"}, {"code_verifier": "s"}
            )

    async def test_a_reply_with_no_token_in_it_says_so_plainly(
        self,
        platform: XPlatform,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/oauth2/token").mock(
                return_value=httpx.Response(200, json={"token_type": "bearer"})
            )

            with pytest.raises(PlatformError, match="access_token"):
                await platform.finish_login(
                    login(), {"code": "c"}, {"code_verifier": "s"}
                )


class TestRenewingAToken:
    async def test_it_saves_both_halves_because_x_replaces_both(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            renew = network.post(f"{API}/oauth2/token").mock(
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
        assert sent["client_id"] == ["client-id"]

        assert token.access_token == "access-two"
        assert token.refresh_token == "refresh-two"
        assert token.expires_at is not None

    async def test_a_renewal_that_says_nothing_new_keeps_the_one_we_had(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            reply = a_token(access_token="access-two")
            del reply["refresh_token"]
            network.post(f"{API}/oauth2/token").mock(
                return_value=httpx.Response(200, json=reply)
            )

            token = await platform.refresh(account, APP)

        assert token.refresh_token == "refresh-one"

    async def test_renewing_without_credentials_says_where_to_get_them(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(ConfigError, match="save_app"):
            await platform.refresh(account, None)

    async def test_no_refresh_token_names_the_scope_that_was_missing(
        self,
        platform: XPlatform,
    ) -> None:
        alone = Connection(
            id="x:1",
            platform="x",
            host=None,
            account_id="1",
            account_name="@nobody",
            token=Token(access_token="access-one"),
        )

        with pytest.raises(TokenExpiredError) as refused:
            await platform.refresh(alone, APP)

        assert "offline.access" in str(refused.value)

    async def test_a_refusal_means_the_person_has_to_sign_in_again(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/oauth2/token").mock(
                return_value=httpx.Response(
                    400, json={"error": "invalid_request", "title": "Bad"}
                )
            )

            with pytest.raises(TokenExpiredError, match="connect their account again"):
                await platform.refresh(account, APP)

    async def test_a_token_x_will_not_take_means_signing_in_again(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/oauth2/token").mock(
                return_value=httpx.Response(401, json={"title": "Unauthorized"})
            )

            with pytest.raises(TokenExpiredError, match="connect their account again"):
                await platform.refresh(account, APP)

    async def test_x_having_trouble_of_its_own_is_not_a_dead_token(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/oauth2/token").mock(
                return_value=httpx.Response(503, json={"title": "Down"})
            )

            with pytest.raises(PlatformError) as trouble:
                await platform.refresh(account, APP)

        assert not isinstance(trouble.value, TokenExpiredError)


class TestWhatXAllows:
    async def test_the_ordinary_limit_is_two_hundred_and_eighty(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        limits = await platform.limits(account)

        assert limits.max_text_length == 280
        assert limits.max_images == 4
        assert limits.max_videos == 1

    async def test_it_counts_the_way_x_counts_rather_than_the_way_python_does(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        limits = await platform.limits(account)

        assert limits.text_counted_in is TextCount.UTF16_UNITS

    async def test_a_post_of_seventy_big_letters_is_allowed(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        # 70 x 4 units is exactly 280. Python's own len says 140, so a
        # platform counting characters would let 140 of them through and X
        # would refuse the post.
        with respx.mock() as network:
            network.post(f"{API}/tweets").mock(
                return_value=httpx.Response(201, json=A_TWEET)
            )

            await platform.publish(account, Post(text=BIG_LETTER * 70))

    async def test_a_post_of_seventy_one_never_leaves_the_building(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(assert_all_called=False) as network:
            route = network.post(f"{API}/tweets").mock(
                return_value=httpx.Response(201, json=A_TWEET)
            )

            with pytest.raises(InvalidPostError, match="284"):
                await platform.publish(account, Post(text=BIG_LETTER * 71))

        assert not route.called


class TestPublishing:
    async def test_it_sends_the_words_and_hands_back_a_link(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            route = network.post(f"{API}/tweets").mock(
                return_value=httpx.Response(201, json=A_TWEET)
            )

            result = await platform.publish(account, Post(text="Hello"))

        assert route.calls.last.request.read() == b'{"text":"Hello"}'
        assert result.id == "1800000000000000001"
        assert result.url == f"https://x.com/{HANDLE}/status/1800000000000000001"
        assert result.state is PostState.DONE
        assert result.raw == A_TWEET["data"]

    async def test_a_reply_names_the_post_it_answers(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            route = network.post(f"{API}/tweets").mock(
                return_value=httpx.Response(201, json=A_TWEET)
            )

            await platform.publish(account, Post(text="Yes", reply_to="42"))

        sent = route.calls.last.request.read().decode()
        assert '"reply":{"in_reply_to_tweet_id":"42"}' in sent

    async def test_an_account_with_no_name_still_gets_a_working_link(
        self,
        platform: XPlatform,
    ) -> None:
        nameless = Connection(
            id="x:1",
            platform="x",
            host=None,
            account_id="1",
            account_name="",
            token=Token(access_token="access-one"),
        )

        with respx.mock() as network:
            network.post(f"{API}/tweets").mock(
                return_value=httpx.Response(201, json=A_TWEET)
            )

            result = await platform.publish(nameless, Post(text="Hello"))

        # X redirects /i/status/<id> to the right handle, so a link built
        # this way works even when we never learned the handle.
        assert result.url == "https://x.com/i/status/1800000000000000001"

    @pytest.mark.parametrize(
        ("options", "wanted"),
        [
            ({"reply_settings": "following"}, '"reply_settings":"following"'),
            ({"quote_tweet_id": "77"}, '"quote_tweet_id":"77"'),
        ],
    )
    async def test_the_settings_it_accepts_reach_x(
        self,
        platform: XPlatform,
        account: Connection,
        options: dict[str, Any],
        wanted: str,
    ) -> None:
        with respx.mock(assert_all_called=False) as network:
            route = network.post(f"{API}/tweets").mock(
                return_value=httpx.Response(201, json=A_TWEET)
            )

            await platform.publish(account, Post(text="Hello", options=options))

        assert wanted in route.calls.last.request.read().decode()

    @pytest.mark.parametrize(
        ("options", "wanted"),
        [
            ({"visibility": "public"}, "does not know the post option"),
            ({"reply_settings": "nobody"}, "reply_settings is 'nobody'"),
            ({"quote_tweet_id": 77}, "quote_tweet_id is 77"),
        ],
    )
    async def test_a_setting_x_does_not_know_costs_no_request(
        self,
        platform: XPlatform,
        account: Connection,
        options: dict[str, Any],
        wanted: str,
    ) -> None:
        with respx.mock(assert_all_called=False) as network:
            route = network.post(f"{API}/tweets").mock(
                return_value=httpx.Response(201, json=A_TWEET)
            )

            with pytest.raises(InvalidPostError, match=wanted):
                await platform.publish(account, Post(text="Hi", options=options))

        assert not route.called

    async def test_scheduling_is_refused_rather_than_posted_now(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        later = Post(text="later", publish_at=datetime.now(UTC) + timedelta(hours=1))

        with respx.mock(assert_all_called=False) as network:
            route = network.post(f"{API}/tweets").mock(
                return_value=httpx.Response(201, json=A_TWEET)
            )

            with pytest.raises(NotSupportedError, match="scheduling posts"):
                await platform.publish(account, later)

        assert not route.called

    async def test_a_reply_with_no_id_in_it_says_so_plainly(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/tweets").mock(
                return_value=httpx.Response(201, json={"data": {}})
            )

            with pytest.raises(PlatformError, match="'id'"):
                await platform.publish(account, Post(text="Hello"))


class TestSendingFiles:
    async def test_a_picture_goes_up_in_three_steps_and_lands_on_the_post(
        self,
        platform: XPlatform,
        account: Connection,
        waits: list[float],
    ) -> None:
        with respx.mock() as network:
            upload = network.post(f"{API}/media/upload").mock(
                side_effect=[
                    httpx.Response(202, json={"data": {"id": "media-1"}}),
                    httpx.Response(204),
                    httpx.Response(201, json={"data": {"id": "media-1"}}),
                ]
            )
            tweet = network.post(f"{API}/tweets").mock(
                return_value=httpx.Response(201, json=A_TWEET)
            )

            await platform.publish(
                account,
                Post(
                    text="Look",
                    media=(Media.from_bytes(b"a picture", filename="a.png"),),
                ),
            )

        commands = [
            parse_qs(call.request.content.decode()).get("command", ["?"])[0]
            if call.request.headers["content-type"].startswith("application/x-www")
            else "APPEND"
            for call in upload.calls
        ]
        assert commands == ["INIT", "APPEND", "FINALIZE"]

        started = parse_qs(upload.calls[0].request.content.decode())
        assert started["total_bytes"] == ["9"]
        assert started["media_type"] == ["image/png"]
        assert started["media_category"] == ["tweet_image"]

        sent = tweet.calls.last.request.read().decode()
        assert '"media":{"media_ids":["media-1"]}' in sent
        # A picture is ready the moment it is finalised, so nothing waits.
        assert waits == []

    async def test_a_big_video_is_sent_a_piece_at_a_time_off_disk(
        self,
        account: Connection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        waits: list[float],
    ) -> None:
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"0123456789")

        def never(self: Media) -> bytes:
            message = "The whole file must never be read into memory."
            raise AssertionError(message)

        # A four gigabyte video read whole is four gigabytes of memory, so
        # this proves the pieces come off disk one at a time.
        monkeypatch.setattr(Media, "read", never)

        platform = XPlatform(retries=ONCE, chunk_bytes=4, media_wait_seconds=0.0)

        with respx.mock() as network:
            upload = network.post(f"{API}/media/upload").mock(
                side_effect=[
                    httpx.Response(202, json={"data": {"id": "media-9"}}),
                    httpx.Response(204),
                    httpx.Response(204),
                    httpx.Response(204),
                    httpx.Response(
                        201,
                        json={
                            "data": {
                                "id": "media-9",
                                "processing_info": {
                                    "state": "in_progress",
                                    "check_after_secs": 5,
                                },
                            }
                        },
                    ),
                ]
            )
            status = network.get(f"{API}/media/upload").mock(
                return_value=httpx.Response(
                    200,
                    json={"data": {"processing_info": {"state": "succeeded"}}},
                )
            )
            network.post(f"{API}/tweets").mock(
                return_value=httpx.Response(201, json=A_TWEET)
            )

            await platform.publish(
                account,
                Post(text="Watch", media=(Media.from_file(clip),)),
            )

        pieces = [call.request for call in upload.calls[1:4]]
        assert [
            part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
            for part in (
                request.read().split(b'name="media"', 1)[1] for request in pieces
            )
        ] == [b"0123", b"4567", b"89"]
        assert [
            request.read().split(b'name="segment_index"\r\n\r\n', 1)[1][:1]
            for request in pieces
        ] == [b"0", b"1", b"2"]

        assert parse_qs(status.calls.last.request.url.query.decode()) == {
            "command": ["STATUS"],
            "media_id": ["media-9"],
        }
        # X said to come back in five seconds, so we waited five seconds.
        assert waits == [5.0]

    async def test_a_video_x_gives_up_on_is_a_problem_with_the_file(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/media/upload").mock(
                side_effect=[
                    httpx.Response(202, json={"data": {"id": "media-9"}}),
                    httpx.Response(204),
                    httpx.Response(
                        201,
                        json={
                            "data": {
                                "id": "media-9",
                                "processing_info": {"state": "pending"},
                            }
                        },
                    ),
                ]
            )
            network.get(f"{API}/media/upload").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": {
                            "processing_info": {
                                "state": "failed",
                                "error": {
                                    "code": 3,
                                    "name": "InvalidMedia",
                                    "message": "Unsupported video format",
                                },
                            }
                        }
                    },
                )
            )

            with pytest.raises(InvalidPostError, match="Unsupported video format"):
                await platform.publish(
                    account,
                    Post(
                        text="Watch",
                        media=(Media.from_bytes(b"a clip", filename="a.mp4"),),
                    ),
                )

    async def test_a_video_that_never_finishes_says_what_to_turn_up(
        self,
        account: Connection,
        waits: list[float],
    ) -> None:
        platform = XPlatform(retries=ONCE, media_checks=2, media_wait_seconds=0.0)

        with respx.mock() as network:
            network.post(f"{API}/media/upload").mock(
                side_effect=[
                    httpx.Response(202, json={"data": {"id": "media-9"}}),
                    httpx.Response(204),
                    httpx.Response(
                        201,
                        json={
                            "data": {
                                "id": "media-9",
                                "processing_info": {"state": "in_progress"},
                            }
                        },
                    ),
                ]
            )
            network.get(f"{API}/media/upload").mock(
                return_value=httpx.Response(
                    200,
                    json={"data": {"processing_info": {"state": "in_progress"}}},
                )
            )

            with pytest.raises(PlatformError, match="media_checks"):
                await platform.publish(
                    account,
                    Post(
                        text="Watch",
                        media=(Media.from_bytes(b"a clip", filename="a.mp4"),),
                    ),
                )

        assert len(waits) == 2

    async def test_a_gif_is_told_apart_from_an_ordinary_picture(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            upload = network.post(f"{API}/media/upload").mock(
                side_effect=[
                    httpx.Response(202, json={"data": {"id": "m"}}),
                    httpx.Response(204),
                    httpx.Response(201, json={"data": {"id": "m"}}),
                ]
            )
            network.post(f"{API}/tweets").mock(
                return_value=httpx.Response(201, json=A_TWEET)
            )

            await platform.publish(
                account,
                Post(media=(Media.from_bytes(b"gif bytes", filename="a.gif"),)),
            )

        started = parse_qs(upload.calls[0].request.content.decode())
        assert started["media_category"] == ["tweet_gif"]

    async def test_a_file_that_is_only_a_link_says_what_to_do_instead(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(assert_all_called=False) as network:
            route = network.post(f"{API}/media/upload").mock(
                return_value=httpx.Response(202, json={"data": {"id": "m"}})
            )

            with pytest.raises(InvalidPostError, match=r"Media\.from_file"):
                await platform.publish(
                    account,
                    Post(
                        text="Look",
                        media=(Media.from_url("https://pics.example/a.png"),),
                    ),
                )

        assert not route.called

    async def test_it_reads_an_id_written_the_older_way_too(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        # X moved this endpoint out of v1.1 and the two spell the id
        # differently. Reading both means one code path rather than two.
        with respx.mock() as network:
            network.post(f"{API}/media/upload").mock(
                side_effect=[
                    httpx.Response(202, json={"media_id_string": "old-style"}),
                    httpx.Response(204),
                    httpx.Response(201, json={"media_id_string": "old-style"}),
                ]
            )
            tweet = network.post(f"{API}/tweets").mock(
                return_value=httpx.Response(201, json=A_TWEET)
            )

            await platform.publish(
                account,
                Post(media=(Media.from_bytes(b"a picture", filename="a.png"),)),
            )

        assert '"media_ids":["old-style"]' in tweet.calls.last.request.read().decode()

    async def test_an_upload_with_no_id_in_it_says_so_plainly(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/media/upload").mock(
                return_value=httpx.Response(202, json={"data": {}})
            )

            with pytest.raises(PlatformError, match="no id for the file"):
                await platform.publish(
                    account,
                    Post(media=(Media.from_bytes(b"a picture", filename="a.png"),)),
                )


class TestPostingAThread:
    async def test_each_post_answers_the_one_before_it(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            route = network.post(f"{API}/tweets").mock(
                side_effect=[
                    httpx.Response(201, json={"data": {"id": "1"}}),
                    httpx.Response(201, json={"data": {"id": "2"}}),
                    httpx.Response(201, json={"data": {"id": "3"}}),
                ]
            )

            results = await platform.publish_thread(
                account,
                [Post(text="one"), Post(text="two"), Post(text="three")],
            )

        assert [found.id for found in results] == ["1", "2", "3"]

        bodies = [call.request.read().decode() for call in route.calls]
        assert "reply" not in bodies[0]
        assert '"in_reply_to_tweet_id":"1"' in bodies[1]
        assert '"in_reply_to_tweet_id":"2"' in bodies[2]

    async def test_the_first_post_may_answer_somebody_else(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            route = network.post(f"{API}/tweets").mock(
                side_effect=[
                    httpx.Response(201, json={"data": {"id": "1"}}),
                    httpx.Response(201, json={"data": {"id": "2"}}),
                ]
            )

            await platform.publish_thread(
                account,
                [Post(text="one", reply_to="99"), Post(text="two")],
            )

        assert '"in_reply_to_tweet_id":"99"' in route.calls[0].request.read().decode()

    async def test_it_stops_where_it_broke_and_says_what_did_go_out(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            route = network.post(f"{API}/tweets").mock(
                side_effect=[
                    httpx.Response(201, json={"data": {"id": "1"}}),
                    httpx.Response(201, json={"data": {"id": "2"}}),
                    httpx.Response(403, json={"detail": "duplicate content"}),
                ]
            )

            with pytest.raises(PartialThreadError) as broke:
                await platform.publish_thread(
                    account,
                    [
                        Post(text="one"),
                        Post(text="two"),
                        Post(text="three"),
                        Post(text="four"),
                        Post(text="five"),
                    ],
                )

        # Three tried, two live, and posts four and five never went near X:
        # they would have answered a post that does not exist.
        assert route.call_count == 3
        assert [found.id for found in broke.value.published] == ["1", "2"]
        assert broke.value.failed_at == 3
        assert broke.value.posts_left == 2

        said = str(broke.value)
        assert "post 3 of 5" in said
        assert "2 posts are live" in said
        assert "not been deleted" in said
        assert isinstance(broke.value.__cause__, InvalidPostError)

    async def test_a_thread_whose_very_first_post_fails_is_just_a_failed_post(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/tweets").mock(
                return_value=httpx.Response(401, json={"title": "Unauthorized"})
            )

            # Nothing is public, so there is no half-posted thread to
            # describe and no reason to wrap what X said in something else.
            with pytest.raises(AuthError):
                await platform.publish_thread(
                    account, [Post(text="one"), Post(text="two")]
                )

    async def test_the_second_post_naming_its_own_parent_is_a_mistake(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(assert_all_called=False) as network:
            route = network.post(f"{API}/tweets").mock(
                return_value=httpx.Response(201, json={"data": {"id": "1"}})
            )

            with pytest.raises(InvalidPostError, match="post 2 of 2"):
                await platform.publish_thread(
                    account,
                    [Post(text="one"), Post(text="two", reply_to="99")],
                )

        # Checked before anything is sent, so a mistake costs no post.
        assert not route.called

    async def test_a_thread_of_nothing_is_a_mistake(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(InvalidPostError, match="no posts in it"):
            await platform.publish_thread(account, [])

    async def test_every_post_is_checked_before_the_first_one_goes_out(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(assert_all_called=False) as network:
            route = network.post(f"{API}/tweets").mock(
                return_value=httpx.Response(201, json={"data": {"id": "1"}})
            )

            with pytest.raises(InvalidPostError, match="284"):
                await platform.publish_thread(
                    account,
                    [Post(text="one"), Post(text=BIG_LETTER * 71)],
                )

        assert not route.called


class TestRemovingAPost:
    async def test_it_asks_x_to_delete_the_post(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            route = network.delete(f"{API}/tweets/1800000000000000001").mock(
                return_value=httpx.Response(200, json={"data": {"deleted": True}})
            )

            await platform.delete_post(account, "1800000000000000001")

        assert route.called

    async def test_a_post_that_is_not_there_says_so(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.delete(f"{API}/tweets/1").mock(
                return_value=httpx.Response(404, json={"title": "Not Found Error"})
            )

            with pytest.raises(NotFoundError):
                await platform.delete_post(account, "1")


class TestReadingWhatHappened:
    async def test_it_reads_mentions_oldest_first(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        older = dict(A_MENTION, id="1", created_at="2026-08-31T09:00:00.000Z")
        newer = dict(A_MENTION, id="2", created_at="2026-08-31T11:00:00.000Z")

        with respx.mock() as network:
            route = network.get(f"{API}/users/{ACCOUNT_ID}/mentions").mock(
                return_value=httpx.Response(
                    200, json={"data": [newer, older], "meta": {"result_count": 2}}
                )
            )

            updates = await platform.fetch_updates(account, None)

        assert [update.id for update in updates] == ["1", "2"]
        assert updates[0].kind is UpdateKind.MENTION
        assert updates[0].platform == "x"
        assert updates[0].connection_id == account.id
        assert updates[0].created_at == datetime(2026, 8, 31, 9, 0, tzinfo=UTC)

        asked = route.calls.last.request.url.params
        assert asked["max_results"] == "40"
        assert "created_at" in asked["tweet.fields"]
        assert "start_time" not in asked

    async def test_a_marker_becomes_a_start_time_x_will_accept(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        since = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)

        with respx.mock() as network:
            route = network.get(f"{API}/users/{ACCOUNT_ID}/mentions").mock(
                return_value=httpx.Response(200, json={"data": [A_MENTION]})
            )

            updates = await platform.fetch_updates(account, since)

        # X wants a whole number of seconds and a Z on the end; the form
        # Python writes by default is refused.
        assert route.calls.last.request.url.params["start_time"] == (
            "2026-08-31T10:00:00Z"
        )
        # And anything exactly on the marker is dropped, so nothing is
        # handed on twice.
        assert updates == []

    async def test_an_account_with_no_mentions_is_not_a_failure(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.get(f"{API}/users/{ACCOUNT_ID}/mentions").mock(
                return_value=httpx.Response(200, json={"meta": {"result_count": 0}})
            )

            assert await platform.fetch_updates(account, None) == []

    async def test_it_shrugs_off_a_reply_it_cannot_make_sense_of(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.get(f"{API}/users/{ACCOUNT_ID}/mentions").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": [
                            "not a mention",
                            {"id": "7", "created_at": "whenever"},
                            {"id": "8", "created_at": "2026-08-31T10:00:00.000Z"},
                        ]
                    },
                )
            )

            updates = await platform.fetch_updates(account, None)

        assert [update.id for update in updates] == ["8"]


class TestWhenXSaysNo:
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
        platform: XPlatform,
        account: Connection,
        status: int,
        expected: type[Exception],
    ) -> None:
        with respx.mock() as network:
            network.delete(f"{API}/tweets/1").mock(
                return_value=httpx.Response(status, json={"detail": "Nope"})
            )

            with pytest.raises(expected, match="Nope"):
                await platform.delete_post(account, "1")

    async def test_a_slow_down_says_how_long_the_window_has_left(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        resets_at = datetime.now(UTC) + timedelta(seconds=300)

        with respx.mock() as network:
            network.delete(f"{API}/tweets/1").mock(
                return_value=httpx.Response(
                    429,
                    headers={
                        "x-rate-limit-limit": "50",
                        "x-rate-limit-remaining": "0",
                        "x-rate-limit-reset": str(int(resets_at.timestamp())),
                    },
                    json={"title": "Too Many Requests"},
                )
            )

            with pytest.raises(RateLimitError) as slow:
                await platform.delete_post(account, "1")

        # X sends no Retry-After, only the moment its window starts again.
        assert slow.value.retry_after is not None
        assert 290 <= slow.value.retry_after <= 300
        assert "15 minutes" in str(slow.value)

    async def test_a_slow_down_with_no_headers_still_reads_as_one(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.delete(f"{API}/tweets/1").mock(
                return_value=httpx.Response(429, json={})
            )

            with pytest.raises(RateLimitError) as slow:
                await platform.delete_post(account, "1")

        assert slow.value.retry_after is None
        assert "It said" not in str(slow.value)

    async def test_a_retry_after_of_its_own_still_wins(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.delete(f"{API}/tweets/1").mock(
                return_value=httpx.Response(
                    429, headers={"Retry-After": "12"}, json={"title": "Slow down"}
                )
            )

            with pytest.raises(RateLimitError) as slow:
                await platform.delete_post(account, "1")

        assert slow.value.retry_after == 12.0

    async def test_a_post_x_has_already_seen_is_a_post_problem(
        self,
        platform: XPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            network.post(f"{API}/tweets").mock(
                return_value=httpx.Response(
                    403,
                    json={
                        "title": "Forbidden",
                        "detail": (
                            "You are not allowed to create a Tweet with "
                            "duplicate content."
                        ),
                    },
                )
            )

            with pytest.raises(InvalidPostError, match="already posted"):
                await platform.publish(account, Post(text="Hello"))


class TestSettingItUp:
    def test_a_piece_of_no_bytes_is_refused_at_once(self) -> None:
        with pytest.raises(ConfigError, match="at least one byte"):
            XPlatform(chunk_bytes=0)

    async def test_the_pause_between_checks_is_a_real_pause(self) -> None:
        # The rest of these tests watch the pauses instead of taking them,
        # so this is the one that runs the waiting itself.
        await x_module._wait(0.0)


class TestReadingTheAllowance:
    def test_it_reads_the_three_headers_x_actually_sends(self) -> None:
        resets_at = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
        headers = httpx.Headers(
            {
                "x-rate-limit-limit": "300",
                "x-rate-limit-remaining": "12",
                "x-rate-limit-reset": str(int(resets_at.timestamp())),
            }
        )

        left = rate_limit_in(headers)

        assert left is not None
        assert left.limit == 300
        assert left.remaining == 12
        assert left.resets_at == resets_at
        assert not left.is_used_up

    def test_a_reply_that_says_nothing_reads_as_nothing(self) -> None:
        assert rate_limit_in(httpx.Headers({})) is None

    def test_it_shrugs_off_headers_it_cannot_read(self) -> None:
        left = rate_limit_in(
            httpx.Headers({"x-rate-limit-remaining": "0", "x-rate-limit-limit": "lots"})
        )

        assert left is not None
        assert left.limit is None
        assert left.is_used_up


class TestXBehavesLikeTheOthers(PlatformChecks):
    def make_platform(self) -> Platform:
        return XPlatform(transport=self.transport, retries=ONCE)

    def make_connection(self) -> Connection | None:
        return Connection(
            id=f"x:{ACCOUNT_ID}",
            platform="x",
            host=None,
            account_id=ACCOUNT_ID,
            account_name=f"@{HANDLE}",
            token=Token(access_token="access-one", refresh_token="refresh-one"),
        )

    def make_transport(self) -> httpx.AsyncBaseTransport | None:
        return RecordingTransport(
            {
                "POST /2/tweets": A_TWEET,
                f"GET /2/users/{ACCOUNT_ID}/mentions": {"data": [A_MENTION]},
            }
        )
