"""Tests for the pieces Facebook, Instagram and Threads share."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from socialchimp import (
    AuthError,
    InvalidPostError,
    NotAllowedError,
    NotFoundError,
    NotSupportedError,
    PlatformError,
    RateLimitError,
    SignatureError,
)
from socialchimp.http import HttpClient, Retries
from socialchimp.platforms import _meta
from socialchimp.platforms._meta import (
    DEVELOPER_PORTAL,
    GRAPH_API,
    GRAPH_VERSION,
    SIGN_IN_PAGE,
    Graph,
    Usage,
    app_must_be_made_by_hand,
    changes_in,
    check_meta_signature,
    long_lived_token,
    meta_errors,
    page_by_id,
    pages_of,
    required_text,
    sign_in_url,
    swap_code_for_token,
    token_from,
    usage_from_headers,
)

PLATFORM = "facebook"
APP_ID = "1234567890"
APP_SECRET = "app-secret"
PAGE_ID = "111222333"
PAGE_TOKEN = "page-token"
USER_TOKEN = "user-token"

# One try and no waiting, so the error tests do not spend real seconds asleep.
ONCE = Retries(attempts=1)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Freeze the moment a token's expiry is measured from."""
    monkeypatch.setattr(_meta, "_now", lambda: NOW)
    return NOW


def a_graph(token: str | None = None) -> Graph:
    """A conversation with Meta that gives up after one try."""
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return Graph(
        HttpClient(
            GRAPH_API,
            platform=PLATFORM,
            headers=headers,
            retries=ONCE,
            errors=lambda reply: meta_errors(reply, platform=PLATFORM),
        ),
        platform=PLATFORM,
    )


def an_error(
    code: int,
    *,
    subcode: int | None = None,
    message: str = "Something went wrong",
) -> dict[str, Any]:
    """What Meta's own error object looks like."""
    error: dict[str, Any] = {
        "message": message,
        "type": "OAuthException",
        "code": code,
        "fbtrace_id": "AaBbCcDdEe",
    }
    if subcode is not None:
        error["error_subcode"] = subcode
    return {"error": error}


def a_page(
    page_id: str = PAGE_ID,
    *,
    name: str = "Ada's Cakes",
    token: str | None = PAGE_TOKEN,
) -> dict[str, Any]:
    """One entry from the list of pages somebody manages."""
    page: dict[str, Any] = {"id": page_id, "name": name, "category": "Bakery"}
    if token is not None:
        page["access_token"] = token
    return page


async def raising(graph: Graph, path: str) -> Exception:
    """Send one request and hand back whatever it raised."""
    try:
        await graph.json("GET", path)
    except Exception as problem:
        return problem
    raise AssertionError("that request was supposed to fail")


# ---------------------------------------------------------------------------
# Where Meta lives
# ---------------------------------------------------------------------------


class TestWhereMetaLives:
    def test_every_address_carries_the_same_version(self) -> None:
        assert GRAPH_VERSION == "v21.0"
        assert GRAPH_API == "https://graph.facebook.com/v21.0"
        assert SIGN_IN_PAGE == "https://www.facebook.com/v21.0/dialog/oauth"

    def test_the_api_address_has_no_trailing_slash(self) -> None:
        # Every path joined onto it starts with one already.
        assert not GRAPH_API.endswith("/")


class TestThereIsNoAppToRegister:
    def test_it_sends_people_to_metas_own_portal(self) -> None:
        refused = app_must_be_made_by_hand(PLATFORM)

        assert isinstance(refused, NotSupportedError)
        assert refused.platform == PLATFORM
        assert DEVELOPER_PORTAL in str(refused)

    def test_it_warns_about_the_review_and_the_verification(self) -> None:
        # The two things that stop a brand new Meta app from working, and
        # the two nobody expects.
        said = str(app_must_be_made_by_hand(PLATFORM)).lower()

        assert "review" in said
        assert "business verification" in said


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


