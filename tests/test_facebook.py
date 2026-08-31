"""Tests for the Facebook Pages platform."""

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
    CanCheckState,
    CanDeletePosts,
    CanReadPushedUpdates,
    CanResumeLogin,
    ChooseAccount,
    Finished,
    LoginRequest,
    Platform,
)
from socialchimp.platforms import facebook as facebook_module
from socialchimp.platforms._meta import DEVELOPER_PORTAL, GRAPH_API
from socialchimp.platforms.facebook import (
    BIGGEST_SIMPLE_VIDEO,
    DEFAULT_SCOPES,
    MAX_TEXT_LENGTH,
    FacebookPlatform,
    facebook_errors,
)
from socialchimp.testing import PlatformChecks, RecordingTransport

APP_ID = "1234567890"
APP_SECRET = "app-secret"
PAGE_ID = "111222333"
PAGE_TOKEN = "page-token"
POST_ID = f"{PAGE_ID}_444555"
VIDEO_ID = "777888999"
REDIRECT = "https://app.example/callback"

# One try and no waiting, so the error tests do not spend real seconds asleep.
ONCE = Retries(attempts=1)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
IN_AN_HOUR = NOW + timedelta(hours=1)

PAGES: dict[str, Any] = {
    "data": [
        {
            "id": PAGE_ID,
            "name": "Ada's Cakes",
            "category": "Bakery",
            "access_token": PAGE_TOKEN,
        },
        {
            "id": "999888",
            "name": "Ada's Bikes",
            "category": "Shop",
            "access_token": "other-page-token",
        },
    ]
}

ONE_PAGE: dict[str, Any] = {"data": [PAGES["data"][0]]}

PUBLISHED: dict[str, Any] = {"id": POST_ID}


@pytest.fixture
def platform() -> FacebookPlatform:
    """A platform that gives up after one try."""
    return FacebookPlatform(retries=ONCE)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Freeze the moment scheduling and token expiry are measured from."""
    monkeypatch.setattr(facebook_module, "_now", lambda: NOW)
    return NOW


def an_app() -> AppCredentials:
    """Your app's credentials, as they arrive on a login request."""
    return AppCredentials(
        platform="facebook",
        host=None,
        client_id=APP_ID,
        client_secret=APP_SECRET,
    )


def a_request(*, state: str | None = "abc123") -> LoginRequest:
    """A login request carrying your app's credentials."""
    return LoginRequest(redirect_uri=REDIRECT, state=state, app=an_app())


