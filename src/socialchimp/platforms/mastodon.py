"""Mastodon: the one network you can start using in five minutes.

Everywhere else, before a single line of your code runs, you fill in a form
in a developer portal and wait for somebody to approve your app. Mastodon has
no portal and no review. You ask a server to register your app, it answers
straight away with the two values you need, and you are signing people in.

That is why Mastodon is the first network socialchimp supports.

## Every server is its own network

Mastodon is thousands of separate servers. `mastodon.social` and
`fosstodon.org` share software, not accounts, not settings, and not your app.
An app registered on one means nothing on the other, so **every server needs
its own registration**, and everything here takes a `host`:

    app = await mastodon.create_app(
        name="My App",
        redirect_uri="https://myapp.example/callback",
        host="mastodon.social",
    )

Save what comes back and hand it to every login on that server. The next
server somebody signs in on needs a registration of its own.

## Signing someone in

Two steps, and socialchimp does the fiddly part:

1. `start_login` gives you a web address. Send the person's browser there.
   They see Mastodon's own "do you allow this app?" page.
2. Mastodon sends them back to your `redirect_uri` with a short code in the
   query string. Hand that whole query to `finish_login`, along with the
   `remember` value from step one, and you get a connection to save.

The address from step one carries a hashed secret (Mastodon and everybody
else call this PKCE). socialchimp makes the secret, sends only the hash to
Mastodon, and sends the secret itself when it swaps the code for a token.
That way a code stolen out of a browser's history is worth nothing without
the secret, which never left your server. Newer Mastodon servers expect this;
older ones ignore it.

The secret comes back to you in `SendToNetwork.remember`. Keep it with that
person's session - not in memory - because the person may be sent away by one
web worker and come back to another.

## Tokens do not expire

A Mastodon access token works until the person revokes it. There is no
refresh token because there is nothing to refresh. `Token.expires_at` stays
`None` and `refresh()` hands the same token straight back without calling
anything. That is a real property of Mastodon, not something missing here.

## What a post can carry

`Post.options` accepts four settings, all Mastodon's own:

    Post(
        text="Hello",
        options={
            "visibility": "unlisted",       # public, unlisted, private, direct
            "spoiler_text": "Film ending",  # the warning shown before the text
            "sensitive": True,              # hide pictures behind a click
            "language": "en",               # two-letter language code
        },
    )

Anything else is refused before we send it, with a message listing what is
accepted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

# anyio comes with httpx, so waiting through it adds no new dependency and
# lets this run under trio as happily as under asyncio.
import anyio
import httpx

from socialchimp.errors import (
    AuthError,
    ConfigError,
    InvalidPostError,
    PlatformError,
)
from socialchimp.events import Update
from socialchimp.features import Feature, Limits, TextCount, check_post
from socialchimp.http import HttpClient, error_from_response, read_body
from socialchimp.models import (
    AppCredentials,
    Connection,
    Media,
    Post,
    PostResult,
    PostState,
    RawData,
    Token,
)
from socialchimp.platform import Finished, LoginRequest, SendToNetwork

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from socialchimp.errors import SocialChimpError
    from socialchimp.http import Retries

__all__ = ["MastodonPlatform", "mastodon_errors", "post_fingerprint"]

PLATFORM_NAME: Final = "mastodon"

DEFAULT_SCOPES: Final = ("read", "write")
"""Enough to read an account's own timeline and to post as them.

