# Mastodon fixtures

Realistic Mastodon REST API response bodies for socialchimp's social-inbox
tests: reading a post, its thread, who favourited it, notifications, and
direct messages. Our own account is `fridgedoor` on `social.example`; every
other account lives on `other.example`.

Built from https://docs.joinmastodon.org (entities and methods pages) and
from the `mastodon/mastodon` GitHub repo's `main` branch source — mostly
`app/serializers/rest/*.rb`, `app/models/notification.rb` and its
`Notification::Groups` concern, `app/lib/text_formatter.rb`,
`app/controllers/api/base_controller.rb`,
`app/controllers/concerns/api/error_handling.rb`,
`app/controllers/concerns/api/rate_limit_headers.rb`, and
`app/policies/status_policy.rb`. Each entry below says which of those it
leans on most.

A general note on shape: attributes that Ruby's `ActiveModel::Serializer`
declares with an `if:` condition (e.g. `pinned`, `application`, `noindex`,
`roles`) are **absent from the JSON**, not `null`, when the condition is
false. These fixtures follow that: `pinned` and `application` only appear on
`fridgedoor`'s own statuses (the caller's own account), `roles`/`noindex`
only appear on the local account, and `status` is missing entirely from the
`follow` notification (it isn't about a status).

- **status_own_post.json** — a public status with two images, a mention, a
  hashtag, a link and a card. Based on `entities/Status`, `entities/Account`,
  `entities/MediaAttachment`, `entities/PreviewCard`, and
  `status_serializer.rb` / `account_serializer.rb` /
  `media_attachment_serializer.rb` for exact field lists and which
  attributes are conditional. The mention/hashtag/link HTML markup is copied
  from `app/lib/text_formatter.rb`'s `link_to_mention`, `link_to_hashtag`
  and `shortened_link` methods. **Uncertainty:** current `text_formatter.rb`
  emits link `rel="nofollow noopener"` with a separate `translate="no"`
  attribute; the docs page's own example still shows the older
  `rel="nofollow noopener noreferrer"` with no `translate` attribute. We
  followed the source since it's newer. `text_url` on `MediaAttachment` is
  set `null` because it only gets a value when the attachment has a legacy
  `shortcode`, which ordinary `media_ids` uploads never have.

- **status_direct.json** — a `direct`-visibility status from a remote
  account mentioning `fridgedoor`, written as two lines to show the
  `<br />` that `simple_format` inserts for single newlines inside one
  paragraph. Not our own status, so no `application` or `pinned` key.

- **context_thread.json** — `/api/v1/statuses/:id/context` for
  `status_own_post.json`: empty `ancestors`, and 5 `descendants` forming a
  tree (2 direct replies to the root; one of them has 2 nested replies; the
  other's reply is answered by the merchant). `in_reply_to_id` /
  `in_reply_to_account_id` chain correctly through the tree. Based on
  `entities/Context`.

- **status_favourited.json** — `status_own_post.json` with `favourited:
  true` and `favourites_count` incremented, matching the response shape
  `POST /api/v1/statuses/:id/favourite` returns (the docs page's own example
  for this endpoint is abbreviated; the real endpoint returns the full
  `REST::StatusSerializer` object, confirmed via `favourites_controller.rb`).

- **favourited_by.json** / **favourited_by_link_header.txt** — 3 full
  `Account` objects (not the abbreviated shape shown in the docs example —
  the endpoint uses the same `AccountSerializer` as everywhere else) plus a
  `Link` header using `max_id`/`since_id`, copied from the
  `methods/statuses` docs page's own example for this endpoint.

- **notifications.json** — `GET /api/v1/notifications`, 6 notifications
  newest-id-first: a mention that's a reply to the merchant's post, a plain
  mention, a mention inside a direct status, a favourite, a reblog, and a
  follow. `group_key` values follow `Notification::Groups#set_group_key!`:
  `favourite`/`reblog` group as `"{type}-{status_id}-{hour_bucket}"`,
  `follow` as `"follow-{hour_bucket}"`, and non-groupable types (mention)
  fall back to the serializer's own `"ungrouped-{id}"`. We did not include a
  `quote` notification: `docs/entities/Notification` and
  `app/models/notification.rb`'s `PROPERTIES`/`TYPES` on `main` do list
  `quote` and `quoted_update`, but quoting is a very recent addition and we
  judged a synthetic quote notification less useful than getting the
  well-established types exactly right, so we left it out rather than guess
  at its shape.

- **notification_single.json** — `GET /api/v1/notifications/:id` for the
  mention-that-is-a-reply notification, full `Notification` shape per
  `notification_serializer.rb`.

- **conversations.json** — `GET /api/v1/conversations`: one unread
  conversation (a direct status just received) and one read one, each with
  its `accounts` and `last_status`. Based on `entities/Conversation` and
  `conversation_serializer.rb`.

- **conversation_read.json** — `POST /api/v1/conversations/:id/read`: the
  same conversation as the unread one above, with `unread: false`.

- **markers.json** / **marker_saved.json** — `GET
  /api/v1/markers?timeline[]=notifications` and the `POST /api/v1/markers`
  response, keyed by timeline name per `entities/Marker` and
  `marker_serializer.rb` (`version` is `lock_version`, an optimistic-locking
  counter, not a schema version).

- **errors.json** — one example per status code, with the exact message
  strings Mastodon sends: `"Record not found"` (404, `error_handling.rb`),
  `"This action is not allowed"` (403, `Mastodon::NotPermittedError` via
  `error_handling.rb` — we picked a real trigger for it: favouriting a
  public post by an account you've blocked, which passes `StatusPolicy#show?`
  but fails `#favourite?`), `"This action is outside the authorized scopes"`
  (403, `doorkeeper_forbidden_render_options` in
  `api/base_controller.rb`), a 422 built from
  `StatusLengthValidator`'s real `statuses.over_character_limit` locale
  string (`"character limit of %{max} exceeded"`) wrapped in
  ActiveRecord's own `"Validation failed: Text ..."` format, and a 429 with
  `X-RateLimit-Limit`/`-Remaining`/`-Reset` headers per
  `api/rate_limit_headers.rb` — note `-Reset` is an ISO 8601 timestamp with
  microseconds (`Time#iso8601(6)`), not a Unix epoch integer, which is easy
  to get wrong.

## Other known uncertainties

- `Account#feature_approval` and `Status#quote_approval`/`quotes_count`/
  `quote`/`tagged_collections` are all present on `main` as of this
  research but are recent (interaction-policy and quote/collections
  features). We included them with plausible empty/default values since
  omitting real fields is exactly the kind of drift this fixture set is
  meant to avoid, but a server that hasn't upgraded yet may not send them.
- `Account.fields[].value` link markup mirrors `shortened_link`, adding a
  `me` keyword to `rel` for profile-field links specifically (`rel_me:
  true` in `account_field_value_format`), which is why it differs slightly
  from the plain link in a status's `content`.
