"""Turning one form into the post each of the nine networks will accept.

`Post` holds what every network has - words, files, a reply, a time to
publish. Anything that exists on one network and nowhere else goes in
`Post.options`, and a name that network has never heard of is refused before
anything is sent, with the accepted names in the message. Every option below
was read off the platform file it belongs to; none is invented.

The compose page asks one set of questions and this file works out what each
network wants out of the answers. Where a network simply cannot do the thing
that was asked for, nothing here tries to be clever about it - the post goes
as it was asked for and socialchimp refuses it by name. Quietly posting
something else is the one behaviour that would be worse than failing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from socialchimp import Media, Post, RawData


@dataclass(frozen=True, slots=True)
class Composition:
    """What the compose form said, before any network has seen it.

    Attributes:
        text: The words. Called a caption on Instagram and TikTok, a
            description on YouTube and Pinterest, and the post itself
            everywhere else.
        media_url: A picture or video already online. Instagram, Threads and
            Pinterest fetch the file themselves and this is the only kind
            they take.
        media_path: A picture or video on this machine. Everything except
            Instagram and Threads can upload one.
        publish_at: When to publish. Only Mastodon, Facebook and YouTube can
            be asked; the other six refuse, which is the point of asking.
        title: YouTube requires one. Pinterest has one too, separate from
            the description.
        made_for_kids: Required by Google on every upload. There is no
            default, and socialchimp will not guess one.
        privacy_status: YouTube's. Left out, a video goes up private.
        board_id: Pinterest's. Every pin needs a board.
        link: Where Facebook's post and Pinterest's pin point.
        visibility: Mastodon's - public, unlisted, private or direct.
        language: Mastodon's, as a two-letter code.
        langs: Bluesky's, which is a list rather than one value.
        send_to: TikTok's, either `drafts` or `profile`.
        reply_settings: X's - who may reply.
    """

    text: str = ""
    media_url: str = ""
    media_path: str = ""
    publish_at: datetime | None = None
    title: str = ""
    made_for_kids: bool = False
    privacy_status: str = ""
    board_id: str = ""
    link: str = ""
    visibility: str = ""
    language: str = ""
    langs: tuple[str, ...] = ()
    send_to: str = "drafts"
    reply_settings: str = ""


def read_time(written: str) -> datetime | None:
    """Read the time an HTML datetime-local box gave us.

    Args:
        written: What the browser sent, such as `2026-09-01T10:00`.

    Returns:
        The moment in UTC, or `None` if the box was empty.

        A browser sends a local time with no timezone on it, and socialchimp
        refuses a naive datetime outright rather than guessing - a time with
        no zone compares wrongly against every other time it holds, and does
        it silently. A real app would use the person's own timezone here;
        this one says UTC and means it.
    """
    if not written:
        return None
    return datetime.fromisoformat(written).replace(tzinfo=UTC)


def media_for(asked: Composition) -> tuple[Media, ...]:
    """Work out what file, if any, is being attached.

    A web address wins over a path, because a network that can take either
    is happy with the address and the three that fetch the file themselves
    will only take that.

    Args:
        asked: What the form said.

    Returns:
        The attachment, or nothing.
    """
    if asked.media_url:
        # Instagram, Threads and Pinterest fetch this themselves. For the
        # others socialchimp downloads it first and uploads it for them, so
        # the same line works everywhere.
        return (Media.from_url(asked.media_url, alt_text=asked.title or None),)
    if asked.media_path:
        return (Media.from_file(asked.media_path, alt_text=asked.title or None),)
    return ()


def _options_for(platform: str, asked: Composition) -> RawData:
    """Work out the settings that belong to one network and nowhere else.

    Every name below appears in that platform file's own `POST_OPTIONS`.

    Args:
        platform: Which network.
        asked: What the form said.

    Returns:
        The settings, ready for `Post.options`.
    """
    options: RawData = {}

    if platform == "mastodon":
        # visibility is public, unlisted, private or direct.
        if asked.visibility:
            options["visibility"] = asked.visibility
        if asked.language:
            options["language"] = asked.language

    elif platform == "bluesky":
        # The only setting Bluesky takes. Links in the text are marked up
        # for you, byte offsets and all, which is the part people get wrong
        # writing this by hand.
        if asked.langs:
            options["langs"] = list(asked.langs)

    elif platform == "facebook":
        # A Page post can carry a link, and that is the whole list.
        if asked.link:
            options["link"] = asked.link

    elif platform == "youtube":
        # Both of these are required. A missing title is refused by name,
        # and Google requires an answer about children on every upload -
        # socialchimp will not guess it, because getting it wrong has
        # consequences for the channel rather than for the video.
        options["title"] = asked.title
        options["made_for_kids"] = asked.made_for_kids
        # Left out, the video goes up private on purpose: making somebody's
        # video public by accident cannot be undone.
        if asked.privacy_status:
            options["privacy_status"] = asked.privacy_status

    elif platform == "tiktok":
        # drafts puts the video in the person's TikTok inbox for them to
        # finish and publish themselves; profile posts it straight to their
        # profile. drafts is the default because it needs only the
        # video.upload permission, and because nothing reaches anybody's
        # profile without a person tapping a button.
        options["send_to"] = asked.send_to
        if asked.send_to == "profile" and asked.privacy_status:
            # A video with no privacy_level goes up SELF_ONLY, for the same
            # reason YouTube's goes up private.
            options["privacy_level"] = asked.privacy_status

    elif platform == "pinterest":
        # Every pin needs a board and socialchimp never picks one. Name it
        # here, or save board_id on the connection's extra and every pin
        # from that account goes there.
        if asked.board_id:
            options["board_id"] = asked.board_id
        # Post.text is the pin's *description*. The title is separate, and
        # this is the thing people trip over.
        if asked.title:
            options["title"] = asked.title
        if asked.link:
            options["link"] = asked.link

    elif platform == "x":
        # everyone, mentionedUsers, following or subscribers.
        if asked.reply_settings:
            options["reply_settings"] = asked.reply_settings

    # instagram and threads take only `carousel`, which is for forcing a
    # single picture into a swipeable post. Two or more pictures already
    # make one, so there is nothing to set here.
    return options


def build_post(platform: str, asked: Composition) -> Post:
    """Build the post one network will take, out of what the form said.

    Args:
        platform: Which network this copy is for.
        asked: What the form said.

    Returns:
        The post.

    Raises:
        ValueError: If the post would have neither words nor a file.
            `Post` refuses an empty one, and this is where that surfaces.
    """
    text = asked.text
    media = media_for(asked)

    if platform == "tiktok" and asked.send_to == "drafts":
        # TikTok's inbox takes the file and nothing else - there is no
        # caption field on that call, and the person writes the words
        # themselves in the app. socialchimp refuses a drafts post that
        # carries text rather than letting the caption quietly disappear,
        # so this app drops the words and says so on the results page.
        # Sending to the profile instead keeps them.
        text = ""

    return Post(
        text=text,
        media=media,
        publish_at=asked.publish_at,
        options=_options_for(platform, asked),
    )


@dataclass(frozen=True, slots=True)
class Outcome:
    """What happened when one post went to one connection.

    Attributes:
        connection_id: Which account it was for.
        platform: Which network.
        account_name: What to show a person.
        state: The word from `PostState`, or `refused`.
        detail: The link to the post, or the refusal in the network's own
            words.
        error_kind: The class name of the error, where there was one, so a
            page can say whether this is worth retrying.
    """

    connection_id: str
    platform: str
    account_name: str
    state: str
    detail: str
    error_kind: str = ""
    link: str | None = field(default=None)
