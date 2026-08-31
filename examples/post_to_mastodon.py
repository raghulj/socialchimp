"""Register an app on a Mastodon server, sign someone in, and post.

Mastodon is the only network where socialchimp can register your app for
you, so this runs against a real server with no developer portal and no
waiting for approval.

Run it with:

    uv run python examples/post_to_mastodon.py

It prints a link to open in your browser, then asks for the code you are
sent back with.
"""

import asyncio
import webbrowser

from socialchimp import (
    InMemoryStorage,
    Post,
    SocialChimp,
)
from socialchimp.platform import SendToNetwork

# Change this to any Mastodon server you have an account on.
SERVER = "mastodon.social"

# Mastodon sends people back here after they approve the app. For a real app
# this is a page you serve; for this example we read the code by hand, so
# anything the server accepts will do.
REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"


async def main() -> None:
    """Walk through registering, signing in, and posting."""
    # In a real app this is your database. See docs/getting-started.md.
    storage = InMemoryStorage()

    async with SocialChimp(storage=storage) as sc:
        # Registering the app is a one-off per server. Every Mastodon server
        # is separate, so an app made here means nothing on another server.
        app = await sc.create_app(
            "mastodon",
            host=SERVER,
            name="socialchimp example",
            redirect_uri=REDIRECT_URI,
        )
        print(f"Registered an app on {SERVER} (client id {app.client_id}).")

        step = await sc.start_login(
            "mastodon",
            host=SERVER,
            redirect_uri=REDIRECT_URI,
        )
        if not isinstance(step, SendToNetwork):
            message = f"Expected to be sent to {SERVER}, got {step!r}."
            raise RuntimeError(message)

        print("\nOpening your browser to approve the app.")
        print(f"If it does not open, go to:\n  {step.url}\n")
        webbrowser.open(step.url)

        # input() would block the event loop, so it waits on a thread.
        answer = await asyncio.to_thread(input, "Paste the code Mastodon gave you: ")
        code = answer.strip()

        # step.remember carries a secret from the first half of signing in.
        # A real app keeps it with the person's session, because the two
        # halves can happen in different web workers.
        finished = await sc.finish_login(
            "mastodon",
            host=SERVER,
            redirect_uri=REDIRECT_URI,
            callback={"code": code},
            remember=step.remember,
        )
        connection = getattr(finished, "connection", None)
        if connection is None:
            message = f"Signing in did not finish: {finished!r}."
            raise RuntimeError(message)

        print(f"\nConnected as {connection.account_name}.")

        account = sc.account(connection.id)

        # Limits are read from the server, not written into the code: the
        # person running this server decides how long a post may be.
        limits = await account.limits()
        print(f"This server allows {limits.max_text_length} characters.")

        result = await account.post(Post(text="Posted with socialchimp."))
        print(f"\nPosted: {result.url}")


if __name__ == "__main__":
    asyncio.run(main())
