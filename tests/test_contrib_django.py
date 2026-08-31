from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from importlib import import_module
from typing import TYPE_CHECKING, Protocol, cast

import httpx
import pytest
import respx
from asgiref.sync import async_to_sync

from socialchimp.client import SocialChimp
from socialchimp.errors import ConfigError
from socialchimp.events import Dispatcher, UpdateKind
from socialchimp.http import HttpClient
from socialchimp.models import AppCredentials, Connection
from socialchimp.platform import AccountChoice
from socialchimp.testing import FakePlatform, RecordingStorage

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from socialchimp.events import Update


# --------------------------------------------------------------------------
# Django, reached the same way `socialchimp.contrib.django` reaches it: by
# name, with the shape we expect written down. Django ships no type
# information, so this is what keeps the test file checked as strictly as
# everything else.
# --------------------------------------------------------------------------


class Configurable(Protocol):
    configured: bool

    def configure(self, **options: object) -> None: ...

    def __getattr__(self, name: str) -> object: ...

    def __setattr__(self, name: str, value: object) -> None: ...

    def __delattr__(self, name: str) -> None: ...


class Startable(Protocol):
    def setup(self) -> None: ...


SETTINGS = cast("Configurable", import_module("django.conf").settings)

if not SETTINGS.configured:
    SETTINGS.configure(
        DEBUG=True,
        ALLOWED_HOSTS=["testserver"],
        DATABASES={},
        INSTALLED_APPS=[],
        MIDDLEWARE=["django.middleware.csrf.CsrfViewMiddleware"],
        ROOT_URLCONF=__name__,
        SECRET_KEY="not-a-real-secret",
        USE_TZ=True,
    )
    cast("Startable", import_module("django")).setup()


# Imported only after the settings above exist, which is Django's rule.
from socialchimp.contrib.django import (  # noqa: E402
    Request,
    View,
    get_client,
    orm_storage,
    urls,
)


class Answer(Protocol):
    status_code: int
    content: bytes
    headers: Mapping[str, str]


class Factory(Protocol):
    def get(
        self,
        path: str,
        data: Mapping[str, str] | None = ...,
    ) -> Request: ...

    def post(
        self,
        path: str,
        data: bytes = ...,
        content_type: str = ...,
        *,
        headers: Mapping[str, str] | None = ...,
    ) -> Request: ...


class Browser(Protocol):
    def post(
        self,
        path: str,
        data: bytes = ...,
        content_type: str = ...,
        *,
        headers: Mapping[str, str] | None = ...,
    ) -> Answer: ...


class MakeBrowser(Protocol):
    def __call__(self, *, enforce_csrf_checks: bool) -> Browser: ...


class Route(Protocol):
    name: str
    callback: View


_test = import_module("django.test")
make_factory = cast("Callable[[], Factory]", _test.RequestFactory)
make_browser = cast("MakeBrowser", _test.Client)

FORM = "application/x-www-form-urlencoded"

APP = AppCredentials(
    platform="fake",
    host=None,
    client_id="id",
    client_secret="secret",
)

REDIRECT = "https://app.example/callback/{platform}"


class WatchfulPlatform(FakePlatform):
    """Writes down the exact bytes its signature check was handed."""

    def __init__(self, *, accounts: tuple[AccountChoice, ...] = ()) -> None:
        super().__init__(accounts=accounts)
        self.checked: list[bytes] = []

    def check_signature(
        self,
        body: bytes,
        headers: Mapping[str, str],
        *,
        secret: str,
    ) -> None:
        self.checked.append(body)
        super().check_signature(body, headers, secret=secret)


class OrmStorage:
    """Five methods, written the way Django code is written."""

    def get_connection(self, connection_id: str) -> Connection | None:
        return None

    def save_connection(self, connection: Connection) -> None:
        saved.append(connection)

    def delete_connection(self, connection_id: str) -> None:
        raise NotImplementedError

    def get_app(self, platform: str, host: str | None) -> AppCredentials | None:
        threads_used.append(threading.current_thread().name)
        return APP if (platform, host) == APP.key else None

    def save_app(self, app: AppCredentials) -> None:
        raise NotImplementedError


class AwaitableStorage:
    """Five methods that are already async, the way most apps write them."""

    async def get_connection(self, connection_id: str) -> Connection | None:
        return None

    async def save_connection(self, connection: Connection) -> None:
        saved.append(connection)

    async def delete_connection(self, connection_id: str) -> None:
        raise NotImplementedError

    async def get_app(self, platform: str, host: str | None) -> AppCredentials | None:
        return APP if (platform, host) == APP.key else None

    async def save_app(self, app: AppCredentials) -> None:
        raise NotImplementedError


