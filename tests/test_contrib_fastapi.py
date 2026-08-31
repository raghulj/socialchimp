from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from socialchimp.client import SocialChimp
from socialchimp.contrib.fastapi import router
from socialchimp.events import Dispatcher, UpdateKind
from socialchimp.models import AppCredentials
from socialchimp.platform import AccountChoice, Finished
from socialchimp.testing import FakePlatform, RecordingStorage

if TYPE_CHECKING:
    from collections.abc import Mapping

    from socialchimp.events import Update
    from socialchimp.models import RawData
    from socialchimp.platform import LoginRequest, LoginStep

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


class ChoosyPlatform(WatchfulPlatform):
    """A network that asks which page to use, the way Facebook does."""

    async def resume_login(
        self,
        request: LoginRequest,
        *,
        resume_token: str,
        account_id: str,
        remember: RawData | None = None,
    ) -> LoginStep:
        return Finished(connection=self.connection(account_id=account_id))


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


def build(platform: FakePlatform, seen: list[Update]) -> TestClient:
    async def remember(update: Update) -> None:
        seen.append(update)

    dispatcher = Dispatcher()
    dispatcher.on(UpdateKind.COMMENT_CREATED, remember)

    sc = SocialChimp(
        storage=RecordingStorage(apps=[APP]),
        platforms={"fake": platform},
    )
    app = FastAPI()
    app.include_router(
        router(
            sc,
            redirect_uri=REDIRECT,
            secrets={"fake": platform.secret},
            setup_tokens={"fake": "tok"},
            deliver=dispatcher.deliver,
        ),
        prefix="/social",
    )
    return TestClient(app)


@pytest.fixture
def client(fake: WatchfulPlatform, seen: list[Update]) -> TestClient:
    return build(fake, seen)


# --------------------------------------------------------------------------
# Signing in
# --------------------------------------------------------------------------


def test_connect_sends_the_person_to_the_network(client: TestClient) -> None:
    answer = client.get(
        "/social/connect/fake",
        params={"state": "mine"},
        follow_redirects=False,
    )
    assert answer.status_code == 302
    assert answer.headers["Location"] == "https://fake.example/authorize?state=mine"


def test_the_callback_connects_the_account(client: TestClient) -> None:
    client.get("/social/connect/fake", params={"state": "mine"}, follow_redirects=False)

    answer = client.get("/social/callback/fake", params={"state": "mine", "code": "c"})
    assert answer.status_code == 200
    assert answer.json()["step"] == "connected"
    assert answer.json()["connection_id"] == "fake-connection"


def test_the_callback_takes_a_posted_form_too(client: TestClient) -> None:
    client.get("/social/connect/fake", params={"state": "mine"}, follow_redirects=False)

    answer = client.post(
        "/social/callback/fake",
        content=b"state=mine&code=c",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert answer.json()["step"] == "connected"


def test_the_callback_offers_the_accounts_to_choose_between(
    seen: list[Update],
) -> None:
    choosy = ChoosyPlatform(accounts=(AccountChoice(id="7", name="A Page"),))
    client = build(choosy, seen)
    client.get("/social/connect/fake", params={"state": "mine"}, follow_redirects=False)

    answer = client.get("/social/callback/fake", params={"state": "mine", "code": "c"})
    assert answer.json() == {
        "step": "choose_account",
        "state": "mine",
        "options": [{"id": "7", "name": "A Page", "kind": None}],
    }


def test_choosing_an_account_finishes_the_sign_in(seen: list[Update]) -> None:
    choosy = ChoosyPlatform(accounts=(AccountChoice(id="7", name="A Page"),))
    client = build(choosy, seen)
    client.get("/social/connect/fake", params={"state": "mine"}, follow_redirects=False)
    client.get("/social/callback/fake", params={"state": "mine", "code": "c"})

    answer = client.post(
        "/social/choose/fake",
        content=b"state=mine&account_id=7",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert answer.json()["step"] == "connected"


def test_our_errors_become_sensible_statuses(seen: list[Update]) -> None:
    sc = SocialChimp(storage=RecordingStorage(), platforms={"fake": FakePlatform()})
    app = FastAPI()
    app.include_router(router(sc, redirect_uri=REDIRECT))
    client = TestClient(app)

    answer = client.get("/connect/fake", follow_redirects=False)
    assert answer.status_code == 500
    assert "No app credentials" in answer.json()["error"]


def test_a_callback_with_no_sign_in_waiting_is_refused(client: TestClient) -> None:
    answer = client.get("/social/callback/fake", params={"state": "invented"})
    assert answer.status_code == 400


# --------------------------------------------------------------------------
# Webhooks
# --------------------------------------------------------------------------


def test_a_signed_webhook_is_accepted(
    client: TestClient,
    fake: WatchfulPlatform,
    seen: list[Update],
) -> None:
    body = update_body()
    answer = client.post("/social/webhooks/fake", content=body, headers=fake.sign(body))
    assert answer.status_code == 200
    assert answer.json() == {"ok": True}
    assert [update.id for update in seen] == ["u1"]


def test_a_tampered_webhook_is_refused(
    client: TestClient,
    fake: WatchfulPlatform,
    seen: list[Update],
) -> None:
    body = update_body()
    headers = fake.sign(body)
    answer = client.post(
        "/social/webhooks/fake",
        content=body.replace(b"hello", b"hellp"),
        headers=headers,
    )
    assert answer.status_code == 401
    assert seen == []


def test_the_raw_body_reaches_the_signature_check_unchanged(
    client: TestClient,
    fake: WatchfulPlatform,
    seen: list[Update],
) -> None:
    # Non-ASCII on purpose. A signature is over the bytes that were sent, so
    # anything that decodes and re-encodes the body on the way in breaks it,
    # and this is where that shows up.
    body = update_body("café ☕ 日本語")
    assert body != body.decode().encode("latin-1", "replace")

    answer = client.post("/social/webhooks/fake", content=body, headers=fake.sign(body))
    assert answer.status_code == 200
    assert fake.checked == [body]
    assert seen[0].raw["text"] == "café ☕ 日本語"


def test_the_setup_check_answers_with_the_challenge(client: TestClient) -> None:
    answer = client.get(
        "/social/webhooks/fake",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "tok",
            "hub.challenge": "1234",
        },
    )
    assert answer.status_code == 200
    assert answer.text == "1234"
    assert answer.headers["content-type"].startswith("text/plain")


def test_the_setup_check_refuses_a_wrong_token(client: TestClient) -> None:
    answer = client.get(
        "/social/webhooks/fake",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "1234",
        },
    )
    assert answer.status_code == 403
