# Bluesky / atproto fixtures

Fixtures for socialchimp's social-inbox tests: reading a post, its thread and
likes, notifications, and DMs. Merchant account is `fridgedoor.bsky.social`
(`did:plc:dc7hcchu6gliiknwqdeffydi`). All DIDs, TIDs (record keys / revs) and
CIDs are fake but shaped exactly like real ones (`did:plc:` + 24
base32-sortable characters, 13-character TID record keys, 59-character
`bafyrei…`/`bafkrei…` CIDv1 strings) so length/format assertions in code stay
honest. Every fixture was built from the lexicon JSON in
`bluesky-social/atproto` (fetched from GitHub) plus a few live, unauthenticated
calls to `public.api.bsky.app` to confirm real-world shapes (actor
`bsky.app`, its pinned post). No fixture was invented from memory of the
shape alone.

- **get_posts_own.json** — `app.bsky.feed.getPosts` for the merchant's post
  (`lexicons/app/bsky/feed/getPosts.json` + `feed/defs.json#postView`,
  `richtext/facet.json`, `embed/images.json`). Two images with alt text and a
  blob ref on the record side, an `app.bsky.embed.images#view` on the view
  side, a mention/link/tag facet set, and `viewer: {}` (no like yet). The post
  text puts an emoji, an accented "café" and a checkmark before the facets so
  `byteStart`/`byteEnd` (UTF-8 bytes) diverge from character offsets — verified
  by decoding the byte slices back out (139 bytes vs. 133 characters for the
  full text).
- **get_posts_liked.json** — same call, after the merchant likes their own
  post: `viewer.like` is set to an `at://.../app.bsky.feed.like/<rkey>` URI
  and `likeCount` is one higher, matching what the AppView would show once
  the like round-trips.
- **get_post_thread.json** — `app.bsky.feed.getPostThread`
  (`feed/getPostThread.json` + `feed/defs.json#threadViewPost` /
  `#notFoundPost` / `#blockedPost`). Root post's `replies` has 5 entries: two
  plain direct replies (one of which itself carries 2 nested replies), one
  direct reply from the merchant, one `#notFoundPost`, and one `#blockedPost`
  (author only exposes `did` + `viewer.blockedBy`, per lexicon). Every reply's
  `record.reply.{root,parent}` is a `com.atproto.repo.strongRef`
  (`uri`+`cid`) pointing at the real root/parent, not a view union — that shape
  comes from `lexicons/app/bsky/feed/post.json#replyRef`. No `threadgate` key,
  as requested. One judgment call: posts with no children omit the `replies`
  key entirely rather than sending `"replies": []`; both are legal per the
  lexicon (the field is optional) and real responses do both depending on
  path, so this is a guess, not a confirmed shape.
- **create_record_like.json** — `com.atproto.repo.createRecord` response for
  the like above (`lexicons/com/atproto/repo/createRecord.json` +
  `repo/defs.json#commitMeta`): `uri`, `cid`, `commit.{cid,rev}` (`rev` is a
  TID), `validationStatus: "valid"`.
