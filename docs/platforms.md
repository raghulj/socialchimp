# Networks

What each network can do, what it needs from you before it will work, and the
things about it that surprise people.

| Network | Ready | Register the app | Post text | Pictures | Video | Schedule | Push updates |
|---|---|---|---|---|---|---|---|
| Mastodon | yes | automatic | yes | yes | yes | yes | on a timer |
| Bluesky | yes | not needed | yes | yes | no | no | on a timer |
| Facebook Pages | yes | by hand, reviewed | yes | yes | small only | yes | yes |
| YouTube | yes | by hand, reviewed | **no** | no | yes | yes | on a timer |
| Instagram | yes | by hand, reviewed | **no** | yes | yes | no | yes |
| TikTok | yes | by hand, audited | **no** | no | yes | no | yes |
| Threads | yes | by hand, **its own app id** | yes | yes | yes | no | yes |
| X | yes | by hand, **paid plan** | yes | yes | yes | no | on a timer |
| Pinterest | yes | by hand, reviewed | **no** | yes | yes | no | **no** |

"On a timer" means the network has no way to tell us when something happens,
so socialchimp checks instead. Your code gets the same updates either way.

## Alt text

`Media.alt_text` is the description read out to somebody using a screen
reader, and it reaches **Bluesky, Facebook, Instagram, Mastodon, Threads and
X** — every network here that takes a picture through `Media`.

Two exceptions, both of them real properties of the network rather than
something missing:

- **Pinterest** hangs alt text off the whole pin rather than off one picture,
  so it is `options={"alt_text": "..."}` there and `Media.alt_text` is not
  read. A pin with five pictures has one description, which is Pinterest's
  own shape.
- **YouTube and TikTok** take video and nothing else, and neither has alt
  text for a video.

On X it costs one extra request per file, because X has nowhere to carry a
description on the upload itself.

## How much of a video has to fit in memory

**YouTube, TikTok and X send a video in pieces**, reading it off disk one
piece at a time, so a four gigabyte file costs one piece of memory rather
than four gigabytes.

**Facebook and Pinterest read the whole file first.** Neither sends a video
in pieces today — Facebook's chunked upload is not written yet, and Pinterest
hands out one upload form for one request — so a video really does cost its
own size in memory on your own server while it goes out. Facebook refuses
anything over a gigabyte for that reason, and `biggest_video_bytes` lowers
the line if a gigabyte is more than your server has.

---

## Mastodon

**The easiest one to try.** socialchimp registers the app for you, so there is
no portal and no waiting for approval.

```python
app = await sc.create_app(
    "mastodon",
    host="mastodon.social",
    name="My App",
    redirect_uri="https://example.com/cb",
)
```

**Do this once per server.** Every Mastodon server is separate, so an app
registered on mastodon.social means nothing on fosstodon.org.

- **Post options**: `visibility` (`public`, `unlisted`, `private`, `direct`),
  `spoiler_text`, `sensitive`, `language`
- **Tokens never expire.** Nothing to renew.
- **The post length is set by whoever runs the server** — 500 by default,
  5,000 on plenty of them. Read it with `await account.limits()` rather than
  assuming.

## Bluesky

No portal at all. People sign in with an **app password**, which is not their
real password and can be revoked on its own.

`start_login` answers with `AskForDetails` rather than a link. Show the fields
it gives you, and never log the one marked secret.

**Nothing has to be saved first.** Bluesky is the only network here that lists
`Feature.NEEDS_NO_APP`, so socialchimp asks your storage for no credentials
before a sign-in and hands the platform `app=None`. `sc.start_login("bluesky",
redirect_uri="unused")` works against empty storage. There is no `create_app`
either — asking for one says there is no app to register, rather than sending
you to a portal that does not exist.

- **Post options**: `langs`
- **Length is 300 letters and 3,000 bytes**, both enforced. A family emoji is
  one letter and eleven bytes, so the two limits catch different posts.
- **Links need marking up or they are dead text.** socialchimp does this for
  you, including getting the byte offsets right — the most common mistake
  people make writing this by hand.
