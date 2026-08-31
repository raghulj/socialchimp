from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from socialchimp import storage
from socialchimp.client import SocialChimp
from socialchimp.contrib import shared
from socialchimp.contrib.shared import (
    InMemoryLoginMemory,
    Reply,
    Routes,
    read_form,
    status_for,
)
from socialchimp.errors import (
    AuthError,
    ConfigError,
    InvalidPostError,
    NetworkError,
    NotAllowedError,
    NotFoundError,
    NotSupportedError,
    PlatformError,
    RateLimitError,
    SignatureError,
    SocialChimpError,
    TokenExpiredError,
)
from socialchimp.features import Feature
from socialchimp.models import AppCredentials, Connection, Token
from socialchimp.platform import (
    AccountChoice,
    ChooseAccount,
    LoginField,
)
from socialchimp.testing import FakePlatform, RecordingStorage

if TYPE_CHECKING:
    from collections.abc import Mapping

    from socialchimp.events import Update
    from socialchimp.features import Limits
    from socialchimp.models import Post, PostResult, RawData
    from socialchimp.platform import LoginRequest, LoginStep

APP = AppCredentials(
    platform="fake",
    host=None,
    client_id="id",
    client_secret="secret",
)

REDIRECT = "https://app.example/callback/{platform}"

UPDATE_BODY = json.dumps(
    {
        "id": "u1",
        "kind": "comment_created",
        "connection_id": "c1",
        "at": datetime.now(UTC).isoformat(),
    }
).encode()


class NeverCarriesOn(FakePlatform):
    """Asks which page to use and then cannot carry on. Wrong on purpose.

    A well-behaved fake given `accounts` can resume, which is the point of
    `FakePlatform`. This is the platform `PlatformChecks` exists to catch,
    and the only way to see what socialchimp does when it meets one.
    """

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> LoginStep:
        return ChooseAccount(
            options=(AccountChoice(id="7", name="A Page"),),
            resume_token="carry-on",
        )


class LyingPusher:
    """Says it can push updates, but has no way to check a signature."""

    name = "lying-pusher"
    features = Feature.POST_TEXT | Feature.PUSH_UPDATES

    def api_base(self, connection: Connection) -> str:
        raise NotImplementedError

    def auth_headers(self, connection: Connection) -> Mapping[str, str]:
        raise NotImplementedError

    async def limits(self, connection: Connection) -> Limits:
        raise NotImplementedError

    async def start_login(self, request: LoginRequest) -> LoginStep:
        raise NotImplementedError

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> LoginStep:
        raise NotImplementedError

    async def refresh(
        self,
        connection: Connection,
        app: AppCredentials | None = None,
    ) -> Token:
        raise NotImplementedError

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        raise NotImplementedError


def make_routes(
    platform: FakePlatform | None = None,
    *,
    collected: list[Update] | None = None,
    secrets: dict[str, str] | None = None,
    setup_tokens: dict[str, str] | None = None,
    with_deliver: bool = True,
    memory: InMemoryLoginMemory | None = None,
) -> tuple[Routes, FakePlatform]:
    fake = platform if platform is not None else FakePlatform()
    sc = SocialChimp(
        storage=RecordingStorage(apps=[APP]),
        platforms={"fake": fake, "lying-pusher": LyingPusher()},
    )

    async def deliver(update: Update) -> None:
        if collected is not None:
            collected.append(update)

    return (
        Routes(
            sc,
            redirect_uri=REDIRECT,
            memory=memory,
            secrets=secrets if secrets is not None else {"fake": fake.secret},
            setup_tokens=setup_tokens if setup_tokens is not None else {"fake": "tok"},
            deliver=deliver if with_deliver else None,
        ),
        fake,
    )


def body_of(reply: Reply) -> dict[str, object]:
    parsed = json.loads(reply.body)
    assert isinstance(parsed, dict)
    return parsed


