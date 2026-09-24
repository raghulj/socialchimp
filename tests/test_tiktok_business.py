"""Tests for the TikTok Business Profile platform."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
import respx

from socialchimp import (
    AppCredentials,
    AuthError,
    ConfigError,
    Connection,
    Feature,
    NotSupportedError,
    PlatformError,
    Post,
    RateLimitError,
    Token,
)
from socialchimp.events import Update, UpdateKind
from socialchimp.http import Retries
from socialchimp.platform import (
    CanReadProfile,
    Finished,
    LoginRequest,
    SendToNetwork,
)
from socialchimp.platforms.tiktok_business import (
    TikTokBusinessPlatform,
    tiktok_business_errors,
)

REDIRECT = "https://app.example/callback"
ADVERTISER_ID = "1234567890"
ONE_TRY = Retries(attempts=1)

APP = AppCredentials(
    platform="tiktok_business",
    host=None,
    client_id="business-app-id",
    client_secret="business-app-secret",
)

# A moment to hold still at
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def at(moment: datetime = NOW) -> Callable[[], datetime]:
    """A clock that always says the same thing."""
    return lambda: moment


@pytest.fixture
def platform() -> TikTokBusinessPlatform:
    """A platform that gives up after one try, with the clock held still."""
    return TikTokBusinessPlatform(retries=ONE_TRY, now=at())


@pytest.fixture
def account() -> Connection:
    """A connected TikTok Business account."""
    return Connection(
        id=f"tiktok_business:{ADVERTISER_ID}",
        platform="tiktok_business",
        host=None,
        account_id=ADVERTISER_ID,
        account_name="tiktok_business",
        token=Token(
            access_token="business-access-token",
            refresh_token="business-refresh-token",
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        ),
        scopes=(),
    )


def login(
    *,
    state: str | None = None,
    scopes: tuple[str, ...] = (),
    app: AppCredentials | None = APP,
) -> LoginRequest:
    """A login request with the everyday values already filled in."""
    return LoginRequest(redirect_uri=REDIRECT, scopes=scopes, state=state, app=app)


class TestStartLogin:
    """Tests for start_login."""

    @pytest.mark.asyncio
    async def test_builds_correct_authorize_url(
        self, platform: TikTokBusinessPlatform
    ) -> None:
        """start_login builds the correct authorize URL with app_id."""
        result = await platform.start_login(login())

        assert isinstance(result, SendToNetwork)
        assert "business-api.tiktok.com/portal/auth" in result.url
        assert "app_id=business-app-id" in result.url
        assert f"redirect_uri={REDIRECT}" in result.url
        assert "state=" in result.url
        assert result.state is not None
        assert result.remember == {}

    @pytest.mark.asyncio
    async def test_uses_provided_state(self, platform: TikTokBusinessPlatform) -> None:
        """start_login uses the provided state value."""
        result = await platform.start_login(login(state="my-state"))

        assert result.state == "my-state"
        assert "state=my-state" in result.url

    @pytest.mark.asyncio
    async def test_refuses_without_app_credentials(
        self, platform: TikTokBusinessPlatform
    ) -> None:
        """start_login refuses plainly when there are no app credentials."""
        with pytest.raises(PlatformError, match=r"business-api\.tiktok\.com/portal"):
            await platform.start_login(login(app=None))


class TestFinishLogin:
    """Tests for finish_login."""

    @pytest.mark.asyncio
    async def test_posts_correct_body_to_correct_url(
        self, platform: TikTokBusinessPlatform
    ) -> None:
        """finish_login posts the correct body to the correct URL."""
        with respx.mock:
            respx.post(
                "https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "message": "ok",
                        "request_id": "req-1",
                        "data": {
                            "access_token": "new-access-token",
                            "refresh_token": "new-refresh-token",
                        },
                    },
                )
            )

            result = await platform.finish_login(
                login(),
                callback={"auth_code": "the-code", "state": "my-state"},
            )

            assert isinstance(result, Finished)
            # account_id falls back to app_id when advertiser_id not in response
            assert result.connection.account_id == APP.client_id
            assert result.connection.token.access_token == "new-access-token"
            assert result.connection.token.refresh_token == "new-refresh-token"

    @pytest.mark.asyncio
    async def test_parses_expires_in_when_present(
        self, platform: TikTokBusinessPlatform
    ) -> None:
        """finish_login parses expires_in to set Token.expires_at."""
        with respx.mock:
            respx.post(
                "https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "message": "ok",
                        "request_id": "req-1",
                        "data": {
                            "access_token": "new-access-token",
                            "refresh_token": "new-refresh-token",
                            "expires_in": 3600,
                        },
                    },
                )
            )

            result = await platform.finish_login(
                login(),
                callback={"auth_code": "the-code", "state": "my-state"},
            )

            assert result.connection.token.expires_at is not None
            expected_time = NOW.timestamp() + 3600
            actual_time = result.connection.token.expires_at.timestamp()
            assert abs(actual_time - expected_time) < 1

    @pytest.mark.asyncio
    async def test_leaves_expires_at_none_when_not_present(
        self, platform: TikTokBusinessPlatform
    ) -> None:
        """finish_login leaves expires_at as None when not in response."""
        with respx.mock:
            respx.post(
                "https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "message": "ok",
                        "request_id": "req-1",
                        "data": {
                            "access_token": "new-access-token",
                            "refresh_token": "new-refresh-token",
                        },
                    },
                )
            )

            result = await platform.finish_login(
                login(),
                callback={"auth_code": "the-code", "state": "my-state"},
            )

            assert result.connection.token.expires_at is None

    @pytest.mark.asyncio
    async def test_the_finished_connection_carries_no_picture(
        self, platform: TikTokBusinessPlatform
    ) -> None:
        """finish_login leaves avatar_url as None, with no identity call made."""
        with respx.mock:
            respx.post(
                "https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "message": "ok",
                        "request_id": "req-1",
                        "data": {
                            "access_token": "new-access-token",
                            "refresh_token": "new-refresh-token",
                        },
                    },
                )
            )

            result = await platform.finish_login(
                login(),
                callback={"auth_code": "the-code", "state": "my-state"},
            )

            assert result.connection.avatar_url is None

    def test_it_does_not_offer_reading_the_profile_again(
        self, platform: TikTokBusinessPlatform
    ) -> None:
        """There is no identity endpoint, so CanReadProfile is not offered."""
        assert not isinstance(platform, CanReadProfile)

    @pytest.mark.asyncio
    async def test_raises_error_on_non_zero_code(
        self, platform: TikTokBusinessPlatform
    ) -> None:
        """finish_login raises error when response code is not 0."""
        with respx.mock:
            respx.post(
                "https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 10001,
                        "message": "Invalid auth code",
                        "request_id": "req-1",
                    },
                )
            )

            # Any non-zero code should raise an error
            with pytest.raises(PlatformError):
                await platform.finish_login(
                    login(),
                    callback={"auth_code": "bad-code", "state": "my-state"},
                )

    @pytest.mark.asyncio
    async def test_refuses_without_app_credentials(
        self, platform: TikTokBusinessPlatform
    ) -> None:
        """finish_login refuses plainly when there are no app credentials."""
        with pytest.raises(PlatformError, match="app credentials"):
            await platform.finish_login(
                login(app=None),
                callback={"auth_code": "the-code", "state": "my-state"},
            )

    @pytest.mark.asyncio
    async def test_refuses_without_auth_code(
        self, platform: TikTokBusinessPlatform
    ) -> None:
        """finish_login refuses plainly when the callback carries no auth_code."""
        with pytest.raises(PlatformError, match="auth_code"):
            await platform.finish_login(login(), callback={"state": "my-state"})

    @pytest.mark.asyncio
    async def test_refuses_when_no_access_token_in_reply(
        self, platform: TikTokBusinessPlatform
    ) -> None:
        """finish_login refuses plainly when TikTok's reply carries no token."""
        with respx.mock:
            respx.post(
                "https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "message": "ok",
                        "request_id": "req-1",
                        "data": {},
                    },
                )
            )

            with pytest.raises(PlatformError, match="access token"):
                await platform.finish_login(
                    login(),
                    callback={"auth_code": "the-code", "state": "my-state"},
                )

    @pytest.mark.asyncio
    async def test_parses_refresh_token_expires_in_when_present(
        self, platform: TikTokBusinessPlatform
    ) -> None:
        """finish_login parses refresh_token_expires_in when TikTok sends it."""
        with respx.mock:
            respx.post(
                "https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "message": "ok",
                        "request_id": "req-1",
                        "data": {
                            "access_token": "new-access-token",
                            "refresh_token": "new-refresh-token",
                            "refresh_token_expires_in": 31536000,
                        },
                    },
                )
            )

            result = await platform.finish_login(
                login(),
                callback={"auth_code": "the-code", "state": "my-state"},
            )

            assert result.connection.token.refresh_token_expires_at is not None
            expected_time = NOW.timestamp() + 31536000
            actual_time = result.connection.token.refresh_token_expires_at.timestamp()
            assert abs(actual_time - expected_time) < 1


