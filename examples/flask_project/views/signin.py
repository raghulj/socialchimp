"""Signing somebody in, in all four of the shapes the nine networks use.

This is where people get stuck, so every branch is here rather than only the
common one. `start_login`, `finish_login` and `choose` each hand back a
`LoginStep`, which is one of four things, and an app that handles all four
works with every network including the ones it has not added yet.

    SendToNetwork   send them to the network and wait  (six of the nine)
    AskForDetails   there is nowhere to send them      (Bluesky)
    ChooseAccount   which Page, channel, account?      (Facebook, Instagram,
                                                        YouTube)
    Finished        done, and already saved for you

Mastodon needs one thing before any of that: an app registered on that
person's server. socialchimp does it, once per server, and there is no
portal and no waiting for approval.

**Why `run(...)` and not `asyncio.run(...)`.** Flask serves each request on
a thread with no event loop, and socialchimp is async. `asyncio.run` per
request would build a loop, do the work and throw the loop away - and the
HTTP connections socialchimp pooled belong to that loop, so the next request
finds a pool full of sockets from a loop that no longer exists.
`socialchimp.contrib.flask.run` hands the work to one loop that lives on one
background thread for the whole process, which is the same loop the
ready-made blueprint uses. Views share the pool instead of throwing it away
every request.
"""

from __future__ import annotations

import secrets

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue

from socialchimp import RawData, SocialChimpError
from socialchimp.contrib.flask import run
from socialchimp.platform import (
    AskForDetails,
    ChooseAccount,
    LoginStep,
    SendToNetwork,
)

from ..networks import NETWORKS, REGISTER_FIRST, network_for
from ..runtime import Chimp, chimp

bp = Blueprint("signin", __name__, url_prefix="/sign-in")

STATE_KEY = "sign_in_state"
"""What the browser's cookie holds.

The state, and nothing else. Everything the state stands for - the secret
half of a PKCE pair, the resume token that can carry an access token - stays
in the `login_note` table, because a Flask session is a signed cookie and
signed means unforgeable, not unreadable.
"""


def _kept_state() -> str | None:
    """Read the state this browser is part way through a sign-in with.

    Returns:
        The state, or `None` if there is not one.
    """
    found = session.get(STATE_KEY)
    return found if isinstance(found, str) else None


def _register_on_this_server(here: Chimp, host: str) -> None:
    """Make sure this app exists on one Mastodon server.

    Every Mastodon server is separate, so an app registered on
    mastodon.social means nothing on fosstodon.org. `create_app` saves what
    the server hands back through `Storage.save_app`, keyed by platform
    *and* host, so this only ever happens once per server - registering
    again would waste a record on somebody else's machine and hand us a
    different id and secret for no reason.

    Args:
        here: This app's state.
        host: The server, such as `mastodon.social`.
    """
    if here.store.get_app("mastodon", host) is not None:
        return

    run(
        here.sc.create_app(
            "mastodon",
            host=host,
            name=here.settings.mastodon_app_name,
            redirect_uri=here.settings.redirect_uri("mastodon"),
        )
    )
    flash(f"Registered this app on {host}. That only happens once.", "note")


@bp.get("/")
def index() -> ResponseReturnValue:
    """Show the nine networks and what each one needs first."""
    here = chimp()
    # A network with no credentials stored cannot sign anybody in, and
    # saying so on this page is friendlier than a ConfigError after a click.
    ready = {
        network.name
        for network in NETWORKS
        if here.store.get_app(network.name, None) is not None
    }
    return render_template(
        "connect.html",
        networks=NETWORKS,
        ready=ready,
        register_first=REGISTER_FIRST,
    )


