"""Tests for the TikTok platform."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
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
    MediaKind,
    NotAllowedError,
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
    CanCheckState,
    CanCreateApp,
    CanReadPushedUpdates,
    CanReadUpdates,
    CanResumeLogin,
    Finished,
    LoginRequest,
    Platform,
    SendToNetwork,
)
from socialchimp.platforms.tiktok import (
    MAX_CAPTION_UNITS,
    MAX_CHUNKS,
    MAX_VIDEO_BYTES,
    MIN_CHUNK_BYTES,
    SIGNATURE_HEADER,
    TikTokPlatform,
    _chunk_plan,
    tiktok_errors,
)
from socialchimp.testing import PlatformChecks, RecordingTransport

REDIRECT = "https://app.example/callback"
OPEN_ID = "open-id-ada"
PUBLISH_ID = "v_pub_file~v2-1.1234567890"
UPLOAD_TO = "https://open-upload.tiktokapis.com/upload"

INIT_DIRECT = "https://open.tiktokapis.com/v2/post/publish/video/init/"
INIT_INBOX = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
USER_URL = "https://open.tiktokapis.com/v2/user/info/"
TOKEN_ENDPOINT = "https://open.tiktokapis.com/v2/oauth/token/"

# One try and no waiting, so the error tests do not spend real seconds asleep.
ONCE = Retries(attempts=1)

APP = AppCredentials(
    platform="tiktok",
    host=None,
    client_id="awx1234567890",
    client_secret="tiktok-client-secret",
)

A_MEGABYTE = 1024 * 1024

# A moment to hold still at, for everything that would otherwise be timed
# against whatever the clock happened to say while the test ran.
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


WHEN = int(NOW.timestamp())


def at(moment: datetime = NOW) -> Callable[[], datetime]:
    """A clock that always says the same thing."""
    return lambda: moment


@pytest.fixture
def platform() -> TikTokPlatform:
    """A platform that gives up after one try, with the clock held still."""
    return TikTokPlatform(retries=ONCE, now=at())


@pytest.fixture
def account() -> Connection:
    """A connected TikTok account."""
    return Connection(
        id=f"tiktok:{OPEN_ID}",
        platform="tiktok",
        host=None,
        account_id=OPEN_ID,
        account_name="Ada",
        token=Token(
            access_token="access-one",
            refresh_token="refresh-one",
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        ),
        scopes=("user.info.basic", "video.upload", "video.publish"),
        extra={"open_id": OPEN_ID, "username": "ada"},
    )


def login(
    *,
    state: str | None = None,
    scopes: tuple[str, ...] = (),
    app: AppCredentials | None = APP,
) -> LoginRequest:
    """A login request with the everyday values already filled in."""
    return LoginRequest(redirect_uri=REDIRECT, scopes=scopes, state=state, app=app)


def a_video(size: int = 24, name: str = "clip.mp4") -> Media:
    """A tiny video held in memory."""
    return Media.from_bytes(b"v" * size, filename=name)


def a_file_video(tmp_path: Path, size: int, name: str = "clip.mp4") -> Media:
    """A video of any size on disk, made without filling memory with it."""
    where = tmp_path / name
    with where.open("wb") as opened:
        opened.truncate(size)
    return Media.from_file(where)


def a_post(**options: object) -> Post:
    """A post TikTok would take, with settings layered on top."""
    settings: dict[str, Any] = {"send_to": "profile"}
    settings.update(options)
    return Post(text="A caption", media=(a_video(),), options=settings)


def ok(data: dict[str, Any]) -> dict[str, Any]:
    """A happy reply, wrapped the way TikTok wraps every one of them."""
    return {"data": data, "error": {"code": "ok", "message": "", "log_id": "log-1"}}


def refusal(code: str, message: str = "no") -> dict[str, Any]:
    """A refusal, wrapped the way TikTok wraps every one of them."""
    return {"error": {"code": code, "message": message, "log_id": "log-1"}}


def an_init_reply(**extra: object) -> dict[str, Any]:
    """What an init call answers with."""
    data: dict[str, Any] = {"publish_id": PUBLISH_ID, "upload_url": UPLOAD_TO}
    data.update(extra)
    return ok(data)


def token_reply(**extra: object) -> dict[str, Any]:
    """What TikTok's token endpoint answers with."""
    said: dict[str, Any] = {
        "access_token": "access-one",
        "expires_in": 86400,
        "open_id": OPEN_ID,
        "refresh_token": "refresh-one",
        "refresh_expires_in": 31536000,
        "scope": "user.info.basic,video.upload,video.publish",
        "token_type": "Bearer",
    }
    said.update(extra)
    return said


def user_reply(**extra: object) -> dict[str, Any]:
    """What /v2/user/info/ answers with."""
    user: dict[str, Any] = {
        "open_id": OPEN_ID,
        "display_name": "Ada Lovelace",
        "username": "ada",
    }
    user.update(extra)
    return ok({"user": user})


def form_of(request: httpx.Request) -> dict[str, list[str]]:
    """Read a sent form back into a dictionary."""
    return parse_qs(request.content.decode(), keep_blank_values=True)


def body_of(request: httpx.Request) -> dict[str, Any]:
    """Read a sent JSON body back into a dictionary."""
    parsed: dict[str, Any] = json.loads(request.content)
    return parsed


def upload_routes(
    network: respx.Router,
    *,
    init: str = INIT_DIRECT,
    reply: dict[str, Any] | None = None,
    puts: Callable[[httpx.Request], httpx.Response] | None = None,
) -> tuple[respx.Route, respx.Route]:
    """Set up the two calls every upload makes: init, then the bytes."""
    started = network.post(init).mock(
        return_value=httpx.Response(
            200, json=reply if reply is not None else an_init_reply()
        )
    )
    sending = network.put(UPLOAD_TO)
    if puts is not None:
        sending.mock(side_effect=puts)
    else:
        sending.mock(return_value=httpx.Response(201))
    return started, sending


def quiet_network() -> respx.MockRouter:
    """A router for the tests whose point is that nothing was sent."""
    return respx.mock(assert_all_called=False)


async def sign_in(
    platform: TikTokPlatform,
    network: respx.Router,
    *,
    reply: dict[str, Any] | None = None,
    remember: dict[str, Any] | None = None,
) -> Finished:
    """Run a whole sign-in and insist it finished."""
    network.post(TOKEN_ENDPOINT).mock(
        return_value=httpx.Response(
            200, json=reply if reply is not None else token_reply()
        )
    )
    network.get(USER_URL).mock(return_value=httpx.Response(200, json=user_reply()))
    step = await platform.finish_login(login(), {"code": "the-code"}, remember)
    assert isinstance(step, Finished)
    return step


def signature_for(
    body: bytes,
    *,
    secret: str = APP.client_secret,
    at: int = WHEN,
) -> dict[str, str]:
    """Sign a body the way TikTok signs the ones it pushes to us."""
    when = at
    signed = f"{when}.".encode() + body
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return {SIGNATURE_HEADER: f"t={when},s={digest}"}


def an_event(
    name: str,
    *,
    publish_id: str = PUBLISH_ID,
    at: int = 1790000000,
    content: dict[str, Any] | None = None,
) -> bytes:
    """One of the messages TikTok pushes to us, as raw bytes."""
    inside = content if content is not None else {"publish_id": publish_id}
    payload = {
        "client_key": APP.client_id,
        "event": name,
        "create_time": at,
        "user_openid": OPEN_ID,
        # TikTok puts the interesting half in as a *string* of JSON rather
        # than as an object, which is the thing that catches people out.
        "content": json.dumps(inside),
    }
    return json.dumps(payload).encode()


# ---------------------------------------------------------------------------
# What it says it can do
# ---------------------------------------------------------------------------


