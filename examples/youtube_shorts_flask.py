"""Publish YouTube Shorts from a Flask app.

The code behind `docs/use-cases/youtube-shorts-flask.md`. It talks to real
YouTube, so it cannot run in CI. What you need first:

1. A project at https://console.cloud.google.com with the **YouTube Data
   API v3** turned on.
2. An **OAuth client** (type: Web application) with
   `http://localhost:5000/social/callback/youtube` in its authorised
   redirect URIs. Character for character.
3. Google's review, because uploading video is a sensitive permission.
   Until it passes, only accounts added as **test users** on the consent
   screen can sign in. Everyone else is turned away by Google, not by this
   code.
4. A Google account that actually has a YouTube channel on it. A Google
   account and a channel are different things.

Then:

    export GOOGLE_CLIENT_ID=...
    export GOOGLE_CLIENT_SECRET=...
    uv run --with flask flask --app examples/youtube_shorts_flask run

Connect a channel by opening http://localhost:5000/social/connect/youtube.
YouTube asks which channel, so the callback answers with a JSON list rather
than finishing - post the id you want back to `/social/choose/youtube` as
`state` and `account_id`, and that finishes the job.

**Quota, not rate limiting.** The project gets 10,000 units a day. One upload
costs about 1,600, so roughly six a day on the default allowance. Running out
raises `RateLimitError` here, but retrying in thirty seconds is the wrong
move - the allowance resets at midnight Pacific and nothing before then
changes the answer.

Connections are kept in memory, so every restart disconnects everybody.
`examples/facebook_django/page_post_demo.py` has a storage class over a real
database; swap it in and nothing else changes.
"""

from __future__ import annotations

import os

from flask import Flask, jsonify, request
from werkzeug.wrappers.response import Response

from socialchimp import (
    AppCredentials,
    Feature,
    InMemoryStorage,
    Media,
    Post,
    SocialChimp,
)
from socialchimp.contrib.flask import blueprint, run
from socialchimp.errors import NotSupportedError, RateLimitError, SocialChimpError
from socialchimp.platforms.youtube import YouTubePlatform

PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:5000")

GOOGLE_APP = AppCredentials(
    platform="youtube",
    host=None,
    client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
)

# Built here rather than reached for through `sc.platform_for("youtube")`,
# which would hand it back typed as the `Platform` protocol - and that has no
# `check_state`, because most networks have nothing to check.
youtube = YouTubePlatform()

storage = InMemoryStorage()
sc = SocialChimp(storage=storage, platforms={"youtube": youtube})

app = Flask(__name__)
app.register_blueprint(
    blueprint(
        sc,
        redirect_uri=f"{PUBLIC_URL}/social/callback/{{platform}}",
        # YouTube pushes nothing socialchimp uses, so there is no webhook
        # secret and no setup token here. Comments are read on a timer
        # instead, through the platform's `fetch_updates`.
    ),
    url_prefix="/social",
)


@app.errorhandler(SocialChimpError)
def explain(refused: SocialChimpError) -> tuple[Response, int]:
    """Turn one of socialchimp's errors into an answer, not a traceback."""
    advice = "Fix the post and try again."
    status = 400
    if isinstance(refused, RateLimitError):
        # Google's quota arrives as a 403 that reads like a permission
        # problem, and socialchimp renames it. It is a daily allowance, so
        # there is deliberately no Retry-After on it: waiting seconds only
        # spends what is left of today.
        advice = (
            "This is YouTube's daily quota, not a request to slow down. It "
            "resets at midnight Pacific. Do not retry on a timer."
        )
        status = 429
    if isinstance(refused, NotSupportedError):
        advice = "YouTube cannot do this at all. Nothing to retry."
    return jsonify({"error": str(refused), "what_now": advice}), status


@app.post("/shorts")
def publish_a_short() -> dict[str, str | None]:
    """Upload one video, which YouTube will decide is a Short or is not.

    **There is no flag for a Short.** YouTube reads the file: a video taller
    than it is wide and under about three minutes becomes a Short, and one
    that is not, does not. Both are properties of the file, decided before
    socialchimp sees it. Putting `#Shorts` in the description is a
    convention people follow to help it along; it does not make the
    decision.
    """
    form = request.form
    post = Post(
        # `Post.text` is the **description** on YouTube. The title is its
        # own setting, below, and this is the part that catches people out.
        text=form.get("description", ""),
        media=(Media.from_file(form["path"]),),
        options={
            # Required. YouTube refuses a video with no title, and
            # socialchimp refuses it first, by name, at most 100 characters.
            "title": form["title"],
            # Required by Google for every upload, and socialchimp will not
            # guess. Getting it wrong has consequences for the channel.
            "made_for_kids": form.get("made_for_kids") == "yes",
            # Leave this out and the video goes up **private**, on purpose:
            # making somebody's video public by accident cannot be undone.
            "privacy_status": form.get("privacy_status", "private"),
        },
    )

    result = run(sc.account(form["connection_id"]).post(post))
    # PROCESSING, not DONE. Taking the bytes is not publishing - YouTube
    # encodes afterwards, and that can take minutes or hours.
    return {
        "video_id": result.id,
        "url": result.url,
        "state": result.state.name,
        "what_now": "YouTube is still encoding. Ask /shorts/<id> later.",
    }


@app.get("/shorts/<connection_id>/<video_id>")
def how_is_it_going(connection_id: str, video_id: str) -> dict[str, str | None]:
    """Ask YouTube what happened to a video.

    One unit of the daily quota, against an upload's 1,600 - so this is
    cheap enough to put on a timer, unlike the upload itself.
    """
    # `check_state` wants a connection rather than an account handle, and
    # this is the call that renews the token before handing one over.
    connection = run(sc.fresh_connection(connection_id))
    result = run(youtube.check_state(connection, video_id))
    return {"state": result.state.name, "url": result.url}


@app.get("/what-youtube-cannot-do/<connection_id>")
def what_youtube_cannot_do(connection_id: str) -> dict[str, object]:
    """Show the refusal you get for a post of words alone.

    YouTube's community posts are text, and they are in no part of its API -
    not read, not write, at no access level. So `Feature.POST_TEXT` is off,
    and socialchimp says so rather than inventing something to do with your
    words. Nothing is sent to YouTube here: the refusal happens before the
    first request, which is also why it costs no quota.
    """
    features = youtube.features
    refusal: str | None = None
    try:
        run(sc.account(connection_id).post(Post(text="Just some words")))
    except NotSupportedError as refused:
        refusal = str(refused)

    return {
        "can_post_text": Feature.POST_TEXT in features,
        "can_post_video": Feature.POST_VIDEO in features,
        "can_schedule": Feature.SCHEDULE in features,
        "refused_with": refusal,
    }


def save_the_app_credentials() -> None:
    """Write the client id and secret where every sign-in can find them."""
    if not GOOGLE_APP.client_id or not GOOGLE_APP.client_secret:
        message = (
            "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET. Both come from "
            "an OAuth client at https://console.cloud.google.com/apis/credentials."
        )
        raise SystemExit(message)
    run(storage.save_app(GOOGLE_APP))


save_the_app_credentials()