class TestTheSignInAddress:
    def test_it_asks_meta_for_a_code(self) -> None:
        address = sign_in_url(
            client_id=APP_ID,
            redirect_uri="https://app.example/callback",
            scopes=("pages_manage_posts", "pages_show_list"),
            state="abc123",
        )

        assert address.startswith(f"{SIGN_IN_PAGE}?")
        query = httpx.URL(address).params
        assert query["client_id"] == APP_ID
        assert query["redirect_uri"] == "https://app.example/callback"
        assert query["state"] == "abc123"
        assert query["response_type"] == "code"

    def test_it_separates_permissions_with_commas(self) -> None:
        # Facebook is one of the few that wants commas here. Spaces get you
        # a sign-in page that asks for one very long permission.
        address = sign_in_url(
            client_id=APP_ID,
            redirect_uri="https://app.example/callback",
            scopes=("pages_manage_posts", "pages_show_list"),
            state="abc123",
        )

        assert httpx.URL(address).params["scope"] == (
            "pages_manage_posts,pages_show_list"
        )


class TestSwappingTheCodeForAToken:
    async def test_it_sends_the_code_and_both_halves_of_the_app(
        self,
        clock: datetime,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            route = network.get("/oauth/access_token").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "access_token": "short-lived",
                        "token_type": "bearer",
                        "expires_in": 3600,
                    },
                )
            )

            async with a_graph() as graph:
                token = await swap_code_for_token(
                    graph,
                    client_id=APP_ID,
                    client_secret=APP_SECRET,
                    redirect_uri="https://app.example/callback",
                    code="the-code",
                )

        query = route.calls.last.request.url.params
        assert query["client_id"] == APP_ID
        assert query["client_secret"] == APP_SECRET
        assert query["redirect_uri"] == "https://app.example/callback"
        assert query["code"] == "the-code"
        assert token.access_token == "short-lived"
        assert token.expires_at == datetime(2026, 8, 31, 13, 0, tzinfo=UTC)

    async def test_a_reply_with_no_token_in_it_says_so_plainly(self) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/oauth/access_token").mock(
                return_value=httpx.Response(200, json={"token_type": "bearer"})
            )

            async with a_graph() as graph:
                with pytest.raises(PlatformError, match="access_token"):
                    await swap_code_for_token(
                        graph,
                        client_id=APP_ID,
                        client_secret=APP_SECRET,
                        redirect_uri="https://app.example/callback",
                        code="the-code",
                    )


