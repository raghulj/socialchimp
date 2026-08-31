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
an `Update`. See `socialchimp.events`.

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

### Posting to several accounts at once

```python
job = await sc.post_to_many([page_id, tiktok_id], Post(text="Hi"))

for result in job.succeeded:
    print("posted:", result.url)
for failure in job.failed:
    print("failed:", failure.connection_id, failure.error)
```

Every account is sent to at the same time and every one gets its own outcome.
One network being down never hides the three that worked. There is a runnable
version in [`examples/post_to_many.py`](../examples/post_to_many.py).

### Settings that belong to one network only

Anything that exists on one network and nowhere else goes in `Post.options`:

```python
Post(text="Read this", options={"link": "https://example.com/a"})  # Facebook
Post(media=(clip,), options={"title": "My video", "made_for_kids": False})  # YouTube
```

Each network's page in [platforms](platforms.md) lists what it takes, and a
name that network has never heard of is refused before anything is sent, with
the accepted names in the message. Posting one `Post` to several networks at
once? `post_to_many(..., options_per_platform={"youtube": {...}})` adds them
per network without changing the post you passed in.

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

Both live in `socialchimp.testing`, behind an extra
(`pip install "socialchimp[testing]"`), because pytest is not something an
app should need installed to post a picture.

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
