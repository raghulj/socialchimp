from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from flask import Flask

from socialchimp.client import SocialChimp
from socialchimp.contrib.flask import blueprint, run
from socialchimp.contrib.shared import sync_storage
from socialchimp.events import Dispatcher, UpdateKind
from socialchimp.models import AppCredentials, Connection, Token
from socialchimp.platform import AccountChoice
from socialchimp.testing import FakePlatform, RecordingStorage

if TYPE_CHECKING:
    from collections.abc import Mapping

    from flask.testing import FlaskClient

    from socialchimp.events import Update

APP = AppCredentials(
    platform="fake",
    host=None,
    client_id="id",
    client_secret="secret",
)

REDIRECT = "https://app.example/callback/{platform}"

FORM = "application/x-www-form-urlencoded"


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


@pytest.fixture
def fake() -> WatchfulPlatform:
    return WatchfulPlatform()


@pytest.fixture
def seen() -> list[Update]:
    return []


def build(platform: FakePlatform, seen: list[Update], *, name: str) -> FlaskClient:
    async def remember(update: Update) -> None:
        seen.append(update)

    dispatcher = Dispatcher()
    dispatcher.on(UpdateKind.COMMENT_CREATED, remember)

    sc = SocialChimp(
        storage=RecordingStorage(apps=[APP]),
        platforms={"fake": platform},
    )
    app = Flask(name)
    app.register_blueprint(
        blueprint(
            sc,
            redirect_uri=REDIRECT,
            secrets={"fake": platform.secret},
            setup_tokens={"fake": "tok"},
            deliver=dispatcher.deliver,
        ),
        url_prefix="/social",
    )
    return app.test_client()


@pytest.fixture
def client(fake: WatchfulPlatform, seen: list[Update]) -> FlaskClient:
    return build(fake, seen, name="one")


# --------------------------------------------------------------------------
# Signing in
# --------------------------------------------------------------------------


def test_connect_sends_the_person_to_the_network(client: FlaskClient) -> None:
    answer = client.get("/social/connect/fake?state=mine")
    assert answer.status_code == 302
    assert answer.headers["Location"] == "https://fake.example/authorize?state=mine"


def test_the_callback_connects_the_account(client: FlaskClient) -> None:
    client.get("/social/connect/fake?state=mine")

    answer = client.get("/social/callback/fake?state=mine&code=c")
    assert answer.status_code == 200
    assert answer.get_json()["step"] == "connected"
    assert answer.get_json()["connection_id"] == "fake-connection"


def test_the_callback_takes_a_posted_form_too(client: FlaskClient) -> None:
    client.get("/social/connect/fake?state=mine")

    answer = client.post(
        "/social/callback/fake",
        data=b"state=mine&code=c",
        content_type=FORM,
    )
    assert answer.get_json()["step"] == "connected"


def test_the_callback_offers_the_accounts_to_choose_between(
    seen: list[Update],
) -> None:
    choosy = WatchfulPlatform(accounts=(AccountChoice(id="7", name="A Page"),))
    client = build(choosy, seen, name="two")
    client.get("/social/connect/fake?state=mine")

    answer = client.get("/social/callback/fake?state=mine&code=c")
    assert answer.get_json() == {
        "step": "choose_account",
        "state": "mine",
        "options": [{"id": "7", "name": "A Page", "kind": None}],
    }


def test_choosing_an_account_finishes_the_sign_in(seen: list[Update]) -> None:
    choosy = WatchfulPlatform(accounts=(AccountChoice(id="7", name="A Page"),))
    client = build(choosy, seen, name="three")
    client.get("/social/connect/fake?state=mine")
    client.get("/social/callback/fake?state=mine&code=c")

    answer = client.post(
        "/social/choose/fake",
        data=b"state=mine&account_id=7",
        content_type=FORM,
    )
    assert answer.get_json()["step"] == "connected"


def test_our_errors_become_sensible_statuses() -> None:
    sc = SocialChimp(storage=RecordingStorage(), platforms={"fake": FakePlatform()})
    app = Flask("four")
    app.register_blueprint(blueprint(sc, redirect_uri=REDIRECT))

    answer = app.test_client().get("/connect/fake")
    assert answer.status_code == 500
    assert "No app credentials" in answer.get_json()["error"]