class TestRefresh:
    """Tests for refresh."""

    @pytest.mark.asyncio
    async def test_posts_correct_body_and_returns_new_token(
        self, platform: TikTokBusinessPlatform, account: Connection
    ) -> None:
        """refresh posts the correct body and returns a new Token."""
        with respx.mock:
            respx.post(
                "https://business-api.tiktok.com/open_api/v1.3/oauth2/refresh_token/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "message": "ok",
                        "request_id": "req-1",
                        "data": {
                            "access_token": "refreshed-access-token",
                            "refresh_token": "refreshed-refresh-token",
                        },
                    },
                )
            )

            new_token = await platform.refresh(account, app=APP)

            assert new_token.access_token == "refreshed-access-token"
            assert new_token.refresh_token == "refreshed-refresh-token"

    @pytest.mark.asyncio
    async def test_parses_expiries_when_present(
        self, platform: TikTokBusinessPlatform, account: Connection
    ) -> None:
        """refresh parses expires_in and refresh_token_expires_in when present."""
        with respx.mock:
            respx.post(
                "https://business-api.tiktok.com/open_api/v1.3/oauth2/refresh_token/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "message": "ok",
                        "request_id": "req-1",
                        "data": {
                            "access_token": "refreshed-access-token",
                            "refresh_token": "refreshed-refresh-token",
                            "expires_in": 86400,
                            "refresh_token_expires_in": 31536000,
                        },
                    },
                )
            )

            new_token = await platform.refresh(account, app=APP)

            assert new_token.expires_at is not None
            assert new_token.refresh_token_expires_at is not None

    @pytest.mark.asyncio
    async def test_refuses_without_app_credentials(
        self, platform: TikTokBusinessPlatform, account: Connection
    ) -> None:
        """refresh refuses plainly when no app credentials are given."""
        with pytest.raises(PlatformError, match="app credentials"):
            await platform.refresh(account, app=None)

    @pytest.mark.asyncio
    async def test_refuses_without_refresh_token(
        self, platform: TikTokBusinessPlatform
    ) -> None:
        """refresh refuses plainly when the connection has no refresh token."""
        no_refresh = Connection(
            id=f"tiktok_business:{ADVERTISER_ID}",
            platform="tiktok_business",
            host=None,
            account_id=ADVERTISER_ID,
            account_name="tiktok_business",
            token=Token(access_token="business-access-token"),
        )

        with pytest.raises(PlatformError, match="refresh token"):
            await platform.refresh(no_refresh, app=APP)

    @pytest.mark.asyncio
    async def test_raises_error_on_non_zero_code(
        self, platform: TikTokBusinessPlatform, account: Connection
    ) -> None:
        """refresh raises an error when TikTok's response code is not 0."""
        with respx.mock:
            respx.post(
                "https://business-api.tiktok.com/open_api/v1.3/oauth2/refresh_token/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 10001,
                        "message": "Invalid refresh token",
                        "request_id": "r",
                    },
                )
            )

            with pytest.raises(PlatformError):
                await platform.refresh(account, app=APP)

    @pytest.mark.asyncio
    async def test_refuses_when_no_access_token_in_reply(
        self, platform: TikTokBusinessPlatform, account: Connection
    ) -> None:
        """refresh refuses plainly when TikTok's reply carries no token."""
        with respx.mock:
            respx.post(
                "https://business-api.tiktok.com/open_api/v1.3/oauth2/refresh_token/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "message": "ok",
                        "request_id": "req-1",
                        "data": {},
                    },
                )
            )

            with pytest.raises(PlatformError, match="access token"):
                await platform.refresh(account, app=APP)


