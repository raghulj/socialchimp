"""Tests for the Bluesky platform."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from socialchimp import (
    AuthError,
    Connection,
    Feature,
    InvalidPostError,
    Limits,
    Media,
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
from socialchimp.features import count_graphemes as shared_count_graphemes
from socialchimp.http import Retries
from socialchimp.platform import (
    AskForDetails,
    CanDeletePosts,
    CanReadUpdates,
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
            ("repost", UpdateKind.REACTION_ADDED),
            ("reply", UpdateKind.COMMENT_CREATED),
            ("mention", UpdateKind.MENTION),
            ("follow", UpdateKind.UNKNOWN),
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
# The shared checks every platform has to pass
# ---------------------------------------------------------------------------


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
