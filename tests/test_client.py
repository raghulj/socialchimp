"""Tests for the client an app talks to."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from socialchimp import (
    AppCredentials,
    ConfigError,
    Connection,
    Feature,
    InMemoryStorage,
    InvalidPostError,
    Limits,
    NotSupportedError,
    PlatformError,
    Post,
    PostResult,
    RawData,
    Storage,
    Token,
    TokenManager,
)
from socialchimp.client import Account, PostError, PostJob, SocialChimp
from socialchimp.http import HttpClient
from socialchimp.platform import (
    AccountChoice,
    ChooseAccount,
    Finished,
    LoginRequest,
    LoginStep,
    SendToNetwork,
)
from socialchimp.registry import register_platform, unregister_platform

HOST = "fake.example"
BASE = f"https://{HOST}"

OLD_ACCESS = "old-access"
NEW_ACCESS = "new-access"
OLD_REFRESH = "old-refresh"

# Far enough away that nothing renews it.
HOURS_LEFT = timedelta(hours=6)

# Inside the one minute window, so a new token gets asked for.
ALMOST_OUT = timedelta(seconds=20)

REDIRECT = "https://example.com/cb"

# Every platform the client built, in the order it built them. A platform is
# created with no arguments, so this is how a test reaches the one that was
# actually used.
MADE: list[FakePlatform] = []


def a_token(
    *,
    access_token: str = OLD_ACCESS,
    refresh_token: str | None = OLD_REFRESH,
    left: timedelta | None = HOURS_LEFT,
) -> Token:
    expires_at = None if left is None else datetime.now(UTC) + left
    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
    )


def a_connection(
    *,
    connection_id: str = "conn-1",
    platform: str = "fake",
    host: str | None = HOST,
    token: Token | None = None,
) -> Connection:
    return Connection(
        id=connection_id,
        platform=platform,
        host=host,
        account_id="42",
        account_name="someone",
        token=token if token is not None else a_token(),
    )


def expiring_soon(
    *,
    connection_id: str = "conn-1",
    platform: str = "fake",
) -> Connection:
    return a_connection(
        connection_id=connection_id,
        platform=platform,
        token=a_token(left=ALMOST_OUT),
    )


def an_app(platform: str = "fake", host: str | None = HOST) -> AppCredentials:
    return AppCredentials(
        platform=platform,
        host=host,
        client_id=f"{platform}-id",
        client_secret="shh",
    )


async def storage_holding_the_app(
    platform: str = "fake",
    host: str | None = HOST,
) -> InMemoryStorage:
    storage = InMemoryStorage()
    await storage.save_app(an_app(platform, host))
    return storage


async def storage_holding(*connections: Connection) -> InMemoryStorage:
    storage = InMemoryStorage()
    for connection in connections:
        await storage.save_connection(connection)
    return storage


def made(name: str) -> FakePlatform:
    for platform in MADE:
        if platform.name == name:
            return platform
    raise AssertionError(f"no {name} platform was built")


class FakePlatform:
    """A platform that answers from memory instead of over a network."""

    name = "fake"
    features = Feature.POST_TEXT | Feature.POST_IMAGE | Feature.REPLY
    allowed = Limits(max_text_length=50)
    pause = 0.0

    def __init__(self) -> None:
        MADE.append(self)
        self.published: list[tuple[Connection, Post]] = []
        self.asked_limits: list[Connection] = []
        self.deleted: list[tuple[Connection, str]] = []
        self.refreshed: list[Connection] = []
        self.started: list[LoginRequest] = []
        self.finished: list[tuple[LoginRequest, Mapping[str, str]]] = []
        self.remembered: list[RawData | None] = []
        self.resumed: list[tuple[str, str]] = []

    async def limits(self, connection: Connection) -> Limits:
        self.asked_limits.append(connection)
        return self.allowed

    async def start_login(self, request: LoginRequest) -> SendToNetwork:
        self.started.append(request)
        return SendToNetwork(
            url=f"{BASE}/oauth/authorize",
            state=request.state if request.state is not None else "made-up",
        )

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> LoginStep:
        self.finished.append((request, callback))
        self.remembered.append(remember)
        return Finished(
            connection=a_connection(connection_id="conn-new", platform=self.name)
        )

    async def refresh(self, connection: Connection) -> Token:
        self.refreshed.append(connection)
        return a_token(access_token=NEW_ACCESS)

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        await asyncio.sleep(self.pause)
        self.published.append((connection, post))
        return PostResult(id=f"post-{connection.id}")


class DeletingPlatform(FakePlatform):
    """A platform that can take a post back down again."""

    name = "deleter"
    features = FakePlatform.features | Feature.DELETE_POST

    async def delete_post(self, connection: Connection, post_id: str) -> None:
        self.deleted.append((connection, post_id))


class QuietDeleter(DeletingPlatform):
    """Has the method, but does not list the feature. The list is the truth."""

    name = "quiet-deleter"
    features = FakePlatform.features


class LyingDeleter(FakePlatform):
    """Says it can take posts down, but has no method for it."""

    name = "lying-deleter"
    features = FakePlatform.features | Feature.DELETE_POST


class AppMakerPlatform(FakePlatform):
    """A platform that registers an app for you, the way Mastodon does."""

    name = "appmaker"
    features = FakePlatform.features | Feature.CREATE_APP

    async def create_app(
        self,
        *,
        name: str,
        redirect_uri: str,
        host: str | None = None,
        scopes: tuple[str, ...] = (),
    ) -> AppCredentials:
        return AppCredentials(
            platform=self.name,
            host=host,
            client_id=f"{name}-id",
            client_secret="shh",
        )


class LyingAppMaker(FakePlatform):
    """Says it can register an app, but has no method for it."""

    name = "lying-appmaker"
    features = FakePlatform.features | Feature.CREATE_APP


class ChoosyPlatform(FakePlatform):
    """A platform that pauses to ask which page to use, the way Facebook does."""

    name = "choosy"

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> LoginStep:
        self.finished.append((request, callback))
        self.remembered.append(remember)
        return ChooseAccount(
            options=(AccountChoice(id="page-1", name="A Page", kind="page"),),
            resume_token="carry-on",
        )

    async def resume_login(
        self,
        request: LoginRequest,
        *,
        resume_token: str,
        account_id: str,
        remember: RawData | None = None,
    ) -> LoginStep:
        self.resumed.append((resume_token, account_id))
        self.remembered.append(remember)
        return Finished(
            connection=a_connection(connection_id="conn-page", platform=self.name)
        )


class BrokenPlatform(FakePlatform):
    """A platform whose network is having a bad day."""

    name = "broken"

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        message = "the network fell over"
        raise PlatformError(message, platform=self.name)


class CancellingPlatform(FakePlatform):
    """A platform whose work gets cancelled part way through."""

    name = "cancels"

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        raise asyncio.CancelledError


class SlowPlatform(FakePlatform):
    """A platform that takes its time, so results do not arrive in order."""

    name = "slow"
    pause = 0.05


FAKES: dict[str, type[FakePlatform]] = {
    "fake": FakePlatform,
    "deleter": DeletingPlatform,
    "quiet-deleter": QuietDeleter,
    "lying-deleter": LyingDeleter,
    "appmaker": AppMakerPlatform,
    "lying-appmaker": LyingAppMaker,
    "choosy": ChoosyPlatform,
    "broken": BrokenPlatform,
    "cancels": CancellingPlatform,
    "slow": SlowPlatform,
}


@pytest.fixture(autouse=True)
def _fake_platforms() -> Iterator[None]:
    MADE.clear()
    for name, platform_class in FAKES.items():
        register_platform(name, platform_class)
    yield
    for name in FAKES:
        unregister_platform(name)
    MADE.clear()


class CountingTokens(TokenManager):
    """A token manager that writes down every account it was asked about."""

    def __init__(self, storage: Storage) -> None:
        super().__init__(storage, self._new_token)
        self.asked: list[str] = []

    async def _new_token(self, connection: Connection) -> Token:
        return a_token(access_token=NEW_ACCESS)

    async def valid_token(self, connection_id: str) -> Connection:
        self.asked.append(connection_id)
        return await super().valid_token(connection_id)


class TestBuildingTheClient:
    async def test_a_platform_comes_from_the_registry_by_default(self) -> None:
        storage = await storage_holding(a_connection())
        sc = SocialChimp(storage)

        await sc.account("conn-1").post(Post(text="hello"))

        assert [platform.name for platform in MADE] == ["fake"]

    async def test_a_platform_is_only_built_once(self) -> None:
        storage = await storage_holding(a_connection())
        sc = SocialChimp(storage)

        await sc.account("conn-1").post(Post(text="one"))
        await sc.account("conn-1").post(Post(text="two"))

        assert len(MADE) == 1

    async def test_a_platform_you_hand_in_is_used_instead(self) -> None:
        storage = await storage_holding(a_connection())
        yours = FakePlatform()
        sc = SocialChimp(storage, platforms={"fake": yours})

        await sc.account("conn-1").post(Post(text="hello"))

        assert len(yours.published) == 1
        # Only the one the test made. Nothing was built from the registry.
        assert len(MADE) == 1
        assert MADE[0] is yours

    async def test_an_unknown_platform_says_what_is_installed(self) -> None:
        sc = SocialChimp(InMemoryStorage())

        with pytest.raises(ConfigError) as refused:
            await sc.start_login("nope", redirect_uri=REDIRECT)

        assert 'There is no platform called "nope"' in str(refused.value)
        assert "fake" in str(refused.value)


class TestSigningSomeoneIn:
    async def test_starting_a_login_hands_the_request_to_the_platform(self) -> None:
        sc = SocialChimp(await storage_holding_the_app())

        step = await sc.start_login(
            "fake",
            redirect_uri=REDIRECT,
            scopes=("read", "write"),
            host=HOST,
            state="mine",
        )

        assert step == SendToNetwork(url=f"{BASE}/oauth/authorize", state="mine")
        assert made("fake").started == [
            LoginRequest(
                redirect_uri=REDIRECT,
                scopes=("read", "write"),
                host=HOST,
                state="mine",
                app=an_app(),
            )
        ]

    async def test_signing_in_before_the_app_is_registered_says_so(self) -> None:
        sc = SocialChimp(InMemoryStorage())

        with pytest.raises(ConfigError) as missing:
            await sc.start_login("fake", redirect_uri=REDIRECT, host=HOST)

        message = str(missing.value)
        assert f"No app credentials are stored for fake on {HOST}" in message
        assert "developer portal" in message

    async def test_a_network_with_one_server_is_named_on_its_own(self) -> None:
        sc = SocialChimp(InMemoryStorage())

        with pytest.raises(ConfigError) as missing:
            await sc.start_login("fake", redirect_uri=REDIRECT)

        assert "stored for fake. Nobody" in str(missing.value)

    async def test_finishing_a_login_saves_the_connection(self) -> None:
        storage = await storage_holding_the_app()
        sc = SocialChimp(storage)

        step = await sc.finish_login(
            "fake",
            callback={"code": "abc", "state": "mine"},
            redirect_uri=REDIRECT,
            host=HOST,
        )

        assert isinstance(step, Finished)
        assert await storage.get_connection("conn-new") == step.connection
        assert made("fake").finished[0][1] == {"code": "abc", "state": "mine"}

    async def test_what_you_were_asked_to_remember_is_handed_back(self) -> None:
        sc = SocialChimp(await storage_holding_the_app())

        await sc.finish_login(
            "fake",
            callback={"code": "abc"},
            redirect_uri=REDIRECT,
            host=HOST,
            remember={"verifier": "the-secret-half"},
        )

        assert made("fake").remembered == [{"verifier": "the-secret-half"}]

    async def test_a_network_that_pauses_asks_which_account(self) -> None:
        storage = await storage_holding_the_app("choosy")
        sc = SocialChimp(storage)

        step = await sc.finish_login(
            "choosy",
            callback={"code": "abc"},
            redirect_uri=REDIRECT,
            host=HOST,
        )

        assert isinstance(step, ChooseAccount)
        assert step.options[0].name == "A Page"
        # Nothing is connected yet, so nothing is saved yet.
        assert await storage.get_connection("conn-page") is None

    async def test_choosing_an_account_finishes_the_login(self) -> None:
        storage = await storage_holding_the_app("choosy")
        sc = SocialChimp(storage)
        step = await sc.finish_login(
            "choosy",
            callback={"code": "abc"},
            redirect_uri=REDIRECT,
            host=HOST,
            remember={"verifier": "the-secret-half"},
        )
        assert isinstance(step, ChooseAccount)

        done = await sc.choose(
            "choosy",
            account_id=step.options[0].id,
            resume_token=step.resume_token,
            redirect_uri=REDIRECT,
            host=HOST,
            remember={"verifier": "the-secret-half"},
        )

        assert isinstance(done, Finished)
        assert done.connection.id == "conn-page"
        assert await storage.get_connection("conn-page") == done.connection
        assert made("choosy").resumed == [("carry-on", "page-1")]
        # The same thing was carried into both halves of the login.
        assert made("choosy").remembered == [
            {"verifier": "the-secret-half"},
            {"verifier": "the-secret-half"},
        ]

    async def test_choosing_on_a_network_that_never_asks_says_so(self) -> None:
        sc = SocialChimp(await storage_holding_the_app())

        with pytest.raises(NotSupportedError) as refused:
            await sc.choose(
                "fake",
                account_id="page-1",
                resume_token="carry-on",
                redirect_uri=REDIRECT,
                host=HOST,
            )

        assert refused.value.platform == "fake"
        assert "one step" in str(refused.value)


class TestRegisteringAnApp:
    async def test_it_registers_the_app_and_saves_the_credentials(self) -> None:
        storage = InMemoryStorage()
        sc = SocialChimp(storage)

        app = await sc.create_app(
            "appmaker",
            name="My App",
            redirect_uri=REDIRECT,
            host=HOST,
        )

        assert app.client_id == "My App-id"
        assert await storage.get_app("appmaker", HOST) == app

    async def test_a_network_that_cannot_says_where_to_register_by_hand(self) -> None:
        sc = SocialChimp(InMemoryStorage())

        with pytest.raises(NotSupportedError) as refused:
            await sc.create_app("fake", name="My App", redirect_uri=REDIRECT)

        message = str(refused.value)
        assert refused.value.platform == "fake"
        assert "fake does not support" in message
        assert "developer portal" in message
        assert "save_app" in message

    async def test_a_platform_that_claims_it_but_cannot_is_a_setup_problem(
        self,
    ) -> None:
        sc = SocialChimp(InMemoryStorage())

        with pytest.raises(ConfigError) as broken:
            await sc.create_app("lying-appmaker", name="My App", redirect_uri=REDIRECT)

        assert "create_app" in str(broken.value)


class TestWorkingWithOneAccount:
    def test_an_account_handle_reads_nothing_until_you_use_it(self) -> None:
        sc = SocialChimp(InMemoryStorage())

        account = sc.account("not-saved-yet")

        assert isinstance(account, Account)
        assert account.id == "not-saved-yet"

    def test_the_handle_says_which_connection_it_is_for(self) -> None:
        sc = SocialChimp(InMemoryStorage())

        assert repr(sc.account("conn-1")) == "Account('conn-1')"

    async def test_an_unknown_connection_says_so_when_you_use_it(self) -> None:
        sc = SocialChimp(InMemoryStorage())

        with pytest.raises(ConfigError) as missing:
            await sc.account("conn-1").post(Post(text="hello"))

        assert "conn-1" in str(missing.value)

    async def test_posting_renews_the_token_first(self) -> None:
        storage = await storage_holding(expiring_soon())
        sc = SocialChimp(storage)

        await sc.account("conn-1").post(Post(text="hello"))

        sent, _ = made("fake").published[0]
        assert sent.token.access_token == NEW_ACCESS
        saved = await storage.get_connection("conn-1")
        assert saved is not None
        assert saved.token.access_token == NEW_ACCESS

    async def test_asking_for_limits_renews_the_token_first(self) -> None:
        storage = await storage_holding(expiring_soon())
        sc = SocialChimp(storage)

        limits = await sc.account("conn-1").limits()

        assert limits == Limits(max_text_length=50)
        assert made("fake").asked_limits[0].token.access_token == NEW_ACCESS

    async def test_deleting_a_post_renews_the_token_first(self) -> None:
        storage = await storage_holding(
            expiring_soon(connection_id="conn-d", platform="deleter")
        )
        sc = SocialChimp(storage)

        await sc.account("conn-d").delete_post("post-9")

        sent, post_id = made("deleter").deleted[0]
        assert sent.token.access_token == NEW_ACCESS
        assert post_id == "post-9"

    async def test_posting_hands_back_what_the_network_said(self) -> None:
        storage = await storage_holding(a_connection())
        sc = SocialChimp(storage)

        result = await sc.account("conn-1").post(Post(text="hello"))

        assert result == PostResult(id="post-conn-1")

    async def test_a_post_that_is_too_long_never_reaches_the_network(self) -> None:
        storage = await storage_holding(a_connection())
        sc = SocialChimp(storage)

        with pytest.raises(InvalidPostError) as refused:
            await sc.account("conn-1").post(Post(text="x" * 60))

        assert "at most 50" in str(refused.value)
        assert made("fake").published == []

    async def test_a_post_asking_to_be_scheduled_is_refused(self) -> None:
        storage = await storage_holding(a_connection())
        sc = SocialChimp(storage)
        later = datetime.now(UTC) + timedelta(days=1)

        with pytest.raises(NotSupportedError) as refused:
            await sc.account("conn-1").post(Post(text="hello", publish_at=later))

        assert "scheduling posts" in str(refused.value)
        assert made("fake").published == []

    async def test_deleting_where_the_network_cannot_says_so(self) -> None:
        storage = await storage_holding(
            a_connection(connection_id="conn-q", platform="quiet-deleter")
        )
        sc = SocialChimp(storage)

        with pytest.raises(NotSupportedError) as refused:
            await sc.account("conn-q").delete_post("post-9")

        assert "removing a post" in str(refused.value)
        assert made("quiet-deleter").deleted == []

    async def test_a_platform_that_claims_deleting_but_cannot_is_a_setup_problem(
        self,
    ) -> None:
        storage = await storage_holding(
            a_connection(connection_id="conn-l", platform="lying-deleter")
        )
        sc = SocialChimp(storage)

        with pytest.raises(ConfigError) as broken:
            await sc.account("conn-l").delete_post("post-9")

        assert "delete_post" in str(broken.value)

    async def test_you_can_read_the_connection_a_handle_stands_for(self) -> None:
        storage = await storage_holding(expiring_soon())
        sc = SocialChimp(storage)

        connection = await sc.account("conn-1").connection()

        assert connection.account_name == "someone"
        assert connection.token.access_token == NEW_ACCESS


class TestGoingDirect:
    async def test_a_direct_request_carries_a_freshly_renewed_token(self) -> None:
        storage = await storage_holding(expiring_soon())
        sc = SocialChimp(storage)

        with respx.mock(base_url=BASE) as network:
            route = network.post("/api/v1/statuses").mock(
                return_value=httpx.Response(200, json={"id": "1"})
            )

            reply = await sc.account("conn-1").direct.post(
                "/api/v1/statuses", json={"status": "hello"}
            )

        assert reply.json() == {"id": "1"}
        assert route.calls.last.request.headers["Authorization"] == (
            f"Bearer {NEW_ACCESS}"
        )

    async def test_headers_you_pass_win_over_the_ones_we_set(self) -> None:
        storage = await storage_holding(a_connection())
        sc = SocialChimp(storage)

        with respx.mock(base_url=BASE) as network:
            route = network.get("/me").mock(return_value=httpx.Response(200, json={}))

            await sc.account("conn-1").direct.get(
                "/me", headers={"Authorization": "Basic mine"}
            )

        assert route.calls.last.request.headers["Authorization"] == "Basic mine"

    async def test_every_way_of_sending_is_there(self) -> None:
        storage = await storage_holding(a_connection())
        sc = SocialChimp(storage)
        direct = sc.account("conn-1").direct

        with respx.mock(base_url=BASE) as network:
            route = network.route(path="/thing").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )

            await direct.get("/thing")
            await direct.post("/thing")
            await direct.put("/thing")
            await direct.delete("/thing")
            body = await direct.json("PATCH", "/thing")
            reply = await direct.request("HEAD", "/thing")

        assert body == {"ok": True}
        assert reply.status_code == 200
        assert [call.request.method for call in route.calls] == [
            "GET",
            "POST",
            "PUT",
            "DELETE",
            "PATCH",
            "HEAD",
        ]

    async def test_a_connection_with_no_host_needs_the_whole_address(self) -> None:
        storage = await storage_holding(a_connection(host=None))
        sc = SocialChimp(storage)

        with respx.mock() as network:
            route = network.get("https://elsewhere.example/me").mock(
                return_value=httpx.Response(200, json={})
            )

            await sc.account("conn-1").direct.get("https://elsewhere.example/me")

        assert route.called

    async def test_a_host_that_already_says_https_is_left_alone(self) -> None:
        storage = await storage_holding(a_connection(host=BASE))
        sc = SocialChimp(storage)

        with respx.mock(base_url=BASE) as network:
            route = network.get("/me").mock(return_value=httpx.Response(200, json={}))

            await sc.account("conn-1").direct.get("/me")

        assert route.called

    async def test_the_http_client_you_pass_in_is_the_one_used(self) -> None:
        storage = await storage_holding(a_connection())
        seen: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"ok": True})

        yours = HttpClient(
            "https://yours.example", transport=httpx.MockTransport(handle)
        )
        sc = SocialChimp(storage, http=yours)

        await sc.account("conn-1").direct.get("/me")

        assert [str(request.url) for request in seen] == ["https://yours.example/me"]


class TestPostingToManyAccounts:
    async def test_every_account_gets_its_own_result_in_order(self) -> None:
        storage = await storage_holding(
            a_connection(connection_id="conn-a", platform="slow"),
            a_connection(connection_id="conn-b"),
            a_connection(connection_id="conn-c"),
        )
        sc = SocialChimp(storage)

        job = await sc.post_to_many(["conn-a", "conn-b", "conn-c"], Post(text="hello"))

        assert job.connection_ids == ("conn-a", "conn-b", "conn-c")
        assert [result.id for result in job.succeeded] == [
            "post-conn-a",
            "post-conn-b",
            "post-conn-c",
        ]
        assert job.results == tuple(job.succeeded)

    async def test_one_account_failing_does_not_hide_the_others(self) -> None:
        storage = await storage_holding(
            a_connection(connection_id="conn-a"),
            a_connection(connection_id="conn-b", platform="broken"),
            a_connection(connection_id="conn-c"),
        )
        sc = SocialChimp(storage)

        job = await sc.post_to_many(["conn-a", "conn-b", "conn-c"], Post(text="hi"))

        first, second, third = job.results
        assert isinstance(first, PostResult)
        assert isinstance(third, PostResult)
        assert isinstance(second, PostError)
        assert second.connection_id == "conn-b"
        assert isinstance(second.error, PlatformError)
        assert job.failed == [second]
        assert job.succeeded == [first, third]

    async def test_a_missing_connection_is_one_account_failing(self) -> None:
        storage = await storage_holding(a_connection(connection_id="conn-a"))
        sc = SocialChimp(storage)

        job = await sc.post_to_many(["conn-a", "gone"], Post(text="hi"))

        assert [failure.connection_id for failure in job.failed] == ["gone"]
        assert isinstance(job.failed[0].error, ConfigError)

    async def test_options_for_one_network_are_added_to_the_post(self) -> None:
        storage = await storage_holding(a_connection())
        sc = SocialChimp(storage)
        post = Post(text="hi", options={"mine": "kept"})

        await sc.post_to_many(
            ["conn-1"],
            post,
            options_per_platform={"fake": {"board_id": "x"}, "other": {"no": "no"}},
        )

        _, sent = made("fake").published[0]
        assert sent.options == {"mine": "kept", "board_id": "x"}
        # The post you passed in is left exactly as it was.
        assert post.options == {"mine": "kept"}

    def test_the_repr_says_how_it_went(self) -> None:
        worked = PostResult(id="post-1")
        broke = PostError(connection_id="conn-b", error=ValueError("no"))

        assert repr(PostJob(connection_ids=("conn-a",), results=(worked,))) == (
            "PostJob(1 posted)"
        )
        assert repr(
            PostJob(connection_ids=("conn-a", "conn-b"), results=(worked, broke))
        ) == ("PostJob(1 posted, 1 failed: conn-b)")

    async def test_being_cancelled_is_passed_on_rather_than_written_down(self) -> None:
        storage = await storage_holding(
            a_connection(connection_id="conn-a"),
            a_connection(connection_id="conn-b", platform="cancels"),
        )
        sc = SocialChimp(storage)

        with pytest.raises(asyncio.CancelledError):
            await sc.post_to_many(["conn-a", "conn-b"], Post(text="hi"))


class TestKeepingTokensFresh:
    async def test_the_token_manager_you_pass_in_is_asked_every_time(self) -> None:
        storage = await storage_holding(
            a_connection(),
            a_connection(connection_id="conn-d", platform="deleter"),
        )
        tokens = CountingTokens(storage)
        sc = SocialChimp(storage, token_manager=tokens)

        await sc.account("conn-1").post(Post(text="hello"))
        await sc.account("conn-1").limits()
        await sc.account("conn-d").delete_post("post-9")

        assert tokens.asked == ["conn-1", "conn-1", "conn-d"]


class TestClosing:
    async def test_the_clients_it_made_are_closed_at_the_end(self) -> None:
        storage = await storage_holding(a_connection())

        async with SocialChimp(storage) as sc:
            with respx.mock(base_url=BASE) as network:
                network.get("/me").mock(return_value=httpx.Response(200, json={}))
                await sc.account("conn-1").direct.get("/me")

            # The only way to see what it made, and worth seeing: a client
            # left open holds connections open too.
            made_here = list(sc._http_made.values())
            assert [http.is_closed for http in made_here] == [False]

        assert [http.is_closed for http in made_here] == [True]

    async def test_a_client_you_passed_in_is_left_open(self) -> None:
        storage = await storage_holding(a_connection())
        yours = HttpClient(
            "https://yours.example",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        )

        async with SocialChimp(storage, http=yours) as sc:
            await sc.account("conn-1").direct.get("/me")

        assert yours.is_closed is False
        await yours.aclose()

    async def test_closing_by_hand_works_too_and_twice_is_quiet(self) -> None:
        storage = await storage_holding(a_connection())
        sc = SocialChimp(storage)
        with respx.mock(base_url=BASE) as network:
            network.get("/me").mock(return_value=httpx.Response(200, json={}))
            await sc.account("conn-1").direct.get("/me")
        made_here = list(sc._http_made.values())

        await sc.aclose()
        await sc.aclose()

        assert [http.is_closed for http in made_here] == [True]
