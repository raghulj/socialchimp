"""Tests for the Bluesky platform."""

from __future__ import annotations

import base64
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from socialchimp import (
    AppCredentials,
    AuthError,
    BlockedError,
    ConfigError,
    Connection,
    Feature,
    InMemoryStorage,
    InvalidPostError,
    Limits,
    LinkKind,
    Media,
    MissingPermissionError,
    NotSupportedError,
    PlatformError,
    Post,
    PostGoneError,
    PostState,
    RateLimitError,
    SocialChimp,
    Token,
    TokenExpiredError,
    Unavailable,
    UpdateKind,
)
from socialchimp.features import TextCount
from socialchimp.features import count_graphemes as shared_count_graphemes
from socialchimp.http import Retries
from socialchimp.platform import (
    AskForDetails,
    CanDeletePosts,
    CanLike,
    CanMessage,
    CanReadLikes,
    CanReadPost,
    CanReadThread,
    CanReadUpdates,
    CanReadUpdatesAfter,
    CanReply,
    CanStartConversations,
    Finished,
    LoginRequest,
    Platform,
)
from socialchimp.platforms import bluesky as bluesky_module
from socialchimp.platforms.bluesky import (
    BlueskyPlatform,
    bluesky_errors,
    count_graphemes,
    facets_for,
)
from socialchimp.testing import PlatformChecks, RecordingTransport

HOST = "bsky.social"
OTHER = "pds.example"
XRPC = f"https://{HOST}/xrpc"

FIXTURES = Path(__file__).parent / "fixtures" / "bluesky"

MERCHANT_DID = "did:plc:dc7hcchu6gliiknwqdeffydi"
MERCHANT_HANDLE = "fridgedoor.bsky.social"
MERCHANT_POST_URI = f"at://{MERCHANT_DID}/app.bsky.feed.post/cyirmvgfbeyhx"


def fixture(name: str) -> dict[str, Any]:
    """Load one of the real-shape Bluesky fixtures, as fresh JSON.

    A plain `json.loads` rather than a cached object, so a test that
    mutates its copy never leaks into another test - see the fixtures'
    own README for why they are shaped this way.
    """
    loaded: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return loaded


def merchant_account() -> Connection:
    """The merchant account the fixtures were built around."""
    return Connection(
        id=f"bluesky:{MERCHANT_DID}",
        platform="bluesky",
        host=HOST,
        account_id=MERCHANT_DID,
        account_name=f"@{MERCHANT_HANDLE}",
        token=Token(access_token="access-token", refresh_token=REFRESH),
        extra={"handle": MERCHANT_HANDLE},
    )


DID = "did:plc:ada"
HANDLE = "ada.bsky.social"

# One try and no waiting, so the error tests do not spend real seconds asleep.
ONCE = Retries(attempts=1)

POST_URI = f"at://{DID}/app.bsky.feed.post/3kaposted"
CREATED: dict[str, Any] = {"uri": POST_URI, "cid": "bafypost"}

A_NOTIFICATION: dict[str, Any] = {
    "uri": "at://did:plc:bob/app.bsky.feed.like/3kalike",
    "cid": "bafylike",
    "author": {"did": "did:plc:bob", "handle": "bob.bsky.social"},
    "reason": "like",
    "record": {},
    "isRead": False,
    "indexedAt": "2026-08-31T10:00:00.000Z",
}


def jwt_holding(payload: object, *, pieces: int = 3) -> str:
    """Build a token whose middle piece says what we want it to say."""
    written = json.dumps(payload).encode()
    middle = base64.urlsafe_b64encode(written).decode().rstrip("=")
    return ".".join(["headerpart", middle, "signaturepart"][:pieces])


def jwt_expiring_at(when: datetime) -> str:
    """Build an access token that says it runs out at this moment."""
    return jwt_holding({"scope": "com.atproto.access", "exp": int(when.timestamp())})


IN_TWO_HOURS = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
ACCESS = jwt_expiring_at(IN_TWO_HOURS)
REFRESH = "refresh-token-one"

# A family emoji: seven characters, one letter, 25 bytes written out.
FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466"


def a_session(
    *,
    access: str = ACCESS,
    refresh: str = REFRESH,
    **extra: object,
) -> dict[str, Any]:
    """What createSession and refreshSession both answer with."""
    return {
        "did": DID,
        "handle": HANDLE,
        "accessJwt": access,
        "refreshJwt": refresh,
        **extra,
    }


def an_account(
    *,
    host: str | None = HOST,
    access: str = "access-token",
    refresh: str | None = REFRESH,
) -> Connection:
    """A connected Bluesky account."""
    return Connection(
        id=f"bluesky:{DID}",
        platform="bluesky",
        host=host,
        account_id=DID,
        account_name=f"@{HANDLE}",
        token=Token(access_token=access, refresh_token=refresh),
        extra={"handle": HANDLE},
    )


@pytest.fixture
def platform() -> BlueskyPlatform:
    """A platform that gives up after one try."""
    return BlueskyPlatform(retries=ONCE)


