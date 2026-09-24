# Building a social inbox: comments, likes, DMs and updates

An app that shows somebody their latest post, the replies underneath it, who
liked it, and their direct messages - and lets them answer all three from one
screen, without reloading a page to find out what happened while they were
away.

New to socialchimp? Read the [tutorial](../tutorial.md) first. This page
assumes you know what a connection and an `Account` are.

As of 0.8.0 this works on **Mastodon and Bluesky**. Everywhere else,
`account.features()` says so plainly rather than the calls below quietly
doing nothing - see [Hiding a button a network does not
support](#hiding-a-button-a-network-does-not-support).

- [Showing a post with its comments](#showing-a-post-with-its-comments)
- [Replying](#replying)
- [Liking, and who liked](#liking-and-who-liked)
- [Polling for updates with a stored marker](#polling-for-updates-with-a-stored-marker)
- [Direct messages](#direct-messages)
- [Hiding a button a network does not support](#hiding-a-button-a-network-does-not-support)
- [Handling the new errors](#handling-the-new-errors)
- [Testing with `FakePlatform`](#testing-with-fakeplatform)
- [Per-network notes](#per-network-notes)

---

## Showing a post with its comments

`account.read_post(post_id)` reads one post back in full - not only what
publishing it returned, but the author, the text, any attachments, and the
counts the network keeps:

```python
post = await account.read_post(post_id)
print(post.author.display_name, post.text, post.like_count)
```

To show it together with the replies underneath it, ask for the thread
instead. `Thread.replies` comes back flat, oldest first - build the tree
yourself by matching `parent_id` to `PostDetails.id`:

```python
from socialchimp import PostDetails

thread = await account.read_thread(post_id)

by_parent: dict[str | None, list[PostDetails]] = {}
for reply in thread.replies:
    by_parent.setdefault(reply.parent_id, []).append(reply)


def render(post_id: str, depth: int = 0) -> None:
    for reply in by_parent.get(post_id, []):
        print("  " * depth + reply.text)
        render(reply.id, depth + 1)


print(thread.post.text)
render(thread.post.id)
```

`thread.complete` is `False` when `depth`, `limit`, or the network's own cap
cut the replies short - Mastodon has no pagination here, so a very long
thread is the one case this can happen without you asking for a limit
yourself. A deleted or blocked reply on Bluesky comes back as a placeholder
with `unavailable` set, rather than vanishing and leaving a gap in the
numbering.

`post.html` is Mastodon's own HTML for the post, and it is **untrusted** -
sanitise it yourself before putting it on a page. `post.text` is already
plain text, safe to show as-is.

---

## Replying

```python
result = await account.reply(post_id, "Thanks for the order - it ships Monday.")
```

This is the call to reach for once a network offers it, rather than
`account.post(Post(text=..., reply_to=post_id))` - the older call still
works, but `reply()` knows things it does not: on Mastodon it keeps a reply
no wider than its parent's own visibility, and it mentions the people
already in the thread the way Mastodon's own web app does.

**A reply to a direct or followers-only Mastodon post keeps that
visibility**, whatever you ask for. Otherwise nothing is sent at all, so the
account's own default visibility applies - `options={"visibility":
"unlisted"}` only narrows a reply, it never widens one past its parent:

```python
# The parent is public. Nothing forces "public" here - the account's own
# default applies, same as an ordinary post.
await account.reply(public_post_id, "Good question!")

# The parent is a DM. This still comes out as "direct", not "unlisted".
await account.reply(direct_post_id, "Sent!", options={"visibility": "unlisted"})
```

Attach media the same way `post()` does:

```python
from socialchimp import Media

await account.reply(post_id, "Here's a photo", media=(Media.from_file("cat.jpg"),))
```

---

## Liking, and who liked

```python
like = await account.like(post_id)
...
await account.unlike(post_id, like_id=like.like_id)
```

Both are idempotent - liking something twice, or unliking something not
liked, succeeds and does nothing, so a double-tap or a retried request never
raises. Pass `like_id` back to `unlike()` when you have it; on Bluesky that
saves a lookup, and on Mastodon it is simply ignored.

```python
page = await account.read_likes(post_id)
for like in page.items:
    print(like.person.display_name, like.liked_at)
```

`Like.liked_at` is `None` on Mastodon - the server does not say when a
favourite happened, only who made it - and filled in on Bluesky. Read the
next page the same way every other paged call here works:

```python
page = await account.read_likes(post_id)
while page.next is not None:
    page = await account.read_likes(post_id, after=page.next)
```

---

## Polling for updates with a stored marker

`account.fetch_updates_after` is the resumable way to ask "what happened
since I last checked" - unlike `fetch_updates(since)`, which takes a moment
in time and can miss or repeat things at the edges, this takes an opaque
marker your app stores and hands back:

```python
marker = await load_marker(connection_id)  # None the first time

batch = await account.fetch_updates_after(marker)
for update in batch.updates:
    handle(update)

await save_marker(connection_id, batch.marker)
if batch.more:
    # There is more waiting right now - ask again without waiting for the
    # next tick of your poll loop.
    ...
```

`marker=None` on the first call reads the latest page, which is what sets
the starting point - it does not replay a network's whole history. Once you
have a marker, tell the network you have handled it:

```python
await account.mark_seen(batch.marker)
```

**Store the marker exactly as it comes back, and pass it back exactly as
given.** A marker your app made up, or one saved against the wrong
connection, raises `ConfigError` rather than silently starting over -
starting over could skip updates, which this design refuses to risk. Pass
`None` if you have genuinely lost track; that is the only safe way to
restart.

`Update` carries more than it used to: `actor` (who did it), `post_id` (the
reply, mention or message itself), `about_post_id` (your own post this
concerns), `thread_root_id`, and `conversation_id` for a direct message:

```python
from socialchimp import UpdateKind

for update in batch.updates:
    match update.kind:
        case UpdateKind.COMMENT_CREATED:
            notify(f"{update.actor.display_name} replied: see {update.post_id}")
        case UpdateKind.REACTION_ADDED:
            notify(f"{update.actor.display_name} liked {update.about_post_id}")
        case UpdateKind.REPOST_ADDED:
            notify(f"{update.actor.display_name} reposted {update.about_post_id}")
        case UpdateKind.MESSAGE_RECEIVED:
            notify(f"New message in {update.conversation_id}")
        case UpdateKind.FOLLOWED:
            notify(f"{update.actor.display_name} followed you")
```

---

## Direct messages

```python
conversations = await account.read_conversations()
for conversation in conversations.items:
    who = ", ".join(
        person.display_name or person.handle or "?" for person in conversation.people
    )
    print(who, conversation.unread_count)
```

Reading and answering one:

```python
messages = await account.read_messages(conversation_id)  # newest first
for message in messages.items:
    print(message.sender.handle, message.text)

await account.send_message(conversation_id, "On my way!")
await account.mark_read(conversation_id)
```

Starting a new one, where the network allows it:

```python
from socialchimp import Feature

if Feature.START_CONVERSATIONS in await account.features():
    message = await account.start_conversation([other_person_id], "Hi there!")
    conversation_id = message.conversation_id
```

Meta cannot do this - the customer has to write first - so `Feature.MESSAGES`
can be on while `Feature.START_CONVERSATIONS` is off. Check the flag rather
than assuming both come together.

---

## Hiding a button a network does not support

`account.features()` is async and looks the connection up lazily, so it is
safe to call before deciding what to show:

```python
from socialchimp import Feature

features = await account.features()
show_like_button = Feature.LIKE in features
show_dm_tab = Feature.MESSAGES in features
```

Deciding what to show **before** anyone has connected an account - to grey
out a "reply from here" option in a network picker, say - use the client
itself, by platform name, with no connection needed:

```python
if Feature.READ_UPDATES_AFTER in sc.features("bluesky"):
    ...
```

Calling one of these methods on a network that lacks the feature raises
`NotSupportedError` naming the network and what it cannot do, rather than
doing nothing or returning an empty result - check the flag first so your UI
never gets there.

---

## Handling the new errors

Four error classes arrived with the social inbox - `MissingPermissionError`
and `BlockedError` (both a `NotAllowedError`), `PostGoneError` (a
`NotFoundError`), and `ReplyWindowClosedError` (reserved for Meta's 24-hour
reply window, in a later release). Mastodon and Bluesky raise the first
three today:

```python
from socialchimp import BlockedError, MissingPermissionError, PostGoneError

try:
    await account.reply(post_id, "Still here?")
except PostGoneError:
    show("That post isn't there any more.")
except BlockedError:
    show("You can't reply to that account.")
except MissingPermissionError as error:
    show(f"Reconnect this account: {error.needs} is missing.")
```

`MissingPermissionError.suggestion` is set where there is something more
useful to say than "reconnect" - Bluesky's DM gap says a new app password
has to be made, because the permission cannot be added to an existing one:

```python
try:
    await account.read_conversations()
except MissingPermissionError as error:
    print(error.needs, error.suggestion)
    # "direct messages" "A new app password is needed ..."
```

An `except NotAllowedError` or `except NotFoundError` written before 0.8.0
already catches all four - nothing breaks, you only get to be more specific
if you want to be.

A fifth thing to catch is not a new class: an unrecognised
`fetch_updates_after` or `mark_seen` marker raises the existing
`ConfigError` - see [Polling for updates with a stored
marker](#polling-for-updates-with-a-stored-marker) above.

---

## Testing with `FakePlatform`

Every call above works against `FakePlatform` with nothing installed beyond
socialchimp itself - no pytest, no network, no credentials:

```python
from socialchimp import Feature, InMemoryStorage, SocialChimp
from socialchimp.testing import FakePlatform

fake = FakePlatform(name="mastodon")
storage = InMemoryStorage()
sc = SocialChimp(storage=storage, platforms={"mastodon": fake})

connection = fake.connection()
await storage.save_connection(connection)
account = sc.account(connection.id)
```

Seed a post and a reply, then read the thread back exactly the way
`read_thread` would hand it to a real app:

```python
root = fake.add_post(text="Cabinets are back in stock.")
fake.add_reply(root.id, text="Grabbing one today!")

thread = await account.read_thread(root.id)
assert len(thread.replies) == 1
```

Seed a like, an update, and a conversation the same way:

```python
from socialchimp import Person, UpdateKind

fake.add_like(
    root.id,
    Person(
        id="p1",
        handle="rita@example.social",
        display_name="Rita",
        avatar_url=None,
        url=None,
    ),
)

fake.add_update(UpdateKind.COMMENT_CREATED, about_post_id=root.id)
batch = await account.fetch_updates_after(None)

buyer = Person(
    id="p2",
    handle="buyer@example.social",
    display_name="Buyer",
    avatar_url=None,
    url=None,
)
conversation = fake.add_conversation([buyer])
fake.add_message(conversation.id, buyer, "Is this still available?")

page = await account.read_messages(conversation.id)
assert page.items[0].text == "Is this still available?"
```

`fetch_updates_after(None)` on a fake behaves the same as a real network's
first call: it reads the latest page (`page_size`, or whatever `limit` you
pass), not the whole history you have ever seeded. And the same marker rule
applies:

```python
from socialchimp import ConfigError

with pytest.raises(ConfigError):
    await account.mark_seen("not-a-real-marker")
```

So a bug in your own marker storage shows up in a test rather than in
production.

Give `FakePlatform` fixed features to test what your app does when one is
missing:

```python
no_dms = FakePlatform(name="mastodon", features=Feature.POST_TEXT | Feature.READ_POST)
assert Feature.MESSAGES not in no_dms.features
```

---

## Per-network notes

- **Bluesky direct messages need an app password with "Allow access to your
  direct messages" ticked.** An app password made without it answers every
  DM call with `MissingPermissionError(needs="direct messages")`. The box
  cannot be turned on for an existing app password - a new one has to be
  made with it checked from the start.
- **Mastodon only has part of a conversation's history.** Mastodon has no
  "every message in this conversation" call, so `read_messages` reads the
  thread around the conversation's last message and keeps what belongs to
  it - `Conversation.full_history` is `False` to say so, and there is no
  further page to ask for.
- **Mastodon has no `liked_at`.** `Like.liked_at` is always `None` there;
  Bluesky fills it in.
- **A Bluesky quote's `about_post_id` is the post it quotes**, and it
  arrives as `UpdateKind.MENTION` - quoting is not replying, so it shares
  the "somebody is talking about you" shape a mention already has.
- **Push is not here yet.** Both networks are polled with
  `fetch_updates_after` in 0.8.0; Mastodon Web Push and Meta's webhooks are
  planned together for a later release.

---

## Elsewhere

- [Networks](../platforms.md#mastodon) - Mastodon and Bluesky's own pages,
  with the social inbox notes alongside everything else about them.
- [Capability matrix](../networks.md) - the same facts as a generated table.
- [Errors](../api/errors.md) - every error socialchimp raises, in full.
- [Testing helpers](../api/testing.md) - the full `FakePlatform` reference.