saved: list[Connection] = []
threads_used: list[str] = []


def update_body(text: str = "hello") -> bytes:
    return json.dumps(
        {
            "id": "u1",
            "kind": "comment_created",
            "connection_id": "c1",
            "at": datetime.now(UTC).isoformat(),
            "text": text,
        },
        ensure_ascii=False,
    ).encode()


def build(
    platform: FakePlatform,
    seen: list[Update],
    *,
    storage: object | None = None,
) -> list[object]:
    async def remember(update: Update) -> None:
        seen.append(update)

    dispatcher = Dispatcher()
    dispatcher.on(UpdateKind.COMMENT_CREATED, remember)

    sc = SocialChimp(
        storage=cast(
            "RecordingStorage",
            storage if storage is not None else RecordingStorage(apps=[APP]),
        ),
        platforms={"fake": platform},
    )
    return urls(
        sc,
        redirect_uri=REDIRECT,
        secrets={"fake": platform.secret},
        setup_tokens={"fake": "tok"},
        deliver=dispatcher.deliver,
    )


def view_named(patterns: list[object], name: str) -> View:
    for pattern in patterns:
        route = cast("Route", pattern)
        if route.name == name:
            return route.callback
    raise AssertionError(f"there is no {name} route")


def call(patterns: list[object], name: str, request: Request) -> Answer:
    return cast("Answer", view_named(patterns, name)(request, platform="fake"))


# The urlconf Django itself routes through, for the two tests that go all the
# way in and back rather than calling a view directly.
CSRF_PLATFORM = WatchfulPlatform()
CSRF_SEEN: list[Update] = []
urlpatterns = build(CSRF_PLATFORM, CSRF_SEEN)


@pytest.fixture
def factory() -> Factory:
    return make_factory()


@pytest.fixture
def fake() -> WatchfulPlatform:
    return WatchfulPlatform()


@pytest.fixture
def seen() -> list[Update]:
    return []


@pytest.fixture
def routes(fake: WatchfulPlatform, seen: list[Update]) -> list[object]:
    return build(fake, seen)


@pytest.fixture(autouse=True)
def _clean() -> None:
    saved.clear()
    threads_used.clear()
    get_client.cache_clear()


# --------------------------------------------------------------------------
# Signing in
# --------------------------------------------------------------------------


def test_connect_sends_the_person_to_the_network(
    routes: list[object],
    factory: Factory,
) -> None:
    answer = call(
        routes,
        "socialchimp-connect",
        factory.get("/connect/fake", {"state": "mine"}),
    )
    assert answer.status_code == 302
    assert answer.headers["Location"] == "https://fake.example/authorize?state=mine"


def test_the_callback_connects_the_account(
    routes: list[object],
    factory: Factory,
) -> None:
    call(routes, "socialchimp-connect", factory.get("/c/fake", {"state": "mine"}))

    answer = call(
        routes,
        "socialchimp-callback",
        factory.get("/cb/fake", {"state": "mine", "code": "c"}),
    )
    assert answer.status_code == 200
    assert json.loads(answer.content)["connection_id"] == "fake:42"


def test_the_callback_takes_a_posted_form_too(
    routes: list[object],
    factory: Factory,
) -> None:
    call(routes, "socialchimp-connect", factory.get("/c/fake", {"state": "mine"}))

    answer = call(
        routes,
        "socialchimp-callback",
        factory.post("/cb/fake", b"state=mine&code=c", FORM),
    )
    assert json.loads(answer.content)["step"] == "connected"


def test_the_callback_offers_the_accounts_to_choose_between(
    seen: list[Update],
    factory: Factory,
) -> None:
    routes = build(
        WatchfulPlatform(accounts=(AccountChoice(id="7", name="A Page"),)),
        seen,
    )
    call(routes, "socialchimp-connect", factory.get("/c/fake", {"state": "mine"}))

    answer = call(
        routes,
        "socialchimp-callback",
        factory.get("/cb/fake", {"state": "mine", "code": "c"}),
    )
    assert json.loads(answer.content) == {
        "step": "choose_account",
        "state": "mine",
        "options": [{"id": "7", "name": "A Page", "kind": None}],
    }


