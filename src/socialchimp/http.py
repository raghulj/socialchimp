"""The shared way socialchimp talks to a social network.

Every platform file sends its requests through `HttpClient`. Trying again
after a hiccup, waiting as long as a network asks, reading how much of an
allowance is left, and turning an unhappy reply into a socialchimp error are
all written once here instead of nine times.

Nothing here is hidden or fixed in place. Pass your own transport to send
requests however you like, pass your own error function to name a network's
quirks, or use `paginate` on its own if page-by-page reading is all you want.

Example:
    async with HttpClient(
        "https://mastodon.social",
        platform="mastodon",
        headers={"Authorization": "Bearer ..."},
    ) as http:
        me = await http.json("GET", "/api/v1/accounts/verify_credentials")
"""

from __future__ import annotations

import math
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, TypeVar, cast

# anyio comes with httpx, so waiting through it adds no new dependency
# and lets a platform run under trio as happily as under asyncio.
import anyio
import httpx

from socialchimp.errors import (
    AuthError,
    ConfigError,
    NotAllowedError,
    NotFoundError,
    PlatformError,
    RateLimitError,
    SocialChimpError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
    from types import TracebackType

    from socialchimp.models import RawData

__all__ = [
    "HttpClient",
    "RateLimit",
    "Retries",
    "error_from_response",
    "paginate",
    "rate_limit_from_headers",
    "read_body",
    "retry_after_seconds",
]

# Used in messages when nobody said which network we are talking to.
_UNNAMED = "the network"

_DEFAULT_TIMEOUT = 30.0

# Headers different networks use for the same three numbers. httpx compares
# header names without caring about case, so `X-RateLimit-Limit` and
# `x-ratelimit-limit` both land on the first name below.
#
# X is the odd one out: it writes `x-rate-limit-*`, with a hyphen nobody else
# has. It is one character, it is easy to miss, and missing it means the
# allowance quietly reads as unknown forever - so both spellings are listed.
_LIMIT_HEADERS = ("x-ratelimit-limit", "x-rate-limit-limit", "ratelimit-limit")
_REMAINING_HEADERS = (
    "x-ratelimit-remaining",
    "x-rate-limit-remaining",
    "ratelimit-remaining",
)
_RESET_HEADERS = ("x-ratelimit-reset", "x-rate-limit-reset", "ratelimit-reset")

# A reset written as a number is either seconds from now (a handful) or a unix
# time (a very large number). Nothing sensible sits between the two, so this
# is where we split them: about four months in seconds.
_LOOKS_LIKE_A_UNIX_TIME = 10_000_000.0

# Keys networks put their own explanation under, best first.
_MESSAGE_KEYS = ("error_description", "error_message", "error", "message", "detail")


async def _wait(seconds: float) -> None:
    """Pause before trying again.

    Kept as its own function so tests can watch the pauses instead of
    sitting through them.

    Args:
        seconds: How long to wait.
    """
    await anyio.sleep(seconds)


def _random_fraction() -> float:
    """Return a number from 0 up to just under 1.

    Returns:
        A fraction, used to spread waits out.
    """
    # secrets rather than the plain random module, which this project's
    # lint rules turn away. Either would do for spreading waits.
    return secrets.randbelow(1_000) / 1_000


def read_body(response: httpx.Response) -> RawData:
    """Return a reply's body as a dictionary, whatever shape it arrived in.

    Most networks answer with a JSON object and that is handed straight back.
    Anything else - a JSON list, a plain string, an HTML error page - is put
    under a `body` key, so callers never have to guess what they were given.

    Args:
        response: The reply to read.

    Returns:
        The body as a dictionary.
    """
    try:
        parsed = response.json()
    except ValueError:
        return {"body": response.text}

    if isinstance(parsed, dict):
        return parsed
    return {"body": parsed}


def _what_it_said(body: RawData) -> str:
    """Pull the network's own explanation out of a reply.

    Args:
        body: The reply, already read into a dictionary.

    Returns:
        The explanation, or an empty string when there is not one.
    """
    for key in _MESSAGE_KEYS:
        value = body.get(key)
        if isinstance(value, str) and value:
            return f" It said: {value}"
    return ""


def retry_after_seconds(
    response: httpx.Response,
    *,
    now: datetime | None = None,
) -> float | None:
    """Read how long a network has asked us to wait, in seconds.

    Networks write `Retry-After` two ways: a number of seconds (`"30"`) or a
    date (`"Wed, 21 Oct 2026 07:28:00 GMT"`). Both come back here as seconds
    from now. A date that has already gone by, or a negative number, comes
    back as zero rather than as a wait that runs backwards.

    Args:
        response: The reply to read.
        now: What to treat as the current moment. Only useful in tests.

    Returns:
        Seconds to wait, or `None` when the network did not say or wrote
        something we cannot read.
    """
    header = response.headers.get("retry-after")
    if header is None:
        return None

    text = header.strip()
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        # "inf" reads as a float and would park us forever, so it is turned
        # away with everything else we cannot use.
        if not math.isfinite(seconds):
            return None
        return max(seconds, 0.0)

    try:
        when = parsedate_to_datetime(text)
    except ValueError:
        return None

    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    moment = now if now is not None else datetime.now(UTC)
    return max((when - moment).total_seconds(), 0.0)


@dataclass(frozen=True, slots=True)
class RateLimit:
    """How much of a network's allowance is left, as it last told us.

    Every field may be `None`, which means "the network did not say" - never
    "zero". Read this before sending a burst of requests and you can slow
    down before being told to.

    Attributes:
        limit: How many requests are allowed in the current stretch of time.
        remaining: How many of those are left.
        resets_at: When the count starts again.
    """

    limit: int | None = None
    remaining: int | None = None
    resets_at: datetime | None = None

    @property
    def is_used_up(self) -> bool:
        """Whether there are no requests left before the count starts again."""
        return self.remaining is not None and self.remaining <= 0


def _first_header(headers: httpx.Headers, names: tuple[str, ...]) -> str | None:
    """Return the first of these headers the reply actually carries.

    Args:
        headers: The reply's headers.
        names: Header names to look for, best first.

    Returns:
        The value, or `None` if none of them are there.
    """
    for name in names:
        value: str | None = headers.get(name)
        if value is not None:
            return value
    return None


def _the_one_that_applies_now(value: str) -> str:
    """Return the figure a header is talking about right now.

    Pinterest lists every window it is counting in one header, as
    `"100, 100;w=1, 1000;w=60"`. The bare number in front is the one that
    applies right now and the rest describe the longer windows, so that is
    what is read and the rest is left alone.

    Args:
        value: The header's value.

    Returns:
        The first figure, with the spaces taken off.
    """
    return value.split(",")[0].strip()


def _whole_number(value: str | None) -> int | None:
    """Read a header that should hold a count.

    Args:
        value: The header's value, or `None`.

    Returns:
        The count, or `None` if it is missing or not a whole number.
    """
    if value is None:
        return None
    try:
        return int(_the_one_that_applies_now(value))
    except ValueError:
        return None


def _reset_time(value: str | None, now: datetime | None) -> datetime | None:
    """Work out when a network's count starts again.

    Networks write this three ways: seconds from now (Twitter's `"900"`), a
    unix time (GitHub's `"1793534400"`), or a date (Mastodon's
    `"2026-08-31T12:00:00Z"`). All three end up as a moment in time.

    Args:
        value: The header's value, or `None`.
        now: What to treat as the current moment.

    Returns:
        When the count starts again, or `None` if we cannot tell.
    """
    if value is None:
        return None

    text = value.strip()
    try:
        number = float(_the_one_that_applies_now(text))
    except ValueError:
        pass
    else:
        if number >= _LOOKS_LIKE_A_UNIX_TIME:
            return datetime.fromtimestamp(number, UTC)
        moment = now if now is not None else datetime.now(UTC)
        return moment + timedelta(seconds=number)

    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when


def rate_limit_from_headers(
    headers: httpx.Headers,
    *,
    now: datetime | None = None,
) -> RateLimit | None:
    """Read a reply's rate-limit headers.

    Both spellings are read: `x-ratelimit-limit`, which nearly everybody
    uses, and X's `x-rate-limit-limit`. So is Pinterest's habit of listing
    every window in one header - `"100, 100;w=1, 1000;w=60"` - where the
    bare number in front is the one that applies right now.

    Args:
        headers: The reply's headers.
        now: What to treat as the current moment, for networks that count
            down in seconds. Only useful in tests.

    Returns:
        What the network said, or `None` if it said nothing we recognise.
    """
    limit = _whole_number(_first_header(headers, _LIMIT_HEADERS))
    remaining = _whole_number(_first_header(headers, _REMAINING_HEADERS))
    resets_at = _reset_time(_first_header(headers, _RESET_HEADERS), now)

    if limit is None and remaining is None and resets_at is None:
        return None
    return RateLimit(limit=limit, remaining=remaining, resets_at=resets_at)


def error_from_response(
    response: httpx.Response,
    *,
    platform: str = _UNNAMED,
) -> SocialChimpError:
    """Turn an unhappy reply into the socialchimp error that describes it.

    This is the shared mapping every network starts from. A platform that
    wants to name its own quirks writes its own function, handles the replies
    it recognises, and calls this one for the rest:

        def bluesky_errors(response: httpx.Response) -> SocialChimpError:
            body = read_body(response)
            if body.get("error") == "TextTooLong":
                return InvalidPostError("This post is too long for bluesky.")
            return error_from_response(response, platform="bluesky")

    Args:
        response: The reply to turn into an error.
        platform: Which network sent it, used in the message.

    Returns:
        The error to raise. Always an error, never `None`, so a caller
        cannot forget a case.
    """
    body = read_body(response)
    said = _what_it_said(body)
    status = response.status_code

    if status == httpx.codes.UNAUTHORIZED:
        return AuthError(
            f"{platform} would not accept our sign-in (401). The person may "
            f"need to connect their account again.{said}"
        )
    if status == httpx.codes.FORBIDDEN:
        return NotAllowedError(
            f"{platform} will not let this account do that (403). It is "
            f"usually a permission that was never asked for.{said}"
        )
    if status == httpx.codes.NOT_FOUND:
        return NotFoundError(
            f"{platform} has no such post, account or page (404).{said}"
        )
    if status == httpx.codes.TOO_MANY_REQUESTS:
        return RateLimitError(
            f"{platform} is asking us to slow down (429).{said}",
            retry_after=retry_after_seconds(response),
        )

    if status >= httpx.codes.INTERNAL_SERVER_ERROR:
        message = (
            f"{platform} had trouble of its own ({status}). Trying again in a "
            f"little while usually works.{said}"
        )
    elif status >= httpx.codes.BAD_REQUEST:
        message = f"{platform} refused this request ({status}).{said}"
    else:
        # Nothing should map a good reply, but a function that can return
        # nothing is a trap for whoever does it by accident.
        message = f"{platform} sent a reply we did not expect ({status}).{said}"

    return PlatformError(
        message,
        platform=platform,
        status_code=status,
        raw=body,
    )


def _worth_another_try(response: httpx.Response) -> bool:
    """Say whether a reply is the sort that often works on a second try.

    Args:
        response: The reply to judge.

    Returns:
        True for "slow down" and for the network's own failures. A refusal
        aimed at this request, such as a missing page, is never tried again -
        it would only waste the allowance.
    """
    return (
        response.status_code == httpx.codes.TOO_MANY_REQUESTS
        or response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR
    )


@dataclass(frozen=True, slots=True)
class Retries:
    """How many times to try again, and how long to wait in between.

    Waits double after each failure, so a network that is struggling is not
    hammered. Part of each wait is random, so that every client which failed
    at the same moment does not come back at the same moment.

    Attributes:
        attempts: How many tries in total, counting the first one.
        first_wait: Seconds to wait after the first failure.
        biggest_wait: The longest we will ever wait between two tries.
        spread: How much of a wait is random, from 0 (never) to 1 (up to
            double the wait).
    """

    attempts: int = 4
    first_wait: float = 0.5
    biggest_wait: float = 30.0
    spread: float = 0.5

    def __post_init__(self) -> None:
        """Refuse settings that could never work.

        Raises:
            ConfigError: If there is not at least one try, or the spread is
                not between 0 and 1.
        """
        if self.attempts < 1:
            message = (
                f"attempts is {self.attempts}, but a request needs at least "
                f"one try. Use attempts=1 to send it once and not try again."
            )
            raise ConfigError(message)
        if not 0.0 <= self.spread <= 1.0:
            message = f"spread is {self.spread}, but it has to be between 0 and 1."
            raise ConfigError(message)

    def wait_after(self, failures: int, asked_for: float | None = None) -> float:
        """Work out how long to wait before the next try.

        Args:
            failures: How many tries have failed so far.
            asked_for: Seconds the network itself asked us to wait, when
                it said. This is the least we will wait; if the wait we
                worked out is longer, we take the longer one.

        Returns:
            Seconds to wait.
        """
        wait = min(self.first_wait * 2.0 ** (failures - 1), self.biggest_wait)
        wait += wait * self.spread * _random_fraction()
        if asked_for is not None:
            return max(wait, asked_for)
        return wait


Item = TypeVar("Item")
Page = TypeVar("Page")
Marker = TypeVar("Marker")


async def paginate(
    fetch_page: Callable[[Marker | None], Awaitable[Page]],
    extract_items: Callable[[Page], Iterable[Item]],
    extract_next: Callable[[Page], Marker | None],
    *,
    max_pages: int | None = None,
) -> AsyncIterator[Item]:
    """Read a network's results page by page, handing back one item at a time.

    Networks disagree about paging. Some hand out a cursor, some a page
    token, some a `Link` header. Write three small functions that know which
    of those this network uses and the difference stops mattering to anyone
    reading the items.

    Args:
        fetch_page: Fetch one page. It is given `None` for the first page,
            then whatever `extract_next` returned for the page before.
        extract_items: Pull the items out of a page.
        extract_next: Pull out the marker for the page after this one, or
            `None` when this was the last page.
        max_pages: Stop after this many pages. `None` keeps going until the
            network says there are no more.

    Yields:
        Every item on every page, in the order the network gave them.

    Example:
        async def fetch(cursor: str | None) -> RawData:
            params = {"cursor": cursor} if cursor else {}
            return await http.json("GET", "/statuses", params=params)

        async for status in paginate(
            fetch,
            lambda page: page["data"],
            lambda page: page["cursor"],
        ):
            print(status)
    """
    marker: Marker | None = None
    pages_read = 0

    while max_pages is None or pages_read < max_pages:
        page = await fetch_page(marker)
        pages_read += 1

        for item in extract_items(page):
            yield item

        following = extract_next(page)
        # A network that hands back the marker it was just given would keep
        # us asking for the same page for as long as we let it.
        if following is None or following == marker:
            return
        marker = following


class HttpClient:
    """The shared way to send requests to one network.

    Wraps an `httpx.AsyncClient` and adds the parts every platform needs:
    trying again after a hiccup, waiting as long as the network asks,
    remembering how much of the allowance is left, and raising a socialchimp
    error instead of handing back a reply nobody checked.

    A failed request is sent again with exactly the arguments you gave, so
    pass bytes rather than an open file - a file read once cannot be read
    again.

    Example:
        async with HttpClient(
            "https://mastodon.social",
            platform="mastodon",
        ) as http:
            me = await http.json("GET", "/api/v1/accounts/verify_credentials")
    """

    def __init__(
        self,
        base_url: str = "",
        *,
        platform: str = _UNNAMED,
        headers: Mapping[str, str] | None = None,
        timeout: float | httpx.Timeout = _DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
        retries: Retries | None = None,
        errors: Callable[[httpx.Response], SocialChimpError] | None = None,
    ) -> None:
        """Set up a client for one network.

        Args:
            base_url: What every path is joined onto, such as
                `"https://mastodon.social"`.
            platform: Which network this talks to, used in error messages.
            headers: Sent with every request. A token usually goes here.
            timeout: Seconds to wait for a reply before giving up on it.
            transport: Where requests actually go. Leave it out for ordinary
                network calls; pass your own to send them through something
                else, which is also how tests answer without a network.
            retries: How many times to try again, and how long to wait in
                between. Left out, four tries with growing waits.
            errors: Your own function turning an unhappy reply into an
                error, for a network with quirks worth naming. Left out,
                `error_from_response` is used.
        """
        self.platform = platform
        self.retries = retries if retries is not None else Retries()
        self._errors = errors
        self._rate_limit: RateLimit | None = None
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=dict(headers) if headers is not None else None,
            timeout=timeout,
            transport=transport,
        )

    @property
    def rate_limit(self) -> RateLimit | None:
        """What the network last said about how much allowance is left.

        `None` until a reply mentions it. A later reply that says nothing
        leaves the last figures alone, because "no headers" means "no news",
        not "nothing left".
        """
        return self._rate_limit

    @property
    def is_closed(self) -> bool:
        """Whether this client has been closed and cannot send any more."""
        return self._client.is_closed

    def _error_for(self, response: httpx.Response) -> SocialChimpError:
        """Turn an unhappy reply into an error.

        Args:
            response: The reply to turn into an error.

        Returns:
            The error to raise, from your own function if you gave one.
        """
        if self._errors is not None:
            return self._errors(response)
        return error_from_response(response, platform=self.platform)

    async def request(
        self,
        method: str,
        path: str,
        **kwargs: object,
    ) -> httpx.Response:
        """Send a request, trying again if it is worth it.

        Args:
            method: `"GET"`, `"POST"` and so on.
            path: Joined onto the base url.
            **kwargs: Anything `httpx.AsyncClient.request` takes, such as
                `params`, `json`, `content`, `files` or extra `headers`.

        Returns:
            The reply, which is always one the network was happy with.

        Raises:
            SocialChimpError: If the network refused, or could not be
                reached at all. Which error depends on what it said; see
                `error_from_response`.
        """
        # httpx already names every option it takes - params, json, files
        # and the rest - so we hand them straight on rather than writing that
        # list out a second time here.
        options = cast("dict[str, Any]", kwargs)

        failures = 0
        while True:
            try:
                response = await self._client.request(method, path, **options)
            except httpx.TransportError as problem:
                failures += 1
                if failures >= self.retries.attempts:
                    message = (
                        f"Could not reach {self.platform} after "
                        f"{failures} tries: {problem}"
                    )
                    raise PlatformError(
                        message,
                        platform=self.platform,
                    ) from problem
                await _wait(self.retries.wait_after(failures))
                continue

            seen = rate_limit_from_headers(response.headers)
            if seen is not None:
                self._rate_limit = seen

            failures += 1
            if failures < self.retries.attempts and _worth_another_try(response):
                await _wait(
                    self.retries.wait_after(failures, retry_after_seconds(response))
                )
                continue

            if response.is_error:
                raise self._error_for(response)
            return response

    async def get(self, path: str, **kwargs: object) -> httpx.Response:
        """Send a GET request.

        Args:
            path: Joined onto the base url.
            **kwargs: Anything `request` takes.

        Returns:
            The reply.
        """
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: object) -> httpx.Response:
        """Send a POST request.

        Args:
            path: Joined onto the base url.
            **kwargs: Anything `request` takes.

        Returns:
            The reply.
        """
        return await self.request("POST", path, **kwargs)

    async def put(self, path: str, **kwargs: object) -> httpx.Response:
        """Send a PUT request.

        Args:
            path: Joined onto the base url.
            **kwargs: Anything `request` takes.

        Returns:
            The reply.
        """
        return await self.request("PUT", path, **kwargs)

    async def delete(self, path: str, **kwargs: object) -> httpx.Response:
        """Send a DELETE request.

        Args:
            path: Joined onto the base url.
            **kwargs: Anything `request` takes.

        Returns:
            The reply.
        """
        return await self.request("DELETE", path, **kwargs)

    async def json(self, method: str, path: str, **kwargs: object) -> RawData:
        """Send a request and read the reply as a JSON object.

        Args:
            method: `"GET"`, `"POST"` and so on.
            path: Joined onto the base url.
            **kwargs: Anything `request` takes.

        Returns:
            The reply, parsed.

        Raises:
            PlatformError: If the reply was not JSON, or was JSON but not an
                object. The reply itself is kept on `raw`.
            SocialChimpError: If the network refused the request.
        """
        response = await self.request(method, path, **kwargs)
        try:
            parsed = response.json()
        except ValueError as problem:
            message = (
                f"{self.platform} was expected to answer with JSON but sent "
                f"something else. It starts: {response.text[:200]!r}"
            )
            raise PlatformError(
                message,
                platform=self.platform,
                status_code=response.status_code,
                raw={"body": response.text},
            ) from problem

        if not isinstance(parsed, dict):
            message = (
                f"{self.platform} answered with JSON, but with a "
                f"{type(parsed).__name__} where an object was expected."
            )
            raise PlatformError(
                message,
                platform=self.platform,
                status_code=response.status_code,
                raw={"body": parsed},
            )
        return parsed

    async def aclose(self) -> None:
        """Close the connections this client is holding open."""
        await self._client.aclose()

    async def __aenter__(self) -> HttpClient:
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
        """Close the client when the block ends.

        Args:
            exc_type: The kind of error that ended the block, if any.
            exc: The error that ended the block, if any.
            traceback: Where that error came from, if any.
        """
        await self.aclose()
