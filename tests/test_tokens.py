"""Tests for keeping a connection's token usable."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import TracebackType

import pytest

from socialchimp import (
    AuthError,
    ConfigError,
    Connection,
    InMemoryStorage,
    PlatformError,
    Token,
    TokenExpiredError,
)
from socialchimp.tokens import Lock, TokenManager

OLD_ACCESS = "old-access"
OLD_REFRESH = "old-refresh"
NEW_ACCESS = "new-access"

# Far enough away that none of the windows below reach it.
HOURS_LEFT = timedelta(hours=6)

# Inside the default one minute window, so a new token gets asked for.
ALMOST_OUT = timedelta(seconds=20)


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


def a_connection(token: Token, connection_id: str = "conn-1") -> Connection:
    return Connection(
        id=connection_id,
        platform="bluesky",
        host=None,
        account_id="42",
        account_name="someone.bsky.social",
        token=token,
    )


def expiring_soon(connection_id: str = "conn-1") -> Connection:
    return a_connection(a_token(left=ALMOST_OUT), connection_id)


def good_for_hours(connection_id: str = "conn-1") -> Connection:
    return a_connection(a_token(), connection_id)


async def storage_holding(*connections: Connection) -> InMemoryStorage:
    storage = InMemoryStorage()
    for connection in connections:
        await storage.save_connection(connection)
    return storage


class FakeRefresh:
    """Stands in for a platform being asked for a new token."""

    def __init__(
        self,
        token: Token | None = None,
        error: Exception | None = None,
    ) -> None:
        self.token = token if token is not None else a_token(access_token=NEW_ACCESS)
        self.error = error
        self.calls = 0
        self.asked_about: list[Connection] = []

    async def __call__(self, connection: Connection) -> Token:
        self.calls += 1
        self.asked_about.append(connection)
        # Hand control back to the loop, so tests that start several callers
        # really do overlap instead of running one after the other.
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        return self.token


class TestATokenThatIsStillFine:
    async def test_a_token_with_no_expiry_is_never_refreshed(self) -> None:
        # Mastodon, Discord and Telegram tokens last until someone revokes
        # them. Asking for a new one is a wasted request at best.
        storage = await storage_holding(a_connection(a_token(left=None)))
        refresh = FakeRefresh()
        tokens = TokenManager(storage, refresh)

        connection = await tokens.valid_token("conn-1")

        assert refresh.calls == 0
        assert connection.token.access_token == OLD_ACCESS

    async def test_a_token_with_hours_left_is_handed_back_as_it_is(self) -> None:
        storage = await storage_holding(good_for_hours())
        refresh = FakeRefresh()
        tokens = TokenManager(storage, refresh)

        connection = await tokens.valid_token("conn-1")

        assert refresh.calls == 0
        assert connection.token.access_token == OLD_ACCESS


class TestATokenAboutToRunOut:
    async def test_it_is_refreshed_before_it_is_handed_back(self) -> None:
        storage = await storage_holding(expiring_soon())
        refresh = FakeRefresh()
        tokens = TokenManager(storage, refresh)

        connection = await tokens.valid_token("conn-1")

        assert refresh.calls == 1
        assert connection.token.access_token == NEW_ACCESS

    async def test_the_platform_is_shown_the_connection_it_belongs_to(self) -> None:
        storage = await storage_holding(expiring_soon())
        refresh = FakeRefresh()
        tokens = TokenManager(storage, refresh)

        await tokens.valid_token("conn-1")

        assert refresh.asked_about[0].id == "conn-1"

    async def test_the_new_token_is_saved_straight_away(self) -> None:
        storage = await storage_holding(expiring_soon())
        tokens = TokenManager(storage, FakeRefresh())

        await tokens.valid_token("conn-1")

        saved = await storage.get_connection("conn-1")
        assert saved is not None
        assert saved.token.access_token == NEW_ACCESS

    async def test_a_rotated_refresh_token_is_saved_too(self) -> None:
        # Bluesky, Pinterest and TikTok hand back a new refresh token and
        # stop the old one working. Losing it disconnects the account.
        rotated = a_token(access_token=NEW_ACCESS, refresh_token="rotated-refresh")
        storage = await storage_holding(expiring_soon())
        tokens = TokenManager(storage, FakeRefresh(token=rotated))

        await tokens.valid_token("conn-1")

        saved = await storage.get_connection("conn-1")
        assert saved is not None
        assert saved.token.refresh_token == "rotated-refresh"

    async def test_everything_else_about_the_connection_is_kept(self) -> None:
        storage = await storage_holding(expiring_soon())
        tokens = TokenManager(storage, FakeRefresh())

        connection = await tokens.valid_token("conn-1")

        assert connection.account_name == "someone.bsky.social"
        assert connection.platform == "bluesky"


class TestHowEarlyToRefresh:
    async def test_a_wider_window_refreshes_a_token_that_still_has_hours(self) -> None:
        storage = await storage_holding(good_for_hours())
        refresh = FakeRefresh()
        a_whole_day = 60 * 60 * 24
        tokens = TokenManager(storage, refresh, refresh_before_seconds=a_whole_day)

        await tokens.valid_token("conn-1")

        assert refresh.calls == 1

    async def test_a_zero_window_waits_until_the_token_has_really_run_out(self) -> None:
        storage = await storage_holding(expiring_soon())
        refresh = FakeRefresh()
        tokens = TokenManager(storage, refresh, refresh_before_seconds=0)

        await tokens.valid_token("conn-1")

        assert refresh.calls == 0


class TestWhenARefreshFails:
    async def test_a_refusal_from_the_network_asks_for_a_new_sign_in(self) -> None:
        refused = AuthError("invalid_grant")
        storage = await storage_holding(expiring_soon())
        tokens = TokenManager(storage, FakeRefresh(error=refused))

        with pytest.raises(TokenExpiredError) as caught:
            await tokens.valid_token("conn-1")

        assert "sign in again" in str(caught.value)
        # The original refusal is kept, so the traceback still shows what
        # the network actually said.
        assert caught.value.__cause__ is refused

    async def test_a_connection_with_no_refresh_token_cannot_be_renewed(self) -> None:
        # There is nothing to send, so there is no point asking the network.
        refresh = FakeRefresh()
        storage = await storage_holding(
            a_connection(a_token(refresh_token=None, left=ALMOST_OUT))
        )
        tokens = TokenManager(storage, refresh)

        with pytest.raises(TokenExpiredError, match="sign in again"):
            await tokens.valid_token("conn-1")

        assert refresh.calls == 0

    async def test_a_refresh_token_that_has_run_out_says_so_by_name(self) -> None:
        # Asking the network would only get back invalid_grant, and the
        # message for that guesses between "expired" and "revoked". Here we
        # know which, so we say which - and spend no request finding out.
        refresh = FakeRefresh()
        expired = Token(
            access_token=OLD_ACCESS,
            refresh_token=OLD_REFRESH,
            expires_at=datetime.now(UTC) + ALMOST_OUT,
            refresh_token_expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        storage = await storage_holding(a_connection(expired))
        tokens = TokenManager(storage, refresh)

        with pytest.raises(TokenExpiredError) as caught:
            await tokens.valid_token("conn-1")

        said = str(caught.value)
        assert "refresh token" in said
        assert "sign in again" in said
        assert refresh.calls == 0

    async def test_a_refresh_token_with_time_left_is_used_as_normal(self) -> None:
        refresh = FakeRefresh()
        fine = Token(
            access_token=OLD_ACCESS,
            refresh_token=OLD_REFRESH,
            expires_at=datetime.now(UTC) + ALMOST_OUT,
            refresh_token_expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        storage = await storage_holding(a_connection(fine))
        tokens = TokenManager(storage, refresh)

        renewed = await tokens.valid_token("conn-1")

        assert renewed.token.access_token == NEW_ACCESS
        assert refresh.calls == 1

    async def test_a_temporary_failure_is_passed_on_untouched(self) -> None:
        # A timeout or a 500 says nothing about the token. Turning it into
        # "sign in again" would disconnect accounts that are perfectly fine.
        outage = PlatformError("bad gateway", platform="bluesky", status_code=502)
        storage = await storage_holding(expiring_soon())
        tokens = TokenManager(storage, FakeRefresh(error=outage))

        with pytest.raises(PlatformError) as caught:
            await tokens.valid_token("conn-1")

        assert caught.value is outage

    async def test_nothing_is_saved_when_the_refresh_fails(self) -> None:
        storage = await storage_holding(expiring_soon())
        tokens = TokenManager(storage, FakeRefresh(error=AuthError("revoked")))

        with pytest.raises(TokenExpiredError):
            await tokens.valid_token("conn-1")

        saved = await storage.get_connection("conn-1")
        assert saved is not None
        assert saved.token.access_token == OLD_ACCESS


class TestAConnectionWeDoNotHave:
    async def test_an_unknown_id_is_a_mistake_in_your_own_code(self) -> None:
        tokens = TokenManager(InMemoryStorage(), FakeRefresh())

        with pytest.raises(ConfigError) as caught:
            await tokens.valid_token("never-saved")

        assert "never-saved" in str(caught.value)


class TestTwoCallersAtOnce:
    async def test_five_callers_together_refresh_exactly_once(self) -> None:
        # With a rotating refresh token, a second refresh would kill the
        # first one's token and the account would be dead by morning.
        storage = await storage_holding(expiring_soon())
        refresh = FakeRefresh()
        tokens = TokenManager(storage, refresh)

        await asyncio.gather(*(tokens.valid_token("conn-1") for _ in range(5)))

        assert refresh.calls == 1

    async def test_every_caller_gets_the_one_new_token(self) -> None:
        storage = await storage_holding(expiring_soon())
        tokens = TokenManager(storage, FakeRefresh())

        results = await asyncio.gather(
            *(tokens.valid_token("conn-1") for _ in range(5))
        )

        assert {result.token.access_token for result in results} == {NEW_ACCESS}

    async def test_the_saved_connection_is_the_one_everybody_got(self) -> None:
        storage = await storage_holding(expiring_soon())
        tokens = TokenManager(storage, FakeRefresh())

        await asyncio.gather(*(tokens.valid_token("conn-1") for _ in range(5)))

        saved = await storage.get_connection("conn-1")
        assert saved is not None
        assert saved.token.access_token == NEW_ACCESS

    async def test_two_different_accounts_do_not_wait_for_each_other(self) -> None:
        # Each connection gets its own lock. One slow network must not hold
        # up every other account. The barrier below only opens once both
        # refreshes are running together, so a single shared lock would
        # leave this test waiting for ever.
        both_inside = asyncio.Barrier(2)

        async def refresh(connection: Connection) -> Token:
            await asyncio.wait_for(both_inside.wait(), timeout=5)
            return a_token(access_token=NEW_ACCESS)

        storage = await storage_holding(
            expiring_soon("conn-1"),
            expiring_soon("conn-2"),
        )
        tokens = TokenManager(storage, refresh)

        results = await asyncio.gather(
            tokens.valid_token("conn-1"),
            tokens.valid_token("conn-2"),
        )

        assert [result.id for result in results] == ["conn-1", "conn-2"]


class TestTellingTheAppAboutARenewal:
    async def test_a_listener_hears_about_the_renewed_connection(self) -> None:
        heard: list[Connection] = []
        storage = await storage_holding(expiring_soon())
        tokens = TokenManager(storage, FakeRefresh())
        tokens.on_token_renewed(heard.append)

        await tokens.valid_token("conn-1")

        assert len(heard) == 1
        assert heard[0].token.access_token == NEW_ACCESS

    async def test_listeners_stay_quiet_when_nothing_was_renewed(self) -> None:
        heard: list[Connection] = []
        storage = await storage_holding(good_for_hours())
        tokens = TokenManager(storage, FakeRefresh())
        tokens.on_token_renewed(heard.append)

        await tokens.valid_token("conn-1")

        assert heard == []

    async def test_a_listener_that_breaks_does_not_break_the_refresh(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Listeners watch. They must never be able to stop a renewal, or one
        # careless callback disconnects every account in the system.
        def explode(connection: Connection) -> None:
            message = "this listener is having a bad day"
            raise RuntimeError(message)

        storage = await storage_holding(expiring_soon())
        tokens = TokenManager(storage, FakeRefresh())
        tokens.on_token_renewed(explode)

        with caplog.at_level(logging.ERROR, logger="socialchimp.tokens"):
            connection = await tokens.valid_token("conn-1")

        assert connection.token.access_token == NEW_ACCESS
        assert "this listener is having a bad day" in caplog.text

    async def test_later_listeners_still_hear_about_it(self) -> None:
        heard: list[Connection] = []

        def explode(connection: Connection) -> None:
            message = "no"
            raise RuntimeError(message)

        storage = await storage_holding(expiring_soon())
        tokens = TokenManager(storage, FakeRefresh())
        tokens.on_token_renewed(explode)
        tokens.on_token_renewed(heard.append)

        await tokens.valid_token("conn-1")

        assert len(heard) == 1


class CountingLock:
    """A lock that records how many times it was taken."""

    def __init__(self) -> None:
        self.taken = 0
        self._inner = asyncio.Lock()

    async def __aenter__(self) -> None:
        self.taken += 1
        await self._inner.acquire()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._inner.release()


class CountingLocks:
    """Stands in for locks kept somewhere shared, such as Redis."""

    def __init__(self) -> None:
        self.asked_for: list[str] = []
        self.made: dict[str, CountingLock] = {}

    def __call__(self, connection_id: str) -> Lock:
        self.asked_for.append(connection_id)
        lock = CountingLock()
        self.made[connection_id] = lock
        return lock


class TestBringingYourOwnLock:
    async def test_the_supplied_lock_is_the_one_that_gets_taken(self) -> None:
        locks = CountingLocks()
        storage = await storage_holding(expiring_soon())
        tokens = TokenManager(storage, FakeRefresh(), make_lock=locks)

        await tokens.valid_token("conn-1")

        assert locks.asked_for == ["conn-1"]
        assert locks.made["conn-1"].taken == 1

    async def test_one_lock_is_made_per_connection_and_then_reused(self) -> None:
        # A fresh lock every time would protect nothing at all.
        locks = CountingLocks()
        still_short = a_token(access_token=NEW_ACCESS, left=ALMOST_OUT)
        storage = await storage_holding(expiring_soon())
        tokens = TokenManager(storage, FakeRefresh(token=still_short), make_lock=locks)

        await tokens.valid_token("conn-1")
        await tokens.valid_token("conn-1")

        assert locks.asked_for == ["conn-1"]
        assert locks.made["conn-1"].taken == 2

    async def test_no_lock_is_taken_when_the_token_is_still_fine(self) -> None:
        locks = CountingLocks()
        storage = await storage_holding(good_for_hours())
        tokens = TokenManager(storage, FakeRefresh(), make_lock=locks)

        await tokens.valid_token("conn-1")

        assert locks.asked_for == []
