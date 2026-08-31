"""The parts of Meta that Facebook, Instagram and Threads all share.

Meta runs three networks socialchimp cares about. Facebook and Instagram are
the same network wearing two hats: one sign-in page, one way of swapping a
code for a token, one way of turning a short token into a long one, one
address for their API, one set of error codes, one pair of rate-limit
headers, and one way of signing the requests they push to you.

So all of that is written once, here, and each network adds only what is
actually its own. This module is private - the leading underscore says so -
because it is a place for us to share code, not a thing to build against.

**Threads shares less of this than you would expect.** It has its own app id
and secret, its own sign-in page on threads.net, its own API host at
graph.threads.net, and its own shape of pushed message - so `GRAPH_API`,
`SIGN_IN_PAGE`, `swap_code_for_token`, `long_lived_token` and `changes_in`
are all Facebook-and-Instagram only. What Threads does share is everything
below that is about Meta rather than about one network: the error codes, the
rate-limit headers, the signature on a pushed request, the way a state and a
login code are checked, and the shape of the daily posting allowance.

## You have to make the app by hand

There is no `create_app` anywhere in Meta. You fill in a form at
https://developers.facebook.com/apps, and then two more things have to
happen before a single post goes out:

1. **Meta reviews your app.** Permissions like `pages_manage_posts` only
   work for people who have a role on the app until it passes review.
2. **Your business is verified.** That means sending Meta documents about
   the company behind the app, and waiting.

Until both are done your code works perfectly for you and for nobody else,
which is a confusing thing to debug. `app_must_be_made_by_hand` builds the
error that says all of this out loud.

## An unhappy reply can arrive as a perfectly happy one

Meta answers 200 and puts the refusal in the body more often than you would
believe:

    HTTP/1.1 200 OK
    {"error": {"message": "...", "code": 190, "error_subcode": 463}}

Anything that only looks at the status code sails straight past that and then
falls over somewhere else entirely. So every reply goes through `Graph`,
which reads the body of a happy reply as carefully as an unhappy one.

The codes matter more than the statuses. 190 is a token problem, 4, 17, 32
and 613 are all "slow down" wearing different hats, 10 and 200 are missing
permissions. `meta_errors` turns each of them into one of ours with a
sentence a person can act on, because a raw Meta error is not readable.

## Meta counts what you use, and tells you

Two headers come back on most replies, both holding JSON:

- `X-App-Usage` - how much of your app's hourly allowance is gone, as three
  percentages.
- `X-Business-Use-Case-Usage` - the same, per page, plus how many minutes
  Meta thinks you should wait if you have gone too far.

`usage_from_headers` reads both, and `Graph.usage` keeps the last thing they
said. Watching that number is how you slow down before Meta stops answering
rather than after.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import httpx

from socialchimp.errors import (
    AuthError,
    ConfigError,
    InvalidPostError,
    NotAllowedError,
    NotSupportedError,
    PlatformError,
    RateLimitError,
)
from socialchimp.events import verify_hmac_sha256
from socialchimp.http import HttpClient, error_from_response, paginate, read_body
from socialchimp.models import RawData, Token

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType

    from socialchimp.errors import SocialChimpError
    from socialchimp.events import Update
    from socialchimp.models import AppCredentials, Connection
    from socialchimp.platform import LoginRequest

__all__ = [
    "DEVELOPER_PORTAL",
    "GRAPH_API",
    "GRAPH_VERSION",
    "PAGE_FIELDS",
    "SIGNATURE_HEADER",
    "SIGN_IN_PAGE",
    "STATE_BYTES",
    "Change",
    "Graph",
    "MetaPage",
    "Usage",
    "app_must_be_made_by_hand",
    "changes_in",
    "check_meta_signature",
    "check_state",
    "code_from",
    "credentials_or_refuse",
    "first_update",
    "long_lived_token",
    "meta_errors",
    "page_by_id",
    "pages_of",
    "quota_left",
    "required_text",
    "sign_in_url",
    "state_for",
    "swap_code_for_token",
    "token_from",
    "usage_from_headers",
    "where_to_post",
]

GRAPH_VERSION: Final = "v21.0"
"""Which version of Meta's API everything here talks to.

Meta keeps roughly two years of versions working at once and this one is
good until January 2027. Changing this one line moves every address below,
which is the whole reason it is a constant - but read Meta's changelog
first, because a new version is where they remove things.
"""

GRAPH_API: Final = f"https://graph.facebook.com/{GRAPH_VERSION}"
"""Where Meta's API lives. One address for everybody, unlike Mastodon."""

SIGN_IN_PAGE: Final = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth"
"""The page people are sent to so they can approve your app.

On facebook.com rather than graph.facebook.com, because this one is for a
person to look at and the other is for your code.
"""

DEVELOPER_PORTAL: Final = "https://developers.facebook.com/apps"
"""Where a person creates a Meta app by hand. There is no other way."""

