"""Tests for the YouTube platform."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
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
    Limits,
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
    CanCheckState,
    CanCreateApp,
    CanReadUpdates,
    CanResumeLogin,
    ChooseAccount,
    Finished,
    LoginRequest,
    Platform,
    SendToNetwork,
)
from socialchimp.platforms.youtube import (
    CHUNK_MULTIPLE,
    UPLOAD_URL,
    YouTubePlatform,
    youtube_errors,
)
from socialchimp.testing import PlatformChecks, RecordingTransport

REDIRECT = "https://app.example/callback"
CHANNEL = "UCadalovelace"
OTHER_CHANNEL = "UCcharlesbabbage"
VIDEO_ID = "dQw4w9WgXcQ"

# One try and no waiting, so the error tests do not spend real seconds asleep.
ONCE = Retries(attempts=1)

APP = AppCredentials(
    platform="youtube",
    host=None,
    client_id="client-id.apps.googleusercontent.com",
    client_secret="client-secret",
)

SESSION = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=up-1"

A_VIDEO_RESOURCE: dict[str, Any] = {
    "id": VIDEO_ID,
    "snippet": {"title": "Hello"},
    "status": {"uploadStatus": "uploaded", "privacyStatus": "private"},
}

A_COMMENT_THREAD: dict[str, Any] = {
    "id": "thread-1",
    "snippet": {
        "videoId": VIDEO_ID,
        "topLevelComment": {
            "id": "comment-1",
            "snippet": {
                "authorDisplayName": "Bob",
                "textDisplay": "Nice video",
                "publishedAt": "2026-08-31T10:00:00Z",
            },
        },
    },
}


@pytest.fixture
def platform() -> YouTubePlatform:
    """A platform that gives up after one try."""
    return YouTubePlatform(retries=ONCE)


@pytest.fixture
def account() -> Connection:
    """A connected channel."""
    return Connection(
        id=f"youtube:{CHANNEL}",
        platform="youtube",
        host=None,
        account_id=CHANNEL,
        account_name="Ada's Channel",
        token=Token(
            access_token="access-one",
            refresh_token="refresh-one",
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        ),
        scopes=("https://www.googleapis.com/auth/youtube.upload",),
        extra={"channel_id": CHANNEL},
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


def a_post(**options: object) -> Post:
    """A post that YouTube would accept, with settings layered on top."""
    settings: dict[str, Any] = {"title": "Hello", "made_for_kids": False}
    settings.update(options)
    return Post(text="A description", media=(a_video(),), options=settings)


def form_of(request: httpx.Request) -> dict[str, list[str]]:
    """Read a sent form back into a dictionary."""
    return parse_qs(request.content.decode(), keep_blank_values=True)


def body_of(request: httpx.Request) -> dict[str, Any]:
    """Read a sent JSON body back into a dictionary."""
    parsed: dict[str, Any] = json.loads(request.content)
    return parsed


def challenge_for(verifier: str) -> str:
    """Work out the code challenge a verifier should produce."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def token_reply(**extra: object) -> dict[str, Any]:
    """What Google's token endpoint answers with."""
    said: dict[str, Any] = {
        "access_token": "access-one",
        "refresh_token": "refresh-one",
        "expires_in": 3599,
        "scope": " ".join(
            (
                "https://www.googleapis.com/auth/youtube.upload",
                "https://www.googleapis.com/auth/youtube.readonly",
            )
        ),
        "token_type": "Bearer",
    }
    said.update(extra)
    return said


def channels_reply(*ids: str) -> dict[str, Any]:
    """What channels?mine=true answers with."""
    return {
        "items": [{"id": one, "snippet": {"title": f"Channel {one}"}} for one in ids]
    }


def google_error(
    reason: str, *, code: int = 403, message: str = "no"
) -> dict[str, Any]:
    """The shape Google wraps every refusal in."""
    return {
        "error": {
            "code": code,
            "message": message,
            "errors": [
                {"domain": "youtube.video", "reason": reason, "message": message}
            ],
        }
    }


async def sign_in(
    platform: YouTubePlatform,
    network: respx.Router,
    *,
    channels: tuple[str, ...] = (CHANNEL,),
    reply: dict[str, Any] | None = None,
) -> ChooseAccount:
    """Run a whole sign-in and insist YouTube asked which channel to use."""
    said = reply if reply is not None else token_reply()
    network.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json=said)
    )
    network.get("https://www.googleapis.com/youtube/v3/channels").mock(
        return_value=httpx.Response(200, json=channels_reply(*channels))
    )
    step = await platform.finish_login(
        login(), {"code": "the-code"}, {"code_verifier": "the-secret"}
    )
    assert isinstance(step, ChooseAccount)
    return step


# ---------------------------------------------------------------------------
# What it says it can do
# ---------------------------------------------------------------------------


class TestWhatItSaysItCanDo:
    def test_it_provides_everything_a_platform_must(
        self, platform: YouTubePlatform
    ) -> None:
        checked: Platform = platform
        reads: CanReadUpdates = platform
        resumes: CanResumeLogin = platform

        assert isinstance(checked, Platform)
        assert isinstance(reads, CanReadUpdates)
        assert isinstance(resumes, CanResumeLogin)
        assert platform.name == "youtube"

    def test_it_lists_the_features_youtube_really_has(
        self, platform: YouTubePlatform
    ) -> None:
        for feature in (Feature.POST_VIDEO, Feature.SCHEDULE, Feature.READ_POSTS):
            assert feature in platform.features

    def test_it_does_not_claim_it_can_post_words_on_their_own(
        self, platform: YouTubePlatform
    ) -> None:
        # Everything on YouTube is a video. Community posts are text, and
        # they are not in the API at all.
        assert Feature.POST_TEXT not in platform.features
        assert Feature.POST_IMAGE not in platform.features

    def test_it_does_not_claim_to_push_updates(self, platform: YouTubePlatform) -> None:
        # WebSub tells us about new uploads, never about comments.
        assert Feature.PUSH_UPDATES not in platform.features


