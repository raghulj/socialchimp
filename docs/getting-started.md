# Getting started

This page takes you from nothing to a post on Mastodon, then shows how the
same code posts to Bluesky.

Mastodon comes first because it is the only network where socialchimp can
register your app for you. No developer portal, no waiting for approval.

## Install

```bash
pip install socialchimp
```

## Step 1 - somewhere to keep connections

socialchimp does not have a database. It hands you data and you decide where
it goes. That is five methods:

```python
from socialchimp import InMemoryStorage

storage = InMemoryStorage()
```

`InMemoryStorage` forgets everything when your program stops, which is fine
while you are trying things out. [Step 6](#step-6-real-storage) replaces it
with your own database.

## Step 2 - register your app

```python
from socialchimp import SocialChimp

sc = SocialChimp(storage=storage)

app = await sc.create_app(
    "mastodon",
    host="mastodon.social",
    name="My App",
    redirect_uri="http://localhost:8000/callback",
)
```

That is a real app on mastodon.social now, and socialchimp has saved its
credentials for you.

**Do this once per server.** Every Mastodon server is separate, so an app
registered on mastodon.social means nothing on fosstodon.org. socialchimp
keeps them apart for you, but you do have to register on each one.

Most networks cannot do this. Ask Facebook and you get a clear refusal
pointing at its developer portal, because Meta reviews every app by hand.

**Bluesky needs no app at all** - no portal, nothing to register, nothing to
save. Skip this step for it entirely; see
[networks with no sign-in page](#networks-with-no-sign-in-page).

## Step 3 - sign someone in

Signing in has two halves, because the person goes off to the network in the
middle.

```python
step = await sc.start_login(
    "mastodon",
    host="mastodon.social",
    redirect_uri="http://localhost:8000/callback",
)

# step is SendToNetwork. Send the person to step.url.
# Keep step.remember with their session - you need it in a moment.
```

When they come back:

```python
step = await sc.finish_login(
    "mastodon",
    host="mastodon.social",
    redirect_uri="http://localhost:8000/callback",
    callback=dict(request.query_params),
    remember=session["socialchimp_remember"],
)

connection = step.connection  # already saved for you
```

### Why `remember` has to travel through your app

It holds a secret that proves the person who comes back is the same one who
left. socialchimp cannot hold it for you, because the two halves can happen
in different processes - your app might send someone away from one web worker
and get them back on another. Keeping it in memory works on your laptop and
fails in production.

Put it in the session, a signed cookie, or a row in your database.

### Some networks ask a question instead

`finish_login` can also come back with `ChooseAccount`. Facebook asks which
page, Instagram which business account, YouTube which channel:

```python
match step:
    case Finished(connection):
        ...  # done
    case ChooseAccount(options, resume_token):
        # Show options to the person, then:
        step = await sc.choose(
            "facebook",
            account_id=whichever_they_picked,
            resume_token=resume_token,
            redirect_uri="http://localhost:8000/callback",
        )
```

### Networks with no sign-in page

Bluesky has no page to send anyone to. You sign in with an app password, so
`start_login` answers with `AskForDetails` instead:

```python
# Nothing was registered first. Bluesky has no developer portal and no app,
# so there is nothing for save_app to hold and nothing to look up.
step = await sc.start_login("bluesky", redirect_uri="unused")

# step.fields tells you what to ask for:
#   handle        "Your Bluesky handle"
#   app_password  "App password"  (secret=True - do not log it)
# step.help_url points at where the person creates one.

step = await sc.finish_login(
    "bluesky",
    redirect_uri="unused",
    callback={"handle": "someone.bsky.social", "app_password": "xxxx-xxxx"},
)
```

Show the fields in the order given, and never write anything marked `secret`
to a log.

**Step 2 does not apply here.** Bluesky's platform says `Feature.NEEDS_NO_APP`,
so socialchimp asks your storage for no credentials before the sign-in and
hands the platform `None` where the app would go. Every other network still
needs its id and secret saved first, and says so plainly when they are
missing.

## Step 4 - post

```python
from socialchimp import Post, Media

account = sc.account(connection.id)

result = await account.post(Post(text="Hello from socialchimp"))
print(result.url)
```

With a picture:

```python
await account.post(
    Post(
        text="Look at this",
        media=(Media.from_file("cat.png", alt_text="A cat asleep on a keyboard"),),
    )
)
```

The same two lines post to Bluesky. You do not write anything per network.

Your token is renewed before every post, so a post never fails just because
a token aged out. Bluesky's tokens last minutes, and you will not notice.

### Posting to more than one account

You loop. socialchimp posts as one account at a time, and there is no call
that spans several:

```python
from socialchimp import SocialChimpError

for connection_id in (mastodon_id, bluesky_id):
    try:
        result = await sc.account(connection_id).post(Post(text="Hi"))
        print("posted:", result.url)
    except SocialChimpError as refused:
        print("failed:", connection_id, refused)
```

That loop is four lines, and writing it yourself is the point. socialchimp
raises when something goes wrong and stops; your app catches it and decides
what happens next. Only your app knows whether Bluesky being down should
stop the Mastodon post too, whether the failure belongs in a table so a
worker can retry it tonight, or whether somebody needs telling. A library
that looped for you would have to pick one of those on your behalf, and it
would pick wrong often enough to matter.

Catching `SocialChimpError` catches everything socialchimp raises. Catch
something narrower - `RateLimitError`, `AuthError`, `NotSupportedError` - when
you want to treat one kind differently, which is the reason those types exist:
one set of errors across every network, instead of nine networks' error
formats.

There is a runnable version in
[`examples/post_to_each.py`](../examples/post_to_each.py).

## Step 5 - when a network cannot do something

socialchimp refuses rather than guessing:

```python
from socialchimp import NotSupportedError

try:
    await account.post(Post(text="Later", publish_at=tomorrow))
except NotSupportedError as refused:
    print(refused)  # "bluesky does not support scheduling posts."
```

Mastodon takes the same post happily. Ask before you post if you need to:

```python
from socialchimp import Feature

if Feature.SCHEDULE in sc.platform_for("bluesky").features:
    ...
```

Limits work the same way, and are looked up while running because they
genuinely change - a Mastodon server's post length is set by whoever runs it:

```python
limits = await account.limits()
print(limits.max_text_length)  # 500 on one server, 5000 on another
```

An over-long post is refused before anything reaches the network, so you get
a clear message instead of the network's error code.

## Step 6 - real storage

Swap `InMemoryStorage` for your own. Five methods, all `async`:

```python
class MyStorage:
    async def get_connection(self, connection_id): ...
    async def save_connection(self, connection): ...
    async def delete_connection(self, connection_id): ...
    async def get_app(self, platform, host): ...
    async def save_app(self, app): ...
```

Three things to get right:

- **`get_connection` returns `None` when there is nothing**, rather than
  raising.
- **`delete_connection` is quiet** when the connection is already gone.
  Retries happen.
- **`get_app` is keyed by platform *and* host.** Mastodon needs its own app
  per server, so `("mastodon", "mastodon.social")` and
  `("mastodon", "fosstodon.org")` are different rows.

`Connection` holds a token. Encrypt it at rest if you can.

### When your database layer is not async

Most apps already have a blocking one - the Django ORM, a psycopg cursor, a
SQLAlchemy session - and there is no reason to rewrite it as async code just
to keep socialchimp happy. Write the same five methods without `async`, and
hand the class to `sync_storage`:

```python
from socialchimp import SocialChimp, sync_storage


class MyStorage:
    def get_connection(self, connection_id):
        row = session.get(SocialAccount, connection_id)
        return row.to_connection() if row else None

    def save_connection(self, connection): ...
    def delete_connection(self, connection_id): ...
    def get_app(self, platform, host): ...
    def save_app(self, app): ...


sc = SocialChimp(storage=sync_storage(MyStorage()))
```

Each call then runs on a spare thread, so a slow query does not hold up
everything else that is in the air at the same time. The three rules above
apply exactly as they do to the async version.

On Django, use `socialchimp.contrib.django.orm_storage` instead. Same idea,
with the one difference that matters there: Django keeps a database
connection per thread and a transaction belongs to the thread that opened it,
so your ORM code is run back on the thread the request arrived on.

### Running more than one process

Tokens are renewed under a lock so that two workers cannot renew the same
connection at once. That matters because Bluesky, Pinterest and TikTok
replace the refresh token every time it is used - the loser of a race ends up
holding a token the network has already thrown away, and that account is
disconnected until the person signs in again.

The default lock only holds inside one process. If you run a web worker and a
queue worker, or several of either, give socialchimp a lock they all share:

```python
sc = SocialChimp(storage=storage, make_lock=my_redis_lock_for)
```

`make_lock` is called once per connection with its id, and must return
something that works with `async with`. Anything else about it is up to you.

## Doing something socialchimp does not cover

Every network has features we have not modelled. You are not stuck:

```python
await account.direct.post(
    "/api/v1/statuses",
    json={"status": "Hello", "visibility": "unlisted"},
)
```

The token is still renewed, retries still happen, rate limits are still
respected. Only the request is yours.

## What next

- [Adding a platform](adding-a-platform.md) - a network we do not support yet
- [The plan](PLAN.md) - what is built and what is coming