def an_account(
    *,
    page_id: str = PAGE_ID,
    token: str = PAGE_TOKEN,
    expires_at: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> Connection:
    """A connected Facebook page."""
    return Connection(
        id=f"facebook:{page_id}",
        platform="facebook",
        host=None,
        account_id=page_id,
        account_name="Ada's Cakes",
        token=Token(access_token=token, expires_at=expires_at),
        scopes=DEFAULT_SCOPES,
        extra=extra if extra is not None else {"page_id": page_id},
    )


@pytest.fixture
def account() -> Connection:
    """A connected page whose token does not expire."""
    return an_account()


def sent_form(route: respx.Route, *, at: int = -1) -> dict[str, str]:
    """Read a form that went to the wire."""
    body = route.calls[at].request.content.decode()
    return dict(httpx.QueryParams(body))


def sent_parts(route: respx.Route, *, at: int = -1) -> str:
    """Read a multipart body that went to the wire, as text."""
    body: bytes = route.calls[at].request.content
    return body.decode("utf-8", "replace")


def an_error(code: int, message: str = "Nope") -> dict[str, Any]:
    """What Meta's own error object looks like."""
    return {"error": {"message": message, "code": code, "type": "OAuthException"}}


def signed(body: bytes, *, secret: str = APP_SECRET) -> dict[str, str]:
    """The header Meta sends alongside a pushed body."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}"}


def a_push(value: dict[str, Any], *, topic: str = "feed") -> bytes:
    """One page change, wrapped the way Meta wraps it."""
    return json.dumps(
        {
            "object": "page",
            "entry": [
                {
                    "id": PAGE_ID,
                    "time": 1_790_000_000,
                    "changes": [{"field": topic, "value": value}],
                }
            ],
        }
    ).encode()


def stub_feed(network: respx.Router) -> respx.Route:
    """Answer "publish this" with a post that exists."""
    return network.post(f"/{PAGE_ID}/feed").mock(
        return_value=httpx.Response(200, json=PUBLISHED)
    )


async def publish_text(
    platform: FacebookPlatform,
    account: Connection,
    post: Post,
) -> dict[str, str]:
    """Publish a post and hand back the form that went to the wire."""
    with respx.mock(base_url=GRAPH_API) as network:
        route = stub_feed(network)
        await platform.publish(account, post)
    return sent_form(route)


# ---------------------------------------------------------------------------
# What it says it can do
# ---------------------------------------------------------------------------


class TestWhatItSaysItCanDo:
    def test_it_provides_everything_a_platform_must(
        self,
        platform: FacebookPlatform,
    ) -> None:
        checked: Platform = platform
        resumes: CanResumeLogin = platform
        deletes: CanDeletePosts = platform
        listens: CanCheckSignature = platform
        answers: CanCheckState = platform

        assert isinstance(checked, Platform)
        assert isinstance(resumes, CanResumeLogin)
        assert isinstance(deletes, CanDeletePosts)
        assert isinstance(listens, CanCheckSignature)
        assert isinstance(answers, CanCheckState)
        assert platform.name == "facebook"

    def test_it_lists_the_features_facebook_really_has(
        self,
        platform: FacebookPlatform,
    ) -> None:
        for feature in (
            Feature.POST_TEXT,
            Feature.POST_IMAGE,
            Feature.POST_VIDEO,
            Feature.SCHEDULE,
            Feature.DELETE_POST,
            Feature.PUSH_UPDATES,
        ):
            assert feature in platform.features

    def test_it_does_not_claim_what_facebook_cannot_do_here(
        self,
        platform: FacebookPlatform,
    ) -> None:
        # There is no app to register anywhere in Meta, and a reply on
        # Facebook is a comment rather than a post, which is a different
        # thing we have not written.
        for missing in (Feature.CREATE_APP, Feature.REPLY, Feature.READ_POSTS):
            assert missing not in platform.features

    def test_every_page_talks_to_the_same_address(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        assert platform.api_base(account) == "https://graph.facebook.com/v21.0"

    def test_it_signs_requests_with_the_pages_own_token(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        # A header rather than the access_token query parameter Meta also
        # accepts: a token in a web address ends up in server logs, proxy
        # logs and browser history.
        assert platform.auth_headers(account) == {
            "Authorization": f"Bearer {PAGE_TOKEN}"
        }

    async def test_its_limits_are_the_same_for_everybody(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        assert await platform.limits(account) == Limits(
            max_text_length=MAX_TEXT_LENGTH,
            text_counted_in=TextCount.CHARACTERS,
            max_videos=1,
            max_video_bytes=BIGGEST_SIMPLE_VIDEO,
        )

    async def test_its_text_limit_is_the_one_facebook_is_known_for(
        self,
        platform: FacebookPlatform,
    ) -> None:
        assert MAX_TEXT_LENGTH == 63_206


class TestThereIsNoAppToRegister:
    async def test_it_sends_people_to_metas_own_portal(
        self,
        platform: FacebookPlatform,
    ) -> None:
        with pytest.raises(NotSupportedError) as refused:
            await platform.create_app(name="My App", redirect_uri=REDIRECT)

        assert DEVELOPER_PORTAL in str(refused.value)

    async def test_it_warns_about_the_review_and_the_verification(
        self,
        platform: FacebookPlatform,
    ) -> None:
        with pytest.raises(NotSupportedError) as refused:
            await platform.create_app(name="My App", redirect_uri=REDIRECT)

        said = str(refused.value).lower()
        assert "review" in said
        assert "business verification" in said

    async def test_it_does_not_claim_it_can_register_an_app(
        self,
        platform: FacebookPlatform,
    ) -> None:
        # The method exists only to say no with a useful message. socialchimp
        # reads `features` before calling anything, so nothing ever gets here
        # by accident.
        assert Feature.CREATE_APP not in platform.features

    async def test_asking_it_to_register_sends_nothing_to_meta(
        self,
        platform: FacebookPlatform,
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
        platform: FacebookPlatform,
    ) -> None:
        step = await platform.start_login(a_request())

        query = httpx.URL(step.url).params
        assert step.url.startswith("https://www.facebook.com/v21.0/dialog/oauth?")
        assert query["client_id"] == APP_ID
        assert query["redirect_uri"] == REDIRECT
        assert query["response_type"] == "code"
        assert step.state == "abc123"

    async def test_it_asks_for_the_permissions_posting_needs(
        self,
        platform: FacebookPlatform,
    ) -> None:
        step = await platform.start_login(a_request())

        asked = httpx.URL(step.url).params["scope"].split(",")
        assert asked == list(DEFAULT_SCOPES)
        assert "pages_manage_posts" in asked

    async def test_it_asks_for_what_you_asked_for_instead(
        self,
        platform: FacebookPlatform,
    ) -> None:
        request = LoginRequest(
            redirect_uri=REDIRECT,
            scopes=("pages_show_list",),
            app=an_app(),
        )

        step = await platform.start_login(request)

        assert httpx.URL(step.url).params["scope"] == "pages_show_list"

    async def test_it_makes_a_state_when_you_did_not(
        self,
        platform: FacebookPlatform,
    ) -> None:
        step = await platform.start_login(a_request(state=None))

        assert step.state
        assert httpx.URL(step.url).params["state"] == step.state

    async def test_there_is_nothing_to_carry_between_the_two_halves(
        self,
        platform: FacebookPlatform,
    ) -> None:
        # Unlike Mastodon there is no secret to keep: the swap at the end is
        # signed with your app secret, which never leaves your server.
        step = await platform.start_login(a_request())

        assert step.remember == {}

    async def test_a_login_with_no_app_credentials_says_where_to_get_them(
        self,
        platform: FacebookPlatform,
    ) -> None:
        with pytest.raises(ConfigError) as refused:
            await platform.start_login(LoginRequest(redirect_uri=REDIRECT))

        assert DEVELOPER_PORTAL in str(refused.value)

    async def test_starting_a_login_sends_nothing_to_facebook(
        self,
        platform: FacebookPlatform,
    ) -> None:
        with respx.mock(assert_all_called=False) as network:
            await platform.start_login(a_request())
        assert not network.calls


class TestFinishingALogin:
    async def test_it_swaps_the_code_then_makes_the_token_last(
        self,
        platform: FacebookPlatform,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            swap = network.get("/oauth/access_token").mock(
                side_effect=[
                    httpx.Response(
                        200, json={"access_token": "short", "expires_in": 3600}
                    ),
                    httpx.Response(
                        200, json={"access_token": "long", "expires_in": 5_184_000}
                    ),
                ]
            )
            network.get("/me/accounts").mock(
                return_value=httpx.Response(200, json=PAGES)
            )

            await platform.finish_login(a_request(), {"code": "the-code"})

        first = swap.calls[0].request.url.params
        assert first["code"] == "the-code"
        assert first["client_secret"] == APP_SECRET
        assert first["redirect_uri"] == REDIRECT

        second = swap.calls[1].request.url.params
        assert second["grant_type"] == "fb_exchange_token"
        assert second["fb_exchange_token"] == "short"

    async def test_it_asks_which_page_with_the_long_lived_token(
        self,
        platform: FacebookPlatform,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/oauth/access_token").mock(
                side_effect=[
                    httpx.Response(200, json={"access_token": "short"}),
                    httpx.Response(200, json={"access_token": "long"}),
                ]
            )
            pages = network.get("/me/accounts").mock(
                return_value=httpx.Response(200, json=PAGES)
            )

            step = await platform.finish_login(a_request(), {"code": "the-code"})

        assert pages.calls.last.request.headers["Authorization"] == "Bearer long"
        assert isinstance(step, ChooseAccount)
        assert [(one.id, one.name, one.kind) for one in step.options] == [
            (PAGE_ID, "Ada's Cakes", "page"),
            ("999888", "Ada's Bikes", "page"),
        ]

    async def test_it_asks_even_when_there_is_only_one_page(
        self,
        platform: FacebookPlatform,
    ) -> None:
        # Picking silently would mean somebody finds out which page they
        # connected when a post turns up on it.
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/oauth/access_token").mock(
                side_effect=[
                    httpx.Response(200, json={"access_token": "short"}),
                    httpx.Response(200, json={"access_token": "long"}),
                ]
            )
            network.get("/me/accounts").mock(
                return_value=httpx.Response(200, json=ONE_PAGE)
            )

            step = await platform.finish_login(a_request(), {"code": "the-code"})

        assert isinstance(step, ChooseAccount)
        assert len(step.options) == 1

    async def test_somebody_who_manages_no_pages_is_told_what_happened(
        self,
        platform: FacebookPlatform,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/oauth/access_token").mock(
                side_effect=[
                    httpx.Response(200, json={"access_token": "short"}),
                    httpx.Response(200, json={"access_token": "long"}),
                ]
            )
            network.get("/me/accounts").mock(
                return_value=httpx.Response(200, json={"data": []})
            )

            with pytest.raises(AuthError, match="page picker"):
                await platform.finish_login(a_request(), {"code": "the-code"})

    async def test_a_state_that_did_not_come_from_here_is_refused(
        self,
        platform: FacebookPlatform,
    ) -> None:
        with (
            respx.mock(assert_all_called=False) as network,
            pytest.raises(AuthError, match="did not start here"),
        ):
            await platform.finish_login(
                a_request(),
                {"code": "the-code", "state": "somebody-elses"},
            )
        assert not network.calls

    async def test_somebody_who_pressed_cancel_is_not_a_mystery(
        self,
        platform: FacebookPlatform,
    ) -> None:
        with pytest.raises(AuthError, match="pressed cancel"):
            await platform.finish_login(
                a_request(),
                {
                    "error": "access_denied",
                    "error_description": "Permissions error",
                    "state": "abc123",
                },
            )

    async def test_a_callback_with_no_code_says_what_to_pass(
        self,
        platform: FacebookPlatform,
    ) -> None:
        with pytest.raises(AuthError, match="whole query string"):
            await platform.finish_login(a_request(), {"state": "abc123"})

    async def test_finishing_without_app_credentials_says_so(
        self,
        platform: FacebookPlatform,
    ) -> None:
        with pytest.raises(ConfigError):
            await platform.finish_login(
                LoginRequest(redirect_uri=REDIRECT), {"code": "the-code"}
            )


class TestChoosingAPage:
    async def test_it_finishes_with_the_page_that_was_picked(
        self,
        platform: FacebookPlatform,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            lookup = network.get(f"/{PAGE_ID}").mock(
                return_value=httpx.Response(200, json=PAGES["data"][0])
            )

            step = await platform.resume_login(
                a_request(),
                resume_token="long",
                account_id=PAGE_ID,
            )

        assert lookup.calls.last.request.headers["Authorization"] == "Bearer long"
        assert isinstance(step, Finished)
        connection = step.connection
        assert connection.id == f"facebook:{PAGE_ID}"
        assert connection.platform == "facebook"
        assert connection.account_id == PAGE_ID
        assert connection.account_name == "Ada's Cakes"
        assert connection.scopes == DEFAULT_SCOPES

    async def test_it_saves_the_pages_own_token_not_the_persons(
        self,
        platform: FacebookPlatform,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get(f"/{PAGE_ID}").mock(
                return_value=httpx.Response(200, json=PAGES["data"][0])
            )

            step = await platform.resume_login(
                a_request(), resume_token="long", account_id=PAGE_ID
            )

        assert isinstance(step, Finished)
        assert step.connection.token.access_token == PAGE_TOKEN

    async def test_the_page_id_is_kept_where_a_webhook_can_find_it(
        self,
        platform: FacebookPlatform,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get(f"/{PAGE_ID}").mock(
                return_value=httpx.Response(200, json=PAGES["data"][0])
            )

            step = await platform.resume_login(
                a_request(), resume_token="long", account_id=PAGE_ID
            )

        assert isinstance(step, Finished)
        assert step.connection.extra["page_id"] == PAGE_ID
        assert step.connection.extra["category"] == "Bakery"
        assert step.connection.extra["profile_url"].endswith(PAGE_ID)

    async def test_a_page_token_made_this_way_does_not_run_out(
        self,
        platform: FacebookPlatform,
    ) -> None:
        # This is the part that surprises people: the user token expires in
        # about sixty days, and the page token made from it does not expire
        # at all.
        with respx.mock(base_url=GRAPH_API) as network:
            network.get(f"/{PAGE_ID}").mock(
                return_value=httpx.Response(200, json=PAGES["data"][0])
            )

            step = await platform.resume_login(
                a_request(), resume_token="long", account_id=PAGE_ID
            )

        assert isinstance(step, Finished)
        assert step.connection.token.expires_at is None

    async def test_a_page_they_do_not_manage_says_what_to_do(
        self,
        platform: FacebookPlatform,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/999").mock(
                return_value=httpx.Response(200, json={"id": "999", "name": "Theirs"})
            )

            with pytest.raises(AuthError, match="connect their account again"):
                await platform.resume_login(
                    a_request(), resume_token="long", account_id="999"
                )

    async def test_carrying_on_with_no_token_from_the_first_half(
        self,
        platform: FacebookPlatform,
    ) -> None:
        with pytest.raises(AuthError, match="start a new one"):
            await platform.resume_login(
                a_request(), resume_token="", account_id=PAGE_ID
            )


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


class TestTokensThatDoNotRunOut:
    async def test_a_page_token_is_handed_straight_back(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(assert_all_called=False) as network:
            token = await platform.refresh(account)

        assert token is account.token
        assert not network.calls

    async def test_a_token_that_runs_out_is_traded_for_another_sixty_days(
        self,
        platform: FacebookPlatform,
    ) -> None:
        # Trading it in while it still works is the only renewal Meta has.
        running_out = an_account(expires_at=IN_AN_HOUR)

        with respx.mock(base_url=GRAPH_API) as network:
            swap = network.get("/oauth/access_token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "another-sixty", "expires_in": 5_184_000}
                )
            )

            token = await platform.refresh(running_out, an_app())

        asked = swap.calls.last.request.url.params
        assert asked["grant_type"] == "fb_exchange_token"
        assert asked["fb_exchange_token"] == PAGE_TOKEN
        assert asked["client_id"] == APP_ID
        assert asked["client_secret"] == APP_SECRET
        assert token.access_token == "another-sixty"
        assert token.expires_at is not None

    async def test_renewing_needs_your_apps_credentials(
        self,
        platform: FacebookPlatform,
    ) -> None:
        # Meta signs the trade with the app's id and secret, so a renewal
        # handed none has nothing to send.
        running_out = an_account(expires_at=IN_AN_HOUR)

        with respx.mock(assert_all_called=False) as network:
            with pytest.raises(ConfigError) as refused:
                await platform.refresh(running_out)

            assert not network.calls

        said = str(refused.value)
        assert "extend a token" in said
        assert DEVELOPER_PORTAL in said

    async def test_a_token_meta_will_not_trade_means_signing_in_again(
        self,
        platform: FacebookPlatform,
    ) -> None:
        # There is no refresh token to fall back on, so a refusal here is
        # the end of the connection rather than something to retry.
        running_out = an_account(expires_at=IN_AN_HOUR)

        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/oauth/access_token").mock(
                return_value=httpx.Response(
                    400,
                    json={"error": {"code": 190, "message": "Session has expired"}},
                )
            )

            with pytest.raises(TokenExpiredError) as refused:
                await platform.refresh(running_out, an_app())

        assert "connect their account again" in str(refused.value)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


class TestPublishingWords:
    async def test_it_posts_the_words_to_the_pages_feed(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            route = stub_feed(network)

            result = await platform.publish(account, Post(text="Fresh buns today"))

        assert sent_form(route) == {"message": "Fresh buns today", "published": "true"}
        assert route.calls.last.request.headers["Authorization"] == (
            f"Bearer {PAGE_TOKEN}"
        )
        assert result.id == POST_ID
        assert result.state is PostState.DONE
        assert result.url == f"https://www.facebook.com/{POST_ID}"
        assert result.raw == PUBLISHED

    async def test_a_link_rides_along_with_the_words(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        form = await publish_text(
            platform,
            account,
            Post(text="Read this", options={"link": "https://example.com/a"}),
        )

        assert form["link"] == "https://example.com/a"
        assert form["message"] == "Read this"

    async def test_it_posts_as_the_page_from_the_connections_extra(
        self,
        platform: FacebookPlatform,
    ) -> None:
        odd = an_account(extra={"page_id": "555"})
        with respx.mock(base_url=GRAPH_API) as network:
            route = network.post("/555/feed").mock(
                return_value=httpx.Response(200, json=PUBLISHED)
            )

            await platform.publish(odd, Post(text="Hello"))

        assert route.called

    async def test_an_older_connection_without_a_page_id_still_works(
        self,
        platform: FacebookPlatform,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            route = stub_feed(network)

            await platform.publish(an_account(extra={}), Post(text="Hello"))

        assert route.called

    async def test_a_connection_naming_no_page_at_all_says_so(
        self,
        platform: FacebookPlatform,
    ) -> None:
        nameless = Connection(
            id="facebook:broken",
            platform="facebook",
            host=None,
            account_id="",
            account_name="Broken",
            token=Token(access_token=PAGE_TOKEN),
        )

        with pytest.raises(ConfigError, match="page"):
            await platform.publish(nameless, Post(text="Hello"))

    async def test_a_post_longer_than_facebook_allows_never_leaves(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        with (
            respx.mock(assert_all_called=False) as network,
            pytest.raises(InvalidPostError, match="63206"),
        ):
            await platform.publish(account, Post(text="x" * (MAX_TEXT_LENGTH + 1)))
        assert not network.calls


class TestSchedulingForLater:
    async def test_facebook_really_does_publish_it_later(
        self,
        platform: FacebookPlatform,
        account: Connection,
        clock: datetime,
    ) -> None:
        when = NOW + timedelta(days=2)

        with respx.mock(base_url=GRAPH_API) as network:
            route = stub_feed(network)

            result = await platform.publish(
                account, Post(text="Out on Wednesday", publish_at=when)
            )

        form = sent_form(route)
        assert form["published"] == "false"
        assert form["scheduled_publish_time"] == str(int(when.timestamp()))
        assert result.state is PostState.SCHEDULED
        assert result.id == POST_ID

    async def test_a_scheduled_post_has_no_address_yet(
        self,
        platform: FacebookPlatform,
        account: Connection,
        clock: datetime,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            stub_feed(network)

            result = await platform.publish(
                account,
                Post(text="Later", publish_at=NOW + timedelta(days=2)),
            )

        assert result.url is None

    async def test_it_measures_from_the_real_clock(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        # No frozen clock here on purpose: the window is worked out against
        # the moment the post is actually sent.
        when = datetime.now(UTC) + timedelta(days=1)

        with respx.mock(base_url=GRAPH_API) as network:
            route = stub_feed(network)

            await platform.publish(account, Post(text="Later", publish_at=when))

        assert sent_form(route)["scheduled_publish_time"] == str(int(when.timestamp()))

    async def test_too_soon_is_refused_before_anything_is_sent(
        self,
        platform: FacebookPlatform,
        account: Connection,
        clock: datetime,
    ) -> None:
        with (
            respx.mock(assert_all_called=False) as network,
            pytest.raises(InvalidPostError, match="ten minutes"),
        ):
            await platform.publish(
                account,
                Post(text="Nearly now", publish_at=NOW + timedelta(minutes=5)),
            )
        assert not network.calls

    async def test_too_far_ahead_is_refused_before_anything_is_sent(
        self,
        platform: FacebookPlatform,
        account: Connection,
        clock: datetime,
    ) -> None:
        with (
            respx.mock(assert_all_called=False) as network,
            pytest.raises(InvalidPostError, match="75 days"),
        ):
            await platform.publish(
                account,
                Post(text="Next year", publish_at=NOW + timedelta(days=80)),
            )
        assert not network.calls


class TestPublishingPictures:
    async def test_one_picture_is_uploaded_then_hung_off_a_post(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        picture = Media.from_bytes(b"pretend png", filename="buns.png", alt_text="Buns")

        with respx.mock(base_url=GRAPH_API) as network:
            photos = network.post(f"/{PAGE_ID}/photos").mock(
                return_value=httpx.Response(200, json={"id": "photo-1"})
            )
            feed = stub_feed(network)

            await platform.publish(account, Post(text="Look", media=(picture,)))

        # Uploaded without being published, so it does not appear on its own
        # before the post that names it.
        assert "buns.png" in sent_parts(photos)
        assert 'name="published"' in sent_parts(photos)
        assert sent_form(feed)["attached_media[0]"] == '{"media_fbid": "photo-1"}'

    async def test_several_pictures_all_hang_off_the_one_post(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        pictures = (
            Media.from_bytes(b"one", filename="one.png"),
            Media.from_bytes(b"two", filename="two.png"),
        )

        with respx.mock(base_url=GRAPH_API) as network:
            network.post(f"/{PAGE_ID}/photos").mock(
                side_effect=[
                    httpx.Response(200, json={"id": "photo-1"}),
                    httpx.Response(200, json={"id": "photo-2"}),
                ]
            )
            feed = stub_feed(network)

            await platform.publish(account, Post(text="Two", media=pictures))

        form = sent_form(feed)
        assert form["attached_media[0]"] == '{"media_fbid": "photo-1"}'
        assert form["attached_media[1]"] == '{"media_fbid": "photo-2"}'

    async def test_facebook_will_fetch_a_picture_from_a_web_address(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        # Unlike Mastodon and Bluesky, Facebook downloads it itself, so
        # there is no need to fetch it here first.
        picture = Media.from_url("https://example.com/buns.png")

        with respx.mock(base_url=GRAPH_API) as network:
            photos = network.post(f"/{PAGE_ID}/photos").mock(
                return_value=httpx.Response(200, json={"id": "photo-1"})
            )
            stub_feed(network)

            await platform.publish(account, Post(text="Look", media=(picture,)))

        assert sent_form(photos)["url"] == "https://example.com/buns.png"

    async def test_a_photo_upload_with_no_id_in_it_says_so(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        picture = Media.from_bytes(b"pretend png", filename="buns.png")

        with respx.mock(base_url=GRAPH_API) as network:
            network.post(f"/{PAGE_ID}/photos").mock(
                return_value=httpx.Response(200, json={})
            )

            with pytest.raises(PlatformError, match="id"):
                await platform.publish(account, Post(text="Look", media=(picture,)))

    async def test_pictures_and_video_cannot_share_a_post(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        both = (
            Media.from_bytes(b"pretend png", filename="a.png"),
            Media.from_bytes(b"pretend mp4", filename="a.mp4"),
        )

        with (
            respx.mock(assert_all_called=False) as network,
            pytest.raises(InvalidPostError, match="not both"),
        ):
            await platform.publish(account, Post(media=both))
        assert not network.calls


class TestPublishingVideo:
    async def test_a_small_video_goes_up_in_one_request(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        clip = Media.from_bytes(b"pretend mp4", filename="buns.mp4")

        with respx.mock(base_url=GRAPH_API) as network:
            videos = network.post(f"/{PAGE_ID}/videos").mock(
                return_value=httpx.Response(200, json={"id": "video-1"})
            )

            result = await platform.publish(
                account, Post(text="Watch this", media=(clip,))
            )

        body = sent_parts(videos)
        assert "buns.mp4" in body
        assert "Watch this" in body
        # Facebook keeps encoding after it answers, so the post is not live
        # the moment publish() returns.
        assert result.state is PostState.PROCESSING
        assert result.id == "video-1"
        assert result.url == f"https://www.facebook.com/{PAGE_ID}/videos/video-1"

    async def test_facebook_will_fetch_a_video_from_a_web_address(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        clip = Media.from_url("https://example.com/buns.mp4")

        with respx.mock(base_url=GRAPH_API) as network:
            videos = network.post(f"/{PAGE_ID}/videos").mock(
                return_value=httpx.Response(200, json={"id": "video-1"})
            )

            await platform.publish(account, Post(text="Watch", media=(clip,)))

        assert sent_form(videos)["file_url"] == "https://example.com/buns.mp4"

    async def test_a_scheduled_video_comes_back_as_scheduled(
        self,
        platform: FacebookPlatform,
        account: Connection,
        clock: datetime,
    ) -> None:
        clip = Media.from_bytes(b"pretend mp4", filename="buns.mp4")
        when = NOW + timedelta(days=1)

        with respx.mock(base_url=GRAPH_API) as network:
            network.post(f"/{PAGE_ID}/videos").mock(
                return_value=httpx.Response(200, json={"id": "video-1"})
            )

            result = await platform.publish(
                account, Post(text="Later", media=(clip,), publish_at=when)
            )

        assert result.state is PostState.SCHEDULED
        assert result.url is None

    async def test_a_video_too_big_for_one_request_is_refused_clearly(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        # A real gigabyte is not worth building in memory to prove this,
        # so the platform is told to draw the line lower.
        small = FacebookPlatform(retries=ONCE, biggest_video_bytes=10)
        huge = Media.from_bytes(b"x" * 11, filename="epic.mp4")

        with (
            respx.mock(assert_all_called=False) as network,
            pytest.raises(NotSupportedError) as refused,
        ):
            await small.publish(account, Post(media=(huge,)))

        assert not network.calls
        said = str(refused.value)
        # It has to say what to do, not just that it will not.
        assert "in pieces" in said
        assert "gigabyte" in said

    async def test_the_line_it_draws_is_the_one_it_reports(
        self,
        account: Connection,
    ) -> None:
        small = FacebookPlatform(retries=ONCE, biggest_video_bytes=10)

        assert (await small.limits(account)).max_video_bytes == 10

    async def test_two_videos_on_one_post_are_refused(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        clips = (
            Media.from_bytes(b"one", filename="one.mp4"),
            Media.from_bytes(b"two", filename="two.mp4"),
        )

        with (
            respx.mock(assert_all_called=False) as network,
            pytest.raises(InvalidPostError),
        ):
            await platform.publish(account, Post(media=clips))
        assert not network.calls


# ---------------------------------------------------------------------------
# What happens after a video is accepted
# ---------------------------------------------------------------------------


def stub_status(network: respx.Router, said: dict[str, Any]) -> respx.Route:
    """Answer "how is this video getting on?" with what Facebook would say."""
    return network.get(f"/{VIDEO_ID}").mock(return_value=httpx.Response(200, json=said))


class TestAskingHowAVideoIsGettingOn:
    def test_it_offers_asking_how_a_post_is_getting_on(
        self,
        platform: FacebookPlatform,
    ) -> None:
        # publish() answers PROCESSING for a video, so there has to be
        # somewhere to ask. Without this, Account.check_state refuses and
        # the state means nothing anybody can act on.
        assert isinstance(platform, CanCheckState)

    async def test_it_asks_facebook_for_the_one_field_that_says(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            route = stub_status(
                network,
                {
                    "id": VIDEO_ID,
                    "status": {"video_status": "ready", "processing_progress": 100},
                },
            )

            result = await platform.check_state(account, VIDEO_ID)

        asked = route.calls.last.request
        assert asked.url.params["fields"] == "status"
        assert asked.headers["Authorization"] == f"Bearer {PAGE_TOKEN}"
        assert result.id == VIDEO_ID
        assert result.state is PostState.DONE
        assert result.is_done
        assert result.url == f"https://www.facebook.com/{PAGE_ID}/videos/{VIDEO_ID}"

    @pytest.mark.parametrize(
        ("video_status", "expected"),
        [
            ("ready", PostState.DONE),
            ("processing", PostState.PROCESSING),
            ("error", PostState.FAILED),
            # A word Meta adds next year means "ask again", never "done".
            ("something-new", PostState.PROCESSING),
        ],
    )
    async def test_it_reads_every_state_facebook_reports(
        self,
        platform: FacebookPlatform,
        account: Connection,
        video_status: str,
        expected: PostState,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            stub_status(
                network, {"id": VIDEO_ID, "status": {"video_status": video_status}}
            )

            result = await platform.check_state(account, VIDEO_ID)

        assert result.state is expected

    async def test_the_state_publish_gives_back_can_be_resolved(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        # The whole point: what publish() hands back is something you can
        # then go and ask about.
        clip = Media.from_bytes(b"pretend mp4", filename="buns.mp4")

        with respx.mock(base_url=GRAPH_API) as network:
            network.post(f"/{PAGE_ID}/videos").mock(
                return_value=httpx.Response(200, json={"id": VIDEO_ID})
            )
            stub_status(network, {"id": VIDEO_ID, "status": {"video_status": "ready"}})

            published = await platform.publish(
                account, Post(text="Watch this", media=(clip,))
            )
            settled = await platform.check_state(account, published.id)

        assert published.state is PostState.PROCESSING
        assert settled.state is PostState.DONE
        assert settled.url == published.url

    async def test_it_keeps_what_facebook_said_about_the_allowance(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get(f"/{VIDEO_ID}").mock(
                return_value=httpx.Response(
                    200,
                    json={"id": VIDEO_ID, "status": {"video_status": "processing"}},
                    headers={
                        "X-App-Usage": json.dumps(
                            {"call_count": 12, "total_time": 3, "total_cputime": 1}
                        )
                    },
                )
            )

            await platform.check_state(account, VIDEO_ID)

        assert platform.usage is not None
        assert platform.usage.calls == 12

    async def test_it_says_so_when_facebook_answers_without_a_status(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            stub_status(network, {"id": VIDEO_ID})

            with pytest.raises(PlatformError) as refused:
                await platform.check_state(account, VIDEO_ID)

        said = str(refused.value)
        assert VIDEO_ID in said
        # It has to say what check_state is for, or the next thing somebody
        # does is call it on a text post and get the same message again.
        assert "video" in said

    async def test_a_video_facebook_has_never_heard_of(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get(f"/{VIDEO_ID}").mock(
                return_value=httpx.Response(404, text="gone")
            )

            with pytest.raises(NotFoundError):
                await platform.check_state(account, VIDEO_ID)

    async def test_it_refuses_a_connection_that_names_no_page(
        self,
        platform: FacebookPlatform,
    ) -> None:
        with (
            respx.mock(assert_all_called=False) as network,
            pytest.raises(ConfigError),
        ):
            await platform.check_state(an_account(page_id="", extra={}), VIDEO_ID)

        assert not network.calls


class TestPostOptions:
    async def test_it_refuses_a_setting_facebook_does_not_know(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        with (
            respx.mock(assert_all_called=False) as network,
            pytest.raises(InvalidPostError, match="link"),
        ):
            await platform.publish(
                account, Post(text="Hi", options={"visibility": "public"})
            )
        assert not network.calls

    async def test_a_link_has_to_be_some_text(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(InvalidPostError, match="link"):
            await platform.publish(account, Post(text="Hi", options={"link": 7}))


class TestWatchingTheAllowance:
    async def test_it_knows_nothing_until_facebook_has_said_something(
        self,
        platform: FacebookPlatform,
    ) -> None:
        assert platform.usage is None

    async def test_it_keeps_what_facebook_said_about_the_hourly_allowance(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.post(f"/{PAGE_ID}/feed").mock(
                return_value=httpx.Response(
                    200,
                    json=PUBLISHED,
                    headers={
                        "X-App-Usage": json.dumps(
                            {"call_count": 42, "total_cputime": 8, "total_time": 9}
                        )
                    },
                )
            )

            await platform.publish(account, Post(text="Hello"))

        seen = platform.usage
        assert seen is not None
        assert seen.calls == 42
        assert seen.worst == 42


class TestDeleting:
    async def test_it_removes_a_post(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            route = network.delete(f"/{POST_ID}").mock(
                return_value=httpx.Response(200, json={"success": True})
            )

            await platform.delete_post(account, POST_ID)

        assert route.calls.last.request.headers["Authorization"] == (
            f"Bearer {PAGE_TOKEN}"
        )


# ---------------------------------------------------------------------------
# When Facebook says no
# ---------------------------------------------------------------------------


class TestWhenFacebookSaysNo:
    async def test_a_refusal_hiding_in_a_happy_reply_still_stops_us(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.post(f"/{PAGE_ID}/feed").mock(
                return_value=httpx.Response(200, json=an_error(190))
            )

            with pytest.raises(AuthError):
                await platform.publish(account, Post(text="Hello"))

    async def test_being_asked_to_slow_down_comes_back_as_ours(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.post(f"/{PAGE_ID}/feed").mock(
                return_value=httpx.Response(
                    400,
                    json=an_error(32, "Page request limit reached"),
                    headers={
                        "X-Business-Use-Case-Usage": json.dumps(
                            {PAGE_ID: [{"estimated_time_to_regain_access": 3}]}
                        )
                    },
                )
            )

            with pytest.raises(RateLimitError) as complaint:
                await platform.publish(account, Post(text="Hello"))

        assert complaint.value.retry_after == 180.0

    async def test_a_missing_post_is_a_missing_post(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.delete(f"/{POST_ID}").mock(
                return_value=httpx.Response(404, text="gone")
            )

            with pytest.raises(NotFoundError):
                await platform.delete_post(account, POST_ID)

    def test_the_error_mapping_is_handed_out_for_reuse(self) -> None:
        reply = httpx.Response(400, json=an_error(190))

        assert isinstance(facebook_errors(reply), AuthError)

    async def test_a_reply_with_no_post_id_says_so_plainly(
        self,
        platform: FacebookPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.post(f"/{PAGE_ID}/feed").mock(
                return_value=httpx.Response(200, json={})
            )

            with pytest.raises(PlatformError, match="publish a post"):
                await platform.publish(account, Post(text="Hello"))


# ---------------------------------------------------------------------------
# Being told something happened
# ---------------------------------------------------------------------------


class TestCheckingWhatFacebookPushed:
    def test_a_body_signed_with_the_app_secret_is_accepted(
        self,
        platform: FacebookPlatform,
    ) -> None:
        body = a_push({"item": "comment", "verb": "add"})

        platform.check_signature(body, signed(body), secret=APP_SECRET)

    def test_a_body_that_was_changed_on_the_way_here_is_refused(
        self,
        platform: FacebookPlatform,
    ) -> None:
        body = a_push({"item": "comment", "verb": "add"})
        headers = signed(body)

        with pytest.raises(SignatureError):
            platform.check_signature(body + b" ", headers, secret=APP_SECRET)

    def test_a_body_signed_with_the_wrong_secret_is_refused(
        self,
        platform: FacebookPlatform,
    ) -> None:
        body = a_push({"item": "comment", "verb": "add"})

        with pytest.raises(SignatureError):
            platform.check_signature(
                body, signed(body, secret="not-it"), secret=APP_SECRET
            )

    def test_a_typed_caller_can_reach_the_setup_check_and_the_updates(
        self,
        platform: FacebookPlatform,
    ) -> None:
        # SocialChimp.answer_setup_check and SocialChimp.read_updates
        # both look for these before they will hand anything on.
        assert isinstance(platform, CanAnswerSetupCheck)
        assert isinstance(platform, CanReadPushedUpdates)

    def test_it_answers_metas_one_off_setup_question(
        self,
        platform: FacebookPlatform,
    ) -> None:
        answer = platform.answer_setup_check(
            {
                "hub.mode": "subscribe",
                "hub.challenge": "1158201444",
                "hub.verify_token": "the-one-we-typed-in",
            },
            verify_token="the-one-we-typed-in",
        )

        assert answer == "1158201444"

    def test_a_setup_question_with_the_wrong_token_is_refused(
        self,
        platform: FacebookPlatform,
    ) -> None:
        with pytest.raises(SignatureError):
            platform.answer_setup_check(
                {
                    "hub.mode": "subscribe",
                    "hub.challenge": "1158201444",
                    "hub.verify_token": "guessed",
                },
                verify_token="the-one-we-typed-in",
            )


class TestReadingWhatFacebookPushed:
    def test_a_new_comment_arrives_as_a_new_comment(
        self,
        platform: FacebookPlatform,
    ) -> None:
        body = a_push(
            {
                "item": "comment",
                "verb": "add",
                "comment_id": f"{POST_ID}_777",
                "post_id": POST_ID,
                "created_time": 1_790_000_500,
                "from": {"id": "user-1", "name": "Bob"},
                "message": "Lovely",
            }
        )

        update = platform.read_update(body, {})

        assert update.kind is UpdateKind.COMMENT_CREATED
        assert update.platform == "facebook"
        assert update.created_at == datetime.fromtimestamp(1_790_000_500, UTC)
        assert update.raw["message"] == "Lovely"

    def test_each_update_carries_its_own_change_and_nothing_else(
        self,
        platform: FacebookPlatform,
    ) -> None:
        # One entry, two changes, two updates - and each `raw` is the change
        # that update is about. A handler reads it straight, rather than
        # walking the entry looking for its own change again.
        body = json.dumps(
            {
                "object": "page",
                "entry": [
                    {
                        "id": PAGE_ID,
                        "time": 1_790_000_000,
                        "changes": [
                            {
                                "field": "feed",
                                "value": {
                                    "item": "comment",
                                    "verb": "add",
                                    "comment_id": "c-1",
                                    "message": "First",
                                },
                            },
                            {
                                "field": "feed",
                                "value": {
                                    "item": "comment",
                                    "verb": "add",
                                    "comment_id": "c-2",
                                    "message": "Second",
                                },
                            },
                        ],
                    }
                ],
            }
        ).encode()

        first, second = platform.read_updates(body)

        assert first.raw["message"] == "First"
        assert second.raw["message"] == "Second"

    def test_the_entry_it_arrived_in_is_kept_beside_the_change(
        self,
        platform: FacebookPlatform,
    ) -> None:
        # The page id and the time live out on the entry, so the entry is
        # kept - just not where the change should be.
        update = platform.read_update(a_push({"item": "comment", "verb": "add"}), {})

        assert update.envelope["id"] == PAGE_ID
        assert update.envelope["time"] == 1_790_000_000

    def test_it_names_the_connection_the_way_a_login_named_it(
        self,
        platform: FacebookPlatform,
    ) -> None:
        # Meta says which page, not which of your connections. The two are
        # tied together by the id a login builds, so an app can look the
        # connection up without a table of its own.
        body = a_push({"item": "comment", "verb": "add"})

        assert platform.read_update(body, {}).connection_id == f"facebook:{PAGE_ID}"

    def test_a_removed_comment_arrives_as_a_removed_comment(
        self,
        platform: FacebookPlatform,
    ) -> None:
        body = a_push(
            {"item": "comment", "verb": "remove", "comment_id": f"{POST_ID}_777"}
        )

        assert platform.read_update(body, {}).kind is UpdateKind.COMMENT_DELETED

    @pytest.mark.parametrize("item", ["reaction", "like"])
    def test_somebody_reacting_arrives_as_a_reaction(
        self,
        platform: FacebookPlatform,
        item: str,
    ) -> None:
        body = a_push(
            {
                "item": item,
                "verb": "add",
                "reaction_type": "love",
                "post_id": POST_ID,
                "from": {"id": "user-1"},
            }
        )

        assert platform.read_update(body, {}).kind is UpdateKind.REACTION_ADDED

    def test_something_we_have_no_name_for_keeps_facebooks_own_words(
        self,
        platform: FacebookPlatform,
    ) -> None:
        body = a_push({"item": "photo", "verb": "edited", "post_id": POST_ID})

        update = platform.read_update(body, {})

        assert update.kind is UpdateKind.UNKNOWN
        assert update.kind_name == "photo edited"

    def test_a_topic_we_do_not_watch_still_reaches_your_app(
        self,
        platform: FacebookPlatform,
    ) -> None:
        body = a_push({"rating": 5}, topic="ratings")

        update = platform.read_update(body, {})

        assert update.kind is UpdateKind.UNKNOWN
        assert update.kind_name == "ratings"

    def test_somebody_naming_the_page_arrives_as_a_mention(
        self,
        platform: FacebookPlatform,
    ) -> None:
        # Facebook's own name for this topic is already the word socialchimp
        # uses, so subscribing to it needs nothing else here.
        body = a_push({"post_id": POST_ID, "sender_id": "user-1"}, topic="mention")

        assert platform.read_update(body, {}).kind is UpdateKind.MENTION

    def test_the_same_change_twice_carries_the_same_id(
        self,
        platform: FacebookPlatform,
    ) -> None:
        # Meta puts no id of its own on a change, and promises to deliver at
        # least once - so one built from the change is what stops a comment
        # being answered twice.
        body = a_push(
            {
                "item": "comment",
                "verb": "add",
                "comment_id": f"{POST_ID}_777",
                "post_id": POST_ID,
            }
        )

        first = platform.read_update(body, {})
        second = platform.read_update(body, {})

        assert first.id == second.id
        assert f"{POST_ID}_777" in first.id

    def test_two_different_changes_do_not_share_an_id(
        self,
        platform: FacebookPlatform,
    ) -> None:
        added = a_push({"item": "comment", "verb": "add", "comment_id": "c1"})
        removed = a_push({"item": "comment", "verb": "remove", "comment_id": "c1"})

        assert (
            platform.read_update(added, {}).id != platform.read_update(removed, {}).id
        )

    def test_one_message_can_carry_several_changes(
        self,
        platform: FacebookPlatform,
    ) -> None:
        body = json.dumps(
            {
                "object": "page",
                "entry": [
                    {
                        "id": PAGE_ID,
                        "time": 1_790_000_000,
                        "changes": [
                            {
                                "field": "feed",
                                "value": {"item": "comment", "verb": "add"},
                            },
                            {
                                "field": "feed",
                                "value": {"item": "reaction", "verb": "add"},
                            },
                        ],
                    }
                ],
            }
        ).encode()

        updates = platform.read_updates(body)

        assert [update.kind for update in updates] == [
            UpdateKind.COMMENT_CREATED,
            UpdateKind.REACTION_ADDED,
        ]

    def test_read_update_takes_the_first_of_a_batch(
        self,
        platform: FacebookPlatform,
    ) -> None:
        body = json.dumps(
            {
                "entry": [
                    {
                        "id": PAGE_ID,
                        "time": 1_790_000_000,
                        "changes": [
                            {"field": "feed", "value": {"item": "comment"}},
                            {"field": "feed", "value": {"item": "reaction"}},
                        ],
                    }
                ]
            }
        ).encode()

        assert platform.read_update(body, {}).kind_name == "comment"
        assert len(platform.read_updates(body)) == 2

    def test_a_message_with_nothing_in_it_says_to_use_read_updates(
        self,
        platform: FacebookPlatform,
    ) -> None:
        with pytest.raises(PlatformError, match="read_updates"):
            platform.read_update(b'{"object": "page", "entry": []}', {})


# ---------------------------------------------------------------------------
# The shared checks every platform has to pass
# ---------------------------------------------------------------------------


class TestFacebookBehavesLikeTheOthers(PlatformChecks):
    def make_platform(self) -> Platform:
        return FacebookPlatform(transport=self.transport, retries=ONCE)

    def make_connection(self) -> Connection | None:
        return an_account()

    def make_transport(self) -> httpx.AsyncBaseTransport | None:
        return RecordingTransport({f"POST /v21.0/{PAGE_ID}/feed": PUBLISHED})
