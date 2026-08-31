"""Every page in the project: connecting an account, and writing a post.

Signing in comes in four shapes and all nine networks are one of them. Get
the four right once and adding the tenth network is a row in a table.

1. **Send them to the network.** `start_login` answers `SendToNetwork`, you
   redirect, they approve, they come back. Mastodon, Facebook, Instagram,
   Threads, TikTok, YouTube, X and Pinterest all begin this way.
2. **Ask them for details.** Bluesky has no sign-in page anywhere - people
   use an app password - so `start_login` answers `AskForDetails`: a list of
   boxes to show. Nobody goes anywhere, and `state` is never used.
3. **Ask which account.** Facebook, Instagram and YouTube stop half way to
   ask which Page, which business account, which channel. `finish_login`
   answers `ChooseAccount`, and `sc.choose(...)` is a third request to this
   app.
4. **Done.** `Finished`, and the connection is already saved through
   `ConnectionStorage.save_connection`. There is nothing here to write.

Two things travel between those requests and neither can live in a variable:
`remember`, which holds the secret half of a PKCE pair, and `resume_token`,
which on Facebook carries the person's own access token. The two halves of a
sign-in are separate requests and can be answered by different web workers,
so both go in the session. Both are secrets: never in a URL, never in a
hidden field, never in a log.

These views are ordinary sync Django. `asgiref.sync.async_to_sync` runs the
async call and hands back what it returned - see `client.py`.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Any

from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from socialchimp import Post, PostState
from socialchimp.errors import SocialChimpError
from socialchimp.platform import (
    AskForDetails,
    ChooseAccount,
    Finished,
    SendToNetwork,
)

from . import networks
from .client import client
from .models import PostAttempt, SocialConnection
from .posting import Draft, DraftError, post_for

if TYPE_CHECKING:
    from django.http import HttpRequest

    from socialchimp.platform import LoginField, LoginStep


# Where a half-finished sign-in waits, inside the session. One entry per
# network, so somebody connecting Mastodon in one tab and Facebook in
# another does not overwrite their own sign-in.
_SESSION_KEY = "socialchimp"


def _redirect_uri(platform: str) -> str:
    """Work out where this network should send the person back to.

    It has to be the same string in all three requests - `start_login`,
    `finish_login` and `choose` - and the same string again in the
    network's developer portal, character for character. A trailing slash
    is a different address as far as every one of them is concerned.

    Args:
        platform: Which network.

    Returns:
        The address.
    """
    return str(settings.SOCIAL_REDIRECT_URI).format(platform=platform)


def _kept(request: HttpRequest, platform: str) -> dict[str, Any]:
    """Read what this network's half-finished sign-in left in the session.

    Args:
        request: The request.
        platform: Which network.

    Returns:
        Whatever was kept, or an empty dict when there is nothing.
    """
    everything: dict[str, Any] = request.session.get(_SESSION_KEY, {})
    found: dict[str, Any] = everything.get(platform, {})
    return found


def _keep(request: HttpRequest, platform: str, values: dict[str, Any]) -> None:
    """Add to what this network's sign-in has kept in the session.

    Args:
        request: The request.
        platform: Which network.
        values: What to keep.
    """
    everything: dict[str, Any] = request.session.get(_SESSION_KEY, {})
    everything.setdefault(platform, {}).update(values)
    request.session[_SESSION_KEY] = everything
    # Django only notices a change to the session when a key is assigned, so
    # a nested dict edited in place is written back nowhere. Saying so is
    # cheaper than finding out from a sign-in that works on the first try
    # and not the second.
    request.session.modified = True


def _forget(request: HttpRequest, platform: str) -> None:
    """Throw away what a finished sign-in kept.

    `remember` and `resume_token` are both secrets and both are of no use
    once the account is connected, so they do not stay in the session for
    the rest of the day.

    Args:
        request: The request.
        platform: Which network.
    """
    everything: dict[str, Any] = request.session.get(_SESSION_KEY, {})
    everything.pop(platform, None)
    request.session[_SESSION_KEY] = everything
    request.session.modified = True


def _network_or_404(name: str) -> networks.Network:
    """Look up a network from the address, or refuse the address.

    Args:
        name: The name in the URL.

    Returns:
        The network.

    Raises:
        Http404: If this project does not cover that name.
    """
    found = networks.network_or_none(name)
    if found is None:
        message = f"There is no network called {name!r} in this project."
        raise Http404(message)
    return found


# ---------------------------------------------------------------------------
# The list of connected accounts.
# ---------------------------------------------------------------------------


def connections(request: HttpRequest) -> HttpResponse:
    """Show every connected account, and every network you could connect.

    Args:
        request: The request.

    Returns:
        The page.
    """
    rows = list(SocialConnection.objects.all())
    connected = {row.platform for row in rows}

    return render(
        request,
        "social/connections.html",
        {
            "connections": rows,
            # A question socialchimp cannot answer for you: `Storage` has
            # five methods and none of them lists anything. Reading across
            # connections is a query over this app's own columns.
            "running_out": SocialConnection.objects.refresh_running_out(),
            "networks": [
                {
                    "network": network,
                    "connected": network.name in connected,
                    "pushes": networks.pushes_updates(network.name),
                }
                for network in networks.NETWORKS
            ],
            "attempts": PostAttempt.objects.all()[:20],
        },
    )


@require_http_methods(["POST"])
def disconnect(request: HttpRequest, connection_id: str) -> HttpResponseRedirect:
    """Forget one connected account.

    Args:
        request: The request.
        connection_id: Which connection.

    Returns:
        Back to the list.
    """
    # Straight through socialchimp's own storage rather than the model, so
    # that this app has one way of deleting a connection rather than two.
    async_to_sync(client().storage.delete_connection)(connection_id)
    messages.success(request, f"Disconnected {connection_id}.")
    return redirect("social:connections")


# ---------------------------------------------------------------------------
# Shape 1 and 2: starting a sign-in.
# ---------------------------------------------------------------------------


def connect(request: HttpRequest, platform: str) -> HttpResponse:
    """Show the connect page, and on POST begin the sign-in.

    Args:
        request: The request.
        platform: Which network.

    Returns:
        The connect page, a redirect to the network, or the form of boxes a
        network with no sign-in page asks for.
    """
    network = _network_or_404(platform)

    if request.method != "POST":
        return render(
            request,
            "social/connect.html",
            {
                "network": network,
                "can_register_the_app": networks.can_register_the_app(platform),
                "asks_which_account": networks.asks_which_account(platform),
                "redirect_uri": _redirect_uri(platform),
            },
        )

    # Only Mastodon has anything to ask before we start. Every other network
    # has one server, and passing a host to them does nothing.
    host = request.POST.get("host", "").strip() or None

    try:
        step = async_to_sync(client().start_login)(
            platform,
            redirect_uri=_redirect_uri(platform),
            host=host,
            # `state` is the one value that makes the round trip through
            # the network, so it is the natural thing to file a half-done
            # sign-in under. An app with users would put one in it -
            # `f"user-{request.user.pk}"` - and know on the way back whose
            # this was. This project has no users, so it is only a value
            # nobody can guess.
            state=secrets.token_urlsafe(16),
        )
    except SocialChimpError as refused:
        # The commonest one by far is "your app is not registered with this
        # network yet", because seven of the nine need credentials that
        # somebody had to go and get. socialchimp's message names the
        # network and says where to get them, so it is shown as it is.
        messages.error(request, str(refused))
        return redirect("social:connect", platform=platform)

    match step:
        # Shape 1: the ordinary redirect. Mastodon, Facebook, Instagram,
        # Threads, TikTok, YouTube, X, Pinterest.
        case SendToNetwork():
            _keep(
                request,
                platform,
                {
                    "state": step.state,
                    # The callback request needs this and cannot get it any
                    # other way. It usually holds the secret half of a PKCE
                    # pair - a secret this server keeps while only its hash
                    # travels to the network.
                    "remember": step.remember,
                    "host": host,
                },
            )
            return redirect(step.url)

        # Shape 2: there is nowhere to send anybody. Bluesky.
        case AskForDetails():
            _keep(request, platform, {"host": host})
            return render(
                request,
                "social/details.html",
                {
                    "network": network,
                    # Shown in the order given, and the ones marked secret
                    # get a password box. Never log one.
                    "fields": step.fields,
                    "help_url": step.help_url,
                },
            )

        # A network that needed nothing at all could finish here. None of
        # the nine does, but handling it costs one line and the alternative
        # is a connection dropped on the one path that is never tested.
        case Finished():
            return _finished(request, platform, step)

        case ChooseAccount():
            # Not reachable from `start_login` on any of the nine - a
            # network cannot know which of your Pages you meant before
            # anybody has approved anything.
            return _choose_account(request, platform, network, step)


@require_http_methods(["POST"])
def register_app(request: HttpRequest, platform: str) -> HttpResponseRedirect:
    """Ask socialchimp to register this app on a network. Mastodon only.

    Every other network makes somebody fill in a form in a developer portal
    and then reviews it, and asking here says exactly that rather than
    failing later on.

    Do it once per Mastodon server: every server is a separate place, so an
    app registered on mastodon.social means nothing on fosstodon.org.

    Args:
        request: The request.
        platform: Which network.

    Returns:
        Back to the connect page.
    """
    _network_or_404(platform)
    host = request.POST.get("host", "").strip() or None

    try:
        app = async_to_sync(client().create_app)(
            platform,
            name=request.POST.get("name", "socialchimp sample project"),
            redirect_uri=_redirect_uri(platform),
            host=host,
        )
    except SocialChimpError as refused:
        # "facebook does not support registering an app for you. Register it
        # by hand in that network's developer portal..." - which is the
        # honest answer for seven of the nine.
        messages.error(request, str(refused))
    else:
        # Already written through `save_app`, into the SocialApp table.
        # Nothing to save here.
        messages.success(
            request,
            f"Registered on {app.host}. Its id and secret are in the "
            f"SocialApp table now, so this does not have to happen again "
            f"for that server.",
        )

    return redirect("social:connect", platform=platform)


# ---------------------------------------------------------------------------
# Coming back: the callback, the details form, and the account picker.
# ---------------------------------------------------------------------------


def callback(request: HttpRequest, platform: str) -> HttpResponse:
    """Carry on after the network sends the person back.

    Args:
        request: The request.
        platform: Which network.

    Returns:
        The account picker, or the list of connections.
    """
    network = _network_or_404(platform)
    kept = _kept(request, platform)

    try:
        step = async_to_sync(client().finish_login)(
            platform,
            # Everything the network put in the address. socialchimp reads
            # the code and the state out of it.
            callback=request.GET.dict(),
            # The same three values the sign-in started with. All three
            # requests have to agree, or the network refuses the swap.
            redirect_uri=_redirect_uri(platform),
            host=kept.get("host"),
            state=kept.get("state"),
            remember=kept.get("remember"),
        )
    except SocialChimpError as refused:
        messages.error(request, str(refused))
        return redirect("social:connections")

    return _after_coming_back(request, platform, network, step)


@require_http_methods(["POST"])
def details(request: HttpRequest, platform: str) -> HttpResponse:
    """Finish a sign-in for a network with no sign-in page. Bluesky.

    What the person typed goes straight to `finish_login` as `callback`,
    keyed by the `name` each field gave. Nothing left this app on the way
    out, so there is no `state` to match up - which is the one way this
    shape differs from the other three.

    Args:
        request: The request.
        platform: Which network.

    Returns:
        The list of connections, or back to the form.
    """
    network = _network_or_404(platform)
    kept = _kept(request, platform)

    # Only the names the platform asked for. Posting the whole form would
    # send the CSRF token to the network as well.
    typed = {
        field.name: request.POST.get(field.name, "") for field in _fields_for(platform)
    }

    try:
        step = async_to_sync(client().finish_login)(
            platform,
            callback=typed,
            # Unused here - nobody was sent anywhere - but the argument is
            # required, and passing the real one costs nothing.
            redirect_uri=_redirect_uri(platform),
            host=kept.get("host"),
        )
    except SocialChimpError as refused:
        # A wrong handle or a revoked app password lands here. The message
        # is already written for a person to read.
        messages.error(request, str(refused))
        return redirect("social:connect", platform=platform)

    return _after_coming_back(request, platform, network, step)


def _fields_for(platform: str) -> tuple[LoginField, ...]:
    """Ask the network again what it wanted to be asked.

    Cheaper than keeping the fields in the session, and it cannot go stale.
    Starting a sign-in on a network of this shape sends nothing anywhere -
    it only reads a list off the platform - so asking twice costs nothing.

    Args:
        platform: Which network.

    Returns:
        The fields, or nothing for a network that does not work this way,
        or for one this app has no credentials for.
    """
    try:
        step = async_to_sync(client().start_login)(
            platform, redirect_uri=_redirect_uri(platform)
        )
    except SocialChimpError:
        # Bluesky is the only network of this shape and it needs no
        # credentials, so this is unreachable today. Answering with no
        # fields rather than a traceback is what keeps it that way when a
        # second network of this shape turns up and does need some.
        return ()
    return step.fields if isinstance(step, AskForDetails) else ()


@require_http_methods(["POST"])
def choose(request: HttpRequest, platform: str) -> HttpResponse:
    """Carry on after the person picked which account to use.

    The third request of three. Facebook, Instagram and YouTube all get
    here; the other six never do.

    Args:
        request: The request.
        platform: Which network.

    Returns:
        The list of connections, or the picker again.
    """
    network = _network_or_404(platform)
    kept = _kept(request, platform)

    resume_token = kept.get("resume_token")
    if resume_token is None:
        messages.error(
            request,
            "That sign-in is no longer in progress. Start it again.",
        )
        return redirect("social:connect", platform=platform)

    try:
        step = async_to_sync(client().choose)(
            platform,
            account_id=request.POST.get("account_id", ""),
            resume_token=resume_token,
            # The same values again, for the third time.
            redirect_uri=_redirect_uri(platform),
            host=kept.get("host"),
            state=kept.get("state"),
            remember=kept.get("remember"),
        )
    except SocialChimpError as refused:
        messages.error(request, str(refused))
        return redirect("social:connect", platform=platform)

    return _after_coming_back(request, platform, network, step)


def _after_coming_back(
    request: HttpRequest,
    platform: str,
    network: networks.Network,
    step: LoginStep,
) -> HttpResponse:
    """Deal with whatever `finish_login` or `choose` answered.

    Matched rather than checked with `if`, so that a step socialchimp adds
    later becomes a type error here instead of a branch that is quietly
    never taken.

    Args:
        request: The request.
        platform: Which network.
        network: What this app knows about it.
        step: Where the sign-in got to.

    Returns:
        The next page.
    """
    match step:
        # Shape 4.
        case Finished():
            return _finished(request, platform, step)

        # Shape 3: the pause. Facebook, Instagram, YouTube.
        case ChooseAccount():
            return _choose_account(request, platform, network, step)

        case SendToNetwork():
            # No network does this from `finish_login`, but saying so beats
            # falling through to a blank page if one ever does.
            return redirect(step.url)

        case AskForDetails():
            return render(
                request,
                "social/details.html",
                {
                    "network": network,
                    "fields": step.fields,
                    "help_url": step.help_url,
                },
            )


def _choose_account(
    request: HttpRequest,
    platform: str,
    network: networks.Network,
    step: ChooseAccount,
) -> HttpResponse:
    """Show the account picker.

    It asks even when there is one Page, and that is on purpose. Choosing
    silently would leave this app with two paths through a sign-in, one of
    which almost never runs and is therefore never right - and somebody with
    two Pages would find out which one got connected when a post appeared on
    it.

    Args:
        request: The request.
        platform: Which network.
        network: What this app knows about it.
        step: The pause, with the options on it.

    Returns:
        The picker.
    """
    # Into the session, and nowhere else. On Facebook this carries the
    # person's own access token, because the code Facebook sent back can
    # only be swapped once and that swap already happened. Not a hidden
    # form field, not a query value, not a log line.
    _keep(request, platform, {"resume_token": step.resume_token})

    return render(
        request,
        "social/choose.html",
        {"network": network, "options": step.options},
    )


def _finished(
    request: HttpRequest, platform: str, step: Finished
) -> HttpResponseRedirect:
    """Finish a sign-in that got to the end.

    Args:
        request: The request.
        platform: Which network.
        step: The finished step.

    Returns:
        Back to the list of connections.
    """
    _forget(request, platform)
    # socialchimp already wrote this through `save_connection` before
    # handing it back, so there is nothing to save. What is worth doing is
    # keeping `step.connection.id` against whichever of your own users this
    # belongs to - this project has no users, so it does not.
    messages.success(
        request,
        f"Connected {step.connection.account_name} as {step.connection.id}.",
    )
    return redirect("social:connections")


# ---------------------------------------------------------------------------
# Writing a post.
# ---------------------------------------------------------------------------


def _when(value: str) -> datetime | None:
    """Read the time out of a datetime-local box.

    Args:
        value: What the browser sent, such as `"2026-09-01T09:30"`.

    Returns:
        The moment, with a timezone on it, or `None` for an empty box.

    Raises:
        DraftError: If it is not a time.
    """
    if not value:
        return None

    try:
        naive = datetime.fromisoformat(value)
    except ValueError as bad:
        message = f"{value!r} is not a time this app can read."
        raise DraftError(message) from bad
    # socialchimp refuses a datetime with no timezone at the door, because a
    # naive one compares wrongly against an aware one and nothing says so.
    # The browser sends a naive one, so it is made aware here.
    return timezone.make_aware(naive) if timezone.is_naive(naive) else naive


def _draft_from(request: HttpRequest) -> Draft:
    """Read the compose form.

    Args:
        request: The request.

    Returns:
        The draft.

    Raises:
        DraftError: If the publish time cannot be read.
    """
    return Draft(
        text=request.POST.get("text", ""),
        link=request.POST.get("link", "").strip(),
        media_url=request.POST.get("media_url", "").strip(),
        media_path=request.POST.get("media_path", "").strip(),
        alt_text=request.POST.get("alt_text", "").strip(),
        publish_at=_when(request.POST.get("publish_at", "").strip()),
        youtube_title=request.POST.get("youtube_title", "").strip(),
        made_for_kids=request.POST.get("made_for_kids", ""),
        pinterest_board_id=request.POST.get("pinterest_board_id", "").strip(),
        tiktok_send_to=request.POST.get("tiktok_send_to", "drafts"),
    )


def _record(row: SocialConnection, post: Post | None, **fields: str) -> None:
    """Write down one attempt, whether it worked or not.

    Args:
        row: The connection it went to.
        post: The post, when one was built.
        fields: What happened.
    """
    PostAttempt.objects.create(
        connection_id=row.pk,
        platform=row.platform,
        account_name=row.account_name,
        text=post.text if post is not None else "",
        **fields,
    )


def _publish_to_each(
    request: HttpRequest, rows: list[SocialConnection], draft: Draft
) -> None:
    """Post to every chosen account, and decide what a failure means.

    **This loop is the app's, not the library's.** socialchimp posts as one
    account at a time: when one fails it raises at the call that failed and
    stops. There is no `post_to_many`, and that is deliberate rather than
    missing - only this app knows whether TikTok refusing should stop the
    Facebook post as well, whether the failure belongs in a row for a worker
    to retry at midnight, or whether somebody needs telling tonight.

    This app decides to carry on: every account is tried, every result is
    written to `PostAttempt`, and every refusal is shown to the person.
    Deleting the `try` would make it stop at the first failure instead, and
    both are one line of difference.

    A network that cannot do what was asked is **refused, visibly**. Nothing
    here skips a network quietly, and nothing here rewrites a post to make
    it acceptable - the whole value of the refusal is that somebody reads
    it.

    Args:
        request: The request, for the messages.
        rows: The connections to post to.
        draft: The filled-in form.
    """
    for row in rows:
        post = None
        try:
            post = post_for(row.platform, draft)
            result = async_to_sync(client().account(row.pk).post)(post)
        except (SocialChimpError, DraftError) as refused:
            # Both kinds are already one clear sentence written for a person
            # to read, so neither is translated. The two are caught together
            # and told apart in the row: socialchimp's, or ours.
            _record(row, post, error=str(refused))
            messages.error(request, f"{row.account_name}: {refused}")
            continue

        _record(
            row,
            post,
            post_id=result.id,
            url=result.url or "",
            state=result.state.name,
        )
        messages.success(request, f"{row.account_name}: {_in_words(result.state)}")


def _in_words(state: PostState) -> str:
    """Say what a post's state means, for somebody reading a page.

    `DONE` is one of five answers and the other four are not failures.
    `WAITING_FOR_PERSON` is the one worth learning early: TikTok has
    finished everything it will ever do, and the video changes when its
    author opens the app, which may be never. An app that polls this one
    polls forever.

    Args:
        state: What came back.

    Returns:
        A sentence.
    """
    return {
        PostState.DONE: "posted.",
        PostState.SCHEDULED: "scheduled - the network will publish it later.",
        PostState.PROCESSING: "uploaded; the network is still encoding it.",
        PostState.WAITING_FOR_PERSON: (
            "in their drafts. The network has finished; somebody has to open "
            "the app and publish it. Nothing else will happen on its own."
        ),
        PostState.FAILED: "the network gave up on it.",
    }[state]


def compose(request: HttpRequest) -> HttpResponse:
    """Show the write-a-post form, and on POST publish it.

    Args:
        request: The request.

    Returns:
        The page.
    """
    rows = list(SocialConnection.objects.all())

    if request.method == "POST":
        chosen = [row for row in rows if row.pk in request.POST.getlist("connections")]
        if not chosen:
            messages.error(request, "Tick at least one account.")
        else:
            try:
                draft = _draft_from(request)
            except DraftError as bad:
                messages.error(request, str(bad))
            else:
                _publish_to_each(request, chosen, draft)
        return redirect("social:compose")

    return render(
        request,
        "social/compose.html",
        {
            "connections": rows,
            "attempts": PostAttempt.objects.all()[:20],
        },
    )