SIGNATURE_HEADER: Final = "X-Hub-Signature-256"
"""The header Meta signs a pushed request with.

It also sends `X-Hub-Signature`, which is SHA-1 and much weaker. We only
ever look at this one, so a request carrying only the old header is refused.
"""

SWAP_PATH: Final = "/oauth/access_token"
"""Where a code is swapped for a token, and a short token for a long one."""

MY_PAGES_PATH: Final = "/me/accounts"
"""Where the pages a person manages are listed, each with its own token."""

PAGE_FIELDS: Final = "id,name,category,access_token"
"""What to ask for about a page.

Meta hands back an id and a name unless you ask for more, and the token is
the part we actually need. Instagram asks for more than this and passes its
own list in.
"""

PAGES_PER_REQUEST: Final = 100
"""How many pages to read at a time. Meta's own default is 25."""

MOST_PAGES_TO_READ: Final = 10
"""How many pages of pages to read before stopping.

At a hundred a time that is a thousand pages, which is far more than anyone
manages. It is here so a paging bug on either side cannot spin forever.
"""

STATE_BYTES: Final = 24
"""How much randomness goes into a state nobody was given one for.

Long enough that nobody can guess one, short enough to sit in a URL.
"""

# Meta's error codes, grouped by what a person would have to do about them.
# The numbers are the valuable part of a Meta error: the status code is
# usually 400 whatever went wrong, and the message is written for whoever
# built the API rather than whoever has to fix it.
_TOKEN_TROUBLE: Final = frozenset({190})
_SLOW_DOWN: Final = frozenset({4, 17, 32, 613})
_NOT_ALLOWED: Final = frozenset({10, 200})
_BLOCKED_ON_POLICY: Final = 368
_DUPLICATE_POST: Final = 506
_DID_NOT_UNDERSTAND: Final = 100

# Why a token stopped working. Meta puts this under `error_subcode`, and it
# is the difference between "ask them to sign in again" and "wait, they are
# in the middle of something on Facebook".
_WHY_THE_TOKEN_FAILED: Final = {
    458: "The person removed your app from their Facebook settings.",
    459: (
        "Facebook is asking the person to log in to Facebook itself and "
        "confirm something before it will let the app act for them again."
    ),
    460: (
        "The person changed their Facebook password, which throws away every "
        "token that was made before it."
    ),
    463: "The token has run out.",
    467: (
        "The token is no longer valid - it was taken away, or replaced by a "
        "newer one, or the app secret behind it changed."
    ),
}

_FAILED_SOMEHOW: Final = "The token has run out, been taken away, or was never right."


def _now() -> datetime:
    """Return the current moment.

    Kept as its own function so tests can say exactly when a token runs out.

    Returns:
        Now, with a timezone.
    """
    return datetime.now(UTC)


def app_must_be_made_by_hand(platform: str) -> NotSupportedError:
    """Build the error for somebody who asked us to register a Meta app.

    Meta has no way to do this from code, and saying so early saves a lot of
    hunting. The message names the portal, the review and the business
    verification, because those are the three surprises in that order.

    Args:
        platform: Which of Meta's networks was asked, for the message.

    Returns:
        The error to raise. Returned rather than raised so the type checker
        follows what happens next at the place it is used.
    """
    return NotSupportedError(
        platform=platform,
        what="registering an app for you",
        suggestion=(
            f"No Meta network does - there is no call for it. Make the app "
            f"by hand at {DEVELOPER_PORTAL}, then save its id and secret "
            f"with Storage.save_app. Two more things have to happen before "
            f"it works for anybody but you: Meta has to review the app, and "
            f"it needs business verification, which means sending Meta "
            f"documents about the company behind it. Until both are done, "
            f"posting works for people with a role on the app and silently "
            f"fails for everyone else."
        ),
    )


def credentials_or_refuse(
    app: AppCredentials | None,
    *,
    platform: str,
    what: str,
) -> AppCredentials:
    """Insist on your app's id and secret, or say plainly where to get them.

    Meta signs both halves of a sign-in and every token swap with the pair,
    so there is nothing worth trying without them. Every Meta network needs
    this, which is why it lives here rather than in one of them.

    Args:
        app: The credentials that arrived, which may be none at all.
        platform: Which network, for the message.
        what: What we were about to do, for the message.

    Returns:
        The credentials.

    Raises:
        ConfigError: If there are none.
    """
    if app is None:
        message = (
            f"{platform} needs your app's id and secret to {what}, and none "
            f"arrived. Meta has no way to register an app from code, so "
            f"create one by hand at {DEVELOPER_PORTAL}, then save its id and "
            f"secret with Storage.save_app - socialchimp hands them to every "
            f"sign-in and every renewal after that."
        )
        raise ConfigError(message)
    return app


