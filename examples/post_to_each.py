"""Post to several accounts by looping, and decide what a failure means.

socialchimp posts as one account at a time. If that account fails it raises,
and the loop over your accounts is yours to write - because only your app
knows whether one network refusing should stop the others, and where the
failure should be written down.

This app decides to carry on: it catches `SocialChimpError` per account, keeps
the failure in a list, and reports both lists at the end. An app that wanted
the opposite would let the error out of the loop and stop.

The failure handling is the point. Posting to five networks means five chances
to fail, and one network being down must never cost you the four that worked -
but that is a decision, and it is yours rather than ours.

This example does not talk to a real network. It uses the fake platform
that ships with socialchimp for testing, so it runs anywhere with no
credentials.

Run it with:

    uv run python examples/post_to_each.py
"""

import asyncio

from socialchimp import InMemoryStorage, Post, PostResult, SocialChimp
from socialchimp.errors import RateLimitError, SocialChimpError
from socialchimp.registry import register_platform, unregister_platform
from socialchimp.testing import FakePlatform


class WorkingNetwork(FakePlatform):
    """A network that accepts everything."""

    def __init__(self) -> None:
        """Accept every post."""
        super().__init__(name="working")


class BusyNetwork(FakePlatform):
    """A network that is refusing posts right now."""

    def __init__(self) -> None:
        """Refuse every post, the way a network over its limit would."""
        super().__init__(
            name="busy",
            publish_fails_with=RateLimitError(
                "Too many posts for now.", retry_after=60.0
            ),
        )


async def main() -> None:
    """Post to two networks, one of which is refusing, and report both."""
    register_platform("working", WorkingNetwork)
    register_platform("busy", BusyNetwork)
    try:
        storage = InMemoryStorage()
        good = WorkingNetwork().connection(connection_id="good")
        bad = BusyNetwork().connection(connection_id="bad")
        await storage.save_connection(good)
        await storage.save_connection(bad)

        async with SocialChimp(storage=storage) as sc:
            post = Post(text="Hello everywhere")
            posted: list[PostResult] = []
            failed: list[tuple[str, SocialChimpError]] = []

            # The loop is the app's. So is the try, and so is what goes in
            # these two lists - a real app would more likely write the
            # failure against a row in its own database and retry it later.
            for connection_id in (good.id, bad.id):
                try:
                    posted.append(await sc.account(connection_id).post(post))
                except SocialChimpError as refused:
                    # Carrying on is this app's choice. Re-raising here would
                    # stop at the first network that refused, and for some
                    # apps that is the right answer.
                    failed.append((connection_id, refused))

            print(f"{len(posted)} posted, {len(failed)} failed")
            for result in posted:
                print(f"  posted   {result.id}")
            for connection_id, error in failed:
                print(f"  failed   {connection_id}: {error}")
    finally:
        unregister_platform("working")
        unregister_platform("busy")


if __name__ == "__main__":
    asyncio.run(main())
