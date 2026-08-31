"""Writing one post and sending it to as many accounts as were ticked.

**The loop is this app's, on purpose.** socialchimp posts as one account at
a time and there is no call that spans several. That is not a gap: carrying
on after a failure is a decision, and only an app knows whether TikTok
refusing should stop the Facebook post too, whether this belongs in a row
for a worker to retry at midnight, or whether somebody needs telling
tonight. The loop below chooses to carry on and to write every outcome down.
Delete the `try` and it chooses to stop at the first failure. Both are one
line of difference.

What socialchimp does give you is one set of errors to catch.
`SocialChimpError` is the base of all of them, so this loop handles nine
networks without knowing anything about nine networks' error formats.
"""

from __future__ import annotations

from flask import Blueprint, render_template, request
from flask.typing import ResponseReturnValue

from socialchimp import (
    InvalidPostError,
    NotSupportedError,
    PostState,
    RateLimitError,
    SocialChimpError,
    TextCount,
    measure_text,
)
from socialchimp.contrib.flask import run

from ..db import write_activity
from ..networks import BY_NAME
from ..posts import Composition, Outcome, build_post, read_time
from ..runtime import Chimp, chimp

bp = Blueprint("posting", __name__)

WHAT_NOW = {
    PostState.DONE: "Live now.",
    PostState.SCHEDULED: (
        "The network took it and will publish it later. There is usually no "
        "link yet, because there is nothing on the page to link to."
    ),
    PostState.PROCESSING: (
        "Still encoding. Ask again later with check_state - do not assume "
        "this one worked."
    ),
    PostState.WAITING_FOR_PERSON: (
        "The network has finished and a person has to tap a button. Stop "
        "checking and tell them instead."
    ),
    PostState.FAILED: "The network gave up on it. Look at result.raw.",
}
"""What each of the five states means for the app. `DONE` is one of five."""


def _composition() -> Composition:
    """Read the compose form.

    Returns:
        What was asked for, before any network has seen it.
    """
    form = request.form
    return Composition(
        text=form.get("text", ""),
        media_url=form.get("media_url", "").strip(),
        media_path=form.get("media_path", "").strip(),
        publish_at=read_time(form.get("publish_at", "")),
        title=form.get("title", "").strip(),
        made_for_kids=form.get("made_for_kids") == "yes",
        privacy_status=form.get("privacy_status", "").strip(),
        board_id=form.get("board_id", "").strip(),
        link=form.get("link", "").strip(),
        visibility=form.get("visibility", "").strip(),
        language=form.get("language", "").strip(),
        langs=tuple(one for one in form.get("langs", "").split(",") if one),
        send_to=form.get("send_to", "drafts"),
        reply_settings=form.get("reply_settings", "").strip(),
    )


def _send(here: Chimp, connection_id: str, asked: Composition) -> Outcome:
    """Send one post to one account, and say what happened.

    Every path out of here is an `Outcome` and a row in `activity`. Nothing
    raises past this function, which is what lets the loop decide to carry
    on.

    Args:
        here: This app's state.
        connection_id: Which account.
        asked: What the form said.

    Returns:
        What happened.
    """
    connection = here.store.get_connection(connection_id)
    if connection is None:
        return Outcome(
            connection_id=connection_id,
            platform="?",
            account_name=connection_id,
            state="refused",
            detail="There is no connection with that id any more.",
            error_kind="Missing",
        )

    platform = connection.platform
    try:
        post = build_post(platform, asked)
    except ValueError as empty:
        # `Post` refuses one with neither words nor a file. TikTok drafts
        # reach this when nothing was attached, because the words are
        # dropped for them - see posts.build_post.
        return _refused(
            here,
            connection_id,
            platform,
            connection.account_name,
            str(empty),
            "ValueError",
        )

    try:
        result = run(here.sc.account(connection_id).post(post))
    except SocialChimpError as refused:
        # One base class, nine networks. Catching the subclasses separately
        # is only worth doing where the app would act differently, and here
        # the difference is what the page tells somebody to do next.
        return _refused(
            here,
            connection_id,
            platform,
            connection.account_name,
            f"{refused} {_advice(refused)}".strip(),
            type(refused).__name__,
        )

    write_activity(
        here.settings.database,
        what="posted",
        platform=platform,
        connection_id=connection_id,
        detail=f"{result.state.name}: {WHAT_NOW[result.state]}",
        link=result.url,
    )
    return Outcome(
        connection_id=connection_id,
        platform=platform,
        account_name=connection.account_name,
        state=result.state.name,
        detail=WHAT_NOW[result.state],
        link=result.url,
    )


