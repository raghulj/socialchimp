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
| X, Pinterest, Threads | not yet | | | | | | |

"On a timer" means the network has no way to tell us when something happens,
so socialchimp checks instead. Your code gets the same updates either way.

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
- Large video is refused with a clear message rather than half-uploaded.

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
- Files are sent in pieces, so a large video does not have to fit in memory.

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
  worse than being told we do not know.
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
  memory. Up to 4 GB.
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

---

## A network that is not here

You do not have to wait for us. Write it yourself and publish it — socialchimp
will find it. See [adding a platform](adding-a-platform.md).