class TestWhereTheApiIs:
    def test_every_channel_uses_the_same_address(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        assert platform.api_base(account) == "https://www.googleapis.com/youtube/v3"

    def test_the_headers_carry_the_accounts_own_token(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        assert platform.auth_headers(account) == {"Authorization": "Bearer access-one"}


# ---------------------------------------------------------------------------
# Registering an app
# ---------------------------------------------------------------------------


class TestRegisteringAnApp:
    def test_it_cannot_register_an_app_for_you(self, platform: YouTubePlatform) -> None:
        # Somebody has to make a Google Cloud project by hand, and Google
        # reviews the permissions before anyone outside the project can use
        # them. There is nothing to automate.
        assert Feature.CREATE_APP not in platform.features
        assert not isinstance(platform, CanCreateApp)
        assert not hasattr(platform, "create_app")

    async def test_starting_a_login_without_credentials_points_at_the_console(
        self,
    ) -> None:
        bare = YouTubePlatform()

        with pytest.raises(ConfigError) as refused:
            await bare.start_login(LoginRequest(redirect_uri=REDIRECT))

        said = str(refused.value)
        assert "console.cloud.google.com" in said
        assert "review" in said


# ---------------------------------------------------------------------------
# Signing someone in
# ---------------------------------------------------------------------------


class TestSendingSomeoneToGoogle:
    async def test_the_address_asks_for_a_refresh_token_out_loud(
        self, platform: YouTubePlatform
    ) -> None:
        step = await platform.start_login(login())

        assert isinstance(step, SendToNetwork)
        query = parse_qs(urlparse(step.url).query)
        # Without both of these Google hands back no refresh token at all
        # and the connection dies in an hour.
        assert query["access_type"] == ["offline"]
        assert query["prompt"] == ["consent"]

    async def test_the_address_is_googles_own_sign_in_page(
        self, platform: YouTubePlatform
    ) -> None:
        step = await platform.start_login(login())

        assert isinstance(step, SendToNetwork)
        assert step.url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        query = parse_qs(urlparse(step.url).query)
        assert query["response_type"] == ["code"]
        assert query["client_id"] == [APP.client_id]
        assert query["redirect_uri"] == [REDIRECT]
        assert query["state"] == [step.state]

    async def test_it_asks_for_upload_and_read_when_you_say_nothing(
        self, platform: YouTubePlatform
    ) -> None:
        step = await platform.start_login(login())

        assert isinstance(step, SendToNetwork)
        query = parse_qs(urlparse(step.url).query)
        assert query["scope"] == [
            "https://www.googleapis.com/auth/youtube.upload "
            "https://www.googleapis.com/auth/youtube.readonly"
        ]

    async def test_it_asks_for_the_scopes_you_named(
        self, platform: YouTubePlatform
    ) -> None:
        step = await platform.start_login(
            login(scopes=("https://www.googleapis.com/auth/youtube.upload",))
        )

        assert isinstance(step, SendToNetwork)
        query = parse_qs(urlparse(step.url).query)
        assert query["scope"] == ["https://www.googleapis.com/auth/youtube.upload"]

    async def test_it_keeps_the_state_you_gave_it(
        self, platform: YouTubePlatform
    ) -> None:
        step = await platform.start_login(login(state="mine"))

        assert isinstance(step, SendToNetwork)
        assert step.state == "mine"

    async def test_only_the_hash_of_the_secret_travels_to_google(
        self, platform: YouTubePlatform
    ) -> None:
        step = await platform.start_login(login())

        assert isinstance(step, SendToNetwork)
        verifier = step.remember["code_verifier"]
        query = parse_qs(urlparse(step.url).query)
        assert query["code_challenge_method"] == ["S256"]
        assert query["code_challenge"] == [challenge_for(verifier)]
        # The secret itself never leaves your server.
        assert verifier not in step.url

    async def test_two_logins_never_share_a_secret(
        self, platform: YouTubePlatform
    ) -> None:
        first = await platform.start_login(login())
        second = await platform.start_login(login())

        assert isinstance(first, SendToNetwork)
        assert isinstance(second, SendToNetwork)
        assert first.remember["code_verifier"] != second.remember["code_verifier"]


class TestSwappingTheCodeForAToken:
    async def test_it_sends_everything_googles_token_endpoint_wants(
        self, platform: YouTubePlatform
    ) -> None:
        with respx.mock() as network:
            route = network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(200, json=token_reply())
            )
            network.get("https://www.googleapis.com/youtube/v3/channels").mock(
                return_value=httpx.Response(200, json=channels_reply(CHANNEL))
            )

            await platform.finish_login(
                login(), {"code": "the-code"}, {"code_verifier": "secret-half"}
            )

        sent = form_of(route.calls.last.request)
        assert sent["grant_type"] == ["authorization_code"]
        assert sent["code"] == ["the-code"]
        assert sent["client_id"] == [APP.client_id]
        assert sent["client_secret"] == [APP.client_secret]
        assert sent["redirect_uri"] == [REDIRECT]
        # The other half of the pair made in start_login.
        assert sent["code_verifier"] == ["secret-half"]

    async def test_the_secret_from_the_first_step_has_to_come_back(
        self, platform: YouTubePlatform
    ) -> None:
        with pytest.raises(AuthError, match="did not come back"):
            await platform.finish_login(login(), {"code": "the-code"}, None)

    async def test_a_state_that_does_not_match_is_refused(
        self, platform: YouTubePlatform
    ) -> None:
        with pytest.raises(AuthError, match="did not start here"):
            await platform.finish_login(
                login(state="ours"),
                {"code": "the-code", "state": "somebody-elses"},
                {"code_verifier": "v"},
            )

    async def test_someone_pressing_cancel_is_said_plainly(
        self, platform: YouTubePlatform
    ) -> None:
        with pytest.raises(AuthError, match="pressed cancel"):
            await platform.finish_login(
                login(),
                {"error": "access_denied", "error_description": "no thanks"},
                {"code_verifier": "v"},
            )

    async def test_a_callback_with_no_code_is_said_plainly(
        self, platform: YouTubePlatform
    ) -> None:
        with pytest.raises(AuthError, match="no code"):
            await platform.finish_login(login(), {}, {"code_verifier": "v"})

    async def test_a_reply_with_no_refresh_token_is_refused_loudly(
        self, platform: YouTubePlatform
    ) -> None:
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(200, json=token_reply(refresh_token=None))
            )

            with pytest.raises(AuthError) as refused:
                await platform.finish_login(
                    login(), {"code": "the-code"}, {"code_verifier": "v"}
                )

        said = str(refused.value)
        assert "access_type=offline" in said
        assert "prompt=consent" in said

    async def test_a_reply_with_no_access_token_says_so(
        self, platform: YouTubePlatform
    ) -> None:
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(200, json={"expires_in": 3599})
            )

            with pytest.raises(PlatformError, match="access_token"):
                await platform.finish_login(
                    login(), {"code": "the-code"}, {"code_verifier": "v"}
                )

    async def test_a_login_without_credentials_points_at_the_console(self) -> None:
        bare = YouTubePlatform()

        with pytest.raises(ConfigError, match=re.escape("console.cloud.google.com")):
            await bare.finish_login(
                LoginRequest(redirect_uri=REDIRECT),
                {"code": "the-code"},
                {"code_verifier": "v"},
            )