class TestTheLongLivedToken:
    async def test_it_trades_a_short_token_for_a_two_month_one(
        self,
        clock: datetime,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            route = network.get("/oauth/access_token").mock(
                return_value=httpx.Response(
                    200,
                    json={"access_token": "long-lived", "expires_in": 5_184_000},
                )
            )

            async with a_graph() as graph:
                token = await long_lived_token(
                    graph,
                    client_id=APP_ID,
                    client_secret=APP_SECRET,
                    token="short-lived",
                )

        query = route.calls.last.request.url.params
        assert query["grant_type"] == "fb_exchange_token"
        assert query["fb_exchange_token"] == "short-lived"
        assert query["client_id"] == APP_ID
        assert query["client_secret"] == APP_SECRET
        assert token.access_token == "long-lived"
        assert token.expires_at == datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


class TestReadingATokenOutOfAReply:
    def test_a_reply_with_no_expiry_gives_a_token_that_never_runs_out(self) -> None:
        token = token_from(
            {"access_token": "forever"},
            platform=PLATFORM,
            when="sign someone in",
        )

        assert token.access_token == "forever"
        assert token.expires_at is None

    def test_meta_hands_out_no_refresh_token(self) -> None:
        # There is nothing to refresh with. Saying so here stops anyone
        # looking for one that was never sent.
        token = token_from(
            {"access_token": "forever", "expires_in": 60},
            platform=PLATFORM,
            when="sign someone in",
        )

        assert token.refresh_token is None

    @pytest.mark.parametrize("given", [0, -1, "soon", None])
    def test_an_expiry_it_cannot_read_means_no_expiry(self, given: object) -> None:
        token = token_from(
            {"access_token": "forever", "expires_in": given},
            platform=PLATFORM,
            when="sign someone in",
        )

        assert token.expires_at is None


class TestReadingAValueMetaAlwaysSends:
    def test_it_hands_back_the_value(self) -> None:
        assert (
            required_text({"id": "7"}, "id", platform=PLATFORM, when="publish a post")
            == "7"
        )

    @pytest.mark.parametrize("reply", [{}, {"id": ""}, {"id": 7}])
    def test_a_missing_value_keeps_the_whole_reply_on_the_error(
        self,
        reply: dict[str, Any],
    ) -> None:
        with pytest.raises(PlatformError) as complaint:
            required_text(reply, "id", platform=PLATFORM, when="publish a post")

        assert "publish a post" in str(complaint.value)
        assert complaint.value.raw == reply


# ---------------------------------------------------------------------------
# Which pages somebody manages
# ---------------------------------------------------------------------------


class TestListingThePagesSomebodyManages:
    async def test_it_reads_the_pages_and_their_own_tokens(self) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            route = network.get("/me/accounts").mock(
                return_value=httpx.Response(200, json={"data": [a_page()]})
            )

            async with a_graph(USER_TOKEN) as graph:
                pages = await pages_of(graph)

        assert "access_token" in route.calls.last.request.url.params["fields"]
        assert len(pages) == 1
        assert pages[0].id == PAGE_ID
        assert pages[0].name == "Ada's Cakes"
        assert pages[0].token == PAGE_TOKEN
        assert pages[0].category == "Bakery"

    async def test_it_keeps_reading_while_meta_offers_another_page(self) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/me/accounts").mock(
                side_effect=[
                    httpx.Response(
                        200,
                        json={
                            "data": [a_page("1", name="One")],
                            "paging": {
                                "cursors": {"after": "cursor-one"},
                                "next": f"{GRAPH_API}/me/accounts?after=cursor-one",
                            },
                        },
                    ),
                    httpx.Response(
                        200,
                        json={
                            "data": [a_page("2", name="Two")],
                            # No "next", so this is the last page - even
                            # though Meta still sends a cursor.
                            "paging": {"cursors": {"after": "cursor-two"}},
                        },
                    ),
                ]
            )

            async with a_graph(USER_TOKEN) as graph:
                pages = await pages_of(graph)

        assert [page.id for page in pages] == ["1", "2"]

    async def test_it_stops_after_the_page_it_was_told_to_stop_at(self) -> None:
        endless = httpx.Response(
            200,
            json={
                "data": [a_page()],
                "paging": {
                    "cursors": {"after": "on-and-on"},
                    "next": f"{GRAPH_API}/me/accounts?after=on-and-on",
                },
            },
        )
        with respx.mock(base_url=GRAPH_API) as network:
            route = network.get("/me/accounts").mock(return_value=endless)

            async with a_graph(USER_TOKEN) as graph:
                pages = await pages_of(graph, max_pages=2)

        assert route.call_count == 2
        assert len(pages) == 2

    async def test_a_reply_that_is_not_a_list_of_pages_is_no_pages(self) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/me/accounts").mock(
                return_value=httpx.Response(200, json={"data": "not a list"})
            )

            async with a_graph(USER_TOKEN) as graph:
                assert await pages_of(graph) == ()

    async def test_a_page_with_no_id_on_it_is_left_out(self) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/me/accounts").mock(
                return_value=httpx.Response(
                    200,
                    json={"data": [{"name": "Nameless", "access_token": PAGE_TOKEN}]},
                )
            )

            async with a_graph(USER_TOKEN) as graph:
                assert await pages_of(graph) == ()

    async def test_a_page_with_no_name_is_shown_by_its_id(self) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/me/accounts").mock(
                return_value=httpx.Response(
                    200,
                    json={"data": [{"id": PAGE_ID, "access_token": PAGE_TOKEN}]},
                )
            )

            async with a_graph(USER_TOKEN) as graph:
                pages = await pages_of(graph)

        assert pages[0].name == PAGE_ID
        assert pages[0].category is None

    @pytest.mark.parametrize(
        "paging",
        [
            {"next": "https://graph.facebook.com/next"},
            {"next": "https://graph.facebook.com/next", "cursors": "not an object"},
            {"next": "https://graph.facebook.com/next", "cursors": {"after": ""}},
            "not an object",
        ],
    )
    async def test_paging_we_cannot_follow_stops_rather_than_repeats(
        self,
        paging: object,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            route = network.get("/me/accounts").mock(
                return_value=httpx.Response(
                    200, json={"data": [a_page()], "paging": paging}
                )
            )

            async with a_graph(USER_TOKEN) as graph:
                pages = await pages_of(graph)

        assert route.call_count == 1
        assert len(pages) == 1

    async def test_a_page_with_no_token_on_it_is_left_out(self) -> None:
        # A page the person can see but has not given us is no use: posting
        # as it needs its own token.
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/me/accounts").mock(
                return_value=httpx.Response(
                    200,
                    json={"data": [a_page("1", token=None), a_page("2")]},
                )
            )

            async with a_graph(USER_TOKEN) as graph:
                pages = await pages_of(graph)

        assert [page.id for page in pages] == ["2"]