def sign_in_url(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: tuple[str, ...],
    state: str,
    page: str = SIGN_IN_PAGE,
) -> str:
    """Build the address to send somebody to so they can approve your app.

    Nothing is sent to Meta here. The address is built and handed back, and
    the person's browser is what actually goes there.

    Args:
        client_id: Your app's id, from the developer portal.
        redirect_uri: Where Meta sends them back to. It has to be listed on
            the app in the portal, character for character, or the person
            sees Meta's own error page instead of your app.
        scopes: The permissions to ask for.
        state: A value that comes back with them, for matching up the reply.
        page: Which sign-in page. Facebook's own by default.

    Returns:
        The address to redirect to.
    """
    query = httpx.QueryParams(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            # Meta wants the permissions separated by commas. Nearly every
            # other network wants spaces, and a space-separated list here
            # gets you a sign-in page asking for one long nonsense
            # permission rather than an error saying what is wrong.
            "scope": ",".join(scopes),
            "state": state,
            "response_type": "code",
        }
    )
    return f"{page}?{query}"


def state_for(request: LoginRequest) -> str:
    """Work out the state to send out with a sign-in.

    Args:
        request: The request the sign-in is starting from.

    Returns:
        Whatever the caller chose, or a fresh random one when they left it
        out. The value comes straight back from the network, which is how
        your app tells one person's half-finished sign-in from another's.
    """
    return request.state or secrets.token_urlsafe(STATE_BYTES)


def check_state(
    request: LoginRequest,
    callback: Mapping[str, str],
    *,
    platform: str,
) -> None:
    """Check the value that came back is the one we sent.

    This is what stops somebody handing your app a login they started
    themselves and having it saved against one of your users.

    Args:
        request: The request used to start the login.
        callback: The query values the network sent back.
        platform: Which of Meta's networks sent them, for the message.

    Raises:
        AuthError: If both sides have a state and they are different. A
            callback with no state at all is left alone: Meta only sends one
            back when one went out, and missing is not the same as wrong.
    """
    returned = callback.get("state", "")
    if request.state is not None and returned and returned != request.state:
        message = (
            f"The state {platform} sent back did not match the one we sent. "
            f"This login did not start here, so nothing has been saved. Start "
            f"a new one."
        )
        raise AuthError(message)


def code_from(callback: Mapping[str, str], *, platform: str) -> str:
    """Pull the login code out of what the network sent back.

    Args:
        callback: The query values the network sent back.
        platform: Which of Meta's networks sent them, for the message.

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
            f"{platform} did not sign this person in ({refused}). Usually they "
            f"pressed cancel on the approval page.{detail}"
        )
        raise AuthError(message)

    code = callback.get("code")
    if not code:
        message = (
            f"{platform} sent no code back, so there is nothing to swap for a "
            f"token. Check you are passing the whole query string from your "
            f"redirect address."
        )
        raise AuthError(message)
    return code


def where_to_post(
    connection: Connection,
    *,
    key: str,
    what: str,
    platform: str,
) -> str:
    """Work out which page or account a connection publishes to.

    Every Meta network posts to something other than the person who signed
    in - a Facebook page, an Instagram business account, a Threads profile -
    and every one of them writes that id onto the connection the same way.

    Args:
        connection: The account to look at.
        key: Where a sign-in wrote the id, under `Connection.extra`.
        what: What kind of thing it is, for the message.
        platform: Which of Meta's networks, for the message.

    Returns:
        The network's id for the thing being posted to.

    Raises:
        ConfigError: If the connection names none.
    """
    found = connection.extra.get(key)
    if isinstance(found, str) and found:
        return found
    # Every connection a sign-in here builds sets both, and the account id is
    # the same id. A connection from somewhere else may only have that one.
    if connection.account_id:
        return connection.account_id

    message = (
        f"The connection {connection.id!r} names no {what}, so there is "
        f"nowhere to post. A connection made by signing in to {platform} "
        f"carries the id in extra[{key!r}]; one built by hand needs that set, "
        f"or needs account_id filled in."
    )
    raise ConfigError(message)


def required_text(reply: RawData, key: str, *, platform: str, when: str) -> str:
    """Read a value Meta always sends, and complain plainly if it did not.

    Args:
        reply: What Meta answered.
        key: The field we need.
        platform: Which network answered, for the message.
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
        f"{platform} left {key!r} out of its reply when we asked it to "
        f"{when}. That should not happen. The whole reply is on this error."
    )
    raise PlatformError(message, platform=platform, raw=reply)


