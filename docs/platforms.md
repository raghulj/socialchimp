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
| TikTok, X, Pinterest, Threads | not yet | | | | | | |

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

---

## A network that is not here

You do not have to wait for us. Write it yourself and publish it — socialchimp
will find it. See [adding a platform](adding-a-platform.md).