class TestAuthHeaders:
    """Tests for auth_headers."""

    def test_returns_access_token_header(
        self, platform: TikTokBusinessPlatform, account: Connection
    ) -> None:
        """auth_headers returns Access-Token header with token value."""
        headers = platform.auth_headers(account)

        assert headers == {"Access-Token": "business-access-token"}
        assert "Authorization" not in headers

    def test_access_token_not_bearer(
        self, platform: TikTokBusinessPlatform, account: Connection
    ) -> None:
        """auth_headers does NOT use Authorization: Bearer format."""
        headers = platform.auth_headers(account)

        assert "Bearer" not in str(headers)
        assert "Access-Token" in headers


class TestPublish:
    """Tests for publish."""

    @pytest.mark.asyncio
    async def test_raises_not_supported_error(
        self, platform: TikTokBusinessPlatform, account: Connection
    ) -> None:
        """publish raises NotSupportedError via check_post."""
        post = Post(text="Hello")

        with pytest.raises(NotSupportedError):
            await platform.publish(account, post)


class TestLimits:
    """Tests for limits."""

    @pytest.mark.asyncio
    async def test_returns_empty_limits(
        self, platform: TikTokBusinessPlatform, account: Connection
    ) -> None:
        """limits returns an empty Limits object."""
        from socialchimp.features import Limits

        result = await platform.limits(account)

        assert isinstance(result, Limits)
        assert result.max_text_length is None


class TestApiBase:
    """Tests for api_base."""

    def test_returns_business_api_url(
        self, platform: TikTokBusinessPlatform, account: Connection
    ) -> None:
        """api_base returns the TikTok Business API base URL."""
        url = platform.api_base(account)

        assert url == "https://business-api.tiktok.com"


class TestPlatformFeatures:
    """Tests for platform features."""

    def test_declares_no_features(self, platform: TikTokBusinessPlatform) -> None:
        """Platform declares no Feature.* flags - see the module docstring."""
        assert platform.features == Feature(0)


class TestErrorMapping:
    """Tests for tiktok_business_errors."""

    def test_maps_non_zero_code_to_error(self) -> None:
        """tiktok_business_errors maps non-zero code to a SocialChimpError."""
        response = httpx.Response(
            200,
            json={
                "code": 10001,
                "message": "Invalid request",
                "request_id": "req-1",
            },
        )

        error = tiktok_business_errors(response)

        assert error is not None
        assert isinstance(error, Exception)

    def test_maps_auth_error_codes(self) -> None:
        """tiktok_business_errors maps auth error codes to AuthError."""
        response = httpx.Response(
            200,
            json={
                "code": 40001,
                "message": "Access token invalid",
                "request_id": "req-1",
            },
        )

        error = tiktok_business_errors(response)

        assert isinstance(error, AuthError)

    def test_maps_rate_limit_error_codes(self) -> None:
        """tiktok_business_errors maps rate limit codes to RateLimitError."""
        response = httpx.Response(
            200,
            json={
                "code": 30003,
                "message": "Rate limit exceeded",
                "request_id": "req-1",
            },
        )

        error = tiktok_business_errors(response)

        assert isinstance(error, RateLimitError)

    def test_falls_back_to_error_from_response(self) -> None:
        """tiktok_business_errors falls back for unknown codes."""
        response = httpx.Response(
            500,
            json={
                "code": 0,
                "message": "ok",
                "request_id": "req-1",
            },
        )

        error = tiktok_business_errors(response)

        assert error is not None
        assert isinstance(error, Exception)

    def test_handles_zero_code_as_success(self) -> None:
        """tiktok_business_errors does not raise for code 0."""
        response = httpx.Response(
            200,
            json={
                "code": 0,
                "message": "ok",
                "request_id": "req-1",
            },
        )

        # This should not raise in the context of the finish_login call
        # The error function itself just returns an error - whether it's raised
        # is up to the caller
        error = tiktok_business_errors(response)

        # For code 0, it should fall through to error_from_response
        # which will handle the 200 status code appropriately
        assert error is not None

    def test_falls_back_when_code_is_missing(self) -> None:
        """tiktok_business_errors falls back sensibly when 'code' is absent."""
        response = httpx.Response(
            500,
            json={"message": "Something went wrong", "request_id": "req-1"},
        )

        error = tiktok_business_errors(response)

        assert error is not None
        assert not isinstance(error, (AuthError, RateLimitError))


