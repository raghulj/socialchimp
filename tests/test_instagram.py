"""Tests for the Instagram platform."""

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
    Limits,
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
    CanAnswerSetupCheck,
    CanCheckSignature,
    CanReadPushedUpdates,
    Finished,
    LoginRequest,
    Platform,
)
from socialchimp.platforms import _meta
from socialchimp.platforms import instagram as instagram_module
from socialchimp.platforms._meta import DEVELOPER_PORTAL
from socialchimp.platforms.instagram import (
    DEFAULT_SCOPES,
    HOW_LONG_TO_WAIT,
    HOW_OFTEN_TO_CHECK,
    IG_GRAPH_API,
    IG_LOGIN_HOST,
    MAX_CAPTION_LENGTH,
    MOST_HASHTAGS,
    MOST_IN_A_CAROUSEL,
    REFRESH_AFTER_SECONDS,
    SIGN_IN_PAGE,
    InstagramPlatform,
    instagram_errors,
)
from socialchimp.testing import PlatformChecks, RecordingTransport

APP_ID = "1234567890"
APP_SECRET = "app-secret"
ACCESS_TOKEN = "access-token"
IG_ID = "17841400000000000"
IG_NAME = "adascakes"
CONTAINER = "17999000000000001"
OTHER_CONTAINER = "17999000000000002"
THIRD_CONTAINER = "17999000000000003"
PARENT_CONTAINER = "17999000000000009"
MEDIA_ID = "17895695668004550"
REDIRECT = "https://app.example/callback"

PICTURE_URL = "https://files.example/cake.jpg"
OTHER_PICTURE_URL = "https://files.example/icing.jpg"
THIRD_PICTURE_URL = "https://files.example/candles.jpg"
VIDEO_URL = "https://files.example/baking.mp4"

# One try and no waiting, so the error tests do not spend real seconds asleep.
ONCE = Retries(attempts=1)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)

# Four of a hundred used, so ninety-six posts are left today.
QUOTA: dict[str, Any] = {
    "data": [
        {"quota_usage": 4, "config": {"quota_total": 100, "quota_duration": 86_400}}
    ]
}

USED_UP: dict[str, Any] = {
    "data": [
        {"quota_usage": 100, "config": {"quota_total": 100, "quota_duration": 86_400}}
    ]
}


@pytest.fixture
def platform() -> InstagramPlatform:
    """A platform that gives up after one try."""
    return InstagramPlatform(retries=ONCE)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, datetime]:
    """A clock that only moves when the platform waits, so tests are instant."""
    moment = {"now": NOW}

    async def move_on(seconds: float) -> None:
        moment["now"] += timedelta(seconds=seconds)

    monkeypatch.setattr(instagram_module, "_now", lambda: moment["now"])
    monkeypatch.setattr(instagram_module, "_sleep", move_on)
    # A token's expiry is stamped by the shared Meta code, so that clock has
    # to stop too or every expiry test is off by however long the test took.
    monkeypatch.setattr(_meta, "_now", lambda: moment["now"])
    return moment


def an_app() -> AppCredentials:
    """Your app's credentials, as they arrive on a login request."""
    return AppCredentials(
        platform="instagram",
        host=None,
        client_id=APP_ID,
        client_secret=APP_SECRET,
    )


def a_request(*, state: str | None = "abc123") -> LoginRequest:
    """A login request carrying your app's credentials."""
    return LoginRequest(redirect_uri=REDIRECT, state=state, app=an_app())


