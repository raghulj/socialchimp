"""The one class an app talks to.

Everything else in socialchimp is a piece this puts together. `SocialChimp`
finds the platform for a network, keeps its tokens working, checks a post
against that network's rules before sending it, and hands you back what the
network said.

    sc = SocialChimp(storage=MyStorage())

    account = sc.account(connection_id)
    result = await account.post(Post(text="hello"))

Three things are worth knowing before you read on.

**Nothing is hidden.** `account.direct` sends whatever request you like to
the same network as the same account. Tokens, retries and rate limits still
apply; only the request is yours.

**Nothing is guessed.** Where a network cannot do something - Bluesky cannot
schedule, most networks cannot register an app for you - you get a
`NotSupportedError` that names the network and the thing it cannot do, rather
than something else happening quietly. Where a network lives and how it wants
to be asked come from the platform for the same reason: a host name is not
enough to work either one out.

**Nothing is shared between accounts.** `post_to_many` sends to every account
at once and gives each one its own result, so one account failing never hides
the rest.

Everything is passed in rather than reached for: storage, platforms, token
renewal and the HTTP client. That is how the tests here run without a
network, and it is how your app swaps any piece for its own.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from socialchimp.errors import ConfigError, NotSupportedError
from socialchimp.features import Feature, check_post
from socialchimp.http import HttpClient
from socialchimp.models import PostResult
from socialchimp.platform import (
    CanCreateApp,
    CanDeletePosts,
    CanResumeLogin,
    Finished,
    LoginRequest,
)
from socialchimp.registry import get_platform_class
from socialchimp.tokens import MakeLock, TokenManager

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import TracebackType

    import httpx

    from socialchimp.features import Limits
    from socialchimp.models import AppCredentials, Connection, Post, RawData, Token
    from socialchimp.platform import LoginStep, Platform
    from socialchimp.storage import Storage
    from socialchimp.tokens import GetNewToken

__all__ = [
    "Account",
    "Direct",
    "PostError",
    "PostJob",
    "SocialChimp",
]

# What we tell someone who asks a network to register an app for them when
# that network has no way to do it. Every network but Mastodon is like this.
_REGISTER_BY_HAND = (
    "registering an app for you. Register it by hand in that network's "
    "developer portal, then save the id and secret you are given with "
    "Storage.save_app"
)


@dataclass(frozen=True, slots=True)
class PostError:
    """One account's post that did not work.

    Handed back inside a `PostJob` rather than raised, so that one account
    failing never costs you the accounts that worked.

    Attributes:
        connection_id: Which connected account this was for.
        error: What went wrong. Usually a `SocialChimpError`, but anything a
            platform raised ends up here.
    """

    connection_id: str
    error: Exception


@dataclass(frozen=True, slots=True, repr=False)
class PostJob:
    """What happened when one post went to several accounts.

    Attributes:
        connection_ids: The accounts you asked for, in the order you asked.
        results: One outcome per account, in that same order. Each is either
            a `PostResult` or a `PostError`.
    """

    connection_ids: tuple[str, ...]
    results: tuple[PostResult | PostError, ...]

    @property
    def succeeded(self) -> list[PostResult]:
        """The accounts that posted, in the order you asked for them."""
        return [item for item in self.results if isinstance(item, PostResult)]

    @property
    def failed(self) -> list[PostError]:
        """The accounts that did not post, each with its own error."""
        return [item for item in self.results if isinstance(item, PostError)]

    def __repr__(self) -> str:
        """Say how many posted and name the accounts that did not.

        Returns:
            A line short enough for a log, naming what went wrong.
        """
        failed = self.failed
        if not failed:
            return f"PostJob({len(self.succeeded)} posted)"
        names = ", ".join(item.connection_id for item in failed)
        return f"PostJob({len(self.succeeded)} posted, {len(failed)} failed: {names})"


def _no_app_saved(platform: str, host: str | None) -> str:
    """Write the message for a network whose app has not been registered.

    Args:
        platform: Which network was being signed in to.
        host: Which server, for networks that have more than one.

    Returns:
        What is missing and how to get it, in plain words.
    """
    where = f" on {host}" if host is not None else ""
    return (
        f"No app credentials are stored for {platform}{where}. Nobody can "
        f"be signed in until your app is registered with that network. "
        f"Where the network allows it, create_app() does that and saves "
        f"them for you; everywhere else, register the app by hand in that "
        f"network's developer portal and save the id and secret with "
        f"Storage.save_app."
    )


def _refuse(platform: Platform, feature: Feature, what: str) -> None:
    """Stop here if the network cannot do this.

    Args:
        platform: The network being asked.
        feature: The thing it would need to be able to do.
        what: How to describe that in the message, in plain words.

    Raises:
        NotSupportedError: If the network does not list the feature.
    """
    if feature not in platform.features:
        raise NotSupportedError(platform=platform.name, what=what)


def _missing_method(platform: Platform, method: str) -> ConfigError:
    """Describe a platform that promises something its class cannot do.

    A platform lists what it can do in `features`, and that list is what we
    trust. `isinstance` against one of the extra protocols only checks that
    the method names exist, so it cannot be the deciding answer - but when it
    disagrees with the list, the platform itself is wrong.

    Args:
        platform: The network whose platform file is wrong.
        method: The method its class should have had.

    Returns:
        The error to raise. Raised rather than returned at the call site so
        that the type checker follows what happens next.
    """
    message = (
        f"The {platform.name} platform says it can do this, but its class "
        f"has no {method} method. That is a mistake in the platform file: "
        f"either add {method} or take the feature off its list."
    )
    return ConfigError(message)


async def _publish(
    platform: Platform,
    connection: Connection,
    post: Post,
) -> PostResult:
    """Check a post against a network's rules, then send it.

    The check happens first on purpose. An over-long post or a schedule the
    network cannot keep fails here with our own clear message, instead of
    spending a request to be told off in the network's words.

    Args:
        platform: The network to publish on.
        connection: The account to publish as, with a working token.
        post: What to publish.

    Returns:
        What the network said about the new post.

    Raises:
        InvalidPostError: If the post breaks one of the network's limits.
        NotSupportedError: If the post needs something the network cannot do.
    """
    limits = await platform.limits(connection)
    check_post(
        post,
        platform=platform.name,
        features=platform.features,
        limits=limits,
    )
    return await platform.publish(connection, post)


class Direct:
    """Your own requests to a network, sent as one connected account.

    Reached through `Account.direct`. The token is renewed before every
    request, and retries and rate limits are handled exactly as they are for
    `post()`. Only the request itself is yours.

        reply = await account.direct.post(
            "/api/v1/statuses",
            json={"status": "hello", "visibility": "unlisted"},
        )

    Paths are joined onto the address the platform gives for this account -
    the account's own server for Mastodon, the one address everybody uses
    for Facebook. Pass a whole address instead and it is used as it is.
    """

    def __init__(self, client: SocialChimp, connection_id: str) -> None:
        """Point direct access at one connected account.

        Args:
            client: The client this account belongs to.
            connection_id: The id your app gave this connection.
        """
        self._client = client
        self._connection_id = connection_id

    async def _ready(
        self,
        headers: Mapping[str, str] | None,
    ) -> tuple[HttpClient, dict[str, str]]:
        """Renew the token, then work out where to send and what to send with.

        Args:
            headers: Headers the caller wants sent.

        Returns:
            The client to send through, and the headers to send. A header
            the caller set wins over the platform's.
        """
        connection = await self._client.fresh_connection(self._connection_id)
        platform = self._client.platform_for(connection.platform)
        # The platform says how it proves who we are. Guessing a bearer token
        # here would be right for Mastodon and wrong for every network that
        # signs its requests some other way.
        sending = dict(platform.auth_headers(connection))
        if headers is not None:
            sending.update(headers)
        return self._client.http_for(connection), sending

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        **kwargs: object,
    ) -> httpx.Response:
        """Send a request as this account.

        Args:
            method: `"GET"`, `"POST"` and so on.
            path: Joined onto the address the platform gives for this
                account.
            headers: Sent along with the ones the platform set. A header
                you set here wins, so a request that has to be signed some
                other way is still yours to send.
            **kwargs: Anything `httpx.AsyncClient.request` takes, such as
                `params`, `json`, `content` or `files`.

        Returns:
            The reply, which is always one the network was happy with.

        Raises:
            SocialChimpError: If the network refused, or could not be
                reached. See `socialchimp.http.error_from_response`.
        """
        http, sending = await self._ready(headers)
        return await http.request(method, path, headers=sending, **kwargs)

    async def get(
        self,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        **kwargs: object,
    ) -> httpx.Response:
        """Send a GET request as this account.

        Args:
            path: Joined onto the address the platform gives for this
                account.
            headers: Sent along with the ones the platform set.
            **kwargs: Anything `request` takes.

        Returns:
            The reply.
        """
        return await self.request("GET", path, headers=headers, **kwargs)

    async def post(
        self,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        **kwargs: object,
    ) -> httpx.Response:
        """Send a POST request as this account.

        Args:
            path: Joined onto the address the platform gives for this
                account.
            headers: Sent along with the ones the platform set.
            **kwargs: Anything `request` takes.

        Returns:
            The reply.
        """
        return await self.request("POST", path, headers=headers, **kwargs)

    async def put(
        self,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        **kwargs: object,
    ) -> httpx.Response:
        """Send a PUT request as this account.

        Args:
            path: Joined onto the address the platform gives for this
                account.
            headers: Sent along with the ones the platform set.
            **kwargs: Anything `request` takes.

        Returns:
            The reply.
        """
        return await self.request("PUT", path, headers=headers, **kwargs)

    async def delete(
        self,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        **kwargs: object,
    ) -> httpx.Response:
        """Send a DELETE request as this account.

        Args:
            path: Joined onto the address the platform gives for this
                account.
            headers: Sent along with the ones the platform set.
            **kwargs: Anything `request` takes.

        Returns:
            The reply.
        """
        return await self.request("DELETE", path, headers=headers, **kwargs)

    async def json(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        **kwargs: object,
    ) -> RawData:
        """Send a request as this account and read the reply as JSON.

        Args:
            method: `"GET"`, `"POST"` and so on.
            path: Joined onto the address the platform gives for this
                account.
            headers: Sent along with the ones the platform set.
            **kwargs: Anything `request` takes.

        Returns:
            The reply, parsed.

        Raises:
            PlatformError: If the reply was not a JSON object.
            SocialChimpError: If the network refused the request.
        """
        http, sending = await self._ready(headers)
        return await http.json(method, path, headers=sending, **kwargs)


class Account:
    """One connected account, and the things you can do as it.

    Made by `SocialChimp.account`. Making one reads nothing: the connection
    is looked up when you actually do something, so a handle for an account
    that does not exist yet is fine to hold.

        account = sc.account(connection_id)
        result = await account.post(Post(text="hello"))

    Every call here renews the token first, so a post never fails just
    because a token aged out while it sat in a queue.

    Attributes:
        id: The id your app gave this connection.
        direct: Your own requests to the same network as the same account.
    """

    def __init__(self, client: SocialChimp, connection_id: str) -> None:
        """Point a handle at one connection, without reading anything.

        Args:
            client: The client this account belongs to.
            connection_id: The id your app gave this connection.
        """
        self.id = connection_id
        self.direct = Direct(client, connection_id)
        self._client = client

    def __repr__(self) -> str:
        """Name the connection this handle stands for.

        Returns:
            A line short enough for a log.
        """
        return f"Account({self.id!r})"

    async def connection(self) -> Connection:
        """Read this connection, with a token that works right now.

        Returns:
            The connection, renewed first if its token was running out.

        Raises:
            ConfigError: If nothing is stored under this id.
            TokenExpiredError: If the token needed renewing and could not be.
        """
        return await self._client.fresh_connection(self.id)

    async def limits(self) -> Limits:
        """Look up what this network is allowing this account right now.

        Worth reading before a burst of posts: a Mastodon server's post
        length is set by whoever runs it, and Instagram counts down how many
        posts are left today.

        Returns:
            The current limits.
        """
        connection = await self.connection()
        return await self._client.platform_for(connection.platform).limits(connection)

    async def post(self, post: Post) -> PostResult:
        """Publish a post as this account.

        The post is checked against the network's features and limits first,
        so an over-long post or a schedule the network cannot keep fails
        before a request is spent on it.

        Args:
            post: What to publish.

        Returns:
            What the network said about the new post.

        Raises:
            InvalidPostError: If the post breaks one of the network's limits.
            NotSupportedError: If the post needs something the network cannot
                do, such as scheduling.
        """
        connection = await self.connection()
        return await _publish(
            self._client.platform_for(connection.platform), connection, post
        )

    async def delete_post(self, post_id: str) -> None:
        """Take a post back down again.

        Args:
            post_id: The network's identifier for the post, which is what
                `post()` handed back.

        Raises:
            NotSupportedError: If this network cannot remove posts.
            ConfigError: If the platform says it can but has no method for
                it.
        """
        connection = await self.connection()
        platform = self._client.platform_for(connection.platform)
        _refuse(platform, Feature.DELETE_POST, "removing a post once it is published")
        if not isinstance(platform, CanDeletePosts):
            raise _missing_method(platform, "delete_post")
        await platform.delete_post(connection, post_id)


class SocialChimp:
    """The way in. One of these is enough for a whole app.

    Give it somewhere to keep connections and it does the rest: finds the
    platform for each network, keeps tokens working, checks posts before
    sending them, and closes what it opened.

        sc = SocialChimp(storage=MyStorage())
        step = await sc.start_login("mastodon", host="mastodon.social",
                                    redirect_uri="https://example.com/cb")

    Keep one for the life of your process. The locks that stop two workers
    renewing the same token at once live on it, so a new one per request
    protects nothing.

    Attributes:
        storage: Where connections and app credentials are kept.
    """

    def __init__(
        self,
        storage: Storage,
        *,
        platforms: Mapping[str, Platform] | None = None,
        token_manager: TokenManager | None = None,
        make_lock: MakeLock | None = None,
        http: HttpClient | None = None,
    ) -> None:
        """Set up one app's use of socialchimp.

        Args:
            storage: Where connections and app credentials are kept. The one
                thing you have to provide.
            platforms: Ready-made platforms, by name. Anything not named here
                is found among the installed platforms and created with no
                arguments, so this is where a platform that needs settings of
                its own goes - and where a test puts a fake.
            token_manager: Renews tokens. Left out, one is made for each
                network, which is what you want almost always. Pass your own
                only if you need to change how renewal works entirely - and
                note that yours has to look up app credentials itself, which
                `make_lock` saves you from.
            make_lock: Makes the lock held while a token is renewed. Pass one
                that every process shares - built on Redis, say - if you run
                more than one web or queue worker. The default only holds
                inside one process, so without this two workers can renew the
                same connection at once, and on the networks that replace the
                refresh token each time that disconnects the account.
            http: Sends requests for `Account.direct`. Left out, one client
                is made for each network and server, and closed by `aclose`.
                One you pass in is yours to close.
        """
        self.storage = storage
        self._platforms: dict[str, Platform] = dict(platforms or {})
        self._one_token_manager = token_manager
        self._make_lock = make_lock
        self._token_managers: dict[str, TokenManager] = {}
        self._http = http
        self._http_made: dict[tuple[str, str], HttpClient] = {}

    def platform_for(self, name: str) -> Platform:
        """Return the platform for one network, making it if need be.

        Args:
            name: Which network, for example `"mastodon"`.

        Returns:
            The platform. The same one every time, so anything it remembers
            is kept.

        Raises:
            ConfigError: If nothing is installed or registered under that
                name. The message lists what is, and how to install the
                network you asked for when it is one socialchimp covers.
        """
        ready = self._platforms.get(name)
        if ready is None:
            # Nothing is imported until a platform is actually asked for, so
            # an app with ten installed pays for the one it uses.
            ready = get_platform_class(name)()
            self._platforms[name] = ready
        return ready

    def _renewal_for(self, name: str) -> GetNewToken:
        """Bind your app's credentials into one platform's `refresh`.

        Most networks will not renew a token without them - Google, Meta and
        X all sign the renewal with a client id and secret. A platform is
        never given your storage, so the credentials are looked up here and
        handed down, exactly the way they are handed down for a sign-in.

        Args:
            name: Which network, for example `"youtube"`.

        Returns:
            Something `TokenManager` can call with a connection and nothing
            else, which is the shape it asks for.
        """
        platform = self.platform_for(name)

        async def renew(connection: Connection) -> Token:
            # Read on every renewal rather than once. Credentials saved or
            # replaced after this client was built are picked up without a
            # restart, which matters because rotating an app secret is the
            # sort of thing done in a hurry.
            app = await self.storage.get_app(name, connection.host)
            return await platform.refresh(connection, app)

        return renew

    def tokens_for(self, name: str) -> TokenManager:
        """Return the token manager for one network.

        Args:
            name: Which network, for example `"mastodon"`.

        Returns:
            The manager, made on first use unless you passed one in. The
            same one every time, because the locks that stop two renewals
            colliding live on it.
        """
        if self._one_token_manager is not None:
            return self._one_token_manager

        manager = self._token_managers.get(name)
        if manager is None:
            # make_lock is only passed on when given, so the default
            # stays whatever TokenManager decides rather than being
            # duplicated here and drifting from it.
            manager = (
                TokenManager(self.storage, self._renewal_for(name))
                if self._make_lock is None
                else TokenManager(
                    self.storage,
                    self._renewal_for(name),
                    make_lock=self._make_lock,
                )
            )
            self._token_managers[name] = manager
        return manager

    def http_for(self, connection: Connection) -> HttpClient:
        """Return the HTTP client for one connection's network and address.

        Args:
            connection: The account whose network we are talking to.

        Returns:
            The client, made on first use unless you passed one in. One per
            network and address, so accounts on the same server share one.
        """
        if self._http is not None:
            return self._http

        # The platform says where its API is: the account's own server for
        # Mastodon, one address for everybody on Facebook. So the address is
        # what tells two clients apart, rather than anything on the
        # connection.
        address = self.platform_for(connection.platform).api_base(connection)
        key = (connection.platform, address)
        made = self._http_made.get(key)
        if made is None:
            made = HttpClient(address, platform=connection.platform)
            self._http_made[key] = made
        return made

    async def fresh_connection(self, connection_id: str) -> Connection:
        """Read one connection, with a token that works right now.

        Every call that acts as an account goes through here first, so a
        token is always renewed before it is used.

        Args:
            connection_id: The id your app gave this connection.

        Returns:
            The connection, renewed first if its token was running out.

        Raises:
            ConfigError: If nothing is stored under that id.
            TokenExpiredError: If the token needed renewing and could not be.
        """
        # Read once to learn which network this is, because tokens are
        # renewed by the platform that issued them. The read after it is the
        # one that renews, and it is the answer we hand back.
        known = await self.storage.get_connection(connection_id)
        if known is None:
            message = (
                f"No connection is stored with the id {connection_id!r}. "
                f"Check the id, or connect the account again."
            )
            raise ConfigError(message)
        return await self.tokens_for(known.platform).valid_token(connection_id)

    def account(self, connection_id: str) -> Account:
        """Return a handle for one connected account.

        Cheap to make and reads nothing, so a handle for a connection that
        has not been saved yet is fine to hold. The connection is looked up
        when you actually do something with it.

        Args:
            connection_id: The id your app gave this connection.

        Returns:
            The handle.
        """
        return Account(self, connection_id)

    async def create_app(
        self,
        platform: str,
        *,
        name: str,
        redirect_uri: str,
        host: str | None = None,
        scopes: tuple[str, ...] = (),
    ) -> AppCredentials:
        """Register your app with a network, and save what it gives back.

        Only Mastodon can do this, and it has to be done once per server.
        Everywhere else you register the app by hand in a developer portal,
        and several networks review it before it works at all - so asking
        here says exactly that instead of failing later.

        Args:
            platform: Which network, for example `"mastodon"`.
            name: The app name people see when they approve it.
            redirect_uri: Where the network sends people back to.
            host: Which server to register on, for networks with many.
            scopes: Permissions the app will ask for.

        Returns:
            The credentials, already saved through your storage.

        Raises:
            NotSupportedError: If this network cannot register an app for
                you. The message says where to register it by hand.
            ConfigError: If the platform says it can but has no method for
                it.
        """
        maker = self.platform_for(platform)
        _refuse(maker, Feature.CREATE_APP, _REGISTER_BY_HAND)
        if not isinstance(maker, CanCreateApp):
            raise _missing_method(maker, "create_app")

        app = await maker.create_app(
            name=name,
            redirect_uri=redirect_uri,
            host=host,
            scopes=scopes,
        )
        # Saved for you, because registering again on the same server wastes
        # a record on that server and hands you a different id and secret.
        await self.storage.save_app(app)
        return app

    async def _login_request(
        self,
        platform: str,
        *,
        redirect_uri: str,
        scopes: tuple[str, ...],
        host: str | None,
        state: str | None,
    ) -> LoginRequest:
        """Build the request a platform signs someone in with.

        Your app's own credentials are looked up here and handed down with
        the request, so no platform has to reach into your storage for them.

        Args:
            platform: Which network, for example `"mastodon"`.
            redirect_uri: Where the network sends the person back to.
            scopes: Permissions to ask for.
            host: Which server, for networks that have more than one.
            state: A value handed back to you at the end.

        Returns:
            The request, carrying your app's credentials for this network.

        Raises:
            ConfigError: If no credentials are stored for this network and
                server. The message says how to get some.
        """
        app = await self.storage.get_app(platform, host)
        if app is None:
            raise ConfigError(_no_app_saved(platform, host))
        return LoginRequest(
            redirect_uri=redirect_uri,
            scopes=scopes,
            host=host,
            state=state,
            app=app,
        )

    async def start_login(
        self,
        platform: str,
        *,
        redirect_uri: str,
        scopes: tuple[str, ...] = (),
        host: str | None = None,
        state: str | None = None,
    ) -> LoginStep:
        """Begin signing someone in to a network.

        Args:
            platform: Which network, for example `"mastodon"`.
            redirect_uri: Where the network sends the person back to. It has
                to match what the network's developer portal has on file.
            scopes: Permissions to ask for. Empty uses the platform's
                sensible defaults.
            host: Which server, for networks that have more than one.
            state: A value handed back to you at the end, so you can tell
                which of your users came back. One is made for you if you
                leave it out.

        Returns:
            What to do next, handed back exactly as the platform gave it.

            Usually `SendToNetwork`: redirect the person's browser to
            `step.url`, and keep `step.remember` with that person's session,
            because `finish_login` needs it back and only you can carry it
            there.

            Networks signed in to with an app password or a bot token answer
            with `AskForDetails` instead, because there is nowhere to send
            anybody. Show a box for each of `step.fields`, hide the ones
            marked `secret`, and pass what the person typed to `finish_login`
            as `callback`, under the names the fields gave.

        Raises:
            ConfigError: If your app is not registered with this network yet.
        """
        # The platform is found first, so a name nobody has is answered with
        # the registry's message rather than one about app credentials.
        starter = self.platform_for(platform)
        request = await self._login_request(
            platform,
            redirect_uri=redirect_uri,
            scopes=scopes,
            host=host,
            state=state,
        )
        step = await starter.start_login(request)
        # Saved here too, not just in finish_login. A network that needs
        # nothing from the person could answer with Finished right away, and
        # a connection dropped on one path out of three is the kind of bug
        # that only shows up on the one network that does it.
        return await self._save_if_finished(step)

    async def finish_login(
        self,
        platform: str,
        *,
        callback: Mapping[str, str],
        redirect_uri: str,
        scopes: tuple[str, ...] = (),
        host: str | None = None,
        state: str | None = None,
        remember: RawData | None = None,
    ) -> LoginStep:
        """Carry on after the person comes back from the network.

        Args:
            platform: Which network, for example `"mastodon"`.
            callback: The query values the network sent back, such as
                Django's `request.GET` or FastAPI's `request.query_params`.
                For a network that asked for details instead of sending the
                person anywhere, this is what they typed into your form,
                under the names `AskForDetails` gave.
            redirect_uri: The same one the login was started with.
            scopes: The same ones the login was started with.
            host: The same server the login was started on.
            state: The value you started with, if you chose one.
            remember: What `start_login` handed you in
                `SendToNetwork.remember`. Keep it with that person's session
                and give it back here. socialchimp cannot keep it for you:
                the person can be sent away by one web worker and come back
                to another, so anything held in memory works on your laptop
                and fails in production.

        Returns:
            `Finished` when the account is connected, and the connection is
            saved for you. `ChooseAccount` when the network needs to know
            which page or channel to use - show the options, then call
            `choose`.

        Raises:
            ConfigError: If your app is not registered with this network yet.
        """
        finisher = self.platform_for(platform)
        request = await self._login_request(
            platform,
            redirect_uri=redirect_uri,
            scopes=scopes,
            host=host,
            state=state,
        )
        step = await finisher.finish_login(request, callback, remember)
        return await self._save_if_finished(step)

    async def choose(
        self,
        platform: str,
        *,
        account_id: str,
        resume_token: str,
        redirect_uri: str,
        scopes: tuple[str, ...] = (),
        host: str | None = None,
        state: str | None = None,
        remember: RawData | None = None,
    ) -> LoginStep:
        """Carry on a login after the person picked which account to use.

        Args:
            platform: Which network, for example `"facebook"`.
            account_id: The id of the option they picked, from
                `ChooseAccount.options`.
            resume_token: The value from `ChooseAccount`, handed straight
                back.
            redirect_uri: The same one the login was started with.
            scopes: The same ones the login was started with.
            host: The same server the login was started on.
            state: The value you started with, if you chose one.
            remember: The same value `finish_login` was given, still kept
                with that person's session.

        Returns:
            `Finished` when the account is connected, and the connection is
            saved for you. A network that asks twice can answer with another
            `ChooseAccount`.

        Raises:
            NotSupportedError: If this network never pauses to ask, so there
                is nothing to carry on from.
            ConfigError: If your app is not registered with this network yet.
        """
        chooser = self.platform_for(platform)
        if not isinstance(chooser, CanResumeLogin):
            raise NotSupportedError(
                platform=chooser.name,
                what="choosing an account part way through a login",
                suggestion=(
                    "It signs someone in in one step, so finish_login is the "
                    "whole of it."
                ),
            )

        request = await self._login_request(
            platform,
            redirect_uri=redirect_uri,
            scopes=scopes,
            host=host,
            state=state,
        )
        step = await chooser.resume_login(
            request,
            resume_token=resume_token,
            account_id=account_id,
            remember=remember,
        )
        return await self._save_if_finished(step)

    async def _save_if_finished(self, step: LoginStep) -> LoginStep:
        """Save the connection when a login has got to the end.

        Args:
            step: Where the login got to.

        Returns:
            The same step, so callers can match on it.
        """
        if isinstance(step, Finished):
            await self.storage.save_connection(step.connection)
        return step

    async def post_to_many(
        self,
        connection_ids: Sequence[str],
        post: Post,
        *,
        options_per_platform: Mapping[str, RawData] | None = None,
    ) -> PostJob:
        """Publish one post as several accounts at once.

        Every account is sent to at the same time, and every account gets its
        own outcome. One failing never cancels or hides the rest, so a job
        where two accounts worked and one did not tells you exactly that.

        Args:
            connection_ids: The accounts to post as.
            post: What to publish.
            options_per_platform: Settings to add for one network only, such
                as `{"pinterest": {"board_id": "x"}}`. They are added to the
                post's own options for accounts on that network; the post you
                passed in is left alone.

        Returns:
            One outcome per account, in the order you asked for them.

        Raises:
            BaseException: Anything that is not an ordinary error, such as
                the job being cancelled. Those are passed on rather than
                written down, so that shutting down really does shut down.
        """
        ids = tuple(connection_ids)
        extras = options_per_platform if options_per_platform is not None else {}
        outcomes: list[PostResult | BaseException] = await asyncio.gather(
            *(self._post_one(connection_id, post, extras) for connection_id in ids),
            return_exceptions=True,
        )

        results: list[PostResult | PostError] = []
        for connection_id, outcome in zip(ids, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, Exception):
                    raise outcome
                results.append(PostError(connection_id=connection_id, error=outcome))
            else:
                results.append(outcome)

        return PostJob(connection_ids=ids, results=tuple(results))

    async def _post_one(
        self,
        connection_id: str,
        post: Post,
        options_per_platform: Mapping[str, RawData],
    ) -> PostResult:
        """Publish one post as one account, adding that network's options.

        Args:
            connection_id: The account to post as.
            post: What to publish.
            options_per_platform: Settings to add for one network only.

        Returns:
            What the network said about the new post.
        """
        connection = await self.fresh_connection(connection_id)
        extra = options_per_platform.get(connection.platform)
        if extra:
            # A new post rather than a changed one: the caller's post goes to
            # several networks at once, and each needs different options.
            post = replace(post, options={**post.options, **extra})
        return await _publish(self.platform_for(connection.platform), connection, post)

    async def aclose(self) -> None:
        """Close the HTTP clients this made.

        A client you passed in yourself is left alone - it is yours, and you
        may still be using it.
        """
        for http in self._http_made.values():
            await http.aclose()

    async def __aenter__(self) -> SocialChimp:
        """Hand this client to an `async with` block.

        Returns:
            This client.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close what this made when the block ends.

        Args:
            exc_type: The kind of error that ended the block, if any.
            exc: The error that ended the block, if any.
            traceback: Where that error came from, if any.
        """
        await self.aclose()