def a_connection() -> Connection:
    return Connection(
        id="c1",
        platform="fake",
        host=None,
        account_id="1",
        account_name="someone",
        token=Token(access_token="a"),
    )


# --------------------------------------------------------------------------
# Reply and status codes
# --------------------------------------------------------------------------


def test_reply_json_carries_the_json_content_type() -> None:
    reply = Reply.json({"a": 1})
    assert reply.status == 200
    assert reply.content_type == "application/json"
    assert json.loads(reply.body) == {"a": 1}


def test_reply_text_is_plain_text() -> None:
    reply = Reply.text("challenge-value")
    assert reply.body == b"challenge-value"
    assert reply.content_type == "text/plain; charset=utf-8"


def test_reply_redirect_says_where_to_go() -> None:
    reply = Reply.redirect("https://network.example/authorize")
    assert reply.status == 302
    assert reply.headers["Location"] == "https://network.example/authorize"
    assert reply.body == b""


def test_reply_for_a_rate_limit_says_how_long_to_wait() -> None:
    reply = Reply.for_error(RateLimitError("slow down", retry_after=12.4))
    assert reply.status == 429
    assert reply.headers["Retry-After"] == "13"


def test_reply_for_a_rate_limit_with_no_wait_has_no_header() -> None:
    reply = Reply.for_error(RateLimitError("slow down"))
    assert reply.status == 429
    assert "Retry-After" not in reply.headers


def test_reply_for_a_bad_signature_says_nothing_about_which_check_failed() -> None:
    reply = Reply.for_error(SignatureError("the sha256 digest did not match"))
    assert reply.status == 401
    assert "sha256" not in reply.body.decode()


def test_reply_for_an_error_names_the_network() -> None:
    reply = Reply.for_error(NotFoundError("no such post", platform="fake"))
    assert body_of(reply) == {"error": "no such post", "platform": "fake"}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (SignatureError("no"), 401),
        (TokenExpiredError("no"), 401),
        (AuthError("no"), 401),
        (NotAllowedError("no"), 403),
        (NotFoundError("no"), 404),
        (RateLimitError("no"), 429),
        (InvalidPostError("no"), 400),
        (NotSupportedError(platform="fake", what="that"), 400),
        (NetworkError("no"), 502),
        (PlatformError("no", platform="fake"), 502),
        (ConfigError("no"), 500),
        (SocialChimpError("no"), 500),
    ],
)
def test_every_error_has_a_sensible_status(
    error: SocialChimpError,
    expected: int,
) -> None:
    assert status_for(error) == expected


# --------------------------------------------------------------------------
# read_form
# --------------------------------------------------------------------------


def test_read_form_reads_values_a_browser_posted() -> None:
    assert read_form(b"state=abc&account_id=7") == {"state": "abc", "account_id": "7"}


def test_read_form_keeps_non_ascii_intact() -> None:
    assert read_form(b"name=caf%C3%A9") == {"name": "café"}


def test_read_form_of_an_empty_body_is_empty() -> None:
    assert read_form(b"") == {}


# --------------------------------------------------------------------------
# Where a half-finished sign-in is kept
# --------------------------------------------------------------------------


async def test_login_memory_hands_back_what_it_was_given() -> None:
    memory = InMemoryLoginMemory()
    await memory.keep("s", {"remember": {"v": 1}})
    assert await memory.look_up("s") == {"remember": {"v": 1}}


async def test_login_memory_has_nothing_for_a_state_it_never_saw() -> None:
    assert await InMemoryLoginMemory().look_up("nope") is None


async def test_login_memory_forgets_and_forgetting_twice_is_quiet() -> None:
    memory = InMemoryLoginMemory()
    await memory.keep("s", {})
    await memory.forget("s")
    await memory.forget("s")
    assert await memory.look_up("s") is None