def an_account(
    *,
    token: str = ACCESS_TOKEN,
    expires_at: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> Connection:
    """A connected Instagram business account."""
    if extra is None:
        extra = {
            "instagram_id": IG_ID,
            "username": IG_NAME,
            "profile_url": f"https://www.instagram.com/{IG_NAME}",
        }
    return Connection(
        id=f"instagram:{IG_ID}",
        platform="instagram",
        host=None,
        account_id=IG_ID,
        account_name=IG_NAME,
        token=Token(access_token=token, expires_at=expires_at),
        scopes=DEFAULT_SCOPES,
        extra=extra,
    )


@pytest.fixture
def account() -> Connection:
    """A connected account whose token does not expire."""
    return an_account()


def a_picture(url: str = PICTURE_URL, *, alt_text: str | None = None) -> Media:
    """A picture Instagram can fetch for itself."""
    return Media.from_url(url, alt_text=alt_text)


def a_video(url: str = VIDEO_URL) -> Media:
    """A video Instagram can fetch for itself."""
    return Media.from_url(url)


def sent_form(route: respx.Route, *, at: int = -1) -> dict[str, str]:
    """Read a form that went to the wire."""
    body = route.calls[at].request.content.decode()
    return dict(httpx.QueryParams(body))


def an_error(
    code: int,
    message: str = "Nope",
    subcode: int | None = None,
) -> dict[str, Any]:
    """What Meta's own error object looks like."""
    error: dict[str, Any] = {"message": message, "code": code}
    if subcode is not None:
        error["error_subcode"] = subcode
    return {"error": error}


def signed(body: bytes, *, secret: str = APP_SECRET) -> dict[str, str]:
    """The header Meta sends alongside a pushed body."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}"}


def a_push(value: dict[str, Any], *, topic: str = "comments") -> bytes:
    """One Instagram change, wrapped the way Meta wraps it."""
    return json.dumps(
        {
            "object": "instagram",
            "entry": [
                {
                    "id": IG_ID,
                    "time": 1_790_000_000,
                    "changes": [{"field": topic, "value": value}],
                }
            ],
        }
    ).encode()


def stub_quota(network: respx.Router, reply: dict[str, Any] = QUOTA) -> respx.Route:
    """Answer "how many posts are left today?"."""
    return network.get(f"/{IG_ID}/content_publishing_limit").mock(
        return_value=httpx.Response(200, json=reply)
    )


def stub_publish(network: respx.Router) -> respx.Route:
    """Answer "publish that container" with a post that exists."""
    return network.post(f"/{IG_ID}/media_publish").mock(
        return_value=httpx.Response(200, json={"id": MEDIA_ID})
    )


def status(code: str) -> httpx.Response:
    """What Instagram says when asked how a container is getting on."""
    return httpx.Response(200, json={"id": CONTAINER, "status_code": code})


def stub_ready(network: respx.Router, *containers: str) -> None:
    """Answer "how is that container getting on?" with "finished", for each."""
    for container in containers:
        network.get(f"/{container}").mock(
            return_value=httpx.Response(
                200, json={"id": container, "status_code": "FINISHED"}
            )
        )


# ---------------------------------------------------------------------------
# What it says it can do
# ---------------------------------------------------------------------------


class TestWhatItSaysItCanDo:
    def test_it_provides_everything_a_platform_must(
        self,
        platform: InstagramPlatform,
    ) -> None:
        checked: Platform = platform
        listens: CanCheckSignature = platform

        assert isinstance(checked, Platform)
        assert isinstance(listens, CanCheckSignature)
        assert platform.name == "instagram"

    def test_it_lists_the_features_instagram_really_has(
        self,
        platform: InstagramPlatform,
    ) -> None:
        for feature in (
            Feature.POST_IMAGE,
            Feature.POST_VIDEO,
            Feature.PUSH_UPDATES,
        ):
            assert feature in platform.features

    def test_it_says_it_cannot_post_words_on_their_own(
        self,
        platform: InstagramPlatform,
    ) -> None:
        assert Feature.POST_TEXT not in platform.features

    def test_it_does_not_claim_what_instagram_cannot_do_here(
        self,
        platform: InstagramPlatform,
    ) -> None:
        for missing in (
            Feature.CREATE_APP,
            Feature.SCHEDULE,
            Feature.REPLY,
            Feature.DELETE_POST,
            Feature.READ_POSTS,
            Feature.READ_STATS,
        ):
            assert missing not in platform.features

    def test_every_account_talks_to_the_same_address(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        assert platform.api_base(account) == IG_GRAPH_API

    def test_it_signs_requests_with_the_accounts_own_token(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        assert platform.auth_headers(account) == {
            "Authorization": f"Bearer {account.token.access_token}"
        }

    def test_its_caption_limit_is_the_one_instagram_is_known_for(self) -> None:
        assert MAX_CAPTION_LENGTH == 2_200
        assert MOST_HASHTAGS == 30
        assert MOST_IN_A_CAROUSEL == 10

    def test_it_checks_about_once_a_minute_for_five(self) -> None:
        assert HOW_OFTEN_TO_CHECK == 60.0
        assert HOW_LONG_TO_WAIT == 300.0


class TestThereIsNoAppToRegister:
    async def test_it_sends_people_to_metas_own_portal(
        self,
        platform: InstagramPlatform,
    ) -> None:
        with pytest.raises(NotSupportedError) as refused:
            await platform.create_app(name="My App", redirect_uri=REDIRECT)

        assert DEVELOPER_PORTAL in str(refused.value)

    async def test_it_warns_about_the_review_and_the_verification(
        self,
        platform: InstagramPlatform,
    ) -> None:
        with pytest.raises(NotSupportedError) as refused:
            await platform.create_app(name="My App", redirect_uri=REDIRECT)

        said = str(refused.value).lower()
        assert "review" in said
        assert "business" in said

    async def test_it_does_not_claim_it_can_register_an_app(
        self,
        platform: InstagramPlatform,
    ) -> None:
        assert Feature.CREATE_APP not in platform.features

    async def test_asking_it_to_register_sends_nothing_to_instagram(
        self,
        platform: InstagramPlatform,
    ) -> None:
        with (
            respx.mock(assert_all_called=False) as network,
            pytest.raises(NotSupportedError),
        ):
            await platform.create_app(name="My App", redirect_uri=REDIRECT)
        assert not network.calls


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


class TestStartingALogin:
    async def test_it_builds_the_address_to_send_somebody_to(
        self,
        platform: InstagramPlatform,
    ) -> None:
        step = await platform.start_login(a_request())

        query = httpx.URL(step.url).params
        assert step.url.startswith(f"{SIGN_IN_PAGE}?")
        assert query["client_id"] == APP_ID
        assert query["redirect_uri"] == REDIRECT
        assert step.state == "abc123"

    async def test_it_asks_for_the_permissions_instagram_needs_now(
        self,
        platform: InstagramPlatform,
    ) -> None:
        step = await platform.start_login(a_request())

        asked = httpx.URL(step.url).params["scope"].split(",")
        assert asked == list(DEFAULT_SCOPES)
        assert "instagram_business_content_publish" in asked

    async def test_it_makes_a_state_when_you_did_not(
        self,
        platform: InstagramPlatform,
    ) -> None:
        step = await platform.start_login(a_request(state=None))

        assert step.state
        assert httpx.URL(step.url).params["state"] == step.state

    async def test_there_is_nothing_to_carry_between_the_two_halves(
        self,
        platform: InstagramPlatform,
    ) -> None:
        step = await platform.start_login(a_request())

        assert step.remember == {}

    async def test_a_login_with_no_app_credentials_says_where_to_get_them(
        self,
        platform: InstagramPlatform,
    ) -> None:
        with pytest.raises(ConfigError) as refused:
            await platform.start_login(LoginRequest(redirect_uri=REDIRECT))

        assert DEVELOPER_PORTAL in str(refused.value)

    async def test_starting_a_login_sends_nothing_to_instagram(
        self,
        platform: InstagramPlatform,
    ) -> None:
        with respx.mock(assert_all_called=False) as network:
            await platform.start_login(a_request())
        assert not network.calls


class TestFinishingALogin:
    async def test_it_swaps_the_code_makes_it_last_and_reads_the_profile(
        self,
        platform: InstagramPlatform,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=IG_LOGIN_HOST) as network:
            swap = network.post("/oauth/access_token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "short-lived", "user_id": int(IG_ID)}
                )
            )
            with respx.mock(base_url="https://graph.instagram.com") as graph_network:
                make_it_last = graph_network.get("/access_token").mock(
                    return_value=httpx.Response(
                        200,
                        json={
                            "access_token": "long-lived",
                            "token_type": "bearer",
                            "expires_in": 5_184_000,
                        },
                    )
                )
                profile = graph_network.get(f"/v21.0/{IG_ID}").mock(
                    return_value=httpx.Response(
                        200, json={"id": IG_ID, "username": IG_NAME}
                    )
                )

                step = await platform.finish_login(a_request(), {"code": "abc"})

        sent = dict(httpx.QueryParams(swap.calls[-1].request.content.decode()))
        assert sent["client_id"] == APP_ID
        assert sent["client_secret"] == APP_SECRET
        assert sent["grant_type"] == "authorization_code"
        assert sent["redirect_uri"] == REDIRECT
        assert sent["code"] == "abc"

        traded = make_it_last.calls[-1].request.url.params
        assert traded["grant_type"] == "ig_exchange_token"
        assert traded["client_secret"] == APP_SECRET
        assert traded["access_token"] == "short-lived"

        assert profile.calls[-1].request.headers["Authorization"] == (
            "Bearer long-lived"
        )

        assert isinstance(step, Finished)
        saved = step.connection
        assert saved.id == f"instagram:{IG_ID}"
        assert saved.account_id == IG_ID
        assert saved.account_name == IG_NAME
        assert saved.token.access_token == "long-lived"
        assert saved.token.expires_at == NOW + timedelta(seconds=5_184_000)

    async def test_it_finishes_the_login_outright(
        self,
        platform: InstagramPlatform,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=IG_LOGIN_HOST) as network:
            network.post("/oauth/access_token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "short-lived", "user_id": int(IG_ID)}
                )
            )
            with respx.mock(base_url="https://graph.instagram.com") as graph_network:
                graph_network.get("/access_token").mock(
                    return_value=httpx.Response(
                        200,
                        json={
                            "access_token": "long-lived",
                            "expires_in": 5_184_000,
                        },
                    )
                )
                graph_network.get(f"/v21.0/{IG_ID}").mock(
                    return_value=httpx.Response(
                        200, json={"id": IG_ID, "username": IG_NAME}
                    )
                )

                step = await platform.finish_login(a_request(), {"code": "abc"})

        assert isinstance(step, Finished)

    def test_it_never_imports_choose_account(self) -> None:
        import socialchimp.platforms.instagram as instagram_module

        assert not hasattr(instagram_module, "ChooseAccount")

    async def test_it_remembers_the_instagram_id_and_username(
        self,
        platform: InstagramPlatform,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=IG_LOGIN_HOST) as network:
            network.post("/oauth/access_token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "short-lived", "user_id": int(IG_ID)}
                )
            )
            with respx.mock(base_url="https://graph.instagram.com") as graph_network:
                graph_network.get("/access_token").mock(
                    return_value=httpx.Response(
                        200,
                        json={
                            "access_token": "long-lived",
                            "expires_in": 5_184_000,
                        },
                    )
                )
                graph_network.get(f"/v21.0/{IG_ID}").mock(
                    return_value=httpx.Response(
                        200, json={"id": IG_ID, "username": IG_NAME}
                    )
                )

                step = await platform.finish_login(a_request(), {"code": "abc"})

        assert step.connection.extra["instagram_id"] == IG_ID
        assert step.connection.extra["username"] == IG_NAME
        assert IG_NAME in str(step.connection.extra["profile_url"])

    async def test_it_uses_the_permissions_the_login_asked_for(
        self,
        platform: InstagramPlatform,
        clock: dict[str, datetime],
    ) -> None:
        asked = LoginRequest(
            redirect_uri=REDIRECT, scopes=("instagram_business_basic",), app=an_app()
        )

        with respx.mock(base_url=IG_LOGIN_HOST) as network:
            network.post("/oauth/access_token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "short-lived", "user_id": int(IG_ID)}
                )
            )
            with respx.mock(base_url="https://graph.instagram.com") as graph_network:
                graph_network.get("/access_token").mock(
                    return_value=httpx.Response(
                        200,
                        json={
                            "access_token": "long-lived",
                            "expires_in": 5_184_000,
                        },
                    )
                )
                graph_network.get(f"/v21.0/{IG_ID}").mock(
                    return_value=httpx.Response(
                        200, json={"id": IG_ID, "username": IG_NAME}
                    )
                )

                step = await platform.finish_login(asked, {"code": "abc"})

        assert step.connection.scopes == ("instagram_business_basic",)

    async def test_a_state_that_does_not_match_stops_the_login(
        self,
        platform: InstagramPlatform,
    ) -> None:
        with pytest.raises(AuthError) as refused:
            await platform.finish_login(
                a_request(), {"code": "abc", "state": "somebody-elses"}
            )

        assert "did not start here" in str(refused.value)

    async def test_a_person_who_pressed_cancel_is_explained(
        self,
        platform: InstagramPlatform,
    ) -> None:
        with pytest.raises(AuthError) as refused:
            await platform.finish_login(
                a_request(state=None),
                {"error": "access_denied", "error_description": "Nope"},
            )

        assert "cancel" in str(refused.value)
        assert "Nope" in str(refused.value)

    async def test_a_refusal_with_no_explanation_still_says_what_happened(
        self,
        platform: InstagramPlatform,
    ) -> None:
        with pytest.raises(AuthError) as refused:
            await platform.finish_login(
                a_request(state=None), {"error": "access_denied"}
            )

        assert "access_denied" in str(refused.value)

    async def test_a_callback_with_no_code_says_what_to_pass(
        self,
        platform: InstagramPlatform,
    ) -> None:
        with pytest.raises(AuthError) as refused:
            await platform.finish_login(a_request(state=None), {})

        assert "whole query string" in str(refused.value)

    async def test_finishing_with_no_app_credentials_says_where_to_get_them(
        self,
        platform: InstagramPlatform,
    ) -> None:
        with pytest.raises(ConfigError):
            await platform.finish_login(
                LoginRequest(redirect_uri=REDIRECT), {"code": "abc"}
            )

    async def test_a_refused_token_swap_says_the_app_id_is_likely_wrong(
        self,
        platform: InstagramPlatform,
    ) -> None:
        with respx.mock(base_url=IG_LOGIN_HOST) as network:
            network.post("/oauth/access_token").mock(
                return_value=httpx.Response(400, json=an_error(100))
            )

            with pytest.raises(AuthError) as refused:
                await platform.finish_login(a_request(), {"code": "abc"})

        said = str(refused.value)
        assert "Instagram App ID" in said
        assert "Instagram App Secret" in said

    async def test_a_refused_long_lived_token_says_the_app_is_wrong(
        self,
        platform: InstagramPlatform,
    ) -> None:
        with respx.mock(base_url=IG_LOGIN_HOST) as network:
            network.post("/oauth/access_token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "short-lived", "user_id": int(IG_ID)}
                )
            )
            with respx.mock(base_url="https://graph.instagram.com") as graph_network:
                graph_network.get("/access_token").mock(
                    return_value=httpx.Response(400, json=an_error(100))
                )

                with pytest.raises(AuthError) as refused:
                    await platform.finish_login(a_request(), {"code": "abc"})

        said = str(refused.value)
        assert "Instagram App ID" in said


# ---------------------------------------------------------------------------
# Renewing a token
# ---------------------------------------------------------------------------


class TestRenewingAToken:
    async def test_a_token_with_plenty_of_life_left_is_not_renewed(
        self,
        platform: InstagramPlatform,
        clock: dict[str, datetime],
    ) -> None:
        plenty = an_account(
            expires_at=NOW + timedelta(seconds=REFRESH_AFTER_SECONDS + 86400)
        )

        with respx.mock(assert_all_called=False) as network:
            token = await platform.refresh(plenty)

        assert token is plenty.token
        assert not network.calls

    async def test_a_token_with_thirty_days_exactly_left_is_renewed(
        self,
        platform: InstagramPlatform,
        clock: dict[str, datetime],
    ) -> None:
        running_out = an_account(
            expires_at=NOW + timedelta(seconds=REFRESH_AFTER_SECONDS)
        )

        with respx.mock(base_url="https://graph.instagram.com") as network:
            renew = network.get("/refresh_access_token").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "access_token": "fresh",
                        "token_type": "bearer",
                        "expires_in": 5_184_000,
                    },
                )
            )

            token = await platform.refresh(running_out)

        query = renew.calls[-1].request.url.params
        assert query["grant_type"] == "ig_refresh_token"
        assert query["access_token"] == running_out.token.access_token
        assert token.access_token == "fresh"
        assert token.expires_at is not None

    async def test_a_token_with_less_than_thirty_days_left_is_renewed(
        self,
        platform: InstagramPlatform,
        clock: dict[str, datetime],
    ) -> None:
        running_out = an_account(expires_at=NOW + timedelta(days=1))

        with respx.mock(base_url="https://graph.instagram.com") as network:
            renew = network.get("/refresh_access_token").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "access_token": "fresh",
                        "expires_in": 5_184_000,
                    },
                )
            )

            token = await platform.refresh(running_out)

        assert renew.called
        assert token.access_token == "fresh"

    async def test_a_token_with_no_expiry_is_renewed(
        self,
        platform: InstagramPlatform,
    ) -> None:
        no_expiry = an_account(expires_at=None)

        with respx.mock(base_url="https://graph.instagram.com") as network:
            network.get("/refresh_access_token").mock(
                return_value=httpx.Response(
                    200,
                    json={"access_token": "fresh", "expires_in": 5_184_000},
                )
            )

            token = await platform.refresh(no_expiry)

        assert token.access_token == "fresh"

    async def test_a_token_meta_will_not_extend_needs_a_new_sign_in(
        self,
        platform: InstagramPlatform,
        clock: dict[str, datetime],
    ) -> None:
        running_out = an_account(expires_at=NOW + timedelta(days=1))

        with (
            respx.mock(base_url="https://graph.instagram.com") as network,
            pytest.raises(TokenExpiredError) as refused,
        ):
            network.get("/refresh_access_token").mock(
                return_value=httpx.Response(400, json=an_error(190))
            )
            await platform.refresh(running_out)

        assert "connect their account again" in str(refused.value)

    async def test_the_refresh_needs_no_app_credentials(
        self,
        platform: InstagramPlatform,
        clock: dict[str, datetime],
    ) -> None:
        running_out = an_account(expires_at=NOW + timedelta(days=1))

        with respx.mock(base_url="https://graph.instagram.com") as network:
            renew = network.get("/refresh_access_token").mock(
                return_value=httpx.Response(
                    200,
                    json={"access_token": "fresh", "expires_in": 5_184_000},
                )
            )

            await platform.refresh(running_out)

        query = renew.calls[-1].request.url.params
        assert "client_secret" not in query
        assert "client_id" not in query

    async def test_your_app_credentials_are_accepted_and_ignored(
        self,
        platform: InstagramPlatform,
        clock: dict[str, datetime],
    ) -> None:
        running_out = an_account(expires_at=NOW + timedelta(days=1))

        with respx.mock(base_url="https://graph.instagram.com") as network:
            network.get("/refresh_access_token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "fresh", "expires_in": 5_184_000}
                )
            )

            token = await platform.refresh(running_out, an_app())

        assert token.access_token == "fresh"


# ---------------------------------------------------------------------------
# Account ID extraction
# ---------------------------------------------------------------------------


class TestAccountIdExtraction:
    async def test_it_extracts_an_integer_user_id(
        self,
        platform: InstagramPlatform,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=IG_LOGIN_HOST) as network:
            network.post("/oauth/access_token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "short-lived", "user_id": 12345}
                )
            )
            with respx.mock(base_url="https://graph.instagram.com") as graph_network:
                graph_network.get("/access_token").mock(
                    return_value=httpx.Response(
                        200,
                        json={"access_token": "long-lived", "expires_in": 5_184_000},
                    )
                )
                graph_network.get("/v21.0/12345").mock(
                    return_value=httpx.Response(
                        200, json={"id": "12345", "username": "test"}
                    )
                )

                step = await platform.finish_login(a_request(), {"code": "abc"})

        assert step.connection.account_id == "12345"

    async def test_it_extracts_a_string_user_id(
        self,
        platform: InstagramPlatform,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=IG_LOGIN_HOST) as network:
            network.post("/oauth/access_token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "short-lived", "user_id": "67890"}
                )
            )
            with respx.mock(base_url="https://graph.instagram.com") as graph_network:
                graph_network.get("/access_token").mock(
                    return_value=httpx.Response(
                        200,
                        json={"access_token": "long-lived", "expires_in": 5_184_000},
                    )
                )
                graph_network.get("/v21.0/67890").mock(
                    return_value=httpx.Response(
                        200, json={"id": "67890", "username": "test"}
                    )
                )

                step = await platform.finish_login(a_request(), {"code": "abc"})

        assert step.connection.account_id == "67890"

    async def test_a_boolean_user_id_is_rejected(
        self,
        platform: InstagramPlatform,
    ) -> None:
        with respx.mock(base_url=IG_LOGIN_HOST) as network:
            network.post("/oauth/access_token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "short-lived", "user_id": True}
                )
            )

            with pytest.raises(PlatformError) as refused:
                await platform.finish_login(a_request(), {"code": "abc"})

        assert "user_id" in str(refused.value)

    async def test_a_missing_user_id_is_rejected(
        self,
        platform: InstagramPlatform,
    ) -> None:
        with respx.mock(base_url=IG_LOGIN_HOST) as network:
            network.post("/oauth/access_token").mock(
                return_value=httpx.Response(200, json={"access_token": "short-lived"})
            )

            with pytest.raises(PlatformError) as refused:
                await platform.finish_login(a_request(), {"code": "abc"})

        assert "user_id" in str(refused.value)


# ---------------------------------------------------------------------------
# Publishing: one picture
# ---------------------------------------------------------------------------


class TestPublishingAPicture:
    async def test_it_builds_the_post_then_publishes_it(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network)
            build = network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(200, json={"id": CONTAINER})
            )
            stub_ready(network, CONTAINER)
            put_out = stub_publish(network)

            result = await platform.publish(
                account, Post(text="Fresh out of the oven", media=(a_picture(),))
            )

        assert sent_form(build) == {
            "caption": "Fresh out of the oven",
            "image_url": PICTURE_URL,
        }
        assert sent_form(put_out) == {"creation_id": CONTAINER}
        assert result.id == MEDIA_ID
        assert result.state is PostState.DONE
        assert result.is_done

    async def test_it_hands_back_no_link_because_instagram_gives_none(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network)
            network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(200, json={"id": CONTAINER})
            )
            stub_ready(network, CONTAINER)
            stub_publish(network)

            result = await platform.publish(account, Post(media=(a_picture(),)))

        assert result.url is None
        assert result.raw == {"id": MEDIA_ID}

    async def test_it_sends_the_alt_text_where_instagram_will_read_it(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network)
            build = network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(200, json={"id": CONTAINER})
            )
            stub_ready(network, CONTAINER)
            stub_publish(network)

            await platform.publish(
                account,
                Post(media=(a_picture(alt_text="A lemon cake, iced"),)),
            )

        assert sent_form(build)["alt_text"] == "A lemon cake, iced"

    async def test_it_posts_as_the_account_named_on_the_connection(
        self,
        platform: InstagramPlatform,
    ) -> None:
        plain = an_account(extra={})

        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network)
            network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(200, json={"id": CONTAINER})
            )
            stub_ready(network, CONTAINER)
            stub_publish(network)

            result = await platform.publish(plain, Post(media=(a_picture(),)))

        assert result.id == MEDIA_ID

    async def test_a_connection_naming_no_account_says_what_is_missing(
        self,
        platform: InstagramPlatform,
    ) -> None:
        nowhere = Connection(
            id="instagram:mystery",
            platform="instagram",
            host=None,
            account_id="",
            account_name="",
            token=Token(access_token="token"),
        )

        with pytest.raises(ConfigError) as refused:
            await platform.publish(nowhere, Post(media=(a_picture(),)))

        assert "instagram_id" in str(refused.value)

    async def test_a_reply_with_no_container_id_is_complained_about(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API) as network,
            pytest.raises(PlatformError) as refused,
        ):
            stub_quota(network)
            network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(200, json={"nothing": "useful"})
            )
            await platform.publish(account, Post(media=(a_picture(),)))

        assert "'id'" in str(refused.value)


# ---------------------------------------------------------------------------
# Publishing: video, and the waiting that comes with it
# ---------------------------------------------------------------------------


class TestPublishingAVideo:
    async def test_a_single_video_goes_out_as_a_reel(
        self,
        platform: InstagramPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network)
            build = network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(200, json={"id": CONTAINER})
            )
            network.get(f"/{CONTAINER}").mock(side_effect=[status("FINISHED")])
            stub_publish(network)

            result = await platform.publish(
                account, Post(text="Baking", media=(a_video(),))
            )

        assert sent_form(build) == {
            "caption": "Baking",
            "media_type": "REELS",
            "video_url": VIDEO_URL,
        }
        assert result.state is PostState.DONE

    async def test_it_waits_while_instagram_is_still_working(
        self,
        platform: InstagramPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network)
            network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(200, json={"id": CONTAINER})
            )
            asked = network.get(f"/{CONTAINER}").mock(
                side_effect=[status("IN_PROGRESS"), status("FINISHED")]
            )
            put_out = stub_publish(network)

            result = await platform.publish(account, Post(media=(a_video(),)))

        assert asked.call_count == 2
        assert asked.calls[0].request.url.params["fields"] == "status_code,status"
        assert clock["now"] == NOW + timedelta(seconds=HOW_OFTEN_TO_CHECK)
        assert put_out.called
        assert result.id == MEDIA_ID

    async def test_the_waiting_is_a_real_wait(
        self,
        account: Connection,
    ) -> None:
        eager = InstagramPlatform(retries=ONCE, check_every_seconds=0.0)

        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network)
            network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(200, json={"id": CONTAINER})
            )
            network.get(f"/{CONTAINER}").mock(
                side_effect=[status("IN_PROGRESS"), status("FINISHED")]
            )
            stub_publish(network)

            result = await eager.publish(account, Post(media=(a_video(),)))

        assert result.id == MEDIA_ID

    async def test_a_container_that_fails_says_what_instagram_said(
        self,
        platform: InstagramPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API, assert_all_called=False) as network,
            pytest.raises(InvalidPostError) as refused,
        ):
            stub_quota(network)
            network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(200, json={"id": CONTAINER})
            )
            network.get(f"/{CONTAINER}").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "status_code": "ERROR",
                        "status": "Error: 2207026 - Unsupported video format",
                    },
                )
            )
            put_out = stub_publish(network)
            await platform.publish(account, Post(media=(a_video(),)))

        assert "Unsupported video format" in str(refused.value)
        assert not put_out.called

    async def test_a_container_that_fails_quietly_still_says_so(
        self,
        platform: InstagramPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API) as network,
            pytest.raises(InvalidPostError) as refused,
        ):
            stub_quota(network)
            network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(200, json={"id": CONTAINER})
            )
            network.get(f"/{CONTAINER}").mock(side_effect=[status("ERROR")])
            await platform.publish(account, Post(media=(a_video(),)))

        assert "gave up" in str(refused.value)

    async def test_a_container_instagram_threw_away_is_explained(
        self,
        platform: InstagramPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API) as network,
            pytest.raises(PlatformError) as refused,
        ):
            stub_quota(network)
            network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(200, json={"id": CONTAINER})
            )
            network.get(f"/{CONTAINER}").mock(side_effect=[status("EXPIRED")])
            await platform.publish(account, Post(media=(a_video(),)))

        assert "24 hours" in str(refused.value)

    async def test_a_container_that_never_finishes_says_it_may_still_appear(
        self,
        platform: InstagramPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API, assert_all_called=False) as network,
            pytest.raises(PlatformError) as refused,
        ):
            stub_quota(network)
            network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(200, json={"id": CONTAINER})
            )
            asked = network.get(f"/{CONTAINER}").mock(
                return_value=status("IN_PROGRESS")
            )
            put_out = stub_publish(network)
            await platform.publish(account, Post(media=(a_video(),)))

        said = str(refused.value)
        assert "may still appear" in said
        assert CONTAINER in said
        assert asked.call_count == 6
        assert clock["now"] == NOW + timedelta(seconds=HOW_LONG_TO_WAIT)
        assert not put_out.called

    async def test_how_long_to_wait_is_yours_to_set(
        self,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        patient = InstagramPlatform(
            retries=ONCE, check_every_seconds=5.0, wait_up_to_seconds=10.0
        )

        with (
            respx.mock(base_url=IG_GRAPH_API) as network,
            pytest.raises(PlatformError),
        ):
            stub_quota(network)
            network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(200, json={"id": CONTAINER})
            )
            asked = network.get(f"/{CONTAINER}").mock(
                return_value=status("IN_PROGRESS")
            )
            await patient.publish(account, Post(media=(a_video(),)))

        assert asked.call_count == 3
        assert clock["now"] == NOW + timedelta(seconds=10)

    async def test_a_word_instagram_has_not_used_before_is_waited_out(
        self,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        quick = InstagramPlatform(
            retries=ONCE, check_every_seconds=5.0, wait_up_to_seconds=5.0
        )

        with (
            respx.mock(base_url=IG_GRAPH_API) as network,
            pytest.raises(PlatformError) as refused,
        ):
            stub_quota(network)
            network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(200, json={"id": CONTAINER})
            )
            network.get(f"/{CONTAINER}").mock(return_value=status("PUBLISHED"))
            await quick.publish(account, Post(media=(a_video(),)))

        assert "may still appear" in str(refused.value)


# ---------------------------------------------------------------------------
# Publishing: a picture is not ready the instant Instagram says it has one
# ---------------------------------------------------------------------------

# What Instagram sent, more or less, when a picture was published 0.27 seconds
# after it was created: a 400 that is not a refusal of the post at all.
NOT_READY: dict[str, Any] = {
    "error": {
        "message": "Media ID is not available",
        "type": "OAuthException",
        "code": 9007,
        "error_subcode": 2207027,
        "is_transient": False,
        "error_user_title": "Media not ready for publishing",
        "error_user_msg": "The media is not ready for publishing, please wait.",
    }
}

WRONG_SHAPE: dict[str, Any] = {
    "error": {
        "message": "The aspect ratio is not supported",
        "type": "OAuthException",
        "code": 36003,
        "error_subcode": 2207009,
    }
}


@pytest.fixture
def stopwatch(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Like `clock`, but remembers how long each wait was."""
    moment = {"now": NOW}
    waits: list[float] = []

    async def move_on(seconds: float) -> None:
        waits.append(seconds)
        moment["now"] += timedelta(seconds=seconds)

    monkeypatch.setattr(instagram_module, "_now", lambda: moment["now"])
    monkeypatch.setattr(instagram_module, "_sleep", move_on)
    monkeypatch.setattr(_meta, "_now", lambda: moment["now"])
    return waits


def what_went_out(network: respx.Router) -> list[str]:
    """Every request that was made, in order, as `METHOD last-part-of-path`."""
    return [
        f"{call.request.method} {call.request.url.path.rsplit('/', 1)[-1]}"
        for call in network.calls
    ]


def a_picture_container(network: respx.Router) -> None:
    """Answer "start making this picture" with a container."""
    network.post(f"/{IG_ID}/media").mock(
        return_value=httpx.Response(200, json={"id": CONTAINER})
    )


class TestWaitingForAPictureToBeReady:
    async def test_it_publishes_only_once_the_picture_says_it_is_finished(
        self,
        platform: InstagramPlatform,
        account: Connection,
        stopwatch: list[float],
    ) -> None:
        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network)
            a_picture_container(network)
            asked = network.get(f"/{CONTAINER}").mock(
                side_effect=[status("IN_PROGRESS"), status("FINISHED")]
            )
            stub_publish(network)

            result = await platform.publish(account, Post(media=(a_picture(),)))
            # Read inside the block: respx forgets the router's calls on the
            # way out (each route keeps its own).
            went = what_went_out(network)

        assert asked.call_count == 2
        assert asked.calls[0].request.url.params["fields"] == "status_code,status"
        assert went[-3:] == [
            f"GET {CONTAINER}",
            f"GET {CONTAINER}",
            "POST media_publish",
        ]
        assert stopwatch == [instagram_module.PICTURE_FIRST_WAIT]
        assert result.id == MEDIA_ID

    async def test_a_picture_that_is_already_ready_costs_one_look_and_no_wait(
        self,
        platform: InstagramPlatform,
        account: Connection,
        stopwatch: list[float],
    ) -> None:
        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network)
            a_picture_container(network)
            asked = network.get(f"/{CONTAINER}").mock(side_effect=[status("FINISHED")])
            stub_publish(network)

            await platform.publish(account, Post(media=(a_picture(),)))

        assert asked.call_count == 1
        assert stopwatch == []

    async def test_the_first_looks_are_quick_and_slow_down_to_a_cap(
        self,
        platform: InstagramPlatform,
        account: Connection,
        stopwatch: list[float],
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API, assert_all_called=False) as network,
            pytest.raises(PlatformError) as refused,
        ):
            stub_quota(network)
            a_picture_container(network)
            network.get(f"/{CONTAINER}").mock(return_value=status("IN_PROGRESS"))
            put_out = stub_publish(network)

            await platform.publish(account, Post(media=(a_picture(),)))

        assert stopwatch[:6] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
        assert max(stopwatch) == instagram_module.PICTURE_LONGEST_WAIT == 30.0
        assert sum(stopwatch) >= HOW_LONG_TO_WAIT
        assert "may still appear" in str(refused.value)
        assert not put_out.called

    async def test_a_picture_instagram_gave_up_on_is_an_invalid_post(
        self,
        platform: InstagramPlatform,
        account: Connection,
        stopwatch: list[float],
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API, assert_all_called=False) as network,
            pytest.raises(InvalidPostError),
        ):
            stub_quota(network)
            a_picture_container(network)
            network.get(f"/{CONTAINER}").mock(return_value=status("ERROR"))
            put_out = stub_publish(network)

            await platform.publish(account, Post(media=(a_picture(),)))

        assert not put_out.called

    async def test_a_picture_instagram_threw_away_is_a_platform_error(
        self,
        platform: InstagramPlatform,
        account: Connection,
        stopwatch: list[float],
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API, assert_all_called=False) as network,
            pytest.raises(PlatformError, match="threw away"),
        ):
            stub_quota(network)
            a_picture_container(network)
            network.get(f"/{CONTAINER}").mock(return_value=status("EXPIRED"))
            put_out = stub_publish(network)

            await platform.publish(account, Post(media=(a_picture(),)))

        assert not put_out.called

    async def test_a_picture_with_no_status_at_all_is_published_not_waited_on(
        self,
        platform: InstagramPlatform,
        account: Connection,
        stopwatch: list[float],
    ) -> None:
        # Meta's guide does not say a picture has a status, so a reply that
        # carries none must not turn every picture into a five minute wait.
        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network)
            a_picture_container(network)
            asked = network.get(f"/{CONTAINER}").mock(
                return_value=httpx.Response(200, json={"id": CONTAINER})
            )
            put_out = stub_publish(network)

            result = await platform.publish(account, Post(media=(a_picture(),)))

        assert asked.call_count == 1
        assert put_out.called
        assert stopwatch == []
        assert result.id == MEDIA_ID

    async def test_a_video_with_no_status_is_still_waited_on(
        self,
        platform: InstagramPlatform,
        account: Connection,
        stopwatch: list[float],
    ) -> None:
        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network)
            a_picture_container(network)
            network.get(f"/{CONTAINER}").mock(
                side_effect=[
                    httpx.Response(200, json={"id": CONTAINER}),
                    status("FINISHED"),
                ]
            )
            stub_publish(network)

            await platform.publish(account, Post(media=(a_video(),)))

        assert stopwatch == [HOW_OFTEN_TO_CHECK]

    async def test_a_carousel_of_pictures_waits_on_every_piece_and_the_whole(
        self,
        platform: InstagramPlatform,
        account: Connection,
        stopwatch: list[float],
    ) -> None:
        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network)
            network.post(f"/{IG_ID}/media").mock(
                side_effect=[
                    httpx.Response(200, json={"id": CONTAINER}),
                    httpx.Response(200, json={"id": OTHER_CONTAINER}),
                    httpx.Response(200, json={"id": PARENT_CONTAINER}),
                ]
            )
            first = network.get(f"/{CONTAINER}").mock(return_value=status("FINISHED"))
            second = network.get(f"/{OTHER_CONTAINER}").mock(
                return_value=status("FINISHED")
            )
            parent = network.get(f"/{PARENT_CONTAINER}").mock(
                side_effect=[status("IN_PROGRESS"), status("FINISHED")]
            )
            put_out = stub_publish(network)

            await platform.publish(
                account,
                Post(media=(a_picture(), a_picture(OTHER_PICTURE_URL))),
            )
            went = what_went_out(network)

        assert (first.call_count, second.call_count, parent.call_count) == (1, 1, 2)
        assert sent_form(put_out) == {"creation_id": PARENT_CONTAINER}
        # The parent is not asked about until the children have been named on
        # it, and nothing is published until the parent says it is ready.
        last_build = max(i for i, line in enumerate(went) if line == "POST media")
        assert went.index(f"GET {PARENT_CONTAINER}") > last_build
        assert went.index("POST media_publish") > max(
            i for i, line in enumerate(went) if line == f"GET {PARENT_CONTAINER}"
        )

    async def test_video_is_still_looked_at_once_a_minute(
        self,
        platform: InstagramPlatform,
        account: Connection,
        stopwatch: list[float],
    ) -> None:
        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network)
            a_picture_container(network)
            network.get(f"/{CONTAINER}").mock(
                side_effect=[
                    status("IN_PROGRESS"),
                    status("IN_PROGRESS"),
                    status("FINISHED"),
                ]
            )
            stub_publish(network)

            await platform.publish(account, Post(media=(a_video(),)))

        assert stopwatch == [HOW_OFTEN_TO_CHECK, HOW_OFTEN_TO_CHECK]