def token_from(reply: RawData, *, platform: str, when: str) -> Token:
    """Build a token out of what Meta answered.

    Meta hands out no refresh token, ever. There is nothing to refresh with:
    a token is either extended by trading it in while it still works, or the
    person signs in again. So `Token.refresh_token` is always `None` here,
    and that is a fact about Meta rather than something missing.

    Args:
        reply: What Meta answered.
        platform: Which network answered, for the message.
        when: What we had asked it to do, for the message.

    Returns:
        The token, with an expiry when Meta gave one. No expiry means the
        token does not run out on its own, which is what a page token
        made from a long-lived user token does.

    Raises:
        PlatformError: If there is no token in the reply.
    """
    access = required_text(reply, "access_token", platform=platform, when=when)

    seconds = reply.get("expires_in")
    # A zero, a missing value or anything that is not a number all mean the
    # same thing here: Meta is not telling us when this runs out. `None` is
    # how socialchimp says "does not expire on its own", and treating a
    # missing number as an instant expiry would renew a working token to
    # death.
    if not isinstance(seconds, int | float) or isinstance(seconds, bool):
        return Token(access_token=access)
    if seconds <= 0:
        return Token(access_token=access)

    return Token(
        access_token=access,
        expires_at=_now() + timedelta(seconds=float(seconds)),
    )


# ---------------------------------------------------------------------------
# How many posts are left today
# ---------------------------------------------------------------------------