- **get_likes.json** — `app.bsky.feed.getLikes`
  (`lexicons/app/bsky/feed/getLikes.json`), 3 likes plus a `cursor`. The
  `cursor` and each like's `createdAt`/`indexedAt` are real timestamp strings,
  matching what a live call to `public.api.bsky.app` for `getLikes` actually
  returned (cursor there is the oldest returned like's `indexedAt`).
- **list_notifications.json** — `app.bsky.notification.listNotifications`
  (`lexicons/app/bsky/notification/listNotifications.json`). Six reasons:
  `like`, `repost`, `reply` (reusing the thread's real reply so uris/cids
  line up), `mention`, `quote`, `follow`. `reasonSubject` is set for
  like/repost/reply/quote (the post being acted on) and left out for
  mention/follow — the lexicon only documents `reasonSubject` as "at-uri",
  optional, with no enumerated rule for which reasons set it, so this split
  follows the field's evident purpose and observed AppView behaviour rather
  than an explicit spec statement; flagging as the one soft inference in this
  set. `isRead` is mixed, `cursor` and `seenAt` are both present (`seenAt` is
  a real field on the output schema, separate from the deprecated `seenAt`
  *parameter*).
- **chat_list_convos.json**, **chat_get_messages.json**,
  **chat_send_message.json**, **chat_update_read.json**,
  **chat_get_convo_for_members.json** — from `chat.bsky.convo` lexicons
  (`listConvos.json`, `getMessages.json`, `sendMessage.json`,
  `updateRead.json`, `getConvoForMembers.json`, `convo/defs.json`).
  `chat_get_messages.json` has a `messageView` with a link facet, a
  `deletedMessageView`, and a plain older `messageView`, newest first, plus a
  `cursor` and `relatedProfiles`. `chat_send_message.json` is a bare
  `messageView` (the lexicon's `sendMessage` output ref points straight at
  `chat.bsky.convo.defs#messageView`, it is not wrapped in an object).
  `convoView.kind` is stamped `{"$type":"chat.bsky.convo.defs#directConvo"}`
  since it is a union field even though `directConvo` itself has no
  properties.
- **errors.json** — example XRPC error bodies with their real HTTP status,
  sourced from `bluesky-social/atproto` server code (via `gh api`/`gh search
  code`), not guessed:
  - `post_thread_not_found`: `getPostThread` when the anchor post can't be
    hydrated throws `InvalidRequestError('Post not found: <uri>', 'NotFound')`
    (`packages/bsky/src/api/app/bsky/feed/getPostThread.ts`) → **400**, not
    404.
  - `blocked_actor` / `blocked_by_actor`: `getAuthorFeed`/`getActorLikes`
    throw `InvalidRequestError` with `'BlockedActor'` /
    `'BlockedByActor'` (`packages/bsky/src/api/app/bsky/feed/getAuthorFeed.ts`)
    → 400. Note `getPostThread` and `getProfile` do **not** carry these named
    errors in their own lexicons — blocked posts show up inline as
    `#blockedPost` in a thread instead, and `getProfile` has no `errors` list
    at all.
  - `invalid_token` / `expired_token_pds`: from
    `packages/pds/src/auth-verifier.ts` — a malformed JWT is
    `InvalidRequestError('Malformed token', 'InvalidToken')`, and an expired
    access JWT handled directly by the PDS is
    `InvalidRequestError('Token has expired', 'ExpiredToken')` — both → 400.
  - `expired_token_appview`: the same expired-JWT condition, but
    `packages/bsky/src/auth-verifier.ts` (the AppView's own verifier) throws
    `AuthRequiredError('Token has expired', 'ExpiredToken')` → **401**. Which
    status you actually see depends on whether the failing call lands on the
    PDS or is proxied through the AppView, so both variants are included.
  - `dm_scope_forbidden`: **the requested DM-without-permission case.** A
    regular app password's access token carries scope
    `com.atproto.appPass`; only a password created with "Allow this app
    password to access your direct messages" checked carries
    `com.atproto.appPassPrivileged`. `chat.bsky.*` methods are in that
    privileged set, so a non-privileged token calling them hits
    `packages/pds/src/pipethrough.ts`, which throws
    `InvalidRequestError('Bad token method', 'InvalidToken')` → **HTTP 400**,
    body `{"error":"InvalidToken","message":"Bad token method"}`. This is
    confirmed end-to-end by
    `packages/pds/tests/app-passwords.test.ts` ("restricts privileged app
    password actions" → `rejects.toThrow('Bad token method')`), and matches
    the underlying scope model in
    `packages/pds/tests/app-passwords.test.ts` lines around the
    `com.atproto.appPass`/`com.atproto.appPassPrivileged` scope assertions.
    Note the message is **"Bad token method"**, not the commonly guessed
    "Bad token scope" (that string is real too, but it's thrown by
    `auth-verifier.ts`'s own scope-membership check for a token whose scope
    isn't recognised at all — a different, narrower case than the
    privileged-method gate that DM access actually falls under).
  - `rate_limit_exceeded`: 429 with `RateLimit-Limit` / `RateLimit-Remaining`
    / `RateLimit-Reset` (unix seconds) / `RateLimit-Policy` (`"<limit>;w=<window
    seconds>"`) and `Retry-After`, exactly as set in
    `packages/xrpc-server/src/rate-limiter-http.ts`, body
    `{"error":"RateLimitExceeded","message":"Rate Limit Exceeded"}`.

## Uncertainties worth flagging

- The `feed_thumbnail`/`feed_fullsize` CDN URL shape used for image embeds
  (`https://cdn.bsky.app/img/feed_thumbnail/plain/<did>/<cid>@jpeg`) is
  inferred from the confirmed `avatar`/`banner` CDN URL convention seen on a
  live `getProfile` call; it was not independently confirmed against a live
  image post fetch in this session.
- Whether a real `getPostThread` reply with zero children omits `replies`
  entirely or returns `"replies": []` is not settled by the lexicon (the
  field is optional either way); this fixture set omits it, as noted above.
- The `mention`/`follow` reasons omitting `reasonSubject` is inferred from
  the field's purpose and typical AppView output, not from an explicit line
  in the lexicon.
