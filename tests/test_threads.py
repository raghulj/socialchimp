"""Tests for the Threads platform."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any

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
    NotFoundError,
    NotSupportedError,
    PlatformError,
    Post,
    PostState,
    RateLimitError,
    SignatureError,
    Token,
    TokenExpiredError,
    UpdateKind,
)
from socialchimp.features import TextCount
from socialchimp.http import Retries
from socialchimp.platform import (
    CanCheckSignature,
    CanDeletePosts,
    CanResumeLogin,
    Finished,
    LoginRequest,
    Platform,
)
from socialchimp.platforms import _meta
from socialchimp.platforms import threads as threads_module
from socialchimp.platforms._meta import GRAPH_API
from socialchimp.platforms.threads import (
    DEFAULT_SCOPES,
    HOW_LONG_TO_WAIT,
    HOW_OFTEN_TO_CHECK,
    MAX_TEXT_BYTES,
    MOST_IN_A_CAROUSEL,
    SIGN_IN_PAGE,
    THREADS_API,
    THREADS_HOST,
    Allowance,
    ThreadsPlatform,
    threads_errors,
)
from socialchimp.testing import PlatformChecks, RecordingTransport

APP_ID = "1122334455"
APP_SECRET = "threads-app-secret"
USER_ID = "9876543210"
USERNAME = "adascakes"
REDIRECT = "https://app.example/callback"

# The app secret Meta signs a pushed request with. Not a real one.
SIGNING_KEY = "the-app-secret"

CONTAINER = "18001"
OTHER_CONTAINER = "18002"
PARENT_CONTAINER = "18009"
POST_ID = "18100"

PICTURE_URL = "https://files.example/cake.jpg"
OTHER_PICTURE_URL = "https://files.example/icing.jpg"
VIDEO_URL = "https://files.example/baking.mp4"

# One try and no waiting, so the error tests do not spend real seconds asleep.
ONCE = Retries(attempts=1)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
SIXTY_DAYS = timedelta(days=60)

# Threads answers the publishing limit with posts and replies side by side,
# counted separately.
ALLOWANCE: dict[str, Any] = {
    "data": [
        {
            "quota_usage": 4,
            "config": {"quota_total": 250, "quota_duration": 86_400},
            "reply_quota_usage": 30,
            "reply_config": {"quota_total": 1_000, "quota_duration": 86_400},
        }
    ]
}

USED_UP: dict[str, Any] = {
    "data": [
        {
            "quota_usage": 250,
            "config": {"quota_total": 250, "quota_duration": 86_400},
            "reply_quota_usage": 30,
            "reply_config": {"quota_total": 1_000, "quota_duration": 86_400},
        }
    ]
}


def api(path: str) -> str:
    """A path under the versioned half of graph.threads.net."""
    return f"/v1.0{path}"


@pytest.fixture
def platform() -> ThreadsPlatform:
    """A platform that gives up after one try."""
    return ThreadsPlatform(retries=ONCE)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, datetime]:
    """A clock that only moves when the platform waits, so tests are instant."""
    moment = {"now": NOW}

    async def move_on(seconds: float) -> None:
        moment["now"] += timedelta(seconds=seconds)

    monkeypatch.setattr(threads_module, "_now", lambda: moment["now"])
    monkeypatch.setattr(threads_module, "_sleep", move_on)
    # A token's expiry is stamped by the shared Meta code, so that clock has
    # to stop too or every expiry test is off by however long the test took.
    monkeypatch.setattr(_meta, "_now", lambda: moment["now"])
    return moment


def an_app() -> AppCredentials:
    """Your **Threads** app's credentials, as they arrive on a login request."""
    return AppCredentials(
        platform="threads",
        host=None,
        client_id=APP_ID,
        client_secret=APP_SECRET,
    )


def a_request(*, state: str | None = "abc123") -> LoginRequest:
    """A login request carrying app credentials."""
    return LoginRequest(redirect_uri=REDIRECT, state=state, app=an_app())


def an_account(*, expires_at: datetime | None = None) -> Connection:
    """A connected Threads account."""
    return Connection(
        id=f"threads:{USER_ID}",
        platform="threads",
        host=None,
        account_id=USER_ID,
        account_name=USERNAME,
        token=Token(access_token="long-lived", expires_at=expires_at),
        scopes=DEFAULT_SCOPES,
        extra={"threads_id": USER_ID, "username": USERNAME},
    )


@pytest.fixture
def account() -> Connection:
    """A connected Threads account, for the tests that publish."""
    return an_account()


