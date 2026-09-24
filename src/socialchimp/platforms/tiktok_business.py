"""TikTok Business API: comment management for business accounts.

This is a **different TikTok product** from `tiktok.py` (Login Kit /
Content Posting API). TikTok Business has:

- Separate app registration at business-api.tiktok.com/portal
- Separate client id and secret (different app)
- Different OAuth flow with no PKCE
- Requires account to be linked to a TikTok Business Center (manual step)

**This is the ads/business Comment Management API.** It manages comments tied
to advertiser/ad accounts. Writing comments requires `ad_id` and `identity_id`
alongside comment id, per TikTok's schema.

**No video-level stats here.** TikTok also has a `business/video/list`
endpoint said to return like/comment/share/view counts, but unlike every
endpoint this platform actually calls, it does not appear anywhere in
TikTok's own SDK source - it was only ever seen in search-indexed doc
fragments, with no confirmed request or response shape. Rather than guess at
a schema nobody could verify, it is left out. `features` carries no
`Feature.READ_STATS` because of this, and there is no `read_stats` here -
not because TikTok has no such number, but because this platform will not
claim one it cannot back with a source.

**No name or picture, for the same reason.** `finish_login` never asks
who the advertiser account belongs to - there is no confirmed identity
endpoint for it either - so `account_name` is left as a placeholder and
`Connection.avatar_url` stays `None`. This platform does not implement
`read_profile`, so `Account.profile` raises `NotSupportedError` here rather
than pretending to know a name it never asked for.

Reference links:
- https://business-api.tiktok.com/portal/docs (main portal)
- https://business-api.tiktok.com/portal/docs/organic-api/v1.3 (Accounts API)
- https://github.com/tiktok/tiktok-business-api-sdk (official SDK for verification)

## Before any of this works

There is no `create_app` here. A person has to:

1. Create an app at https://business-api.tiktok.com/portal
2. Get approved for Business access (manual review by TikTok)
3. Link their TikTok Business Center account to the app
4. Add your redirect address to the app settings

Then hand the client id and client secret to socialchimp as `AppCredentials`.

## Signing someone in

Two steps: `start_login` gives you an authorize URL, `finish_login` swaps
the code for a token. No PKCE - simpler than Login Kit.

## Tokens

An access token lifetime is not explicitly documented. `finish_login` and
`refresh` attempt to read `expires_in` and `refresh_token_expires_in` from
the response, but leave the expiry as `None` if they are not present, rather
than guessing.

**`refresh`'s endpoint is not confirmed.** TikTok's own SDK
(github.com/tiktok/tiktok-business-api-sdk) documents the token exchange
call but has no refresh endpoint at all - its own docs say a token must be
refreshed daily, without saying how. `REFRESH_URL` below comes from
third-party integration guides that show it working, not from TikTok's own
SDK source. Verify it against a real TikTok Business sandbox before relying
on it in production; TikTok could change or remove it without this having
been documented anywhere we could check.

## Reading and answering comments

`fetch_updates` requires `connection.extra["advertiser_id"]` to be set.
When `fetch_updates` is passed no video/ad id argument, it reads
`connection.extra.get("ad_id")` if present, and uses that as the `search_value`.
If `ad_id` is not present in extra, a `ConfigError` is raised explaining that
either `ad_id` must be set in `connection.extra`, or the time-based filtering
must be done differently (TikTok's comment/list endpoint requires a search_field
and search_value).

Only the first page of comments is fetched (page=1, page_size=10 defaults).

Comment field names in `Update.raw` are best-effort (based on TikTok's own SDK
docs): we attempt to read `comment_id` and `create_time`, but `raw` carries
the untouched comment dict as the safety net - nothing is lost if our field
name guesses are wrong.

The hide/show `operation` values sent to the comment/status/update endpoint
are unconfirmed - we use `"HIDE"` and `"SHOW"` as the most obvious values, but
these should be verified against a live TikTok Business account if possible.

`reply_to_update`'s `comment_type` field is read from `update.raw` when
present (which it will be for any comment `fetch_updates` actually fetched),
falling back to `"text"` only when it is not - also unconfirmed as a real
TikTok enum value, and worth checking before relying on the fallback path.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import httpx

from socialchimp.errors import (
    AuthError,
    ConfigError,
    NotSupportedError,
    PlatformError,
    RateLimitError,
)
from socialchimp.events import Update, UpdateKind
from socialchimp.features import Feature, Limits, check_post
from socialchimp.http import HttpClient, error_from_response, read_body
from socialchimp.models import (
    Connection,
    Post,
    PostResult,
    RawData,
    Token,
)
from socialchimp.platform import Finished, LoginRequest, SendToNetwork

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from socialchimp.errors import SocialChimpError
    from socialchimp.http import Retries
    from socialchimp.models import AppCredentials

__all__ = ["TikTokBusinessPlatform", "tiktok_business_errors"]

PLATFORM_NAME: Final = "tiktok_business"

API_URL: Final = "https://business-api.tiktok.com"
"""Base URL for all TikTok Business API calls."""

AUTHORIZE_URL: Final = "https://business-api.tiktok.com/portal/auth"
"""The page a person approves the app on."""

TOKEN_URL: Final = "https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/"  # noqa: S105
"""Where an auth code or refresh token is exchanged for an access token."""

REFRESH_URL: Final = (
    "https://business-api.tiktok.com/open_api/v1.3/oauth2/refresh_token/"
)
"""Where a refresh token is exchanged for a new access token.

