"""Instagram direct messages: turning Meta's shapes into socialchimp's.

Everything here is a plain function over what Instagram sent - no requests.
`InstagramPlatform` in `instagram.py` makes the requests and hands the
replies to these. Kept apart because `instagram.py` is already long, and
because every one of these can be tested on a dictionary alone.

## Where Meta documents each part

When something here stops matching what Instagram sends, these are the pages
to read first. Meta's Instagram pages are thin on examples, so the Messenger
Platform pages - which the Instagram API shares its message shapes with -
are listed as well.

- Sending, the 24-hour window, message tags:
  https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/messaging-api
- Listing conversations and reading their messages (and the "20 most recent
  messages" rule):
  https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/conversations-api
- The fields on one message (`attachments`, `shares`, `story`,
  `is_unsupported`), from the Messenger side:
  https://developers.facebook.com/docs/graph-api/reference/message
- Webhook fields to subscribe to (`messages`, `message_echoes`,
  `message_reactions`, `messaging_seen`, `messaging_postbacks`):
  https://developers.facebook.com/docs/instagram-platform/webhooks
- The shape of each webhook event (`message`, `reaction`, `read`,
  `postback`, `is_echo`, `is_deleted`):
  https://developers.facebook.com/docs/graph-api/webhooks/reference/instagram
  and https://developers.facebook.com/docs/messenger-platform/reference/webhook-events
- Error codes (outside the window, recipient unavailable, rate limits, a
  file that could not be fetched or is too big):
  https://developers.facebook.com/docs/messenger-platform/error-codes/
- Sending pictures ("Send Images", several in one message) and video,
  sound or a PDF ("Send audio, video or file", one at a time), with the
  formats and sizes allowed:
  https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/messaging-api
- Message tags, including `HUMAN_AGENT`:
  https://developers.facebook.com/docs/messenger-platform/send-messages/message-tags
- Rate limits (Send API: 100 calls a second per account for text; the
  Conversations API: 2 a second):
  https://developers.facebook.com/docs/graph-api/overview/rate-limiting/

## A conversation is named after the other person

Meta has its own id for a conversation, but a webhook never carries it - a
pushed message names only the sender and the recipient. So that a message
read back and a message pushed to you land in the same conversation, the
id socialchimp uses for an Instagram conversation is **the other person's
Instagram-scoped id** (their IGSID), the same as `Person.id`. Meta's own
conversation id is kept on `Conversation.raw["id"]`.

Instagram's business messaging is one-to-one, so the person is enough to
name the conversation, and sending needs nothing else: `POST /me/messages`
takes the person's id as its recipient.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from socialchimp.errors import (
    BlockedError,
    InvalidPostError,
    MissingPermissionError,
    NotAllowedError,
    NotFoundError,
    ReplyWindowClosedError,
    SocialChimpError,
)
from socialchimp.events import MessageEvent, MessageEventKind, Update
from socialchimp.models import (
    Attachment,
    Connection,
    Conversation,
    Message,
    Person,
    RawData,
)

if TYPE_CHECKING:
    from socialchimp.platforms._meta import Messaging

PLATFORM_NAME: Final = "instagram"

STANDARD_WINDOW: Final = timedelta(hours=24)
"""How long after a person's last message the account may answer.

"Your app has 24 hours to respond to a user" -
https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/messaging-api
"""

HUMAN_AGENT_WINDOW: Final = timedelta(days=7)
"""How long a message tagged `HUMAN_AGENT` may still be sent.

Only for a person writing the answer, and only for an app Meta has let use
the tag. See
https://developers.facebook.com/docs/messenger-platform/send-messages/message-tags
"""

HUMAN_AGENT: Final = "HUMAN_AGENT"
"""Meta's name for the tag that stretches the window to seven days."""

MESSAGE_FIELDS: Final = (
    "id,created_time,from,to,message,attachments,shares,story,is_unsupported,reactions"
)
"""What to ask for about each message.

The Instagram page documents `id,created_time,from,to,message`. The rest are
the Messenger message fields Instagram also fills in - see
https://developers.facebook.com/docs/graph-api/reference/message
"""

