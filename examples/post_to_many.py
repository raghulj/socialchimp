"""Send one post to several accounts, and handle the ones that fail.

The point of this example is the failure handling. Posting to five networks
means five chances to fail, and one network being down must never cost you
the four that worked.

This example does not talk to a real network. It uses the fake platform
that ships with socialchimp for testing, so it runs anywhere with no
credentials.

Run it with:

    uv run python examples/post_to_many.py
"""

import asyncio

from socialchimp import InMemoryStorage, Post, SocialChimp
from socialchimp.errors import RateLimitError
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
            job = await sc.post_to_many(
                [good.id, bad.id],
                Post(text="Hello everywhere"),
            )

            print(job)
            for result in job.succeeded:
                print(f"  posted   {result.id}")
            for failure in job.failed:
                # The error is kept, not raised, so one network being busy
                # never hides the network that worked.
                print(f"  failed   {failure.connection_id}: {failure.error}")
    finally:
        unregister_platform("working")
        unregister_platform("busy")


if __name__ == "__main__":
    asyncio.run(main())