@bp.get("/start/<platform>")
def start(platform: str) -> ResponseReturnValue:
    """Begin signing somebody in to one network."""
    here = chimp()
    try:
        network = network_for(platform)
    except KeyError:
        flash(f"This app knows nothing about {platform!r}.", "bad")
        return redirect(url_for("signin.index"))

    host = request.args.get("host") or None
    if network.sign_in == REGISTER_FIRST and not host:
        flash("Mastodon needs a server, such as mastodon.social.", "bad")
        return redirect(url_for("signin.index"))

    # Our own state, so the callback can tell which of our users came back.
    # Left out, socialchimp makes one; choosing it means we recognise it.
    state = secrets.token_urlsafe(16)

    try:
        if host is not None:
            _register_on_this_server(here, host)
        step = run(
            here.sc.start_login(
                platform,
                redirect_uri=here.settings.redirect_uri(platform),
                host=host,
                state=state,
            )
        )
    except SocialChimpError as refused:
        # socialchimp raises and this app decides what that means. Here it
        # is a message on the page the person is already looking at.
        flash(f"{network.label} would not start a sign-in: {refused}", "bad")
        return redirect(url_for("signin.index"))

    return _next(here, platform, host=host, state=state, step=step)


@bp.route("/callback/<platform>", methods=["GET", "POST"])
def callback(platform: str) -> ResponseReturnValue:
    """Carry on after the network sends the person back to us."""
    here = chimp()
    came_back = request.values.to_dict()

    # The state that came back through the network is the one to trust for
    # looking things up, and comparing it against the one in this browser's
    # cookie is what stops somebody feeding us a callback of their own.
    state = came_back.get("state") or _kept_state()
    if state is None or state != _kept_state():
        flash("That sign-in does not match this browser. Start again.", "bad")
        return redirect(url_for("signin.index"))

    note = here.notes.look_up(state)
    if note is None:
        flash("That sign-in has expired. Start again.", "bad")
        return redirect(url_for("signin.index"))

    try:
        step = run(
            here.sc.finish_login(
                platform,
                callback=came_back,
                # All three halves of a sign-in have to agree on these.
                redirect_uri=here.settings.redirect_uri(platform),
                host=note.host,
                state=state,
                remember=note.remember,
            )
        )
    except SocialChimpError as refused:
        flash(f"{platform} refused the sign-in: {refused}", "bad")
        return redirect(url_for("signin.index"))

    return _next(here, platform, host=note.host, state=state, step=step)


@bp.post("/details/<platform>")
def details(platform: str) -> ResponseReturnValue:
    """Finish a sign-in for a network that had nowhere to send anybody.

    Bluesky is the one here. What the person typed goes straight to
    `finish_login` as `callback`, keyed by the names `AskForDetails` gave.

    **Nothing in this form is logged.** One of the fields is marked
    `secret`, and an app password is as good as a password for everything
    the API can do. If your app logs request forms - plenty do, by accident
    - this is the route to leave out.
    """
    here = chimp()
    state = _kept_state()
    if state is None:
        flash("That sign-in has expired. Start again.", "bad")
        return redirect(url_for("signin.index"))

    note = here.notes.look_up(state)
    if note is None:
        flash("That sign-in has expired. Start again.", "bad")
        return redirect(url_for("signin.index"))

    typed: dict[str, str] = request.form.to_dict()
    try:
        step = run(
            here.sc.finish_login(
                platform,
                callback=typed,
                # Bluesky never sends anybody anywhere, so this is never
                # used. It is still required, and it still has to match.
                redirect_uri=here.settings.redirect_uri(platform),
                host=note.host,
                state=state,
                remember=note.remember,
            )
        )
    except SocialChimpError as refused:
        # The message is safe to show; the app password is not in it.
        flash(f"{platform} refused those details: {refused}", "bad")
        return redirect(url_for("signin.index"))

    return _next(here, platform, host=note.host, state=state, step=step)