class TestLookingUpOnePage:
    async def test_it_asks_meta_for_that_pages_own_token(self) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            route = network.get(f"/{PAGE_ID}").mock(
                return_value=httpx.Response(200, json=a_page())
            )

            async with a_graph(USER_TOKEN) as graph:
                page = await page_by_id(graph, page_id=PAGE_ID)

        assert "access_token" in route.calls.last.request.url.params["fields"]
        assert page.id == PAGE_ID
        assert page.token == PAGE_TOKEN

    async def test_a_page_without_a_token_says_what_to_do_about_it(self) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get(f"/{PAGE_ID}").mock(
                return_value=httpx.Response(200, json=a_page(token=None))
            )

            async with a_graph(USER_TOKEN) as graph:
                with pytest.raises(AuthError, match="connect their account again"):
                    await page_by_id(graph, page_id=PAGE_ID)


# ---------------------------------------------------------------------------
# When Meta says no
# ---------------------------------------------------------------------------


class TestWhenMetaSaysNo:
    async def test_a_token_problem_is_a_sign_in_problem(self) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/me").mock(
                return_value=httpx.Response(400, json=an_error(190))
            )

            async with a_graph(USER_TOKEN) as graph:
                problem = await raising(graph, "/me")

        assert isinstance(problem, AuthError)
        assert "190" in str(problem)

    @pytest.mark.parametrize(
        ("subcode", "wanted"),
        [
            (458, "removed your app"),
            (459, "log in to Facebook"),
            (460, "changed their Facebook password"),
            (463, "run out"),
            (467, "no longer valid"),
        ],
    )
    async def test_it_says_which_kind_of_token_problem_it_was(
        self,
        subcode: int,
        wanted: str,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/me").mock(
                return_value=httpx.Response(400, json=an_error(190, subcode=subcode))
            )

            async with a_graph(USER_TOKEN) as graph:
                problem = await raising(graph, "/me")

        assert isinstance(problem, AuthError)
        assert wanted in str(problem)

    @pytest.mark.parametrize("code", [4, 17, 32, 613])
    async def test_the_four_ways_meta_asks_us_to_slow_down(self, code: int) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/me").mock(
                return_value=httpx.Response(400, json=an_error(code))
            )

            async with a_graph(USER_TOKEN) as graph:
                problem = await raising(graph, "/me")

        assert isinstance(problem, RateLimitError)

    @pytest.mark.parametrize("code", [10, 200, 368])
    async def test_a_permission_that_was_never_granted(self, code: int) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/me").mock(
                return_value=httpx.Response(403, json=an_error(code))
            )

            async with a_graph(USER_TOKEN) as graph:
                problem = await raising(graph, "/me")

        assert isinstance(problem, NotAllowedError)

    @pytest.mark.parametrize("code", [100, 506])
    async def test_something_wrong_with_the_post_itself(self, code: int) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/me").mock(
                return_value=httpx.Response(400, json=an_error(code))
            )

            async with a_graph(USER_TOKEN) as graph:
                problem = await raising(graph, "/me")

        assert isinstance(problem, InvalidPostError)

    async def test_a_duplicate_post_says_that_is_what_happened(self) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/me").mock(
                return_value=httpx.Response(400, json=an_error(506))
            )

            async with a_graph(USER_TOKEN) as graph:
                problem = await raising(graph, "/me")

        assert "same words twice" in str(problem)

    async def test_a_code_we_have_no_name_for_still_arrives_readable(self) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/me").mock(
                return_value=httpx.Response(
                    400, json=an_error(99999, message="Mystery")
                )
            )

            async with a_graph(USER_TOKEN) as graph:
                problem = await raising(graph, "/me")

        assert isinstance(problem, PlatformError)
        assert "Mystery" in str(problem)
        assert "99999" in str(problem)

    async def test_meta_keeps_its_own_reply_on_the_error(self) -> None:
        body = an_error(190)
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/me").mock(return_value=httpx.Response(400, json=body))

            async with a_graph(USER_TOKEN) as graph:
                problem = await raising(graph, "/me")

        assert isinstance(problem, AuthError)
        assert problem.raw == body

    async def test_a_refusal_with_no_error_object_falls_back_to_the_shared_one(
        self,
    ) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/me").mock(
                return_value=httpx.Response(404, text="<html>no</html>")
            )

            async with a_graph(USER_TOKEN) as graph:
                problem = await raising(graph, "/me")

        assert isinstance(problem, NotFoundError)

    async def test_an_error_hiding_inside_a_perfectly_happy_reply(self) -> None:
        # This is the one that catches people out: Meta answers 200 and puts
        # the refusal in the body, so a check on the status alone sees
        # nothing wrong at all.
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/me").mock(
                return_value=httpx.Response(200, json=an_error(190))
            )

            async with a_graph(USER_TOKEN) as graph:
                problem = await raising(graph, "/me")

        assert isinstance(problem, AuthError)

    async def test_an_error_that_is_not_an_object_is_not_an_error(self) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/me").mock(
                return_value=httpx.Response(200, json={"error": "nope", "id": "7"})
            )

            async with a_graph(USER_TOKEN) as graph:
                assert await graph.json("GET", "/me") == {"error": "nope", "id": "7"}

    async def test_how_long_to_wait_comes_from_metas_own_estimate(self) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/me").mock(
                return_value=httpx.Response(
                    400,
                    json=an_error(32),
                    headers={
                        "X-Business-Use-Case-Usage": json.dumps(
                            {
                                PAGE_ID: [
                                    {
                                        "type": "pages",
                                        "call_count": 100,
                                        "total_cputime": 20,
                                        "total_time": 30,
                                        "estimated_time_to_regain_access": 12,
                                    }
                                ]
                            }
                        )
                    },
                )
            )

            async with a_graph(USER_TOKEN) as graph:
                problem = await raising(graph, "/me")

        assert isinstance(problem, RateLimitError)
        # Meta counts that one in minutes; everything else here is seconds.
        assert problem.retry_after == 720.0

    async def test_being_told_to_slow_down_says_how_much_is_gone(self) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/me").mock(
                return_value=httpx.Response(
                    400,
                    json=an_error(4),
                    headers={"X-App-Usage": json.dumps({"call_count": 97})},
                )
            )

            async with a_graph(USER_TOKEN) as graph:
                problem = await raising(graph, "/me")

        assert "97%" in str(problem)


