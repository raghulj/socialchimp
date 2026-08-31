"""The nine networks, and the handful of things only this app can know.

Most of what a page here wants to show - can this network schedule, does it
take a video, does it push updates - is already on the platform, and asking
it is better than keeping a second copy that drifts. So the functions at the
bottom ask socialchimp.

What is left is genuinely the app's own knowledge: whether the connect page
has to ask for a server address, and where a person goes to get the
credentials in `.env`. That is the table at the top, and it is nine short
rows rather than nine special cases scattered through the views.
"""

from __future__ import annotations

from dataclasses import dataclass

from socialchimp.features import Feature
from socialchimp.platform import (
    CanAnswerSetupCheck,
    CanCheckSignature,
    CanResumeLogin,
)

from .client import client


@dataclass(frozen=True, slots=True)
class Network:
    """What this app knows about one network.

    Attributes:
        name: The name socialchimp knows it by, and the one in every URL
            here.
        label: What to call it on a page.
        asks_for_a_server: Whether the connect page needs a box for a server
            address. True for Mastodon, where every server is a separate
            place with its own app, its own accounts and its own post
            length.
        where_the_app_comes_from: One sentence for the connect page, saying
            where the id and secret in `.env` were got from. This is the
            part that takes weeks, so it is worth saying on the page rather
            than only in the README.
    """

    name: str
    label: str
    asks_for_a_server: bool
    where_the_app_comes_from: str


NETWORKS: tuple[Network, ...] = (
    Network(
        name="mastodon",
        label="Mastodon",
        asks_for_a_server=True,
        where_the_app_comes_from=(
            "Nowhere - socialchimp registers the app itself, once per "
            "server. This is the only network where that is possible."
        ),
    ),
    Network(
        name="bluesky",
        label="Bluesky",
        asks_for_a_server=False,
        where_the_app_comes_from=(
            "Nowhere - there is no app and no portal. People sign in with "
            "an app password they make in Bluesky's own settings."
        ),
    ),
    Network(
        name="facebook",
        label="Facebook Pages",
        asks_for_a_server=False,
        where_the_app_comes_from=(
            "developers.facebook.com/apps, with Facebook Login added. Meta "
            "reviews the posting permissions and verifies the business "
            "behind the app before either works for anybody but you."
        ),
    ),
    Network(
        name="instagram",
        label="Instagram",
        asks_for_a_server=False,
        where_the_app_comes_from=(
            "The same Meta app as Facebook Pages, and the same review. Only "
            "Business and Creator accounts can publish at all."
        ),
    ),
    Network(
        name="threads",
        label="Threads",
        asks_for_a_server=False,
        where_the_app_comes_from=(
            "The same Meta app - but adding the Threads use case makes a "
            "second id and secret, sitting next to the Facebook pair and "
            "not interchangeable with it. Use the Threads pair."
        ),
    ),
    Network(
        name="tiktok",
        label="TikTok",
        asks_for_a_server=False,
        where_the_app_comes_from=(
            "developers.tiktok.com, with Login Kit and the Content Posting "
            "API added. TikTok calls them the client key and client secret. "
            "Until the app is audited everything it posts is private."
        ),
    ),
    Network(
        name="youtube",
        label="YouTube",
        asks_for_a_server=False,
        where_the_app_comes_from=(
            "console.cloud.google.com, with the YouTube Data API v3 turned "
            "on and an OAuth client made. Uploading is a sensitive "
            "permission, so Google reviews it."
        ),
    ),
    Network(
        name="x",
        label="X",
        asks_for_a_server=False,
        where_the_app_comes_from=(
            "developer.x.com, where somebody creates the app and its OAuth "
            "client and chooses a paid plan. Posting is not on the free "
            "one."
        ),
    ),
    Network(
        name="pinterest",
        label="Pinterest",
        asks_for_a_server=False,
        where_the_app_comes_from=(
            "developers.pinterest.com/apps. A new app gets Trial access, "
            "and pins made on Trial are visible only to the person who made "
            "them - nothing in the API says so."
        ),
    ),
)

BY_NAME = {network.name: network for network in NETWORKS}


def network_or_none(name: str) -> Network | None:
    """Look up one network by the name in a URL.

    Args:
        name: The name from the address.

    Returns:
        The network, or `None` for a name this project does not cover. The
        views turn that into a 404 rather than letting it reach socialchimp,
        because a 404 is the honest answer to an address that does not
        exist.
    """
    return BY_NAME.get(name)


def features(name: str) -> Feature:
    """Return what one network can do, as the platform itself states it.

    Asked rather than written down here, so this page cannot disagree with
    the code that enforces it.

    Args:
        name: Which network.

    Returns:
        The feature flags.
    """
    return client().platform_for(name).features


def can_register_the_app(name: str) -> bool:
    """Say whether socialchimp can register your app on this network.

    Args:
        name: Which network.

    Returns:
        True for Mastodon and nothing else, today.
    """
    return Feature.CREATE_APP in features(name)


def asks_which_account(name: str) -> bool:
    """Say whether signing in here pauses to ask which account to use.

    Facebook asks which Page, Instagram which business account, YouTube
    which channel. A platform that can be resumed part way through a
    sign-in is exactly the set that asks, so this reads that off the
    platform rather than keeping a list of three names.

    Args:
        name: Which network.

    Returns:
        True where `finish_login` can answer with `ChooseAccount`.
    """
    return isinstance(client().platform_for(name), CanResumeLogin)


def pushes_updates(name: str) -> bool:
    """Say whether this network sends requests to a URL of ours.

    Args:
        name: Which network.

    Returns:
        True where there is a webhook to receive. The rest are asked on a
        timer instead, which is `Account.fetch_updates` and
        `socialchimp.events.Poller` rather than anything in this file.
    """
    return isinstance(client().platform_for(name), CanCheckSignature)


def answers_a_setup_check(name: str) -> bool:
    """Say whether this network asks a question before it will push.

    Meta's three do: point one at a URL and it does a GET to that URL
    first, carrying a token you invented and a challenge to echo back.
    TikTok starts sending straight away.

    Args:
        name: Which network.

    Returns:
        True where the webhook has a `GET` half to answer.
    """
    return isinstance(client().platform_for(name), CanAnswerSetupCheck)