@bp.post("/choose/<platform>")
def choose(platform: str) -> ResponseReturnValue:
    """Carry on after the person picked which Page, channel or account.

    The third request of three. The resume token comes out of our own table
    rather than out of the form, because on Facebook it carries the
    person's own access token - Facebook's code can only be swapped once,
    and that swap already happened, before they had picked.
    """
    here = chimp()
    state = _kept_state()
    note = None if state is None else here.notes.look_up(state)
    if state is None or note is None or note.resume_token is None:
        flash("That sign-in has expired. Start again.", "bad")
        return redirect(url_for("signin.index"))

    account_id = request.form.get("account_id", "")
    if not account_id:
        flash("Pick one of the accounts.", "bad")
        return redirect(url_for("signin.index"))

    try:
        step = run(
            here.sc.choose(
                platform,
                account_id=account_id,
                resume_token=note.resume_token,
                redirect_uri=here.settings.redirect_uri(platform),
                host=note.host,
                state=state,
                remember=note.remember,
            )
        )
    except SocialChimpError as refused:
        flash(f"{platform} refused that account: {refused}", "bad")
        return redirect(url_for("signin.index"))

    return _next(here, platform, host=note.host, state=state, step=step)


def _next(
    here: Chimp,
    platform: str,
    *,
    host: str | None,
    state: str,
    step: LoginStep,
) -> ResponseReturnValue:
    """Do whatever the step socialchimp handed back asks for.

    All four are here. An app that handles fewer works until the day it
    adds a network that uses one of the others.

    Args:
        here: This app's state.
        platform: Which network.
        host: Which server, for Mastodon.
        state: The state this sign-in is filed under.
        step: Where the sign-in got to.

    Returns:
        What to send the browser.
    """
    if isinstance(step, SendToNetwork):
        # `step.remember` holds what the platform needs when the person
        # comes back - usually the secret half of a PKCE pair. Filed under
        # the state the platform is actually sending, which is the one that
        # will come back, not necessarily the one we asked for.
        _remember(here, step.state, platform=platform, host=host, kept=step.remember)
        return redirect(step.url)

    if isinstance(step, AskForDetails):
        # No trip through a browser, so no state comes back and nothing is
        # filed under one by the network. We still keep a note, because the
        # form posts to a second request of ours.
        _remember(here, state, platform=platform, host=host, kept={})
        return render_template(
            "details.html",
            platform=platform,
            network=network_for(platform),
            fields=step.fields,
            help_url=step.help_url,
        )

    if isinstance(step, ChooseAccount):
        # It asks even when there is only one Page. That is on purpose:
        # choosing silently would leave this app with two paths through a
        # sign-in, one of which almost never runs and is therefore never
        # right.
        #
        # The note under this state already holds what `start_login` handed
        # us, and `sc.choose` wants that same value back - so it is read and
        # written again rather than replaced with nothing. Overwriting it
        # here is an easy mistake and a quiet one: the networks that pause
        # to ask happen not to need it a third time, so it would only break
        # on the next network that does.
        already = here.notes.look_up(state)
        _remember(
            here,
            state,
            platform=platform,
            host=host,
            kept=already.remember if already is not None else {},
        )
        here.notes.keep_resume_token(state, step.resume_token)
        return render_template(
            "choose.html",
            platform=platform,
            network=network_for(platform),
            options=step.options,
        )

    # Finished. socialchimp has already written the connection through
    # `Storage.save_connection`, so there is nothing here to save - only the
    # notes to throw away and a person to tell.
    here.notes.forget(state)
    session.pop(STATE_KEY, None)
    flash(
        f"Connected {step.connection.account_name} on {platform}. "
        f"Its id is {step.connection.id}.",
        "good",
    )
    return redirect(url_for("connections.index"))


def _remember(
    here: Chimp,
    state: str,
    *,
    platform: str,
    host: str | None,
    kept: RawData,
) -> None:
    """Write the note the next request will need, and tell the browser.

    Args:
        here: This app's state.
        state: What to file it under.
        platform: Which network.
        host: Which server, for Mastodon.
        kept: What `SendToNetwork.remember` held.
    """
    here.notes.keep(state, platform=platform, host=host, remember=kept)
    session[STATE_KEY] = state