async def test_login_memory_drops_the_oldest_when_it_is_full() -> None:
    memory = InMemoryLoginMemory(max_size=2)
    await memory.keep("a", {})
    await memory.keep("b", {})
    await memory.keep("c", {})
    assert await memory.look_up("a") is None
    assert await memory.look_up("c") == {}


# --------------------------------------------------------------------------
# Storage written the ordinary blocking way, which lives in the core now
# --------------------------------------------------------------------------


def test_the_blocking_storage_names_are_still_reachable_from_here() -> None:
    # They moved to socialchimp.storage, and plenty of apps already import
    # them from this module. Both spellings have to go on meaning the same
    # four things.
    assert shared.SyncStorage is storage.SyncStorage
    assert shared.sync_storage is storage.sync_storage
    assert shared.RunInThread is storage.RunInThread
    assert shared.in_a_thread is storage.in_a_thread


# --------------------------------------------------------------------------
# Signing in
# --------------------------------------------------------------------------


async def test_start_sends_the_person_to_the_network() -> None:
    routes, _ = make_routes()
    reply = await routes.start("fake", {})
    assert reply.status == 302
    assert "authorize" in reply.headers["Location"]


async def test_start_uses_the_state_the_app_asked_for() -> None:
    routes, _ = make_routes()
    reply = await routes.start("fake", {"state": "mine"})
    assert "state=mine" in reply.headers["Location"]


async def test_start_makes_a_state_when_the_app_did_not() -> None:
    memory = InMemoryLoginMemory()
    routes, _ = make_routes(memory=memory)
    reply = await routes.start("fake", {})
    made = reply.headers["Location"].partition("state=")[2]
    assert await memory.look_up(made) is not None


async def test_start_asks_for_details_when_there_is_no_sign_in_page() -> None:
    routes, _ = make_routes(FakePlatform(ask_for=(LoginField("handle", "Handle"),)))
    reply = await routes.start("fake", {"state": "mine"})
    assert body_of(reply) == {
        "step": "ask_for_details",
        "state": "mine",
        "help_url": "https://fake.example/help/signing-in",
        "fields": [
            {
                "name": "handle",
                "label": "Handle",
                "secret": False,
                "help_text": None,
            }
        ],
    }


async def test_start_says_so_when_the_app_is_not_registered_yet() -> None:
    sc = SocialChimp(storage=RecordingStorage(), platforms={"fake": FakePlatform()})
    routes = Routes(sc, redirect_uri=REDIRECT)
    reply = await routes.start("fake", {})
    assert reply.status == 500
    assert "No app credentials" in str(body_of(reply)["error"])


async def test_the_callback_connects_the_account() -> None:
    routes, _ = make_routes()
    assert (await routes.start("fake", {"state": "mine"})).status == 302

    reply = await routes.finish("fake", {"state": "mine", "code": "c"})
    assert reply.status == 200
    assert body_of(reply) == {
        "step": "connected",
        "connection_id": "fake-connection",
        "platform": "fake",
        "account_name": "someone@fake.example",
    }


async def test_the_callback_carries_what_start_asked_us_to_keep() -> None:
    routes, fake = make_routes()
    await routes.start("fake", {"state": "mine"})
    await routes.finish("fake", {"state": "mine", "code": "c"})
    assert fake.last_remember == {"verifier": "fake-verifier"}


async def test_the_callback_needs_a_state() -> None:
    routes, _ = make_routes()
    reply = await routes.finish("fake", {"code": "c"})
    assert reply.status == 400


async def test_the_callback_refuses_a_state_it_never_gave_out() -> None:
    routes, _ = make_routes()
    reply = await routes.finish("fake", {"state": "invented", "code": "c"})
    assert reply.status == 400
    assert "start" in str(body_of(reply)["error"])


async def test_a_finished_sign_in_is_forgotten() -> None:
    memory = InMemoryLoginMemory()
    routes, _ = make_routes(memory=memory)
    await routes.start("fake", {"state": "mine"})
    await routes.finish("fake", {"state": "mine", "code": "c"})
    assert await memory.look_up("mine") is None