Not present in TikTok's own SDK - see "## Tokens" in the module docstring.
"""

# State should be 24 random bytes, URL-safe
_STATE_BYTES: Final = 24


def _random_state() -> str:
    """Generate a random state value for OAuth."""
    return secrets.token_urlsafe(_STATE_BYTES)


def tiktok_business_errors(response: httpx.Response) -> SocialChimpError:
    """Turn an unhappy reply from TikTok Business into a socialchimp error.

    TikTok Business wraps every response in an envelope:
        {"code": int, "message": str, "request_id": str, "data": {...}}

    code == 0 means success. Any non-zero code is an error.
    """
    body = read_body(response)

    # Read the TikTok Business API envelope
    code = body.get("code")
    message = body.get("message", "")

    # code == 0 means success - should not be here, but handle gracefully
    if code == 0:
        return error_from_response(response, platform=PLATFORM_NAME)

    # Build error message from TikTok's message if available
    said = f" It said: {message}" if message else ""

    # Map specific error codes to appropriate error types
    # These are commonly used error codes based on TikTok's documentation
    if isinstance(code, int):
        # 40000-40999: Authentication/authorization errors
        if 40000 <= code <= 40999:
            return AuthError(
                f"{PLATFORM_NAME} would not accept our credentials ({code}).{said}",
                platform=PLATFORM_NAME,
                raw=body,
            )
        # 30000-30999: Rate limit errors
        if 30000 <= code <= 30999:
            return RateLimitError(
                f"{PLATFORM_NAME} is asking us to slow down ({code}).{said}",
                platform=PLATFORM_NAME,
                raw=body,
            )

    # Fall back to the generic error handler for unknown codes
    return error_from_response(response, platform=PLATFORM_NAME)


class TikTokBusinessPlatform:
    """TikTok Business API platform for reading and managing comments.

    This platform is read-only at this stage. Publishing is not supported.
    """

    name: Final = PLATFORM_NAME
    # No Feature.* flags apply here - see the module docstring.
    features: Final = Feature(0)

    def __init__(
        self,
        *,
        retries: Retries | None = None,
        now: Callable[[], datetime] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Set up the platform.

        Args:
            retries: How to retry failed requests.
            now: A callable returning the current time (for testing).
            transport: Custom HTTP transport (for testing).
        """
        self._retries = retries
        self._now = now or (lambda: datetime.now(UTC))
        self._transport = transport

    def api_base(self, connection: Connection) -> str:
        """Return the TikTok Business API base URL.

        Args:
            connection: The connected account.

        Returns:
            The API base URL.
        """
        return API_URL

    def auth_headers(self, connection: Connection) -> Mapping[str, str]:
        """Return the Access-Token header for this account.

        Note: This is NOT "Authorization: Bearer" - TikTok Business uses
        a literal "Access-Token" header.

        Args:
            connection: The connected account.

        Returns:
            Headers with the access token.
        """
        return {"Access-Token": connection.token.access_token}

    async def limits(self, connection: Connection) -> Limits:
        """Return the limits for this account.

        TikTok Business does not publish posts, so limits are minimal.

        Args:
            connection: The connected account.

        Returns:
            An empty Limits object.
        """
        return Limits()

    async def start_login(self, request: LoginRequest) -> SendToNetwork:
        """Begin signing someone in.

        Args:
            request: The login request with redirect URI and app credentials.

        Returns:
            A SendToNetwork with the authorize URL.
        """
        if request.app is None:
            message = (
                f"Cannot sign in to {PLATFORM_NAME} without app credentials. "
                f"Create credentials at https://business-api.tiktok.com/portal and "
                f"save them with your app."
            )
            raise PlatformError(message, platform=PLATFORM_NAME)

        state = request.state or _random_state()

        # Build the authorize URL: https://business-api.tiktok.com/portal/auth
        # Parameters: app_id, redirect_uri, state
        url = (
            f"{AUTHORIZE_URL}"
            f"?app_id={request.app.client_id}"
            f"&redirect_uri={request.redirect_uri}"
            f"&state={state}"
        )

        return SendToNetwork(url=url, state=state, remember={})

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> Finished:
        """Carry on after the person comes back from TikTok.

        Exchanges the auth_code for an access token.

        Args:
            request: The same login request as start_login.
            callback: Query parameters from the callback (auth_code, state).
            remember: Unused (no PKCE).

        Returns:
            A Finished with the connection.

        Raises:
            PlatformError: If credentials are missing or auth fails.
        """
        if request.app is None:
            message = f"Cannot finish login to {PLATFORM_NAME} without app credentials."
            raise PlatformError(message, platform=PLATFORM_NAME)

        auth_code = callback.get("auth_code")
        if not auth_code:
            message = "No auth_code in callback from TikTok Business."
            raise PlatformError(message, platform=PLATFORM_NAME)

        # Exchange auth code for access token. Body matches TikTok's own
        # Oauth2AccessTokenBody model exactly: app_id, auth_code, secret -
        # no grant_type field, unlike the Login Kit flow in tiktok.py.
        body = {
            "app_id": request.app.client_id,
            "secret": request.app.client_secret,
            "auth_code": auth_code,
        }

        async with HttpClient(
            base_url="",
            platform=PLATFORM_NAME,
            transport=self._transport,
            retries=self._retries,
            errors=tiktok_business_errors,
        ) as http:
            response = await http.post(TOKEN_URL, json=body)
            data = response.json()

        # Handle error response
        if data.get("code") != 0:
            raise tiktok_business_errors(response)

        # Extract tokens from data envelope
        token_data = data.get("data", {})
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")

        if not access_token:
            message = "TikTok Business did not return an access token."
            raise PlatformError(message, platform=PLATFORM_NAME, raw=data)

        # Calculate expiry if expires_in is provided
        expires_at = None
        expires_in = token_data.get("expires_in")
        if isinstance(expires_in, (int, float)):
            expires_at = self._now() + timedelta(seconds=expires_in)

        refresh_token_expires_at = None
        refresh_expires_in = token_data.get("refresh_token_expires_in")
        if isinstance(refresh_expires_in, (int, float)):
            expires_delta = timedelta(seconds=refresh_expires_in)
            refresh_token_expires_at = self._now() + expires_delta

        token = Token(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            refresh_token_expires_at=refresh_token_expires_at,
        )

        # Use advertiser_id or app_id as account_id (placeholder approach)
        account_id = token_data.get("advertiser_id") or request.app.client_id

        connection = Connection(
            id=f"{PLATFORM_NAME}:{account_id}",
            platform=PLATFORM_NAME,
            host=None,
            account_id=account_id,
            account_name=PLATFORM_NAME,  # Placeholder - no identity endpoint
            token=token,
            scopes=(),
            extra={},
            # No identity endpoint means no picture either. `avatar_url`
            # defaults to `None`, and this platform has no `read_profile`
            # to refresh it later - see the module docstring.
            avatar_url=None,
        )

        return Finished(connection=connection)

    async def refresh(
        self,
        connection: Connection,
        app: AppCredentials | None = None,
    ) -> Token:
        """Get a fresh access token.

        Args:
            connection: The account to refresh.
            app: App credentials (required for refresh).

        Returns:
            A new Token.

        Raises:
            PlatformError: If credentials are missing or refresh fails.
        """
        if app is None:
            message = f"Cannot refresh {PLATFORM_NAME} without app credentials."
            raise PlatformError(message, platform=PLATFORM_NAME)

        if not connection.token.refresh_token:
            message = f"No refresh token available for {PLATFORM_NAME}."
            raise PlatformError(message, platform=PLATFORM_NAME)

        # Post to refresh endpoint. This shape (app_id, secret, refresh_token,
        # grant_type) is not in TikTok's own SDK - see REFRESH_URL above.
        body = {
            "app_id": app.client_id,
            "secret": app.client_secret,
            "refresh_token": connection.token.refresh_token,
            "grant_type": "refresh_token",
        }

        async with HttpClient(
            base_url="",
            platform=PLATFORM_NAME,
            transport=self._transport,
            retries=self._retries,
            errors=tiktok_business_errors,
        ) as http:
            response = await http.post(REFRESH_URL, json=body)
            data = response.json()

        # Handle error response
        if data.get("code") != 0:
            raise tiktok_business_errors(response)

        # Extract tokens from data envelope
        token_data = data.get("data", {})
        access_token = token_data.get("access_token")

        if not access_token:
            message = "TikTok Business did not return an access token on refresh."
            raise PlatformError(message, platform=PLATFORM_NAME, raw=data)

        # Calculate expiry if expires_in is provided
        expires_at = None
        expires_in = token_data.get("expires_in")
        if isinstance(expires_in, (int, float)):
            expires_at = self._now() + timedelta(seconds=expires_in)

        refresh_token_expires_at = None
        refresh_expires_in = token_data.get("refresh_token_expires_in")
        if isinstance(refresh_expires_in, (int, float)):
            expires_delta = timedelta(seconds=refresh_expires_in)
            refresh_token_expires_at = self._now() + expires_delta

        refresh_token = token_data.get("refresh_token")

        return Token(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            refresh_token_expires_at=refresh_token_expires_at,
        )

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        """Publish a post.

        This platform cannot publish. It will raise NotSupportedError via
        check_post, which validates against platform capabilities.

        Args:
            connection: The account to publish as.
            post: The post to publish.

        Returns:
            Never - always raises.

        Raises:
            NotSupportedError: Always, via check_post.
        """
        # Let check_post raise NotSupportedError because this platform
        # has no POST_* features
        check_post(
            post,
            platform=PLATFORM_NAME,
            features=self.features,
            limits=Limits(),
        )

        # check_post always raises for this platform - it has no POST_*
        # feature, and Post.__post_init__ guarantees text or media is set -
        # so this line only exists to satisfy the type checker.
        raise NotSupportedError(
            platform=PLATFORM_NAME,
            what="publishing posts",
        )  # pragma: no cover

    async def fetch_updates(
        self,
        connection: Connection,
        since: datetime | None,
    ) -> Sequence[Update]:
        """Return comments on this advertiser's ads since a moment in time.

        Args:
            connection: The advertiser account to ask about.
            since: Only return comments newer than this. `None` on the first
                call, when there is no marker saved yet - defaults to 30 days
                before now.

        Returns:
            The comments, oldest first.

        Raises:
            ConfigError: If `connection.extra["advertiser_id"]` is missing.
        """
        advertiser_id = connection.extra.get("advertiser_id")
        if not advertiser_id:
            message = (
                "fetch_updates requires connection.extra['advertiser_id'] to be set. "
                "The TikTok Business API has no identity-lookup endpoint, so the "
                "advertiser_id cannot be discovered automatically."
            )
            raise ConfigError(message)

        ad_id = connection.extra.get("ad_id")
        if not ad_id:
            message = (
                "fetch_updates requires connection.extra['ad_id'] to be set. "
                "TikTok's comment/list endpoint has no way to ask for every "
                "comment on an advertiser account at once - it wants a "
                "search_field and search_value naming one ad, so an empty "
                "search is refused here rather than sent to TikTok."
            )
            raise ConfigError(message)

        # Build the time window
        start_time = self._now() - timedelta(days=30) if since is None else since
        end_time = self._now()

        # Build request parameters
        # Field names here are best-effort based on SDK docs, but raw always
        # carries the untouched comment dict as the safety net.
        params = {
            "advertiser_id": advertiser_id,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "search_field": "AD_ID",
            "search_value": ad_id,
            "page": 1,
            "page_size": 10,
        }

        async with HttpClient(
            base_url=API_URL,
            platform=PLATFORM_NAME,
            headers=self.auth_headers(connection),
            transport=self._transport,
            retries=self._retries,
            errors=tiktok_business_errors,
        ) as http:
            response = await http.get("/open_api/v1.3/comment/list/", params=params)
            data = response.json()

        # Handle error response
        if data.get("code") != 0:
            raise tiktok_business_errors(response)

        updates: list[Update] = []
        comments = data.get("data", {}).get("comments", [])
        if not isinstance(comments, list):
            comments = []

        for comment in comments:
            # Extract fields defensively
            comment_id = comment.get("comment_id") or comment.get("id")
            if not comment_id:
                continue

            # Parse create_time - try to convert from Unix timestamp
            create_time = comment.get("create_time")
            if isinstance(create_time, (int, float)):
                created_at = datetime.fromtimestamp(create_time, tz=UTC)
            else:
                # Fall back to now if we can't parse
                created_at = self._now()

            # Only include if newer than since
            if since is not None and created_at <= since:
                continue

            updates.append(
                Update.from_network(
                    update_id=str(comment_id),
                    kind_name="comment_created",
                    platform=PLATFORM_NAME,
                    connection_id=connection.id,
                    created_at=created_at,
                    raw=comment,
                )
            )

        # Return oldest first
        updates.sort(key=lambda u: u.created_at)
        return updates

    async def reply_to_update(
        self,
        connection: Connection,
        update: Update,
        text: str,
    ) -> None:
        """Answer a comment in place.

        Args:
            connection: The advertiser account the comment belongs to.
            update: The comment to answer, exactly as `fetch_updates` handed it
                back. Its `raw` carries the comment dict with fields needed to
                reply.
            text: The reply.

        Raises:
            NotSupportedError: If this update is not a comment.
            ConfigError: If required fields are missing from `update.raw` or
                `connection.extra`.
        """
        if update.kind != UpdateKind.COMMENT_CREATED:
            raise NotSupportedError(
                platform=PLATFORM_NAME,
                what="replying to this kind of update",
                suggestion="Only comments can be answered here.",
            )

        # Source required fields from update.raw and connection.extra
        advertiser_id = update.raw.get("advertiser_id") or connection.extra.get(
            "advertiser_id"
        )
        if not advertiser_id:
            raise ConfigError(
                "reply_to_update requires advertiser_id from update.raw or "
                "connection.extra['advertiser_id']"
            )

        comment_id = update.raw.get("comment_id") or update.raw.get("id")
        if not comment_id:
            raise ConfigError("reply_to_update requires comment_id from update.raw")

        ad_id = update.raw.get("ad_id")
        if not ad_id:
            raise ConfigError("reply_to_update requires ad_id from update.raw")

        tiktok_item_id = update.raw.get("tiktok_item_id")
        if not tiktok_item_id:
            raise ConfigError("reply_to_update requires tiktok_item_id from update.raw")

        comment_type = update.raw.get("comment_type", "text")
        identity_id = update.raw.get("identity_id") or connection.extra.get(
            "identity_id"
        )
        if not identity_id:
            raise ConfigError(
                "reply_to_update requires identity_id from update.raw or "
                "connection.extra['identity_id']"
            )

        identity_type = update.raw.get("identity_type") or connection.extra.get(
            "identity_type"
        )
        if not identity_type:
            raise ConfigError(
                "reply_to_update requires identity_type from update.raw or "
                "connection.extra['identity_type']"
            )

        body = {
            "ad_id": ad_id,
            "advertiser_id": advertiser_id,
            "comment_id": comment_id,
            "comment_type": comment_type,
            "identity_id": identity_id,
            "identity_type": identity_type,
            "text": text,
            "tiktok_item_id": tiktok_item_id,
        }

        async with HttpClient(
            base_url=API_URL,
            platform=PLATFORM_NAME,
            headers=self.auth_headers(connection),
            transport=self._transport,
            retries=self._retries,
            errors=tiktok_business_errors,
        ) as http:
            response = await http.post("/open_api/v1.3/comment/post/", json=body)
            data = response.json()

        # Handle error response
        if data.get("code") != 0:
            raise tiktok_business_errors(response)

    async def delete_comment(
        self,
        connection: Connection,
        update: Update,
    ) -> None:
        """Remove a comment outright.

        Args:
            connection: The advertiser account the comment belongs to.
            update: The comment to remove, exactly as `fetch_updates` handed it
                back. Its `raw` carries the comment dict with fields needed to
                delete.

        Raises:
            NotSupportedError: If this update is not a comment.
            ConfigError: If required fields are missing from `update.raw` or
                `connection.extra`.
        """
        if update.kind != UpdateKind.COMMENT_CREATED:
            raise NotSupportedError(
                platform=PLATFORM_NAME,
                what="deleting this kind of update",
                suggestion="Only comments can be deleted here.",
            )

        # Source required fields from update.raw and connection.extra
        advertiser_id = update.raw.get("advertiser_id") or connection.extra.get(
            "advertiser_id"
        )
        if not advertiser_id:
            raise ConfigError(
                "delete_comment requires advertiser_id from update.raw or "
                "connection.extra['advertiser_id']"
            )

        comment_id = update.raw.get("comment_id") or update.raw.get("id")
        if not comment_id:
            raise ConfigError("delete_comment requires comment_id from update.raw")

        ad_id = update.raw.get("ad_id")
        if not ad_id:
            raise ConfigError("delete_comment requires ad_id from update.raw")

        tiktok_item_id = update.raw.get("tiktok_item_id")
        if not tiktok_item_id:
            raise ConfigError("delete_comment requires tiktok_item_id from update.raw")

        identity_id = update.raw.get("identity_id") or connection.extra.get(
            "identity_id"
        )
        if not identity_id:
            raise ConfigError(
                "delete_comment requires identity_id from update.raw or "
                "connection.extra['identity_id']"
            )

        identity_type = update.raw.get("identity_type") or connection.extra.get(
            "identity_type"
        )
        if not identity_type:
            raise ConfigError(
                "delete_comment requires identity_type from update.raw or "
                "connection.extra['identity_type']"
            )

        body = {
            "ad_id": ad_id,
            "advertiser_id": advertiser_id,
            "comment_id": comment_id,
            "identity_id": identity_id,
            "identity_type": identity_type,
            "tiktok_item_id": tiktok_item_id,
        }

        async with HttpClient(
            base_url=API_URL,
            platform=PLATFORM_NAME,
            headers=self.auth_headers(connection),
            transport=self._transport,
            retries=self._retries,
            errors=tiktok_business_errors,
        ) as http:
            response = await http.post("/open_api/v1.3/comment/delete/", json=body)
            data = response.json()

        # Handle error response
        if data.get("code") != 0:
            raise tiktok_business_errors(response)

    async def set_comment_visibility(
        self,
        connection: Connection,
        update: Update,
        *,
        hidden: bool,
    ) -> None:
        """Hide a comment from public view, or show one again.

        Args:
            connection: The advertiser account the comment belongs to.
            update: The comment to hide or show, exactly as `fetch_updates`
                handed it back.
            hidden: `True` to hide it, `False` to show it again.

        Raises:
            ConfigError: If required fields are missing from `connection.extra`.
        """
        advertiser_id = connection.extra.get("advertiser_id")
        if not advertiser_id:
            raise ConfigError(
                "set_comment_visibility requires connection.extra['advertiser_id']"
            )

        comment_id = update.raw.get("comment_id") or update.raw.get("id")
        if not comment_id:
            comment_id = update.id

        # The operation strings are unconfirmed - these are the most obvious
        # values based on the endpoint description, but should be verified
        # against a live TikTok Business account.
        operation = "HIDE" if hidden else "SHOW"

        body = {
            "advertiser_id": advertiser_id,
            "comment_ids": [comment_id],
            "operation": operation,
            "ad_type": "BIDDING",
        }

        async with HttpClient(
            base_url=API_URL,
            platform=PLATFORM_NAME,
            headers=self.auth_headers(connection),
            transport=self._transport,
            retries=self._retries,
            errors=tiktok_business_errors,
        ) as http:
            response = await http.post(
                "/open_api/v1.3/comment/status/update/", json=body
            )
            data = response.json()

        # Handle error response
        if data.get("code") != 0:
            raise tiktok_business_errors(response)