class TestWhatItSaysItCanDo:
    def test_it_provides_everything_a_platform_must(
        self, platform: TikTokPlatform
    ) -> None:
        checked: Platform = platform
        pushes: CanCheckSignature = platform

        assert isinstance(checked, Platform)
        assert isinstance(pushes, CanCheckSignature)
        assert platform.name == "tiktok"

    def test_it_lists_the_features_tiktok_really_has(
        self, platform: TikTokPlatform
    ) -> None:
        assert Feature.POST_VIDEO in platform.features
        assert Feature.PUSH_UPDATES in platform.features

    def test_it_does_not_claim_it_can_post_words_on_their_own(
        self, platform: TikTokPlatform
    ) -> None:
        # A TikTok is a video. There is no post made of words here.
        assert Feature.POST_TEXT not in platform.features

    def test_it_does_not_claim_it_can_post_pictures(
        self, platform: TikTokPlatform
    ) -> None:
        # Photo carousels exist, but they are a different endpoint that
        # fetches from web addresses rather than taking an upload.
        assert Feature.POST_IMAGE not in platform.features

    def test_it_does_not_claim_it_can_schedule(self, platform: TikTokPlatform) -> None:
        # TikTok's API has no way to say "publish this on Friday".
        assert Feature.SCHEDULE not in platform.features

    def test_it_does_not_claim_it_can_delete_or_reply(
        self, platform: TikTokPlatform
    ) -> None:
        assert Feature.DELETE_POST not in platform.features
        assert Feature.REPLY not in platform.features
        assert not hasattr(platform, "delete_post")

    def test_it_does_not_claim_to_be_asked_on_a_timer(
        self, platform: TikTokPlatform
    ) -> None:
        # TikTok pushes publish results to us, so there is nothing to ask for.
        assert not isinstance(platform, CanReadUpdates)

    def test_it_never_pauses_to_ask_which_account(
        self, platform: TikTokPlatform
    ) -> None:
        # One token, one account. There is nothing to choose between.
        assert not isinstance(platform, CanResumeLogin)


class TestWhereTheApiIs:
    def test_every_account_uses_the_same_address(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        assert platform.api_base(account) == "https://open.tiktokapis.com/v2"

    def test_the_headers_carry_the_accounts_own_token(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        assert platform.auth_headers(account) == {"Authorization": "Bearer access-one"}


# ---------------------------------------------------------------------------
# Registering an app, and the audit that catches everyone
# ---------------------------------------------------------------------------


class TestRegisteringAnApp:
    def test_it_cannot_register_an_app_for_you(self, platform: TikTokPlatform) -> None:
        assert Feature.CREATE_APP not in platform.features
        assert not isinstance(platform, CanCreateApp)
        assert not hasattr(platform, "create_app")

    async def test_starting_a_login_without_credentials_warns_about_the_audit(
        self,
    ) -> None:
        bare = TikTokPlatform()

        with pytest.raises(ConfigError) as refused:
            await bare.start_login(LoginRequest(redirect_uri=REDIRECT))

        said = str(refused.value)
        assert "developers.tiktok.com" in said
        # The trap: an app that has not been audited posts everything
        # privately, whatever privacy level was asked for.
        assert "audit" in said
        assert "SELF_ONLY" in said
        assert "5" in said

    async def test_renewing_without_credentials_says_the_same_thing(
        self, account: Connection
    ) -> None:
        bare = TikTokPlatform()

        with pytest.raises(ConfigError, match=r"developers\.tiktok\.com"):
            await bare.refresh(account)

    async def test_the_module_says_the_audit_trap_out_loud(self) -> None:
        from socialchimp.platforms import tiktok

        said = tiktok.__doc__ or ""
        assert "SELF_ONLY" in said
        assert "audit" in said


# ---------------------------------------------------------------------------
# Signing someone in
# ---------------------------------------------------------------------------


class TestSendingSomeoneToTikTok:
    async def test_the_address_is_tiktoks_own_sign_in_page(
        self, platform: TikTokPlatform
    ) -> None:
        step = await platform.start_login(login())

        assert isinstance(step, SendToNetwork)
        assert step.url.startswith("https://www.tiktok.com/v2/auth/authorize/?")
        query = parse_qs(urlparse(step.url).query)
        assert query["response_type"] == ["code"]
        # TikTok calls it client_key, not client_id.
        assert query["client_key"] == [APP.client_id]
        assert query["redirect_uri"] == [REDIRECT]
        assert query["state"] == [step.state]

    async def test_it_asks_for_the_three_scopes_posting_needs(
        self, platform: TikTokPlatform
    ) -> None:
        step = await platform.start_login(login())

        assert isinstance(step, SendToNetwork)
        query = parse_qs(urlparse(step.url).query)
        # Commas, not spaces. TikTok is the odd one out here.
        assert query["scope"] == ["user.info.basic,video.upload,video.publish"]

    async def test_it_asks_for_the_scopes_you_named(
        self, platform: TikTokPlatform
    ) -> None:
        step = await platform.start_login(
            login(scopes=("user.info.basic", "video.upload"))
        )

        assert isinstance(step, SendToNetwork)
        query = parse_qs(urlparse(step.url).query)
        assert query["scope"] == ["user.info.basic,video.upload"]

    async def test_it_keeps_the_state_you_gave_it(
        self, platform: TikTokPlatform
    ) -> None:
        step = await platform.start_login(login(state="mine"))

        assert isinstance(step, SendToNetwork)
        assert step.state == "mine"

    async def test_two_logins_never_share_a_state(
        self, platform: TikTokPlatform
    ) -> None:
        first = await platform.start_login(login())
        second = await platform.start_login(login())

        assert isinstance(first, SendToNetwork)
        assert isinstance(second, SendToNetwork)
        assert first.state != second.state

    async def test_a_web_app_sends_no_pkce_and_keeps_nothing(
        self, platform: TikTokPlatform
    ) -> None:
        step = await platform.start_login(login())

        assert isinstance(step, SendToNetwork)
        query = parse_qs(urlparse(step.url).query)
        assert "code_challenge" not in query
        assert step.remember == {}

    async def test_a_desktop_app_sends_pkce_and_keeps_the_secret(self) -> None:
        platform = TikTokPlatform(pkce=True)

        step = await platform.start_login(login())

        assert isinstance(step, SendToNetwork)
        verifier = step.remember["code_verifier"]
        query = parse_qs(urlparse(step.url).query)
        assert query["code_challenge_method"] == ["S256"]
        # TikTok wants the hash written as hex, where everybody else wants
        # it as base64. Sending base64 fails with a puzzling message.
        expected = hashlib.sha256(str(verifier).encode()).hexdigest()
        assert query["code_challenge"] == [expected]
        # The secret itself never leaves your server.
        assert str(verifier) not in step.url

    async def test_the_pkce_secret_is_long_enough_for_the_rules(self) -> None:
        platform = TikTokPlatform(pkce=True)

        step = await platform.start_login(login())

        assert isinstance(step, SendToNetwork)
        # PKCE asks for between 43 and 128 characters.
        assert 43 <= len(str(step.remember["code_verifier"])) <= 128

    async def test_two_logins_never_share_a_pkce_secret(self) -> None:
        platform = TikTokPlatform(pkce=True)

        first = await platform.start_login(login())
        second = await platform.start_login(login())

        assert isinstance(first, SendToNetwork)
        assert isinstance(second, SendToNetwork)
        assert first.remember["code_verifier"] != second.remember["code_verifier"]


class TestSwappingTheCodeForAToken:
    async def test_it_sends_everything_tiktoks_token_endpoint_wants(
        self, platform: TikTokPlatform
    ) -> None:
        with respx.mock() as network:
            route = network.post(TOKEN_ENDPOINT).mock(
                return_value=httpx.Response(200, json=token_reply())
            )
            network.get(USER_URL).mock(
                return_value=httpx.Response(200, json=user_reply())
            )

            await platform.finish_login(login(), {"code": "the-code"})

        sent = form_of(route.calls.last.request)
        assert sent["grant_type"] == ["authorization_code"]
        assert sent["code"] == ["the-code"]
        assert sent["client_key"] == [APP.client_id]
        assert sent["client_secret"] == [APP.client_secret]
        assert sent["redirect_uri"] == [REDIRECT]
        assert "code_verifier" not in sent

    async def test_it_sends_the_pkce_secret_back_when_your_app_kept_one(
        self, platform: TikTokPlatform
    ) -> None:
        with respx.mock() as network:
            route = network.post(TOKEN_ENDPOINT).mock(
                return_value=httpx.Response(200, json=token_reply())
            )
            network.get(USER_URL).mock(
                return_value=httpx.Response(200, json=user_reply())
            )

            await platform.finish_login(
                login(), {"code": "the-code"}, {"code_verifier": "secret-half"}
            )

        assert form_of(route.calls.last.request)["code_verifier"] == ["secret-half"]

    async def test_it_hands_back_a_connection_worth_saving(
        self, platform: TikTokPlatform
    ) -> None:
        with respx.mock() as network:
            step = await sign_in(platform, network)

        connected = step.connection
        assert connected.id == f"tiktok:{OPEN_ID}"
        assert connected.platform == "tiktok"
        assert connected.account_id == OPEN_ID
        assert connected.account_name == "Ada Lovelace"
        assert connected.token.access_token == "access-one"
        assert connected.token.refresh_token == "refresh-one"
        assert connected.scopes == ("user.info.basic", "video.upload", "video.publish")
        assert connected.extra["username"] == "ada"

    async def test_the_token_it_saves_runs_out_when_tiktok_says_it_does(
        self, platform: TikTokPlatform
    ) -> None:
        with respx.mock() as network:
            step = await sign_in(platform, network)

        # A day, which is what TikTok gives.
        assert step.connection.token.expires_at == NOW + timedelta(seconds=86400)

    async def test_a_token_with_no_expiry_is_treated_as_lasting_a_day(
        self, platform: TikTokPlatform
    ) -> None:
        said = token_reply()
        del said["expires_in"]

        with respx.mock() as network:
            step = await sign_in(platform, network, reply=said)

        assert step.connection.token.expires_at == NOW + timedelta(seconds=86400)

    async def test_a_platform_with_no_clock_of_its_own_uses_the_real_one(
        self,
    ) -> None:
        # Nobody passes `now` outside a test, so the everyday path is the
        # one where it is left out.
        platform = TikTokPlatform(retries=ONCE)
        before = datetime.now(UTC)

        with respx.mock() as network:
            step = await sign_in(platform, network)

        expires_at = step.connection.token.expires_at
        assert expires_at is not None
        assert timedelta(hours=23) < expires_at - before < timedelta(hours=25)

    async def test_the_username_is_used_when_tiktok_gives_no_display_name(
        self, platform: TikTokPlatform
    ) -> None:
        with respx.mock() as network:
            network.post(TOKEN_ENDPOINT).mock(
                return_value=httpx.Response(200, json=token_reply())
            )
            network.get(USER_URL).mock(
                return_value=httpx.Response(
                    200, json=ok({"user": {"open_id": OPEN_ID, "username": "ada"}})
                )
            )

            step = await platform.finish_login(login(), {"code": "the-code"})

        assert isinstance(step, Finished)
        assert step.connection.account_name == "ada"

    async def test_the_open_id_is_used_when_tiktok_gives_no_name_at_all(
        self, platform: TikTokPlatform
    ) -> None:
        with respx.mock() as network:
            network.post(TOKEN_ENDPOINT).mock(
                return_value=httpx.Response(200, json=token_reply())
            )
            network.get(USER_URL).mock(
                return_value=httpx.Response(200, json=ok({"user": {}}))
            )

            step = await platform.finish_login(login(), {"code": "the-code"})

        assert isinstance(step, Finished)
        assert step.connection.account_name == OPEN_ID
        assert "username" not in step.connection.extra

    async def test_the_scopes_the_person_granted_win_over_the_ones_we_asked_for(
        self, platform: TikTokPlatform
    ) -> None:
        with respx.mock() as network:
            step = await sign_in(
                platform,
                network,
                reply=token_reply(scope="user.info.basic,video.upload"),
            )

        assert step.connection.scopes == ("user.info.basic", "video.upload")

    async def test_the_scopes_we_asked_for_are_kept_when_tiktok_says_nothing(
        self, platform: TikTokPlatform
    ) -> None:
        said = token_reply()
        del said["scope"]

        with respx.mock() as network:
            step = await sign_in(platform, network, reply=said)

        assert step.connection.scopes == (
            "user.info.basic",
            "video.upload",
            "video.publish",
        )

    async def test_a_person_who_pressed_cancel_is_reported_plainly(
        self, platform: TikTokPlatform
    ) -> None:
        with pytest.raises(AuthError, match="pressed cancel"):
            await platform.finish_login(
                login(),
                {"error": "access_denied", "error_description": "user denied"},
            )

    async def test_no_code_at_all_says_what_to_pass(
        self, platform: TikTokPlatform
    ) -> None:
        with pytest.raises(AuthError, match="no code"):
            await platform.finish_login(login(), {})

    async def test_a_state_that_does_not_match_is_refused(
        self, platform: TikTokPlatform
    ) -> None:
        with pytest.raises(AuthError, match="did not start here"):
            await platform.finish_login(
                login(state="ours"), {"code": "the-code", "state": "somebody-elses"}
            )

    async def test_a_state_that_matches_is_let_through(
        self, platform: TikTokPlatform
    ) -> None:
        with respx.mock() as network:
            network.post(TOKEN_ENDPOINT).mock(
                return_value=httpx.Response(200, json=token_reply())
            )
            network.get(USER_URL).mock(
                return_value=httpx.Response(200, json=user_reply())
            )

            step = await platform.finish_login(
                login(state="ours"), {"code": "the-code", "state": "ours"}
            )

        assert isinstance(step, Finished)

    async def test_finishing_without_credentials_is_refused(self) -> None:
        bare = TikTokPlatform()

        with pytest.raises(ConfigError, match=r"developers\.tiktok\.com"):
            await bare.finish_login(
                LoginRequest(redirect_uri=REDIRECT), {"code": "the-code"}
            )

    async def test_a_reply_with_no_access_token_says_so(
        self, platform: TikTokPlatform
    ) -> None:
        said = token_reply()
        del said["access_token"]

        with respx.mock() as network:
            network.post(TOKEN_ENDPOINT).mock(
                return_value=httpx.Response(200, json=said)
            )

            with pytest.raises(PlatformError, match="'access_token'"):
                await platform.finish_login(login(), {"code": "the-code"})

    async def test_a_reply_with_no_open_id_says_so(
        self, platform: TikTokPlatform
    ) -> None:
        # Everything socialchimp saves is named after the open id, so a
        # connection without one would be broken from the start.
        said = token_reply()
        del said["open_id"]

        with respx.mock() as network:
            network.post(TOKEN_ENDPOINT).mock(
                return_value=httpx.Response(200, json=said)
            )

            with pytest.raises(PlatformError, match="'open_id'"):
                await platform.finish_login(login(), {"code": "the-code"})

    async def test_a_refusal_dressed_up_as_a_good_reply_is_still_a_refusal(
        self, platform: TikTokPlatform
    ) -> None:
        # TikTok's token endpoint answers 200 with the trouble inside, so a
        # platform that only looks at the status never notices.
        with respx.mock() as network:
            network.post(TOKEN_ENDPOINT).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "error": "invalid_grant",
                        "error_description": "code already used",
                        "log_id": "log-1",
                    },
                )
            )

            with pytest.raises(AuthError, match="invalid_grant") as refused:
                await platform.finish_login(login(), {"code": "the-code"})

        assert "code already used" in str(refused.value)