# Messenger error codes and subcodes that only mean something when sending a
# message. From https://developers.facebook.com/docs/messenger-platform/error-codes/
_OUTSIDE_THE_WINDOW: Final = frozenset({2_534_022, 2_018_278})
_CANNOT_BE_MESSAGED_CODE: Final = 551
_CANNOT_BE_MESSAGED_SUBCODE: Final = 2_018_108
_NOBODY_BY_THAT_ID: Final = 2_534_014
_DM_ACCESS_TURNED_OFF: Final = 2_534_041
# Refusals of an attached file, all under code 100, from the same page. Each
# is said in words a person can act on.
_FILE_REFUSALS: Final[dict[int, str]] = {
    2_018_047: (
        "Instagram could not take the attached file (error 100, subcode "
        "2018047). Usually its type does not match what it says it is - a "
        "file sent as a picture that is not a PNG or a JPEG, say. Check the "
        "file's type against Limits.messages."
    ),
    2_018_008: (
        "Instagram could not fetch the attached file from its web address "
        "(error 100, subcode 2018008). The address has to be reachable from "
        "the public internet, over HTTPS, without signing in, and still "
        "good when Instagram asks - give a signed link a few minutes, not "
        "seconds. A slow server or a file too big can also cause this."
    ),
    2_018_109: (
        "The attached file is too big for Instagram (error 100, subcode "
        "2018109). See Limits.messages for the most each kind may be: 8 MB "
        "for a picture, 25 MB for video, sound or a PDF."
    ),
    2_018_294: (
        "Instagram gave up fetching or reading the attached video (error "
        "100, subcode 2018294). It allows 75 seconds to fetch one, and "
        "refuses a broken file. Make it smaller, or serve it faster."
    ),
    2_018_074: (
        "Instagram does not know that attachment, or it belongs to another "
        "app (error 100, subcode 2018074)."
    ),
}

# Plain "no permission", which on a messaging call can only mean one thing.
_NO_PERMISSION: Final = frozenset({10, 200})

MESSAGES_SCOPE: Final = "instagram_business_manage_messages"

# What Instagram calls an attachment in a webhook, where it differs from
# what we call it. Anything else keeps Instagram's own word.
_OUR_WORD_FOR_ATTACHMENT: Final = {"ig_reel": "reel"}


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def _said(error: RawData) -> str:
    """Pull Instagram's own message out of its error object.

    Args:
        error: The error object Meta sent.

    Returns:
        Its message, ready to add to the end of ours, or an empty string.
    """
    said = error.get("message")
    return f" Instagram said: {said}" if isinstance(said, str) and said else ""


def _codes(error: RawData) -> tuple[int | None, int | None]:
    """Read the code and subcode off one Meta error object.

    Args:
        error: The error object Meta sent.

    Returns:
        The code and the subcode, each `None` when missing.
    """
    code = error.get("code")
    subcode = error.get("error_subcode")
    return (
        code if isinstance(code, int) else None,
        subcode if isinstance(subcode, int) else None,
    )


def message_error(body: RawData) -> SocialChimpError | None:
    """Name a refusal that can only have come from sending or reading messages.

    Each of these is a subcode no other Instagram call uses, so it is safe to
    look for them on every reply. See
    https://developers.facebook.com/docs/messenger-platform/error-codes/

    Args:
        body: The reply, already read into a dictionary.

    Returns:
        The error to raise, or `None` when this is not one of them.
    """
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    code, subcode = _codes(error)
    raw = {"error": error}

    if subcode in _OUTSIDE_THE_WINDOW:
        message = (
            f"Instagram will not deliver this message: the 24 hours to reply "
            f"have passed since this person last wrote (error {code}, subcode "
            f"{subcode}). Nothing was sent. They have to write again before "
            f"the account can answer. An app Meta has approved for the "
            f"{HUMAN_AGENT} tag can answer for up to 7 days, for replies "
            f"written by a person - pass options={{'tag': '{HUMAN_AGENT}'}}, "
            f"or build the platform with InstagramPlatform(human_agent=True)."
            f"{_said(error)}"
        )
        return ReplyWindowClosedError(message, platform=PLATFORM_NAME, raw=raw)

    if code == _CANNOT_BE_MESSAGED_CODE or subcode == _CANNOT_BE_MESSAGED_SUBCODE:
        message = (
            f"Instagram says this person cannot be messaged right now (error "
            f"{code}). They may have blocked the account, turned off messages "
            f"from businesses, or closed their account. Trying again will not "
            f"help.{_said(error)}"
        )
        return BlockedError(message, platform=PLATFORM_NAME, raw=raw)

    if subcode == _NOBODY_BY_THAT_ID:
        message = (
            f"Instagram cannot find anybody by that id (error {code}, subcode "
            f"{subcode}). An Instagram conversation id is the other person's "
            f"Instagram-scoped id, as read_conversations and pushed messages "
            f"give it.{_said(error)}"
        )
        return NotFoundError(message, platform=PLATFORM_NAME, raw=raw)

    if subcode is not None and subcode in _FILE_REFUSALS:
        message = f"{_FILE_REFUSALS[subcode]}{_said(error)}"
        return InvalidPostError(message, platform=PLATFORM_NAME, raw=raw)

    if subcode == _DM_ACCESS_TURNED_OFF:
        return _missing_messages_permission(
            error,
            why=(
                "The account's owner has turned off this app's access to "
                "their messages, in Instagram's own settings (Settings > "
                "Messages > Connected tools). They have to turn it back on."
            ),
        )
    return None