Mastodon also has narrower scopes such as `write:statuses`. Ask for those
instead if your app only ever posts - people are more likely to say yes to a
smaller request.
"""

VISIBILITIES: Final = ("public", "unlisted", "private", "direct")
"""Who can see a post. `direct` is a message to the people it mentions."""

POST_OPTIONS: Final = ("visibility", "spoiler_text", "sensitive", "language")
"""The settings `Post.options` accepts here. Anything else is refused."""

# What Mastodon does when nobody has changed it. Servers do change it, which
# is why we ask the server rather than trusting these.
DEFAULT_MAX_CHARACTERS: Final = 500
DEFAULT_MAX_MEDIA: Final = 4

# Mastodon takes one video per post, and will not mix video with pictures.
MAX_VIDEOS_PER_POST: Final = 1

# The kinds of notification worth telling an app about. Mastodon has more -
# polls ending, moderation warnings - which nobody has asked for yet.
WATCHED_NOTIFICATIONS: Final = ("mention", "favourite", "reblog", "follow")

# Mastodon's word for something, and ours. A word missing from here is passed
# through as it is and lands as `UpdateKind.UNKNOWN` with Mastodon's own word
# kept on the update, so a kind we have never seen still reaches your app.
_OUR_WORD_FOR: Final = {
    "mention": "mention",
    "favourite": "reaction_added",
    "reblog": "reaction_added",
}

# Long enough that nobody can guess one, short enough to sit in a URL.
_STATE_BYTES: Final = 24
_VERIFIER_BYTES: Final = 48


async def _wait(seconds: float) -> None:
    """Pause while a server finishes working on a file.

    Kept as its own function so tests can watch the pauses instead of
    sitting through them.

    Args:
        seconds: How long to wait.
    """
    await anyio.sleep(seconds)


def _clean_host(host: str | None) -> str:
    """Turn whatever somebody wrote into a bare server name.

    `"https://mastodon.social/"` and `"mastodon.social"` mean the same
    server, and both are things people type.

    Args:
        host: The server, however it was written.

    Returns:
        Just the name, such as `"mastodon.social"`.

    Raises:
        ConfigError: If no server was named. Mastodon is thousands of
            separate servers, so there is no sensible one to guess.
    """
    cleaned = (
        (host or "").strip().removeprefix("https://").removeprefix("http://").strip("/")
    )
    if not cleaned:
        message = (
            "Mastodon needs to know which server. Every Mastodon server is "
            'separate, so pass host="mastodon.social" - or whichever server '
            "this account is on."
        )
        raise ConfigError(message)
    return cleaned


def _host_of(connection: Connection) -> str:
    """Work out which server a connected account lives on.

    Args:
        connection: The account to look at.

    Returns:
        The server name.

    Raises:
        ConfigError: If the connection was saved without one.
    """
    return _clean_host(connection.host)


def _text(reply: RawData, key: str, when: str) -> str:
    """Read a value Mastodon always sends, and complain plainly if it did not.

    Args:
        reply: What Mastodon answered.
        key: The field we need.
        when: What we had asked it to do, for the message.

    Returns:
        The value.

    Raises:
        PlatformError: If the field is missing or empty. The whole reply is
            kept on the error so you can see what did arrive.
    """
    value = reply.get(key)
    if isinstance(value, str) and value:
        return value

    message = (
        f"Mastodon left {key!r} out of its reply when we asked it to {when}. "
        f"That should not happen. The whole reply is on this error."
    )
    raise PlatformError(message, platform=PLATFORM_NAME, raw=reply)


def _section(parent: RawData, name: str) -> RawData:
    """Read one nested object out of a reply, or an empty one.

    Servers run different versions and leave parts out, so a missing section
    is normal rather than a failure.

    Args:
        parent: The object to look inside.
        name: The section to read.

    Returns:
        The section, or `{}` if it is missing or not an object.
    """
    value = parent.get(name)
    return value if isinstance(value, dict) else {}


def _number(section: RawData, name: str, fallback: int | None) -> int | None:
    """Read a count out of a reply, falling back when it is not there.

    Args:
        section: The object to look inside.
        name: The field to read.
        fallback: What to use when the field is missing or not a number.

    Returns:
        The count, or the fallback.
    """
    value = section.get(name)
    return value if isinstance(value, int) else fallback


def _moment(text: str) -> datetime | None:
    """Read a time Mastodon wrote, such as `"2026-08-31T10:00:00.000Z"`.

    Args:
        text: The time as it arrived.

    Returns:
        The moment, always with a timezone, or `None` if it cannot be read.
    """
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    # Mastodon always writes UTC, but a fork might leave the zone off, and a
    # time with no zone compares wrongly against every other time we hold.
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _challenge_for(verifier: str) -> str:
    """Hash the secret we keep, so only the hash travels to Mastodon.

    Args:
        verifier: The secret made at the start of a login.

    Returns:
        The hash, written the way Mastodon expects it.
    """
    digest = hashlib.sha256(verifier.encode()).digest()
    # Base64 with the two URL-unsafe characters swapped and the padding
    # dropped, which is what the PKCE rules ask for.
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def mastodon_errors(response: httpx.Response) -> SocialChimpError:
    """Turn an unhappy reply from Mastodon into a socialchimp error.

    Only one status needs a word of its own. Mastodon answers 422 when a post
    breaks one of its rules - too long, empty, a picture it will not take -
    and that is a problem with the post rather than a mystery, so it comes
    back as `InvalidPostError`. Everything else is the shared mapping: 401 is
    an `AuthError`, 403 a `NotAllowedError`, 404 a `NotFoundError`, 429 a
    `RateLimitError`.

    Args:
        response: The reply to turn into an error.

    Returns:
        The error to raise.
    """
    if response.status_code == httpx.codes.UNPROCESSABLE_ENTITY:
        body = read_body(response)
        said = body.get("error")
        detail = f" It said: {said}" if isinstance(said, str) and said else ""
        message = (
            f"Mastodon would not accept this post (422). Something in it "
            f"breaks a rule of that server.{detail}"
        )
        return InvalidPostError(message, platform=PLATFORM_NAME, raw=body)

    return error_from_response(response, platform=PLATFORM_NAME)


def post_fingerprint(post: Post) -> str:
    """Return a short code that stands for this exact post.

    It goes out as Mastodon's `Idempotency-Key` header. Mastodon remembers
    that header for an hour: send the same one twice and the second request
    gives you back the first post instead of making a second one. So if a
    reply is lost on the way back to us and the request is sent again, the
    person's followers still see one post, not two.

    The code is built from what you asked for - the words, what it replies
    to, when it should go out, the settings, and the files by name - and not
    from anything the server hands back. A file uploaded twice gets two
    different ids, and hashing those would give a different code every time,
    which is exactly the case this is meant to protect.

    Args:
        post: The post about to be sent.

    Returns:
        A hex string, the same every time for the same post.
    """
    parts = {
        "text": post.text,
        "reply_to": post.reply_to,
        "publish_at": (
            post.publish_at.isoformat() if post.publish_at is not None else None
        ),
        "options": {str(key): str(value) for key, value in post.options.items()},
        "media": [
            {
                "kind": item.kind.name,
                "filename": item.filename,
                "url": item.url,
                "alt_text": item.alt_text,
            }
            for item in post.media
        ],
    }
    written = json.dumps(parts, sort_keys=True).encode()
    return hashlib.sha256(written).hexdigest()


def _checked_option(key: str, value: object) -> str:
    """Check one post setting and turn it into what a form can carry.

    Args:
        key: Which setting it is.
        value: What was given for it.

    Returns:
        The value as text, ready to send.

    Raises:
        InvalidPostError: If the value is not one Mastodon takes. The message
            lists what is accepted.
    """
    if key == "visibility":
        if value not in VISIBILITIES:
            message = (
                f"visibility is {value!r}, which Mastodon does not know. It "
                f"accepts: {', '.join(VISIBILITIES)}."
            )
            raise InvalidPostError(message)
        return str(value)

    if key == "sensitive":
        if not isinstance(value, bool):
            message = (
                f"sensitive is {value!r}, but it has to be True or False. It "
                f"decides whether pictures are hidden behind a click."
            )
            raise InvalidPostError(message)
        return "true" if value else "false"

    if not isinstance(value, str) or not value:
        message = f"{key} is {value!r}, but it has to be some text."
        raise InvalidPostError(message)
    return value


def _checked_options(options: RawData) -> dict[str, str]:
    """Check every setting on a post before anything is sent.

    Args:
        options: What was put in `Post.options`.

    Returns:
        The same settings, as text a form can carry.

    Raises:
        InvalidPostError: If a setting is unknown or its value is wrong. This
            happens before any request, so a typo costs nothing.
    """
    checked: dict[str, str] = {}
    for key, value in options.items():
        if key not in POST_OPTIONS:
            message = (
                f"Mastodon does not know the post option {key!r}. It accepts: "
                f"{', '.join(POST_OPTIONS)}."
            )
            raise InvalidPostError(message)
        checked[key] = _checked_option(key, value)
    return checked


def _limits_from_instance(reply: RawData) -> Limits:
    """Read what one server currently allows out of its own description.

    Args:
        reply: What `/api/v2/instance` answered.

    Returns:
        The limits, falling back to Mastodon's defaults for anything the
        server did not mention.
    """
    configuration = _section(reply, "configuration")
    statuses = _section(configuration, "statuses")
    attachments = _section(configuration, "media_attachments")

    return Limits(
        max_text_length=_number(statuses, "max_characters", DEFAULT_MAX_CHARACTERS),
        # Mastodon is one of the few networks that really does mean
        # characters when it says characters, so this is said out loud
        # rather than left to the default. A family emoji costs seven of a
        # server's 500 here, where on Bluesky it costs one of 300.
        text_counted_in=TextCount.CHARACTERS,
        max_images=_number(statuses, "max_media_attachments", DEFAULT_MAX_MEDIA),
        max_image_bytes=_number(attachments, "image_size_limit", None),
        max_videos=MAX_VIDEOS_PER_POST,
        max_video_bytes=_number(attachments, "video_size_limit", None),
    )


class MastodonPlatform:
    """Everything socialchimp does with Mastodon.

    Registering an app on each server it meets, signing people in,
    publishing, and reading what has happened since.

        mastodon = MastodonPlatform()

        app = await mastodon.create_app(
            name="My App",
            redirect_uri="https://myapp.example/callback",
            host="mastodon.social",
        )

    It holds nothing between calls except what one server said its limits
    were. Credentials arrive on the `LoginRequest`, and anything a sign-in
    needs a second time travels through your app. So one of these can be
    shared by your whole process, and two of them behave the same as one.

    Attributes:
        name: `"mastodon"`.
        features: What Mastodon can do. Notably it cannot push updates to a
            single account, so `Feature.PUSH_UPDATES` is missing and
            socialchimp checks on a timer instead.
    """

    name: str = PLATFORM_NAME

    features: Feature = (
        Feature.CREATE_APP
        | Feature.POST_TEXT
        | Feature.POST_IMAGE
        | Feature.POST_VIDEO
        | Feature.SCHEDULE
        | Feature.REPLY
        | Feature.DELETE_POST
        | Feature.READ_POSTS
    )

    def __init__(
        self,
        *,
        website: str | None = None,
        timeout: float = 30.0,
        retries: Retries | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        limits_cache_seconds: float = 300.0,
        media_checks: int = 30,
        media_wait_seconds: float = 1.0,
        updates_per_check: int = 40,
    ) -> None:
        """Set Mastodon up for one app.

        Args:
            website: Your app's home page, shown to people on the approval
                page. Left out, none is sent.
            timeout: Seconds to wait for a server to answer.
            retries: How many times to try again after a hiccup. Left out,
                the shared default is used.
            transport: Where requests actually go. Leave it out for ordinary
                calls; pass your own to send them somewhere else.
            limits_cache_seconds: How long to trust what a server said about
                its own limits before asking again.
            media_checks: How many times to ask whether a video has finished
                being processed before giving up.
            media_wait_seconds: How long to wait between those checks.
            updates_per_check: How many notifications to read at a time.
                Mastodon allows up to 80.
        """
        self._website = website
        self._timeout = timeout
        self._retries = retries
        self._transport = transport
        self._limits_cache_seconds = limits_cache_seconds
        self._media_checks = media_checks
        self._media_wait_seconds = media_wait_seconds
        self._updates_per_check = updates_per_check

        # What each server last said it allows, and when to stop believing
        # it. Keyed by server, because two servers rarely agree. This is the
        # only thing kept between calls, and losing it costs one request.
        self._known_limits: dict[str, tuple[float, Limits]] = {}

    def _client(self, host: str, token: str | None = None) -> HttpClient:
        """Make a client pointed at one server.

        Args:
            host: The server to talk to.
            token: The account's token, for anything that needs one.

        Returns:
            A client. Use it in an `async with` block so it closes itself.
        """
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        return HttpClient(
            f"https://{host}",
            platform=PLATFORM_NAME,
            headers=headers,
            timeout=self._timeout,
            transport=self._transport,
            retries=self._retries,
            errors=mastodon_errors,
        )

    def api_base(self, connection: Connection) -> str:
        """Return the address of the server this account is on.

        Mastodon is thousands of separate servers, so there is no one
        address to write down here. The account says which server it is on,
        and that is where its requests go.

        Args:
            connection: The account we are about to act as.

        Returns:
            The server's address, such as `"https://mastodon.social"`.

        Raises:
            ConfigError: If the connection was saved without a server on it.
        """
        return f"https://{_host_of(connection)}"

    def auth_headers(self, connection: Connection) -> Mapping[str, str]:
        """Return the header that proves we may act as this account.

        Args:
            connection: The account we are acting as.

        Returns:
            Mastodon's `Authorization` header. Its tokens do not expire, so
            the one on the connection is always the right one.
        """
        return {"Authorization": f"Bearer {connection.token.access_token}"}

    async def create_app(
        self,
        *,
        name: str,
        redirect_uri: str,
        host: str | None = None,
        scopes: tuple[str, ...] = (),
    ) -> AppCredentials:
        """Register your app on one Mastodon server.

        No portal, no review, no waiting. The server answers with the two
        values you need and you can sign somebody in straight away.

        Save what comes back and put it on the `LoginRequest` for every
        login on this server - `SocialChimp` does that for you. Registering
        again would work, but it leaves an unused record on somebody else's
        server, so it is worth saving.

        Args:
            name: What people see on the approval page.
            redirect_uri: Where Mastodon sends people back to. It has to
                match exactly at login time.
            host: Which server to register on. Required - `mastodon.social`
                and `fosstodon.org` are different networks.
            scopes: Permissions to ask for. Left out, `read write`.

        Returns:
            The credentials for this server. Save them.

        Raises:
            ConfigError: If no server was named.
            PlatformError: If the server answered without credentials.
        """
        server = _clean_host(host)
        form: dict[str, Any] = {
            "client_name": name,
            "redirect_uris": redirect_uri,
            "scopes": " ".join(scopes or DEFAULT_SCOPES),
        }
        if self._website is not None:
            form["website"] = self._website

        async with self._client(server) as http:
            reply = await http.json("POST", "/api/v1/apps", data=form)

        return AppCredentials(
            platform=PLATFORM_NAME,
            # Stamped with the server it works on, because that is half of
            # what makes it findable again.
            host=server,
            client_id=_text(reply, "client_id", "register an app"),
            client_secret=_text(reply, "client_secret", "register an app"),
        )

    async def start_login(self, request: LoginRequest) -> SendToNetwork:
        """Build the address to send somebody to so they can approve your app.

        Nothing is sent to Mastodon here. The address carries the hash of a
        secret; the secret itself comes back to you in `remember`, and is
        sent later, in `finish_login`, to prove the code came back to the
        same place that asked for it.

        Keep `remember` with that person's session and hand it back. Nothing
        is held here between the two calls, because the person may be sent
        away by one web worker and come back to another.

        Args:
            request: Where to send them back to, which server, what to ask
                for, and your app's credentials for that server.

        Returns:
            Always a `SendToNetwork`: Mastodon has an approval page, so
            there is somewhere to send people. It carries the address to
            redirect their browser to, the state value that will come back
            with them, and the secret to hand back.

        Raises:
            ConfigError: If no server was named, or the request carries no
                credentials for it.
        """
        server = _clean_host(request.host)
        app = _app_on(request, server)

        state = request.state or secrets.token_urlsafe(_STATE_BYTES)
        verifier = secrets.token_urlsafe(_VERIFIER_BYTES)

        query = httpx.QueryParams(
            {
                "response_type": "code",
                "client_id": app.client_id,
                "redirect_uri": request.redirect_uri,
                "scope": " ".join(request.scopes or DEFAULT_SCOPES),
                "state": state,
                "code_challenge": _challenge_for(verifier),
                "code_challenge_method": "S256",
            }
        )
        return SendToNetwork(
            url=f"https://{server}/oauth/authorize?{query}",
            state=state,
            remember={"code_verifier": verifier},
        )

    async def finish_login(
        self,
        request: LoginRequest,
        callback: Mapping[str, str],
        remember: RawData | None = None,
    ) -> Finished:
        """Swap the code Mastodon sent back for a token, and build a connection.

        Hand this the whole query string Mastodon put on your redirect
        address, as a dictionary, along with the `remember` value
        `start_login` gave you.

        Args:
            request: The same request used to start the login.
            callback: The query values Mastodon sent back. It must have
                `code`; `state` is checked when it is there.
            remember: What `start_login` put in `SendToNetwork.remember`.

        Returns:
            The finished connection. Save it.

        Raises:
            AuthError: If the person said no, if there is no code, if the
                state that came back is not the one we sent, or if the secret
                from `start_login` did not come back.
            ConfigError: If no server was named, or the request carries no
                credentials for it.
            PlatformError: If Mastodon answered without a token.
        """
        server = _clean_host(request.host)
        app = _app_on(request, server)
        _check_state(request, callback)
        code = _code_from(callback)
        verifier = _verifier_from(remember)

        asked_for = request.scopes or DEFAULT_SCOPES
        form: dict[str, Any] = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": app.client_id,
            "client_secret": app.client_secret,
            "redirect_uri": request.redirect_uri,
            "scope": " ".join(asked_for),
            # The other half of the pair from `start_login`. Mastodon hashes
            # it and checks the result against what it was sent earlier.
            "code_verifier": verifier,
        }

        async with self._client(server) as http:
            reply = await http.json("POST", "/oauth/token", data=form)
            access_token = _text(reply, "access_token", "sign someone in")
            me = await http.json(
                "GET",
                "/api/v1/accounts/verify_credentials",
                headers={"Authorization": f"Bearer {access_token}"},
            )

        # A server may grant less than we asked for, and it says so here.
        granted = reply.get("scope")
        given = granted.split() if isinstance(granted, str) and granted else []
        scopes = tuple(given) if given else asked_for
        account_id = _text(me, "id", "say who just signed in")
        handle = _text(me, "acct", "say who just signed in")

        return Finished(
            connection=Connection(
                # A name that cannot collide with the same person's account
                # on another server. Rename it if your app prefers its own.
                id=f"{PLATFORM_NAME}:{server}:{account_id}",
                platform=PLATFORM_NAME,
                host=server,
                account_id=account_id,
                # verify_credentials always answers about a local account, so
                # `acct` is the bare username and the server has to be added.
                account_name=f"@{handle}@{server}",
                token=Token(access_token=access_token),
                scopes=scopes,
                extra={"profile_url": me.get("url")},
            )
        )

    async def refresh(self, connection: Connection) -> Token:
        """Hand back the token that is already there.

        Mastodon access tokens do not expire. They work until the person
        revokes them, and there is no refresh token because there is nothing
        to refresh. So this calls nothing and changes nothing.

        Args:
            connection: The account socialchimp was about to renew.

        Returns:
            The token the connection already holds.
        """
        return connection.token

    async def limits(self, connection: Connection) -> Limits:
        """Ask a server what it currently allows.

        Worth asking rather than assuming. Mastodon's own default is 500
        characters, but whoever runs a server can change it, and plenty run
        at 5,000. The answer is kept for a few minutes so a burst of posts
        does not ask again for every one.

        Args:
            connection: The account whose server to ask.

        Returns:
            What that server allows right now.

        Raises:
            ConfigError: If the connection has no server on it.
        """
        server = _host_of(connection)

        remembered = self._known_limits.get(server)
        now = time.monotonic()
        if remembered is not None and remembered[0] > now:
            return remembered[1]

        async with self._client(server, connection.token.access_token) as http:
            reply = await http.json("GET", "/api/v2/instance")

        found = _limits_from_instance(reply)
        self._known_limits[server] = (now + self._limits_cache_seconds, found)
        return found

    async def publish(self, connection: Connection, post: Post) -> PostResult:
        """Publish a post.

        Files are uploaded first, one at a time. A video usually comes back
        as "still being processed", and this waits for it, because a post
        that names a file the server has not finished with is refused.

        Args:
            connection: The account to publish as.
            post: What to publish.

        Returns:
            What Mastodon said about the new post. A scheduled post comes
            back as `PostState.SCHEDULED` - Mastodon has taken it, and it
            goes live later.

        Raises:
            ConfigError: If the connection has no server on it.
            InvalidPostError: If a setting is unknown, if the post breaks one
                of the server's limits, or if Mastodon refuses it.
            PlatformError: If a video never finishes processing.
        """
        server = _host_of(connection)
        # Settings are checked before anything is sent, so a typo costs no
        # request and no part of the account's allowance.
        options = _checked_options(post.options)

        allowed = await self.limits(connection)
        check_post(
            post,
            platform=PLATFORM_NAME,
            features=self.features,
            limits=allowed,
        )

        async with self._client(server, connection.token.access_token) as http:
            media_ids = [await self._upload(http, item) for item in post.media]

            form: dict[str, Any] = {"status": post.text, **options}
            if media_ids:
                form["media_ids[]"] = media_ids
            if post.reply_to is not None:
                form["in_reply_to_id"] = post.reply_to
            if post.publish_at is not None:
                form["scheduled_at"] = post.publish_at.isoformat()

            reply = await http.json(
                "POST",
                "/api/v1/statuses",
                data=form,
                headers={"Idempotency-Key": post_fingerprint(post)},
            )

        post_id = _text(reply, "id", "publish a post")
        if post.publish_at is not None:
            # A scheduled post has no address yet, because it does not exist
            # yet. Mastodon answers with a plan, not a post.
            return PostResult(
                id=post_id,
                url=None,
                state=PostState.SCHEDULED,
                raw=reply,
            )

        url = reply.get("url")
        return PostResult(
            id=post_id,
            url=url if isinstance(url, str) else None,
            state=PostState.DONE,
            raw=reply,
        )

    async def _upload(self, http: HttpClient, item: Media) -> str:
        """Send one file to a server and wait until it is usable.

        Args:
            http: A client already pointed at the right server.
            item: The picture or video to send.

        Returns:
            The server's id for the file, to name in the post.

        Raises:
            InvalidPostError: If all we have is a link to the file.
            PlatformError: If the server never finishes processing it.
        """
        if item.content is None and item.path is None:
            message = (
                f"Mastodon will not fetch {item.url!r} for you - it only "
                f"takes files sent to it. Download the file first, then use "
                f"Media.from_bytes or Media.from_file."
            )
            raise InvalidPostError(message)

        files = {"file": (item.filename or "upload", item.read(), item.content_type)}
        described = {"description": item.alt_text} if item.alt_text else {}

        response = await http.post("/api/v2/media", files=files, data=described)
        reply = read_body(response)
        media_id = _text(reply, "id", "take a file")

        # A picture is usually ready at once (200). Video and audio come back
        # as "accepted, still working on it" (202).
        if response.status_code == httpx.codes.ACCEPTED:
            await self._wait_until_ready(http, media_id)
        return media_id

    async def _wait_until_ready(self, http: HttpClient, media_id: str) -> None:
        """Keep asking about a file until the server has finished with it.

        Args:
            http: A client already pointed at the right server.
            media_id: The file to ask about.

        Raises:
            PlatformError: If it is still not ready after all our checks. The
                file is not lost - it is on the account, and posting it again
                later will work.
        """
        for _ in range(self._media_checks):
            await _wait(self._media_wait_seconds)
            # 200 means the file is ready. **206** means it is still being
            # processed - not 202. The upload answers 202 the first time, but
            # every later check answers 206, and the docs put those two codes
            # on separate pages, which is easy to misread.
            response = await http.get(f"/api/v1/media/{media_id}")
            if response.status_code == httpx.codes.OK:
                return

        message = (
            f"Mastodon is still working on file {media_id} after "
            f"{self._media_checks} checks. Big videos take longer than this; "
            f"raise media_checks or media_wait_seconds and try again."
        )
        raise PlatformError(message, platform=PLATFORM_NAME)

    async def delete_post(self, connection: Connection, post_id: str) -> None:
        """Remove a post.

        Args:
            connection: The account that published it.
            post_id: Mastodon's id for the post.

        Raises:
            ConfigError: If the connection has no server on it.
            NotFoundError: If there is no such post on this account.
        """
        server = _host_of(connection)
        async with self._client(server, connection.token.access_token) as http:
            await http.delete(f"/api/v1/statuses/{post_id}")

    # Mastodon can also hold a socket open and tell us the moment something
    # happens (`/api/v1/streaming`). That would go alongside this method, as
    # a `CanCheckSignature`-style listener. Checking on a timer comes first
    # because it needs nothing kept running, survives a restart with no lost
    # updates, and works the same on every server - a socket that drops
    # silently loses updates until somebody notices.
    async def fetch_updates(
        self,
        connection: Connection,
        since: datetime | None,
    ) -> Sequence[Update]:
        """Return what has happened on this account since a moment in time.

        Mastodon pages its notifications by id rather than by time, and ids
        are not comparable across servers. So we read a recent page and drop
        anything older than the marker. Check often enough that a page covers
        the gap - the default of 40 is plenty for most accounts.

        A follow has no name of ours, so it arrives as
        `UpdateKind.UNKNOWN` with Mastodon's own word kept on `kind_name`.

        Args:
            connection: The account to ask about.
            since: Only return things newer than this. `None` on the first
                call.

        Returns:
            The updates, oldest first.

        Raises:
            ConfigError: If the connection has no server on it.
        """
        server = _host_of(connection)
        params = [("types[]", word) for word in WATCHED_NOTIFICATIONS]
        params.append(("limit", str(self._updates_per_check)))

        async with self._client(server, connection.token.access_token) as http:
            response = await http.get("/api/v1/notifications", params=params)

        # Notifications arrive as a list, so `read_body` puts them under
        # "body" rather than handing back an object.
        found = read_body(response).get("body")
        items = (
            [raw for raw in found if isinstance(raw, dict)]
            if isinstance(found, list)
            else []
        )

        updates: list[Update] = []
        for raw in items:
            when = _moment(str(raw.get("created_at", "")))
            if when is None or (since is not None and when <= since):
                continue
            word = str(raw.get("type", ""))
            updates.append(
                Update.from_network(
                    update_id=str(raw.get("id", "")),
                    kind_name=_OUR_WORD_FOR.get(word, word),
                    platform=PLATFORM_NAME,
                    connection_id=connection.id,
                    created_at=when,
                    raw=raw,
                )
            )

        # Mastodon hands back the newest first; socialchimp wants the oldest.
        updates.reverse()
        return updates


def _app_on(request: LoginRequest, host: str) -> AppCredentials:
    """Read your app's credentials off a login request.

    Args:
        request: The request being started or finished.
        host: The server they have to be for, used in the message.

    Returns:
        The credentials.

    Raises:
        ConfigError: If the request carries none, saying what to call.
    """
    if request.app is None:
        message = (
            f"This login request carries no app credentials for {host}. "
            f"Every Mastodon server is separate, so an app registered on one "
            f'means nothing on another. Call create_app(host="{host}", ...) '
            f"once, save what comes back, and put it on the LoginRequest."
        )
        raise ConfigError(message)
    return request.app


def _check_state(request: LoginRequest, callback: Mapping[str, str]) -> None:
    """Check the value that came back is the one we sent.

    This is what stops somebody handing your app a login they started
    themselves and having it saved against one of your users.

    Args:
        request: The request used to start the login.
        callback: The query values Mastodon sent back.

    Raises:
        AuthError: If both sides have a state and they are different.
    """
    returned = callback.get("state", "")
    if request.state is not None and returned and returned != request.state:
        message = (
            "The state Mastodon sent back did not match the one we sent. "
            "This login did not start here, so nothing has been saved. Start "
            "a new one."
        )
        raise AuthError(message)


def _verifier_from(remember: RawData | None) -> str:
    """Read the secret `start_login` made back out of what your app kept.

    Args:
        remember: What `start_login` put in `SendToNetwork.remember`.

    Returns:
        The secret to send with the code.

    Raises:
        AuthError: If it did not come back. Without it Mastodon cannot tell
            that this is the same sign-in it started, and will refuse the
            code - so saying it here is clearer than letting the server say
            it in its own words.
    """
    verifier = (remember or {}).get("code_verifier")
    if not isinstance(verifier, str) or not verifier:
        message = (
            "This sign-in cannot be finished because the secret made at the "
            "start did not come back. Pass SendToNetwork.remember to "
            "finish_login as `remember`. Keep it with that person's session "
            "rather than in memory: they may be sent away by one web worker "
            "and come back to another."
        )
        raise AuthError(message)
    return verifier


def _code_from(callback: Mapping[str, str]) -> str:
    """Pull the login code out of what Mastodon sent back.

    Args:
        callback: The query values Mastodon sent back.

    Returns:
        The code to swap for a token.

    Raises:
        AuthError: If the person said no, or if there is no code.
    """
    refused = callback.get("error")
    if refused:
        said = callback.get("error_description", "")
        detail = f" It said: {said}" if said else ""
        message = (
            f"Mastodon did not sign this person in ({refused}). Usually they "
            f"pressed cancel on the approval page.{detail}"
        )
        raise AuthError(message)

    code = callback.get("code")
    if not code:
        message = (
            "Mastodon sent no code back, so there is nothing to swap for a "
            "token. Check you are passing the whole query string from your "
            "redirect address."
        )
        raise AuthError(message)
    return code
