"""Tests for the shared way socialchimp talks to a network over HTTP."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest
import respx

from socialchimp import (
    AuthError,
    ConfigError,
    InvalidPostError,
    NotAllowedError,
    NotFoundError,
    PlatformError,
    RateLimitError,
    SocialChimpError,
)
from socialchimp import http as http_module
from socialchimp.http import (
    HttpClient,
    RateLimit,
    Retries,
    error_from_response,
    paginate,
    rate_limit_from_headers,
    read_body,
    retry_after_seconds,
)

BASE = "https://api.test"

# Tries again straight away, so the retry tests do not spend real seconds
# asleep.
FAST = Retries(attempts=3, first_wait=0.0, spread=0.0)

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every pause instead of taking it."""
    recorded: list[float] = []

    async def remember(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(http_module, "_wait", remember)
    return recorded


class TestSendingRequests:
    async def test_it_sends_to_the_base_url_with_the_headers_you_gave(self) -> None:
        with respx.mock(base_url=BASE) as network:
            route = network.get("/accounts/me").mock(
                return_value=httpx.Response(200, json={"id": "1"})
            )

            async with HttpClient(
                BASE,
                platform="mastodon",
                headers={"Authorization": "Bearer secret"},
            ) as http:
                await http.get("/accounts/me")

        sent = route.calls.last.request
        assert str(sent.url) == f"{BASE}/accounts/me"
        assert sent.headers["authorization"] == "Bearer secret"

    @pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
    async def test_the_short_names_send_the_method_they_are_named_after(
        self,
        method: str,
    ) -> None:
        with respx.mock(base_url=BASE) as network:
            route = network.request(method, "/thing").mock(
                return_value=httpx.Response(200, json={})
            )

            async with HttpClient(BASE) as http:
                shortcut = getattr(http, method.lower())
                await shortcut("/thing")

        assert route.calls.last.request.method == method

    async def test_a_body_you_pass_reaches_the_network(self) -> None:
        with respx.mock(base_url=BASE) as network:
            route = network.post("/statuses").mock(
                return_value=httpx.Response(200, json={"id": "9"})
            )

            async with HttpClient(BASE) as http:
                await http.post("/statuses", json={"status": "hello"})

        assert route.calls.last.request.read() == b'{"status":"hello"}'

    async def test_json_hands_back_the_parsed_reply(self) -> None:
        with respx.mock(base_url=BASE) as network:
            network.get("/me").mock(return_value=httpx.Response(200, json={"id": "1"}))

            async with HttpClient(BASE) as http:
                body = await http.json("GET", "/me")

        assert body == {"id": "1"}

    async def test_json_complains_when_the_reply_is_not_json_at_all(self) -> None:
        # A network that returns an HTML error page should not turn into a
        # confusing JSONDecodeError three layers up.
        with respx.mock(base_url=BASE) as network:
            network.get("/me").mock(return_value=httpx.Response(200, text="<html>"))

            async with HttpClient(BASE, platform="mastodon") as http:
                with pytest.raises(PlatformError) as caught:
                    await http.json("GET", "/me")

        assert "mastodon" in str(caught.value)
        assert "JSON" in str(caught.value)
        assert caught.value.raw == {"body": "<html>"}

    async def test_json_complains_when_the_reply_is_json_but_not_an_object(
        self,
    ) -> None:
        with respx.mock(base_url=BASE) as network:
            network.get("/me").mock(return_value=httpx.Response(200, json=[1, 2]))

            async with HttpClient(BASE, platform="mastodon") as http:
                with pytest.raises(PlatformError) as caught:
                    await http.json("GET", "/me")

        assert caught.value.raw == {"body": [1, 2]}

    async def test_leaving_the_block_closes_the_client(self) -> None:
        async with HttpClient(BASE) as http:
            pass

        assert http.is_closed

    async def test_aclose_closes_the_client(self) -> None:
        http = HttpClient(BASE)

        await http.aclose()

        assert http.is_closed


class TestTryingAgain:
    async def test_a_server_error_is_tried_again_and_then_works(self) -> None:
        with respx.mock(base_url=BASE) as network:
            route = network.get("/me").mock(
                side_effect=[
                    httpx.Response(503),
                    httpx.Response(200, json={"id": "1"}),
                ]
            )

            async with HttpClient(BASE, retries=FAST) as http:
                body = await http.json("GET", "/me")

        assert body == {"id": "1"}
        assert route.call_count == 2

    async def test_a_broken_connection_is_tried_again(self) -> None:
        with respx.mock(base_url=BASE) as network:
            route = network.get("/me").mock(
                side_effect=[
                    httpx.ConnectError("no route to host"),
                    httpx.Response(200, json={"id": "1"}),
                ]
            )

            async with HttpClient(BASE, retries=FAST) as http:
                await http.get("/me")

        assert route.call_count == 2

    async def test_being_asked_to_slow_down_is_tried_again(self) -> None:
        with respx.mock(base_url=BASE) as network:
            route = network.get("/me").mock(
                side_effect=[
                    httpx.Response(429),
                    httpx.Response(200, json={"id": "1"}),
                ]
            )

            async with HttpClient(BASE, retries=FAST) as http:
                await http.get("/me")

        assert route.call_count == 2

    async def test_it_gives_up_after_the_number_of_tries_you_set(self) -> None:
        with respx.mock(base_url=BASE) as network:
            route = network.get("/me").mock(return_value=httpx.Response(500))

            async with HttpClient(BASE, platform="mastodon", retries=FAST) as http:
                with pytest.raises(PlatformError) as caught:
                    await http.get("/me")

        assert route.call_count == 3
        assert caught.value.status_code == 500

    async def test_a_missing_page_is_not_tried_again(self) -> None:
        # Asking a second time for something that does not exist only wastes
        # the caller's rate limit.
        with respx.mock(base_url=BASE) as network:
            route = network.get("/me").mock(return_value=httpx.Response(404))

            async with HttpClient(BASE, retries=FAST) as http:
                with pytest.raises(NotFoundError):
                    await http.get("/me")

        assert route.call_count == 1

    async def test_a_connection_that_never_recovers_is_reported_plainly(self) -> None:
        with respx.mock(base_url=BASE) as network:
            route = network.get("/me").mock(side_effect=httpx.ConnectError("down"))

            async with HttpClient(BASE, platform="mastodon", retries=FAST) as http:
                with pytest.raises(PlatformError) as caught:
                    await http.get("/me")

        assert route.call_count == 3
        assert "mastodon" in str(caught.value)
        assert caught.value.status_code is None
        assert isinstance(caught.value.__cause__, httpx.ConnectError)

    async def test_a_request_that_never_answers_is_tried_again(self) -> None:
        with respx.mock(base_url=BASE) as network:
            route = network.get("/me").mock(
                side_effect=[
                    httpx.ReadTimeout("took too long"),
                    httpx.Response(200, json={"id": "1"}),
                ]
            )

            async with HttpClient(BASE, retries=FAST) as http:
                await http.get("/me")

        assert route.call_count == 2

    async def test_the_wait_grows_after_each_failure(self, waits: list[float]) -> None:
        patient = Retries(attempts=4, first_wait=1.0, spread=0.0)
        with respx.mock(base_url=BASE) as network:
            network.get("/me").mock(return_value=httpx.Response(500))

            async with HttpClient(BASE, retries=patient) as http:
                with pytest.raises(PlatformError):
                    await http.get("/me")

        assert waits == [1.0, 2.0, 4.0]

    async def test_a_wait_the_network_asks_for_is_taken_instead(
        self,
        waits: list[float],
    ) -> None:
        with respx.mock(base_url=BASE) as network:
            network.get("/me").mock(
                side_effect=[
                    httpx.Response(429, headers={"Retry-After": "7"}),
                    httpx.Response(200, json={}),
                ]
            )

            async with HttpClient(BASE, retries=FAST) as http:
                await http.get("/me")

        assert waits == [7.0]

    async def test_a_wait_asked_for_as_a_date_is_taken_too(
        self,
        waits: list[float],
    ) -> None:
        # An hour out rather than twenty seconds, on purpose. The header
        # carries whole seconds, and real time passes between building it and
        # the wait being worked out, so a twenty second window is only about
        # ninety-five per cent reliable on a loaded machine - which is a
        # failing build for no reason. Against an hour, both are noise.
        soon = format_datetime(datetime.now(UTC) + timedelta(hours=1), usegmt=True)
        with respx.mock(base_url=BASE) as network:
            network.get("/me").mock(
                side_effect=[
                    httpx.Response(503, headers={"Retry-After": soon}),
                    httpx.Response(200, json={}),
                ]
            )

            async with HttpClient(BASE, retries=FAST) as http:
                await http.get("/me")

        # Just under the hour we asked for: the header rounds down to whole
        # seconds, and a moment passes before the wait is worked out.
        assert 3590.0 <= waits[0] <= 3600.0

    async def test_the_wait_asked_for_is_a_floor_not_a_ceiling(
        self,
        waits: list[float],
    ) -> None:
        # A network asking for one second does not shorten a back-off we
        # already decided should be longer.
        patient = Retries(attempts=2, first_wait=10.0, spread=0.0)
        with respx.mock(base_url=BASE) as network:
            network.get("/me").mock(
                side_effect=[
                    httpx.Response(429, headers={"Retry-After": "1"}),
                    httpx.Response(200, json={}),
                ]
            )

            async with HttpClient(BASE, retries=patient) as http:
                await http.get("/me")

        assert waits == [10.0]

    async def test_the_spread_keeps_waits_inside_their_window(self) -> None:
        # Every client that failed at the same moment must not come back at
        # the same moment, so part of each wait is random.
        retries = Retries(first_wait=2.0, spread=0.5)

        seen = {retries.wait_after(failures=1) for _ in range(50)}

        assert all(2.0 <= wait <= 3.0 for wait in seen)
        assert len(seen) > 1


class TestRetries:
    async def test_asking_for_no_tries_at_all_is_a_setup_mistake(self) -> None:
        with pytest.raises(ConfigError, match="at least one"):
            Retries(attempts=0)

    async def test_a_spread_outside_nought_to_one_is_a_setup_mistake(self) -> None:
        with pytest.raises(ConfigError, match="spread"):
            Retries(spread=1.5)

    async def test_the_wait_never_grows_past_the_biggest_wait(self) -> None:
        retries = Retries(first_wait=1.0, spread=0.0, biggest_wait=3.0)

        assert retries.wait_after(failures=9) == 3.0

    async def test_four_tries_is_the_default(self) -> None:
        assert Retries().attempts == 4


class TestRetryAfter:
    def test_a_number_of_seconds(self) -> None:
        response = httpx.Response(429, headers={"Retry-After": "30"})

        assert retry_after_seconds(response) == 30.0

    def test_a_date_is_turned_into_seconds_from_now(self) -> None:
        later = format_datetime(NOW + timedelta(seconds=45), usegmt=True)
        response = httpx.Response(429, headers={"Retry-After": later})

        assert retry_after_seconds(response, now=NOW) == 45.0

    def test_a_date_with_no_timezone_is_read_as_utc(self) -> None:
        response = httpx.Response(
            429, headers={"Retry-After": "Mon, 31 Aug 2026 12:01:00"}
        )

        assert retry_after_seconds(response, now=NOW) == 60.0

    def test_a_date_already_gone_means_no_wait(self) -> None:
        past = format_datetime(NOW - timedelta(hours=1), usegmt=True)
        response = httpx.Response(429, headers={"Retry-After": past})

        assert retry_after_seconds(response, now=NOW) == 0.0

    def test_a_negative_number_means_no_wait(self) -> None:
        response = httpx.Response(429, headers={"Retry-After": "-5"})

        assert retry_after_seconds(response) == 0.0

    def test_a_value_we_cannot_read_is_ignored(self) -> None:
        response = httpx.Response(429, headers={"Retry-After": "in a bit"})

        assert retry_after_seconds(response) is None

    def test_a_wait_with_no_end_is_ignored(self) -> None:
        # "inf" reads as a float, so it has to be turned away on purpose.
        response = httpx.Response(429, headers={"Retry-After": "inf"})

        assert retry_after_seconds(response) is None

    def test_no_header_means_the_network_did_not_say(self) -> None:
        assert retry_after_seconds(httpx.Response(429)) is None


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, AuthError),
            (403, NotAllowedError),
            (404, NotFoundError),
            (429, RateLimitError),
            (400, PlatformError),
            (418, PlatformError),
            (500, PlatformError),
            (503, PlatformError),
        ],
    )
    def test_each_status_becomes_the_error_that_describes_it(
        self,
        status: int,
        expected: type[SocialChimpError],
    ) -> None:
        error = error_from_response(httpx.Response(status), platform="mastodon")

        assert isinstance(error, expected)
        assert "mastodon" in str(error)

    def test_a_reply_we_did_not_expect_still_becomes_an_error(self) -> None:
        # Nothing should ever call this with a good reply, but a function that
        # can return nothing is a trap for whoever does.
        error = error_from_response(httpx.Response(200), platform="mastodon")

        assert isinstance(error, PlatformError)

    def test_the_networks_own_reply_is_kept_for_you_to_look_at(self) -> None:
        response = httpx.Response(500, json={"error": "boom", "id": "abc"})

        error = error_from_response(response, platform="mastodon")

        assert isinstance(error, PlatformError)
        assert error.platform == "mastodon"
        assert error.status_code == 500
        assert error.raw == {"error": "boom", "id": "abc"}

    def test_what_the_network_said_is_repeated_in_the_message(self) -> None:
        response = httpx.Response(403, json={"error_description": "no write scope"})

        error = error_from_response(response, platform="mastodon")

        assert "no write scope" in str(error)

    def test_a_reply_that_is_not_json_is_still_kept(self) -> None:
        response = httpx.Response(500, text="Bad Gateway")

        error = error_from_response(response, platform="mastodon")

        assert isinstance(error, PlatformError)
        assert error.raw == {"body": "Bad Gateway"}

    def test_a_reply_that_is_a_json_list_is_still_kept(self) -> None:
        response = httpx.Response(400, json=["too long"])

        error = error_from_response(response, platform="bluesky")

        assert isinstance(error, PlatformError)
        assert error.raw == {"body": ["too long"]}

    def test_being_asked_to_slow_down_carries_how_long_to_wait(self) -> None:
        response = httpx.Response(429, headers={"Retry-After": "12"})

        error = error_from_response(response, platform="mastodon")

        assert isinstance(error, RateLimitError)
        assert error.retry_after == 12.0

    def test_the_network_is_named_even_when_you_do_not_say_which(self) -> None:
        error = error_from_response(httpx.Response(404))

        assert "the network" in str(error)

    async def test_the_client_raises_the_matching_error(self) -> None:
        with respx.mock(base_url=BASE) as network:
            network.get("/me").mock(return_value=httpx.Response(401))

            async with HttpClient(BASE, platform="mastodon", retries=FAST) as http:
                with pytest.raises(AuthError):
                    await http.get("/me")

    async def test_a_platform_can_name_its_own_quirks(self) -> None:
        # Bluesky answers "text too long" with a plain 400, so its own error
        # function says what really happened and hands the rest back to the
        # shared mapping.
        def bluesky_errors(response: httpx.Response) -> SocialChimpError:
            body = read_body(response)
            if body.get("error") == "TextTooLong":
                return InvalidPostError("This post is too long for bluesky.")
            return error_from_response(response, platform="bluesky")

        with respx.mock(base_url=BASE) as network:
            network.post("/post").mock(
                side_effect=[
                    httpx.Response(400, json={"error": "TextTooLong"}),
                    httpx.Response(404),
                ]
            )

            async with HttpClient(BASE, retries=FAST, errors=bluesky_errors) as http:
                with pytest.raises(InvalidPostError, match="too long"):
                    await http.post("/post")
                with pytest.raises(NotFoundError):
                    await http.post("/post")


