# Publishing YouTube Shorts from a Flask app

A team makes short vertical clips - product demos, thirty seconds each - and
wants to push them to the company's YouTube channel from the small Flask
service that already holds the video files.

New to socialchimp? Read the [tutorial](../tutorial.md) first.

- [What we are building](#what-we-are-building)
- [What you need before you start](#what-you-need-before-you-start)
- [Three things about YouTube that are not like the others](#three-things-about-youtube-that-are-not-like-the-others)
- [The code](#the-code)
- [Running it](#running-it)
- [What will go wrong, and why](#what-will-go-wrong-and-why)

---

## What we are building

A Flask service with:

1. **Sign-in**, using the ready-made blueprint. YouTube pauses to ask which
   channel, so the callback answers with a list rather than finishing.
2. **An upload endpoint** that sends a vertical clip and lets YouTube decide
   it is a Short.
3. **A state endpoint**, because `publish()` comes back while YouTube is
   still encoding.
4. **Error handling that knows quota from rate limiting**, which is the
   difference between an app that recovers and one that spends the rest of
   the day's allowance discovering it has none.

Runnable code:
[`examples/youtube_shorts_flask.py`](../../examples/youtube_shorts_flask.py).
It needs a real Google project, so it will not run in CI.

---

## What you need before you start

1. **A project** at <https://console.cloud.google.com>.
2. **The YouTube Data API v3 turned on** for it. This is a separate step from
   creating the project and is easy to skip; every call fails with a
   permission error until you do it.
3. **An OAuth client** (type: Web application) with your callback in its
   authorised redirect URIs -
   `http://localhost:5000/social/callback/youtube` while you build. Character
   for character.
4. **Google's review.** Uploading video is a sensitive permission, so until
   the review passes, only accounts you add as **test users** on the consent
   screen can sign in. Everyone else is turned away by Google, before your
   code sees anything.
5. **A Google account that actually has a YouTube channel.** A Google account
   and a channel are different things, and a Google account with none signs
   in fine and then has nothing to connect. socialchimp says so plainly:

   ```
   AuthError: This Google account has no YouTube channel, so there is nothing
   to connect. ... the person can make one at
   https://www.youtube.com/create_channel and then sign in again.
   ```

There is no `create_app` here. Ask for one and socialchimp refuses with a
message naming the console.

---

## Three things about YouTube that are not like the others

### There is no text-only post

Every post on YouTube is a video. `Feature.POST_TEXT` is off, and a post with
no video is refused:

```python
await account.post(Post(text="Just some words"))
# NotSupportedError: youtube does not support posting words on their own.
# Every post here carries a video, so attach one.
# Media.from_file('clip.mp4') will do it. Community posts are words, but
# they are not in YouTube's API at all.
```

That last sentence is there because it is the first thing people argue back.
YouTube's community posts *are* text - and they are in no part of the API, at
no access level, in neither direction. There is nothing socialchimp could
call.

The refusal costs nothing: it happens before the first request, which also
means before any quota is spent.

### `title` and `made_for_kids` are required, and `Post.text` is the description

This is the part that catches everybody. On YouTube:

- **`Post.text` is the description.** Up to 5,000 bytes, counted in bytes -
  so an emoji costs four of them and an accented letter two.
- **The title is `Post.options["title"]`**, and it is required. At most 100
  characters, no `<` or `>`.
- **`made_for_kids` is required too**, and socialchimp will not guess.

```python
Post(
    text="How the bench folds flat. #Shorts",  # the description
    media=(Media.from_file("bench.mp4"),),
    options={
        "title": "Folding bench, 30 seconds",  # required
        "made_for_kids": False,  # required
        "privacy_status": "public",  # private if you leave it out
        "category_id": "22",
        "tags": ["furniture", "workshop"],
        "notify_subscribers": True,
    },
)
```

Leave the title out and you get this, before anything is sent:

```
InvalidPostError: This post has no title, and YouTube refuses a video
without one. Post.text is the video's description here, so the title is its
own setting: Post(..., options={'title': 'My video'}).
```

Leave `made_for_kids` out and you get a refusal too, deliberately:

```
InvalidPostError: This post does not say whether the video is made for
children, and Google requires an answer for every upload. Set
options={'made_for_kids': False} - or True - and mean it: it changes what
YouTube allows on the video, and getting it wrong has consequences for the
channel. socialchimp will not guess.
```

A default here would be a library making a compliance decision on your
behalf. It changes what YouTube allows on the video - comments, notifications,
personalised ads - and getting it wrong is a channel-level problem, not a
post-level one.

The title's limit is on `Limits`, so you can show somebody the cap before
they type past it:

```python
limits = await account.limits()
limits.max_title_length  # 100
limits.max_text_length  # 5000, the description
limits.text_counted_in  # TextCount.UTF8_BYTES
```

### You do not ask for a Short

There is no Shorts endpoint, no Shorts flag, and nothing in socialchimp that
can make a video into a Short. **YouTube decides**, from the file:

- **Vertical** - taller than it is wide (square counts; 9:16 is what people
  use).
- **Under about three minutes.**

Both are properties of the file you uploaded, fixed long before socialchimp
saw it. So there is deliberately no `shorts=True` option here, because it
would be a lie - it could not do anything.

Putting `#Shorts` in the title or description is a convention people follow
to help YouTube along. It is not what makes the decision, and a landscape
five-minute video with `#Shorts` in the title is a normal video.

If it matters to you, check the file before you upload it - read its width,
height and duration with whatever you already use - and tell the person their
clip will be a normal video rather than letting them find out.

`result.url` is the ordinary `watch?v=` address either way. YouTube sends
people on to its own Shorts player when the video qualifies.

---

## The code

### The client

```python
from socialchimp import InMemoryStorage, SocialChimp

storage = InMemoryStorage()  # your five methods go here in production
sc = SocialChimp(storage=storage)
```

`InMemoryStorage` forgets everything on restart. There is a real storage
class, five methods over sqlite,
in [`examples/facebook_django/page_post_demo.py`](../../examples/facebook_django/page_post_demo.py);
swap it in and nothing else changes.

### Flask, and the one event loop

Flask serves each request on a thread with no event loop, and socialchimp is
async. The blueprint starts **one** loop, on one background thread, for the
whole process, and hands every request's work to it. `run` is that bridge,
and it is public so your own views use the same loop:

```python
from socialchimp.contrib.flask import blueprint, run

app.register_blueprint(
    blueprint(
        sc,
        redirect_uri=f"{PUBLIC_URL}/social/callback/{{platform}}",
        # No secrets or setup_tokens: YouTube pushes nothing socialchimp
        # uses. See "no pushed updates" below.
    ),
    url_prefix="/social",
)


@app.post("/shorts")
def publish_a_short() -> dict[str, str | None]:
    result = run(sc.account(request.form["connection_id"]).post(post))
    ...
```

The obvious alternative - `asyncio.run(...)` per request - is the wrong one.
It builds a loop, runs the work and throws the loop away, and the HTTP
connections socialchimp pooled belong to that loop. The next request finds a
pool full of sockets from a loop that no longer exists.

### Sign-in, and the channel question

```
GET  /social/connect/youtube?state=user-42
GET  /social/callback/youtube            <- answers with the channels
POST /social/choose/youtube              <- state + account_id
```

One Google account can own several channels, so `finish_login` never finishes
on its own: it answers `{"step": "choose_account", "state": ..., "options":
[...]}`. Show the list, then post the id back to `/social/choose/youtube`
along with the same `state`.

It asks even when there is only one channel, because a person can add a
second tomorrow and an app that only handled the one-channel case breaks that
day.

Two things happening under the surface that you should know about:

- **The sign-in address carries `access_type=offline` and `prompt=consent`.**
  Leave either one out and Google hands back no refresh token at all, the
  access token dies in an hour, and the person is signed out by lunchtime.
  socialchimp sends both. If you ever build the address by hand, do not drop
  them - and socialchimp refuses a sign-in that came back without a refresh
  token rather than saving a connection that will break.
- **Google does not rotate refresh tokens.** A renewal answers with a new
  access token and nothing else, so the refresh token already on the
  connection is carried across. This is the opposite of TikTok, and you do
  not have to care - but it is why nothing here needs a shared lock as badly
  as TikTok does.

### Uploading

```python
@app.post("/shorts")
def publish_a_short() -> dict[str, str | None]:
    form = request.form
    post = Post(
        text=form.get("description", ""),
        media=(Media.from_file(form["path"]),),
        options={
            "title": form["title"],
            "made_for_kids": form.get("made_for_kids") == "yes",
            "privacy_status": form.get("privacy_status", "private"),
        },
    )
    result = run(sc.account(form["connection_id"]).post(post))
    return {
        "video_id": result.id,
        "url": result.url,
        "state": result.state.name,  # PROCESSING, not DONE
    }
```

`Media.from_file` reads nothing when you call it. YouTube takes the video a
piece at a time - 8 MB by default, and it must be a multiple of 256 KB - and
socialchimp reads the file as it goes, so a four gigabyte video costs one
piece of memory rather than four gigabytes. It also uses the number YouTube
reports after each piece rather than assuming, which is the whole point of
uploading this way: a dropped connection resumes from where YouTube actually
got to, instead of leaving a hole in the middle of the video.

### Private by default, on purpose

**A video with no `privacy_status` is uploaded private.** Making somebody's
video public by accident cannot be undone - the notification has already
gone out, the subscribers have already seen it - so the quiet default is the
safe one. Say `"public"` when you mean it. The three values are `private`,
`unlisted`, `public`.

YouTube can schedule, through `Post.publish_at`, and there is one rule
socialchimp enforces first: a video with a publishing time **must** be
private until then. Anything else is visible straight away, which is the
opposite of scheduling it.

```python
Post(..., publish_at=friday, options={"title": ..., "made_for_kids": False})
# privacy_status left out -> "private", which is what YouTube wants
```

### `publish()` says PROCESSING, not DONE

Taking the bytes is not publishing. Encoding happens afterwards and takes
minutes or, for a long video, hours. So:

| State | When |
|---|---|
| `PROCESSING` | The usual answer from `publish()`. YouTube has the file and is working on it. |
| `SCHEDULED` | You gave a `publish_at`. |
| `DONE` | From `check_state`, once YouTube has finished. |
| `FAILED` | YouTube gave up on it, or rejected it, or it was deleted. |

```python
@app.get("/shorts/<connection_id>/<video_id>")
def how_is_it_going(connection_id: str, video_id: str) -> dict[str, str | None]:
    result = run(sc.account(connection_id).check_state(video_id))
    return {"state": result.state.name, "url": result.url}
```

The token is renewed first, the same as every other call on an `Account`.

It costs **one unit** of the daily quota, against an upload's 1,600 - so it
is cheap enough to put on a timer, unlike the upload.

### Quota is not a rate limit

This is the one to get right, because the usual instinct is exactly wrong.

A Google Cloud project gets **10,000 units a day**. An upload costs about
**1,600**. So roughly **six uploads a day** on the default allowance, and
every other call you make eats into the same pot.

Running out arrives as an HTTP 403 with `quotaExceeded` in the body, which
reads like a permission problem. socialchimp turns it into a
`RateLimitError` - with a message that says plainly what it is:

```
YouTube's daily quota for this Google Cloud project is used up
(quotaExceeded). This is a daily allowance, not a request to slow down: a
project gets 10,000 units a day and an upload costs about 1,600, so roughly
six uploads. It starts again at midnight Pacific time, and trying again in a
few seconds only spends what is left. Ask for more quota in the Google Cloud
console, or wait for the reset.
```

**`retry_after` is deliberately `None` on this one.** Google does not say
when, and a number there would have every caller retrying inside the same day
and burning what is left of it. Whatever backoff your job runner does, this
is the error to make it stop for:

```python
@app.errorhandler(SocialChimpError)
def explain(refused: SocialChimpError) -> tuple[Response, int]:
    if isinstance(refused, RateLimitError):
        advice = "Daily quota. Resets at midnight Pacific. Do not retry."
        return jsonify({"error": str(refused), "what_now": advice}), 429
    return jsonify({"error": str(refused)}), 400
```

`uploadLimitExceeded` is the same idea for videos rather than requests - the
channel has uploaded as many as YouTube allows it today - and it comes back
the same way, for the same reason.

If six a day is not enough, the answer is a quota increase request in the
Google Cloud console, and it is reviewed by a person.

### Comments, if you want them

YouTube has WebSub and it announces new uploads on a channel - not comments,
not likes, nothing else. Comments are what apps actually want, so
`Feature.PUSH_UPDATES` is off and socialchimp reads them on a timer instead:

```python
from socialchimp import Dispatcher, Poller, UpdateKind


async def check_for_comments() -> Sequence[Update]:
    return await sc.account(connection_id).fetch_updates(since=marker)


poller = Poller(
    fetch=check_for_comments,
    deliver=dispatcher.deliver,
    every_seconds=300,
    save_marker=write_the_marker,
)
await poller.run_forever()
```

Your handlers see the same `Update` objects a pushing network would have
produced, and cannot tell the difference. Two things to watch: each round
costs quota, and YouTube pages comments rather than filtering them by time -
so check often enough that one page covers the gap.

---

## Running it

```bash
pip install "socialchimp[youtube,flask]"
```

```bash
export GOOGLE_CLIENT_ID=...
export GOOGLE_CLIENT_SECRET=...

uv run --with flask flask --app examples/youtube_shorts_flask run
```

Then:

1. Open `http://localhost:5000/social/connect/youtube` and approve. You must
   be one of the test users on the consent screen until Google's review
   passes.
2. The callback answers with your channels. Post the one you want back to
   `/social/choose/youtube` as `state` and `account_id`.
3. `POST /shorts` with `connection_id`, `path`, `title` and
   `made_for_kids=yes|no`.
4. `GET /shorts/<connection_id>/<video_id>` a minute later.

---

## What will go wrong, and why

### "No title" on a post that has words in it

`Post.text` is the description. The title is `options["title"]`, and it is
required. The message says exactly this, by name.

### `made_for_kids` refused rather than defaulted

On purpose. See [above](#title-and-made_for_kids-are-required-and-posttext-is-the-description).

### The video went up private and nobody asked for that

They did not ask for public either. A missing `privacy_status` is `private`,
because the mistake in that direction is recoverable and the other one is
not. Set `"privacy_status": "public"`.

### It is not a Short

The file is landscape, or over three minutes, or both. Nothing in the API
changes that. Check the file before you upload it.

### `RateLimitError` and retrying makes it worse

Quota, not rate limiting. Six uploads a day; resets at midnight Pacific.
Every retry spends more of what is left. See [above](#quota-is-not-a-rate-limit).

### 403 on everything, from the first call

Usually the YouTube Data API v3 is not turned on for the project. It is a
separate switch from creating the project.

### Everybody except you is turned away at Google's sign-in page

Google's review has not passed. Add them as test users on the consent screen
while you wait.

### Signed in, and then "no YouTube channel"

A Google account and a YouTube channel are different things. They make one at
<https://www.youtube.com/create_channel> and sign in again.

### Everyone gets signed out after an hour

Google issued no refresh token. That happens when the sign-in address is
missing `access_type=offline`, or missing `prompt=consent` when the person
has approved the app before. socialchimp sends both and refuses to save a
connection that came back without a refresh token, so the usual cause is a
sign-in started somewhere else, or an address built by hand.

### `tags` behaved oddly

It has to be a list. One string on its own is read as a list of letters -
which YouTube accepts without complaint - so socialchimp refuses it:

```
InvalidPostError: tags is 'python', but it has to be a list of words:
['python', 'async']. One string on its own is read as a list of letters. All
of them together may be at most 500 characters.
```

### Deleting a video

Not here. Deleting needs the `youtube.force-ssl` permission, which is wider
than uploading and gets a harder look in Google's review, and it is not worth
asking every app to request it for something most never do. Use `direct` if
you need it and have the permission.

---

## Elsewhere

- [Networks](../platforms.md#youtube) - the short list.
- [Frameworks](../frameworks.md#flask) - the ready-made routes.
- [Facebook from Django](facebook-django.md) and
  [TikTok from FastAPI](tiktok-fastapi.md) - the other two use cases.
