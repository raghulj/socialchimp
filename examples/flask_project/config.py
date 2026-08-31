"""Everything this app reads out of the environment.

One place to look when a sign-in says your app is not registered with a
network, which is the first thing that goes wrong for everybody.

Three things here are not obvious.

**Meta hands out two pairs, not one.** Facebook and Instagram share an app
id and secret. Adding the Threads use case to the same app makes a *second*
pair that sits next to the first, and they are not interchangeable. Reusing
the Facebook pair for Threads fails at the very last step of a sign-in, with
a message from Meta that mentions none of this - so they are separate
settings here, and they are saved under separate platform names.

**Bluesky needs nothing here.** It has no developer portal and no app to
register, and it says so with `Feature.NEEDS_NO_APP`, so socialchimp does
not ask storage for credentials it could never have. A person signs in with
their handle and an app password instead.

**A webhook secret is not a verify token.** Meta signs the requests it
pushes with your *app secret* - the same value already in this file - and
separately asks you to invent a *verify token* and type it into its
dashboard, which it quotes back once when you first point it at a URL. Two
different values doing two different jobs, and mixing them up is the usual
reason a webhook cannot be set up.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from socialchimp import AppCredentials

from .networks import NETWORKS

DEV_COOKIE_PHRASE: Final = "development-only-change-me"
"""What Flask signs session cookies with when nothing is set.

Fine on your laptop. In production set `FLASK_SECRET_KEY`, because anybody
who knows this value can forge a session cookie.
"""

# The networks that push updates to a URL of yours. Meta's three sign with
# the app secret; TikTok signs with its client secret. Both are values this
# file already holds, so there is nothing extra to set.
PUSHES_TO_US: Final = ("facebook", "instagram", "threads", "tiktok")

# The networks that ask a question before they will push anything. Only
# Meta's three do this; TikTok starts sending straight away.
ASKS_FIRST: Final = ("facebook", "instagram", "threads")


@dataclass(frozen=True, slots=True)
class Settings:
    """What this app was told, all read at startup and never again.

    Attributes:
        database: Where the sqlite file lives.
        public_url: The address networks and browsers reach this app on.
            Not `localhost` once you are past your own machine - a network
            has to be able to reach the callback and the webhook.
        cookie_phrase: What Flask signs session cookies with.
        mastodon_app_name: The name people see on the Mastodon approval
            page, used when socialchimp registers the app for you.
        apps: Your app's credentials for each network, by network name.
            A network you have set nothing for is simply absent, and its
            sign-in says so plainly instead of half-working.
        setup_tokens: The verify token each network quotes back during its
            one-off setup check, by network name.
    """

    database: Path
    public_url: str
    cookie_phrase: str
    mastodon_app_name: str
    apps: dict[str, AppCredentials]
    setup_tokens: dict[str, str]

    @classmethod
    def from_environment(cls) -> Settings:
        """Read the settings out of the environment.

        Anything missing is left out rather than guessed at, so a half-set
        network fails at the sign-in with a message naming it, instead of
        sending a blank client id to a network and reading its reply.

        Returns:
            The settings.
        """
        apps: dict[str, AppCredentials] = {}
        for network in NETWORKS:
            if not network.id_var:
                # Mastodon is registered for us; Bluesky has nothing to
                # register. Neither reads an id out of the environment.
                continue
            client_id = os.environ.get(network.id_var, "")
            client_secret = os.environ.get(network.secret_var, "")
            if client_id and client_secret:
                apps[network.name] = AppCredentials(
                    platform=network.name,
                    host=None,
                    client_id=client_id,
                    client_secret=client_secret,
                )

        return cls(
            database=Path(
                os.environ.get("SOCIALCHIMP_DATABASE", "flask_project.sqlite3")
            ),
            public_url=os.environ.get("PUBLIC_URL", "http://localhost:5000"),
            cookie_phrase=os.environ.get("FLASK_SECRET_KEY", DEV_COOKIE_PHRASE),
            mastodon_app_name=os.environ.get(
                "MASTODON_APP_NAME", "socialchimp Flask example"
            ),
            apps=apps,
            setup_tokens={
                name: os.environ[f"{name.upper()}_VERIFY_TOKEN"]
                for name in ASKS_FIRST
                if f"{name.upper()}_VERIFY_TOKEN" in os.environ
            },
        )

    def redirect_uri(self, platform: str) -> str:
        """Where this network should send people back to.

        It has to match what the network's developer portal has on file,
        character for character, including the scheme and any trailing
        slash. A mismatch is refused by the network before your code runs.

        Args:
            platform: Which network, for example `"facebook"`.

        Returns:
            The address.
        """
        return f"{self.public_url}/sign-in/callback/{platform}"

    def webhook_secret(self, platform: str) -> str | None:
        """The secret one network signs its pushed requests with.

        Not a separate setting: Meta signs with the app secret and TikTok
        with its client secret, both of which are already in `apps`.

        Args:
            platform: Which network.

        Returns:
            The secret, or `None` if this app has no credentials for that
            network yet.
        """
        app = self.apps.get(platform)
        return None if app is None else app.client_secret
