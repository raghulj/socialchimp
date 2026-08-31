"""Turning one form into the nine different posts the nine networks want.

The compose page collects one draft. Every network then wants something
slightly different out of it, and the differences are not decoration - a
YouTube upload with no title is refused, a pin with no board has nowhere to
go, and a TikTok draft cannot carry a caption at all.

Two rules run through this file.

**Never invent an option name.** Each network states exactly what
`Post.options` takes, and anything else is refused before a request is
spent. The names used below are that list and nothing more.

**Let the network's own refusal reach the person.** Where something is
missing, this file leaves it missing rather than guessing a value, so what
comes back is socialchimp's sentence naming the network and the problem.
The alternative - filling in a sensible default - is how a video goes public
that should have been unlisted.

The one thing this file does refuse by itself is a web address handed to a
network that uploads bytes, because that is a mistake socialchimp answers
with a plain `ValueError` rather than one of its own errors, and an app
catching `SocialChimpError` would miss it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from socialchimp import Media, Post

if TYPE_CHECKING:
    from datetime import datetime

    from socialchimp.models import RawData

__all__ = ["Draft", "DraftError", "post_for"]

# Instagram and Threads have no upload of any kind. You give them a web
# address and they come and fetch the file themselves, so a local file is
# refused - politely, and by socialchimp rather than by this app. Pinterest
# will fetch a web address too, but it takes an upload as well, so it is not
# in this set.
FETCHES_THE_FILE_ITSELF = frozenset({"instagram", "threads"})


class DraftError(Exception):
    """Something this app can see is wrong before socialchimp is asked.

    Kept separate from `SocialChimpError` so that reading the loop in
    `views.py` tells you which of the two decided: the library, or us.
    """


@dataclass(frozen=True, slots=True)
class Draft:
    """One filled-in compose form, before it becomes nine different posts.

    Attributes:
        text: The words. Most networks call this the post; Pinterest calls
            it the description and YouTube calls it the description too,
            with the title kept separately.
        link: A web address to go with the post. Facebook takes one as
            `link`; Pinterest takes one as the pin's destination. Nobody
            else has anywhere to put it.
        media_url: A picture or video already on the public internet.
        media_path: A picture or video on this machine.
        alt_text: What the picture shows, for somebody using a screen
            reader. Worth setting. It reaches every network here that takes
            a picture through `Media` - except Pinterest, which hangs one
            description off the whole pin rather than off each picture, so
            there it is an option instead.
        publish_at: When to publish. Only three of the nine can schedule at
            all, and the other six refuse rather than posting it now.
        youtube_title: YouTube's own title field, which is required and is
            not `text`.
        made_for_kids: `"true"`, `"false"`, or empty for not answered.
            Google requires an answer on every upload and socialchimp will
            not guess one.
        pinterest_board_id: Which board to pin to. There is no such thing as
            a pin without one.
        tiktok_send_to: `"drafts"` or `"profile"`.
    """

    text: str = ""
    link: str = ""
    media_url: str = ""
    media_path: str = ""
    alt_text: str = ""
    publish_at: datetime | None = None
    youtube_title: str = ""
    made_for_kids: str = ""
    pinterest_board_id: str = ""
    tiktok_send_to: str = "drafts"


def _media_for(platform: str, draft: Draft) -> tuple[Media, ...]:
    """Attach the file in the form the way this network takes files.

    Args:
        platform: Which network.
        draft: The filled-in form.

    Returns:
        The attachments, which is nought or one of them here.

    Raises:
        DraftError: If only a web address was given to a network that
            uploads the bytes, or if the ending of the file is not one
            socialchimp recognises.
    """
    if not draft.media_url and not draft.media_path:
        return ()

    if platform in FETCHES_THE_FILE_ITSELF:
        # Prefer the web address, and hand over the local file when that is
        # all there is. Doing that on purpose: Instagram and Threads both
        # answer a local file with a `NotSupportedError` explaining that
        # they fetch files themselves and that this one needs to go
        # somewhere public first, and that message is far more use to
        # somebody than this app quietly posting nothing.
        chosen_url = draft.media_url
        chosen_path = "" if chosen_url else draft.media_path
    else:
        # Everybody else is sent the bytes. A `Media.from_url` here would be
        # read off disk at upload time and raise a bare `ValueError` from
        # deep inside the platform, which is not a `SocialChimpError` and
        # would go straight past the loop in `views.py`. So this app says no
        # itself, in a sentence of its own.
        chosen_path = draft.media_path
        chosen_url = "" if chosen_path else draft.media_url
        if chosen_url:
            message = (
                f"{platform} uploads the file itself rather than fetching "
                f"it, so it needs a file on this machine. Download "
                f"{chosen_url} first and put its path in the file box."
            )
            raise DraftError(message)

    described = draft.alt_text or None
    try:
        if chosen_url:
            return (Media.from_url(chosen_url, alt_text=described),)
        return (Media.from_file(chosen_path, alt_text=described),)
    except ValueError as unknown_kind:
        # socialchimp works out picture-or-video from the ending of the
        # name, and says so plainly when it cannot. Passed on as our own
        # error so the loop catches it with everything else.
        raise DraftError(str(unknown_kind)) from unknown_kind


def _youtube_options(draft: Draft) -> RawData:
    """Build YouTube's options, leaving out anything not answered.

    Args:
        draft: The filled-in form.

    Returns:
        The options.
    """
    options: RawData = {}
    if draft.youtube_title:
        # `Post.text` is the description on YouTube; the title is its own
        # setting. Left out, socialchimp refuses with a message saying
        # exactly that, which is more use than a default title of "Untitled"
        # appearing on somebody's channel.
        options["title"] = draft.youtube_title
    if draft.made_for_kids:
        # Google requires an answer on every upload and getting it wrong has
        # consequences for the channel, so there is no default here and
        # socialchimp will not invent one either.
        options["made_for_kids"] = draft.made_for_kids == "true"
    # `privacy_status` is deliberately not set. A video with none goes up
    # private, because making somebody's video public by accident cannot be
    # undone.
    return options


def _pinterest_options(draft: Draft) -> RawData:
    """Build Pinterest's options, leaving out anything not answered.

    Args:
        draft: The filled-in form.

    Returns:
        The options.
    """
    options: RawData = {}
    if draft.pinterest_board_id:
        # Every pin needs a board - Pinterest has no feed to post to. A pin
        # naming none is refused with a message giving both ways to name
        # one: here, or on the connection's `extra` as a default for the
        # account.
        options["board_id"] = draft.pinterest_board_id
    if draft.link:
        options["link"] = draft.link
    if draft.alt_text:
        # The one network where alt text is not on the picture. A pin can
        # carry five pictures and has one description between them, so
        # Pinterest puts it on the pin - which means `Media.alt_text` is
        # not read here and this option is.
        options["alt_text"] = draft.alt_text
    # `Post.text` is the pin's description, which is the thing people trip
    # over: `title` is a separate option and this form does not collect one.
    return options


def _options_for(platform: str, draft: Draft) -> RawData:
    """Build the settings that belong to one network and no other.

    Every name here comes from that network's own list. A name a network has
    never heard of is refused before anything is sent, with the accepted
    names in the message, so a typo costs no request.

    Args:
        platform: Which network.
        draft: The filled-in form.

    Returns:
        The options for that network, which is often nothing.
    """
    if platform == "facebook":
        # `link` is the only setting Facebook takes on a post.
        return {"link": draft.link} if draft.link else {}
    if platform == "youtube":
        return _youtube_options(draft)
    if platform == "pinterest":
        return _pinterest_options(draft)
    if platform == "tiktok":
        # Two places a video can go. `drafts` puts it in the person's TikTok
        # inbox for them to finish and publish themselves, and needs only
        # the easier of the two permissions; `profile` posts straight to
        # their profile. `privacy_level` is left out on purpose - a profile
        # post with none goes up as SELF_ONLY, which is the safe way round.
        return {"send_to": draft.tiktok_send_to}
    # Mastodon takes visibility, spoiler_text, sensitive and language;
    # Bluesky takes langs; X takes reply_settings and quote_tweet_id;
    # Instagram and Threads take carousel. None of them is something this
    # form collects, so nothing is sent and each network's own default
    # stands.
    return {}


def post_for(platform: str, draft: Draft) -> Post:
    """Build the post one network should be sent.

    Args:
        platform: Which network.
        draft: The filled-in form.

    Returns:
        The post.

    Raises:
        DraftError: If the draft cannot be turned into a post for this
            network, or if it is empty. socialchimp refuses an empty post
            with a `ValueError` from the dataclass itself rather than a
            `SocialChimpError`, so it is caught and renamed here.
    """
    media = _media_for(platform, draft)

    # The text goes out as written, even to TikTok's drafts, which carry no
    # caption at all - the inbox takes the file and nothing else.
    # socialchimp refuses a drafts post that has text rather than letting
    # the words silently disappear, and this app lets that refusal reach the
    # person instead of dropping the text on their behalf, because dropping
    # it quietly is exactly what the refusal exists to prevent. Choose
    # "profile", or empty the text box.
    try:
        return Post(
            text=draft.text,
            media=media,
            # Handed to every network, including the six that cannot do it.
            # They refuse by name - "tiktok does not support scheduling
            # posts" - rather than publishing it now, and being told is the
            # point. Skipping them here would hide the difference.
            publish_at=draft.publish_at,
            options=_options_for(platform, draft),
        )
    except ValueError as empty:
        raise DraftError(str(empty)) from empty