def test_choosing_an_account_finishes_the_sign_in(
    seen: list[Update],
    factory: Factory,
) -> None:
    routes = build(
        WatchfulPlatform(accounts=(AccountChoice(id="7", name="A Page"),)),
        seen,
    )
    call(routes, "socialchimp-connect", factory.get("/c/fake", {"state": "mine"}))
    call(
        routes,
        "socialchimp-callback",
        factory.get("/cb/fake", {"state": "mine", "code": "c"}),
    )

    answer = call(
        routes,
        "socialchimp-choose",
        factory.post("/ch/fake", b"state=mine&account_id=7", FORM),
    )
    assert json.loads(answer.content)["step"] == "connected"


def test_a_set_up_mistake_is_not_dressed_up_as_an_answer(
    seen: list[Update],
    factory: Factory,
) -> None:
    # No app credentials stored is a mistake in the app, not something this
    # request did, so it comes out as an error Django reports rather than a
    # tidy 500 that reads like the network's fault.
    routes = build(FakePlatform(), seen, storage=RecordingStorage())

    with pytest.raises(ConfigError, match="No app credentials"):
        call(routes, "socialchimp-connect", factory.get("/c/fake"))


# --------------------------------------------------------------------------
# Webhooks
# --------------------------------------------------------------------------


def test_a_signed_webhook_is_accepted(
    routes: list[object],
    fake: WatchfulPlatform,
    seen: list[Update],
    factory: Factory,
) -> None:
    body = update_body()
    answer = call(
        routes,
        "socialchimp-webhook",
        factory.post("/w/fake", body, "application/json", headers=fake.sign(body)),
    )
    assert answer.status_code == 200
    assert [update.id for update in seen] == ["u1"]


def test_a_tampered_webhook_is_refused(
    routes: list[object],
    fake: WatchfulPlatform,
    seen: list[Update],
    factory: Factory,
) -> None:
    body = update_body()
    headers = fake.sign(body)
    answer = call(
        routes,
        "socialchimp-webhook",
        factory.post(
            "/w/fake",
            body.replace(b"hello", b"hellp"),
            "application/json",
            headers=headers,
        ),
    )
    assert answer.status_code == 401
    assert seen == []


def test_the_raw_body_reaches_the_signature_check_unchanged(
    routes: list[object],
    fake: WatchfulPlatform,
    seen: list[Update],
    factory: Factory,
) -> None:
    # Non-ASCII on purpose. A signature is over the bytes that were sent, so
    # anything that decodes and re-encodes the body on the way in breaks it,
    # and this is where that shows up.
    body = update_body("café ☕ 日本語")
    answer = call(
        routes,
        "socialchimp-webhook",
        factory.post("/w/fake", body, "application/json", headers=fake.sign(body)),
    )
    assert answer.status_code == 200
    assert fake.checked == [body]
    assert seen[0].raw["text"] == "café ☕ 日本語"


def test_the_setup_check_answers_with_the_challenge(
    routes: list[object],
    factory: Factory,
) -> None:
    answer = call(
        routes,
        "socialchimp-webhook",
        factory.get(
            "/w/fake",
            {
                "hub.mode": "subscribe",
                "hub.verify_token": "tok",
                "hub.challenge": "1234",
            },
        ),
    )
    assert answer.status_code == 200
    assert answer.content == b"1234"
    assert answer.headers["Content-Type"].startswith("text/plain")


def test_the_setup_check_refuses_a_wrong_token(
    routes: list[object],
    factory: Factory,
) -> None:
    answer = call(
        routes,
        "socialchimp-webhook",
        factory.get(
            "/w/fake",
            {
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong",
                "hub.challenge": "1234",
            },
        ),
    )
    assert answer.status_code == 403


# --------------------------------------------------------------------------
# CSRF, all the way through Django's own middleware
# --------------------------------------------------------------------------


def test_a_network_can_post_a_webhook_without_one_of_djangos_tokens() -> None:
    body = update_body()
    answer = make_browser(enforce_csrf_checks=True).post(
        "/webhooks/fake",
        body,
        "application/json",
        headers=CSRF_PLATFORM.sign(body),
    )
    assert answer.status_code == 200
    assert [update.id for update in CSRF_SEEN] == ["u1"]


def test_your_own_forms_still_need_one() -> None:
    browser = make_browser(enforce_csrf_checks=True)
    answer = browser.post("/choose/fake", b"state=x&account_id=7", FORM)
    assert answer.status_code == 403


# --------------------------------------------------------------------------
# Going direct from an ordinary sync view
# --------------------------------------------------------------------------