class TestChoosingAChannel:
    async def test_it_lists_every_channel_the_person_has(
        self, platform: YouTubePlatform
    ) -> None:
        with respx.mock() as network:
            step = await sign_in(platform, network, channels=(CHANNEL, OTHER_CHANNEL))

        assert [option.id for option in step.options] == [CHANNEL, OTHER_CHANNEL]
        assert [option.kind for option in step.options] == ["channel", "channel"]
        assert step.options[0].name == f"Channel {CHANNEL}"

    async def test_it_asks_which_channel_even_when_there_is_only_one(
        self, platform: YouTubePlatform
    ) -> None:
        # A person can add a second channel tomorrow. An app that only
        # handles the one-channel case breaks the day they do.
        with respx.mock() as network:
            step = await sign_in(platform, network)

        assert len(step.options) == 1

    async def test_it_asks_google_only_about_this_persons_channels(
        self, platform: YouTubePlatform
    ) -> None:
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(200, json=token_reply())
            )
            route = network.get("https://www.googleapis.com/youtube/v3/channels").mock(
                return_value=httpx.Response(200, json=channels_reply(CHANNEL))
            )

            await platform.finish_login(
                login(), {"code": "the-code"}, {"code_verifier": "v"}
            )

        asked = route.calls.last.request
        assert asked.url.params["mine"] == "true"
        assert asked.url.params["part"] == "snippet"
        assert asked.headers["authorization"] == "Bearer access-one"

    async def test_a_channel_with_no_name_is_shown_by_its_id(
        self, platform: YouTubePlatform
    ) -> None:
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(200, json=token_reply())
            )
            network.get("https://www.googleapis.com/youtube/v3/channels").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "items": [
                            {"id": CHANNEL},
                            {"id": OTHER_CHANNEL, "snippet": {}},
                        ]
                    },
                )
            )

            step = await platform.finish_login(
                login(), {"code": "the-code"}, {"code_verifier": "v"}
            )

        assert isinstance(step, ChooseAccount)
        assert [option.name for option in step.options] == [CHANNEL, OTHER_CHANNEL]

    async def test_a_google_account_with_no_channel_says_so(
        self, platform: YouTubePlatform
    ) -> None:
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(200, json=token_reply())
            )
            network.get("https://www.googleapis.com/youtube/v3/channels").mock(
                return_value=httpx.Response(200, json={"items": []})
            )

            with pytest.raises(AuthError, match="no YouTube channel"):
                await platform.finish_login(
                    login(), {"code": "the-code"}, {"code_verifier": "v"}
                )

    async def test_picking_a_channel_finishes_the_login(
        self, platform: YouTubePlatform
    ) -> None:
        with respx.mock() as network:
            step = await sign_in(platform, network, channels=(CHANNEL, OTHER_CHANNEL))

        done = await platform.resume_login(
            login(), resume_token=step.resume_token, account_id=OTHER_CHANNEL
        )

        assert isinstance(done, Finished)
        connection = done.connection
        assert connection.platform == "youtube"
        assert connection.account_id == OTHER_CHANNEL
        assert connection.account_name == f"Channel {OTHER_CHANNEL}"
        assert connection.extra["channel_id"] == OTHER_CHANNEL
        assert connection.token.access_token == "access-one"
        assert connection.token.refresh_token == "refresh-one"

    async def test_the_token_it_saves_runs_out_in_about_an_hour(
        self, platform: YouTubePlatform
    ) -> None:
        with respx.mock() as network:
            step = await sign_in(platform, network)

        done = await platform.resume_login(
            login(), resume_token=step.resume_token, account_id=CHANNEL
        )

        assert isinstance(done, Finished)
        expires_at = done.connection.token.expires_at
        assert expires_at is not None
        left = (expires_at - datetime.now(UTC)).total_seconds()
        assert 3000 < left <= 3599

    async def test_it_keeps_the_permissions_google_actually_granted(
        self, platform: YouTubePlatform
    ) -> None:
        with respx.mock() as network:
            step = await sign_in(
                platform,
                network,
                reply=token_reply(
                    scope="https://www.googleapis.com/auth/youtube.upload"
                ),
            )

        done = await platform.resume_login(
            login(), resume_token=step.resume_token, account_id=CHANNEL
        )

        assert isinstance(done, Finished)
        assert done.connection.scopes == (
            "https://www.googleapis.com/auth/youtube.upload",
        )

    async def test_it_falls_back_to_what_we_asked_for(
        self, platform: YouTubePlatform
    ) -> None:
        with respx.mock() as network:
            step = await sign_in(platform, network, reply=token_reply(scope=""))

        done = await platform.resume_login(
            login(), resume_token=step.resume_token, account_id=CHANNEL
        )

        assert isinstance(done, Finished)
        assert len(done.connection.scopes) == 2

    async def test_a_channel_nobody_offered_is_refused(
        self, platform: YouTubePlatform
    ) -> None:
        with respx.mock() as network:
            step = await sign_in(platform, network)

        with pytest.raises(AuthError, match="was not one of the channels"):
            await platform.resume_login(
                login(), resume_token=step.resume_token, account_id="UCsomeoneelse"
            )

    async def test_a_resume_token_that_makes_no_sense_is_refused(
        self, platform: YouTubePlatform
    ) -> None:
        with pytest.raises(AuthError, match="could not be read"):
            await platform.resume_login(
                login(), resume_token="not-a-real-token", account_id=CHANNEL
            )

    async def test_a_resume_token_holding_the_wrong_shape_is_refused(
        self, platform: YouTubePlatform
    ) -> None:
        packed = base64.urlsafe_b64encode(b"[1, 2, 3]").decode().rstrip("=")

        with pytest.raises(AuthError, match="could not be read"):
            await platform.resume_login(
                login(), resume_token=packed, account_id=CHANNEL
            )