def signed(body: bytes, *, secret: str = SIGNING_KEY) -> dict[str, str]:
    """The header Meta puts on a request it pushes to us."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}"}


def pushed(field: str, value: dict[str, Any], *, topic: str = "moderate") -> bytes:
    """One message from Threads, in the shape Threads actually sends."""
    return json.dumps(
        {
            "app_id": APP_ID,
            "topic": topic,
            "target_id": USER_ID,
            "time": 1_790_000_000,
            "subscription_id": "234567",
            "has_uid_field": False,
            "values": {"value": value, "field": field},
        }
    ).encode()


# ---------------------------------------------------------------------------
# Where Threads lives, which is not where the rest of Meta lives
# ---------------------------------------------------------------------------


class TestThreadsIsItsOwnPlaceEntirely:
    def test_its_api_is_not_the_facebook_graph(self) -> None:
        assert THREADS_HOST == "https://graph.threads.net"
        assert THREADS_API == "https://graph.threads.net/v1.0"
        assert not THREADS_API.startswith(GRAPH_API)

    def test_the_api_address_has_no_trailing_slash(self) -> None:
        assert not THREADS_API.endswith("/")

    def test_signing_in_happens_on_threads_net(self, account: Connection) -> None:
        # Not Facebook Login. Somebody who points a Threads app at
        # facebook.com/dialog/oauth gets a page that will not sign them in.
        assert SIGN_IN_PAGE == "https://threads.net/oauth/authorize"

    def test_api_base_is_where_requests_go(
        self,
        platform: ThreadsPlatform,
        account: Connection,
    ) -> None:
        assert platform.api_base(account) == THREADS_API

    def test_it_proves_who_we_are_with_a_header_not_a_web_address(
        self,
        platform: ThreadsPlatform,
        account: Connection,
    ) -> None:
        assert platform.auth_headers(account) == {"Authorization": "Bearer long-lived"}


class TestTheSecondAppIdNobodyExpects:
    async def test_asking_us_to_register_an_app_warns_about_both_pairs(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        with pytest.raises(NotSupportedError) as refused:
            await platform.create_app(name="My App", redirect_uri=REDIRECT)

        said = str(refused.value)
        assert "Threads app id" in said
        assert "Threads use case" in said

    async def test_signing_in_without_credentials_names_the_threads_pair(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        with pytest.raises(ConfigError) as refused:
            await platform.start_login(LoginRequest(redirect_uri=REDIRECT))

        said = str(refused.value)
        assert "Threads app id" in said
        assert "Facebook" in said

    async def test_a_refused_token_swap_says_to_check_the_app_id(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        # This is where reusing a Facebook app id actually lands: the sign-in
        # page takes it and the swap does not, with a message about nothing.
        with respx.mock(base_url=THREADS_HOST) as network:
            network.post("/oauth/access_token").mock(
                return_value=httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": "Invalid platform app",
                            "type": "OAuthException",
                            "code": 100,
                        }
                    },
                )
            )

            with pytest.raises(AuthError) as refused:
                await platform.finish_login(a_request(), {"code": "the-code"})

        said = str(refused.value)
        assert "Threads app id" in said
        assert "Invalid platform app" in said

    async def test_a_refused_trade_for_a_long_token_says_the_same(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        # The second half of the swap is signed with the app secret too, so
        # the wrong pair can be refused here rather than a moment earlier.
        with respx.mock(base_url=THREADS_HOST) as network:
            network.post("/oauth/access_token").mock(
                return_value=httpx.Response(200, json={"access_token": "short-lived"})
            )
            network.get("/access_token").mock(
                return_value=httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": "Invalid client secret",
                            "type": "OAuthException",
                            "code": 190,
                        }
                    },
                )
            )

            with pytest.raises(AuthError) as refused:
                await platform.finish_login(a_request(), {"code": "the-code"})

        assert "Threads app secret" in str(refused.value)

    async def test_being_asked_to_slow_down_during_the_trade_is_left_alone(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        with respx.mock(base_url=THREADS_HOST) as network:
            network.post("/oauth/access_token").mock(
                return_value=httpx.Response(200, json={"access_token": "short-lived"})
            )
            network.get("/access_token").mock(
                return_value=httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": "Slow down",
                            "type": "OAuthException",
                            "code": 4,
                        }
                    },
                )
            )

            with pytest.raises(RateLimitError):
                await platform.finish_login(a_request(), {"code": "the-code"})

    async def test_being_asked_to_slow_down_is_not_blamed_on_the_app_id(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        with respx.mock(base_url=THREADS_HOST) as network:
            network.post("/oauth/access_token").mock(
                return_value=httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": "Slow down",
                            "type": "OAuthException",
                            "code": 4,
                        }
                    },
                )
            )

            with pytest.raises(RateLimitError):
                await platform.finish_login(a_request(), {"code": "the-code"})


# ---------------------------------------------------------------------------
# What Threads can and cannot do
# ---------------------------------------------------------------------------


class TestWhatThreadsSaysItCanDo:
    def test_it_can_post_words_on_their_own_unlike_instagram(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        assert Feature.POST_TEXT in platform.features

    @pytest.mark.parametrize(
        "feature",
        [
            Feature.POST_IMAGE,
            Feature.POST_VIDEO,
            Feature.DELETE_POST,
            Feature.PUSH_UPDATES,
        ],
    )
    def test_the_rest_of_what_it_claims(
        self,
        platform: ThreadsPlatform,
        feature: Feature,
    ) -> None:
        assert feature in platform.features

    @pytest.mark.parametrize(
        "feature",
        [Feature.SCHEDULE, Feature.CREATE_APP, Feature.REPLY, Feature.READ_POSTS],
    )
    def test_what_it_does_not_claim(
        self,
        platform: ThreadsPlatform,
        feature: Feature,
    ) -> None:
        assert feature not in platform.features

    def test_it_is_a_platform_with_the_extras_it_claims(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        assert isinstance(platform, Platform)
        assert isinstance(platform, CanCheckSignature)
        assert isinstance(platform, CanDeletePosts)

    def test_a_sign_in_never_pauses_to_ask_which_account(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        # Facebook and Instagram both ask, because a person has many pages.
        # A Threads sign-in is one profile, so there is nothing to choose.
        assert not isinstance(platform, CanResumeLogin)


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


class TestStartingASignIn:
    async def test_it_sends_people_to_threads_own_page(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        step = await platform.start_login(a_request())

        assert step.url.startswith(f"{SIGN_IN_PAGE}?")
        query = httpx.URL(step.url).params
        assert query["client_id"] == APP_ID
        assert query["redirect_uri"] == REDIRECT
        assert query["response_type"] == "code"
        assert query["state"] == "abc123"
        assert step.state == "abc123"

    async def test_it_asks_for_the_threads_permissions(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        step = await platform.start_login(a_request())

        asked = httpx.URL(step.url).params["scope"].split(",")

        assert "threads_basic" in asked
        assert "threads_content_publish" in asked
        assert "threads_delete" in asked
        assert asked == list(DEFAULT_SCOPES)

    async def test_it_makes_a_state_when_you_did_not(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        step = await platform.start_login(a_request(state=None))

        assert step.state
        assert httpx.URL(step.url).params["state"] == step.state

    async def test_nothing_is_sent_to_threads_yet(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        with respx.mock(assert_all_called=False) as network:
            await platform.start_login(a_request())

        assert not network.calls

    async def test_you_can_ask_for_your_own_permissions(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        step = await platform.start_login(
            LoginRequest(redirect_uri=REDIRECT, scopes=("threads_basic",), app=an_app())
        )

        assert httpx.URL(step.url).params["scope"] == "threads_basic"


class TestFinishingASignIn:
    async def test_it_swaps_the_code_makes_it_last_and_reads_the_profile(
        self,
        platform: ThreadsPlatform,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=THREADS_HOST) as network:
            swap = network.post("/oauth/access_token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "short-lived", "user_id": 9876543210}
                )
            )
            make_it_last = network.get("/access_token").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "access_token": "long-lived",
                        "token_type": "bearer",
                        "expires_in": 5_184_000,
                    },
                )
            )
            profile = network.get(api("/me")).mock(
                return_value=httpx.Response(
                    200, json={"id": USER_ID, "username": USERNAME}
                )
            )

            step = await platform.finish_login(a_request(), {"code": "the-code"})

        sent = dict(httpx.QueryParams(swap.calls.last.request.content.decode()))
        assert sent["client_id"] == APP_ID
        assert sent["client_secret"] == APP_SECRET
        assert sent["grant_type"] == "authorization_code"
        assert sent["redirect_uri"] == REDIRECT
        assert sent["code"] == "the-code"

        traded = make_it_last.calls.last.request.url.params
        assert traded["grant_type"] == "th_exchange_token"
        assert traded["client_secret"] == APP_SECRET
        assert traded["access_token"] == "short-lived"

        assert profile.calls.last.request.headers["Authorization"] == (
            "Bearer long-lived"
        )

        assert isinstance(step, Finished)
        assert step.connection.id == f"threads:{USER_ID}"
        assert step.connection.account_id == USER_ID
        assert step.connection.account_name == USERNAME
        assert step.connection.token.access_token == "long-lived"
        assert step.connection.token.expires_at == NOW + timedelta(seconds=5_184_000)
        assert step.connection.extra["threads_id"] == USER_ID
        assert step.connection.extra["username"] == USERNAME
        assert USERNAME in str(step.connection.extra["profile_url"])

    async def test_the_hash_threads_glues_onto_the_code_is_taken_off(
        self,
        platform: ThreadsPlatform,
        clock: dict[str, datetime],
    ) -> None:
        # Threads puts "#_" on the end of the code it hands back, and sending
        # that on gets the swap refused for no visible reason.
        with respx.mock(base_url=THREADS_HOST) as network:
            swap = network.post("/oauth/access_token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "short-lived", "user_id": USER_ID}
                )
            )
            network.get("/access_token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "long-lived", "expires_in": 5_184_000}
                )
            )
            network.get(api("/me")).mock(
                return_value=httpx.Response(
                    200, json={"id": USER_ID, "username": USERNAME}
                )
            )

            await platform.finish_login(a_request(), {"code": "the-code#_"})

        sent = dict(httpx.QueryParams(swap.calls.last.request.content.decode()))
        assert sent["code"] == "the-code"

    async def test_a_state_that_did_not_come_from_here_is_refused(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        with (
            respx.mock(assert_all_called=False) as network,
            pytest.raises(AuthError, match="did not start here"),
        ):
            await platform.finish_login(
                a_request(), {"code": "the-code", "state": "somebody-elses"}
            )

        assert not network.calls

    async def test_somebody_who_pressed_cancel_is_not_a_mystery(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        with pytest.raises(AuthError, match="pressed cancel"):
            await platform.finish_login(
                a_request(), {"error": "access_denied", "state": "abc123"}
            )

    async def test_a_callback_with_no_code_says_what_to_pass(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        with pytest.raises(AuthError, match="whole query string"):
            await platform.finish_login(a_request(), {"state": "abc123"})

    async def test_finishing_without_app_credentials_says_so(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        with pytest.raises(ConfigError):
            await platform.finish_login(
                LoginRequest(redirect_uri=REDIRECT), {"code": "the-code"}
            )

    async def test_a_profile_with_no_id_on_it_says_so_plainly(
        self,
        platform: ThreadsPlatform,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=THREADS_HOST) as network:
            network.post("/oauth/access_token").mock(
                return_value=httpx.Response(200, json={"access_token": "short-lived"})
            )
            network.get("/access_token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "long-lived", "expires_in": 5_184_000}
                )
            )
            network.get(api("/me")).mock(
                return_value=httpx.Response(200, json={"username": USERNAME})
            )

            with pytest.raises(PlatformError, match="id"):
                await platform.finish_login(a_request(), {"code": "the-code"})

    async def test_a_profile_with_no_username_is_shown_by_its_id(
        self,
        platform: ThreadsPlatform,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=THREADS_HOST) as network:
            network.post("/oauth/access_token").mock(
                return_value=httpx.Response(200, json={"access_token": "short-lived"})
            )
            network.get("/access_token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "long-lived", "expires_in": 5_184_000}
                )
            )
            network.get(api("/me")).mock(
                return_value=httpx.Response(200, json={"id": USER_ID})
            )

            step = await platform.finish_login(a_request(), {"code": "the-code"})

        assert isinstance(step, Finished)
        assert step.connection.account_name == USER_ID


# ---------------------------------------------------------------------------
# The one Meta network with a real refresh
# ---------------------------------------------------------------------------


class TestRenewingAToken:
    async def test_it_asks_threads_for_a_fresh_sixty_days(
        self,
        platform: ThreadsPlatform,
        clock: dict[str, datetime],
    ) -> None:
        # Two days old, so twenty-two hours past the point Threads will renew.
        running = an_account(expires_at=NOW + SIXTY_DAYS - timedelta(days=2))

        with respx.mock(base_url=THREADS_HOST) as network:
            renew = network.get("/refresh_access_token").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "access_token": "renewed",
                        "token_type": "bearer",
                        "expires_in": 5_184_000,
                    },
                )
            )

            token = await platform.refresh(running)

        query = renew.calls.last.request.url.params
        assert query["grant_type"] == "th_refresh_token"
        assert query["access_token"] == "long-lived"
        assert token.access_token == "renewed"
        assert token.expires_at == NOW + timedelta(seconds=5_184_000)

    async def test_renewing_needs_nothing_but_the_token(
        self,
        platform: ThreadsPlatform,
        clock: dict[str, datetime],
    ) -> None:
        # The rest of Meta signs a renewal with the app's id and secret and
        # refuses without them. Threads does not, so none are sent.
        running = an_account(expires_at=NOW + SIXTY_DAYS - timedelta(days=2))

        with respx.mock(base_url=THREADS_HOST) as network:
            renew = network.get("/refresh_access_token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "renewed", "expires_in": 5_184_000}
                )
            )

            await platform.refresh(running)

        query = renew.calls.last.request.url.params
        assert "client_secret" not in query
        assert "client_id" not in query

    async def test_a_token_under_a_day_old_is_refused_before_anything_is_sent(
        self,
        platform: ThreadsPlatform,
        clock: dict[str, datetime],
    ) -> None:
        # Threads will not renew one until it is 24 hours old, so asking now
        # would be refused by Threads with a much less helpful message.
        just_made = an_account(expires_at=NOW + SIXTY_DAYS)

        with respx.mock(assert_all_called=False) as network:
            with pytest.raises(RateLimitError) as refused:
                await platform.refresh(just_made)

            assert not network.calls

        assert refused.value.retry_after == 24 * 60 * 60
        assert "24 hours" in str(refused.value)

    async def test_the_refusal_says_the_token_is_fine_as_it_is(
        self,
        platform: ThreadsPlatform,
        clock: dict[str, datetime],
    ) -> None:
        just_made = an_account(expires_at=NOW + SIXTY_DAYS)

        with pytest.raises(RateLimitError, match="nothing wrong with it"):
            await platform.refresh(just_made)

    async def test_a_token_with_no_expiry_is_taken_to_threads_to_judge(
        self,
        platform: ThreadsPlatform,
        clock: dict[str, datetime],
    ) -> None:
        # Nothing here says how old it is, so guessing would be worse than
        # letting Threads answer.
        with respx.mock(base_url=THREADS_HOST) as network:
            network.get("/refresh_access_token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "renewed", "expires_in": 5_184_000}
                )
            )

            token = await platform.refresh(an_account())

        assert token.access_token == "renewed"

    async def test_a_token_threads_will_not_renew_means_signing_in_again(
        self,
        platform: ThreadsPlatform,
        clock: dict[str, datetime],
    ) -> None:
        running = an_account(expires_at=NOW + SIXTY_DAYS - timedelta(days=2))

        with respx.mock(base_url=THREADS_HOST) as network:
            network.get("/refresh_access_token").mock(
                return_value=httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": "Invalid OAuth access token",
                            "code": 190,
                        }
                    },
                )
            )

            with pytest.raises(TokenExpiredError) as refused:
                await platform.refresh(running)

        assert "sixty days" in str(refused.value)

    async def test_a_renewal_that_comes_back_empty_says_so(
        self,
        platform: ThreadsPlatform,
        clock: dict[str, datetime],
    ) -> None:
        running = an_account(expires_at=NOW + SIXTY_DAYS - timedelta(days=2))

        with respx.mock(base_url=THREADS_HOST) as network:
            network.get("/refresh_access_token").mock(
                return_value=httpx.Response(200, json={"token_type": "bearer"})
            )

            with pytest.raises(PlatformError, match="access_token"):
                await platform.refresh(running)

    async def test_your_app_credentials_are_accepted_and_ignored(
        self,
        platform: ThreadsPlatform,
        clock: dict[str, datetime],
    ) -> None:
        running = an_account(expires_at=NOW + SIXTY_DAYS - timedelta(days=2))

        with respx.mock(base_url=THREADS_HOST) as network:
            network.get("/refresh_access_token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "renewed", "expires_in": 5_184_000}
                )
            )

            token = await platform.refresh(running, an_app())

        assert token.access_token == "renewed"


# ---------------------------------------------------------------------------
# How much is left today
# ---------------------------------------------------------------------------


class TestWhatThreadsAllows:
    async def test_it_reads_the_posts_and_the_replies_left(
        self,
        platform: ThreadsPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=THREADS_HOST) as network:
            route = network.get(api(f"/{USER_ID}/threads_publishing_limit")).mock(
                return_value=httpx.Response(200, json=ALLOWANCE)
            )

            left = await platform.allowance(account)

        asked = route.calls.last.request.url.params["fields"]
        assert "quota_usage" in asked
        assert "reply_quota_usage" in asked
        assert left == Allowance(posts_left=246, replies_left=970)

    async def test_replies_are_counted_apart_from_posts(
        self,
        platform: ThreadsPlatform,
        account: Connection,
    ) -> None:
        # Two hundred and fifty posts and a thousand replies, and answering a
        # thousand people costs none of the posts.
        with respx.mock(base_url=THREADS_HOST) as network:
            network.get(api(f"/{USER_ID}/threads_publishing_limit")).mock(
                return_value=httpx.Response(200, json=USED_UP)
            )

            left = await platform.allowance(account)

        assert left.posts_left == 0
        assert left.replies_left == 970

    async def test_the_posts_left_land_on_the_limits(
        self,
        platform: ThreadsPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=THREADS_HOST) as network:
            network.get(api(f"/{USER_ID}/threads_publishing_limit")).mock(
                return_value=httpx.Response(200, json=ALLOWANCE)
            )

            limits = await platform.limits(account)

        assert limits.posts_left_today == 246
        assert limits.max_text_length == MAX_TEXT_BYTES
        assert limits.text_counted_in is TextCount.UTF8_BYTES
        assert limits.max_images == MOST_IN_A_CAROUSEL
        assert limits.max_videos == MOST_IN_A_CAROUSEL

    async def test_the_number_is_read_rather_than_written_down(
        self,
        platform: ThreadsPlatform,
        account: Connection,
    ) -> None:
        # Whatever Threads says today is what we use, not the 250 in its docs.
        moved = {
            "data": [
                {"quota_usage": 1, "config": {"quota_total": 30, "quota_duration": 1}}
            ]
        }
        with respx.mock(base_url=THREADS_HOST) as network:
            network.get(api(f"/{USER_ID}/threads_publishing_limit")).mock(
                return_value=httpx.Response(200, json=moved)
            )

            limits = await platform.limits(account)

        assert limits.posts_left_today == 29

    async def test_an_answer_we_cannot_read_is_not_a_guess(
        self,
        platform: ThreadsPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=THREADS_HOST) as network:
            network.get(api(f"/{USER_ID}/threads_publishing_limit")).mock(
                return_value=httpx.Response(200, json={"data": []})
            )

            limits = await platform.limits(account)

        assert limits.posts_left_today is None

    async def test_a_connection_naming_no_account_says_which_key_to_set(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        nowhere = Connection(
            id="threads:broken",
            platform="threads",
            host=None,
            account_id="",
            account_name="",
            token=Token(access_token="long-lived"),
        )

        with pytest.raises(ConfigError) as refused:
            await platform.limits(nowhere)

        assert "threads_id" in str(refused.value)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def a_publishing_network(
    network: respx.MockRouter,
    *,
    containers: list[httpx.Response] | None = None,
) -> dict[str, respx.Route]:
    """Set up the requests a post makes, and hand back the routes to look at."""
    return {
        "limit": network.get(api(f"/{USER_ID}/threads_publishing_limit")).mock(
            return_value=httpx.Response(200, json=ALLOWANCE)
        ),
        "build": network.post(api(f"/{USER_ID}/threads")).mock(
            side_effect=containers
            if containers is not None
            else [httpx.Response(200, json={"id": CONTAINER})]
        ),
        "publish": network.post(api(f"/{USER_ID}/threads_publish")).mock(
            return_value=httpx.Response(200, json={"id": POST_ID})
        ),
    }


class TestPublishingWords:
    async def test_words_on_their_own_go_out_as_a_text_post(
        self,
        platform: ThreadsPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=THREADS_HOST) as network:
            routes = a_publishing_network(network)
            build = routes["build"]
            publish = routes["publish"]

            result = await platform.publish(account, Post(text="Fresh cakes today"))

        made = dict(httpx.QueryParams(build.calls.last.request.content.decode()))
        assert made["media_type"] == "TEXT"
        assert made["text"] == "Fresh cakes today"
        assert "image_url" not in made

        put_out = dict(httpx.QueryParams(publish.calls.last.request.content.decode()))
        assert put_out["creation_id"] == CONTAINER

        assert result.id == POST_ID
        assert result.state is PostState.DONE
        # Threads' id for a post is not its web address, and the address needs
        # another request to find out.
        assert result.url is None

    async def test_a_text_post_is_not_waited_on(
        self,
        platform: ThreadsPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=THREADS_HOST, assert_all_called=False) as network:
            a_publishing_network(network)
            looking = network.get(api(f"/{CONTAINER}")).mock(
                return_value=httpx.Response(200, json={"status": "FINISHED"})
            )

            await platform.publish(account, Post(text="Fresh cakes today"))

        assert not looking.called

    async def test_five_hundred_bytes_of_plain_letters_is_allowed(
        self,
        platform: ThreadsPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=THREADS_HOST) as network:
            a_publishing_network(network)

            result = await platform.publish(account, Post(text="x" * MAX_TEXT_BYTES))

        assert result.id == POST_ID

    async def test_one_letter_more_never_leaves(
        self,
        platform: ThreadsPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(assert_all_called=False) as network:
            with pytest.raises(InvalidPostError, match="500"):
                await platform.publish(account, Post(text="x" * (MAX_TEXT_BYTES + 1)))

            assert not network.calls

    async def test_five_hundred_emoji_are_far_too_many_bytes(
        self,
        platform: ThreadsPlatform,
        account: Connection,
    ) -> None:
        # Threads counts bytes, and a cake is four of them. Counting
        # characters here would send a post Threads refuses.
        cakes = "\U0001f370" * 500

        assert len(cakes) == 500
        assert len(cakes.encode()) == 2_000

        with respx.mock(assert_all_called=False) as network:
            with pytest.raises(InvalidPostError) as refused:
                await platform.publish(account, Post(text=cakes))

            assert not network.calls

        said = str(refused.value)
        assert "2000 bytes" in said
        assert "emoji" in said

    async def test_a_hundred_and_twenty_five_emoji_fit_exactly(
        self,
        platform: ThreadsPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        cakes = "\U0001f370" * 125

        assert len(cakes.encode()) == MAX_TEXT_BYTES

        with respx.mock(base_url=THREADS_HOST) as network:
            a_publishing_network(network)

            result = await platform.publish(account, Post(text=cakes))

        assert result.id == POST_ID


class TestPublishingAPicture:
    async def test_threads_is_given_the_address_and_fetches_it_itself(
        self,
        platform: ThreadsPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=THREADS_HOST) as network:
            build = a_publishing_network(network)["build"]

            result = await platform.publish(
                account,
                Post(
                    text="Iced this morning",
                    media=(Media.from_url(PICTURE_URL, alt_text="A pink cake"),),
                ),
            )

        made = dict(httpx.QueryParams(build.calls.last.request.content.decode()))
        assert made["media_type"] == "IMAGE"
        assert made["image_url"] == PICTURE_URL
        assert made["text"] == "Iced this morning"
        assert made["alt_text"] == "A pink cake"
        assert "is_carousel_item" not in made

        assert result.id == POST_ID

    async def test_a_picture_is_not_waited_on_either(
        self,
        platform: ThreadsPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=THREADS_HOST, assert_all_called=False) as network:
            a_publishing_network(network)
            looking = network.get(api(f"/{CONTAINER}")).mock(
                return_value=httpx.Response(200, json={"status": "FINISHED"})
            )

            await platform.publish(
                account, Post(text="Cake", media=(Media.from_url(PICTURE_URL),))
            )

        assert not looking.called

    async def test_a_file_from_disk_is_refused_with_what_to_do_instead(
        self,
        platform: ThreadsPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(assert_all_called=False) as network:
            with pytest.raises(NotSupportedError) as refused:
                await platform.publish(
                    account,
                    Post(
                        text="Cake",
                        media=(Media.from_bytes(b"not a cake", filename="cake.jpg"),),
                    ),
                )

            assert not network.calls

        assert "Media.from_url" in str(refused.value)


class TestPublishingAVideo:
    async def test_it_waits_for_threads_to_finish_making_the_post(
        self,
        platform: ThreadsPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=THREADS_HOST) as network:
            build = a_publishing_network(network)["build"]
            looking = network.get(api(f"/{CONTAINER}")).mock(
                side_effect=[
                    httpx.Response(200, json={"status": "IN_PROGRESS"}),
                    httpx.Response(200, json={"status": "FINISHED"}),
                ]
            )

            result = await platform.publish(
                account, Post(text="Baking", media=(Media.from_url(VIDEO_URL),))
            )

        made = dict(httpx.QueryParams(build.calls.last.request.content.decode()))
        assert made["media_type"] == "VIDEO"
        assert made["video_url"] == VIDEO_URL

        assert looking.call_count == 2
        assert clock["now"] == NOW + timedelta(seconds=HOW_OFTEN_TO_CHECK)
        assert result.id == POST_ID

    async def test_a_post_threads_has_already_published_is_not_waited_on_further(
        self,
        platform: ThreadsPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=THREADS_HOST) as network:
            a_publishing_network(network)
            network.get(api(f"/{CONTAINER}")).mock(
                return_value=httpx.Response(200, json={"status": "PUBLISHED"})
            )

            result = await platform.publish(
                account, Post(text="Baking", media=(Media.from_url(VIDEO_URL),))
            )

        assert result.id == POST_ID

    async def test_a_video_threads_gave_up_on_says_it_is_the_file(
        self,
        platform: ThreadsPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=THREADS_HOST, assert_all_called=False) as network:
            a_publishing_network(network)
            network.get(api(f"/{CONTAINER}")).mock(
                return_value=httpx.Response(
                    200,
                    json={"status": "ERROR", "error_message": "FILE_INVALID"},
                )
            )

            with pytest.raises(InvalidPostError) as refused:
                await platform.publish(
                    account, Post(text="Baking", media=(Media.from_url(VIDEO_URL),))
                )

        said = str(refused.value)
        assert "FILE_INVALID" in said
        assert "nothing has been published" in said

    async def test_a_half_made_post_threads_threw_away_says_to_send_it_again(
        self,
        platform: ThreadsPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=THREADS_HOST, assert_all_called=False) as network:
            a_publishing_network(network)
            network.get(api(f"/{CONTAINER}")).mock(
                return_value=httpx.Response(200, json={"status": "EXPIRED"})
            )

            with pytest.raises(PlatformError, match="24 hours"):
                await platform.publish(
                    account, Post(text="Baking", media=(Media.from_url(VIDEO_URL),))
                )

    async def test_giving_up_watching_is_not_the_same_as_the_post_failing(
        self,
        platform: ThreadsPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=THREADS_HOST, assert_all_called=False) as network:
            a_publishing_network(network)
            network.get(api(f"/{CONTAINER}")).mock(
                return_value=httpx.Response(200, json={"status": "IN_PROGRESS"})
            )

            with pytest.raises(PlatformError) as refused:
                await platform.publish(
                    account, Post(text="Baking", media=(Media.from_url(VIDEO_URL),))
                )

        said = str(refused.value)
        assert "may still appear" in said
        assert CONTAINER in said
        assert clock["now"] >= NOW + timedelta(seconds=HOW_LONG_TO_WAIT)

    async def test_it_really_waits_when_no_test_has_stopped_the_clock(
        self,
        account: Connection,
    ) -> None:
        # Every other test here freezes time. This one does not, so the real
        # clock and the real waiting both run - for no seconds at all.
        quick = ThreadsPlatform(retries=ONCE, check_every_seconds=0.0)

        with respx.mock(base_url=THREADS_HOST) as network:
            a_publishing_network(network)
            network.get(api(f"/{CONTAINER}")).mock(
                side_effect=[
                    httpx.Response(200, json={"status": "IN_PROGRESS"}),
                    httpx.Response(200, json={"status": "FINISHED"}),
                ]
            )

            result = await quick.publish(
                account, Post(text="Baking", media=(Media.from_url(VIDEO_URL),))
            )

        assert result.id == POST_ID

    async def test_how_long_to_wait_is_yours_to_set(
        self,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        patient = ThreadsPlatform(
            retries=ONCE, check_every_seconds=10.0, wait_up_to_seconds=25.0
        )

        with respx.mock(base_url=THREADS_HOST, assert_all_called=False) as network:
            a_publishing_network(network)
            looking = network.get(api(f"/{CONTAINER}")).mock(
                return_value=httpx.Response(200, json={"status": "IN_PROGRESS"})
            )

            with pytest.raises(PlatformError):
                await patient.publish(
                    account, Post(text="Baking", media=(Media.from_url(VIDEO_URL),))
                )

        assert looking.call_count == 4


class TestPublishingACarousel:
    async def test_every_piece_becomes_its_own_container_then_one_parent(
        self,
        platform: ThreadsPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=THREADS_HOST) as network:
            routes = a_publishing_network(
                network,
                containers=[
                    httpx.Response(200, json={"id": CONTAINER}),
                    httpx.Response(200, json={"id": OTHER_CONTAINER}),
                    httpx.Response(200, json={"id": PARENT_CONTAINER}),
                ],
            )
            build = routes["build"]
            publish = routes["publish"]

            result = await platform.publish(
                account,
                Post(
                    text="Two cakes",
                    media=(
                        Media.from_url(PICTURE_URL),
                        Media.from_url(OTHER_PICTURE_URL),
                    ),
                ),
            )

        sent = [
            dict(httpx.QueryParams(call.request.content.decode()))
            for call in build.calls
        ]
        assert sent[0]["is_carousel_item"] == "true"
        assert sent[0]["image_url"] == PICTURE_URL
        # The caption belongs to the carousel, not to its pieces.
        assert "text" not in sent[0]
        assert sent[1]["image_url"] == OTHER_PICTURE_URL

        assert sent[2]["media_type"] == "CAROUSEL"
        assert sent[2]["children"] == f"{CONTAINER},{OTHER_CONTAINER}"
        assert sent[2]["text"] == "Two cakes"

        put_out = dict(httpx.QueryParams(publish.calls.last.request.content.decode()))
        assert put_out["creation_id"] == PARENT_CONTAINER
        assert result.id == POST_ID

    async def test_a_carousel_with_a_video_in_it_is_waited_on(
        self,
        platform: ThreadsPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=THREADS_HOST) as network:
            a_publishing_network(
                network,
                containers=[
                    httpx.Response(200, json={"id": CONTAINER}),
                    httpx.Response(200, json={"id": OTHER_CONTAINER}),
                    httpx.Response(200, json={"id": PARENT_CONTAINER}),
                ],
            )
            piece = network.get(api(f"/{OTHER_CONTAINER}")).mock(
                return_value=httpx.Response(200, json={"status": "FINISHED"})
            )
            parent = network.get(api(f"/{PARENT_CONTAINER}")).mock(
                return_value=httpx.Response(200, json={"status": "FINISHED"})
            )

            await platform.publish(
                account,
                Post(
                    text="A cake and the making of it",
                    media=(Media.from_url(PICTURE_URL), Media.from_url(VIDEO_URL)),
                ),
            )

        assert piece.called
        assert parent.called

    async def test_asking_for_a_carousel_of_one_says_to_leave_it_out(
        self,
        platform: ThreadsPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(assert_all_called=False) as network:
            with pytest.raises(InvalidPostError, match="carousel"):
                await platform.publish(
                    account,
                    Post(
                        text="One cake",
                        media=(Media.from_url(PICTURE_URL),),
                        options={"carousel": True},
                    ),
                )

            assert not network.calls

    async def test_asking_for_a_carousel_of_one_that_has_no_media_at_all(
        self,
        platform: ThreadsPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(InvalidPostError, match="carousel"):
            await platform.publish(
                account, Post(text="Just words", options={"carousel": True})
            )

    async def test_more_than_twenty_pieces_is_refused_before_anything_is_sent(
        self,
        platform: ThreadsPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(assert_all_called=False) as network:
            with pytest.raises(InvalidPostError):
                await platform.publish(
                    account,
                    Post(
                        text="Too many",
                        media=tuple(
                            Media.from_url(PICTURE_URL)
                            for _ in range(MOST_IN_A_CAROUSEL + 1)
                        ),
                    ),
                )

            assert not network.calls


class TestSettingsOnAPost:
    async def test_it_says_which_settings_it_knows(
        self,
        platform: ThreadsPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(InvalidPostError, match="carousel"):
            await platform.publish(
                account, Post(text="Cake", options={"visibility": "public"})
            )

    async def test_the_carousel_setting_is_true_or_false(
        self,
        platform: ThreadsPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(InvalidPostError, match="True or False"):
            await platform.publish(
                account, Post(text="Cake", options={"carousel": "yes"})
            )


class TestWhenThreadsSaysNo:
    async def test_no_posts_left_today_is_refused_with_what_to_do(
        self,
        platform: ThreadsPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=THREADS_HOST, assert_all_called=False) as network:
            limit = network.get(api(f"/{USER_ID}/threads_publishing_limit")).mock(
                return_value=httpx.Response(200, json=USED_UP)
            )
            build = network.post(api(f"/{USER_ID}/threads")).mock(
                return_value=httpx.Response(200, json={"id": CONTAINER})
            )

            with pytest.raises(InvalidPostError, match="tomorrow"):
                await platform.publish(account, Post(text="One more"))

        assert limit.called
        assert not build.called

    async def test_scheduling_is_refused_rather_than_going_out_now(
        self,
        platform: ThreadsPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(NotSupportedError, match="scheduling"):
            await platform.publish(
                account,
                Post(text="Later", publish_at=datetime.now(UTC) + timedelta(hours=2)),
            )

    async def test_replying_is_refused_rather_than_posted_on_its_own(
        self,
        platform: ThreadsPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(NotSupportedError, match="replying"):
            await platform.publish(account, Post(text="Thanks", reply_to=POST_ID))

    async def test_a_container_reply_with_no_id_says_so(
        self,
        platform: ThreadsPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=THREADS_HOST, assert_all_called=False) as network:
            a_publishing_network(
                network, containers=[httpx.Response(200, json={"ok": True})]
            )

            with pytest.raises(PlatformError, match="id"):
                await platform.publish(account, Post(text="Cake"))

    async def test_a_publish_reply_with_no_id_says_so(
        self,
        platform: ThreadsPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=THREADS_HOST) as network:
            network.get(api(f"/{USER_ID}/threads_publishing_limit")).mock(
                return_value=httpx.Response(200, json=ALLOWANCE)
            )
            network.post(api(f"/{USER_ID}/threads")).mock(
                return_value=httpx.Response(200, json={"id": CONTAINER})
            )
            network.post(api(f"/{USER_ID}/threads_publish")).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            with pytest.raises(PlatformError, match="id"):
                await platform.publish(account, Post(text="Cake"))

    def test_it_names_metas_shared_codes_as_its_own(self) -> None:
        refused = threads_errors(
            httpx.Response(400, json={"error": {"message": "Bad token", "code": 190}})
        )

        assert isinstance(refused, AuthError)
        assert refused.platform == "threads"

    async def test_it_knows_nothing_about_the_allowance_until_meta_says(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        assert platform.usage is None

    async def test_it_remembers_how_much_of_the_allowance_is_gone(
        self,
        platform: ThreadsPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=THREADS_HOST) as network:
            network.get(api(f"/{USER_ID}/threads_publishing_limit")).mock(
                return_value=httpx.Response(
                    200,
                    json=ALLOWANCE,
                    headers={"X-App-Usage": json.dumps({"call_count": 63})},
                )
            )

            await platform.limits(account)

        seen = platform.usage
        assert seen is not None
        assert seen.calls == 63


# ---------------------------------------------------------------------------
# Deleting
# ---------------------------------------------------------------------------


class TestDeletingAPost:
    async def test_it_removes_the_post(
        self,
        platform: ThreadsPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=THREADS_HOST) as network:
            gone = network.delete(api(f"/{POST_ID}")).mock(
                return_value=httpx.Response(200, json={"success": True})
            )

            await platform.delete_post(account, POST_ID)

        assert gone.called
        assert gone.calls.last.request.headers["Authorization"] == "Bearer long-lived"

    async def test_a_post_that_is_not_there_says_so(
        self,
        platform: ThreadsPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=THREADS_HOST) as network:
            network.delete(api("/no-such-post")).mock(
                return_value=httpx.Response(404, json={})
            )

            with pytest.raises(NotFoundError):
                await platform.delete_post(account, "no-such-post")


# ---------------------------------------------------------------------------
# Requests Threads pushes to us
# ---------------------------------------------------------------------------


class TestCheckingASignature:
    def test_a_body_signed_with_the_app_secret_is_accepted(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        body = pushed("replies", {"id": "1"})

        platform.check_signature(body, signed(body), secret=SIGNING_KEY)

    def test_a_tampered_body_is_refused(self, platform: ThreadsPlatform) -> None:
        body = pushed("replies", {"id": "1"})
        headers = signed(body)

        with pytest.raises(SignatureError):
            platform.check_signature(
                body.replace(b'"id": "1"', b'"id": "2"'),
                headers,
                secret=SIGNING_KEY,
            )

    def test_a_signature_made_with_the_wrong_secret_is_refused(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        body = pushed("replies", {"id": "1"})

        with pytest.raises(SignatureError):
            platform.check_signature(
                body, signed(body, secret="not-the-agreed-one"), secret=SIGNING_KEY
            )

    def test_a_request_with_no_signature_at_all_is_refused(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        with pytest.raises(SignatureError, match="X-Hub-Signature-256"):
            platform.check_signature(b"{}", {}, secret=SIGNING_KEY)

    def test_it_answers_the_one_off_setup_check(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        answer = platform.answer_setup_check(
            {
                "hub.mode": "subscribe",
                "hub.challenge": "12345",
                "hub.verify_token": "the-token",
            },
            verify_token="the-token",
        )

        assert answer == "12345"

    def test_a_setup_check_with_the_wrong_token_is_refused(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        with pytest.raises(SignatureError):
            platform.answer_setup_check(
                {
                    "hub.mode": "subscribe",
                    "hub.challenge": "12345",
                    "hub.verify_token": "guessed",
                },
                verify_token="the-token",
            )


class TestReadingWhatThreadsPushed:
    def test_a_reply_is_a_comment_to_an_app_that_answers_comments(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        body = pushed(
            "replies",
            {
                "id": "8901234",
                "username": "someone",
                "text": "Looks lovely",
                "timestamp": "2026-08-31T10:33:16+0000",
            },
        )

        update = platform.read_update(body, {})

        assert update.kind is UpdateKind.COMMENT_CREATED
        assert update.platform == "threads"
        assert update.connection_id == f"threads:{USER_ID}"
        assert update.created_at == datetime(2026, 8, 31, 10, 33, 16, tzinfo=UTC)
        assert update.raw["values"]["value"]["text"] == "Looks lovely"

    def test_a_mention_arrives_as_a_mention(self, platform: ThreadsPlatform) -> None:
        body = pushed(
            "mentions",
            {"id": "8901234", "text": "@adascakes look", "media_type": "TEXT_POST"},
            topic="interaction",
        )

        assert platform.read_update(body, {}).kind is UpdateKind.MENTION

    def test_a_post_going_live_arrives_as_published(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        body = pushed(
            "publish",
            {"id": POST_ID, "media_type": "TEXT_POST"},
            topic="interaction",
        )

        assert platform.read_update(body, {}).kind is UpdateKind.POST_PUBLISHED

    def test_something_being_removed_arrives_as_a_deleted_post(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        body = pushed(
            "delete",
            {
                "id": POST_ID,
                "owner": {"owner_id": USER_ID},
                "deleted_at": "2026-08-31T10:33:16+0000",
                "timestamp": "2026-08-31T09:00:00+0000",
            },
        )

        update = platform.read_update(body, {})

        assert update.kind is UpdateKind.POST_DELETED
        assert update.raw["values"]["value"]["id"] == POST_ID

    def test_which_post_was_removed_is_on_the_update(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        # Threads names the thing it removed and says when, and an app that
        # keeps its own copy of a post needs both to act on this.
        body = pushed(
            "delete",
            {"id": POST_ID, "deleted_at": "2026-08-31T10:33:16+0000"},
        )

        removed = platform.read_update(body, {}).raw["values"]["value"]

        assert removed["deleted_at"] == "2026-08-31T10:33:16+0000"

    def test_a_word_threads_adds_later_still_reaches_your_app(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        # Four topics today. A fifth arrives as UNKNOWN with Threads' own
        # word kept, rather than being dropped on the floor.
        update = platform.read_update(pushed("quotes", {"id": "1"}), {})

        assert update.kind is UpdateKind.UNKNOWN
        assert update.kind_name == "quotes"

    def test_the_same_message_twice_is_the_same_update(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        # Meta promises to deliver at least once, which is a promise to
        # deliver twice sometimes.
        body = pushed("replies", {"id": "8901234"})

        assert platform.read_update(body, {}).id == platform.read_update(body, {}).id

    def test_two_different_things_are_two_different_updates(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        one = platform.read_update(pushed("replies", {"id": "1"}), {})
        two = platform.read_update(pushed("replies", {"id": "2"}), {})

        assert one.id != two.id

    def test_a_message_with_no_time_on_it_at_all_is_stamped_as_it_arrives(
        self,
        platform: ThreadsPlatform,
        clock: dict[str, datetime],
    ) -> None:
        body = json.dumps(
            {"target_id": USER_ID, "values": {"field": "replies", "value": {}}}
        ).encode()

        assert platform.read_update(body, {}).created_at == NOW

    def test_a_message_with_only_the_envelope_time_uses_that(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        body = pushed("replies", {"id": "1"})

        assert platform.read_update(body, {}).created_at == datetime.fromtimestamp(
            1_790_000_000, UTC
        )

    def test_a_timestamp_threads_wrote_oddly_falls_back_to_the_envelope(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        body = pushed("replies", {"id": "1", "timestamp": "last Tuesday"})

        assert platform.read_update(body, {}).created_at == datetime.fromtimestamp(
            1_790_000_000, UTC
        )

    @pytest.mark.parametrize(
        "body",
        [
            b"{}",
            b'{"values": "not an object"}',
            b'{"values": {"value": {"id": "1"}}, "target_id": "1"}',
            b'{"values": {"field": "", "value": {}}}',
        ],
    )
    def test_a_message_carrying_nothing_we_can_act_on_is_no_updates(
        self,
        platform: ThreadsPlatform,
        body: bytes,
    ) -> None:
        assert platform.read_updates(body) == []

    def test_reading_one_update_out_of_nothing_says_to_use_read_updates(
        self,
        platform: ThreadsPlatform,
    ) -> None:
        with pytest.raises(PlatformError, match="read_updates"):
            platform.read_update(b"{}", {})

    @pytest.mark.parametrize("body", [b"not json", b"[1, 2, 3]"])
    def test_a_body_that_is_not_one_of_threads_messages_says_so(
        self,
        platform: ThreadsPlatform,
        body: bytes,
    ) -> None:
        with pytest.raises(PlatformError, match="could not be read"):
            platform.read_updates(body)

    def test_one_message_carries_one_change(self, platform: ThreadsPlatform) -> None:
        # Unlike the rest of Meta, which batches into a list of entries.
        assert len(platform.read_updates(pushed("replies", {"id": "1"}))) == 1


# ---------------------------------------------------------------------------
# The shared checks
# ---------------------------------------------------------------------------


class TestThreadsBehavesLikeTheOthers(PlatformChecks):
    def make_platform(self) -> Platform:
        return ThreadsPlatform(transport=self.transport, retries=ONCE)

    def make_connection(self) -> Connection | None:
        return an_account()

    def make_transport(self) -> httpx.AsyncBaseTransport | None:
        return RecordingTransport(
            {
                f"GET /v1.0/{USER_ID}/threads_publishing_limit": ALLOWANCE,
                f"POST /v1.0/{USER_ID}/threads": {"id": CONTAINER},
                f"POST /v1.0/{USER_ID}/threads_publish": {"id": POST_ID},
            }
        )