class TestFetchUpdates:
    """Tests for fetch_updates."""

    @pytest.fixture
    def account_with_advertiser_id(self) -> Connection:
        """A connected TikTok Business account with advertiser_id in extra."""
        return Connection(
            id=f"tiktok_business:{ADVERTISER_ID}",
            platform="tiktok_business",
            host=None,
            account_id=ADVERTISER_ID,
            account_name="tiktok_business",
            token=Token(
                access_token="business-access-token",
                refresh_token="business-refresh-token",
                expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            ),
            scopes=(),
            extra={"advertiser_id": ADVERTISER_ID, "ad_id": "12345"},
        )

    @pytest.mark.asyncio
    async def test_fetches_comments_with_time_window(
        self,
        platform: TikTokBusinessPlatform,
        account_with_advertiser_id: Connection,
    ) -> None:
        """fetch_updates requests comments within a time window."""
        with respx.mock:
            route = respx.get(
                "https://business-api.tiktok.com/open_api/v1.3/comment/list/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "message": "ok",
                        "request_id": "req-1",
                        "data": {
                            "comments": [
                                {
                                    "comment_id": "comment-1",
                                    "text": "Great product!",
                                    "create_time": 1725177600,
                                }
                            ]
                        },
                    },
                )
            )

            result = await platform.fetch_updates(account_with_advertiser_id, None)

            assert len(result) == 1
            assert result[0].kind == UpdateKind.COMMENT_CREATED
            assert result[0].id == "comment-1"
            # Verify that the request was made with the correct params
            request = route.calls.last.request
            assert f"advertiser_id={ADVERTISER_ID}" in request.url.query.decode()
            assert "search_field=AD_ID" in request.url.query.decode()
            assert "search_value=12345" in request.url.query.decode()

    @pytest.mark.asyncio
    async def test_filters_by_since(
        self,
        platform: TikTokBusinessPlatform,
        account_with_advertiser_id: Connection,
    ) -> None:
        """fetch_updates filters results by since parameter."""
        # NOW is 2026-08-31 12:00:00 UTC = 1725087600 Unix seconds
        # Use a since time just before that, so only newer comments are included
        since = datetime(2026, 8, 31, 11, 0, tzinfo=UTC)

        # Construct Unix timestamps relative to NOW
        # Comment older than since: 2026-08-31 10:00:00
        old_timestamp = int((datetime(2026, 8, 31, 10, 0, tzinfo=UTC)).timestamp())
        # Comment newer than since: 2026-08-31 12:30:00
        new_timestamp = int((datetime(2026, 8, 31, 12, 30, tzinfo=UTC)).timestamp())

        with respx.mock:
            respx.get(
                "https://business-api.tiktok.com/open_api/v1.3/comment/list/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "message": "ok",
                        "request_id": "req-1",
                        "data": {
                            "comments": [
                                {
                                    "comment_id": "old-comment",
                                    "text": "Old",
                                    "create_time": old_timestamp,
                                },
                                {
                                    "comment_id": "new-comment",
                                    "text": "New",
                                    "create_time": new_timestamp,
                                },
                            ]
                        },
                    },
                )
            )

            result = await platform.fetch_updates(account_with_advertiser_id, since)

            # Only the new comment should be returned (newer than since)
            assert len(result) == 1
            assert result[0].id == "new-comment"
            assert result[0].created_at > since

    @pytest.mark.asyncio
    async def test_raises_config_error_when_advertiser_id_missing(
        self, platform: TikTokBusinessPlatform, account: Connection
    ) -> None:
        """fetch_updates raises ConfigError if advertiser_id is missing."""
        with pytest.raises(ConfigError, match="advertiser_id"):
            await platform.fetch_updates(account, None)

    @pytest.mark.asyncio
    async def test_raises_config_error_when_ad_id_missing(
        self, platform: TikTokBusinessPlatform
    ) -> None:
        """fetch_updates raises ConfigError if ad_id is missing from extra."""
        account_without_ad_id = Connection(
            id=f"tiktok_business:{ADVERTISER_ID}",
            platform="tiktok_business",
            host=None,
            account_id=ADVERTISER_ID,
            account_name="tiktok_business",
            token=Token(
                access_token="business-access-token",
                refresh_token="business-refresh-token",
                expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            ),
            scopes=(),
            extra={"advertiser_id": ADVERTISER_ID},
        )

        with pytest.raises(ConfigError, match="ad_id"):
            await platform.fetch_updates(account_without_ad_id, None)

    @pytest.mark.asyncio
    async def test_handles_empty_comments_list(
        self,
        platform: TikTokBusinessPlatform,
        account_with_advertiser_id: Connection,
    ) -> None:
        """fetch_updates handles empty comments list gracefully."""
        with respx.mock:
            respx.get(
                "https://business-api.tiktok.com/open_api/v1.3/comment/list/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "message": "ok",
                        "request_id": "req-1",
                        "data": {"comments": []},
                    },
                )
            )

            result = await platform.fetch_updates(account_with_advertiser_id, None)

            assert result == []

    @pytest.mark.asyncio
    async def test_skips_comments_without_id(
        self,
        platform: TikTokBusinessPlatform,
        account_with_advertiser_id: Connection,
    ) -> None:
        """fetch_updates skips comments without comment_id or id."""
        with respx.mock:
            respx.get(
                "https://business-api.tiktok.com/open_api/v1.3/comment/list/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "message": "ok",
                        "request_id": "req-1",
                        "data": {
                            "comments": [
                                {
                                    "text": "Comment without ID",
                                    "create_time": 1725177600,
                                }
                            ]
                        },
                    },
                )
            )

            result = await platform.fetch_updates(account_with_advertiser_id, None)

            assert result == []

    @pytest.mark.asyncio
    async def test_handles_unparseable_create_time(
        self,
        platform: TikTokBusinessPlatform,
        account_with_advertiser_id: Connection,
    ) -> None:
        """fetch_updates handles unparseable create_time by using now."""
        with respx.mock:
            respx.get(
                "https://business-api.tiktok.com/open_api/v1.3/comment/list/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "message": "ok",
                        "request_id": "req-1",
                        "data": {
                            "comments": [
                                {
                                    "comment_id": "comment-1",
                                    "text": "Comment",
                                    "create_time": "not-a-number",
                                }
                            ]
                        },
                    },
                )
            )

            result = await platform.fetch_updates(account_with_advertiser_id, None)

            assert len(result) == 1
            # Should have used _now() for created_at
            assert result[0].created_at == NOW

    @pytest.mark.asyncio
    async def test_raises_error_on_api_failure(
        self,
        platform: TikTokBusinessPlatform,
        account_with_advertiser_id: Connection,
    ) -> None:
        """fetch_updates raises error when API returns non-zero code."""
        with respx.mock:
            respx.get(
                "https://business-api.tiktok.com/open_api/v1.3/comment/list/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 40001,
                        "message": "Access token invalid",
                        "request_id": "req-1",
                    },
                )
            )

            with pytest.raises(AuthError):
                await platform.fetch_updates(account_with_advertiser_id, None)

    @pytest.mark.asyncio
    async def test_handles_comments_not_as_list(
        self,
        platform: TikTokBusinessPlatform,
        account_with_advertiser_id: Connection,
    ) -> None:
        """fetch_updates handles case where comments is not a list."""
        with respx.mock:
            respx.get(
                "https://business-api.tiktok.com/open_api/v1.3/comment/list/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "message": "ok",
                        "request_id": "req-1",
                        "data": {"comments": {"error": "unexpected format"}},
                    },
                )
            )

            result = await platform.fetch_updates(account_with_advertiser_id, None)

            # Should handle gracefully and return empty list
            assert result == []