def test_a_callback_with_no_sign_in_waiting_is_refused(client: FlaskClient) -> None:
    answer = client.get("/social/callback/fake?state=invented")
    assert answer.status_code == 400


# --------------------------------------------------------------------------
# Webhooks
# --------------------------------------------------------------------------


def test_a_signed_webhook_is_accepted(
    client: FlaskClient,
    fake: WatchfulPlatform,
    seen: list[Update],
) -> None:
    body = update_body()
    answer = client.post("/social/webhooks/fake", data=body, headers=fake.sign(body))
    assert answer.status_code == 200
    assert answer.get_json() == {"ok": True}
    assert [update.id for update in seen] == ["u1"]


def test_a_tampered_webhook_is_refused(
    client: FlaskClient,
    fake: WatchfulPlatform,
    seen: list[Update],
) -> None:
    body = update_body()
    headers = fake.sign(body)
    answer = client.post(
        "/social/webhooks/fake",
        data=body.replace(b"hello", b"hellp"),
        headers=headers,
    )
    assert answer.status_code == 401
    assert seen == []


def test_the_raw_body_reaches_the_signature_check_unchanged(
    client: FlaskClient,
    fake: WatchfulPlatform,
    seen: list[Update],
) -> None:
    # Non-ASCII on purpose. A signature is over the bytes that were sent, so
    # anything that decodes and re-encodes the body on the way in breaks it,
    # and this is where that shows up.
    body = update_body("café ☕ 日本語")
    answer = client.post("/social/webhooks/fake", data=body, headers=fake.sign(body))
    assert answer.status_code == 200
    assert fake.checked == [body]
    assert seen[0].raw["text"] == "café ☕ 日本語"


def test_the_setup_check_answers_with_the_challenge(client: FlaskClient) -> None:
    answer = client.get(
        "/social/webhooks/fake"
        "?hub.mode=subscribe&hub.verify_token=tok&hub.challenge=1234"
    )
    assert answer.status_code == 200
    assert answer.data == b"1234"
    assert answer.headers["Content-Type"].startswith("text/plain")


def test_the_setup_check_refuses_a_wrong_token(client: FlaskClient) -> None:
    answer = client.get(
        "/social/webhooks/fake"
        "?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=1234"
    )
    assert answer.status_code == 403


# --------------------------------------------------------------------------
# The bridge from Flask's threads to the async client
# --------------------------------------------------------------------------


def test_the_work_happens_off_the_request_thread(client: FlaskClient) -> None:
    client.get("/social/connect/fake?state=mine")
    client.get("/social/callback/fake?state=mine&code=c")

    running = [one.name for one in threading.enumerate()]
    assert any(name.startswith("socialchimp") for name in running)


def test_a_blocking_storage_class_works_here_too(seen: list[Update]) -> None:
    class PlainStorage:
        def __init__(self) -> None:
            self.connections: dict[str, Connection] = {}

        def get_connection(self, connection_id: str) -> Connection | None:
            return self.connections.get(connection_id)

        def save_connection(self, connection: Connection) -> None:
            self.connections[connection.id] = connection

        def delete_connection(self, connection_id: str) -> None:
            self.connections.pop(connection_id, None)

        def get_app(self, platform: str, host: str | None) -> AppCredentials | None:
            return APP if (platform, host) == APP.key else None

        def save_app(self, app: AppCredentials) -> None:
            raise NotImplementedError

    plain = PlainStorage()
    sc = SocialChimp(
        storage=sync_storage(plain),
        platforms={"fake": FakePlatform()},
    )
    app = Flask("five")
    app.register_blueprint(blueprint(sc, redirect_uri=REDIRECT))
    client = app.test_client()

    client.get("/connect/fake?state=mine")
    answer = client.get("/callback/fake?state=mine&code=c")
    assert answer.status_code == 200
    assert plain.connections["fake-connection"].token == Token(
        access_token="fake-access",
        refresh_token="fake-refresh",
        expires_at=plain.connections["fake-connection"].token.expires_at,
    )


def test_your_own_views_can_use_the_same_bridge() -> None:
    async def work() -> str:
        return "done"

    assert run(work()) == "done"