def _missing_messages_permission(error: RawData, *, why: str) -> MissingPermissionError:
    """Build the error for a messaging call without the messaging permission.

    Args:
        error: The error object Meta sent.
        why: What to do about it.

    Returns:
        The error to raise.
    """
    code, subcode = _codes(error)
    number = f"error {code}" if subcode is None else f"error {code}, subcode {subcode}"
    return MissingPermissionError(
        needs=MESSAGES_SCOPE,
        suggestion=f"Instagram refused ({number}). {why}{_said(error)}",
        platform=PLATFORM_NAME,
        raw={"error": error},
    )


def while_messaging(refused: SocialChimpError) -> SocialChimpError:
    """Name a refusal more exactly, knowing the call was about messages.

    A plain "no permission" (code 10 or 200) could mean anything on another
    call, but on one of these it means the messaging permission is missing.

    Args:
        refused: The error already raised.

    Returns:
        A better error, or the same one when there is nothing to add.
    """
    better = message_error(refused.raw)
    if better is not None:
        return better
    error = refused.raw.get("error")
    if (
        isinstance(refused, NotAllowedError)
        and isinstance(error, dict)
        and _codes(error)[0] in _NO_PERMISSION
        and _codes(error)[1] is None
    ):
        return _missing_messages_permission(
            error,
            why=(
                f"Check the app asks for {MESSAGES_SCOPE}, that Meta has "
                f"approved it in App Review, and that the person granted it "
                f"when they signed in. A connection made before it was asked "
                f"for has to sign in again."
            ),
        )
    return refused


# ---------------------------------------------------------------------------
# Reading what Instagram sends back
# ---------------------------------------------------------------------------


def _text(value: object) -> str | None:
    """Keep a value only if it is a string with something in it.

    Args:
        value: Anything.

    Returns:
        The string, or `None`.
    """
    return value if isinstance(value, str) and value else None