# ---------------------------------------------------------------------------
# How much of the allowance is left
# ---------------------------------------------------------------------------


class TestHowMuchAllowanceIsLeft:
    def test_it_reads_the_three_percentages_meta_sends(self) -> None:
        headers = httpx.Headers(
            {
                "X-App-Usage": json.dumps(
                    {"call_count": 10, "total_cputime": 20, "total_time": 30}
                )
            }
        )

        assert usage_from_headers(headers) == Usage(
            calls=10,
            cpu_time=20,
            total_time=30,
        )

    def test_the_worst_of_the_three_is_the_one_that_cuts_us_off(self) -> None:
        assert Usage(calls=10, cpu_time=95, total_time=30).worst == 95
        assert Usage().worst is None

    def test_it_reads_the_business_header_too(self) -> None:
        headers = httpx.Headers(
            {
                "X-Business-Use-Case-Usage": json.dumps(
                    {
                        PAGE_ID: [
                            {
                                "type": "pages",
                                "call_count": 40,
                                "total_cputime": 5,
                                "total_time": 6,
                                "estimated_time_to_regain_access": 0,
                            }
                        ]
                    }
                )
            }
        )

        seen = usage_from_headers(headers)

        assert seen is not None
        assert seen.calls == 40
        assert seen.wait_seconds is None

    def test_two_pages_in_one_header_report_the_worse_of_them(self) -> None:
        headers = httpx.Headers(
            {
                "X-Business-Use-Case-Usage": json.dumps(
                    {
                        "111": [
                            {"call_count": 40, "estimated_time_to_regain_access": 2}
                        ],
                        "222": [
                            {"call_count": 90, "estimated_time_to_regain_access": 5}
                        ],
                    }
                )
            }
        )

        seen = usage_from_headers(headers)

        assert seen is not None
        assert seen.calls == 90
        assert seen.wait_seconds == 300.0

    def test_both_headers_together_report_whichever_is_worse(self) -> None:
        headers = httpx.Headers(
            {
                "X-App-Usage": json.dumps({"call_count": 80, "total_time": 5}),
                "X-Business-Use-Case-Usage": json.dumps(
                    {PAGE_ID: [{"call_count": 20, "total_time": 70}]}
                ),
            }
        )

        assert usage_from_headers(headers) == Usage(calls=80, total_time=70)

    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"X-App-Usage": "not json at all"},
            {"X-App-Usage": "[1, 2, 3]"},
            {"X-App-Usage": json.dumps({"call_count": "lots"})},
            {"X-Business-Use-Case-Usage": json.dumps({PAGE_ID: "not a list"})},
            {"X-Business-Use-Case-Usage": json.dumps({PAGE_ID: ["not an object"]})},
        ],
    )
    def test_a_header_it_cannot_read_is_no_news_rather_than_zero(
        self,
        headers: dict[str, str],
    ) -> None:
        assert usage_from_headers(httpx.Headers(headers)) is None


