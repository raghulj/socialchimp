"""The list of connected accounts, and what this app knows about each.

Nothing on this page goes near a network except `/limits`, which is here
because it is the one call that shows the two things people conflate.
`Feature` is fixed - a network either can schedule or it cannot, and that
never changes while the process runs. `Limits` is looked up while running,
because it genuinely moves: a Mastodon server's post length is whatever its
administrator set, and Instagram counts down how many posts are left today.
"""

from __future__ import annotations

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    url_for,
)
from flask.typing import ResponseReturnValue

from socialchimp import Feature, SocialChimpError
from socialchimp.contrib.flask import run

from ..db import recent_activity
from ..networks import BY_NAME
from ..runtime import chimp

bp = Blueprint("connections", __name__)


@bp.get("/")
def index() -> ResponseReturnValue:
    """Show every connected account and the last few things that happened."""
    here = chimp()
    return render_template(
        "connections.html",
        connections=here.store.everything(),
        networks=BY_NAME,
        activity=recent_activity(here.settings.database),
    )


@bp.post("/connections/<path:connection_id>/disconnect")
def disconnect(connection_id: str) -> ResponseReturnValue:
    """Forget one account.

    Straight to this app's own storage class rather than through
    socialchimp, because the row is ours and there is nothing to tell a
    network - the person revokes your app on the network's own settings
    page, not here. socialchimp calls the same method when a webhook says
    somebody has taken your app's access away.
    """
    here = chimp()
    here.store.delete_connection(connection_id)
    flash(f"Forgot {connection_id}.", "note")
    return redirect(url_for("connections.index"))


@bp.get("/connections/<path:connection_id>/limits")
def limits(connection_id: str) -> ResponseReturnValue:
    """Say what this network can do, and what it currently allows.

    Answers JSON because it is a diagnostic rather than a page. Worth
    reading before you build a character counter: `text_counted_in` says how
    that network counts, and three of the nine do not count characters.
    """
    here = chimp()
    try:
        connection = run(here.sc.fresh_connection(connection_id))
        # This call renews the token first if it is running out, and writes
        # the new one back through `save_connection` - which is why that
        # method has to replace rather than insert.
        allowed = run(here.sc.account(connection_id).limits())
    except SocialChimpError as refused:
        return {"error": str(refused), "kind": type(refused).__name__}, 400

    features = here.sc.platform_for(connection.platform).features
    return {
        "connection_id": connection.id,
        "platform": connection.platform,
        "account_name": connection.account_name,
        "can": sorted(one.name for one in Feature if one in features),
        "limits": {
            "max_text_length": allowed.max_text_length,
            "max_text_bytes": allowed.max_text_bytes,
            "text_counted_in": allowed.text_counted_in.name,
            "max_images": allowed.max_images,
            "max_videos": allowed.max_videos,
            "max_title_length": allowed.max_title_length,
            "posts_left_today": allowed.posts_left_today,
        },
    }