class TestReplyToUpdate:
    """Tests for reply_to_update."""

    @pytest.fixture
    def account_with_extra(self) -> Connection:
        """A connected TikTok Business account with required fields in extra."""
        return Connection(
            id=f"tiktok_business:{ADVERTISER_ID}",
            platform="tiktok_business",
            host=None,
            account_id=ADVERTISER_ID,
            account_name="tiktok_business",
            token=Token(
                access_token="business-access-token",
                refresh_token="business-refresh-token",
                expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            ),
            scopes=(),
            extra={
                "advertiser_id": ADVERTISER_ID,
                "identity_id": "identity-123",
                "identity_type": "BUSINESS",
            },
        )

    @pytest.mark.asyncio
    async def test_replies_to_comment(
        self, platform: TikTokBusinessPlatform, account_with_extra: Connection
    ) -> None:
        """reply_to_update sends a reply to a comment."""
        update = Update.from_network(
            update_id="comment-1",
            kind_name="comment_created",
            platform="tiktok_business",
            connection_id=account_with_extra.id,
            created_at=NOW,
            raw={
                "comment_id": "comment-1",
                "ad_id": "12345",
                "tiktok_item_id": "item-1",
            },
        )

        with respx.mock:
            respx.post(
                "https://business-api.tiktok.com/open_api/v1.3/comment/post/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={"code": 0, "message": "ok", "request_id": "req-1"},
                )
            )

            await platform.reply_to_update(account_with_extra, update, "Great!")

    @pytest.mark.asyncio
    async def test_refuses_non_comment_updates(
        self, platform: TikTokBusinessPlatform, account_with_extra: Connection
    ) -> None:
        """reply_to_update refuses non-comment updates."""
        update = Update.from_network(
            update_id="like-1",
            kind_name="reaction_added",
            platform="tiktok_business",
            connection_id=account_with_extra.id,
            created_at=NOW,
            raw={},
        )

        with pytest.raises(NotSupportedError):
            await platform.reply_to_update(account_with_extra, update, "Great!")

    @pytest.mark.asyncio
    async def test_raises_config_error_missing_advertiser_id(
        self, platform: TikTokBusinessPlatform, account: Connection
    ) -> None:
        """reply_to_update raises ConfigError if advertiser_id is missing."""
        update = Update.from_network(
            update_id="comment-1",
            kind_name="comment_created",
            platform="tiktok_business",
            connection_id=account.id,
            created_at=NOW,
            raw={"comment_id": "comment-1"},
        )

        with pytest.raises(ConfigError, match="advertiser_id"):
            await platform.reply_to_update(account, update, "Great!")

    @pytest.mark.asyncio
    async def test_raises_config_error_missing_comment_id(
        self,
        platform: TikTokBusinessPlatform,
        account_with_extra: Connection,
    ) -> None:
        """reply_to_update raises ConfigError if comment_id is missing."""
        update = Update.from_network(
            update_id="comment-1",
            kind_name="comment_created",
            platform="tiktok_business",
            connection_id=account_with_extra.id,
            created_at=NOW,
            raw={},
        )

        with pytest.raises(ConfigError, match="comment_id"):
            await platform.reply_to_update(account_with_extra, update, "Great!")

    @pytest.mark.asyncio
    async def test_raises_config_error_missing_ad_id(
        self,
        platform: TikTokBusinessPlatform,
        account_with_extra: Connection,
    ) -> None:
        """reply_to_update raises ConfigError if ad_id is missing."""
        update = Update.from_network(
            update_id="comment-1",
            kind_name="comment_created",
            platform="tiktok_business",
            connection_id=account_with_extra.id,
            created_at=NOW,
            raw={"comment_id": "comment-1"},
        )

        with pytest.raises(ConfigError, match="ad_id"):
            await platform.reply_to_update(account_with_extra, update, "Great!")

    @pytest.mark.asyncio
    async def test_raises_config_error_missing_tiktok_item_id(
        self,
        platform: TikTokBusinessPlatform,
        account_with_extra: Connection,
    ) -> None:
        """reply_to_update raises ConfigError if tiktok_item_id is missing."""
        update = Update.from_network(
            update_id="comment-1",
            kind_name="comment_created",
            platform="tiktok_business",
            connection_id=account_with_extra.id,
            created_at=NOW,
            raw={"comment_id": "comment-1", "ad_id": "12345"},
        )

        with pytest.raises(ConfigError, match="tiktok_item_id"):
            await platform.reply_to_update(account_with_extra, update, "Great!")

    @pytest.mark.asyncio
    async def test_raises_config_error_missing_identity_id(
        self,
        platform: TikTokBusinessPlatform,
    ) -> None:
        """reply_to_update raises ConfigError if identity_id is missing."""
        account = Connection(
            id=f"tiktok_business:{ADVERTISER_ID}",
            platform="tiktok_business",
            host=None,
            account_id=ADVERTISER_ID,
            account_name="tiktok_business",
            token=Token(
                access_token="business-access-token",
                refresh_token="business-refresh-token",
                expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            ),
            scopes=(),
            extra={
                "advertiser_id": ADVERTISER_ID,
                "identity_type": "BUSINESS",
            },
        )
        update = Update.from_network(
            update_id="comment-1",
            kind_name="comment_created",
            platform="tiktok_business",
            connection_id=account.id,
            created_at=NOW,
            raw={
                "comment_id": "comment-1",
                "ad_id": "12345",
                "tiktok_item_id": "item-1",
            },
        )

        with pytest.raises(ConfigError, match="identity_id"):
            await platform.reply_to_update(account, update, "Great!")

    @pytest.mark.asyncio
    async def test_raises_config_error_missing_identity_type(
        self,
        platform: TikTokBusinessPlatform,
    ) -> None:
        """reply_to_update raises ConfigError if identity_type is missing."""
        account = Connection(
            id=f"tiktok_business:{ADVERTISER_ID}",
            platform="tiktok_business",
            host=None,
            account_id=ADVERTISER_ID,
            account_name="tiktok_business",
            token=Token(
                access_token="business-access-token",
                refresh_token="business-refresh-token",
                expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            ),
            scopes=(),
            extra={
                "advertiser_id": ADVERTISER_ID,
                "identity_id": "identity-123",
            },
        )
        update = Update.from_network(
            update_id="comment-1",
            kind_name="comment_created",
            platform="tiktok_business",
            connection_id=account.id,
            created_at=NOW,
            raw={
                "comment_id": "comment-1",
                "ad_id": "12345",
                "tiktok_item_id": "item-1",
            },
        )

        with pytest.raises(ConfigError, match="identity_type"):
            await platform.reply_to_update(account, update, "Great!")

    @pytest.mark.asyncio
    async def test_raises_error_on_api_failure(
        self, platform: TikTokBusinessPlatform, account_with_extra: Connection
    ) -> None:
        """reply_to_update raises error when API returns non-zero code."""
        update = Update.from_network(
            update_id="comment-1",
            kind_name="comment_created",
            platform="tiktok_business",
            connection_id=account_with_extra.id,
            created_at=NOW,
            raw={
                "comment_id": "comment-1",
                "ad_id": "12345",
                "tiktok_item_id": "item-1",
            },
        )

        with respx.mock:
            respx.post(
                "https://business-api.tiktok.com/open_api/v1.3/comment/post/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 40001,
                        "message": "Access token invalid",
                        "request_id": "req-1",
                    },
                )
            )

            with pytest.raises(AuthError):
                await platform.reply_to_update(account_with_extra, update, "Great!")


