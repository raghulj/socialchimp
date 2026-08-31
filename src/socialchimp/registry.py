"""How socialchimp finds the platforms that are installed.

Each network lives in its own package. Some ship with socialchimp, some are
written by other people, and socialchimp keeps no hand-written list of them.
Instead, a package says "I add a platform called mastodon" when it is
installed, and socialchimp reads that. A platform nobody here has heard of
works exactly like one that came in the box.

Two places are looked at, and code always wins over an installed package:

1. Platforms handed to `register_platform`. Tests use this, and so does an
   app that builds its own platform while running.
2. Platforms found in installed packages.

Nothing is imported until it is asked for. `import socialchimp` reads the
names of installed platforms and stops there, so having ten platforms
installed does not cost ten imports at startup.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Final, cast

from socialchimp.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from importlib.metadata import EntryPoint

    from socialchimp.platform import Platform

__all__ = [
    "GROUP",
    "PLATFORM_EXTRAS",
    "available_platforms",
    "clear_platform_cache",
    "get_platform_class",
    "register_platform",
    "unregister_platform",
]

GROUP: Final = "socialchimp.platforms"
"""The list a package adds itself to so socialchimp can find it.

Packages name it in their own `pyproject.toml`. See
`docs/adding-a-platform.md`.
"""

PLATFORM_EXTRAS: Final[Mapping[str, str]] = {
    "bluesky": "bluesky",
    "facebook": "facebook",
    "instagram": "instagram",
    "mastodon": "mastodon",
    "pinterest": "pinterest",
    "threads": "threads",
    "tiktok": "tiktok",
    "twitter": "twitter",
    "x": "twitter",
    "youtube": "youtube",
}
"""Networks socialchimp covers, and the extra that installs each one.

Asking for one of these when it is not installed is a common, easy mistake,
so the error says which extra to install instead of only saying no. `x` and
`twitter` are one network under two names, so they share an extra.

