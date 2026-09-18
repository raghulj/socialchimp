"""Tests for the Google Business Profile platform."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from socialchimp import (
    AppCredentials,
    AuthError,
    BusinessLocation,
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
    SignatureError,
    Token,
    TokenExpiredError,
    Update,
    UpdateKind,
    Verification,
    VerificationOption,
)
from socialchimp.http import Retries
from socialchimp.platform import (
    CanCheckSignature,
    CanEditBusinessInfo,
    CanManageVerification,
    CanReadPushedUpdates,
    CanReadUpdates,
    CanReplyToUpdates,
    CanResumeLogin,
    ChooseAccount,
    Finished,
    LoginRequest,
    Platform,
    SendToNetwork,
)
from socialchimp.platforms.google_business import (
    ACCOUNT_MANAGEMENT_API,
    BUSINESS_INFORMATION_API,
    LOCAL_API,
    QANDA_API,
    VERIFICATIONS_API,
    GoogleBusinessPlatform,
    google_business_errors,
)
from socialchimp.testing import PlatformChecks, RecordingTransport

REDIRECT = "https://app.example/callback"
LOCATION = "accounts/123/locations/456"
OTHER_LOCATION = "accounts/123/locations/789"
AUDIENCE = "https://app.example/webhooks/google_business"
SERVICE_ACCOUNT = "service-1@gcp-sa-pubsub.iam.gserviceaccount.com"

ONE_TRY = Retries(attempts=1)

APP = AppCredentials(
    platform="google_business",
    host=None,
    client_id="client-id.apps.googleusercontent.com",
    client_secret="client-secret",
)

# A throwaway 1024-bit RSA keypair, generated once for these tests only. It
# proves nothing about anyone's real key and signs nothing that ever leaves
# this process.
_N = 104381542679399550398733857845728037057073688960642932599507573928579178251377963413207413367842061985523261411477007891176877164505112875515683005592464429475458234740060427083220674849491991620920416248522296543156327471817000252550004096375268796950051813232681232301660963259943071158937407495488716189509  # noqa: E501
_E = 65537
_D = 55440699438900452413591205849397244944377649862383384055667167262461092447811397049763291757651621765629784800526468524405693230072454858840432270956377215905609236787449649467028060112955922562923220600028661569070249533622245260473895127497864773421657602112872149230458361877196572272856221486017934273601  # noqa: E501
_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _int_to_bytes(value: int) -> bytes:
    length = (value.bit_length() + 7) // 8
    return value.to_bytes(length, "big")


def _sign_rs256(signing_input: bytes) -> bytes:
    modulus_bytes = (_N.bit_length() + 7) // 8
    tail = _SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(signing_input).digest()
    padding_length = modulus_bytes - len(tail) - 3
    padded = b"\x00\x01" + b"\xff" * padding_length + b"\x00" + tail
    signature_int = pow(int.from_bytes(padded, "big"), _D, _N)
    return signature_int.to_bytes(modulus_bytes, "big")


def jwk(kid: str = "key-1") -> dict[str, str]:
    """The public half of the test keypair, in JWK form."""
    return {
        "kid": kid,
        "n": _b64url(_int_to_bytes(_N)),
        "e": _b64url(_int_to_bytes(_E)),
    }


def make_jwt(
    payload: dict[str, Any],
    *,
    kid: str = "key-1",
    alg: str = "RS256",
    header: dict[str, Any] | None = None,
) -> str:
    """Sign a token with the test keypair, the way Google's OIDC tokens are."""
    used_header = (
        header if header is not None else {"alg": alg, "kid": kid, "typ": "JWT"}
    )
    header_b64 = _b64url(json.dumps(used_header).encode())
    payload_b64 = _b64url(json.dumps(payload).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode()
    signature = _sign_rs256(signing_input)
    return f"{header_b64}.{payload_b64}.{_b64url(signature)}"


def secret_config(
    *,
    audience: str = AUDIENCE,
    keys: list[dict[str, str]] | None = None,
    service_account: str = SERVICE_ACCOUNT,
) -> str:
    return json.dumps(
        {
            "keys": keys if keys is not None else [jwk()],
            "audience": audience,
            "service_account": service_account,
        }
    )


def a_token(**extra: object) -> str:
    payload: dict[str, Any] = {
        "aud": AUDIENCE,
        "exp": time.time() + 300,
        "email": SERVICE_ACCOUNT,
        "email_verified": True,
    }
    payload.update(extra)
    return make_jwt(payload)


@pytest.fixture
def platform() -> GoogleBusinessPlatform:
    """A platform that gives up after one try."""
    return GoogleBusinessPlatform(retries=ONE_TRY)


@pytest.fixture
def account() -> Connection:
    """A connected location."""
    return Connection(
        id="google_business:456",
        platform="google_business",
        host=None,
        account_id="456",
        account_name="Ada's Bakery",
        token=Token(
            access_token="access-one",
            refresh_token="refresh-one",
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        ),
        scopes=("https://www.googleapis.com/auth/business.manage",),
        extra={"location_name": LOCATION},
    )


def login(
    *,
    state: str | None = None,
    scopes: tuple[str, ...] = (),
    app: AppCredentials | None = APP,
) -> LoginRequest:
    return LoginRequest(redirect_uri=REDIRECT, scopes=scopes, state=state, app=app)


def token_reply(**extra: object) -> dict[str, Any]:
    said: dict[str, Any] = {
        "access_token": "access-one",
        "refresh_token": "refresh-one",
        "expires_in": 3599,
        "scope": "https://www.googleapis.com/auth/business.manage",
        "token_type": "Bearer",
    }
    said.update(extra)
    return said


def accounts_reply(*names: str) -> dict[str, Any]:
    return {"accounts": [{"name": name, "accountName": name} for name in names]}


def locations_reply(*items: tuple[str, str]) -> dict[str, Any]:
    return {"locations": [{"name": name, "title": title} for name, title in items]}


def google_error(
    reason: str, *, code: int = 403, message: str = "no"
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "errors": [{"domain": "global", "reason": reason, "message": message}],
        }
    }


def google_status_error(
    status: str, *, code: int = 403, message: str = "no"
) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "status": status}}


