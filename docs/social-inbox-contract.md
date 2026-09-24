# socialchimp: Social Inbox contract (v1.3, APPROVED by the owner 2026-09-24)

v1.1 change: `account.features` is an async method, not a property (see section 8).
v1.2 clarifications, found in review (the surface is unchanged): Mastodon reply visibility, direct messages checked
first when classifying updates, and the `status:` conversation id fallback.
v1.3 (from review): `PostResult.cid` comes after `raw`; an unknown marker raises `ConfigError`; a Bluesky quote's
`about_post_id` is the quoted post (from `reasonSubject`); a repeat delivery is possible, a lost update is not.

Owner decisions: (1) Mastodon polls in 0.8.0 and push comes later, built together with Meta webhooks;
`push` is added to Mastodon DEFAULT_SCOPES now so new connections are ready.
(2) Reposts and follows get their own UpdateKinds (REPOST_ADDED, FOLLOWED).

Target release: **0.8.0** (new public surface; nothing existing is removed or
changes meaning). All new data classes are `@dataclass(frozen=True, slots=True)`,
all times have a timezone, every model keeps `raw: RawData` (repr=False) for
anything not modelled. Every network call is `async`. Everything below is
reached through `Account` methods with the same names (the usual pattern).

Networks now: Mastodon, Bluesky. Shaped so Facebook Pages and Instagram fit
next without changes to this surface.

---------------------------------------------------------------------------
## 1. Shared shapes (models.py)