class TestNotReadyYet:
    async def test_it_is_named_and_is_the_kind_an_app_may_try_again(
        self,
        platform: InstagramPlatform,
        account: Connection,
        stopwatch: list[float],
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API) as network,
            pytest.raises(RateLimitError) as refused,
        ):
            stub_quota(network)
            a_picture_container(network)
            network.get(f"/{CONTAINER}").mock(return_value=status("FINISHED"))
            put_out = network.post(f"/{IG_ID}/media_publish").mock(
                return_value=httpx.Response(400, json=NOT_READY)
            )

            await platform.publish(account, Post(media=(a_picture(),)))

        said = str(refused.value)
        assert "not ready" in said
        assert "9007" in said
        assert refused.value.retry_after is not None
        assert refused.value.raw == NOT_READY
        # Once, and twice more after short waits, and then it is the app's turn.
        assert put_out.call_count == 1 + instagram_module.TIMES_TO_PUBLISH_AGAIN
        assert stopwatch == [instagram_module.WAIT_BEFORE_PUBLISHING_AGAIN] * (
            instagram_module.TIMES_TO_PUBLISH_AGAIN
        )

    async def test_a_second_try_that_works_is_a_post_and_not_an_error(
        self,
        platform: InstagramPlatform,
        account: Connection,
        stopwatch: list[float],
    ) -> None:
        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network)
            a_picture_container(network)
            network.get(f"/{CONTAINER}").mock(return_value=status("FINISHED"))
            put_out = network.post(f"/{IG_ID}/media_publish").mock(
                side_effect=[
                    httpx.Response(400, json=NOT_READY),
                    httpx.Response(200, json={"id": MEDIA_ID}),
                ]
            )

            result = await platform.publish(account, Post(media=(a_picture(),)))

        assert result.id == MEDIA_ID
        assert put_out.call_count == 2
        assert [sent_form(put_out, at=i) for i in (0, 1)] == [
            {"creation_id": CONTAINER}
        ] * 2
        assert stopwatch == [instagram_module.WAIT_BEFORE_PUBLISHING_AGAIN]

    async def test_it_is_the_same_when_instagram_hides_it_in_a_200(
        self,
        platform: InstagramPlatform,
        account: Connection,
        stopwatch: list[float],
    ) -> None:
        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network)
            a_picture_container(network)
            network.get(f"/{CONTAINER}").mock(return_value=status("FINISHED"))
            network.post(f"/{IG_ID}/media_publish").mock(
                side_effect=[
                    httpx.Response(200, json=NOT_READY),
                    httpx.Response(200, json={"id": MEDIA_ID}),
                ]
            )

            result = await platform.publish(account, Post(media=(a_picture(),)))

        assert result.id == MEDIA_ID

    @pytest.mark.parametrize(
        "error",
        [
            {"code": 9007},
            {"code": 1, "error_subcode": 2207027},
            # Which of the two Instagram puts it under is not something to
            # rely on, so both count - and so does 24, which is where the
            # generic "something went wrong" was hiding it before.
            {"code": 24, "error_subcode": 2207027},
        ],
    )
    def test_either_code_names_it(self, error: dict[str, Any]) -> None:
        named = instagram_errors(httpx.Response(400, json={"error": error}))

        assert isinstance(named, RateLimitError)
        assert named.platform == "instagram"

    async def test_an_ordinary_rate_limit_is_not_tried_again_here(
        self,
        platform: InstagramPlatform,
        account: Connection,
        stopwatch: list[float],
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API) as network,
            pytest.raises(RateLimitError),
        ):
            stub_quota(network)
            a_picture_container(network)
            network.get(f"/{CONTAINER}").mock(return_value=status("FINISHED"))
            put_out = network.post(f"/{IG_ID}/media_publish").mock(
                return_value=httpx.Response(400, json=an_error(32))
            )

            await platform.publish(account, Post(media=(a_picture(),)))

        # That one means the hour's allowance is gone, and waiting seconds
        # does nothing about it - so it is left alone for the app to handle.
        assert put_out.call_count == 1
        assert stopwatch == []

    async def test_a_plain_429_with_no_body_is_not_tried_again_either(
        self,
        platform: InstagramPlatform,
        account: Connection,
        stopwatch: list[float],
    ) -> None:
        # Nothing in it says "not ready", because nothing in it says anything.
        with (
            respx.mock(base_url=IG_GRAPH_API) as network,
            pytest.raises(RateLimitError),
        ):
            stub_quota(network)
            a_picture_container(network)
            network.get(f"/{CONTAINER}").mock(return_value=status("FINISHED"))
            put_out = network.post(f"/{IG_ID}/media_publish").mock(
                return_value=httpx.Response(429, text="slow down")
            )

            await platform.publish(account, Post(media=(a_picture(),)))

        assert put_out.call_count == 1
        assert stopwatch == []

    async def test_another_refusal_while_publishing_is_not_tried_again(
        self,
        platform: InstagramPlatform,
        account: Connection,
        stopwatch: list[float],
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API) as network,
            pytest.raises(InvalidPostError),
        ):
            stub_quota(network)
            a_picture_container(network)
            network.get(f"/{CONTAINER}").mock(return_value=status("FINISHED"))
            put_out = network.post(f"/{IG_ID}/media_publish").mock(
                return_value=httpx.Response(400, json=an_error(9004))
            )

            await platform.publish(account, Post(media=(a_picture(),)))

        assert put_out.call_count == 1
        assert stopwatch == []