def _whole_number(value: object) -> int | None:
    """Read a value that should be a count.

    Args:
        value: Whatever arrived under that key.

    Returns:
        The number, or `None` for anything that is not one. `True` is not a
        number here, whatever Python thinks.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def quota_left(
    reply: RawData,
    *,
    used: str = "quota_usage",
    allowed_in: str = "config",
) -> int | None:
    """Read how much of a daily allowance is left out of Meta's answer.

    Instagram and Threads both count posts over a rolling 24 hours and both
    answer in the same shape: a list of one, holding how many have been used
    and an object holding how many are allowed. Threads counts replies as
    well, under names of their own, which is why the two keys are arguments.

    Args:
        reply: What the network answered.
        used: The key holding how many have been used.
        allowed_in: The key holding the object with `quota_total` in it.

    Returns:
        How many are left, or `None` when the answer was not one we can read.
        Never a guess: a made-up number here would refuse posts the network
        would have taken.
    """
    entries = reply.get("data")
    first = entries[0] if isinstance(entries, list) and entries else None
    if not isinstance(first, dict):
        return None

    config = first.get(allowed_in)
    allowed = (
        _whole_number(config.get("quota_total")) if isinstance(config, dict) else None
    )
    spent = _whole_number(first.get(used))
    if allowed is None or spent is None:
        return None

    # Meta has been known to count past its own total. That means none left,
    # not a negative number of posts.
    return max(allowed - spent, 0)


# ---------------------------------------------------------------------------
# How much of the allowance is left
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Usage:
    """How much of Meta's hourly allowance has been used, as percentages.

    Every field may be `None`, which means "Meta did not say" - never
    "zero". At 100 the next request is refused, so it is worth slowing down
    somewhere around 80.

    Attributes:
        calls: Percentage of the requests allowed in the last hour that have
            been made.
        cpu_time: Percentage of the processing time allowed.
        total_time: Percentage of the wall-clock time allowed.
        wait_seconds: How long Meta thinks it will be before it answers
            normally again. Meta counts this one in minutes; it is turned
            into seconds here, because everything else in socialchimp is
            seconds.
    """

    calls: int | None = None
    cpu_time: int | None = None
    total_time: int | None = None
    wait_seconds: float | None = None

    @property
    def worst(self) -> int | None:
        """The highest of the three percentages, which is the one that bites.

        Meta cuts you off when any single one of them reaches 100, so this
        is the number worth watching.
        """
        known = [
            found
            for found in (self.calls, self.cpu_time, self.total_time)
            if found is not None
        ]
        return max(known) if known else None


def _json_header(headers: httpx.Headers, name: str) -> object:
    """Read a header that holds JSON.

    Args:
        headers: The reply's headers.
        name: Which header to read.

    Returns:
        Whatever it held, or `None` if it is missing or unreadable. A header
        we cannot read is treated as no news, because guessing at a number
        that decides whether to keep posting is worse than not knowing.
    """
    value = headers.get(name)
    if value is None:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


def _highest(readings: list[RawData], key: str) -> int | None:
    """Return the largest whole number found under one key.

    Args:
        readings: Every set of figures Meta sent on this reply.
        key: Which figure to look at.

    Returns:
        The largest, or `None` if none of them mentioned it.
    """
    found = [
        value
        for reading in readings
        if isinstance(value := reading.get(key), int) and not isinstance(value, bool)
    ]
    return max(found) if found else None


def usage_from_headers(headers: httpx.Headers) -> Usage | None:
    """Read Meta's rate-limit headers.

    Two headers say the same thing at different scales: `X-App-Usage` is
    your whole app, `X-Business-Use-Case-Usage` is broken down per page.
    When both arrive we report the worse of them, because it is the worse
    one that stops the next request.

    Args:
        headers: The reply's headers.

    Returns:
        What Meta said, or `None` when it said nothing we can read.
    """
    readings: list[RawData] = []
    waits: list[float] = []

    for_the_app = _json_header(headers, "x-app-usage")
    if isinstance(for_the_app, dict):
        readings.append(for_the_app)

    per_page = _json_header(headers, "x-business-use-case-usage")
    if isinstance(per_page, dict):
        for entries in per_page.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                readings.append(entry)
                minutes = entry.get("estimated_time_to_regain_access")
                if isinstance(minutes, int | float) and minutes > 0:
                    waits.append(float(minutes) * 60.0)

    seen = Usage(
        calls=_highest(readings, "call_count"),
        cpu_time=_highest(readings, "total_cputime"),
        total_time=_highest(readings, "total_time"),
        wait_seconds=max(waits) if waits else None,
    )
    return seen if seen != Usage() else None


# ---------------------------------------------------------------------------
# Turning Meta's refusals into ours
# ---------------------------------------------------------------------------


def _said(error: RawData) -> str:
    """Pull Meta's own words out of its error object.

    Args:
        error: The error object Meta sent.

    Returns:
        Its message, ready to add to the end of ours, or an empty string.
    """
    message = error.get("message")
    return f" Meta said: {message}" if isinstance(message, str) and message else ""


def _how_much_is_gone(usage: Usage | None) -> str:
    """Say how much of the hourly allowance has been used, if we know.

    Args:
        usage: What the reply's headers said, if anything.

    Returns:
        A sentence to add to a "slow down" message, or an empty string.
    """
    if usage is None or usage.worst is None:
        return ""
    return (
        f" Your app has used {usage.worst}% of the requests Meta allows it in an hour."
    )


def _token_error(code: int, error: RawData, *, platform: str) -> AuthError:
    """Describe a token that Meta will not accept.

    Args:
        code: Meta's error code, always 190 here.
        error: The error object Meta sent.
        platform: Which network refused.

    Returns:
        The error to raise, naming why the token failed where Meta said.
    """
    subcode = error.get("error_subcode")
    why = _WHY_THE_TOKEN_FAILED.get(
        subcode if isinstance(subcode, int) else 0,
        _FAILED_SOMEHOW,
    )
    message = (
        f"{platform} would not accept our sign-in (error {code}). {why} The "
        f"person has to connect their account again - there is nothing to "
        f"renew, because Meta hands out no refresh token.{_said(error)}"
    )
    return AuthError(message, platform=platform, raw={"error": error})


def _error_from(
    body: RawData,
    *,
    platform: str,
    usage: Usage | None,
) -> SocialChimpError | None:
    """Turn Meta's own error object into one of ours.

    Args:
        body: The reply, already read into a dictionary.
        platform: Which network answered.
        usage: What the reply's rate-limit headers said, if anything.

    Returns:
        The error to raise, or `None` when this reply carries no error at
        all - which is how the caller tells a good reply from a bad one that
        arrived as a 200.
    """
    error = body.get("error")
    if not isinstance(error, dict):
        return None

    raw = {"error": error}
    code = error.get("code")
    code = code if isinstance(code, int) else 0

    if code in _TOKEN_TROUBLE:
        return _token_error(code, error, platform=platform)

    if code in _SLOW_DOWN:
        message = (
            f"{platform} is asking us to slow down (error {code})."
            f"{_how_much_is_gone(usage)} Meta counts requests by the hour, "
            f"so waiting is what fixes this.{_said(error)}"
        )
        return RateLimitError(
            message,
            retry_after=usage.wait_seconds if usage is not None else None,
            platform=platform,
            raw=raw,
        )

    if code in _NOT_ALLOWED:
        message = (
            f"{platform} will not let this account do that (error {code}). A "
            f"permission is missing or was taken away. Check the app asks "
            f"for it, that Meta has approved it in review, and that the "
            f"person granted it when they signed in.{_said(error)}"
        )
        return NotAllowedError(message, platform=platform, raw=raw)

    if code == _BLOCKED_ON_POLICY:
        message = (
            f"{platform} has blocked this account for a while (error {code}). "
            f"Meta does that when it thinks something breaks its rules - "
            f"posting too fast and posting the same thing repeatedly are the "
            f"usual reasons. Waiting is the only fix.{_said(error)}"
        )
        return NotAllowedError(message, platform=platform, raw=raw)

    if code == _DUPLICATE_POST:
        message = (
            f"{platform} will not publish the same words twice in a row "
            f"(error {code}). Change something in the post, or wait.{_said(error)}"
        )
        return InvalidPostError(message, platform=platform, raw=raw)

    if code == _DID_NOT_UNDERSTAND:
        message = (
            f"{platform} did not understand something in this request (error "
            f"{code}). Usually a value that is missing, empty or the wrong "
            f"shape.{_said(error)}"
        )
        return InvalidPostError(message, platform=platform, raw=raw)

    message = (
        f"{platform} refused this request (error {code}). socialchimp has no "
        f"better name for that code yet; the whole reply is on this "
        f"error.{_said(error)}"
    )
    return PlatformError(message, platform=platform, raw=raw)


def meta_errors(response: httpx.Response, *, platform: str) -> SocialChimpError:
    """Turn an unhappy reply from Meta into a socialchimp error.

    Meta's status codes say very little - almost everything is a 400 - so
    this reads the code inside the body instead, and only falls back to the
    shared mapping when there is no Meta error object to read.

    Args:
        response: The reply to turn into an error.
        platform: Which of Meta's networks sent it.

    Returns:
        The error to raise.
    """
    found = _error_from(
        read_body(response),
        platform=platform,
        usage=usage_from_headers(response.headers),
    )
    if found is not None:
        return found
    return error_from_response(response, platform=platform)


# ---------------------------------------------------------------------------
# Talking to Meta
# ---------------------------------------------------------------------------


class Graph:
    """One conversation with Meta's API.

    A thin layer over `HttpClient` that does the two things every Meta call
    needs and nothing else:

    1. Reads the body of a happy reply as carefully as an unhappy one,
       because Meta answers 200 with an error inside surprisingly often.
    2. Remembers what the rate-limit headers said, so you can slow down
       before Meta stops answering.

    Use it in an `async with` block and it closes the client underneath.

    Example:
        async with Graph(client, platform="facebook") as graph:
            me = await graph.json("GET", "/me")
    """

    def __init__(self, http: HttpClient, *, platform: str) -> None:
        """Wrap a client that is already pointed at Meta.

        Args:
            http: The client to send through. Give it `meta_errors` for its
                own error mapping so unhappy replies are named too.
            platform: Which of Meta's networks this talks to, used in
                messages.
        """
        self._http = http
        self._platform = platform
        self._usage: Usage | None = None

    @property
    def platform(self) -> str:
        """Which of Meta's networks this conversation is with."""
        return self._platform

    @property
    def usage(self) -> Usage | None:
        """What Meta last said about how much allowance is left.

        `None` until a reply mentions it. A later reply that says nothing
        leaves the last figures alone, because "no headers" means "no news",
        not "nothing left".
        """
        return self._usage

    async def json(self, method: str, path: str, **kwargs: object) -> RawData:
        """Send a request and read the reply, checking it for trouble.

        Args:
            method: `"GET"`, `"POST"` and so on.
            path: Joined onto Meta's address.
            **kwargs: Anything `HttpClient.request` takes, such as `params`,
                `data` or `files`.

        Returns:
            The reply, parsed.

        Raises:
            SocialChimpError: If Meta refused - whether it said so with a
                status code or hid it in the body of a 200.
        """
        response = await self._http.request(method, path, **kwargs)

        seen = usage_from_headers(response.headers)
        if seen is not None:
            self._usage = seen

        body = read_body(response)
        problem = _error_from(body, platform=self._platform, usage=self._usage)
        if problem is not None:
            raise problem
        return body

    async def aclose(self) -> None:
        """Close the connections underneath this conversation."""
        await self._http.aclose()

    async def __aenter__(self) -> Graph:
        """Hand this conversation to an `async with` block.

        Returns:
            This conversation.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the client when the block ends.

        Args:
            exc_type: The kind of error that ended the block, if any.
            exc: The error that ended the block, if any.
            traceback: Where that error came from, if any.
        """
        await self.aclose()


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