class TestRememberingTheAllowance:
    async def test_a_conversation_remembers_what_the_last_reply_said(self) -> None:
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/me").mock(
                return_value=httpx.Response(
                    200,
                    json={"id": "7"},
                    headers={"X-App-Usage": json.dumps({"call_count": 42})},
                )
            )

            async with a_graph(USER_TOKEN) as graph:
                await graph.json("GET", "/me")
                seen = graph.usage

        assert seen is not None
        assert seen.calls == 42

    async def test_it_knows_nothing_until_meta_has_said_something(self) -> None:
        async with a_graph(USER_TOKEN) as graph:
            assert graph.usage is None

    async def test_a_reply_that_says_nothing_leaves_the_last_figures_alone(
        self,
    ) -> None:
        # No headers means no news, not "nothing left".
        with respx.mock(base_url=GRAPH_API) as network:
            network.get("/me").mock(
                side_effect=[
                    httpx.Response(
                        200,
                        json={"id": "7"},
                        headers={"X-App-Usage": json.dumps({"call_count": 42})},
                    ),
                    httpx.Response(200, json={"id": "7"}),
                ]
            )

            async with a_graph(USER_TOKEN) as graph:
                await graph.json("GET", "/me")
                await graph.json("GET", "/me")
                seen = graph.usage

        assert seen is not None
        assert seen.calls == 42

    async def test_it_says_which_network_it_is_talking_to(self) -> None:
        async with a_graph() as graph:
            assert graph.platform == PLATFORM


# ---------------------------------------------------------------------------
# Requests Meta sends us
# ---------------------------------------------------------------------------