class TestAPictureTheWrongShape:
    @pytest.mark.parametrize(
        "error",
        [
            WRONG_SHAPE["error"],
            {"code": 1, "error_subcode": 36003},
        ],
    )
    async def test_it_is_a_permanent_invalid_post_that_says_what_to_fix(
        self,
        platform: InstagramPlatform,
        account: Connection,
        stopwatch: list[float],
        error: dict[str, Any],
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API) as network,
            pytest.raises(InvalidPostError) as refused,
        ):
            stub_quota(network)
            build = network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(400, json={"error": error})
            )

            await platform.publish(account, Post(media=(a_picture(),)))

        said = str(refused.value)
        assert "36003" in said
        assert "aspect ratio" in said
        assert "4:5" in said
        assert "1.91" in said
        assert build.call_count == 1
        assert stopwatch == []


# ---------------------------------------------------------------------------
# Publishing: carousels
# ---------------------------------------------------------------------------


class TestPublishingACarousel:
    async def test_three_pictures_become_three_containers_and_a_parent(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network)
            build = network.post(f"/{IG_ID}/media").mock(
                side_effect=[
                    httpx.Response(200, json={"id": CONTAINER}),
                    httpx.Response(200, json={"id": OTHER_CONTAINER}),
                    httpx.Response(200, json={"id": THIRD_CONTAINER}),
                    httpx.Response(200, json={"id": PARENT_CONTAINER}),
                ]
            )
            stub_ready(
                network, CONTAINER, OTHER_CONTAINER, THIRD_CONTAINER, PARENT_CONTAINER
            )
            put_out = stub_publish(network)

            result = await platform.publish(
                account,
                Post(
                    text="Three of them",
                    media=(
                        a_picture(),
                        a_picture(OTHER_PICTURE_URL),
                        a_picture(THIRD_PICTURE_URL),
                    ),
                ),
            )

        assert build.call_count == 4
        assert sent_form(build, at=0) == {
            "image_url": PICTURE_URL,
            "is_carousel_item": "true",
        }
        assert sent_form(build, at=3) == {
            "caption": "Three of them",
            "media_type": "CAROUSEL",
            "children": f"{CONTAINER},{OTHER_CONTAINER},{THIRD_CONTAINER}",
        }
        assert sent_form(put_out) == {"creation_id": PARENT_CONTAINER}
        assert result.id == MEDIA_ID

    async def test_a_video_inside_a_carousel_is_not_a_reel(
        self,
        platform: InstagramPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network)
            build = network.post(f"/{IG_ID}/media").mock(
                side_effect=[
                    httpx.Response(200, json={"id": CONTAINER}),
                    httpx.Response(200, json={"id": OTHER_CONTAINER}),
                    httpx.Response(200, json={"id": PARENT_CONTAINER}),
                ]
            )
            stub_ready(network, CONTAINER, OTHER_CONTAINER, PARENT_CONTAINER)
            stub_publish(network)

            await platform.publish(account, Post(media=(a_picture(), a_video())))

        assert sent_form(build, at=1) == {
            "media_type": "VIDEO",
            "video_url": VIDEO_URL,
            "is_carousel_item": "true",
        }

    async def test_a_carousel_with_a_video_waits_on_every_piece_and_the_whole(
        self,
        platform: InstagramPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with respx.mock(base_url=IG_GRAPH_API, assert_all_called=False) as network:
            stub_quota(network)
            network.post(f"/{IG_ID}/media").mock(
                side_effect=[
                    httpx.Response(200, json={"id": CONTAINER}),
                    httpx.Response(200, json={"id": OTHER_CONTAINER}),
                    httpx.Response(200, json={"id": PARENT_CONTAINER}),
                ]
            )
            picture_watched = network.get(f"/{CONTAINER}").mock(
                return_value=httpx.Response(200, json={"status_code": "FINISHED"})
            )
            video_watched = network.get(f"/{OTHER_CONTAINER}").mock(
                return_value=httpx.Response(200, json={"status_code": "FINISHED"})
            )
            parent_watched = network.get(f"/{PARENT_CONTAINER}").mock(
                return_value=httpx.Response(200, json={"status_code": "FINISHED"})
            )
            stub_publish(network)

            await platform.publish(account, Post(media=(a_picture(), a_video())))

        assert picture_watched.called
        assert video_watched.called
        assert parent_watched.called

    async def test_asking_for_a_carousel_of_one_is_refused(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API, assert_all_called=False) as network,
            pytest.raises(InvalidPostError) as refused,
        ):
            stub_quota(network)
            await platform.publish(
                account,
                Post(media=(a_picture(),), options={"carousel": True}),
            )

        said = str(refused.value)
        assert "2" in said
        assert "10" in said

    async def test_more_than_ten_things_is_refused_before_anything_is_sent(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        too_much = Post(media=(a_picture(),) * 6 + (a_video(),) * 6)

        with (
            respx.mock(base_url=IG_GRAPH_API, assert_all_called=False) as network,
            pytest.raises(InvalidPostError) as refused,
        ):
            await platform.publish(account, too_much)

        assert "12" in str(refused.value)
        assert not network.calls

    async def test_a_carousel_can_be_asked_for_when_there_are_two_anyway(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network)
            build = network.post(f"/{IG_ID}/media").mock(
                side_effect=[
                    httpx.Response(200, json={"id": CONTAINER}),
                    httpx.Response(200, json={"id": OTHER_CONTAINER}),
                    httpx.Response(200, json={"id": PARENT_CONTAINER}),
                ]
            )
            stub_ready(network, CONTAINER, OTHER_CONTAINER, PARENT_CONTAINER)
            stub_publish(network)

            await platform.publish(
                account,
                Post(
                    media=(a_picture(), a_picture(OTHER_PICTURE_URL)),
                    options={"carousel": True},
                ),
            )

        assert sent_form(build, at=2)["media_type"] == "CAROUSEL"

    async def test_an_unknown_setting_is_refused_with_the_list(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(InvalidPostError) as refused:
            await platform.publish(
                account,
                Post(media=(a_picture(),), options={"filter": "sepia"}),
            )

        assert "carousel" in str(refused.value)

    async def test_a_setting_that_is_not_yes_or_no_is_refused(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(InvalidPostError) as refused:
            await platform.publish(
                account,
                Post(media=(a_picture(),), options={"carousel": "yes please"}),
            )

        assert "True or False" in str(refused.value)


# ---------------------------------------------------------------------------
# What it will not publish
# ---------------------------------------------------------------------------


class TestWhatItWillNotPublish:
    async def test_words_on_their_own_are_refused(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API, assert_all_called=False) as network,
            pytest.raises(NotSupportedError) as refused,
        ):
            await platform.publish(account, Post(text="Just words"))

        said = str(refused.value)
        assert "picture" in said
        assert "video" in said
        assert not network.calls

    async def test_a_file_from_disk_is_refused_with_the_reason(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API, assert_all_called=False) as network,
            pytest.raises(NotSupportedError) as refused,
        ):
            await platform.publish(
                account, Post(media=(Media.from_file("pictures/cake.jpg"),))
            )

        said = str(refused.value)
        assert "web address" in said
        assert "Media.from_url" in said
        assert not network.calls

    async def test_bytes_in_memory_are_refused_the_same_way(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        held = Media.from_bytes(b"not really a picture", filename="cake.jpg")

        with pytest.raises(NotSupportedError) as refused:
            await platform.publish(account, Post(media=(held,)))

        assert "Media.from_url" in str(refused.value)

    async def test_a_caption_over_the_limit_is_refused(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API, assert_all_called=False) as network,
            pytest.raises(InvalidPostError) as refused,
        ):
            await platform.publish(
                account,
                Post(text="x" * (MAX_CAPTION_LENGTH + 1), media=(a_picture(),)),
            )

        assert "2200" in str(refused.value)
        assert not network.calls

    async def test_too_many_hashtags_are_refused(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        many = " ".join(f"#tag{number}" for number in range(MOST_HASHTAGS + 1))

        with (
            respx.mock(base_url=IG_GRAPH_API, assert_all_called=False) as network,
            pytest.raises(InvalidPostError) as refused,
        ):
            await platform.publish(account, Post(text=many, media=(a_picture(),)))

        said = str(refused.value)
        assert "31" in said
        assert "30" in said
        assert not network.calls

    async def test_exactly_thirty_hashtags_are_fine(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        allowed = " ".join(f"#tag{number}" for number in range(MOST_HASHTAGS))

        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network)
            network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(200, json={"id": CONTAINER})
            )
            stub_ready(network, CONTAINER)
            stub_publish(network)

            result = await platform.publish(
                account, Post(text=allowed, media=(a_picture(),))
            )

        assert result.id == MEDIA_ID

    async def test_scheduling_is_refused_because_instagram_has_none(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        from datetime import UTC, timedelta

        later = Post(
            media=(a_picture(),), publish_at=datetime.now(UTC) + timedelta(hours=2)
        )

        with (
            respx.mock(base_url=IG_GRAPH_API, assert_all_called=False) as network,
            pytest.raises(NotSupportedError) as refused,
        ):
            await platform.publish(account, later)

        assert "scheduling" in str(refused.value)
        assert not network.calls

    async def test_replying_is_refused(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(NotSupportedError):
            await platform.publish(
                account, Post(media=(a_picture(),), reply_to="17999")
            )


# ---------------------------------------------------------------------------
# How many posts are left today
# ---------------------------------------------------------------------------


class TestTheDailyLimit:
    async def test_it_reads_the_number_rather_than_writing_one_down(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=IG_GRAPH_API) as network:
            asked = stub_quota(network)
            limits = await platform.limits(account)

        assert limits == Limits(
            max_text_length=MAX_CAPTION_LENGTH,
            text_counted_in=TextCount.CHARACTERS,
            max_images=MOST_IN_A_CAROUSEL,
            max_videos=MOST_IN_A_CAROUSEL,
            posts_left_today=96,
        )
        assert asked.calls[-1].request.url.params["fields"] == "config,quota_usage"

    async def test_a_post_with_none_left_today_is_refused(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API, assert_all_called=False) as network,
            pytest.raises(InvalidPostError) as refused,
        ):
            stub_quota(network, USED_UP)
            build = network.post(f"/{IG_ID}/media")
            await platform.publish(account, Post(media=(a_picture(),)))

        assert "daily limit" in str(refused.value)
        assert not build.called

    async def test_meta_counting_past_its_own_total_still_means_none_left(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        over = {"data": [{"quota_usage": 105, "config": {"quota_total": 100}}]}

        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network, over)
            limits = await platform.limits(account)

        assert limits.posts_left_today == 0

    @pytest.mark.parametrize(
        "reply",
        [
            {},
            {"data": []},
            {"data": ["not an entry"]},
            {"data": [{"quota_usage": 4}]},
            {"data": [{"config": {"quota_total": 100}}]},
            {"data": [{"quota_usage": 4, "config": {"quota_total": "lots"}}]},
        ],
        ids=["nothing", "empty", "wrong-shape", "no-total", "no-usage", "not-a-number"],
    )
    async def test_a_reply_we_cannot_read_means_we_do_not_know(
        self,
        platform: InstagramPlatform,
        account: Connection,
        reply: dict[str, Any],
    ) -> None:
        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network, reply)
            limits = await platform.limits(account)

        assert limits.posts_left_today is None

    async def test_not_knowing_does_not_stop_a_post(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=IG_GRAPH_API) as network:
            stub_quota(network, {})
            network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(200, json={"id": CONTAINER})
            )
            stub_ready(network, CONTAINER)
            stub_publish(network)

            result = await platform.publish(account, Post(media=(a_picture(),)))

        assert result.id == MEDIA_ID