async def swap_code_for_token(
    graph: Graph,
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
) -> Token:
    """Swap the code Meta sent back for a token.

    The redirect address is sent again here, and Meta checks it matches the
    one the person was sent from. That is why it is an argument rather than
    something we could work out.

    Args:
        graph: A conversation with Meta. It needs no token of its own.
        client_id: Your app's id.
        client_secret: Your app's secret, which never leaves your server.
        redirect_uri: The same address the sign-in was started with.
        code: What Meta put on the end of your redirect address.

    Returns:
        A token that works for about an hour. Trade it for a long-lived one
        with `long_lived_token` before doing anything else with it.

    Raises:
        SocialChimpError: If Meta will not make the swap.
    """
    reply = await graph.json(
        "GET",
        SWAP_PATH,
        params={
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
    )
    return token_from(reply, platform=graph.platform, when="sign someone in")


async def long_lived_token(
    graph: Graph,
    *,
    client_id: str,
    client_secret: str,
    token: str,
) -> Token:
    """Trade a token for one that lasts about two months.

    This has to happen while the token being traded still works. There is no
    way back afterwards - Meta hands out no refresh token - so a login that
    skips this step gives you a connection that dies within the hour.

    It is also the whole of renewal on Meta. A token with an expiry on it is
    extended by trading it in the same way, well before it runs out, which
    is what `refresh` does with the sixty-day window socialchimp is given.

    Args:
        graph: A conversation with Meta. It needs no token of its own.
        client_id: Your app's id.
        client_secret: Your app's secret.
        token: The short-lived token to trade in.

    Returns:
        The long-lived token, good for about sixty days.

    Raises:
        SocialChimpError: If Meta will not make the trade.
    """
    reply = await graph.json(
        "GET",
        SWAP_PATH,
        params={
            "grant_type": "fb_exchange_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "fb_exchange_token": token,
        },
    )
    return token_from(reply, platform=graph.platform, when="extend a token")


# ---------------------------------------------------------------------------
# The pages somebody manages
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetaPage:
    """One page a person manages, and the token to act as it.

    Attributes:
        id: Meta's identifier for the page.
        name: What the page is called, which is what to show a person.
        token: The page's own token. Not the person's - posting as a page
            needs the page's. Kept out of `repr` so it does not reach a log.
        category: What kind of page Meta says it is, such as `"Bakery"`.
        raw: Meta's untouched entry, for anything we did not model.
    """

    id: str
    name: str
    token: str = field(repr=False)
    category: str | None = None
    raw: RawData = field(default_factory=dict, repr=False)


def _page_from(raw: RawData) -> MetaPage | None:
    """Read one page out of Meta's list.

    Args:
        raw: One entry as Meta sent it.

    Returns:
        The page, or `None` when there is no token on it. A page we cannot
        act as is no use to anybody, and offering it would mean somebody
        picks it and the sign-in fails at the last step.
    """
    page_id = raw.get("id")
    token = raw.get("access_token")
    if not (isinstance(page_id, str) and page_id):
        return None
    if not (isinstance(token, str) and token):
        return None

    name = raw.get("name")
    category = raw.get("category")
    return MetaPage(
        id=page_id,
        # A page always has a name, but showing an id is better than showing
        # nothing if one ever arrives without.
        name=name if isinstance(name, str) and name else page_id,
        token=token,
        category=category if isinstance(category, str) and category else None,
        raw=raw,
    )


def _pages_in(reply: RawData) -> list[RawData]:
    """Pull the entries out of one page of Meta's list.

    Args:
        reply: What Meta answered.

    Returns:
        The entries, or none at all if the reply is not shaped like a list.
    """
    found = reply.get("data")
    if not isinstance(found, list):
        return []
    return [entry for entry in found if isinstance(entry, dict)]


def _after_in(reply: RawData) -> str | None:
    """Work out how to ask for the next page of the list.

    Args:
        reply: What Meta answered.

    Returns:
        The cursor to send next time, or `None` when this was the last page.
    """
    paging = reply.get("paging")
    if not isinstance(paging, dict):
        return None
    # Meta sends a cursor on the last page as well as every other one, so
    # following the cursor alone asks for the same empty page forever. The
    # presence of `next` is what actually means "there is more".
    if not isinstance(paging.get("next"), str):
        return None
    cursors = paging.get("cursors")
    if not isinstance(cursors, dict):
        return None
    after = cursors.get("after")
    return after if isinstance(after, str) and after else None


async def pages_of(
    graph: Graph,
    *,
    fields: str = PAGE_FIELDS,
    per_request: int = PAGES_PER_REQUEST,
    max_pages: int = MOST_PAGES_TO_READ,
) -> tuple[MetaPage, ...]:
    """List the pages one person manages, each with its own token.

    Sign-in gives you a token for the *person*, and a person is not a page.
    This is the step in between: it turns "who signed in" into "which pages
    they could post as".

    Args:
        graph: A conversation carrying that person's token.
        fields: What to ask about each page. Instagram passes its own list.
        per_request: How many to read at a time.
        max_pages: How many requests to make before stopping.

    Returns:
        The pages, in Meta's own order, leaving out any that came back
        without a token.

    Raises:
        SocialChimpError: If Meta refuses to list them, which usually means
            the `pages_show_list` permission was never granted.
    """

    async def fetch(after: str | None) -> RawData:
        params: dict[str, object] = {"fields": fields, "limit": per_request}
        if after is not None:
            params["after"] = after
        return await graph.json("GET", MY_PAGES_PATH, params=params)

    found = [
        entry
        async for entry in paginate(fetch, _pages_in, _after_in, max_pages=max_pages)
    ]
    read = (_page_from(entry) for entry in found)
    return tuple(page for page in read if page is not None)


async def page_by_id(
    graph: Graph,
    *,
    page_id: str,
    fields: str = PAGE_FIELDS,
) -> MetaPage:
    """Look up one page, and the token to act as it.

    Args:
        graph: A conversation carrying the person's token.
        page_id: Which page.
        fields: What to ask about it.

    Returns:
        The page.

    Raises:
        AuthError: If Meta answered without a token for it. That means this
            person cannot post as that page - either they never could, or
            they left out that page in Meta's own page picker.
        SocialChimpError: If Meta refuses the lookup.
    """
    reply = await graph.json("GET", f"/{page_id}", params={"fields": fields})

    page = _page_from(reply)
    if page is None:
        message = (
            f"{graph.platform} gave us no token for page {page_id!r}, so we "
            f"cannot post as it. Either this person does not manage that "
            f"page, or they left it unticked on Facebook's own page picker "
            f"while signing in. Ask them to connect their account again and "
            f"tick the page they want."
        )
        raise AuthError(message, platform=graph.platform, raw=reply)
    return page


# ---------------------------------------------------------------------------
# Requests Meta sends us
# ---------------------------------------------------------------------------


def check_meta_signature(
    body: bytes,
    headers: Mapping[str, str],
    *,
    secret: str,
) -> None:
    """Check a request Meta pushed to us really came from Meta.

    The signature covers the **raw bytes** of the body. Any framework that
    parses the JSON and builds it again first changes the spacing and the
    key order, and the check then fails on a request that was perfectly
    good. Read the body, check it here, and parse it afterwards.

    Args:
        body: The request body, exactly as it arrived.
        headers: The request headers. Case does not matter.
        secret: Your app secret, from the developer portal. Meta signs with
            that, not with the verify token you typed into the webhook form.

    Raises:
        SignatureError: If the request cannot be trusted. Answer 401 and do
            nothing else with it.
    """
    verify_hmac_sha256(body, headers, secret=secret, header_name=SIGNATURE_HEADER)


@dataclass(frozen=True, slots=True)
class Change:
    """One thing that happened, out of a message Meta pushed to us.

    Meta wraps everything the same way whichever of its networks is talking:
    a list of accounts, and under each one a list of changes. This is one
    change, with the account it happened on already pulled out.

    Attributes:
        account_id: Which account it happened on. The page id for Facebook.
        when: When Meta says it happened. Always has a timezone.
        topic: What Meta calls this kind of change, such as `"feed"`.
        value: What actually happened, in Meta's own words. This is the part
            an app wants, and it is what ends up on `Update.raw`.
        envelope: The whole untouched entry this came in. The account id and
            the time live out there rather than on the change, so it is kept
            - alongside the change rather than in place of it.
    """

    account_id: str
    when: datetime
    topic: str
    value: RawData
    envelope: RawData = field(default_factory=dict, repr=False)


def _when_in(entry: RawData) -> datetime:
    """Work out when one entry says it happened.

    Args:
        entry: One account's entry from a pushed message.

    Returns:
        The moment, with a timezone. An entry with no time on it is stamped
        as it arrives - a little late is better than no time at all, since
        every update socialchimp holds is ordered by this.
    """
    seconds = entry.get("time")
    if isinstance(seconds, int | float) and not isinstance(seconds, bool):
        return datetime.fromtimestamp(float(seconds), UTC)
    return _now()


def changes_in(body: bytes, *, platform: str) -> list[Change]:
    """Unpack a message Meta pushed to us.

    One message can carry changes for several accounts at once, and several
    changes for each of them, so this always hands back a list. Meta batches
    when it is busy, which is exactly when you least want to drop the rest.

    Args:
        body: The request body, exactly as it arrived. Check its signature
            with `check_meta_signature` first.
        platform: Which of Meta's networks sent it, for the message.

    Returns:
        Every change in the message, in the order Meta listed them. Empty
        when the message carried none, which is not an error - Meta sends
        shapes we have no interest in.

    Raises:
        PlatformError: If the body is not a Meta message at all.
    """
    try:
        parsed = json.loads(body)
    except ValueError as problem:
        message = (
            f"This request from {platform} could not be read as JSON, so "
            f"there is nothing in it to act on. Pass the raw body, exactly "
            f"as it arrived."
        )
        raise PlatformError(message, platform=platform) from problem

    if not isinstance(parsed, dict):
        message = (
            f"This request from {platform} could not be read as one of its "
            f"messages: it holds a {type(parsed).__name__} where an object "
            f"was expected."
        )
        raise PlatformError(message, platform=platform)

    entries = parsed.get("entry")
    if not isinstance(entries, list):
        return []

    found: list[Change] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        account_id = str(entry.get("id", ""))
        when = _when_in(entry)
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            found.append(
                Change(
                    account_id=account_id,
                    when=when,
                    topic=str(change.get("field", "")),
                    value=value if isinstance(value, dict) else {},
                    envelope=entry,
                )
            )
    return found


def first_update(found: list[Update], *, platform: str) -> Update:
    """Take the one update a `read_update` is expected to hand back.

    Every Meta network can send several things in one message and every one
    of them offers a `read_updates` that hands back all of them. This is the
    other half: the single-update method, and the same refusal when there is
    nothing in the message to give.

    Args:
        found: Whatever `read_updates` made of the message.
        platform: Which of Meta's networks sent it, for the message.

    Returns:
        The first update.

    Raises:
        PlatformError: If the message carried none. That is not unusual -
            Meta sends shapes we have no interest in - which is why the
            message points at `read_updates`, whose answer is an empty list.
    """
    if not found:
        message = (
            f"This message from {platform} carries no change we can read, so "
            f"there is no update to hand back. It sends shapes we have no "
            f"interest in; call read_updates instead, which answers with an "
            f"empty list rather than raising."
        )
        raise PlatformError(message, platform=platform)
    return found[0]