class TestCheckingASignature:
    def test_a_body_signed_with_the_agreed_secret_is_accepted(self) -> None:
        import hashlib
        import hmac

        body = b'{"object":"page"}'
        digest = hmac.new(b"the-app-secret", body, hashlib.sha256).hexdigest()

        check_meta_signature(
            body,
            {"X-Hub-Signature-256": f"sha256={digest}"},
            secret="the-app-secret",
        )

    def test_a_body_that_was_changed_on_the_way_here_is_refused(self) -> None:
        import hashlib
        import hmac

        body = b'{"object":"page"}'
        digest = hmac.new(b"the-app-secret", body, hashlib.sha256).hexdigest()

        with pytest.raises(SignatureError):
            check_meta_signature(
                b'{"object":"page","extra":1}',
                {"X-Hub-Signature-256": f"sha256={digest}"},
                secret="the-app-secret",
            )

    def test_the_old_sha1_header_is_not_enough(self) -> None:
        # Meta still sends X-Hub-Signature as well. It is SHA-1 and we do
        # not look at it, so a request carrying only that one is refused.
        with pytest.raises(SignatureError, match="X-Hub-Signature-256"):
            check_meta_signature(
                b"{}",
                {"X-Hub-Signature": "sha1=abc"},
                secret="the-app-secret",
            )


class TestReadingWhatMetaPushed:
    def test_it_pulls_out_what_happened_and_where(self) -> None:
        body = json.dumps(
            {
                "object": "page",
                "entry": [
                    {
                        "id": PAGE_ID,
                        "time": 1_790_000_000,
                        "changes": [
                            {
                                "field": "feed",
                                "value": {"item": "comment", "verb": "add"},
                            }
                        ],
                    }
                ],
            }
        ).encode()

        changes = changes_in(body, platform=PLATFORM)

        assert len(changes) == 1
        assert changes[0].account_id == PAGE_ID
        assert changes[0].topic == "feed"
        assert changes[0].value == {"item": "comment", "verb": "add"}
        assert changes[0].when == datetime.fromtimestamp(1_790_000_000, UTC)

    def test_one_message_can_carry_several_changes(self) -> None:
        body = json.dumps(
            {
                "entry": [
                    {
                        "id": PAGE_ID,
                        "time": 1_790_000_000,
                        "changes": [
                            {"field": "feed", "value": {"item": "comment"}},
                            {"field": "feed", "value": {"item": "reaction"}},
                        ],
                    },
                    {
                        "id": "other-page",
                        "time": 1_790_000_001,
                        "changes": [{"field": "feed", "value": {"item": "status"}}],
                    },
                ]
            }
        ).encode()

        changes = changes_in(body, platform=PLATFORM)

        assert len(changes) == 3
        assert changes[-1].account_id == "other-page"

    def test_an_entry_with_no_time_on_it_is_stamped_as_it_arrives(
        self,
        clock: datetime,
    ) -> None:
        body = json.dumps(
            {"entry": [{"id": PAGE_ID, "changes": [{"field": "feed", "value": {}}]}]}
        ).encode()

        assert changes_in(body, platform=PLATFORM)[0].when == NOW

    @pytest.mark.parametrize(
        "body",
        [
            b'{"entry": []}',
            b'{"entry": "not a list"}',
            b"{}",
            b'{"entry": [{"id": "1", "changes": "not a list"}]}',
            b'{"entry": [{"id": "1", "changes": ["not an object"]}]}',
            b'{"entry": ["not an object"]}',
        ],
    )
    def test_a_message_with_nothing_in_it_is_no_changes(self, body: bytes) -> None:
        assert changes_in(body, platform=PLATFORM) == []

    @pytest.mark.parametrize("body", [b"not json", b"[1, 2, 3]"])
    def test_a_body_that_is_not_a_meta_message_says_so(self, body: bytes) -> None:
        with pytest.raises(PlatformError, match="could not be read"):
            changes_in(body, platform=PLATFORM)

    def test_the_whole_entry_is_kept_for_anything_we_did_not_model(self) -> None:
        entry = {
            "id": PAGE_ID,
            "time": 1_790_000_000,
            "changes": [{"field": "feed", "value": {"item": "comment"}}],
        }
        body = json.dumps({"entry": [entry]}).encode()

        assert changes_in(body, platform=PLATFORM)[0].raw == entry