Add a network here when it becomes installable.
"""

_PLATFORM_METHODS: Final = (
    "api_base",
    "auth_headers",
    "limits",
    "start_login",
    "finish_login",
    "refresh",
    "publish",
)

# Platforms handed to us in code. Checked first, always.
_registered: dict[str, type[Platform]] = {}

# What the installed packages offer. `None` means we have not looked yet.
_installed: dict[str, EntryPoint] | None = None

# Platforms we have already imported, so asking twice imports once.
_imported: dict[str, type[Platform]] = {}


def register_platform(name: str, platform_class: type[Platform]) -> None:
    """Tell socialchimp about a platform, without installing a package.

    A platform registered this way is used in place of an installed one with
    the same name, which is how a test swaps in a fake.

    Args:
        name: How the platform will be asked for, for example `"mastodon"`.
        platform_class: The class to use. It is not created here; that
            happens when the platform is used.

    Raises:
        ConfigError: If the class does not provide what a platform must.
    """
    _check_it_is_a_platform(name, platform_class)
    _registered[name] = platform_class


def unregister_platform(name: str) -> None:
    """Forget a platform that was registered in code.

    Quiet if there was nothing registered under that name. Installed
    packages are left alone - this only undoes `register_platform`.

    Args:
        name: The name it was registered under.
    """
    _registered.pop(name, None)


def clear_platform_cache() -> None:
    """Look for installed platforms again next time one is asked for.

    socialchimp reads the installed packages once and remembers what it
    found. Call this after installing a package while the program is
    running, and between tests. Platforms registered in code are kept.
    """
    global _installed
    _installed = None
    _imported.clear()


def available_platforms() -> list[str]:
    """List every platform socialchimp can use right now.

    Returns:
        The names, in alphabetical order, from both places we look. A name
        appearing here does not promise its package imports cleanly - a
        broken package says so when you ask for it.
    """
    return sorted(set(_registered) | set(_find_installed()))


def get_platform_class(name: str) -> type[Platform]:
    """Find the class for one platform.

    The class is imported the first time it is asked for and remembered
    after that.

    Args:
        name: Which network, for example `"mastodon"`.

    Returns:
        The platform class. Create it to use it.

    Raises:
        ConfigError: If nothing is installed under that name, if its package
            could not be imported, or if what came back is not a platform.
    """
    if name in _registered:
        return _registered[name]
    if name in _imported:
        return _imported[name]

    installed = _find_installed()
    if name not in installed:
        raise ConfigError(_no_such_platform(name))

    platform_class = _import_platform(name, installed[name])
    _imported[name] = platform_class
    return platform_class


def _find_installed() -> dict[str, EntryPoint]:
    """Read what the installed packages offer, once.

    Reading the list does not import any of them. That only happens in
    `_import_platform`, when someone actually asks for one.

    Returns:
        Each installed platform name, and where its code lives.
    """
    global _installed
    if _installed is None:
        # Two packages offering the same name is their argument to settle;
        # we take the last one and carry on rather than refusing to start.
        _installed = {point.name: point for point in entry_points(group=GROUP)}
    return _installed


def _import_platform(name: str, point: EntryPoint) -> type[Platform]:
    """Import one platform's code.

    Args:
        name: Which platform is being asked for.
        point: Where its code lives.

    Returns:
        The platform class.

    Raises:
        ConfigError: If the package could not be imported, or what came back
            is not a platform.
    """
    try:
        imported = point.load()
    except Exception as error:
        # Anything at all can go wrong inside someone else's package: a
        # missing import, work done at import time, a plain syntax error. We
        # catch the lot, because one broken package must not stop the others
        # being usable - and this only runs for the platform being asked for.
        message = (
            f'The platform "{name}" is installed but could not be loaded.\n'
            f"It comes from {_where_it_came_from(point)}, which failed with: "
            f"{type(error).__name__}: {error}\n"
            f"That is a problem with that package rather than with "
            f"socialchimp. Reinstalling it is the usual fix."
        )
        raise ConfigError(message) from error

    _check_it_is_a_platform(name, imported)
    return cast("type[Platform]", imported)


def _where_it_came_from(point: EntryPoint) -> str:
    """Name the package a platform came from, for an error message.

    Args:
        point: Where the platform's code lives.

    Returns:
        The package name, or the code it points at when the package cannot
        be worked out - some ways of installing leave that out.
    """
    if point.dist is None:
        return point.value
    return point.dist.name


def _check_it_is_a_platform(name: str, candidate: object) -> None:
    """Check something has the methods a platform must have.

    Args:
        name: The name it is being registered or asked for under.
        candidate: The class to check.

    Raises:
        ConfigError: If any of the methods are missing, saying which.
    """
    # `Platform` is runtime_checkable, but isinstance() against a protocol
    # only checks that the method names exist - not what they take, what they
    # return, or that they are async. So this is an early warning that
    # catches the everyday mistakes, such as an entry point pointing at a
    # module or at the wrong class. It is never a promise that the platform
    # is correct; mypy is what checks the rest.
    missing = [method for method in _PLATFORM_METHODS if not hasattr(candidate, method)]
    if not missing:
        return

    message = (
        f'"{name}" is not a platform. A platform provides '
        f"{', '.join(_PLATFORM_METHODS)}, and this one is missing "
        f"{', '.join(missing)}."
    )
    raise ConfigError(message)


def _no_such_platform(name: str) -> str:
    """Write the message for a name nobody has.

    Args:
        name: The name that was asked for.

    Returns:
        What went wrong, what is installed, and how to install the asked-for
        network when it is one we cover.
    """
    lines = [f'There is no platform called "{name}".']

    installed = available_platforms()
    if installed:
        lines.append(f"Installed platforms: {', '.join(installed)}.")
    else:
        lines.append("No platforms are installed.")

    extra = PLATFORM_EXTRAS.get(name.lower())
    if extra is not None:
        lines.append(
            f"socialchimp covers {name}, it is just not installed here. "
            f'Install it with: pip install "socialchimp[{extra}]"'
        )

    return "\n".join(lines)