def test_each_request_gets_an_http_client_made_on_its_own_loop() -> None:
    # async_to_sync builds an event loop for each call and closes it at the
    # end, so this is what several requests in a row look like under WSGI. A
    # client kept from an earlier one belongs to a loop that has gone, and so
    # do the sockets it is holding open.
    fake = FakePlatform()
    connection = fake.connection()
    sc = SocialChimp(
        storage=RecordingStorage(connections=[connection]),
        platforms={"fake": fake},
    )
    used: list[HttpClient] = []

    async def one_request() -> None:
        with respx.mock(base_url=fake.api_base(connection)) as network:
            network.get("/me").mock(return_value=httpx.Response(200, json={}))
            await sc.account(connection.id).direct.get("/me")
        used.append(sc.http_for(connection))

    for _ in range(3):
        async_to_sync(one_request)()

    assert len({id(http) for http in used}) == 3
    # And the finished loops took their clients with them.
    assert len(sc._http_made) == 1


# --------------------------------------------------------------------------
# Storage written as ordinary Django ORM code
# --------------------------------------------------------------------------


def test_orm_code_runs_on_the_thread_the_request_arrived_on(
    seen: list[Update],
    factory: Factory,
) -> None:
    routes = build(FakePlatform(), seen, storage=orm_storage(OrmStorage()))

    answer = call(
        routes,
        "socialchimp-connect",
        factory.get("/c/fake", {"state": "mine"}),
    )
    assert answer.status_code == 302
    # The point of thread_sensitive=True. Anywhere else and the ORM would be
    # on a second database connection, outside the request's transaction.
    assert threads_used == [threading.current_thread().name]


def test_orm_code_can_be_awaited_directly_too() -> None:
    storage = orm_storage(OrmStorage())
    assert async_to_sync(storage.get_app)("fake", None) == APP


# --------------------------------------------------------------------------
# get_client
# --------------------------------------------------------------------------


def set_setting(value: object) -> None:
    SETTINGS.SOCIALCHIMP = value


def clear_setting() -> None:
    del SETTINGS.SOCIALCHIMP


def test_get_client_wraps_a_storage_class_written_as_orm_code() -> None:
    set_setting({"SYNC_STORAGE": f"{__name__}.OrmStorage"})
    try:
        # A class written the blocking way, now awaited without complaint.
        assert async_to_sync(get_client().storage.get_app)("fake", None) == APP
    finally:
        clear_setting()


def test_get_client_takes_an_async_storage_class_as_it_is() -> None:
    set_setting({"STORAGE": f"{__name__}.AwaitableStorage"})
    try:
        assert isinstance(get_client().storage, AwaitableStorage)
    finally:
        clear_setting()


def test_get_client_keeps_the_one_client() -> None:
    set_setting({"STORAGE": f"{__name__}.AwaitableStorage"})
    try:
        assert get_client() is get_client()
    finally:
        clear_setting()


def test_get_client_says_so_when_the_setting_is_missing() -> None:
    with pytest.raises(ConfigError, match="should be a dict"):
        get_client()


def test_get_client_says_so_when_the_setting_is_the_wrong_shape() -> None:
    set_setting("myapp.social.MyStorage")
    try:
        with pytest.raises(ConfigError, match="should be a dict"):
            get_client()
    finally:
        clear_setting()


def test_get_client_says_so_when_neither_kind_is_named() -> None:
    set_setting({})
    try:
        with pytest.raises(ConfigError, match="It names neither"):
            get_client()
    finally:
        clear_setting()


def test_get_client_says_so_when_both_kinds_are_named() -> None:
    set_setting(
        {
            "STORAGE": f"{__name__}.AwaitableStorage",
            "SYNC_STORAGE": f"{__name__}.OrmStorage",
        }
    )
    try:
        with pytest.raises(ConfigError, match="It names both"):
            get_client()
    finally:
        clear_setting()


def test_get_client_says_so_when_the_path_has_no_dot_in_it() -> None:
    set_setting({"STORAGE": "MyStorage"})
    try:
        with pytest.raises(ConfigError, match="not a dotted path"):
            get_client()
    finally:
        clear_setting()


def test_get_client_says_so_when_nothing_is_at_that_path() -> None:
    set_setting({"STORAGE": f"{__name__}.NoSuchStorage"})
    try:
        with pytest.raises(ConfigError, match="There is no class at"):
            get_client()
    finally:
        clear_setting()


def test_get_client_says_so_when_the_module_is_not_there() -> None:
    set_setting({"STORAGE": "no_such_module_anywhere.MyStorage"})
    try:
        with pytest.raises(ConfigError, match="There is no class at"):
            get_client()
    finally:
        clear_setting()
