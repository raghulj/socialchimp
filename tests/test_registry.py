"""Tests for how socialchimp finds the platforms that are installed."""

from collections.abc import Iterator, Mapping
from importlib.metadata import Distribution, EntryPoint
from typing import cast
from unittest import mock

import pytest

from socialchimp import (
    ConfigError,
    Connection,
    Feature,
    Limits,
    Post,
    PostResult,
    Token,
)
from socialchimp.models import RawData
from socialchimp.platform import (
    Finished,
    LoginRequest,
    LoginStep,
    Platform,
    SendToNetwork,
)
from socialchimp.registry import (
    PLATFORM_EXTRAS,
    available_platforms,
    clear_platform_cache,
    get_platform_class,
    register_platform,
    unregister_platform,
)

WHERE_WE_LOOK = "socialchimp.registry.entry_points"


class FakePlatform:
    """A platform that does nothing, used wherever a real one would go."""

    name = "fake"
    features = Feature.POST_TEXT

    def api_base(self, connection: Connection) -> str:
        return "https://fake.example"

    def auth_headers(self, connection: Connection) -> Mapping[str, str]:
        return {"Authorization": f"Bearer {connection.token.access_token}"}

    async def limits(self, connection: Connection) -> Limits:
        return Limits(max_text_length=100)

    async def start_login(self, request: LoginRequest) -> SendToNetwork:
        return SendToNetwork(url="https://fake.example/authorize", state="xyz")

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> LoginStep:
        return Finished(connection=_a_connection())

    async def refresh(self, connection: Connection) -> Token:
        return Token(access_token="new")

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        return PostResult(id="1")


class OtherFakePlatform(FakePlatform):
    """A second platform, for telling two of them apart."""

    name = "other-fake"


class HalfAPlatform:
    """Something that looks like a platform but cannot publish."""

    name = "half"
    features = Feature.POST_TEXT

    async def limits(self, connection: Connection) -> Limits:
        return Limits()


def _a_connection() -> Connection:
    return Connection(
        id="conn-1",
        platform="fake",
        host=None,
        account_id="42",
        account_name="someone",
        token=Token(access_token="abc"),
    )


def _installed_package(
    name: str,
    *,
    holds: object = FakePlatform,
    breaks: Exception | None = None,
    package: str | None = "socialchimp-fake",
) -> mock.Mock:
    """Stand in for one platform that a real installed package would add."""
    point = mock.Mock(spec=EntryPoint)
    point.name = name
    point.value = f"{name}_package.platform:Platform"
    if breaks is not None:
        point.load.side_effect = breaks
    else:
        point.load.return_value = holds
    if package is None:
        point.dist = None
    else:
        point.dist = mock.Mock(spec=Distribution)
        point.dist.name = package
    return point


@pytest.fixture(autouse=True)
def _clean_slate() -> Iterator[None]:
    # Every test starts with nothing found and nothing registered. Real
    # installed packages are patched out too, so the answers do not depend on
    # what happens to be installed on the machine running the tests.
    clear_platform_cache()
    with mock.patch(WHERE_WE_LOOK, return_value=[]):
        yield
        for name in available_platforms():
            unregister_platform(name)
    clear_platform_cache()


class TestPlatformsRegisteredInCode:
    def test_a_platform_registered_in_code_is_found_again(self) -> None:
        register_platform("fake", FakePlatform)

        assert get_platform_class("fake") is FakePlatform

    def test_a_platform_registered_in_code_is_listed(self) -> None:
        register_platform("fake", FakePlatform)

        assert available_platforms() == ["fake"]

    def test_registering_the_same_name_twice_keeps_the_newer_one(self) -> None:
        register_platform("fake", FakePlatform)
        register_platform("fake", OtherFakePlatform)

        assert get_platform_class("fake") is OtherFakePlatform

    def test_something_missing_a_platform_method_is_refused(self) -> None:
        # Registering the wrong object should fail here, not hours later in
        # the middle of publishing a post.
        with pytest.raises(ConfigError) as caught:
            register_platform("half", cast("type[Platform]", HalfAPlatform))

        message = str(caught.value)
        assert "publish" in message
        assert "half" in message

    def test_forgetting_a_platform_removes_it(self) -> None:
        register_platform("fake", FakePlatform)
        unregister_platform("fake")

        assert available_platforms() == []

    def test_forgetting_a_platform_nobody_registered_is_quiet(self) -> None:
        unregister_platform("never-existed")


