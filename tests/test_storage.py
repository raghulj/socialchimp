"""Tests for the storage your app fills in."""

import pytest

from socialchimp import AppCredentials, Connection, InMemoryStorage, Storage, Token


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
