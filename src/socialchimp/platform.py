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
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from socialchimp.events import Update
    from socialchimp.features import Feature, Limits
    from socialchimp.models import (
        AppCredentials,
        Connection,
        Post,
        PostResult,
        RawData,
        Token,
    )

__all__ = [
    "AccountChoice",
    "AskForDetails",
    "CanAnswerSetupCheck",
    "CanCheckSignature",
    "CanCheckState",
    "CanCreateApp",
    "CanDeletePosts",
    "CanReadPushedUpdates",
    "CanReadUpdates",
    "CanResumeLogin",
    "ChooseAccount",
    "Finished",
    "LoginField",
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
        app: Your app's credentials for this network. `SocialChimp` fills
            this in from storage; you only set it yourself if you are
            calling a platform directly.
    """

    redirect_uri: str
    scopes: tuple[str, ...] = ()
    host: str | None = None
    state: str | None = None
    app: AppCredentials | None = None


@dataclass(frozen=True, slots=True)
class SendToNetwork:
    """Step one: send the person to the network to approve your app.

    Attributes:
        url: Where to send them. Redirect their browser here.
        state: The value that will come back, for matching up the reply.
        remember: Something the platform needs again when the person comes
            back, such as the secret half of a PKCE pair. Keep it with the
            rest of that person's session and hand it back to `finish_login`.

            It has to travel through your app because the two halves of a
            sign-in can happen in different processes - the person may be
            sent away by one web worker and come back to another. Holding it
            in memory would work on your laptop and fail in production.
    """

    url: str
    state: str
    remember: RawData = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LoginField:
    """One thing to ask a person for.

    Attributes:
        name: What to call this value when handing it back. Put it in the
            `callback` mapping given to `finish_login` under this name.
        label: What to show next to the box, in words a person understands.
        secret: True for anything that should not be shown as it is typed,
            or written to a log.
        help_text: A sentence under the box, usually saying where on the
            network to find the value.
    """

    name: str
    label: str
    secret: bool = False
    help_text: str | None = None


@dataclass(frozen=True, slots=True)
class AskForDetails:
    """Step one, for networks that have no sign-in page to send people to.

    Bluesky uses an app password, and Discord and Telegram use a bot token
    that someone pastes in. There is nowhere to redirect to, so instead the
    platform says what to ask for, your app shows a form, and the answers go
    back through `finish_login` as the `callback` mapping.

    Show the fields in the order given, and never log anything marked
    `secret`.

    Nothing leaves your app on this route, so `LoginRequest.state` is not
    used - there is no trip through a browser to match up afterwards. If your
    sign-in code expects every step to carry state back, this is the one that
    will not.

    Attributes:
        fields: What to ask for.
        help_url: A page explaining where to get these, worth linking to
            beside the form.
    """

    fields: tuple[LoginField, ...]
    help_url: str | None = None


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

            **Treat it as a secret.** On some networks it has to carry the
            tokens themselves, because the sign-in code can only be swapped
            once and that happens before the person picks. Keep it with
            their session, the way you keep `SendToNetwork.remember`. Do not
            put it in a URL, a hidden form field, or a log.
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


# Every place a sign-in can get to. Match on it rather than checking types
# by hand, so a step added later becomes a type error in your code instead
# of a branch that is quietly never taken.
LoginStep = SendToNetwork | AskForDetails | ChooseAccount | Finished


@runtime_checkable
class Platform(Protocol):
    """What every platform file provides.

    A platform is built with no arguments - `MastodonPlatform()` - because
    `SocialChimp` builds it for you from the name you asked for. Everything
    it needs for a particular account arrives as an argument: credentials on
    the `LoginRequest`, the account on the `Connection`. Keep nothing per
    account on the instance, so one platform can serve every account your
    app holds.

    Attributes:
        name: How this network is named in code, for example `"mastodon"`.
        features: What this network can do. Anything not listed here is
            refused with a clear message instead of being attempted.
    """

    name: str
    features: Feature

    def api_base(self, connection: Connection) -> str:
        """Return where this network's API lives for this account.

        Most networks have one address for everyone. Mastodon has a
        different one per server, which is why the connection is passed in
        rather than this being a plain attribute.

        Args:
            connection: The account we are about to act as.

        Returns:
            The address to send requests to, with no trailing slash, for
            example `"https://graph.facebook.com/v21.0"`.
        """
        ...

    def auth_headers(self, connection: Connection) -> Mapping[str, str]:
        """Return the headers that prove we may act as this account.

        Usually one `Authorization` header. Called on every request, so
        keep it cheap and do not go to the network here - the token has
        already been renewed by the time this runs.

        Args:
            connection: The account we are acting as.

        Returns:
            Headers to add to the request.
        """
        ...

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

    async def start_login(self, request: LoginRequest) -> LoginStep:
        """Begin signing someone in.

        Most networks answer with `SendToNetwork`: redirect the person there
        and wait for them to come back. Networks that use an app password or
        a bot token answer with `AskForDetails` instead, because there is
        nowhere to send anyone - your app shows a form and passes the answers
        to `finish_login`.

        Args:
            request: Where to send them back to, and what to ask for.

        Returns:
            What to do next.
        """
        ...

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> LoginStep:
        """Carry on after the person comes back from the network.

        Usually this finishes the job and returns `Finished`. Networks that
        need to know which page or channel to use return `ChooseAccount`
        first.

        Args:
            request: The same request used to start the login.
            callback: The query values the network sent back.
            remember: Whatever `start_login` put in `SendToNetwork.remember`.

        Returns:
            Either the finished connection or a question to ask.
        """
        ...

    async def refresh(
        self,
        connection: Connection,
        app: AppCredentials | None = None,
    ) -> Token:
        """Get a fresh token for an account.

        Called for you before a token runs out. A platform whose tokens do
        not expire can return the existing one unchanged.

        Args:
            connection: The account whose token is running out.
            app: Your app's credentials for this network. Most networks want
                them to renew a token - Google, Meta and X all do. Networks
                that do not can ignore this.

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
class CanResumeLogin(Protocol):
    """Extra for networks that pause to ask which account to use.

    Facebook asks which page, Instagram which business account, YouTube which
    channel. Those platforms answer `finish_login` with `ChooseAccount`, and
    finish the job here once the person has picked one.
    """

    async def resume_login(
        self,
        request: LoginRequest,
        *,
        resume_token: str,
        account_id: str,
        remember: RawData | None = None,
    ) -> LoginStep:
        """Carry on with the login, now that an account has been picked.

        Args:
            request: The same request the login was started with.
            resume_token: The value from `ChooseAccount`, handed straight
                back. Only this platform understands it.
            account_id: Which of the offered accounts the person picked.
            remember: Whatever `start_login` put in `SendToNetwork.remember`,
                the same as `finish_login` was given.

        Returns:
            Usually the finished connection. A network that asks twice can
            answer with another question instead.
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


@runtime_checkable
class CanReadUpdates(Protocol):
    """Extra for networks we can ask "what has happened since?".

    Used for networks that cannot tell us themselves. LinkedIn, Pinterest,
    Reddit and Tumblr all work this way: socialchimp calls this on a timer,
    works out what is new, and hands your app the same `Update` objects a
    pushing network would have produced. Your handlers never learn which
    kind of network they are dealing with.

    A network that pushes updates should say so with `Feature.PUSH_UPDATES`
    and provide `CanCheckSignature` instead. Providing both is fine, and
    lets an app fall back to checking on a timer if it cannot receive
    incoming requests.
    """

    async def fetch_updates(
        self,
        connection: Connection,
        since: datetime | None,
    ) -> Sequence[Update]:
        """Return what has happened on this account since a moment in time.

        Args:
            connection: The account to ask about.
            since: Only return things newer than this. `None` on the first
                call, when there is no marker saved yet - return a recent
                page rather than the whole history.

        Returns:
            The updates, oldest first.
        """
        ...


@runtime_checkable
class CanCheckSignature(Protocol):
    """Extra for networks that send us requests when something happens.

    Every network signs these differently: Meta uses HMAC-SHA256 in a header,
    Telegram echoes a shared secret, Discord signs with Ed25519. A platform
    file knows which, and this is where it says so.

    The check must work on the **raw bytes** of the request, exactly as they
    arrived. Any framework that parses the JSON and builds it again first
    will change the bytes and break the signature, so never accept a parsed
    body here.
    """

    def check_signature(
        self,
        body: bytes,
        headers: Mapping[str, str],
        *,
        secret: str,
    ) -> None:
        """Check an incoming request really came from the network.

        Args:
            body: The request body, untouched.
            headers: The request headers.
            secret: The shared secret for this network, from your settings.

        Raises:
            SignatureError: If the request cannot be trusted. Answer 401 and
                do nothing else with it.
        """
        ...

    def read_update(
        self,
        body: bytes,
        headers: Mapping[str, str],
    ) -> Update:
        """Turn a checked request into an update your app understands.

        Only call this after `check_signature` has passed.

        Args:
            body: The request body, untouched.
            headers: The request headers.

        Returns:
            What happened, in socialchimp's own words.
        """
        ...


@runtime_checkable
class CanCheckState(Protocol):
    """Extra for networks that keep working after they accept a post.

    YouTube encodes a video for minutes, sometimes hours. TikTok can put one
    in somebody's drafts instead of publishing it. Both answer `publish`
    before they have finished, so a `PostResult` that comes back
    `PROCESSING` is not the end of the story - this is how an app finds out
    the rest of it.

    `Account.check_state` is what your app calls. It looks for this, renews
    the token, and hands the connection down.
    """

    async def check_state(self, connection: Connection, post_id: str) -> PostResult:
        """Ask the network how far it has got with a post.

        Called with both arguments by position, so the order matters and
        the names do not.

        Args:
            connection: The account the post belongs to, with a token that
                works right now.
            post_id: The network's identifier for the post, which is what
                `publish` handed back.

        Returns:
            Where the post has got to now. The same shape `publish` gave,
            so an app can treat the two the same way.
        """
        ...


@runtime_checkable
class CanAnswerSetupCheck(Protocol):
    """Extra for networks that ask a question before they will push anything.

    Facebook, Instagram and Threads all do this. Point Meta at a URL of
    yours and it does a GET to it first, carrying a token you chose and a
    challenge to echo back. Get it wrong and Meta says the URL could not be
    verified, without saying why.

    This happens before anybody has connected an account, so there is no
    connection to hang it on. `SocialChimp.answer_setup_check` is what your
    app calls.
    """

    def answer_setup_check(
        self,
        params: Mapping[str, str],
        *,
        verify_token: str,
    ) -> str:
        """Answer the one-off check and hand back what to reply with.

        Args:
            params: The query values from that GET, such as Django's
                `request.GET` or FastAPI's `request.query_params`.
            verify_token: The token you typed into the network's own form.

        Returns:
            The challenge. Send it back as the whole body, with a 200 and a
            content type of `text/plain`.

        Raises:
            SignatureError: If this is not a setup check, or the token is
                wrong. Answer 403 and send nothing back.
        """
        ...


@runtime_checkable
class CanReadPushedUpdates(Protocol):
    """Extra for reading everything one pushed request carries.

    Not the same as `CanReadUpdates`, which asks a network what has happened
    since. This is for a request the network sent us, and it hands back the
    whole of what that request held.

    Which matters because Meta batches. One message from Facebook can carry
    changes for several pages, and several changes for each of them, and it
    does that when it is busy - exactly when you least want to drop the
    rest. `read_update` on `CanCheckSignature` gives you the first one only.

    A network that never batches still has this, handing back a list of one,
    so that an app written against one network works against all of them.

    `SocialChimp.read_updates` is what your app calls, after
    `SocialChimp.check_signature` has passed.
    """

    def read_updates(self, body: bytes) -> list[Update]:
        """Turn a checked request into every update it carries.

        Args:
            body: The request body, exactly as it arrived. Check its
                signature first.

        Returns:
            What happened, in the order the network listed it. Empty when
            the message carried nothing we can act on, which is not an
            error - networks send shapes we have no interest in.
        """
        ...
