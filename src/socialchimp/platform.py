"""What a platform file must provide.

A platform is one class. It knows how to sign someone in to a network, keep
their token working, and publish for them. Everything else - retries, rate
limits, saving to your database - is handled for it.

Anything a network cannot do is left out rather than stubbed. A platform that
cannot create apps simply has no `create_app` method, and socialchimp asks
before calling it.

Adding your own platform does not need a change to this repository. Publish a
package that registers itself and socialchimp will find it. See
`docs/adding-a-platform.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping

    from socialchimp.features import Feature, Limits
    from socialchimp.models import (
        AppCredentials,
        Connection,
        Post,
        PostResult,
        Token,
    )

__all__ = [
    "AccountChoice",
    "CanCreateApp",
    "CanDeletePosts",
    "ChooseAccount",
    "Finished",
    "LoginRequest",
    "LoginStep",
    "Platform",
    "SendToNetwork",
]


@dataclass(frozen=True, slots=True)
class LoginRequest:
    """What we need in order to start signing someone in.

    Attributes:
        redirect_uri: Where the network sends the person back to. It must
            match what the network's developer portal has on file.
        scopes: Permissions to ask for. Each platform's page lists sensible
            defaults; leaving this empty uses them.
        host: Which server, for networks that have more than one. Required
            for Mastodon, ignored elsewhere.
        state: A value handed back to you at the end, so you can tell which
            of your users came back. One is made for you if you leave it out.
    """

    redirect_uri: str
    scopes: tuple[str, ...] = ()
    host: str | None = None
    state: str | None = None


@dataclass(frozen=True, slots=True)
class SendToNetwork:
    """Step one: send the person to the network to approve your app.

    Attributes:
        url: Where to send them. Redirect their browser here.
        state: The value that will come back, for matching up the reply.
    """

    url: str
    state: str


@dataclass(frozen=True, slots=True)
class AccountChoice:
    """One of several accounts a person could connect.

    Attributes:
        id: The network's identifier, passed back to carry on.
        name: Something a person would recognise, shown in your UI.
        kind: What sort of thing it is, such as `"page"` or `"channel"`.
    """

    id: str
    name: str
    kind: str | None = None


@dataclass(frozen=True, slots=True)
class ChooseAccount:
    """A pause: the person has approved, but we need to know which account.

    Facebook asks which page, Instagram which business account, YouTube which
    channel. Show `options`, then carry on with the one they picked.

    Attributes:
        options: What they can choose from.
        resume_token: Hand this back to carry on. Treat it as meaningless
            text; only the platform file understands it.
    """

    options: tuple[AccountChoice, ...]
    resume_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class Finished:
    """The last step: the account is connected.

    Attributes:
        connection: Save this. It is everything needed to act as the account.
    """

    connection: Connection


# Where a login can get to after a person comes back from the network. Match
# on it, so a new step added later becomes a type error rather than a
# silently skipped branch.
LoginStep = SendToNetwork | ChooseAccount | Finished


@runtime_checkable
class Platform(Protocol):
    """What every platform file provides.

    Attributes:
        name: How this network is named in code, for example `"mastodon"`.
        features: What this network can do. Anything not listed here is
            refused with a clear message instead of being attempted.
    """

    name: str
    features: Feature

    async def limits(self, connection: Connection) -> Limits:
        """Look up the numbers this network is enforcing right now.

        Some of these genuinely change: a Mastodon server's post length is
        set by whoever runs it, and Instagram counts down how many posts are
        left today. Results are worth caching for a short while.

        Args:
            connection: The account to ask about.

        Returns:
            The current limits.
        """
        ...

    async def start_login(self, request: LoginRequest) -> SendToNetwork:
        """Begin signing someone in.

        Args:
            request: Where to send them back to, and what to ask for.

        Returns:
            Where to send the person next.
        """
        ...

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
    ) -> LoginStep:
        """Carry on after the person comes back from the network.

        Usually this finishes the job and returns `Finished`. Networks that
        need to know which page or channel to use return `ChooseAccount`
        first.

        Args:
            request: The same request used to start the login.
            callback: The query values the network sent back.

        Returns:
            Either the finished connection or a question to ask.
        """
        ...

    async def refresh(self, connection: Connection) -> Token:
        """Get a fresh token for an account.

        Called for you before a token runs out. A platform whose tokens do
        not expire can return the existing one unchanged.

        Args:
            connection: The account whose token is running out.

        Returns:
            The new token. Save it - if the network rotates refresh tokens,
            the old one has already stopped working.
        """
        ...

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        """Publish a post.

        Networks that publish in several steps, such as Instagram, do all of
        them here and return once the post is live or the network has taken
        over.

        Args:
            connection: The account to publish as.
            post: What to publish.

        Returns:
            What the network said about the new post.
        """
        ...


@runtime_checkable
class CanCreateApp(Protocol):
    """Extra for networks that let us register an app automatically.

    Mastodon is the only one today. Everywhere else you register your app by
    hand in a developer portal, and several networks review it before it
    works at all.
    """

    async def create_app(
        self,
        *,
        name: str,
        redirect_uri: str,
        host: str | None = None,
        scopes: tuple[str, ...] = (),
    ) -> AppCredentials:
        """Register an app with the network and return its credentials.

        Args:
            name: The app name people will see when approving it.
            redirect_uri: Where the network sends people back to.
            host: Which server to register on, for networks with many.
            scopes: Permissions the app will ask for.

        Returns:
            Credentials to save and reuse. Registering again for the same
            server wastes a record on that server, so save these.
        """
        ...


@runtime_checkable
class CanDeletePosts(Protocol):
    """Extra for networks that let us remove a post we published."""

    async def delete_post(self, connection: Connection, post_id: str) -> None:
        """Remove a post.

        Args:
            connection: The account that published it.
            post_id: The network's identifier for the post.
        """
        ...