```python
@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    """One page of results."""

    items: tuple[T, ...]
    next: str | None = None  # pass back as `after=` for the next page; None = no more
```
Every list call takes `after: str | None = None, limit: int | None = None`
(limit None = network default; capped at the network's own maximum). `next`
is opaque: the app stores it as a string and never parses it. (Mastodon: taken from
the Link header. Bluesky / Meta: the network's cursor.)

```python
@dataclass(frozen=True, slots=True)
class Person:
    id: str  # Mastodon account id / Bluesky DID / Meta PSID, IGSID or user id
    handle: (
        str | None
    )  # "user@host" / "name.bsky.social"; None where the network has none (Meta messaging)
    display_name: str | None
    avatar_url: str | None
    url: str | None  # profile page
    raw: RawData


class Visibility(Enum):
    PUBLIC = "public"
    UNLISTED = "unlisted"  # Mastodon only
    FOLLOWERS = "followers"  # Mastodon "private"
    DIRECT = "direct"  # Mastodon "direct"


class LinkKind(Enum):
    MENTION = "mention"
    LINK = "link"
    TAG = "tag"


@dataclass(frozen=True, slots=True)
class TextLink:
    start: int  # character offsets into PostDetails.text (Python str indices)
    end: int
    kind: LinkKind
    target: str  # URL for LINK; person id for MENTION; tag name (no '#') for TAG
    url: str | None  # clickable URL where known


@dataclass(frozen=True, slots=True)
class Attachment:
    kind: str  # "image" | "video" | "gifv" | "audio" | "link" | "unknown"
    url: str | None
    preview_url: str | None
    alt_text: str | None
    width: int | None
    height: int | None
    raw: RawData


class Unavailable(Enum):
    DELETED = "deleted"  # post removed, or not found
    BLOCKED = "blocked"  # the author blocked us, or we blocked them
    HIDDEN = "hidden"  # hidden by moderation (Meta hidden comments; Bluesky labels/threadgate)


@dataclass(frozen=True, slots=True)
class PostDetails:
    id: str  # Mastodon status id / Bluesky at:// uri / Meta object id
    cid: str | None  # Bluesky content hash; None elsewhere
    url: str | None  # permalink on the network's website
    author: Person | None  # None only when unavailable is set
    text: str  # plain text (Mastodon HTML converted); "" when unavailable
    html: (
        str | None
    )  # the network's own HTML (Mastodon). UNTRUSTED: the app must sanitise
    links: tuple[TextLink, ...]
    attachments: tuple[Attachment, ...]
    created_at: datetime | None
    visibility: Visibility | None  # None = the network has no such idea (Bluesky, Meta)
    parent_id: str | None  # the post this replies to
    root_id: str | None  # top of the thread (== id for a top-level post)
    reply_count: int | None  # None = the network does not say, never "zero"
    like_count: int | None
    repost_count: int | None
    quote_count: int | None
    liked_by_me: bool | None  # None = unknown
    my_like_id: str | None  # Bluesky like-record uri; pass to unlike() to save a lookup
    is_mine: bool  # authored by the connected account
    unavailable: Unavailable | None  # set for placeholders in a thread
    raw: RawData
```

`PostResult` (existing) gains one optional field, `cid: str | None = None`, placed AFTER `raw`.
Bluesky fills it. Adding it does not break existing callers.

---------------------------------------------------------------------------
## 2. Reading posts and threads (task 1, 2)

```python
@runtime_checkable
class CanReadPost(Protocol):
    async def read_post(self, connection: Connection, post_id: str) -> PostDetails: ...


@runtime_checkable
class CanReadThread(Protocol):
    async def read_thread(
        self,
        connection: Connection,
        post_id: str,
        *,
        depth: int
        | None = None,  # how many reply levels to fetch; None = network default
        limit: int | None = None,  # cap on replies returned
    ) -> Thread: ...


@dataclass(frozen=True, slots=True)
class Thread:
    post: PostDetails  # the post asked for
    replies: tuple[
        PostDetails, ...
    ]  # flat, oldest first; build the tree with parent_id
    complete: bool  # False if depth/limit/network caps cut replies off
    raw: RawData
```
- Mastodon: `GET /api/v1/statuses/:id` + `/context` (descendants; the network allows up to
  4096 when signed in). No pagination exists. `depth`/`limit` are applied by the library.
  `complete=False` when capped. A `Mastodon-Async-Refresh` header (remote replies still
  arriving) also gives `complete=False`.
- Bluesky: `app.bsky.feed.getPostThread` (depth default 6, max 1000; parentHeight=0).
  `notFoundPost`/`blockedPost` become placeholders with `unavailable` set.
- Meta later: comments are one level deep on Instagram and two on Facebook. The flat-list-plus-
  `parent_id` shape fits both.
- The existing `CanReadReplies.read_replies` (returns `Update`s) stays as it is.

---------------------------------------------------------------------------
## 3. Replying to a comment (task 3)

```python
@runtime_checkable
class CanReply(Protocol):
    async def reply(
        self,
        connection: Connection,
        post_id: str,
        text: str,
        *,
        media: tuple[Media, ...] = (),
        options: RawData | None = None,  # per-network extras, as Post.options
    ) -> PostResult: ...
```
- `post_id` can be any post or comment at any depth.
- Bluesky: builds root and parent from the target (one `getPosts` lookup, as publish does today).
  Returns `id` (uri), `cid`, `url`.
- Mastodon: a reply to a `direct` or `followers` parent keeps that visibility. Otherwise
  the library sends no visibility, so the account's own default applies. A visibility passed
  in `options` is narrowed to the parent's. Adds `@acct` of the parent's
  author (plus other people mentioned in the parent, as the Mastodon web app does) unless
  already in `text`, and never mentions the connected account itself.
- `publish(Post(reply_to=...))` keeps working. `reply()` is the recommended path.
- The existing `CanReplyToUpdates.reply_to_update(update, text)` stays (Threads, Google Business).

---------------------------------------------------------------------------
## 4. Likes (task 4, 5)

```python
@runtime_checkable
class CanLike(Protocol):
    async def like(self, connection: Connection, post_id: str) -> LikeResult: ...
    async def unlike(
        self, connection: Connection, post_id: str, *, like_id: str | None = None
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class LikeResult:
    post_id: str
    like_id: (
        str | None
    )  # Bluesky like-record uri; None on Mastodon/Meta (nothing to keep)
    raw: RawData


@runtime_checkable
class CanReadLikes(Protocol):
    async def read_likes(
        self,
        connection: Connection,
        post_id: str,
        *,
        after: str | None = None,
        limit: int | None = None,
    ) -> Page[Like]: ...


@dataclass(frozen=True, slots=True)
class Like:
    person: Person
    liked_at: datetime | None  # Bluesky has it; Mastodon does not (always None)
    raw: RawData
```
- Both calls are idempotent. Liking twice or unliking something not liked succeeds and does nothing.
  - Mastodon: favourite and unfavourite are already idempotent on the server.
  - Bluesky: `createRecord` is NOT deduplicated by the network, so `like()` reads `viewer.like`
    first and returns the existing like instead of making a second one. `unlike()`
    uses `like_id` when given (0 lookups). Otherwise it looks up `viewer.like`
    (1 lookup), then `deleteRecord` by rkey. Deleting a like that is already gone succeeds.
- Liking a comment is the same call with the comment's id.
- Meta later: Facebook Pages can like posts and comments (`CanLike`). A Page can't list
  likers (only the count), so it will not implement `CanReadLikes`. Instagram does neither.

---------------------------------------------------------------------------
## 5. Updates: polling with a resumable marker (task 6)

`Update` (events.py) gains optional fields, all defaulting to None/empty:
```python
actor: Person | None  # who did it
post_id: str | None  # the thing that happened: the reply/mention/message post itself
about_post_id: (
    str | None
)  # the connected account's OWN post it concerns (liked / reposted / replied to)
thread_root_id: str | None  # top of the thread, when known without an extra request
conversation_id: str | None  # for MESSAGE_RECEIVED
```
New `UpdateKind`s: `REPOST_ADDED`, `MESSAGE_RECEIVED`, `FOLLOWED`.
- Likes → `REACTION_ADDED`, replies → `COMMENT_CREATED`, mentions → `MENTION`.
- CHANGE: Mastodon and Bluesky reposts move from `REACTION_ADDED` to `REPOST_ADDED`. The
  "follow" notification moves from `UNKNOWN` to `FOLLOWED`. This goes in the changelog under
  "Changed" (0.x minor bump).
- Mastodon `mention` notification (a direct-visibility status is checked FIRST):
  - A direct-visibility status comes out as `MESSAGE_RECEIVED`, even when it also replies to the
    connected account.
  - Otherwise, a reply comes out as `COMMENT_CREATED` when `status.in_reply_to_account_id` is the connected
    account. `about_post_id` = `in_reply_to_id`.
  - Anything else is `MENTION`.
- Bluesky: `reasonSubject` becomes `about_post_id` for like, repost and quote (a quote comes through as
  `MENTION`). For reply and mention, the post's `record.reply.parent.uri` becomes `about_post_id` and
  `record.reply.root.uri` becomes `thread_root_id` when the post is a reply.
- Mastodon `thread_root_id` stays None unless asked for (it needs a `/context` call).

```python
@runtime_checkable
class CanReadUpdatesAfter(Protocol):
    async def fetch_updates_after(
        self,
        connection: Connection,
        marker: str | None,
        *,
        limit: int | None = None,
    ) -> UpdateBatch: ...
    async def mark_seen(self, connection: Connection, marker: str) -> None: ...


@dataclass(frozen=True, slots=True)
class UpdateBatch:
    updates: tuple[Update, ...]  # oldest first, only ones newer than `marker`
    marker: (
        str | None
    )  # store it; pass it next time. None only if nothing was ever seen
    more: bool  # True = more new updates waiting; call again straight away
```
- `marker=None` on first use returns the latest page, which sets a starting point.
- A marker string the platform did not make raises `ConfigError` (`fetch_updates_after` and
  `mark_seen`). The library never silently starts over, because that would drop updates. Pass None to start afresh.
- Updates can occasionally come back twice (Bluesky: same `indexedAt` as the marker). They are never lost.
  Dedupe by `Update.id`.
- Mastodon: marker = newest notification id. Fetched with `min_id=` (pages forward with no
  gaps). `mark_seen` → `POST /api/v1/markers` (notifications).
- Bluesky: the network's cursor only pages backwards, so marker = newest `indexedAt`
  plus uri. The library pages back until it reaches the marker. `mark_seen` →
  `app.bsky.notification.updateSeen`.
- Existing `fetch_updates(connection, since)` and `Poller` stay unchanged.

---------------------------------------------------------------------------
## 6. Push delivery (owner addition)

Pushed and polled updates are the SAME enriched `Update`, so the app has one handler path.

| Network | Push? | How | Payload | Lifecycle |
|---|---|---|---|---|
| Mastodon | Yes, 2 ways | (a) Web Push `POST /api/v1/push/subscription` to the app's HTTPS endpoint; (b) streaming WebSocket (`user:notification`, `direct`) | (a) encrypted (RFC 8291/VAPID) and holds only `notification_id` + type, so the library fetches `GET /notifications/:id` to build the full Update; (b) full entities | (a) one subscription per token (a new one replaces the old); needs the **`push` scope, which is NOT in "read write"**, so existing connections must reconnect; DELETE on disconnect. (b) one open socket per account |
| Bluesky | No | No webhooks. The firehose/Jetstream can't filter to one account's likes and replies (those live in other people's repos), so it would mean reading the whole network | n/a | **Polling (`fetch_updates_after`) is the supported path.** DMs have a resumable log (`chat.bsky.convo.getLog`), also polled |
| Meta (next) | Yes | Webhooks (existing `CanAnswerSetupCheck` / `CanCheckSignature` / `CanReadPushedUpdates`) + per-page `POST /{page-id}/subscribed_apps` | id/summary, hydrated when needed | subscribe on connect, `DELETE subscribed_apps` on disconnect; no renewal; a revoked token silently stops delivery |

**0.8.0 ships none of the push protocols below.** Mastodon and Bluesky both use
`fetch_updates_after`. `DEFAULT_SCOPES` becomes `("read", "write", "push")` so new
Mastodon connections can use Web Push later without reconnecting. The shapes below are RESERVED
for the release that adds Meta webhooks and Mastodon Web Push, and may be refined then:
```python
@runtime_checkable
class CanSubscribeToUpdates(Protocol):
    async def subscribe(
        self,
        connection: Connection,
        *,
        endpoint_url: str,
        kinds: frozenset[UpdateKind] | None = None,
    ) -> Subscription: ...
    async def unsubscribe(
        self, connection: Connection, subscription: Subscription
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class Subscription:
    id: str
    connection_id: str
    endpoint_url: str
    expires_at: (
        datetime | None
    )  # None = never expires on its own (renew = subscribe again)
    secrets: RawData = field(
        repr=False
    )  # e.g. Web Push private key + auth secret; the app stores this
    raw: RawData


@runtime_checkable
class CanReadPushedUpdatesFor(
    Protocol
):  # push bodies that need the connection (decrypt + fetch)
    async def read_pushed_updates(
        self,
        connection: Connection,
        subscription: Subscription,
        body: bytes,
        headers: Mapping[str, str],
    ) -> list[Update]: ...
```
The existing sync `read_updates(body)` stays for Meta-style signed webhooks that
carry enough to build the Update without the connection.

---------------------------------------------------------------------------
## 7. Direct messages (task 7)

```python
@runtime_checkable
class CanMessage(Protocol):
    async def read_conversations(
        self,
        connection: Connection,
        *,
        after: str | None = None,
        limit: int | None = None,
    ) -> Page[Conversation]: ...
    async def read_messages(
        self,
        connection: Connection,
        conversation_id: str,
        *,
        after: str | None = None,
        limit: int | None = None,
    ) -> Page[Message]: ...
    async def send_message(
        self,
        connection: Connection,
        conversation_id: str,
        text: str,
        *,
        options: RawData | None = None,
    ) -> Message: ...  # options: e.g. Meta message tag
    async def mark_read(self, connection: Connection, conversation_id: str) -> None: ...


@runtime_checkable
class CanStartConversations(Protocol):  # Meta can't: the customer must write first
    async def start_conversation(
        self, connection: Connection, person_ids: Sequence[str], text: str
    ) -> Message: ...


@dataclass(frozen=True, slots=True)
class Conversation:
    id: str
    people: tuple[Person, ...]  # everyone except the connected account
    last_message: Message | None
    unread_count: int | None  # Mastodon gives only yes/no, so it reports 1 or 0
    updated_at: datetime | None
    can_reply_until: datetime | None  # Meta 24-hour window; None = no deadline
    full_history: bool  # False on Mastodon (see below)
    raw: RawData


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    conversation_id: str
    sender: Person
    text: str  # "" when deleted
    sent_at: datetime
    is_mine: bool
    deleted: bool
    attachments: tuple[Attachment, ...]
    raw: RawData
```
Messages come back in `Page`s, newest first. `after` goes further back in time.
- Mastodon: conversations = `GET /api/v1/conversations`. Mastodon has no "all
  messages" call, so `read_messages` = `/context` of `last_status`, keeping only direct
  statuses between the same people. That's why `full_history=False`, and it never returns a `next`.
  `send_message` = a direct status that mentions every participant, as a reply to the
  last status. `mark_read` = `POST /conversations/:id/read`. `start_conversation`
  = a direct status that mentions the given accounts. Its `conversation_id` is the real
  Mastodon conversation id. In the rare case Mastodon has not listed the conversation yet, it is
  `"status:<status id>"`; `send_message`, `read_messages` and `mark_read` accept that form too.
  Treat it as opaque either way.
- Bluesky: `chat.bsky.convo.listConvos` / `getMessages` / `sendMessage` /
  `updateRead`, plus `getConvoForMembers` for `start_conversation`. Every call sends the header
  `atproto-proxy: did:web:api.bsky.chat#bsky_chat`. An app password made without
  "Allow access to your direct messages" answers HTTP 400 `{"error":"InvalidToken","message":"Bad token method"}` (atproto pds pipethrough.ts), which raises
  `MissingPermissionError(needs="direct messages")` with the fix. (A new app password is needed,
  because the box can't be ticked later.)
- Meta later: PSID/IGSID are `Person.id`. The 24-hour window is `can_reply_until`, and sending
  after it closes raises `ReplyWindowClosedError`. A tag like `HUMAN_AGENT` goes in `options`.

---------------------------------------------------------------------------
## 8. Asking what a network supports (task 8)

New `Feature` flags: `READ_POST`, `READ_THREAD`, `REPLY_TO_COMMENTS`, `LIKE`,
`READ_LIKES`, `READ_UPDATES_AFTER`, `SUBSCRIBE_UPDATES`, `MESSAGES`,
`START_CONVERSATIONS` (`SUBSCRIBE_UPDATES` is reserved and added with the push release). Each matches one protocol above, and the tests enforce that
the flag and the protocol always agree.

```python
await account.features() -> Feature    # async: Account looks its connection up lazily
client.features("mastodon") -> Feature # sync, by platform name, no connection needed
Feature.LIKE in await account.features()   # how the UI decides to show a button
```
Mastodon: all except `SUBSCRIBE_UPDATES` (Web Push comes in a later release).
Bluesky: all except `SUBSCRIBE_UPDATES`.

---------------------------------------------------------------------------
## 9. Errors (task 9)

Existing ones, unchanged: `RateLimitError(retry_after)`, `NotAllowedError`, `NotFoundError`,
`AuthError`, `NotSupportedError`, `PlatformError`, `NetworkError`.

New subclasses, so existing `except` blocks still catch them:
```python
class MissingPermissionError(NotAllowedError):   # needs: str, suggestion: str | None
class BlockedError(NotAllowedError):             # the other person blocked us / we blocked them
class ReplyWindowClosedError(NotAllowedError):   # closed_at: datetime | None (Meta)
class PostGoneError(NotFoundError):              # target post/comment deleted or never existed
```
- `retry_after` now also reads `X-RateLimit-Reset` (Mastodon) and `RateLimit-Reset`
  (Bluesky) when `Retry-After` is absent.
- Mastodon: 403 with a block message → `BlockedError`; 404 on a status → `PostGoneError`;
  403 on a missing scope → `MissingPermissionError`.
- Bluesky: `BlockedActor` / `BlockedByActor` → `BlockedError`; `NotFound` / a `notFoundPost` target
  → `PostGoneError`; `InvalidToken` + "Bad token method" on a chat call → `MissingPermissionError`.

---------------------------------------------------------------------------
## 10. Permissions / reconnect

- Mastodon "read write" covers everything here except Web Push (`push`). No reconnect is needed
  for tasks 1-5 and 7.
- Bluesky: DMs need an app password with DM access. Other features work with any app password.
