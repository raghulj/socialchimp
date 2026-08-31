"""Tests for the storage your app fills in."""

import threading
from collections.abc import Callable
from typing import TypeVar

import pytest

from socialchimp import (
    AppCredentials,
    Connection,
    InMemoryStorage,
    Storage,
    SyncStorage,
    Token,
    in_a_thread,
    sync_storage,
)

T = TypeVar("T")


@pytest.fixture
def storage() -> InMemoryStorage:
    return InMemoryStorage()


def a_connection(connection_id: str = "conn-1") -> Connection:
    return Connection(
        id=connection_id,
        platform="mastodon",
        host="mastodon.social",
        account_id="42",
        account_name="@someone@mastodon.social",
        token=Token(access_token="abc"),
    )


def test_the_bundled_storage_is_a_valid_storage() -> None:
    # If this stops being true, every app that copied it breaks too.
    accepted: Storage = InMemoryStorage()

    assert isinstance(accepted, Storage)


class TestConnections:
    async def test_a_saved_connection_comes_back(
        self, storage: InMemoryStorage
    ) -> None:
        await storage.save_connection(a_connection())

        found = await storage.get_connection("conn-1")

        assert found is not None
        assert found.account_name == "@someone@mastodon.social"

    async def test_an_unknown_connection_is_none_not_an_error(
        self, storage: InMemoryStorage
    ) -> None:
        assert await storage.get_connection("never-saved") is None

    async def test_saving_again_replaces_the_old_one(
        self, storage: InMemoryStorage
    ) -> None:
        # This is how a refreshed token gets written back.
        await storage.save_connection(a_connection())
        renewed = a_connection().with_token(Token(access_token="new"))

        await storage.save_connection(renewed)

        found = await storage.get_connection("conn-1")
        assert found is not None
        assert found.token.access_token == "new"

    async def test_a_deleted_connection_is_gone(self, storage: InMemoryStorage) -> None:
        await storage.save_connection(a_connection())

        await storage.delete_connection("conn-1")

        assert await storage.get_connection("conn-1") is None

    async def test_deleting_something_that_is_not_there_is_quiet(
        self, storage: InMemoryStorage
    ) -> None:
        # Deleting twice should not be an error. Retries happen.
        await storage.delete_connection("never-saved")


class TestAppCredentials:
    async def test_credentials_come_back_by_platform_and_host(
        self, storage: InMemoryStorage
    ) -> None:
        await storage.save_app(
            AppCredentials(
                platform="mastodon",
                host="mastodon.social",
                client_id="id",
                client_secret="secret",
            )
        )

        found = await storage.get_app("mastodon", "mastodon.social")

        assert found is not None
        assert found.client_id == "id"

    async def test_each_mastodon_server_keeps_its_own_credentials(
        self, storage: InMemoryStorage
    ) -> None:
        # Every Mastodon server is separate, so an app registered on one is
        # meaningless on another. Mixing them up would be a confusing bug.
        for host in ("mastodon.social", "fosstodon.org"):
            await storage.save_app(
                AppCredentials(
                    platform="mastodon",
                    host=host,
                    client_id=f"id-for-{host}",
                    client_secret="secret",
                )
            )

        found = await storage.get_app("mastodon", "fosstodon.org")

        assert found is not None
        assert found.client_id == "id-for-fosstodon.org"

    async def test_networks_with_one_server_use_no_host(
        self, storage: InMemoryStorage
    ) -> None:
        await storage.save_app(
            AppCredentials(
                platform="bluesky",
                host=None,
                client_id="id",
                client_secret="secret",
            )
        )

        assert await storage.get_app("bluesky", None) is not None

    async def test_unknown_credentials_are_none(self, storage: InMemoryStorage) -> None:
        assert await storage.get_app("mastodon", "nowhere.example") is None


class PlainStorage:
    """Five methods, none of them async, the way most apps already have."""

    def __init__(self) -> None:
        self.connections: dict[str, Connection] = {}
        self.apps: dict[tuple[str, str | None], AppCredentials] = {}

    def get_connection(self, connection_id: str) -> Connection | None:
        return self.connections.get(connection_id)

    def save_connection(self, connection: Connection) -> None:
        self.connections[connection.id] = connection

    def delete_connection(self, connection_id: str) -> None:
        self.connections.pop(connection_id, None)

    def get_app(self, platform: str, host: str | None) -> AppCredentials | None:
        return self.apps.get((platform, host))

    def save_app(self, app: AppCredentials) -> None:
        self.apps[app.key] = app


def an_app(host: str | None = "mastodon.social") -> AppCredentials:
    return AppCredentials(
        platform="mastodon",
        host=host,
        client_id="id",
        client_secret="secret",
    )


class TestStorageWrittenTheBlockingWay:
    def test_a_class_with_five_plain_methods_is_a_sync_storage(self) -> None:
        assert isinstance(PlainStorage(), SyncStorage)

    def test_wrapping_one_gives_a_storage_the_core_can_use(self) -> None:
        assert isinstance(sync_storage(PlainStorage()), Storage)

    async def test_connections_go_in_and_come_back(self) -> None:
        wrapped = sync_storage(PlainStorage())

        await wrapped.save_connection(a_connection())
        found = await wrapped.get_connection("conn-1")
        assert found is not None
        assert found.account_name == "@someone@mastodon.social"

        await wrapped.delete_connection("conn-1")
        assert await wrapped.get_connection("conn-1") is None

    async def test_app_credentials_go_in_and_come_back(self) -> None:
        wrapped = sync_storage(PlainStorage())

        await wrapped.save_app(an_app())

        assert await wrapped.get_app("mastodon", "mastodon.social") == an_app()

    async def test_every_call_goes_off_the_event_loop(self) -> None:
        # The whole point: a blocking read must not stop everything else the
        # loop is in the middle of.
        threads: list[str] = []

        class Nosy(PlainStorage):
            def save_app(self, app: AppCredentials) -> None:
                threads.append(threading.current_thread().name)
                super().save_app(app)

        await sync_storage(Nosy()).save_app(an_app())

        assert threads[0] != threading.current_thread().name

    async def test_your_own_runner_is_used_instead(self) -> None:
        # Which is how Django gets its ORM code back onto the request's own
        # thread - see socialchimp.contrib.django.orm_storage.
        ran: list[str] = []

        async def run_here(work: Callable[[], T]) -> T:
            ran.append("here")
            return work()

        wrapped = sync_storage(PlainStorage(), run=run_here)

        await wrapped.save_app(an_app())
        assert await wrapped.get_app("mastodon", "mastodon.social") == an_app()

        assert ran == ["here", "here"]

    async def test_the_default_runner_can_be_used_on_its_own(self) -> None:
        # in_a_thread is a RunInThread anybody can call, not a private thing
        # sync_storage keeps to itself.
        where = await in_a_thread(lambda: threading.current_thread().name)

        assert where != threading.current_thread().name
