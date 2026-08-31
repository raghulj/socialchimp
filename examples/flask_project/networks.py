"""The nine networks, and what each one needs before it will work.

One table, read by three things: the connect page, the README, and the code
that builds a post. Keeping it in one place is the point - the differences
between these networks are the whole difficulty of the job, and hiding them
behind an average would only move the surprise to production.

Everything in `Network` is a fact you can check against `docs/platforms.md`
and against the platform file itself. Nothing here is invented.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class Network:
    """One network, and the shape of the work it makes you do.

    Attributes:
        name: What socialchimp calls it. This is the string that goes into
            `sc.start_login(...)` and into `Connection.platform`.
        label: What a person is shown.
        sign_in: Which of the four sign-in shapes this one is. See
            `views/signin.py`, which has a branch for each.
        asks_which_account: Whether `finish_login` pauses with
            `ChooseAccount` and needs a third request to `sc.choose`.
        register_app: How your app comes to exist on this network.
        reviewed: Whether a human at the network has to approve your app
            before it works for anybody but you.
        env_vars: The two environment variables holding this network's
            client id and client secret, in that order. Empty where there is
            nothing to set - Mastodon is registered for you and Bluesky has
            nothing to register.
        updates: How you hear that something happened - pushed to a URL of
            yours, found by asking on a timer, or not at all.
        posts_words_alone: Whether a post of only words is allowed. Four of
            the nine refuse one.
        schedules: Whether `Post.publish_at` works. Six of the nine refuse.
        wants_media_url: Whether the network fetches the file itself from a
            web address instead of taking an upload.
        bites: The one thing about this network that costs people a day.
    """

    name: str
    label: str
    sign_in: str
    asks_which_account: bool
    register_app: str
    reviewed: bool
    env_vars: tuple[str, ...]
    updates: str
    posts_words_alone: bool
    schedules: bool
    wants_media_url: bool
    bites: str

    @property
    def id_var(self) -> str:
        """The environment variable holding the client id, or empty."""
        return self.env_vars[0] if self.env_vars else ""

    @property
    def secret_var(self) -> str:
        """The environment variable holding the client secret, or empty."""
        return self.env_vars[1] if self.env_vars else ""


# Sign-in shapes. There are four, and every one of the nine is one of them.
REDIRECT: Final = "redirect"
"""Send the person to the network and wait for them to come back."""

NO_SIGN_IN_PAGE: Final = "app password"
"""No sign-in page at all. Ask for a handle and an app password."""

REGISTER_FIRST: Final = "register the app first"
"""socialchimp registers your app on that server, then it is a redirect."""


NETWORKS: Final = (
    Network(
        name="mastodon",
        label="Mastodon",
        sign_in=REGISTER_FIRST,
        asks_which_account=False,
        register_app="socialchimp does it, once per server",
        reviewed=False,
        env_vars=(),
        updates="timer",
        posts_words_alone=True,
        schedules=True,
        wants_media_url=False,
        bites=(
            "Every server is separate. An app registered on mastodon.social "
            "means nothing on fosstodon.org, so create_app runs once per "
            "host and the credentials are stored per host."
        ),
    ),
    Network(
        name="bluesky",
        label="Bluesky",
        sign_in=NO_SIGN_IN_PAGE,
        asks_which_account=False,
        register_app="nothing to register",
        reviewed=False,
        env_vars=(),
        updates="timer",
        posts_words_alone=True,
        schedules=False,
        wants_media_url=False,
        bites=(
            "300 graphemes and 3,000 bytes, both enforced. A family emoji is "
            "one grapheme and eleven bytes, so the two limits catch "
            "different posts."
        ),
    ),
    Network(
        name="facebook",
        label="Facebook Page",
        sign_in=REDIRECT,
        asks_which_account=True,
        register_app="by hand at developers.facebook.com",
        reviewed=True,
        env_vars=("FACEBOOK_APP_ID", "FACEBOOK_APP_SECRET"),
        updates="pushed",
        posts_words_alone=True,
        schedules=True,
        wants_media_url=False,
        bites=(
            "It asks which Page even when there is only one, so your app has "
            "one code path instead of two."
        ),
    ),
    Network(
        name="instagram",
        label="Instagram",
        sign_in=REDIRECT,
        asks_which_account=True,
        register_app="by hand at developers.facebook.com",
        reviewed=True,
        env_vars=("INSTAGRAM_APP_ID", "INSTAGRAM_APP_SECRET"),
        updates="pushed",
        posts_words_alone=False,
        schedules=False,
        wants_media_url=True,
        bites=(
            "Instagram fetches the picture itself from a web address. A file "
            "on your disk is refused, so put it somewhere public first."
        ),
    ),
    Network(
        name="youtube",
        label="YouTube",
        sign_in=REDIRECT,
        asks_which_account=True,
        register_app="by hand in the Google Cloud console",
        reviewed=True,
        env_vars=("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
        updates="timer",
        posts_words_alone=False,
        schedules=True,
        wants_media_url=False,
        bites=(
            "title and made_for_kids are both required, and a video with no "
            "privacy_status goes up private on purpose."
        ),
    ),
    Network(
        name="tiktok",
        label="TikTok",
        sign_in=REDIRECT,
        asks_which_account=False,
        register_app="by hand at developers.tiktok.com",
        reviewed=True,
        env_vars=("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"),
        updates="pushed",
        posts_words_alone=False,
        schedules=False,
        wants_media_url=False,
        bites=(
            "Until TikTok audits your app everything it posts is SELF_ONLY, "
            "whatever privacy level you ask for, and it may post for five "
            "people a day. There is no bug to find."
        ),
    ),
    Network(
        name="threads",
        label="Threads",
        sign_in=REDIRECT,
        asks_which_account=False,
        register_app="by hand, and it has its own app id and secret",
        reviewed=True,
        env_vars=("THREADS_APP_ID", "THREADS_APP_SECRET"),
        updates="pushed",
        posts_words_alone=True,
        schedules=False,
        wants_media_url=True,
        bites=(
            "Adding the Threads use case makes a second app id and secret. "
            "Reusing the Facebook pair fails at the token swap, in a way "
            "that mentions none of this."
        ),
    ),
    Network(
        name="x",
        label="X",
        sign_in=REDIRECT,
        asks_which_account=False,
        register_app="by hand at developer.x.com, on a paid plan",
        reviewed=False,
        env_vars=("X_CLIENT_ID", "X_CLIENT_SECRET"),
        updates="timer",
        posts_words_alone=True,
        schedules=False,
        wants_media_url=False,
        bites=(
            "Ask for offline.access or tokens die two hours after they were "
            "handed out, and X sends back no refresh token at all."
        ),
    ),
    Network(
        name="pinterest",
        label="Pinterest",
        sign_in=REDIRECT,
        asks_which_account=False,
        register_app="by hand at developers.pinterest.com/apps",
        reviewed=True,
        env_vars=("PINTEREST_APP_ID", "PINTEREST_APP_SECRET"),
        updates="none",
        posts_words_alone=False,
        schedules=False,
        wants_media_url=False,
        bites=(
            "Every pin needs a board_id, and a new app is on Trial access, "
            "where real 2xx replies carry real pin ids that nobody but you "
            "can see."
        ),
    ),
)
"""Every network this app knows about, in the order the pages list them."""

BY_NAME: Final = {network.name: network for network in NETWORKS}
"""The same nine, to look one up by the name socialchimp uses."""


def network_for(name: str) -> Network:
    """Look up one network by the name socialchimp uses.

    Args:
        name: For example `"facebook"`.

    Returns:
        What we know about it.

    Raises:
        KeyError: If this app has no entry for that name. socialchimp itself
            would find a platform for anything installed, including one from
            somebody else's package - this table is only what these pages
            know how to draw.
    """
    return BY_NAME[name]
