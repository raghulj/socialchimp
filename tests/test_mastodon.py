"""Tests for the Mastodon platform."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
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
    Conversation,
    Feature,
    InvalidPostError,
    Like,
    LikeResult,
    LinkKind,
    Media,
    Message,
    MissingPermissionError,
    NotAllowedError,
    NotFoundError,
    Page,
    Person,
    PlatformError,
    Post,
    PostDetails,
    PostGoneError,
    PostState,
    PostStats,
    RateLimitError,
    Thread,
    Token,
    UpdateKind,
    Visibility,
)
from socialchimp.features import TextCount
from socialchimp.http import Retries
from socialchimp.platform import (
    CanCreateApp,
    CanDeletePosts,
    CanLike,
    CanMessage,
    CanReadLikes,
    CanReadPost,
    CanReadStats,
    CanReadThread,
    CanReadUpdates,
    CanReadUpdatesAfter,
    CanReply,
    CanStartConversations,
    LoginRequest,
    Platform,
    SendToNetwork,
)
from socialchimp.platforms import mastodon as mastodon_module
from socialchimp.platforms.mastodon import MastodonPlatform, post_fingerprint

HOST = "mastodon.social"
OTHER = "fosstodon.org"
REDIRECT = "https://app.example/callback"

# The server and account the social-inbox fixtures were built around - see
# tests/fixtures/mastodon/README.md.
SOCIAL_HOST = "social.example"
FRIDGEDOOR_ID = "113140000000000001"

FIXTURES = Path(__file__).parent / "fixtures" / "mastodon"


def fixture(name: str) -> Any:  # noqa: ANN401 - a status, a list, or an error map
    """Load one of the real-shape Mastodon fixtures as parsed JSON."""
    return json.loads((FIXTURES / name).read_text())


def fixture_text(name: str) -> str:
    """Load one of the fixtures as plain text - used for the `Link` header."""
    return (FIXTURES / name).read_text().strip()


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

# The same post once people have replied to it, favourited it and boosted it.
# Those three counts are every number Mastodon keeps about a status.
A_BUSY_STATUS: dict[str, Any] = {
    **A_STATUS,
    "replies_count": 3,
    "favourites_count": 12,
    "reblogs_count": 5,
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
def fridgedoor() -> Connection:
    """The account the social-inbox fixtures were built around.

    `fridgedoor@social.example` - see tests/fixtures/mastodon/README.md.
    """
    return Connection(
        id="fridgedoor-conn",
        platform="mastodon",
        host=SOCIAL_HOST,
        account_id=FRIDGEDOOR_ID,
        account_name="@fridgedoor@social.example",
        token=Token(access_token="fridgedoor-token"),
        scopes=("read", "write", "push"),
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
        counts: CanReadStats = platform
        reads_post: CanReadPost = platform
        reads_thread: CanReadThread = platform
        replies: CanReply = platform
        likes: CanLike = platform
        reads_likes: CanReadLikes = platform
        reads_after: CanReadUpdatesAfter = platform
        messages: CanMessage = platform
        starts: CanStartConversations = platform

        assert isinstance(checked, Platform)
        assert isinstance(creates, CanCreateApp)
        assert isinstance(deletes, CanDeletePosts)
        assert isinstance(reads, CanReadUpdates)
        assert isinstance(counts, CanReadStats)
        assert isinstance(reads_post, CanReadPost)
        assert isinstance(reads_thread, CanReadThread)
        assert isinstance(replies, CanReply)
        assert isinstance(likes, CanLike)
        assert isinstance(reads_likes, CanReadLikes)
        assert isinstance(reads_after, CanReadUpdatesAfter)
        assert isinstance(messages, CanMessage)
        assert isinstance(starts, CanStartConversations)
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
            Feature.READ_STATS,
            Feature.READ_POST,
            Feature.READ_THREAD,
            Feature.REPLY_TO_COMMENTS,
            Feature.LIKE,
            Feature.READ_LIKES,
            Feature.READ_UPDATES_AFTER,
            Feature.MESSAGES,
            Feature.START_CONVERSATIONS,
        ):
            assert feature in platform.features

    def test_it_does_not_claim_to_push_updates(
        self,
        platform: MastodonPlatform,
    ) -> None:
        # No per-account webhook exists yet, so we check on a timer instead -
        # Web Push comes in a later release, along with the reserved
        # Feature.SUBSCRIBE_UPDATES (see docs/social-inbox-contract.md).
        assert Feature.PUSH_UPDATES not in platform.features


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

    async def test_it_asks_for_read_write_and_push_when_you_say_nothing(
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
        assert sent["scopes"] == ["read write push"]
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
        assert query["scope"] == ["read write push"]
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
        assert sent["scope"] == ["read write push"]
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
        # CHANGE (0.8.0): a reblog used to arrive as REACTION_ADDED and a
        # follow as UNKNOWN. Both have their own kind now.
        assert [update.kind for update in updates] == [
            UpdateKind.MENTION,
            UpdateKind.REACTION_ADDED,
            UpdateKind.REPOST_ADDED,
            UpdateKind.FOLLOWED,
        ]
        assert updates[-1].kind_name == "followed"
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


class TestReadingAPostsNumbers:
    async def test_it_reads_the_numbers_back(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            route = network.get("/api/v1/statuses/110001").mock(
                return_value=httpx.Response(200, json=A_BUSY_STATUS)
            )

            found = await platform.read_stats(account, "110001")

        assert route.calls.last.request.headers["authorization"] == "Bearer user-token"
        assert found == PostStats(
            id="110001",
            comments=3,
            likes=12,
            shares=5,
            raw=A_BUSY_STATUS,
        )

    async def test_a_post_nobody_has_touched_reads_as_zero(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        # Zero is a real answer, and has to stay one. Reading it as "we do
        # not know" would make a brand new post and a server that does not
        # count look the same.
        quiet = {**A_STATUS, "replies_count": 0, "favourites_count": 0}

        with respx.mock(base_url=f"https://{HOST}") as network:
            network.get("/api/v1/statuses/110001").mock(
                return_value=httpx.Response(200, json=quiet)
            )

            found = await platform.read_stats(account, "110001")

        assert found.comments == 0
        assert found.likes == 0
        # This one really was left out, and that is not the same as zero.
        assert found.shares is None

    async def test_a_number_a_server_leaves_out_is_not_guessed_at(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            network.get("/api/v1/statuses/110001").mock(
                return_value=httpx.Response(
                    200, json={**A_STATUS, "favourites_count": "lots"}
                )
            )

            found = await platform.read_stats(account, "110001")

        assert found.comments is None
        assert found.likes is None
        assert found.shares is None

    async def test_a_post_that_is_gone_says_so(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        # Mastodon answers 404 once a status is deleted, which is the same
        # answer it gives for one that never existed.
        with respx.mock(base_url=f"https://{HOST}") as network:
            network.get("/api/v1/statuses/110001").mock(
                return_value=httpx.Response(404, json={"error": "Record not found"})
            )

            with pytest.raises(NotFoundError, match="no such post"):
                await platform.read_stats(account, "110001")

    async def test_a_token_that_stopped_working_says_so(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        # Mastodon tokens do not expire on their own, but a person can
        # revoke one, and then every request answers 401.
        with respx.mock(base_url=f"https://{HOST}") as network:
            network.get("/api/v1/statuses/110001").mock(
                return_value=httpx.Response(401, json={"error": "The access token"})
            )

            with pytest.raises(AuthError, match="connect their account again"):
                await platform.read_stats(account, "110001")

    async def test_it_passes_on_how_long_to_wait(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            network.get("/api/v1/statuses/110001").mock(
                return_value=httpx.Response(
                    429, headers={"Retry-After": "42"}, json={"error": "Slow down"}
                )
            )

            with pytest.raises(RateLimitError) as complaint:
                await platform.read_stats(account, "110001")

        assert complaint.value.retry_after == 42.0

    async def test_it_says_so_when_the_reply_has_no_post_id(
        self,
        platform: MastodonPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{HOST}") as network:
            network.get("/api/v1/statuses/110001").mock(
                return_value=httpx.Response(200, json={"favourites_count": 1})
            )

            with pytest.raises(PlatformError, match="id"):
                await platform.read_stats(account, "110001")

    async def test_it_asks_which_server_a_connection_is_on(
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
            await platform.read_stats(homeless, "110001")


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


class TestReadingAPost:
    async def test_it_converts_a_public_posts_html_to_plain_text_and_links(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        status = fixture("status_own_post.json")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get(f"/api/v1/statuses/{status['id']}").mock(
                return_value=httpx.Response(200, json=status)
            )

            post = await platform.read_post(fridgedoor, status["id"])

        assert isinstance(post, PostDetails)
        assert post.id == status["id"]
        assert post.cid is None
        assert post.html == status["content"]
        assert post.text.startswith(
            "Spring clear-out on the shop floor today @mabel_finds"
        )
        # The paragraph break becomes a blank line.
        assert "\n\n#fridgesale" in post.text
        # The invisible "https://" in front of the shortened link is gone,
        # but the link's target is still the whole address.
        assert "https://" not in post.text
        assert "fridgedoorvintage.example/shop" in post.text

        mention = next(link for link in post.links if link.kind is LinkKind.MENTION)
        assert post.text[mention.start : mention.end] == "@mabel_finds"
        assert mention.target == "109612345678900001"
        assert mention.url == "https://other.example/@mabel_finds"

        tag = next(link for link in post.links if link.kind is LinkKind.TAG)
        assert post.text[tag.start : tag.end] == "#fridgesale"
        assert tag.target == "fridgesale"
        assert tag.url == "https://social.example/tags/fridgesale"

        plain = next(link for link in post.links if link.kind is LinkKind.LINK)
        assert post.text[plain.start : plain.end] == "fridgedoorvintage.example/shop"
        assert plain.target == "https://fridgedoorvintage.example/shop"
        assert plain.url == "https://fridgedoorvintage.example/shop"

        assert len(post.attachments) == 2
        first = post.attachments[0]
        assert first.kind == "image"
        assert first.width == 1600
        assert first.height == 1200
        assert first.alt_text == status["media_attachments"][0]["description"]

        assert post.visibility is Visibility.PUBLIC
        assert post.created_at == datetime(2026, 9, 20, 14, 3, 11, tzinfo=UTC)
        assert post.parent_id is None
        assert post.root_id == status["id"]
        assert post.reply_count == 5
        assert post.like_count == 9
        assert post.repost_count == 3
        assert post.quote_count == 0
        assert post.liked_by_me is False
        assert post.my_like_id is None
        assert post.is_mine is True
        assert post.unavailable is None
        assert post.raw == status

        assert post.author is not None
        assert post.author.id == FRIDGEDOOR_ID
        assert post.author.handle == "fridgedoor@social.example"

    async def test_it_maps_the_narrower_visibilities(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        status = fixture("status_direct.json")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get(f"/api/v1/statuses/{status['id']}").mock(
                return_value=httpx.Response(200, json=status)
            )

            post = await platform.read_post(fridgedoor, status["id"])

        assert post.visibility is Visibility.DIRECT
        # <br /> becomes a line break inside the one paragraph.
        assert "still available?\nHappy to collect today" in post.text
        assert post.is_mine is False
        assert post.author is not None
        # This account's own acct already carries its host.
        assert post.author.handle == "quietbuyer@other.example"

        mention = next(link for link in post.links if link.kind is LinkKind.MENTION)
        assert mention.target == FRIDGEDOOR_ID

    async def test_a_favourited_post_says_so(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        status = fixture("status_favourited.json")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get(f"/api/v1/statuses/{status['id']}").mock(
                return_value=httpx.Response(200, json=status)
            )

            post = await platform.read_post(fridgedoor, status["id"])

        assert post.liked_by_me is True
        assert post.like_count == 10

    async def test_a_reply_has_no_root_id_without_reading_the_thread(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        reply = fixture("context_thread.json")["descendants"][0]
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get(f"/api/v1/statuses/{reply['id']}").mock(
                return_value=httpx.Response(200, json=reply)
            )

            post = await platform.read_post(fridgedoor, reply["id"])

        assert post.parent_id == reply["in_reply_to_id"]
        assert post.root_id is None

    async def test_a_post_that_is_gone_is_told_apart_from_other_404s(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/statuses/999").mock(
                return_value=httpx.Response(404, json={"error": "Record not found"})
            )

            with pytest.raises(PostGoneError, match="no such post"):
                await platform.read_post(fridgedoor, "999")


def stub_thread(network: respx.Router) -> tuple[dict[str, Any], dict[str, Any]]:
    """Mock the status and its `/context` for `status_own_post.json`."""
    status = fixture("status_own_post.json")
    context = fixture("context_thread.json")
    network.get(f"/api/v1/statuses/{status['id']}").mock(
        return_value=httpx.Response(200, json=status)
    )
    network.get(f"/api/v1/statuses/{status['id']}/context").mock(
        return_value=httpx.Response(200, json=context)
    )
    return status, context


class TestReadingAThread:
    async def test_it_reads_the_post_and_every_reply(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            status, context = stub_thread(network)
            thread = await platform.read_thread(fridgedoor, status["id"])

        assert isinstance(thread, Thread)
        assert thread.post.id == status["id"]
        # No ancestors, so the requested post is its own root.
        assert thread.post.root_id == status["id"]
        assert thread.complete is True
        assert thread.raw == context

        descendants = context["descendants"]
        oldest_first = sorted(descendants, key=lambda item: item["created_at"])
        assert [reply.id for reply in thread.replies] == [
            item["id"] for item in oldest_first
        ]
        for reply in thread.replies:
            assert reply.root_id == status["id"]

        by_id = {reply.id: reply for reply in thread.replies}
        for item in descendants:
            assert by_id[item["id"]].parent_id == item["in_reply_to_id"]

    async def test_depth_keeps_only_the_levels_asked_for(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            status, _context = stub_thread(network)
            thread = await platform.read_thread(fridgedoor, status["id"], depth=1)

        # Direct replies to the root only - two of the five descendants.
        assert {reply.id for reply in thread.replies} == {
            "113140126000000001",
            "113140128000000004",
        }
        assert thread.complete is False

    async def test_limit_caps_how_many_replies_come_back(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            status, _context = stub_thread(network)
            thread = await platform.read_thread(fridgedoor, status["id"], limit=2)

        assert len(thread.replies) == 2
        assert thread.complete is False
        # Oldest first, so the two earliest replies are kept.
        assert [reply.id for reply in thread.replies] == [
            "113140126000000001",
            "113140127000000002",
        ]

    async def test_a_pending_remote_refresh_marks_the_thread_incomplete(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        status = fixture("status_own_post.json")
        context = fixture("context_thread.json")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get(f"/api/v1/statuses/{status['id']}").mock(
                return_value=httpx.Response(200, json=status)
            )
            network.get(f"/api/v1/statuses/{status['id']}/context").mock(
                return_value=httpx.Response(
                    200,
                    json=context,
                    headers={"Mastodon-Async-Refresh": "true"},
                )
            )

            thread = await platform.read_thread(fridgedoor, status["id"])

        assert thread.complete is False

    async def test_a_reply_whose_parent_is_missing_still_gets_a_depth(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        status = fixture("status_own_post.json")
        context = fixture("context_thread.json")
        orphan = copy.deepcopy(context["descendants"][0])
        orphan["id"] = "999999999"
        orphan["in_reply_to_id"] = "no-such-status"
        context["descendants"] = [orphan]

        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get(f"/api/v1/statuses/{status['id']}").mock(
                return_value=httpx.Response(200, json=status)
            )
            network.get(f"/api/v1/statuses/{status['id']}/context").mock(
                return_value=httpx.Response(200, json=context)
            )

            thread = await platform.read_thread(fridgedoor, status["id"], depth=1)

        # Given a depth it cannot resolve, the orphan is still kept rather
        # than silently dropped.
        assert [reply.id for reply in thread.replies] == ["999999999"]
        assert thread.complete is True

    async def test_a_reply_whose_root_is_itself_a_reply_uses_the_true_root(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        reply = fixture("context_thread.json")["descendants"][0]
        root = fixture("status_own_post.json")

        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get(f"/api/v1/statuses/{reply['id']}").mock(
                return_value=httpx.Response(200, json=reply)
            )
            network.get(f"/api/v1/statuses/{reply['id']}/context").mock(
                return_value=httpx.Response(
                    200, json={"ancestors": [root], "descendants": []}
                )
            )

            thread = await platform.read_thread(fridgedoor, reply["id"])

        assert thread.post.root_id == root["id"]

    async def test_a_post_that_is_gone_says_so(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/statuses/999").mock(
                return_value=httpx.Response(404, json={"error": "Record not found"})
            )

            with pytest.raises(PostGoneError):
                await platform.read_thread(fridgedoor, "999")


class TestReplying:
    async def test_it_mentions_the_parents_other_mentions_but_not_itself(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        # status_own_post.json is fridgedoor's own post, mentioning
        # mabel_finds. Replying to your own post never mentions yourself,
        # but the web app still names anyone else the post mentions.
        parent = fixture("status_own_post.json")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            stub_instance(network, host=SOCIAL_HOST)
            network.get(f"/api/v1/statuses/{parent['id']}").mock(
                return_value=httpx.Response(200, json=parent)
            )
            route = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=A_STATUS)
            )

            result = await platform.reply(fridgedoor, parent["id"], "Still there!")

        sent = form_of(route.calls.last.request)
        assert sent["status"] == ["@mabel_finds@other.example Still there!"]
        # No visibility was asked for, and a public parent asks for nothing
        # narrower - so nothing is sent, and the account's own default
        # applies.
        assert "visibility" not in sent
        assert sent["in_reply_to_id"] == [parent["id"]]
        assert result.id == A_STATUS["id"]

    async def test_a_private_parent_with_no_requested_visibility_keeps_private(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        parent = {**fixture("status_own_post.json"), "visibility": "private"}
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            stub_instance(network, host=SOCIAL_HOST)
            network.get(f"/api/v1/statuses/{parent['id']}").mock(
                return_value=httpx.Response(200, json=parent)
            )
            route = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=A_STATUS)
            )

            await platform.reply(fridgedoor, parent["id"], "Still there!")

        # A direct or private parent is the one exception: with nothing
        # requested, the reply keeps the parent's own visibility rather
        # than sending none.
        assert form_of(route.calls.last.request)["visibility"] == ["private"]

    async def test_an_unlisted_parent_with_no_requested_visibility_sends_nothing(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        parent = {**fixture("status_own_post.json"), "visibility": "unlisted"}
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            stub_instance(network, host=SOCIAL_HOST)
            network.get(f"/api/v1/statuses/{parent['id']}").mock(
                return_value=httpx.Response(200, json=parent)
            )
            route = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=A_STATUS)
            )

            await platform.reply(fridgedoor, parent["id"], "Still there!")

        # unlisted is not direct or private, so nothing beats sending no
        # visibility at all and letting the account default apply.
        assert "visibility" not in form_of(route.calls.last.request)

    async def test_a_requested_visibility_narrower_than_the_parent_wins(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        # status_own_post.json's parent is public - narrower than that,
        # requesting "private" should be honoured rather than overridden.
        parent = fixture("status_own_post.json")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            stub_instance(network, host=SOCIAL_HOST)
            network.get(f"/api/v1/statuses/{parent['id']}").mock(
                return_value=httpx.Response(200, json=parent)
            )
            route = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=A_STATUS)
            )

            await platform.reply(
                fridgedoor,
                parent["id"],
                "Still there!",
                options={"visibility": "private"},
            )

        assert form_of(route.calls.last.request)["visibility"] == ["private"]

    async def test_it_mentions_the_parents_author_but_not_the_replying_account(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        # status_direct.json mentions fridgedoor - the account doing the
        # replying - which must never be mentioned back to itself.
        parent = fixture("status_direct.json")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            stub_instance(network, host=SOCIAL_HOST)
            network.get(f"/api/v1/statuses/{parent['id']}").mock(
                return_value=httpx.Response(200, json=parent)
            )
            route = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=A_STATUS)
            )

            await platform.reply(fridgedoor, parent["id"], "Yes, still here!")

        sent = form_of(route.calls.last.request)
        assert sent["status"] == ["@quietbuyer@other.example Yes, still here!"]
        # A reply to a direct message stays direct, whatever the default is.
        assert sent["visibility"] == ["direct"]

    async def test_it_does_not_mention_someone_already_named_in_the_text(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        parent = fixture("status_direct.json")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            stub_instance(network, host=SOCIAL_HOST)
            network.get(f"/api/v1/statuses/{parent['id']}").mock(
                return_value=httpx.Response(200, json=parent)
            )
            route = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=A_STATUS)
            )

            await platform.reply(
                fridgedoor,
                parent["id"],
                # Case does not matter when checking for a mention already
                # in the text.
                "Thanks @QuietBuyer@other.example, still here!",
            )

        sent = form_of(route.calls.last.request)
        assert sent["status"] == ["Thanks @QuietBuyer@other.example, still here!"]

    async def test_a_reply_stays_no_wider_than_its_parent(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        parent = fixture("status_direct.json")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            stub_instance(network, host=SOCIAL_HOST)
            network.get(f"/api/v1/statuses/{parent['id']}").mock(
                return_value=httpx.Response(200, json=parent)
            )
            route = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=A_STATUS)
            )

            await platform.reply(
                fridgedoor,
                parent["id"],
                "Yes",
                options={"visibility": "public"},
            )

        # Asking for "public" does not widen a reply to a direct message.
        assert form_of(route.calls.last.request)["visibility"] == ["direct"]

    async def test_it_uploads_media_the_same_way_publish_does(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        parent = fixture("status_own_post.json")
        picture = Media.from_bytes(b"png-bytes", filename="cat.png")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            stub_instance(network, host=SOCIAL_HOST)
            network.get(f"/api/v1/statuses/{parent['id']}").mock(
                return_value=httpx.Response(200, json=parent)
            )
            network.post("/api/v2/media").mock(
                return_value=httpx.Response(200, json={"id": "m1"})
            )
            route = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=A_STATUS)
            )

            await platform.reply(fridgedoor, parent["id"], "Look", media=(picture,))

        assert form_of(route.calls.last.request)["media_ids[]"] == ["m1"]

    async def test_replying_to_a_post_that_is_gone_says_so(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/statuses/999").mock(
                return_value=httpx.Response(404, json={"error": "Record not found"})
            )

            with pytest.raises(PostGoneError):
                await platform.reply(fridgedoor, "999", "Hello?")


class TestLiking:
    async def test_it_favourites_a_post(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        favourited = fixture("status_favourited.json")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            route = network.post(f"/api/v1/statuses/{favourited['id']}/favourite").mock(
                return_value=httpx.Response(200, json=favourited)
            )

            result = await platform.like(fridgedoor, favourited["id"])

        assert route.calls.last.request.headers["authorization"] == (
            "Bearer fridgedoor-token"
        )
        assert isinstance(result, LikeResult)
        assert result.post_id == favourited["id"]
        assert result.like_id is None
        assert result.raw == favourited

    async def test_it_unfavourites_a_post(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            route = network.post("/api/v1/statuses/110001/unfavourite").mock(
                return_value=httpx.Response(200, json=fixture("status_own_post.json"))
            )

            await platform.unlike(fridgedoor, "110001", like_id="ignored-anyway")

        assert route.called

    async def test_liking_a_post_that_is_gone_says_so(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.post("/api/v1/statuses/999/favourite").mock(
                return_value=httpx.Response(404, json={"error": "Record not found"})
            )

            with pytest.raises(PostGoneError):
                await platform.like(fridgedoor, "999")


class TestReadingLikes:
    async def test_it_lists_who_liked_a_post(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        accounts = fixture("favourited_by.json")
        link_header = fixture_text("favourited_by_link_header.txt")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            route = network.get("/api/v1/statuses/110001/favourited_by").mock(
                return_value=httpx.Response(
                    200, json=accounts, headers={"Link": link_header}
                )
            )

            page = await platform.read_likes(fridgedoor, "110001")

        assert route.calls.last.request.url.params.get("max_id") is None
        assert isinstance(page, Page)
        assert len(page.items) == 3
        first = page.items[0]
        assert isinstance(first, Like)
        assert first.liked_at is None
        assert isinstance(first.person, Person)
        assert first.person.id == accounts[0]["id"]
        assert first.person.handle == accounts[0]["acct"]
        # Taken from the Link header's "next" rel, whose url carries max_id.
        assert page.next == "109612345678900003"

    async def test_after_is_sent_back_as_max_id(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            route = network.get("/api/v1/statuses/110001/favourited_by").mock(
                return_value=httpx.Response(200, json=[])
            )

            page = await platform.read_likes(fridgedoor, "110001", after="42")

        assert route.calls.last.request.url.params["max_id"] == "42"
        assert page.items == ()
        assert page.next is None

    async def test_the_limit_is_capped_at_eighty(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            route = network.get("/api/v1/statuses/110001/favourited_by").mock(
                return_value=httpx.Response(200, json=[])
            )

            await platform.read_likes(fridgedoor, "110001", limit=500)

        assert route.calls.last.request.url.params["limit"] == "80"

    async def test_reading_likes_for_a_post_that_is_gone_says_so(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/statuses/999/favourited_by").mock(
                return_value=httpx.Response(404, json={"error": "Record not found"})
            )

            with pytest.raises(PostGoneError):
                await platform.read_likes(fridgedoor, "999")


class TestReadingUpdatesAfterAMarker:
    async def test_the_first_call_reads_the_latest_page(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        items = fixture("notifications.json")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            route = network.get("/api/v1/notifications").mock(
                return_value=httpx.Response(200, json=items)
            )

            batch = await platform.fetch_updates_after(fridgedoor, None)

        assert "min_id" not in route.calls.last.request.url.params
        assert [update.id for update in batch.updates] == [
            item["id"] for item in reversed(items)
        ]
        # The newest notification's id, ready to store.
        assert batch.marker == items[0]["id"]
        # Fewer than a full page (40 by default) came back.
        assert batch.more is False

    async def test_a_direct_mention_arrives_as_a_message(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        items = fixture("notifications.json")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/notifications").mock(
                return_value=httpx.Response(200, json=items)
            )
            batch = await platform.fetch_updates_after(fridgedoor, None)

        by_id = {update.id: update for update in batch.updates}
        message = by_id["118980500000000006"]
        assert message.kind is UpdateKind.MESSAGE_RECEIVED
        assert message.post_id == "113140200000000001"
        assert message.about_post_id is None
        assert message.actor is not None
        assert message.actor.handle == "quietbuyer@other.example"

    async def test_a_reply_to_our_own_post_is_a_comment(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        items = fixture("notifications.json")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/notifications").mock(
                return_value=httpx.Response(200, json=items)
            )
            batch = await platform.fetch_updates_after(fridgedoor, None)

        by_id = {update.id: update for update in batch.updates}
        comment = by_id["118915600000000003"]
        assert comment.kind is UpdateKind.COMMENT_CREATED
        assert comment.about_post_id == "113140123456789012"

    async def test_a_direct_reply_to_our_own_post_is_still_a_message(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        # A status can be both direct-visibility and a reply to one of our
        # own posts. Direct wins: it is a message, not a comment.
        items = [
            {
                "id": "999000000000000001",
                "type": "mention",
                "created_at": "2026-09-24T00:00:00.000Z",
                "account": {
                    "id": "109612345678900006",
                    "acct": "quietbuyer@other.example",
                    "url": "https://other.example/@quietbuyer",
                },
                "status": {
                    "id": "113140200000000099",
                    "created_at": "2026-09-24T00:00:00.000Z",
                    "visibility": "direct",
                    "in_reply_to_id": "113140123456789012",
                    "in_reply_to_account_id": FRIDGEDOOR_ID,
                },
            }
        ]
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/notifications").mock(
                return_value=httpx.Response(200, json=items)
            )
            batch = await platform.fetch_updates_after(fridgedoor, None)

        update = batch.updates[0]
        assert update.kind is UpdateKind.MESSAGE_RECEIVED
        assert update.about_post_id is None

    async def test_a_plain_mention_stays_a_mention(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        items = fixture("notifications.json")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/notifications").mock(
                return_value=httpx.Response(200, json=items)
            )
            batch = await platform.fetch_updates_after(fridgedoor, None)

        by_id = {update.id: update for update in batch.updates}
        plain = by_id["118902200000000001"]
        assert plain.kind is UpdateKind.MENTION
        assert plain.about_post_id is None

    async def test_a_reblog_and_a_favourite_concern_our_own_post(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        items = fixture("notifications.json")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/notifications").mock(
                return_value=httpx.Response(200, json=items)
            )
            batch = await platform.fetch_updates_after(fridgedoor, None)

        by_id = {update.id: update for update in batch.updates}
        reblog = by_id["118958200000000005"]
        favourite = by_id["118940100000000004"]
        assert reblog.kind is UpdateKind.REPOST_ADDED
        assert reblog.about_post_id == "113140123456789012"
        assert reblog.post_id == "113140123456789012"
        assert favourite.kind is UpdateKind.REACTION_ADDED
        assert favourite.about_post_id == "113140123456789012"

    async def test_a_follow_has_no_post_but_has_an_actor(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        items = fixture("notifications.json")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/notifications").mock(
                return_value=httpx.Response(200, json=items)
            )
            batch = await platform.fetch_updates_after(fridgedoor, None)

        by_id = {update.id: update for update in batch.updates}
        follow = by_id["118912300000000002"]
        assert follow.kind is UpdateKind.FOLLOWED
        assert follow.post_id is None
        assert follow.actor is not None
        assert follow.actor.handle == "newintown@other.example"

    async def test_a_marker_is_sent_as_min_id(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            route = network.get("/api/v1/notifications").mock(
                return_value=httpx.Response(200, json=[])
            )

            batch = await platform.fetch_updates_after(fridgedoor, "118902200000000001")

        assert route.calls.last.request.url.params["min_id"] == "118902200000000001"
        # Nothing came back, so the marker we were given is kept as it was.
        assert batch.marker == "118902200000000001"
        assert batch.updates == ()

    async def test_only_updates_newer_than_the_marker_are_kept(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        # A defensive filter of our own: even if a server answered with more
        # than it should have, nothing at or before the marker gets through.
        items = fixture("notifications.json")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/notifications").mock(
                return_value=httpx.Response(200, json=items)
            )

            batch = await platform.fetch_updates_after(fridgedoor, "118915600000000003")

        assert {update.id for update in batch.updates} == {
            "118980500000000006",
            "118958200000000005",
            "118940100000000004",
        }

    async def test_more_is_true_when_a_full_page_comes_back(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        items = fixture("notifications.json")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            route = network.get("/api/v1/notifications").mock(
                return_value=httpx.Response(200, json=items)
            )

            batch = await platform.fetch_updates_after(
                fridgedoor, None, limit=len(items)
            )

        assert route.calls.last.request.url.params["limit"] == str(len(items))
        assert batch.more is True

    async def test_an_unreadable_notification_is_left_out(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/notifications").mock(
                return_value=httpx.Response(
                    200, json=[{"id": "1", "type": "follow", "created_at": "whenever"}]
                )
            )

            batch = await platform.fetch_updates_after(fridgedoor, None)

        assert batch.updates == ()
        assert batch.marker is None


class TestMarkingSeen:
    async def test_it_posts_the_marker(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            route = network.post("/api/v1/markers").mock(
                return_value=httpx.Response(200, json=fixture("marker_saved.json"))
            )

            await platform.mark_seen(fridgedoor, "118980500000000006")

        sent = form_of(route.calls.last.request)
        assert sent["notifications[last_read_id]"] == ["118980500000000006"]

    async def test_a_conflict_is_retried_once(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            route = network.post("/api/v1/markers")
            route.side_effect = [
                httpx.Response(409, json={"error": "Conflict"}),
                httpx.Response(200, json=fixture("marker_saved.json")),
            ]

            await platform.mark_seen(fridgedoor, "118980500000000006")

        assert route.call_count == 2

    async def test_a_second_conflict_is_let_through(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.post("/api/v1/markers").mock(
                return_value=httpx.Response(409, json={"error": "Conflict"})
            )

            with pytest.raises(PlatformError):
                await platform.mark_seen(fridgedoor, "118980500000000006")


class TestReadingConversations:
    async def test_it_lists_conversations(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        conversations = fixture("conversations.json")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(200, json=conversations)
            )

            page = await platform.read_conversations(fridgedoor)

        assert isinstance(page, Page)
        assert len(page.items) == 2
        unread, read = page.items

        assert isinstance(unread, Conversation)
        assert unread.id == "418212"
        assert unread.unread_count == 1
        assert unread.full_history is False
        assert unread.can_reply_until is None
        assert len(unread.people) == 1
        assert unread.people[0].handle == "quietbuyer@other.example"
        assert unread.last_message is not None
        assert unread.last_message.conversation_id == "418212"
        assert unread.updated_at == unread.last_message.sent_at

        assert read.unread_count == 0

    async def test_after_and_limit_go_to_mastodon_as_max_id_and_limit(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            route = network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(200, json=[])
            )

            await platform.read_conversations(fridgedoor, after="418100", limit=5)

        params = route.calls.last.request.url.params
        assert params["max_id"] == "418100"
        assert params["limit"] == "5"

    async def test_the_limit_is_capped_at_forty(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            route = network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(200, json=[])
            )

            await platform.read_conversations(fridgedoor, limit=1000)

        assert route.calls.last.request.url.params["limit"] == "40"

    async def test_the_link_header_becomes_page_next(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        header = (
            '<https://social.example/api/v1/conversations?max_id=418000>; rel="next"'
        )
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(200, json=[], headers={"Link": header})
            )

            page = await platform.read_conversations(fridgedoor)

        assert page.next == "418000"


class TestReadingMessages:
    async def test_it_reads_the_direct_statuses_between_the_same_people(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        conversation = fixture("conversations.json")[0]
        last_status_id = conversation["last_status"]["id"]
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(200, json=[conversation])
            )
            network.get(f"/api/v1/statuses/{last_status_id}/context").mock(
                return_value=httpx.Response(
                    200, json={"ancestors": [], "descendants": []}
                )
            )

            page = await platform.read_messages(fridgedoor, conversation["id"])

        assert isinstance(page, Page)
        assert page.next is None
        assert len(page.items) == 1
        message = page.items[0]
        assert isinstance(message, Message)
        assert message.id == last_status_id
        assert message.conversation_id == conversation["id"]
        assert message.is_mine is False
        assert message.deleted is False
        assert "record cabinets still available" in message.text

    async def test_only_direct_visibility_statuses_are_kept(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        conversation = fixture("conversations.json")[0]
        last_status_id = conversation["last_status"]["id"]

        first_descendant = fixture("context_thread.json")["descendants"][0]
        public_ancestor = copy.deepcopy(first_descendant)
        public_ancestor["visibility"] = "public"

        direct_descendant = copy.deepcopy(conversation["last_status"])
        direct_descendant["id"] = "113140200000000002"
        direct_descendant["created_at"] = "2026-09-23T09:20:00.000Z"

        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(200, json=[conversation])
            )
            network.get(f"/api/v1/statuses/{last_status_id}/context").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "ancestors": [public_ancestor],
                        "descendants": [direct_descendant],
                    },
                )
            )

            page = await platform.read_messages(fridgedoor, conversation["id"])

        # Newest first, and the public ancestor never makes it in.
        assert [message.id for message in page.items] == [
            "113140200000000002",
            last_status_id,
        ]

    async def test_only_statuses_between_this_conversations_own_people_are_kept(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        # A mixed-participant tree: this conversation is just fridgedoor and
        # quietbuyer. A direct status can share the same /context thread
        # without belonging to this conversation at all - a bystander's own
        # direct message to fridgedoor about something unrelated, threaded
        # underneath by Mastodon's own view. Only statuses strictly between
        # this conversation's own people belong in it.
        conversation = fixture("conversations.json")[0]
        last_status_id = conversation["last_status"]["id"]
        quietbuyer = conversation["accounts"][0]
        bystander = {
            "id": "109612345678900007",
            "acct": "regular_visitor@other.example",
        }

        from_a_bystander = {
            "id": "113140200000000010",
            "created_at": "2026-09-23T09:15:00.000Z",
            "visibility": "direct",
            "content": "<p>Unrelated direct message</p>",
            "account": bystander,
            "mentions": [
                {"id": FRIDGEDOOR_ID, "acct": "fridgedoor"},
            ],
        }
        leaks_to_a_third_party = {
            "id": "113140200000000011",
            "created_at": "2026-09-23T09:16:00.000Z",
            "visibility": "direct",
            "content": "<p>Loop them in too</p>",
            "account": quietbuyer,
            "mentions": [
                {"id": FRIDGEDOOR_ID, "acct": "fridgedoor"},
                {"id": bystander["id"], "acct": bystander["acct"]},
            ],
        }
        belongs_here = {
            "id": "113140200000000012",
            "created_at": "2026-09-23T09:17:00.000Z",
            "visibility": "direct",
            "content": "<p>Still just us</p>",
            "account": quietbuyer,
            "mentions": [{"id": FRIDGEDOOR_ID, "acct": "fridgedoor"}],
        }

        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(200, json=[conversation])
            )
            network.get(f"/api/v1/statuses/{last_status_id}/context").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "ancestors": [],
                        "descendants": [
                            from_a_bystander,
                            leaks_to_a_third_party,
                            belongs_here,
                        ],
                    },
                )
            )

            page = await platform.read_messages(fridgedoor, conversation["id"])

        assert [message.id for message in page.items] == [
            belongs_here["id"],
            last_status_id,
        ]

    async def test_after_drops_everything_up_to_and_including_it(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        conversation = fixture("conversations.json")[0]
        last_status_id = conversation["last_status"]["id"]
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(200, json=[conversation])
            )
            network.get(f"/api/v1/statuses/{last_status_id}/context").mock(
                return_value=httpx.Response(
                    200, json={"ancestors": [], "descendants": []}
                )
            )

            page = await platform.read_messages(
                fridgedoor, conversation["id"], after=last_status_id
            )

        assert page.items == ()

    async def test_limit_caps_how_many_messages_come_back(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        conversation = fixture("conversations.json")[0]
        last_status_id = conversation["last_status"]["id"]
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(200, json=[conversation])
            )
            network.get(f"/api/v1/statuses/{last_status_id}/context").mock(
                return_value=httpx.Response(
                    200, json={"ancestors": [], "descendants": []}
                )
            )

            page = await platform.read_messages(fridgedoor, conversation["id"], limit=0)

        assert page.items == ()

    async def test_no_such_conversation_is_an_empty_page(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(200, json=[])
            )

            page = await platform.read_messages(fridgedoor, "does-not-exist")

        assert page.items == ()
        assert page.next is None

    async def test_a_conversation_with_no_last_status_is_an_empty_page(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        conversation = copy.deepcopy(fixture("conversations.json")[0])
        del conversation["last_status"]
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(200, json=[conversation])
            )

            page = await platform.read_messages(fridgedoor, conversation["id"])

        assert page.items == ()


class TestSendingAMessage:
    async def test_it_sends_a_direct_status_mentioning_every_participant(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        conversation = fixture("conversations.json")[0]
        last_status_id = conversation["last_status"]["id"]
        new_status = {
            **fixture("status_direct.json"),
            "id": "113140200000000099",
        }
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(200, json=[conversation])
            )
            route = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=new_status)
            )

            message = await platform.send_message(
                fridgedoor, conversation["id"], "On my way!"
            )

        sent = form_of(route.calls.last.request)
        assert sent["status"] == ["@quietbuyer@other.example On my way!"]
        assert sent["visibility"] == ["direct"]
        assert sent["in_reply_to_id"] == [last_status_id]
        assert isinstance(message, Message)
        assert message.conversation_id == conversation["id"]
        assert message.id == new_status["id"]

    async def test_sending_into_a_conversation_that_does_not_exist_says_so(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(200, json=[])
            )

            with pytest.raises(NotFoundError, match="does-not-exist"):
                await platform.send_message(fridgedoor, "does-not-exist", "Hi")


class TestMarkingAConversationRead:
    async def test_it_marks_a_conversation_as_read(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            route = network.post("/api/v1/conversations/418212/read").mock(
                return_value=httpx.Response(200, json=fixture("conversation_read.json"))
            )

            await platform.mark_read(fridgedoor, "418212")

        assert route.called


class TestStartingAConversation:
    async def test_it_starts_a_conversation_and_finds_its_id(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        quietbuyer = fixture("conversations.json")[0]["accounts"][0]
        new_status = {**fixture("status_direct.json"), "id": "113140200000000100"}
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get(f"/api/v1/accounts/{quietbuyer['id']}").mock(
                return_value=httpx.Response(200, json=quietbuyer)
            )
            route = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=new_status)
            )
            network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(
                    200,
                    json=[{"id": "555", "last_status": new_status, "accounts": []}],
                )
            )

            message = await platform.start_conversation(
                fridgedoor, [quietbuyer["id"]], "Hello there"
            )

        sent = form_of(route.calls.last.request)
        assert sent["status"] == ["@quietbuyer@other.example Hello there"]
        assert sent["visibility"] == ["direct"]
        assert message.conversation_id == "555"
        assert message.id == new_status["id"]

    async def test_it_falls_back_to_a_clearly_marked_status_id(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        quietbuyer = fixture("conversations.json")[0]["accounts"][0]
        new_status = {**fixture("status_direct.json"), "id": "113140200000000101"}
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get(f"/api/v1/accounts/{quietbuyer['id']}").mock(
                return_value=httpx.Response(200, json=quietbuyer)
            )
            network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=new_status)
            )
            # No conversation on the first page matches the new status - a
            # burst of other direct messages, in this telling.
            network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(200, json=[])
            )

            message = await platform.start_conversation(
                fridgedoor, [quietbuyer["id"]], "Hello there"
            )

        # Not the bare status id - a clearly marked fallback, since a bare
        # id would be mistaken for a real (if wrong) conversation id.
        assert message.conversation_id == "status:113140200000000101"

    async def test_sending_into_the_fallback_id_replies_to_the_status_directly(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        parent = fixture("status_direct.json")
        new_reply = {**fixture("status_direct.json"), "id": "113140200000000200"}
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get(f"/api/v1/statuses/{parent['id']}").mock(
                return_value=httpx.Response(200, json=parent)
            )
            route = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=new_reply)
            )

            message = await platform.send_message(
                fridgedoor, f"status:{parent['id']}", "Still here!"
            )

        sent = form_of(route.calls.last.request)
        # quietbuyer is the parent's author; fridgedoor is mentioned in the
        # parent but is the connected account, so it is never mentioned back.
        assert sent["status"] == ["@quietbuyer@other.example Still here!"]
        assert sent["visibility"] == ["direct"]
        assert sent["in_reply_to_id"] == [parent["id"]]
        assert message.conversation_id == f"status:{parent['id']}"

    async def test_reading_the_fallback_id_reads_that_statuss_context(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        parent = fixture("status_direct.json")
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get(f"/api/v1/statuses/{parent['id']}").mock(
                return_value=httpx.Response(200, json=parent)
            )
            network.get(f"/api/v1/statuses/{parent['id']}/context").mock(
                return_value=httpx.Response(
                    200, json={"ancestors": [], "descendants": []}
                )
            )

            page = await platform.read_messages(fridgedoor, f"status:{parent['id']}")

        assert len(page.items) == 1
        assert page.items[0].id == parent["id"]
        assert page.items[0].conversation_id == f"status:{parent['id']}"

    async def test_marking_the_fallback_id_read_finds_the_real_conversation(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        conversation = {
            "id": "418300",
            "accounts": [],
            "last_status": {"id": "113140200000000300"},
        }
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(200, json=[conversation])
            )
            route = network.post("/api/v1/conversations/418300/read").mock(
                return_value=httpx.Response(200, json=fixture("conversation_read.json"))
            )

            await platform.mark_read(fridgedoor, "status:113140200000000300")

        assert route.called

    async def test_marking_the_fallback_id_read_does_nothing_if_not_found_yet(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        # No POST route is mocked at all here - if mark_read tried to POST
        # anything, respx would raise for the unmatched request.
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            conversations_route = network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(200, json=[])
            )

            await platform.mark_read(fridgedoor, "status:113140200000099999")

        assert conversations_route.called

    async def test_the_found_conversation_id_keeps_working_downstream(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        # The everyday case: start_conversation found a real conversation
        # id, and it keeps working for every later call.
        quietbuyer = fixture("conversations.json")[0]["accounts"][0]
        new_status = {**fixture("status_direct.json"), "id": "113140200000000400"}
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get(f"/api/v1/accounts/{quietbuyer['id']}").mock(
                return_value=httpx.Response(200, json=quietbuyer)
            )
            network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=new_status)
            )
            network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(
                    200,
                    json=[{"id": "418400", "last_status": new_status, "accounts": []}],
                )
            )

            started = await platform.start_conversation(
                fridgedoor, [quietbuyer["id"]], "Hello there"
            )

        assert started.conversation_id == "418400"

        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            route = network.post("/api/v1/conversations/418400/read").mock(
                return_value=httpx.Response(200, json=fixture("conversation_read.json"))
            )

            await platform.mark_read(fridgedoor, started.conversation_id)

        assert route.called


class TestSocialInboxErrors:
    async def test_a_missing_scope_names_itself(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        error = fixture("errors.json")["403_insufficient_scope"]
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.post("/api/v1/statuses/110001/favourite").mock(
                return_value=httpx.Response(error["status"], json=error["body"])
            )

            with pytest.raises(MissingPermissionError) as complaint:
                await platform.like(fridgedoor, "110001")

        assert complaint.value.needs
        assert complaint.value.suggestion is not None

    async def test_a_generic_403_stays_a_plain_not_allowed_error(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        # Mastodon answers "This action is not allowed" both for a genuine
        # block and for every other permission failure - see errors.json's
        # 403_not_permitted - so this cannot become BlockedError.
        error = fixture("errors.json")["403_not_permitted"]
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.post("/api/v1/statuses/110001/favourite").mock(
                return_value=httpx.Response(error["status"], json=error["body"])
            )

            with pytest.raises(NotAllowedError) as complaint:
                await platform.like(fridgedoor, "110001")

        assert not isinstance(complaint.value, MissingPermissionError)
        assert not isinstance(complaint.value, PostGoneError)

    async def test_a_real_rate_limit_reply_is_read_correctly(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        error = fixture("errors.json")["429_rate_limited"]
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/statuses/110001").mock(
                return_value=httpx.Response(
                    error["status"], json=error["body"], headers=error["headers"]
                )
            )

            with pytest.raises(RateLimitError) as complaint:
                await platform.read_post(fridgedoor, "110001")

        # X-RateLimit-Reset, read as the Retry-After fallback.
        assert complaint.value.retry_after is not None
        assert complaint.value.retry_after >= 0.0

    async def test_a_404_that_is_not_a_status_stays_a_plain_not_found_error(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        # /api/v1/markers is not a status endpoint, so a 404 there is not
        # turned into PostGoneError.
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.post("/api/v1/markers").mock(
                return_value=httpx.Response(404, json={"error": "Not found"})
            )

            with pytest.raises(NotFoundError) as complaint:
                await platform.mark_seen(fridgedoor, "1")

        assert not isinstance(complaint.value, PostGoneError)


class TestSocialInboxEdgeCases:
    """One test per branch the everyday cases above never touch."""

    async def test_an_account_with_no_acct_has_no_handle(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        no_acct = {"id": "1", "display_name": "Nobody"}
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/statuses/110001/favourited_by").mock(
                return_value=httpx.Response(200, json=[no_acct])
            )

            page = await platform.read_likes(fridgedoor, "110001")

        assert page.items[0].person.handle is None

    async def test_the_fallback_reply_skips_a_bare_mention_and_a_duplicate(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        parent = {
            "id": "900",
            "visibility": "direct",
            "account": {"id": "1", "acct": "quietbuyer@other.example"},
            "mentions": [
                {"id": "2", "acct": None},
                # A duplicate of the author - already seen, so skipped.
                {"id": "3", "acct": "quietbuyer@other.example"},
                {"id": FRIDGEDOOR_ID, "acct": "fridgedoor"},
            ],
        }
        new_reply = {**fixture("status_direct.json"), "id": "901"}
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/statuses/900").mock(
                return_value=httpx.Response(200, json=parent)
            )
            route = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=new_reply)
            )

            await platform.send_message(fridgedoor, "status:900", "Hi")

        sent = form_of(route.calls.last.request)
        assert sent["status"] == ["@quietbuyer@other.example Hi"]

    async def test_the_fallback_anchor_with_no_author_id_is_dropped(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        anchor = copy.deepcopy(fixture("status_direct.json"))
        del anchor["account"]["id"]
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get(f"/api/v1/statuses/{anchor['id']}").mock(
                return_value=httpx.Response(200, json=anchor)
            )
            network.get(f"/api/v1/statuses/{anchor['id']}/context").mock(
                return_value=httpx.Response(
                    200, json={"ancestors": [], "descendants": []}
                )
            )

            page = await platform.read_messages(fridgedoor, f"status:{anchor['id']}")

        # An author with no id at all cannot be verified as a participant,
        # so it is dropped along with everything else.
        assert page.items == ()

    async def test_a_tag_this_parser_does_not_treat_specially_is_still_read(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        status = {
            **fixture("status_own_post.json"),
            "content": "<p>Hello <b>world</b></p>",
            "mentions": [],
            "tags": [],
            "media_attachments": [],
        }
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get(f"/api/v1/statuses/{status['id']}").mock(
                return_value=httpx.Response(200, json=status)
            )

            post = await platform.read_post(fridgedoor, status["id"])

        assert post.text == "Hello world"

    async def test_a_mention_with_no_acct_is_never_prepended(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        parent = copy.deepcopy(fixture("status_own_post.json"))
        parent["account"]["acct"] = "fridgedoor"
        parent["mentions"] = [{"id": "999", "url": "https://other.example/@x"}]

        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            stub_instance(network, host=SOCIAL_HOST)
            network.get(f"/api/v1/statuses/{parent['id']}").mock(
                return_value=httpx.Response(200, json=parent)
            )
            route = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=A_STATUS)
            )

            await platform.reply(fridgedoor, parent["id"], "Hi")

        # Neither fridgedoor (self) nor the mention with no acct is added.
        assert form_of(route.calls.last.request)["status"] == ["Hi"]

    async def test_starting_a_conversation_with_nobody_adds_no_mentions(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        new_status = {**fixture("status_direct.json"), "id": "999"}
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            route = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=new_status)
            )
            network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(200, json=[])
            )

            await platform.start_conversation(fridgedoor, [], "Just me")

        assert form_of(route.calls.last.request)["status"] == ["Just me"]

    async def test_ids_that_are_not_numbers_still_compare(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/notifications").mock(
                return_value=httpx.Response(
                    200,
                    json=[
                        {
                            "id": "not-a-number",
                            "type": "follow",
                            "created_at": "2026-09-24T00:00:00.000Z",
                        }
                    ],
                )
            )

            batch = await platform.fetch_updates_after(fridgedoor, "also-not-a-number")

        assert [update.id for update in batch.updates] == ["not-a-number"]

    async def test_a_non_conflict_failure_marking_seen_is_not_retried(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            route = network.post("/api/v1/markers").mock(
                return_value=httpx.Response(500, json={"error": "Broken"})
            )

            with pytest.raises(PlatformError):
                await platform.mark_seen(fridgedoor, "1")

        assert route.call_count == 1

    async def test_finding_a_conversation_pages_past_one_that_does_not_match(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        other_conversation = {
            "id": "1",
            "unread": False,
            "accounts": [],
            "last_status": {**fixture("status_direct.json"), "id": "1"},
        }
        wanted = fixture("conversations.json")[0]
        last_status_id = wanted["last_status"]["id"]

        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            conversations_route = network.get("/api/v1/conversations")
            conversations_route.side_effect = [
                httpx.Response(
                    200,
                    json=[other_conversation],
                    headers={
                        "Link": (
                            "<https://social.example/api/v1/conversations"
                            '?max_id=2>; rel="next"'
                        )
                    },
                ),
                httpx.Response(200, json=[wanted]),
            ]
            network.get(f"/api/v1/statuses/{last_status_id}/context").mock(
                return_value=httpx.Response(
                    200, json={"ancestors": [], "descendants": []}
                )
            )

            page = await platform.read_messages(fridgedoor, wanted["id"])

        assert len(page.items) == 1
        assert page.items[0].id == last_status_id

    async def test_after_that_matches_nothing_changes_nothing(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        conversation = fixture("conversations.json")[0]
        last_status_id = conversation["last_status"]["id"]
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(200, json=[conversation])
            )
            network.get(f"/api/v1/statuses/{last_status_id}/context").mock(
                return_value=httpx.Response(
                    200, json={"ancestors": [], "descendants": []}
                )
            )

            page = await platform.read_messages(
                fridgedoor, conversation["id"], after="no-such-message"
            )

        assert len(page.items) == 1

    async def test_sending_into_a_conversation_with_no_last_status_yet(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        conversation = {
            "id": "418212",
            "unread": False,
            "accounts": fixture("conversations.json")[0]["accounts"],
        }
        new_status = {**fixture("status_direct.json"), "id": "1"}
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(200, json=[conversation])
            )
            route = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=new_status)
            )

            await platform.send_message(fridgedoor, "418212", "Hi there")

        assert "in_reply_to_id" not in form_of(route.calls.last.request)

    async def test_starting_a_conversation_pages_past_one_that_does_not_match(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        quietbuyer = fixture("conversations.json")[0]["accounts"][0]
        new_status = {**fixture("status_direct.json"), "id": "1234"}
        unrelated = {"id": "9", "last_status": {"id": "not-the-new-one"}}
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get(f"/api/v1/accounts/{quietbuyer['id']}").mock(
                return_value=httpx.Response(200, json=quietbuyer)
            )
            network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=new_status)
            )
            network.get("/api/v1/conversations").mock(
                return_value=httpx.Response(
                    200, json=[unrelated, {"id": "10", "last_status": new_status}]
                )
            )

            message = await platform.start_conversation(
                fridgedoor, [quietbuyer["id"]], "Hi"
            )

        assert message.conversation_id == "10"

    async def test_starting_a_conversation_pages_past_more_than_one_page(
        self,
        platform: MastodonPlatform,
        fridgedoor: Connection,
    ) -> None:
        # The genuine multi-page case: the new conversation is not on the
        # first page at all, and only shows up once a `Link` header sends
        # this on to a second `GET`. This is the same bounded paging
        # `_find_conversation` uses for an ordinary conversation id.
        quietbuyer = fixture("conversations.json")[0]["accounts"][0]
        new_status = {**fixture("status_direct.json"), "id": "5678"}
        unrelated = {"id": "9", "last_status": {"id": "not-the-new-one"}}
        with respx.mock(base_url=f"https://{SOCIAL_HOST}") as network:
            network.get(f"/api/v1/accounts/{quietbuyer['id']}").mock(
                return_value=httpx.Response(200, json=quietbuyer)
            )
            network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json=new_status)
            )
            conversations_route = network.get("/api/v1/conversations")
            conversations_route.side_effect = [
                httpx.Response(
                    200,
                    json=[unrelated],
                    headers={
                        "Link": (
                            "<https://social.example/api/v1/conversations"
                            '?max_id=9>; rel="next"'
                        )
                    },
                ),
                httpx.Response(200, json=[{"id": "20", "last_status": new_status}]),
            ]

            message = await platform.start_conversation(
                fridgedoor, [quietbuyer["id"]], "Hi"
            )

        assert message.conversation_id == "20"
        assert conversations_route.call_count == 2
