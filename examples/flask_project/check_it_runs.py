"""Run the whole app against pretend networks, with no credentials at all.

    uv run --with flask python -m examples.flask_project.check_it_runs

Nothing here reaches the internet. `socialchimp.testing.FakePlatform` is a
network that works without a network, and handing one to `SocialChimp` under
a real network's name means every line of the app runs unchanged - the
routes, the storage, the sign-in, the webhook signature check.

It walks:

1. Mastodon, where socialchimp registers the app for you, once per server.
2. Bluesky, which has no sign-in page and asks for a handle and an app
   password instead.
3. Facebook, which pauses to ask which Page.
4. One post to four accounts at once, with two of them refusing for two
   different reasons, and the app carrying on.
5. A scheduled post, refused by the network that cannot schedule.
6. Meta's setup check, right and wrong, and TikTok refusing to answer one
   because it does not ask.
7. A signed webhook, an unsigned one, and somebody revoking the app.
8. Every option name this app can produce, checked against the `POST_OPTIONS`
   the platform file itself declares.

It prints a line per step and exits non-zero if any of them is wrong.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final
from urllib.parse import parse_qs, urlparse

from flask.testing import FlaskClient

from socialchimp import (
    AppCredentials,
    Feature,
)
from socialchimp.events import answer_setup_check
from socialchimp.platform import AccountChoice, LoginField, Platform
from socialchimp.platforms import (
    bluesky,
    facebook,
    instagram,
    mastodon,
    pinterest,
    threads,
    tiktok,
    x,
    youtube,
)
from socialchimp.testing import FakePlatform

from .config import Settings
from .factory import create_app
from .posts import Composition, build_post
from .runtime import Chimp

# None of these is a secret. They are named rather than written where they
# are used so that a linter's hunt for real ones stays worth listening to.
PRETEND_ID: Final = "pretend-client-id"
PRETEND_HALF: Final = "pretend-client-half"
PRETEND_VERIFY: Final = "pretend-verify-phrase"
COOKIE_PHRASE: Final = "pretend-cookie-phrase"

EVERY_NETWORK: Final = (
    "mastodon",
    "bluesky",
    "facebook",
    "instagram",
    "youtube",
    "tiktok",
    "threads",
    "x",
    "pinterest",
)

# Every network's own list of what `Post.options` accepts, read from the
# platform file rather than copied out of the documentation. If somebody
# renames an option upstream, this script fails instead of the example
# quietly teaching a name that no longer exists.
DECLARED_OPTIONS: Final = {
    "mastodon": mastodon.POST_OPTIONS,
    "bluesky": bluesky.POST_OPTIONS,
    "facebook": facebook.POST_OPTIONS,
    "instagram": instagram.POST_OPTIONS,
    "youtube": youtube.POST_OPTIONS,
    "tiktok": tiktok.POST_OPTIONS,
    "threads": threads.POST_OPTIONS,
    "x": x.POST_OPTIONS,
    "pinterest": pinterest.POST_OPTIONS,
}


def check(true_of_it: bool, what: str) -> None:
    """Fail loudly, naming the step, rather than carrying on.

    Args:
        true_of_it: What was expected.
        what: The step, for the message.

    Raises:
        SystemExit: If it was not true.
    """
    if not true_of_it:
        raise SystemExit(f"WRONG: {what}")
    print(f"  ok  {what}")


# The library names a connection after the network and the account by
# default - `facebook:<page id>`, `tiktok:<open id>` - which is what lets a
# webhook naming a Page line up with a row without a lookup table of your
# own. The fakes get the same shape, so nine networks do not share one row.
PretendNetwork = FakePlatform


class PretendMetaNetwork(PretendNetwork):
    """A fake that also answers the check Meta makes before it will push.

    Facebook, Instagram and Threads ask this question and TikTok does not,
    so only these three have the method - which is what lets the app show
    TikTok refusing it by name.
    """

    def answer_setup_check(
        self,
        params: Mapping[str, str],
        *,
        verify_token: str,
    ) -> str:
        """Answer the one-off GET Meta sends to a new webhook address.

        Args:
            params: The query values from that GET.
            verify_token: The token we typed into Meta's own form.

        Returns:
            The challenge, to send back as the whole body.
        """
        return answer_setup_check(params, expected_token=verify_token)


def pretend_networks() -> dict[str, Platform]:
    """Build one fake per network, each behaving like the real one.

    Returns:
        The nine, by name, ready for `create_app`.
    """
    posting = Feature.POST_TEXT | Feature.POST_IMAGE | Feature.POST_VIDEO

    return {
        "mastodon": PretendNetwork(
            name="mastodon",
            # The only network socialchimp can register an app with, and
            # its tokens never expire.
            features=posting | Feature.SCHEDULE | Feature.CREATE_APP,
            token_lifetime=None,
            secret=PRETEND_HALF,
        ),
        "bluesky": PretendNetwork(
            name="bluesky",
            # No video, no scheduling, and no sign-in page to send anybody
            # to - an app password instead.
            features=Feature.POST_TEXT | Feature.POST_IMAGE,
            ask_for=(
                LoginField(name="handle", label="Your Bluesky handle"),
                LoginField(name="app_password", label="App password", secret=True),
            ),
            secret=PRETEND_HALF,
        ),
        "facebook": PretendMetaNetwork(
            name="facebook",
            features=posting | Feature.SCHEDULE | Feature.PUSH_UPDATES,
            # Giving it accounts makes it pause to ask which Page, the way
            # the real one always does.
            accounts=(
                AccountChoice(id="1001", name="Bench & Bloom", kind="page"),
                AccountChoice(id="1002", name="Bench & Bloom North", kind="page"),
            ),
            secret=PRETEND_HALF,
        ),
        "instagram": PretendMetaNetwork(
            name="instagram",
            # No text-only post anywhere in the API.
            features=Feature.POST_IMAGE | Feature.POST_VIDEO | Feature.PUSH_UPDATES,
            accounts=(AccountChoice(id="2001", name="benchandbloom", kind="account"),),
            secret=PRETEND_HALF,
        ),
        "youtube": PretendNetwork(
            name="youtube",
            # Video only. Community posts are in no part of the API.
            features=Feature.POST_VIDEO | Feature.SCHEDULE,
            accounts=(AccountChoice(id="3001", name="Bench & Bloom", kind="channel"),),
            secret=PRETEND_HALF,
        ),
        "tiktok": PretendNetwork(
            name="tiktok",
            features=Feature.POST_VIDEO | Feature.PUSH_UPDATES,
            secret=PRETEND_HALF,
            # TikTok pushes, but it asks nothing before it starts - it just
            # begins sending. Only Meta's three do the setup check, so this
            # fake says it does not, the same as the real one.
            answers_setup_checks=False,
        ),
        "threads": PretendMetaNetwork(
            name="threads",
            features=posting | Feature.PUSH_UPDATES,
            secret=PRETEND_HALF,
        ),
        "x": PretendNetwork(
            name="x",
            features=posting,
            secret=PRETEND_HALF,
        ),
        "pinterest": PretendNetwork(
            name="pinterest",
            features=Feature.POST_IMAGE | Feature.POST_VIDEO,
            secret=PRETEND_HALF,
        ),
    }


def pretend_settings(database: Path) -> Settings:
    """Build the settings, with credentials for every network.

    Mastodon is left out on purpose: it has none until socialchimp registers
    the app on a server, which is one of the things this script checks.

    Args:
        database: Where to put the sqlite file.

    Returns:
        The settings.
    """
    return Settings(
        database=database,
        public_url="http://localhost:5000",
        cookie_phrase=COOKIE_PHRASE,
        mastodon_app_name="socialchimp Flask example",
        apps={
            name: AppCredentials(
                platform=name,
                host=None,
                client_id=PRETEND_ID,
                client_secret=PRETEND_HALF,
            )
            for name in EVERY_NETWORK
            if name != "mastodon"
        },
        setup_tokens={
            "facebook": PRETEND_VERIFY,
            "instagram": PRETEND_VERIFY,
            "threads": PRETEND_VERIFY,
        },
    )


def state_in(location: str) -> str:
    """Pull the state out of the address a network was going to send us to.

    Args:
        location: Where the browser was redirected.

    Returns:
        The state.
    """
    return parse_qs(urlparse(location).query)["state"][0]


def connect_by_redirect(
    client: FlaskClient,
    platform: str,
    *,
    host: str = "",
    picking: str = "",
) -> None:
    """Walk the ordinary sign-in: away to the network, then back again.

    Args:
        client: The test client, which keeps the session cookie between
            requests the way a browser does.
        platform: Which network.
        host: Which server, for Mastodon.
        picking: The account id to pick, for a network that pauses to ask.
    """
    where = f"/sign-in/start/{platform}"
    if host:
        where = f"{where}?host={host}"

    sent_away = client.get(where)
    check(sent_away.status_code == 302, f"{platform}: sent to the network")
    state = state_in(sent_away.headers["Location"])

    came_back = client.get(f"/sign-in/callback/{platform}?state={state}&code=pretend")

    if picking:
        check(came_back.status_code == 200, f"{platform}: asked which account")
        check(
            "Which" in came_back.get_data(as_text=True),
            f"{platform}: the choose page was drawn",
        )
        chosen = client.post(
            f"/sign-in/choose/{platform}",
            data={"account_id": picking},
        )
        check(chosen.status_code == 302, f"{platform}: finished after choosing")
        return

    check(came_back.status_code == 302, f"{platform}: finished in one step")


def signed_body(
    networks: Mapping[str, Platform], platform: str, sending: object
) -> tuple[bytes, dict[str, str]]:
    """Build a webhook body and the headers that prove where it came from.

    Args:
        networks: The fakes, so we can ask one to sign.
        platform: Which network is pretending to push.
        sending: What to send, as anything `json.dumps` will take.

    Returns:
        The exact bytes, and the headers to send with them.
    """
    pusher = networks[platform]
    if not isinstance(pusher, FakePlatform):
        message = "This only works against the fakes."
        raise SystemExit(message)
    # The bytes are built once and both signed and sent. That is the whole
    # point: a signature is over exactly these bytes, and building the JSON
    # again anywhere in between would change the spacing and break it.
    body = json.dumps(sending).encode()
    return body, pusher.sign(body)


def main() -> int:
    """Run every check and say how it went.

    Returns:
        0 when everything passed.
    """
    where = Path(tempfile.mkdtemp(prefix="socialchimp-flask-")) / "demo.sqlite3"
    networks = pretend_networks()
    app = create_app(pretend_settings(where), networks)
    client = app.test_client()

    print("\nPages")
    check(client.get("/").status_code == 200, "the connections page draws")
    check(client.get("/sign-in/").status_code == 200, "the connect page draws")
    check(client.get("/compose").status_code == 200, "the compose form draws")

    print("\nMastodon - socialchimp registers the app, once per server")
    fake_mastodon = networks["mastodon"]
    if not isinstance(fake_mastodon, FakePlatform):
        message = "The mastodon fake is not a fake."
        raise SystemExit(message)
    connect_by_redirect(client, "mastodon", host="mastodon.social")
    check(len(fake_mastodon.created_apps) == 1, "the app was registered once")
    connect_by_redirect(client, "mastodon", host="mastodon.social")
    check(
        len(fake_mastodon.created_apps) == 1,
        "and not registered again on the same server",
    )

    print("\nBluesky - a handle and an app password, with nowhere to send anybody")
    asked = client.get("/sign-in/start/bluesky")
    check(asked.status_code == 200, "the details form was drawn instead of a redirect")
    page = asked.get_data(as_text=True)
    check('name="handle"' in page, "the handle box is there")
    check('type="password"' in page, "and the secret field is hidden")
    typed = client.post(
        "/sign-in/details/bluesky",
        data={"handle": "someone.bsky.social", "app_password": "pretend-app-half"},
    )
    check(typed.status_code == 302, "the details finished the sign-in")

    print("\nFacebook - it pauses to ask which Page")
    connect_by_redirect(client, "facebook", picking="1002")

    fake_facebook = networks["facebook"]
    if not isinstance(fake_facebook, FakePlatform):
        message = "The facebook fake is not a fake."
        raise SystemExit(message)
    # What `start_login` handed us has to survive all three requests. It is
    # easy to overwrite it while writing the resume token down, and the
    # networks that pause to ask happen not to need it a third time - so
    # that mistake would only break on the next network that does.
    check(
        fake_facebook.last_remember == {"verifier": "fake-verifier"},
        "facebook: what start_login handed us reached the third request",
    )

    print("\nYouTube - it pauses to ask which channel")
    connect_by_redirect(client, "youtube", picking="3001")

    here = app.extensions["socialchimp"]
    if not isinstance(here, Chimp):
        message = "create_app did not leave its state where it says it does."
        raise SystemExit(message)

    saved = {connection.id for connection in here.store.everything()}
    check(
        saved
        == {
            "mastodon:42",
            "bluesky:42",
            "facebook:1002",
            "youtube:3001",
        },
        f"four connections were saved: {sorted(saved)}",
    )

    print("\nOne post, four accounts, this app's own loop")
    posted = client.post(
        "/compose",
        data={
            "connection_id": [
                "mastodon:42",
                "bluesky:42",
                "facebook:1002",
                "youtube:3001",
            ],
            "text": "We are open until six today.",
        },
    )
    check(posted.status_code == 200, "the results page drew")
    results = posted.get_data(as_text=True)
    check(results.count("DONE") == 3, "three networks took it")
    check(
        "youtube does not support posting text" in results.lower()
        or "NotSupportedError" in results,
        "YouTube refused a post of words alone, by name",
    )

    print("\nA scheduled post, refused by the network that cannot schedule")
    friday = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    later = client.post(
        "/compose",
        data={
            "connection_id": ["mastodon:42", "bluesky:42"],
            "text": "Open late on Friday.",
            "publish_at": friday,
        },
    )
    said = later.get_data(as_text=True)
    check("NotSupportedError" in said, "Bluesky refused to schedule")
    check("DONE" in said, "and Mastodon still took its copy")

    print("\nThe setup check Meta makes before it will push anything")
    right = client.get(
        "/webhooks/facebook?hub.mode=subscribe"
        f"&hub.verify_token={PRETEND_VERIFY}&hub.challenge=echo-this"
    )
    check(right.status_code == 200, "a correct setup check is answered")
    check(
        right.get_data(as_text=True) == "echo-this",
        "with the challenge and nothing else",
    )
    wrong = client.get(
        "/webhooks/facebook?hub.mode=subscribe"
        "&hub.verify_token=not-ours&hub.challenge=echo-this"
    )
    check(wrong.status_code == 403, "a wrong token gets 403 and no explanation")
    none_asked = client.get(
        "/webhooks/tiktok?hub.mode=subscribe"
        f"&hub.verify_token={PRETEND_VERIFY}&hub.challenge=echo-this"
    )
    check(none_asked.status_code == 400, "TikTok refuses one: it asks nothing first")

    print("\nA signed webhook, on the raw bytes")
    body, headers = signed_body(
        networks,
        "facebook",
        {
            "id": "update-1",
            "kind": "comment_created",
            "connection_id": "facebook:1002",
            "at": datetime.now(UTC).isoformat(),
        },
    )
    pushed = client.post("/webhooks/facebook", data=body, headers=headers)
    check(pushed.status_code == 200, "a properly signed update is taken")
    check(pushed.get_json() == {"ok": True, "updates": 1}, "and handed on")

    unsigned = client.post("/webhooks/facebook", data=body)
    check(unsigned.status_code == 401, "an unsigned one gets 401 and nothing else")
    check(
        unsigned.get_data(as_text=True) == "Refused.",
        "which does not say which check failed",
    )

    print("\nSomebody removing the app takes their connection with it")
    gone_body, gone_headers = signed_body(
        networks,
        "facebook",
        {
            "id": "update-2",
            "kind": "connection_revoked",
            "connection_id": "facebook:1002",
            "at": datetime.now(UTC).isoformat(),
        },
    )
    revoked = client.post("/webhooks/facebook", data=gone_body, headers=gone_headers)
    check(revoked.status_code == 200, "the revocation was taken")
    check(
        here.store.get_connection("facebook:1002") is None,
        "and the connection was deleted by the handler",
    )

    print("\nThe same words, counted four different ways")
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466"
    counted = client.get("/how-long", query_string={"text": family})
    check(
        counted.get_json()["counted"]
        == {
            "CHARACTERS": 7,
            "GRAPHEMES": 1,
            "UTF8_BYTES": 25,
            "UTF16_UNITS": 11,
        },
        "one family emoji is 1, 7, 25 or 11 depending on who is asking",
    )

    print("\nEvery option name, against the platform file that declares it")
    asked_for = Composition(
        text="words",
        media_url="https://example.com/a.jpg",
        title="A title",
        made_for_kids=False,
        privacy_status="public",
        board_id="12345",
        link="https://example.com",
        visibility="unlisted",
        language="en",
        langs=("en",),
        send_to="profile",
        reply_settings="everyone",
    )
    for name, declared in DECLARED_OPTIONS.items():
        built = build_post(name, asked_for)
        unknown = set(built.options) - set(declared)
        check(not unknown, f"{name}: every option is one it accepts")

    print("\nAll of it ran. Nothing left this machine.")
    print(f"The database it used: {where}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
