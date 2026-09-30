# Instagram fixtures

Response and webhook bodies for socialchimp's social-inbox tests against
**Instagram API with Instagram Login** (host `graph.instagram.com`, scope
`instagram_business_manage_messages`). Our account is `fridgedoor`, IG
professional account id `17841400000000000`. The customer is `ada.bakes`,
IGSID `1234567890123456`. Every message id and conversation id is made up
(long base64-ish strings starting `aWdfZAG1...`, generated from hashes); CDN
URLs and `fbtrace_id`s are fake too.

Meta's Instagram docs are thin on payload examples. Most of these bodies are
the documented shape plus field names from the Graph/Messenger references,
and I say below where that is a guess. **Nothing here was captured from a
live account.**

Doc pages (all under https://developers.facebook.com):

- [MSG] `/docs/instagram-platform/instagram-api-with-instagram-login/messaging-api` (Send Messages). Anchors I could not confirm: the page has sections "Send a text message", "Send Images", "Send audio, video or file", "Send a Sticker", "React or unreact to a message", "Send a Published Post". Use the page URL plus the heading text.
- [CONV] `/docs/instagram-platform/instagram-api-with-instagram-login/conversations-api`
- [HOOKS] `/docs/instagram-platform/webhooks` (sections "Event Notifications", "Payload Contents", "Enable Subscriptions", "Subscribe to webhook fields")
- [HOOKREF] `/docs/graph-api/webhooks/reference/instagram` (field-by-field reference)
- [ERR] `/docs/messenger-platform/error-codes/` (heading "Common Error Codes")
- [RATE] `/docs/graph-api/overview/rate-limiting/` (Instagram messaging limits section; the fetched text showed no heading id)
- [START] `/docs/instagram-platform/instagram-api-with-instagram-login/get-started` (section "Get the app user ID & username", anchor `#fields`)
- [SENDER] `/docs/messenger-platform/send-messages/sender-actions` (Messenger page, not Instagram-specific)
- [TAGS] `/docs/messenger-platform/send-messages/message-tags` (Messenger page; human agent tag)

No anchors exist that I verified beyond `#fields`. Fragments I guessed would
be worse than none, so use the page plus the heading.

- **conversations.json** — `GET /v21.0/me/conversations?platform=instagram&fields=id,updated_time,participants,messages.limit(20){...}`
  (also `/{IG_ID}/conversations`). Two conversations, `paging` with
  `cursors.before/after` and `next`, inner `messages` has its own `paging`.
  Source: [CONV] for the endpoint and `platform=instagram`. **Uncertainty:**
  [CONV] shows only `id` and `updated_time` on the list and never shows
  `participants` or nested `messages{}` on it. Expanding them is ordinary
  Graph field expansion and I believe it works, but it is unverified. Both
  participants are listed, the business with its IG professional account id
  and username. That `participants` shape is from memory of the Messenger
  conversations API, and [CONV]'s message example uses `{username,id}` for
  `from` and `to.data[]`, so I copied that. Version: the docs now show
  `v25.0`, we were asked for `v21.0`; bodies don't differ that I know of.

- **conversation_by_user.json** — `GET /me/conversations?platform=instagram&user_id=<IGSID>`.
  Source: [CONV] ("Find specific conversation" example uses `user_id`).
  Without `fields`, only `id` and `updated_time` come back per [CONV]'s
  list example.

- **messages.json** — three shapes in one file.
  `conversation_node` is `GET /{conversation-id}?fields=messages{...}`, the
  only way [CONV] documents. `messages_edge` is
  `GET /{conversation-id}/messages?fields=...`; **[CONV] does not document
  this edge for Instagram Login**, it works on the Messenger side and I
  assume it does here, so treat it as unconfirmed. `single_message` is
  `GET /{message-id}?fields=id,created_time,from,to,message`, documented in
  [CONV] (their example id `aWdGGiblWZ...`, `created_time` like
  `2022-07-12T19:11:07+0000`). Newest message first. [CONV]: "You can only
  get details about the 20 most recent messages in the conversation", so
  older messages exist but come back without fields. Content: plain text from
  customer and business, image (`image_data`), video (`video_data`), audio and
  PDF (`file_url`), a share (`shares.data[].link`), story mention
  (`story.mention`), story reply (`story.reply_to`), `is_unsupported: true`
  and `reactions`. **Uncertainty, all of the following are from the
  Messenger conversations reference and real-world reports, not from the
  Instagram pages:** the inner keys of `image_data`/`video_data`
  (`width,height,max_width,max_height,url,preview_url,render_as_sticker`;
  `length,video_type`), using `file_url` for audio and files (Instagram voice
  clips may arrive as unsupported instead), `shares.data[]` having
  `name`/`description`, the `story.mention`/`story.reply_to` `{link,id}`
  pair, and `reactions.data[] = {reaction, users[]}` (reaction names such as
  `love`; whether `users` carries `username` is a guess). Empty-text
  attachment-only messages may omit `message` rather than send `""`; I sent
  `""`. A reaction sent via the API is `sender_action: react`, which is how
  [MSG] names it.

- **send_message.json** — `POST /me/messages` (or `/{IG_ID}/messages`) with
  `{"recipient":{"id":IGSID},"message":{"text":...}}`. Response from [MSG]:
  `{"recipient_id","message_id"}`. Text max 1000 bytes per [MSG].

- **mark_seen.json** — `{"recipient_id": ...}`, the response shape Messenger
  gives for sender actions ([SENDER]). **Uncertainty, important:** [MSG] for
  Instagram Login lists only `react` and `unreact` as sender actions. [SENDER]
  lists `mark_seen`, `typing_on`, `typing_off`, and Meta's search snippets
  say they are supported on Instagram, but I found no Instagram Login page
  that states it. Treat `mark_seen` support as unconfirmed and test it with a
  real token before depending on it. [MSG] also notes that webhook messages
  are not marked read in the app inbox until a reply is sent.

- **errors.json** — each entry is `{http_status, body}`; the body is the usual
  Graph `{"error": {...}}`. Code/subcode pairs and titles are from the [ERR]
  table (verified to the extent that I read the table): `10/2534022` "This
  message is sent outside of allowed window", `10/2018278`, `10/2018108`
  "They can't receive your messages right now", `551` (message on the page
  "This person isn't receiving messages from you right now", and `551/1545041`
  "This person isn't available right now"), `200/2534041` "Account owner
  disabled Instagram DM access", `4`, `613/2534040` "Calls to this api have
  exceeded the rate limit", plus `200` and `10` for permission problems.
  **Uncertainty:** the exact `message` strings (the "(#NN)" prefixes,
  `error_user_title`/`error_user_msg` on 2534022, `type`, `is_transient`,
  HTTP status, `fbtrace_id`) are reconstructed from memory of Graph errors
  and forum reports; [ERR] gives only code, subcode and a short title.
  `2534014` "requested user cannot be found" (code 100) comes from
  third-party write-ups, not [ERR]. Codes `32` and `17` are standard Graph
  Page/user rate limit codes that I could not tie to Instagram Login; include
  them in handling but don't rely on them. `missing_human_agent_permission_UNVERIFIED`
  is a guess (code 10 without a subcode): no official page I found shows the
  body. Which of `2534022` or `2018278` Instagram Login returns for an
  expired window is not clear; [ERR] lists both, so handle both.

- **webhook_messaging.json** — name -> full body
  `{"object":"instagram","entry":[{"time","id","messaging":[...]}]}`.
  Field names from [HOOKREF]: message has `mid`, `text`, `attachments[]`
  (`type`, `payload`), `reply_to` (`mid` or `story{url,id}`), `is_deleted`,
  `is_self`, `is_echo`, `is_unsupported`; reaction has `mid`, `action`
  (`react`/`unreact`), `emoji`, `reaction`; seen has `read.mid`; postback has
  `title`, `payload`, `mid`. `timestamp` is milliseconds; `entry.time` is
  milliseconds in these fixtures too (see below). Attachment `type` strings:
  `image`, `video`, `audio`, `file`, `share`, `story_mention`, `ig_reel` are
  ones I'm fairly sure of. I also saw `reel` mentioned for shared reels, but
  could not confirm it, so there is no fixture for it. Other types exist too
  (`fallback` for things Meta can't render), and reel shares from private
  accounts, voice messages and GIPHY GIFs are documented to arrive as
  unsupported. **Uncertainty:** `is_echo` messages carry `app_id` (from
  Messenger; not checked for Instagram Login); the `share` payload may have
  only `url` (I gave only `url`); the `ig_reel` payload keys
  (`reel_video_id`, `title`, `url`) are from Messenger/IG-Login forum
  reports; `quick_reply.payload` and the postback body mirror Messenger;
  the `unreact` body may omit `reaction`/`emoji`; whether `entry.time` is
  seconds or milliseconds for messaging is uncertain ([HOOKS] example uses
  seconds, `1520383571`, and messaging events use ms for `timestamp`).
  `dashboard_test_notification_changes_form` is the one payload [HOOKS]
  actually prints (a dashboard "Test" send, `changes[].field` form, with
  `is_echo` and `is_self` true); the real messaging events use `messaging[]`.

## Ids

- `entry.id` is the IG professional account id (`17841400000000000`). [START]:
  `user_id` from `GET /me?fields=user_id,username` "is the value of the `id`
  field received in webhook notifications for this account". The `id` field
  on that same call is the **app-scoped** id and is a different number.
  Whether the token-exchange response's `user_id` equals the professional
  account id: [START] does not spell it out, but I believe the two match.
  Verify on a live account.
- Customer messages: `sender.id` = IGSID, `recipient.id` = `17841400000000000`.
  Echoes and our own sends are the reverse.
- In conversation objects `participants`/`from`/`to` use the same two ids
  (IGSID for the customer, IG professional account id for us).

## Other known uncertainties

- Field list requested for messages (`shares`, `story`, `is_unsupported`,
  `reactions`) are accepted by Instagram's Graph API in practice per forum
  reports, but only `id,created_time,from,to,message` is in [CONV].
- Requests-folder conversations inactive for 30 days are not returned ([MSG]).