# ---------------------------------------------------------------------------
# Keeping the token working
# ---------------------------------------------------------------------------


class TestKeepingTheTokenWorking:
    async def test_it_asks_google_for_a_new_access_token(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            route = network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "access-two", "expires_in": 3599}
                )
            )

            token = await platform.refresh(account, APP)

        sent = form_of(route.calls.last.request)
        assert sent["grant_type"] == ["refresh_token"]
        assert sent["refresh_token"] == ["refresh-one"]
        assert sent["client_id"] == [APP.client_id]
        assert sent["client_secret"] == [APP.client_secret]
        assert token.access_token == "access-two"

    async def test_google_does_not_rotate_the_refresh_token(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        # Unlike Bluesky, Google sends no new refresh token, so the one we
        # already hold has to be carried across or the next renewal has
        # nothing to renew with.
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(
                    200, json={"access_token": "access-two", "expires_in": 3599}
                )
            )

            token = await platform.refresh(account, APP)

        assert token.refresh_token == "refresh-one"

    async def test_it_takes_a_new_refresh_token_when_google_sends_one(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "access_token": "access-two",
                        "refresh_token": "refresh-two",
                        "expires_in": 3599,
                    },
                )
            )

            token = await platform.refresh(account, APP)

        assert token.refresh_token == "refresh-two"

    async def test_a_connection_with_no_refresh_token_cannot_be_renewed(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        without = account.with_token(Token(access_token="access-one"))

        with pytest.raises(TokenExpiredError, match="no refresh token"):
            await platform.refresh(without, APP)

    async def test_a_refresh_token_google_refuses_means_signing_in_again(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(400, json={"error": "invalid_grant"})
            )

            with pytest.raises(TokenExpiredError, match="connect their channel"):
                await platform.refresh(account, APP)

    async def test_a_token_endpoint_that_answers_401_means_signing_in_again(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(
                    401, json=google_error("authError", code=401)
                )
            )

            with pytest.raises(TokenExpiredError, match="connect their channel"):
                await platform.refresh(account, APP)

    async def test_google_having_a_bad_day_is_not_a_dead_refresh_token(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        # Reading a 500 as "this person must sign in again" would have apps
        # throwing away connections that were perfectly good.
        with respx.mock() as network:
            network.post("https://oauth2.googleapis.com/token").mock(
                return_value=httpx.Response(500, text="oh dear")
            )

            with pytest.raises(PlatformError) as refused:
                await platform.refresh(account, APP)

        assert not isinstance(refused.value, TokenExpiredError)

    async def test_renewing_needs_your_apps_credentials(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        # Google signs a renewal with the client id and secret, so a refresh
        # that was handed none has nothing to send and says where to get them.
        with pytest.raises(ConfigError) as refused:
            await platform.refresh(account)

        said = str(refused.value)
        assert "renew a token" in said
        assert "Storage.save_app" in said


# ---------------------------------------------------------------------------
# What it allows
# ---------------------------------------------------------------------------


class TestWhatItAllows:
    async def test_it_counts_the_description_in_bytes(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        limits = await platform.limits(account)

        assert limits.max_text_length == 5000
        assert limits.text_counted_in is TextCount.UTF8_BYTES

    async def test_it_says_how_long_a_title_may_be(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        # The title lives in Post.options, so check_post cannot see it. The
        # number here is how an app shows somebody the cap before they type
        # past it, and it is the number publishing checks against.
        limits = await platform.limits(account)

        assert limits.max_title_length == 100

    async def test_one_video_and_no_pictures(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        limits = await platform.limits(account)

        assert limits.max_videos == 1
        assert limits.max_images is None
        assert limits.max_video_bytes == 256 * 1024**3

    async def test_asking_what_it_allows_costs_no_quota(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock(assert_all_called=False) as network:
            everything = network.route().mock(return_value=httpx.Response(200, json={}))

            await platform.limits(account)

        assert not everything.called


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def upload_routes(
    network: respx.Router,
    *,
    puts: list[httpx.Response]
    | Callable[[httpx.Request], httpx.Response]
    | None = None,
    location: str = SESSION,
) -> tuple[respx.Route, respx.Route]:
    """Answer both halves of a resumable upload."""
    start = network.post(UPLOAD_URL).mock(
        return_value=httpx.Response(200, headers={"Location": location})
    )
    finish = network.put(location.split("?")[0])
    if puts is None:
        finish.mock(return_value=httpx.Response(200, json=A_VIDEO_RESOURCE))
    else:
        finish.mock(side_effect=puts)
    return start, finish


class TestStartingAnUpload:
    async def test_it_tells_youtube_how_big_the_video_is_first(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            start, _ = upload_routes(network)

            await platform.publish(account, a_post())

        asked = start.calls.last.request
        assert asked.url.params["uploadType"] == "resumable"
        assert asked.url.params["part"] == "snippet,status"
        assert asked.headers["x-upload-content-length"] == "24"
        assert asked.headers["x-upload-content-type"] == "video/mp4"
        assert asked.headers["authorization"] == "Bearer access-one"

    async def test_the_words_of_the_post_become_the_description(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            start, _ = upload_routes(network)

            await platform.publish(account, a_post())

        sent = body_of(start.calls.last.request)
        assert sent["snippet"]["title"] == "Hello"
        assert sent["snippet"]["description"] == "A description"

    async def test_a_video_is_private_unless_you_say_otherwise(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            start, _ = upload_routes(network)

            await platform.publish(account, a_post())

        sent = body_of(start.calls.last.request)
        assert sent["status"]["privacyStatus"] == "private"

    async def test_it_sends_the_settings_you_gave_it(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            start, _ = upload_routes(network)

            await platform.publish(
                account,
                a_post(
                    privacy_status="public",
                    category_id="22",
                    tags=["python", "async"],
                    made_for_kids=True,
                    notify_subscribers=False,
                ),
            )

        asked = start.calls.last.request
        sent = body_of(asked)
        assert sent["status"]["privacyStatus"] == "public"
        assert sent["status"]["selfDeclaredMadeForKids"] is True
        assert sent["snippet"]["categoryId"] == "22"
        assert sent["snippet"]["tags"] == ["python", "async"]
        assert asked.url.params["notifySubscribers"] == "false"

    async def test_it_reads_a_video_off_disk_without_holding_it_all(
        self, platform: YouTubePlatform, account: Connection, tmp_path: Path
    ) -> None:
        file = tmp_path / "clip.mp4"
        file.write_bytes(b"v" * 40)
        post = Post(
            text="",
            media=(Media.from_file(file),),
            options={"title": "Hello", "made_for_kids": False},
        )

        with respx.mock() as network:
            start, finish = upload_routes(network)

            await platform.publish(account, post)

        assert start.calls.last.request.headers["x-upload-content-length"] == "40"
        assert finish.calls.last.request.content == b"v" * 40

    async def test_a_video_that_is_only_a_link_is_refused(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        post = Post(
            text="",
            media=(Media.from_url("https://example.com/clip.mp4"),),
            options={"title": "Hello", "made_for_kids": False},
        )

        with pytest.raises(InvalidPostError, match="will not fetch"):
            await platform.publish(account, post)

    async def test_a_reply_with_no_session_address_says_so(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.post(UPLOAD_URL).mock(return_value=httpx.Response(200, json={}))

            with pytest.raises(PlatformError, match="Location"):
                await platform.publish(account, a_post())

    async def test_the_session_address_is_used_exactly_as_given(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        elsewhere = "https://uploads.example/put/here?upload_id=up-2"

        with respx.mock() as network:
            _, finish = upload_routes(network, location=elsewhere)

            await platform.publish(account, a_post())

        assert str(finish.calls.last.request.url) == elsewhere


class TestSendingTheBytes:
    async def test_a_small_video_goes_in_one_piece(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            _, finish = upload_routes(network)

            result = await platform.publish(account, a_post())

        assert len(finish.calls) == 1
        sent = finish.calls.last.request
        assert sent.headers["content-range"] == "bytes 0-23/24"
        assert sent.content == b"v" * 24
        assert result.id == VIDEO_ID
        assert result.url == f"https://www.youtube.com/watch?v={VIDEO_ID}"

    async def test_a_big_video_goes_in_several_pieces(
        self, account: Connection
    ) -> None:
        platform = YouTubePlatform(retries=ONCE, chunk_bytes=CHUNK_MULTIPLE)
        total = CHUNK_MULTIPLE * 2 + 100
        post = Post(
            text="",
            media=(a_video(total),),
            options={"title": "Hello", "made_for_kids": False},
        )
        answers = [
            httpx.Response(308, headers={"Range": f"bytes=0-{CHUNK_MULTIPLE - 1}"}),
            httpx.Response(308, headers={"Range": f"bytes=0-{CHUNK_MULTIPLE * 2 - 1}"}),
            httpx.Response(200, json=A_VIDEO_RESOURCE),
        ]

        with respx.mock() as network:
            _, finish = upload_routes(network, puts=answers)

            result = await platform.publish(account, post)

        ranges = [call.request.headers["content-range"] for call in finish.calls]
        assert ranges == [
            f"bytes 0-{CHUNK_MULTIPLE - 1}/{total}",
            f"bytes {CHUNK_MULTIPLE}-{CHUNK_MULTIPLE * 2 - 1}/{total}",
            f"bytes {CHUNK_MULTIPLE * 2}-{total - 1}/{total}",
        ]
        assert [len(call.request.content) for call in finish.calls] == [
            CHUNK_MULTIPLE,
            CHUNK_MULTIPLE,
            100,
        ]
        assert result.state is PostState.PROCESSING

    async def test_it_asks_the_media_for_one_piece_at_a_time(
        self,
        account: Connection,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # A four gigabyte video must not become four gigabytes of memory, so
        # the upload goes through Media.piece and never through Media.read.
        platform = YouTubePlatform(retries=ONCE, chunk_bytes=CHUNK_MULTIPLE)
        total = CHUNK_MULTIPLE + 10
        file = tmp_path / "clip.mp4"
        file.write_bytes(b"v" * total)

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
            media=(Media.from_file(file),),
            options={"title": "Hello", "made_for_kids": False},
        )
        answers = [
            httpx.Response(308, headers={"Range": f"bytes=0-{CHUNK_MULTIPLE - 1}"}),
            httpx.Response(200, json=A_VIDEO_RESOURCE),
        ]

        with respx.mock() as network:
            _, finish = upload_routes(network, puts=answers)

            await platform.publish(account, post)

        assert asked == [(0, CHUNK_MULTIPLE), (CHUNK_MULTIPLE, 10)]
        assert [len(call.request.content) for call in finish.calls] == [
            CHUNK_MULTIPLE,
            10,
        ]

    async def test_it_carries_on_from_where_youtube_says_it_got_to(
        self, account: Connection
    ) -> None:
        # The whole point of the protocol: YouTube may have taken less than
        # we sent, and it says how much. Assuming the piece arrived whole
        # would leave a hole in the middle of the video.
        platform = YouTubePlatform(retries=ONCE, chunk_bytes=CHUNK_MULTIPLE)
        total = CHUNK_MULTIPLE * 2 + 100
        half = CHUNK_MULTIPLE // 2
        post = Post(
            text="",
            media=(a_video(total),),
            options={"title": "Hello", "made_for_kids": False},
        )
        answers = [
            httpx.Response(308, headers={"Range": f"bytes=0-{half - 1}"}),
            httpx.Response(308, headers={"Range": f"bytes=0-{CHUNK_MULTIPLE * 2 - 1}"}),
            httpx.Response(200, json=A_VIDEO_RESOURCE),
        ]

        with respx.mock() as network:
            _, finish = upload_routes(network, puts=answers)

            await platform.publish(account, post)

        ranges = [call.request.headers["content-range"] for call in finish.calls]
        assert ranges == [
            f"bytes 0-{CHUNK_MULTIPLE - 1}/{total}",
            f"bytes {half}-{half + CHUNK_MULTIPLE - 1}/{total}",
            f"bytes {CHUNK_MULTIPLE * 2}-{total - 1}/{total}",
        ]

    async def test_a_308_with_no_range_means_nothing_arrived(
        self, account: Connection
    ) -> None:
        platform = YouTubePlatform(retries=ONCE, resends_allowed=1)

        with respx.mock() as network:
            _, finish = upload_routes(network, puts=lambda request: httpx.Response(308))

            with pytest.raises(PlatformError, match="none of it arrived"):
                await platform.publish(account, a_post())

        # One try, then one resend, then we stop rather than loop forever.
        assert len(finish.calls) == 2

    async def test_youtube_taking_every_byte_and_saying_nothing_is_an_error(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            upload_routes(
                network,
                puts=lambda request: httpx.Response(
                    308, headers={"Range": "bytes=0-23"}
                ),
            )

            with pytest.raises(PlatformError, match="never answered with the video"):
                await platform.publish(account, a_post())

    async def test_a_range_it_cannot_read_is_treated_as_nothing_arrived(
        self, account: Connection
    ) -> None:
        platform = YouTubePlatform(retries=ONCE, resends_allowed=0)

        with respx.mock() as network:
            upload_routes(
                network,
                puts=lambda request: httpx.Response(
                    308, headers={"Range": "bytes=nonsense"}
                ),
            )

            with pytest.raises(PlatformError, match="none of it arrived"):
                await platform.publish(account, a_post())

    async def test_a_chunk_size_youtube_will_not_take_is_refused(self) -> None:
        with pytest.raises(ConfigError, match="256"):
            YouTubePlatform(chunk_bytes=1000)

    async def test_a_reply_with_no_video_id_says_so(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            upload_routes(network, puts=lambda request: httpx.Response(200, json={}))

            with pytest.raises(PlatformError, match="'id'"):
                await platform.publish(account, a_post())


class TestWhatAPostMustCarry:
    async def test_a_post_with_no_video_is_refused(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with pytest.raises(NotSupportedError) as refused:
            await platform.publish(account, Post(text="Just some words"))

        said = str(refused.value)
        assert "words on their own" in said
        assert "Community posts" in said

    async def test_a_post_with_no_title_is_refused(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        post = Post(text="", media=(a_video(),), options={"made_for_kids": False})

        with pytest.raises(InvalidPostError, match="title"):
            await platform.publish(account, post)

    async def test_an_empty_title_is_refused(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError, match="title"):
            await platform.publish(account, a_post(title=""))

    async def test_a_title_over_a_hundred_characters_is_refused(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError, match="100"):
            await platform.publish(account, a_post(title="x" * 101))

    async def test_the_title_is_checked_against_the_limit_it_declares(
        self,
        platform: YouTubePlatform,
        account: Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # One number, said once. A cap written out a second time inside the
        # check is a cap that drifts away from the one apps are shown.
        async def shorter(connection: Connection) -> Limits:
            return Limits(max_title_length=3)

        monkeypatch.setattr(platform, "limits", shorter)

        with pytest.raises(InvalidPostError, match="at most 3"):
            await platform.publish(account, a_post(title="far too long"))

    async def test_a_post_that_does_not_say_whether_it_is_for_children(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        post = Post(text="", media=(a_video(),), options={"title": "Hello"})

        with pytest.raises(InvalidPostError, match="made_for_kids"):
            await platform.publish(account, post)

    async def test_made_for_kids_has_to_be_true_or_false(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError, match="True or False"):
            await platform.publish(account, a_post(made_for_kids="yes"))

    async def test_notify_subscribers_has_to_be_true_or_false(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError, match="True or False"):
            await platform.publish(account, a_post(notify_subscribers="yes"))

    async def test_a_privacy_status_youtube_does_not_know_is_refused(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError, match="private, unlisted, public"):
            await platform.publish(account, a_post(privacy_status="secret"))

    async def test_tags_have_to_be_a_list_of_words(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError, match="tags"):
            await platform.publish(account, a_post(tags="python"))

    async def test_an_empty_tag_is_refused(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError, match="tags"):
            await platform.publish(account, a_post(tags=["python", ""]))

    async def test_a_category_id_has_to_be_some_text(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError, match="category_id"):
            await platform.publish(account, a_post(category_id=22))

    async def test_a_setting_youtube_has_never_heard_of_is_refused(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError, match="made_for_kids"):
            await platform.publish(account, a_post(shorts=True))

    async def test_asking_to_schedule_through_options_points_at_the_post(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with pytest.raises(InvalidPostError, match=re.escape("Post.publish_at")):
            await platform.publish(account, a_post(publish_at="2026-09-01T10:00:00Z"))

    async def test_a_description_over_the_limit_never_reaches_youtube(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        post = Post(
            text="x" * 5001,
            media=(a_video(),),
            options={"title": "Hello", "made_for_kids": False},
        )

        with respx.mock(assert_all_called=False) as network:
            everything = network.route().mock(return_value=httpx.Response(200, json={}))

            with pytest.raises(InvalidPostError, match="5000"):
                await platform.publish(account, post)

        assert not everything.called

    async def test_two_videos_on_one_post_are_refused(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        post = Post(
            text="",
            media=(a_video(), a_video(name="second.mp4")),
            options={"title": "Hello", "made_for_kids": False},
        )

        with pytest.raises(InvalidPostError, match="at most 1"):
            await platform.publish(account, post)

    async def test_a_picture_is_refused(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        post = Post(
            text="",
            media=(Media.from_bytes(b"png", filename="a.png"),),
            options={"title": "Hello", "made_for_kids": False},
        )

        with pytest.raises(NotSupportedError, match="pictures"):
            await platform.publish(account, post)


class TestSchedulingAVideo:
    async def test_a_scheduled_video_is_private_until_its_moment(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        when = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
        post = Post(
            text="",
            media=(a_video(),),
            publish_at=when,
            options={"title": "Hello", "made_for_kids": False},
        )

        with respx.mock() as network:
            start, _ = upload_routes(network)

            result = await platform.publish(account, post)

        sent = body_of(start.calls.last.request)
        assert sent["status"]["publishAt"] == when.isoformat()
        # YouTube's own rule: a video with a publishing time has to be
        # private until then, or it goes out at once.
        assert sent["status"]["privacyStatus"] == "private"
        assert result.state is PostState.SCHEDULED

    async def test_scheduling_a_public_video_is_refused(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        post = Post(
            text="",
            media=(a_video(),),
            publish_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
            options={
                "title": "Hello",
                "made_for_kids": False,
                "privacy_status": "public",
            },
        )

        with pytest.raises(InvalidPostError, match="private until"):
            await platform.publish(account, post)


# ---------------------------------------------------------------------------
# What happens after the upload
# ---------------------------------------------------------------------------


class TestCheckingWhatYouTubeDidNext:
    def test_it_offers_asking_how_a_post_is_getting_on(
        self, platform: YouTubePlatform
    ) -> None:
        # YouTube answers publish while it is still encoding, so Account
        # .check_state has to be able to find this.
        assert isinstance(platform, CanCheckState)

    async def test_a_freshly_uploaded_video_is_still_being_worked_on(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            upload_routes(network)

            result = await platform.publish(account, a_post())

        # YouTube keeps encoding long after it takes the bytes, so this is
        # not DONE.
        assert result.state is PostState.PROCESSING
        assert not result.is_done

    async def test_it_can_be_asked_whether_a_video_is_ready(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            route = network.get("https://www.googleapis.com/youtube/v3/videos").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "id": VIDEO_ID,
                                "status": {
                                    "uploadStatus": "processed",
                                    "privacyStatus": "public",
                                },
                            }
                        ]
                    },
                )
            )

            result = await platform.check_state(account, VIDEO_ID)

        asked = route.calls.last.request
        assert asked.url.params["id"] == VIDEO_ID
        assert asked.url.params["part"] == "status,processingDetails"
        assert result.state is PostState.DONE
        assert result.url == f"https://www.youtube.com/watch?v={VIDEO_ID}"

    @pytest.mark.parametrize(
        ("upload_status", "expected"),
        [
            ("uploaded", PostState.PROCESSING),
            ("processed", PostState.DONE),
            ("failed", PostState.FAILED),
            ("rejected", PostState.FAILED),
            ("deleted", PostState.FAILED),
            ("something-new", PostState.PROCESSING),
        ],
    )
    async def test_it_reads_every_state_youtube_reports(
        self,
        platform: YouTubePlatform,
        account: Connection,
        upload_status: str,
        expected: PostState,
    ) -> None:
        with respx.mock() as network:
            network.get("https://www.googleapis.com/youtube/v3/videos").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "items": [
                            {"id": VIDEO_ID, "status": {"uploadStatus": upload_status}}
                        ]
                    },
                )
            )

            result = await platform.check_state(account, VIDEO_ID)

        assert result.state is expected

    async def test_a_video_waiting_for_its_moment_is_scheduled(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.get("https://www.googleapis.com/youtube/v3/videos").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "id": VIDEO_ID,
                                "status": {
                                    "uploadStatus": "processed",
                                    "privacyStatus": "private",
                                    "publishAt": "2026-09-01T10:00:00Z",
                                },
                            }
                        ]
                    },
                )
            )

            result = await platform.check_state(account, VIDEO_ID)

        assert result.state is PostState.SCHEDULED

    async def test_a_video_youtube_has_never_heard_of(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.get("https://www.googleapis.com/youtube/v3/videos").mock(
                return_value=httpx.Response(200, json={"items": []})
            )

            with pytest.raises(NotFoundError, match=VIDEO_ID):
                await platform.check_state(account, VIDEO_ID)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def refusal(reason: str, *, code: int = 403) -> httpx.Response:
    """One of Google's refusals, in the shape it really sends."""
    return httpx.Response(code, json=google_error(reason, code=code))


class TestTurningGooglesErrorsIntoOurs:
    def test_running_out_of_quota_is_not_a_request_to_slow_down(self) -> None:
        error = youtube_errors(refusal("quotaExceeded"))

        assert isinstance(error, RateLimitError)
        said = str(error)
        assert "daily" in said
        assert "midnight Pacific" in said
        # Nothing to wait for: the usual advice of trying again shortly
        # would spend what is left of the day.
        assert error.retry_after is None

    def test_running_out_of_uploads_reads_the_same_way(self) -> None:
        error = youtube_errors(refusal("uploadLimitExceeded", code=400))

        assert isinstance(error, RateLimitError)
        assert "videos" in str(error)

    def test_a_permission_that_was_never_asked_for(self) -> None:
        error = youtube_errors(refusal("forbidden"))

        assert isinstance(error, NotAllowedError)

    def test_a_token_google_will_not_take(self) -> None:
        error = youtube_errors(refusal("authError", code=401))

        assert isinstance(error, AuthError)

    def test_a_video_that_is_not_there(self) -> None:
        error = youtube_errors(refusal("videoNotFound", code=404))

        assert isinstance(error, NotFoundError)

    def test_a_title_youtube_will_not_take(self) -> None:
        error = youtube_errors(refusal("invalidTitle", code=400))

        assert isinstance(error, InvalidPostError)
        assert "title" in str(error)

    def test_it_keeps_whatever_google_said(self) -> None:
        error = youtube_errors(
            httpx.Response(403, json=google_error("quotaExceeded", message="all gone"))
        )

        assert "all gone" in str(error)
        assert error.raw["error"]["code"] == 403

    def test_a_reason_we_have_no_name_for_falls_back_to_the_shared_mapping(
        self,
    ) -> None:
        error = youtube_errors(refusal("somethingNew", code=400))

        assert isinstance(error, PlatformError)
        assert error.status_code == 400

    def test_a_reply_that_is_not_googles_shape_at_all(self) -> None:
        error = youtube_errors(httpx.Response(500, text="<html>oh dear</html>"))

        assert isinstance(error, PlatformError)

    def test_it_reads_the_status_when_there_is_no_reason_list(self) -> None:
        error = youtube_errors(
            httpx.Response(403, json={"error": {"code": 403, "status": "forbidden"}})
        )

        assert isinstance(error, NotAllowedError)

    def test_it_looks_past_an_entry_with_no_reason_on_it(self) -> None:
        error = youtube_errors(
            httpx.Response(
                403,
                json={
                    "error": {
                        "code": 403,
                        "errors": [{"domain": "global"}, {"reason": "forbidden"}],
                    }
                },
            )
        )

        assert isinstance(error, NotAllowedError)

    def test_an_error_list_with_nothing_useful_in_it(self) -> None:
        error = youtube_errors(
            httpx.Response(400, json={"error": {"code": 400, "errors": ["odd"]}})
        )

        assert isinstance(error, PlatformError)

    async def test_a_refusal_during_an_upload_comes_back_as_ours(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.post(UPLOAD_URL).mock(return_value=refusal("quotaExceeded"))

            with pytest.raises(RateLimitError, match="midnight Pacific"):
                await platform.publish(account, a_post())


# ---------------------------------------------------------------------------
# Reading what has happened
# ---------------------------------------------------------------------------


class TestReadingComments:
    async def test_it_asks_about_this_channels_comments(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            route = network.get(
                "https://www.googleapis.com/youtube/v3/commentThreads"
            ).mock(return_value=httpx.Response(200, json={"items": [A_COMMENT_THREAD]}))

            updates = await platform.fetch_updates(account, None)

        asked = route.calls.last.request
        assert asked.url.params["allThreadsRelatedToChannelId"] == CHANNEL
        assert asked.url.params["part"] == "snippet"
        assert asked.url.params["order"] == "time"
        assert len(updates) == 1
        assert updates[0].kind is UpdateKind.COMMENT_CREATED
        assert updates[0].id == "thread-1"
        assert updates[0].created_at == datetime(2026, 8, 31, 10, 0, tzinfo=UTC)

    async def test_it_falls_back_to_the_account_id_for_the_channel(
        self, platform: YouTubePlatform
    ) -> None:
        older = Connection(
            id="conn-old",
            platform="youtube",
            host=None,
            account_id=CHANNEL,
            account_name="Ada's Channel",
            token=Token(access_token="access-one"),
        )

        with respx.mock() as network:
            route = network.get(
                "https://www.googleapis.com/youtube/v3/commentThreads"
            ).mock(return_value=httpx.Response(200, json={"items": []}))

            await platform.fetch_updates(older, None)

        assert (
            route.calls.last.request.url.params["allThreadsRelatedToChannelId"]
            == CHANNEL
        )

    async def test_it_hands_back_the_oldest_first(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        newer = json.loads(json.dumps(A_COMMENT_THREAD))
        newer["id"] = "thread-2"
        newer["snippet"]["topLevelComment"]["snippet"]["publishedAt"] = (
            "2026-08-31T11:00:00Z"
        )

        with respx.mock() as network:
            network.get("https://www.googleapis.com/youtube/v3/commentThreads").mock(
                return_value=httpx.Response(
                    200, json={"items": [newer, A_COMMENT_THREAD]}
                )
            )

            updates = await platform.fetch_updates(account, None)

        assert [update.id for update in updates] == ["thread-1", "thread-2"]

    async def test_it_leaves_out_anything_older_than_the_marker(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.get("https://www.googleapis.com/youtube/v3/commentThreads").mock(
                return_value=httpx.Response(200, json={"items": [A_COMMENT_THREAD]})
            )

            updates = await platform.fetch_updates(
                account, datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
            )

        assert updates == []

    async def test_a_comment_with_a_time_we_cannot_read_is_left_out(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        broken = json.loads(json.dumps(A_COMMENT_THREAD))
        broken["snippet"]["topLevelComment"]["snippet"]["publishedAt"] = "whenever"

        with respx.mock() as network:
            network.get("https://www.googleapis.com/youtube/v3/commentThreads").mock(
                return_value=httpx.Response(200, json={"items": [broken]})
            )

            updates = await platform.fetch_updates(account, None)

        assert updates == []

    async def test_a_thread_shaped_in_a_way_we_do_not_expect_is_left_out(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.get("https://www.googleapis.com/youtube/v3/commentThreads").mock(
                return_value=httpx.Response(
                    200, json={"items": [{"id": "thread-9", "snippet": "odd"}]}
                )
            )

            updates = await platform.fetch_updates(account, None)

        assert updates == []

    async def test_a_reply_with_nothing_in_it(
        self, platform: YouTubePlatform, account: Connection
    ) -> None:
        with respx.mock() as network:
            network.get("https://www.googleapis.com/youtube/v3/commentThreads").mock(
                return_value=httpx.Response(200, json={})
            )

            updates = await platform.fetch_updates(account, None)

        assert updates == []

    async def test_it_reads_as_many_at_a_time_as_you_asked_for(
        self, account: Connection
    ) -> None:
        platform = YouTubePlatform(retries=ONCE, updates_per_check=5)

        with respx.mock() as network:
            route = network.get(
                "https://www.googleapis.com/youtube/v3/commentThreads"
            ).mock(return_value=httpx.Response(200, json={"items": []}))

            await platform.fetch_updates(account, None)

        assert route.calls.last.request.url.params["maxResults"] == "5"


# ---------------------------------------------------------------------------
# The shared checks every platform has to pass
# ---------------------------------------------------------------------------


class TestYouTubeBehavesLikeTheOthers(PlatformChecks):
    def make_platform(self) -> Platform:
        return YouTubePlatform(transport=self.transport, retries=ONCE)

    def make_post(self, text: str) -> Post:
        # The checks that measure length need a post YouTube would look at,
        # and YouTube looks at nothing without a video, a title and an
        # answer about children. Without this they would be refused for one
        # of those instead, and the length would never be measured at all.
        return Post(
            text=text,
            media=(Media.from_bytes(b"a tiny video", filename="clip.mp4"),),
            options={"title": "A video", "made_for_kids": False},
        )

    def make_connection(self) -> Connection | None:
        return Connection(
            id=f"youtube:{CHANNEL}",
            platform="youtube",
            host=None,
            account_id=CHANNEL,
            account_name="Ada's Channel",
            token=Token(access_token="access-one", refresh_token="refresh-one"),
            extra={"channel_id": CHANNEL},
        )

    def make_transport(self) -> httpx.AsyncBaseTransport | None:
        return RecordingTransport(
            {"GET /youtube/v3/commentThreads": {"items": [A_COMMENT_THREAD]}}
        )
