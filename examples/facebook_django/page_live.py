"""Connect a real Facebook Page from the console, then schedule a post.

This one talks to Facebook, so it cannot run in CI. What you need first:

1. An app at https://developers.facebook.com/apps, with Facebook Login added.
2. `http://localhost:8000/callback` in that app's **Valid OAuth Redirect
   URIs**. It has to match the address below character for character.
3. Meta's app review for `pages_manage_posts`, and business verification.
   Until both are done this works only for people who have a role on the app
   in the portal, and fails for everybody else. See
   `docs/use-cases/facebook-django.md`.
4. Your own Facebook account with a Page on it.

Then:

    export FACEBOOK_APP_ID=...
    export FACEBOOK_APP_SECRET=...
    uv run python examples/facebook_django/page_live.py

It prints a link, you approve the app, Facebook sends your browser to
`http://localhost:8000/callback?code=...` - which will not load, because
nothing is listening there. That is fine: copy the whole address out of the
browser bar and paste it back here.

Nothing is stored between runs. `InMemoryStorage` forgets everything when the
program stops, which is what you want for a one-off script and wrong for
anything else.
"""

from __future__ import annotations

import asyncio
import os
import webbrowser
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlparse

from socialchimp import AppCredentials, InMemoryStorage, Post, SocialChimp
from socialchimp.platform import ChooseAccount, Finished, SendToNetwork

REDIRECT_URI = "http://localhost:8000/callback"

# Far enough ahead to clear Facebook's floor, which is ten minutes. Anything
# nearer is refused before a request is spent on it.
POST_IN = timedelta(hours=1)


def credentials() -> AppCredentials:
    """Read the app id and secret out of the environment."""
    client_id = os.environ.get("FACEBOOK_APP_ID")
    client_secret = os.environ.get("FACEBOOK_APP_SECRET")
    if not client_id or not client_secret:
        message = (
            "Set FACEBOOK_APP_ID and FACEBOOK_APP_SECRET. Both come from "
            "https://developers.facebook.com/apps, under Settings > Basic."
        )
        raise SystemExit(message)
    return AppCredentials(
        platform="facebook",
        host=None,
        client_id=client_id,
        client_secret=client_secret,
    )


def query_of(address: str) -> dict[str, str]:
    """Read the query values out of the address Facebook sent the browser to."""
    return dict(parse_qsl(urlparse(address.strip()).query))


async def main() -> None:
    """Sign in, pick a page, schedule a post on it."""
    storage = InMemoryStorage()
    # Meta will not register an app for you, so these are saved by hand,
    # once. `sc.create_app("facebook", ...)` refuses with a message saying
    # exactly this.
    await storage.save_app(credentials())

    async with SocialChimp(storage=storage) as sc:
        step = await sc.start_login("facebook", redirect_uri=REDIRECT_URI)
        if not isinstance(step, SendToNetwork):
            message = f"Expected to be sent to Facebook, got {step!r}."
            raise RuntimeError(message)

        print("Opening your browser to approve the app.")
        print(f"If it does not open, go to:\n  {step.url}\n")
        webbrowser.open(step.url)

        # input() would block the event loop, so it waits on a thread.
        came_back = await asyncio.to_thread(
            input, "Paste the whole address you were sent back to: "
        )

        step = await sc.finish_login(
            "facebook",
            callback=query_of(came_back),
            redirect_uri=REDIRECT_URI,
            state=step.state,
            remember=step.remember,
        )

        # Facebook always asks which page, even when there is only one.
        if not isinstance(step, ChooseAccount):
            message = f"Expected to be asked which page, got {step!r}."
            raise RuntimeError(message)

        print("\nPages you manage:")
        for option in step.options:
            print(f"  {option.id}  {option.name}")
        picked = (await asyncio.to_thread(input, "Which page id? ")).strip()

        step = await sc.choose(
            "facebook",
            account_id=picked,
            resume_token=step.resume_token,
            redirect_uri=REDIRECT_URI,
        )
        if not isinstance(step, Finished):
            message = f"Expected a finished connection, got {step!r}."
            raise RuntimeError(message)

        connection = step.connection
        print(f"\nConnected {connection.account_name} ({connection.id}).")
        # A page token taken from a long-lived person's token does not
        # expire, so there is usually nothing here to renew.
        print(f"Token expires at: {connection.token.expires_at}")

        account = sc.account(connection.id)
        when = datetime.now(UTC) + POST_IN
        result = await account.post(
            Post(
                text="Scheduled by socialchimp.",
                publish_at=when,
                options={"link": "https://github.com/raghulj/socialchimp"},
            )
        )
        # SCHEDULED, and no url - Facebook has taken a plan, and there is
        # nothing on the page to link to until the moment arrives.
        print(f"\n{result.state.name}: {result.id} at {when.isoformat()}")


if __name__ == "__main__":
    asyncio.run(main())