async def test_the_callback_offers_the_accounts_to_choose_between() -> None:
    routes, _ = make_routes(
        FakePlatform(accounts=(AccountChoice(id="7", name="A Page", kind="page"),))
    )
    await routes.start("fake", {"state": "mine"})
    reply = await routes.finish("fake", {"state": "mine", "code": "c"})
    assert body_of(reply) == {
        "step": "choose_account",
        "state": "mine",
        "options": [{"id": "7", "name": "A Page", "kind": "page"}],
    }


async def test_the_resume_token_never_reaches_the_browser() -> None:
    routes, _ = make_routes(
        FakePlatform(accounts=(AccountChoice(id="7", name="A Page"),))
    )
    await routes.start("fake", {"state": "mine"})
    reply = await routes.finish("fake", {"state": "mine", "code": "c"})
    assert "fake-resume" not in reply.body.decode()


async def test_choosing_an_account_finishes_the_sign_in() -> None:
    choosy = FakePlatform(accounts=(AccountChoice(id="7", name="A Page"),))
    routes, _ = make_routes(choosy)
    await routes.start("fake", {"state": "mine"})
    await routes.finish("fake", {"state": "mine", "code": "c"})

    reply = await routes.choose("fake", {"state": "mine", "account_id": "7"})
    assert body_of(reply)["step"] == "connected"
    assert choosy.resumed == [("fake-resume", "7")]
    assert choosy.last_remember == {"verifier": "fake-verifier"}


async def test_choosing_needs_a_state() -> None:
    routes, _ = make_routes()
    reply = await routes.choose("fake", {"account_id": "7"})
    assert reply.status == 400


async def test_choosing_needs_an_account_id() -> None:
    routes, _ = make_routes()
    await routes.start("fake", {"state": "mine"})
    reply = await routes.choose("fake", {"state": "mine"})
    assert reply.status == 400


async def test_choosing_refuses_a_state_it_never_gave_out() -> None:
    routes, _ = make_routes()
    reply = await routes.choose("fake", {"state": "invented", "account_id": "7"})
    assert reply.status == 400


async def test_choosing_refuses_a_sign_in_that_never_paused_to_ask() -> None:
    routes, _ = make_routes()
    await routes.start("fake", {"state": "mine"})
    reply = await routes.choose("fake", {"state": "mine", "account_id": "7"})
    assert reply.status == 400
    assert "did not stop" in str(body_of(reply)["error"])


async def test_the_server_a_person_named_is_used_for_both_halves() -> None:
    storage = RecordingStorage(
        apps=[
            AppCredentials(
                platform="fake",
                host="one.example",
                client_id="id",
                client_secret="secret",
            )
        ]
    )
    sc = SocialChimp(storage=storage, platforms={"fake": FakePlatform()})
    routes = Routes(sc, redirect_uri=REDIRECT)

    await routes.start("fake", {"state": "mine", "host": "one.example"})
    reply = await routes.finish("fake", {"state": "mine", "code": "c"})
    assert reply.status == 200


async def test_the_scopes_for_one_network_reach_it() -> None:
    fake = FakePlatform()
    sc = SocialChimp(storage=RecordingStorage(apps=[APP]), platforms={"fake": fake})
    routes = Routes(sc, redirect_uri=REDIRECT, scopes={"fake": ["read", "write"]})
    assert (await routes.start("fake", {})).status == 302


# --------------------------------------------------------------------------
# Webhooks
# --------------------------------------------------------------------------


async def test_a_signed_webhook_is_accepted_and_handed_on() -> None:
    collected: list[Update] = []
    routes, fake = make_routes(collected=collected)
    reply = await routes.webhook("fake", UPDATE_BODY, fake.sign(UPDATE_BODY))
    assert reply.status == 200
    assert [update.id for update in collected] == ["u1"]