# ---------------------------------------------------------------------------
# When Instagram says no
# ---------------------------------------------------------------------------


class TestWhenInstagramSaysNo:
    async def test_a_file_it_could_not_fetch_says_what_to_check(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API) as network,
            pytest.raises(InvalidPostError) as refused,
        ):
            stub_quota(network)
            network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(400, json=an_error(9004))
            )
            await platform.publish(account, Post(media=(a_picture(),)))

        said = str(refused.value)
        assert "9004" in said
        assert "reachable" in said

    async def test_a_video_in_the_wrong_format_names_the_right_one(
        self,
        platform: InstagramPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API) as network,
            pytest.raises(InvalidPostError) as refused,
        ):
            stub_quota(network)
            network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(400, json=an_error(2207026))
            )
            await platform.publish(account, Post(media=(a_video(),)))

        said = str(refused.value)
        assert "MP4" in said
        assert "H.264" in said

    async def test_the_same_trouble_hidden_in_a_subcode_is_named_too(
        self,
        platform: InstagramPlatform,
        account: Connection,
        clock: dict[str, datetime],
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API) as network,
            pytest.raises(InvalidPostError) as refused,
        ):
            stub_quota(network)
            network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(400, json=an_error(-1, subcode=2207026))
            )
            await platform.publish(account, Post(media=(a_video(),)))

        assert "MP4" in str(refused.value)

    async def test_instagrams_own_shrug_is_passed_on_honestly(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API) as network,
            pytest.raises(PlatformError) as refused,
        ):
            stub_quota(network)
            network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(400, json=an_error(24))
            )
            await platform.publish(account, Post(media=(a_picture(),)))

        said = str(refused.value)
        assert "24" in said
        assert "without saying what" in said

    async def test_a_refusal_meta_shares_with_facebook_keeps_metas_words(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API) as network,
            pytest.raises(RateLimitError),
        ):
            stub_quota(network)
            network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(400, json=an_error(4))
            )
            await platform.publish(account, Post(media=(a_picture(),)))

    async def test_a_refusal_hidden_in_a_happy_reply_is_still_caught(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API) as network,
            pytest.raises(InvalidPostError) as refused,
        ):
            stub_quota(network)
            network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(200, json=an_error(9004))
            )
            await platform.publish(account, Post(media=(a_picture(),)))

        assert "reachable" in str(refused.value)

    async def test_a_code_nobody_has_named_still_comes_back_as_ours(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        with (
            respx.mock(base_url=IG_GRAPH_API) as network,
            pytest.raises(PlatformError) as refused,
        ):
            stub_quota(network)
            network.post(f"/{IG_ID}/media").mock(
                return_value=httpx.Response(200, json=an_error(987_654))
            )
            await platform.publish(account, Post(media=(a_picture(),)))

        assert "987654" in str(refused.value)

    def test_a_reply_with_no_error_object_falls_back_to_the_shared_names(
        self,
    ) -> None:
        refused = instagram_errors(httpx.Response(404, json={"nothing": "here"}))

        assert isinstance(refused, NotFoundError)
        assert "instagram" in str(refused)

    def test_it_says_nothing_about_the_allowance_until_a_reply_does(
        self,
        platform: InstagramPlatform,
    ) -> None:
        assert platform.usage is None

    async def test_it_remembers_how_much_of_the_allowance_is_gone(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        headers = {"X-App-Usage": json.dumps({"call_count": 42})}

        with respx.mock(base_url=IG_GRAPH_API) as network:
            network.get(f"/{IG_ID}/content_publishing_limit").mock(
                return_value=httpx.Response(200, json=QUOTA, headers=headers)
            )
            await platform.limits(account)

        assert platform.usage is not None
        assert platform.usage.calls == 42

    async def test_a_reply_that_says_nothing_leaves_the_last_word_alone(
        self,
        platform: InstagramPlatform,
        account: Connection,
    ) -> None:
        headers = {"X-App-Usage": json.dumps({"call_count": 42})}

        with respx.mock(base_url=IG_GRAPH_API) as network:
            quiet = network.get(f"/{IG_ID}/content_publishing_limit")
            quiet.mock(
                side_effect=[
                    httpx.Response(200, json=QUOTA, headers=headers),
                    httpx.Response(200, json=QUOTA),
                ]
            )
            await platform.limits(account)
            await platform.limits(account)

        assert platform.usage is not None
        assert platform.usage.calls == 42


# ---------------------------------------------------------------------------
# Requests Instagram pushes to us
# ---------------------------------------------------------------------------


class TestRequestsInstagramPushesToUs:
    def test_a_properly_signed_request_is_accepted(
        self,
        platform: InstagramPlatform,
    ) -> None:
        body = a_push({"id": "17888", "text": "Lovely"})

        platform.check_signature(body, signed(body), secret=APP_SECRET)

    def test_a_tampered_request_is_refused(
        self,
        platform: InstagramPlatform,
    ) -> None:
        body = a_push({"id": "17888", "text": "Lovely"})
        headers = signed(body)

        with pytest.raises(SignatureError):
            platform.check_signature(b'{"entry": []}', headers, secret=APP_SECRET)

    def test_a_request_signed_with_the_wrong_secret_is_refused(
        self,
        platform: InstagramPlatform,
    ) -> None:
        body = a_push({"id": "17888"})

        with pytest.raises(SignatureError):
            platform.check_signature(
                body, signed(body, secret="someone-elses"), secret=APP_SECRET
            )

    def test_a_typed_caller_can_reach_the_setup_check_and_the_updates(
        self,
        platform: InstagramPlatform,
    ) -> None:
        assert isinstance(platform, CanAnswerSetupCheck)
        assert isinstance(platform, CanReadPushedUpdates)

    def test_it_answers_the_one_off_setup_check(
        self,
        platform: InstagramPlatform,
    ) -> None:
        answer = platform.answer_setup_check(
            {
                "hub.mode": "subscribe",
                "hub.challenge": "1158201444",
                "hub.verify_token": "chosen-by-you",
            },
            verify_token="chosen-by-you",
        )

        assert answer == "1158201444"

    def test_a_setup_check_with_the_wrong_token_is_refused(
        self,
        platform: InstagramPlatform,
    ) -> None:
        with pytest.raises(SignatureError):
            platform.answer_setup_check(
                {
                    "hub.mode": "subscribe",
                    "hub.challenge": "1158201444",
                    "hub.verify_token": "a-guess",
                },
                verify_token="chosen-by-you",
            )

    def test_a_new_comment_arrives_as_a_comment(
        self,
        platform: InstagramPlatform,
    ) -> None:
        body = a_push(
            {
                "id": "17888",
                "text": "Lovely",
                "from": {"id": "555", "username": "someone"},
                "media": {"id": MEDIA_ID},
            }
        )

        update = platform.read_update(body, {})

        assert update.kind is UpdateKind.COMMENT_CREATED
        assert update.platform == "instagram"
        assert update.connection_id == f"instagram:{IG_ID}"
        assert update.created_at == datetime.fromtimestamp(1_790_000_000, UTC)
        assert "17888" in update.id

    def test_the_same_comment_twice_has_the_same_id_both_times(
        self,
        platform: InstagramPlatform,
    ) -> None:
        body = a_push({"id": "17888", "text": "Lovely"})

        first = platform.read_update(body, {})
        again = platform.read_update(body, {})

        assert first.id == again.id

    def test_a_comment_on_a_live_video_is_a_comment_too(
        self,
        platform: InstagramPlatform,
    ) -> None:
        body = a_push({"id": "17999"}, topic="live_comments")

        assert platform.read_update(body, {}).kind is UpdateKind.COMMENT_CREATED

    def test_being_named_in_somebody_elses_post_is_a_mention(
        self,
        platform: InstagramPlatform,
    ) -> None:
        body = a_push({"comment_id": "17777", "media_id": MEDIA_ID}, topic="mentions")

        update = platform.read_update(body, {})

        assert update.kind is UpdateKind.MENTION
        assert "17777" in update.id

    def test_numbers_about_a_story_keep_instagrams_own_word(
        self,
        platform: InstagramPlatform,
    ) -> None:
        body = a_push({"media_id": MEDIA_ID, "impressions": 44}, topic="story_insights")

        update = platform.read_update(body, {})

        assert update.kind is UpdateKind.UNKNOWN
        assert update.kind_name == "story_insights"
        assert MEDIA_ID in update.id

    def test_a_change_with_nothing_to_name_it_by_is_still_delivered(
        self,
        platform: InstagramPlatform,
    ) -> None:
        body = a_push({}, topic="comments")

        update = platform.read_update(body, {})

        assert update.id.startswith(f"{IG_ID}:comments:")

    def test_a_busy_moment_hands_back_every_change(
        self,
        platform: InstagramPlatform,
    ) -> None:
        body = json.dumps(
            {
                "object": "instagram",
                "entry": [
                    {
                        "id": IG_ID,
                        "time": 1_790_000_000,
                        "changes": [
                            {"field": "comments", "value": {"id": "1"}},
                            {"field": "comments", "value": {"id": "2"}},
                        ],
                    }
                ],
            }
        ).encode()

        first, second = platform.read_updates(body)

        assert first.raw == {"id": "1"}
        assert second.raw == {"id": "2"}
        assert first.envelope["id"] == IG_ID

    def test_a_message_with_nothing_in_it_is_not_an_error(
        self,
        platform: InstagramPlatform,
    ) -> None:
        assert platform.read_updates(b'{"object": "instagram"}') == []

    def test_asking_for_one_update_where_there_is_none_says_so(
        self,
        platform: InstagramPlatform,
    ) -> None:
        with pytest.raises(PlatformError) as refused:
            platform.read_update(b'{"object": "instagram"}', {})

        assert "read_updates" in str(refused.value)


# ---------------------------------------------------------------------------
# The checks every platform has to pass
# ---------------------------------------------------------------------------


class TestInstagramBehavesLikeTheOthers(PlatformChecks):
    def make_platform(self) -> Platform:
        return InstagramPlatform(transport=self.transport, retries=ONCE)

    def make_connection(self) -> Connection | None:
        return an_account()

    def make_post(self, text: str) -> Post:
        return Post(text=text, media=(Media.from_url(PICTURE_URL),))

    def make_transport(self) -> httpx.AsyncBaseTransport | None:
        return RecordingTransport(
            {
                f"GET /v21.0/{IG_ID}/content_publishing_limit": QUOTA,
                f"POST /v21.0/{IG_ID}/media": {"id": CONTAINER},
                f"POST /v21.0/{IG_ID}/media_publish": {"id": MEDIA_ID},
            }
        )
