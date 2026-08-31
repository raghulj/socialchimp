"""Tests for the Mastodon platform."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
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
    PlatformError,
    Post,
    PostState,
    RateLimitError,
    Token,
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
from socialchimp.platforms import mastodon as mastodon_module
from socialchimp.platforms.mastodon import MastodonPlatform, post_fingerprint

HOST = "mastodon.social"
OTHER = "fosstodon.org"
REDIRECT = "https://app.example/callback"

# One try and no waiting, so the error tests do not spend real seconds asleep.
ONCE = Retries(attempts=1)

# A server whose owner raised the post length to 5,000. The whole reason
# limits are looked up while running rather than written into the code.
BIG_INSTANCE: dict[str, Any] = {
    "configuration": {
        "statuses": {"max_characters": 5000, "max_media_attachments": 6},
        "media_attachments": {
            "image_size_limit": 16777216,
            "video_size_limit": 103809024,
        },
    }
}

# The registration `SocialChimp` looks up and puts on a login request.
APP = AppCredentials(
    platform="mastodon",
    host=HOST,
    client_id="client-id",
    client_secret="client-secret",
)

A_STATUS: dict[str, Any] = {
    "id": "110001",
    "url": f"https://{HOST}/@ada/110001",
    "content": "<p>Hello</p>",
}


@pytest.fixture
def platform() -> MastodonPlatform:
    """A platform that gives up after one try and never really sleeps."""
    return MastodonPlatform(retries=ONCE, media_wait_seconds=0.0)


@pytest.fixture
def account() -> Connection:
    """A connected account on mastodon.social."""
    return Connection(
        id="conn-1",
        platform="mastodon",
        host=HOST,
        account_id="1",
        account_name="@ada@mastodon.social",
        token=Token(access_token="user-token"),
        scopes=("read", "write"),
    )


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every pause instead of taking it."""
    recorded: list[float] = []

    async def remember(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(mastodon_module, "_wait", remember)
    return recorded


def login(
    *,
    state: str | None = None,
    scopes: tuple[str, ...] = (),
    host: str | None = HOST,
    app: AppCredentials | None = APP,
) -> LoginRequest:
    """A login request with the everyday values already filled in."""
    return LoginRequest(
        redirect_uri=REDIRECT, scopes=scopes, host=host, state=state, app=app
    )


async def start(platform: MastodonPlatform, request: LoginRequest) -> SendToNetwork:
    """Start a login, and insist Mastodon answered with an address to visit."""
    step = await platform.start_login(request)
    assert isinstance(step, SendToNetwork)
    return step


def stub_instance(
    network: respx.Router,
    *,
    host: str = HOST,
    reply: dict[str, Any] | None = None,
) -> respx.Route:
    """Answer the "what does this server allow?" question."""
    said = reply if reply is not None else BIG_INSTANCE
    return network.get(f"https://{host}/api/v2/instance").mock(
        return_value=httpx.Response(200, json=said)
    )


def form_of(request: httpx.Request) -> dict[str, list[str]]:
    """Read a sent form back into a dictionary."""
    return parse_qs(request.content.decode(), keep_blank_values=True)


def challenge_for(verifier: str) -> str:
    """Work out the code challenge a verifier should produce."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


class TestWhatItSaysItCanDo:
    def test_it_provides_everything_a_platform_must(
        self,
        platform: MastodonPlatform,
    ) -> None:
        checked: Platform = platform
        creates: CanCreateApp = platform
        deletes: CanDeletePosts = platform
        reads: CanReadUpdates = platform

        assert isinstance(checked, Platform)
        assert isinstance(creates, CanCreateApp)
        assert isinstance(deletes, CanDeletePosts)
        assert isinstance(reads, CanReadUpdates)
        assert platform.name == "mastodon"

    def test_it_lists_the_features_mastodon_really_has(
        self,
        platform: MastodonPlatform,
    ) -> None:
        for feature in (
            Feature.CREATE_APP,
            Feature.POST_TEXT,
            Feature.POST_IMAGE,
            Feature.POST_VIDEO,
            Feature.SCHEDULE,
            Feature.REPLY,
            Feature.DELETE_POST,
            Feature.READ_POSTS,
        ):
            assert feature in platform.features

    def test_it_does_not_claim_to_push_updates(
        self,
        platform: MastodonPlatform,
    ) -> None:
        # No per-account webhook exists, so we check on a timer instead.
        assert Feature.PUSH_UPDATES not in platform.features
        assert Feature.READ_STATS not in platform.features


class TestWhereTheServerIs:
    def test_the_address_is_the_server_the_account_is_on(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        # Every Mastodon server is its own network, so this is the one thing
        # that cannot be written into the platform once and for all.
        assert platform.api_base(account) == f"https://{HOST}"

    def test_a_connection_saved_without_a_server_says_so(
        self,
        platform: MastodonPlatform,
    ) -> None:
        homeless = Connection(
            id="conn-3",
            platform="mastodon",
            host=None,
            account_id="3",
            account_name="@nobody",
            token=Token(access_token="tok"),
        )

        with pytest.raises(ConfigError, match="which server"):
            platform.api_base(homeless)

    def test_the_headers_carry_the_accounts_own_token(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        assert platform.auth_headers(account) == {"Authorization": "Bearer user-token"}


class TestRegisteringAnApp:
    async def test_it_sends_the_fields_mastodon_asks_for(self) -> None:
        platform = MastodonPlatform(retries=ONCE, website="https://app.example")

        with respx.mock(base_url=f"https://{HOST}") as network:
            route = network.post("/api/v1/apps").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": "563419",
                        "client_id": "public-half",
                        "client_secret": "private-half",
                    },
                )
            )

            app = await platform.create_app(
                name="My App",
                redirect_uri=REDIRECT,
                host=HOST,
                scopes=("read", "write", "push"),
            )

        sent = form_of(route.calls.last.request)
        assert sent["client_name"] == ["My App"]
        assert sent["redirect_uris"] == [REDIRECT]
        assert sent["scopes"] == ["read write push"]
        assert sent["website"] == ["https://app.example"]

        assert app.platform == "mastodon"
        assert app.host == HOST
        assert app.client_id == "public-half"
        assert app.client_secret == "private-half"

    async def test_it_asks_for_read_and_write_when_you_say_nothing(
        self,
        platform: MastodonPlatform,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            route = network.post("/api/v1/apps").mock(
                return_value=httpx.Response(
                    200, json={"client_id": "a", "client_secret": "b"}
                )
            )

            await platform.create_app(name="My App", redirect_uri=REDIRECT, host=HOST)

        sent = form_of(route.calls.last.request)
        assert sent["scopes"] == ["read write"]
        # No website was set, so none is sent.
        assert "website" not in sent

    async def test_registering_on_two_servers_keeps_them_apart(
        self,
        platform: MastodonPlatform,
    ) -> None:
        with respx.mock() as network:
            here = network.post(f"https://{HOST}/api/v1/apps").mock(
                return_value=httpx.Response(
                    200, json={"client_id": "here-id", "client_secret": "here-secret"}
                )
            )
            there = network.post(f"https://{OTHER}/api/v1/apps").mock(
                return_value=httpx.Response(
                    200, json={"client_id": "there-id", "client_secret": "there-secret"}
                )
            )

            first = await platform.create_app(
                name="My App", redirect_uri=REDIRECT, host=HOST
            )
            second = await platform.create_app(
                name="My App", redirect_uri=REDIRECT, host=OTHER
            )

        assert here.called
        assert there.called
        # Each set of credentials is stamped with the server it works on, so
        # your storage keeps them apart rather than one overwriting the other.
        assert first.key == ("mastodon", HOST)
        assert second.key == ("mastodon", OTHER)
        assert first.client_id != second.client_id

    async def test_it_accepts_a_host_written_as_a_full_address(
        self,
        platform: MastodonPlatform,
    ) -> None:
        with respx.mock() as network:
            route = network.post(f"https://{HOST}/api/v1/apps").mock(
                return_value=httpx.Response(
                    200, json={"client_id": "a", "client_secret": "b"}
                )
            )

            app = await platform.create_app(
                name="My App",
                redirect_uri=REDIRECT,
                host="https://mastodon.social/",
            )

        assert route.called
        assert app.host == HOST

    async def test_it_asks_for_a_host_when_none_is_given(
        self,
        platform: MastodonPlatform,
    ) -> None:
        with pytest.raises(ConfigError, match="which server"):
            await platform.create_app(name="My App", redirect_uri=REDIRECT)

    async def test_it_says_so_when_the_reply_has_no_client_id(
        self,
        platform: MastodonPlatform,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            network.post("/api/v1/apps").mock(
                return_value=httpx.Response(200, json={"id": "563419"})
            )

            with pytest.raises(PlatformError, match="client_id"):
                await platform.create_app(
                    name="My App", redirect_uri=REDIRECT, host=HOST
                )


class TestStartingALogin:
    async def test_it_builds_the_sign_in_address(
        self,
        platform: MastodonPlatform,
    ) -> None:
        step = await start(platform, login(state="my-state"))

        parts = urlparse(step.url)
        query = parse_qs(parts.query)
        assert parts.scheme == "https"
        assert parts.netloc == HOST
        assert parts.path == "/oauth/authorize"
        assert query["response_type"] == ["code"]
        assert query["client_id"] == ["client-id"]
        assert query["redirect_uri"] == [REDIRECT]
        assert query["scope"] == ["read write"]
        assert query["state"] == ["my-state"]
        assert query["code_challenge_method"] == ["S256"]
        assert step.state == "my-state"

    async def test_only_the_hashed_secret_goes_to_mastodon(
        self,
        platform: MastodonPlatform,
    ) -> None:
        step = await start(platform, login())

        verifier = step.remember["code_verifier"]
        challenge = parse_qs(urlparse(step.url).query)["code_challenge"][0]

        # The secret itself is handed to your app to keep. Only its hash is
        # sent, so a code stolen from a browser's history is worth nothing.
        assert challenge == challenge_for(verifier)
        assert verifier not in step.url

    async def test_it_makes_a_state_when_you_do_not(
        self,
        platform: MastodonPlatform,
    ) -> None:
        step = await start(platform, login())

        assert step.state
        assert f"state={step.state}" in step.url

    async def test_it_asks_for_the_scopes_you_named(
        self,
        platform: MastodonPlatform,
    ) -> None:
        step = await start(platform, login(scopes=("read:statuses", "write:statuses")))

        query = parse_qs(urlparse(step.url).query)
        assert query["scope"] == ["read:statuses write:statuses"]

    async def test_it_says_so_when_the_request_carries_no_app(
        self,
        platform: MastodonPlatform,
    ) -> None:
        with pytest.raises(ConfigError, match="create_app"):
            await platform.start_login(login(app=None, host=OTHER))


class TestFinishingALogin:
    async def test_it_swaps_the_code_for_a_token_and_builds_a_connection(
        self,
        platform: MastodonPlatform,
    ) -> None:
        request = login(state="my-state")
        started = await start(platform, request)
        challenge = parse_qs(urlparse(started.url).query)["code_challenge"][0]

        with respx.mock(base_url=f"https://{HOST}") as network:
            token = network.post("/oauth/token").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "access_token": "user-token",
                        "token_type": "Bearer",
                        "scope": "read write",
                        "created_at": 1573979017,
                    },
                )
            )
            me = network.get("/api/v1/accounts/verify_credentials").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": "971724",
                        "acct": "ada",
                        "username": "ada",
                        "url": f"https://{HOST}/@ada",
                    },
                )
            )

            step = await platform.finish_login(
                request,
                {"code": "the-code", "state": "my-state"},
                started.remember,
            )

        sent = form_of(token.calls.last.request)
        assert sent["grant_type"] == ["authorization_code"]
        assert sent["code"] == ["the-code"]
        assert sent["client_id"] == ["client-id"]
        assert sent["client_secret"] == ["client-secret"]
        assert sent["redirect_uri"] == [REDIRECT]
        assert sent["scope"] == ["read write"]
        # The secret we send now must hash to the challenge we sent earlier.
        # That is the whole point of it: it proves the code came back to the
        # same place that asked for it.
        assert challenge_for(sent["code_verifier"][0]) == challenge

        assert me.calls.last.request.headers["authorization"] == "Bearer user-token"

        connection = step.connection
        assert connection.platform == "mastodon"
        assert connection.host == HOST
        assert connection.account_id == "971724"
        assert connection.account_name == "@ada@mastodon.social"
        assert connection.scopes == ("read", "write")
        assert connection.token.access_token == "user-token"
        assert connection.token.refresh_token is None
        assert connection.token.expires_at is None
        assert connection.extra["profile_url"] == f"https://{HOST}/@ada"

    async def test_the_secret_survives_a_trip_through_your_app(
        self,
        platform: MastodonPlatform,
    ) -> None:
        # start_login and finish_login often run in different web workers.
        # Whatever your app kept and handed back is what we use, so a
        # separate instance finishes a login the first one started.
        started = await start(MastodonPlatform(retries=ONCE), login())

        with respx.mock(base_url=f"https://{HOST}") as network:
            token = network.post("/oauth/token").mock(
                return_value=httpx.Response(200, json={"access_token": "user-token"})
            )
            network.get("/api/v1/accounts/verify_credentials").mock(
                return_value=httpx.Response(200, json={"id": "1", "acct": "ada"})
            )

            await platform.finish_login(
                login(),
                {"code": "the-code", "state": started.state},
                started.remember,
            )

        assert form_of(token.calls.last.request)["code_verifier"] == [
            started.remember["code_verifier"]
        ]

    async def test_it_will_not_finish_a_login_whose_secret_never_came_back(
        self,
        platform: MastodonPlatform,
    ) -> None:
        with pytest.raises(AuthError, match="remember"):
            await platform.finish_login(login(), {"code": "the-code"})

    async def test_it_falls_back_to_the_scopes_we_asked_for(
        self,
        platform: MastodonPlatform,
    ) -> None:
        request = login(scopes=("read:statuses",))
        started = await start(platform, request)

        with respx.mock(base_url=f"https://{HOST}") as network:
            network.post("/oauth/token").mock(
                return_value=httpx.Response(200, json={"access_token": "user-token"})
            )
            network.get("/api/v1/accounts/verify_credentials").mock(
                return_value=httpx.Response(200, json={"id": "1", "acct": "ada"})
            )

            step = await platform.finish_login(
                request, {"code": "the-code"}, started.remember
            )

        assert step.connection.scopes == ("read:statuses",)

    async def test_it_refuses_a_state_that_does_not_match(
        self,
        platform: MastodonPlatform,
    ) -> None:
        with pytest.raises(AuthError, match="did not match"):
            await platform.finish_login(
                login(state="mine"),
                {"code": "the-code", "state": "someone-elses"},
                {"code_verifier": "secret"},
            )

    async def test_it_reports_a_person_who_said_no(
        self,
        platform: MastodonPlatform,
    ) -> None:
        with pytest.raises(AuthError, match="access_denied"):
            await platform.finish_login(
                login(),
                {"error": "access_denied", "error_description": "They said no."},
                {"code_verifier": "secret"},
            )

    async def test_it_says_so_when_the_code_is_missing(
        self,
        platform: MastodonPlatform,
    ) -> None:
        with pytest.raises(AuthError, match="no code"):
            await platform.finish_login(login(), {}, {"code_verifier": "secret"})


class TestTokensThatNeverExpire:
    async def test_it_hands_back_the_same_token_untouched(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        # Mastodon access tokens live until the person revokes them. There is
        # nothing to call, so nothing is called.
        with respx.mock(assert_all_called=False) as network:
            catch_all = network.route().mock(return_value=httpx.Response(500, json={}))
            token = await platform.refresh(account)

        assert token is account.token
        assert token.expires_at is None
        assert not catch_all.called

    async def test_it_takes_your_apps_credentials_and_ignores_them(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        # Google and Meta sign a renewal with them. Mastodon has no renewal
        # to sign, so they arrive and nothing happens.
        with respx.mock(assert_all_called=False) as network:
            catch_all = network.route().mock(return_value=httpx.Response(500, json={}))
            token = await platform.refresh(account, APP)

        assert token is account.token
        assert not catch_all.called


class TestLimits:
    async def test_it_reads_what_this_server_allows(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            route = stub_instance(network)
            found = await platform.limits(account)

        assert route.calls.last.request.headers["authorization"] == "Bearer user-token"
        assert found.max_text_length == 5000
        assert found.max_images == 6
        assert found.max_videos == 1
        assert found.max_image_bytes == 16777216
        assert found.max_video_bytes == 103809024
        # Mastodon really does count characters, so a post of 5,000 family
        # emoji is 35,000 characters and too long for this server.
        assert found.text_counted_in is TextCount.CHARACTERS

    async def test_it_uses_mastodons_own_defaults_when_a_server_says_nothing(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            stub_instance(network, reply={})
            found = await platform.limits(account)

        assert found.max_text_length == 500
        assert found.max_images == 4
        assert found.max_image_bytes is None
        assert found.max_video_bytes is None

    async def test_it_ignores_numbers_that_are_not_numbers(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            stub_instance(
                network,
                reply={"configuration": {"statuses": {"max_characters": "lots"}}},
            )
            found = await platform.limits(account)

        assert found.max_text_length == 500

    async def test_it_asks_each_server_once_and_remembers(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with respx.mock() as network:
            here = stub_instance(network, host=HOST)
            there = stub_instance(
                network,
                host=OTHER,
                reply={"configuration": {"statuses": {"max_characters": 1000}}},
            )

            somewhere_else = Connection(
                id="conn-2",
                platform="mastodon",
                host=OTHER,
                account_id="2",
                account_name="@bob@fosstodon.org",
                token=Token(access_token="other-token"),
            )

            first = await platform.limits(account)
            again = await platform.limits(account)
            elsewhere = await platform.limits(somewhere_else)

        assert here.call_count == 1
        assert there.call_count == 1
        assert first == again
        # Two servers, two answers. One is never used for the other.
        assert first.max_text_length == 5000
        assert elsewhere.max_text_length == 1000

    async def test_it_asks_again_once_what_it_remembered_is_stale(
        self,
        account: Connection,
    ) -> None:
        platform = MastodonPlatform(retries=ONCE, limits_cache_seconds=0.0)

        with respx.mock() as network:
            route = stub_instance(network)
            await platform.limits(account)
            await platform.limits(account)

        assert route.call_count == 2

    async def test_it_asks_which_server_a_connection_is_on(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        homeless = Connection(
            id="conn-3",
            platform="mastodon",
            host=None,
            account_id="3",
            account_name="@nobody",
            token=Token(access_token="tok"),
        )

        with pytest.raises(ConfigError, match="which server"):
            await platform.limits(homeless)


class TestPublishing:
    async def test_it_posts_text(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            stub_instance(network)
            route = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=A_STATUS)
            )

            result = await platform.publish(account, Post(text="Hello"))

        sent = route.calls.last.request
        assert sent.headers["authorization"] == "Bearer user-token"
        assert sent.headers["idempotency-key"] == post_fingerprint(Post(text="Hello"))
        assert form_of(sent)["status"] == ["Hello"]
        assert "media_ids[]" not in form_of(sent)

        assert result.id == "110001"
        assert result.url == f"https://{HOST}/@ada/110001"
        assert result.state is PostState.DONE
        assert result.is_done
        assert result.raw == A_STATUS

    async def test_it_posts_a_picture(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        picture = Media.from_bytes(
            b"png-bytes", filename="cat.png", alt_text="A cat asleep."
        )

        with respx.mock(base_url=f"https://{HOST}") as network:
            stub_instance(network)
            upload = network.post("/api/v2/media").mock(
                return_value=httpx.Response(200, json={"id": "m1", "url": "https://x"})
            )
            statuses = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=A_STATUS)
            )

            await platform.publish(account, Post(text="Look", media=(picture,)))

        body = upload.calls.last.request.content
        assert b'name="file"; filename="cat.png"' in body
        assert b"image/png" in body
        assert b"A cat asleep." in body

        assert form_of(statuses.calls.last.request)["media_ids[]"] == ["m1"]

    async def test_it_waits_for_a_video_that_is_still_being_processed(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        # This one takes the real pause, which the fixture sets to nothing.
        clip = Media.from_bytes(b"mp4-bytes", filename="clip.mp4")

        with respx.mock(base_url=f"https://{HOST}") as network:
            stub_instance(network)
            network.post("/api/v2/media").mock(
                return_value=httpx.Response(202, json={"id": "m9", "url": None})
            )
            checks = network.get("/api/v1/media/m9")
            checks.side_effect = [
                httpx.Response(206, json={"id": "m9", "url": None}),
                httpx.Response(200, json={"id": "m9", "url": "https://x/clip.mp4"}),
            ]
            statuses = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=A_STATUS)
            )

            await platform.publish(account, Post(text="Watch", media=(clip,)))

        assert checks.call_count == 2
        assert form_of(statuses.calls.last.request)["media_ids[]"] == ["m9"]

    async def test_it_gives_up_on_a_video_that_never_finishes(
        self,
        account: Connection,
        waits: list[float],
    ) -> None:
        platform = MastodonPlatform(
            retries=ONCE, media_checks=2, media_wait_seconds=0.25
        )
        clip = Media.from_bytes(b"mp4-bytes", filename="clip.mp4")

        with respx.mock(base_url=f"https://{HOST}") as network:
            stub_instance(network)
            network.post("/api/v2/media").mock(
                return_value=httpx.Response(202, json={"id": "m9"})
            )
            checks = network.get("/api/v1/media/m9").mock(
                return_value=httpx.Response(206, json={"id": "m9"})
            )

            with pytest.raises(PlatformError, match="still working on"):
                await platform.publish(account, Post(text="Watch", media=(clip,)))

        assert checks.call_count == 2
        # It waits between checks rather than hammering the server.
        assert waits == [0.25, 0.25]

    async def test_it_will_not_send_a_file_it_only_has_a_link_to(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        remote = Media.from_url("https://pics.example/cat.png")

        with respx.mock(base_url=f"https://{HOST}") as network:
            stub_instance(network)

            with pytest.raises(InvalidPostError, match="Download the file first"):
                await platform.publish(account, Post(text="Look", media=(remote,)))

    async def test_it_replies_to_another_post(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            stub_instance(network)
            route = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=A_STATUS)
            )

            await platform.publish(account, Post(text="Agreed", reply_to="109999"))

        assert form_of(route.calls.last.request)["in_reply_to_id"] == ["109999"]

    async def test_it_schedules_a_post_for_later(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        later = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

        with respx.mock(base_url=f"https://{HOST}") as network:
            stub_instance(network)
            route = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(
                    200,
                    json={"id": "sched-1", "scheduled_at": "2026-09-01T12:00:00.000Z"},
                )
            )

            result = await platform.publish(
                account, Post(text="Later", publish_at=later)
            )

        assert form_of(route.calls.last.request)["scheduled_at"] == [
            "2026-09-01T12:00:00+00:00"
        ]
        assert result.id == "sched-1"
        assert result.url is None
        assert result.state is PostState.SCHEDULED
        assert not result.is_done

    async def test_it_keeps_a_url_only_when_the_reply_has_one(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            stub_instance(network)
            network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json={"id": "110002", "url": None})
            )

            result = await platform.publish(account, Post(text="Hello"))

        assert result.url is None

    async def test_it_refuses_a_post_longer_than_the_server_allows(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            stub_instance(
                network,
                reply={"configuration": {"statuses": {"max_characters": 10}}},
            )

            with pytest.raises(InvalidPostError, match="at most 10"):
                await platform.publish(account, Post(text="x" * 11))

    async def test_it_says_so_when_the_reply_has_no_post_id(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            stub_instance(network)
            network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json={"url": "https://x"})
            )

            with pytest.raises(PlatformError, match="id"):
                await platform.publish(account, Post(text="Hello"))


class TestPostOptions:
    @pytest.mark.parametrize(
        ("key", "value", "sent"),
        [
            ("visibility", "public", "public"),
            ("visibility", "unlisted", "unlisted"),
            ("visibility", "private", "private"),
            ("visibility", "direct", "direct"),
            ("spoiler_text", "Spoilers ahead", "Spoilers ahead"),
            ("sensitive", True, "true"),
            ("sensitive", False, "false"),
            ("language", "en", "en"),
        ],
    )
    async def test_it_passes_an_option_mastodon_understands(
        self,
        platform: MastodonPlatform,
        account: Connection,
        key: str,
        value: object,
        sent: str,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            stub_instance(network)
            route = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=A_STATUS)
            )

            await platform.publish(account, Post(text="Hi", options={key: value}))

        assert form_of(route.calls.last.request)[key] == [sent]

    async def test_it_turns_away_an_option_it_does_not_know(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(InvalidPostError) as complaint:
            await platform.publish(account, Post(text="Hi", options={"board_id": "12"}))

        message = str(complaint.value)
        assert "board_id" in message
        # It lists everything that is accepted, so the fix is obvious.
        for accepted in ("visibility", "spoiler_text", "sensitive", "language"):
            assert accepted in message

    async def test_it_lists_the_visibilities_it_accepts(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(InvalidPostError) as complaint:
            await platform.publish(
                account, Post(text="Hi", options={"visibility": "shouted"})
            )

        message = str(complaint.value)
        assert "shouted" in message
        for accepted in ("public", "unlisted", "private", "direct"):
            assert accepted in message

    async def test_it_wants_sensitive_to_be_yes_or_no(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with pytest.raises(InvalidPostError, match="True or False"):
            await platform.publish(
                account, Post(text="Hi", options={"sensitive": "yes"})
            )

    @pytest.mark.parametrize("value", [5, ""])
    async def test_it_wants_words_where_words_belong(
        self,
        platform: MastodonPlatform,
        account: Connection,
        value: object,
    ) -> None:
        with pytest.raises(InvalidPostError, match="has to be some text"):
            await platform.publish(
                account, Post(text="Hi", options={"spoiler_text": value})
            )


class TestNotPostingTwice:
    def test_the_same_post_always_gets_the_same_key(self) -> None:
        first = Post(text="Hello", options={"visibility": "unlisted"})
        again = Post(text="Hello", options={"visibility": "unlisted"})

        assert post_fingerprint(first) == post_fingerprint(again)

    def test_a_different_post_gets_a_different_key(self) -> None:
        assert post_fingerprint(Post(text="Hello")) != post_fingerprint(
            Post(text="Goodbye")
        )

    def test_every_part_of_a_post_counts(self) -> None:
        plain = Post(text="Hello")
        keys = {
            post_fingerprint(plain),
            post_fingerprint(Post(text="Hello", reply_to="1")),
            post_fingerprint(
                Post(text="Hello", publish_at=datetime(2026, 9, 1, tzinfo=UTC))
            ),
            post_fingerprint(Post(text="Hello", options={"visibility": "private"})),
            post_fingerprint(
                Post(
                    text="Hello",
                    media=(Media.from_bytes(b"x", filename="cat.png"),),
                )
            ),
        }

        assert len(keys) == 5


class TestDeleting:
    async def test_it_removes_a_post(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            route = network.delete("/api/v1/statuses/110001").mock(
                return_value=httpx.Response(200, json=A_STATUS)
            )

            await platform.delete_post(account, "110001")

        assert route.calls.last.request.headers["authorization"] == "Bearer user-token"


class TestReadingUpdates:
    async def test_it_turns_notifications_into_updates(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            route = network.get("/api/v1/notifications").mock(
                return_value=httpx.Response(
                    200,
                    json=[
                        {
                            "id": "4",
                            "type": "follow",
                            "created_at": "2026-08-31T10:03:00.000Z",
                            "account": {"acct": "dee"},
                        },
                        {
                            "id": "3",
                            "type": "reblog",
                            "created_at": "2026-08-31T10:02:00.000Z",
                        },
                        {
                            "id": "2",
                            "type": "favourite",
                            "created_at": "2026-08-31T10:01:00.000Z",
                        },
                        {
                            "id": "1",
                            "type": "mention",
                            "created_at": "2026-08-31T10:00:00.000Z",
                        },
                    ],
                )
            )

            updates = await platform.fetch_updates(account, None)

        sent = route.calls.last.request
        assert sent.headers["authorization"] == "Bearer user-token"
        assert sorted(sent.url.params.get_list("types[]")) == [
            "favourite",
            "follow",
            "mention",
            "reblog",
        ]

        # Oldest first, which is the opposite of the order Mastodon sends.
        assert [update.id for update in updates] == ["1", "2", "3", "4"]
        assert [update.kind for update in updates] == [
            UpdateKind.MENTION,
            UpdateKind.REACTION_ADDED,
            UpdateKind.REACTION_ADDED,
            UpdateKind.UNKNOWN,
        ]
        # A follow has no name of ours, so Mastodon's own word is kept.
        assert updates[-1].kind_name == "follow"
        assert updates[0].platform == "mastodon"
        assert updates[0].connection_id == "conn-1"
        assert updates[0].created_at == datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
        assert updates[0].raw["type"] == "mention"

    async def test_it_drops_anything_older_than_the_marker(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            network.get("/api/v1/notifications").mock(
                return_value=httpx.Response(
                    200,
                    json=[
                        {
                            "id": "2",
                            "type": "mention",
                            "created_at": "2026-08-31T10:05:00.000Z",
                        },
                        {
                            "id": "1",
                            "type": "mention",
                            "created_at": "2026-08-31T09:00:00.000Z",
                        },
                    ],
                )
            )

            updates = await platform.fetch_updates(
                account, datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
            )

        assert [update.id for update in updates] == ["2"]

    async def test_it_asks_for_the_page_size_you_set(
        self,
        account: Connection,
    ) -> None:
        platform = MastodonPlatform(retries=ONCE, updates_per_check=5)

        with respx.mock(base_url=f"https://{HOST}") as network:
            route = network.get("/api/v1/notifications").mock(
                return_value=httpx.Response(200, json=[])
            )

            assert await platform.fetch_updates(account, None) == []

        assert route.calls.last.request.url.params["limit"] == "5"

    async def test_it_shrugs_off_a_reply_it_cannot_make_sense_of(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            network.get("/api/v1/notifications").mock(
                return_value=httpx.Response(
                    200,
                    json=[
                        "not a notification",
                        {"id": "9", "type": "mention", "created_at": "whenever"},
                        {
                            "id": "8",
                            "created_at": "2026-08-31T10:00:00",
                        },
                    ],
                )
            )

            updates = await platform.fetch_updates(account, None)

        # Only the one with a readable time survives, and a missing type is
        # simply something we have no name for.
        assert [update.id for update in updates] == ["8"]
        assert updates[0].kind is UpdateKind.UNKNOWN
        assert updates[0].created_at == datetime(2026, 8, 31, 10, 0, tzinfo=UTC)

    async def test_a_reply_that_is_not_a_list_gives_nothing(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            network.get("/api/v1/notifications").mock(
                return_value=httpx.Response(200, json={"error": "odd"})
            )

            assert await platform.fetch_updates(account, None) == []


class TestWhenMastodonSaysNo:
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
        platform: MastodonPlatform,
        account: Connection,
        status: int,
        expected: type[Exception],
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            network.delete("/api/v1/statuses/110001").mock(
                return_value=httpx.Response(status, json={"error": "Nope"})
            )

            with pytest.raises(expected, match="Nope"):
                await platform.delete_post(account, "110001")

    async def test_it_passes_on_how_long_to_wait(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            network.delete("/api/v1/statuses/110001").mock(
                return_value=httpx.Response(
                    429, headers={"Retry-After": "42"}, json={"error": "Slow down"}
                )
            )

            with pytest.raises(RateLimitError) as complaint:
                await platform.delete_post(account, "110001")

        assert complaint.value.retry_after == 42.0

    @pytest.mark.parametrize(
        ("body", "wanted"),
        [
            ({"error": "Text character limit of 500 exceeded"}, "limit of 500"),
            ({}, "would not accept this post"),
        ],
    )
    async def test_a_rejected_post_is_a_post_problem_not_a_mystery(
        self,
        platform: MastodonPlatform,
        account: Connection,
        body: dict[str, Any],
        wanted: str,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            stub_instance(network)
            network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(422, json=body)
            )

            with pytest.raises(InvalidPostError, match=wanted):
                await platform.publish(account, Post(text="Hello"))