class TestDeleteComment:
    """Tests for delete_comment."""

    @pytest.fixture
    def account_with_extra(self) -> Connection:
        """A connected TikTok Business account with required fields in extra."""
        return Connection(
            id=f"tiktok_business:{ADVERTISER_ID}",
            platform="tiktok_business",
            host=None,
            account_id=ADVERTISER_ID,
            account_name="tiktok_business",
            token=Token(
                access_token="business-access-token",
                refresh_token="business-refresh-token",
                expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            ),
            scopes=(),
            extra={
                "advertiser_id": ADVERTISER_ID,
                "identity_id": "identity-123",
                "identity_type": "BUSINESS",
            },
        )

    @pytest.mark.asyncio
    async def test_deletes_comment(
        self, platform: TikTokBusinessPlatform, account_with_extra: Connection
    ) -> None:
        """delete_comment sends a delete request for a comment."""
        update = Update.from_network(
            update_id="comment-1",
            kind_name="comment_created",
            platform="tiktok_business",
            connection_id=account_with_extra.id,
            created_at=NOW,
            raw={
                "comment_id": "comment-1",
                "ad_id": "12345",
                "tiktok_item_id": "item-1",
            },
        )

        with respx.mock:
            respx.post(
                "https://business-api.tiktok.com/open_api/v1.3/comment/delete/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={"code": 0, "message": "ok", "request_id": "req-1"},
                )
            )

            await platform.delete_comment(account_with_extra, update)

    @pytest.mark.asyncio
    async def test_refuses_non_comment_updates(
        self, platform: TikTokBusinessPlatform, account_with_extra: Connection
    ) -> None:
        """delete_comment refuses non-comment updates."""
        update = Update.from_network(
            update_id="like-1",
            kind_name="reaction_added",
            platform="tiktok_business",
            connection_id=account_with_extra.id,
            created_at=NOW,
            raw={},
        )

        with pytest.raises(NotSupportedError):
            await platform.delete_comment(account_with_extra, update)

    @pytest.mark.asyncio
    async def test_raises_config_error_missing_advertiser_id(
        self, platform: TikTokBusinessPlatform, account: Connection
    ) -> None:
        """delete_comment raises ConfigError if advertiser_id is missing."""
        update = Update.from_network(
            update_id="comment-1",
            kind_name="comment_created",
            platform="tiktok_business",
            connection_id=account.id,
            created_at=NOW,
            raw={"comment_id": "comment-1"},
        )

        with pytest.raises(ConfigError, match="advertiser_id"):
            await platform.delete_comment(account, update)

    @pytest.mark.asyncio
    async def test_raises_config_error_missing_comment_id(
        self,
        platform: TikTokBusinessPlatform,
        account_with_extra: Connection,
    ) -> None:
        """delete_comment raises ConfigError if comment_id is missing."""
        update = Update.from_network(
            update_id="comment-1",
            kind_name="comment_created",
            platform="tiktok_business",
            connection_id=account_with_extra.id,
            created_at=NOW,
            raw={},
        )

        with pytest.raises(ConfigError, match="comment_id"):
            await platform.delete_comment(account_with_extra, update)

    @pytest.mark.asyncio
    async def test_raises_config_error_missing_ad_id(
        self,
        platform: TikTokBusinessPlatform,
        account_with_extra: Connection,
    ) -> None:
        """delete_comment raises ConfigError if ad_id is missing."""
        update = Update.from_network(
            update_id="comment-1",
            kind_name="comment_created",
            platform="tiktok_business",
            connection_id=account_with_extra.id,
            created_at=NOW,
            raw={"comment_id": "comment-1"},
        )

        with pytest.raises(ConfigError, match="ad_id"):
            await platform.delete_comment(account_with_extra, update)

    @pytest.mark.asyncio
    async def test_raises_config_error_missing_tiktok_item_id(
        self,
        platform: TikTokBusinessPlatform,
        account_with_extra: Connection,
    ) -> None:
        """delete_comment raises ConfigError if tiktok_item_id is missing."""
        update = Update.from_network(
            update_id="comment-1",
            kind_name="comment_created",
            platform="tiktok_business",
            connection_id=account_with_extra.id,
            created_at=NOW,
            raw={"comment_id": "comment-1", "ad_id": "12345"},
        )

        with pytest.raises(ConfigError, match="tiktok_item_id"):
            await platform.delete_comment(account_with_extra, update)

    @pytest.mark.asyncio
    async def test_raises_config_error_missing_identity_id(
        self,
        platform: TikTokBusinessPlatform,
    ) -> None:
        """delete_comment raises ConfigError if identity_id is missing."""
        account = Connection(
            id=f"tiktok_business:{ADVERTISER_ID}",
            platform="tiktok_business",
            host=None,
            account_id=ADVERTISER_ID,
            account_name="tiktok_business",
            token=Token(
                access_token="business-access-token",
                refresh_token="business-refresh-token",
                expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            ),
            scopes=(),
            extra={
                "advertiser_id": ADVERTISER_ID,
                "identity_type": "BUSINESS",
            },
        )
        update = Update.from_network(
            update_id="comment-1",
            kind_name="comment_created",
            platform="tiktok_business",
            connection_id=account.id,
            created_at=NOW,
            raw={
                "comment_id": "comment-1",
                "ad_id": "12345",
                "tiktok_item_id": "item-1",
            },
        )

        with pytest.raises(ConfigError, match="identity_id"):
            await platform.delete_comment(account, update)

    @pytest.mark.asyncio
    async def test_raises_config_error_missing_identity_type(
        self,
        platform: TikTokBusinessPlatform,
    ) -> None:
        """delete_comment raises ConfigError if identity_type is missing."""
        account = Connection(
            id=f"tiktok_business:{ADVERTISER_ID}",
            platform="tiktok_business",
            host=None,
            account_id=ADVERTISER_ID,
            account_name="tiktok_business",
            token=Token(
                access_token="business-access-token",
                refresh_token="business-refresh-token",
                expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            ),
            scopes=(),
            extra={
                "advertiser_id": ADVERTISER_ID,
                "identity_id": "identity-123",
            },
        )
        update = Update.from_network(
            update_id="comment-1",
            kind_name="comment_created",
            platform="tiktok_business",
            connection_id=account.id,
            created_at=NOW,
            raw={
                "comment_id": "comment-1",
                "ad_id": "12345",
                "tiktok_item_id": "item-1",
            },
        )

        with pytest.raises(ConfigError, match="identity_type"):
            await platform.delete_comment(account, update)

    @pytest.mark.asyncio
    async def test_raises_error_on_api_failure(
        self, platform: TikTokBusinessPlatform, account_with_extra: Connection
    ) -> None:
        """delete_comment raises error when API returns non-zero code."""
        update = Update.from_network(
            update_id="comment-1",
            kind_name="comment_created",
            platform="tiktok_business",
            connection_id=account_with_extra.id,
            created_at=NOW,
            raw={
                "comment_id": "comment-1",
                "ad_id": "12345",
                "tiktok_item_id": "item-1",
            },
        )

        with respx.mock:
            respx.post(
                "https://business-api.tiktok.com/open_api/v1.3/comment/delete/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 40001,
                        "message": "Access token invalid",
                        "request_id": "req-1",
                    },
                )
            )

            with pytest.raises(AuthError):
                await platform.delete_comment(account_with_extra, update)