def _number(value: object) -> int | None:
    """Keep a value only if it is a whole number.

    Args:
        value: Anything.

    Returns:
        The number, or `None`.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _dicts(value: object) -> list[RawData]:
    """Read a Graph list - `{"data": [...]}` - keeping only its objects.

    Args:
        value: Anything.

    Returns:
        Every object in its `data`, or nothing.
    """
    if not isinstance(value, dict):
        return []
    data = value.get("data")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def when(value: object) -> datetime | None:
    """Read a Graph time such as `2026-09-28T14:11:00+0000`.

    Args:
        value: What Meta sent.

    Returns:
        The moment, with a timezone, or `None` if it is not a time.
    """
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def person_from(raw: RawData) -> Person:
    """Build a person out of a Graph `{"username", "id"}` pair.

    Instagram gives no display name or picture here; asking for them is a
    request per person, which an inbox listing twenty conversations should
    not pay for.

    Args:
        raw: What Meta sent.

    Returns:
        The person.
    """
    username = _text(raw.get("username"))
    return Person(
        id=str(raw.get("id", "")),
        handle=username,
        display_name=None,
        avatar_url=None,
        url=f"https://www.instagram.com/{username}" if username else None,
        raw=raw,
    )


def is_me(raw: RawData, connection: Connection) -> bool:
    """Say whether a Graph `{"username", "id"}` pair is the connected account.

    Instagram Login gives an account two ids - the professional account id
    that webhooks and conversations use, and an app-scoped one - and does
    not promise which a reply will carry. The username is the same either
    way, so it is looked at too. See
    https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/get-started#fields

    Args:
        raw: What Meta sent.
        connection: The account we are acting as.

    Returns:
        True when it is the connected account.
    """
    ours = {connection.account_id, str(connection.extra.get("instagram_id", ""))}
    if str(raw.get("id", "")) in ours:
        return True
    username = _text(raw.get("username"))
    mine = _text(connection.extra.get("username"))
    return (
        username is not None and mine is not None and username.lower() == mine.lower()
    )


def me(connection: Connection) -> Person:
    """Build the connected account as a person, for a message it sent.

    Args:
        connection: The account we are acting as.

    Returns:
        The account.
    """
    username = _text(connection.extra.get("username"))
    return Person(
        id=connection.account_id,
        handle=username,
        display_name=None,
        avatar_url=None,
        url=f"https://www.instagram.com/{username}" if username else None,
    )


def _attachment(
    kind: str,
    url: object,
    raw: RawData,
    *,
    sizes: RawData | None = None,
) -> Attachment:
    """Build one attachment.

    Args:
        kind: What sort of file this is.
        url: Where it is, if Meta said.
        raw: What Meta sent about it.
        sizes: Meta's `image_data` or `video_data`, holding `preview_url`,
            `width` and `height`, where there is one.

    Returns:
        The attachment.
    """
    sizes = sizes or {}
    return Attachment(
        kind=kind,
        url=_text(url),
        preview_url=_text(sizes.get("preview_url")),
        alt_text=None,
        width=_number(sizes.get("width")),
        height=_number(sizes.get("height")),
        raw=raw,
    )


def _file_kind(raw: RawData) -> str:
    """Say what a Graph attachment with no picture or video data is.

    Args:
        raw: One item from a message's `attachments`.

    Returns:
        `"audio"` for a voice note or other sound, otherwise `"file"`.
    """
    mime = raw.get("mime_type")
    return "audio" if isinstance(mime, str) and mime.startswith("audio/") else "file"


def graph_attachments(raw: RawData) -> tuple[Attachment, ...]:
    """Read every attachment off one message read back from the Graph API.

    Pictures and videos carry `image_data` or `video_data`; everything else
    is a `file_url`. Shared posts are under `shares`, and a story the person
    mentioned the account in, or replied to, is under `story`. See
    https://developers.facebook.com/docs/graph-api/reference/message

    Args:
        raw: One message, as Meta sent it.

    Returns:
        Its attachments, in the order Meta listed them.
    """
    found: list[Attachment] = []
    for item in _dicts(raw.get("attachments")):
        for kind in ("image", "video"):
            data = item.get(f"{kind}_data")
            if isinstance(data, dict):
                found.append(_attachment(kind, data.get("url"), item, sizes=data))
                break
        else:
            found.append(_attachment(_file_kind(item), item.get("file_url"), item))

    found.extend(
        _attachment("share", item.get("link"), item)
        for item in _dicts(raw.get("shares"))
    )

    story = raw.get("story")
    if isinstance(story, dict):
        for key, kind in (("mention", "story_mention"), ("reply_to", "story_reply")):
            about = story.get(key)
            if isinstance(about, dict):
                found.append(_attachment(kind, about.get("link"), about))
    return tuple(found)


def graph_message(
    raw: RawData,
    connection: Connection,
    *,
    conversation_id: str,
) -> Message | None:
    """Build one message read back from the Graph API.

    Args:
        raw: One message, as Meta sent it.
        connection: The account we are acting as.
        conversation_id: The other person's id, which names the
            conversation.

    Returns:
        The message, or `None` when it has no id or no time - Meta leaves
        both off a message older than the 20 it gives details of.
    """
    message_id = _text(raw.get("id"))
    sent_at = when(raw.get("created_time"))
    if message_id is None or sent_at is None:
        return None

    sender = raw.get("from")
    sender = sender if isinstance(sender, dict) else {"id": conversation_id}
    mine = is_me(sender, connection)
    return Message(
        id=message_id,
        conversation_id=conversation_id,
        sender=me(connection) if mine else person_from(sender),
        text=_text(raw.get("message")) or "",
        sent_at=sent_at,
        is_mine=mine,
        # The Graph API leaves an unsent message out altogether; only a
        # webhook says one was unsent. `is_unsupported` - something the API
        # cannot show, such as a voice call - stays on `raw`.
        deleted=False,
        attachments=graph_attachments(raw),
        raw=raw,
    )


def reply_until(
    messages: tuple[Message, ...],
    *,
    updated_at: datetime | None,
    window: timedelta,
) -> datetime | None:
    """Work out when the window to reply closes.

    It runs from the person's last message, not the account's. When none of
    the messages given are theirs, their last one is older than all of them,
    so the window closed no later than `window` after the oldest - which is
    what this gives, knowing it may be a little late. With no messages at
    all, it runs from the conversation's last change, on the same terms.

    Args:
        messages: The messages we have, newest first.
        updated_at: When the conversation last changed.
        window: 24 hours, or 7 days for a `HUMAN_AGENT` app.

    Returns:
        When the window closes, or `None` with no time to go on.
    """
    theirs = [message.sent_at for message in messages if not message.is_mine]
    if theirs:
        return max(theirs) + window
    if messages:
        return min(message.sent_at for message in messages) + window
    return updated_at + window if updated_at is not None else None


def graph_conversation(
    raw: RawData,
    connection: Connection,
    *,
    window: timedelta,
) -> Conversation | None:
    """Build one conversation read back from the Graph API.

    Args:
        raw: One conversation, as Meta sent it.
        connection: The account we are acting as.
        window: 24 hours, or 7 days for a `HUMAN_AGENT` app.

    Returns:
        The conversation, named after the other person, or `None` when
        there is nobody else in it to name it after.
    """
    others = [
        person_from(item)
        for item in _dicts(raw.get("participants"))
        if not is_me(item, connection)
    ]
    if not others:
        return None
    conversation_id = others[0].id

    messages = graph_messages(raw, connection, conversation_id=conversation_id)
    updated_at = when(raw.get("updated_time"))
    return Conversation(
        id=conversation_id,
        people=tuple(others),
        last_message=messages[0] if messages else None,
        # Instagram does not say how many are unread.
        unread_count=None,
        updated_at=updated_at,
        can_reply_until=reply_until(messages, updated_at=updated_at, window=window),
        # Only the 20 newest messages can be read back.
        full_history=False,
        raw=raw,
    )


def graph_messages(
    raw: RawData,
    connection: Connection,
    *,
    conversation_id: str,
) -> tuple[Message, ...]:
    """Read the messages nested in one conversation, newest first.

    Args:
        raw: One conversation, as Meta sent it.
        connection: The account we are acting as.
        conversation_id: The other person's id.

    Returns:
        Every message that could be read, newest first.
    """
    found = (
        graph_message(item, connection, conversation_id=conversation_id)
        for item in _dicts(raw.get("messages"))
    )
    kept = [message for message in found if message is not None]
    return tuple(sorted(kept, key=lambda message: message.sent_at, reverse=True))


# ---------------------------------------------------------------------------
# Reading what Instagram pushes
# ---------------------------------------------------------------------------


def _webhook_attachments(message: RawData) -> tuple[Attachment, ...]:
    """Read every attachment off one pushed message.

    Each is `{"type": ..., "payload": {"url": ...}}`. A reply to a story is
    not an attachment to Meta - it is `reply_to.story` - but it is one here,
    so an inbox can show the story being answered. See
    https://developers.facebook.com/docs/instagram-platform/webhooks

    Args:
        message: The `message` object of one event.

    Returns:
        Its attachments.
    """
    found: list[Attachment] = []
    listed = message.get("attachments")
    for item in listed if isinstance(listed, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type", "unknown"))
        payload = item.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        found.append(
            _attachment(
                _OUR_WORD_FOR_ATTACHMENT.get(kind, kind), payload.get("url"), item
            )
        )

    reply_to = message.get("reply_to")
    story = reply_to.get("story") if isinstance(reply_to, dict) else None
    if isinstance(story, dict):
        found.append(_attachment("story_reply", story.get("url"), story))
    return tuple(found)


def _somebody(event: RawData, key: str) -> str | None:
    """Read `sender.id` or `recipient.id` off one event.

    Args:
        event: One pushed event.
        key: `"sender"` or `"recipient"`.

    Returns:
        The id, or `None`.
    """
    found = event.get(key)
    if not isinstance(found, dict):
        return None
    person_id = found.get("id")
    if isinstance(person_id, int) and not isinstance(person_id, bool):
        return str(person_id)
    return _text(person_id)


def _someone(person_id: str) -> Person:
    """Build a person known only by their id, as a webhook gives them.

    Args:
        person_id: Their Instagram-scoped id.

    Returns:
        The person, with nothing else filled in.
    """
    return Person(
        id=person_id, handle=None, display_name=None, avatar_url=None, url=None
    )


def message_events(
    found: list[Messaging],
    *,
    window: timedelta,
) -> list[MessageEvent]:
    """Turn the pushed events Meta sent into message events.

    Args:
        found: What `_meta.messaging_in` read out of the request.
        window: 24 hours, or 7 days for a `HUMAN_AGENT` app.

    Returns:
        One event for each that could be understood, in Meta's order.
        Events we have no word for - referrals, opt-ins, handovers - are
        left out.
    """
    events = (_event_from(item, window=window) for item in found)
    return [event for event in events if event is not None]


def _event(
    kind: MessageEventKind,
    item: Messaging,
    other: str,
    *,
    message_id: str | None = None,
    message: Message | None = None,
    conversation: Conversation | None = None,
    reaction: str | None = None,
    payload: str | None = None,
) -> MessageEvent:
    """Build one message event, filling in what every event shares.

    Args:
        kind: What happened.
        item: The pushed event, with the account it came to.
        other: The other person's id, which names the conversation.
        message_id: The message it is about.
        message: The message itself.
        conversation: What is known about the conversation.
        reaction: The reaction.
        payload: The value of a tapped button or quick reply.

    Returns:
        The event.
    """
    return MessageEvent(
        kind=kind,
        platform=PLATFORM_NAME,
        connection_id=f"{PLATFORM_NAME}:{item.account_id}",
        conversation_id=other,
        person=_someone(other),
        happened_at=item.when,
        message_id=message_id,
        message=message,
        conversation=conversation,
        reaction=reaction,
        payload=payload,
        raw=item.event,
    )


def _just_written(message: Message, *, window: timedelta) -> Conversation:
    """Say what one message the person just sent tells us about its conversation.

    Args:
        message: What they sent.
        window: 24 hours, or 7 days for a `HUMAN_AGENT` app.

    Returns:
        The conversation, with its reply window running from this message.
    """
    return Conversation(
        id=message.conversation_id,
        people=(message.sender,),
        last_message=message,
        unread_count=None,
        updated_at=message.sent_at,
        can_reply_until=message.sent_at + window,
        full_history=False,
    )


def _event_from(item: Messaging, *, window: timedelta) -> MessageEvent | None:
    """Turn one pushed event into a message event.

    Which key the event carries says what it is: `message` (with `is_echo`
    or `is_deleted` on it for those), `reaction`, `read` or `postback`. See
    https://developers.facebook.com/docs/instagram-platform/webhooks

    Args:
        item: One event, with the account it came to.
        window: 24 hours, or 7 days for a `HUMAN_AGENT` app.

    Returns:
        The event, or `None` when it is not one we understand.
    """
    event = item.event
    sender = _somebody(event, "sender")
    if sender is None:
        return None

    message = event.get("message")
    if isinstance(message, dict):
        return _from_message(item, message, sender=sender, window=window)

    reaction = event.get("reaction")
    if isinstance(reaction, dict):
        undone = reaction.get("action") == "unreact"
        return _event(
            MessageEventKind.UNREACTED if undone else MessageEventKind.REACTED,
            item,
            sender,
            message_id=_text(reaction.get("mid")),
            reaction=_text(reaction.get("emoji")) or _text(reaction.get("reaction")),
        )

    read = event.get("read")
    if isinstance(read, dict):
        return _event(
            MessageEventKind.READ, item, sender, message_id=_text(read.get("mid"))
        )

    postback = event.get("postback")
    if isinstance(postback, dict):
        return _from_postback(item, postback, sender=sender, window=window)
    return None


def _from_message(
    item: Messaging,
    raw: RawData,
    *,
    sender: str,
    window: timedelta,
) -> MessageEvent | None:
    """Turn a pushed `message` into a received, sent or deleted event.

    Args:
        item: The pushed event, with the account it came to.
        raw: Its `message` object.
        sender: Who sent it.
        window: 24 hours, or 7 days for a `HUMAN_AGENT` app.

    Returns:
        The event, or `None` when the message has no `mid`, or is an echo
        with no recipient.
    """
    message_id = _text(raw.get("mid"))
    if message_id is None:
        return None
    echo = raw.get("is_echo") is True
    deleted = raw.get("is_deleted") is True
    # On an echo the account sent it, and the person is the recipient.
    other = _somebody(item.event, "recipient") if echo else sender
    if other is None:
        # An echo that does not say who it went to belongs to no
        # conversation we could name.
        return None
    account = _someone(item.account_id)
    message = Message(
        id=message_id,
        conversation_id=other,
        sender=account if echo else _someone(sender),
        text="" if deleted else _text(raw.get("text")) or "",
        sent_at=item.when,
        is_mine=echo,
        deleted=deleted,
        attachments=() if deleted else _webhook_attachments(raw),
        raw=raw,
    )

    if deleted:
        return _event(
            MessageEventKind.DELETED,
            item,
            other,
            message_id=message_id,
            message=message,
        )
    if echo:
        return _event(
            MessageEventKind.SENT, item, other, message_id=message_id, message=message
        )
    quick_reply = raw.get("quick_reply")
    return _event(
        MessageEventKind.RECEIVED,
        item,
        other,
        message_id=message_id,
        message=message,
        conversation=_just_written(message, window=window),
        payload=(
            _text(quick_reply.get("payload")) if isinstance(quick_reply, dict) else None
        ),
    )


def _from_postback(
    item: Messaging,
    raw: RawData,
    *,
    sender: str,
    window: timedelta,
) -> MessageEvent:
    """Turn a pushed `postback` - a tapped button - into an event.

    The button's words become a message from the person, because that is
    how Instagram shows it in the conversation, and a tap opens the reply
    window just as a message does.

    Args:
        item: The pushed event, with the account it came to.
        raw: Its `postback` object.
        sender: Who tapped it.
        window: 24 hours, or 7 days for a `HUMAN_AGENT` app.

    Returns:
        The event.
    """
    message_id = _text(raw.get("mid"))
    message = Message(
        # A tap is not always given an id of its own; this one is stable for
        # the same tap delivered twice.
        id=message_id or f"postback:{sender}:{int(item.when.timestamp() * 1000)}",
        conversation_id=sender,
        sender=_someone(sender),
        text=_text(raw.get("title")) or "",
        sent_at=item.when,
        is_mine=False,
        deleted=False,
        attachments=(),
        raw=raw,
    )
    return _event(
        MessageEventKind.BUTTON_TAPPED,
        item,
        sender,
        message_id=message_id,
        message=message,
        conversation=_just_written(message, window=window),
        payload=_text(raw.get("payload")),
    )


def received_updates(events: list[MessageEvent]) -> list[Update]:
    """Turn every new message from a person into a `MESSAGE_RECEIVED` update.

    For an app that only wants to hear "a message arrived" through the same
    `read_updates` it already uses for comments. Everything else - sent,
    deleted, reactions, reads, taps - is only a `MessageEvent`.

    Args:
        events: What `message_events` made of the request.

    Returns:
        One update for each new message.
    """
    found: list[Update] = []
    for event in events:
        if event.kind is not MessageEventKind.RECEIVED:
            continue
        account = event.connection_id.removeprefix(f"{PLATFORM_NAME}:")
        found.append(
            Update.from_network(
                update_id=f"{account}:message:{event.message_id}",
                kind_name="message_received",
                platform=PLATFORM_NAME,
                connection_id=event.connection_id,
                created_at=event.happened_at,
                raw=event.raw,
                actor=event.person,
                post_id=event.message_id,
                conversation_id=event.conversation_id,
            )
        )
    return found