@pytest.fixture
def account() -> Connection:
    """A connected account on bsky.social."""
    return an_account()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Freeze the moment a post says it was created."""
    frozen = datetime(2026, 8, 31, 9, 30, tzinfo=UTC)
    monkeypatch.setattr(bluesky_module, "_now", lambda: frozen)
    return frozen


def sent_json(route: respx.Route) -> dict[str, Any]:
    """Read the body of the last request sent to a route."""
    body: dict[str, Any] = json.loads(route.calls.last.request.content)
    return body


def stub_create(network: respx.Router) -> respx.Route:
    """Answer "make me a post" with a post that exists."""
    return network.post("/com.atproto.repo.createRecord").mock(
        return_value=httpx.Response(200, json=CREATED)
    )


async def publish_text(
    platform: BlueskyPlatform,
    account: Connection,
    text: str,
) -> dict[str, Any]:
    """Publish some text and hand back the record that went to the wire."""
    with respx.mock(base_url=XRPC) as network:
        route = stub_create(network)
        await platform.publish(account, Post(text=text))
    record: dict[str, Any] = sent_json(route)["record"]
    return record


# ---------------------------------------------------------------------------
# What it says it can do
# ---------------------------------------------------------------------------


class TestWhatItSaysItCanDo:
    def test_it_provides_everything_a_platform_must(
        self,
        platform: BlueskyPlatform,
    ) -> None:
        checked: Platform = platform
        deletes: CanDeletePosts = platform
        reads: CanReadUpdates = platform

        assert isinstance(checked, Platform)
        assert isinstance(deletes, CanDeletePosts)
        assert isinstance(reads, CanReadUpdates)
        assert platform.name == "bluesky"

    def test_it_lists_the_features_bluesky_really_has(
        self,
        platform: BlueskyPlatform,
    ) -> None:
        for feature in (
            Feature.POST_TEXT,
            Feature.POST_IMAGE,
            Feature.REPLY,
            Feature.DELETE_POST,
            Feature.READ_POSTS,
            # There is no developer portal and no app to register, so
            # socialchimp does not ask for credentials before a sign-in.
            Feature.NEEDS_NO_APP,
        ):
            assert feature in platform.features

    def test_it_does_not_claim_what_bluesky_cannot_do(
        self,
        platform: BlueskyPlatform,
    ) -> None:
        # There is no app to register, no way to ask for a post later, and
        # video is a separate flow we have not written.
        for missing in (
            Feature.CREATE_APP,
            Feature.SCHEDULE,
            Feature.POST_VIDEO,
            Feature.PUSH_UPDATES,
        ):
            assert missing not in platform.features

    def test_its_address_is_the_persons_own_server(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        assert platform.api_base(account) == XRPC
        assert platform.api_base(an_account(host=OTHER)) == f"https://{OTHER}/xrpc"

    def test_it_falls_back_to_bsky_social_when_no_server_is_named(
        self,
        platform: BlueskyPlatform,
    ) -> None:
        # Nearly everybody is on bsky.social, so a connection saved without
        # a server is not an error the way it is on Mastodon.
        assert platform.api_base(an_account(host=None)) == XRPC

    def test_it_signs_requests_with_the_access_token(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        assert platform.auth_headers(account) == {
            "Authorization": "Bearer access-token"
        }

    async def test_its_limits_are_the_same_everywhere(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        # Both text limits are real and a post has to be inside both, and
        # the 300 is letters as a person counts them - not characters.
        assert await platform.limits(account) == Limits(
            max_text_length=300,
            max_text_bytes=3000,
            text_counted_in=TextCount.GRAPHEMES,
            max_images=4,
            max_image_bytes=1_000_000,
        )


# ---------------------------------------------------------------------------
# Signing in with an app password
# ---------------------------------------------------------------------------


class TestAskingForAnAppPassword:
    async def test_it_asks_for_a_handle_and_an_app_password(
        self,
        platform: BlueskyPlatform,
    ) -> None:
        step = await platform.start_login(LoginRequest(redirect_uri="unused"))

        assert isinstance(step, AskForDetails)
        assert [field.name for field in step.fields] == ["handle", "app_password"]
        assert step.help_url is not None
        assert "app-password" in step.help_url

    async def test_the_app_password_field_is_marked_secret(
        self,
        platform: BlueskyPlatform,
    ) -> None:
        step = await platform.start_login(LoginRequest(redirect_uri="unused"))
        assert isinstance(step, AskForDetails)
        handle, password = step.fields

        assert handle.secret is False
        assert password.secret is True
        # The help text has to say this is not their real password, because
        # that is the whole question anyone typing it is asking.
        assert password.help_text is not None
        assert "not your" in password.help_text.lower()

    async def test_starting_a_login_sends_nothing_to_bluesky(
        self,
        platform: BlueskyPlatform,
    ) -> None:
        with respx.mock(assert_all_called=False) as network:
            await platform.start_login(LoginRequest(redirect_uri="unused"))
        assert not network.calls


class TestSigningInWithNothingStored:
    """Bluesky has no app to register, so empty storage is the normal state."""

    async def test_the_getting_started_example_works(self) -> None:
        # This is docs/getting-started.md, line for line. Nothing has been
        # saved with save_app, because on Bluesky there is nothing to save.
        sc = SocialChimp(InMemoryStorage())

        step = await sc.start_login("bluesky", redirect_uri="unused")

        assert isinstance(step, AskForDetails)
        assert [field.name for field in step.fields] == ["handle", "app_password"]

    async def test_the_platform_is_handed_no_credentials(self) -> None:
        # Nothing invented a placeholder on the way through: the request
        # carries `app=None`, which is the truth about Bluesky.
        seen: list[LoginRequest] = []

        class Watched(BlueskyPlatform):
            async def start_login(self, request: LoginRequest) -> AskForDetails:
                seen.append(request)
                return await super().start_login(request)

        sc = SocialChimp(InMemoryStorage(), platforms={"bluesky": Watched()})
        await sc.start_login("bluesky", redirect_uri="unused")

        assert seen[0].app is None

    async def test_finishing_that_login_needs_nothing_stored_either(self) -> None:
        storage = InMemoryStorage()
        sc = SocialChimp(storage)

        with respx.mock(base_url=XRPC) as network:
            network.post("/com.atproto.server.createSession").mock(
                return_value=httpx.Response(200, json=a_session())
            )
            step = await sc.finish_login(
                "bluesky",
                redirect_uri="unused",
                callback={"handle": HANDLE, "app_password": "abcd-efgh"},
            )

        assert isinstance(step, Finished)
        assert await storage.get_connection(step.connection.id) == step.connection


class TestCreatingASession:
    async def test_it_swaps_a_handle_and_app_password_for_tokens(
        self,
        platform: BlueskyPlatform,
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            route = network.post("/com.atproto.server.createSession").mock(
                return_value=httpx.Response(200, json=a_session())
            )

            step = await platform.finish_login(
                LoginRequest(redirect_uri="unused"),
                {"handle": HANDLE, "app_password": "abcd-efgh-ijkl-mnop"},
            )

        assert sent_json(route) == {
            "identifier": HANDLE,
            "password": "abcd-efgh-ijkl-mnop",
        }
        assert isinstance(step, Finished)
        connection = step.connection
        assert connection.platform == "bluesky"
        assert connection.host == HOST
        assert connection.account_id == DID
        assert connection.account_name == f"@{HANDLE}"
        assert connection.token.access_token == ACCESS
        assert connection.token.refresh_token == REFRESH
        assert connection.extra["handle"] == HANDLE

    async def test_it_tidies_up_a_handle_somebody_typed_with_an_at_sign(
        self,
        platform: BlueskyPlatform,
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            route = network.post("/com.atproto.server.createSession").mock(
                return_value=httpx.Response(200, json=a_session())
            )

            await platform.finish_login(
                LoginRequest(redirect_uri="unused"),
                {"handle": " @Ada.BSky.Social ", "app_password": "pw"},
            )

        assert sent_json(route)["identifier"] == HANDLE

    async def test_it_signs_in_on_the_persons_own_server(
        self,
        platform: BlueskyPlatform,
    ) -> None:
        with respx.mock(base_url=f"https://{OTHER}/xrpc") as network:
            route = network.post("/com.atproto.server.createSession").mock(
                return_value=httpx.Response(200, json=a_session())
            )

            step = await platform.finish_login(
                LoginRequest(redirect_uri="unused", host=OTHER),
                {"handle": HANDLE, "app_password": "pw"},
            )

        assert route.called
        assert isinstance(step, Finished)
        assert step.connection.host == OTHER

    @pytest.mark.parametrize(
        "callback",
        [
            {"app_password": "pw"},
            {"handle": HANDLE},
            {"handle": "", "app_password": "pw"},
        ],
    )
    async def test_it_says_which_field_is_missing(
        self,
        platform: BlueskyPlatform,
        callback: dict[str, str],
    ) -> None:
        with pytest.raises(AuthError, match="app password"):
            await platform.finish_login(LoginRequest(redirect_uri="unused"), callback)

    async def test_it_says_so_when_the_reply_has_no_token_in_it(
        self,
        platform: BlueskyPlatform,
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.post("/com.atproto.server.createSession").mock(
                return_value=httpx.Response(200, json={"did": DID, "handle": HANDLE})
            )

            with pytest.raises(PlatformError, match="accessJwt"):
                await platform.finish_login(
                    LoginRequest(redirect_uri="unused"),
                    {"handle": HANDLE, "app_password": "pw"},
                )


# ---------------------------------------------------------------------------
# Reading the expiry out of the access token
# ---------------------------------------------------------------------------


class TestWhenTheTokenRunsOut:
    async def test_it_reads_the_expiry_out_of_the_token_itself(
        self,
        platform: BlueskyPlatform,
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.post("/com.atproto.server.createSession").mock(
                return_value=httpx.Response(200, json=a_session())
            )

            step = await platform.finish_login(
                LoginRequest(redirect_uri="unused"),
                {"handle": HANDLE, "app_password": "pw"},
            )

        assert isinstance(step, Finished)
        assert step.connection.token.expires_at == IN_TWO_HOURS

    @pytest.mark.parametrize(
        "broken",
        [
            "not-a-jwt-at-all",
            "header.$$$$.signature",
            jwt_holding(["not", "an", "object"]),
            jwt_holding({"scope": "com.atproto.access"}),
            jwt_holding({"exp": "half past two"}),
            jwt_holding({"exp": 1}, pieces=2),
        ],
    )
    async def test_a_token_it_cannot_read_expires_almost_at_once(
        self,
        platform: BlueskyPlatform,
        broken: str,
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.post("/com.atproto.server.createSession").mock(
                return_value=httpx.Response(200, json=a_session(access=broken))
            )

            step = await platform.finish_login(
                LoginRequest(redirect_uri="unused"),
                {"handle": HANDLE, "app_password": "pw"},
            )

        assert isinstance(step, Finished)
        expires_at = step.connection.token.expires_at
        assert expires_at is not None
        # Not readable means renew it now rather than trust it for hours.
        assert expires_at <= datetime.now(UTC) + timedelta(minutes=2)


# ---------------------------------------------------------------------------
# Renewing
# ---------------------------------------------------------------------------


class TestRenewingAToken:
    async def test_it_signs_the_renewal_with_the_refresh_token(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            route = network.post("/com.atproto.server.refreshSession").mock(
                return_value=httpx.Response(200, json=a_session())
            )

            await platform.refresh(account)

        # The trap: this one call is signed with the refresh token, not the
        # access token every other call uses.
        headers = route.calls.last.request.headers
        assert headers["authorization"] == f"Bearer {REFRESH}"

    async def test_it_takes_your_apps_credentials_and_ignores_them(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        # Google and Meta sign a renewal with a client id and secret.
        # Bluesky is signed in to with an app password, so there is no
        # registered app and nothing for these to say.
        app = AppCredentials(
            platform="bluesky",
            host=None,
            client_id="client-id",
            client_secret="client-secret",
        )

        with respx.mock(base_url=XRPC) as network:
            route = network.post("/com.atproto.server.refreshSession").mock(
                return_value=httpx.Response(200, json=a_session())
            )

            await platform.refresh(account, app)

        sent = route.calls.last.request
        assert "client-id" not in str(sent.url)
        assert not sent.content

    async def test_both_tokens_are_replaced_every_time(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.post("/com.atproto.server.refreshSession").mock(
                return_value=httpx.Response(
                    200,
                    json=a_session(access="access-two", refresh="refresh-token-two"),
                )
            )

            token = await platform.refresh(account)

        assert token.access_token == "access-two"
        # The old refresh token stopped working the moment this call
        # succeeded, so a caller that does not save this is locked out.
        assert token.refresh_token == "refresh-token-two"
        assert token.refresh_token != account.token.refresh_token

    async def test_it_reads_the_new_expiry_out_of_the_new_token(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        later = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
        with respx.mock(base_url=XRPC) as network:
            network.post("/com.atproto.server.refreshSession").mock(
                return_value=httpx.Response(
                    200, json=a_session(access=jwt_expiring_at(later))
                )
            )

            token = await platform.refresh(account)

        assert token.expires_at == later

    async def test_it_says_to_sign_in_again_when_there_is_no_refresh_token(
        self,
        platform: BlueskyPlatform,
    ) -> None:
        with pytest.raises(TokenExpiredError, match="connect their account again"):
            await platform.refresh(an_account(refresh=None))

    async def test_a_refused_renewal_asks_the_person_to_sign_in_again(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.post("/com.atproto.server.refreshSession").mock(
                return_value=httpx.Response(
                    400, json={"error": "ExpiredToken", "message": "Token has expired"}
                )
            )

            with pytest.raises(TokenExpiredError, match="app password"):
                await platform.refresh(account)

    async def test_it_says_so_when_the_renewal_reply_has_no_token(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.post("/com.atproto.server.refreshSession").mock(
                return_value=httpx.Response(200, json={"did": DID, "accessJwt": ACCESS})
            )

            with pytest.raises(PlatformError, match="refreshJwt"):
                await platform.refresh(account)


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------


class TestPostingText:
    async def test_it_writes_a_post_record_into_the_account(
        self,
        platform: BlueskyPlatform,
        account: Connection,
        clock: datetime,
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            route = stub_create(network)
            result = await platform.publish(account, Post(text="Hello Bluesky"))

        sent = sent_json(route)
        assert sent["repo"] == DID
        assert sent["collection"] == "app.bsky.feed.post"
        assert sent["record"]["$type"] == "app.bsky.feed.post"
        assert sent["record"]["text"] == "Hello Bluesky"
        assert sent["record"]["createdAt"] == clock.isoformat()
        assert "facets" not in sent["record"]
        assert "embed" not in sent["record"]
        assert "reply" not in sent["record"]

        assert result.id == POST_URI
        assert result.state is PostState.DONE
        assert result.url == f"https://bsky.app/profile/{DID}/post/3kaposted"
        assert result.cid == CREATED["cid"]
        assert result.raw == CREATED

    async def test_it_signs_the_post_with_the_access_token(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            route = stub_create(network)
            await platform.publish(account, Post(text="hi"))

        assert route.calls.last.request.headers["authorization"] == (
            "Bearer access-token"
        )

    async def test_it_takes_the_language_from_post_options(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        record = await publish_text(platform, an_account(), "hei")
        assert "langs" not in record

        with respx.mock(base_url=XRPC) as network:
            route = stub_create(network)
            await platform.publish(account, Post(text="hei", options={"langs": "nb"}))
        assert sent_json(route)["record"]["langs"] == ["nb"]

        with respx.mock(base_url=XRPC) as network:
            route = stub_create(network)
            await platform.publish(
                account, Post(text="hei", options={"langs": ["nb", "en"]})
            )
        assert sent_json(route)["record"]["langs"] == ["nb", "en"]

    @pytest.mark.parametrize(
        "options",
        [
            {"visibility": "public"},
            {"langs": 7},
            {"langs": ["en", "fr", "de", "it"]},
            {"langs": []},
        ],
    )
    async def test_it_refuses_options_bluesky_does_not_have(
        self,
        platform: BlueskyPlatform,
        account: Connection,
        options: dict[str, Any],
    ) -> None:
        with (
            respx.mock(assert_all_called=False) as network,
            pytest.raises(InvalidPostError),
        ):
            await platform.publish(account, Post(text="hi", options=options))
        assert not network.calls


class TestMakingLinksWork:
    def test_a_link_gets_a_facet_over_exactly_its_bytes(self) -> None:
        text = "look at https://example.com/a for this"
        found = facets_for(text)

        assert len(found) == 1
        start = found[0]["index"]["byteStart"]
        end = found[0]["index"]["byteEnd"]
        assert text.encode()[start:end].decode() == "https://example.com/a"
        assert found[0]["features"] == [
            {
                "$type": "app.bsky.richtext.facet#link",
                "uri": "https://example.com/a",
            }
        ]

    def test_the_offsets_are_bytes_and_not_characters(self) -> None:
        # The single most common Bluesky bug. Every character before the link
        # here is two bytes, so counting characters puts the facet in the
        # wrong place and the link silently stops being a link.
        text = "café ☕ https://example.com"
        found = facets_for(text)

        start = found[0]["index"]["byteStart"]
        assert start == text.encode().index(b"https://")
        assert start != text.index("https://")
        assert text.encode()[start : found[0]["index"]["byteEnd"]] == (
            b"https://example.com"
        )

    def test_a_link_right_after_a_non_ascii_character_is_still_found(self) -> None:
        text = "→https://example.com"
        found = facets_for(text)

        assert len(found) == 1
        assert found[0]["index"]["byteStart"] == len("→".encode())

    def test_the_full_stop_ending_a_sentence_is_not_part_of_the_link(self) -> None:
        found = facets_for("read https://example.com/page.")

        assert found[0]["features"][0]["uri"] == "https://example.com/page"
        assert found[0]["index"]["byteEnd"] == len("read https://example.com/page")

    def test_it_finds_every_link_in_a_post(self) -> None:
        found = facets_for("https://one.example and http://two.example/x")

        assert [facet["features"][0]["uri"] for facet in found] == [
            "https://one.example",
            "http://two.example/x",
        ]

    def test_text_that_is_not_a_link_is_left_alone(self) -> None:
        assert facets_for("no links here, not even example.com") == []

    def test_an_address_inside_a_link_is_not_found_twice(self) -> None:
        found = facets_for("https://example.com/?to=https://other.example")
        assert len(found) == 1

    async def test_a_posted_link_reaches_bluesky_as_a_facet(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        record = await publish_text(platform, account, "see https://example.com")

        assert record["facets"][0]["index"] == {"byteStart": 4, "byteEnd": 23}


class TestMentioningPeople:
    async def test_a_mention_is_looked_up_and_marked(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            resolve = network.get("/com.atproto.identity.resolveHandle").mock(
                return_value=httpx.Response(200, json={"did": "did:plc:bob"})
            )
            route = stub_create(network)

            await platform.publish(account, Post(text="hi @bob.bsky.social!"))

        assert resolve.calls.last.request.url.params["handle"] == "bob.bsky.social"
        facet = sent_json(route)["record"]["facets"][0]
        assert facet["index"] == {"byteStart": 3, "byteEnd": 19}
        assert facet["features"] == [
            {"$type": "app.bsky.richtext.facet#mention", "did": "did:plc:bob"}
        ]

    async def test_a_handle_nobody_can_find_is_left_as_plain_words(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.get("/com.atproto.identity.resolveHandle").mock(
                return_value=httpx.Response(
                    400,
                    json={
                        "error": "InvalidRequest",
                        "message": "Unable to resolve handle",
                    },
                )
            )
            route = stub_create(network)

            await platform.publish(account, Post(text="hi @gone.example"))

        # One person who has left is not a reason to refuse the whole post.
        assert "facets" not in sent_json(route)["record"]

    async def test_mentions_and_links_are_sorted_by_where_they_appear(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.get("/com.atproto.identity.resolveHandle").mock(
                return_value=httpx.Response(200, json={"did": "did:plc:bob"})
            )
            route = stub_create(network)

            await platform.publish(
                account, Post(text="@bob.bsky.social said https://example.com")
            )

        facets = sent_json(route)["record"]["facets"]
        assert [facet["index"]["byteStart"] for facet in facets] == [0, 22]


class TestPostingPictures:
    async def test_it_uploads_a_picture_and_hangs_it_off_the_post(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        blob = {"$type": "blob", "ref": {"$link": "bafyblob"}, "size": 12}
        picture = Media.from_bytes(
            b"pretend png", filename="cat.png", alt_text="A cat asleep on a keyboard"
        )

        with respx.mock(base_url=XRPC) as network:
            upload = network.post("/com.atproto.repo.uploadBlob").mock(
                return_value=httpx.Response(200, json={"blob": blob})
            )
            route = stub_create(network)

            await platform.publish(account, Post(text="look", media=(picture,)))

        assert upload.calls.last.request.content == b"pretend png"
        assert upload.calls.last.request.headers["content-type"] == "image/png"

        embed = sent_json(route)["record"]["embed"]
        assert embed == {
            "$type": "app.bsky.embed.images",
            "images": [{"alt": "A cat asleep on a keyboard", "image": blob}],
        }

    async def test_a_picture_with_no_description_still_carries_the_field(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        picture = Media.from_bytes(b"png", filename="cat.png")

        with respx.mock(base_url=XRPC) as network:
            network.post("/com.atproto.repo.uploadBlob").mock(
                return_value=httpx.Response(200, json={"blob": {"$type": "blob"}})
            )
            route = stub_create(network)

            await platform.publish(account, Post(media=(picture,)))

        assert sent_json(route)["record"]["embed"]["images"][0]["alt"] == ""

    async def test_it_will_not_fetch_a_picture_from_a_link_for_you(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        picture = Media.from_url("https://example.com/cat.png")

        with (
            respx.mock(assert_all_called=False),
            pytest.raises(InvalidPostError, match=r"Media\.from_bytes"),
        ):
            await platform.publish(account, Post(media=(picture,)))

    async def test_it_says_so_when_the_upload_answers_without_a_blob(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        picture = Media.from_bytes(b"png", filename="cat.png")

        with respx.mock(base_url=XRPC) as network:
            network.post("/com.atproto.repo.uploadBlob").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            with pytest.raises(PlatformError, match="blob"):
                await platform.publish(account, Post(media=(picture,)))

    async def test_five_pictures_are_refused_before_anything_is_uploaded(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        picture = Media.from_bytes(b"png", filename="cat.png")

        with (
            respx.mock(assert_all_called=False) as network,
            pytest.raises(InvalidPostError, match="at most 4"),
        ):
            await platform.publish(account, Post(media=(picture,) * 5))
        assert not network.calls

    async def test_video_is_refused_with_a_message_saying_why(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        clip = Media.from_bytes(b"mp4", filename="clip.mp4")

        with (
            respx.mock(assert_all_called=False) as network,
            pytest.raises(NotSupportedError, match="video"),
        ):
            await platform.publish(account, Post(media=(clip,)))
        assert not network.calls


class TestReplying:
    def parent(self, *, reply: dict[str, Any] | None = None) -> dict[str, Any]:
        record: dict[str, Any] = {"text": "the parent"}
        if reply is not None:
            record["reply"] = reply
        return {
            "uri": "at://did:plc:bob/app.bsky.feed.post/parent",
            "cid": "bafyp",
            "record": record,
        }

    async def test_a_reply_to_a_first_post_makes_that_post_the_root(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        parent = self.parent()

        with respx.mock(base_url=XRPC) as network:
            lookup = network.get("/app.bsky.feed.getPosts").mock(
                return_value=httpx.Response(200, json={"posts": [parent]})
            )
            route = stub_create(network)

            await platform.publish(account, Post(text="agreed", reply_to=parent["uri"]))

        assert lookup.calls.last.request.url.params["uris"] == parent["uri"]
        strong = {"uri": parent["uri"], "cid": "bafyp"}
        assert sent_json(route)["record"]["reply"] == {
            "root": strong,
            "parent": strong,
        }

    async def test_a_reply_to_a_reply_keeps_the_original_root(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        root = {"uri": "at://did:plc:zoe/app.bsky.feed.post/root", "cid": "bafyroot"}
        parent = self.parent(reply={"root": root, "parent": {"uri": "x", "cid": "y"}})

        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPosts").mock(
                return_value=httpx.Response(200, json={"posts": [parent]})
            )
            route = stub_create(network)

            await platform.publish(account, Post(text="agreed", reply_to=parent["uri"]))

        sent = sent_json(route)["record"]["reply"]
        # Bluesky hangs the whole conversation off the first post, so the
        # root is the parent's root and not the parent.
        assert sent["root"] == root
        assert sent["parent"] == {"uri": parent["uri"], "cid": "bafyp"}

    async def test_a_root_bluesky_wrote_oddly_falls_back_to_the_parent(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        parent = self.parent(reply={"root": "at://not-an-object"})

        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPosts").mock(
                return_value=httpx.Response(200, json={"posts": [parent]})
            )
            route = stub_create(network)

            await platform.publish(account, Post(text="agreed", reply_to=parent["uri"]))

        sent = sent_json(route)["record"]["reply"]
        assert sent["root"] == sent["parent"]

    @pytest.mark.parametrize("reply", [{"posts": []}, {"posts": "nonsense"}])
    async def test_replying_to_a_post_that_is_gone_says_so(
        self,
        platform: BlueskyPlatform,
        account: Connection,
        reply: dict[str, Any],
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPosts").mock(
                return_value=httpx.Response(200, json=reply)
            )

            with pytest.raises(InvalidPostError, match="nothing to reply to"):
                await platform.publish(
                    account, Post(text="agreed", reply_to="at://gone/x/y")
                )


class TestWhatBlueskyWillNotTake:
    async def test_a_post_over_three_hundred_letters_never_leaves_the_house(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        with (
            respx.mock(assert_all_called=False) as network,
            pytest.raises(InvalidPostError, match="301"),
        ):
            await platform.publish(account, Post(text="x" * 301))
        assert not network.calls

    async def test_three_hundred_letters_exactly_is_fine(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        record = await publish_text(platform, account, "x" * 300)
        assert len(record["text"]) == 300

    async def test_emoji_families_are_counted_the_way_bluesky_counts_them(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        # Seven code points each, one letter each as far as Bluesky is
        # concerned. Counting characters would refuse a post it accepts.
        record = await publish_text(platform, account, FAMILY * 100)
        assert count_graphemes(record["text"]) == 100

    async def test_a_post_that_is_short_but_heavy_is_refused(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        # 250 letters, but well over 3,000 bytes once written out.
        heavy = FAMILY * 250

        with (
            respx.mock(assert_all_called=False) as network,
            pytest.raises(InvalidPostError, match="bytes"),
        ):
            await platform.publish(account, Post(text=heavy))
        assert not network.calls

    async def test_asking_for_it_later_is_refused_rather_than_posted_now(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        later = Post(text="soon", publish_at=datetime.now(UTC) + timedelta(hours=1))

        with (
            respx.mock(assert_all_called=False) as network,
            pytest.raises(NotSupportedError, match="scheduling"),
        ):
            await platform.publish(account, later)
        assert not network.calls


class TestCountingLetters:
    def test_it_hands_out_the_shared_way_of_counting_letters(self) -> None:
        # The counting itself lives in features.py, because every network
        # that counts this way needs it. It is handed out from here too,
        # since Bluesky is where people meet the problem first.
        assert count_graphemes is shared_count_graphemes
        assert count_graphemes(FAMILY) == 1


# ---------------------------------------------------------------------------
# Deleting
# ---------------------------------------------------------------------------


class TestDeleting:
    async def test_it_deletes_the_record_the_post_lives_in(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            route = network.post("/com.atproto.repo.deleteRecord").mock(
                return_value=httpx.Response(200, json={})
            )

            await platform.delete_post(account, POST_URI)

        assert sent_json(route) == {
            "repo": DID,
            "collection": "app.bsky.feed.post",
            "rkey": "3kaposted",
        }

    async def test_it_also_takes_the_short_id_on_its_own(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            route = network.post("/com.atproto.repo.deleteRecord").mock(
                return_value=httpx.Response(200, json={})
            )

            await platform.delete_post(account, "3kaposted")

        assert sent_json(route)["rkey"] == "3kaposted"


# ---------------------------------------------------------------------------
# Reading what has happened
# ---------------------------------------------------------------------------


def notification(
    reason: str, *, at: str = "2026-08-31T10:00:00.000Z"
) -> dict[str, Any]:
    """One notification, as Bluesky writes them."""
    return {
        **A_NOTIFICATION,
        "uri": f"at://did:plc:bob/x/{reason}",
        "reason": reason,
        "indexedAt": at,
    }


class TestReadingUpdates:
    async def test_it_asks_for_a_page_of_notifications(
        self,
        account: Connection,
    ) -> None:
        platform = BlueskyPlatform(retries=ONCE, updates_per_check=10)

        with respx.mock(base_url=XRPC) as network:
            route = network.get("/app.bsky.notification.listNotifications").mock(
                return_value=httpx.Response(200, json={"notifications": []})
            )

            assert await platform.fetch_updates(account, None) == []

        assert route.calls.last.request.url.params["limit"] == "10"

    @pytest.mark.parametrize(
        ("reason", "kind"),
        [
            ("like", UpdateKind.REACTION_ADDED),
            ("repost", UpdateKind.REPOST_ADDED),
            ("reply", UpdateKind.COMMENT_CREATED),
            ("mention", UpdateKind.MENTION),
            ("quote", UpdateKind.MENTION),
            ("follow", UpdateKind.FOLLOWED),
        ],
    )
    async def test_it_says_what_happened_in_socialchimps_own_words(
        self,
        platform: BlueskyPlatform,
        account: Connection,
        reason: str,
        kind: UpdateKind,
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.notification.listNotifications").mock(
                return_value=httpx.Response(
                    200, json={"notifications": [notification(reason)]}
                )
            )

            found = await platform.fetch_updates(account, None)

        assert len(found) == 1
        assert found[0].kind is kind
        assert found[0].platform == "bluesky"
        assert found[0].connection_id == account.id
        assert found[0].created_at == datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
        assert found[0].raw["reason"] == reason

    async def test_a_kind_we_have_no_name_for_keeps_blueskys_word(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.notification.listNotifications").mock(
                return_value=httpx.Response(
                    200, json={"notifications": [notification("starterpack-joined")]}
                )
            )

            found = await platform.fetch_updates(account, None)

        assert found[0].kind is UpdateKind.UNKNOWN
        assert found[0].kind_name == "starterpack-joined"

    async def test_it_hands_them_back_oldest_first(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.notification.listNotifications").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "notifications": [
                            notification("like", at="2026-08-31T12:00:00Z"),
                            notification("reply", at="2026-08-31T11:00:00Z"),
                        ]
                    },
                )
            )

            found = await platform.fetch_updates(account, None)

        assert [update.kind_name for update in found] == [
            "comment_created",
            "reaction_added",
        ]

    async def test_it_leaves_out_anything_older_than_the_marker(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.notification.listNotifications").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "notifications": [
                            notification("like", at="2026-08-31T12:00:00Z"),
                            notification("reply", at="2026-08-30T11:00:00Z"),
                        ]
                    },
                )
            )

            found = await platform.fetch_updates(
                account, datetime(2026, 8, 31, tzinfo=UTC)
            )

        assert [update.kind_name for update in found] == ["reaction_added"]

    @pytest.mark.parametrize(
        "reply",
        [
            {"notifications": [{"reason": "like", "indexedAt": "not a time"}]},
            {"notifications": "nonsense"},
            {"notifications": ["not an object"]},
            {},
        ],
    )
    async def test_it_skips_anything_it_cannot_read(
        self,
        platform: BlueskyPlatform,
        account: Connection,
        reply: dict[str, Any],
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.notification.listNotifications").mock(
                return_value=httpx.Response(200, json=reply)
            )

            assert await platform.fetch_updates(account, None) == []


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def refusal(status: int, error: str | None = None) -> httpx.Response:
    """A reply Bluesky would send when it is unhappy."""
    body = {"error": error, "message": f"{error} happened"} if error else {}
    return httpx.Response(status, json=body)


class TestErrors:
    def test_a_bad_request_about_a_post_is_a_post_problem(self) -> None:
        error = bluesky_errors(refusal(400, "InvalidRequest"))

        assert isinstance(error, InvalidPostError)
        assert "InvalidRequest happened" in str(error)
        assert error.raw["error"] == "InvalidRequest"

    def test_a_picture_that_is_too_big_says_to_shrink_it(self) -> None:
        error = bluesky_errors(refusal(400, "BlobTooLarge"))

        assert isinstance(error, InvalidPostError)
        assert "smaller" in str(error)

    @pytest.mark.parametrize("named", ["ExpiredToken", "InvalidToken"])
    def test_a_token_bluesky_will_not_take_is_a_sign_in_problem(
        self,
        named: str,
    ) -> None:
        # Bluesky answers 400 rather than 401 for a token that has run out,
        # which is the sort of thing that sends people hunting.
        error = bluesky_errors(refusal(400, named))

        assert isinstance(error, AuthError)

    def test_a_refused_sign_in_is_an_auth_error(self) -> None:
        assert isinstance(bluesky_errors(refusal(401, "AuthMissing")), AuthError)

    def test_being_asked_to_slow_down_is_a_rate_limit(self) -> None:
        error = bluesky_errors(refusal(429, "RateLimitExceeded"))

        assert isinstance(error, RateLimitError)

    def test_anything_else_falls_through_to_the_shared_mapping(self) -> None:
        error = bluesky_errors(refusal(500))

        assert isinstance(error, PlatformError)
        assert error.platform == "bluesky"

    def test_a_plain_bad_request_is_still_a_bad_request(self) -> None:
        error = bluesky_errors(refusal(400))

        assert isinstance(error, PlatformError)

    def test_a_gone_post_is_a_post_gone_error(self) -> None:
        error = bluesky_errors(refusal(400, "NotFound"))

        assert isinstance(error, PostGoneError)
        assert "NotFound happened" in str(error)

    @pytest.mark.parametrize("named", ["BlockedActor", "BlockedByActor"])
    def test_a_block_between_the_two_accounts_is_a_blocked_error(
        self,
        named: str,
    ) -> None:
        error = bluesky_errors(refusal(400, named))

        assert isinstance(error, BlockedError)

    async def test_a_refusal_from_the_wire_arrives_as_a_socialchimp_error(
        self,
        platform: BlueskyPlatform,
        account: Connection,
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.post("/com.atproto.repo.createRecord").mock(
                return_value=refusal(400, "InvalidRequest")
            )

            with pytest.raises(InvalidPostError):
                await platform.publish(account, Post(text="hi"))


# ---------------------------------------------------------------------------
# Reading one post back in full
# ---------------------------------------------------------------------------


class TestReadingOnePost:
    async def test_it_says_it_can_read_a_post(self, platform: BlueskyPlatform) -> None:
        assert Feature.READ_POST in platform.features
        assert isinstance(platform, CanReadPost)

    async def test_it_reads_a_post_back_in_full(
        self, platform: BlueskyPlatform
    ) -> None:
        account = merchant_account()
        raw = fixture("get_posts_own")

        with respx.mock(base_url=XRPC) as network:
            route = network.get("/app.bsky.feed.getPosts").mock(
                return_value=httpx.Response(200, json=raw)
            )
            details = await platform.read_post(account, MERCHANT_POST_URI)

        assert route.calls.last.request.url.params["uris"] == MERCHANT_POST_URI
        raw_post = raw["posts"][0]
        assert details.id == MERCHANT_POST_URI
        assert details.cid == raw_post["cid"]
        assert details.url == (
            f"https://bsky.app/profile/{MERCHANT_HANDLE}/post/cyirmvgfbeyhx"
        )
        assert details.author is not None
        assert details.author.id == MERCHANT_DID
        assert details.author.handle == MERCHANT_HANDLE
        assert details.author.display_name == "Fridge Door Parts"
        assert details.author.url == f"https://bsky.app/profile/{MERCHANT_HANDLE}"
        assert details.html is None
        assert details.visibility is None
        assert details.parent_id is None
        assert details.root_id == details.id
        assert details.reply_count == 5
        assert details.like_count == 342
        assert details.repost_count == 58
        assert details.quote_count == 12
        assert details.liked_by_me is False
        assert details.my_like_id is None
        assert details.is_mine is True
        assert details.unavailable is None
        assert details.created_at == datetime(
            2026, 9, 20, 14, 32, 7, 481000, tzinfo=UTC
        )

        by_kind = {link.kind: link for link in details.links}
        mention = by_kind[LinkKind.MENTION]
        assert details.text[mention.start : mention.end] == (
            "@coldchainparts.bsky.social"
        )
        assert mention.target == "did:plc:gtfxuqrljnk2c2gez6fi3qzj"
        assert mention.url is None

        link = by_kind[LinkKind.LINK]
        assert details.text[link.start : link.end] == (
            "https://fridgedoor.example/inventory"
        )
        assert link.target == "https://fridgedoor.example/inventory"
        assert link.url == link.target

        tag = by_kind[LinkKind.TAG]
        assert details.text[tag.start : tag.end] == "#FridgeParts"
        assert tag.target == "FridgeParts"
        assert tag.url is None

        assert len(details.attachments) == 2
        first_image = raw_post["embed"]["images"][0]
        assert details.attachments[0].kind == "image"
        assert details.attachments[0].url == first_image["fullsize"]
        assert details.attachments[0].preview_url == first_image["thumb"]
        assert details.attachments[0].alt_text == first_image["alt"]
        assert details.attachments[0].width == 1600
        assert details.attachments[0].height == 1200

    async def test_it_knows_when_it_already_liked_its_own_post(
        self, platform: BlueskyPlatform
    ) -> None:
        raw = fixture("get_posts_liked")
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPosts").mock(
                return_value=httpx.Response(200, json=raw)
            )
            details = await platform.read_post(merchant_account(), MERCHANT_POST_URI)

        assert details.liked_by_me is True
        assert details.my_like_id == raw["posts"][0]["viewer"]["like"]

    async def test_a_post_with_no_viewer_state_has_an_unknown_like(
        self, platform: BlueskyPlatform
    ) -> None:
        raw = copy.deepcopy(fixture("get_posts_own"))
        del raw["posts"][0]["viewer"]
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPosts").mock(
                return_value=httpx.Response(200, json=raw)
            )
            details = await platform.read_post(merchant_account(), MERCHANT_POST_URI)

        assert details.liked_by_me is None
        assert details.my_like_id is None

    async def test_a_post_from_someone_else_is_not_mine(
        self, platform: BlueskyPlatform
    ) -> None:
        raw = copy.deepcopy(fixture("get_posts_own"))
        raw["posts"][0]["author"]["did"] = "did:plc:someoneelse"
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPosts").mock(
                return_value=httpx.Response(200, json=raw)
            )
            details = await platform.read_post(merchant_account(), MERCHANT_POST_URI)

        assert details.is_mine is False

    async def test_a_post_with_no_record_reads_as_empty(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        raw = {"posts": [{"uri": "at://x/y/z"}]}
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPosts").mock(
                return_value=httpx.Response(200, json=raw)
            )
            details = await platform.read_post(account, "at://x/y/z")

        assert details.text == ""
        assert details.cid is None
        assert details.created_at is None
        assert details.author is None
        assert details.is_mine is False
        assert details.url is None
        assert details.parent_id is None
        assert details.root_id == details.id

    async def test_a_post_with_a_record_but_no_created_at(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        raw = {"posts": [{"uri": "at://x/y/z", "record": {"text": "hi"}}]}
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPosts").mock(
                return_value=httpx.Response(200, json=raw)
            )
            details = await platform.read_post(account, "at://x/y/z")

        assert details.text == "hi"
        assert details.created_at is None

    async def test_reading_a_post_that_is_gone_says_so(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPosts").mock(
                return_value=httpx.Response(200, json={"posts": []})
            )
            with pytest.raises(PostGoneError):
                await platform.read_post(account, "at://gone/x/y")


# ---------------------------------------------------------------------------
# Reading a thread
# ---------------------------------------------------------------------------


class TestReadingAThread:
    async def test_it_says_it_can_read_a_thread(
        self, platform: BlueskyPlatform
    ) -> None:
        assert Feature.READ_THREAD in platform.features
        assert isinstance(platform, CanReadThread)

    async def test_default_depth_is_six(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        raw = fixture("get_post_thread")
        with respx.mock(base_url=XRPC) as network:
            route = network.get("/app.bsky.feed.getPostThread").mock(
                return_value=httpx.Response(200, json=raw)
            )
            await platform.read_thread(account, MERCHANT_POST_URI)

        params = route.calls.last.request.url.params
        assert params["uri"] == MERCHANT_POST_URI
        assert params["depth"] == "6"
        assert params["parentHeight"] == "0"

    async def test_a_given_depth_is_passed_through(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        raw = fixture("get_post_thread")
        with respx.mock(base_url=XRPC) as network:
            route = network.get("/app.bsky.feed.getPostThread").mock(
                return_value=httpx.Response(200, json=raw)
            )
            await platform.read_thread(account, MERCHANT_POST_URI, depth=2)

        assert route.calls.last.request.url.params["depth"] == "2"

    async def test_depth_is_capped_at_a_thousand(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        raw = fixture("get_post_thread")
        with respx.mock(base_url=XRPC) as network:
            route = network.get("/app.bsky.feed.getPostThread").mock(
                return_value=httpx.Response(200, json=raw)
            )
            await platform.read_thread(account, MERCHANT_POST_URI, depth=5000)

        assert route.calls.last.request.url.params["depth"] == "1000"

    async def test_it_flattens_the_thread_oldest_first_with_parent_ids(
        self, platform: BlueskyPlatform
    ) -> None:
        account = merchant_account()
        raw = fixture("get_post_thread")
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPostThread").mock(
                return_value=httpx.Response(200, json=raw)
            )
            thread = await platform.read_thread(account, MERCHANT_POST_URI)

        assert thread.post.id == MERCHANT_POST_URI
        assert thread.complete is True
        assert len(thread.replies) == 7
        assert [reply.unavailable for reply in thread.replies] == [
            None,
            None,
            None,
            None,
            None,
            Unavailable.DELETED,
            Unavailable.BLOCKED,
        ]
        handles = [
            reply.author.handle if reply.author is not None else None
            for reply in thread.replies
        ]
        assert handles == [
            "gasketguy.bsky.social",
            "fridgetechie.bsky.social",
            "shopfloorsam.bsky.social",
            "coldroomcarla.bsky.social",
            MERCHANT_HANDLE,
            None,
            None,
        ]
        assert thread.replies[0].parent_id == thread.post.id
        assert thread.replies[1].parent_id == thread.post.id
        assert thread.replies[2].parent_id == thread.replies[1].id
        assert thread.replies[3].parent_id == thread.replies[1].id
        assert thread.replies[4].parent_id == thread.post.id
        assert thread.replies[5].parent_id == thread.post.id
        assert thread.replies[6].parent_id == thread.post.id
        assert all(reply.root_id == thread.post.id for reply in thread.replies)

    async def test_placeholders_carry_no_author_or_text(
        self, platform: BlueskyPlatform
    ) -> None:
        raw = fixture("get_post_thread")
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPostThread").mock(
                return_value=httpx.Response(200, json=raw)
            )
            thread = await platform.read_thread(merchant_account(), MERCHANT_POST_URI)

        deleted = next(
            reply
            for reply in thread.replies
            if reply.unavailable is Unavailable.DELETED
        )
        blocked = next(
            reply
            for reply in thread.replies
            if reply.unavailable is Unavailable.BLOCKED
        )
        assert deleted.author is None
        assert deleted.text == ""
        assert deleted.cid is None
        assert blocked.author is None
        assert blocked.text == ""

    async def test_a_limit_truncates_oldest_first_and_marks_it_incomplete(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        raw = fixture("get_post_thread")
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPostThread").mock(
                return_value=httpx.Response(200, json=raw)
            )
            thread = await platform.read_thread(account, MERCHANT_POST_URI, limit=3)

        assert len(thread.replies) == 3
        assert thread.complete is False

    async def test_a_depth_cut_marks_the_thread_incomplete(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        raw = copy.deepcopy(fixture("get_post_thread"))
        leaf = raw["thread"]["replies"][1]["replies"][0]["post"]
        leaf["replyCount"] = 2
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPostThread").mock(
                return_value=httpx.Response(200, json=raw)
            )
            thread = await platform.read_thread(account, MERCHANT_POST_URI)

        assert thread.complete is False

    async def test_no_replies_and_no_reply_count_is_complete(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        raw = copy.deepcopy(fixture("get_post_thread"))
        del raw["thread"]["replies"]
        raw["thread"]["post"]["replyCount"] = 0
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPostThread").mock(
                return_value=httpx.Response(200, json=raw)
            )
            thread = await platform.read_thread(account, MERCHANT_POST_URI)

        assert thread.replies == ()
        assert thread.complete is True

    async def test_the_anchors_own_reply_count_can_mark_it_incomplete_too(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        raw = copy.deepcopy(fixture("get_post_thread"))
        del raw["thread"]["replies"]
        raw["thread"]["post"]["replyCount"] = 5
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPostThread").mock(
                return_value=httpx.Response(200, json=raw)
            )
            thread = await platform.read_thread(account, MERCHANT_POST_URI)

        assert thread.complete is False

    async def test_a_non_dict_reply_entry_is_skipped(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        raw = copy.deepcopy(fixture("get_post_thread"))
        raw["thread"]["replies"].append("nonsense")
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPostThread").mock(
                return_value=httpx.Response(200, json=raw)
            )
            thread = await platform.read_thread(account, MERCHANT_POST_URI)

        assert len(thread.replies) == 7

    async def test_a_non_dict_nested_reply_entry_is_skipped(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        raw = copy.deepcopy(fixture("get_post_thread"))
        raw["thread"]["replies"][1]["replies"].append("nonsense")
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPostThread").mock(
                return_value=httpx.Response(200, json=raw)
            )
            thread = await platform.read_thread(account, MERCHANT_POST_URI)

        assert len(thread.replies) == 7

    async def test_a_reply_node_missing_its_own_post_is_skipped(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        raw = copy.deepcopy(fixture("get_post_thread"))
        raw["thread"]["replies"] = [{"$type": "app.bsky.feed.defs#threadViewPost"}]
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPostThread").mock(
                return_value=httpx.Response(200, json=raw)
            )
            thread = await platform.read_thread(account, MERCHANT_POST_URI)

        assert thread.replies == ()
        assert thread.complete is True

    async def test_the_anchor_itself_being_gone_is_a_post_gone_error(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPostThread").mock(
                return_value=httpx.Response(
                    400,
                    json={"error": "NotFound", "message": "Post not found: x"},
                )
            )
            with pytest.raises(PostGoneError):
                await platform.read_thread(account, "at://gone/x/y")

    async def test_a_reply_missing_entirely_is_a_platform_error(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPostThread").mock(
                return_value=httpx.Response(200, json={})
            )
            with pytest.raises(PlatformError, match="thread"):
                await platform.read_thread(account, MERCHANT_POST_URI)

    async def test_the_anchor_post_missing_from_the_thread_is_a_platform_error(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPostThread").mock(
                return_value=httpx.Response(200, json={"thread": {}})
            )
            with pytest.raises(PlatformError, match="post"):
                await platform.read_thread(account, MERCHANT_POST_URI)


# ---------------------------------------------------------------------------
# Replying, the recommended way
# ---------------------------------------------------------------------------


class TestReplyingViaReply:
    async def test_it_says_it_can_reply_to_comments(
        self, platform: BlueskyPlatform
    ) -> None:
        assert Feature.REPLY_TO_COMMENTS in platform.features
        assert isinstance(platform, CanReply)

    async def test_it_reuses_publish_and_fills_the_cid(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        parent: dict[str, Any] = {
            "uri": "at://did:plc:bob/app.bsky.feed.post/parent",
            "cid": "bafyp",
            "record": {"text": "the parent"},
        }
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPosts").mock(
                return_value=httpx.Response(200, json={"posts": [parent]})
            )
            route = stub_create(network)
            result = await platform.reply(account, parent["uri"], "agreed")

        sent = sent_json(route)["record"]
        assert sent["reply"]["parent"]["uri"] == parent["uri"]
        assert sent["text"] == "agreed"
        assert result.id == POST_URI
        assert result.cid == CREATED["cid"]

    async def test_options_are_passed_through_to_publish(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        parent: dict[str, Any] = {
            "uri": "at://did:plc:bob/app.bsky.feed.post/parent",
            "cid": "bafyp",
            "record": {"text": "the parent"},
        }
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPosts").mock(
                return_value=httpx.Response(200, json={"posts": [parent]})
            )
            route = stub_create(network)
            await platform.reply(account, parent["uri"], "hei", options={"langs": "nb"})

        assert sent_json(route)["record"]["langs"] == ["nb"]


# ---------------------------------------------------------------------------
# Liking, unliking, reading likes
# ---------------------------------------------------------------------------


class TestLikingAndUnliking:
    async def test_it_says_it_can_like(self, platform: BlueskyPlatform) -> None:
        assert Feature.LIKE in platform.features
        assert isinstance(platform, CanLike)

    async def test_liking_a_post_for_the_first_time(
        self, platform: BlueskyPlatform
    ) -> None:
        account = merchant_account()
        posts_reply = fixture("get_posts_own")
        like_reply = fixture("create_record_like")
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPosts").mock(
                return_value=httpx.Response(200, json=posts_reply)
            )
            create = network.post("/com.atproto.repo.createRecord").mock(
                return_value=httpx.Response(200, json=like_reply)
            )
            result = await platform.like(account, MERCHANT_POST_URI)

        body = sent_json(create)
        assert body["collection"] == "app.bsky.feed.like"
        assert body["record"]["subject"] == {
            "uri": MERCHANT_POST_URI,
            "cid": posts_reply["posts"][0]["cid"],
        }
        assert result.post_id == MERCHANT_POST_URI
        assert result.like_id == like_reply["uri"]

    async def test_liking_an_already_liked_post_is_idempotent(
        self, platform: BlueskyPlatform
    ) -> None:
        account = merchant_account()
        raw = fixture("get_posts_liked")
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPosts").mock(
                return_value=httpx.Response(200, json=raw)
            )
            result = await platform.like(account, MERCHANT_POST_URI)

        # No createRecord route was registered above - if the code had
        # called it anyway, respx would have raised for an unmocked request.
        assert result.like_id == raw["posts"][0]["viewer"]["like"]

    async def test_liking_a_post_that_is_gone(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPosts").mock(
                return_value=httpx.Response(200, json={"posts": []})
            )
            with pytest.raises(PostGoneError):
                await platform.like(account, "at://gone/x/y")

    async def test_unlike_with_a_like_id_skips_the_lookup(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            route = network.post("/com.atproto.repo.deleteRecord").mock(
                return_value=httpx.Response(200, json={})
            )
            await platform.unlike(
                account,
                MERCHANT_POST_URI,
                like_id="at://x/app.bsky.feed.like/therkey",
            )

        # No getPosts route was registered - unlike must not have looked
        # anything up first.
        assert sent_json(route)["rkey"] == "therkey"

    async def test_unlike_without_a_like_id_looks_it_up_first(
        self, platform: BlueskyPlatform
    ) -> None:
        account = merchant_account()
        raw = fixture("get_posts_liked")
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPosts").mock(
                return_value=httpx.Response(200, json=raw)
            )
            route = network.post("/com.atproto.repo.deleteRecord").mock(
                return_value=httpx.Response(200, json={})
            )
            await platform.unlike(account, MERCHANT_POST_URI)

        expected_rkey = raw["posts"][0]["viewer"]["like"].rsplit("/", 1)[-1]
        assert sent_json(route)["rkey"] == expected_rkey

    async def test_unliking_something_never_liked_is_a_no_op(
        self, platform: BlueskyPlatform
    ) -> None:
        account = merchant_account()
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPosts").mock(
                return_value=httpx.Response(200, json=fixture("get_posts_own"))
            )
            await platform.unlike(account, MERCHANT_POST_URI)
        # No deleteRecord route was registered - if it had been called,
        # respx would have raised for an unmocked request.

    async def test_unliking_a_gone_post_is_a_no_op(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPosts").mock(
                return_value=httpx.Response(200, json={"posts": []})
            )
            await platform.unlike(account, "at://gone/x/y")


class TestReadingLikes:
    async def test_it_says_it_can_read_likes(self, platform: BlueskyPlatform) -> None:
        assert Feature.READ_LIKES in platform.features
        assert isinstance(platform, CanReadLikes)

    async def test_it_reads_who_liked_a_post(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        raw = fixture("get_likes")
        with respx.mock(base_url=XRPC) as network:
            route = network.get("/app.bsky.feed.getLikes").mock(
                return_value=httpx.Response(200, json=raw)
            )
            page = await platform.read_likes(account, MERCHANT_POST_URI)

        assert route.calls.last.request.url.params["uri"] == MERCHANT_POST_URI
        assert len(page.items) == 3
        assert page.items[0].person.handle == "freezerfranny.bsky.social"
        assert page.items[0].liked_at == datetime(
            2026, 9, 23, 10, 13, 35, 141000, tzinfo=UTC
        )
        assert page.next == raw["cursor"]

    async def test_it_passes_after_and_limit_through(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            route = network.get("/app.bsky.feed.getLikes").mock(
                return_value=httpx.Response(200, json={"likes": []})
            )
            await platform.read_likes(
                account, MERCHANT_POST_URI, after="cursor-1", limit=500
            )

        params = route.calls.last.request.url.params
        assert params["cursor"] == "cursor-1"
        assert params["limit"] == "100"

    async def test_it_skips_entries_it_cannot_read(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        raw = {
            "likes": [
                "nonsense",
                {"actor": {"handle": "no-did"}},
                {"actor": {"did": "did:plc:x"}, "createdAt": "not a time"},
            ]
        }
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getLikes").mock(
                return_value=httpx.Response(200, json=raw)
            )
            page = await platform.read_likes(account, MERCHANT_POST_URI)

        assert len(page.items) == 1
        assert page.items[0].liked_at is None

    async def test_no_likes_key_is_an_empty_page(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getLikes").mock(
                return_value=httpx.Response(200, json={})
            )
            page = await platform.read_likes(account, MERCHANT_POST_URI)

        assert page.items == ()
        assert page.next is None


# ---------------------------------------------------------------------------
# Updates: the enriched fields, shared by fetch_updates and
# fetch_updates_after
# ---------------------------------------------------------------------------


def notifications_by_reason() -> dict[str, dict[str, Any]]:
    """Every real-shape notification, keyed by its own `reason`."""
    return {
        raw["reason"]: raw for raw in fixture("list_notifications")["notifications"]
    }


class TestUpdateEnrichment:
    @pytest.mark.parametrize(
        "reason", ["like", "repost", "reply", "mention", "quote", "follow"]
    )
    def test_it_says_who_did_it(self, reason: str) -> None:
        raw = notifications_by_reason()[reason]
        update = bluesky_module._update_from(raw, connection=merchant_account())

        assert update is not None
        assert update.actor is not None
        assert update.actor.id == raw["author"]["did"]
        assert update.actor.handle == raw["author"]["handle"]

    def test_a_like_names_the_post_it_concerns_but_not_itself(self) -> None:
        raw = notifications_by_reason()["like"]
        update = bluesky_module._update_from(raw, connection=merchant_account())

        assert update is not None
        assert update.kind is UpdateKind.REACTION_ADDED
        assert update.post_id is None
        assert update.about_post_id == raw["reasonSubject"]
        assert update.thread_root_id is None

    def test_a_repost_names_the_post_it_concerns_but_not_itself(self) -> None:
        raw = notifications_by_reason()["repost"]
        update = bluesky_module._update_from(raw, connection=merchant_account())

        assert update is not None
        assert update.kind is UpdateKind.REPOST_ADDED
        assert update.post_id is None
        assert update.about_post_id == raw["reasonSubject"]

    def test_a_reply_names_itself_its_parent_and_its_root(self) -> None:
        raw = notifications_by_reason()["reply"]
        update = bluesky_module._update_from(raw, connection=merchant_account())

        assert update is not None
        assert update.kind is UpdateKind.COMMENT_CREATED
        assert update.post_id == raw["uri"]
        assert update.about_post_id == raw["record"]["reply"]["parent"]["uri"]
        assert update.thread_root_id == raw["record"]["reply"]["root"]["uri"]

    def test_a_mention_with_no_reply_field_names_only_itself(self) -> None:
        raw = notifications_by_reason()["mention"]
        update = bluesky_module._update_from(raw, connection=merchant_account())

        assert update is not None
        assert update.kind is UpdateKind.MENTION
        assert update.post_id == raw["uri"]
        assert update.about_post_id is None
        assert update.thread_root_id is None

    def test_a_quote_is_folded_into_mention_and_names_itself(self) -> None:
        raw = notifications_by_reason()["quote"]
        update = bluesky_module._update_from(raw, connection=merchant_account())

        assert update is not None
        assert update.kind is UpdateKind.MENTION
        assert update.kind_name == "mention"
        assert update.post_id == raw["uri"]

    def test_a_follow_names_nothing_about_a_post(self) -> None:
        raw = notifications_by_reason()["follow"]
        update = bluesky_module._update_from(raw, connection=merchant_account())

        assert update is not None
        assert update.kind is UpdateKind.FOLLOWED
        assert update.post_id is None
        assert update.about_post_id is None
        assert update.thread_root_id is None

    def test_an_unreadable_indexed_at_drops_the_notification(self) -> None:
        raw = {"reason": "like", "indexedAt": "not a time", "uri": "x"}
        assert bluesky_module._update_from(raw, connection=merchant_account()) is None

    def test_an_author_with_no_did_has_no_actor(self) -> None:
        raw = {
            "reason": "follow",
            "indexedAt": "2026-01-01T00:00:00Z",
            "uri": "x",
            "author": {"handle": "no-did"},
        }
        update = bluesky_module._update_from(raw, connection=merchant_account())

        assert update is not None
        assert update.actor is None

    def test_a_missing_author_has_no_actor(self) -> None:
        raw = {"reason": "follow", "indexedAt": "2026-01-01T00:00:00Z", "uri": "x"}
        update = bluesky_module._update_from(raw, connection=merchant_account())

        assert update is not None
        assert update.actor is None

    async def test_fetch_updates_carries_the_new_fields_too(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        raw = fixture("list_notifications")
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.notification.listNotifications").mock(
                return_value=httpx.Response(200, json=raw)
            )
            found = await platform.fetch_updates(account, None)

        reply_update = next(u for u in found if u.kind is UpdateKind.COMMENT_CREATED)
        assert reply_update.actor is not None
        assert reply_update.post_id is not None
        assert reply_update.thread_root_id is not None


# ---------------------------------------------------------------------------
# Polling with a resumable marker
# ---------------------------------------------------------------------------


class TestFetchUpdatesAfterAMarker:
    async def test_it_says_it_can_be_asked_this_way(
        self, platform: BlueskyPlatform
    ) -> None:
        assert Feature.READ_UPDATES_AFTER in platform.features
        assert isinstance(platform, CanReadUpdatesAfter)

    async def test_no_marker_returns_the_latest_page_oldest_first(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        items = [
            notification("like", at="2026-08-31T12:00:00Z"),
            notification("reply", at="2026-08-31T11:00:00Z"),
        ]
        with respx.mock(base_url=XRPC) as network:
            route = network.get("/app.bsky.notification.listNotifications").mock(
                return_value=httpx.Response(200, json={"notifications": items})
            )
            batch = await platform.fetch_updates_after(account, None)

        assert route.calls.call_count == 1
        assert [update.kind_name for update in batch.updates] == [
            "comment_created",
            "reaction_added",
        ]
        assert batch.more is False
        assert batch.marker == "2026-08-31T12:00:00Z::at://did:plc:bob/x/like"

    async def test_a_marker_found_on_the_first_page_stops_pagination(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        items = [
            notification("like", at="2026-08-31T14:00:00Z"),
            notification("repost", at="2026-08-31T13:00:00Z"),
            notification("reply", at="2026-08-31T12:00:00Z"),
        ]
        marker = "2026-08-31T12:00:00Z::at://did:plc:bob/x/reply"
        with respx.mock(base_url=XRPC) as network:
            route = network.get("/app.bsky.notification.listNotifications").mock(
                return_value=httpx.Response(200, json={"notifications": items})
            )
            batch = await platform.fetch_updates_after(account, marker)

        assert route.calls.call_count == 1
        assert [update.kind_name for update in batch.updates] == [
            "repost_added",
            "reaction_added",
        ]
        assert batch.marker == "2026-08-31T14:00:00Z::at://did:plc:bob/x/like"
        assert batch.more is False

    async def test_it_pages_back_until_it_finds_the_marker(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        page_one = [notification("like", at="2026-08-31T14:00:00Z")]
        page_two = [notification("reply", at="2026-08-31T10:00:00Z")]
        marker = "2026-08-31T10:00:00Z::at://did:plc:bob/x/reply"
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            if calls["count"] == 1:
                return httpx.Response(
                    200,
                    json={"notifications": page_one, "cursor": "page-two"},
                )
            return httpx.Response(200, json={"notifications": page_two})

        with respx.mock(base_url=XRPC) as network:
            route = network.get("/app.bsky.notification.listNotifications").mock(
                side_effect=handler
            )
            batch = await platform.fetch_updates_after(account, marker)

        assert route.calls.call_count == 2
        assert route.calls[1].request.url.params["cursor"] == "page-two"
        assert [update.kind_name for update in batch.updates] == ["reaction_added"]
        assert batch.marker == "2026-08-31T14:00:00Z::at://did:plc:bob/x/like"
        assert batch.more is False

    async def test_running_out_of_pages_before_the_marker_still_answers(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        page_one = [notification("like", at="2026-08-31T14:00:00Z")]
        marker = "2020-01-01T00:00:00Z::at://did:plc:bob/x/long-gone"
        with respx.mock(base_url=XRPC) as network:
            route = network.get("/app.bsky.notification.listNotifications").mock(
                return_value=httpx.Response(200, json={"notifications": page_one})
            )
            batch = await platform.fetch_updates_after(account, marker)

        assert route.calls.call_count == 1
        assert batch.more is False
        assert [update.kind_name for update in batch.updates] == ["reaction_added"]

    async def test_a_marker_that_never_turns_up_hits_the_page_cap(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params.get("cursor", "0"))
            return httpx.Response(
                200,
                json={
                    "notifications": [
                        notification("like", at=f"2026-08-31T{10 + page:02d}:00:00Z")
                    ],
                    "cursor": str(page + 1),
                },
            )

        marker = "2000-01-01T00:00:00Z::at://never/found"
        with respx.mock(base_url=XRPC) as network:
            route = network.get("/app.bsky.notification.listNotifications").mock(
                side_effect=handler
            )
            batch = await platform.fetch_updates_after(account, marker)

        assert route.calls.call_count == bluesky_module._MAX_MARKER_PAGES
        assert batch.more is True

    async def test_a_malformed_marker_raises_instead_of_starting_over(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        # Treating this like None would silently restart from the latest
        # page and drop whatever came after it - the opposite of safe.
        with respx.mock(base_url=XRPC, assert_all_called=False) as network:
            route = network.get("/app.bsky.notification.listNotifications")

            with pytest.raises(ConfigError, match="None"):
                await platform.fetch_updates_after(account, "not-a-real-marker")

        assert route.call_count == 0

    async def test_the_marker_stays_put_when_nothing_new_is_found(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        marker = "2026-09-01T00:00:00Z::at://did:plc:bob/x/like"
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.notification.listNotifications").mock(
                return_value=httpx.Response(200, json={"notifications": []})
            )
            batch = await platform.fetch_updates_after(account, marker)

        assert batch.updates == ()
        assert batch.marker == marker
        assert batch.more is False

    async def test_a_limit_is_capped_at_a_hundred(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            route = network.get("/app.bsky.notification.listNotifications").mock(
                return_value=httpx.Response(200, json={"notifications": []})
            )
            await platform.fetch_updates_after(account, None, limit=500)

        assert route.calls.last.request.url.params["limit"] == "100"

    async def test_the_page_size_defaults_to_updates_per_check(
        self, account: Connection
    ) -> None:
        platform = BlueskyPlatform(retries=ONCE, updates_per_check=7)
        with respx.mock(base_url=XRPC) as network:
            route = network.get("/app.bsky.notification.listNotifications").mock(
                return_value=httpx.Response(200, json={"notifications": []})
            )
            await platform.fetch_updates_after(account, None)

        assert route.calls.last.request.url.params["limit"] == "7"


class TestMarkingAMarkerSeen:
    async def test_it_sends_the_indexed_at_half_of_the_marker(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            route = network.post("/app.bsky.notification.updateSeen").mock(
                return_value=httpx.Response(200, json={})
            )
            await platform.mark_seen(
                account, "2026-08-31T12:00:00Z::at://did:plc:bob/x/like"
            )

        assert sent_json(route) == {"seenAt": "2026-08-31T12:00:00Z"}

    async def test_a_marker_it_did_not_write_raises_instead_of_being_sent(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC, assert_all_called=False) as network:
            route = network.post("/app.bsky.notification.updateSeen")

            with pytest.raises(ConfigError, match="None"):
                await platform.mark_seen(account, "some-other-marker")

        assert route.call_count == 0


# ---------------------------------------------------------------------------
# Direct messages
# ---------------------------------------------------------------------------


class TestDirectMessages:
    async def test_it_says_it_can_message_and_start_conversations(
        self, platform: BlueskyPlatform
    ) -> None:
        assert Feature.MESSAGES in platform.features
        assert isinstance(platform, CanMessage)
        assert Feature.START_CONVERSATIONS in platform.features
        assert isinstance(platform, CanStartConversations)

    async def test_every_chat_call_carries_the_proxy_header(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            route = network.get("/chat.bsky.convo.listConvos").mock(
                return_value=httpx.Response(200, json=fixture("chat_list_convos"))
            )
            await platform.read_conversations(account)

        assert (
            route.calls.last.request.headers["atproto-proxy"]
            == "did:web:api.bsky.chat#bsky_chat"
        )

    async def test_it_lists_conversations(self, platform: BlueskyPlatform) -> None:
        account = merchant_account()
        raw = fixture("chat_list_convos")
        with respx.mock(base_url=XRPC) as network:
            network.get("/chat.bsky.convo.listConvos").mock(
                return_value=httpx.Response(200, json=raw)
            )
            page = await platform.read_conversations(account)

        assert len(page.items) == 2
        assert page.next == raw["cursor"]
        first = page.items[0]
        assert first.id == raw["convos"][0]["id"]
        assert len(first.people) == 1
        assert first.people[0].handle == "customerchris.bsky.social"
        assert first.unread_count == 2
        assert first.full_history is True
        assert first.can_reply_until is None
        assert first.last_message is not None
        assert first.last_message.text.startswith("Thanks!")
        assert first.updated_at == first.last_message.sent_at

    async def test_a_deleted_last_message_reads_as_deleted(
        self, platform: BlueskyPlatform
    ) -> None:
        account = merchant_account()
        raw = fixture("chat_list_convos")
        with respx.mock(base_url=XRPC) as network:
            network.get("/chat.bsky.convo.listConvos").mock(
                return_value=httpx.Response(200, json=raw)
            )
            page = await platform.read_conversations(account)

        second = page.items[1]
        assert second.last_message is not None
        assert second.last_message.deleted is True
        assert second.last_message.text == ""
        assert second.last_message.is_mine is True

    async def test_it_passes_after_and_limit_through(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            route = network.get("/chat.bsky.convo.listConvos").mock(
                return_value=httpx.Response(200, json={"convos": []})
            )
            await platform.read_conversations(account, after="cursor-1", limit=5)

        params = route.calls.last.request.url.params
        assert params["cursor"] == "cursor-1"
        assert params["limit"] == "5"

    async def test_no_convos_key_is_an_empty_page(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.get("/chat.bsky.convo.listConvos").mock(
                return_value=httpx.Response(200, json={})
            )
            page = await platform.read_conversations(account)

        assert page.items == ()
        assert page.next is None

    async def test_it_reads_messages_newest_first(
        self, platform: BlueskyPlatform
    ) -> None:
        account = merchant_account()
        raw = fixture("chat_get_messages")
        with respx.mock(base_url=XRPC) as network:
            route = network.get("/chat.bsky.convo.getMessages").mock(
                return_value=httpx.Response(200, json=raw)
            )
            page = await platform.read_messages(account, "eul6tja4znqhx")

        params = route.calls.last.request.url.params
        assert params["convoId"] == "eul6tja4znqhx"
        assert page.next == raw["cursor"]
        assert len(page.items) == 3
        first, second, third = page.items
        assert first.sender.handle == "customerchris.bsky.social"
        assert first.is_mine is False
        assert first.deleted is False
        assert second.deleted is True
        assert second.text == ""
        assert second.sender.handle == MERCHANT_HANDLE
        assert second.is_mine is True
        assert third.sender.handle == "customerchris.bsky.social"

    async def test_it_passes_after_and_limit_through_for_messages(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            route = network.get("/chat.bsky.convo.getMessages").mock(
                return_value=httpx.Response(200, json={"messages": []})
            )
            await platform.read_messages(account, "convo-1", after="cursor-1", limit=5)

        params = route.calls.last.request.url.params
        assert params["cursor"] == "cursor-1"
        assert params["limit"] == "5"

    async def test_no_related_profiles_falls_back_to_bare_ids_or_the_account(
        self, platform: BlueskyPlatform
    ) -> None:
        account = merchant_account()
        raw = copy.deepcopy(fixture("chat_get_messages"))
        del raw["relatedProfiles"]
        with respx.mock(base_url=XRPC) as network:
            network.get("/chat.bsky.convo.getMessages").mock(
                return_value=httpx.Response(200, json=raw)
            )
            page = await platform.read_messages(account, "convo-1")

        assert page.items[0].sender.handle is None
        assert page.items[0].sender.id == raw["messages"][0]["sender"]["did"]
        assert page.items[1].sender.handle == MERCHANT_HANDLE

    async def test_a_message_with_no_sent_at_fails_loudly(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        raw = copy.deepcopy(fixture("chat_get_messages"))
        del raw["messages"][0]["sentAt"]
        with respx.mock(base_url=XRPC) as network:
            network.get("/chat.bsky.convo.getMessages").mock(
                return_value=httpx.Response(200, json=raw)
            )
            with pytest.raises(PlatformError, match="sentAt"):
                await platform.read_messages(account, "convo-1")

    async def test_no_messages_key_is_an_empty_page(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.get("/chat.bsky.convo.getMessages").mock(
                return_value=httpx.Response(200, json={})
            )
            page = await platform.read_messages(account, "convo-1")

        assert page.items == ()

    async def test_it_sends_a_plain_message(self, platform: BlueskyPlatform) -> None:
        account = merchant_account()
        raw = fixture("chat_send_message")
        with respx.mock(base_url=XRPC) as network:
            route = network.post("/chat.bsky.convo.sendMessage").mock(
                return_value=httpx.Response(200, json=raw)
            )
            message = await platform.send_message(account, "convo-1", "hello")

        assert sent_json(route) == {
            "convoId": "convo-1",
            "message": {"text": "hello"},
        }
        assert message.conversation_id == "convo-1"
        assert message.text == raw["text"]
        assert message.is_mine is True

    async def test_a_link_in_the_message_gets_a_facet(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            route = network.post("/chat.bsky.convo.sendMessage").mock(
                return_value=httpx.Response(200, json=fixture("chat_send_message"))
            )
            await platform.send_message(account, "convo-1", "see https://example.com")

        facet = sent_json(route)["message"]["facets"][0]
        assert facet["features"][0]["uri"] == "https://example.com"

    async def test_send_message_refuses_unknown_options(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with (
            respx.mock(assert_all_called=False),
            pytest.raises(InvalidPostError),
        ):
            await platform.send_message(account, "convo-1", "hi", options={"tag": "x"})

    async def test_mark_read(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            route = network.post("/chat.bsky.convo.updateRead").mock(
                return_value=httpx.Response(200, json={})
            )
            await platform.mark_read(account, "convo-1")

        assert sent_json(route) == {"convoId": "convo-1"}

    async def test_start_conversation(self, platform: BlueskyPlatform) -> None:
        account = merchant_account()
        with respx.mock(base_url=XRPC) as network:
            get_convo = network.get("/chat.bsky.convo.getConvoForMembers").mock(
                return_value=httpx.Response(
                    200, json=fixture("chat_get_convo_for_members")
                )
            )
            send = network.post("/chat.bsky.convo.sendMessage").mock(
                return_value=httpx.Response(200, json=fixture("chat_send_message"))
            )
            message = await platform.start_conversation(
                account, ["did:plc:d2yalmapuaocmlqbfzwwl33y"], "hi there"
            )

        assert get_convo.calls.last.request.url.params.get_list("members") == [
            "did:plc:d2yalmapuaocmlqbfzwwl33y"
        ]
        assert sent_json(send)["convoId"] == "yuf7t6awrceda"
        assert sent_json(send)["message"]["text"] == "hi there"
        assert message.conversation_id == "yuf7t6awrceda"

    async def test_starting_a_conversation_with_a_link_marks_it_up(
        self, platform: BlueskyPlatform
    ) -> None:
        account = merchant_account()
        with respx.mock(base_url=XRPC) as network:
            network.get("/chat.bsky.convo.getConvoForMembers").mock(
                return_value=httpx.Response(
                    200, json=fixture("chat_get_convo_for_members")
                )
            )
            send = network.post("/chat.bsky.convo.sendMessage").mock(
                return_value=httpx.Response(200, json=fixture("chat_send_message"))
            )
            await platform.start_conversation(
                account,
                ["did:plc:d2yalmapuaocmlqbfzwwl33y"],
                "see https://example.com",
            )

        facet = sent_json(send)["message"]["facets"][0]
        assert facet["features"][0]["uri"] == "https://example.com"

    async def test_start_conversation_without_a_convo_in_the_reply(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.get("/chat.bsky.convo.getConvoForMembers").mock(
                return_value=httpx.Response(200, json={})
            )
            with pytest.raises(PlatformError, match="conversation"):
                await platform.start_conversation(account, ["did:plc:x"], "hi")

    async def test_a_missing_dm_permission_is_a_clear_error(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.get("/chat.bsky.convo.listConvos").mock(
                return_value=httpx.Response(
                    400,
                    json={"error": "InvalidToken", "message": "Bad token method"},
                )
            )
            with pytest.raises(MissingPermissionError, match="direct messages"):
                await platform.read_conversations(account)

    async def test_an_invalid_token_on_a_non_chat_call_is_still_an_auth_error(
        self, platform: BlueskyPlatform, account: Connection
    ) -> None:
        with respx.mock(base_url=XRPC) as network:
            network.get("/app.bsky.feed.getPosts").mock(
                return_value=httpx.Response(
                    400,
                    json={"error": "InvalidToken", "message": "Bad token method"},
                )
            )
            with pytest.raises(AuthError):
                await platform.read_post(account, "at://x/y/z")


# ---------------------------------------------------------------------------
# Private helpers, tested directly for the edge cases fixtures do not carry
# ---------------------------------------------------------------------------


class TestAttachmentsFromEmbeds:
    def test_no_embed_is_no_attachments(self) -> None:
        assert bluesky_module._attachments_from(None) == ()
        assert bluesky_module._attachments_from("not a dict") == ()

    def test_an_unrecognised_embed_kind_is_no_attachments(self) -> None:
        assert (
            bluesky_module._attachments_from({"$type": "app.bsky.embed.record#view"})
            == ()
        )

    def test_images_with_no_images_list(self) -> None:
        assert (
            bluesky_module._attachments_from({"$type": "app.bsky.embed.images#view"})
            == ()
        )

    def test_images_skips_non_dict_entries(self) -> None:
        found = bluesky_module._attachments_from(
            {
                "$type": "app.bsky.embed.images#view",
                "images": ["nonsense", {"fullsize": "u", "thumb": "t"}],
            }
        )
        assert len(found) == 1

    def test_an_image_with_no_aspect_ratio_or_alt_has_no_size(self) -> None:
        found = bluesky_module._attachments_from(
            {
                "$type": "app.bsky.embed.images#view",
                "images": [{"fullsize": "u", "thumb": "t"}],
            }
        )
        assert found[0].width is None
        assert found[0].height is None
        assert found[0].alt_text is None

    def test_a_video_embed_becomes_a_video_attachment(self) -> None:
        found = bluesky_module._attachments_from(
            {
                "$type": "app.bsky.embed.video#view",
                "playlist": "https://example.com/video.m3u8",
                "thumbnail": "https://example.com/thumb.jpg",
                "alt": "a video",
                "aspectRatio": {"width": 640, "height": 360},
            }
        )
        assert len(found) == 1
        assert found[0].kind == "video"
        assert found[0].url == "https://example.com/video.m3u8"
        assert found[0].preview_url == "https://example.com/thumb.jpg"
        assert found[0].alt_text == "a video"
        assert found[0].width == 640
        assert found[0].height == 360

    def test_an_external_embed_becomes_a_link_attachment(self) -> None:
        found = bluesky_module._attachments_from(
            {
                "$type": "app.bsky.embed.external#view",
                "external": {
                    "uri": "https://example.com",
                    "title": "Example",
                    "thumb": "https://example.com/t.jpg",
                },
            }
        )
        assert found[0].kind == "link"
        assert found[0].url == "https://example.com"
        assert found[0].preview_url == "https://example.com/t.jpg"
        assert found[0].alt_text is None

    def test_an_external_embed_with_no_external_object(self) -> None:
        found = bluesky_module._attachments_from(
            {"$type": "app.bsky.embed.external#view"}
        )
        assert found == ()

    def test_record_with_media_recurses_into_the_media(self) -> None:
        found = bluesky_module._attachments_from(
            {
                "$type": "app.bsky.embed.recordWithMedia#view",
                "record": {"record": {"$type": "app.bsky.embed.record#viewRecord"}},
                "media": {
                    "$type": "app.bsky.embed.images#view",
                    "images": [{"fullsize": "u", "thumb": "t"}],
                },
            }
        )
        assert len(found) == 1
        assert found[0].kind == "image"


class TestLinksFromFacets:
    def test_no_facets_is_no_links(self) -> None:
        assert bluesky_module._links_from("hi", None) == ()
        assert bluesky_module._links_from("hi", "nonsense") == ()

    def test_a_non_dict_facet_is_skipped(self) -> None:
        assert bluesky_module._links_from("hi", ["nonsense"]) == ()

    def test_a_facet_missing_index_or_features_is_skipped(self) -> None:
        assert bluesky_module._links_from("hi", [{"index": {}, "features": "no"}]) == ()
        assert bluesky_module._links_from("hi", [{"index": "no", "features": []}]) == ()

    def test_a_facet_with_non_integer_offsets_is_skipped(self) -> None:
        facet = {"index": {"byteStart": "0", "byteEnd": 2}, "features": []}
        assert bluesky_module._links_from("hi", [facet]) == ()

    def test_a_non_dict_feature_is_skipped(self) -> None:
        facet = {"index": {"byteStart": 0, "byteEnd": 2}, "features": ["nonsense"]}
        assert bluesky_module._links_from("hi", [facet]) == ()

    def test_an_unrecognised_feature_type_is_skipped(self) -> None:
        facet = {
            "index": {"byteStart": 0, "byteEnd": 2},
            "features": [{"$type": "app.bsky.richtext.facet#other"}],
        }
        assert bluesky_module._links_from("hi", [facet]) == ()

    @pytest.mark.parametrize(
        "feature",
        [
            {"$type": "app.bsky.richtext.facet#mention"},
            {"$type": "app.bsky.richtext.facet#link"},
            {"$type": "app.bsky.richtext.facet#tag"},
        ],
    )
    def test_a_feature_missing_its_own_field_is_skipped(
        self, feature: dict[str, Any]
    ) -> None:
        facet = {"index": {"byteStart": 0, "byteEnd": 2}, "features": [feature]}
        assert bluesky_module._links_from("hi", [facet]) == ()


class TestMarkerHelpers:
    def test_a_marker_round_trips(self) -> None:
        marker = bluesky_module._marker_for(
            {"indexedAt": "2026-01-01T00:00:00Z", "uri": "at://x"}
        )
        assert marker == "2026-01-01T00:00:00Z::at://x"
        assert bluesky_module._parse_marker(marker) == (
            "2026-01-01T00:00:00Z",
            "at://x",
        )

    def test_a_notification_missing_a_half_makes_no_marker(self) -> None:
        assert bluesky_module._marker_for({"indexedAt": "2026-01-01T00:00:00Z"}) is None
        assert bluesky_module._marker_for({"uri": "at://x"}) is None

    @pytest.mark.parametrize("garbage", ["garbage", "::", "only-when::", "::only-uri"])
    def test_a_marker_this_platform_did_not_write_does_not_parse(
        self, garbage: str
    ) -> None:
        assert bluesky_module._parse_marker(garbage) is None


class TestReplyMoment:
    def test_it_prefers_the_records_own_created_at(self) -> None:
        view = {
            "record": {"createdAt": "2026-01-01T00:00:00Z"},
            "indexedAt": "2026-01-02T00:00:00Z",
        }
        assert bluesky_module._reply_moment(view) == datetime(2026, 1, 1, tzinfo=UTC)

    def test_it_falls_back_to_indexed_at_with_no_record(self) -> None:
        view = {"indexedAt": "2026-01-02T00:00:00Z"}
        assert bluesky_module._reply_moment(view) == datetime(2026, 1, 2, tzinfo=UTC)

    def test_it_falls_back_to_indexed_at_when_the_record_has_none(self) -> None:
        view = {"record": {"text": "hi"}, "indexedAt": "2026-01-02T00:00:00Z"}
        assert bluesky_module._reply_moment(view) == datetime(2026, 1, 2, tzinfo=UTC)

    def test_it_falls_back_to_indexed_at_when_created_at_cannot_be_read(self) -> None:
        view = {
            "record": {"createdAt": "not a time"},
            "indexedAt": "2026-01-02T00:00:00Z",
        }
        assert bluesky_module._reply_moment(view) == datetime(2026, 1, 2, tzinfo=UTC)

    def test_none_when_nothing_can_be_read(self) -> None:
        assert bluesky_module._reply_moment({}) is None


class TestPostReplyRefs:
    def test_no_record_is_no_refs(self) -> None:
        assert bluesky_module._post_reply_refs(None) == (None, None)

    def test_a_record_with_no_reply_is_no_refs(self) -> None:
        assert bluesky_module._post_reply_refs({"text": "hi"}) == (None, None)

    def test_a_malformed_reply_reference_is_ignored(self) -> None:
        record = {"reply": {"parent": "not-an-object", "root": {"uri": 7}}}
        assert bluesky_module._post_reply_refs(record) == (None, None)


class TestViewerLike:
    def test_no_viewer_means_unknown(self) -> None:
        assert bluesky_module._viewer_like({}) == (None, None)

    def test_an_empty_viewer_is_not_liked(self) -> None:
        assert bluesky_module._viewer_like({"viewer": {}}) == (False, None)

    def test_a_liked_viewer_carries_the_like_id(self) -> None:
        assert bluesky_module._viewer_like({"viewer": {"like": "at://x"}}) == (
            True,
            "at://x",
        )


class TestPersonFrom:
    def test_a_person_with_no_handle_has_no_profile_url(self) -> None:
        person = bluesky_module._person_from({"did": "did:plc:x"})
        assert person.handle is None
        assert person.url is None
        assert person.display_name is None
        assert person.avatar_url is None


class TestMessageMoment:
    def test_a_message_with_no_sent_at_says_so(self) -> None:
        with pytest.raises(PlatformError, match="sentAt"):
            bluesky_module._message_moment({}, "read a message")


class TestProfileLookup:
    def test_it_finds_a_matching_profile(self) -> None:
        profiles = [{"did": "did:plc:x", "handle": "x.bsky.social"}]
        person = bluesky_module._profile_lookup(
            "did:plc:x", profiles, connection=merchant_account()
        )
        assert person.handle == "x.bsky.social"

    def test_it_falls_back_to_the_connections_own_handle(self) -> None:
        person = bluesky_module._profile_lookup(
            MERCHANT_DID, [], connection=merchant_account()
        )
        assert person.handle == MERCHANT_HANDLE
        assert person.url == f"https://bsky.app/profile/{MERCHANT_HANDLE}"

    def test_it_falls_back_to_a_bare_id_for_a_stranger(self) -> None:
        person = bluesky_module._profile_lookup(
            "did:plc:unknown", [], connection=merchant_account()
        )
        assert person.id == "did:plc:unknown"
        assert person.handle is None

    def test_it_falls_back_to_a_bare_id_when_the_account_has_no_handle_either(
        self,
    ) -> None:
        bare_account = Connection(
            id="bluesky:did:plc:x",
            platform="bluesky",
            host=HOST,
            account_id="did:plc:x",
            account_name="@x",
            token=Token(access_token="a"),
        )
        person = bluesky_module._profile_lookup(
            "did:plc:x", [], connection=bare_account
        )
        assert person.handle is None


class TestConversationFrom:
    def test_a_bare_convo_has_nothing_to_show(self) -> None:
        convo = bluesky_module._conversation_from({}, connection=merchant_account())
        assert convo.id == ""
        assert convo.people == ()
        assert convo.last_message is None
        assert convo.updated_at is None
        assert convo.unread_count is None


class TestMessageFrom:
    def test_a_message_with_no_sender_falls_back_to_a_bare_person(self) -> None:
        raw = {"id": "m1", "text": "hi", "sentAt": "2026-01-01T00:00:00Z"}
        message = bluesky_module._message_from(
            raw, conversation_id="c1", profiles=(), connection=merchant_account()
        )
        assert message.sender.id == ""
        assert message.sender.handle is None


class TestBlueskyBehavesLikeTheOthers(PlatformChecks):
    def make_platform(self) -> Platform:
        return BlueskyPlatform(transport=self.transport, retries=ONCE)

    def make_connection(self) -> Connection | None:
        return an_account()

    def make_transport(self) -> httpx.AsyncBaseTransport | None:
        return RecordingTransport(
            {
                "GET /xrpc/app.bsky.notification.listNotifications": {
                    "notifications": [A_NOTIFICATION]
                },
                "POST /xrpc/com.atproto.repo.createRecord": CREATED,
            }
        )