class TestRateLimitHeaders:
    async def test_the_client_remembers_what_the_last_reply_said(self) -> None:
        with respx.mock(base_url=BASE) as network:
            network.get("/me").mock(
                return_value=httpx.Response(
                    200,
                    json={},
                    headers={
                        "X-RateLimit-Limit": "300",
                        "X-RateLimit-Remaining": "299",
                    },
                )
            )

            async with HttpClient(BASE) as http:
                assert http.rate_limit is None

                await http.get("/me")

                assert http.rate_limit == RateLimit(limit=300, remaining=299)

    async def test_a_later_reply_that_says_nothing_leaves_the_figures_alone(
        self,
    ) -> None:
        with respx.mock(base_url=BASE) as network:
            network.get("/me").mock(
                side_effect=[
                    httpx.Response(
                        200, json={}, headers={"X-RateLimit-Remaining": "5"}
                    ),
                    httpx.Response(200, json={}),
                ]
            )

            async with HttpClient(BASE) as http:
                await http.get("/me")
                await http.get("/me")

                assert http.rate_limit == RateLimit(remaining=5)

    def test_lowercase_headers_are_read_too(self) -> None:
        headers = httpx.Headers({"x-ratelimit-remaining": "7"})

        assert rate_limit_from_headers(headers) == RateLimit(remaining=7)

    def test_headers_without_the_x_are_read_too(self) -> None:
        headers = httpx.Headers({"RateLimit-Remaining": "7"})

        assert rate_limit_from_headers(headers) == RateLimit(remaining=7)

    def test_x_writes_the_names_with_one_more_hyphen(self) -> None:
        # X spells these `x-rate-limit-*` where everybody else spells them
        # `x-ratelimit-*`. Missing that hyphen means never knowing how much
        # of X's allowance is left, and nothing anywhere says so.
        headers = httpx.Headers(
            {
                "x-rate-limit-limit": "300",
                "x-rate-limit-remaining": "299",
                "x-rate-limit-reset": str(int(NOW.timestamp())),
            }
        )

        assert rate_limit_from_headers(headers) == RateLimit(
            limit=300, remaining=299, resets_at=NOW
        )

    def test_a_header_listing_every_window_reads_the_first_number(self) -> None:
        # Pinterest lists every window it counts in one header. The bare
        # number in front is the one that applies right now.
        headers = httpx.Headers(
            {
                "x-ratelimit-limit": "100, 100;w=1, 1000;w=60",
                "x-ratelimit-remaining": "99, 99;w=1, 999;w=60",
            }
        )

        assert rate_limit_from_headers(headers) == RateLimit(limit=100, remaining=99)

    def test_a_reset_listing_every_window_reads_the_first_number(self) -> None:
        headers = httpx.Headers({"x-ratelimit-reset": "60, 3600;w=60"})

        limit = rate_limit_from_headers(headers, now=NOW)

        assert limit is not None
        assert limit.resets_at == NOW + timedelta(seconds=60)

    def test_a_reset_given_as_a_unix_time(self) -> None:
        headers = httpx.Headers({"X-RateLimit-Reset": str(int(NOW.timestamp()))})

        limit = rate_limit_from_headers(headers)

        assert limit is not None
        assert limit.resets_at == NOW

    def test_a_reset_given_as_seconds_from_now(self) -> None:
        headers = httpx.Headers({"X-RateLimit-Reset": "60"})

        limit = rate_limit_from_headers(headers, now=NOW)

        assert limit is not None
        assert limit.resets_at == NOW + timedelta(seconds=60)

    def test_a_reset_given_as_a_date(self) -> None:
        # Mastodon writes this one as a date rather than a number.
        headers = httpx.Headers({"X-RateLimit-Reset": "2026-08-31T12:00:00Z"})

        limit = rate_limit_from_headers(headers)

        assert limit is not None
        assert limit.resets_at == NOW

    def test_a_date_with_no_timezone_is_read_as_utc(self) -> None:
        headers = httpx.Headers({"X-RateLimit-Reset": "2026-08-31T12:00:00"})

        limit = rate_limit_from_headers(headers)

        assert limit is not None
        assert limit.resets_at == NOW

    def test_a_reset_we_cannot_read_is_left_out(self) -> None:
        headers = httpx.Headers(
            {"X-RateLimit-Remaining": "1", "X-RateLimit-Reset": "tomorrow"}
        )

        limit = rate_limit_from_headers(headers)

        assert limit is not None
        assert limit.resets_at is None

    def test_a_count_we_cannot_read_is_left_out(self) -> None:
        headers = httpx.Headers({"X-RateLimit-Remaining": "lots"})

        assert rate_limit_from_headers(headers) is None

    def test_a_reply_that_says_nothing_about_limits(self) -> None:
        assert rate_limit_from_headers(httpx.Headers()) is None

    def test_it_says_when_the_allowance_is_used_up(self) -> None:
        assert RateLimit(remaining=0).is_used_up is True
        assert RateLimit(remaining=4).is_used_up is False
        assert RateLimit().is_used_up is False