async def test_a_tampered_webhook_is_refused() -> None:
    collected: list[Update] = []
    routes, fake = make_routes(collected=collected)
    headers = fake.sign(UPDATE_BODY)
    reply = await routes.webhook("fake", UPDATE_BODY + b" ", headers)
    assert reply.status == 401
    assert collected == []


async def test_a_webhook_with_no_secret_configured_says_so() -> None:
    routes, fake = make_routes(secrets={})
    reply = await routes.webhook("fake", UPDATE_BODY, fake.sign(UPDATE_BODY))
    assert reply.status == 500
    assert "secrets" in str(body_of(reply)["error"])


async def test_a_webhook_for_a_network_that_never_pushes_is_refused() -> None:
    routes, fake = make_routes(FakePlatform(features=Feature.POST_TEXT))
    reply = await routes.webhook("fake", UPDATE_BODY, fake.sign(UPDATE_BODY))
    assert reply.status == 400


async def test_a_webhook_for_an_unknown_network_says_so() -> None:
    routes, _ = make_routes()
    reply = await routes.webhook("nobody", UPDATE_BODY, {})
    assert reply.status == 500


async def test_a_platform_that_promises_to_push_but_cannot_check_is_a_bug() -> None:
    routes, _ = make_routes()
    reply = await routes.webhook("lying-pusher", UPDATE_BODY, {})
    assert reply.status == 500
    assert "check_signature" in str(body_of(reply)["error"])


async def test_a_webhook_with_nowhere_to_hand_it_on_is_still_accepted() -> None:
    routes, fake = make_routes(with_deliver=False)
    reply = await routes.webhook("fake", UPDATE_BODY, fake.sign(UPDATE_BODY))
    assert reply.status == 200


async def test_the_setup_check_answers_with_the_challenge() -> None:
    routes, _ = make_routes()
    reply = await routes.setup_check(
        "fake",
        {
            "hub.mode": "subscribe",
            "hub.verify_token": "tok",
            "hub.challenge": "1234",
        },
    )
    assert reply.status == 200
    assert reply.body == b"1234"
    assert reply.content_type == "text/plain; charset=utf-8"


async def test_the_setup_check_refuses_a_wrong_token_with_403() -> None:
    routes, _ = make_routes()
    reply = await routes.setup_check(
        "fake",
        {
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "1234",
        },
    )
    assert reply.status == 403


async def test_the_setup_check_needs_a_token_to_check_against() -> None:
    routes, _ = make_routes(setup_tokens={})
    reply = await routes.setup_check("fake", {"hub.mode": "subscribe"})
    assert reply.status == 500
    assert "setup_tokens" in str(body_of(reply)["error"])


async def test_the_callback_says_what_the_network_said_when_it_refuses() -> None:
    routes, _ = make_routes(
        FakePlatform(
            login_fails_with=AuthError(
                "that code has already been used", platform="fake"
            )
        )
    )
    await routes.start("fake", {"state": "mine"})
    reply = await routes.finish("fake", {"state": "mine", "code": "c"})
    assert reply.status == 401
    assert "already been used" in str(body_of(reply)["error"])


async def test_choosing_refuses_a_network_that_signs_in_in_one_step() -> None:
    routes, _ = make_routes(NeverCarriesOn())
    await routes.start("fake", {"state": "mine"})
    await routes.finish("fake", {"state": "mine", "code": "c"})

    reply = await routes.choose("fake", {"state": "mine", "account_id": "7"})
    assert reply.status == 400
    assert "does not support" in str(body_of(reply)["error"])


# --------------------------------------------------------------------------
# Keeping the frameworks optional
# --------------------------------------------------------------------------


def test_importing_socialchimp_imports_no_framework() -> None:
    # In a process of its own, because by now this one has imported all
    # three and sys.modules would answer yes whatever the library does.
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, socialchimp, socialchimp.contrib.shared;"
            "print([n for n in ('django', 'fastapi', 'flask') if n in sys.modules])",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.strip() == "[]"
