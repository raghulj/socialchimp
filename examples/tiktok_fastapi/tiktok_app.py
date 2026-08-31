"""A creator tool that uploads video to TikTok, as a FastAPI backend.

The code behind `docs/use-cases/tiktok-fastapi.md`. It talks to real TikTok,
so it cannot run in CI. What you need first:

1. An app at https://developers.tiktok.com/ with **Login Kit** and the
   **Content Posting API** added to it.
2. `https://your-address/social/callback/tiktok` in that app's redirect
   addresses. TikTok will not take `http://localhost`, so use a tunnel and
   put the tunnel's address in both places.
3. TikTok's **audit**, if you ever want a post to be visible to anybody but
   its author. Until the audit passes, everything this uploads is forced to
   `SELF_ONLY` no matter what it asks for, and the app may post for at most
   five people in any 24 hours. TikTok does not warn you: the post
   succeeds and nobody can see it. Read the use case before you promise
   anyone a public video.
4. `video.publish` on top of `video.upload`, if you want to post straight to
   a profile rather than to somebody's drafts.

Then:

    export TIKTOK_CLIENT_KEY=...
    export TIKTOK_CLIENT_SECRET=...
    export PUBLIC_URL=https://your-tunnel.example
    uv run --with uvicorn uvicorn tiktok_app:app --reload \
        --app-dir examples/tiktok_fastapi

Connections are kept in memory here, so every restart disconnects everybody.
`examples/facebook_django/page_post_demo.py` has a storage class over a real
database; swap it in and nothing else changes.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel

from socialchimp import (
    AppCredentials,
    Dispatcher,
    InMemorySeenUpdates,
    InMemoryStorage,
    Media,
    Post,
    PostState,
    SocialChimp,
    Update,
    UpdateKind,
)
from socialchimp.contrib.fastapi import router
from socialchimp.errors import SocialChimpError
from socialchimp.platforms.tiktok import TikTokPlatform

CLIENT_KEY = os.environ.get("TIKTOK_CLIENT_KEY", "")
CLIENT_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8000")

# TikTok calls them the client key and the client secret. The client key is
# what goes in `client_id` - there is no third value.
TIKTOK_APP = AppCredentials(
    platform="tiktok",
    host=None,
    client_id=CLIENT_KEY,
    client_secret=CLIENT_SECRET,
)


# ---------------------------------------------------------------------------
# The client.
#
# The platform is built here and passed in only because this one takes
# settings of its own in production - the chunk size and a longer timeout.
# Everything an app calls is on `sc` and on `sc.account(...)`.
# ---------------------------------------------------------------------------

storage = InMemoryStorage()
sc = SocialChimp(storage=storage, platforms={"tiktok": TikTokPlatform()})

# TikTok delivers at least once and keeps retrying for 72 hours, so the same
# message arriving twice is normal rather than a fault. A memory of what has
# been handled turns the second copy into nothing.
dispatcher = Dispatcher(seen=InMemorySeenUpdates())


async def video_is_live(update: Update) -> None:
    """TikTok finished with a post and it is public now."""
    print(f"live: {update.connection_id} {update.raw}")


async def video_is_in_the_drafts(update: Update) -> None:
    """TikTok put the video in somebody's drafts. Nothing else will happen."""
    print(f"drafted: {update.connection_id} - waiting for them to open the app")


async def video_failed(update: Update) -> None:
    """TikTok gave up on a post, usually at moderation."""
    print(f"failed: {update.connection_id} {update.raw}")


async def app_was_removed(update: Update) -> None:
    """Somebody took your app's access away. The token is already dead."""
    await storage.delete_connection(update.connection_id)


dispatcher.on(UpdateKind.POST_PUBLISHED, video_is_live)
dispatcher.on(UpdateKind.POST_DRAFTED, video_is_in_the_drafts)
dispatcher.on(UpdateKind.POST_FAILED, video_failed)
dispatcher.on(UpdateKind.CONNECTION_REVOKED, app_was_removed)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Save the client key and secret, and close what socialchimp opened."""
    # Every sign-in reads these out of storage, so they have to be there
    # before the first person is sent to TikTok.
    await storage.save_app(TIKTOK_APP)
    yield
    await sc.aclose()


app = FastAPI(title="TikTok uploader", lifespan=lifespan)


app.include_router(
    router(
        sc,
        redirect_uri=f"{PUBLIC_URL}/social/callback/{{platform}}",
        scopes={"tiktok": ["user.info.basic", "video.upload", "video.publish"]},
        # TikTok signs a pushed message with your client secret, not with a
        # separate webhook secret. Point its dashboard at
        # {PUBLIC_URL}/social/webhooks/tiktok.
        secrets={"tiktok": CLIENT_SECRET},
        deliver=dispatcher.deliver,
    ),
    prefix="/social",
)


class Upload(BaseModel):
    """What the front end asks us to upload."""

    connection_id: str
    path: str
    caption: str = ""
    # "drafts" puts the video in the person's TikTok inbox for them to
    # finish; "profile" posts it straight to their profile and needs the
    # video.publish permission. socialchimp defaults to the drafts.
    send_to: str = "drafts"
    privacy_level: str = "SELF_ONLY"


@app.post("/uploads")
async def upload(body: Annotated[Upload, Body()]) -> dict[str, str | None]:
    """Send one video to TikTok.

    `Media.from_file` does not read the file here. socialchimp asks for it a
    piece at a time while the upload runs, so a four gigabyte video costs
    one piece of memory rather than four gigabytes.
    """
    options: dict[str, object] = {"send_to": body.send_to}
    caption = body.caption
    if body.send_to == "profile":
        # Only a profile post carries any of this. TikTok's drafts take the
        # file and nothing else, and socialchimp refuses a caption sent
        # there rather than letting it disappear.
        options["privacy_level"] = body.privacy_level
    else:
        caption = ""

    post = Post(
        text=caption,
        media=(Media.from_file(body.path),),
        options=options,
    )

    try:
        result = await sc.account(body.connection_id).post(post)
    except SocialChimpError as refused:
        raise HTTPException(status_code=400, detail=str(refused)) from refused

    return {
        "publish_id": result.id,
        # Never DONE. Taking the bytes is not publishing.
        "state": result.state.name,
        "what_now": _what_now(result.state),
    }


@app.get("/uploads/{connection_id}/{publish_id}")
async def how_is_it_going(connection_id: str, publish_id: str) -> dict[str, str | None]:
    """Ask TikTok what happened to a post. Thirty times a minute, per person.

    Do not put this on a timer for a post that came back
    `WAITING_FOR_PERSON`. That one is finished as far as TikTok is
    concerned: the video is in somebody's drafts, and it changes when they
    open the app and not before.
    """
    # The token is renewed first, the same as every other call on an
    # account, so this is safe to leave on a timer.
    result = await sc.account(connection_id).check_state(publish_id)
    return {
        "state": result.state.name,
        "url": result.url,
        "what_now": _what_now(result.state),
    }


def _what_now(state: PostState) -> str:
    """Say in plain words what the app should do next about a post."""
    if state is PostState.WAITING_FOR_PERSON:
        return (
            "It is in their TikTok drafts. Nothing more happens until they "
            "open the app, so stop checking and tell them instead."
        )
    if state is PostState.PROCESSING:
        return "TikTok is still encoding and moderating. Check again shortly."
    if state is PostState.FAILED:
        return "TikTok gave up on it. The reason is in the raw reply."
    return "Live."