class TestPaging:
    async def test_it_walks_every_page_and_hands_back_each_item(self) -> None:
        pages: dict[str | None, dict[str, Any]] = {
            None: {"items": [1, 2], "next": "b"},
            "b": {"items": [3], "next": "c"},
            "c": {"items": [4, 5], "next": None},
        }
        asked: list[str | None] = []

        async def fetch(marker: str | None) -> dict[str, Any]:
            asked.append(marker)
            return pages[marker]

        found = [
            item
            async for item in paginate(
                fetch,
                lambda page: list(page["items"]),
                lambda page: page["next"],
            )
        ]

        assert found == [1, 2, 3, 4, 5]
        assert asked == [None, "b", "c"]

    async def test_it_stops_when_the_network_repeats_the_same_marker(self) -> None:
        # A network that keeps handing back the marker it was just given
        # would otherwise keep us asking forever.
        calls: list[str | None] = []

        async def fetch(marker: str | None) -> dict[str, Any]:
            calls.append(marker)
            return {"items": ["x"], "next": "same"}

        found = [
            item
            async for item in paginate(
                fetch,
                lambda page: list(page["items"]),
                lambda page: page["next"],
            )
        ]

        assert found == ["x", "x"]
        assert calls == [None, "same"]

    async def test_it_stops_after_the_number_of_pages_you_allow(self) -> None:
        async def fetch(marker: int | None) -> dict[str, Any]:
            start = marker or 0
            return {"items": [start], "next": start + 1}

        found = [
            item
            async for item in paginate(
                fetch,
                lambda page: list(page["items"]),
                lambda page: page["next"],
                max_pages=2,
            )
        ]

        assert found == [0, 1]

    async def test_a_page_with_no_items_is_no_reason_to_stop(self) -> None:
        pages: dict[str | None, dict[str, Any]] = {
            None: {"items": [], "next": "b"},
            "b": {"items": ["last"], "next": None},
        }

        async def fetch(marker: str | None) -> dict[str, Any]:
            return pages[marker]

        found = [
            item
            async for item in paginate(
                fetch,
                lambda page: list(page["items"]),
                lambda page: page["next"],
            )
        ]

        assert found == ["last"]

    async def test_it_reads_real_pages_over_http(self) -> None:
        with respx.mock(base_url=BASE) as network:
            route = network.get("/statuses").mock(
                side_effect=[
                    httpx.Response(200, json={"data": ["a"], "cursor": "2"}),
                    httpx.Response(200, json={"data": ["b"], "cursor": None}),
                ]
            )

            async with HttpClient(BASE) as http:

                async def fetch(marker: str | None) -> dict[str, Any]:
                    params = {"cursor": marker} if marker else {}
                    return await http.json("GET", "/statuses", params=params)

                found = [
                    item
                    async for item in paginate(
                        fetch,
                        lambda page: list(page["data"]),
                        lambda page: page["cursor"],
                    )
                ]

        assert found == ["a", "b"]
        assert [str(call.request.url) for call in route.calls] == [
            f"{BASE}/statuses",
            f"{BASE}/statuses?cursor=2",
        ]


class TestSendingRequestsYourOwnWay:
    async def test_a_transport_you_pass_in_is_the_one_used(self) -> None:
        seen: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"ok": True})

        async with HttpClient(BASE, transport=httpx.MockTransport(handle)) as http:
            body = await http.json("GET", "/whoami")

        assert body == {"ok": True}
        assert [str(request.url) for request in seen] == [f"{BASE}/whoami"]

    async def test_the_timeout_you_set_is_sent_with_the_request(self) -> None:
        seen: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={})

        async with HttpClient(
            BASE,
            timeout=2.5,
            transport=httpx.MockTransport(handle),
        ) as http:
            await http.get("/whoami")

        assert seen[0].extensions["timeout"] == {
            "connect": 2.5,
            "pool": 2.5,
            "read": 2.5,
            "write": 2.5,
        }