class TestPlatformsFromInstalledPackages:
    def test_a_platform_from_an_installed_package_is_found(self) -> None:
        with mock.patch(WHERE_WE_LOOK, return_value=[_installed_package("mastodon")]):
            assert get_platform_class("mastodon") is FakePlatform
            assert available_platforms() == ["mastodon"]

    def test_installed_packages_are_looked_for_only_once(self) -> None:
        with mock.patch(WHERE_WE_LOOK, return_value=[]) as looking:
            available_platforms()
            available_platforms()

        assert looking.call_count == 1

    def test_nothing_is_imported_until_a_platform_is_asked_for(self) -> None:
        # The whole point of being lazy: ten installed platforms must not
        # cost ten imports at startup.
        point = _installed_package("mastodon")

        with mock.patch(WHERE_WE_LOOK, return_value=[point]):
            available_platforms()
            point.load.assert_not_called()

            get_platform_class("mastodon")
            point.load.assert_called_once_with()

    def test_a_platform_is_imported_only_once_however_often_it_is_asked_for(
        self,
    ) -> None:
        point = _installed_package("mastodon")

        with mock.patch(WHERE_WE_LOOK, return_value=[point]):
            get_platform_class("mastodon")
            get_platform_class("mastodon")

        point.load.assert_called_once_with()

    def test_a_platform_registered_in_code_beats_an_installed_one(self) -> None:
        # This is what lets a test, or an app with its own build of a
        # platform, replace one that happens to be installed.
        point = _installed_package("mastodon")

        with mock.patch(WHERE_WE_LOOK, return_value=[point]):
            register_platform("mastodon", OtherFakePlatform)

            assert get_platform_class("mastodon") is OtherFakePlatform
            point.load.assert_not_called()

    def test_both_sources_are_listed_together_in_name_order(self) -> None:
        with mock.patch(WHERE_WE_LOOK, return_value=[_installed_package("mastodon")]):
            register_platform("bluesky", FakePlatform)

            assert available_platforms() == ["bluesky", "mastodon"]

    def test_an_installed_platform_missing_a_method_is_refused(self) -> None:
        point = _installed_package("half", holds=HalfAPlatform)

        with (
            mock.patch(WHERE_WE_LOOK, return_value=[point]),
            pytest.raises(ConfigError) as caught,
        ):
            get_platform_class("half")

        assert "publish" in str(caught.value)


class TestABrokenPackage:
    def test_one_broken_package_does_not_hide_the_others(self) -> None:
        broken = _installed_package("broken", breaks=ImportError("no module bits"))
        working = _installed_package("mastodon")

        with mock.patch(WHERE_WE_LOOK, return_value=[broken, working]):
            assert available_platforms() == ["broken", "mastodon"]
            assert get_platform_class("mastodon") is FakePlatform

    def test_asking_for_a_broken_platform_names_the_package_that_broke(self) -> None:
        broken = _installed_package(
            "broken",
            breaks=ImportError("no module named bits"),
            package="chimp-broken",
        )

        with (
            mock.patch(WHERE_WE_LOOK, return_value=[broken]),
            pytest.raises(ConfigError) as caught,
        ):
            get_platform_class("broken")

        message = str(caught.value)
        assert "broken" in message
        assert "chimp-broken" in message
        assert "ImportError" in message
        assert "no module named bits" in message

    def test_a_break_with_no_package_name_points_at_the_code_instead(self) -> None:
        # Some ways of installing leave the package name out. Naming the
        # code the entry points at is still enough to go on.
        broken = _installed_package("broken", breaks=RuntimeError("boom"), package=None)

        with (
            mock.patch(WHERE_WE_LOOK, return_value=[broken]),
            pytest.raises(ConfigError) as caught,
        ):
            get_platform_class("broken")

        assert "broken_package.platform:Platform" in str(caught.value)


class TestAskingForANameNobodyHas:
    def test_an_unknown_name_lists_what_is_installed(self) -> None:
        register_platform("bluesky", FakePlatform)

        with (
            mock.patch(WHERE_WE_LOOK, return_value=[_installed_package("mastodon")]),
            pytest.raises(ConfigError) as caught,
        ):
            get_platform_class("myspace")

        message = str(caught.value)
        assert "myspace" in message
        assert "bluesky" in message
        assert "mastodon" in message

    def test_a_network_we_cover_that_is_not_installed_says_how_to_install_it(
        self,
    ) -> None:
        with pytest.raises(ConfigError) as caught:
            get_platform_class("mastodon")

        message = str(caught.value)
        assert 'pip install "socialchimp[mastodon]"' in message
        assert "No platforms are installed" in message

    def test_x_and_twitter_are_the_same_network_so_share_an_extra(self) -> None:
        assert PLATFORM_EXTRAS["x"] == PLATFORM_EXTRAS["twitter"]

    def test_the_suggestion_does_not_care_about_capital_letters(self) -> None:
        with pytest.raises(ConfigError) as caught:
            get_platform_class("Bluesky")

        assert 'pip install "socialchimp[bluesky]"' in str(caught.value)


class TestLookingAgain:
    def test_a_package_installed_while_running_is_found_after_looking_again(
        self,
    ) -> None:
        with mock.patch(WHERE_WE_LOOK, return_value=[]):
            assert available_platforms() == []

        with mock.patch(WHERE_WE_LOOK, return_value=[_installed_package("mastodon")]):
            assert available_platforms() == []

            clear_platform_cache()

            assert available_platforms() == ["mastodon"]

    def test_looking_again_keeps_platforms_registered_in_code(self) -> None:
        register_platform("fake", FakePlatform)

        clear_platform_cache()

        assert available_platforms() == ["fake"]