# ---------------------------------------------------------------------------
# Keeping the token working
# ---------------------------------------------------------------------------


class TestKeepingTheTokenWorking:
    async def test_it_sends_what_tiktok_wants_for_a_renewal(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            route = network.post(TOKEN_ENDPOINT).mock(
                return_value=httpx.Response(
                    200,
                    json=token_reply(
                        access_token="access-two", refresh_token="refresh-two"
                    ),
                )
            )

            await platform.refresh(account, APP)

        sent = form_of(route.calls.last.request)
        assert sent["grant_type"] == ["refresh_token"]
        assert sent["refresh_token"] == ["refresh-one"]
        assert sent["client_key"] == [APP.client_id]
        assert sent["client_secret"] == [APP.client_secret]

    async def test_both_tokens_are_replaced_so_the_old_one_is_never_reused(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        # TikTok destroys a refresh token the moment it is used. Carrying
        # the old one across, the way Google's platform has to, would lock
        # the person out at the next renewal.
        with respx.mock() as network:
            network.post(TOKEN_ENDPOINT).mock(
                return_value=httpx.Response(
                    200,
                    json=token_reply(
                        access_token="access-two", refresh_token="refresh-two"
                    ),
                )
            )

            renewed = await platform.refresh(account, APP)

        assert renewed.access_token == "access-two"
        assert renewed.refresh_token == "refresh-two"
        assert renewed.refresh_token != account.token.refresh_token

    async def test_the_one_we_had_is_kept_when_tiktok_sends_none_back(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        said = token_reply(access_token="access-two")
        del said["refresh_token"]

        with respx.mock() as network:
            network.post(TOKEN_ENDPOINT).mock(
                return_value=httpx.Response(200, json=said)
            )

            renewed = await platform.refresh(account, APP)

        assert renewed.refresh_token == "refresh-one"

    async def test_an_account_with_no_refresh_token_has_to_sign_in_again(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        without = account.with_token(Token(access_token="access-one"))

        with pytest.raises(TokenExpiredError, match="no refresh token"):
            await platform.refresh(without, APP)

    async def test_a_refresh_token_tiktok_will_not_take_asks_for_a_new_sign_in(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.post(TOKEN_ENDPOINT).mock(
                return_value=httpx.Response(400, json=refusal("invalid_grant"))
            )

            with pytest.raises(TokenExpiredError, match="connect their account again"):
                await platform.refresh(account, APP)

    async def test_a_refusal_dressed_up_as_a_good_reply_ends_the_connection_too(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.post(TOKEN_ENDPOINT).mock(
                return_value=httpx.Response(
                    200, json={"error": "invalid_grant", "error_description": "gone"}
                )
            )

            with pytest.raises(TokenExpiredError, match="connect their account again"):
                await platform.refresh(account, APP)

    async def test_tiktok_having_trouble_is_not_a_dead_token(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        # Throwing a connection away over a bad five minutes at TikTok is
        # the sort of quiet damage this library exists to avoid.
        with respx.mock() as network:
            network.post(TOKEN_ENDPOINT).mock(
                return_value=httpx.Response(500, json=refusal("internal_error"))
            )

            with pytest.raises(PlatformError) as refused:
                await platform.refresh(account, APP)

        assert not isinstance(refused.value, TokenExpiredError)

    async def test_being_unable_to_reach_tiktok_is_not_a_dead_token_either(
        self, account: Connection
    ) -> None:
        platform = TikTokPlatform(retries=ONCE)

        with respx.mock() as network:
            network.post(TOKEN_ENDPOINT).mock(
                side_effect=httpx.ConnectError("no route")
            )

            with pytest.raises(PlatformError) as refused:
                await platform.refresh(account, APP)

        assert not isinstance(refused.value, TokenExpiredError)

    async def test_a_renewal_with_no_access_token_in_it_says_so(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.post(TOKEN_ENDPOINT).mock(
                return_value=httpx.Response(200, json={"expires_in": 86400})
            )

            with pytest.raises(PlatformError, match="'access_token'"):
                await platform.refresh(account, APP)


# ---------------------------------------------------------------------------
# What it allows
# ---------------------------------------------------------------------------


class TestWhatItAllows:
    async def test_a_caption_is_counted_the_way_tiktok_counts_it(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        limits = await platform.limits(account)

        assert limits.max_text_length == MAX_CAPTION_UNITS
        assert limits.text_counted_in is TextCount.UTF16_UNITS

    async def test_it_asks_tiktok_nothing_to_answer(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            await platform.limits(account)

        assert not network.calls

    async def test_one_video_at_a_time_and_four_gigabytes_at_most(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        limits = await platform.limits(account)

        assert limits.max_videos == 1
        assert limits.max_video_bytes == MAX_VIDEO_BYTES
        # No pictures at all, so there is no number to give.
        assert limits.max_images is None
        # The caption *is* Post.text here; there is no separate title.
        assert limits.max_title_length is None

    async def test_an_emoji_costs_two_of_the_two_thousand_two_hundred(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        # The whole point of counting in UTF-16: a caption of 1,101 thumbs
        # up is 1,101 characters to Python and 2,202 to TikTok.
        just_over = "\U0001f44d" * (MAX_CAPTION_UNITS // 2 + 1)

        with quiet_network() as network:
            upload_routes(network)

            with pytest.raises(InvalidPostError, match="2202"):
                await platform.publish(
                    account,
                    Post(
                        text=just_over,
                        media=(a_video(),),
                        options={"send_to": "profile"},
                    ),
                )

            assert not network.calls

    async def test_a_caption_of_exactly_the_limit_in_emoji_is_allowed(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        exactly = "\U0001f44d" * (MAX_CAPTION_UNITS // 2)

        with respx.mock() as network:
            started, _ = upload_routes(network)

            await platform.publish(
                account,
                Post(text=exactly, media=(a_video(),), options={"send_to": "profile"}),
            )

        assert body_of(started.calls.last.request)["post_info"]["title"] == exactly


# ---------------------------------------------------------------------------
# Working out the pieces to send
# ---------------------------------------------------------------------------


class TestWorkingOutThePieces:
    def test_a_video_under_five_megabytes_goes_whole(self) -> None:
        # TikTok's floor for a piece is 5 MB, so anything smaller has to be
        # one piece the size of the file.
        assert _chunk_plan(24, 10 * A_MEGABYTE) == (24, 1)
        assert _chunk_plan(MIN_CHUNK_BYTES - 1, 10 * A_MEGABYTE) == (
            MIN_CHUNK_BYTES - 1,
            1,
        )

    def test_a_video_smaller_than_one_piece_is_still_one_piece(self) -> None:
        assert _chunk_plan(6 * A_MEGABYTE, 10 * A_MEGABYTE) == (6 * A_MEGABYTE, 1)

    def test_the_count_is_rounded_down_not_up(self) -> None:
        # This is the arithmetic that catches people out. A 12 MB video sent
        # in 10 MB pieces is *one* piece, not two: the leftover 2 MB rides
        # along on the last one rather than becoming a piece of its own,
        # which would be under TikTok's 5 MB floor and be refused.
        assert _chunk_plan(12 * A_MEGABYTE, 10 * A_MEGABYTE) == (10 * A_MEGABYTE, 1)
        assert _chunk_plan(25 * A_MEGABYTE, 10 * A_MEGABYTE) == (10 * A_MEGABYTE, 2)
        assert _chunk_plan(30 * A_MEGABYTE, 10 * A_MEGABYTE) == (10 * A_MEGABYTE, 3)

    def test_the_last_piece_can_be_nearly_twice_the_size_of_the_others(self) -> None:
        size, count = _chunk_plan(19 * A_MEGABYTE, 10 * A_MEGABYTE)
        last = 19 * A_MEGABYTE - (count - 1) * size

        assert (size, count) == (10 * A_MEGABYTE, 1)
        # TikTok allows the last piece up to 128 MB, and this arithmetic
        # cannot produce one bigger than twice the piece size.
        assert last == 19 * A_MEGABYTE
        assert last < 2 * size

    def test_the_thousand_piece_cap_cannot_be_reached(self) -> None:
        # The largest file TikTok takes, cut into the smallest pieces it
        # takes, is well under a thousand pieces - so there is no case here
        # that needs handling, only arithmetic that needs checking.
        assert MAX_VIDEO_BYTES // MIN_CHUNK_BYTES <= MAX_CHUNKS

    def test_a_piece_size_tiktok_will_not_take_is_refused_at_once(self) -> None:
        with pytest.raises(ConfigError, match="5 MB"):
            TikTokPlatform(chunk_bytes=A_MEGABYTE)

        with pytest.raises(ConfigError, match="64 MB"):
            TikTokPlatform(chunk_bytes=100 * A_MEGABYTE)


# ---------------------------------------------------------------------------
# Sending a video
# ---------------------------------------------------------------------------


class TestSendingItStraightToTheProfile:
    async def test_it_uses_the_direct_post_address(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            started, _ = upload_routes(network)

            await platform.publish(account, a_post())

        assert str(started.calls.last.request.url) == INIT_DIRECT

    async def test_it_sends_the_caption_and_the_size_of_the_file(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            started, _ = upload_routes(network)

            await platform.publish(account, a_post())

        sent = body_of(started.calls.last.request)
        assert sent["post_info"]["title"] == "A caption"
        assert sent["source_info"] == {
            "source": "FILE_UPLOAD",
            "video_size": 24,
            "chunk_size": 24,
            "total_chunk_count": 1,
        }

    async def test_a_video_goes_up_private_unless_you_say_otherwise(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        # Putting somebody's video in front of the world by accident cannot
        # be undone, so the quiet answer is the careful one.
        with respx.mock() as network:
            started, _ = upload_routes(network)

            await platform.publish(account, a_post())

        assert body_of(started.calls.last.request)["post_info"]["privacy_level"] == (
            "SELF_ONLY"
        )

    async def test_it_sends_the_privacy_level_you_asked_for(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            started, _ = upload_routes(network)

            await platform.publish(account, a_post(privacy_level="PUBLIC_TO_EVERYONE"))

        assert body_of(started.calls.last.request)["post_info"]["privacy_level"] == (
            "PUBLIC_TO_EVERYONE"
        )

    async def test_it_passes_on_the_switches_tiktok_offers(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            started, _ = upload_routes(network)

            await platform.publish(
                account,
                a_post(
                    disable_comment=True,
                    disable_duet=True,
                    disable_stitch=False,
                    brand_content_toggle=True,
                    brand_organic_toggle=False,
                    video_cover_timestamp_ms=1500,
                ),
            )

        sent = body_of(started.calls.last.request)["post_info"]
        assert sent["disable_comment"] is True
        assert sent["disable_duet"] is True
        assert sent["disable_stitch"] is False
        assert sent["brand_content_toggle"] is True
        assert sent["brand_organic_toggle"] is False
        assert sent["video_cover_timestamp_ms"] == 1500

    async def test_switches_left_out_are_left_out_of_the_request(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            started, _ = upload_routes(network)

            await platform.publish(account, a_post())

        sent = body_of(started.calls.last.request)["post_info"]
        assert set(sent) == {"title", "privacy_level"}

    async def test_tiktok_is_still_working_when_publish_comes_back(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            upload_routes(network)

            result = await platform.publish(account, a_post())

        assert result.id == PUBLISH_ID
        # Taking the bytes is not publishing. Ask check_state later.
        assert result.state is PostState.PROCESSING
        assert result.is_done is False
        # TikTok gives no address for a post until it is live.
        assert result.url is None


class TestSendingItToTheDrafts:
    async def test_the_drafts_are_where_a_post_goes_when_you_say_nothing(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        # The safer of the two: it needs only video.upload, and nothing
        # reaches anybody's profile without the person finishing it.
        with respx.mock() as network:
            started, _ = upload_routes(network, init=INIT_INBOX)

            await platform.publish(
                account, Post(text="", media=(a_video(),), options={})
            )

        assert str(started.calls.last.request.url) == INIT_INBOX

    async def test_it_sends_no_caption_and_no_privacy_level(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            started, _ = upload_routes(network, init=INIT_INBOX)

            await platform.publish(
                account, Post(text="", media=(a_video(),), options={})
            )

        sent = body_of(started.calls.last.request)
        # TikTok's inbox takes nothing but the file. The person types the
        # rest in the app.
        assert set(sent) == {"source_info"}

    async def test_a_caption_is_refused_rather_than_quietly_dropped(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with quiet_network() as network:
            upload_routes(network, init=INIT_INBOX)

            with pytest.raises(InvalidPostError) as refused:
                await platform.publish(account, Post(text="hello", media=(a_video(),)))

            assert not network.calls

        said = str(refused.value)
        assert "send_to" in said
        assert "profile" in said

    async def test_a_drafts_post_comes_back_waiting_for_a_person(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        # Not PROCESSING. Nothing more happens on its own, so an app that
        # reads PROCESSING here would sit checking forever for a change
        # that only a person can make.
        with respx.mock() as network:
            upload_routes(network, init=INIT_INBOX)

            result = await platform.publish(
                account, Post(text="", media=(a_video(),), options={})
            )

        assert result.state is PostState.WAITING_FOR_PERSON
        assert result.is_done is False

    async def test_a_profile_post_is_still_only_processing(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            upload_routes(network)

            result = await platform.publish(account, a_post())

        assert result.state is PostState.PROCESSING

    async def test_asking_for_the_drafts_by_name_works_too(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            started, _ = upload_routes(network, init=INIT_INBOX)

            await platform.publish(
                account,
                Post(text="", media=(a_video(),), options={"send_to": "drafts"}),
            )

        assert str(started.calls.last.request.url) == INIT_INBOX

    async def test_a_privacy_level_in_drafts_mode_is_refused(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError, match="drafts"):
            await platform.publish(
                account,
                Post(
                    text="",
                    media=(a_video(),),
                    options={"send_to": "drafts", "privacy_level": "SELF_ONLY"},
                ),
            )

    async def test_a_send_to_tiktok_does_not_know_is_refused(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError, match="send_to"):
            await platform.publish(account, a_post(send_to="everywhere"))


class TestSendingTheBytes:
    async def test_a_small_video_goes_in_one_piece(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            _, sending = upload_routes(network)

            await platform.publish(account, a_post())

        assert len(sending.calls) == 1
        sent = sending.calls.last.request
        assert sent.headers["content-range"] == "bytes 0-23/24"
        assert sent.headers["content-type"] == "video/mp4"
        assert sent.content == b"v" * 24

    async def test_a_big_video_goes_in_several_pieces(
        self, account: Connection, tmp_path: Path
    ) -> None:
        platform = TikTokPlatform(retries=ONCE, chunk_bytes=MIN_CHUNK_BYTES)
        total = MIN_CHUNK_BYTES * 3
        post = Post(
            text="",
            media=(a_file_video(tmp_path, total),),
            options={"send_to": "profile"},
        )

        with respx.mock() as network:
            _, sending = upload_routes(network)

            await platform.publish(account, post)

        ranges = [call.request.headers["content-range"] for call in sending.calls]
        assert ranges == [
            f"bytes 0-{MIN_CHUNK_BYTES - 1}/{total}",
            f"bytes {MIN_CHUNK_BYTES}-{MIN_CHUNK_BYTES * 2 - 1}/{total}",
            f"bytes {MIN_CHUNK_BYTES * 2}-{total - 1}/{total}",
        ]

    async def test_the_leftover_rides_on_the_last_piece(
        self, account: Connection, tmp_path: Path
    ) -> None:
        # The rule that catches people out, seen from the wire: 12 MB in
        # 5 MB pieces is two pieces, and the second one is 7 MB.
        platform = TikTokPlatform(retries=ONCE, chunk_bytes=MIN_CHUNK_BYTES)
        total = MIN_CHUNK_BYTES * 2 + 2 * A_MEGABYTE
        post = Post(
            text="",
            media=(a_file_video(tmp_path, total),),
            options={"send_to": "profile"},
        )

        with respx.mock() as network:
            started, sending = upload_routes(network)

            await platform.publish(account, post)

        assert body_of(started.calls.last.request)["source_info"] == {
            "source": "FILE_UPLOAD",
            "video_size": total,
            "chunk_size": MIN_CHUNK_BYTES,
            "total_chunk_count": 2,
        }
        ranges = [call.request.headers["content-range"] for call in sending.calls]
        assert ranges == [
            f"bytes 0-{MIN_CHUNK_BYTES - 1}/{total}",
            f"bytes {MIN_CHUNK_BYTES}-{total - 1}/{total}",
        ]
        assert [len(call.request.content) for call in sending.calls] == [
            MIN_CHUNK_BYTES,
            MIN_CHUNK_BYTES + 2 * A_MEGABYTE,
        ]

    async def test_it_asks_the_media_for_one_piece_at_a_time(
        self,
        account: Connection,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # A four gigabyte video must not become four gigabytes of memory,
        # so the upload goes through Media.piece and never Media.read.
        platform = TikTokPlatform(retries=ONCE, chunk_bytes=MIN_CHUNK_BYTES)
        total = MIN_CHUNK_BYTES * 2 + 10
        asked: list[tuple[int, int]] = []
        reading_a_piece = Media.piece

        def watched(media: Media, start: int, length: int) -> bytes:
            asked.append((start, length))
            return reading_a_piece(media, start, length)

        def never(media: Media) -> bytes:
            raise AssertionError("the whole video was read into memory")

        monkeypatch.setattr(Media, "piece", watched)
        monkeypatch.setattr(Media, "read", never)

        post = Post(
            text="",
            media=(a_file_video(tmp_path, total),),
            options={"send_to": "profile"},
        )

        with respx.mock() as network:
            upload_routes(network)

            await platform.publish(account, post)

        assert asked == [
            (0, MIN_CHUNK_BYTES),
            (MIN_CHUNK_BYTES, MIN_CHUNK_BYTES + 10),
        ]

    async def test_our_token_is_never_sent_to_the_upload_address(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        # The upload address is signed by TikTok and lives on a different
        # host. Sending the account's token there would hand it to a machine
        # that has no business seeing it.
        with respx.mock() as network:
            started, sending = upload_routes(network)

            await platform.publish(account, a_post())

        assert "authorization" in started.calls.last.request.headers
        assert "authorization" not in sending.calls.last.request.headers

    async def test_a_reply_with_no_upload_address_says_so(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with quiet_network() as network:
            upload_routes(network, reply=ok({"publish_id": PUBLISH_ID}))

            with pytest.raises(PlatformError, match="'upload_url'"):
                await platform.publish(account, a_post())

    async def test_a_reply_with_no_publish_id_says_so(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with quiet_network() as network:
            upload_routes(network, reply=ok({"upload_url": UPLOAD_TO}))

            with pytest.raises(PlatformError, match="'publish_id'"):
                await platform.publish(account, a_post())

    async def test_a_reply_with_nothing_in_it_says_so(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with quiet_network() as network:
            upload_routes(network, reply={"error": {"code": "ok"}})

            with pytest.raises(PlatformError, match="publish_id"):
                await platform.publish(account, a_post())

    async def test_a_refusal_dressed_up_as_a_good_reply_stops_the_upload(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with quiet_network() as network:
            _, sending = upload_routes(
                network, reply=refusal("spam_risk_too_many_posts")
            )

            with pytest.raises(RateLimitError):
                await platform.publish(account, a_post())

        assert not sending.calls


class TestWhatAPostMustCarry:
    async def test_a_post_with_no_video_says_what_to_attach(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with pytest.raises(NotSupportedError) as refused:
            await platform.publish(account, Post(text="just some words"))

        assert "Media.from_file" in str(refused.value)

    async def test_pictures_are_refused_with_somewhere_to_go(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        picture = Media.from_bytes(b"not really a picture", filename="a.png")

        with pytest.raises(NotSupportedError) as refused:
            await platform.publish(account, Post(text="look", media=(picture,)))

        said = str(refused.value)
        # A person with 12 photos deserves to be told where they would go.
        assert "carousel" in said
        assert "/v2/post/publish/content/init/" in said

    async def test_two_videos_on_one_post_are_refused(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError, match="at most 1"):
            await platform.publish(
                account,
                Post(text="", media=(a_video(), a_video(name="other.mp4"))),
            )

    async def test_a_setting_tiktok_has_never_heard_of_is_refused(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError, match="hashtags") as refused:
            await platform.publish(account, a_post(hashtags=["python"]))

        assert "send_to" in str(refused.value)

    async def test_a_privacy_level_tiktok_does_not_know_is_refused(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError, match="PUBLIC_TO_EVERYONE"):
            await platform.publish(account, a_post(privacy_level="public"))

    async def test_a_switch_that_is_not_true_or_false_is_refused(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError, match="disable_comment"):
            await platform.publish(account, a_post(disable_comment="yes"))

    async def test_a_cover_time_that_is_not_a_number_is_refused(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError, match="video_cover_timestamp_ms"):
            await platform.publish(account, a_post(video_cover_timestamp_ms="1.5s"))

    async def test_a_cover_time_that_is_true_is_refused(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        # True is an int in Python, and would sail through a plain check.
        with pytest.raises(InvalidPostError, match="video_cover_timestamp_ms"):
            await platform.publish(account, a_post(video_cover_timestamp_ms=True))

    async def test_scheduling_says_tiktok_cannot_do_it(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        later = datetime.now(UTC) + timedelta(hours=1)

        with pytest.raises(NotSupportedError, match="scheduling"):
            await platform.publish(
                account, Post(text="", media=(a_video(),), publish_at=later)
            )

    async def test_a_video_only_we_have_a_link_to_is_refused(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        linked = Media.from_url("https://example.com/clip.mp4")

        with pytest.raises(InvalidPostError) as refused:
            await platform.publish(
                account, Post(text="", media=(linked,), options={"send_to": "profile"})
            )

        assert "Media.from_file" in str(refused.value)

    async def test_a_video_bigger_than_tiktok_takes_is_refused_before_sending(
        self, platform: TikTokPlatform, account: Connection, tmp_path: Path
    ) -> None:
        too_big = a_file_video(tmp_path, MAX_VIDEO_BYTES + 1)

        with quiet_network() as network:
            upload_routes(network)

            with pytest.raises(InvalidPostError, match="4"):
                await platform.publish(
                    account,
                    Post(text="", media=(too_big,), options={"send_to": "profile"}),
                )

            assert not network.calls

    async def test_a_file_tiktok_will_not_recognise_is_refused(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        odd = Media.from_bytes(b"v" * 24, filename="clip.mkv", kind=MediaKind.VIDEO)

        with pytest.raises(InvalidPostError) as refused:
            await platform.publish(
                account, Post(text="", media=(odd,), options={"send_to": "profile"})
            )

        assert "video/mp4" in str(refused.value)

    async def test_a_quicktime_file_is_fine(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        movie = Media.from_bytes(b"v" * 24, filename="clip.mov")

        with respx.mock() as network:
            _, sending = upload_routes(network)

            await platform.publish(
                account, Post(text="", media=(movie,), options={"send_to": "profile"})
            )

        assert sending.calls.last.request.headers["content-type"] == "video/quicktime"


# ---------------------------------------------------------------------------
# Asking what happened next
# ---------------------------------------------------------------------------


class TestCheckingWhatTikTokDidNext:
    async def test_it_asks_with_the_publish_id_it_was_given(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            route = network.post(STATUS_URL).mock(
                return_value=httpx.Response(
                    200, json=ok({"status": "PROCESSING_UPLOAD"})
                )
            )

            await platform.check_state(account, PUBLISH_ID)

        assert body_of(route.calls.last.request) == {"publish_id": PUBLISH_ID}

    @pytest.mark.parametrize(
        ("said", "expected"),
        [
            ("PROCESSING_UPLOAD", PostState.PROCESSING),
            ("PROCESSING_DOWNLOAD", PostState.PROCESSING),
            ("SEND_TO_USER_INBOX", PostState.WAITING_FOR_PERSON),
            ("PUBLISH_COMPLETE", PostState.DONE),
            ("FAILED", PostState.FAILED),
            ("SOMETHING_NEW", PostState.PROCESSING),
        ],
    )
    async def test_it_translates_every_status_tiktok_reports(
        self,
        platform: TikTokPlatform,
        account: Connection,
        said: str,
        expected: PostState,
    ) -> None:
        with respx.mock() as network:
            network.post(STATUS_URL).mock(
                return_value=httpx.Response(200, json=ok({"status": said}))
            )

            result = await platform.check_state(account, PUBLISH_ID)

        assert result.state is expected

    async def test_a_video_sitting_in_the_drafts_is_not_still_processing(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        # TikTok has done everything it is going to do. What happens next
        # is somebody opening the app, and no amount of checking hurries it.
        with respx.mock() as network:
            network.post(STATUS_URL).mock(
                return_value=httpx.Response(
                    200, json=ok({"status": "SEND_TO_USER_INBOX"})
                )
            )

            result = await platform.check_state(account, PUBLISH_ID)

        assert result.state is PostState.WAITING_FOR_PERSON

    async def test_a_finished_post_comes_back_with_somewhere_to_watch_it(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.post(STATUS_URL).mock(
                return_value=httpx.Response(
                    200,
                    json=ok(
                        {
                            "status": "PUBLISH_COMPLETE",
                            # TikTok's own spelling, typo and all.
                            "publicaly_available_post_id": [7248000000000000000],
                        }
                    ),
                )
            )

            result = await platform.check_state(account, PUBLISH_ID)

        assert result.url == "https://www.tiktok.com/@ada/video/7248000000000000000"

    async def test_there_is_no_address_when_we_do_not_know_the_username(
        self, platform: TikTokPlatform
    ) -> None:
        nameless = Connection(
            id="tiktok:x",
            platform="tiktok",
            host=None,
            account_id=OPEN_ID,
            account_name="Ada",
            token=Token(access_token="access-one"),
        )

        with respx.mock() as network:
            network.post(STATUS_URL).mock(
                return_value=httpx.Response(
                    200,
                    json=ok(
                        {
                            "status": "PUBLISH_COMPLETE",
                            "publicaly_available_post_id": [7248000000000000000],
                        }
                    ),
                )
            )

            result = await platform.check_state(nameless, PUBLISH_ID)

        assert result.url is None

    async def test_there_is_no_address_before_tiktok_gives_one(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.post(STATUS_URL).mock(
                return_value=httpx.Response(
                    200, json=ok({"status": "PUBLISH_COMPLETE"})
                )
            )

            result = await platform.check_state(account, PUBLISH_ID)

        assert result.url is None

    async def test_a_failure_keeps_tiktoks_own_reason(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.post(STATUS_URL).mock(
                return_value=httpx.Response(
                    200,
                    json=ok(
                        {
                            "status": "FAILED",
                            "fail_reason": "video_pull_failed",
                        }
                    ),
                )
            )

            result = await platform.check_state(account, PUBLISH_ID)

        assert result.state is PostState.FAILED
        assert result.raw["fail_reason"] == "video_pull_failed"

    async def test_a_publish_id_tiktok_has_never_heard_of(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.post(STATUS_URL).mock(
                return_value=httpx.Response(
                    404, json=refusal("publish_id_not_found", "no such publish_id")
                )
            )

            with pytest.raises(NotFoundError):
                await platform.check_state(account, PUBLISH_ID)

    async def test_a_reply_with_no_status_in_it_says_so(
        self, platform: TikTokPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.post(STATUS_URL).mock(return_value=httpx.Response(200, json=ok({})))

            with pytest.raises(PlatformError, match="'status'"):
                await platform.check_state(account, PUBLISH_ID)


# ---------------------------------------------------------------------------
# The requests TikTok pushes to us
# ---------------------------------------------------------------------------


class TestCheckingARequestCameFromTikTok:
    def test_a_properly_signed_request_is_let_through(
        self, platform: TikTokPlatform
    ) -> None:
        body = an_event("post.publish.complete")

        platform.check_signature(body, signature_for(body), secret=APP.client_secret)

    def test_a_body_changed_on_the_way_here_is_refused(
        self, platform: TikTokPlatform
    ) -> None:
        headers = signature_for(an_event("post.publish.complete"))
        tampered = an_event("post.publish.failed")

        with pytest.raises(SignatureError, match="does not match"):
            platform.check_signature(tampered, headers, secret=APP.client_secret)

    def test_the_wrong_secret_is_refused(self, platform: TikTokPlatform) -> None:
        body = an_event("post.publish.complete")

        with pytest.raises(SignatureError):
            platform.check_signature(
                body, signature_for(body), secret="somebody-elses-secret"
            )

    def test_a_request_with_no_signature_at_all_is_refused(
        self, platform: TikTokPlatform
    ) -> None:
        with pytest.raises(SignatureError, match=SIGNATURE_HEADER):
            platform.check_signature(
                an_event("post.publish.complete"),
                {"Content-Type": "application/json"},
                secret=APP.client_secret,
            )

    def test_a_signature_we_cannot_read_is_refused(
        self, platform: TikTokPlatform
    ) -> None:
        with pytest.raises(SignatureError):
            platform.check_signature(
                an_event("post.publish.complete"),
                {SIGNATURE_HEADER: "nonsense"},
                secret=APP.client_secret,
            )

    def test_a_signature_with_a_time_we_cannot_read_is_refused(
        self, platform: TikTokPlatform
    ) -> None:
        with pytest.raises(SignatureError):
            platform.check_signature(
                an_event("post.publish.complete"),
                {SIGNATURE_HEADER: "t=whenever,s=abc"},
                secret=APP.client_secret,
            )

    def test_a_time_we_cannot_read_is_refused_even_when_it_was_signed(
        self, platform: TikTokPlatform
    ) -> None:
        # The time is part of what TikTok signs, so a signature can be
        # perfectly correct over a time that is not a time at all.
        body = an_event("post.publish.complete")
        digest = hmac.new(
            APP.client_secret.encode(), b"whenever." + body, hashlib.sha256
        ).hexdigest()

        with pytest.raises(SignatureError, match="not a moment"):
            platform.check_signature(
                body,
                {SIGNATURE_HEADER: f"t=whenever,s={digest}"},
                secret=APP.client_secret,
            )

    def test_the_header_is_found_whatever_case_it_arrives_in(
        self, platform: TikTokPlatform
    ) -> None:
        body = an_event("post.publish.complete")
        headers = signature_for(body)
        lowered = {name.lower(): value for name, value in headers.items()}

        platform.check_signature(body, lowered, secret=APP.client_secret)

    def test_an_old_request_is_refused_even_with_a_good_signature(
        self, platform: TikTokPlatform
    ) -> None:
        # A signature stays correct forever, so anyone who copies one
        # request can send it again next year unless the age is checked.
        body = an_event("post.publish.complete")

        with pytest.raises(SignatureError, match="signed"):
            platform.check_signature(
                body, signature_for(body, at=WHEN - 3600), secret=APP.client_secret
            )

    def test_you_can_say_how_old_is_too_old(self) -> None:
        platform = TikTokPlatform(allowed_age_seconds=7200, now=at())
        body = an_event("post.publish.complete")

        platform.check_signature(
            body, signature_for(body, at=WHEN - 3600), secret=APP.client_secret
        )

    def test_the_clock_it_checks_against_is_yours_to_set(self) -> None:
        # Without this the age check can only be tested against whatever the
        # real clock says while the test runs, which is the sort of test
        # that passes for a year and then fails on a slow machine.
        body = an_event("post.publish.complete")
        headers = signature_for(body, at=WHEN)
        a_minute_later = TikTokPlatform(now=at(NOW + timedelta(minutes=1)))
        an_hour_later = TikTokPlatform(now=at(NOW + timedelta(hours=1)))

        a_minute_later.check_signature(body, headers, secret=APP.client_secret)

        with pytest.raises(SignatureError, match="signed"):
            an_hour_later.check_signature(body, headers, secret=APP.client_secret)


class TestWhatATypedCallerCanReach:
    def test_it_offers_asking_how_a_post_is_getting_on(
        self, platform: TikTokPlatform
    ) -> None:
        # TikTok answers publish while it is still working, so Account
        # .check_state has to be able to find this.
        assert isinstance(platform, CanCheckState)

    def test_it_offers_reading_a_whole_pushed_message(
        self, platform: TikTokPlatform
    ) -> None:
        assert isinstance(platform, CanReadPushedUpdates)


class TestReadingWhatTikTokPushed:
    @pytest.mark.parametrize(
        ("event", "kind"),
        [
            ("post.publish.complete", UpdateKind.POST_PUBLISHED),
            ("post.publish.publicly_available", UpdateKind.POST_PUBLISHED),
            ("post.publish.failed", UpdateKind.POST_FAILED),
            ("post.publish.inbox_delivered", UpdateKind.POST_DRAFTED),
            ("authorization.removed", UpdateKind.CONNECTION_REVOKED),
            ("something.we.have.never.seen", UpdateKind.UNKNOWN),
        ],
    )
    def test_it_translates_every_event_tiktok_sends(
        self, platform: TikTokPlatform, event: str, kind: UpdateKind
    ) -> None:
        update = platform.read_update(an_event(event), {})

        assert update.kind is kind
        assert update.platform == "tiktok"
        assert update.connection_id == f"tiktok:{OPEN_ID}"

    def test_a_video_reaching_the_drafts_has_a_word_of_ours(
        self, platform: TikTokPlatform
    ) -> None:
        update = platform.read_update(an_event("post.publish.inbox_delivered"), {})

        assert update.kind is UpdateKind.POST_DRAFTED
        assert update.kind_name == "post_drafted"

    def test_an_event_we_have_no_word_for_keeps_tiktoks_own(
        self, platform: TikTokPlatform
    ) -> None:
        update = platform.read_update(an_event("something.we.have.never.seen"), {})

        assert update.kind is UpdateKind.UNKNOWN
        assert update.kind_name == "something.we.have.never.seen"

    def test_it_unpacks_the_json_string_tiktok_hides_the_details_in(
        self, platform: TikTokPlatform
    ) -> None:
        # `content` arrives as a string of JSON rather than an object, so a
        # reader that does not unpack it finds nothing useful in there.
        update = platform.read_update(an_event("post.publish.complete"), {})

        assert update.raw["content"]["publish_id"] == PUBLISH_ID

    def test_the_time_comes_from_tiktok_and_has_a_timezone(
        self, platform: TikTokPlatform
    ) -> None:
        update = platform.read_update(
            an_event("post.publish.complete", at=1790000000), {}
        )

        assert update.created_at == datetime.fromtimestamp(1790000000, UTC)

    def test_a_message_with_no_time_is_stamped_as_it_arrives(
        self, platform: TikTokPlatform
    ) -> None:
        body = json.dumps(
            {"event": "post.publish.complete", "user_openid": OPEN_ID}
        ).encode()
        update = platform.read_update(body, {})

        assert update.created_at == NOW

    def test_the_same_message_twice_gets_the_same_id(
        self, platform: TikTokPlatform
    ) -> None:
        # TikTok keeps retrying for three days and delivers at least once,
        # so a duplicate is normal rather than a surprise. Matching ids are
        # what let SeenUpdates throw the second one away.
        body = an_event("post.publish.complete")

        first = platform.read_update(body, {})
        again = platform.read_update(body, {})

        assert first.id == again.id

    def test_one_message_carries_one_event_and_still_comes_back_as_a_list(
        self, platform: TikTokPlatform
    ) -> None:
        # TikTok never batches, unlike Facebook and Instagram. The list is
        # so that an app written against one network works against all of
        # them.
        found = platform.read_updates(an_event("post.publish.complete"))

        assert [update.kind for update in found] == [UpdateKind.POST_PUBLISHED]

    def test_two_different_messages_get_different_ids(
        self, platform: TikTokPlatform
    ) -> None:
        complete = platform.read_update(an_event("post.publish.complete"), {})
        failed = platform.read_update(an_event("post.publish.failed"), {})
        other = platform.read_update(
            an_event("post.publish.complete", publish_id="v_pub_file~other"), {}
        )

        assert len({complete.id, failed.id, other.id}) == 3

    async def test_a_duplicate_is_only_handled_once(
        self, platform: TikTokPlatform
    ) -> None:
        from socialchimp import Dispatcher, InMemorySeenUpdates, Update

        handled: list[Update] = []

        async def remember(update: Update) -> None:
            handled.append(update)

        dispatcher = Dispatcher(seen=InMemorySeenUpdates())
        dispatcher.on(UpdateKind.POST_PUBLISHED, remember)
        body = an_event("post.publish.complete")

        await dispatcher.deliver(platform.read_update(body, {}))
        await dispatcher.deliver(platform.read_update(body, {}))

        assert len(handled) == 1

    def test_content_that_is_not_json_is_kept_as_it_arrived(
        self, platform: TikTokPlatform
    ) -> None:
        body = json.dumps(
            {
                "event": "post.publish.complete",
                "user_openid": OPEN_ID,
                "create_time": 1790000000,
                "content": "not json at all",
            }
        ).encode()

        update = platform.read_update(body, {})

        assert update.raw["content"] == "not json at all"

    def test_content_that_is_already_an_object_is_left_alone(
        self, platform: TikTokPlatform
    ) -> None:
        body = json.dumps(
            {
                "event": "post.publish.complete",
                "user_openid": OPEN_ID,
                "create_time": 1790000000,
                "content": {"publish_id": PUBLISH_ID},
            }
        ).encode()

        update = platform.read_update(body, {})

        assert update.raw["content"]["publish_id"] == PUBLISH_ID

    def test_a_body_that_is_not_json_says_so(self, platform: TikTokPlatform) -> None:
        with pytest.raises(PlatformError, match="raw body"):
            platform.read_update(b"not json", {})

    def test_a_body_that_is_not_one_of_tiktoks_messages_says_so(
        self, platform: TikTokPlatform
    ) -> None:
        with pytest.raises(PlatformError, match="object was expected"):
            platform.read_update(b"[1, 2, 3]", {})


# ---------------------------------------------------------------------------
# Turning TikTok's errors into ours
# ---------------------------------------------------------------------------


def response_for(code: str, *, status: int = 403, **headers: str) -> httpx.Response:
    """One of TikTok's refusals, ready to be turned into an error."""
    return httpx.Response(status, json=refusal(code, "TikTok said no"), headers=headers)


class TestTurningTikToksErrorsIntoOurs:
    def test_an_unaudited_app_is_told_exactly_what_is_happening(self) -> None:
        problem = tiktok_errors(
            response_for("unaudited_client_can_only_post_to_private_accounts")
        )

        assert isinstance(problem, NotAllowedError)
        said = str(problem)
        assert "audit" in said
        assert "SELF_ONLY" in said

    def test_a_creator_who_has_posted_too_much_today(self) -> None:
        problem = tiktok_errors(response_for("spam_risk_too_many_posts"))

        assert isinstance(problem, RateLimitError)
        assert "15" in str(problem)
        # Not a "slow down for a moment": waiting seconds changes nothing.
        assert problem.retry_after is None

    def test_too_many_uploads_waiting_in_the_drafts(self) -> None:
        problem = tiktok_errors(response_for("spam_risk_too_many_pending_share"))

        assert isinstance(problem, RateLimitError)
        assert "drafts" in str(problem)

    def test_a_creator_tiktok_has_banned_from_posting(self) -> None:
        problem = tiktok_errors(response_for("spam_risk_user_banned_from_posting"))

        assert isinstance(problem, NotAllowedError)

    def test_a_token_tiktok_will_not_take(self) -> None:
        problem = tiktok_errors(response_for("access_token_invalid", status=401))

        assert isinstance(problem, AuthError)

    def test_a_permission_that_was_never_asked_for(self) -> None:
        problem = tiktok_errors(response_for("scope_not_authorized", status=401))

        assert isinstance(problem, NotAllowedError)
        assert "video.publish" in str(problem)

    def test_going_too_fast(self) -> None:
        problem = tiktok_errors(
            response_for("rate_limit_exceeded", status=429, **{"retry-after": "30"})
        )

        assert isinstance(problem, RateLimitError)
        assert problem.retry_after == 30.0
        # The two numbers people actually run into.
        assert "6" in str(problem)
        assert "30" in str(problem)

    def test_going_too_fast_without_being_told_how_long_to_wait(self) -> None:
        problem = tiktok_errors(response_for("rate_limit_exceeded", status=429))

        assert isinstance(problem, RateLimitError)
        assert problem.retry_after is None

    def test_a_web_address_tiktok_will_not_fetch_from(self) -> None:
        problem = tiktok_errors(response_for("url_ownership_unverified"))

        assert isinstance(problem, NotAllowedError)

    def test_something_wrong_with_the_post_itself(self) -> None:
        problem = tiktok_errors(response_for("invalid_param", status=400))

        assert isinstance(problem, InvalidPostError)
        assert "TikTok said no" in str(problem)

    def test_a_privacy_level_this_creator_cannot_use(self) -> None:
        problem = tiktok_errors(response_for("privacy_level_option_mismatch"))

        assert isinstance(problem, InvalidPostError)

    def test_a_publish_id_that_is_not_there(self) -> None:
        problem = tiktok_errors(response_for("publish_id_not_found", status=404))

        assert isinstance(problem, NotFoundError)

    def test_anything_else_falls_through_to_the_shared_mapping(self) -> None:
        problem = tiktok_errors(response_for("internal_error", status=500))

        assert isinstance(problem, PlatformError)
        assert problem.status_code == 500

    def test_a_refusal_with_no_code_in_it_still_becomes_an_error(self) -> None:
        problem = tiktok_errors(httpx.Response(400, text="a wall of html"))

        assert isinstance(problem, PlatformError)

    def test_a_refusal_written_the_oauth_way_is_read_too(self) -> None:
        # TikTok's token endpoint writes `error` as a word rather than as an
        # object, so the same reader has to cope with both shapes.
        problem = tiktok_errors(
            httpx.Response(400, json={"error": "invalid_client", "log_id": "log-1"})
        )

        assert isinstance(problem, AuthError)


# ---------------------------------------------------------------------------
# The shared checks every platform has to pass
# ---------------------------------------------------------------------------


class TestTikTokBehavesLikeTheOthers(PlatformChecks):
    def make_platform(self) -> Platform:
        return TikTokPlatform(transport=self.transport, retries=ONCE)

    def make_post(self, text: str) -> Post:
        # The checks that measure length need a post TikTok would look at.
        # Everything here is a video, and a caption only has somewhere to go
        # on a post headed for the profile rather than the drafts.
        return Post(
            text=text,
            media=(Media.from_bytes(b"a tiny video", filename="clip.mp4"),),
            options={"send_to": "profile"},
        )

    def make_connection(self) -> Connection | None:
        return Connection(
            id=f"tiktok:{OPEN_ID}",
            platform="tiktok",
            host=None,
            account_id=OPEN_ID,
            account_name="Ada",
            token=Token(access_token="access-one", refresh_token="refresh-one"),
            extra={"open_id": OPEN_ID, "username": "ada"},
        )

    def make_transport(self) -> httpx.AsyncBaseTransport | None:
        return RecordingTransport(
            {
                "POST /v2/post/publish/video/init/": an_init_reply(),
                "POST /v2/post/publish/inbox/video/init/": an_init_reply(),
                "PUT /upload": {},
            }
        )
