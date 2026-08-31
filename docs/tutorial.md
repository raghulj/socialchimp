# Tutorial

For somebody who has built a Django or Flask app before and has never touched
a social network's API. No knowledge of tokens, sign-in flows or webhooks is
assumed. By the end you will know what the four moving parts are, how to sign
somebody in, how to write the storage class, and what socialchimp does when a
network cannot do the thing you asked for.

[Getting started](getting-started.md) is the ten-minute version, and does not
explain much. This one explains.

- [What problem this solves](#what-problem-this-solves)
- [The four ideas](#the-four-ideas)
- [Signing somebody in](#signing-somebody-in)
- [Why the library never touches your database](#why-the-library-never-touches-your-database)
- [Your first post](#your-first-post)
- [When a network cannot do it](#when-a-network-cannot-do-it)
- [Handling errors, and recording them](#handling-errors-and-recording-them)
- [Keeping a token working](#keeping-a-token-working)
- [When somebody has to sign in again](#when-somebody-has-to-sign-in-again)
- [Adding a post](#adding-a-post)
- [Adding a post with pictures](#adding-a-post-with-pictures)
- [Adding a video](#adding-a-video)
- [How the classes fit together](#how-the-classes-fit-together)
- [Where to go next](#where-to-go-next)

---

## What problem this solves

Posting a line of text to a Facebook Page from your own app is not one HTTP
request. Before the first byte goes anywhere, this has to be true:

- **Your app exists, on that network.** Meta, Google and TikTok all make you
  create it by hand in a developer portal, and all three review it before its
  posting permissions work for anybody outside your own account. Meta also
  wants documents proving the company behind it is real. That review is
  measured in weeks, not minutes.
- **A person has said yes.** Your app cannot post as somebody; it posts as
  somebody who has approved it, and approving means sending them off to
  Facebook and getting them back again.
- **You know which page.** A person is not a Page. Somebody can manage a
  dozen, and Facebook will not guess which one you meant.
- **You are holding a token that still works.** Most networks hand out a
  token that dies in an hour, and a second token that buys a new one. Some
  networks throw away the second token every time you use it, so two of your
  own workers renewing at the same moment can disconnect an account for good.
- **The post is one that network will take.** Bluesky counts letters the way
  a person does; Threads counts bytes; TikTok counts the way Java does, where
  an emoji is two. YouTube has no text-only post at all. TikTok cannot
  schedule. Facebook will not schedule anything less than ten minutes ahead
  or more than 75 days out.

Every one of those is different on every network, and most of them fail
quietly. TikTok will accept a public post from an app it has not audited,
answer that it worked, show the video to its author, and hide it from
everybody else. There is no error to find.

socialchimp is the part in the middle. You write one `Post` and one storage
class. It finds the right network's code, keeps the token working, checks the
post against that network's actual rules before spending a request, and tells
you plainly when a network cannot do what you asked instead of doing
something else.

What it does not do: pretend the differences are not there. Where a network
genuinely cannot schedule, you get a refusal that names the network and the
missing feature. That is the whole design.

---

## The four ideas

Four words do most of the work. Learn these and the rest of the library reads
easily.

### A connection

One social account that one person has linked to your app. A Facebook Page, a
TikTok account, a YouTube channel. It is a frozen `Connection` object holding
the token, who the account is, and anything that one network needs to
remember - a page id, a channel id.

**You choose its `id`.** The platforms name it after the account, so
Facebook's is `facebook:<page id>` and TikTok's is `tiktok:<open id>`. That
matters for webhooks: Meta tells you which page something happened on, not
which of your rows, and naming the connection after the page is what makes
the two line up without a table of your own.

```python
connection.id  # "facebook:101234567890"
connection.platform  # "facebook"
connection.account_name  # "Bench & Bloom"   - show this to a person
connection.extra  # {"page_id": ..., "page_name": ..., ...}
```

### Storage

Your database. socialchimp never opens it, never creates a table and never
runs a migration. It calls five methods you write, and where the rows go is
entirely your business. That is the reason one library serves Django,
FastAPI, Flask and no framework at all.

### A platform

One network's file: `facebook.py`, `tiktok.py`, `youtube.py`. It knows how to
sign somebody in to that network, keep their token working, and publish for
them. It also states, once, what that network can and cannot do - as a
`Feature` flag set - and that statement is what socialchimp trusts.

You almost never build one. `SocialChimp` finds it from the name you pass:

```python
await sc.start_login("facebook", redirect_uri=...)
```

Nothing is imported until it is asked for, so having nine networks installed
costs nothing at startup.

### An update

Something that happened: a comment, a like, a post finishing its encoding,
somebody removing your app. Every one arrives as the same `Update` object,
whichever network it came from and however it got to you.

There are two ways it can get to you, and **your code cannot tell them
apart**:

- **Pushed.** The network sends a request to a URL of yours when something
  happens. Facebook and TikTok do this. The request is signed, and the
  signature is checked before you see anything.
- **Found on a timer.** The network has no way to tell you, so socialchimp
  asks it every so often, works out which items are new, and hands them on.
  YouTube's comments work this way, and so do LinkedIn, Pinterest, Reddit and
  Tumblr.

Either way you register handlers on a `Dispatcher` and they are called with
an `Update`. Every handler runs even if an earlier one raised, and if any of
them did, the update is **not** written down as handled and `deliver` raises
an `ExceptionGroup` of the failures - so the network's retry is a real second
chance and your app gets to decide what a broken handler means. See
`socialchimp.events`.

---

## Signing somebody in

This is the part beginners get wrong, so it is worth going slowly.

### Why it is two halves

Your app cannot ask somebody for their Facebook password. Instead it sends
them **to Facebook**, Facebook asks them whether your app may act for them,
and Facebook sends them back to your app with a short code. Your server then
swaps that code for a token, on its own, without the person's browser being
involved.

So a sign-in is two separate HTTP requests to *your* app, with a trip through
somebody else's website in between:

```
GET  /connect/facebook    <- you send them away
     ... they approve on facebook.com ...
GET  /callback/facebook   <- Facebook sends them back, with ?code=...
```

Two requests means two chances to be handled by two different web workers.
Anything the first request needs to hand to the second has to travel through
something both can see - a session, a Redis key, a small table. Not a
variable. Holding it in memory works on your laptop and fails the day you run
a second worker.

### The four things `start_login` and `finish_login` can hand back

Both calls return a `LoginStep`, which is one of four things. Match on it.
Getting this right once means every network works, including the ones you
have not added yet.

```python
from socialchimp.platform import (
    AskForDetails,
    ChooseAccount,
    Finished,
    SendToNetwork,
)

step = await sc.start_login("facebook", redirect_uri=CALLBACK)

match step:
    case SendToNetwork():
        ...
    case AskForDetails():
        ...
    case ChooseAccount():
        ...
    case Finished():
        ...
```

#### `SendToNetwork` - "send them to the network"

The usual answer from `start_login`. Facebook, TikTok, YouTube, Mastodon and
X all give you this.

```python
step.url  # redirect their browser here
step.state  # the value that will come back in the callback
step.remember  # keep this with their session; finish_login needs it back
```

Your job: redirect to `step.url`, and **write `step.remember` down somewhere
the callback request can read it**, filed under `step.state`. It holds
whatever the platform needs on the way back - usually the secret half of a
PKCE pair, which is a secret your server keeps while only its hash travels to
the network. socialchimp cannot keep it for you, for the two-workers reason
above.

`state` is the one value that makes the round trip through the network, so it
is the natural key to file everything under. Choose your own if you want to
know which of your users came back:

```python
step = await sc.start_login("facebook", redirect_uri=CALLBACK, state=f"user-{me.id}")
```

#### `AskForDetails` - "there is nowhere to send them"

Some networks have no sign-in page at all. Bluesky uses an app password;
Discord and Telegram use a bot token somebody pastes in. `start_login`
answers with a list of fields instead of a URL.

```python
step.fields  # LoginField(name=..., label=..., secret=..., help_text=...)
step.help_url  # a page explaining where to get them - worth linking to
```

Your job: show one box per field, in the order given, hide the ones marked
`secret`, and pass what the person typed straight to `finish_login` as
`callback`, keyed by each field's `name`.

Nothing leaves your app on this route, so nobody goes anywhere and `state` is
not used. If your sign-in code assumes every step carries a state back, this
is the one that will not.

#### `ChooseAccount` - "which one?"

A pause. The person has approved your app, but the network needs to know
which account inside it you mean. Facebook asks which Page, Instagram which
business account, YouTube which channel. It comes back from `finish_login`.

```python
step.options  # (AccountChoice(id=..., name=..., kind="page"), ...)
step.resume_token  # hand this straight back. Treat it as a secret.
```

Your job: show `options` to the person, then call `sc.choose(...)` with the
id they picked and the `resume_token` handed straight back:

```python
step = await sc.choose(
    "facebook",
    account_id=whichever_they_picked,
    resume_token=resume_token,
    redirect_uri=CALLBACK,  # the same one the sign-in started with
    remember=remember,  # the same one finish_login was given
)
```

Three things about this one:

- **It asks even when there is only one page.** That is on purpose. Choosing
  silently would leave your app with two paths through the sign-in, one of
  which almost never runs and is therefore never right - and somebody with
  two pages would find out which one got connected when a post appeared on
  it. One page today is two pages next year.
- **`resume_token` is a secret.** On Facebook it carries the person's own
  access token, because the code Facebook sent back can only be swapped once
  and that swap happens before the person picks. Keep it with their session,
  exactly the way you keep `remember`. Never in a URL, never in a hidden form
  field, never in a log.
- **`sc.choose` is a third request to your app.** So there are three halves,
  not two, and all three need the same `redirect_uri`, `scopes` and `host`
  the sign-in started with.

#### `Finished` - "done"

```python
step.connection  # already saved through your Storage
```

socialchimp writes it through `Storage.save_connection` before handing it
back, so there is nothing for you to save. Keep `step.connection.id`
somewhere in your own tables against whichever of your users this belongs to.

### Putting it together

```python
CALLBACK = "https://app.example/social/callback/facebook"

# Request one.
step = await sc.start_login("facebook", redirect_uri=CALLBACK, state=state)
session["socialchimp"] = {"state": step.state, "remember": step.remember}
return redirect(step.url)

# Request two: Facebook sent them back with ?code=...&state=...
kept = session["socialchimp"]
step = await sc.finish_login(
    "facebook",
    callback=dict(request.GET),  # or request.query_params, or request.args
    redirect_uri=CALLBACK,
    state=kept["state"],
    remember=kept["remember"],
)
if isinstance(step, ChooseAccount):
    session["socialchimp"]["resume_token"] = step.resume_token
    return render("pick-a-page.html", options=step.options)

# Request three: they picked one.
step = await sc.choose(
    "facebook",
    account_id=request.POST["account_id"],
    resume_token=session["socialchimp"]["resume_token"],
    redirect_uri=CALLBACK,
    state=kept["state"],
    remember=kept["remember"],
)
connection = step.connection
```

If you would rather not write those three routes at all, each framework has
them ready-made - see [frameworks](frameworks.md). They are a few lines
around exactly the calls above, so reading this first is not wasted.

---

## Why the library never touches your database

socialchimp has no models, no migrations and no opinion about your schema. It
never has, on any of the three frameworks.

The reason is not purity. It is that a library that owns a table owns your
migrations, your naming, your database engine and your deployment order, and
it can then only serve one framework properly. Five methods cost you an hour
once, and after that your rows are yours: encrypt the token column, put it in
a different database, name the table whatever your team names tables.

What you write is a class with five methods. You do **not** import a base
class and inherit from it. You write a class that has the right methods, and
socialchimp accepts it because it has them. That is what a Protocol means
here, and it is covered properly in
[How the classes fit together](#how-the-classes-fit-together) below.

The five methods, and the three rules that matter:

```python
class MyStorage:
    async def get_connection(self, connection_id: str) -> Connection | None: ...
    async def save_connection(self, connection: Connection) -> None: ...
    async def delete_connection(self, connection_id: str) -> None: ...
    async def get_app(
        self, platform: str, host: str | None
    ) -> AppCredentials | None: ...
    async def save_app(self, app: AppCredentials) -> None: ...
```

- **Return `None` when there is nothing.** Do not raise. "Not connected yet"
  is a normal state, not a fault.
- **`delete_connection` is quiet** when the row is already gone. Retries
  happen.
- **`get_app` is keyed by platform *and* host.** Every Mastodon server needs
  its own app, so `("mastodon", "mastodon.social")` and
  `("mastodon", "fosstodon.org")` are different rows. For Facebook, TikTok
  and YouTube the host is always `None`.

`save_connection` is called twice as often as you would expect: once when the
account is connected, and again after **every token renewal**. It has to
replace, not insert. On TikTok and Bluesky, a renewal that never reaches your
database disconnects the account for good, because the old refresh token has
already stopped working.

There is a full worked storage class, over sqlite, in
[`examples/facebook_django/page_post_demo.py`](../examples/facebook_django/page_post_demo.py).

---

## Your first post

Whatever the network, this is the shape:

```python
from socialchimp import Media, Post, SocialChimp

sc = SocialChimp(storage=MyStorage())  # one for the whole process

account = sc.account(connection_id)  # cheap; reads nothing
result = await account.post(
    Post(
        text="We are open until six today.",
        media=(Media.from_file("shop.jpg", alt_text="A shop front"),),
    )
)
print(result.id, result.url, result.state)
```

Six things happen inside that `post()`, in this order:

1. Your `get_connection` is called.
2. If the token is nearly out of time it is renewed, under a lock, and the
   new one is written back through your `save_connection`. So a post never
   fails because a token aged out while the job sat in a queue.
3. The network's current limits are looked up.
4. The post is checked against those limits and against the network's
   features - length, counted the way that network counts; how many pictures;
   whether it can schedule at all. **This costs no request.** An over-long
   post is refused here, in plain words, rather than by the network in its
   own words at the cost of one of your rate-limited requests.
5. The post is published.
6. You get a `PostResult`.

### One `SocialChimp` for the whole process

```python
sc = SocialChimp(storage=MyStorage())
```

Build it once and keep it. The locks that stop two workers renewing the same
token at the same moment live on it, so a new one per request protects
nothing. Django's helper does this for you with
`socialchimp.contrib.django.get_client()`.

### Read the state, do not assume it worked

`result.state` is a `PostState`, and `DONE` is only one of five answers:

| State | What it means | What to do |
|---|---|---|
| `DONE` | Live now. | Nothing. `result.url` works. |
| `SCHEDULED` | The network took it and will publish it later. | Nothing. There is usually no `url` yet - there is nothing on the page to link to. |
| `PROCESSING` | The network is still encoding it. | Check back later, or wait to be told. YouTube and TikTok both do this. |
| `WAITING_FOR_PERSON` | The network has finished. A human has to tap a button. | **Stop checking.** Tell the person instead. |
| `FAILED` | The network gave up. | Look at `result.raw`. |

`WAITING_FOR_PERSON` is the one worth learning early. A TikTok video sent to
somebody's drafts is not "still processing" - TikTok has done everything it
is ever going to do, and the video changes when that person opens the app,
which may be never. An app that polls this one polls forever.

### Posting to several accounts is your loop

socialchimp posts as one account at a time. To post to several, you write the
loop:

```python
from socialchimp import SocialChimpError

for connection_id in (page_id, tiktok_id):
    try:
        result = await sc.account(connection_id).post(Post(text="Hi"))
        print("posted:", result.url)
    except SocialChimpError as refused:
        print("failed:", connection_id, refused)
```

There is no `post_to_many`, and that is deliberate rather than missing.

The rule this library follows is: **socialchimp raises, your app handles.**
When something goes wrong you get an error at the call that caused it, and
socialchimp stops. It does not write the failure down and carry on, because
carrying on is a decision, and it is not ours to make. Only your app knows
whether TikTok refusing should stop the Facebook post as well, whether this
failure belongs in a row somewhere for a worker to retry at midnight, or
whether it is the kind of thing a person needs telling about tonight.

The loop above chooses to carry on. Delete the `try` and it chooses to stop
at the first failure. Both are one line of difference, and both are choices
you can only make with a view of your own app - which is exactly why the
library does not make them.

What socialchimp does give you is one set of errors to catch. `SocialChimpError`
is the base of all of them, so the loop above handles nine networks without
knowing anything about nine networks' error formats. Catch `RateLimitError`,
`AuthError` or `NotSupportedError` when you want to treat one kind
differently - retry the first, reconnect the account for the second, and never
retry the third.

There is a runnable version, with one network refusing, in
[`examples/post_to_each.py`](../examples/post_to_each.py).

### Settings that belong to one network only

Anything that exists on one network and nowhere else goes in `Post.options`:

```python
Post(text="Read this", options={"link": "https://example.com/a"})  # Facebook
Post(media=(clip,), options={"title": "My video", "made_for_kids": False})  # YouTube
```

Each network's page in [platforms](platforms.md) lists what it takes, and a
name that network has never heard of is refused before anything is sent, with
the accepted names in the message. Sending the same words to several networks
that each want different options? Build the post each one needs inside your
loop - `dataclasses.replace` does it without changing the post you started
from:

```python
from dataclasses import replace

extras = {"youtube": {"title": "My video", "made_for_kids": False}}

for connection_id in (youtube_id, tiktok_id):
    account = sc.account(connection_id)
    platform = (await account.connection()).platform
    extra = extras.get(platform, {})
    await account.post(replace(post, options={**post.options, **extra}))
```

---

## When a network cannot do it

socialchimp refuses rather than guessing. This is a deliberate choice and it
is worth understanding, because the alternative is worse in a way you would
not find out about for weeks.

```python
from socialchimp import NotSupportedError

try:
    await account.post(Post(text="Later", publish_at=friday))
except NotSupportedError as refused:
    print(refused)
    # "tiktok does not support scheduling posts."
```

The alternative would be to post it now. That is a library deciding, on your
behalf, that "publish on Friday" and "publish immediately" are close enough -
and your user finds out when Friday's announcement went out on Tuesday.

The same applies to `Feature.POST_TEXT`. YouTube has no text-only post
anywhere in its API, so `Post(text="hello")` to YouTube is refused with a
message that says so and says what to attach instead. It is not turned into a
video of a title card.

You can ask in advance:

```python
from socialchimp import Feature

features = sc.platform_for("tiktok").features
Feature.SCHEDULE in features  # False
Feature.POST_TEXT in features  # False
Feature.POST_VIDEO in features  # True
```

`Feature` is fixed - a network either has the capability or it does not.
`Limits` is looked up while running, because it genuinely changes: a Mastodon
server's post length is set by whoever runs that server, and Instagram counts
down how many posts are left today.

```python
limits = await account.limits()
limits.max_text_length  # 2200 on TikTok
limits.text_counted_in  # TextCount.UTF16_UNITS - an emoji is two
limits.max_video_bytes
limits.posts_left_today  # None where the network does not say
```

The two errors to catch are `NotSupportedError` (this network never can) and
`InvalidPostError` (this post breaks a rule). Neither is worth retrying.
`NetworkError` is - it means the network never answered. The full list is in
`socialchimp.errors`, and `SocialChimpError` catches all of them.

### Doing something socialchimp has not modelled

Every network has corners nobody has covered. You are not stuck:

```python
reply = await account.direct.post(
    "/1234567890/comments",
    data={"message": "Thanks!"},
)
```

The token is renewed first, retries and rate limits still apply, and the
request goes to the right address for that account. Only the request itself
is yours.

---

## Handling errors, and recording them

The rule from [your first post](#your-first-post), said once more because
everything below follows from it: **socialchimp raises, your application
handles.** The library never writes a failure down, never retries in the
background, and never decides an account is finished. It reports what went
wrong at the call that caused it, and stops there.

### Catch everything, or catch one thing

`SocialChimpError` is the base of every error socialchimp raises. Catch it
when the answer is the same whatever went wrong:

```python
import logging

from socialchimp import SocialChimpError

logger = logging.getLogger(__name__)

try:
    result = await sc.account(connection_id).post(post)
except SocialChimpError as refused:
    logger.warning("could not post as %s: %s", connection_id, refused)
```

**Everything** means everything, and from 0.3.0 that is finally true. Until
then a handful of refusals came out of `socialchimp.models` as a bare
`ValueError` - a post with neither text nor media, a datetime with no
timezone, a picture that is only a web address on a network that will not
fetch it - and walked straight past the `except` above. They are an
`InvalidPostError` and a `ConfigError` now.

Both of those are still a `ValueError` as well, so code that noticed the old
behaviour and caught `ValueError` keeps working. You do not need to catch
both; catch `SocialChimpError`.

Catch a specific one when you will **do something different about it**. That
is the only reason to tell them apart. Six `except` clauses that all log and
carry on is five clauses of noise.

### The ones worth telling apart

Every one of these is importable from `socialchimp` itself, and every one of
them wants a different response from your app.

- **`RateLimitError`** - the network is asking you to slow down.
  `refused.retry_after` is the number of seconds it asked for, or `None` when
  it did not say. Wait that long and try the same post again; nothing about
  the post is wrong.
- **`TokenExpiredError`** - the token ran out and socialchimp could not renew
  it. There is nothing your code can do about this one. The person has to
  sign in again, and until they do, every call for that connection fails the
  same way. It has a whole section below.
- **`NotSupportedError`** - this network cannot do that, and never will.
  Asking TikTok to schedule is not a temporary condition. Fix the code or
  skip that network; retrying is pure waste. `refused.what` names the missing
  capability and `refused.suggestion` says what to do instead, when there is
  something.
- **`InvalidPostError`** - the post breaks a rule of the network it was going
  to. Too long, too many pictures, a required option missing. Also a post no
  network would take, such as one with neither text nor media. Fix the post.
  Sending the same bytes again gets the same answer.
- **`NetworkError`** - nobody answered. A dropped connection, a name that
  would not resolve, a request that timed out. socialchimp already tried
  several times before raising this, so it means the network really is
  unreachable rather than slow. Trying again later is reasonable, and it is
  one of the few errors here where it is.
- **`PlatformError`** - the network said no in a way socialchimp has no
  better name for. `refused.raw` holds what the network actually replied, and
  `refused.status_code` the HTTP status when there was one. Read `raw` before
  deciding anything; it is the only place the real reason lives.

### What to do about each

| Error | What it means | What your app should do | Retry? |
|---|---|---|---|
| `RateLimitError` | Slow down. | Wait `retry_after` seconds, then send the same post. | Yes, after the wait |
| `TokenExpiredError` | The token is dead and cannot be renewed. | Mark the connection as needing attention; show the person a reconnect link. | No, not until they sign in |
| `AuthError` | The network would not accept who we say we are. | Usually the same: reconnect the account. | No |
| `NotAllowedError` | Real account, missing permission. | Ask for the right scope the next time somebody connects. | No |
| `NotSupportedError` | This network cannot, ever. | Fix the code, or skip this network. | Never |
| `InvalidPostError` | The post breaks a rule. | Fix the post. | Not unchanged |
| `NotFoundError` | The post, account or page is not there. | Stop referring to it. | No |
| `NetworkError` | Nobody answered. | Try again in a few minutes. | Yes |
| `SignatureError` | The request did not come from the network it claims to. | Answer 401 and nothing else. Do not say which check failed. | No |
| `ConfigError` | Something is wrong on your side: a missing credential, an unknown platform name, an id you never saved, a datetime with no timezone. | Fix it. | No |
| `PlatformError` | The network refused and we have no better name for it. | Read `raw`. | Whatever `raw` says |

### Writing the failure down

An error that only reaches a log is an error nobody acts on. Give failures a
table of their own. The columns that matter:

```sql
CREATE TABLE post_failure (
    id             INTEGER PRIMARY KEY,
    connection_id  TEXT    NOT NULL,  -- which account it was going to
    attempted      TEXT    NOT NULL,  -- enough to build the Post again
    error_type     TEXT    NOT NULL,  -- "RateLimitError", the class name
    message        TEXT    NOT NULL,  -- str(refused), for a person to read
    worth_retrying INTEGER NOT NULL,
    try_again_at   TEXT,              -- when, or NULL for never
    tries          INTEGER NOT NULL DEFAULT 0
);
```

`error_type` holds the **class name**, not the message. Messages are written
for people and change between releases; the class is the part your code is
allowed to depend on. Along with `retry_after`, it is the whole of what a
worker needs later to decide what to do, without ever reading the text.

One function decides when, and `None` means never:

```python
from datetime import UTC, datetime, timedelta

from socialchimp import NetworkError, RateLimitError, SocialChimpError

DEFAULT_RATE_LIMIT_WAIT = 15 * 60


def when_to_try_again(refused: SocialChimpError) -> datetime | None:
    """When this failure is worth another go. None means never."""
    if isinstance(refused, RateLimitError):
        # retry_after is seconds, and None where the network did not say.
        # Not `refused.retry_after or DEFAULT`: a network really can answer
        # nought, and nought is an answer.
        wait = (
            DEFAULT_RATE_LIMIT_WAIT
            if refused.retry_after is None
            else refused.retry_after
        )
        return datetime.now(UTC) + timedelta(seconds=wait)
    if isinstance(refused, NetworkError):
        return datetime.now(UTC) + timedelta(minutes=5)
    # Everything else is the post, the code or the person - none of which
    # gets better on its own.
    return None
```

And the `except` writes the row:

```python
try:
    result = await sc.account(connection_id).post(post)
except SocialChimpError as refused:
    again = when_to_try_again(refused)
    await db.execute(
        "INSERT INTO post_failure (connection_id, attempted, error_type,"
        " message, worth_retrying, try_again_at)"
        " VALUES (:connection, :attempted, :kind, :message, :worth, :when)",
        {
            "connection": connection_id,
            "attempted": json.dumps({"text": post.text}),
            "kind": type(refused).__name__,
            "message": str(refused),
            "worth": again is not None,
            "when": again,
        },
    )
```

Two things this buys you. A support person can answer "why did Tuesday's post
not go out" without reading logs. And a background worker can pick the row up
later, which is the next section.

There is a runnable version of the table, the `except` and the worker, over
sqlite and against a pretend network, in
[`examples/failures_and_retries.py`](../examples/failures_and_retries.py).

### The worker that retries them

Nobody is holding a web request open at three in the morning, so something
has to read those rows. That something is yours: socialchimp has no queue, no
scheduler and no retry loop of its own.

The job is small. Read the rows that are due, try again, and give up after a
while:

```python
MOST_TRIES = 5


async def retry_due_failures() -> None:
    """Send again anything whose try_again_at has passed."""
    rows = await db.fetch_all(
        "SELECT * FROM post_failure WHERE worth_retrying"
        " AND try_again_at <= :now AND tries < :most",
        {"now": datetime.now(UTC), "most": MOST_TRIES},
    )
    for row in rows:
        attempted: dict[str, str] = json.loads(row["attempted"])
        post = Post(text=attempted["text"])
        try:
            await sc.account(row["connection_id"]).post(post)
        except SocialChimpError as refused:
            again = when_to_try_again(refused)
            await db.execute(
                "UPDATE post_failure SET tries = tries + 1, error_type = :kind,"
                " message = :message, worth_retrying = :worth,"
                " try_again_at = :when WHERE id = :id",
                {
                    "id": row["id"],
                    "kind": type(refused).__name__,
                    "message": str(refused),
                    "worth": again is not None,
                    "when": again,
                },
            )
        else:
            await db.execute(
                "DELETE FROM post_failure WHERE id = :id", {"id": row["id"]}
            )
```

The error **class** is what decides, on the way in and on every retry.
`RateLimitError` comes back around with a new `retry_after`.
`NotSupportedError` sets `worth_retrying` to false the first time and is
never read again. `TokenExpiredError` stops being about the post at all - it
is about the account, and the next section deals with it.

`tries < :most` is the part people leave out. Without it a network that is
refusing for a reason you have not modelled gets hit forever.

### Two ways to run it

**With Celery**, which is what most Django and Flask apps already have. A
Celery worker runs synchronous code, so something has to bridge to the async
call. On Django that is `async_to_sync`, which Django already depends on:

```python
# tasks.py
from asgiref.sync import async_to_sync
from celery import shared_task


@shared_task
def retry_failures() -> None:
    async_to_sync(retry_due_failures)()


@shared_task
def warm_tokens() -> None:
    async_to_sync(warm_tokens_running_out)()
```

```python
# celery.py
app.conf.beat_schedule = {
    "retry-failures": {"task": "myapp.tasks.retry_failures", "schedule": 300.0},
    "warm-tokens": {"task": "myapp.tasks.warm_tokens", "schedule": 3600.0},
}
```

On Flask, use the bridge socialchimp already ships:
`socialchimp.contrib.flask.run` hands your call to **one** event loop kept on
a background thread, and waits for the answer.

```python
from socialchimp.contrib.flask import run


@shared_task
def retry_failures() -> None:
    run(retry_due_failures())
```

One shared loop rather than `asyncio.run` per task is not a detail. An HTTP
client holds sockets that belong to the loop it was made on, so a loop built
and thrown away per task leaves a pool full of sockets from a loop that no
longer exists, and every task pays to open new connections. Django's bridge
does start a fresh loop per call, which is why `SocialChimp` keeps one HTTP
client per loop and lets go of it when the loop finishes - that part is
handled for you. The locks are not: **in a worker with more than one process,
pass `make_lock`**, or two of them can renew the same connection at once. See
[keeping a token working](#keeping-a-token-working) below.

**With no queue at all.** Plenty of apps have cron and nothing else, and that
is enough:

```python
# manage.py social_retry, or a script cron runs every five minutes.
import asyncio


def main() -> None:
    asyncio.run(retry_due_failures())
```

One `asyncio.run` for the whole process is fine - the loop lives as long as
the work does.

socialchimp deliberately depends on no queue and no scheduler: everything
above is an ordinary async function that is safe to call from inside any
worker, so arq, dramatiq, RQ, APScheduler or a cron line all work the same
way.

---

## Keeping a token working

Most networks hand out an access token that stops working after an hour or
two, plus a refresh token that buys a new one. socialchimp renews the access
token for you, and for most apps that is the end of the story.

### It happens by itself

Every call that acts as an account - `post`, `limits`, `check_state`,
`fetch_updates`, `delete_post`, anything through `account.direct` - reads the
connection first and renews the token if it is close to running out. There is
no `refresh()` for you to call and no place you are meant to call it from.

Renewal happens **shortly before** the token runs out rather than after a
request has already failed, with about a minute of headroom. That covers a
slow request and a clock that is a little off, and it is why a post does not
fail because a token aged out while the job sat in a queue.

A token with no expiry at all - Mastodon's - is never renewed, because there
is nothing to renew and asking would spend a request for nothing.

### Your `save_connection` will be called, and must work

The renewed token is written through your storage **before** it is handed
back. So `save_connection` runs far more often than the once-per-sign-in you
might expect, and it has to replace rather than insert.

This matters more than it sounds on Bluesky, Pinterest and TikTok, which give
out a brand new refresh token every time the old one is used and stop the old
one working immediately. A renewal that reaches the network but never reaches
your database leaves you holding a refresh token that is already dead, and
that account is disconnected until the person signs in again. A
`save_connection` that swallows its own errors turns a working integration
into a slow leak of accounts.

### The locking, in plain words

Same three networks, same reason. If two of your workers renew one connection
at the same moment, both send the old refresh token, the network honours the
first and throws it away, and the second worker gets back a refusal while the
first worker's new token is the only one that works. Whichever of them writes
last, one of them is wrong, and on those networks wrong means gone.

So a renewal takes a lock first, then reads the connection again - by which
time the other worker has usually finished and there is nothing left to do.

**The default lock only holds inside one Python process.** One web worker is
fine. A web worker and a queue worker is not, and neither is two of anything.
If that is you, hand `SocialChimp` a `make_lock` that returns a lock every
process shares, built on Redis or your database:

```python
sc = SocialChimp(storage=MyStorage(), make_lock=my_redis_lock_for)
```

It is called once per connection id, and the thing it returns needs to work
with `async with` and nothing else. Redis already has one of those, so this
is the whole implementation:

```python
from redis.asyncio import Redis

redis = Redis.from_url(os.environ["REDIS_URL"])


def my_redis_lock_for(connection_id: str) -> Lock:
    # A timeout so a worker that dies holding this does not lock the
    # connection out for good.
    return redis.lock(f"socialchimp:token:{connection_id}", timeout=30)
```

`Lock` there is `socialchimp.tokens.Lock`, which is a Protocol - you are not
importing a base class, and redis-py has never heard of us. The short version
of all this is in
[getting started](getting-started.md#running-more-than-one-process).

### Renewing ahead of time, on a schedule

Renewal happens when you use a connection. An account nobody has posted from
for two months is never used, so nothing ever renews it - and on Pinterest
the refresh token itself expires after sixty days whatever your code does.
The account is then dead and nobody found out.

A job on a timer fixes that. It reads the connections whose token is running
out and warms them, and reading a connection is what renews it:

```python
from socialchimp import SocialChimpError, TokenExpiredError


async def warm_tokens_running_out() -> None:
    """Renew every token that runs out in the next day."""
    rows = await db.fetch_all(
        "SELECT id FROM social_account"
        " WHERE needs_reconnect = 0 AND token_expires_at < :soon",
        {"soon": datetime.now(UTC) + timedelta(days=1)},
    )
    for row in rows:
        try:
            # Reading it is what renews it. There is nothing else to call.
            await sc.account(row["id"]).connection()
        except TokenExpiredError as gone:
            # Renewal is not possible. Stop trying and ask the person.
            await mark_needs_reconnect(row["id"], str(gone))
        except SocialChimpError as refused:
            # A timeout or a 500. Leave it alone; the next run picks it up.
            logger.warning("could not warm %s: %s", row["id"], refused)
```

The query is over **your** table, because socialchimp has none. Two
consequences worth planning for: write `token_expires_at` as a real column in
`save_connection` rather than burying it in a JSON blob, or this job cannot
find anything; and write `refresh_token_expires_at` next to it, because that
is the one Pinterest kills accounts with.

Note what the `except TokenExpiredError` does. It **stops**. Calling again
cannot help, and a job that keeps hammering a dead connection every hour is
how you get rate limited on top of being disconnected.

---

## When somebody has to sign in again

### `TokenExpiredError`, and how it differs from `AuthError`

`TokenExpiredError` is a kind of `AuthError`, so catching `AuthError` catches
both. Tell them apart when you can act on the difference:

- **`AuthError`** is the network refusing who you say you are, for any
  reason. Some of those you can fix - wrong app credentials, an app secret
  you rotated and did not save.
- **`TokenExpiredError`** is narrower and final. socialchimp tried to renew
  and could not. There is nothing left to try in code.

There are three ways to arrive at it, and the message says which:

- **No refresh token at all.** Facebook and Instagram never issue one; on X
  you get none unless your scopes include `offline.access`. When the access
  token runs out, that is the end of it.
- **The refresh token was revoked.** The person removed your app, or changed
  their password on a network that invalidates tokens when they do. The
  network answers the renewal with a refusal.
- **The refresh token itself expired.** Pinterest's lasts sixty days and
  nothing extends it.

### What your app should actually do

Three things, in this order.

**Mark the connection as needing attention.** A column on your own row -
`needs_reconnect`, and the date, and the message. Nothing in socialchimp
tracks this, because socialchimp does not have your table.

**Stop trying.** Every scheduled post, every poll, every warm-up job skips a
connection in that state. This is the part people miss, and it is the part
that matters: retrying a dead token is a request you spend to be told the
same thing again.

**Show the person a reconnect link.** They are the only one who can fix it,
and they cannot fix what they have not been told about.

```python
async def post_or_ask_for_a_reconnect(connection_id: str, post: Post) -> None:
    if await needs_reconnect(connection_id):
        return  # Nothing to do until the person comes back.
    try:
        await sc.account(connection_id).post(post)
    except TokenExpiredError as gone:
        await mark_needs_reconnect(connection_id, str(gone))
        await tell_the_owner(connection_id, str(gone))
```

### Reconnecting is the same sign-in, again

There is no separate repair call. A reconnect is
[signing somebody in](#signing-somebody-in) from the top, with the same
`redirect_uri` and the same scopes, and `Finished.connection` is saved
through your storage over the row that was broken - as long as it comes back
with the same `id`. It will, on the networks that name a connection after the
account: Facebook's is `facebook:<page id>` whether it is the first
connection or the fifth.

So the reconnect link is the connect link:

```html
<a href="/social/connect/pinterest">Reconnect Pinterest</a>
```

Clear `needs_reconnect` when the sign-in finishes. That is your row, so it is
your write.

### Warning somebody in week eight

Where a network tells us when the refresh token dies, the date is on
`Token.refresh_token_expires_at`. Today that is Pinterest and nowhere else,
which is exactly where it is needed.

```python
connection = await sc.account(connection_id).connection()

# A week's notice. Long enough for somebody to get round to it.
if connection.token.refresh_token_expires_within(7 * 24 * 60 * 60):
    await ask_them_to_reconnect(connection_id)
```

`refresh_token_expires_within` answers `False` where the network never told
us, so this is safe to run over every connection you have rather than only
the Pinterest ones. Put it in the same job that warms tokens.

The difference this makes: somebody gets a "reconnect Pinterest" prompt in
week eight while the account still works, instead of discovering in week nine
that a fortnight of posts went nowhere.

---

## Adding a post

The plain case, whichever network:

```python
from socialchimp import Post

result = await sc.account(connection_id).post(Post(text="We open at nine."))
```

`Post` is frozen, and needs text or media - one with neither raises
`InvalidPostError` before it goes anywhere near a network. Everything that
exists on one network only goes in `options`; see
[settings that belong to one network only](#settings-that-belong-to-one-network-only).

### What comes back

A `PostResult`:

| Field | What it holds |
|---|---|
| `result.id` | The network's id for the new post. Always there. |
| `result.url` | A link to the post, where the network gives us one. `None` otherwise. |
| `result.state` | A `PostState` - see below. |
| `result.raw` | The network's untouched reply, for anything we did not model. |
| `result.is_done` | True when `state` is `DONE`. |

`result.url` is `None` more often than people expect. A scheduled post has no
page yet, and some networks return an id without an address you can build a
link from. Keep `result.id` - it is the one thing always there, and it is
what `check_state` and `delete_post` take.

### Why `state` is not always `DONE`

`PostState` has five values, and the full table of what to do about each is
in [read the state](#read-the-state-do-not-assume-it-worked) above. The short
version of why `DONE` is not guaranteed: some networks finish while you wait
and some do not. Mastodon, Bluesky, Facebook text and X answer when the post
is live. YouTube and TikTok answer when they have taken your bytes and are
still working. TikTok drafts answer when they have finished and a person has
not started.

Read `state`. Treating `post()` returning as "it is live" is wrong on
YouTube, on TikTok, on any Facebook video, and on anything scheduled.

### Scheduling, where the network has it

`publish_at` asks a network to publish later. It must have a timezone, or you
get a `ConfigError` naming the field:

```python
from datetime import UTC, datetime, timedelta

friday = datetime.now(UTC) + timedelta(days=3)
result = await account.post(Post(text="Open late on Friday", publish_at=friday))
result.state  # PostState.SCHEDULED
```

**Most networks cannot schedule.** Today only Mastodon, Facebook Pages and
YouTube can; Bluesky, Instagram, TikTok, Threads, X and Pinterest have
nothing in their APIs to ask. socialchimp refuses rather than posting now, so
"publish on Friday" never quietly becomes "publish on Tuesday".

Check first, or be ready to catch:

```python
from socialchimp import Feature, NotSupportedError

if Feature.SCHEDULE in sc.platform_for("tiktok").features:
    ...
```

```python
try:
    await account.post(Post(text="Later", publish_at=friday))
except NotSupportedError as refused:
    print(refused)  # "tiktok does not support scheduling posts."
```

Checking suits a form that greys out a date picker; catching suits a loop over
several accounts where one of them cannot. Where a network can schedule it
still has its own rules - Facebook takes nothing less than ten minutes ahead
or more than 75 days out, and says so as an `InvalidPostError`.

---

## Adding a post with pictures

Three ways to name a file, and which one you use is sometimes the network's
decision rather than yours:

```python
from socialchimp import Media

Media.from_file("shop.jpg", alt_text="A shop front on a wet morning")
Media.from_bytes(uploaded.read(), filename="shop.jpg", alt_text="A shop front")
Media.from_url("https://cdn.example/shop.jpg", alt_text="A shop front")
```

Each works out picture or video from the file ending. For an ending they do
not recognise, say which: `kind=MediaKind.IMAGE`. Attach them as a tuple:

```python
await account.post(
    Post(
        text="New in this week.",
        media=(
            Media.from_file("chair.jpg", alt_text="A red velvet armchair"),
            Media.from_file("lamp.jpg", alt_text="A brass floor lamp"),
        ),
    )
)
```

### Alt text, and why it matters

`alt_text` is the description read aloud to somebody using a screen reader.
Without it they get "image", which is the same as getting nothing. It is one
argument, it is the difference between your app being usable and not, and
almost every app leaves it out because nothing breaks when you do.

Make it a required field in whatever form uploads the picture. If you leave
it to be filled in later it will not be.

Where it goes, honestly: socialchimp sends alt text to Bluesky, Facebook,
Instagram, Mastodon, Threads and X. Pinterest takes it as
`options={"alt_text": ...}` instead, because a pin has one description rather
than one per picture. YouTube and TikTok take video only, and a video has no
alt text. So every network that can carry a description of a picture is
given one. Set it everywhere.

### The differences that will bite

**Instagram cannot take an upload at all.** It has no upload endpoint of any
kind: you give it a web address and it fetches the file itself. So
`Media.from_url(...)` is the only thing that works, and `from_file` and
`from_bytes` are refused with a message saying so rather than being quietly
downloaded and re-hosted by us. Put the file somewhere public first. Threads
works the same way.

**How many pictures differs, and `account.limits()` is where the number
lives.** Bluesky takes four, X four, Pinterest five, Instagram ten, Threads
twenty, and Mastodon whatever the server running it says - which is why this
is looked up rather than written down:

```python
limits = await account.limits()
```

`limits.max_images` and `limits.max_image_bytes` hold the numbers. `None`
means "we do not know", never zero - an unknown limit is not checked.

A post with more than that is refused as an `InvalidPostError` before a
request is spent, with both numbers in the message.

**YouTube and TikTok have no picture post.** `Feature.POST_IMAGE` is off on
both, so pictures are refused with a `NotSupportedError` naming the network.
TikTok does have photo carousels, but through a different call that fetches
each picture from a domain you have proved you own, and that is not written
yet.

Each network's page in [networks](platforms.md) has the rest.

---

## Adding a video

### Use `from_file`, and do not read the file yourself

```python
await account.post(
    Post(
        text="Behind the counter this morning.",
        media=(Media.from_file("clip.mp4"),),
        # YouTube's two required settings. Other networks want other things,
        # or nothing - see below.
        options={"title": "Behind the counter", "made_for_kids": False},
    )
)
```

`Media.from_file` holds a path and nothing else. Nothing is read until the
upload happens, and then the networks that take large files read it in pieces
through `Media.piece`, which seeks into the file and reads a few megabytes at
a time. A four gigabyte video is never four gigabytes of memory.

`Media.from_bytes(path.read_bytes(), filename="clip.mp4")` throws all of
that away: you have already loaded the file, and one request in flight is now
the whole video in your process. Use `from_bytes` for something you genuinely
hold in memory - bytes a person posted to your own API, a file you built -
and `from_file` for anything that came off a disk.

Not every network streams. YouTube, TikTok and X send video a piece at a
time. Facebook and Pinterest send the whole file in one request and really do
cost its own size in memory - Facebook refuses anything over a gigabyte
rather than trying, with a message saying so. Instagram and Threads fetch it
themselves from a web address, so `Media.from_url(...)` is the only thing
they take, exactly as with pictures.

**Which networks take video at all:** Mastodon, Facebook Pages, YouTube,
Instagram, TikTok, Threads, X and Pinterest. Bluesky does not - its video
service needs a separate token and is not written yet, so `Feature.POST_VIDEO`
is off and a video is refused rather than half-attempted.

### The network is not finished when it answers

Encoding takes minutes. Rather than hold your request open, these networks
take the bytes, answer, and carry on:

```python
result = await account.post(Post(media=(clip,), options=youtube_options))
result.state  # PostState.PROCESSING
```

`PROCESSING` means the upload worked and the video is not live yet. Ask again
later:

```python
from socialchimp import PostState

later = await account.check_state(result.id)
if later.state is PostState.DONE:
    ...
```

`check_state` renews the token first, like every other call, so it is safe to
put on a timer for as long as it takes. **YouTube and TikTok are the two that
answer it.** On any other network it raises `NotSupportedError`, because there
is nothing to ask - what `post()` gave you was final.

One rough edge worth knowing: a Facebook video comes back `PROCESSING` too,
because Facebook is still encoding, but Facebook has no `check_state` here.
There is no way to be told when it finished. Treat a Facebook video as sent
rather than live, and watch the Page.

### TikTok drafts never change on their own

A TikTok post sent to somebody's drafts - which is the default, and why is in
[networks](platforms.md#tiktok) - comes back `PostState.WAITING_FOR_PERSON`.

That is not `PROCESSING` and the difference is the whole point. TikTok has
done everything it is ever going to do. The video sits in that person's inbox
until they open the app, add their own caption and publish it, which may be
tomorrow and may be never. `check_state` will answer `WAITING_FOR_PERSON` for
as long as you keep asking.

So: poll `PROCESSING`, never poll `WAITING_FOR_PERSON`. Tell the person
instead. An app that treats the two the same has a job that runs forever
against an account nothing is wrong with.

### YouTube wants a title and an answer about children

Two options are required, and both are refused before anything is sent rather
than defaulted:

```python
await account.post(
    Post(
        text="What the description says.",  # Post.text is the description
        media=(Media.from_file("short.mp4"),),
        options={"title": "Monday morning", "made_for_kids": False},
    )
)
```

`title` is a separate field from the post's words - `Post.text` becomes the
description, which is the thing everybody trips over the first time.
`made_for_kids` is required because Google requires it, and socialchimp will
not guess it for you: it changes what YouTube allows on the video, and
answering wrongly on somebody's behalf is not a default anyone should ship.

A video with no `privacy_status` goes up private, on purpose. Making
somebody's video public by accident cannot be undone.

The rest, including quota and why retrying a `RateLimitError` from YouTube
makes it worse, is in [networks](platforms.md#youtube) and in the
[YouTube Shorts walkthrough](use-cases/youtube-shorts-flask.md).

---

## How the classes fit together

This is the part people conflate, so here it is in one place. There are three
different relationships in this library and they are not interchangeable.

| You... | With | Why |
|---|---|---|
| **implement** | `Storage`, `SyncStorage`, `LoginMemory`, `SeenUpdates`, `Platform` | They are Protocols. Write a class with the right methods. Do not import ours, do not inherit. |
| **subclass** | `FakePlatform`, `PlatformChecks` | Ordinary classes, in `socialchimp.testing`, meant to be extended. |
| **call** | `SocialChimp`, `Account`, `Dispatcher`, `Poller` | Build one and use it. Never subclass these. |

### Implementing a Protocol

A Protocol is a shape, not a parent. mypy checks that your class has the
methods; nothing at runtime demands that you inherited anything.

So this is right:

```python
from socialchimp import Connection, SocialChimp


class MyStorage:  # no base class
    async def get_connection(self, connection_id: str) -> Connection | None: ...


sc = SocialChimp(storage=MyStorage())  # accepted
```

and there is no `class MyStorage(Storage)` to write. You may still import
`Storage` - as a type annotation on a function that takes one, or to have
mypy check your class against it deliberately:

```python
from socialchimp import Storage


def build() -> Storage:  # mypy checks MyStorage fits here
    return MyStorage()
```

The practical difference: if you get a method name or a signature wrong,
mypy tells you at the point where you hand the class to `SocialChimp`, not
inside the library at three in the morning. Run mypy.

#### `Storage`, for real

Five async methods over your database. A real one over sqlite, with all five
written out and the awkward parts commented, is in
[`examples/facebook_django/page_post_demo.py`](../examples/facebook_django/page_post_demo.py).
The shape:

```python
class PageStorage:
    async def get_connection(self, connection_id: str) -> Connection | None:
        row = await db.fetch_one(
            "SELECT * FROM social_account WHERE id = :id", {"id": connection_id}
        )
        return to_connection(row) if row is not None else None

    async def save_connection(self, connection: Connection) -> None:
        await db.execute(UPSERT, fields_of(connection))

    async def delete_connection(self, connection_id: str) -> None:
        await db.execute(
            "DELETE FROM social_account WHERE id = :id", {"id": connection_id}
        )

    async def get_app(
        self, platform: str, host: str | None
    ) -> AppCredentials | None: ...

    async def save_app(self, app: AppCredentials) -> None: ...
```

`Connection` and `Token` are frozen, so nothing you hand back can be changed
underneath you, and a half-applied update is impossible. Unpacking a row into
one and packing one back into a row is all `to_connection` and `fields_of`
ever do.

#### `SyncStorage` and `sync_storage`, when your database layer blocks

Most apps already have a blocking database layer: the Django ORM, a psycopg
cursor, a SQLAlchemy session. There is no reason to rewrite it as async code
to keep socialchimp happy.

Write the same five methods **without `async`** - that shape is `SyncStorage`
- and hand the class to `sync_storage`:

```python
from socialchimp import SocialChimp, sync_storage


class MyStorage:
    def get_connection(self, connection_id: str) -> Connection | None:
        row = session.get(SocialAccount, connection_id)
        return row.to_connection() if row is not None else None

    # ... the other four, also without async


sc = SocialChimp(storage=sync_storage(MyStorage()))
```

`sync_storage` hands back a `Storage`. Each of your five methods then runs on
a spare thread, so a slow query does not stop everything else the event loop
is in the middle of. It lives in `socialchimp.storage` and has nothing to do
with any framework - an app with no framework at all can use it.

#### `orm_storage`, on Django

Django is the one case where "a spare thread" is the wrong answer:

```python
from socialchimp.contrib.django import orm_storage

sc = SocialChimp(storage=orm_storage(MyStorage()))
```

Same five blocking methods, same idea, one difference. Django keeps one
database connection per thread, and a transaction belongs to the thread that
opened it. ORM code run on some pool thread gets a *second* connection,
outside the request's transaction: it cannot see writes the request has not
committed, its own writes land in a transaction nobody rolls back with the
request, and if the request is holding a row lock the two can sit waiting for
each other. `orm_storage` runs your methods back on the thread the request
arrived on, which is where Django expects them.

Use `orm_storage` on Django. Use `sync_storage` everywhere else. Use neither
if your five methods are already `async`.

#### `Platform`, for a network nobody has written yet

The same idea again, one level up: write a class with the right methods and
socialchimp will use it. Publish it as a package that registers itself and
socialchimp finds it, with no change to this repository and no fork.

That is a document of its own - see
[adding a platform](adding-a-platform.md), which covers the seven methods,
the shared helpers you should be using, and the rules that matter more than
they look.

### Subclassing: the two classes meant for it

Both live in `socialchimp.testing`. `FakePlatform` needs nothing beyond
socialchimp itself - pytest is not something an app should need installed to
post a picture, and a fake network is as useful for building an app as for
testing one. `PlatformChecks` runs on pytest and says so if you subclass it
without: `pip install "socialchimp[testing]"`.

**`FakePlatform`** is a network that works with no network. Subclass it to
make it behave like the one you are actually writing against:

```python
from socialchimp import Feature
from socialchimp.platform import AccountChoice
from socialchimp.testing import FakePlatform


class PretendFacebook(FakePlatform):
    def __init__(self) -> None:
        super().__init__(
            name="facebook",
            features=Feature.POST_TEXT | Feature.SCHEDULE | Feature.PUSH_UPDATES,
            accounts=(AccountChoice(id="1001", name="Bench & Bloom", kind="page"),),
        )
```

Giving it `accounts` makes signing in stop to ask which one, the way Facebook
does. Other arguments make it fail on purpose: `publish_fails_with`,
`login_fails_with`, `token_lifetime=None`. Hand it to `SocialChimp` under the
real network's name and every line of your app's code runs unchanged:

```python
sc = SocialChimp(storage=storage, platforms={"facebook": PretendFacebook()})
```

There is a complete worked version in
[`examples/facebook_django/page_post_demo.py`](../examples/facebook_django/page_post_demo.py),
which runs with no credentials at all.

**`PlatformChecks`** is for people writing a platform. Subclass it in a test
file, say how to build your platform, and you inherit every check the
built-in networks pass:

```python
from socialchimp.testing import PlatformChecks


class TestMyPlatform(PlatformChecks):  # the Test... name matters
    def make_platform(self) -> Platform:
        return MyPlatform(transport=self.transport)
```

Call your subclass `Test...`, or pytest collects nothing and you have a green
suite that ran no checks.

### Calling: `SocialChimp` and `Account`

Build one `SocialChimp` per process and call it. Do not subclass it - there
is nothing in it to override that is not already an argument. Everything it
uses is passed in:

```python
sc = SocialChimp(
    storage=MyStorage(),
    platforms={"facebook": my_own_facebook},  # a fake, or one with settings
    make_lock=my_redis_lock_for,  # if you run several processes
    http=my_own_httpx_client,  # if you must
)
```

`sc.account(connection_id)` hands back an `Account`, which is a cheap handle
- it reads nothing until you actually do something, so holding one for a
connection that does not exist yet is fine.

### The things only some networks can do

A few networks do more than the seven things every platform provides. Those
are here too, so nothing sends you off to build a platform by hand.

Anything about one account is on `Account`, where you already are:

```python
result = await account.check_state(post_id)  # YouTube, TikTok
found = await account.fetch_updates(since=marker)  # networks with no push
```

Anything that happens before you know whose account it is takes the network's
name instead, the way `start_login` does - a request Meta pushes to you names
a page, not a connection:

```python
challenge = sc.answer_setup_check("facebook", params, verify_token=TOKEN)
sc.check_signature("facebook", body, headers, secret=APP_SECRET)
for update in sc.read_updates("facebook", body):
    await dispatcher.deliver(update)
```

Each one raises `NotSupportedError`, naming the network, where it cannot -
the same as scheduling on Bluesky.

---

## Where to go next

- [Getting started](getting-started.md) - the short version, and Mastodon,
  which is the only network you can be posting to in five minutes.
- [Networks](platforms.md) - what each one can do, and what will bite.
- [Frameworks](frameworks.md) - the ready-made routes for Django, FastAPI and
  Flask.
- [Adding a platform](adding-a-platform.md) - a network we do not cover.

Three worked use cases, each a whole application:

- [A Facebook Page from the Django admin](use-cases/facebook-django.md) -
  choosing a page, real scheduling, ORM storage, a comment webhook.
- [TikTok video from a FastAPI backend](use-cases/tiktok-fastapi.md) - the
  audit trap, drafts versus profile, large uploads.
- [YouTube Shorts from Flask](use-cases/youtube-shorts-flask.md) - no
  text-only post, required options, quota.