- **Tokens last minutes and are replaced on every renewal.** If you run more
  than one process, give socialchimp a shared lock (see
  [getting started](getting-started.md#running-more-than-one-process)).
- No video yet, no scheduling.

## Facebook Pages

**You create the app by hand**, at
[developers.facebook.com](https://developers.facebook.com/apps). Meta reviews
it and requires business verification before the posting permissions work at
all. That review is the slowest part of getting started, so begin it early.

- **It always asks which page**, even when there is only one, so your app has
  one code path instead of two. Handle `ChooseAccount` from `finish_login` and
  finish with `sc.choose(...)`.
- **Post options**: `link`
- **Real scheduling**, between 10 minutes and 75 days out. You get back
  `PostState.SCHEDULED`.
- **A page token made from a long-lived user token does not expire.**
- **Webhooks work.** Verify with the platform's `check_signature` on the raw
  bytes of the request, before anything parses them.
- **A video comes back `PostState.PROCESSING`, not `DONE`.** Facebook takes
  the bytes and carries on encoding after it answers. Ask again later with
  `check_state`, which reads the video's `status` and says `DONE`,
  `PROCESSING` or `FAILED`. Words and pictures are live the moment `publish`
  returns, so there is nothing to ask about there.
- **A video is read into memory whole**, not sent in pieces. Anything over a
  gigabyte is refused with a clear message rather than half-uploaded, and
  `biggest_video_bytes` lowers that line.
- **Alt text works.** `Media.alt_text` goes up with the picture.

## YouTube

**You create the app by hand** in the
[Google Cloud console](https://console.cloud.google.com/apis/credentials), turn
on the YouTube Data API v3, and create an OAuth client. Uploading is a
sensitive permission, so Google reviews it before anyone outside your test
users can sign in.

- **There is no text-only post.** Every post is a video; a post without one is
  refused. Community posts are not in the API at all.
- **Post options**: `title` (**required**), `made_for_kids` (**required** by
  Google), `privacy_status`, `category_id`, `tags`, `notify_subscribers`.
  `Post.text` becomes the description.
- **A video with no `privacy_status` goes up private**, because making
  someone's video public by accident cannot be undone.
- **Shorts are not a separate thing to ask for.** YouTube decides, from the
  shape and the length of the video.
- **You get `PostState.PROCESSING`, not `DONE`.** YouTube keeps encoding after
  it accepts the upload. Ask again later with `check_state`.
- **The daily allowance is quota, not rate limiting.** An upload costs about
  1,600 of 10,000 units a day. Running out raises `RateLimitError`, but
  retrying shortly is the wrong move — it resets at midnight Pacific.
- Files are sent in pieces, so a large video does not have to fit in memory —
  unlike Facebook and Pinterest, which read the whole file first.
- **No alt text.** Every post here is a video, and YouTube has no alt text
  for one, so `Media.alt_text` is not sent anywhere.

## Instagram

**You create the app by hand** at
[developers.facebook.com](https://developers.facebook.com/apps), the same as
Facebook Pages, with the same review and business verification. Doing one
makes the other much quicker.

- **Only Business and Creator accounts can publish.** A personal account never
  can, through any API. socialchimp says so plainly instead of letting a
  confusing permission error come back.
- **Instagram fetches the picture itself, from a web address.** It does not
  accept an upload, so `Media.from_url(...)` works and a local file is refused
  with an explanation. Put the file somewhere public first.
- **There is no text-only post.** Every post needs a picture or a video.
- **Publishing is two calls with a wait in between**: build the post, wait for
  Instagram to finish with it, then publish. socialchimp does the waiting. If
  it runs out of patience you get a message saying the post *may still appear*
  — because it might, and being told it failed when it later succeeds is
  worse than being told we do not know. Because the waiting happens inside
  `publish`, a post here is live by the time you get it: you never see
  `PostState.PROCESSING`, and there is no `check_state` to need.
- **Post options**: `carousel` (only needed to force a single picture into a
  carousel; two or more already make one). 2 to 10 items.
- **The daily posting limit is read, not written down.** Meta's own
  documentation gives three different numbers, so socialchimp asks the network
  and puts the answer in `limits().posts_left_today`.
- **Captions**: 2,200 characters, up to 30 hashtags.
- **No scheduling, and no deleting** — neither exists in the API.
- **Webhooks work**: comments, mentions, live comments, story insights.

## TikTok

**You create the app by hand** at
[developers.tiktok.com](https://developers.tiktok.com/), add Login Kit and the
Content Posting API to it, and add your redirect address. TikTok calls the two
values the client key and the client secret; the client key goes in
`AppCredentials.client_id`.

**Then read the next paragraph, because it is the one people lose a day to.**

- **Until TikTok has audited your app, everything it posts is private.** An
  unaudited app may post for at most **5 people in any 24 hours**, and every
  single thing it posts is forced to `SELF_ONLY` — visible to the account
  owner and to nobody else. Not their followers, not the For You page, not a
  friend they send the link to. You may ask for `PUBLIC_TO_EVERYONE`, TikTok
  will answer that it worked, the video will be there when the person looks at
  their own profile, and the rest of the world will never see it. There is no
  bug to find. Submit the app for TikTok's compliance audit — usually a week or
  two — before you promise anybody a public video, and until then tell your
  users their posts are private.
- **There are two places a post can go, and you choose.**
  `options={"send_to": "drafts"}` puts the video in the person's TikTok inbox
  and they finish it and publish it themselves in the app.
  `options={"send_to": "profile"}` posts straight to their profile.
  **The drafts are the default**, because they need only the `video.upload`
  permission — `video.publish` is a separate and harder ask — and because
  nothing reaches anybody's profile without a person tapping a button. Say
  `"profile"` when you mean it.
- **The drafts carry no caption.** TikTok's inbox takes the file and nothing
  else; the person writes the words themselves. So a drafts post with
  `Post.text` on it is **refused** rather than having your caption quietly
  disappear. Clear the text, or send it to the profile.
- **There is no text-only post.** Every post is a video; a post without one is
  refused.
- **Post options** (profile posts only): `send_to`, `privacy_level`,
  `disable_comment`, `disable_duet`, `disable_stitch`,
  `video_cover_timestamp_ms`, `brand_content_toggle`, `brand_organic_toggle`.
  `Post.text` becomes the caption — TikTok's API calls it the title, but it is
  the words under the video, and there is no second field.
- **A video with no `privacy_level` goes up as `SELF_ONLY`**, because putting
  someone's video in front of the world by accident cannot be undone.
- **The caption limit of 2,200 is not 2,200 characters.** TikTok counts the way
  Java does, where an emoji is two, so 1,101 thumbs-up is over the line.
  socialchimp counts the same way TikTok does.
- **You get `PostState.PROCESSING` for a profile post**, because TikTok keeps
  encoding and moderating after it takes the bytes — ask again later with
  `check_state`. A drafts post comes back `PostState.WAITING_FOR_PERSON`: the
  network has finished, and nothing else happens until somebody opens the app,
  so there is nothing to wait for.
- **Webhooks work**, for `post.publish.complete`,
  `post.publish.publicly_available`, `post.publish.inbox_delivered` (which
  arrives as `UpdateKind.POST_DRAFTED`) and `post.publish.failed`. Verify with
  the platform's `check_signature` on the raw bytes of the request, before
  anything parses them. **TikTok retries for 72 hours and delivers at least
  once, so the same message arriving twice is normal** — give `Dispatcher` a
  `SeenUpdates` and the second copy is dropped for you.
- **Using a refresh token destroys it.** TikTok hands back a new one every
  time, so save both halves of what `refresh` returns. An access token lasts a
  day and a refresh token a year.
- **Files are sent in pieces**, so a large video does not have to fit in
  memory. Up to 4 GB. Facebook and Pinterest read a whole video first
  instead.
- **No alt text.** Every post here is a video, and TikTok has no alt text for
  one, so `Media.alt_text` is not sent anywhere.
- **The daily posting cap belongs to the creator, not to your app** — about 15
  posts in 24 hours, shared across every app they use. It comes back as a
  `RateLimitError`, but waiting a few seconds is the wrong move; only tomorrow
  helps.
- **No scheduling** — TikTok's API has no way to ask for it.
- **No deleting** — there is no call for it.
- **No photo carousels yet.** TikTok can post up to 35 pictures, but through a
  different call that fetches each one from a public web address on a domain
  you have proved is yours. A post with pictures on it is refused with a
  message saying so.

## Threads

**You create the app by hand** at
[developers.facebook.com](https://developers.facebook.com/apps), the same as
Facebook Pages — and then the thing that catches everybody:

- **Adding the Threads use case makes a second app id and app secret.** They
  sit next to the pair the same app already uses for Facebook and Instagram,
  and they are not interchangeable. Reusing the Facebook pair fails in the
  worst way: the sign-in page accepts it, the person approves, and the token
  swap at the end refuses under whatever code Meta feels like, with a message
  that mentions none of this. Save the Threads pair with `Storage.save_app`
  under the platform name `threads`, and socialchimp keeps the two apart for
  you.

Almost nothing else about it lives where the rest of Meta lives:

- **Signing in is not Facebook Login.** People approve at `threads.net`, and
  the code is swapped at `graph.threads.net`. The API is there too, not on
  `graph.facebook.com`.
- **It never asks which account.** Facebook asks which page and Instagram
  which business account; a Threads sign-in is one profile, so `finish_login`
  finishes rather than handing you a `ChooseAccount`.
- **Renewal actually works here, unlike the rest of Meta.** Facebook and
  Instagram hand out no refresh token at all — a token is extended by trading
  it in while it still works, or the person signs in again. Threads has a real
  refresh endpoint: one request, no app secret, and the sixty-day clock starts
  again. A job that runs once a month keeps a connection alive indefinitely.
  The one rule is that a **token has to be 24 hours old** before it will renew
  one; asking sooner raises `RateLimitError` with the wait on `retry_after`,
  and nothing is wrong with the token.
- **Threads fetches the picture itself, from a web address**, exactly as
  Instagram does. `Media.from_url(...)` works and a local file is refused with
  an explanation.
- **It does take a post of words alone**, which Instagram does not.
- **Publishing is two calls with a wait in between**: build the post, wait for
  Threads to finish with it, then publish. socialchimp does the waiting, and
  only where there is video — words and pictures are ready straight away. If
  it runs out of patience you get a message saying the post *may still appear*.
  As on Instagram, that means a post here is live by the time you get it: no
  `PostState.PROCESSING`, and no `check_state` to need.
- **The length limit is 500 bytes, not 500 characters.** Threads' own
  documentation says characters and means bytes, so an emoji costs four and
  500 emoji are 2,000. socialchimp counts the way Threads does.
- **Post options**: `carousel` (only needed to force a single picture into a
  carousel; two or more already make one). 2 to 20 items — twice what
  Instagram takes.
- **The daily limits are read, not written down**: 250 posts and 1,000
  replies in a rolling 24 hours, and **replies do not come out of the posts**.
  Posts left lands in `limits().posts_left_today`; `allowance()` gives you
  both numbers.
- **Deleting works**, which it does not on Instagram. 100 a day per account.
- **No scheduling** — there is no call for it.
- **Webhooks are narrower than the rest of Meta's**: only `replies`,
  `mentions`, `publish` and `delete`, and **nothing at all where a private
  account is involved**. Verify with `check_signature` on the raw bytes,
  before anything parses them.

---

## X

**Posting costs money, and there is no fixed price to write down.** Access to
the API that lets an app publish is paid and tiered, what a plan includes
changes without notice, and any number put here would be out of date within
months. **When your plan does not cover something, X does not say "you have
not paid" — it answers 403 with `client-not-enrolled`, which reads exactly
like a scope your app forgot to ask for**, and people spend afternoons
rewriting scopes over it. socialchimp names this one on sight: the message
says plainly that the plan refused this, not your code, and points at
[console.x.com](https://console.x.com), where you look up and change what
your app is allowed to do.

**You create the app and its OAuth client by hand**, at
[developer.x.com](https://developer.x.com/en/portal/dashboard). There is no
`create_app` here — somebody has to fill in the form, agree to the terms,
choose a plan, and add your redirect address to the OAuth client themselves.

- **Ask for `offline.access` or tokens die in two hours.** Leave it out of
  your scopes and X sends back no refresh token at all — the access token
  stops working two hours later and the person has to sign in again. It works
  perfectly the morning you write it and starts logging people out after
  lunch.
- **Post options**: `reply_settings` (`everyone`, `mentionedUsers`,
  `following`, `subscribers`), `quote_tweet_id`.
- **The limit is 280 characters**, counted the way JavaScript counts them —
  an emoji is two. X's paid subscribers can post longer ones, and nothing in
  the API says whether the account we are posting as is one of them, so 280
  is what socialchimp checks for everybody; a subscriber's own longer post
  still gets through by asking for the limit they know they have.
- **A thread is posts chained together, not one thing X has.**
  `publish_thread` sends each one pointed at the id before it. If one fails
  partway through, nothing already published is deleted and nothing after it
  is sent — you get a `PartialThreadError` naming how far it got, so you can
  carry the thread on from the last id once the problem is fixed.
- **Files go up the old way**: INIT, then APPEND once per piece, then
  FINALIZE, and for video a STATUS call in a loop until X finishes encoding
  it. Sent a piece at a time off disk, so a large video does not have to fit
  in memory.
- **Alt text is a request of its own.** X has nowhere to carry a description
  on the upload, so a `Media.alt_text` goes up afterwards through
  `POST /2/media/metadata`, before the file is named on a post — X will not
  take one for a file that is already published. A file with no alt text
  costs no extra request.
- **Deleting works.**
- **No scheduling.** `Feature.SCHEDULE` is missing, so a post with
  `publish_at` is refused rather than published now.
- **No app to create.** See above.
- **No pushed updates.** X's streaming and account-activity products are
  both behind paid plans of their own, so mentions are read on a timer
  through `fetch_updates` instead, which works on every plan that can read at
  all.

## Pinterest

**Every pin needs a board.** Pinterest has no feed to post to — there is no
such thing as a pin without one — and socialchimp never chooses one for you:
name `board_id` on the post, or save one on the connection's `extra` if your
app has a sensible default. A pin naming neither is refused before anything
is sent, with a message naming both routes. `boards(connection)` lists what
an account has, so you can build a picker.

**A new app gets Trial access, and its pins are visible only to you.** Your
code runs against the real API, with real credentials, and gets back real
2xx replies carrying real pin ids — and nobody but the person who made them
can see the pins. Not the public profile, not anybody's home feed, not a
friend looking at the account. Nothing anywhere says this is happening, so
the first thing to check when a pin "did not appear" is whether the app is
still on Trial. Getting to Standard access is a review: a privacy policy and
a video recording of your app taking a real person through the real sign-in.
Check
[the access tiers page](https://developers.pinterest.com/docs/key-concepts/access-tiers/)
— there is no field anywhere in the API that says which tier you are on.

**You create the app by hand** at
[developers.pinterest.com/apps](https://developers.pinterest.com/apps).

- **There is no PKCE.** Pinterest's v5 API refuses a `code_challenge` rather
  than accepting it, so `SendToNetwork.remember` comes back empty here — that
  is a real property of Pinterest, not something missing.
- **Creating a pin needs all four scopes**: `boards:read`, `boards:write`,
  `pins:read`, `pins:write`. Asking for only the `pins:` pair gets a 403 on
  the first pin, which reads like a problem with the board rather than a
  permission never asked for.
- **Post options**: `board_id`, `board_section_id`, `title`, `link`,
  `alt_text`, `dominant_color`.
- **Alt text belongs to the pin, not to one picture.** It is
  `options={"alt_text": ...}` here, and `Media.alt_text` is not read — a pin
  with five pictures has one description, which is Pinterest's own shape
  rather than something missing.
- **`Post.text` is the pin's description, and `title` is a separate
  setting** — the thing people trip over. The description takes 800
  characters, the title 100.
- **Tokens last 30 days; the refresh token lasts 60 and is replaced every
  time you use one.** An account nobody has posted from in two months needs
  signing in again, because its refresh token ran out before anything
  renewed it. That day is on `Token.refresh_token_expires_at`, so you can put
  a "reconnect Pinterest" prompt in front of somebody before a post fails
  rather than after.
- **Pinterest really will fetch a picture from a web address.**
  `Media.from_url(...)` costs nothing here. Two to five pictures become one
  pin people can swipe through — all files or all links, never a mixture.
- **Video is three steps and a different server**: registered with
  Pinterest, uploaded to Amazon with a form Pinterest hands you (no account
  token goes with it — the upload is not going to Pinterest), then waited
  for. Pinterest gives out one form for one upload, so the whole file goes in
  a single request and really does cost its own size in memory. Facebook is
  the same; YouTube, TikTok and X send theirs in pieces.
- **Deleting works.**
- **No text-only pin.** Every pin needs a picture or a video;
  `Feature.POST_TEXT` is off and a post with nothing attached is refused.
- **No comments at all, so no replies.** There is no comment endpoint
  anywhere in v5; a post with `reply_to` is refused by name.
- **No scheduling.** `Feature.SCHEDULE` is missing.
- **No updates worth having.** No webhooks for ordinary pins, and nothing in
  the API reports that something *happened* — so there is no `fetch_updates`
  here and `Feature.PUSH_UPDATES` is off.

---

## A network that is not here

You do not have to wait for us. Write it yourself and publish it — socialchimp
will find it. See [adding a platform](adding-a-platform.md).