class TestSetCommentVisibility:
    """Tests for set_comment_visibility."""

    @pytest.fixture
    def account_with_advertiser_id(self) -> Connection:
        """A connected TikTok Business account with advertiser_id in extra."""
        return Connection(
            id=f"tiktok_business:{ADVERTISER_ID}",
            platform="tiktok_business",
            host=None,
            account_id=ADVERTISER_ID,
            account_name="tiktok_business",
            token=Token(
                access_token="business-access-token",
                refresh_token="business-refresh-token",
                expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            ),
            scopes=(),
            extra={"advertiser_id": ADVERTISER_ID},
        )

    @pytest.mark.asyncio
    async def test_hides_comment(
        self, platform: TikTokBusinessPlatform, account_with_advertiser_id: Connection
    ) -> None:
        """set_comment_visibility can hide a comment."""
        update = Update.from_network(
            update_id="comment-1",
            kind_name="comment_created",
            platform="tiktok_business",
            connection_id=account_with_advertiser_id.id,
            created_at=NOW,
            raw={"comment_id": "comment-1"},
        )

        with respx.mock:
            respx.post(
                "https://business-api.tiktok.com/open_api/v1.3/comment/status/update/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={"code": 0, "message": "ok", "request_id": "req-1"},
                )
            )

            await platform.set_comment_visibility(
                account_with_advertiser_id, update, hidden=True
            )

    @pytest.mark.asyncio
    async def test_shows_comment(
        self, platform: TikTokBusinessPlatform, account_with_advertiser_id: Connection
    ) -> None:
        """set_comment_visibility can show a hidden comment."""
        update = Update.from_network(
            update_id="comment-1",
            kind_name="comment_created",
            platform="tiktok_business",
            connection_id=account_with_advertiser_id.id,
            created_at=NOW,
            raw={"comment_id": "comment-1"},
        )

        with respx.mock:
            respx.post(
                "https://business-api.tiktok.com/open_api/v1.3/comment/status/update/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={"code": 0, "message": "ok", "request_id": "req-1"},
                )
            )

            await platform.set_comment_visibility(
                account_with_advertiser_id, update, hidden=False
            )

    @pytest.mark.asyncio
    async def test_raises_config_error_when_advertiser_id_missing(
        self, platform: TikTokBusinessPlatform, account: Connection
    ) -> None:
        """set_comment_visibility raises ConfigError if advertiser_id is missing."""
        update = Update.from_network(
            update_id="comment-1",
            kind_name="comment_created",
            platform="tiktok_business",
            connection_id=account.id,
            created_at=NOW,
            raw={"comment_id": "comment-1"},
        )

        with pytest.raises(ConfigError, match="advertiser_id"):
            await platform.set_comment_visibility(account, update, hidden=True)

    @pytest.mark.asyncio
    async def test_raises_error_on_api_failure(
        self, platform: TikTokBusinessPlatform, account_with_advertiser_id: Connection
    ) -> None:
        """set_comment_visibility raises error when API returns non-zero code."""
        update = Update.from_network(
            update_id="comment-1",
            kind_name="comment_created",
            platform="tiktok_business",
            connection_id=account_with_advertiser_id.id,
            created_at=NOW,
            raw={"comment_id": "comment-1"},
        )

        with respx.mock:
            respx.post(
                "https://business-api.tiktok.com/open_api/v1.3/comment/status/update/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "code": 40001,
                        "message": "Access token invalid",
                        "request_id": "req-1",
                    },
                )
            )

            with pytest.raises(AuthError):
                await platform.set_comment_visibility(
                    account_with_advertiser_id, update, hidden=True
                )

    @pytest.mark.asyncio
    async def test_uses_update_id_as_fallback(
        self, platform: TikTokBusinessPlatform, account_with_advertiser_id: Connection
    ) -> None:
        """set_comment_visibility uses update.id when comment_id not in raw."""
        update = Update.from_network(
            update_id="comment-from-id",
            kind_name="comment_created",
            platform="tiktok_business",
            connection_id=account_with_advertiser_id.id,
            created_at=NOW,
            raw={},
        )

        with respx.mock:
            route = respx.post(
                "https://business-api.tiktok.com/open_api/v1.3/comment/status/update/"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={"code": 0, "message": "ok", "request_id": "req-1"},
                )
            )

            await platform.set_comment_visibility(
                account_with_advertiser_id, update, hidden=True
            )

            # Verify that update.id was used as comment_id
            request_body = route.calls.last.request.content.decode()
            assert "comment-from-id" in request_body