async def sign_in(
    platform: GoogleBusinessPlatform,
    network: respx.Router,
    *,
    locations: tuple[tuple[str, str], ...] = ((LOCATION, "Ada's Bakery"),),
    reply: dict[str, Any] | None = None,
) -> ChooseAccount:
    said = reply if reply is not None else token_reply()
    network.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json=said)
    )
    network.get(f"{ACCOUNT_MANAGEMENT_API}/accounts").mock(
        return_value=httpx.Response(200, json=accounts_reply("accounts/123"))
    )
    network.get(f"{BUSINESS_INFORMATION_API}/accounts/123/locations").mock(
        return_value=httpx.Response(200, json=locations_reply(*locations))
    )
    step = await platform.finish_login(
        login(), {"code": "the-code"}, {"code_verifier": "the-secret"}
    )
    assert isinstance(step, ChooseAccount)
    return step


def a_post(**options: object) -> Post:
    return Post(text="Fresh bread daily", options=options)


# ---------------------------------------------------------------------------
# What it says it can do
# ---------------------------------------------------------------------------


class TestWhatItSaysItCanDo:
    def test_it_provides_everything_a_platform_must(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        checked: Platform = platform
        resumes: CanResumeLogin = platform
        reads_updates: CanReadUpdates = platform
        replies: CanReplyToUpdates = platform
        edits: CanEditBusinessInfo = platform
        verifies: CanManageVerification = platform
        checks_signature: CanCheckSignature = platform
        reads_pushed: CanReadPushedUpdates = platform

        assert isinstance(checked, Platform)
        assert isinstance(resumes, CanResumeLogin)
        assert isinstance(reads_updates, CanReadUpdates)
        assert isinstance(replies, CanReplyToUpdates)
        assert isinstance(edits, CanEditBusinessInfo)
        assert isinstance(verifies, CanManageVerification)
        assert isinstance(checks_signature, CanCheckSignature)
        assert isinstance(reads_pushed, CanReadPushedUpdates)
        assert platform.name == "google_business"

    def test_it_lists_only_what_it_really_does(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        assert Feature.POST_TEXT in platform.features
        assert Feature.POST_IMAGE in platform.features
        assert Feature.PUSH_UPDATES in platform.features
        assert Feature.POST_VIDEO not in platform.features
        assert Feature.SCHEDULE not in platform.features
        assert Feature.READ_STATS not in platform.features
        assert Feature.CREATE_APP not in platform.features
        assert Feature.DELETE_POST not in platform.features


class TestWhereTheApiIs:
    def test_the_main_address_is_business_information(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        assert (
            platform.api_base(account)
            == "https://mybusinessbusinessinformation.googleapis.com/v1"
        )

    def test_the_headers_carry_the_accounts_own_token(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        assert platform.auth_headers(account) == {"Authorization": "Bearer access-one"}

    async def test_it_allows_one_picture_and_1500_characters(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        limits = await platform.limits(account)

        assert limits.max_text_length == 1500
        assert limits.max_images == 1


# ---------------------------------------------------------------------------
# Signing someone in
# ---------------------------------------------------------------------------


class TestSendingSomeoneToGoogle:
    async def test_the_address_asks_for_a_refresh_token_out_loud(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        step = await platform.start_login(login())

        assert isinstance(step, SendToNetwork)
        query = parse_qs(urlparse(step.url).query)
        assert query["access_type"] == ["offline"]
        assert query["prompt"] == ["consent"]

    async def test_it_asks_for_business_manage_by_default(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        step = await platform.start_login(login())

        query = parse_qs(urlparse(step.url).query)
        assert query["scope"] == ["https://www.googleapis.com/auth/business.manage"]

    async def test_a_login_without_credentials_points_at_the_console(self) -> None:
        bare = GoogleBusinessPlatform()

        with pytest.raises(ConfigError, match=r"console\.cloud\.google\.com"):
            await bare.start_login(LoginRequest(redirect_uri=REDIRECT))

    async def test_only_the_hash_of_the_secret_travels_to_google(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        step = await platform.start_login(login())

        assert isinstance(step, SendToNetwork)
        verifier = step.remember["code_verifier"]
        assert verifier not in step.url


class TestSwappingTheCodeForAToken:
    async def test_it_lists_locations_across_every_account(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(200, json=token_reply())
            )
            network.get(f"{ACCOUNT_MANAGEMENT_API}/accounts").mock(
                return_value=httpx.Response(
                    200, json=accounts_reply("accounts/123", "accounts/999")
                )
            )
            network.get(f"{BUSINESS_INFORMATION_API}/accounts/123/locations").mock(
                return_value=httpx.Response(
                    200, json=locations_reply((LOCATION, "Ada's Bakery"))
                )
            )
            network.get(f"{BUSINESS_INFORMATION_API}/accounts/999/locations").mock(
                return_value=httpx.Response(
                    200, json=locations_reply((OTHER_LOCATION, "Ada's Cafe"))
                )
            )

            step = await platform.finish_login(
                login(), {"code": "the-code"}, {"code_verifier": "v"}
            )

        assert isinstance(step, ChooseAccount)
        assert {option.id for option in step.options} == {LOCATION, OTHER_LOCATION}
        assert all(option.kind == "location" for option in step.options)

    async def test_it_asks_which_location_even_when_there_is_only_one(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        with respx.mock() as network:
            step = await sign_in(platform, network)

        assert len(step.options) == 1

    async def test_a_reply_with_no_refresh_token_is_refused(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(200, json=token_reply(refresh_token=None))
            )

            with pytest.raises(AuthError, match="refresh token"):
                await platform.finish_login(
                    login(), {"code": "the-code"}, {"code_verifier": "v"}
                )

    async def test_an_account_with_no_business_profile_says_so(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(200, json=token_reply())
            )
            network.get(f"{ACCOUNT_MANAGEMENT_API}/accounts").mock(
                return_value=httpx.Response(200, json={"accounts": []})
            )

            with pytest.raises(AuthError, match="no Business Profile account"):
                await platform.finish_login(
                    login(), {"code": "the-code"}, {"code_verifier": "v"}
                )

    async def test_an_account_with_no_locations_says_so(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(200, json=token_reply())
            )
            network.get(f"{ACCOUNT_MANAGEMENT_API}/accounts").mock(
                return_value=httpx.Response(200, json=accounts_reply("accounts/123"))
            )
            network.get(f"{BUSINESS_INFORMATION_API}/accounts/123/locations").mock(
                return_value=httpx.Response(200, json={"locations": []})
            )

            with pytest.raises(AuthError, match="no Business Profile location"):
                await platform.finish_login(
                    login(), {"code": "the-code"}, {"code_verifier": "v"}
                )

    async def test_a_login_without_credentials_points_at_the_console(self) -> None:
        bare = GoogleBusinessPlatform()

        with pytest.raises(ConfigError, match=r"console\.cloud\.google\.com"):
            await bare.finish_login(
                LoginRequest(redirect_uri=REDIRECT),
                {"code": "the-code"},
                {"code_verifier": "v"},
            )

    async def test_a_state_that_matches_is_accepted(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(200, json=token_reply())
            )
            network.get(f"{ACCOUNT_MANAGEMENT_API}/accounts").mock(
                return_value=httpx.Response(200, json=accounts_reply("accounts/123"))
            )
            network.get(f"{BUSINESS_INFORMATION_API}/accounts/123/locations").mock(
                return_value=httpx.Response(
                    200, json=locations_reply((LOCATION, "Ada's Bakery"))
                )
            )
            step = await platform.finish_login(
                login(state="ours"),
                {"code": "the-code", "state": "ours"},
                {"code_verifier": "v"},
            )

        assert isinstance(step, ChooseAccount)

    async def test_a_state_that_does_not_match_is_refused(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        with pytest.raises(AuthError, match="did not start here"):
            await platform.finish_login(
                login(state="ours"),
                {"code": "the-code", "state": "somebody-elses"},
                {"code_verifier": "v"},
            )

    async def test_a_callback_with_no_state_at_all_is_refused(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        with pytest.raises(AuthError, match="did not start here"):
            await platform.finish_login(
                login(state="ours"),
                {"code": "the-code"},
                {"code_verifier": "v"},
            )

    async def test_someone_pressing_cancel_is_said_plainly(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        with pytest.raises(AuthError, match="pressed cancel"):
            await platform.finish_login(
                login(),
                {"error": "access_denied", "error_description": "no thanks"},
                {"code_verifier": "v"},
            )

    async def test_a_callback_with_no_code_is_said_plainly(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        with pytest.raises(AuthError, match="no code"):
            await platform.finish_login(login(), {}, {"code_verifier": "v"})

    async def test_the_secret_from_the_first_step_has_to_come_back(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        with pytest.raises(AuthError, match="did not come back"):
            await platform.finish_login(login(), {"code": "the-code"}, None)

    async def test_an_account_list_that_is_not_really_a_list_counts_as_none(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(200, json=token_reply())
            )
            network.get(f"{ACCOUNT_MANAGEMENT_API}/accounts").mock(
                return_value=httpx.Response(200, json={"accounts": "oops"})
            )

            with pytest.raises(AuthError, match="no Business Profile account"):
                await platform.finish_login(
                    login(), {"code": "the-code"}, {"code_verifier": "v"}
                )

    async def test_an_account_with_no_name_says_so_plainly(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(200, json=token_reply())
            )
            network.get(f"{ACCOUNT_MANAGEMENT_API}/accounts").mock(
                return_value=httpx.Response(
                    200, json={"accounts": [{"accountName": "no id here"}]}
                )
            )

            with pytest.raises(PlatformError, match="'name'"):
                await platform.finish_login(
                    login(), {"code": "the-code"}, {"code_verifier": "v"}
                )

    async def test_a_location_with_no_name_falls_back_to_its_own_id(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(200, json=token_reply())
            )
            network.get(f"{ACCOUNT_MANAGEMENT_API}/accounts").mock(
                return_value=httpx.Response(200, json=accounts_reply("accounts/123"))
            )
            network.get(f"{BUSINESS_INFORMATION_API}/accounts/123/locations").mock(
                return_value=httpx.Response(
                    200, json={"locations": [{"name": LOCATION}]}
                )
            )

            step = await platform.finish_login(
                login(), {"code": "the-code"}, {"code_verifier": "v"}
            )

        assert step.options[0].name == LOCATION


class TestChoosingALocation:
    async def test_picking_a_location_finishes_the_login(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        with respx.mock() as network:
            step = await sign_in(platform, network)

        done = await platform.resume_login(
            login(), resume_token=step.resume_token, account_id=LOCATION
        )

        assert isinstance(done, Finished)
        connection = done.connection
        assert connection.platform == "google_business"
        assert connection.account_id == "456"
        assert connection.account_name == "Ada's Bakery"
        assert connection.extra["location_name"] == LOCATION
        assert connection.token.access_token == "access-one"

    async def test_a_location_nobody_offered_is_refused(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        with respx.mock() as network:
            step = await sign_in(platform, network)

        with pytest.raises(AuthError, match="was not one of the locations"):
            await platform.resume_login(
                login(),
                resume_token=step.resume_token,
                account_id="accounts/1/locations/2",
            )

    async def test_a_resume_token_that_makes_no_sense_is_refused(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        with pytest.raises(AuthError, match="could not be read"):
            await platform.resume_login(
                login(), resume_token="not-a-real-token", account_id=LOCATION
            )


# ---------------------------------------------------------------------------
# Keeping the token working
# ---------------------------------------------------------------------------


class TestKeepingTheTokenWorking:
    async def test_it_asks_google_for_a_new_access_token(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "access-two", "expires_in": 3599}
                )
            )

            token = await platform.refresh(account, APP)

        assert token.access_token == "access-two"
        assert token.refresh_token == "refresh-one"

    async def test_a_connection_with_no_refresh_token_cannot_be_renewed(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        without = account.with_token(Token(access_token="access-one"))

        with pytest.raises(TokenExpiredError, match="no refresh token"):
            await platform.refresh(without, APP)

    async def test_a_refresh_token_google_refuses_means_signing_in_again(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(400, json={"error": "invalid_grant"})
            )

            with pytest.raises(TokenExpiredError, match="connect their location"):
                await platform.refresh(account, APP)

    async def test_a_flatly_unauthenticated_refusal_still_means_signing_in_again(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(
                    401, json=google_status_error("UNAUTHENTICATED", code=401)
                )
            )

            with pytest.raises(TokenExpiredError, match="connect their location"):
                await platform.refresh(account, APP)

    async def test_google_having_a_bad_day_is_not_a_dead_refresh_token(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(500, text="oh dear")
            )

            with pytest.raises(PlatformError) as refused:
                await platform.refresh(account, APP)

        assert not isinstance(refused.value, TokenExpiredError)

    async def test_renewing_needs_your_apps_credentials(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        with pytest.raises(ConfigError, match=r"Storage\.save_app"):
            await platform.refresh(account)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


class TestPublishing:
    async def test_it_creates_an_ordinary_local_post(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            route = network.post(f"{LOCAL_API}/{LOCATION}/localPosts").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "name": f"{LOCATION}/localPosts/1",
                        "state": "LIVE",
                        "searchUrl": "https://g.co/p/1",
                    },
                )
            )

            result = await platform.publish(account, a_post())

        sent = json.loads(route.calls.last.request.content)
        assert sent["summary"] == "Fresh bread daily"
        assert sent["topicType"] == "STANDARD"
        assert result.id == f"{LOCATION}/localPosts/1"
        assert result.url == "https://g.co/p/1"
        assert result.state is PostState.DONE

    async def test_a_rejected_post_is_reported_as_failed(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.post(f"{LOCAL_API}/{LOCATION}/localPosts").mock(
                return_value=httpx.Response(
                    200, json={"name": f"{LOCATION}/localPosts/1", "state": "REJECTED"}
                )
            )

            result = await platform.publish(account, a_post())

        assert result.state is PostState.FAILED

    async def test_a_picture_from_a_web_address_is_sent_as_is(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        post = Post(
            text="Look at this",
            media=(Media.from_url("https://example.com/bread.jpg"),),
        )
        with respx.mock() as network:
            route = network.post(f"{LOCAL_API}/{LOCATION}/localPosts").mock(
                return_value=httpx.Response(
                    200, json={"name": f"{LOCATION}/localPosts/1", "state": "LIVE"}
                )
            )

            await platform.publish(account, post)

        sent = json.loads(route.calls.last.request.content)
        assert sent["media"] == [
            {"mediaFormat": "PHOTO", "sourceUrl": "https://example.com/bread.jpg"}
        ]

    async def test_a_local_file_picture_is_refused(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        post = Post(
            text="Look at this",
            media=(Media.from_bytes(b"not really a picture", filename="a.png"),),
        )

        with pytest.raises(InvalidPostError, match="fetches the picture itself"):
            await platform.publish(account, post)

    async def test_a_video_is_refused(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        post = Post(
            text="",
            media=(Media.from_bytes(b"not a video", filename="a.mp4"),),
        )

        with pytest.raises(NotSupportedError, match="video"):
            await platform.publish(account, post)

    async def test_scheduling_is_refused(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        post = Post(
            text="Later",
            publish_at=datetime(2099, 1, 1, tzinfo=UTC),
        )

        with pytest.raises(NotSupportedError, match="scheduling"):
            await platform.publish(account, post)

    async def test_a_call_to_action_is_sent_along(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            route = network.post(f"{LOCAL_API}/{LOCATION}/localPosts").mock(
                return_value=httpx.Response(
                    200, json={"name": f"{LOCATION}/localPosts/1", "state": "LIVE"}
                )
            )

            await platform.publish(
                account,
                a_post(
                    call_to_action_type="SHOP", call_to_action_url="https://example.com"
                ),
            )

        sent = json.loads(route.calls.last.request.content)
        assert sent["callToAction"] == {
            "actionType": "SHOP",
            "url": "https://example.com",
        }

    async def test_a_call_action_needs_no_url(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            route = network.post(f"{LOCAL_API}/{LOCATION}/localPosts").mock(
                return_value=httpx.Response(
                    200, json={"name": f"{LOCATION}/localPosts/1", "state": "LIVE"}
                )
            )

            await platform.publish(account, a_post(call_to_action_type="CALL"))

        sent = json.loads(route.calls.last.request.content)
        assert sent["callToAction"] == {"actionType": "CALL"}

    async def test_an_unknown_call_to_action_type_is_refused(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError, match="BOOK"):
            await platform.publish(account, a_post(call_to_action_type="DANCE"))

    async def test_a_call_to_action_with_no_url_is_refused(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError, match="call_to_action_url"):
            await platform.publish(account, a_post(call_to_action_type="SHOP"))

    async def test_an_unknown_option_is_refused(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError, match="topic_type"):
            await platform.publish(account, a_post(topic_type="EVENT"))

    async def test_a_summary_over_the_limit_never_reaches_google(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        post = Post(text="x" * 1501)

        with respx.mock(assert_all_called=False) as network:
            everything = network.route().mock(return_value=httpx.Response(200, json={}))

            with pytest.raises(InvalidPostError, match="1500"):
                await platform.publish(account, post)

        assert not everything.called

    async def test_a_connection_with_no_location_saved_is_refused(
        self, platform: GoogleBusinessPlatform
    ) -> None:
        bare = Connection(
            id="google_business:456",
            platform="google_business",
            host=None,
            account_id="456",
            account_name="Ada's Bakery",
            token=Token(access_token="access-one"),
        )

        with pytest.raises(ConfigError, match="location_name"):
            await platform.publish(bare, a_post())


# ---------------------------------------------------------------------------
# Reading reviews and questions on a timer
# ---------------------------------------------------------------------------


class TestPolling:
    async def test_it_reads_reviews_and_questions_together(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        review = {
            "reviewId": "r1",
            "comment": "Lovely",
            "createTime": "2026-08-31T10:00:00Z",
            "updateTime": "2026-08-31T10:00:00Z",
        }
        question = {
            "name": f"{LOCATION.replace('accounts/123/', '')}/questions/q1",
            "text": "Open Sundays?",
            "createTime": "2026-08-31T11:00:00Z",
            "updateTime": "2026-08-31T11:00:00Z",
        }
        with respx.mock() as network:
            network.get(f"{LOCAL_API}/{LOCATION}/reviews").mock(
                return_value=httpx.Response(200, json={"reviews": [review]})
            )
            network.get(f"{QANDA_API}/locations/456/questions").mock(
                return_value=httpx.Response(200, json={"questions": [question]})
            )

            updates = await platform.fetch_updates(account, None)

        assert [update.kind for update in updates] == [
            UpdateKind.REVIEW_CREATED,
            UpdateKind.QUESTION_CREATED,
        ]

    async def test_an_edited_review_is_an_update(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        review = {
            "reviewId": "r1",
            "createTime": "2026-08-31T10:00:00Z",
            "updateTime": "2026-08-31T12:00:00Z",
        }
        with respx.mock() as network:
            network.get(f"{LOCAL_API}/{LOCATION}/reviews").mock(
                return_value=httpx.Response(200, json={"reviews": [review]})
            )
            network.get(f"{QANDA_API}/locations/456/questions").mock(
                return_value=httpx.Response(200, json={"questions": []})
            )

            updates = await platform.fetch_updates(account, None)

        assert updates[0].kind is UpdateKind.REVIEW_UPDATED

    async def test_it_leaves_out_anything_older_than_the_marker(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        review = {
            "reviewId": "r1",
            "createTime": "2026-08-31T10:00:00Z",
            "updateTime": "2026-08-31T10:00:00Z",
        }
        with respx.mock() as network:
            network.get(f"{LOCAL_API}/{LOCATION}/reviews").mock(
                return_value=httpx.Response(200, json={"reviews": [review]})
            )
            network.get(f"{QANDA_API}/locations/456/questions").mock(
                return_value=httpx.Response(200, json={"questions": []})
            )

            updates = await platform.fetch_updates(
                account, datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
            )

        assert updates == []

    async def test_a_review_with_no_readable_time_is_left_out(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.get(f"{LOCAL_API}/{LOCATION}/reviews").mock(
                return_value=httpx.Response(200, json={"reviews": [{"reviewId": "r1"}]})
            )
            network.get(f"{QANDA_API}/locations/456/questions").mock(
                return_value=httpx.Response(200, json={"questions": []})
            )

            updates = await platform.fetch_updates(account, None)

        assert updates == []

    async def test_a_question_with_no_readable_time_is_left_out(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.get(f"{LOCAL_API}/{LOCATION}/reviews").mock(
                return_value=httpx.Response(200, json={"reviews": []})
            )
            network.get(f"{QANDA_API}/locations/456/questions").mock(
                return_value=httpx.Response(
                    200, json={"questions": [{"name": "locations/456/questions/q1"}]}
                )
            )

            updates = await platform.fetch_updates(account, None)

        assert updates == []


# ---------------------------------------------------------------------------
# Replying to a review or a question
# ---------------------------------------------------------------------------


class TestReplyingToUpdates:
    def _update(self, kind: UpdateKind, raw: dict[str, Any]) -> Update:
        return Update.from_network(
            update_id="u1",
            kind_name=kind.value,
            platform="google_business",
            connection_id="google_business:456",
            created_at=datetime(2026, 8, 31, 10, 0, tzinfo=UTC),
            raw=raw,
        )

    async def test_it_replies_to_a_review_by_id(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        update = self._update(UpdateKind.REVIEW_CREATED, {"reviewId": "r1"})
        with respx.mock() as network:
            route = network.patch(f"{LOCAL_API}/{LOCATION}/reviews/r1").mock(
                return_value=httpx.Response(200, json={})
            )

            await platform.reply_to_update(account, update, "Thank you!")

        sent = json.loads(route.calls.last.request.content)
        assert sent == {"comment": "Thank you!"}
        assert route.calls.last.request.url.params["updateMask"] == "comment"

    async def test_it_replies_to_a_review_by_its_full_name(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        update = self._update(
            UpdateKind.REVIEW_UPDATED, {"name": f"{LOCATION}/reviews/r9"}
        )
        with respx.mock() as network:
            route = network.patch(f"{LOCAL_API}/{LOCATION}/reviews/r9").mock(
                return_value=httpx.Response(200, json={})
            )

            await platform.reply_to_update(account, update, "Thanks!")

        assert route.called

    async def test_it_answers_a_question(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        update = self._update(
            UpdateKind.QUESTION_CREATED, {"name": "locations/456/questions/q1"}
        )
        with respx.mock() as network:
            route = network.post(
                f"{QANDA_API}/locations/456/questions/q1/answers:upsert"
            ).mock(return_value=httpx.Response(200, json={}))

            await platform.reply_to_update(account, update, "Yes, until 2pm.")

        sent = json.loads(route.calls.last.request.content)
        assert sent == {"answer": {"text": "Yes, until 2pm."}}

    async def test_it_answers_from_a_pushed_answer_update_too(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        update = self._update(
            UpdateKind.ANSWER_CREATED,
            {"name": "locations/456/questions/q1/answers/a1"},
        )
        with respx.mock() as network:
            route = network.post(
                f"{QANDA_API}/locations/456/questions/q1/answers:upsert"
            ).mock(return_value=httpx.Response(200, json={}))

            await platform.reply_to_update(account, update, "Also yes.")

        assert route.called

    async def test_anything_else_cannot_be_answered(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        update = self._update(UpdateKind.MENTION, {})

        with pytest.raises(NotSupportedError, match="answering"):
            await platform.reply_to_update(account, update, "hello")


# ---------------------------------------------------------------------------
# Business information
# ---------------------------------------------------------------------------


class TestBusinessInformation:
    async def test_it_reads_the_locations_information_back(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        reply = {
            "title": "Ada's Bakery",
            "phoneNumbers": {"primaryPhone": "+1 555 0100"},
            "storefrontAddress": {"addressLines": ["1 Main St"]},
            "categories": {
                "primaryCategory": {"displayName": "Bakery"},
                "additionalCategories": [{"displayName": "Cafe"}],
            },
        }
        with respx.mock() as network:
            network.get(f"{BUSINESS_INFORMATION_API}/{LOCATION}").mock(
                return_value=httpx.Response(200, json=reply)
            )

            location = await platform.get_location(account)

        assert location == BusinessLocation(
            id="456",
            name="Ada's Bakery",
            phone="+1 555 0100",
            address={"addressLines": ["1 Main St"]},
            categories=("Bakery", "Cafe"),
            raw=reply,
        )

    async def test_a_primary_category_with_no_display_name_is_skipped(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        reply = {
            "title": "Ada's Bakery",
            "categories": {"primaryCategory": {}, "additionalCategories": []},
        }
        with respx.mock() as network:
            network.get(f"{BUSINESS_INFORMATION_API}/{LOCATION}").mock(
                return_value=httpx.Response(200, json=reply)
            )

            location = await platform.get_location(account)

        assert location.categories == ()

    async def test_a_location_with_nothing_much_on_file(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.get(f"{BUSINESS_INFORMATION_API}/{LOCATION}").mock(
                return_value=httpx.Response(200, json={"title": "Ada's Bakery"})
            )

            location = await platform.get_location(account)

        assert location.phone is None
        assert location.address == {}
        assert location.categories == ()

    async def test_it_sends_only_the_fields_given(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            route = network.patch(f"{BUSINESS_INFORMATION_API}/{LOCATION}").mock(
                return_value=httpx.Response(200, json={"title": "New Name"})
            )

            await platform.update_location(account, {"title": "New Name"})

        assert route.calls.last.request.url.params["updateMask"] == "title"
        assert json.loads(route.calls.last.request.content) == {"title": "New Name"}

    async def test_changing_nothing_is_refused(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        with pytest.raises(ConfigError, match="empty"):
            await platform.update_location(account, {})


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class TestVerification:
    async def test_it_lists_the_offered_methods(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.post(
                f"{VERIFICATIONS_API}/locations/456:fetchVerificationOptions"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "options": [
                            {
                                "verificationMethod": "PHONE_CALL",
                                "phoneNumber": "***1234",
                            }
                        ]
                    },
                )
            )

            options = await platform.verification_options(account)

        assert options == (
            VerificationOption(
                method="PHONE_CALL", display_data={"phoneNumber": "***1234"}
            ),
        )

    async def test_it_starts_a_verification(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.post(f"{VERIFICATIONS_API}/locations/456:verify").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "name": "locations/456/verifications/v1",
                        "method": "PHONE_CALL",
                        "state": "PENDING",
                    },
                )
            )

            verification = await platform.start_verification(account, "PHONE_CALL")

        assert verification == Verification(
            id="locations/456/verifications/v1",
            method="PHONE_CALL",
            state="PENDING",
            raw={
                "name": "locations/456/verifications/v1",
                "method": "PHONE_CALL",
                "state": "PENDING",
            },
        )

    async def test_it_completes_a_verification(
        self, platform: GoogleBusinessPlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            route = network.post(
                f"{VERIFICATIONS_API}/locations/456/verifications/v1:complete"
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "name": "locations/456/verifications/v1",
                        "method": "PHONE_CALL",
                        "state": "COMPLETED",
                    },
                )
            )

            verification = await platform.complete_verification(
                account, "locations/456/verifications/v1", "123456"
            )

        assert json.loads(route.calls.last.request.content) == {"pin": "123456"}
        assert verification.state == "COMPLETED"

    @pytest.mark.parametrize(
        ("reply", "expected"),
        [
            ({"hasVoiceOfMerchant": True}, "VERIFIED"),
            ({"hasBusinessAuthority": True}, "PENDING"),
            ({}, "UNVERIFIED"),
        ],
    )
    async def test_it_reads_the_verification_state(
        self,
        platform: GoogleBusinessPlatform,
        account: Connection,
        reply: dict[str, Any],
        expected: str,
    ) -> None:
        with respx.mock() as network:
            network.get(f"{VERIFICATIONS_API}/locations/456/voiceOfMerchantState").mock(
                return_value=httpx.Response(200, json=reply)
            )

            state = await platform.verification_state(account)

        assert state == expected


# ---------------------------------------------------------------------------
# The Pub/Sub webhook
# ---------------------------------------------------------------------------


class TestCheckingASignature:
    def test_a_good_token_passes(self) -> None:
        headers = {"Authorization": f"Bearer {a_token()}"}

        GoogleBusinessPlatform().check_signature(b"{}", headers, secret=secret_config())

    def test_headers_are_read_whatever_case_they_arrive_in(self) -> None:
        headers = {"authorization": f"Bearer {a_token()}"}

        GoogleBusinessPlatform().check_signature(b"{}", headers, secret=secret_config())

    def test_no_authorization_header_at_all(self) -> None:
        with pytest.raises(SignatureError):
            GoogleBusinessPlatform().check_signature(b"{}", {}, secret=secret_config())

    def test_headers_that_are_not_authorization_are_skipped_over(self) -> None:
        headers = {"X-Other": "whatever"}

        with pytest.raises(SignatureError):
            GoogleBusinessPlatform().check_signature(
                b"{}", headers, secret=secret_config()
            )

    def test_a_header_with_no_bearer_prefix(self) -> None:
        headers = {"Authorization": a_token()}

        with pytest.raises(SignatureError):
            GoogleBusinessPlatform().check_signature(
                b"{}", headers, secret=secret_config()
            )

    def test_a_token_for_the_wrong_audience(self) -> None:
        headers = {
            "Authorization": f"Bearer {a_token(aud='https://someone-else.example')}"
        }

        with pytest.raises(SignatureError):
            GoogleBusinessPlatform().check_signature(
                b"{}", headers, secret=secret_config()
            )

    def test_an_expired_token(self) -> None:
        headers = {"Authorization": f"Bearer {a_token(exp=time.time() - 60)}"}

        with pytest.raises(SignatureError):
            GoogleBusinessPlatform().check_signature(
                b"{}", headers, secret=secret_config()
            )

    def test_a_token_signed_with_a_key_nobody_offered(self) -> None:
        headers = {"Authorization": f"Bearer {a_token()}"}

        with pytest.raises(SignatureError):
            GoogleBusinessPlatform().check_signature(
                b"{}", headers, secret=secret_config(keys=[jwk(kid="some-other-key")])
            )

    def test_a_token_for_the_wrong_service_account(self) -> None:
        headers = {
            "Authorization": f"Bearer {a_token(email='someone-else@example.com')}"
        }

        with pytest.raises(SignatureError):
            GoogleBusinessPlatform().check_signature(
                b"{}", headers, secret=secret_config()
            )

    def test_a_token_whose_email_is_not_verified(self) -> None:
        headers = {"Authorization": f"Bearer {a_token(email_verified=False)}"}

        with pytest.raises(SignatureError):
            GoogleBusinessPlatform().check_signature(
                b"{}", headers, secret=secret_config()
            )

    def test_a_token_signed_with_an_algorithm_we_do_not_trust(self) -> None:
        token = make_jwt(
            {"aud": AUDIENCE, "exp": time.time() + 300},
            header={"alg": "none", "kid": "key-1"},
        )
        headers = {"Authorization": f"Bearer {token}"}

        with pytest.raises(SignatureError):
            GoogleBusinessPlatform().check_signature(
                b"{}", headers, secret=secret_config()
            )

    def test_a_token_that_is_not_three_parts(self) -> None:
        headers = {"Authorization": "Bearer not-a-jwt"}

        with pytest.raises(SignatureError):
            GoogleBusinessPlatform().check_signature(
                b"{}", headers, secret=secret_config()
            )

    def test_a_token_whose_parts_are_not_readable(self) -> None:
        headers = {"Authorization": "Bearer not.valid.base64!!!"}

        with pytest.raises(SignatureError):
            GoogleBusinessPlatform().check_signature(
                b"{}", headers, secret=secret_config()
            )

    def test_a_tampered_signature(self) -> None:
        token = a_token()
        header_b64, payload_b64, _ = token.split(".")
        broken_signature = _b64url(b"\x00" * 128)
        tampered = f"{header_b64}.{payload_b64}.{broken_signature}"
        headers = {"Authorization": f"Bearer {tampered}"}

        with pytest.raises(SignatureError):
            GoogleBusinessPlatform().check_signature(
                b"{}", headers, secret=secret_config()
            )

    def test_a_tampered_payload(self) -> None:
        token = a_token()
        header_b64, _, signature_b64 = token.split(".")
        forged_payload = _b64url(
            json.dumps({"aud": AUDIENCE, "exp": time.time() + 300}).encode()
        )
        tampered = f"{header_b64}.{forged_payload}.{signature_b64}"
        headers = {"Authorization": f"Bearer {tampered}"}

        with pytest.raises(SignatureError):
            GoogleBusinessPlatform().check_signature(
                b"{}", headers, secret=secret_config()
            )

    def test_a_secret_that_is_not_json_is_a_config_error(self) -> None:
        headers = {"Authorization": f"Bearer {a_token()}"}

        with pytest.raises(ConfigError):
            GoogleBusinessPlatform().check_signature(b"{}", headers, secret="not json")

    def test_a_secret_missing_audience_is_a_config_error(self) -> None:
        headers = {"Authorization": f"Bearer {a_token()}"}

        with pytest.raises(ConfigError):
            GoogleBusinessPlatform().check_signature(
                b"{}",
                headers,
                secret=json.dumps(
                    {"keys": [jwk()], "service_account": SERVICE_ACCOUNT}
                ),
            )

    def test_a_secret_missing_service_account_is_a_config_error(self) -> None:
        headers = {"Authorization": f"Bearer {a_token()}"}

        with pytest.raises(ConfigError):
            GoogleBusinessPlatform().check_signature(
                b"{}",
                headers,
                secret=json.dumps({"keys": [jwk()], "audience": AUDIENCE}),
            )

    def test_a_secret_with_the_wrong_shapes_is_a_config_error(self) -> None:
        headers = {"Authorization": f"Bearer {a_token()}"}

        with pytest.raises(ConfigError):
            GoogleBusinessPlatform().check_signature(
                b"{}",
                headers,
                secret=json.dumps(
                    {
                        "keys": "nope",
                        "audience": AUDIENCE,
                        "service_account": SERVICE_ACCOUNT,
                    }
                ),
            )

    def test_a_payload_that_is_not_an_object_is_untrusted(self) -> None:
        header_b64 = _b64url(json.dumps({"alg": "RS256", "kid": "key-1"}).encode())
        payload_b64 = _b64url(json.dumps([]).encode())
        signature_b64 = _b64url(b"\x00")
        headers = {
            "Authorization": f"Bearer {header_b64}.{payload_b64}.{signature_b64}"
        }

        with pytest.raises(SignatureError):
            GoogleBusinessPlatform().check_signature(
                b"{}", headers, secret=secret_config()
            )

    def test_a_key_with_no_modulus_or_exponent_is_untrusted(self) -> None:
        headers = {"Authorization": f"Bearer {a_token()}"}
        broken_key = {"kid": "key-1"}

        with pytest.raises(SignatureError):
            GoogleBusinessPlatform().check_signature(
                b"{}", headers, secret=secret_config(keys=[broken_key])
            )

    def test_a_signature_the_wrong_number_of_bytes_long(self) -> None:
        header_b64 = _b64url(json.dumps({"alg": "RS256", "kid": "key-1"}).encode())
        payload_b64 = _b64url(
            json.dumps({"aud": AUDIENCE, "exp": time.time() + 300}).encode()
        )
        short_signature = _b64url(b"\x00" * 10)
        headers = {
            "Authorization": f"Bearer {header_b64}.{payload_b64}.{short_signature}"
        }

        with pytest.raises(SignatureError):
            GoogleBusinessPlatform().check_signature(
                b"{}", headers, secret=secret_config()
            )

    def test_a_signature_that_reads_as_bigger_than_the_modulus(self) -> None:
        header_b64 = _b64url(json.dumps({"alg": "RS256", "kid": "key-1"}).encode())
        payload_b64 = _b64url(
            json.dumps({"aud": AUDIENCE, "exp": time.time() + 300}).encode()
        )
        modulus_bytes = (_N.bit_length() + 7) // 8
        biggest_signature = _b64url(b"\xff" * modulus_bytes)
        headers = {
            "Authorization": f"Bearer {header_b64}.{payload_b64}.{biggest_signature}"
        }

        with pytest.raises(SignatureError):
            GoogleBusinessPlatform().check_signature(
                b"{}", headers, secret=secret_config()
            )

    def test_a_key_too_small_to_carry_a_sha256_hash(self) -> None:
        tiny_n = 2**300
        modulus_bytes = (tiny_n.bit_length() + 7) // 8
        tiny_key = {
            "kid": "tiny",
            "n": _b64url(_int_to_bytes(tiny_n)),
            "e": _b64url(_int_to_bytes(65537)),
        }
        header_b64 = _b64url(json.dumps({"alg": "RS256", "kid": "tiny"}).encode())
        payload_b64 = _b64url(
            json.dumps({"aud": AUDIENCE, "exp": time.time() + 300}).encode()
        )
        signature_b64 = _b64url(b"\x00" * (modulus_bytes - 1) + b"\x01")
        headers = {
            "Authorization": f"Bearer {header_b64}.{payload_b64}.{signature_b64}"
        }

        with pytest.raises(SignatureError):
            GoogleBusinessPlatform().check_signature(
                b"{}", headers, secret=secret_config(keys=[tiny_key])
            )


def push_body(notification: dict[str, Any]) -> bytes:
    data = base64.b64encode(json.dumps(notification).encode()).decode()
    return json.dumps(
        {
            "message": {"data": data, "messageId": "1", "publishTime": "now"},
            "subscription": "projects/p/subscriptions/s",
        }
    ).encode()


class TestReadingPushedUpdates:
    def test_a_new_review(self) -> None:
        notification = {
            "locationName": LOCATION,
            "notificationType": "NEW_REVIEW",
            "review": {
                "reviewId": "r1",
                "createTime": "2026-08-31T10:00:00Z",
                "updateTime": "2026-08-31T10:00:00Z",
            },
        }

        updates = GoogleBusinessPlatform().read_updates(push_body(notification))

        assert len(updates) == 1
        assert updates[0].kind is UpdateKind.REVIEW_CREATED
        assert updates[0].connection_id == "google_business:456"
        assert updates[0].raw["reviewId"] == "r1"

    def test_an_updated_review(self) -> None:
        notification = {
            "locationName": LOCATION,
            "notificationType": "UPDATED_REVIEW",
            "review": {"reviewId": "r1", "updateTime": "2026-08-31T10:00:00Z"},
        }

        updates = GoogleBusinessPlatform().read_updates(push_body(notification))

        assert updates[0].kind is UpdateKind.REVIEW_UPDATED

    def test_a_new_question(self) -> None:
        notification = {
            "locationName": LOCATION,
            "notificationType": "NEW_QUESTION",
            "question": {"name": "locations/456/questions/q1"},
        }

        updates = GoogleBusinessPlatform().read_updates(push_body(notification))

        assert updates[0].kind is UpdateKind.QUESTION_CREATED

    def test_a_notification_type_nobody_has_named_yet(self) -> None:
        notification = {"locationName": LOCATION, "notificationType": "NEW_MEDIA"}

        updates = GoogleBusinessPlatform().read_updates(push_body(notification))

        assert updates[0].kind is UpdateKind.UNKNOWN
        assert updates[0].kind_name == "NEW_MEDIA"

    def test_a_message_with_no_data_carries_nothing(self) -> None:
        body = json.dumps({"message": {}, "subscription": "s"}).encode()

        assert GoogleBusinessPlatform().read_updates(body) == []

    def test_data_that_is_not_base64_carries_nothing(self) -> None:
        body = json.dumps({"message": {"data": "not base64!!"}}).encode()

        assert GoogleBusinessPlatform().read_updates(body) == []

    def test_data_that_decodes_to_something_that_is_not_an_object(self) -> None:
        data = base64.b64encode(json.dumps([1, 2, 3]).encode()).decode()
        body = json.dumps({"message": {"data": data}}).encode()

        assert GoogleBusinessPlatform().read_updates(body) == []

    def test_a_body_that_is_not_a_pubsub_push_at_all(self) -> None:
        with pytest.raises(PlatformError):
            GoogleBusinessPlatform().read_updates(b"not json")

    def test_read_update_hands_back_the_one_thing(self) -> None:
        notification = {"locationName": LOCATION, "notificationType": "NEW_REVIEW"}

        update = GoogleBusinessPlatform().read_update(push_body(notification), {})

        assert update.platform == "google_business"

    def test_read_update_with_nothing_to_read_says_so(self) -> None:
        body = json.dumps({"message": {}}).encode()

        with pytest.raises(PlatformError, match="nothing"):
            GoogleBusinessPlatform().read_update(body, {})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TestTurningGooglesErrorsIntoOurs:
    def test_the_legacy_reason_shape_is_read(self) -> None:
        error = google_business_errors(
            httpx.Response(429, json=google_error("quotaExceeded", code=429))
        )

        assert isinstance(error, RateLimitError)

    def test_the_newer_status_shape_is_read_too(self) -> None:
        error = google_business_errors(
            httpx.Response(
                429, json=google_status_error("RESOURCE_EXHAUSTED", code=429)
            )
        )

        assert isinstance(error, RateLimitError)

    def test_an_unauthenticated_token(self) -> None:
        error = google_business_errors(
            httpx.Response(401, json=google_status_error("UNAUTHENTICATED", code=401))
        )

        assert isinstance(error, AuthError)

    def test_a_permission_never_asked_for(self) -> None:
        error = google_business_errors(
            httpx.Response(403, json=google_status_error("PERMISSION_DENIED"))
        )

        assert isinstance(error, NotAllowedError)

    def test_something_that_does_not_exist(self) -> None:
        error = google_business_errors(
            httpx.Response(404, json=google_status_error("NOT_FOUND", code=404))
        )

        assert isinstance(error, NotFoundError)

    def test_it_keeps_whatever_google_said(self) -> None:
        error = google_business_errors(
            httpx.Response(
                403, json=google_error("quotaExceeded", code=429, message="all gone")
            )
        )

        assert "all gone" in str(error)

    def test_a_reason_we_have_no_name_for_falls_back_to_the_shared_mapping(
        self,
    ) -> None:
        error = google_business_errors(
            httpx.Response(400, json=google_status_error("INVALID_ARGUMENT", code=400))
        )

        assert isinstance(error, PlatformError)
        assert error.status_code == 400

    def test_a_reply_that_is_not_googles_shape_at_all(self) -> None:
        error = google_business_errors(httpx.Response(500, text="<html>oh dear</html>"))

        assert isinstance(error, PlatformError)

    def test_it_looks_past_error_entries_with_no_reason_on_them(self) -> None:
        error = google_business_errors(
            httpx.Response(
                400,
                json={
                    "error": {
                        "code": 400,
                        "errors": ["not a dict", {"domain": "global"}],
                        "status": "INVALID_ARGUMENT",
                    }
                },
            )
        )

        assert isinstance(error, PlatformError)
        assert not isinstance(error, (AuthError, NotAllowedError, NotFoundError))

    def test_a_refusal_with_no_message_at_all(self) -> None:
        error = google_business_errors(
            httpx.Response(400, json={"error": {"code": 400}})
        )

        assert isinstance(error, PlatformError)


# ---------------------------------------------------------------------------
# The shared checks every platform has to pass
# ---------------------------------------------------------------------------


class TestGoogleBusinessBehavesLikeTheOthers(PlatformChecks):
    def make_platform(self) -> Platform:
        return GoogleBusinessPlatform(transport=self.transport, retries=ONE_TRY)

    def make_connection(self) -> Connection | None:
        return Connection(
            id="google_business:456",
            platform="google_business",
            host=None,
            account_id="456",
            account_name="Ada's Bakery",
            token=Token(access_token="access-one", refresh_token="refresh-one"),
            extra={"location_name": LOCATION},
        )

    def make_transport(self) -> httpx.AsyncBaseTransport | None:
        return RecordingTransport(
            {
                f"POST /v4/{LOCATION}/localPosts": {
                    "name": f"{LOCATION}/localPosts/1",
                    "state": "LIVE",
                },
                f"GET /v4/{LOCATION}/reviews": {"reviews": []},
                "GET /v1/locations/456/questions": {"questions": []},
            }
        )