def _advice(refused: SocialChimpError) -> str:
    """Say whether this failure is worth doing anything about.

    Args:
        refused: What socialchimp raised.

    Returns:
        A sentence, or an empty string where there is nothing to add.
    """
    if isinstance(refused, NotSupportedError):
        return "This network never can. Nothing to retry."
    if isinstance(refused, InvalidPostError):
        return "Change the post. Retrying it unchanged will fail the same way."
    if isinstance(refused, RateLimitError):
        if refused.retry_after is None:
            # YouTube's quota and TikTok's daily cap both arrive here with
            # nothing to wait for, because only tomorrow helps.
            return "A daily allowance, not a request to slow down. Not tonight."
        return f"Wait {refused.retry_after:.0f} seconds and try again."
    return ""


def _refused(
    here: Chimp,
    connection_id: str,
    platform: str,
    account_name: str,
    detail: str,
    kind: str,
) -> Outcome:
    """Write a refusal down and describe it.

    Args:
        here: This app's state.
        connection_id: Which account.
        platform: Which network.
        account_name: What to show a person.
        detail: The refusal, in the network's own words.
        kind: The class name of the error.

    Returns:
        The outcome to show.
    """
    write_activity(
        here.settings.database,
        what="refused",
        platform=platform,
        connection_id=connection_id,
        detail=detail,
    )
    return Outcome(
        connection_id=connection_id,
        platform=platform,
        account_name=account_name,
        state="refused",
        detail=detail,
        error_kind=kind,
    )


@bp.route("/compose", methods=["GET", "POST"])
def compose() -> ResponseReturnValue:
    """Show the compose form, and on a POST send what it says."""
    here = chimp()
    outcomes: list[Outcome] = []

    if request.method == "POST":
        asked = _composition()
        for connection_id in request.form.getlist("connection_id"):
            # The app's own loop. It carries on after a failure because
            # `_send` never raises - take that away and one refusal stops
            # every account after it in this list.
            outcomes.append(_send(here, connection_id, asked))

    return render_template(
        "compose.html",
        connections=here.store.everything(),
        networks=BY_NAME,
        outcomes=outcomes,
    )


@bp.get("/posts/<path:connection_id>/<post_id>/state")
def state(connection_id: str, post_id: str) -> ResponseReturnValue:
    """Ask a network what happened to a post it was still working on.

    Only YouTube and TikTok have this - everywhere else the post is
    finished by the time `post()` returns, and socialchimp refuses by name
    rather than inventing an answer.

    A TikTok draft is the one not to poll: `WAITING_FOR_PERSON` means the
    network has done everything it is ever going to do.
    """
    here = chimp()
    try:
        result = run(here.sc.account(connection_id).check_state(post_id))
    except SocialChimpError as refused:
        return {"error": str(refused), "kind": type(refused).__name__}, 400

    return {
        "id": result.id,
        "state": result.state.name,
        "url": result.url,
        "what_now": WHAT_NOW[result.state],
    }


@bp.get("/how-long")
def how_long() -> ResponseReturnValue:
    """Measure the same words the way each of the networks counts them.

    Nearly every network says "300 characters" and means something else by
    it, and the difference only shows up the day somebody posts an emoji.
    Try it with a family emoji. That is one grapheme, seven characters,
    eleven UTF-16 units and twenty-five UTF-8 bytes - four answers to the
    same question, and four networks that would each give a different one.
    Bluesky enforces 300 graphemes *and* 3,000 bytes, so its two limits
    catch different posts; Threads' 500 is bytes, not characters; TikTok's
    2,200 is UTF-16 units, so 1,101 thumbs-up is over the line.

    socialchimp counts the way each network does and refuses an over-long
    post before spending a request, so this is only here for building a
    character counter of your own - `limits().text_counted_in` says which
    of these to use.
    """
    words = request.args.get("text", "")
    return {
        "text": words,
        "counted": {
            counting.name: measure_text(words, counting) for counting in TextCount
        },
        # Read off each platform file's own `Limits`, not off anybody's
        # documentation. Four of the nine do not count characters.
        "who_counts_which": {
            "CHARACTERS": ["mastodon", "facebook", "instagram", "pinterest"],
            "GRAPHEMES": ["bluesky (300, and 3,000 UTF8_BYTES as well)"],
            "UTF8_BYTES": ["threads (500)", "youtube description (5,000)"],
            "UTF16_UNITS": ["x (280)", "tiktok (2,200)"],
        },
    }
