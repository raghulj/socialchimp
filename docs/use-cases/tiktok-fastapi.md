# Uploading video to TikTok from a FastAPI backend

A creator tool. People connect their TikTok account, drop a video into a web
page, and the backend uploads it - either straight to their profile, or into
their TikTok drafts so they can write the caption themselves in the app.

New to socialchimp? Read the [tutorial](../tutorial.md) first.

- [Read this before you write any code](#read-this-before-you-write-any-code)
- [What we are building](#what-we-are-building)
- [What you need before you start](#what-you-need-before-you-start)
- [The code](#the-code)
- [Running it](#running-it)
- [What will go wrong, and why](#what-will-go-wrong-and-why)

---

## Read this before you write any code

**Until TikTok has audited your app, everything it posts is private.**

An app you created this morning is unaudited. An unaudited app may post for
at most **five people in any 24 hours**, and every single thing it posts is
forced to `SELF_ONLY` - visible to the account owner and to nobody else. Not
their followers, not the For You page, not a friend they send the link to.

TikTok does this quietly. You can ask for `PUBLIC_TO_EVERYONE`, TikTok will
answer that the post succeeded, the video will be there when the creator
looks at their own profile, and the rest of the world will never see it.
There is no error, no warning and no field in the reply that says so.

This is the day people lose. They read their own code, check the privacy
level three times, add logging, and eventually find a forum thread.

What to do:

- **Submit the app for TikTok's compliance audit early.** A week or two is
  normal.
- **Until it passes, tell your users their posts are private**, in the
  interface, where they will see it. They will look at their own profile,
  see the video, and assume it worked.
- **Do not promise anybody a public video** before the audit is through.

socialchimp repeats this in the message you get when there are no app
credentials stored, because that is the first thing anybody hits.

---

## What we are building

A FastAPI service with:

1. **Sign-in**, using the ready-made router. TikTok finishes in two steps -
   there is no "which account?" pause, because one TikTok token is for one
   account.
2. **An upload endpoint** that takes a file path and sends the video to
   TikTok a piece at a time, without loading it into memory.
3. **A state endpoint** that asks TikTok how a post is getting on - and knows
   when to stop asking.
4. **A webhook** that TikTok pushes to when the post finishes, later.

Runnable code: [`examples/tiktok_fastapi/tiktok_app.py`](../../examples/tiktok_fastapi/tiktok_app.py).
It needs real credentials, so it will not run in CI. For a credential-free
run of the same shapes - sign-in, storage, a post, a webhook - see
[`examples/facebook_django/page_post_demo.py`](../../examples/facebook_django/page_post_demo.py).

---

## What you need before you start

1. **An app** at <https://developers.tiktok.com/>, with **Login Kit** and the
   **Content Posting API** both added to it.
2. **Your redirect address** in that app's settings. TikTok will not accept
   `http://localhost`, so use a tunnel while you build and put the tunnel's
   address in both places.
3. **The audit**, as above.
4. **The right permissions.** `video.upload` is enough to fill somebody's
   drafts. `video.publish` - posting straight to a profile - is a separate
   and harder ask.

TikTok calls its two values the **client key** and the **client secret**.
`AppCredentials.client_id` is where the client key goes; there is no third
value:

```python
AppCredentials(
    platform="tiktok",
    host=None,
    client_id=os.environ["TIKTOK_CLIENT_KEY"],
    client_secret=os.environ["TIKTOK_CLIENT_SECRET"],
)
```

There is no `create_app` here. Ask for one and socialchimp refuses with a
message naming the portal.

---

## The code

### The client, and a typed handle on the platform

```python
from socialchimp import SocialChimp
from socialchimp.platforms.tiktok import TikTokPlatform

tiktok = TikTokPlatform()
sc = SocialChimp(storage=storage, platforms={"tiktok": tiktok})
```

Building the platform yourself and passing it in does two things. It keeps
the real type - `sc.platform_for("tiktok")` hands it back as the `Platform`
protocol, which has no `check_state`, because most networks have nothing to
check. And it is where a platform that needs settings of its own goes:

```python
tiktok = TikTokPlatform(
    chunk_bytes=10 * 1024**2,  # 5 MB to 64 MB; default is 10 MB
    timeout=300.0,  # a piece of video takes far longer than a request
    pkce=False,  # TikTok wants PKCE from mobile apps, not servers
)
```

Keep one `SocialChimp` for the life of the process. The locks that stop two
workers renewing the same token at once live on it.

### Sign-in

FastAPI is async and so is socialchimp, so there is no bridge and nothing
runs on a thread. The ready-made router is four routes:

```python
from socialchimp.contrib.fastapi import router

app.include_router(
    router(
        sc,
        redirect_uri=f"{PUBLIC_URL}/social/callback/{{platform}}",
        scopes={"tiktok": ["user.info.basic", "video.upload", "video.publish"]},
        # TikTok signs a pushed message with your client secret. There is no
        # separate webhook secret.
        secrets={"tiktok": CLIENT_SECRET},
        deliver=dispatcher.deliver,
    ),
    prefix="/social",
)
```

Send somebody to `/social/connect/tiktok?state=user-42`; they come back to
`/social/callback/tiktok`, which answers
`{"step": "connected", "connection_id": "tiktok:<open id>", ...}`.

Where a half-finished sign-in waits matters here. Left alone the router keeps
it in the process, which is fine while you are getting it working and wrong
in production: two workers do not share it, so somebody sent away by one and
returning to another is told their sign-in expired. Write three methods over
Redis or your session and pass them as `memory` - see
[frameworks](../frameworks.md#where-a-half-finished-sign-in-waits).

### Tokens, and the one that destroys itself

You do not write any of this, but you should know it is happening.

A TikTok access token lasts 24 hours. A refresh token lasts 365 days. And
**using a refresh token replaces it**: the reply carries a new one and the
old one stops working the moment it is used.

So two consequences:

- Your `save_connection` is called on every renewal and has to replace the
  row. A new refresh token that never reaches your database disconnects the
  account for good.
- If you run more than one process, give socialchimp a lock they share. Two
  workers renewing the same connection at once means one of them ends up
  holding a token TikTok has already thrown away. See
  [getting started](../getting-started.md#running-more-than-one-process).

### Uploading

```python
@app.post("/uploads")
async def upload(body: Upload) -> dict[str, str | None]:
    options: dict[str, object] = {"send_to": body.send_to}
    caption = body.caption
    if body.send_to == "profile":
        options["privacy_level"] = body.privacy_level
    else:
        caption = ""

    post = Post(
        text=caption,
        media=(Media.from_file(body.path),),
        options=options,
    )
    result = await sc.account(body.connection_id).post(post)
    return {"publish_id": result.id, "state": result.state.name}
```

#### Drafts or profile - and why drafts is the default

`Post.options["send_to"]` takes two values.

**`"drafts"`** (the default) puts the video in the person's TikTok inbox.
They open the app, write their own caption, and publish it themselves. It
needs only `video.upload`, and nothing reaches anybody's profile without a
person tapping a button.

**`"profile"`** posts straight to their profile. It needs `video.publish` and
it carries the caption and every setting below.

The default is the drafts for two reasons: it works for every app, because
`video.publish` is the harder permission to be granted; and it cannot
surprise anybody.

**TikTok's inbox takes no caption.** The person writes the words themselves,
so a drafts post with any `Post.text` on it is refused rather than having
your caption quietly vanish:

```
InvalidPostError: This post has a caption, but it is headed for the person's
TikTok drafts, and TikTok's drafts take the video and nothing else ...
```

The same goes for the other settings - `privacy_level`, `disable_comment` and
the rest mean nothing on a drafts post, so sending them is refused rather
than ignored.

#### The settings a profile post takes

```python
options = {
    "send_to": "profile",
    # SELF_ONLY, MUTUAL_FOLLOW_FRIENDS, FOLLOWER_OF_CREATOR or
    # PUBLIC_TO_EVERYONE.
    "privacy_level": "SELF_ONLY",
    "disable_comment": False,
    "disable_duet": False,
    "disable_stitch": False,
    "video_cover_timestamp_ms": 1000,
    "brand_content_toggle": False,  # a paid partnership
    "brand_organic_toggle": False,  # promoting your own business
}
```

Anything else is refused before a request is spent on it, with the accepted
names in the message.

**A video with no `privacy_level` goes up as `SELF_ONLY`.** Putting
somebody's video in front of the world by accident cannot be undone, so the
quiet default is the careful one - and it is the only one an unaudited app
gets anyway.

Which levels a particular person may use depends on whether their account is
public or private. TikTok refuses one they cannot use, and that arrives as an
`InvalidPostError`.

#### Captions are counted the way Java counts

The limit is 2,200 and it is not 2,200 characters. TikTok counts UTF-16
units, where an emoji is two - so 1,101 thumbs-up is 1,101 characters to
Python and 2,202 to TikTok. socialchimp counts the same way TikTok does, so a
caption is refused here for the reason TikTok would have refused it:

```python
limits = await sc.account(connection_id).limits()
limits.max_text_length  # 2200
limits.text_counted_in  # TextCount.UTF16_UNITS
```

#### The file is never in memory

```python
Media.from_file("/var/uploads/clip.mp4")
```

That reads nothing. It notes the path, works out that `.mp4` is a video, and
stops. socialchimp asks for the file a piece at a time while the upload runs,
so a four gigabyte video costs one piece of memory rather than four
gigabytes. Four gigabytes is TikTok's ceiling; anything bigger is refused
before a byte is sent.

Two details socialchimp handles that catch people out on their own:

- **A piece must be between 5 MB and 64 MB**, and the number of pieces is the
  file size divided by the piece size **rounded down**. A 12 MB video sent in
  10 MB pieces is *one* piece of 12 MB, not two - the leftover rides along on
  the last piece rather than becoming a 2 MB piece of its own, which would be
  under the floor and refused halfway through the upload.
- **Anything under 5 MB goes whole**, in one piece the size of the file.

`Media.from_url` does not work here. TikTok will not fetch a video from an
address for you on this route, and socialchimp says so rather than
downloading it behind your back.

Only three kinds of file: `video/mp4`, `video/quicktime`, `video/webm` - so
an `.mp4`, a `.mov` or a `.webm`. The kind is worked out from the filename,
so a path with no extension is refused by `Media.from_file` before TikTok is
involved at all; pass `kind=MediaKind.VIDEO` if you genuinely have a file
with no useful name.

### `publish()` never says DONE

Taking the bytes is not publishing. TikTok encodes and moderates afterwards,
so `publish()` hands back one of two states and never `PostState.DONE`:

| Where it was going | State you get | What it means |
|---|---|---|
| `"profile"` | `PROCESSING` | TikTok is still working. It will be live, or failed, before long. |
| `"drafts"` | `WAITING_FOR_PERSON` | TikTok has finished. The video is in their drafts. |

**`WAITING_FOR_PERSON` is not "still processing", and this is the second
thing people get wrong here.** As far as the network is concerned the waiting
is over - it has done everything it is ever going to do. The video sits in
somebody's TikTok inbox until they open the app, which may be tomorrow and
may be never. An app that polls this one polls forever, spends its whole
status-check allowance doing it, and never sees a change.

So branch on it:

```python
if result.state is PostState.WAITING_FOR_PERSON:
    # Tell the creator. Do not put this on a timer.
    notify(user, "Your video is in your TikTok drafts - open the app to post it.")
elif result.state is PostState.PROCESSING:
    schedule_a_check(result.id)
```

### Asking how it went

```python
@app.get("/uploads/{connection_id}/{publish_id}")
async def how_is_it_going(connection_id: str, publish_id: str) -> dict[str, str | None]:
    connection = await sc.fresh_connection(connection_id)
    result = await tiktok.check_state(connection, publish_id)
    return {"state": result.state.name, "url": result.url}
```

`check_state` is TikTok's own method rather than part of the `Platform`
protocol, so it takes a `Connection` rather than an `Account` handle.
`sc.fresh_connection(id)` is the public call that reads the connection and
renews its token first - the same one `account.post` uses.

One account may ask **30 times a minute**. `DONE` fills in `result.url`, once
TikTok has a public address for the video; before that there is none, because
there may never be one.

### The webhook

TikTok pushes four events:

| TikTok's word | Arrives as |
|---|---|
| `post.publish.complete` | `UpdateKind.POST_PUBLISHED` |
| `post.publish.publicly_available` | `UpdateKind.POST_PUBLISHED` |
| `post.publish.inbox_delivered` | `UpdateKind.POST_DRAFTED` |
| `post.publish.failed` | `UpdateKind.POST_FAILED` |
| `authorization.removed` | `UpdateKind.CONNECTION_REVOKED` |

```python
dispatcher = Dispatcher(seen=InMemorySeenUpdates())


async def video_is_live(update: Update) -> None:
    print(f"live: {update.connection_id} {update.raw}")


async def app_was_removed(update: Update) -> None:
    # The token has already stopped working. TikTok will not tell you twice.
    await storage.delete_connection(update.connection_id)


dispatcher.on(UpdateKind.POST_PUBLISHED, video_is_live)
dispatcher.on(UpdateKind.POST_DRAFTED, video_is_in_the_drafts)
dispatcher.on(UpdateKind.POST_FAILED, video_failed)
dispatcher.on(UpdateKind.CONNECTION_REVOKED, app_was_removed)
```

The router's `/social/webhooks/tiktok` checks the signature and hands the
update to `dispatcher.deliver`. Point TikTok's dashboard at that address.

`update.connection_id` is `tiktok:<open id>`, which is exactly the id
socialchimp gave the connection when the person signed in. No lookup table.

**The same message can arrive more than once.** TikTok promises to deliver at
least once and retries for 72 hours, so duplicates are normal rather than a
fault. socialchimp builds a stable id out of what TikTok said, so giving
`Dispatcher` a `SeenUpdates` drops the second copy for you.
`InMemorySeenUpdates` is per process - use something your workers share in
production.

---

## Running it

```bash
pip install "socialchimp[tiktok,fastapi]"
```

```bash
export TIKTOK_CLIENT_KEY=...
export TIKTOK_CLIENT_SECRET=...
export PUBLIC_URL=https://your-tunnel.example

uv run --with uvicorn uvicorn tiktok_app:app --reload \
    --app-dir examples/tiktok_fastapi
```

Then:

1. Open `$PUBLIC_URL/social/connect/tiktok` and approve.
2. `POST /uploads` with `{"connection_id": "tiktok:...", "path":
   "/tmp/clip.mp4"}`. It goes to the drafts by default.
3. `GET /uploads/tiktok:.../<publish_id>` to see where it got to.
4. Point TikTok's webhook at `$PUBLIC_URL/social/webhooks/tiktok`.

---

## What will go wrong, and why

### The post says it worked and nobody can see it

The app has not been audited. Read [the top of this
page](#read-this-before-you-write-any-code). This is not a bug in your code
and no amount of reading it will help.

### `InvalidPostError` about a caption on a drafts post

TikTok's inbox takes the video and nothing else. Clear `Post.text`, or send
it to the profile with `options={"send_to": "profile"}` - which needs
`video.publish`.

### The upload stops halfway through with a piece-size complaint

If you set `chunk_bytes` yourself, TikTok takes 5 MB to 64 MB and refuses
anything else *during* the upload rather than at the start. socialchimp
checks the value when you build the platform, so you find out at import time
instead:

```python
TikTokPlatform(chunk_bytes=1024)
# ConfigError: chunk_bytes is 1,024, but TikTok takes pieces of between
# 5 MB (5,242,880 bytes) and 64 MB (67,108,864 bytes) ...
```

### Polling a post forever and nothing changes

Look at the state. `WAITING_FOR_PERSON` means TikTok has finished and a human
has not. Stop checking and tell the creator.

### `RateLimitError` and waiting does not help

Three different caps, and only one of them is worth waiting out:

- **Six posting calls a minute, per person.** Waiting a minute helps.
- **Thirty status checks a minute, per person.** Same.
- **About 15 posts per creator per day**, shared across *every* app they use,
  not only yours. Only tomorrow helps.

An unaudited app has a fourth: five people in any 24 hours.

### The signature never matches

Something parsed the body before the check. A signature is over the exact
bytes TikTok sent, and TikTok signs `<time>.<body>` together. Read the raw
body, check it, parse afterwards. The ready-made router does this correctly;
if you write your own route, do not reach for `await request.json()`.

The other cause is the secret: TikTok signs with your **client secret**, not
a separate webhook secret.

### A signed message from months ago is refused

On purpose. A signature stays correct forever, so socialchimp refuses a
message more than five minutes old - otherwise anybody who got hold of one
request from a log or a proxy could send it again next year. Change the
window with `TikTokPlatform(allowed_age_seconds=...)` if you have a reason.

### A post with photos is refused

TikTok does have photo carousels - up to 35 pictures - but they go through a
different call that makes TikTok *fetch* each picture from a public address
on a domain you have proved is yours. That is a different way of moving a
file, not another setting, and socialchimp does not do it yet. The refusal
says all of that.

### Asking TikTok to schedule

It cannot. `Feature.SCHEDULE` is off and `publish_at` is refused rather than
posted immediately, which is what "it will probably be fine" would have
meant.

### Deleting a post

There is no call for it in TikTok's API. `delete_post` refuses.

---

## Elsewhere

- [Networks](../platforms.md#tiktok) - the short list.
- [Frameworks](../frameworks.md#fastapi) - the ready-made routes.
- [Facebook from Django](facebook-django.md) and
  [YouTube Shorts from Flask](youtube-shorts-flask.md) - the other two use
  cases.
